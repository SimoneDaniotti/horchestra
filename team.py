#!/usr/bin/env python3
"""Per-space agent teams declared in team.toml.

team.toml is the single source of truth for a space's team. It starts with
only an orchestrator; the orchestrator grows and shrinks the team with
`horchestra-team hire/fire`, or edits the file and runs `horchestra-team sync`.

Each member's agent session id is recorded in team.toml. Herdr persists
those ids and resumes the same sessions after a server restart, so `restore`
(run by the plugin's startup hook) can find every member again and repair
names, map tags, and the orchestrator's instructions. Pane labels
(`team:<role>`) are the fallback when a session did not resume; they are
used to find and tag panes, never on their own to close or restart one.
"""

import argparse
import hashlib
import json
import os
import re
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.realpath(__file__)))

import herdr_client as hc  # noqa: E402
import roles  # noqa: E402
import tasks  # noqa: E402

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
LEGACY_STATE_DIR = os.path.expanduser("~/.local/state/herdr-agent-map")


def _state_dir():
    explicit = os.environ.get("HORCHESTRA_STATE_DIR") or os.environ.get("AGENTMAP_STATE_DIR")
    if explicit:
        return explicit
    path = os.path.expanduser("~/.local/state/horchestra")
    if not os.path.exists(path) and os.path.isdir(LEGACY_STATE_DIR):
        try:
            os.rename(LEGACY_STATE_DIR, path)  # one-time move from the pre-0.1 name
        except OSError:
            return LEGACY_STATE_DIR
    return path


STATE_DIR = _state_dir()
REGISTRY = os.path.join(STATE_DIR, "teams.json")
TERMINALS = os.path.join(STATE_DIR, "terminals.json")


def agent_file():
    """The installed orchestrator session agent (honours $CLAUDE_CONFIG_DIR)."""
    return os.path.join(roles.claude_config_dir(), "agents", "orchestrator.md")


# Claude flags Horchestra sets itself; user args must not override them.
RESERVED_CLAUDE_FLAGS = frozenset({
    "--agent", "--settings", "--add-dir", "--resume", "-r", "--continue", "-c",
    "--system-prompt-snapshot", "--name", "-n", "--append-system-prompt",
    "--append-system-prompt-file", "--system-prompt", "--system-prompt-file",
})
STRING_FIELDS = ("role", "kind", "task", "session", "cwd", "profile", "reports_to", "brief",
                 "call_when", "handoff", "reporting")
# Onboarding agreements: (team.toml key, how the member's instructions phrase it).
ONBOARDING = (
    ("call_when", "The orchestrator brings you in for"),
    ("handoff", "How you receive work"),
    ("reporting", "When to report back"),
)
SKILL_FIELDS = ("uses_skills", "only_skills", "deny_skills")


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
    """Git root of `start` if there is one, else `start` itself.

    A .git in the home folder or at / (a dotfiles repo) is not a project.
    """
    path = os.path.abspath(start)
    walk = path
    while True:
        if os.path.exists(os.path.join(walk, ".git")) and not is_unsafe_root(walk):
            return walk
        parent = os.path.dirname(walk)
        if parent == walk:
            return path
        walk = parent


def is_unsafe_root(folder):
    """The home folder or the filesystem root: never a team's project folder.

    A team.toml there is found from every project below it and registered
    for the restart hook, so it is only created when asked for with --file.
    """
    folder = os.path.realpath(folder)
    return folder == os.path.realpath(os.path.expanduser("~")) or os.path.dirname(folder) == folder


def load(path):
    if tomllib is None:
        raise TeamError("python 3.11+ is required to read team.toml")
    try:
        with open(path, "rb") as fh:
            data = tomllib.load(fh)
    except (OSError, ValueError) as exc:
        # TOMLDecodeError and UnicodeDecodeError (a file that is not UTF-8)
        # are both ValueErrors: a bad team.toml is a TeamError, not a crash.
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
    validate(data, os.path.dirname(os.path.abspath(path)), path)
    return data


def validate(team, root, path="team.toml"):
    """Check field types so a bad team.toml fails here, with the field named.

    `args` may be a single string, which is one argument (not split on spaces).
    """
    if "default_kind" in team and not isinstance(team["default_kind"], str):
        raise TeamError(f"{path}: default_kind must be a string")
    if "name_tabs" in team and not isinstance(team["name_tabs"], bool):
        raise TeamError(f"{path}: name_tabs must be true or false")
    if "deny_skills" in team:
        _check_strings(team["deny_skills"], f"{path}: deny_skills")
    entries = [("orchestrator", team["orchestrator"])]
    entries += [(f"member[{i}] ({m['role']})", m) for i, m in enumerate(team["member"])]
    for where, entry in entries:
        validate_entry(team, entry, root, f"{path}: {where}")


def validate_entry(team, entry, root, where):
    for key in STRING_FIELDS:
        if key in entry and not isinstance(entry[key], str):
            raise TeamError(f"{where}.{key} must be a string")
    if isinstance(entry.get("args"), str):
        entry["args"] = [entry["args"]]  # one string is one argument
    if "args" in entry:
        _check_strings(entry["args"], f"{where}.args", allow_one=False)
    for key in SKILL_FIELDS:
        if key in entry:
            _check_strings(entry[key], f"{where}.{key}")
    cwd = entry.get("cwd")
    if cwd:
        inside = os.path.realpath(os.path.join(root, cwd))
        base = os.path.realpath(root)
        if os.path.isabs(cwd) or os.path.commonpath([inside, base]) != base:
            raise TeamError(f"{where}.cwd must be a folder inside {root} (relative to team.toml)")
    kind = entry.get("kind") or team.get("default_kind", "claude")
    if kind == "claude":
        clash = reserved_args(entry.get("args") or [])
        if clash:
            raise TeamError(f"{where}.args: {', '.join(clash)} is set by Horchestra itself; "
                            "remove it (use a role profile for instructions and skill rules)")


def _check_strings(value, where, allow_one=True):
    if allow_one and isinstance(value, str):
        return
    if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
        raise TeamError(f"{where} must be a list of strings")


def reserved_args(args):
    """User args that would override a Claude flag Horchestra sets (flag or flag=value)."""
    return [a for a in args if isinstance(a, str) and a.split("=", 1)[0] in RESERVED_CLAUDE_FLAGS]


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
        "# Agent team for this space, managed by `horchestra-team`.",
        "# The orchestrator hires and fires members; edit by hand, then run",
        "# `horchestra-team sync` to apply.",
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
        self.names = {}
        self.by_label = {
            p.get("label"): p for p in self.panes if isinstance(p.get("label"), str)
        }
        self.labelled = {}
        for p in self.panes:
            if isinstance(p.get("label"), str):
                self.labelled.setdefault(p["label"], []).append(p)
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

    def verified_pane(self, role):
        """A role's pane for operations that close or restart it.

        Returns (pane, None), or (None, why). A label is free text any pane
        can carry, so it is not proof on its own: the pane must run the
        recorded session or, before a session is recorded, carry both the
        label and the role token that sync wrote.
        """
        entry = self.entry(role) or {}
        session = entry.get("session")
        if isinstance(session, str) and session:
            pane = self.by_session.get(session)
            if pane:
                return pane, None
            return None, f"no pane runs {role}'s recorded session"
        labelled = self.labelled.get(LABEL_PREFIX + role, [])
        if len(labelled) > 1:
            ids = ", ".join(p.get("pane_id", "?") for p in labelled)
            return None, f"{len(labelled)} panes are labelled {LABEL_PREFIX}{role} ({ids}); ambiguous"
        if labelled and tokens_of(labelled[0]).get(hc.TOKEN_ROLE) == role:
            return labelled[0], None
        return None, f"no pane is verifiably {role} (no recorded session)"

    def label_only(self, role, pane):
        """True when `pane` matched `role` only by label, against a recorded session.

        Such a pane may be another team's agent sharing the label, so sync
        must not record its session (which would make verified_pane trust it)
        nor tag or rename it.
        """
        recorded = (self.entry(role) or {}).get("session")
        if not (pane and isinstance(recorded, str) and recorded and session_of(pane) != recorded):
            return False
        return not self.same_terminal(role, pane)

    def same_terminal(self, role, pane):
        """Whether `pane` is the terminal last recorded for `role` by this team.

        Terminals are recorded only for verified panes, and a terminal id is
        per pane, so a new session there (e.g. after /clear) is still ours.
        """
        terminal = pane.get("terminal_id") if pane else None
        return bool(terminal) and known_terminals(self.team_file).get(role) == terminal

    def agent_name(self, role):
        """The Herdr agent name for a role, unique across the whole server.

        Herdr agent names are global, so two projects with the same folder
        name (two "api" checkouts) would collide on `<folder>-<role>`. The
        name carries a short hash of the team root, except where this team's
        own pane already holds the plain name (teams named before the hash).
        """
        names = self.__dict__.setdefault("names", {})
        if role not in names:
            plain = self.plain_agent_name(role)
            names[role] = plain if self.holds_name(role, plain) else self.hashed_agent_name(role)
        return names[role]

    def holds_name(self, role, name):
        """Whether the agent called `name` runs in this role's own pane."""
        if getattr(self, "panes", None) is None:
            return False
        pane = self.pane(role)
        if not pane or not pane.get("agent"):
            return False
        named = (hc.call_quiet("agent", "get", name) or {}).get("agent") or {}
        return named.get("pane_id") == pane.get("pane_id")

    def plain_agent_name(self, role):
        # Derived from the project dir, not the workspace id, so names stay
        # the same after a Herdr restart renumbers workspaces. Kept exactly
        # as before so existing teams keep their names.
        project = re.sub(r"[^a-z0-9_-]", "-", os.path.basename(self.root).lower())
        role = re.sub(r"[^a-z0-9_-]", "-", role.lower())
        name = f"{project[: 31 - len(role) - 1]}-{role}"
        if not name[:1].isalpha():
            name = "t" + name
        return name[:32]

    def hashed_agent_name(self, role):
        """`<folder>-<hash>-<role>`, within Herdr's [a-z][a-z0-9_-]{0,31}."""
        digest = hashlib.sha1(os.path.abspath(self.root).encode()).hexdigest()[:4]
        suffix = f"-{digest}-" + re.sub(r"[^a-z0-9_-]", "-", role.lower())
        project = re.sub(r"[^a-z0-9_-]", "-", os.path.basename(self.root).lower())
        if not project[:1].isalpha():
            project = "t" + project
        return project[: 32 - len(suffix)] + suffix


