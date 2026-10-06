"""End-to-end journeys: real `team.main([...])` commands against a stateful fake Herdr.

Each test drives the plugin the way a human or an orchestrator would and
checks only observable results: the fake Herdr's state (panes, tabs, agent
names, tokens, launches, prompts), files on disk, and team.toml.
"""

import contextlib
import io
import json
import os
import re
import shutil
import tempfile
import unittest
from unittest import mock

from tests import helpers  # noqa: F401
from tests.fakeherdr import FakeClock, FakeHerdr
import herdr_client as hc
import team

HASHED_NAME = r"^{project}-[0-9a-f]{{4}}-{role}$"


def arg_after(args, flag):
    """The value following `flag` in an argv list (None when absent)."""
    return args[args.index(flag) + 1] if flag in args else None


class JourneyCase(unittest.TestCase):
    """A temp home, Claude config, state dir and git project, plus a fake Herdr
    with one space ("proj") whose first pane runs the orchestrator's Claude."""

    def setUp(self):
        self.tmp = os.path.realpath(tempfile.mkdtemp(prefix="horchestra-journey-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.home = self.mkdir("home")
        self.state = self.mkdir("state")
        self.claude = self.mkdir("claude")
        self.project = self.make_project("proj")

        env = mock.patch.dict(os.environ, {
            "HOME": self.home,
            "HORCHESTRA_STATE_DIR": self.state,
            "CLAUDE_CONFIG_DIR": self.claude,
            "HORCHESTRA_DETACHED": "1",  # never fork background helpers
        })
        env.start()
        self.addCleanup(env.stop)
        for key in ("HERDR_PLUGIN_ID", "HERDR_PLUGIN_CONTEXT_JSON", "HORCHESTRA_TEAM_FILE",
                    "AGENTMAP_TEAM_FILE", "AGENTMAP_STATE_DIR", "HERDR_SOCKET_PATH",
                    "HERDR_PANE_ID", "HERDR_WORKSPACE_ID", "HORCHESTRA_WIDTH", "AGENTMAP_WIDTH"):
            os.environ.pop(key, None)
        for name, value in (("STATE_DIR", self.state),
                            ("REGISTRY", os.path.join(self.state, "teams.json")),
                            ("TERMINALS", os.path.join(self.state, "terminals.json"))):
            patcher = mock.patch.object(team, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.clock = FakeClock()
        patcher = mock.patch.object(team, "time", self.clock)
        patcher.start()
        self.addCleanup(patcher.stop)

        # `install.py install` would have put the orchestrator session agent here.
        os.makedirs(os.path.join(self.claude, "agents"))
        shutil.copy(os.path.join(team.PLUGIN_ROOT, "agents", "orchestrator.md"),
                    os.path.join(self.claude, "agents", "orchestrator.md"))

        self.herdr = FakeHerdr(claude_config_dir=self.claude).install(self)
        self.ws = self.herdr.add_workspace("proj", self.project)
        self.orch = self.herdr.add_agent(self.herdr.root_pane(self.ws), session="sess-orch")

        cwd = os.getcwd()
        self.addCleanup(os.chdir, cwd)
        os.chdir(self.project)

    # ---- helpers ---------------------------------------------------------

    def mkdir(self, *parts):
        path = os.path.join(self.tmp, *parts)
        os.makedirs(path, exist_ok=True)
        return path

    def make_project(self, *parts):
        path = self.mkdir(*parts)
        os.makedirs(os.path.join(path, ".git"), exist_ok=True)
        return path

    def write(self, path, text):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as fh:
            fh.write(text)

    def run_team(self, *argv, pane=None, cwd=None):
        """Run `horchestra-team <argv>` as if typed in `pane` (default: orchestrator)."""
        pane = pane or self.orch
        info = self.herdr.pane(pane)
        os.environ["HERDR_PANE_ID"] = pane
        os.environ["HERDR_WORKSPACE_ID"] = info["workspace_id"] if info else self.ws
        if cwd:
            os.chdir(cwd)
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = team.main(list(argv))
        return code, out.getvalue(), err.getvalue()

    def ok(self, *argv, **kw):
        code, out, err = self.run_team(*argv, **kw)
        self.assertEqual(code, 0, f"`{' '.join(argv)}` failed:\n{out}\n{err}")
        return out

    def restore(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(team.main(["restore"]), 0)
        return out.getvalue()

    @property
    def team_file(self):
        return os.path.join(self.project, "team.toml")

    def team_data(self, path=None):
        return team.load(path or self.team_file)

    def member(self, role, path=None):
        return next(m for m in self.team_data(path)["member"] if m["role"] == role)

    def registry(self):
        try:
            with open(os.path.join(self.state, "teams.json")) as fh:
                return json.load(fh)
        except OSError:
            return []

    def pane_of(self, role, workspace=None):
        """The live pane labelled team:<role> in a workspace."""
        found = [p for p in self.herdr.panes_in(workspace or self.ws) if p["label"] == "team:" + role]
        self.assertEqual(len(found), 1, f"expected one pane for {role}: {found}")
        return found[0]

    def tab_label(self, tab_id):
        return self.herdr.tabs[tab_id]["label"]

    def make_profile(self, role, role_md="Build the UI with care."):
        folder = os.path.join(self.project, ".orchestra", "roles", role)
        self.write(os.path.join(folder, "ROLE.md"), role_md)
        self.write(os.path.join(folder, ".claude", "skills", f"{role}-skill", "SKILL.md"),
                   f"---\nname: {role}-skill\n---\nRole skill.\n")
        return folder

    def make_user_skill(self, name):
        self.write(os.path.join(self.claude, "skills", name, "SKILL.md"), f"---\nname: {name}\n---\n")

    def init_team(self):
        self.ok("init")

    def hire(self, role, task, *extra, **kw):
        self.ok("hire", role, "--task", task, *extra, **kw)
        return self.pane_of(role, kw.get("workspace"))["pane_id"]


class SetupJourneyTest(JourneyCase):
    def test_init_scan_adopt_status_builds_a_tagged_team_from_running_agents(self):
        stray_tab = self.herdr.add_tab(self.ws, "2")
        stray = self.herdr.add_agent(self.herdr.root_pane(stray_tab), session="sess-stray")

        out = self.ok("init")
        self.assertIn("you are the orchestrator", out)
        self.assertIn(self.team_file, self.registry())
        data = self.team_data()
        self.assertEqual(data["orchestrator"]["session"], "sess-orch")
        orch = self.herdr.pane(self.orch)
        self.assertEqual(orch["label"], "team:orchestrator")
        self.assertEqual(orch["tokens"][hc.TOKEN_ROLE], "orchestrator")
        self.assertRegex(self.herdr.agent_name_of(self.orch), HASHED_NAME.format(project="proj", role="orchestrator"))
        self.assertEqual(self.tab_label(orch["tab_id"]), "orchestrator")
        self.assertEqual(len(self.herdr.map_panes(self.ws)), 1, "init opens the agent map")

        out = self.ok("scan")
        self.assertIn(stray, out)
        self.assertNotIn(self.orch + " ", out)

        self.ok("adopt", stray, "--role", "research", "--task", "dig into the logs")
        member = self.member("research")
        self.assertEqual(member["session"], "sess-stray")
        self.assertTrue(member["adopted"])
        p = self.herdr.pane(stray)
        self.assertEqual(p["label"], "team:research")
        self.assertEqual(p["tokens"][hc.TOKEN_ROLE], "research")
        self.assertEqual(p["tokens"][hc.TOKEN_TASK], "dig into the logs")
        self.assertEqual(p["tokens"][hc.TOKEN_PROFILE], "no profile")
        self.assertEqual(p["tokens"][hc.TOKEN_PARENT], orch["terminal_id"])
        self.assertEqual(self.tab_label(stray_tab), "research")
        self.assertRegex(self.herdr.agent_name_of(stray), HASHED_NAME.format(project="proj", role="research"))
        self.assertEqual({m["tab_id"] for m in self.herdr.map_panes(self.ws)},
                         {orch["tab_id"], stray_tab}, "every team tab shows a map")
        self.assertEqual(self.herdr.launches, [], "adopting never restarts an agent")

        self.assertIn("no unmanaged agents", self.ok("scan"))
        status = self.ok("status")
        rows = {line.split()[0]: line for line in status.splitlines()[2:] if line.strip() and not line.startswith(" ")}
        self.assertIn("orchestrator", rows)
        self.assertIn(stray, rows["research"])
        self.assertIn("no profile", rows["research"])
        self.assertIn(self.herdr.agent_name_of(stray), rows["research"])


class HireJourneyTest(JourneyCase):
    def test_hire_starts_profile_member_in_own_tab_with_profile_flags_and_brief(self):
        folder = self.make_profile("fe")
        self.make_user_skill("frontend-design")
        self.make_user_skill("other-skill")
        self.init_team()

        fe = self.hire("fe", "build the login page", "--only-skill", "frontend-design")

        p = self.herdr.pane(fe)
        self.assertNotEqual(p["tab_id"], self.herdr.pane(self.orch)["tab_id"])
        self.assertEqual(self.tab_label(p["tab_id"]), "fe")
        self.assertEqual(p["agent"], "claude")
        self.assertEqual(p["tokens"][hc.TOKEN_ROLE], "fe")
        self.assertEqual(p["tokens"][hc.TOKEN_TASK], "build the login page")
        self.assertTrue(p["tokens"][hc.TOKEN_PROFILE].startswith("fe · 1 own skill"))
        self.assertRegex(self.herdr.agent_name_of(fe), HASHED_NAME.format(project="proj", role="fe"))

        (launch,) = self.herdr.launches_of(fe)
        args = launch["args"]
        self.assertEqual(launch["name"], self.herdr.agent_name_of(fe))
        self.assertEqual(arg_after(args, "--name"), "fe")
        self.assertEqual(arg_after(args, "--agent"), "horchestra-fe")
        self.assertEqual(arg_after(args, "--add-dir"), folder)
        with open(arg_after(args, "--settings")) as fh:
            settings = json.load(fh)
        self.assertIn("Skill(other-skill)", settings["permissions"]["deny"])
        self.assertNotIn("Skill(frontend-design)", settings["permissions"]["deny"])

        with open(os.path.join(self.project, ".claude", "agents", "horchestra-fe.md")) as fh:
            agent = fh.read()
        self.assertIn("You are the fe member", agent)
        self.assertIn(self.herdr.agent_name_of(self.orch), agent, "names the orchestrator agent")
        self.assertIn("horchestra-team report", agent)
        self.assertIn("horchestra-team message orchestrator", agent)
        self.assertIn("Build the UI with care.", agent)
        self.assertIn("Use ONLY these skills", agent)
        self.assertIn("`frontend-design`", agent)

        (brief,) = self.herdr.prompts_to(fe)
        self.assertIn("build the login page", brief)

        member = self.member("fe")
        self.assertEqual(member["session"], launch["session"])
        self.assertEqual(member["only_skills"], ["frontend-design"])
        self.assertTrue(member["profile_applied"])
        self.assertIn(p["tab_id"], {m["tab_id"] for m in self.herdr.map_panes(self.ws)})

    def test_hire_non_claude_member_gets_task_and_report_instructions_in_first_prompt(self):
        self.init_team()
        be = self.hire("be", "add the /login endpoint", "--kind", "codex")

        (launch,) = self.herdr.launches_of(be)
        self.assertEqual(launch["kind"], "codex")
        self.assertNotIn("--agent", launch["args"])
        (brief,) = self.herdr.prompts_to(be)
        self.assertIn("add the /login endpoint", brief)
        self.assertIn("horchestra-team report", brief)
        self.assertIn("horchestra-team message orchestrator", brief)
        self.assertEqual(self.member("be")["kind"], "codex")


class FireJourneyTest(JourneyCase):
    def test_fire_closes_only_the_members_verified_pane_and_refuses_orchestrator_or_strangers(self):
        self.init_team()
        fe = self.hire("fe", "frontend")
        be = self.hire("be", "backend")

        out = self.ok("fire", "fe")
        self.assertIn("fired fe", out)
        self.assertEqual(self.herdr.closed_panes(), [fe])
        self.assertEqual([m["role"] for m in self.team_data()["member"]], ["be"])
        self.assertIsNotNone(self.herdr.pane(be))
        self.assertIsNotNone(self.herdr.pane(self.orch))

        for role in ("orchestrator", "ghost"):
            code, _, err = self.run_team("fire", role)
            self.assertEqual(code, 1, role)
            self.assertIn("horchestra-team:", err)
        self.assertEqual(self.herdr.closed_panes(), [fe], "refused fires close nothing")
        self.assertEqual([m["role"] for m in self.team_data()["member"]], ["be"])

    def test_fire_leaves_a_label_only_lookalike_pane_open(self):
        self.init_team()
        fe = self.hire("fe", "frontend")
        # The human closes fe's pane; another agent's pane happens to carry its label.
        self.herdr._remove_pane(fe)
        tab = self.herdr.add_tab(self.ws, "misc")
        lookalike = self.herdr.add_agent(self.herdr.root_pane(tab), session="sess-someone-else")
        self.herdr.panes[lookalike]["label"] = "team:fe"

        out = self.ok("fire", "fe")
        self.assertIn("left pane", out)
        self.assertEqual(self.herdr.closed_panes(), [])
        self.assertIsNotNone(self.herdr.pane(lookalike))
        self.assertEqual(self.team_data()["member"], [])


class ReportMessageJourneyTest(JourneyCase):
    def test_report_sets_status_needs_you_notifies_and_message_reaches_the_orchestrator(self):
        self.init_team()
        fe = self.hire("fe", "frontend")

        self.ok("report", "tests 3/5 passing", pane=fe)
        tokens = self.herdr.pane(fe)["tokens"]
        self.assertEqual(tokens[hc.TOKEN_STATUS], "tests 3/5 passing")
        self.assertNotIn(hc.TOKEN_NEEDS, tokens)

        self.ok("report", "--needs-you", "Postgres or SQLite?", pane=fe)
        tokens = self.herdr.pane(fe)["tokens"]
        self.assertEqual(tokens[hc.TOKEN_STATUS], "Postgres or SQLite?")
        self.assertEqual(tokens[hc.TOKEN_NEEDS], "1")
        self.assertEqual(self.herdr.notifications,
                         [{"title": "fe needs you", "body": "Postgres or SQLite?", "sound": "request"}])
        self.assertIn("NEEDS YOU: Postgres or SQLite?", self.ok("status"))

        self.ok("report", "x" * 200, pane=fe)
        tokens = self.herdr.pane(fe)["tokens"]
        self.assertEqual(tokens[hc.TOKEN_STATUS], "x" * 80)
        self.assertNotIn(hc.TOKEN_NEEDS, tokens, "a plain report clears needs-you")

        self.ok("message", "orchestrator", "login", "page", "done", pane=fe)
        self.assertEqual(self.herdr.prompts_to(self.orch)[-1], "[horchestra] message from fe: login page done")
        self.ok("message", "fe", "please add tests")
        self.assertEqual(self.herdr.prompts_to(fe)[-1],
                         "[horchestra] message from orchestrator: please add tests")
        code, _, err = self.run_team("message", "fe", "talking to myself", pane=fe)
        self.assertEqual(code, 1)
        self.assertIn("that is you", err)

    def test_reports_and_messages_leave_a_signal_the_map_animates(self):
        self.init_team()
        fe = self.hire("fe", "frontend")

        def signal(pane):
            return hc.parse_signal(self.herdr.pane(pane)["tokens"].get(hc.TOKEN_SIGNAL))

        self.ok("report", "tests 3/5 passing", pane=fe)
        self.assertEqual(signal(fe)[:2], ("report", None))  # None: toward its parent
        self.ok("report", "--needs-you", "Postgres or SQLite?", pane=fe)
        self.assertEqual(signal(fe)[:2], ("needs", None))
        self.ok("message", "orchestrator", "login", "done", pane=fe)
        self.assertEqual(signal(fe)[:2], ("msg", self.orch))
        self.ok("message", "fe", "please add tests")
        self.assertEqual(signal(self.orch)[:2], ("msg", fe))
        self.ok("report", "--clear", pane=fe)
        self.assertEqual(signal(fe)[:2], ("msg", self.orch), "clearing the status sends nothing")


class RespawnJourneyTest(JourneyCase):
    def test_respawn_keeps_the_conversation_applies_profile_and_waits_for_idle(self):
        folder = self.make_profile("fe")
        self.init_team()
        fe = self.hire("fe", "frontend")
        name = self.herdr.agent_name_of(fe)
        session = self.member("fe")["session"]

        self.herdr.set_status(fe, "working")
        code, _, err = self.run_team("respawn", "fe")
        self.assertEqual(code, 1)
        self.assertIn("working", err)
        self.assertEqual(len(self.herdr.launches_of(fe)), 1, "a working member is not restarted")
        self.assertEqual(self.herdr.sent_keys, [])

        self.ok("respawn", "fe", "--force")
        launch = self.herdr.launches_of(fe)[-1]
        self.assertEqual(len(self.herdr.launches_of(fe)), 2)
        self.assertEqual(self.herdr.sent_keys, [(fe, ["ctrl+c", "ctrl+c"])])
        args = launch["args"]
        self.assertEqual(args[:4], ["--resume", session, "--system-prompt-snapshot", "off"])
        self.assertEqual(arg_after(args, "--agent"), "horchestra-fe")
        self.assertEqual(arg_after(args, "--name"), "fe")
        self.assertEqual(arg_after(args, "--add-dir"), folder)
        self.assertIn("--settings", args)
        p = self.herdr.pane(fe)
        self.assertEqual(p["agent_session"]["value"], session)
        self.assertEqual(self.herdr.agent_name_of(fe), name)
        self.assertEqual(self.member("fe")["session"], session)
        self.assertEqual(p["tokens"][hc.TOKEN_ROLE], "fe")


class RestartJourneyTest(JourneyCase):
    def build_team(self):
        self.make_profile("fe")
        self.init_team()
        fe = self.hire("fe", "frontend")
        be = self.hire("be", "backend")
        return fe, be

    def test_restart_restores_names_tags_profiles_maps_and_briefs_the_orchestrator(self):
        fe, be = self.build_team()
        names = {pid: self.herdr.agent_name_of(pid) for pid in (self.orch, fe, be)}
        sessions = {pid: self.herdr.pane(pid)["agent_session"]["value"] for pid in (self.orch, fe, be)}
        old_maps = [m["pane_id"] for m in self.herdr.map_panes(self.ws)]
        self.assertEqual(len(old_maps), 3)

        self.herdr.simulate_restart()
        self.assertTrue(all(self.herdr.agent_name_of(pid) is None for pid in names))
        self.assertEqual(self.herdr.map_panes(self.ws), [])

        log = self.restore()
        self.assertIn("restored", log)
        for pid, name in names.items():
            self.assertEqual(self.herdr.agent_name_of(pid), name, f"{pid} gets its name back")
            self.assertEqual(self.herdr.pane(pid)["agent_session"]["value"], sessions[pid])
        for role, pid in (("orchestrator", self.orch), ("fe", fe), ("be", be)):
            p = self.herdr.pane(pid)
            self.assertEqual(p["label"], "team:" + role)
            self.assertEqual(p["tokens"][hc.TOKEN_ROLE], role)
        self.assertEqual(self.herdr.pane(fe)["tokens"][hc.TOKEN_TASK], "frontend")

        # Herdr's plain resume dropped the profile flags; restore re-applied them.
        fe_launches = self.herdr.launches_of(fe)
        self.assertEqual([launch["plain_resume"] for launch in fe_launches], [False, True, False])
        args = fe_launches[-1]["args"]
        self.assertEqual(args[:4], ["--resume", sessions[fe], "--system-prompt-snapshot", "off"])
        self.assertEqual(arg_after(args, "--agent"), "horchestra-fe")
        self.assertIn("--add-dir", args)
        self.assertEqual(arg_after(self.herdr.launches_of(be)[-1]["args"], "--agent"), "horchestra-be")
        orch_args = self.herdr.launches_of(self.orch)[-1]["args"]
        self.assertEqual(orch_args[:2], ["--resume", sessions[self.orch]])
        self.assertEqual(arg_after(orch_args, "--agent"), "orchestrator")

        notes = [t for t in self.herdr.prompts_to(self.orch) if t.startswith("[horchestra] Herdr restarted")]
        self.assertEqual(len(notes), 1)
        self.assertIn("Role profiles were re-applied to: fe, be", notes[0])
        self.assertTrue(any(p["wait"] for p in self.herdr.prompts if p["pane_id"] == self.orch))

        for pid in old_maps:
            self.assertIsNone(self.herdr.pane(pid), "dead map shells are closed")
        team_tabs = {self.herdr.pane(pid)["tab_id"] for pid in names}
        self.assertEqual({m["tab_id"] for m in self.herdr.map_panes(self.ws)}, team_tabs)
        self.assertEqual(len(self.herdr.map_panes(self.ws)), 3)

    def test_restart_reports_a_member_that_did_not_resume_instead_of_starting_it(self):
        fe, be = self.build_team()
        self.herdr.simulate_restart(not_resumed=[be])

        log = self.restore()
        self.assertIn("not resumed: be", log)
        p = self.herdr.pane(be)
        self.assertIsNone(p["agent"], "restore never starts a member")
        self.assertEqual(p["label"], "team:be")
        self.assertEqual(len(self.herdr.launches_of(be)), 1, "only the original hire launched it")
        notes = [t for t in self.herdr.prompts_to(self.orch) if t.startswith("[horchestra] Herdr restarted")]
        self.assertEqual(len(notes), 1)
        self.assertIn("did not resume", notes[0])
        self.assertIn("be", notes[0].split("did not resume")[1].split(".")[0])
        self.assertIsNotNone(self.herdr.agent_name_of(fe), "the resumed member is repaired")
        self.assertEqual(self.member("be")["session"], self.herdr.launches_of(be)[0]["session"],
                         "the unresumed member's session stays recorded for reopen")


class ReopenJourneyTest(JourneyCase):
    def test_reopen_recreates_the_space_resuming_every_agent_in_team_order(self):
        self.init_team()
        self.hire("fe", "frontend")
        self.hire("be", "backend")
        sessions = {"orchestrator": self.team_data()["orchestrator"]["session"],
                    "fe": self.member("fe")["session"], "be": self.member("be")["session"]}
        other = self.herdr.add_workspace("scratch", self.home)
        caller = self.herdr.root_pane(other)

        code, _, err = self.run_team("reopen", pane=caller)
        self.assertEqual(code, 1, "reopen refuses while the team is still open")
        self.assertIn("still has panes", err)
        self.assertEqual(len(self.herdr.workspaces), 2)

        self.herdr.close_workspace(self.ws)
        before = len(self.herdr.launches)
        out = self.ok("reopen", pane=caller)
        self.assertIn("resumed 3, fresh 0, failed 0", out)

        (new_ws,) = [w for w in self.herdr.workspaces if w not in (self.ws, other)]
        self.assertEqual(self.herdr.workspaces[new_ws]["label"], "proj")
        self.assertEqual([t["label"] for t in self.herdr.tabs_in(new_ws)], ["orchestrator", "fe", "be"])
        for role, session in sessions.items():
            p = self.pane_of(role, new_ws)
            self.assertEqual(p["agent_session"]["value"], session)
            (launch,) = self.herdr.launches_of(p["pane_id"])
            self.assertEqual(launch["args"][:4], ["--resume", session, "--system-prompt-snapshot", "off"])
            self.assertEqual(p["tokens"][hc.TOKEN_ROLE], role)
        self.assertEqual(len(self.herdr.launches) - before, 3, "each agent started exactly once")
        self.assertEqual(arg_after(self.herdr.launches_of(self.pane_of("fe", new_ws)["pane_id"])[0]["args"],
                                   "--agent"), "horchestra-fe")
        self.assertEqual({m["tab_id"] for m in self.herdr.map_panes(new_ws)},
                         {t["tab_id"] for t in self.herdr.tabs_in(new_ws)})
        orch = self.pane_of("orchestrator", new_ws)["pane_id"]
        self.assertTrue(self.herdr.prompts_to(orch)[-1].startswith("[horchestra] This space was reopened"))
        self.assertIn("Resumed with their conversations: orchestrator, fe, be", self.herdr.prompts_to(orch)[-1])


class NamingJourneyTest(JourneyCase):
    def test_two_projects_with_the_same_folder_name_can_both_hire_the_same_role(self):
        proj_a = self.make_project("a", "api")
        proj_b = self.make_project("b", "api")
        ws_a = self.herdr.add_workspace("api", proj_a)
        ws_b = self.herdr.add_workspace("api", proj_b)
        orch_a = self.herdr.add_agent(self.herdr.root_pane(ws_a))
        orch_b = self.herdr.add_agent(self.herdr.root_pane(ws_b))

        self.ok("init", pane=orch_a, cwd=proj_a)
        self.ok("hire", "fe", "--task", "frontend A", pane=orch_a, cwd=proj_a)
        self.ok("init", pane=orch_b, cwd=proj_b)
        self.ok("hire", "fe", "--task", "frontend B", pane=orch_b, cwd=proj_b)

        fe_a, fe_b = self.pane_of("fe", ws_a)["pane_id"], self.pane_of("fe", ws_b)["pane_id"]
        names = [self.herdr.agent_name_of(p) for p in (orch_a, orch_b, fe_a, fe_b)]
        self.assertEqual(len(set(names)), 4, names)
        for name in names[2:]:
            self.assertRegex(name, HASHED_NAME.format(project="api", role="fe"))
        self.assertEqual(self.member("fe", os.path.join(proj_a, "team.toml"))["task"], "frontend A")
        self.assertEqual(self.member("fe", os.path.join(proj_b, "team.toml"))["task"], "frontend B")

    def test_existing_team_with_plain_agent_names_keeps_them(self):
        fe_tab = self.herdr.add_tab(self.ws, "fe")
        fe = self.herdr.add_agent(self.herdr.root_pane(fe_tab), session="sess-fe", name="proj-fe")
        self.herdr.panes[self.orch]["name"] = "proj-orchestrator"
        self.herdr.panes[self.orch]["label"] = "team:orchestrator"
        self.herdr.panes[fe]["label"] = "team:fe"
        self.write(self.team_file,
                   'default_kind = "claude"\n\n[orchestrator]\nkind = "claude"\nsession = "sess-orch"\n\n'
                   '[[member]]\nrole = "fe"\nkind = "claude"\ntask = "frontend"\nsession = "sess-fe"\n')

        out = self.ok("status")
        self.assertIn("proj-fe ", out)
        self.assertEqual(self.herdr.agent_name_of(fe), "proj-fe")
        self.assertEqual(self.herdr.agent_name_of(self.orch), "proj-orchestrator")
        self.assertEqual(self.herdr.calls_of("agent", "rename"), [])

        self.ok("respawn", "fe")
        self.assertEqual(self.herdr.launches_of(fe)[-1]["name"], "proj-fe")
        self.assertEqual(self.herdr.agent_name_of(fe), "proj-fe", "respawn keeps the plain name")
        self.assertEqual(self.herdr.pane(fe)["agent_session"]["value"], "sess-fe")


class ForgetJourneyTest(JourneyCase):
    def test_forget_sticks_when_members_later_run_status_and_message(self):
        self.init_team()
        fe = self.hire("fe", "frontend")
        self.assertIn(self.team_file, self.registry())

        self.assertIn("forgot", self.ok("forget"))
        self.assertNotIn(self.team_file, self.registry())

        self.ok("status")
        self.ok("message", "orchestrator", "still", "here", pane=fe)
        self.ok("report", "working on it", pane=fe)
        self.ok("sync")
        self.assertNotIn(self.team_file, self.registry())
        self.assertEqual(self.herdr.prompts_to(self.orch)[-1], "[horchestra] message from fe: still here")
        self.assertTrue(os.path.isfile(self.team_file), "forget keeps team.toml")

        # Nothing registered: a restart restores nothing.
        self.herdr.simulate_restart()
        self.restore()
        self.assertIsNone(self.herdr.agent_name_of(fe))
        self.assertEqual(self.herdr.prompts_to(self.orch)[-1], "[horchestra] message from fe: still here")


if __name__ == "__main__":
    unittest.main()
