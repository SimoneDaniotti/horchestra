"""Contract tests: the Herdr CLI commands and JSON shapes Horchestra depends on.

Everything here runs without Herdr. The responses under tests/fixtures/herdr
were recorded from a real Herdr 0.9.3 session (read-only commands, then
sanitized; see the README there), and the CLI table below was written from
that binary's own usage text (`herdr <group> help`) and checked against its
`herdr api schema --json` output, which is recorded as a fixture too.

When Herdr changes:
  * re-record the fixtures with the new version (README) and run this file:
    a renamed field, a changed nesting or a dropped enum value fails a
    response-shape test that names the field and the plugin code using it;
  * update HERDR_CLI from the new usage text: a dropped command or flag that
    Horchestra still passes then fails a command-shape test with file:line.

Each test name says which Herdr dependency it guards.
"""

import argparse
import ast
import contextlib
import io
import json
import os
import re
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

from tests import helpers
import agentmap
import herdr_client as hc
import install
import team
import toggle
import views

ROOT = helpers.ROOT
FIXTURES = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures", "herdr")
HERDR_VERSION = "0.9.3"


def fixture(name):
    with open(os.path.join(FIXTURES, name), encoding="utf-8") as fh:
        return json.load(fh) if name.endswith(".json") else fh.read()


SCHEMA = fixture("api_schema.json")
INDEX = fixture("index.json")
RECORDED_PANES = {p["pane_id"]: p for p in fixture("pane_list.json")["result"]["panes"]}
RECORDED_AGENTS = fixture("agent_list.json")["result"]["agents"]
WORKSPACE = "wV"  # the recorded space with a full Horchestra team
ORCH_PANE, MAP_PANE, PLAIN_SHELL_PANE = "wV:p2", "wV:pK", "w4:p3"


# ---- the Herdr 0.9.3 CLI surface Horchestra may use ------------------------
#
# Written from `herdr <group> help` of the 0.9.3 binary. Each flag maps to the
# socket API parameter it fills (checked against the recorded schema), or None
# for CLI-only switches. `positionals` is (min, max); max None = variadic.


class Cmd:
    def __init__(self, method, positionals, flags=None, passthrough=False):
        self.method = method
        self.positionals = positionals
        self.flags = flags or {}  # flag -> (schema param path or None, takes a value)
        self.passthrough = passthrough  # accepts `-- <args...>` for the launched program


V, B = True, False  # flag takes a value / is a boolean switch
PANE_TARGET = {"--pane": ("pane_id", V), "--current": (None, B)}
FOCUS = {"--focus": ("focus", B), "--no-focus": ("focus", B)}
CREATE = {"--cwd": ("cwd", V), "--label": ("label", V), "--env": ("env", V), **FOCUS}

HERDR_CLI = {
    ("pane", "list"): Cmd("pane.list", (0, 0), {"--workspace": ("workspace_id", V)}),
    ("pane", "get"): Cmd("pane.get", (1, 1)),
    ("pane", "edges"): Cmd("pane.edges", (0, 0), PANE_TARGET),
    ("pane", "process-info"): Cmd("pane.process_info", (0, 0), PANE_TARGET),
    ("pane", "close"): Cmd("pane.close", (1, 1)),
    ("pane", "swap"): Cmd("pane.swap", (0, 0), {
        "--direction": ("direction", V), **PANE_TARGET,
        "--source-pane": ("source_pane_id", V), "--target-pane": ("target_pane_id", V)}),
    ("pane", "resize"): Cmd("pane.resize", (0, 0), {
        "--direction": ("direction", V), "--amount": ("amount", V), **PANE_TARGET}),
    ("pane", "split"): Cmd("pane.split", (0, 1), {
        "--pane": ("target_pane_id", V), "--current": (None, B), "--direction": ("direction", V),
        "--ratio": ("ratio", V), "--cwd": ("cwd", V), "--env": ("env", V),
        "--right-click": ("right_click", V), **FOCUS}),
    ("pane", "rename"): Cmd("pane.rename", (1, 2), {"--clear": ("label", B)}),
    ("pane", "send-keys"): Cmd("pane.send_keys", (2, None)),
    ("pane", "read"): Cmd("pane.read", (1, 1), {
        "--source": ("source", V), "--lines": ("lines", V), "--format": ("format", V),
        "--ansi": ("strip_ansi", B)}),
    ("pane", "report-metadata"): Cmd("pane.report_metadata", (1, 1), {
        "--source": ("source", V), "--agent": ("agent", V),
        "--applies-to-source": ("applies_to_source", V), "--title": ("title", V),
        "--clear-title": ("clear_title", B), "--display-agent": ("display_agent", V),
        "--clear-display-agent": ("clear_display_agent", B), "--state-label": ("state_labels", V),
        "--clear-state-labels": ("clear_state_labels", B), "--token": ("tokens", V),
        "--clear-token": ("tokens", V), "--seq": ("seq", V), "--ttl-ms": ("ttl_ms", V)}),
    ("workspace", "list"): Cmd("workspace.list", (0, 0)),
    ("workspace", "get"): Cmd("workspace.get", (1, 1)),
    ("workspace", "focus"): Cmd("workspace.focus", (1, 1)),
    ("workspace", "create"): Cmd("workspace.create", (0, 0), CREATE),
    ("tab", "list"): Cmd("tab.list", (0, 0), {"--workspace": ("workspace_id", V)}),
    ("tab", "create"): Cmd("tab.create", (0, 0), {"--workspace": ("workspace_id", V), **CREATE}),
    ("tab", "rename"): Cmd("tab.rename", (2, 2)),
    ("agent", "list"): Cmd("agent.list", (0, 0)),
    ("agent", "get"): Cmd("agent.get", (1, 1)),
    ("agent", "prompt"): Cmd("agent.prompt", (2, 2), {
        "--wait": ("wait", B), "--until": ("wait.until", V), "--timeout": ("wait.timeout_ms", V)}),
    ("agent", "rename"): Cmd("agent.rename", (1, 2), {"--clear": ("name", B)}),
    ("agent", "focus"): Cmd("agent.focus", (1, 1)),
    ("agent", "wait"): Cmd("agent.wait", (1, 1), {
        "--until": ("until", V), "--timeout": ("timeout_ms", V)}),
    ("agent", "start"): Cmd("agent.start", (1, 1), {
        "--kind": ("kind", V), "--pane": ("pane_id", V), "--timeout": ("timeout_ms", V)},
        passthrough=True),
    ("plugin", "list"): Cmd("plugin.list", (0, 0), {"--plugin": ("plugin_id", V), "--json": (None, B)}),
    ("plugin", "pane", "open"): Cmd("plugin.pane.open", (0, 0), {
        "--plugin": ("plugin_id", V), "--entrypoint": ("entrypoint", V),
        "--placement": ("placement", V), "--width": ("width", V), "--height": ("height", V),
        "--workspace": ("workspace_id", V), "--target-pane": ("target_pane_id", V),
        "--direction": ("direction", V), "--cwd": ("cwd", V), "--env": ("env", V), **FOCUS}),
    ("plugin", "pane", "focus"): Cmd("plugin.pane.focus", (1, 1)),
    ("plugin", "pane", "close"): Cmd("plugin.pane.close", (1, 1)),
    ("notification", "show"): Cmd("notification.show", (1, 1), {
        "--body": ("body", V), "--position": ("position", V), "--sound": ("sound", V)}),
    ("server", "reload-config"): Cmd("server.reload_config", (0, 0)),
    ("integration", "status"): Cmd("integration.list", (0, 0), {"--outdated-only": (None, B)}),
    ("api", "schema"): Cmd(None, (0, 0), {"--json": (None, B)}),  # used only to record fixtures
}
HERDR_GROUPS = {path[0] for path in HERDR_CLI}

