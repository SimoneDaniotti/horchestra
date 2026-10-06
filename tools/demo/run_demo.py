#!/usr/bin/env python3
"""Scripted demo of the agent map for the README (recorded with demo.tape).

A fictional "webshop" team plays out a short story: the orchestrator briefs
members, they work, report back, message each other and one needs the human.
The real agentmap.py draws it; Herdr is replaced by herdr_stub.py, and agent
transcripts are generated so the activity plot fills in. Time runs
DEMO_SPEED times faster for the plot, so half a minute shows half an hour.

    python3 tools/demo/run_demo.py      # q to quit
"""

import json
import os
import sys
import tempfile
import threading
import time
import types

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
sys.path.insert(0, ROOT)

DEMO_SPEED = 60.0  # demo seconds per real second
START = time.time()


def demo_time():
    return START + (time.time() - START) * DEMO_SPEED


# role, kind, parent role, tab, task
TEAM = [
    ("orchestrator", "claude", None, "t1", "coordinates the team"),
    ("frontend", "claude", "orchestrator", "t2", "checkout page"),
    ("backend", "codex", "orchestrator", "t3", "payments API"),
    ("tests", "claude", "backend", "t4", "API tests"),
    ("docs", "claude", "orchestrator", "t5", "release notes"),
]
PANE = {role: f"w1:p{i + 1}" for i, (role, *_rest) in enumerate(TEAM)}
TERM = {role: f"term_{i + 1:02d}" for i, (role, *_rest) in enumerate(TEAM)}
SESSION = {role: f"00000000-0000-4000-8000-{i + 1:012d}" for i, (role, *_rest) in enumerate(TEAM)}
MAP_PANE = "w1:pM"

# (real second, role, change): status, report, needs, msg, say
STORY = [
    (0.5, "orchestrator", {"status": "working"}),
    (1.5, "orchestrator", {"msg": "frontend", "say": "Briefing frontend on the checkout page."}),
    (2.0, "frontend", {"status": "working"}),
    (3.0, "orchestrator", {"msg": "backend", "say": "Briefing backend on the payments API."}),
    (3.5, "backend", {"status": "working"}),
    (5.0, "orchestrator", {"msg": "docs"}),
    (5.5, "docs", {"status": "working"}),
    (6.0, "orchestrator", {"status": "idle"}),
    (7.0, "backend", {"msg": "tests", "say": "Asked tests to cover refunds."}),
    (7.5, "tests", {"status": "working"}),
    (10.0, "tests", {"report": "12/12 refund tests pass", "status": "idle"}),
    (12.0, "frontend", {"report": "checkout form done", "status": "idle"}),
    (12.5, "orchestrator", {"status": "working"}),
    (14.0, "docs", {"needs": "publish notes for v2.1 or v2.2?", "status": "idle"}),
    (16.0, "backend", {"msg": "frontend", "say": "Told frontend the new /pay fields."}),
    (16.5, "frontend", {"status": "working"}),
    (19.0, "backend", {"report": "payments API ready", "status": "idle"}),
    (20.0, "orchestrator", {"msg": "docs", "say": "v2.2, thanks."}),
    (20.5, "docs", {"status": "working", "clear_needs": True}),
    (23.0, "frontend", {"report": "checkout wired to /pay", "status": "idle"}),
    (25.0, "orchestrator", {"status": "idle"}),
]


def pane_of(role, kind, parent, tab, task):
    tokens = {"agentmap_role": role, "agentmap_task": task}
    if parent:
        tokens["agentmap_parent"] = TERM[parent]
    return {
        "pane_id": PANE[role], "terminal_id": TERM[role], "workspace_id": "w1", "tab_id": tab,
        "agent": kind, "agent_status": "idle", "label": f"team:{role}", "tokens": tokens,
        "agent_session": {"agent": kind, "kind": "id", "source": f"herdr:{kind}", "value": SESSION[role]},
        "screen": "",
    }


