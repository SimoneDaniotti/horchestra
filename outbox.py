#!/usr/bin/env python3
"""Hold messages for an agent while the human is typing into it.

`herdr agent prompt` types the text into the agent's input box and presses
Enter, so a report sent while the human has a half-written prompt there gets
mixed into it and submitted with it. Before sending, look at the input box;
when it holds a draft, queue the message here instead and start a courier
that sends it once the box is empty again (the human sent or cleared it).

Queues live in STATE_DIR/outbox/<pane>.json, one per pane, oldest first.
Messages to a pane that already has a queue wait behind it, so order holds.
At most one courier runs per pane (an exclusive lock); it exits when the
queue is empty.
"""

import contextlib
import fcntl
import json
import os
import re
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.realpath(__file__)))

import herdr_client as hc  # noqa: E402

PROMPT_GLYPHS = ("❯", "›")  # Claude Code, Codex
BOX_LINES = 40  # bottom lines read to find the input box
CHECK_EVERY = 2.0  # seconds between looks at the input box
EMPTY_CHECKS = 2  # consecutive empty looks before sending
GAP_SECONDS = 1.0  # between two queued messages to the same agent
GIVE_UP_SECONDS = 12 * 3600  # a courier stops waiting after this (the queue is kept)
SGR_RE = re.compile(r"\x1b\[([0-9;:]*)m")
ESC_RE = re.compile(r"\x1b(\[[0-9;:?]*[A-Za-z]|\][^\x07\x1b]*(\x07|\x1b\\)|.)")
RULE_CHARS = set("─━═-")


def state_dir():
    explicit = os.environ.get("HORCHESTRA_STATE_DIR") or os.environ.get("AGENTMAP_STATE_DIR")
    return explicit or os.path.expanduser("~/.local/state/horchestra")


def queue_path(pane_id):
    safe = re.sub(r"[^A-Za-z0-9_-]", "_", pane_id)
    return os.path.join(state_dir(), "outbox", safe + ".json")


# ---- reading the input box ---------------------------------------------------


def plain(line):
    return ESC_RE.sub("", line).replace("\xa0", " ")


def bright_text(line):
    """The line's text without dim runs (placeholders and hints are dim)."""
    out, dim, pos = [], False, 0
    for match in SGR_RE.finditer(line):
        if not dim:
            out.append(line[pos:match.start()])
        codes = [c for c in re.split(r"[;:]", match.group(1)) if c] or ["0"]
        for code in codes:
            if code in ("0", "22"):
                dim = False
            elif code == "2":
                dim = True
        pos = match.end()
    if not dim:
        out.append(line[pos:])
    return plain("".join(out))


def is_rule(text):
    """A horizontal rule, possibly with a label in it (Claude's input box edges)."""
    stripped = text.strip()
    return len(stripped) >= 10 and sum(ch in RULE_CHARS for ch in stripped) >= 0.8 * len(stripped)


def draft_in(screen):
    """The text the human has typed into the agent's input box ('' if none).

    `screen` is the pane's bottom buffer with ANSI styling. The input box is
    the last line starting with the prompt glyph; a draft can wrap onto the
    following lines, up to the box's lower edge or a blank line.
    """
    lines = screen.splitlines()
    start = None
    for i in range(len(lines) - 1, -1, -1):
        if plain(lines[i]).lstrip().startswith(PROMPT_GLYPHS):
            start = i
            break
    if start is None:
        return ""
    first = bright_text(lines[start]).lstrip()
    parts = [first[1:].strip()]
    for line in lines[start + 1:]:
        text = plain(line)
        if not text.strip() or is_rule(text):
            break
        parts.append(bright_text(line).strip())
    return " ".join(" ".join(parts).split())


def read_box(pane_id):
    """The pane's bottom screen with styling ('' when it cannot be read).

    `recent` is the tail of the buffer, whatever the human scrolled to.
    Not `detection`: Herdr returns it as plain text even with --format ansi,
    and without the dim styling a suggestion looks like a draft.
    """
    return hc.read_text(pane_id, lines=BOX_LINES, source="recent", fmt="ansi")


def has_draft(pane_id):
    return bool(draft_in(read_box(pane_id)))