# `agent start` limits from `herdr agent start` docs / AgentStartParams.timeout_ms.
AGENT_START_TIMEOUT_MS = (3000, 300000)  # exclusive min, inclusive max
# Agent names: "Names ... must match [a-z][a-z0-9_-]{0,31}" (cli-reference, agent start).
AGENT_NAME_RE = re.compile(r"^[a-z][a-z0-9_-]{0,31}$")

# Error codes Horchestra branches on (`exc.code == "<code>"`; herdr_client sets
# HerdrError.code from Herdr's stderr JSON). Verified in the 0.9.3 binary and in
# Herdr's source/docs when written:
#   agent_not_ready      src/cli/agent.rs, src/app/api/agents.rs; docs: "If detection
#                        reports blocked during startup, the command returns agent_not_ready"
#   timeout              src/cli/agent.rs, src/api/wait.rs; docs: "the normal timeout error"
#   agent_prompt_stalled src/api/wait.rs; docs: agent prompt --wait
MATCHED_ERROR_CODES = {"agent_not_ready", "timeout", "agent_prompt_stalled"}
# Herdr 0.9.3 error codes for the commands Horchestra runs (agent/pane/workspace
# targets, agent start/prompt/wait), from the binary and src/app/agents.rs.
HERDR_ERROR_CODES = {
    "agent_blocked", "agent_launch_pending", "agent_name_taken", "agent_not_found",
    "agent_not_ready", "agent_not_running", "agent_pane_busy", "agent_pane_not_found",
    "agent_pane_unavailable", "agent_prompt_failed", "agent_prompt_stalled",
    "agent_send_keys_failed", "agent_start_input_failed", "agent_target_ambiguous",
    "invalid_agent_argument", "invalid_agent_name", "invalid_agent_timeout",
    "pane_not_found", "timeout", "unsupported_agent_kind", "workspace_not_found",
}

# Fields Horchestra reads from Herdr objects (schema $defs in success_response),
# with the plugin code that reads them.
PLUGIN_READS = {
    "PaneInfo": {
        "pane_id": "everywhere", "tab_id": "agentmap.build_forest, team.name_tabs",
        "workspace_id": "team.resolve_space, agentmap.build_overview",
        "terminal_id": "agentmap.build_forest (parent edges), team.same_terminal",
        "agent": "team.sync, toggle.agent_tabs", "agent_status": "team.is_live, views",
        "agent_session": "team.session_of", "label": "team.Space.by_label, toggle.dead_views",
        "tokens": "agentmap/toggle/team tokens_of", "terminal_title_stripped": "agentmap, team.cmd_scan",
        "cwd": "team.resolve_space", "foreground_cwd": "team.pane_owner", "focused": "toggle.open_map",
    },
    "AgentSessionInfo": {"value": "team.session_of"},
    "AgentInfo": {"pane_id": "team.Space.holds_name, team.adopt"},
    "WorkspaceInfo": {"workspace_id": "agentmap.build_overview", "label": "agentmap",
                      "number": "agentmap.build_overview (sort)", "agent_status": "agentmap.build_overview"},
    "TabInfo": {"tab_id": "team.name_tabs, team.cmd_scan", "label": "team.name_tabs, agentmap"},
    "PaneEdgesResult": {"layout": "toggle.layout_for, team.split_largest"},
    "PaneLayoutSnapshot": {"panes": "toggle.rects", "area": "toggle.toggle_overview"},
    "PaneLayoutPane": {"pane_id": "toggle.rects", "rect": "toggle.rects"},
    "PaneLayoutRect": {"x": "toggle.open_map", "y": "toggle.toggle_overview",
                       "width": "toggle/team", "height": "toggle/team"},
    "PaneProcessInfo": {"foreground_processes": "toggle.is_plain_shell"},
    "PaneProcessInfoProcess": {"name": "toggle.is_plain_shell"},
}
# Response nesting Horchestra unwraps from write commands: (result type, key path).
WRITE_RESULTS = {
    ("pane", "split"): ("pane_info", ["pane", "pane_id"]),  # team.split_largest, start_orchestrator
    ("tab", "create"): ("tab_created", ["root_pane", "pane_id"]),  # team.new_member_pane, cmd_reopen
    ("workspace", "create"): [("workspace_created", ["workspace", "workspace_id"]),
                              ("workspace_created", ["root_pane", "pane_id"]),
                              ("workspace_created", ["tab", "tab_id"])],  # team.cmd_reopen
    ("plugin", "pane", "open"): ("plugin_pane_opened", ["plugin_pane", "pane", "pane_id"]),  # toggle
}


# ---- schema helpers ----------------------------------------------------------


def resolve_ref(pointer):
    node = SCHEMA
    for part in pointer.lstrip("#/").split("/"):
        node = node[part]
    return node


def deref(node):
    while isinstance(node, dict) and "$ref" in node:
        node = {**resolve_ref(node["$ref"]), **{k: v for k, v in node.items() if k != "$ref"}}
    return node


def non_null(node):
    """The non-null branch of an `anyOf: [X, null]` node."""
    node = deref(node)
    for option in node.get("anyOf") or []:
        option = deref(option)
        if option.get("type") != "null":
            return option
    return node


def request_params(method):
    for variant in SCHEMA["schemas"]["request"]["oneOf"]:
        props = variant["properties"]
        if props["method"].get("const") == method:
            return deref(props["params"])
    return None


def param_schema(method, path):
    """Schema of a dotted request param path, e.g. ("agent.prompt", "wait.until")."""
    node = request_params(method)
    for part in path.split("."):
        node = non_null(node["properties"][part])
    return node


def result_variant(type_name):
    for variant in SCHEMA["schemas"]["success_response"]["$defs"]["ResponseResult"]["oneOf"]:
        if variant["properties"]["type"].get("const") == type_name:
            return variant
    return None


def success_def(name):
    return SCHEMA["schemas"]["success_response"]["$defs"][name]


JSON_TYPES = {"string": str, "integer": int, "number": (int, float), "boolean": bool,
              "object": dict, "array": list, "null": type(None)}


def conforms(node, value, path="$"):
    """Problems with `value` against a JSON-schema node (the subset Herdr uses)."""
    node = deref(node)
    options = node.get("anyOf") or node.get("oneOf")
    if options:
        if all(conforms(option, value, path) for option in options):
            return [f"{path}: matches none of the allowed shapes"]
        return []
    if "const" in node and value != node["const"]:
        return [f"{path}: expected {node['const']!r}, got {value!r}"]
    if "enum" in node and value not in node["enum"]:
        return [f"{path}: {value!r} not in {node['enum']}"]
    types = node.get("type")
    if types:
        types = [types] if isinstance(types, str) else types
        if not any(isinstance(value, JSON_TYPES[t]) and not (t in ("integer", "number") and isinstance(value, bool))
                   for t in types):
            return [f"{path}: expected {'/'.join(types)}, got {type(value).__name__}"]
    problems = []
    if isinstance(value, dict):
        problems += [f"{path}: missing required {key!r}" for key in node.get("required", []) if key not in value]
        props, extra = node.get("properties", {}), node.get("additionalProperties")
        for key, item in value.items():
            if key in props:
                problems += conforms(props[key], item, f"{path}.{key}")
            elif isinstance(extra, dict):
                problems += conforms(extra, item, f"{path}.{key}")
    if isinstance(value, list) and "items" in node:
        for i, item in enumerate(value):
            problems += conforms(node["items"], item, f"{path}[{i}]")
    return problems


