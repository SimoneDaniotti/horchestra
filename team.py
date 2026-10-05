#!/usr/bin/env python3
"""Per-space agent teams declared in team.toml.

team.toml is the single source of truth for a space's team. It starts with
only an orchestrator; the orchestrator grows and shrinks the team with
`agentmap-team hire/fire`, or edits the file and runs `agentmap-team sync`.

Each member's agent session id is recorded in team.toml. Herdr persists
those ids and resumes the same sessions after a server restart, so `restore`
(run by the plugin's startup hook) can find every member again and repair
names, map tags, and the orchestrator's instructions. Pane labels
(`team:<role>`) are the fallback when a session did not resume.
"""

import argparse
import json
import os
import re
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.realpath(__file__)))

import herdr_client as hc  # noqa: E402

try:
    import tomllib
except ImportError:  # Python < 3.11
    tomllib = None

PLUGIN_ROOT = os.path.dirname(os.path.realpath(__file__))
TEAM_FILE = "team.toml"
LABEL_PREFIX = "team:"
ORCHESTRATOR = "orchestrator"
ROLE_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_-]{0,23}$")
START_TIMEOUT_MS = 90000
READY_WAIT_MS = 300000
RESTORE_SECONDS = 1800
IDLE_TEAM_SECONDS = 300
STATE_DIR = os.environ.get("AGENTMAP_STATE_DIR") or os.path.expanduser(
    "~/.local/state/herdr-agent-map"
)
REGISTRY = os.path.join(STATE_DIR, "teams.json")
TERMINALS = os.path.join(STATE_DIR, "terminals.json")
AGENT_FILE = os.path.expanduser("~/.claude/agents/orchestrator.md")


class TeamError(Exception):
    pass


# ---- team.toml ----------------------------------------------------------


def find_team_file(start):
    """Walk up from `start` looking for team.toml."""
    path = os.path.abspath(start)
    while True:
        candidate = os.path.join(path, TEAM_FILE)
        if os.path.isfile(candidate):
            return candidate
        parent = os.path.dirname(path)
        if parent == path:
            return None
        path = parent


def project_root(start):
    """Git root of `start` if there is one, else `start` itself."""
    path = os.path.abspath(start)
    walk = path
    while True:
        if os.path.exists(os.path.join(walk, ".git")):
            return walk
        parent = os.path.dirname(walk)
        if parent == walk:
            return path
        walk = parent


def load(path):
    if tomllib is None:
        raise TeamError("python 3.11+ is required to read team.toml")
    try:
        with open(path, "rb") as fh:
            data = tomllib.load(fh)
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise TeamError(f"cannot read {path}: {exc}") from exc
    orch = data.get("orchestrator")
    data["orchestrator"] = orch if isinstance(orch, dict) else {}
    data["orchestrator"].setdefault("kind", "claude")
    members = data.get("member")
    members = [m for m in members if isinstance(m, dict)] if isinstance(members, list) else []
    seen = set()
    for m in members:
        role = m.get("role")
        if not isinstance(role, str) or not ROLE_RE.match(role):
            raise TeamError(f"invalid member role {role!r} in {path}")
        if role.lower() in seen or role.lower() == ORCHESTRATOR:
            raise TeamError(f"duplicate member role {role!r} in {path}")
        seen.add(role.lower())
    data["member"] = members
    return data


def _value(value):
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return repr(value)
    if isinstance(value, list):
        return "[" + ", ".join(_value(v) for v in value) + "]"
    # JSON string escapes are valid TOML basic-string escapes.
    return json.dumps(str(value), ensure_ascii=False)


def _table(lines, header, table):
    lines.append(header)
    for key, value in table.items():
        if value is None or isinstance(value, dict):
            continue
        lines.append(f"{key} = {_value(value)}")
    lines.append("")


def dump(team):
    lines = [
        "# Agent team for this space, managed by `agentmap-team`.",
        "# The orchestrator hires and fires members; edit by hand, then run",
        "# `agentmap-team sync` to apply.",
        "",
    ]
    for key, value in team.items():
        if key not in ("orchestrator", "member") and not isinstance(value, (dict, list)):
            lines.append(f"{key} = {_value(value)}")
    if lines[-1] != "":
        lines.append("")
    _table(lines, "[orchestrator]", team["orchestrator"])
    for member in team["member"]:
        _table(lines, "[[member]]", member)
    return "\n".join(lines)


def save(path, team):
    tmp = path + ".tmp"
    with open(tmp, "w") as fh:
        fh.write(dump(team))
    os.replace(tmp, path)


def new_team(kind="claude"):
    return {"default_kind": kind, "orchestrator": {"kind": kind}, "member": []}


# ---- herdr context ------------------------------------------------------


