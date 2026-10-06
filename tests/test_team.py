import argparse
import os
import tempfile
import unittest
from unittest import mock

from tests import helpers  # noqa: F401
import herdr_client as hc
import team
import toggle


def pane(pid, label=None, agent="claude", session=None, role=None, tab="w1:t1", ws="w1", **extra):
    p = {"pane_id": pid, "tab_id": tab, "workspace_id": ws, "agent": agent,
         "agent_status": "idle" if agent else None, "terminal_id": "term_" + pid, "tokens": {}}
    if label:
        p["label"] = label
    if session:
        p["agent_session"] = {"value": session}
    if role:
        p["tokens"][hc.TOKEN_ROLE] = role
    p.update(extra)
    return p


class FakeHerdr:
    """Stands in for herdr_client: records calls, serves a fixed pane list."""

    def __init__(self, panes, processes=None, broken=(), tabs=()):
        self.panes = panes
        self.tabs = list(tabs)
        self.processes = processes or {}  # pane_id -> foreground process names
        self.broken = set(broken)  # workspaces whose pane list fails
        self.calls = []

    def call(self, *args, timeout=5.0):
        self.calls.append(args)
        if args[:2] == ("pane", "list"):
            return {"panes": list(self.panes)}
        if args[:2] == ("pane", "process-info"):
            names = self.processes.get(args[-1])
            if names is None:
                raise hc.HerdrError("pane_not_found")
            return {"process_info": {"foreground_processes": [{"pid": 1, "name": n} for n in names]}}
        if args[:2] == ("tab", "list"):
            return {"tabs": list(self.tabs)}
        return {}

    def call_quiet(self, *args, timeout=5.0):
        try:
            return self.call(*args, timeout=timeout)
        except hc.HerdrError:
            return None

    def list_panes(self, workspace_id):
        if workspace_id in self.broken:
            raise hc.HerdrError("timeout: pane list")
        return [p for p in self.panes if p.get("workspace_id") == workspace_id]

    def set_tokens(self, pane_id, tokens=None, clear=()):
        self.calls.append(("set_tokens", pane_id))
        return {}

    def get_pane(self, pane_id):
        return next((p for p in self.panes if p["pane_id"] == pane_id), {})

    def patch(self, test):
        for name in ("call", "call_quiet", "list_panes", "set_tokens", "get_pane"):
            patcher = mock.patch.object(hc, name, getattr(self, name))
            patcher.start()
            test.addCleanup(patcher.stop)
        return self

    def closed(self):
        return [c[-1] for c in self.calls if c[:2] in (("pane", "close"), ("plugin", "pane"))
                and (c[:2] == ("pane", "close") or c[2] == "close")]

    def sent_keys(self):
        return [c for c in self.calls if c[:2] == ("pane", "send-keys")]


class TomlTest(unittest.TestCase):
    def test_dump_load_roundtrip_with_awkward_text(self):
        data = team.new_team()
        data["orchestrator"]["brief"] = 'Ship "v2"\nthen tidy up — ünïcode'
        data["member"].append({"role": "FE", "kind": "codex", "task": "a\\b \"c\"",
                               "args": ["--model", "x"], "session": "abc"})
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "team.toml")
            team.save(path, data)
            loaded = team.load(path)
        self.assertEqual(loaded["orchestrator"]["brief"], data["orchestrator"]["brief"])
        self.assertEqual(loaded["member"][0], data["member"][0])

    def test_load_rejects_bad_and_duplicate_roles(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "team.toml")
            for body in ('[[member]]\nrole = "has space"\n',
                         '[[member]]\nrole = "FE"\n[[member]]\nrole = "fe"\n',
                         '[[member]]\nrole = "orchestrator"\n'):
                with open(path, "w") as fh:
                    fh.write(body)
                with self.assertRaises(team.TeamError):
                    team.load(path)

    def test_non_utf8_file_is_a_team_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "team.toml")
            with open(path, "wb") as fh:
                fh.write(b'[orchestrator]\nbrief = "\xff"\n')
            with self.assertRaises(team.TeamError):
                team.load(path)

    def test_find_team_file_walks_up(self):
        with tempfile.TemporaryDirectory() as tmp:
            nested = os.path.join(tmp, "a", "b")
            os.makedirs(nested)
            team.save(os.path.join(tmp, "team.toml"), team.new_team())
            self.assertEqual(team.find_team_file(nested), os.path.join(tmp, "team.toml"))


class OrderingTest(unittest.TestCase):
    def test_parents_come_before_reports(self):
        members = [{"role": "tests", "reports_to": "FE"}, {"role": "FE"}, {"role": "x", "reports_to": "x"}]
        order = [m["role"] for m in team.ordered_members(members)]
        self.assertLess(order.index("FE"), order.index("tests"))
        self.assertEqual(sorted(order), ["FE", "tests", "x"])


NAME_RE = r"^[a-z][a-z0-9_-]{0,31}$"