# ---- argv checking -----------------------------------------------------------


def command_path(args):
    for size in (3, 2):
        if tuple(args[:size]) in HERDR_CLI:
            return tuple(args[:size])
    return None


def check_argv(args):
    """Problems with one herdr argv (without the binary) against HERDR_CLI."""
    path = command_path(args)
    if path is None:
        return [f"`herdr {' '.join(map(str, args[:3]))}` is not a known Herdr {HERDR_VERSION} command"]
    spec, problems, positionals = HERDR_CLI[path], [], []
    rest, i = list(args[len(path):]), 0
    while i < len(rest):
        token = rest[i]
        if token == "--":
            if not spec.passthrough:
                problems.append(f"`herdr {' '.join(path)}` does not take `-- <args>`")
            break
        if token.startswith("--"):
            if token not in spec.flags:
                problems.append(f"`herdr {' '.join(path)}` has no flag {token}")
                i += 1
                continue
            param, takes_value = spec.flags[token]
            if takes_value:
                if i + 1 >= len(rest):
                    problems.append(f"{token} needs a value")
                    break
                problems += check_enum(spec.method, param, token, rest[i + 1])
                i += 2
            else:
                i += 1
            continue
        positionals.append(token)
        i += 1
    low, high = spec.positionals
    if len(positionals) < low or (high is not None and len(positionals) > high):
        problems.append(f"`herdr {' '.join(path)}` takes {low}..{high} positional args, got {positionals}")
    return problems


def check_enum(method, param, flag, value):
    if not method or not param:
        return []
    node = param_schema(method, param)
    if "items" in node:
        node = deref(node["items"])
    if "enum" in node and str(value).replace("-", "_") not in node["enum"]:
        return [f"{flag} {value!r} is not one of {node['enum']}"]
    return []


# ---- a fake `herdr` binary that answers from the recorded fixtures ---------


def success_line(doc):
    return json.dumps(doc, separators=(",", ":")) + "\n"


def error_line(code, message, command_id):
    """Herdr's CLI error output (stderr), in the exact recorded format."""
    return json.dumps({"error": {"code": code, "message": message}, "id": command_id},
                      separators=(",", ":")) + "\n"


def new_pane(pane_id, tab_id="wV:t9"):
    pane = {k: v for k, v in RECORDED_PANES[MAP_PANE].items() if k not in ("tokens", "label")}
    pane.update(pane_id=pane_id, tab_id=tab_id, terminal_id="term_" + pane_id.replace(":", ""))
    return pane


def synthetic_write_results():
    """Minimal results for write commands (never run against the live session)."""
    tab = dict(fixture("tab_list.json")["result"]["tabs"][0], tab_id="wV:t9", label="backend")
    workspace = dict(fixture("workspace_get.json")["result"]["workspace"], workspace_id="wZ")
    return {
        ("pane", "split"): {"type": "pane_info", "pane": new_pane("wV:pX")},
        ("tab", "create"): {"type": "tab_created", "tab": tab, "root_pane": new_pane("wV:pY", "wV:t9")},
        ("workspace", "create"): {"type": "workspace_created", "workspace": workspace,
                                  "tab": dict(tab, tab_id="wZ:t1", workspace_id="wZ"),
                                  "root_pane": dict(new_pane("wZ:p1", "wZ:t1"), workspace_id="wZ")},
        ("plugin", "pane", "open"): {"type": "plugin_pane_opened", "plugin_pane": {
            "plugin_id": "horchestra", "entrypoint": "map", "pane": new_pane("wV:pW", "wV:t2")}},
    }


class FakeHerdrBinary:
    """Stands in for subprocess.run of `herdr ...`, answering from fixtures.

    Reads come from the recorded responses; writes get schema-shaped results
    (or empty output) and are only recorded. `errors` maps a command path to
    a list of Herdr error codes to return, in order, before succeeding.
    """

    def __init__(self, errors=None):
        self.calls = []
        self.errors = {path: list(codes) for path, codes in (errors or {}).items()}
        self.recorded = {tuple(meta["argv"]): name for name, meta in INDEX["fixtures"].items()}
        self.writes = synthetic_write_results()

    @contextlib.contextmanager
    def installed(self):
        with mock.patch.object(subprocess, "run", self.run):
            yield self

    def run(self, argv, **_kw):
        assert argv[0] == hc.HERDR, argv
        args = [str(a) for a in argv[1:]]
        self.calls.append(args)
        path = command_path(args)
        if path and self.errors.get(path):
            code = self.errors[path].pop(0)
            return self.result(argv, stderr=error_line(code, f"{code} (synthetic)", "cli:" + ":".join(path)), code=1)
        recorded = self.recorded.get(tuple(args))
        if recorded:
            return self.replay(argv, recorded)
        return self.route(argv, args, path)

    @staticmethod
    def result(argv, stdout="", stderr="", code=0):
        return subprocess.CompletedProcess(argv, code, stdout, stderr)

    def replay(self, argv, name):
        meta = INDEX["fixtures"][name]
        if name.startswith("errors/"):
            env = fixture(name)
            return self.result(argv, env["stdout"], env["stderr"], env["returncode"])
        body = fixture(name)
        return self.result(argv, success_line(body) if meta["format"] == "json" else body)

    def ok(self, argv, path, result):
        return self.result(argv, success_line({"id": "cli:" + ":".join(path), "result": result}))

    def not_found(self, argv, path, code, what):
        return self.result(argv, stderr=error_line(code, f"{what} not found", "cli:" + ":".join(path)), code=1)

    def route(self, argv, args, path):
        flags = dict(zip(args, args[1:]))  # each token -> the token after it
        if path == ("pane", "get"):
            pane = RECORDED_PANES.get(args[2])
            if pane is None:
                return self.not_found(argv, path, "pane_not_found", f"pane {args[2]}")
            return self.ok(argv, path, {"type": "pane_info", "pane": pane})
        if path == ("pane", "list"):
            ws = flags.get("--workspace")
            panes = [p for p in RECORDED_PANES.values() if ws in (None, p["workspace_id"])]
            return self.ok(argv, path, {"type": "pane_list", "panes": panes})
        if path == ("pane", "edges"):
            return self.replay(argv, "pane_edges.json")
        if path == ("pane", "process-info"):
            pane = RECORDED_PANES.get(flags.get("--pane"), {})
            if not pane:
                return self.replay(argv, "errors/process_info.json")
            if pane.get("agent"):
                return self.replay(argv, "process_info_agent.json")
            if (pane.get("tokens") or {}).get(hc.TOKEN_VIEW):
                return self.replay(argv, "process_info_map.json")
            return self.replay(argv, "process_info_plain_shell.json")
        if path == ("agent", "get"):
            agent = next((a for a in RECORDED_AGENTS if args[2] in (a.get("name"), a["pane_id"])), None)
            if agent is None:
                return self.not_found(argv, path, "agent_not_found", f"agent target {args[2]}")
            return self.ok(argv, path, {"type": "agent_info", "agent": agent})
        if path == ("tab", "list"):
            return self.ok(argv, path, {"type": "tab_list", "tabs": []})
        if path in self.writes:
            return self.ok(argv, path, self.writes[path])
        if path == ("pane", "read"):
            return self.result(argv, "$ pytest -q\n12 passed\n")
        return self.result(argv)  # other writes: success, nothing the plugin reads