# ---- the queue ---------------------------------------------------------------


@contextlib.contextmanager
def locked(path):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "a") as fh:
        fcntl.flock(fh, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(fh, fcntl.LOCK_UN)


def _load(path):
    try:
        with open(path) as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return []
    return [m for m in data if isinstance(m, str)] if isinstance(data, list) else []


def _save(path, messages):
    if not messages:
        with contextlib.suppress(FileNotFoundError):
            os.remove(path)
        return
    tmp = f"{path}.{os.getpid()}.tmp"
    with open(tmp, "w") as fh:
        json.dump(messages, fh, ensure_ascii=False)
    os.replace(tmp, path)


def pending(pane_id):
    path = queue_path(pane_id)
    with locked(path + ".lock"):
        return _load(path)


def enqueue(pane_id, text):
    path = queue_path(pane_id)
    with locked(path + ".lock"):
        _save(path, _load(path) + [text])


def send(pane_id, text, prompt):
    """Send `text` now, or queue it while the human is typing; True when sent.

    `prompt(pane_id, text)` does the sending. Errors from it propagate, so a
    caller still learns when the agent is gone.
    """
    if not pending(pane_id) and not has_draft(pane_id):
        prompt(pane_id, text)
        return True
    enqueue(pane_id, text)
    start_courier(pane_id)
    return False


def start_courier(pane_id):
    path = os.path.join(state_dir(), "outbox.log")
    os.makedirs(state_dir(), exist_ok=True)
    with open(path, "a") as log:
        subprocess.Popen([sys.executable, os.path.realpath(__file__), pane_id],
                         stdin=subprocess.DEVNULL, stdout=log, stderr=log, start_new_session=True)


# ---- the courier -------------------------------------------------------------


def courier(pane_id, prompt, clock=time.monotonic, sleep=time.sleep, log=print):
    """Send the pane's queue once its input box stays empty; returns when done."""
    path = queue_path(pane_id)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path + ".run", "a") as run:
        try:
            fcntl.flock(run, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return  # another courier serves this pane
        try:
            _deliver_queue(pane_id, path, run, prompt, clock, sleep, log)
        finally:
            fcntl.flock(run, fcntl.LOCK_UN)


def _deliver_queue(pane_id, path, run, prompt, clock, sleep, log):
    began, empty = clock(), 0
    while True:
        with locked(path + ".lock"):
            queue = _load(path)
            if not queue:
                # Give up the courier role while no one can enqueue, so a
                # message added right after this starts a courier that runs.
                fcntl.flock(run, fcntl.LOCK_UN)
                return
        if clock() - began > GIVE_UP_SECONDS:
            log(f"{pane_id}: still a draft after {GIVE_UP_SECONDS // 3600}h; "
                f"{len(queue)} message(s) stay queued in {path}")
            return
        empty = 0 if has_draft(pane_id) else empty + 1
        if empty < EMPTY_CHECKS:
            sleep(CHECK_EVERY)
            continue
        try:
            prompt(pane_id, queue[0])
        except hc.HerdrError as exc:
            if pane_gone(pane_id):
                log(f"{pane_id}: pane closed; dropping {len(queue)} queued message(s): {exc}")
                with locked(path + ".lock"):
                    _save(path, [])
                continue
            # E.g. the agent is restarting: keep the message and try again.
            log(f"{pane_id}: could not send a queued message, will retry: {exc}")
            empty = 0
            sleep(CHECK_EVERY)
            continue
        with locked(path + ".lock"):
            _save(path, _load(path)[1:])
        empty = EMPTY_CHECKS - 1  # look once more before the next one
        sleep(GAP_SECONDS)


def pane_gone(pane_id):
    try:
        hc.get_pane(pane_id)
    except hc.HerdrError as exc:
        return exc.code == "pane_not_found"
    return False


def herdr_prompt(pane_id, text):
    hc.call("agent", "prompt", pane_id, text, timeout=60)


if __name__ == "__main__":
    courier(sys.argv[1], herdr_prompt,
            log=lambda msg: print(time.strftime("%Y-%m-%d %H:%M:%S"), msg, flush=True))