def session_of(pane):
    session = pane.get("agent_session")
    value = session.get("value") if isinstance(session, dict) else None
    return value if isinstance(value, str) and value else None


def register(team_file):
    """Remember team files so the startup hook can restore them."""
    teams = registered()
    if team_file not in teams:
        write_registry(set(teams) | {team_file})


def unregister(team_files):
    """Drop team files from the restart list; returns those that were on it."""
    teams = registered()
    dropped = [t for t in teams if t in set(team_files)]
    if dropped:
        write_registry(t for t in teams if t not in dropped)
    return dropped


def write_registry(teams):
    os.makedirs(STATE_DIR, exist_ok=True)
    tmp = REGISTRY + ".tmp"
    with open(tmp, "w") as fh:
        json.dump(sorted(set(teams)), fh, indent=2)
    os.replace(tmp, REGISTRY)


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


def resolve_space(team_file=None, create=False, read_only=False):
    """The caller's space and its team.

    Only `create` callers (up, init, adopt) may write a new team.toml, and
    never straight into the home folder or / unless the path is given with
    --file. `read_only` callers (scan, roles) work without a team.toml: they
    get an empty team that is neither saved nor registered.
    """
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
    explicit = team_file or os.environ.get("HORCHESTRA_TEAM_FILE") or os.environ.get("AGENTMAP_TEAM_FILE")
    path = explicit
    # Plugin actions run from the plugin root, so only trust the shell cwd
    # when invoked from a terminal (e.g. by the orchestrator).
    if not path and not os.environ.get("HERDR_PLUGIN_ID"):
        path = find_team_file(os.getcwd())
    path = path or find_team_file(cwd)
    if path and not os.path.isfile(path):
        if not create:
            raise TeamError(f"{path} does not exist; run `horchestra-team up --file {path}` to create it")
        if not os.path.isdir(os.path.dirname(os.path.abspath(path))):
            raise TeamError(f"cannot create {path}: its folder does not exist")
        save(path, new_team())
    elif not path:
        path = os.path.join(project_root(cwd), TEAM_FILE)
        if read_only:
            return Space(workspace, os.path.abspath(path), new_team()), pane
        if not create:
            raise TeamError(f"no {TEAM_FILE} found from {cwd}; run `horchestra-team up` "
                            "in the project folder first")
        if is_unsafe_root(os.path.dirname(path)):
            raise TeamError(f"not creating {path}: {os.path.dirname(path)} is your home folder or /, "
                            "not a project. cd into the project folder first, or pass "
                            f"`--file {path}` if you really want a team there")
        save(path, new_team())
    path = os.path.abspath(path)
    if create:
        # Only up/init/adopt (and reopen) put a team on the restart list, so
        # `forget` sticks when members later run status/message/sync.
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
            if exc.code == "agent_not_ready":
                return wait_until_ready(name, pane_id)
            if exc.code == "timeout":
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
        raise TeamError(f"{name} is still not ready; answer its prompt, then run `horchestra-team sync`") from exc


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


def new_member_pane(space, cwd, role):
    """Each member gets its own tab, named after its role."""
    created = hc.call("tab", "create", "--workspace", space.workspace_id,
                      "--label", role, "--cwd", cwd)
    return created["root_pane"]["pane_id"]


def name_tabs(space, log=print):
    """Keep each agent's tab named after its role.

    Only renames a tab when the role's own pane runs an agent and that is
    the only agent in it (plain shells and map panes do not count), since
    one tab cannot carry two names and a dead role pane must not name a tab
    after another agent.
    """
    if space.team.get("name_tabs") is False:
        return
    tabs = {t.get("tab_id"): t.get("label") for t in
            (hc.call_quiet("tab", "list", "--workspace", space.workspace_id) or {}).get("tabs") or []}
    agents_per_tab = {}
    for p in space.panes:
        if p.get("agent") and not tokens_of(p).get(hc.TOKEN_VIEW):
            agents_per_tab[p.get("tab_id")] = agents_per_tab.get(p.get("tab_id"), 0) + 1
    for role in [ORCHESTRATOR] + [m["role"] for m in space.team["member"]]:
        pane = space.pane(role)
        if not pane or not pane.get("agent") or tokens_of(pane).get(hc.TOKEN_VIEW):
            continue
        tab = pane.get("tab_id")
        if tab in tabs and agents_per_tab.get(tab) == 1 and tabs[tab] != role:
            if hc.call_quiet("tab", "rename", tab, role) is not None:
                tabs[tab] = role


def team_context(space, role):
    """Standing member instructions (a Claude member's session agent holds them)."""
    entry = space.entry(role) or {}
    text = (
        f"You are the {role} member of an agent team in this Herdr space, "
        f"coordinated by an orchestrator agent (Herdr agent "
        f"`{space.agent_name(ORCHESTRATOR)}`). Work only on your brief and do not "
        "edit team.toml. "
    )
    agreed = [f"{label}: {entry[key].rstrip('.')}." for key, label in ONBOARDING if entry.get(key)]
    if agreed:
        text += "\n\nAgreed when you were onboarded (follow these):\n" + "\n".join(
            f"- {a}" for a in agreed) + "\n\n"
    mates = roster(space, exclude=role)
    if mates:
        text += ("\n\nYour teammates when you joined (`horchestra-team status` shows the "
                 "current team); message one with `horchestra-team message <role> \"...\"` "
                 "when your work affects theirs:\n" + mates + "\n\n")
    return (
        text
        + TASK_HOWTO
        + REPORT_HOWTO
        + "When you finish, end with a short summary of what you changed and "
        "anything the orchestrator must know."
    )


def roster(space, exclude=None):
    """One line per team agent: role, kind, what it does, when to call it."""
    lines = []
    for m in space.team["member"]:
        if exclude and m["role"].lower() == exclude.lower():
            continue
        kind = m.get("kind") or space.team.get("default_kind", "claude")
        line = f"- {m['role']} ({kind}): {m.get('task') or 'no task recorded'}"
        if m.get("call_when"):
            line += f". Call when: {m['call_when'].rstrip('.')}"
        lines.append(line)
    return "\n".join(lines)


def member_brief(member, task=None):
    text = "Your brief:\n\n" + (member.get("task") or "(wait for instructions from the orchestrator)")
    if task:
        text = (f"[horchestra] task #{task['id']} from the orchestrator. " + text
                + f"\n\nWhen it is finished, run `horchestra-team done {task['id']} \"<one-line summary>\"`"
                f" (or `horchestra-team blocked {task['id']} \"<why>\"`).")
    return text