class FixtureTestCase(unittest.TestCase):
    """Installs the fake binary and keeps Horchestra's state in a temp folder."""

    errors = None

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = os.path.realpath(tmp.name)
        state = os.path.join(self.tmp, "state")
        for name, value in (("STATE_DIR", state), ("REGISTRY", os.path.join(state, "teams.json")),
                            ("TERMINALS", os.path.join(state, "terminals.json"))):
            self.patch(mock.patch.object(team, name, value))
        claude = os.path.join(self.tmp, "claude")
        os.makedirs(os.path.join(claude, "agents"))
        env = {"CLAUDE_CONFIG_DIR": claude, "HOME": os.path.join(self.tmp, "home")}
        self.patch(mock.patch.dict(os.environ, env))
        for key in ("HERDR_PLUGIN_ID", "HERDR_PANE_ID", "HERDR_WORKSPACE_ID", "HERDR_PLUGIN_CONTEXT_JSON",
                    "HORCHESTRA_TEAM_FILE", "AGENTMAP_TEAM_FILE", "HORCHESTRA_DETACHED", "HERDR_SOCKET_PATH"):
            os.environ.pop(key, None)
        self.patch(mock.patch.object(team.time, "sleep", lambda _s: None))
        self.herdr = FakeHerdrBinary(self.errors)
        self.patch(self.herdr.installed())

    def patch(self, patcher):
        patcher.__enter__()
        self.addCleanup(patcher.__exit__, None, None, None)

    def recorded_team(self):
        """A team.toml whose sessions are the recorded docs-site team's."""
        sessions = {p["tokens"]["agentmap_role"]: p["agent_session"]["value"]
                    for p in RECORDED_PANES.values()
                    if p["workspace_id"] == WORKSPACE and p.get("agent_session")}
        root = os.path.join(self.tmp, "docs-site")
        os.makedirs(root, exist_ok=True)
        data = team.new_team()
        data["orchestrator"]["session"] = sessions["orchestrator"]
        for role, kind in (("backend", "claude"), ("tests", "claude"), ("reviewer", "codex"), ("docs", "claude")):
            data["member"].append({"role": role, "kind": kind, "task": f"{role} work",
                                   "session": sessions[role], "reports_to": "orchestrator"})
        path = os.path.join(root, "team.toml")
        team.save(path, data)
        return path

    def run_quietly(self, func, *args, **kw):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            func(*args, **kw)
        return out.getvalue()


# ---- response shapes ---------------------------------------------------------


class RecordedFixturesTest(unittest.TestCase):
    def test_fixtures_were_recorded_from_the_supported_herdr_version(self):
        self.assertEqual(INDEX["herdr_version"], HERDR_VERSION)
        with open(os.path.join(ROOT, "herdr-plugin.toml"), encoding="utf-8") as fh:
            self.assertIn(f'min_herdr_version = "{HERDR_VERSION}"', fh.read())

    def test_success_responses_match_the_recorded_api_schema(self):
        envelope = SCHEMA["schemas"]["success_response"]
        for name, meta in INDEX["fixtures"].items():
            if meta["returncode"] != 0 or meta["format"] != "json" or name == "api_schema.json":
                continue
            with self.subTest(fixture=name):
                doc = fixture(name)
                self.assertEqual([], [r for r in envelope["required"] if r not in doc])
                variant = result_variant(doc["result"]["type"])
                self.assertIsNotNone(variant, doc["result"]["type"])
                self.assertEqual([], conforms(variant, doc["result"]))

    def test_schema_declares_every_field_horchestra_reads(self):
        for definition, fields in PLUGIN_READS.items():
            props = success_def(definition)["properties"]
            for field, used_by in fields.items():
                with self.subTest(field=f"{definition}.{field}", used_by=used_by):
                    self.assertIn(field, props)

    def test_ids_horchestra_indexes_by_are_always_present(self):
        required = set(success_def("PaneInfo")["required"])
        self.assertLessEqual({"pane_id", "tab_id", "workspace_id", "terminal_id", "agent_status"}, required)
        self.assertLessEqual({"value"}, set(success_def("AgentSessionInfo")["required"]))
        self.assertLessEqual({"pane_id", "rect"}, set(success_def("PaneLayoutPane")["required"]))
        self.assertLessEqual({"name"}, set(success_def("PaneProcessInfoProcess")["required"]))

    def test_pane_tokens_are_a_string_map(self):
        tokens = success_def("PaneInfo")["properties"]["tokens"]
        self.assertEqual(tokens["type"], "object")
        self.assertEqual(tokens["additionalProperties"], {"type": "string"})

    def test_every_agent_status_has_a_map_style(self):
        statuses = deref(success_def("AgentStatus"))["enum"]
        self.assertLessEqual({"idle", "working", "blocked", "done", "unknown"}, set(statuses))
        for status in statuses:
            with self.subTest(status=status):
                self.assertEqual(views.NodeView("k", "n", "claude", status).status, status)

    def test_write_results_horchestra_unwraps_are_nested_as_expected(self):
        for path, expected in WRITE_RESULTS.items():
            for type_name, keys in expected if isinstance(expected, list) else [expected]:
                with self.subTest(command=" ".join(path), keys=keys):
                    node = result_variant(type_name)
                    self.assertIsNotNone(node, type_name)
                    for key in keys:
                        node = deref(node)
                        self.assertIn(key, node.get("required", []), f"{key} not required in {type_name}")
                        node = node["properties"][key]
                    self.assertEqual(deref(node).get("type"), "string")

    def test_synthetic_write_results_used_by_these_tests_match_the_schema(self):
        for path, result in synthetic_write_results().items():
            with self.subTest(command=" ".join(path)):
                self.assertEqual([], conforms(result_variant(result["type"]), result))