class Space:
    """The workspace a team lives in, plus a cached pane list."""

    def __init__(self, workspace_id, team_file, team):
        self.workspace_id = workspace_id
        self.team_file = team_file
        self.root = os.path.dirname(team_file)
        self.team = team
        self.refresh()

    def refresh(self):
        self.panes = hc.list_panes(self.workspace_id)
        self.by_label = {
            p.get("label"): p for p in self.panes if isinstance(p.get("label"), str)
        }
        self.by_session = {session_of(p): p for p in self.panes if session_of(p)}

    def entry(self, role):
        if role == ORCHESTRATOR:
            return self.team["orchestrator"]
        return next((m for m in self.team["member"] if m["role"] == role), None)

    def pane(self, role):
        """Find a role's pane by its recorded agent session, else its label."""
        entry = self.entry(role) or {}
        pane = self.by_session.get(entry.get("session"))
        return pane or self.by_label.get(LABEL_PREFIX + role)

    def agent_name(self, role):
        # Derived from the project dir, not the workspace id, so names stay
        # the same after a Herdr restart renumbers workspaces.
        project = re.sub(r"[^a-z0-9_-]", "-", os.path.basename(self.root).lower())
        role = re.sub(r"[^a-z0-9_-]", "-", role.lower())
        name = f"{project[: 31 - len(role) - 1]}-{role}"
        if not name[:1].isalpha():
            name = "t" + name
        return name[:32]


def session_of(pane):
    session = pane.get("agent_session")
    value = session.get("value") if isinstance(session, dict) else None
    return value if isinstance(value, str) and value else None


def register(team_file):
    """Remember team files so the startup hook can restore them."""
    teams = registered()
    if team_file not in teams:
        os.makedirs(STATE_DIR, exist_ok=True)
        with open(REGISTRY, "w") as fh:
            json.dump(sorted(set(teams) | {team_file}), fh, indent=2)


def _read_json(path, default):
    try:
        with open(path) as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return default


def known_terminals(team_file):
    data = _read_json(TERMINALS, {})
    entry = data.get(team_file) if isinstance(data, dict) else None
    return entry if isinstance(entry, dict) else {}


def remember_terminals(team_file, terminals):
    """A restart gives every pane a new terminal_id; a live handoff keeps them."""
    data = _read_json(TERMINALS, {})
    data = data if isinstance(data, dict) else {}
    if data.get(team_file) == terminals:
        return
    data[team_file] = terminals
    os.makedirs(STATE_DIR, exist_ok=True)
    with open(TERMINALS, "w") as fh:
        json.dump(data, fh, indent=2)


def registered():
    try:
        with open(REGISTRY) as fh:
            teams = json.load(fh)
    except (OSError, ValueError):
        return []
    return [t for t in teams if isinstance(t, str)] if isinstance(teams, list) else []


def plugin_context():
    try:
        ctx = json.loads(os.environ.get("HERDR_PLUGIN_CONTEXT_JSON") or "{}")
    except ValueError:
        ctx = {}
    return ctx if isinstance(ctx, dict) else {}


def caller_pane():
    ctx = plugin_context()
    for value in (os.environ.get("HERDR_PANE_ID"), ctx.get("pane_id"), ctx.get("focused_pane_id")):
        if isinstance(value, str) and value:
            return value
    return None


def resolve_space(team_file=None, create=False):
    pane_id = caller_pane()
    pane = hc.get_pane(pane_id) if pane_id else {}
    workspace = (
        pane.get("workspace_id")
        or os.environ.get("HERDR_WORKSPACE_ID")
        or plugin_context().get("workspace_id")
    )
    if not workspace:
        raise TeamError("run this inside a Herdr pane (no workspace context)")
    cwd = pane.get("foreground_cwd") or pane.get("cwd") or os.getcwd()
    path = team_file or os.environ.get("AGENTMAP_TEAM_FILE")
    # Plugin actions run from the plugin root, so only trust the shell cwd
    # when invoked from a terminal (e.g. by the orchestrator).
    if not path and not os.environ.get("HERDR_PLUGIN_ID"):
        path = find_team_file(os.getcwd())
    path = path or find_team_file(cwd)
    if not path:
        if not create:
            raise TeamError(f"no {TEAM_FILE} found from {cwd}; run `agentmap-team up` first")
        path = os.path.join(project_root(cwd), TEAM_FILE)
        save(path, new_team())
    path = os.path.abspath(path)
    register(path)
    return Space(workspace, path, load(path)), pane


# ---- spawning -----------------------------------------------------------


def start_agent(name, kind, pane_id, args):
    """Start an agent in a fresh shell pane, retrying while the shell boots."""
    argv = ["agent", "start", name, "--kind", kind, "--pane", pane_id,
            "--timeout", str(START_TIMEOUT_MS)]
    if args:
        argv += ["--", *args]
    last = None
    for attempt in range(5):
        try:
            return hc.call(*argv, timeout=START_TIMEOUT_MS / 1000 + 10)
        except hc.HerdrError as exc:
            last = exc
            if "agent_not_ready" in str(exc):
                return wait_until_ready(name, pane_id)
            if "timeout" in str(exc):
                break
            time.sleep(1.0 + attempt)
    raise TeamError(f"could not start {kind} as {name}: {last}")


