"""Thin, tolerant wrapper around the public Herdr CLI and event socket.

Only documented surfaces are used: the `herdr` CLI (via HERDR_BIN_PATH) for
reads and writes, and `events.subscribe` on the JSON socket purely as a
"something changed" signal. Event payloads are never parsed for state, and
unknown response fields are ignored, so new Herdr releases can add fields or
events without breaking this plugin.
"""

import json
import os
import socket
import subprocess
import threading
import time

HERDR = os.environ.get("HERDR_BIN_PATH") or "herdr"
SOURCE = "agentmap"

# Pane token contract (written by the orchestrator through `horchestra-tag`).
TOKEN_PARENT = "agentmap_parent"  # parent terminal_id (or pane_id)
TOKEN_ROLE = "agentmap_role"  # short display label, e.g. "FE"
TOKEN_VIEW = "agentmap_view"  # marks a pane running the map itself
TOKEN_TASK = "agentmap_task"  # member's task from team.toml (written by sync)
TOKEN_STATUS = "agentmap_status"  # member-reported progress line
TOKEN_NEEDS = "agentmap_needs"  # "1" while a member waits on the human
TOKEN_PROFILE = "agentmap_profile"  # member's role profile summary (written by sync)


class HerdrError(Exception):
    pass


def call(*args, timeout=5.0):
    """Run a herdr CLI command and return its JSON `result` object."""
    try:
        proc = subprocess.run(
            [HERDR, *args],
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise HerdrError(str(exc)) from exc
    out = proc.stdout.strip()
    if not out and proc.returncode == 0:
        return {}  # some write commands print nothing on success
    if not out:
        raise HerdrError(proc.stderr.strip() or f"exit {proc.returncode}")
    try:
        doc = json.loads(out.splitlines()[-1])
    except ValueError as exc:
        raise HerdrError(f"unparseable output: {out[:200]}") from exc
    if not isinstance(doc, dict):
        raise HerdrError("unexpected response")
    if "error" in doc:
        err = doc.get("error") or {}
        raise HerdrError(f"{err.get('code', 'error')}: {err.get('message', '')}")
    result = doc.get("result")
    return result if isinstance(result, dict) else {}


def read_text(pane_id, lines=40, timeout=5.0):
    """Recent plain-text output of a pane ('' on any failure)."""
    try:
        proc = subprocess.run(
            [HERDR, "pane", "read", pane_id, "--source", "recent", "--lines", str(lines)],
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except (OSError, subprocess.TimeoutExpired):
        return ""
    return proc.stdout if proc.returncode == 0 else ""


def call_quiet(*args, timeout=5.0):
    try:
        return call(*args, timeout=timeout)
    except HerdrError:
        return None


def get_pane(pane_id):
    return (call("pane", "get", pane_id).get("pane")) or {}


def list_panes(workspace_id):
    panes = call("pane", "list", "--workspace", workspace_id).get("panes")
    return [p for p in panes or [] if isinstance(p, dict)]


def get_workspace(workspace_id):
    return (call("workspace", "get", workspace_id).get("workspace")) or {}


def set_tokens(pane_id, tokens=None, clear=()):
    args = ["pane", "report-metadata", pane_id, "--source", SOURCE]
    for key, value in (tokens or {}).items():
        args += ["--token", f"{key}={value}"]
    for key in clear:
        args += ["--clear-token", key]
    return call(*args)


def focus_pane(pane_id):
    """Focus a pane; prefer the agent helper, fall back to plugin pane focus."""
    if call_quiet("agent", "focus", pane_id) is not None:
        return True
    return call_quiet("plugin", "pane", "focus", pane_id) is not None


# Lifecycle events that can change what the map shows. Unknown names are
# dropped automatically if a future server rejects them.
GLOBAL_EVENTS = [
    "pane.created",
    "pane.closed",
    "pane.updated",
    "pane.moved",
    "pane.exited",
    "pane.agent_detected",
    "pane.focused",
    "workspace.renamed",
    "workspace.closed",
    "tab.closed",
]


class EventWatcher(threading.Thread):
    """Keeps an events.subscribe stream open and flags `changed` on any event.

    Reconnects with backoff when the server restarts, hands off, or updates.
    Per-pane `pane.agent_status_changed` subscriptions are rebuilt whenever
    the watched pane set changes.
    """

    def __init__(self):
        super().__init__(daemon=True)
        self.changed = threading.Event()
        self.connected = False
        self._lock = threading.Lock()
        self._panes = frozenset()
        self._panes_dirty = threading.Event()
        self._global = list(GLOBAL_EVENTS)
        self._per_pane_ok = True

    def watch_panes(self, pane_ids):
        pane_ids = frozenset(pane_ids)
        with self._lock:
            if pane_ids != self._panes:
                self._panes = pane_ids
                self._panes_dirty.set()

    def _subscriptions(self):
        subs = [{"type": t} for t in self._global]
        if self._per_pane_ok:
            with self._lock:
                panes = sorted(self._panes)
            subs += [
                {"type": "pane.agent_status_changed", "pane_id": p} for p in panes
            ]
        return subs

    def run(self):
        path = os.environ.get("HERDR_SOCKET_PATH")
        if not path or not hasattr(socket, "AF_UNIX"):
            return  # polling only
        backoff = 1.0
        while self._global or self._per_pane_ok:
            try:
                self._stream(path)
                backoff = 1.0
            except (OSError, ValueError):
                pass
            self.connected = False
            self.changed.set()  # force a refresh after reconnecting
            time.sleep(backoff)
            backoff = min(backoff * 2, 10.0)

    def _stream(self, path):
        self._panes_dirty.clear()
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(1.0)
        try:
            sock.connect(path)
            request = {
                "id": "agentmap",
                "method": "events.subscribe",
                "params": {"subscriptions": self._subscriptions()},
            }
            sock.sendall((json.dumps(request) + "\n").encode())
            buf = b""
            acked = False
            while not self._panes_dirty.is_set():
                try:
                    chunk = sock.recv(65536)
                except socket.timeout:
                    continue
                if not chunk:
                    return
                buf += chunk
                while b"\n" in buf:
                    line, buf = buf.split(b"\n", 1)
                    if not acked:
                        acked = True
                        if not self._accept_ack(line):
                            return
                        self.connected = True
                        continue
                    self.changed.set()
        finally:
            sock.close()

    def _accept_ack(self, line):
        """Degrade the subscription set if the server rejects it."""
        try:
            doc = json.loads(line)
        except ValueError:
            return False
        if "error" not in doc:
            return True
        if self._per_pane_ok:
            self._per_pane_ok = False
        elif len(self._global) > 3:
            self._global = ["pane.created", "pane.closed", "pane.updated"]
        else:
            self._global = []
        return False