def task_prompt(task):
    return (f"[horchestra] task #{task['id']} from the orchestrator: {task['text']}\n\n"
            f"When it is finished, run `horchestra-team done {task['id']} \"<one-line summary>\"`; "
            f"if you cannot finish it, run `horchestra-team blocked {task['id']} \"<why>\"`.")


def load_profile(space, member):
    try:
        return roles.load(space.root, space.team, member)
    except roles.RoleError as exc:
        raise TeamError(str(exc)) from exc


TASK_HOWTO = (
    "Work arrives as numbered tasks (`[horchestra] task #N ...`). When task #N is "
    "finished, run `horchestra-team done N \"<one-line summary>\"`; it tells the "
    "orchestrator and updates the map, so there is no need to message it as well. "
    "If you cannot finish it (missing access, a decision outside your role), run "
    "`horchestra-team blocked N \"<why>\"` and stop. "
)

REPORT_HOWTO = (
    "Keep the human's agent map current: at each milestone run "
    "`horchestra-team report \"<short status, e.g. tests 3/5 passing>\"`. If you "
    "need a decision from the human, run "
    "`horchestra-team report --needs-you \"<the question>\"`, ask it, and stop. "
    "`report` only updates the map; to tell the orchestrator something (you "
    "finished, committed, or are blocked), run "
    "`horchestra-team message orchestrator \"<message>\"`. "
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


def orchestrator_args(space, team):
    """Launch args for the orchestrator (before any resume args)."""
    orch = team["orchestrator"]
    kind = orch.get("kind", "claude")
    args = [str(a) for a in orch.get("args", [])]
    protocol = orchestrator_protocol(space, team)
    if kind == "claude" and os.path.isfile(agent_file()):
        # The installed session agent carries the protocol, and
        # `claude --resume` keeps it.
        args = ["--name", ORCHESTRATOR, "--agent", "orchestrator", *args]
    elif kind == "claude":
        # Multi-line text cannot be passed safely as a shell argument; hand
        # Claude a file instead so the protocol lives in its system prompt.
        os.makedirs(STATE_DIR, exist_ok=True)
        prompt_file = os.path.join(STATE_DIR, f"{space.agent_name(ORCHESTRATOR)}.md")
        with open(prompt_file, "w") as fh:
            fh.write(protocol)
        args = ["--name", ORCHESTRATOR, "--append-system-prompt-file", prompt_file, *args]
    return args


def start_orchestrator(space, team, near_pane):
    """Start the orchestrator in the caller's shell pane, or split beside it."""
    kind = team["orchestrator"].get("kind", "claude")
    protocol = orchestrator_protocol(space, team)
    args = orchestrator_args(space, team)
    target = near_pane.get("pane_id") if near_pane else None
    if not target or near_pane.get("agent") or near_pane.get("workspace_id") != space.workspace_id:
        base = target if target and near_pane.get("workspace_id") == space.workspace_id else space.panes[0]["pane_id"]
        target = hc.call("pane", "split", base, "--direction", "right",
                         "--cwd", space.root, "--focus")["pane"]["pane_id"]
    hc.call("pane", "rename", target, LABEL_PREFIX + ORCHESTRATOR)
    start_agent(space.agent_name(ORCHESTRATOR), kind, target, args)
    if kind != "claude":
        prompt(target, protocol)
    pane = wait_for_session(space, target)
    record_started(space, ORCHESTRATOR, pane)
    return pane


def record_started(space, role, pane):
    """Record the session of an agent we just started in `pane`.

    Starting a fresh agent means the recorded session no longer runs here,
    and only a session we started (or one the human adopted) is proof of
    identity, so this is the one place a recorded session is replaced.
    """
    entry = space.entry(role)
    if entry is None:
        return
    session = session_of(pane) if pane else None
    if session:
        entry["session"] = session
    else:
        entry.pop("session", None)  # stale; the label + role token covers the gap
    save(space.team_file, space.team)


def spawn_member(space, team, member, task=None):
    role = member["role"]
    cwd = os.path.join(space.root, member.get("cwd", "")) if member.get("cwd") else space.root
    pane = space.pane(role)
    if pane is None:
        pane_id = new_member_pane(space, cwd, role)
        hc.call("pane", "rename", pane_id, LABEL_PREFIX + role)
    else:
        pane_id = pane["pane_id"]
    kind = member.get("kind") or team.get("default_kind", "claude")
    profile = load_profile(space, member)
    args = member_args(space, member, profile)
    if kind == "claude":
        brief = member_brief(member, task)
        # Its team context and profile live in launch flags; restore re-applies them.
        member["profile_applied"] = True
    else:
        # Other agents get the same instructions in their first message.
        brief = roles.agent_body(profile, team_context(space, role)) + "\n" + member_brief(member, task)
    start_agent(space.agent_name(role), kind, pane_id, args)
    prompt(pane_id, brief)
    pane = wait_for_session(space, pane_id)
    record_started(space, role, pane)
    return pane


def member_args(space, member, profile):
    """Launch args for a member (before any resume args)."""
    args = [str(a) for a in member.get("args", [])]
    kind = member.get("kind") or space.team.get("default_kind", "claude")
    if kind != "claude":
        return args
    # Profile -> session agent (+ role skills dir, deny settings). --add-dir is
    # variadic, so the user's args (flags) follow it.
    context = team_context(space, member["role"])
    try:
        launch = roles.claude_args(space.root, STATE_DIR, space.agent_name(member["role"]), profile, context)
    except roles.RoleError as exc:
        # e.g. the user's own .claude/agents/horchestra-<role>.md is in the way.
        raise TeamError(str(exc)) from exc
    # --name keeps the Claude conversation named after the role (and its tab).
    return ["--name", member["role"]] + launch + args


# ---- respawn ------------------------------------------------------------


def quit_agent(pane_id, seconds=20.0):
    """Exit the agent in a pane (Ctrl+C twice) and wait for its shell."""
    hc.call("pane", "send-keys", pane_id, "ctrl+c", "ctrl+c")
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        time.sleep(1.0)
        pane = hc.get_pane(pane_id)
        if not pane.get("agent"):
            time.sleep(0.5)  # let the shell print its prompt
            return
    raise TeamError(f"the agent in {pane_id} did not exit; quit it by hand and re-run respawn")


def respawn(space, role, force=False, log=print):
    """Restart a Claude agent in place with its current profile, keeping its conversation.

    `claude --resume <session> --system-prompt-snapshot off` rebuilds the system
    prompt from the new --agent / --settings / --add-dir instead of reusing the
    one recorded when the conversation started.
    """
    is_orch = role == ORCHESTRATOR
    entry = space.entry(role)
    if entry is None:
        raise TeamError(f"{role} is not on the team")
    pane, problem = space.verified_pane(role)
    if pane is None:
        raise TeamError(f"not respawning {role}: {problem}")
    if not pane.get("agent"):
        raise TeamError(f"{role} has no running agent to respawn; use `sync` to start it")
    kind = entry.get("kind") or space.team.get("default_kind", "claude")
    if kind != "claude" or pane.get("agent") != "claude":
        raise TeamError(f"{role} is not a Claude agent; respawn only supports Claude")
    session = session_of(pane) or entry.get("session")
    if not session:
        raise TeamError(f"{role} has no recorded session id to resume")
    if pane.get("agent_status") == "working" and not force:
        raise TeamError(f"{role} is working; wait until it is idle or pass --force")
    if is_orch:
        launch = orchestrator_args(space, space.team)
    else:
        profile = load_profile(space, entry)
        launch = member_args(space, entry, profile)
    args = ["--resume", session, "--system-prompt-snapshot", "off", *launch]
    # Decide the name while the agent still runs: once it has quit, nothing
    # holds the plain name any more and a pre-hash team would be renamed.
    name = space.agent_name(role)
    log(f"respawning {role} in {pane['pane_id']}…")
    quit_agent(pane["pane_id"])
    start_agent(name, "claude", pane["pane_id"], args)
    if not is_orch:
        entry.pop("adopted", None)
        entry["profile_applied"] = True
    save(space.team_file, space.team)
    wait_for_session(space, pane["pane_id"])


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
    if entry is not None and session and entry.get("session") != session and (
            not entry.get("session") or space.same_terminal(role, pane)):
        # Fill a missing session, or follow a new one in the role's own
        # terminal. A label match alone never replaces a recorded session.
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


def sync(space, near_pane=None, start_orch=False, spawn=True, only=None, rebrief=False, log=print, briefs=None):
    team = space.team
    space.dirty = False
    orch = space.pane(ORCHESTRATOR)
    if start_orch and (orch is None or not orch.get("agent")):
        log("starting orchestrator…")
        # Reuse a known orchestrator pane whose agent has exited.
        orch = start_orchestrator(space, team, orch or near_pane)
    refs = {}
    if orch and space.label_only(ORCHESTRATOR, orch):
        log(f"note: pane {orch['pane_id']} is labelled {LABEL_PREFIX}{ORCHESTRATOR} but does not run "
            "the recorded orchestrator session; leaving it alone (run `horchestra-team init` "
            "from it if it is this team's orchestrator)")
        orch = None
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
            pane = spawn_member(space, team, member, (briefs or {}).get(role))
        if not pane:
            continue
        if space.label_only(role, pane):
            log(f"note: pane {pane['pane_id']} is labelled {LABEL_PREFIX}{role} but does not run "
                f"{role}'s recorded session; leaving it alone (if it is {role}, "
                f"`fire {role}` then `adopt {pane['pane_id']} --role {role}`)")
            continue
        adopt(space, role, pane, log=log)
        refs[role] = pane.get("terminal_id") or pane["pane_id"]
        parent = refs.get(member.get("reports_to")) or refs.get(ORCHESTRATOR)
        tokens = {hc.TOKEN_ROLE: role, hc.TOKEN_PROFILE: profile_line(space, member)}
        if member.get("task"):
            tokens[hc.TOKEN_TASK] = " ".join(str(member["task"]).split())[:80]
        if parent:
            tokens[hc.TOKEN_PARENT] = parent
        hc.set_tokens(pane["pane_id"], tokens)

    if space.dirty:
        save(space.team_file, team)
    name_tabs(space, log=log)
    remember_terminals(space.team_file, {
        role: pane.get("terminal_id")
        for role in [ORCHESTRATOR] + [m["role"] for m in team["member"]]
        for pane in [space.pane(role)]
        if pane and pane.get("agent") and pane.get("terminal_id") and not space.label_only(role, pane)
    })

    known = {p["pane_id"] for p in (space.pane(r) for r in [ORCHESTRATOR] + [m["role"] for m in team["member"]]) if p}
    for label, pane in space.by_label.items():
        if label.startswith(LABEL_PREFIX) and pane["pane_id"] not in known:
            log(f"note: pane {pane['pane_id']} ({label}) is not in team.toml "
                "(close it by hand or add the role back)")


def profile_line(space, member):
    try:
        line = roles.load(space.root, space.team, member).summary()
    except roles.RoleError as exc:
        line = f"profile error: {exc}"
    if member.get("adopted") and line != "no profile":
        line += " · not applied: respawn"
    return line[:80]


def needs_reapply(space, member, st):
    """A profile-launched Claude member that was restarted (not handed off)."""
    kind = member.get("kind") or space.team.get("default_kind", "claude")
    if kind != "claude" or not member.get("profile_applied"):
        return False
    pane = space.pane(member["role"]) or {}
    before = st.get("terminals", {}).get(member["role"])
    return bool(before) and before != pane.get("terminal_id")


RESUME_NOTE = (
    "[horchestra] Herdr restarted and resumed this session. {missing}Run "
    "`horchestra-team status` before continuing; do not redo finished work."
)

FRESH_NOTE = (
    "[horchestra] Herdr restarted and your previous orchestrator conversation "
    "could not be resumed, so you are a fresh orchestrator for the existing "
    "team in team.toml. {missing}Run `horchestra-team status`, read members' "
    "recent output with `herdr agent read`, and continue coordinating."
)

RESUMED_GRACE_SECONDS = 45  # once some agents resumed, wait this long for the rest
UNRESUMED_GRACE_SECONDS = 180  # when nothing resumed yet (client may attach late)
RESTORE_MAX_FAILURES = 3  # give up on one team after this many failed ticks


def is_live(pane):
    return bool(pane and pane.get("agent") and pane.get("agent_status") not in (None, "unknown"))


def restore(log=print):
    """Startup hook: wait for Herdr to resume team agents, then repair them.

    Members are never started here: one whose session did not resume is left
    as a labelled shell and reported to the orchestrator, which can re-hire
    it. An orchestrator that could not resume is started fresh in its pane.
    Each team is repaired on its own, so one broken team.toml or a pane that
    vanishes mid-way never stops the others.
    """
    pending, state = {}, {}
    gone = [path for path in registered() if not os.path.isfile(path)]
    if gone:
        # Deleted or moved projects: stop looking for them on every start.
        try:
            unregister(gone)
            log(f"forgot team files that no longer exist: {', '.join(gone)}")
        except OSError as exc:
            log(f"could not update the team registry: {exc}")
    for path in registered():
        if os.path.isfile(path):
            try:
                pending[path] = load(path)
                state[path] = {"first_seen": None, "orch_resumed": None, "failures": 0,
                               "terminals": known_terminals(path)}
            except Exception as exc:  # noqa: BLE001 - one bad file must not stop the hook
                pending.pop(path, None)
                log(f"skip {path}: {exc}")
    rivals = shared_sessions(pending)
    claims = {}  # session id or pane id -> the team file that owns it this run
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
            try:
                done = restore_team(path, team, st, by_session, claims, started, now, log, rivals)
            except Exception as exc:  # noqa: BLE001 - isolate teams in the startup hook
                st["failures"] += 1
                if st.get("finishing") or st["failures"] >= RESTORE_MAX_FAILURES:
                    # finish_restore restarts agents: never run it twice.
                    log(f"{path}: giving up: {type(exc).__name__}: {exc}")
                    done = True
                else:
                    log(f"{path}: will retry: {type(exc).__name__}: {exc}")
                    done = False
            if done:
                del pending[path]
        time.sleep(3)


def shared_sessions(teams):
    """Session ids recorded by more than one team file -> those files.

    A copied project carries its original's session ids in a second
    team.toml; both must not act on the same agents.
    """
    owners = {}
    for path, team in teams.items():
        for entry in [team["orchestrator"]] + team["member"]:
            session = entry.get("session")
            if isinstance(session, str) and session:
                owners.setdefault(session, set()).add(path)
    return {s: sorted(paths) for s, paths in owners.items() if len(paths) > 1}


def pane_owner(pane, paths):
    """The team file whose folder holds `pane`'s working directory, else None.

    The innermost team root wins (a copy nested inside the original), and
    None means it cannot be decided (no cwd, outside every root, or a tie).
    """
    cwd = pane.get("foreground_cwd") or pane.get("cwd")
    if not isinstance(cwd, str) or not cwd:
        return None
    cwd = os.path.realpath(cwd)
    inside = []
    for path in paths:
        root = os.path.realpath(os.path.dirname(os.path.abspath(path)))
        if os.path.commonpath([cwd, root]) == root:
            inside.append((len(root), path))
    inside.sort(reverse=True)
    if not inside or (len(inside) > 1 and inside[0][0] == inside[1][0]):
        return None
    return inside[0][1]


def claim(claims, path, keys):
    """Claim session/pane ids for one team file; return those owned by another.

    A copied project carries the same session ids in two team.toml files;
    only the first file to see them may act on those agents.
    """
    taken = sorted({k for k in keys if claims.get(k, path) != path})
    if not taken:
        for key in keys:
            claims[key] = path
    return taken


def restore_team(path, team, st, by_session, claims, started, now, log=print, rivals=None):
    """One restore tick for one team; True when it is finished (or skipped)."""
    entries = [team["orchestrator"]] + team["member"]
    sessions = [e["session"] for e in entries if isinstance(e.get("session"), str) and e.get("session")]
    found = [by_session[s] for s in sessions if s in by_session]
    for s in sessions:
        shared = (rivals or {}).get(s)
        if not shared or s not in by_session:
            continue
        # Decide by where the agent runs, not by which file came first.
        owner = pane_owner(by_session[s], shared)
        if owner != path:
            why = (f"its agents run in {os.path.dirname(owner)}" if owner else
                   f"it shares sessions with {', '.join(p for p in shared if p != path)} "
                   "and their folder cannot tell which team owns them")
            log(f"{path}: skipped: {why} (a copied project? give one copy a fresh team.toml)")
            return True
    if not found and st["first_seen"] is None:
        # Nothing of this team is here: likely an old team.
        return now - started > IDLE_TEAM_SECONDS
    if found:
        workspaces = [p.get("workspace_id") for p in found]
        st["workspace"] = max(set(workspaces), key=workspaces.count)
    st["first_seen"] = st["first_seen"] or now
    space = Space(st["workspace"], path, team)
    roles = [ORCHESTRATOR] + [m["role"] for m in team["member"]]
    keys = [s for s in sessions if s in by_session]
    keys += ["pane:" + p["pane_id"] for p in (space.pane(r) for r in roles) if p and p.get("pane_id")]
    taken = claim(claims, path, keys)
    if taken:
        owners = sorted({claims[k] for k in taken})
        log(f"{path}: skipped: its agents already belong to {', '.join(owners)} "
            "(a copied project? give one copy a fresh team.toml)")
        return True
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
        st["finishing"] = True
        finish_restore(space, live, st, log=lambda msg, p=path: log(f"{p}: {msg}"))
        return True
    return False


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
            if exc.code == "timeout":
                return True  # the turn started and is still running
            if exc.code != "agent_prompt_stalled":
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
    # Herdr resumes with a plain `claude --resume`, which reuses the system
    # prompt recorded when each conversation began: re-apply role profiles.
    reapplied, failed = [], []
    for member in team["member"]:
        if member["role"] in live and needs_reapply(space, member, st):
            try:
                respawn(space, member["role"], log=log)
                reapplied.append(member["role"])
            except (TeamError, hc.HerdrError) as exc:
                log(f"could not re-apply {member['role']}'s profile: {exc}")
                failed.append(member["role"])
    if reapplied:
        note += f"Role profiles were re-applied to: {', '.join(reapplied)}. "
    note += open_tasks_note(space)
    if failed:
        note += (f"Could not re-apply role profiles to: {', '.join(failed)} (they run "
                 "without their role instructions and skill rules; try "
                 "`horchestra-team respawn <role>`). ")
    if ORCHESTRATOR not in live:
        log("orchestrator did not resume; starting a fresh one")
        try:
            orch = start_orchestrator(space, team, space.pane(ORCHESTRATOR))
            sync(space, spawn=False, log=lambda _msg: None)
            if orch:
                deliver(orch["pane_id"], FRESH_NOTE.format(missing=note), log=log)
            else:
                log("the new orchestrator's pane disappeared; not briefing it")
        except (TeamError, hc.HerdrError) as exc:
            log(f"could not start orchestrator: {exc}")
    elif st.get("orch_resumed"):
        log("re-briefing resumed orchestrator")
        if team["orchestrator"].get("kind", "claude") == "claude":
            try:
                # Refresh its instructions from the current orchestrator.md.
                respawn(space, ORCHESTRATOR, log=log)
            except (TeamError, hc.HerdrError) as exc:
                log(f"could not refresh the orchestrator's instructions: {exc}")
        # Only brief a pane proven to be this team's orchestrator.
        orch, problem = space.verified_pane(ORCHESTRATOR)
        text = RESUME_NOTE.format(missing=note)
        if not os.path.isfile(agent_file()):
            # Started with --append-system-prompt-file, which resume drops.
            text += "\n\nYour orchestrator instructions:\n\n" + orchestrator_protocol(space, team)
        if orch:
            deliver(orch["pane_id"], text, log=log)
        else:
            log(f"not re-briefing the orchestrator: {problem}")
    replace_dead_maps(space, log=log)
    log("restored" + (f" (not resumed: {', '.join(missing)})" if missing else ""))


def open_tasks_note(space):
    try:
        lines = open_task_lines(space)
    except TeamError:
        return ""
    return f"Tasks still open: {'; '.join(lines)}. " if lines else ""


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
    """Map panes come back from a restart as idle shells; reopen them.

    toggle.dead_views only returns titled panes that are verifiably idle
    shells, so a pane the human reused (or merely titled alike) stays open.
    """
    import toggle

    space.refresh()
    # A restored overview is just an idle shell; prefix+M reopens it.
    for pane_id in toggle.dead_views(space.panes, toggle.OVERVIEW_LABEL):
        hc.call_quiet("pane", "close", pane_id)
    dead = toggle.dead_views(space.panes, toggle.MAP_LABEL)
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
    if (needs_start and not os.environ.get("HERDR_PLUGIN_ID") and not os.environ.get("HORCHESTRA_DETACHED")
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
                         start_new_session=True, env={**os.environ, "HORCHESTRA_DETACHED": "1"})
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
    member = {"role": args.role, "kind": pane["agent"], "adopted": True,
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
        raise TeamError("run `horchestra-team init` from the orchestrator's own agent session")
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
    space, _ = resolve_space(args.file, read_only=True)
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


def cmd_message(args):
    """Send a message to another team agent by role."""
    space, me = resolve_space(args.file)
    target_role = next((r for r in [ORCHESTRATOR] + [m["role"] for m in space.team["member"]]
                        if r.lower() == args.role.lower()), None)
    if target_role is None:
        raise TeamError(f"{args.role} is not on the team (see `horchestra-team status`)")
    target = space.pane(target_role)
    if not target or not target.get("agent"):
        raise TeamError(f"{target_role} has no running agent")
    if me.get("pane_id") == target["pane_id"]:
        raise TeamError("that is you")
    sender = tokens_of(me).get(hc.TOKEN_ROLE) or me.get("agent") or "the human"
    text = " ".join(" ".join(args.text).split())
    if not text:
        raise TeamError("give a message")
    # agent prompt queues the text even while the target is working.
    prompt(target["pane_id"], f"[horchestra] message from {sender}: {text}")
    if me.get("pane_id"):
        # Lets the agent map run a pulse from sender to receiver.
        try:
            hc.set_tokens(me["pane_id"], {hc.TOKEN_SIGNAL: hc.signal_value("msg", target["pane_id"])})
        except hc.HerdrError:
            pass  # the message itself went through
    print(f"sent to {target_role}")


def cmd_report(args):
    """Set the calling agent's status line in the agent map."""
    pane_id = caller_pane()
    if not pane_id:
        raise TeamError("run `horchestra-team report` inside a Herdr pane")
    if args.clear:
        hc.set_tokens(pane_id, clear=[hc.TOKEN_STATUS, hc.TOKEN_NEEDS])
        print("status cleared")
        return
    text = " ".join(" ".join(args.text).split())[:80]
    if not text:
        raise TeamError("give a short status text (or --clear)")
    tokens = {hc.TOKEN_STATUS: text, hc.TOKEN_SIGNAL: hc.signal_value("needs" if args.needs_you else "report")}
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


def cmd_roles(args):
    """List role profiles in this project and who uses them."""
    space, _ = resolve_space(args.file, read_only=True)
    names = roles.list_profiles(space.root)
    print(f"profiles in {roles.roles_root(space.root)}:")
    if not names:
        print("  (none) create .orchestra/roles/<role>/ROLE.md to add one")
    for name in names:
        folder = os.path.join(roles.roles_root(space.root), name)
        users = [m["role"] for m in space.team["member"] if (m.get("profile") or m["role"]) == name]
        has_md = "ROLE.md" if os.path.isfile(os.path.join(folder, "ROLE.md")) else "no ROLE.md"
        skills = roles.skills_in(folder)
        print(f"  {name:<16}{has_md:<12}skills: {', '.join(skills) or '-':<30} used by: {', '.join(users) or '-'}")


def cmd_respawn(args):
    space, me = resolve_space(args.file)
    roles_wanted = [m["role"] for m in space.team["member"]] if args.all else [args.role]
    if not roles_wanted or roles_wanted == [None]:
        raise TeamError("give a role, or --all")
    targets = {(space.pane(r) or {}).get("pane_id") for r in roles_wanted}
    if me.get("pane_id") in targets and not os.environ.get("HORCHESTRA_DETACHED"):
        # Respawning the agent that runs this command: finish in the background.
        import subprocess

        argv = [sys.executable, os.path.realpath(__file__), "--file", space.team_file, "respawn"]
        argv += ["--all"] if args.all else [args.role]
        argv += ["--force"] if args.force else []
        log_path = os.path.join(STATE_DIR, "respawn.log")
        os.makedirs(STATE_DIR, exist_ok=True)
        with open(log_path, "a") as log:
            subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=log, stderr=log,
                             start_new_session=True, env={**os.environ, "HORCHESTRA_DETACHED": "1"})
        print(f"respawning in the background (this session will restart); log: {log_path}")
        return
    for role in roles_wanted:
        if args.all and not (space.entry(role) or {}).get("kind", "claude") == "claude":
            continue
        respawn(space, role, force=args.force)
        print(f"respawned {role}")
    sync(space, spawn=False, log=lambda _msg: None)


# ---- reopen -------------------------------------------------------------


def session_on_disk(kind, session):
    """Whether the agent CLI still has this conversation to resume."""
    import glob

    if kind == "claude":
        pattern = os.path.join(glob.escape(roles.claude_config_dir()), "projects", "*", f"{glob.escape(session)}.jsonl")
        return bool(glob.glob(pattern))
    if kind == "codex":
        pattern = os.path.expanduser(f"~/.codex/sessions/**/*{session}*.jsonl")
        return bool(glob.glob(pattern, recursive=True))
    return False


def resume_launch(space, role, kind, session):
    """Launch args that resume `session` with the role's current profile."""
    entry = space.entry(role)
    if kind == "claude":
        launch = (orchestrator_args(space, space.team) if role == ORCHESTRATOR
                  else member_args(space, entry, load_profile(space, entry)))
        return ["--resume", session, "--system-prompt-snapshot", "off", *launch]
    # codex resume [OPTIONS] [SESSION_ID]
    return ["resume", *[str(a) for a in entry.get("args", [])], session]


def team_panes_anywhere(root, entries, panes):
    """Panes in any space that still belong to this team.

    A pane counts when it runs one of the team's sessions (even idle), or
    carries a team:<role> label for one of its roles while sitting in the
    project folder (labels alone are shared by every project's teams).
    """
    sessions = {e.get("session") for _, e in entries if e.get("session")}
    labels = {LABEL_PREFIX + role for role, _ in entries}
    base = os.path.realpath(root)

    def in_project(p):
        cwd = p.get("foreground_cwd") or p.get("cwd")
        if not isinstance(cwd, str) or not cwd:
            return False
        cwd = os.path.realpath(cwd)
        return os.path.commonpath([cwd, base]) == base

    return [p for p in panes if isinstance(p, dict) and (
        session_of(p) in sessions or (p.get("label") in labels and in_project(p)))]


def cmd_reopen(args):
    """Recreate a closed space from team.toml, resuming every agent's conversation."""
    path = args.file or os.environ.get("HORCHESTRA_TEAM_FILE") or find_team_file(os.getcwd())
    if not path:
        raise TeamError(f"no {TEAM_FILE} found from {os.getcwd()}; cd into the project or pass --file")
    path = os.path.abspath(path)
    team = load(path)
    register(path)
    root = os.path.dirname(path)
    entries = [(ORCHESTRATOR, team["orchestrator"])] + [(m["role"], m) for m in team["member"]]
    everywhere = hc.call("pane", "list").get("panes") or []
    open_now = team_panes_anywhere(root, entries, everywhere)
    if open_now:
        where = sorted({p.get("workspace_id", "?") for p in open_now})
        ids = ", ".join(p.get("pane_id", "?") for p in open_now)
        raise TeamError(f"this team still has panes in space(s) {', '.join(where)} ({ids}); "
                        "run `horchestra-team sync` there to restart missing members, "
                        "or close those panes first")

    label = args.label or os.path.basename(root)
    created = hc.call("workspace", "create", "--label", label, "--cwd", root, "--focus")
    workspace = created["workspace"]["workspace_id"]
    first_pane = created["root_pane"]["pane_id"]
    hc.call_quiet("tab", "rename", created["tab"]["tab_id"], ORCHESTRATOR)
    space = Space(workspace, path, team)
    print(f"reopening {label} in space {workspace}…")

    resumed, fresh, failed = [], [], []
    for index, (role, entry) in enumerate(entries):
        kind = entry.get("kind") or team.get("default_kind", "claude")
        cwd = os.path.join(root, entry.get("cwd", "")) if entry.get("cwd") else root
        try:
            if index == 0:
                pane_id = first_pane
            else:
                pane_id = hc.call("tab", "create", "--workspace", workspace, "--label", role,
                                  "--cwd", cwd)["root_pane"]["pane_id"]
            hc.call("pane", "rename", pane_id, LABEL_PREFIX + role)
            session = entry.get("session")
            if session and kind in ("claude", "codex") and session_on_disk(kind, session):
                print(f"  {role}: resuming its conversation…")
                start_agent(space.agent_name(role), kind, pane_id, resume_launch(space, role, kind, session))
                resumed.append(role)
            else:
                print(f"  {role}: no saved conversation; starting fresh…")
                space.refresh()
                if role == ORCHESTRATOR:
                    start_orchestrator(space, team, {"pane_id": pane_id, "workspace_id": workspace})
                else:
                    spawn_member(space, team, entry)
                fresh.append(role)
        except (TeamError, hc.HerdrError) as exc:
            print(f"  {role}: FAILED: {exc}")
            failed.append(role)

    space.refresh()
    sync(space, spawn=False, log=lambda _msg: None)
    show_maps(space, force=True)
    orch = space.pane(ORCHESTRATOR)
    if orch and orch.get("agent") and ORCHESTRATOR not in failed:
        note = "[horchestra] This space was reopened from team.toml. "
        note += f"Resumed with their conversations: {', '.join(resumed) or 'none'}. "
        if fresh:
            note += f"Started fresh (no saved conversation): {', '.join(fresh)}. "
        if failed:
            note += f"Could not start: {', '.join(failed)}. "
        note += open_tasks_note(space)
        note += "Run `horchestra-team status` before continuing."
        deliver(orch["pane_id"], note)
    print(f"done: resumed {len(resumed)}, fresh {len(fresh)}, failed {len(failed)}")


def cmd_forget(args):
    """Stop restoring this team after Herdr restarts (team.toml is kept)."""
    path = args.file or os.environ.get("HORCHESTRA_TEAM_FILE") or find_team_file(os.getcwd())
    if not path:
        raise TeamError(f"no {TEAM_FILE} found from {os.getcwd()}; cd into the project or pass --file")
    path = os.path.abspath(path)
    if unregister([path]):
        print(f"forgot {path}; it is no longer restored after a restart (`up` registers it again)")
    else:
        print(f"{path} was not on the restart list")


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
    if args.profile:
        member["profile"] = args.profile
    if args.uses_skill:
        member["uses_skills"] = args.uses_skill
    if args.deny_skill:
        member["deny_skills"] = args.deny_skill
    if args.only_skill:
        member["only_skills"] = args.only_skill
    for key, _ in ONBOARDING:
        if getattr(args, key, None):
            member[key] = getattr(args, key)
    validate_entry(team, member, space.root, f"hire {args.role}")
    load_profile(space, member)  # fail before touching team.toml
    if member["kind"] == "claude":
        try:
            roles.check_agent_file(space.root, args.role)
        except roles.RoleError as exc:
            raise TeamError(str(exc)) from exc
    before = dump(team)
    existing = (space.pane(args.role) or {}).get("pane_id")
    team["member"].append(member)
    save(space.team_file, team)
    task = tasks.add(space.root, args.role, args.task)
    try:
        sync(space, only={args.role}, briefs={args.role: task})
    except (TeamError, hc.HerdrError) as exc:
        kept = hire_left_behind(space, args.role, existing)
        if kept is None:
            # Nothing was created: undo the team.toml change and the task.
            tasks.close(space.root, task["id"], "cancelled", "hire failed")
            tmp = space.team_file + ".tmp"
            with open(tmp, "w") as fh:
                fh.write(before)
            os.replace(tmp, space.team_file)
            raise
        raise TeamError(
            f"{args.role} stays on the team (pane {kept}) but did not finish starting: {exc}. "
            f"Answer the agent's prompt in pane {kept} if it shows one, then run "
            "`horchestra-team sync`") from exc
    show_maps(space)
    pane = space.pane(args.role)
    print(f"hired {args.role}: agent {space.agent_name(args.role)} in pane {pane['pane_id'] if pane else '?'}"
          f" (task #{task['id']})")


def hire_left_behind(space, role, existing):
    """The pane a failed hire created or started an agent in, else None.

    Rolling team.toml back then would orphan a running agent (e.g. one
    waiting on a folder-trust prompt), so the member is kept instead.
    """
    try:
        space.refresh()
    except hc.HerdrError:
        return None
    pane = space.pane(role)
    if pane and (pane.get("pane_id") != existing or pane.get("agent")):
        return pane["pane_id"]
    return None


def cmd_fire(args):
    if args.role.lower() == ORCHESTRATOR:
        raise TeamError("the orchestrator cannot be fired; only members can")
    space, _ = resolve_space(args.file)
    team = space.team
    role = next((m["role"] for m in team["member"] if m["role"].lower() == args.role.lower()), None)
    if role is None:
        raise TeamError(f"{args.role} is not on the team (see `horchestra-team status`)")
    # Resolve before saving: the verification needs the member's session.
    pane, problem = space.verified_pane(role)
    team["member"] = [m for m in team["member"] if m["role"] != role]
    save(space.team_file, team)
    if pane:
        hc.call_quiet("pane", "close", pane["pane_id"])
    elif space.pane(role):
        print(f"note: left pane {space.pane(role)['pane_id']} open ({problem}); "
              f"close it by hand if it is {role}'s")
    print(f"fired {role}")


# ---- tasks --------------------------------------------------------------


def load_tasks(space):
    try:
        return tasks.load(space.root)
    except tasks.TaskError as exc:
        raise TeamError(str(exc)) from exc


def open_task_lines(space):
    board = load_tasks(space)
    return [tasks.line(t) + ("" if t["state"] == "open" else f" [{t['state']}]")
            for t in board["tasks"] if t.get("state") in tasks.ACTIVE]


def caller_role(space, me):
    """The team role of the calling pane, or None (e.g. the human's shell)."""
    role = tokens_of(me).get(hc.TOKEN_ROLE)
    if not role or not me.get("pane_id"):
        return None
    pane = space.pane(role)
    return role if pane and pane.get("pane_id") == me["pane_id"] else None


def cmd_assign(args):
    """Give a member a numbered task and send it to them."""
    space, me = resolve_space(args.file)
    role = next((m["role"] for m in space.team["member"] if m["role"].lower() == args.role.lower()), None)
    if role is None:
        raise TeamError(f"{args.role} is not a member (see `horchestra-team status`)")
    target = space.pane(role)
    if not target or not target.get("agent"):
        raise TeamError(f"{role} has no running agent (`horchestra-team sync` starts it)")
    task = tasks.add(space.root, role, " ".join(args.text))
    try:
        prompt(target["pane_id"], task_prompt(task))
    except hc.HerdrError as exc:
        tasks.close(space.root, task["id"], "cancelled", f"not delivered: {exc}")
        raise TeamError(f"could not send task #{task['id']} to {role} (cancelled it): {exc}") from exc
    if me.get("pane_id"):
        try:
            hc.set_tokens(me["pane_id"], {hc.TOKEN_SIGNAL: hc.signal_value("msg", target["pane_id"])})
        except hc.HerdrError:
            pass
    print(f"task #{task['id']} assigned to {role}")


def parse_task_args(words):
    """`[ID] text...` -> (id or None, text)."""
    if words and words[0].lstrip("#").isdigit():
        return int(words[0].lstrip("#")), " ".join(words[1:])
    return None, " ".join(words)


def cmd_finish(args):
    """`done` / `blocked`: close a task and tell the orchestrator."""
    state = args.command
    space, me = resolve_space(args.file)
    task_id, text = parse_task_args(args.text)
    text = " ".join(text.split())
    if not text:
        raise TeamError(f"give a one-line {'summary' if state == 'done' else 'reason'}")
    role = caller_role(space, me)
    board = load_tasks(space)
    if task_id is None:
        if role is None or role == ORCHESTRATOR:
            raise TeamError("give the task number, e.g. `horchestra-team done 3 \"...\"`")
        mine = tasks.open_for(board, role)
        if not mine:
            raise TeamError("you have no open task; tell the orchestrator with "
                            "`horchestra-team message orchestrator \"...\"`")
        if len(mine) > 1:
            listed = ", ".join(f"#{t['id']}" for t in mine)
            raise TeamError(f"you have several open tasks ({listed}); give the task number")
        task_id = mine[0]["id"]
    task = tasks.find(board, task_id)
    if task is None:
        raise TeamError(f"no task #{task_id} (see `horchestra-team tasks`)")
    if role != ORCHESTRATOR and (role or "").lower() != task["role"].lower():
        raise TeamError(f"task #{task_id} belongs to {task['role']}")
    if task["state"] not in tasks.ACTIVE:
        raise TeamError(f"task #{task_id} is already {task['state']}")
    task = tasks.close(space.root, task_id, state, text)
    owner = space.pane(task["role"])
    if role and role != ORCHESTRATOR and owner:
        line = f"{state}: {text}"[:80]
        hc.set_tokens(owner["pane_id"], {hc.TOKEN_STATUS: line, hc.TOKEN_SIGNAL: hc.signal_value(
            "report" if state == "done" else "needs")}, clear=[hc.TOKEN_NEEDS])
        orch = space.pane(ORCHESTRATOR)
        if orch and orch.get("agent"):
            prompt(orch["pane_id"], f"[horchestra] message from {role}: task #{task_id} {state}: {text}")
    print(f"task #{task_id} {state}")


def cmd_cancel(args):
    """Drop a task without it being done (orchestrator)."""
    space, _ = resolve_space(args.file)
    board = load_tasks(space)
    task = tasks.find(board, args.id)
    if task is None:
        raise TeamError(f"no task #{args.id}")
    if task["state"] not in tasks.ACTIVE:
        raise TeamError(f"task #{args.id} is already {task['state']}")
    tasks.close(space.root, args.id, "cancelled", " ".join(args.reason))
    print(f"task #{args.id} cancelled")


def cmd_tasks(args):
    """List tasks (open and blocked; --all for every task)."""
    space, _ = resolve_space(args.file, read_only=True)
    board = load_tasks(space)
    shown = [t for t in board["tasks"] if args.all or t.get("state") in tasks.ACTIVE]
    if args.role:
        shown = [t for t in shown if t.get("role", "").lower() == args.role.lower()]
    if not shown:
        print("no open tasks" if not args.all else "no tasks yet")
        return
    for t in shown:
        extra = f"  -> {t['summary']}" if t.get("summary") else ""
        print(f"#{t['id']:<4}{t.get('role', '?'):<14}{t.get('state', '?'):<10}{t.get('text', '')}{extra}")


# ---- idle-with-open-task notice (Herdr event hook) ----------------------

NUDGE_GRACE = 3.0  # seconds for a `done` run at the end of the turn to land
NUDGE_EVERY = 600  # at most one notice per task per this many seconds
IDLE_STATES = ("idle", "done")


def event_pane_status(raw):
    """(pane_id, agent_status) from HERDR_PLUGIN_EVENT_JSON, or (None, None)."""
    try:
        doc = json.loads(raw or "{}")
    except ValueError:
        return None, None
    for part in (doc.get("data") if isinstance(doc, dict) else None, doc):
        if isinstance(part, dict) and isinstance(part.get("pane_id"), str):
            status = part.get("agent_status")
            return part["pane_id"], status if isinstance(status, str) else None
    return None, None


def team_of_pane(pane):
    """(Space, role) when `pane` is a registered team's member, else (None, None)."""
    role = tokens_of(pane).get(hc.TOKEN_ROLE)
    if not role or role == ORCHESTRATOR or not pane.get("workspace_id"):
        return None, None
    path = find_team_file(pane.get("foreground_cwd") or pane.get("cwd") or "")
    if not path or os.path.abspath(path) not in registered():
        return None, None
    space = Space(pane["workspace_id"], os.path.abspath(path), load(path))
    if not space.entry(role):
        return None, None
    mine = space.pane(role)
    if not mine or mine.get("pane_id") != pane.get("pane_id"):
        return None, None
    return space, role


def cmd_hook_status(_args):
    """Event hook: a member went idle while it still has an open task."""
    pane_id, status = event_pane_status(os.environ.get("HERDR_PLUGIN_EVENT_JSON"))
    if not pane_id or status not in IDLE_STATES:
        return
    pane = hc.call_quiet("pane", "get", pane_id)
    pane = (pane or {}).get("pane") or {}
    space, role = team_of_pane(pane)
    if not space:
        return
    time.sleep(NUDGE_GRACE)
    pane = (hc.call_quiet("pane", "get", pane_id) or {}).get("pane") or {}
    if pane.get("agent_status") not in IDLE_STATES or tokens_of(pane).get(hc.TOKEN_NEEDS):
        return  # back at work, or waiting on the human (who was already notified)
    stale = [t for t in tasks.open_for(load_tasks(space), role)
             if time.time() - (t.get("nudged") or 0) >= NUDGE_EVERY]
    orch = space.pane(ORCHESTRATOR)
    if not stale or not orch or not orch.get("agent"):
        return
    listed = "; ".join(f"#{t['id']} \"{t['text'][:80]}\"" for t in stale)
    first = stale[0]["id"]
    note = (f"[horchestra] {role} went idle with open task(s): {listed}. Read its output "
            f"(`herdr agent read {space.agent_name(role)} --lines 80`), then close the task with "
            f"`horchestra-team done {first} \"<summary>\"` (or `blocked {first} \"<why>\"`), or "
            f"message {role} if it is not finished.")
    try:
        prompt(orch["pane_id"], note)
    except hc.HerdrError as exc:
        raise TeamError(f"could not tell the orchestrator that {role} went idle: {exc}") from exc
    tasks.mark_nudged(space.root, {t["id"] for t in stale}, time.time())
    print(f"told the orchestrator: {role} idle with {', '.join('#' + str(t['id']) for t in stale)} open")


def cmd_status(args):
    space, _ = resolve_space(args.file)
    team = space.team
    sync(space, spawn=False, log=lambda _msg: None)  # records session ids
    rows = [(ORCHESTRATOR, team["orchestrator"].get("kind", "claude"), "")]
    rows += [(m["role"], m.get("kind") or team.get("default_kind", "claude"), m.get("reports_to", "")) for m in team["member"]]
    print(f"team file: {space.team_file}")
    board = load_tasks(space)
    name_w = max([26] + [len(space.agent_name(r)) + 2 for r, _, _ in rows])
    print(f"{'ROLE':<14}{'KIND':<9}{'AGENT':<{name_w}}{'PANE':<9}{'STATE':<9}{'REPORTS TO':<12}PROFILE / REPORTED")
    for role, kind, boss in rows:
        pane = space.pane(role) or {}
        name = space.agent_name(role) if pane.get("agent") else "-"
        state = pane.get("agent_status", "missing") if pane else "missing"
        member = space.entry(role) if role != ORCHESTRATOR else None
        profile = profile_line(space, member) if member else ""
        print(f"{role:<14}{kind:<9}{name:<{name_w}}{pane.get('pane_id', '-'):<9}{state:<9}{boss:<12}{profile}")
        reported = tokens_of(pane).get(hc.TOKEN_STATUS)
        if reported:
            flag = "NEEDS YOU: " if tokens_of(pane).get(hc.TOKEN_NEEDS) else ""
            print(f"{'':<14}↳ reported: {flag}{reported}")
        for key, _ in ONBOARDING:
            if member and member.get(key):
                print(f"{'':<14}↳ {key.replace('_', ' ')}: {member[key]}")
        for task in tasks.open_for(board, role, tasks.ACTIVE):
            print(f"{'':<14}↳ task #{task['id']} {task['state']}: {task['text']}"
                  + (f" ({task['summary']})" if task.get("summary") else ""))


def add_onboarding_flags(parser):
    parser.add_argument("--call-when", dest="call_when", help="when the orchestrator should bring this member in")
    parser.add_argument("--handoff", help="how the member receives work (task size, batching, review)")
    parser.add_argument("--reporting", help="when the member reports back (milestones, done only, ...)")


def cmd_onboard(args):
    """Record onboarding agreements for a member and tell it (orchestrator)."""
    space, _ = resolve_space(args.file)
    entry = next((m for m in space.team["member"] if m["role"].lower() == args.role.lower()), None)
    if entry is None:
        raise TeamError(f"{args.role} is not a member (hire or adopt it first)")
    changes = {key: getattr(args, key) for key, _ in ONBOARDING if getattr(args, key, None)}
    if not changes:
        agreed = [f"{label}: {entry[key]}" for key, label in ONBOARDING if entry.get(key)]
        print("\n".join(agreed) or f"no onboarding agreements for {entry['role']} yet")
        return
    entry.update(changes)
    validate_entry(space.team, entry, space.root, f"onboard {entry['role']}")
    save(space.team_file, space.team)
    role = entry["role"]
    kind = entry.get("kind") or space.team.get("default_kind", "claude")
    if kind == "claude":
        # Rewrite its agent file now, so a respawn or restart starts from it.
        member_args(space, entry, load_profile(space, entry))
    pane = space.pane(role)
    if pane and pane.get("agent"):
        lines = [f"- {label}: {entry[key]}" for key, label in ONBOARDING if entry.get(key)]
        prompt(pane["pane_id"], "[horchestra] Working agreement with the orchestrator, effective now:\n"
               + "\n".join(lines))
    print(f"onboarded {role}: " + ", ".join(sorted(changes)))


def main(argv=None):
    parser = argparse.ArgumentParser(prog="horchestra-team", description="Manage this space's agent team (team.toml).")
    parser.add_argument("--file", help="path to team.toml (default: search upward from cwd)")
    sub = parser.add_subparsers(dest="command", required=True)

    up = sub.add_parser("up", help="create team.toml if needed, start the orchestrator, open the map")
    up.add_argument("--no-map", action="store_true")
    up.set_defaults(func=cmd_up)

    sub.add_parser("init", help="register the calling agent as this space's orchestrator").set_defaults(func=cmd_init)
    sub.add_parser("scan", help="list running agents in this space that are not on the team").set_defaults(func=cmd_scan)
    sub.add_parser("sync", help="apply team.toml: start missing members, repair tags").set_defaults(func=cmd_sync)
    sub.add_parser("roles", help="list role profiles (.orchestra/roles) and who uses them").set_defaults(func=cmd_roles)
    reopen = sub.add_parser("reopen", help="recreate a closed space from team.toml, resuming every agent")
    reopen.add_argument("--label", help="space name (default: the project folder name)")
    reopen.set_defaults(func=cmd_reopen)
    resp = sub.add_parser("respawn", help="restart a Claude agent with its current profile, keeping its conversation")
    resp.add_argument("role", nargs="?")
    resp.add_argument("--all", action="store_true", help="every Claude member")
    resp.add_argument("--force", action="store_true", help="even if it is working")
    resp.set_defaults(func=cmd_respawn)
    sub.add_parser("status", help="show the team").set_defaults(func=cmd_status)
    sub.add_parser("forget", help="stop restoring this team after Herdr restarts").set_defaults(func=cmd_forget)
    message = sub.add_parser("message", help="send a message to a team agent by role, e.g. orchestrator")
    message.add_argument("role")
    message.add_argument("text", nargs="+")
    message.set_defaults(func=cmd_message)
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
    hire.add_argument("--profile", help="role profile folder in .orchestra/roles (default: the role's own)")
    hire.add_argument("--uses-skill", action="append", help="skill the member must use (repeatable)")
    hire.add_argument("--deny-skill", action="append", help="skill name or pattern to block (repeatable)")
    hire.add_argument("--only-skill", action="append",
                      help="allowlist: the member may use only these skills (repeatable)")
    add_onboarding_flags(hire)
    hire.set_defaults(func=cmd_hire)

    onboard = sub.add_parser("onboard", help="record when to call a member, how it gets work and reports back")
    onboard.add_argument("role")
    add_onboarding_flags(onboard)
    onboard.set_defaults(func=cmd_onboard)

    assign = sub.add_parser("assign", help="give a member a numbered task (orchestrator)")
    assign.add_argument("role")
    assign.add_argument("text", nargs="+")
    assign.set_defaults(func=cmd_assign)
    for name, what in (("done", "a one-line summary"), ("blocked", "why it cannot be finished")):
        finish = sub.add_parser(name, help=f"close your task as {name}: [ID] {what} (members)")
        finish.add_argument("text", nargs="+", help=f"[task id] {what}")
        finish.set_defaults(func=cmd_finish)
    cancel = sub.add_parser("cancel", help="cancel a task (orchestrator)")
    cancel.add_argument("id", type=int)
    cancel.add_argument("reason", nargs="*")
    cancel.set_defaults(func=cmd_cancel)
    tasks_p = sub.add_parser("tasks", help="list open and blocked tasks")
    tasks_p.add_argument("--all", action="store_true", help="include done and cancelled tasks")
    tasks_p.add_argument("--role")
    tasks_p.set_defaults(func=cmd_tasks)
    sub.add_parser("hook-status", help="event hook: notice members idle with open tasks").set_defaults(
        func=cmd_hook_status)

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
    except (TeamError, roles.RoleError, tasks.TaskError, hc.HerdrError) as exc:
        print(f"horchestra-team: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