def wait_until_ready(name, pane_id, log=print):
    """The agent is up but asking the human something (e.g. folder trust).

    Never answer on the human's behalf: wait for them, then continue.
    """
    log(f"{name} is waiting for your answer in pane {pane_id}; continuing once it is ready…")
    try:
        return hc.call("agent", "wait", name, "--until", "idle", "--until", "done",
                       "--timeout", str(READY_WAIT_MS), timeout=READY_WAIT_MS / 1000 + 10)
    except hc.HerdrError as exc:
        raise TeamError(f"{name} is still not ready; answer its prompt, then run `agentmap-team sync`") from exc


def prompt(target, text):
    hc.call("agent", "prompt", target, text, timeout=60)


def split_largest(tab_panes, cwd):
    """Split the largest pane of a tab along its longer side."""
    layout = (hc.call("pane", "edges", "--pane", tab_panes[0]).get("edges") or {}).get("layout") or {}
    rects = {p.get("pane_id"): p.get("rect") or {} for p in layout.get("panes") or []}
    target = max(tab_panes, key=lambda p: rects.get(p, {}).get("width", 0) * rects.get(p, {}).get("height", 0))
    rect = rects.get(target, {})
    # Terminal cells are ~2x taller than wide.
    direction = "right" if rect.get("width", 0) >= 2.2 * rect.get("height", 0) else "down"
    result = hc.call("pane", "split", target, "--direction", direction, "--cwd", cwd, "--no-focus")
    return result["pane"]["pane_id"]


def new_member_pane(space, cwd):
    """Members live in a `team` tab of the space, tiled as they are hired."""
    tabs = hc.call("tab", "list", "--workspace", space.workspace_id).get("tabs") or []
    tab = next((t for t in tabs if t.get("label") == "team"), None)
    if tab:
        in_tab = [p["pane_id"] for p in space.panes if p.get("tab_id") == tab.get("tab_id")]
        if in_tab:
            return split_largest(in_tab, cwd)
    created = hc.call("tab", "create", "--workspace", space.workspace_id,
                      "--label", "team", "--cwd", cwd)
    return created["root_pane"]["pane_id"]


def member_brief(space, member):
    return (
        f"You are the {member['role']} member of an agent team in this Herdr space, "
        f"coordinated by an orchestrator agent (Herdr agent "
        f"`{space.agent_name(ORCHESTRATOR)}`). Your brief:\n\n"
        f"{member.get('task') or '(wait for instructions from the orchestrator)'}\n\n"
        "Work only on your brief and do not edit team.toml. "
        + REPORT_HOWTO
        + "When you finish, end with a short summary of what you changed and "
        "anything the orchestrator must know."
    )


REPORT_HOWTO = (
    "Keep the human's agent map current: at each milestone run "
    "`agentmap-team report \"<short status, e.g. tests 3/5 passing>\"`. If you "
    "need a decision from the human, run "
    "`agentmap-team report --needs-you \"<the question>\"`, ask it, and stop. "
)


def orchestrator_protocol(space, team):
    """The orchestrator instructions: orchestrator.md without its frontmatter."""
    with open(os.path.join(PLUGIN_ROOT, "agents", "orchestrator.md")) as fh:
        text = fh.read()
    if text.startswith("---"):
        text = text.split("---", 2)[2]
    brief = team["orchestrator"].get("brief")
    text = text.strip() + f"\n\nThis space's team file: {space.team_file}\n"
    return text + (f"\nProject brief from the human:\n{brief}\n" if brief else "")


def start_orchestrator(space, team, near_pane):
    """Start the orchestrator in the caller's shell pane, or split beside it."""
    orch = team["orchestrator"]
    kind = orch.get("kind", "claude")
    args = [str(a) for a in orch.get("args", [])]
    protocol = orchestrator_protocol(space, team)
    if kind == "claude" and os.path.isfile(AGENT_FILE):
        # The installed session agent carries the protocol, and
        # `claude --resume` keeps it.
        args = ["--agent", "orchestrator", *args]
    elif kind == "claude":
        # Multi-line text cannot be passed safely as a shell argument; hand
        # Claude a file instead so the protocol lives in its system prompt.
        os.makedirs(STATE_DIR, exist_ok=True)
        prompt_file = os.path.join(STATE_DIR, f"{space.agent_name(ORCHESTRATOR)}.md")
        with open(prompt_file, "w") as fh:
            fh.write(protocol)
        args = ["--append-system-prompt-file", prompt_file, *args]

    target = near_pane.get("pane_id") if near_pane else None
    if not target or near_pane.get("agent") or near_pane.get("workspace_id") != space.workspace_id:
        base = target if target and near_pane.get("workspace_id") == space.workspace_id else space.panes[0]["pane_id"]
        target = hc.call("pane", "split", base, "--direction", "right",
                         "--cwd", space.root, "--focus")["pane"]["pane_id"]
    hc.call("pane", "rename", target, LABEL_PREFIX + ORCHESTRATOR)
    start_agent(space.agent_name(ORCHESTRATOR), kind, target, args)
    if kind != "claude":
        prompt(target, protocol)
    return wait_for_session(space, target)


