"""Agent activity over time, for the map's activity plot.

Claude Code and Codex write a transcript for every session, and Herdr reports
each pane's session id. Every assistant message (Claude) or response item
(Codex) is one unit of activity at its timestamp. Transcripts are tailed:
each poll reads only the bytes appended since the last one. Other agents have
no transcript, so while the map runs it samples their status instead.

Read-only: nothing here writes to a transcript or leaves the machine.
"""

import datetime
import glob
import json
import os
import time

READ_TAIL = 8 << 20  # bytes read back from a transcript the first time it is seen
KEEP_SECONDS = 24 * 3600  # history older than this is dropped
LOCATE_RETRY = 30.0  # seconds before looking again for a transcript not found
SAMPLE_EVERY = 2.0  # seconds between status samples for agents without transcripts
WINDOWS = [600, 1800, 3600, 3 * 3600, 12 * 3600]  # plot windows, cycled with [ and ]
LEVELS = " ▁▂▃▄▅▆▇█"


def parse_time(text):
    """Epoch seconds from an ISO-8601 timestamp, or None."""
    if not isinstance(text, str) or not text:
        return None
    try:
        return datetime.datetime.fromisoformat(text.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def is_activity(kind, doc):
    if kind == "claude":
        return doc.get("type") == "assistant"
    if kind == "codex":
        return doc.get("type") == "response_item"
    return False


def claude_dir():
    return os.environ.get("CLAUDE_CONFIG_DIR") or os.path.expanduser("~/.claude")


def codex_home():
    return os.environ.get("CODEX_HOME") or os.path.expanduser("~/.codex")


def locate(kind, session, claude_root=None, codex_root=None):
    """The session's main transcript, or None."""
    if not session or "/" in session or session.startswith("."):
        return None
    if kind == "claude":
        root = claude_root or claude_dir()
        hits = glob.glob(os.path.join(glob.escape(root), "projects", "*", glob.escape(session) + ".jsonl"))
    elif kind == "codex":
        root = codex_root or codex_home()
        hits = glob.glob(os.path.join(glob.escape(root), "sessions", "*", "*", "*",
                                      "rollout-*" + glob.escape(session) + ".jsonl"))
    else:
        return None
    return max(hits, key=os.path.getmtime) if hits else None


def subagent_files(kind, main):
    """Claude subagent transcripts that belong to the session in `main`."""
    if kind != "claude" or not main:
        return []
    folder = os.path.join(main[: -len(".jsonl")], "subagents")
    try:
        names = os.listdir(folder)
    except OSError:
        return []
    return [os.path.join(folder, n) for n in sorted(names) if n.endswith(".jsonl")]


class Transcript:
    """One JSONL file, read incrementally."""

    def __init__(self, path, kind):
        self.path = path
        self.kind = kind
        self.offset = None  # None until the first read
        self.times = []

    def poll(self):
        try:
            size = os.path.getsize(self.path)
        except OSError:
            return
        if self.offset is not None and size < self.offset:
            self.offset, self.times = None, []  # replaced or truncated: start over
        start = max(0, size - READ_TAIL) if self.offset is None else self.offset
        if start >= size:
            self.offset = size
            return
        try:
            with open(self.path, "rb") as fh:
                fh.seek(start)
                data = fh.read(size - start)
        except OSError:
            return
        end = data.rfind(b"\n")
        if end < 0:
            self.offset = start  # no complete line yet; a mid-line start fails to parse and is skipped
            return
        lines = data[: end + 1].split(b"\n")
        if self.offset is None and start > 0:
            lines = lines[1:]  # began mid-line
        self.offset = start + end + 1
        for line in lines:
            if not line.strip():
                continue
            try:
                doc = json.loads(line)
            except ValueError:
                continue
            if isinstance(doc, dict) and is_activity(self.kind, doc):
                stamp = parse_time(doc.get("timestamp"))
                if stamp is not None:
                    self.times.append(stamp)


class Activity:
    """Activity times per pane, from transcripts or status samples."""

    def __init__(self, clock=time.time, claude_root=None, codex_root=None):
        self.clock = clock
        self.claude_root = claude_root
        self.codex_root = codex_root
        self.sources = {}  # (kind, session) -> {"main": path|None, "looked": t, "files": {path: Transcript}}
        self.samples = {}  # pane_id -> [times sampled while working]
        self.session_of = {}  # pane_id -> (kind, session)

    def update(self, panes):
        now = self.clock()
        for pane in panes:
            pane_id, kind = pane.get("pane_id"), pane.get("agent")
            if not pane_id or not kind:
                continue
            ref = pane.get("agent_session") if isinstance(pane.get("agent_session"), dict) else {}
            session = ref.get("value") if ref.get("kind") == "id" else None
            if kind in ("claude", "codex") and session:
                self.session_of[pane_id] = (kind, session)
                self._poll(kind, session, now)
            elif pane.get("agent_status") == "working":
                times = self.samples.setdefault(pane_id, [])
                if not times or now - times[-1] >= SAMPLE_EVERY:
                    times.append(now)
        cutoff = now - KEEP_SECONDS
        for times in self.samples.values():
            while times and times[0] < cutoff:
                times.pop(0)

    def _poll(self, kind, session, now):
        src = self.sources.setdefault((kind, session), {"main": None, "looked": None, "files": {}})
        if src["main"] is None and (src["looked"] is None or now - src["looked"] >= LOCATE_RETRY):
            src["looked"] = now
            src["main"] = locate(kind, session, self.claude_root, self.codex_root)
        if src["main"] is None:
            return
        for path in [src["main"], *subagent_files(kind, src["main"])]:
            if path not in src["files"]:
                src["files"][path] = Transcript(path, kind)
            src["files"][path].poll()

    def has_history(self, pane_id):
        src = self.sources.get(self.session_of.get(pane_id))
        return bool(src and src["main"]) or bool(self.samples.get(pane_id))

    def times(self, pane_id):
        src = self.sources.get(self.session_of.get(pane_id))
        if src and src["main"]:
            out = []
            for transcript in src["files"].values():
                out.extend(transcript.times)
            return out
        return list(self.samples.get(pane_id, []))


def buckets(times, now, window, count):
    """Activity counts in `count` equal slices of the last `window` seconds.

    Slices are aligned to multiples of their width, so the plot scrolls in
    whole steps instead of jittering as `now` advances.
    """
    count = max(1, count)
    width = window / count
    end = (now // width + 1) * width
    start = end - width * count
    out = [0] * count
    for stamp in times:
        if start <= stamp < end:
            out[min(count - 1, int((stamp - start) // width))] += 1
    return out


def sparkline(counts, peak):
    """One character per slice; any nonzero slice shows at least the lowest bar."""
    top = len(LEVELS) - 1
    if peak <= 0:
        return " " * len(counts)
    return "".join(LEVELS[0 if c <= 0 else max(1, min(top, -(-c * top // peak)))] for c in counts)


def window_label(seconds):
    return f"{seconds // 3600}h" if seconds >= 3600 else f"{seconds // 60}m"