class NameTest(unittest.TestCase):
    def test_agent_names_are_valid_and_stable(self):
        space = team.Space.__new__(team.Space)
        space.root = "/projects/Web Shop"
        name = space.agent_name("Research_Lead")
        self.assertRegex(name, NAME_RE)
        self.assertEqual(name, space.agent_name("Research_Lead"))
        for root in ("/projects/123-numbers-first-and-a-very-long-directory-name", "/", "/x/9"):
            space = team.Space.__new__(team.Space)
            space.root = root
            self.assertRegex(space.agent_name("QA"), NAME_RE)
            self.assertRegex(space.agent_name("a" * 24), NAME_RE)

    def test_same_folder_name_in_two_places_gives_two_names(self):
        FakeHerdr([]).patch(self)
        one = team.Space("w1", "/work/api/team.toml", make_team([{"role": "qa"}]))
        two = team.Space("w2", "/clients/api/team.toml", make_team([{"role": "qa"}]))
        self.assertNotEqual(one.agent_name("qa"), two.agent_name("qa"))
        self.assertRegex(one.agent_name("qa"), r"^api-[0-9a-f]{4}-qa$")

    def test_a_team_already_holding_the_plain_name_keeps_it(self):
        fake = FakeHerdr([pane("w1:p2", label="team:qa", session="s-qa")])
        real_call = fake.call
        fake.call = lambda *a, **k: ({"agent": {"pane_id": "w1:p2"}} if a[:3] == ("agent", "get", "api-qa")
                                     else real_call(*a, **k))
        fake.patch(self)
        space = team.Space("w1", "/work/api/team.toml", make_team([{"role": "qa", "session": "s-qa"}]))
        self.assertEqual(space.agent_name("qa"), "api-qa")

    def test_plain_name_held_by_another_project_is_not_reused(self):
        fake = FakeHerdr([pane("w1:p2", label="team:qa", session="s-qa"),
                          pane("w2:p1", ws="w2", session="other")])
        real_call = fake.call
        fake.call = lambda *a, **k: ({"agent": {"pane_id": "w2:p1"}} if a[:3] == ("agent", "get", "api-qa")
                                     else real_call(*a, **k))
        fake.patch(self)
        space = team.Space("w1", "/work/api/team.toml", make_team([{"role": "qa", "session": "s-qa"}]))
        self.assertNotEqual(space.agent_name("qa"), "api-qa")
        self.assertTrue(space.agent_name("qa").startswith("api-"))


class ValidationTest(unittest.TestCase):
    def load(self, body):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        os.makedirs(os.path.join(tmp.name, "web"))
        path = os.path.join(tmp.name, "team.toml")
        with open(path, "w") as fh:
            fh.write(body)
        return team.load(path)

    def assertRejected(self, body, field):
        with self.assertRaises(team.TeamError) as ctx:
            self.load(body)
        self.assertIn(field, str(ctx.exception))

    def test_field_types_are_checked_with_their_path(self):
        self.assertRejected('[[member]]\nrole = "qa"\ntask = 3\n', "member[0] (qa).task")
        self.assertRejected('[[member]]\nrole = "qa"\nargs = [1]\n', "member[0] (qa).args")
        self.assertRejected('[[member]]\nrole = "qa"\nuses_skills = [true]\n', "uses_skills")
        self.assertRejected('[orchestrator]\nsession = 5\n', "orchestrator.session")
        self.assertRejected('name_tabs = "no"\n', "name_tabs")

    def test_one_string_is_one_argument(self):
        data = self.load('[[member]]\nrole = "qa"\nkind = "codex"\nargs = "--model gpt x"\n')
        self.assertEqual(data["member"][0]["args"], ["--model gpt x"])

    def test_cwd_must_stay_inside_the_team_root(self):
        self.assertEqual(self.load('[[member]]\nrole = "qa"\ncwd = "web"\n')["member"][0]["cwd"], "web")
        self.assertRejected('[[member]]\nrole = "qa"\ncwd = "../elsewhere"\n', "cwd")
        self.assertRejected('[[member]]\nrole = "qa"\ncwd = "/etc"\n', "cwd")

    def test_args_cannot_override_horchestras_claude_flags(self):
        for flag in ('"--agent"', '"--settings=x.json"', '"-r"', '"--continue"', '"--name=x"',
                     '"--append-system-prompt-file"', '"--system-prompt-snapshot"'):
            self.assertRejected(f'[[member]]\nrole = "qa"\nargs = [{flag}]\n', "set by Horchestra")
        self.assertRejected('[orchestrator]\nargs = ["--resume", "abc"]\n', "orchestrator.args")
        # Fine for Claude, and other agents' flags mean other things.
        self.load('[[member]]\nrole = "qa"\nargs = ["--model", "opus"]\n')
        self.load('[[member]]\nrole = "qa"\nkind = "codex"\nargs = ["-c", "x=1"]\n')


class ClaudeConfigDirTest(unittest.TestCase):
    def test_honours_claude_config_dir(self):
        with tempfile.TemporaryDirectory() as tmp:
            os.makedirs(os.path.join(tmp, "projects", "-p"))
            open(os.path.join(tmp, "projects", "-p", "abc.jsonl"), "w").close()
            with mock.patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": tmp}):
                self.assertEqual(team.agent_file(), os.path.join(tmp, "agents", "orchestrator.md"))
                self.assertTrue(team.session_on_disk("claude", "abc"))
                self.assertFalse(team.session_on_disk("claude", "missing"))