def spawn_member(space, team, member):
    role = member["role"]
    cwd = os.path.join(space.root, member.get("cwd", "")) if member.get("cwd") else space.root
    pane = space.pane(role)
    if pane is None:
        pane_id = new_member_pane(space, cwd)
        hc.call("pane", "rename", pane_id, LABEL_PREFIX + role)
    else:
        pane_id = pane["pane_id"]
    kind = member.get("kind") or team.get("default_kind", "claude")
    start_agent(space.agent_name(role), kind, pane_id, [str(a) for a in member.get("args", [])])
    prompt(pane_id, member_brief(space, member))
    return wait_for_session(space, pane_id)


def wait_for_session(space, pane_id, seconds=10.0):
    """Give the integration a moment to report the new agent's session id."""
    deadline = time.monotonic() + seconds
    while True:
        space.refresh()
        pane = next((p for p in space.panes if p.get("pane_id") == pane_id), None)
        if pane is None or session_of(pane) or time.monotonic() >= deadline:
            return pane
        time.sleep(0.5)


# ---- reconcile ----------------------------------------------------------


def ordered_members(members):
    """Parents before children so reports_to edges resolve."""
    roles = {m["role"] for m in members}
    done, out = set(), []

    def visit(m, stack=()):
        if m["role"] in done or m["role"] in stack:
            return
        boss = m.get("reports_to")
        if boss in roles:
            visit(next(x for x in members if x["role"] == boss), stack + (m["role"],))
        done.add(m["role"])
        out.append(m)

    for m in members:
        visit(m)
    return out


def adopt(space, role, pane, remind=False, log=print):
    """Make `pane` recognisable as `role`: session, label, and agent name.

    Returns True when the agent had lost its name, i.e. it was restarted or
    resumed since we last saw it.
    """
    entry = space.entry(role)
    session = session_of(pane)
    if entry is not None and session and entry.get("session") != session:
        entry["session"] = session
        space.dirty = True
    if pane.get("label") != LABEL_PREFIX + role:
        hc.call_quiet("pane", "rename", pane["pane_id"], LABEL_PREFIX + role)
    if not pane.get("agent"):
        return False
    name = space.agent_name(role)
    named = (hc.call_quiet("agent", "get", name) or {}).get("agent") or {}
    if named.get("pane_id") == pane["pane_id"]:
        return False
    if hc.call_quiet("agent", "rename", pane["pane_id"], name) is None:
        log(f"note: could not name {role} agent {name}")
    return True


def sync(space, near_pane=None, start_orch=False, spawn=True, only=None, rebrief=False, log=print):
    team = space.team
    space.dirty = False
    orch = space.pane(ORCHESTRATOR)
    if start_orch and (orch is None or not orch.get("agent")):
        log("starting orchestrator…")
        # Reuse a known orchestrator pane whose agent has exited.
        orch = start_orchestrator(space, team, orch or near_pane)
    refs = {}
    if orch:
        if adopt(space, ORCHESTRATOR, orch, log=log) and rebrief:
            # Resumed after a restart: --append-system-prompt-file is not
            # replayed by `claude --resume`, so hand the protocol back.
            log("re-briefing resumed orchestrator…")
            hc.call_quiet("agent", "prompt", orch["pane_id"], RESUME_NOTE + orchestrator_protocol(space, team), timeout=60)
        hc.set_tokens(orch["pane_id"], {hc.TOKEN_ROLE: ORCHESTRATOR,
                                        hc.TOKEN_TASK: "coordinates the team"}, clear=[hc.TOKEN_PARENT])
        refs[ORCHESTRATOR] = orch.get("terminal_id") or orch["pane_id"]

    for member in ordered_members(team["member"]):
        role = member["role"]
        pane = space.pane(role)
        if spawn and (only is None or role in only) and (pane is None or not pane.get("agent")):
            log(f"starting {role}…")
            pane = spawn_member(space, team, member)
        if not pane:
            continue
        adopt(space, role, pane, log=log)
        refs[role] = pane.get("terminal_id") or pane["pane_id"]
        parent = refs.get(member.get("reports_to")) or refs.get(ORCHESTRATOR)
        tokens = {hc.TOKEN_ROLE: role}
        if member.get("task"):
            tokens[hc.TOKEN_TASK] = " ".join(str(member["task"]).split())[:80]
        if parent:
            tokens[hc.TOKEN_PARENT] = parent
        hc.set_tokens(pane["pane_id"], tokens)

    if space.dirty:
        save(space.team_file, team)
    remember_terminals(space.team_file, {
        role: pane.get("terminal_id")
        for role in [ORCHESTRATOR] + [m["role"] for m in team["member"]]
        for pane in [space.pane(role)]
        if pane and pane.get("agent") and pane.get("terminal_id")
    })

    known = {p["pane_id"] for p in (space.pane(r) for r in [ORCHESTRATOR] + [m["role"] for m in team["member"]]) if p}
    for label, pane in space.by_label.items():
        if label.startswith(LABEL_PREFIX) and pane["pane_id"] not in known:
            log(f"note: pane {label} is not in team.toml (fire it or add it back)")


