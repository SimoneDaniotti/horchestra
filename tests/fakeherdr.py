"""A stateful, in-memory stand-in for the Herdr CLI (modelled on Herdr 0.9.3).

`FakeHerdr.install(test)` replaces the `subprocess` module that
`herdr_client` uses, so `hc.call`, `hc.call_quiet`, `hc.read_text` and every
helper built on them (and therefore all of team.py / toggle.py) run for real:
they build a real `herdr ...` argv, the fake parses it, mutates its state and
prints the same one-line JSON envelope the real CLI prints
(`{"id": ..., "result": {...}}` or `{"error": {"code": ..., "message": ...}}`).

Modelled Herdr rules (see the Herdr source: src/app/agents.rs,
src/app/api_helpers.rs, docs/.../cli-reference.mdx):

- agent names must match ``[a-z][a-z0-9_-]{0,31}`` (``invalid_agent_name``)
  and be unique among live agents (``agent_name_taken``); a name is released
  when its agent exits or its pane closes.
- ``agent start`` only targets a pane running a plain shell
  (``agent_pane_busy`` otherwise) and its ``--timeout`` must be in
  (3000, 300000] ms.
- pane tokens: keys ``[A-Za-z0-9_-]{1,32}``, at most 16 per request and 32
  per pane; values are trimmed, control characters stripped, cut to 80
  characters, and an empty value clears the token.
- agent targets resolve by pane id, terminal id, or agent name.
- closing the last pane of a tab closes the tab; closing the last tab closes
  the workspace.

Things deliberately simplified: agents become ready instantly (status
"idle", session id reported at once), layouts are rough rectangles, and
pane/workspace ids stay stable across `simulate_restart` (the real server
may renumber them, which the plugin must already tolerate because it
identifies agents by session id).

Typical use::

    fake = FakeHerdr(claude_config_dir=claude_dir).install(self)
    ws = fake.add_workspace("proj", project)
    orch = fake.add_agent(fake.root_pane(ws), session="sess-orch")
    team.main(["init"])
    fake.launches_of(pane_id)   # what `agent start` launched there
    fake.prompts_to(pane_id)    # texts sent with `agent prompt`

Extend it by adding a handler method named ``_<group>_<verb>`` (dashes become
underscores, e.g. ``_pane_send_keys``) that takes a parsed ``Args``.
"""

import itertools
import json
import os
import re
import subprocess
import types

from unittest import mock

import herdr_client as hc

AGENT_NAME_RE = re.compile(r"^[a-z][a-z0-9_-]{0,31}$")
TOKEN_KEY_RE = re.compile(r"^[A-Za-z0-9_-]{1,32}$")
MAX_TOKENS_PER_REQUEST = 16
MAX_TOKENS_PER_PANE = 32
MAX_TOKEN_VALUE = 80
AGENT_KINDS = {"claude", "codex", "pi", "opencode", "gemini", "amp", "droid", "cursor"}
SHELL = "zsh"
AREA = {"x": 0, "y": 0, "width": 200, "height": 50}
# Plugin pane titles from herdr-plugin.toml ([[panes]] title).
PLUGIN_PANE_TITLES = {"map": "horchestra-map", "overview": "horchestra-overview"}
# What agentmap.py reports about itself once it runs (TOKEN_VIEW).
PLUGIN_PANE_VIEW = {"map": "1", "overview": "all"}

# Options that take no value; every other --option takes exactly one.
FLAGS = {"--focus", "--no-focus", "--clear", "--wait", "--json", "--all"}


class FakeError(Exception):
    def __init__(self, code, message=""):
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message or code


class Args:
    """A parsed argv: positionals, repeatable --options, flags, and `-- rest`."""

    def __init__(self, argv):
        self.pos, self.opts, self.flags, self.rest = [], {}, set(), []
        it = iter(argv)
        for tok in it:
            if tok == "--":
                self.rest = list(it)
                break
            if tok.startswith("--"):
                if tok in FLAGS:
                    self.flags.add(tok)
                else:
                    self.opts.setdefault(tok, []).append(next(it, ""))
            else:
                self.pos.append(tok)

    def opt(self, name, default=None):
        values = self.opts.get(name)
        return values[-1] if values else default

    def all(self, name):
        return list(self.opts.get(name, []))