class PaneShapeTest(FixtureTestCase):
    def test_list_panes_yields_ids_terminal_and_tokens(self):
        panes = hc.list_panes(WORKSPACE)
        self.assertEqual(len(panes), 10)
        for pane in panes:
            with self.subTest(pane=pane.get("pane_id")):
                for field in ("pane_id", "tab_id", "workspace_id", "terminal_id"):
                    self.assertIsInstance(pane[field], str)
                self.assertEqual(pane["workspace_id"], WORKSPACE)
                self.assertIsInstance(team.tokens_of(pane), dict)
                self.assertTrue(all(isinstance(v, str) for v in team.tokens_of(pane).values()))
                self.assertIsInstance(pane.get("label"), str)

    def test_agent_panes_carry_kind_status_session_and_title(self):
        agents = [p for p in hc.list_panes(WORKSPACE) if p.get("agent")]
        self.assertEqual({p["agent"] for p in agents}, {"claude", "codex"})
        for pane in agents:
            with self.subTest(pane=pane["pane_id"]):
                self.assertIn(pane["agent_status"], ("idle", "working", "blocked", "done", "unknown"))
                self.assertRegex(team.session_of(pane), r"^[0-9a-f-]{36}$")
                self.assertIsInstance(pane["terminal_title_stripped"], str)
                self.assertIsInstance(pane["cwd"], str)
                self.assertIsInstance(pane["foreground_cwd"], str)

    def test_session_of_is_none_for_panes_without_an_agent(self):
        self.assertIsNone(team.session_of(hc.get_pane(MAP_PANE)))

    def test_get_pane_unwraps_the_pane_object(self):
        pane = hc.get_pane(ORCH_PANE)
        self.assertEqual(pane["pane_id"], ORCH_PANE)
        self.assertEqual(pane["workspace_id"], WORKSPACE)
        self.assertEqual(pane["label"], team.LABEL_PREFIX + team.ORCHESTRATOR)
        self.assertEqual(team.tokens_of(pane)[hc.TOKEN_ROLE], team.ORCHESTRATOR)

    def test_global_pane_list_spans_workspaces_and_keeps_tokens(self):
        panes = hc.call("pane", "list")["panes"]
        self.assertGreater(len({p["workspace_id"] for p in panes}), 1)
        self.assertTrue(any(toggle.is_map(p) for p in panes))

    def test_view_token_identifies_live_map_panes(self):
        self.assertTrue(toggle.is_map(hc.get_pane(MAP_PANE)))
        self.assertFalse(toggle.is_overview(hc.get_pane(MAP_PANE)))
        self.assertFalse(toggle.is_map(hc.get_pane(ORCH_PANE)))
        self.assertEqual(toggle.map_panes(hc.list_panes(WORKSPACE)),
                         ["wV:pK", "wV:pJ", "wV:pM", "wV:pN", "wV:pP"])
        self.assertEqual(toggle.agent_tabs(hc.list_panes(WORKSPACE)),
                         {"wV:t1", "wV:t2", "wV:t3", "wV:t4", "wV:t5"})


class WorkspaceAndTabShapeTest(FixtureTestCase):
    def test_get_workspace_unwraps_label_and_number(self):
        ws = hc.get_workspace(WORKSPACE)
        self.assertEqual(ws["workspace_id"], WORKSPACE)
        self.assertIsInstance(ws["label"], str)
        self.assertIsInstance(ws["number"], int)

    def test_workspace_list_has_ids_labels_numbers_and_status(self):
        workspaces = hc.call("workspace", "list")["workspaces"]
        self.assertEqual([w["number"] for w in workspaces], [1, 2, 3, 4, 5])
        for ws in workspaces:
            self.assertIsInstance(ws["workspace_id"], str)
            self.assertIsInstance(ws["label"], str)
            self.assertIn(ws["agent_status"], views.STATUS.keys() | {"unknown"})

    def test_tab_list_maps_tab_ids_to_labels(self):
        tabs = {t["tab_id"]: t["label"] for t in hc.call("tab", "list", "--workspace", WORKSPACE)["tabs"]}
        self.assertEqual(tabs["wV:t2"], "orchestrator")
        self.assertEqual(set(tabs), {p["tab_id"] for p in hc.list_panes(WORKSPACE)})


class AgentShapeTest(FixtureTestCase):
    def test_agent_get_by_name_names_its_pane(self):
        name = INDEX["fixtures"]["agent_get_by_name.json"]["argv"][2]
        agent = (hc.call_quiet("agent", "get", name) or {}).get("agent") or {}
        self.assertEqual(agent["pane_id"], ORCH_PANE)
        self.assertEqual(agent["name"], name)
        self.assertTrue(AGENT_NAME_RE.match(name))

    def test_agent_get_by_pane_id_returns_the_same_agent(self):
        by_pane = hc.call("agent", "get", ORCH_PANE)["agent"]
        self.assertEqual(by_pane["pane_id"], ORCH_PANE)
        self.assertEqual(team.session_of(by_pane), team.session_of(hc.get_pane(ORCH_PANE)))

    def test_holds_name_matches_the_named_agents_pane(self):
        path = self.recorded_team()
        space = team.Space(WORKSPACE, path, team.load(path))
        name = INDEX["fixtures"]["agent_get_by_name.json"]["argv"][2]
        self.assertTrue(space.holds_name(team.ORCHESTRATOR, name))
        self.assertFalse(space.holds_name("backend", name))

    def test_unknown_agent_name_reads_as_not_held(self):
        self.assertIsNone(hc.call_quiet("agent", "get", "no-such-agent"))


class LayoutAndProcessShapeTest(FixtureTestCase):
    def test_pane_edges_layout_gives_integer_rects_per_pane(self):
        layout = toggle.layout_for(ORCH_PANE)
        boxes = toggle.rects(layout)
        self.assertEqual(set(boxes), {MAP_PANE, ORCH_PANE})
        for rect in boxes.values():
            self.assertEqual({k: type(v) for k, v in rect.items()},
                             {"x": int, "y": int, "width": int, "height": int})
        self.assertIsInstance(layout["area"]["height"], int)

    def test_split_largest_splits_the_biggest_pane_along_its_long_side(self):
        new = team.split_largest([MAP_PANE, ORCH_PANE], "/home/user/projects/docs-site")
        self.assertEqual(new, "wV:pX")
        split = [c for c in self.herdr.calls if c[:2] == ["pane", "split"]]
        self.assertEqual(split, [["pane", "split", ORCH_PANE, "--direction", "down",
                                  "--cwd", "/home/user/projects/docs-site", "--no-focus"]])

    def test_plain_shell_is_recognised_from_foreground_process_names(self):
        self.assertTrue(toggle.is_plain_shell(PLAIN_SHELL_PANE))  # login shell "-zsh" -> name "zsh"
        self.assertFalse(toggle.is_plain_shell(MAP_PANE))  # the map's python process
        self.assertFalse(toggle.is_plain_shell(ORCH_PANE))  # claude and its helpers
        self.assertFalse(toggle.is_plain_shell("w99:p99"))  # pane_not_found -> unknown

    def test_live_maps_are_never_taken_for_dead_views(self):
        panes = hc.call("pane", "list")["panes"]
        self.assertEqual(toggle.dead_views(panes, toggle.MAP_LABEL), [])
        self.assertEqual(toggle.dead_views(panes, toggle.OVERVIEW_LABEL), [])


class AgentMapShapeTest(FixtureTestCase):
    def test_build_forest_draws_the_recorded_team_under_its_orchestrator(self):
        roots, raw = agentmap.build_forest(hc.list_panes(WORKSPACE), MAP_PANE, "wV:t2", set(), None)
        self.assertEqual([r.name for r in roots], ["orchestrator"])
        orch = roots[0]
        self.assertEqual(orch.key, ORCH_PANE)
        self.assertTrue(orch.here)
        self.assertEqual(sorted(c.name for c in orch.children), ["backend", "docs", "reviewer", "tests"])
        self.assertEqual({c.kind for c in orch.children}, {"claude", "codex"})
        self.assertTrue(all(c.status == "idle" for c in orch.children))
        self.assertNotIn(MAP_PANE, raw)  # map panes are not agents
        self.assertTrue(all(c.detail for c in orch.children))  # task tokens

    def test_build_overview_has_one_root_per_space_in_number_order(self):
        workspaces = hc.call("workspace", "list")["workspaces"]
        panes = hc.call("pane", "list")["panes"]
        roots, _ = agentmap.build_overview(panes, workspaces, MAP_PANE, WORKSPACE, set(), None)
        self.assertEqual([r.name for r in roots], [w["label"] for w in workspaces])
        here = next(r for r in roots if r.here)
        self.assertEqual(here.key, "ws:" + WORKSPACE)
        self.assertEqual(here.detail, "5 agents")
        self.assertEqual([c.name for c in here.children], ["orchestrator"])

    def test_agent_status_values_map_to_icons(self):
        for pane in hc.call("pane", "list")["panes"]:
            node = views.NodeView(pane["pane_id"], "n", pane.get("agent") or "shell", pane["agent_status"])
            self.assertEqual(node.status, pane["agent_status"])