RESUME_NOTE = (
    "[agent-map] Herdr restarted and resumed this session. {missing}Run "
    "`agentmap-team status` before continuing; do not redo finished work."
)

FRESH_NOTE = (
    "[agent-map] Herdr restarted and your previous orchestrator conversation "
    "could not be resumed, so you are a fresh orchestrator for the existing "
    "team in team.toml. {missing}Run `agentmap-team status`, read members' "
    "recent output with `herdr agent read`, and continue coordinating."
)

RESUMED_GRACE_SECONDS = 45  # once some agents resumed, wait this long for the rest
UNRESUMED_GRACE_SECONDS = 180  # when nothing resumed yet (client may attach late)
MAP_LABEL = "Agent map"


def is_live(pane):
    return bool(pane and pane.get("agent") and pane.get("agent_status") not in (None, "unknown"))


def restore(log=print):
    """Startup hook: wait for Herdr to resume team agents, then repair them.

    Members are never started here: one whose session did not resume is left
    as a labelled shell and reported to the orchestrator, which can re-hire
    it. An orchestrator that could not resume is started fresh in its pane.
    """
    pending, state = {}, {}
    for path in registered():
        if os.path.isfile(path):
            try:
                pending[path] = load(path)
                state[path] = {"first_seen": None, "orch_resumed": None,
                               "terminals": known_terminals(path)}
            except TeamError as exc:
                log(f"skip {path}: {exc}")
    started = time.monotonic()
    while pending and time.monotonic() < started + RESTORE_SECONDS:
        try:
            panes = hc.call("pane", "list").get("panes") or []
        except hc.HerdrError:
            time.sleep(5)
            continue
        by_session = {session_of(p): p for p in panes if isinstance(p, dict) and session_of(p)}
        now = time.monotonic()
        for path, team in list(pending.items()):
            st = state[path]
            entries = [team["orchestrator"]] + team["member"]
            sessions = [e["session"] for e in entries if e.get("session")]
            found = [by_session[s] for s in sessions if s in by_session]
            if not found and st["first_seen"] is None:
                # Nothing of this team is here: likely an old team.
                if now - started > IDLE_TEAM_SECONDS:
                    del pending[path]
                continue
            if found:
                workspaces = [p.get("workspace_id") for p in found]
                st["workspace"] = max(set(workspaces), key=workspaces.count)
            st["first_seen"] = st["first_seen"] or now
            space = Space(st["workspace"], path, team)
            roles = [ORCHESTRATOR] + [m["role"] for m in team["member"]]
            live = {r for r in roles if is_live(space.pane(r))}

            orch = space.pane(ORCHESTRATOR)
            # Decide from terminals recorded before startup: a live handoff
            # keeps the orchestrator's terminal, a restart replaces it.
            if orch and orch.get("agent") and st["orch_resumed"] is None:
                before = st["terminals"].get(ORCHESTRATOR)
                st["orch_resumed"] = bool(before) and before != orch.get("terminal_id")
            if live:
                sync(space, spawn=False, log=lambda _msg: None)

            grace = RESUMED_GRACE_SECONDS if live else UNRESUMED_GRACE_SECONDS
            if len(live) == len(roles) or now - st["first_seen"] > grace:
                finish_restore(space, live, st, log=lambda msg, p=path: log(f"{p}: {msg}"))
                del pending[path]
        time.sleep(3)


def deliver(target, text, log=print, attempts=4):
    """Send a prompt to a just-(re)started agent and confirm a turn began.

    A resumed TUI can report idle before its input box is ready and drop the
    text, so wait for a turn to start and retry when Herdr reports a stall.
    """
    time.sleep(5)
    for _ in range(attempts):
        try:
            hc.call("agent", "prompt", target, text, "--wait", "--timeout", "20000", timeout=90)
            return True
        except hc.HerdrError as exc:
            if "timeout" in str(exc):
                return True  # the turn started and is still running
            if "stalled" not in str(exc):
                log(f"could not message {target}: {exc}")
                return False
            time.sleep(10)
    log(f"could not message {target}: it never started a turn")
    return False