class Demo:
    def __init__(self, folder):
        self.folder = folder
        self.state_path = os.path.join(folder, "state.json")
        self.panes = {row[0]: pane_of(*row) for row in TEAM}
        self.transcripts = {}
        for role, kind, *_ in TEAM:
            if kind == "claude":
                path = os.path.join(folder, "claude", "projects", "-demo-webshop", SESSION[role] + ".jsonl")
            else:
                path = os.path.join(folder, "codex", "sessions", "2026", "10", "06",
                                    f"rollout-2026-10-06T09-00-00-{SESSION[role]}.jsonl")
            os.makedirs(os.path.dirname(path), exist_ok=True)
            open(path, "w").close()
            self.transcripts[role] = (path, kind)
        self.write()

    def write(self):
        panes = list(self.panes.values()) + [{
            "pane_id": MAP_PANE, "terminal_id": "term_map", "workspace_id": "w1", "tab_id": "t1",
            "label": "horchestra-map", "tokens": {"agentmap_view": "1"}}]
        state = {"panes": panes, "workspace": {"workspace_id": "w1", "label": "webshop", "number": 1},
                 "tabs": [{"tab_id": f"t{i + 1}", "label": row[0]} for i, row in enumerate(TEAM)]}
        tmp = self.state_path + ".tmp"
        with open(tmp, "w") as fh:
            json.dump(state, fh)
        os.replace(tmp, self.state_path)

    def log(self, role, count=1):
        path, kind = self.transcripts[role]
        stamp = time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(demo_time())) + ".000Z"
        line = ({"type": "assistant", "timestamp": stamp} if kind == "claude"
                else {"type": "response_item", "timestamp": stamp, "payload": {"type": "message"}})
        with open(path, "a") as fh:
            fh.write((json.dumps(line) + "\n") * count)

    def apply(self, role, change):
        pane = self.panes[role]
        tokens = pane["tokens"]
        signal = None
        if "status" in change:
            pane["agent_status"] = change["status"]
        if "report" in change:
            tokens["agentmap_status"] = change["report"]
            tokens.pop("agentmap_needs", None)
            signal = ("report", "-")
        if "needs" in change:
            tokens["agentmap_status"] = change["needs"]
            tokens["agentmap_needs"] = "1"
            signal = ("needs", "-")
        if change.get("clear_needs"):
            tokens.pop("agentmap_needs", None)
            tokens.pop("agentmap_status", None)
        if "msg" in change:
            signal = ("msg", PANE[change["msg"]])
        if signal:
            tokens["agentmap_signal"] = f"{signal[0]} {signal[1]} {int(demo_time())}"
            self.log(role, 3)
        if "say" in change:
            pane["screen"] = "⏺ " + change["say"]
        self.write()

    def play(self):
        begin = time.time()
        pending = list(STORY)
        while True:
            elapsed = time.time() - begin
            while pending and pending[0][0] <= elapsed:
                _, role, change = pending.pop(0)
                self.apply(role, change)
            for role, pane in self.panes.items():  # working agents write to their transcripts
                if pane["agent_status"] == "working":
                    self.log(role, 1 + (int(elapsed * 7) + len(role)) % 3)
            time.sleep(0.25)


def main():
    folder = tempfile.mkdtemp(prefix="horchestra-demo-")
    os.environ.update({
        "HERDR_BIN_PATH": os.path.join(HERE, "herdr_stub.py"), "DEMO_STATE": os.path.join(folder, "state.json"),
        "HERDR_PANE_ID": MAP_PANE, "HERDR_WORKSPACE_ID": "w1", "HERDR_TAB_ID": "t1",
        "CLAUDE_CONFIG_DIR": os.path.join(folder, "claude"), "CODEX_HOME": os.path.join(folder, "codex"),
    })
    for key in ("HERDR_SOCKET_PATH", "HERDR_PLUGIN_CONFIG_DIR", "HORCHESTRA_ANIMATE"):
        os.environ.pop(key, None)
    demo = Demo(folder)
    threading.Thread(target=demo.play, daemon=True).start()

    import agentmap

    agentmap.POLL_SECONDS = 0.3
    agentmap.SIGNAL_FRESH *= DEMO_SPEED  # signals carry demo-time stamps
    agentmap.time = types.SimpleNamespace(time=demo_time, monotonic=time.monotonic, sleep=time.sleep)
    # agentmap.main() starts curses from terminfo's default size and then
    # follows the real one; recorders' terminals garble that first resize,
    # so start at the real size.
    import curses
    import locale

    locale.setlocale(locale.LC_ALL, "")
    os.environ.setdefault("ESCDELAY", "25")
    size = os.get_terminal_size()
    os.environ["LINES"], os.environ["COLUMNS"] = str(size.lines), str(size.columns)
    try:
        curses.wrapper(agentmap.setup)
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