class TeamRenderingTest(FixtureTestCase):
    def args(self, path, **kw):
        return argparse.Namespace(file=path, **kw)

    def test_status_finds_every_member_by_recorded_session(self):
        path = self.recorded_team()
        os.environ["HERDR_PANE_ID"] = ORCH_PANE
        out = self.run_quietly(team.cmd_status, self.args(path))
        rows = {line.split()[0]: line.split() for line in out.splitlines()[2:] if line.strip()}
        expected = {"orchestrator": "wV:p2", "backend": "wV:p1", "tests": "wV:p5",
                    "reviewer": "wV:p7", "docs": "wV:p9"}
        for role, pane_id in expected.items():
            with self.subTest(role=role):
                self.assertIn(pane_id, rows[role])
                self.assertIn("idle", rows[role])

    def test_sync_tags_members_with_their_parents_terminal(self):
        path = self.recorded_team()
        os.environ["HERDR_PANE_ID"] = ORCH_PANE
        self.run_quietly(team.cmd_sync, self.args(path))
        metadata = [c for c in self.herdr.calls if c[:2] == ["pane", "report-metadata"]]
        backend = next(c for c in metadata if c[2] == "wV:p1")
        orch_terminal = RECORDED_PANES[ORCH_PANE]["terminal_id"]
        self.assertIn(f"{hc.TOKEN_PARENT}={orch_terminal}", backend)

    def test_scan_lists_unmanaged_agents_with_tab_labels(self):
        path = self.recorded_team()
        data = team.load(path)
        data["member"] = [m for m in data["member"] if m["role"] != "reviewer"]
        team.save(path, data)
        os.environ["HERDR_PANE_ID"] = ORCH_PANE
        out = self.run_quietly(team.cmd_scan, self.args(path))
        self.assertIn("wV:p7", out)
        self.assertIn("reviewer", out)  # its tab label
        self.assertIn("codex", out)
        self.assertNotIn("wV:p1", out)


class IntegrationStatusShapeTest(unittest.TestCase):
    """install.check_integrations parses `herdr integration status` text."""

    def run_check(self, text):
        reports = []
        done = subprocess.CompletedProcess(["herdr"], 0, text, "")
        with mock.patch.object(install.subprocess, "run", return_value=done):
            install.check_integrations(reports.append)
        return reports

    def test_status_lines_are_name_colon_state(self):
        lines = fixture("integration_status.txt").splitlines()
        names = [line.split(":", 1)[0].strip() for line in lines]
        self.assertIn("claude", names)
        self.assertIn("codex", names)
        for line in lines:
            self.assertRegex(line, r"^[a-z0-9-]+( \([a-z]+\))?: (current|outdated|not installed)\b")

    def test_current_integrations_need_no_advice(self):
        self.assertEqual(self.run_check(fixture("integration_status.txt")), [])

    def test_outdated_claude_integration_is_reported(self):
        text = re.sub(r"^claude: current \(v\d+\)", "claude: outdated (v9 < v10)",
                      fixture("integration_status.txt"), flags=re.M)
        reports = self.run_check(text)
        self.assertEqual(len(reports), 1)
        self.assertIn("herdr integration install claude", reports[0])


# ---- error responses -----------------------------------------------------------


class ErrorShapeTest(FixtureTestCase):
    def error_fixtures(self):
        return {name: fixture(name) for name in INDEX["fixtures"] if name.startswith("errors/")}

    def test_errors_are_json_on_stderr_with_exit_status_1(self):
        for name, env in self.error_fixtures().items():
            with self.subTest(fixture=name):
                self.assertEqual(env["returncode"], 1)
                self.assertEqual(env["stdout"], "")
                doc = json.loads(env["stderr"])
                self.assertEqual([], conforms(SCHEMA["schemas"]["error_response"], doc))
                self.assertIn(doc["error"]["code"], HERDR_ERROR_CODES)

    def test_herdr_error_text_carries_the_error_code(self):
        for name, env in self.error_fixtures().items():
            code = json.loads(env["stderr"])["error"]["code"]
            with self.subTest(fixture=name):
                with self.assertRaises(hc.HerdrError) as caught:
                    hc.call(*env["argv"])
                self.assertEqual(caught.exception.code, code)
                message = json.loads(env["stderr"])["error"]["message"]
                self.assertEqual(str(caught.exception), f"{code}: {message}")
                self.assertIsNone(hc.call_quiet(*env["argv"]))

    def test_recorded_not_found_codes(self):
        codes = {name: json.loads(env["stderr"])["error"]["code"] for name, env in self.error_fixtures().items()}
        self.assertEqual(codes["errors/pane_get.json"], "pane_not_found")
        self.assertEqual(codes["errors/agent_get.json"], "agent_not_found")
        self.assertEqual(codes["errors/workspace_get.json"], "workspace_not_found")
        self.assertEqual(codes["errors/pane_list.json"], "workspace_not_found")

    def test_getters_raise_herdr_error_for_missing_targets(self):
        with self.assertRaises(hc.HerdrError):
            hc.get_pane("w99:p99")
        with self.assertRaises(hc.HerdrError):
            hc.list_panes("w999")
        with self.assertRaises(hc.HerdrError):
            hc.get_workspace("w999")

    def test_synthesized_errors_use_the_recorded_format(self):
        env = fixture("errors/pane_get.json")
        doc = json.loads(env["stderr"])
        self.assertEqual(error_line(doc["error"]["code"], doc["error"]["message"], doc["id"]), env["stderr"])


def matched_error_codes():
    """`<x>.code ==/!= "<code>"` comparisons in the plugin source -> {code: [file:line]}."""
    found = {}
    for path in plugin_sources():
        tree = parse_source(path)
        for node in ast.walk(tree):
            if (isinstance(node, ast.Compare) and len(node.ops) == 1 and isinstance(node.ops[0], (ast.Eq, ast.NotEq))
                    and isinstance(node.left, ast.Attribute) and node.left.attr == "code"
                    and isinstance(node.comparators[0], ast.Constant)
                    and isinstance(node.comparators[0].value, str)):
                found.setdefault(node.comparators[0].value, []).append(f"{os.path.relpath(path, ROOT)}:{node.lineno}")
    return found


def substring_error_matches():
    """`"<text>" [not] in str(exc)` comparisons: fragile, must not come back."""
    found = []
    for path in plugin_sources():
        for node in ast.walk(parse_source(path)):
            if (isinstance(node, ast.Compare) and len(node.ops) == 1 and isinstance(node.ops[0], (ast.In, ast.NotIn))
                    and isinstance(node.left, ast.Constant) and isinstance(node.left.value, str)
                    and isinstance(node.comparators[0], ast.Call)
                    and getattr(node.comparators[0].func, "id", None) == "str"):
                found.append(f"{os.path.relpath(path, ROOT)}:{node.lineno}")
    return found