def finish_restore(space, live, st, log=print):
    team = space.team
    missing = [m["role"] for m in team["member"] if m["role"] not in live]
    note = (
        f"Members that did not resume (their panes are idle shells; fire and "
        f"re-hire them if still needed): {', '.join(missing)}. "
        if missing else ""
    )
    if ORCHESTRATOR not in live:
        log("orchestrator did not resume; starting a fresh one")
        try:
            orch = start_orchestrator(space, team, space.pane(ORCHESTRATOR))
            sync(space, spawn=False, log=lambda _msg: None)
            deliver(orch["pane_id"], FRESH_NOTE.format(missing=note), log=log)
        except (TeamError, hc.HerdrError) as exc:
            log(f"could not start orchestrator: {exc}")
    elif st.get("orch_resumed"):
        log("re-briefing resumed orchestrator")
        orch = space.pane(ORCHESTRATOR)
        text = RESUME_NOTE.format(missing=note)
        if not os.path.isfile(AGENT_FILE):
            # Started with --append-system-prompt-file, which resume drops.
            text += "\n\nYour orchestrator instructions:\n\n" + orchestrator_protocol(space, team)
        deliver(orch["pane_id"], text, log=log)
    replace_dead_maps(space, log=log)
    log("restored" + (f" (not resumed: {', '.join(missing)})" if missing else ""))


def team_tabs(space):
    roles = [ORCHESTRATOR] + [m["role"] for m in space.team["member"]]
    return {p.get("tab_id") for p in (space.pane(r) for r in roles) if p and p.get("tab_id")}


def show_maps(space, force=False, log=print):
    """Keep a map in every tab that holds a team agent.

    Only adds maps while the space already shows at least one (the human may
    have toggled them off), unless `force` is set.
    """
    import toggle

    space.refresh()
    if not force and not toggle.map_panes(space.panes):
        return
    try:
        toggle.ensure_maps(space.workspace_id, team_tabs(space))
    except hc.HerdrError as exc:
        log(f"could not open the agent map: {exc}")
    space.refresh()


def replace_dead_maps(space, log=print):
    """Map panes come back from a restart as idle shells; reopen them."""
    space.refresh()
    dead = [
        p["pane_id"] for p in space.panes
        if p.get("label") == MAP_LABEL and not (p.get("tokens") or {}).get(hc.TOKEN_VIEW)
    ]
    if not dead:
        return
    for pane_id in dead:
        hc.call_quiet("pane", "close", pane_id)
    show_maps(space, force=True, log=log)


# ---- commands -----------------------------------------------------------


def cmd_up(args):
    space, pane = resolve_space(args.file, create=True)
    orch = space.pane(ORCHESTRATOR)
    needs_start = orch is None or not orch.get("agent")
    if (needs_start and not os.environ.get("HERDR_PLUGIN_ID") and not os.environ.get("AGENTMAP_DETACHED")
            and pane.get("pane_id") and not pane.get("agent") and (orch is None or orch["pane_id"] == pane["pane_id"])):
        # Run from the very shell the orchestrator should take over: that
        # shell is busy running us, so finish in the background and return.
        detach_up(args, space)
        return
    sync(space, near_pane=pane, start_orch=True)
    if not args.no_map:
        replace_dead_maps(space)
        show_maps(space, force=True)
    print(f"team ready: {space.team_file}")


def detach_up(args, space):
    import subprocess

    os.makedirs(STATE_DIR, exist_ok=True)
    log_path = os.path.join(STATE_DIR, "up.log")
    argv = [sys.executable, os.path.realpath(__file__), "--file", space.team_file, "up"]
    if args.no_map:
        argv.append("--no-map")
    with open(log_path, "a") as log:
        subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=log, stderr=log,
                         start_new_session=True, env={**os.environ, "AGENTMAP_DETACHED": "1"})
    print(f"starting the orchestrator in this pane… (team: {space.team_file}, log: {log_path})")


def cmd_adopt(args):
    """Bring an already running agent into the team without restarting it."""
    if not ROLE_RE.match(args.role) or args.role.lower() == ORCHESTRATOR:
        raise TeamError("role must match [A-Za-z][A-Za-z0-9_-]{0,23} and not be 'orchestrator'")
    space, _ = resolve_space(args.file, create=True)
    team = space.team
    pane = next((p for p in space.panes if p.get("pane_id") == args.pane), None)
    if pane is None:
        raise TeamError(f"pane {args.pane} is not in this space")
    if not pane.get("agent"):
        raise TeamError(f"pane {args.pane} has no running agent; use `hire` instead")
    if any(m["role"].lower() == args.role.lower() for m in team["member"]):
        raise TeamError(f"{args.role} is already on the team")
    member = {"role": args.role, "kind": pane["agent"],
              "task": args.task or "(adopted with its existing conversation)"}
    if args.reports_to:
        member["reports_to"] = args.reports_to
    if session_of(pane):
        member["session"] = session_of(pane)
    team["member"].append(member)
    save(space.team_file, team)
    # Label the pane now so it is found even before a session id is known.
    hc.call_quiet("pane", "rename", pane["pane_id"], LABEL_PREFIX + args.role)
    space.refresh()
    sync(space, spawn=False)
    show_maps(space)
    print(f"adopted {args.pane} as {args.role} (agent {space.agent_name(args.role)})")