class FakeClock:
    """Virtual time for modules that sleep while polling (patch `team.time`).

    `sleep` advances the clock instantly, so grace periods and polling loops
    finish at once while still seeing time pass.
    """

    def __init__(self, start=1000.0):
        self.now = start

    def monotonic(self):
        return self.now

    def time(self):
        return self.now

    def sleep(self, seconds):
        self.now += max(0.0, float(seconds))


class FakeHerdr:
    def __init__(self, claude_config_dir=None):
        # Insertion order is creation order (tabs list in that order).
        self.workspaces = {}  # workspace_id -> dict
        self.tabs = {}  # tab_id -> dict
        self.panes = {}  # pane_id -> internal dict (see _new_pane)
        self.focused_workspace = None
        # When set, `agent start` / `add_agent` of a claude agent writes the
        # conversation file Claude Code keeps (projects/<slug>/<session>.jsonl),
        # which is what `team reopen` checks before resuming.
        self.claude_config_dir = claude_config_dir
        # Records for assertions.
        self.calls = []  # every argv received (without the binary)
        self.prompts = []  # {"pane_id", "text", "wait"}
        self.launches = []  # {"pane_id", "name", "kind", "args", "session", "plain_resume"}
        self.closed = []  # pane ids closed with `pane close` / `plugin pane close`
        self.tab_renames = []  # (tab_id, label)
        self.notifications = []  # {"title", "body", "sound"}
        self.sent_keys = []  # (pane_id, [keys])
        self.injected = []  # [(argv prefix tuple, code, message)] consumed in order
        self._ws_seq = itertools.count(1)
        self._term_seq = itertools.count(1)
        self._sess_seq = itertools.count(1)
        self._pid_seq = itertools.count(4000)

    # ---- plumbing --------------------------------------------------------

    def install(self, test):
        """Route herdr_client's subprocess calls here for the test's duration."""
        fake_subprocess = types.SimpleNamespace(
            run=self.run,
            TimeoutExpired=subprocess.TimeoutExpired,
            CompletedProcess=subprocess.CompletedProcess,
            PIPE=subprocess.PIPE,
            DEVNULL=subprocess.DEVNULL,
        )
        patcher = mock.patch.object(hc, "subprocess", fake_subprocess)
        patcher.start()
        test.addCleanup(patcher.stop)
        return self

    def run(self, argv, capture_output=False, text=False, timeout=None, **_kw):
        """subprocess.run replacement: argv[0] is the herdr binary."""
        args = [str(a) for a in argv[1:]]
        self.calls.append(tuple(args))
        try:
            self._maybe_inject(args)
            if args[:2] == ["pane", "read"]:
                out = self._pane_read(Args(args[2:]))
                return subprocess.CompletedProcess(argv, 0, out, "")
            result = self.dispatch(args)
        except FakeError as exc:
            # Real Herdr: exit 1, empty stdout, one JSON error line on stderr.
            doc = {"error": {"code": exc.code, "message": exc.message}, "id": "cli:" + ":".join(args[:2])}
            return subprocess.CompletedProcess(argv, 1, "", json.dumps(doc, separators=(",", ":")) + "\n")
        doc = {"id": "cli", "result": result if result is not None else {}}
        return subprocess.CompletedProcess(argv, 0, json.dumps(doc) + "\n", "")

    def inject_error(self, prefix, code, message=""):
        """Make the next command starting with `prefix` (a tuple) fail once."""
        self.injected.append((tuple(prefix), code, message))

    def _maybe_inject(self, args):
        for i, (prefix, code, message) in enumerate(self.injected):
            if tuple(args[: len(prefix)]) == prefix:
                del self.injected[i]
                raise FakeError(code, message)

    def dispatch(self, args):
        if len(args) >= 3 and args[0] == "plugin" and args[1] == "pane":
            name, rest = f"_plugin_pane_{args[2]}", args[3:]
        elif len(args) >= 2:
            name, rest = f"_{args[0]}_{args[1]}", args[2:]
        else:
            raise FakeError("unknown_command", " ".join(args))
        handler = getattr(self, name.replace("-", "_"), None)
        if handler is None:
            raise FakeError("unknown_command", f"fake herdr does not model `{' '.join(args[:3])}`")
        return handler(Args(rest))

    # ---- scenario builders -----------------------------------------------

    def add_workspace(self, label, cwd, focused=None):
        """A workspace with one tab and one shell pane; returns its id."""
        ws_id = f"w{next(self._ws_seq)}"
        self.workspaces[ws_id] = {"workspace_id": ws_id, "label": label, "cwd": cwd,
                                  "number": len(self.workspaces) + 1, "_pane_seq": itertools.count(1),
                                  "_tab_seq": itertools.count(1)}
        if focused or (focused is None and self.focused_workspace is None):
            self.focused_workspace = ws_id
        self.add_tab(ws_id, "1", cwd)
        return ws_id

    def add_tab(self, workspace_id, label, cwd=None):
        """A new tab with one shell pane; returns the tab id."""
        ws = self._workspace(workspace_id)
        tab_id = f"{workspace_id}:t{next(ws['_tab_seq'])}"
        self.tabs[tab_id] = {"tab_id": tab_id, "workspace_id": workspace_id, "label": label}
        self._new_pane(tab_id, cwd or ws["cwd"], dict(AREA))
        return tab_id

    def add_pane(self, tab_id, cwd=None, label=None, direction="right"):
        """Split a shell pane into `tab_id` (beside its last pane); returns its id."""
        last = self.tab_panes(tab_id)[-1]
        pane = self._split(self.panes[last], direction, cwd)
        pane["label"] = label
        return pane["pane_id"]

    def add_agent(self, pane_id, kind="claude", session=None, name=None, status="idle"):
        """Turn a shell pane into one already running an agent (no launch recorded)."""
        pane = self.panes[pane_id]
        pane.update(agent=kind, agent_status=status, process=kind, name=name,
                    session=session or self._new_session())
        self._save_transcript(pane)
        return pane_id

    def root_pane(self, workspace_or_tab):
        """First pane of a workspace's first tab, or of a tab."""
        tab = workspace_or_tab
        if tab in self.workspaces:
            tab = next(t for t, v in self.tabs.items() if v["workspace_id"] == tab)
        return self.tab_panes(tab)[0]

    def close_workspace(self, workspace_id):
        """The human closes a whole space (its agents end, names are released)."""
        self._workspace_close(Args([workspace_id]))

    def set_status(self, pane_id, status):
        self.panes[pane_id]["agent_status"] = status

    def set_screen(self, pane_id, text):
        self.panes[pane_id]["screen"] = text

    def simulate_restart(self, resume=True, not_resumed=(), keep_names=False):
        """Herdr server restart as 0.9.3 does it.

        Every pane gets a new terminal id; all tokens are dropped; labels are
        kept; agent names are dropped (unless `keep_names`). Agents with a
        recorded session come back resumed with the same session id and status
        idle, launched as a plain `claude --resume <id>` (recorded with
        plain_resume=True, i.e. WITHOUT the profile flags); agents in
        `not_resumed` pane ids (or all of them when `resume` is False) come back
        as plain shells. Plugin panes come back as idle shells keeping their
        label (no longer plugin panes, no view token).
        """
        for pane in self.panes.values():
            pane["terminal_id"] = self._new_terminal()
            pane["tokens"] = {}
            pane["focused"] = False
            if pane["plugin"]:
                pane.update(plugin=None, process=SHELL)
            if not pane["agent"]:
                continue
            session, kind = pane["session"], pane["agent"]
            if resume and session and pane["pane_id"] not in set(not_resumed):
                name = pane["name"] if keep_names else None
                pane.update(agent_status="idle", name=name, process=kind)
                args = ["--resume", session] if kind == "claude" else ["resume", session]
                self.launches.append({"pane_id": pane["pane_id"], "name": name, "kind": kind,
                                      "args": args, "session": session, "plain_resume": True})
            else:
                self._exit_agent(pane)

    # ---- inspection ------------------------------------------------------

    def pane(self, pane_id):
        """Public (CLI-shaped) view of one pane, or None when it is gone."""
        p = self.panes.get(pane_id)
        return self._pane_info(p) if p else None

    def panes_in(self, workspace_id, tab_id=None):
        return [self._pane_info(p) for p in self.panes.values()
                if p["workspace_id"] == workspace_id and (tab_id is None or p["tab_id"] == tab_id)]

    def tab_panes(self, tab_id):
        return [pid for pid, p in self.panes.items() if p["tab_id"] == tab_id]

    def tabs_in(self, workspace_id):
        return [self._tab_info(t) for t in self.tabs.values() if t["workspace_id"] == workspace_id]

    def prompts_to(self, pane_id):
        return [p["text"] for p in self.prompts if p["pane_id"] == pane_id]

    def launches_of(self, pane_id):
        return [launch for launch in self.launches if launch["pane_id"] == pane_id]

    def closed_panes(self):
        return list(self.closed)

    def renamed_tabs(self):
        return list(self.tab_renames)

    def agent_named(self, name):
        """The pane running the live agent called `name`, or None."""
        p = next((p for p in self.panes.values() if p["agent"] and p["name"] == name), None)
        return self._pane_info(p) if p else None

    def agent_name_of(self, pane_id):
        p = self.panes.get(pane_id)
        return p["name"] if p and p["agent"] else None

    def map_panes(self, workspace_id, view="1"):
        """Live plugin map panes (agentmap.py running) in a workspace."""
        return [p for p in self.panes_in(workspace_id)
                if p["tokens"].get(hc.TOKEN_VIEW) == view]

    def calls_of(self, *prefix):
        return [c for c in self.calls if c[: len(prefix)] == prefix]

    # ---- internals -------------------------------------------------------

    def _workspace(self, workspace_id):
        ws = self.workspaces.get(workspace_id)
        if ws is None:
            raise FakeError("workspace_not_found", f"workspace {workspace_id} not found")
        return ws

    def _tab(self, tab_id):
        tab = self.tabs.get(tab_id)
        if tab is None:
            raise FakeError("tab_not_found", f"tab {tab_id} not found")
        return tab

    def _get(self, pane_id):
        pane = self.panes.get(pane_id)
        if pane is None:
            raise FakeError("pane_not_found", f"pane {pane_id} not found")
        return pane

    def _new_terminal(self):
        return f"term_{next(self._term_seq)}"

    def _new_session(self):
        return f"sess-{next(self._sess_seq):04d}-{os.urandom(3).hex()}"

    def _new_pane(self, tab_id, cwd, rect):
        tab = self.tabs[tab_id]
        ws = self.workspaces[tab["workspace_id"]]
        pane_id = f"{ws['workspace_id']}:p{next(ws['_pane_seq'])}"
        pane = {
            "pane_id": pane_id, "workspace_id": ws["workspace_id"], "tab_id": tab_id,
            "terminal_id": self._new_terminal(), "cwd": cwd, "label": None,
            "agent": None, "agent_status": None, "session": None, "name": None,
            "tokens": {}, "focused": False, "process": SHELL, "plugin": None,
            "rect": rect, "screen": "", "pid": next(self._pid_seq),
        }
        self.panes[pane_id] = pane
        return pane

    def _split(self, target, direction, cwd=None):
        r = dict(target["rect"])
        if direction in ("right", "left"):
            half = r["width"] // 2
            target["rect"] = dict(r, width=r["width"] - half)
            new = dict(r, width=half)
            if direction == "right":
                new["x"] = r["x"] + r["width"] - half
            else:
                target["rect"]["x"] = r["x"] + half
        else:
            half = r["height"] // 2
            target["rect"] = dict(r, height=r["height"] - half)
            new = dict(r, height=half)
            if direction == "down":
                new["y"] = r["y"] + r["height"] - half
            else:
                target["rect"]["y"] = r["y"] + half
        return self._new_pane(target["tab_id"], cwd or target["cwd"], new)

    def _remove_pane(self, pane_id):
        pane = self.panes.pop(pane_id)
        tab_id = pane["tab_id"]
        if not self.tab_panes(tab_id):
            ws_id = self.tabs.pop(tab_id)["workspace_id"]
            if not any(t["workspace_id"] == ws_id for t in self.tabs.values()):
                self.workspaces.pop(ws_id, None)
                if self.focused_workspace == ws_id:
                    self.focused_workspace = next(iter(self.workspaces), None)

    def _exit_agent(self, pane):
        pane.update(agent=None, agent_status=None, session=None, name=None, process=SHELL)

    def _save_transcript(self, pane):
        if not self.claude_config_dir or pane["agent"] != "claude" or not pane["session"]:
            return
        slug = re.sub(r"[^A-Za-z0-9]", "-", pane["cwd"] or "unknown")
        folder = os.path.join(self.claude_config_dir, "projects", slug)
        os.makedirs(folder, exist_ok=True)
        with open(os.path.join(folder, pane["session"] + ".jsonl"), "a") as fh:
            fh.write("{}\n")

    def _pane_info(self, p):
        info = {
            "pane_id": p["pane_id"], "terminal_id": p["terminal_id"],
            "workspace_id": p["workspace_id"], "tab_id": p["tab_id"],
            "cwd": p["cwd"], "foreground_cwd": p["cwd"], "label": p["label"],
            "agent": p["agent"], "agent_status": p["agent_status"] if p["agent"] else "unknown",
            "tokens": dict(p["tokens"]),
            "focused": bool(p["focused"]) and p["workspace_id"] == self.focused_workspace,
            "terminal_title_stripped": p["label"] or "",
        }
        if p["agent"] and p["session"]:
            info["agent_session"] = {"source": f"herdr:{p['agent']}", "agent": p["agent"],
                                     "kind": "id", "value": p["session"]}
        return info

    def _agent_info(self, p):
        return dict(self._pane_info(p), name=p["name"], launch_pending=False, interactive_ready=True)

    def _tab_info(self, t):
        return {"tab_id": t["tab_id"], "workspace_id": t["workspace_id"], "label": t["label"],
                "pane_count": len(self.tab_panes(t["tab_id"]))}

    def _workspace_info(self, ws):
        return {"workspace_id": ws["workspace_id"], "label": ws["label"], "number": ws["number"],
                "cwd": ws["cwd"], "focused": ws["workspace_id"] == self.focused_workspace}

    def _resolve_agent(self, target):
        """Agent target: pane id, terminal id, or live agent name."""
        pane = self.panes.get(target) or next(
            (p for p in self.panes.values() if p["terminal_id"] == target), None)
        if pane is None:
            pane = next((p for p in self.panes.values() if p["agent"] and p["name"] == target), None)
        if pane is None or not pane["agent"]:
            raise FakeError("agent_not_found", f"agent target {target} not found")
        return pane

    def _name_conflicts(self, name, except_terminal=""):
        return [p for p in self.panes.values()
                if p["agent"] and p["name"] == name and p["terminal_id"] != except_terminal]

    def _focus(self, pane):
        for p in self.panes.values():
            if p["workspace_id"] == pane["workspace_id"]:
                p["focused"] = p is pane
        self.focused_workspace = pane["workspace_id"]

    # ---- workspace -------------------------------------------------------

    def _workspace_create(self, a):
        ws_id = self.add_workspace(a.opt("--label") or "workspace", a.opt("--cwd") or "/",
                                   focused="--focus" in a.flags)
        tab = next(t for t in self.tabs.values() if t["workspace_id"] == ws_id)
        root = self.panes[self.tab_panes(tab["tab_id"])[0]]
        if "--focus" in a.flags:
            self._focus(root)
        return {"workspace": self._workspace_info(self.workspaces[ws_id]),
                "tab": self._tab_info(tab), "root_pane": self._pane_info(root)}

    def _workspace_list(self, a):
        return {"workspaces": [self._workspace_info(w) for w in self.workspaces.values()]}

    def _workspace_get(self, a):
        return {"workspace": self._workspace_info(self._workspace(a.pos[0]))}

    def _workspace_focus(self, a):
        ws = self._workspace(a.pos[0])
        self.focused_workspace = ws["workspace_id"]
        return {"workspace": self._workspace_info(ws)}

    def _workspace_close(self, a):
        ws = self._workspace(a.pos[0])
        for pid in [pid for pid, p in self.panes.items() if p["workspace_id"] == ws["workspace_id"]]:
            self._remove_pane(pid)
        self.workspaces.pop(ws["workspace_id"], None)
        return {}

    # ---- tab -------------------------------------------------------------

    def _tab_create(self, a):
        tab_id = self.add_tab(a.opt("--workspace"), a.opt("--label") or "tab", a.opt("--cwd"))
        root = self.panes[self.tab_panes(tab_id)[0]]
        if "--focus" in a.flags:
            self._focus(root)
        return {"tab": self._tab_info(self.tabs[tab_id]), "root_pane": self._pane_info(root)}

    def _tab_list(self, a):
        ws = a.opt("--workspace")
        if ws:
            self._workspace(ws)
        return {"tabs": [self._tab_info(t) for t in self.tabs.values()
                         if ws is None or t["workspace_id"] == ws]}

    def _tab_rename(self, a):
        tab = self._tab(a.pos[0])
        tab["label"] = a.pos[1]
        self.tab_renames.append((tab["tab_id"], a.pos[1]))
        return {"tab": self._tab_info(tab)}

    # ---- pane ------------------------------------------------------------

    def _pane_list(self, a):
        ws = a.opt("--workspace")
        if ws:
            self._workspace(ws)
        return {"panes": [self._pane_info(p) for p in self.panes.values()
                          if ws is None or p["workspace_id"] == ws]}

    def _pane_get(self, a):
        return {"pane": self._pane_info(self._get(a.pos[0]))}

    def _pane_split(self, a):
        target = self._get(a.pos[0])
        pane = self._split(target, a.opt("--direction", "right"), a.opt("--cwd"))
        if "--focus" in a.flags:
            self._focus(pane)
        return {"pane": self._pane_info(pane)}

    def _pane_rename(self, a):
        pane = self._get(a.pos[0])
        if "--clear" in a.flags:
            pane["label"] = None
        else:
            pane["label"] = a.pos[1]
        return {"pane": self._pane_info(pane)}

    def _pane_close(self, a):
        self._get(a.pos[0])
        self._remove_pane(a.pos[0])
        self.closed.append(a.pos[0])
        return {}

    def _pane_report_metadata(self, a):
        pane = self._get(a.pos[0])
        patch = {}
        for item in a.all("--token"):
            key, _, value = item.partition("=")
            patch[key] = value
        for key in a.all("--clear-token"):
            patch[key] = None
        if not patch:
            raise FakeError("invalid_metadata_token", "missing token to set or clear")
        if len(patch) > MAX_TOKENS_PER_REQUEST:
            raise FakeError("invalid_metadata_token",
                            f"a metadata report may update at most {MAX_TOKENS_PER_REQUEST} tokens")
        normalized = {}
        for key, value in patch.items():
            if not TOKEN_KEY_RE.match(key):
                raise FakeError("invalid_metadata_token", f"invalid metadata token key: {key}")
            if value is not None:
                value = "".join(ch for ch in value.strip() if ch.isprintable())[:MAX_TOKEN_VALUE].strip()
            normalized[key] = value or None
        after = dict(pane["tokens"])
        for key, value in normalized.items():
            if value is None:
                after.pop(key, None)
            else:
                after[key] = value
        if len(after) > MAX_TOKENS_PER_PANE:
            raise FakeError("metadata_token_limit",
                            f"pane metadata may contain at most {MAX_TOKENS_PER_PANE} tokens")
        pane["tokens"] = after
        return {"pane": self._pane_info(pane)}

    def _pane_send_keys(self, a):
        pane = self._get(a.pos[0])
        keys = a.pos[1:]
        self.sent_keys.append((pane["pane_id"], keys))
        if pane["agent"] and keys.count("ctrl+c") >= 2:
            self._exit_agent(pane)  # two Ctrl+C quit Claude/Codex back to the shell
        return {}

    def _pane_send_text(self, a):
        self._get(a.pos[0])
        return {}

    def _pane_edges(self, a):
        pane = self._get(a.opt("--pane") or a.pos[0])
        panes = [{"pane_id": p["pane_id"], "rect": dict(p["rect"])}
                 for p in self.panes.values() if p["tab_id"] == pane["tab_id"]]
        return {"edges": {"pane_id": pane["pane_id"], "layout": {"area": dict(AREA), "panes": panes}}}

    def _pane_zoom(self, a):
        self._get(a.opt("--pane") or a.pos[0])
        return {}

    def _pane_focus(self, a):
        self._focus(self._get(a.pos[0]))
        return {}

    def _pane_swap(self, a):
        src, dst = self._get(a.opt("--source-pane")), self._get(a.opt("--target-pane"))
        src["rect"], dst["rect"] = dst["rect"], src["rect"]
        return {}

    def _pane_resize(self, a):
        """Move the divider between --pane and its neighbour by --amount of their span."""
        pane = self._get(a.opt("--pane"))
        direction, amount = a.opt("--direction"), float(a.opt("--amount", "0"))
        horizontal = direction in ("left", "right")
        pos, size = ("x", "width") if horizontal else ("y", "height")
        r = pane["rect"]
        others = [p for p in self.panes.values() if p["tab_id"] == pane["tab_id"] and p is not pane]
        after = next((p for p in others if p["rect"][pos] == r[pos] + r[size]), None)
        before = next((p for p in others if p["rect"][pos] + p["rect"][size] == r[pos]), None)
        neighbour = after or before
        if neighbour is None:
            raise FakeError("pane_resize_failed", "no neighbour in that direction")
        delta = max(1, round(amount * (r[size] + neighbour["rect"][size])))
        shrink_first = direction in ("left", "up")  # divider moves toward the start
        first, second = (pane, neighbour) if neighbour is after else (neighbour, pane)
        if shrink_first:
            first["rect"][size] -= delta
            second["rect"][pos] -= delta
            second["rect"][size] += delta
        else:
            first["rect"][size] += delta
            second["rect"][pos] += delta
            second["rect"][size] -= delta
        return {}

    def _pane_process_info(self, a):
        pane = self._get(a.opt("--pane") or a.pos[0])
        return {"process_info": {"shell_pid": pane["pid"], "foreground_process_group_id": pane["pid"] + 1,
                                 "foreground_processes": [{"pid": pane["pid"] + 1, "name": pane["process"]}]}}

    def _pane_read(self, a):
        pane = self.panes.get(a.pos[0]) if a.pos else None
        if pane is None:
            raise FakeError("pane_not_found", "pane not found")
        lines = int(a.opt("--lines", "40"))
        return "\n".join(pane["screen"].splitlines()[-lines:])

    # ---- agent -----------------------------------------------------------

    def _agent_start(self, a):
        name, kind, target = a.pos[0], a.opt("--kind"), a.opt("--pane")
        if not AGENT_NAME_RE.match(name):
            raise FakeError("invalid_agent_name",
                            "agent name must start with a lowercase letter and contain only "
                            "lowercase letters, digits, '-' or '_' (1-32 characters)")
        if kind not in AGENT_KINDS:
            raise FakeError("unsupported_agent_kind", f"unsupported interactive agent kind {kind}")
        if any(any(not ch.isprintable() for ch in arg) for arg in a.rest):
            raise FakeError("invalid_agent_argument",
                            "agent arguments cannot be encoded safely for the target shell")
        conflicts = self._name_conflicts(name)
        if conflicts:
            raise FakeError("agent_name_taken", f"agent name {name} is already used; candidates: "
                            + "; ".join(f"pane_id={p['pane_id']}" for p in conflicts))
        pane = self.panes.get(target)
        if pane is None:
            raise FakeError("agent_pane_not_found", f"agent target pane {target} not found")
        if pane["agent"] or pane["process"] != SHELL:
            raise FakeError("agent_pane_busy", f"agent target pane {target} is not an available shell")
        timeout = int(a.opt("--timeout", "30000"))
        if timeout <= 3000 or timeout > 300000:
            raise FakeError("invalid_agent_timeout",
                            "agent start timeout must be greater than 3000ms and at most 300000ms")
        session = self._resumed_session(kind, a.rest) or self._new_session()
        pane.update(agent=kind, agent_status="idle", name=name, process=kind, session=session)
        self._save_transcript(pane)
        self.launches.append({"pane_id": pane["pane_id"], "name": name, "kind": kind,
                              "args": list(a.rest), "session": session, "plain_resume": False})
        return {"agent": self._agent_info(pane), "argv": [kind, *a.rest]}

    @staticmethod
    def _resumed_session(kind, args):
        if kind == "claude":
            for flag in ("--resume", "-r"):
                if flag in args and args.index(flag) + 1 < len(args):
                    return args[args.index(flag) + 1]
        if kind == "codex" and args[:1] == ["resume"] and len(args) > 1:
            return args[-1]
        return None

    def _agent_get(self, a):
        return {"agent": self._agent_info(self._resolve_agent(a.pos[0]))}

    def _agent_list(self, a):
        return {"agents": [self._agent_info(p) for p in self.panes.values() if p["agent"]]}

    def _agent_rename(self, a):
        pane = self._resolve_agent(a.pos[0])
        if "--clear" in a.flags:
            pane["name"] = None
            return {"agent": self._agent_info(pane)}
        name = a.pos[1]
        if not AGENT_NAME_RE.match(name):
            raise FakeError("invalid_agent_name", "invalid agent name")
        if self._name_conflicts(name, pane["terminal_id"]):
            raise FakeError("agent_name_taken", f"agent name {name} is already used")
        pane["name"] = name
        return {"agent": self._agent_info(pane)}

    def _agent_prompt(self, a):
        pane = self._resolve_agent(a.pos[0])
        self.prompts.append({"pane_id": pane["pane_id"], "text": a.pos[1], "wait": "--wait" in a.flags})
        return {"agent": self._agent_info(pane)}

    def _agent_wait(self, a):
        return {"agent": self._agent_info(self._resolve_agent(a.pos[0]))}

    def _agent_focus(self, a):
        pane = self._resolve_agent(a.pos[0])
        self._focus(pane)
        return {"agent": self._agent_info(pane)}

    def _agent_read(self, a):
        pane = self._resolve_agent(a.pos[0])
        return {"text": pane["screen"]}

    # ---- plugin panes ----------------------------------------------------

    def _plugin_pane_open(self, a):
        entry = a.opt("--entrypoint")
        if a.opt("--workspace") and a.opt("--target-pane"):
            # herdr 0.9.x rejects the combination (see toggle.open_map).
            raise FakeError("invalid_params", "--workspace conflicts with --target-pane")
        anchor = self._get(a.opt("--target-pane"))
        pane = self._split(anchor, a.opt("--direction", "right"))
        pane.update(label=PLUGIN_PANE_TITLES.get(entry, entry), process="python3",
                    plugin={"plugin_id": a.opt("--plugin"), "entrypoint": entry})
        if entry in PLUGIN_PANE_VIEW:
            pane["tokens"][hc.TOKEN_VIEW] = PLUGIN_PANE_VIEW[entry]
        if "--focus" in a.flags:
            self._focus(pane)
        return {"plugin_pane": {"plugin_id": a.opt("--plugin"), "entrypoint": entry,
                                "pane": self._pane_info(pane)}}

    def _plugin_pane_close(self, a):
        pane = self._get(a.pos[0])
        if not pane["plugin"]:
            raise FakeError("plugin_pane_not_found", f"{a.pos[0]} is not a plugin pane")
        self._remove_pane(a.pos[0])
        self.closed.append(a.pos[0])
        return {}

    def _plugin_pane_focus(self, a):
        pane = self._get(a.pos[0])
        if not pane["plugin"]:
            raise FakeError("plugin_pane_not_found", f"{a.pos[0]} is not a plugin pane")
        self._focus(pane)
        return {}

    # ---- misc ------------------------------------------------------------

    def _notification_show(self, a):
        self.notifications.append({"title": a.pos[0] if a.pos else "", "body": a.opt("--body"),
                                   "sound": a.opt("--sound")})
        return {}

    def _server_reload_config(self, a):
        return {}

    def _integration_status(self, a):
        return {"integrations": [{"agent": "claude", "installed": True}]}