class ErrorCodeContractTest(FixtureTestCase):
    def test_every_error_code_horchestra_matches_exists_in_herdr(self):
        found = matched_error_codes()
        self.assertEqual(set(found), MATCHED_ERROR_CODES,
                         f"update MATCHED_ERROR_CODES after checking Herdr: {found}")
        for code in MATCHED_ERROR_CODES:
            with self.subTest(code=code, used_at=found[code]):
                self.assertIn(code, HERDR_ERROR_CODES)

    def test_errors_are_matched_by_exact_code_not_substring(self):
        # "timeout" in str(exc) would also match invalid_agent_timeout.
        self.assertEqual(substring_error_matches(), [])

    def test_agent_start_timeout_is_within_herdr_limits(self):
        low, high = AGENT_START_TIMEOUT_MS
        self.assertTrue(low < team.START_TIMEOUT_MS <= high)
        self.assertIn("greater than 3000 and at most 300000",
                      request_params("agent.start")["properties"]["timeout_ms"]["description"])


class ClientErrorTest(unittest.TestCase):
    def run_call(self, side_effect):
        with mock.patch.object(hc.subprocess, "run", side_effect=side_effect):
            with self.assertRaises(hc.HerdrError) as caught:
                hc.call("agent", "prompt", "x", "hi", timeout=2)
        return caught.exception

    def test_herdr_not_answering_counts_as_timeout(self):
        exc = self.run_call(subprocess.TimeoutExpired(["herdr"], 2))
        self.assertEqual(exc.code, "timeout")
        self.assertEqual(str(exc), "timeout: herdr agent prompt took over 2s")

    def test_plain_text_stderr_is_kept_without_a_code(self):
        exc = self.run_call(lambda *a, **k: subprocess.CompletedProcess(a[0], 2, "", "boom\n"))
        self.assertIsNone(exc.code)
        self.assertEqual(str(exc), "boom")

    def test_missing_binary_has_no_code(self):
        exc = self.run_call(FileNotFoundError("no herdr"))
        self.assertIsNone(exc.code)


class ErrorBranchTest(FixtureTestCase):
    def test_agent_not_ready_on_start_waits_for_the_human(self):
        self.herdr.errors[("agent", "start")] = ["agent_not_ready"]
        with contextlib.redirect_stdout(io.StringIO()):
            team.start_agent("docs-site-backend", "claude", "wV:p1", ["--name", "backend"])
        waits = [c for c in self.herdr.calls if c[:2] == ["agent", "wait"]]
        self.assertEqual(waits, [["agent", "wait", "docs-site-backend", "--until", "idle", "--until", "done",
                                  "--timeout", str(team.READY_WAIT_MS)]])

    def test_agent_start_timeout_is_not_retried(self):
        self.herdr.errors[("agent", "start")] = ["timeout"]
        with self.assertRaises(team.TeamError):
            team.start_agent("docs-site-backend", "claude", "wV:p1", [])
        self.assertEqual(len([c for c in self.herdr.calls if c[:2] == ["agent", "start"]]), 1)

    def test_other_start_errors_are_retried(self):
        self.herdr.errors[("agent", "start")] = ["agent_pane_busy"]
        team.start_agent("docs-site-backend", "claude", "wV:p1", [])
        self.assertEqual(len([c for c in self.herdr.calls if c[:2] == ["agent", "start"]]), 2)

    def test_stalled_prompt_is_retried(self):
        self.herdr.errors[("agent", "prompt")] = ["agent_prompt_stalled"]
        self.assertTrue(team.deliver(ORCH_PANE, "hello", log=lambda _m: None))
        self.assertEqual(len([c for c in self.herdr.calls if c[:2] == ["agent", "prompt"]]), 2)

    def test_prompt_timeout_counts_as_a_started_turn(self):
        self.herdr.errors[("agent", "prompt")] = ["timeout"]
        self.assertTrue(team.deliver(ORCH_PANE, "hello", log=lambda _m: None))
        self.assertEqual(len([c for c in self.herdr.calls if c[:2] == ["agent", "prompt"]]), 1)

    def test_invalid_timeout_is_not_mistaken_for_a_timeout(self):
        self.herdr.errors[("agent", "start")] = ["invalid_agent_timeout"]
        team.start_agent("docs-site-backend", "claude", "wV:p1", [])
        self.assertEqual(len([c for c in self.herdr.calls if c[:2] == ["agent", "start"]]), 2)

    def test_blocked_agent_is_not_messaged(self):
        self.herdr.errors[("agent", "prompt")] = ["agent_blocked"]
        self.assertFalse(team.deliver(ORCH_PANE, "hello", log=lambda _m: None))


# ---- command shapes --------------------------------------------------------------


def parse_source(path):
    with open(path, encoding="utf-8") as fh:
        return ast.parse(fh.read(), path)


def plugin_sources():
    files = [os.path.join(ROOT, f) for f in sorted(os.listdir(ROOT)) if f.endswith(".py")]
    bin_dir = os.path.join(ROOT, "bin")
    # Files only: compiling bin/ scripts creates a bin/__pycache__ directory.
    files += [os.path.join(bin_dir, f) for f in sorted(os.listdir(bin_dir)) if os.path.isfile(os.path.join(bin_dir, f))]
    return files


def literal_herdr_argvs():
    """(file:line, leading string args) for every herdr command literal in the source.

    Covers hc.call/call_quiet(...) calls, [HERDR, ...] lists and argv lists
    starting with a Herdr command group (e.g. ["agent", "start", ...]).
    """
    out = []
    for path in plugin_sources():
        tree = parse_source(path)
        for node in ast.walk(tree):
            items = None
            if isinstance(node, ast.Call):
                name = getattr(node.func, "attr", None) or getattr(node.func, "id", None)
                if name in ("call", "call_quiet"):
                    items = node.args
            elif isinstance(node, ast.List) and node.elts:
                first = node.elts[0]
                if isinstance(first, ast.Name) and first.id == "HERDR":
                    items = node.elts[1:]
                elif (isinstance(first, ast.Constant) and first.value in HERDR_GROUPS and len(node.elts) > 1
                      and isinstance(node.elts[1], ast.Constant) and isinstance(node.elts[1].value, str)
                      and not node.elts[1].value.startswith("-")):
                    items = node.elts
            if not items or not isinstance(items[0], ast.Constant):
                continue
            args = [i.value if isinstance(i, ast.Constant) and isinstance(i.value, str) else "<expr>"
                    for i in items]
            out.append((f"{os.path.relpath(path, ROOT)}:{node.lineno}", args))
    return out