def cmd_init(args):
    """Register the calling agent session as this space's orchestrator."""
    space, me = resolve_space(args.file, create=True)
    if not me.get("pane_id") or not me.get("agent"):
        raise TeamError("run `agentmap-team init` from the orchestrator's own agent session")
    team = space.team
    current = space.pane(ORCHESTRATOR)
    if current and current["pane_id"] != me["pane_id"]:
        if current.get("agent"):
            raise TeamError(f"this space already has a live orchestrator in pane {current['pane_id']}")
        hc.call_quiet("pane", "rename", current["pane_id"], "--clear")  # stale label
    team["orchestrator"]["kind"] = me["agent"]
    team["orchestrator"].pop("session", None)
    if session_of(me):
        team["orchestrator"]["session"] = session_of(me)
    save(space.team_file, team)
    hc.call_quiet("pane", "rename", me["pane_id"], LABEL_PREFIX + ORCHESTRATOR)
    space.refresh()
    sync(space, spawn=False)
    replace_dead_maps(space)
    show_maps(space, force=True)
    print(f"you are the orchestrator of {space.team_file} "
          f"(agent {space.agent_name(ORCHESTRATOR)}, pane {me['pane_id']}); "
          f"{len(team['member'])} member(s) on the team")


def cmd_scan(args):
    """List agents in this space that are not on the team."""
    space, _ = resolve_space(args.file, create=True)
    team = space.team
    managed = {p["pane_id"] for p in (space.pane(r) for r in [ORCHESTRATOR] + [m["role"] for m in team["member"]]) if p}
    tabs = {t.get("tab_id"): t.get("label") for t in
            hc.call("tab", "list", "--workspace", space.workspace_id).get("tabs") or []}
    rows = [p for p in space.panes if p.get("agent") and p["pane_id"] not in managed]
    if not rows:
        print("no unmanaged agents in this space")
        return
    print(f"{'PANE':<9}{'TAB':<20}{'KIND':<9}{'STATE':<9}TITLE")
    for p in rows:
        title = p.get("terminal_title_stripped") or ""
        print(f"{p['pane_id']:<9}{str(tabs.get(p.get('tab_id'), '')):<20}{p['agent']:<9}{p.get('agent_status', ''):<9}{title}")


def cmd_report(args):
    """Set the calling agent's status line in the agent map."""
    pane_id = caller_pane()
    if not pane_id:
        raise TeamError("run `agentmap-team report` inside a Herdr pane")
    if args.clear:
        hc.set_tokens(pane_id, clear=[hc.TOKEN_STATUS, hc.TOKEN_NEEDS])
        print("status cleared")
        return
    text = " ".join(" ".join(args.text).split())[:80]
    if not text:
        raise TeamError("give a short status text (or --clear)")
    tokens = {hc.TOKEN_STATUS: text}
    if args.needs_you:
        tokens[hc.TOKEN_NEEDS] = "1"
        hc.set_tokens(pane_id, tokens)
        pane = hc.get_pane(pane_id)
        role = tokens_of(pane).get(hc.TOKEN_ROLE) or pane.get("agent") or "an agent"
        # Delivered through the human's Herdr toast settings ([ui.toast]).
        hc.call_quiet("notification", "show", f"{role} needs you", "--body", text, "--sound", "request")
    else:
        hc.set_tokens(pane_id, tokens, clear=[hc.TOKEN_NEEDS])
    print("reported")


def tokens_of(pane):
    tokens = pane.get("tokens")
    return tokens if isinstance(tokens, dict) else {}


def cmd_sync(args):
    space, pane = resolve_space(args.file)
    sync(space, near_pane=pane)