class StateDirTestCase(unittest.TestCase):
    """Keeps the restart registry in a temporary folder."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = os.path.realpath(tmp.name)
        state = os.path.join(self.tmp, "state")
        for name, value in (("STATE_DIR", state), ("REGISTRY", os.path.join(state, "teams.json")),
                            ("TERMINALS", os.path.join(state, "terminals.json"))):
            patcher = mock.patch.object(team, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)


class ResolveSpaceTest(StateDirTestCase):
    def resolve(self, cwd, **kw):
        FakeHerdr([]).patch(self)
        env = {"HERDR_WORKSPACE_ID": "w1", "HOME": os.path.join(self.tmp, "home")}
        os.makedirs(env["HOME"], exist_ok=True)
        with mock.patch.dict(os.environ, env), mock.patch("os.getcwd", return_value=cwd), \
                mock.patch.object(team, "caller_pane", lambda: None):
            for key in ("HERDR_PLUGIN_ID", "HORCHESTRA_TEAM_FILE", "AGENTMAP_TEAM_FILE"):
                os.environ.pop(key, None)
            return team.resolve_space(**kw)

    def project(self):
        path = os.path.join(self.tmp, "home", "shop")
        os.makedirs(path, exist_ok=True)
        return path

    def test_read_only_commands_never_create_or_register(self):
        project = self.project()
        space, _ = self.resolve(project, read_only=True)
        self.assertEqual(space.team["member"], [])
        self.assertFalse(os.path.exists(os.path.join(project, "team.toml")))
        self.assertEqual(team.registered(), [])
        with self.assertRaises(team.TeamError):
            self.resolve(project)  # status/sync need a team

    def test_up_creates_in_a_project_but_not_in_home(self):
        home = os.path.join(self.tmp, "home")
        os.makedirs(home, exist_ok=True)
        with self.assertRaises(team.TeamError) as ctx:
            self.resolve(home, create=True)
        self.assertIn("--file", str(ctx.exception))
        self.assertFalse(os.path.exists(os.path.join(home, "team.toml")))
        self.assertEqual(team.registered(), [])
        project = self.project()
        space, _ = self.resolve(project, create=True)
        self.assertEqual(space.team_file, os.path.join(project, "team.toml"))
        self.assertEqual(team.registered(), [space.team_file])

    def test_explicit_file_may_be_created_in_home(self):
        target = os.path.join(self.tmp, "home", "team.toml")
        os.makedirs(os.path.dirname(target), exist_ok=True)
        with self.assertRaises(team.TeamError):
            self.resolve(self.tmp, team_file=target)  # not a creating command
        space, _ = self.resolve(self.tmp, team_file=target, create=True)
        self.assertTrue(os.path.isfile(target))

    def test_dotfiles_repo_in_home_is_not_a_project(self):
        home = os.path.join(self.tmp, "home")
        os.makedirs(os.path.join(home, ".git"), exist_ok=True)
        project = self.project()
        with mock.patch.dict(os.environ, {"HOME": home}):
            self.assertEqual(team.project_root(project), project)


class RegistryTest(StateDirTestCase):
    def test_restore_forgets_deleted_team_files(self):
        kept = os.path.join(self.tmp, "team.toml")
        team.save(kept, team.new_team())
        team.register(kept)
        team.register(os.path.join(self.tmp, "gone", "team.toml"))
        FakeHerdr([]).patch(self)
        with mock.patch.object(team, "RESTORE_SECONDS", 0):
            team.restore(log=lambda _m: None)
        self.assertEqual(team.registered(), [kept])

    def test_forget_removes_only_this_team(self):
        one, two = os.path.join(self.tmp, "a.toml"), os.path.join(self.tmp, "b.toml")
        team.register(one)
        team.register(two)
        with mock.patch("builtins.print"):
            team.cmd_forget(argparse.Namespace(file=one))
            team.cmd_forget(argparse.Namespace(file=one))  # already gone: no error
        self.assertEqual(team.registered(), [two])


class ForgetSticksTest(ResolveSpaceTest):
    def test_status_and_message_do_not_re_register(self):
        project = self.project()
        space, _ = self.resolve(project, create=True)  # up/init registers
        with mock.patch("builtins.print"):
            team.cmd_forget(argparse.Namespace(file=space.team_file))
        self.assertEqual(team.registered(), [])
        self.resolve(project)  # what status/sync/message/hire/fire/respawn call
        self.assertEqual(team.registered(), [])


class HireTest(StateDirTestCase):
    def hire(self, fake, sync):
        fake.patch(self)
        path = os.path.join(self.tmp, "team.toml")
        team.save(path, make_team([]))
        space = team.Space("w1", path, team.load(path))
        args = argparse.Namespace(file=None, role="qa", task="test it", kind=None, reports_to=None,
                                  cwd=None, arg=None, profile=None, uses_skill=None,
                                  deny_skill=None, only_skill=None)
        with mock.patch.object(team, "resolve_space", lambda *_a, **_k: (space, {})), \
                mock.patch.object(team, "sync", sync), mock.patch.object(team, "show_maps"), \
                mock.patch("builtins.print"):
            with self.assertRaises(team.TeamError) as ctx:
                team.cmd_hire(args)
        return str(ctx.exception), team.load(path)

    def test_rolls_back_when_nothing_was_created(self):
        def sync(*_a, **_k):
            raise team.TeamError("tab create failed")
        _, data = self.hire(FakeHerdr([]), sync)
        self.assertEqual(data["member"], [])

    def test_keeps_the_member_once_its_agent_started(self):
        fake = FakeHerdr([])

        def sync(*_a, **_k):
            fake.panes.append(pane("w1:p4", label="team:qa"))
            raise team.TeamError("qa is still not ready")
        message, data = self.hire(fake, sync)
        self.assertEqual([m["role"] for m in data["member"]], ["qa"])
        self.assertIn("w1:p4", message)
        self.assertIn("horchestra-team sync", message)

    def test_users_own_agent_file_is_refused_before_touching_team_toml(self):
        FakeHerdr([]).patch(self)
        path = os.path.join(self.tmp, "team.toml")
        team.save(path, make_team([]))
        agents = os.path.join(self.tmp, ".claude", "agents")
        os.makedirs(agents)
        with open(os.path.join(agents, "horchestra-qa.md"), "w") as fh:
            fh.write("my own agent\n")
        space = team.Space("w1", path, team.load(path))
        args = argparse.Namespace(file=None, role="qa", task="t", kind=None, reports_to=None, cwd=None,
                                  arg=None, profile=None, uses_skill=None, deny_skill=None, only_skill=None)
        with mock.patch.object(team, "resolve_space", lambda *_a, **_k: (space, {})), \
                mock.patch.object(team, "sync") as sync:
            with self.assertRaises(team.TeamError) as ctx:
                team.cmd_hire(args)
        self.assertIn("horchestra-qa.md", str(ctx.exception))
        sync.assert_not_called()
        self.assertEqual(team.load(path)["member"], [])
        with open(os.path.join(agents, "horchestra-qa.md")) as fh:
            self.assertEqual(fh.read(), "my own agent\n")

    def test_member_args_reports_agent_file_clash_as_team_error(self):
        FakeHerdr([]).patch(self)
        path = os.path.join(self.tmp, "team.toml")
        space = team.Space("w1", path, make_team([{"role": "qa"}]))
        member = space.team["member"][0]
        with mock.patch.object(team.roles, "claude_args", side_effect=team.roles.RoleError("clash")):
            with self.assertRaises(team.TeamError):
                team.member_args(space, member, None)

    def test_rejects_reserved_flags_before_touching_team_toml(self):
        FakeHerdr([]).patch(self)
        path = os.path.join(self.tmp, "team.toml")
        team.save(path, make_team([]))
        space = team.Space("w1", path, team.load(path))
        args = argparse.Namespace(file=None, role="qa", task="t", kind=None, reports_to=None, cwd=None,
                                  arg=["--settings=x.json"], profile=None, uses_skill=None,
                                  deny_skill=None, only_skill=None)
        with mock.patch.object(team, "resolve_space", lambda *_a, **_k: (space, {})), \
                mock.patch.object(team, "sync") as sync:
            with self.assertRaises(team.TeamError):
                team.cmd_hire(args)
        sync.assert_not_called()
        self.assertEqual(team.load(path)["member"], [])


class ReopenTest(unittest.TestCase):
    def test_finds_leftover_team_panes_anywhere(self):
        entries = [(team.ORCHESTRATOR, {"session": "s-orch"}), ("qa", {})]
        panes = [
            pane("w3:p1", ws="w3", agent=None, session="s-orch"),  # its session, agent exited
            pane("w4:p2", ws="w4", agent=None, label="team:qa", cwd="/work/shop/web"),  # dead qa shell
            pane("w5:p1", ws="w5", label="team:qa", cwd="/work/other"),  # another project's qa
            pane("w5:p2", ws="w5", label="team:qa"),  # no cwd known
        ]
        found = [p["pane_id"] for p in team.team_panes_anywhere("/work/shop", entries, panes)]
        self.assertEqual(found, ["w3:p1", "w4:p2"])

    def test_refuses_to_open_a_second_copy(self):
        fake = FakeHerdr([pane("w4:p2", ws="w4", agent=None, label="team:qa", cwd="/work/shop")]).patch(self)
        with mock.patch.object(team, "load", lambda _p: make_team([{"role": "qa"}])), \
                mock.patch.object(team, "register"):
            with self.assertRaises(team.TeamError) as ctx:
                team.cmd_reopen(argparse.Namespace(file="/work/shop/team.toml", label=None))
        self.assertIn("w4:p2", str(ctx.exception))
        self.assertFalse(any(c[:2] == ("workspace", "create") for c in fake.calls))


class ReapplyTest(unittest.TestCase):
    def space(self, terminal):
        space = team.Space.__new__(team.Space)
        space.team = {"default_kind": "claude", "orchestrator": {}, "member": []}
        space.pane = lambda role: {"terminal_id": terminal}
        return space

    def test_only_restarted_profile_members_are_reapplied(self):
        st = {"terminals": {"slides": "term_old"}}
        member = {"role": "slides", "kind": "claude", "profile_applied": True}
        self.assertTrue(team.needs_reapply(self.space("term_new"), member, st))
        # Live handoff keeps terminals: nothing to re-apply.
        self.assertFalse(team.needs_reapply(self.space("term_old"), member, st))
        # Adopted and never respawned, or not Claude: leave alone.
        self.assertFalse(team.needs_reapply(self.space("term_new"), {"role": "slides"}, st))
        codex = dict(member, kind="codex")
        self.assertFalse(team.needs_reapply(self.space("term_new"), codex, st))


def make_team(members, orch_session=None):
    data = team.new_team()
    if orch_session:
        data["orchestrator"]["session"] = orch_session
    data["member"] = members
    return data


class IdentityTest(unittest.TestCase):
    def space(self, panes, data):
        FakeHerdr(panes).patch(self)
        return team.Space("w1", "/projects/Web Shop/team.toml", data)

    def test_recorded_session_is_proof(self):
        space = self.space([pane("w1:p2", label="team:qa", session="s-qa")],
                           make_team([{"role": "qa", "session": "s-qa"}]))
        self.assertEqual(space.verified_pane("qa")[0]["pane_id"], "w1:p2")

    def test_label_is_not_proof_when_a_session_is_recorded(self):
        # Someone else's agent sits in a pane that merely carries the label.
        space = self.space([pane("w1:p2", label="team:qa", session="other", role="qa")],
                           make_team([{"role": "qa", "session": "s-qa"}]))
        self.assertEqual(space.pane("qa")["pane_id"], "w1:p2")  # still shown/synced
        found, why = space.verified_pane("qa")
        self.assertIsNone(found)
        self.assertIn("recorded session", why)

    def test_label_and_role_token_before_a_session_is_known(self):
        data = make_team([{"role": "qa"}])
        self.assertEqual(self.space([pane("w1:p2", label="team:qa", role="qa")], data)
                         .verified_pane("qa")[0]["pane_id"], "w1:p2")
        self.assertIsNone(self.space([pane("w1:p2", label="team:qa")], data).verified_pane("qa")[0])

    def test_two_panes_with_one_label_are_ambiguous(self):
        space = self.space([pane("w1:p2", label="team:qa", role="qa"),
                            pane("w1:p3", label="team:qa", role="qa")], make_team([{"role": "qa"}]))
        found, why = space.verified_pane("qa")
        self.assertIsNone(found)
        self.assertIn("ambiguous", why)
        self.assertIn("w1:p3", why)


class SyncIdentityTest(StateDirTestCase):
    """sync must not turn a label match into a recorded (trusted) session."""

    def sync(self, panes, data):
        fake = FakeHerdr(panes).patch(self)
        path = os.path.join(self.tmp, "team.toml")
        team.save(path, data)
        space = team.Space("w1", path, team.load(path))
        team.sync(space, spawn=False, log=lambda _m: None)
        return fake, space, team.load(path)

    def test_foreign_labelled_pane_is_not_laundered(self):
        # qa's session is gone; another team's qa shares the space and label.
        panes = [pane("w1:p9", label="team:qa", session="s-other", role="qa")]
        fake, space, data = self.sync(panes, make_team([{"role": "qa", "session": "s-qa"}]))
        self.assertEqual(data["member"][0]["session"], "s-qa")
        self.assertIsNone(space.verified_pane("qa")[0])
        self.assertNotIn(("set_tokens", "w1:p9"), fake.calls)
        self.assertFalse(any(c[:2] == ("agent", "rename") for c in fake.calls))

    def test_new_session_in_the_roles_own_terminal_is_followed(self):
        # e.g. /clear started a new conversation in qa's recorded terminal.
        path = os.path.join(self.tmp, "team.toml")
        team.remember_terminals(path, {"qa": "term_w1:p9"})
        panes = [pane("w1:p9", label="team:qa", session="s-new", role="qa")]
        _, space, data = self.sync(panes, make_team([{"role": "qa", "session": "s-qa"}]))
        self.assertEqual(data["member"][0]["session"], "s-new")
        self.assertEqual(space.verified_pane("qa")[0]["pane_id"], "w1:p9")

    def test_first_session_is_recorded(self):
        panes = [pane("w1:p9", label="team:qa", session="s-qa", role="qa")]
        _, _, data = self.sync(panes, make_team([{"role": "qa"}]))
        self.assertEqual(data["member"][0]["session"], "s-qa")

    def test_started_agent_replaces_the_stale_session(self):
        FakeHerdr([]).patch(self)
        path = os.path.join(self.tmp, "team.toml")
        space = team.Space("w1", path, make_team([{"role": "qa", "session": "s-old"}]))
        team.record_started(space, "qa", pane("w1:p3", session="s-new"))
        self.assertEqual(team.load(path)["member"][0]["session"], "s-new")
        team.record_started(space, "qa", pane("w1:p3"))  # session not reported yet
        self.assertNotIn("session", team.load(path)["member"][0])


class FireTest(unittest.TestCase):
    def fire(self, role, panes, data):
        fake = FakeHerdr(panes).patch(self)
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        path = os.path.join(tmp.name, "team.toml")
        team.save(path, data)
        space = team.Space("w1", path, team.load(path))
        with mock.patch.object(team, "resolve_space", lambda *_a, **_k: (space, {})), \
                mock.patch("builtins.print"):
            team.cmd_fire(argparse.Namespace(file=None, role=role))
        return fake, team.load(path)

    def test_orchestrator_cannot_be_fired(self):
        with mock.patch.object(team, "resolve_space") as resolve:
            with self.assertRaises(team.TeamError):
                team.cmd_fire(argparse.Namespace(file=None, role="Orchestrator"))
        resolve.assert_not_called()

    def test_non_member_with_a_matching_label_is_rejected(self):
        with self.assertRaises(team.TeamError):
            self.fire("ghost", [pane("w1:p9", label="team:ghost", role="ghost")], make_team([]))

    def test_closes_the_verified_pane_case_insensitively(self):
        fake, data = self.fire("QA", [pane("w1:p2", label="team:qa", session="s-qa")],
                               make_team([{"role": "qa", "session": "s-qa"}, {"role": "fe"}]))
        self.assertEqual(fake.closed(), ["w1:p2"])
        self.assertEqual([m["role"] for m in data["member"]], ["fe"])

    def test_label_only_pane_is_left_open(self):
        fake, data = self.fire("qa", [pane("w1:p2", label="team:qa", session="someone-else")],
                               make_team([{"role": "qa", "session": "s-qa"}]))
        self.assertEqual(fake.closed(), [])
        self.assertEqual(data["member"], [])


class RespawnTest(unittest.TestCase):
    def test_keeps_the_plain_name_it_held_before_quitting(self):
        qa = pane("w1:p2", label="team:qa", session="s-qa")
        fake = FakeHerdr([qa])
        real_call = fake.call
        # Herdr only reports the name holder while its agent runs.
        fake.call = lambda *a, **k: (({"agent": {"pane_id": "w1:p2"}} if qa.get("agent") else {})
                                     if a[:3] == ("agent", "get", "api-qa") else real_call(*a, **k))
        fake.patch(self)
        space = team.Space("w1", "/work/api/team.toml", make_team([{"role": "qa", "session": "s-qa"}]))
        started = []
        with mock.patch.object(team, "quit_agent", lambda _p: qa.update(agent=None)), \
                mock.patch.object(team, "start_agent", lambda name, *a: started.append(name)), \
                mock.patch.object(team, "load_profile", lambda *a: None), \
                mock.patch.object(team, "member_args", lambda *a: []), \
                mock.patch.object(team, "save"), \
                mock.patch.object(team, "wait_for_session", lambda *a: None):
            team.respawn(space, "qa", log=lambda _m: None)
        self.assertEqual(started, ["api-qa"])

    def test_never_interrupts_an_unverified_pane(self):
        fake = FakeHerdr([pane("w1:p2", label="team:qa", session="someone-else")]).patch(self)
        space = team.Space("w1", "/projects/Web Shop/team.toml",
                           make_team([{"role": "qa", "session": "s-qa"}]))
        with self.assertRaises(team.TeamError):
            team.respawn(space, "qa", log=lambda _m: None)
        self.assertEqual(fake.sent_keys(), [])


class RestoreTest(unittest.TestCase):
    def run_restore(self, teams, fake):
        fake.patch(self)
        finished, logs = [], []
        patches = [
            mock.patch.object(team, "registered", lambda: list(teams)),
            mock.patch.object(team.os.path, "isfile", lambda p: p in teams or os.path.exists(p)),
            mock.patch.object(team, "load", lambda p: teams[p]),
            mock.patch.object(team, "known_terminals", lambda _p: {}),
            mock.patch.object(team, "sync", lambda *a, **k: None),
            mock.patch.object(team, "finish_restore",
                              lambda space, live, st, log: finished.append(space.team_file)),
            mock.patch.object(team.time, "sleep", lambda _s: None),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        team.restore(log=logs.append)
        return finished, logs

    def test_one_broken_team_does_not_stop_the_others(self):
        fake = FakeHerdr([pane("w1:p1", session="a-orch", ws="w1"),
                          pane("w2:p1", session="b-orch", ws="w2")], broken={"w1"})
        finished, logs = self.run_restore({
            "/a/team.toml": make_team([], orch_session="a-orch"),
            "/b/team.toml": make_team([], orch_session="b-orch"),
        }, fake)
        self.assertEqual(finished, ["/b/team.toml"])
        self.assertTrue(any("/a/team.toml: giving up" in m for m in logs))
        self.assertEqual(sum("/a/team.toml: will retry" in m for m in logs),
                         team.RESTORE_MAX_FAILURES - 1)

    def test_malformed_team_is_isolated(self):
        fake = FakeHerdr([pane("w1:p1", session="b-orch")])
        bad = make_team([], orch_session="a-orch")
        bad["member"] = [{"session": "x"}]  # no role: KeyError deep inside
        fake.panes.append(pane("w1:p5", session="x"))
        finished, _ = self.run_restore({"/a/team.toml": bad,
                                        "/b/team.toml": make_team([], orch_session="b-orch")}, fake)
        self.assertEqual(finished, ["/b/team.toml"])

    def test_copied_project_is_restored_once(self):
        # The copy sorts first ('-' < '/'); the agents' folder decides, not order.
        fake = FakeHerdr([pane("w1:p1", session="orch", label="team:orchestrator", cwd="/p/shop")])
        finished, logs = self.run_restore({
            "/p/shop-copy/team.toml": make_team([], orch_session="orch"),
            "/p/shop/team.toml": make_team([], orch_session="orch"),
        }, fake)
        self.assertEqual(finished, ["/p/shop/team.toml"])
        self.assertTrue(any(m.startswith("/p/shop-copy/team.toml: skipped") for m in logs))

    def test_undecidable_copy_restores_neither(self):
        fake = FakeHerdr([pane("w1:p1", session="orch", label="team:orchestrator", cwd="/elsewhere")])
        finished, logs = self.run_restore({
            "/p/shop-copy/team.toml": make_team([], orch_session="orch"),
            "/p/shop/team.toml": make_team([], orch_session="orch"),
        }, fake)
        self.assertEqual(finished, [])
        self.assertEqual(sum(": skipped" in m for m in logs), 2)

    def test_pane_owner_prefers_the_innermost_root(self):
        paths = ["/p/shop/team.toml", "/p/shop/copy/team.toml"]
        self.assertEqual(team.pane_owner({"cwd": "/p/shop/copy/src"}, paths), "/p/shop/copy/team.toml")
        self.assertEqual(team.pane_owner({"foreground_cwd": "/p/shop/web", "cwd": "/x"}, paths),
                         "/p/shop/team.toml")
        self.assertIsNone(team.pane_owner({}, paths))


class RestoreLoadTest(StateDirTestCase):
    """restore() with the real load(): one unreadable team.toml is skipped."""

    def test_non_utf8_team_file_does_not_stop_the_hook(self):
        bad = os.path.join(self.tmp, "a", "team.toml")
        good = os.path.join(self.tmp, "b", "team.toml")
        os.makedirs(os.path.dirname(bad))
        os.makedirs(os.path.dirname(good))
        with open(bad, "wb") as fh:
            fh.write(b"brief = \"\xff\"\n")
        team.save(good, make_team([], orch_session="b-orch"))
        team.register(bad)
        team.register(good)
        FakeHerdr([pane("w1:p1", session="b-orch")]).patch(self)
        finished, logs = [], []
        with mock.patch.object(team, "sync", lambda *a, **k: None), \
                mock.patch.object(team, "finish_restore",
                                  lambda space, live, st, log: finished.append(space.team_file)), \
                mock.patch.object(team.time, "sleep", lambda _s: None):
            team.restore(log=logs.append)
        self.assertEqual(finished, [good])
        self.assertTrue(any(m.startswith(f"skip {bad}") for m in logs))

    def test_registry_write_failure_does_not_stop_the_hook(self):
        good = os.path.join(self.tmp, "b", "team.toml")
        os.makedirs(os.path.dirname(good))
        team.save(good, make_team([], orch_session="b-orch"))
        team.register(good)
        team.register(os.path.join(self.tmp, "gone", "team.toml"))
        FakeHerdr([pane("w1:p1", session="b-orch")]).patch(self)
        finished, logs = [], []

        def broken(_files):
            raise OSError("read-only file system")
        with mock.patch.object(team, "unregister", broken), \
                mock.patch.object(team, "sync", lambda *a, **k: None), \
                mock.patch.object(team, "finish_restore",
                                  lambda space, live, st, log: finished.append(space.team_file)), \
                mock.patch.object(team.time, "sleep", lambda _s: None):
            team.restore(log=logs.append)
        self.assertEqual(finished, [good])
        self.assertTrue(any("could not update the team registry" in m for m in logs))


class FinishRestoreTest(unittest.TestCase):
    def test_missing_orchestrator_pane_after_respawn_is_not_indexed(self):
        FakeHerdr([]).patch(self)
        space = team.Space("w1", "/projects/Web Shop/team.toml", make_team([], orch_session="gone"))
        logs, delivered = [], []
        with mock.patch.object(team, "respawn", lambda *a, **k: None), \
                mock.patch.object(team, "deliver", lambda *a, **k: delivered.append(a)), \
                mock.patch.object(team, "replace_dead_maps", lambda *a, **k: None), \
                mock.patch.object(team, "orchestrator_protocol", lambda *a: ""):
            team.finish_restore(space, {team.ORCHESTRATOR}, {"orch_resumed": True}, log=logs.append)
        self.assertEqual(delivered, [])
        self.assertTrue(any("not re-briefing" in m for m in logs))

    def test_fresh_orchestrator_that_vanished_is_not_indexed(self):
        FakeHerdr([]).patch(self)
        space = team.Space("w1", "/projects/Web Shop/team.toml", make_team([]))
        logs = []
        with mock.patch.object(team, "start_orchestrator", lambda *a: None), \
                mock.patch.object(team, "sync", lambda *a, **k: None), \
                mock.patch.object(team, "replace_dead_maps", lambda *a, **k: None):
            team.finish_restore(space, set(), {}, log=logs.append)
        self.assertTrue(any("disappeared" in m for m in logs))


class MapPaneTest(unittest.TestCase):
    def test_only_the_view_token_marks_a_live_map(self):
        self.assertTrue(toggle.is_map(pane("p", agent=None, label="x", tokens={hc.TOKEN_VIEW: "1"})))
        self.assertFalse(toggle.is_map(pane("p", agent=None, label="horchestra-map")))
        self.assertFalse(toggle.is_map(pane("p", agent=None, label="Agent map")))
        self.assertFalse(toggle.is_overview(pane("p", agent=None, label="horchestra-overview")))
        self.assertTrue(toggle.is_overview(pane("p", agent=None, tokens={hc.TOKEN_VIEW: "all"})))

    def test_dead_views_need_an_idle_shell(self):
        panes = [
            pane("w1:p1", agent=None, label="horchestra-map"),  # restored, idle zsh
            pane("w1:p2", agent=None, label="horchestra-map"),  # the human runs vim there
            pane("w1:p3", agent="claude", label="horchestra-map"),  # an agent took it over
            pane("w1:p4", agent=None, label="horchestra-map"),  # process info unavailable
            pane("w1:p5", agent=None, label="Agent map"),  # legacy title: never closed
            pane("w1:p6", agent=None, label="horchestra-map", tokens={hc.TOKEN_VIEW: "1"}),  # live
        ]
        FakeHerdr(panes, processes={"w1:p1": ["-zsh"], "w1:p2": ["zsh", "vim"],
                                    "w1:p3": ["zsh"], "w1:p5": ["zsh"], "w1:p6": ["python3"]}).patch(self)
        self.assertEqual(toggle.dead_views(panes, toggle.MAP_LABEL), ["w1:p1"])

    def test_replace_dead_maps_closes_only_verified_shells(self):
        panes = [
            pane("w1:p1", agent=None, label="horchestra-overview"),
            pane("w1:p2", agent=None, label="horchestra-overview"),
            pane("w1:p3", agent=None, label="Agent map"),
            pane("w1:p4", agent=None, label="horchestra-map"),
        ]
        fake = FakeHerdr(panes, processes={"w1:p1": ["bash"], "w1:p2": ["htop"],
                                           "w1:p3": ["zsh"], "w1:p4": ["node"]}).patch(self)
        space = team.Space("w1", "/projects/Web Shop/team.toml", make_team([]))
        with mock.patch.object(team, "show_maps") as show:
            team.replace_dead_maps(space)
        self.assertEqual(fake.closed(), ["w1:p1"])
        show.assert_not_called()


class TabNameTest(unittest.TestCase):
    def test_dead_role_pane_does_not_name_another_agents_tab(self):
        panes = [
            pane("w1:p3", label="team:qa", agent=None, tab="w1:t2"),  # qa did not resume
            pane("w1:p7", agent="claude", tab="w1:t2"),  # the human's own Claude
        ]
        fake = FakeHerdr(panes, tabs=[{"tab_id": "w1:t2", "label": "my-notes"}]).patch(self)
        space = team.Space("w1", "/projects/Web Shop/team.toml", make_team([{"role": "qa"}]))
        team.name_tabs(space)
        self.assertEqual([c for c in fake.calls if c[:2] == ("tab", "rename")], [])

    def test_tab_renamed_only_when_agent_is_alone(self):
        calls = []
        orig_quiet = team.hc.call_quiet
        team.hc.call_quiet = lambda *a, **k: (calls.append(a) or
            ({"tabs": [{"tab_id": "w1:t1", "label": "1"}, {"tab_id": "w1:t2", "label": "misc"}]}
             if a[:2] == ("tab", "list") else {}))
        try:
            space = team.Space.__new__(team.Space)
            space.workspace_id = "w1"
            space.team = {"orchestrator": {}, "member": [{"role": "slides"}, {"role": "qa"}]}
            panes = {
                "orchestrator": {"pane_id": "w1:p1", "tab_id": "w1:t1", "agent": "claude"},
                "slides": {"pane_id": "w1:p2", "tab_id": "w1:t2", "agent": "claude"},
                "qa": {"pane_id": "w1:p3", "tab_id": "w1:t2", "agent": "claude"},
            }
            space.panes = list(panes.values()) + [
                {"pane_id": "w1:p4", "tab_id": "w1:t1", "agent": None},  # plain shell
                {"pane_id": "w1:p5", "tab_id": "w1:t1", "agent": None, "tokens": {"agentmap_view": "1"}},
            ]
            space.pane = panes.get
            team.name_tabs(space)
        finally:
            team.hc.call_quiet = orig_quiet
        renames = [c for c in calls if c[:2] == ("tab", "rename")]
        self.assertEqual(renames, [("tab", "rename", "w1:t1", "orchestrator")])

    def test_name_tabs_can_be_disabled(self):
        space = team.Space.__new__(team.Space)
        space.team = {"name_tabs": False}
        self.assertIsNone(team.name_tabs(space))


if __name__ == "__main__":
    unittest.main()