class CommandShapeTest(FixtureTestCase):
    def test_cli_table_matches_the_recorded_api_schema(self):
        methods = {v["properties"]["method"]["const"] for v in SCHEMA["schemas"]["request"]["oneOf"]}
        for path, spec in HERDR_CLI.items():
            if spec.method is None:
                continue
            with self.subTest(command=" ".join(path)):
                self.assertIn(spec.method, methods)
                for flag, (param, _takes) in spec.flags.items():
                    if param:
                        self.assertIsInstance(param_schema(spec.method, param), dict, flag)

    def test_recorded_fixture_commands_are_valid(self):
        for name, meta in INDEX["fixtures"].items():
            with self.subTest(fixture=name):
                self.assertEqual(check_argv(meta["argv"]), [])

    def test_every_herdr_command_literal_in_the_source_is_known(self):
        problems = []
        for where, args in literal_herdr_argvs():
            path = command_path(args)
            if path is None:
                problems.append(f"{where}: unknown command `herdr {' '.join(args[:3])}`")
                continue
            for arg in args[len(path):]:
                if arg == "--":
                    break
                if arg.startswith("--") and arg not in HERDR_CLI[path].flags:
                    problems.append(f"{where}: `herdr {' '.join(path)}` has no flag {arg}")
        self.assertEqual(problems, [])

    def test_source_scan_finds_the_commands_horchestra_uses(self):
        paths = {command_path(args) for _, args in literal_herdr_argvs()}
        for expected in [("agent", "start"), ("pane", "report-metadata"), ("pane", "read"),
                         ("plugin", "pane", "open"), ("pane", "edges"), ("integration", "status")]:
            self.assertIn(expected, paths)

    def drive_plugin(self):
        """Run the plugin code paths that talk to Herdr; returns every argv."""
        path = self.recorded_team()
        data = team.load(path)
        space = team.Space(WORKSPACE, path, data)
        quiet = lambda _m: None  # noqa: E731
        self.herdr.errors[("agent", "focus")] = ["agent_not_found"]
        with contextlib.redirect_stdout(io.StringIO()):
            hc.set_tokens("wV:p1", {hc.TOKEN_ROLE: "backend", hc.TOKEN_TASK: "x"}, clear=[hc.TOKEN_NEEDS])
            hc.read_text("wV:p1", lines=40)
            hc.focus_pane("wV:pJ")
            team.wait_until_ready("docs-site-backend", "wV:p1", log=quiet)
            team.deliver(ORCH_PANE, "hello", log=quiet)
            team.prompt(ORCH_PANE, "hello")
            team.split_largest([MAP_PANE, ORCH_PANE], space.root)
            team.new_member_pane(space, space.root, "frontend")
            team.quit_agent(PLAIN_SHELL_PANE)
            team.sync(space, spawn=False, log=quiet)
            team.name_tabs(space, log=quiet)
            toggle.open_map(hc.list_panes(WORKSPACE), ORCH_PANE)
            toggle.toggle_overview(ORCH_PANE)
            toggle.close_view(MAP_PANE)
            install.reload_config(quiet)
            install.check_integrations(quiet)
            os.environ["HERDR_PANE_ID"] = "wV:p1"
            team.cmd_report(argparse.Namespace(file=path, clear=False, text=["need", "a", "decision"],
                                               needs_you=True))
            team.cmd_report(argparse.Namespace(file=path, clear=True, text=[], needs_you=False))
            claude = team.member_args(space, data["member"][0], team.load_profile(space, data["member"][0]))
            team.start_agent(space.agent_name("backend"), "claude", "wV:p1", claude)
            team.start_agent(space.agent_name("backend"), "claude", "wV:p1",
                             team.resume_launch(space, "backend", "claude", "00000000-0000-4000-8000-000000000008"))
            team.start_agent(space.agent_name(team.ORCHESTRATOR), "claude", ORCH_PANE,
                             team.orchestrator_args(space, data))
        return self.herdr.calls

    def test_every_argv_the_plugin_builds_at_runtime_is_valid(self):
        problems = []
        for args in self.drive_plugin():
            problems += [f"herdr {' '.join(args)}: {p}" for p in check_argv(args)]
        self.assertEqual(problems, [])

    def test_runtime_and_source_scan_together_cover_the_cli_table(self):
        used = {command_path(args) for args in self.drive_plugin()}
        used |= {command_path(args) for _, args in literal_herdr_argvs()}
        unused = set(HERDR_CLI) - used - {("api", "schema"), ("plugin", "list"), ("agent", "list")}
        self.assertEqual(unused, set(), "drop commands the plugin no longer uses from HERDR_CLI")

    def test_claude_flags_are_passed_only_after_the_double_dash(self):
        os.makedirs(os.path.join(self.tmp, "docs-site", ".orchestra", "roles", "backend"), exist_ok=True)
        with open(os.path.join(self.tmp, "docs-site", ".orchestra", "roles", "backend", "ROLE.md"), "w") as fh:
            fh.write("Backend role.\n")
        open(os.path.join(os.environ["CLAUDE_CONFIG_DIR"], "agents", "orchestrator.md"), "w").close()
        starts = [c for c in self.drive_plugin() if c[:2] == ["agent", "start"]]
        self.assertEqual(len(starts), 3)
        seen = set()
        for args in starts:
            with self.subTest(argv=args):
                self.assertIn("--", args)
                cut = args.index("--")
                herdr_part, claude_part = args[:cut], args[cut + 1:]
                self.assertEqual(check_argv(herdr_part), [])
                for flag in team.RESERVED_CLAUDE_FLAGS:
                    self.assertNotIn(flag, herdr_part)
                seen |= {a for a in claude_part if a.startswith("-")}
                self.assertEqual(herdr_part[herdr_part.index("--timeout") + 1], str(team.START_TIMEOUT_MS))
        # The Claude flags Horchestra relies on, all after `--`.
        self.assertLessEqual({"--name", "--agent", "--settings", "--add-dir", "--resume",
                              "--system-prompt-snapshot"}, seen)

    def test_metadata_tokens_fit_herdrs_token_rules(self):
        pattern = re.compile(SCHEMA["schemas"]["request"]["$defs"]["PaneReportMetadataParams"]
                             ["properties"]["tokens"]["propertyNames"]["pattern"])
        limit = request_params("pane.report_metadata")["properties"]["tokens"]["maxProperties"]
        names = [getattr(hc, n) for n in dir(hc) if n.startswith("TOKEN_")]
        for name in names:
            self.assertRegex(name, pattern)
        for args in self.drive_plugin():
            if args[:2] == ["pane", "report-metadata"]:
                tokens = [args[i + 1].split("=", 1)[0] for i, a in enumerate(args) if a in ("--token", "--clear-token")]
                self.assertLessEqual(len(tokens), limit)
                self.assertLessEqual(set(tokens), set(names))

    def test_agent_names_follow_herdrs_name_rule(self):
        space = team.Space.__new__(team.Space)
        space.team = team.new_team()
        for folder in ("docs-site", "My Project", "2024_reports", "ünïcode-app", "x" * 60):
            space.root = os.path.join(self.tmp, folder)
            for role in ("orchestrator", "FE", "backend_api", "R" + "x" * 23):
                with self.subTest(folder=folder, role=role):
                    self.assertRegex(space.plain_agent_name(role), AGENT_NAME_RE)
                    self.assertRegex(space.hashed_agent_name(role), AGENT_NAME_RE)

    def test_event_subscriptions_exist_in_herdr(self):
        kinds = {}
        for variant in SCHEMA["schemas"]["request"]["$defs"]["Subscription"]["oneOf"]:
            kinds[variant["properties"]["type"]["const"]] = variant
        for event in hc.GLOBAL_EVENTS:
            with self.subTest(event=event):
                self.assertIn(event, kinds)
                self.assertEqual(kinds[event].get("required"), ["type"])
        per_pane = kinds["pane.agent_status_changed"]
        self.assertIn("pane_id", per_pane["properties"])
        self.assertEqual(request_params("events.subscribe")["required"], ["subscriptions"])

    def test_read_text_uses_plain_text_output(self):
        self.assertEqual(hc.read_text("wV:p1", lines=5), "$ pytest -q\n12 passed\n")
        self.assertEqual(self.herdr.calls[-1], ["pane", "read", "wV:p1", "--source", "recent", "--lines", "5"])


if __name__ == "__main__":
    unittest.main()