def cmd_hire(args):
    if not ROLE_RE.match(args.role):
        raise TeamError("role must match [A-Za-z][A-Za-z0-9_-]{0,23}")
    if args.role.lower() == ORCHESTRATOR:
        raise TeamError("the orchestrator role is reserved")
    space, _ = resolve_space(args.file)
    team = space.team
    if any(m["role"].lower() == args.role.lower() for m in team["member"]):
        raise TeamError(f"{args.role} is already on the team (fire it first, or edit team.toml + sync)")
    member = {"role": args.role, "kind": args.kind or team.get("default_kind", "claude"), "task": args.task}
    if args.reports_to:
        member["reports_to"] = args.reports_to
    if args.cwd:
        member["cwd"] = args.cwd
    if args.arg:
        member["args"] = args.arg
    before = dump(team)
    team["member"].append(member)
    save(space.team_file, team)
    try:
        sync(space, only={args.role})
    except (TeamError, hc.HerdrError):
        with open(space.team_file, "w") as fh:
            fh.write(before)
        raise
    show_maps(space)
    pane = space.pane(args.role)
    print(f"hired {args.role}: agent {space.agent_name(args.role)} in pane {pane['pane_id'] if pane else '?'}")


def cmd_fire(args):
    space, _ = resolve_space(args.file)
    team = space.team
    kept = [m for m in team["member"] if m["role"].lower() != args.role.lower()]
    role = next((m["role"] for m in team["member"] if m["role"].lower() == args.role.lower()), args.role)
    pane = space.pane(role)
    if len(kept) == len(team["member"]) and pane is None:
        raise TeamError(f"{args.role} is not on the team")
    team["member"] = kept
    save(space.team_file, team)
    if pane:
        hc.call_quiet("pane", "close", pane["pane_id"])
    print(f"fired {args.role}")


def cmd_status(args):
    space, _ = resolve_space(args.file)
    team = space.team
    sync(space, spawn=False, log=lambda _msg: None)  # records session ids
    rows = [(ORCHESTRATOR, team["orchestrator"].get("kind", "claude"), "")]
    rows += [(m["role"], m.get("kind") or team.get("default_kind", "claude"), m.get("reports_to", "")) for m in team["member"]]
    print(f"team file: {space.team_file}")
    print(f"{'ROLE':<14}{'KIND':<9}{'AGENT':<26}{'PANE':<9}{'STATE':<9}REPORTS TO")
    for role, kind, boss in rows:
        pane = space.pane(role) or {}
        name = space.agent_name(role) if pane.get("agent") else "-"
        state = pane.get("agent_status", "missing") if pane else "missing"
        print(f"{role:<14}{kind:<9}{name:<26}{pane.get('pane_id', '-'):<9}{state:<9}{boss}")


def main(argv=None):
    parser = argparse.ArgumentParser(prog="agentmap-team", description="Manage this space's agent team (team.toml).")
    parser.add_argument("--file", help="path to team.toml (default: search upward from cwd)")
    sub = parser.add_subparsers(dest="command", required=True)

    up = sub.add_parser("up", help="create team.toml if needed, start the orchestrator, open the map")
    up.add_argument("--no-map", action="store_true")
    up.set_defaults(func=cmd_up)

    sub.add_parser("init", help="register the calling agent as this space's orchestrator").set_defaults(func=cmd_init)
    sub.add_parser("scan", help="list running agents in this space that are not on the team").set_defaults(func=cmd_scan)
    sub.add_parser("sync", help="apply team.toml: start missing members, repair tags").set_defaults(func=cmd_sync)
    sub.add_parser("status", help="show the team").set_defaults(func=cmd_status)
    report = sub.add_parser("report", help="set your status line in the agent map (run by members)")
    report.add_argument("text", nargs="*")
    report.add_argument("--needs-you", action="store_true", help="flag that you are waiting on the human")
    report.add_argument("--clear", action="store_true")
    report.set_defaults(func=cmd_report)
    sub.add_parser("restore", help="startup hook: repair teams after a Herdr restart").set_defaults(
        func=lambda _args: restore()
    )

    hire = sub.add_parser("hire", help="add a member and start it")
    hire.add_argument("role")
    hire.add_argument("--task", required=True, help="the member's brief")
    hire.add_argument("--kind", help="agent kind (default: default_kind in team.toml)")
    hire.add_argument("--reports-to", help="parent role (default: orchestrator)")
    hire.add_argument("--cwd", help="working dir relative to team.toml")
    hire.add_argument("--arg", action="append", help="extra agent CLI arg (repeatable)")
    hire.set_defaults(func=cmd_hire)

    adopt_p = sub.add_parser("adopt", help="add an already running agent to the team (no restart)")
    adopt_p.add_argument("pane", help="pane id of the running agent, e.g. w7:p6")
    adopt_p.add_argument("--role", required=True)
    adopt_p.add_argument("--task", help="what it is working on (for the orchestrator)")
    adopt_p.add_argument("--reports-to", help="parent role (default: orchestrator)")
    adopt_p.set_defaults(func=cmd_adopt)

    fire = sub.add_parser("fire", help="remove a member and close its pane")
    fire.add_argument("role")
    fire.set_defaults(func=cmd_fire)

    args = parser.parse_args(argv)
    try:
        args.func(args)
    except (TeamError, hc.HerdrError) as exc:
        print(f"agentmap-team: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
