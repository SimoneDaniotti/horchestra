import json
import multiprocessing
import os
import tempfile
import unittest
from unittest import mock

from tests import helpers  # noqa: F401
from tests.test_journeys import JourneyCase
import herdr_client as hc
import roles
import tasks
import team


def _add_many(root, role, count):
    for i in range(count):
        tasks.add(root, role, f"task {i}")


class TaskStoreTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = tmp.name

    def test_ids_grow_and_tasks_close_with_a_summary(self):
        a = tasks.add(self.root, "fe", "  build   the  form ")
        b = tasks.add(self.root, "be", "payments API")
        self.assertEqual((a["id"], b["id"]), (1, 2))
        self.assertEqual(a["text"], "build the form")
        closed = tasks.close(self.root, 1, "done", "form   merged")
        self.assertEqual((closed["state"], closed["summary"]), ("done", "form merged"))
        data = tasks.load(self.root)
        self.assertEqual([t["state"] for t in data["tasks"]], ["done", "open"])
        self.assertEqual([t["id"] for t in tasks.open_for(data, "BE")], [2])
        self.assertEqual(tasks.add(self.root, "fe", "next")["id"], 3)

    def test_bad_input_is_refused(self):
        with self.assertRaises(tasks.TaskError):
            tasks.add(self.root, "fe", "   ")
        tasks.add(self.root, "fe", "x")
        for state in ("open", "maybe"):
            with self.assertRaises(tasks.TaskError):
                tasks.close(self.root, 1, state)
        with self.assertRaises(tasks.TaskError):
            tasks.close(self.root, 99, "done")

    def test_a_damaged_file_is_an_error_not_a_crash_and_ids_never_repeat(self):
        path = tasks.path_for(self.root)
        os.makedirs(os.path.dirname(path))
        with open(path, "w") as fh:
            fh.write("{not json")
        with self.assertRaises(tasks.TaskError):
            tasks.load(self.root)
        with open(path, "w") as fh:
            json.dump({"next_id": 2, "tasks": [{"id": 7, "role": "fe"}, "junk"]}, fh)
        self.assertEqual(tasks.add(self.root, "fe", "after")["id"], 8)

    def test_concurrent_writers_do_not_lose_tasks(self):
        ctx = multiprocessing.get_context("fork")
        workers = [ctx.Process(target=_add_many, args=(self.root, f"r{i}", 15)) for i in range(4)]
        for w in workers:
            w.start()
        for w in workers:
            w.join(20)
        ids = [t["id"] for t in tasks.load(self.root)["tasks"]]
        self.assertEqual(sorted(ids), list(range(1, 61)))

    def test_nudges_are_recorded_per_task(self):
        tasks.add(self.root, "fe", "a")
        tasks.add(self.root, "fe", "b")
        tasks.mark_nudged(self.root, {2}, now=1234)
        data = tasks.load(self.root)
        self.assertNotIn("nudged", tasks.find(data, 1))
        self.assertEqual(tasks.find(data, 2)["nudged"], 1234)
        self.assertEqual(tasks.line({"id": 4, "role": "fe", "text": "x" * 80}, width=10), "#4 fe: xxxxxxxxx…")


class TaskJourneyTest(JourneyCase):
    def board(self):
        return tasks.load(self.project)["tasks"]

    def test_hire_records_task_one_and_briefs_with_its_number(self):
        self.init_team()
        fe = self.hire("fe", "checkout page")
        self.assertEqual([(t["id"], t["role"], t["text"], t["state"]) for t in self.board()],
                         [(1, "fe", "checkout page", "open")])
        brief = self.herdr.prompts_to(fe)[0]
        self.assertIn("task #1", brief)
        self.assertIn("horchestra-team done 1", brief)

    def test_assign_done_and_the_orchestrator_hears_about_it(self):
        self.init_team()
        fe = self.hire("fe", "checkout page")
        self.assertIn("task #2 assigned to fe", self.ok("assign", "fe", "add", "a", "coupon", "field"))
        self.assertIn("task #2 from the orchestrator: add a coupon field", self.herdr.prompts_to(fe)[-1])
        self.assertEqual(hc.parse_signal(self.herdr.pane(self.orch)["tokens"][hc.TOKEN_SIGNAL])[:2], ("msg", fe))

        code, _, err = self.run_team("done", "coupon", "works", pane=fe)
        self.assertEqual(code, 1)
        self.assertIn("several open tasks (#1, #2)", err)
        self.ok("done", "2", "coupon", "field", "added", pane=fe)
        self.assertEqual(self.herdr.prompts_to(self.orch)[-1],
                         "[horchestra] message from fe: task #2 done: coupon field added")
        tokens = self.herdr.pane(fe)["tokens"]
        self.assertEqual(tokens[hc.TOKEN_STATUS], "done: coupon field added")
        self.assertEqual(hc.parse_signal(tokens[hc.TOKEN_SIGNAL])[0], "report")
        self.ok("done", "checkout", "page", "live", pane=fe)  # the one open task left: #1
        self.assertEqual([t["state"] for t in self.board()], ["done", "done"])
        code, _, err = self.run_team("done", "again", pane=fe)
        self.assertIn("no open task", err)
        code, _, err = self.run_team("done", "1", "twice", pane=fe)
        self.assertIn("already done", err)

    def test_only_the_owner_or_the_orchestrator_closes_a_task(self):
        self.init_team()
        fe = self.hire("fe", "checkout page")
        be = self.hire("be", "payments API")
        code, _, err = self.run_team("done", "1", "not mine", pane=be)
        self.assertEqual(code, 1)
        self.assertIn("belongs to fe", err)
        before = len(self.herdr.prompts_to(self.orch))
        self.ok("done", "1", "closed after review")  # the orchestrator
        self.assertEqual(len(self.herdr.prompts_to(self.orch)), before, "no message to itself")
        code, _, err = self.run_team("done", "summary without a number")
        self.assertIn("give the task number", err)
        self.assertEqual(self.board()[0]["state"], "done")
        self.assertNotIn(hc.TOKEN_STATUS, self.herdr.pane(fe)["tokens"])

    def test_blocked_flags_the_task_and_cancel_drops_it(self):
        self.init_team()
        fe = self.hire("fe", "checkout page")
        self.ok("blocked", "need", "the", "API", "keys", pane=fe)
        self.assertEqual(self.herdr.prompts_to(self.orch)[-1],
                         "[horchestra] message from fe: task #1 blocked: need the API keys")
        self.assertEqual(hc.parse_signal(self.herdr.pane(fe)["tokens"][hc.TOKEN_SIGNAL])[0], "needs")
        self.assertIn("blocked", self.ok("tasks"))
        self.assertIn("task #1 blocked: checkout page (need the API keys)", self.ok("status"))
        self.ok("cancel", "1", "postponed")
        self.assertEqual(self.board()[0]["state"], "cancelled")
        self.assertIn("no open tasks", self.ok("tasks"))
        self.assertIn("postponed", self.ok("tasks", "--all"))
        code, _, err = self.run_team("cancel", "1")
        self.assertIn("already cancelled", err)

    def test_assign_refuses_strangers_and_stopped_members(self):
        self.init_team()
        code, _, err = self.run_team("assign", "ghost", "x")
        self.assertIn("not a member", err)
        fe = self.hire("fe", "checkout page")
        self.herdr.inject_error(("agent", "prompt"), "agent_not_ready", "not the foreground process")
        code, _, err = self.run_team("assign", "fe", "lost in transit")
        self.assertEqual(code, 1)
        self.assertIn("could not send task #2 to fe (cancelled it)", err)
        self.assertEqual(tasks.find(tasks.load(self.project), 2)["state"], "cancelled")
        self.herdr.panes[fe]["agent"] = None
        code, _, err = self.run_team("assign", "fe", "x")
        self.assertIn("no running agent", err)

    def test_restart_and_reopen_notes_list_open_tasks(self):
        self.init_team()
        self.hire("fe", "checkout page")
        self.ok("assign", "fe", "coupon field")
        self.assertEqual(team.open_tasks_note(team.Space(self.ws, self.team_file, self.team_data())),
                         "Tasks still open: #1 fe: checkout page; #2 fe: coupon field. ")


class OnboardingTest(JourneyCase):
    AGREEMENT = ("--call-when", "UI or CSS changes under web/",
                 "--handoff", "one task at a time, under an hour each",
                 "--reporting", "a status line at each milestone")

    def agent_file(self, role):
        with open(os.path.join(self.project, ".claude", "agents", roles.agent_name(role) + ".md")) as fh:
            return fh.read()

    def test_hire_with_an_agreement_writes_it_everywhere_the_orchestrator_and_member_look(self):
        self.init_team()
        self.hire("be", "payments API", "--call-when", "API or database changes")
        self.hire("fe", "checkout page", *self.AGREEMENT)
        member = self.member("fe")
        self.assertEqual(member["call_when"], "UI or CSS changes under web/")
        self.assertEqual(member["handoff"], "one task at a time, under an hour each")
        text = self.agent_file("fe")
        self.assertIn("The orchestrator brings you in for: UI or CSS changes under web/.", text)
        self.assertIn("How you receive work: one task at a time, under an hour each.", text)
        self.assertIn("When to report back: a status line at each milestone.", text)
        self.assertIn("- be (claude): payments API. Call when: API or database changes", text)
        self.assertIn("horchestra-team done N", text)
        status = self.ok("status")
        self.assertIn("call when: UI or CSS changes under web/", status)
        self.assertIn("reporting: a status line at each milestone", status)

    def test_onboard_updates_an_adopted_member_and_tells_it(self):
        self.init_team()
        tab = self.herdr.add_tab(self.ws, "2")
        stray = self.herdr.add_agent(self.herdr.root_pane(tab), session="sess-stray")
        self.ok("adopt", stray, "--role", "docs", "--task", "release notes")
        self.assertIn("no onboarding agreements for docs yet", self.ok("onboard", "docs"))
        self.ok("onboard", "docs", "--call-when", "anything user-facing changes", "--reporting", "done only")
        self.assertEqual(self.member("docs")["call_when"], "anything user-facing changes")
        note = self.herdr.prompts_to(stray)[-1]
        self.assertIn("Working agreement", note)
        self.assertIn("- When to report back: done only", note)
        self.assertIn("anything user-facing changes", self.agent_file("docs"))
        self.assertIn("call_when: anything user-facing changes", self.ok("onboard", "docs").replace(
            "The orchestrator brings you in for", "call_when"))
        code, _, err = self.run_team("onboard", "ghost", "--handoff", "x")
        self.assertIn("not a member", err)


class IdleNoticeTest(JourneyCase):
    def fire_hook(self, pane, status="idle"):
        event = {"event": "pane_agent_status_changed",
                 "data": {"type": "pane_agent_status_changed", "pane_id": pane,
                          "workspace_id": self.ws, "agent_status": status}}
        with mock.patch.dict(os.environ, {"HERDR_PLUGIN_EVENT_JSON": json.dumps(event),
                                          "HERDR_PLUGIN_ID": "horchestra"}):
            self.ok("hook-status", pane=pane)

    def notes(self):
        return [p for p in self.herdr.prompts_to(self.orch) if "went idle with open task" in p]

    def test_idle_member_with_an_open_task_brings_one_note(self):
        self.init_team()
        fe = self.hire("fe", "checkout page")
        self.fire_hook(fe, "working")  # not idle: nothing
        self.assertEqual(self.notes(), [])
        self.fire_hook(fe)
        self.assertEqual(len(self.notes()), 1)
        note = self.notes()[0]
        self.assertIn('#1 "checkout page"', note)
        self.assertIn(f"herdr agent read {self.herdr.agent_name_of(fe)} --lines 80", note)
        self.assertIn("horchestra-team done 1", note)
        self.fire_hook(fe)  # again soon after: no repeat
        self.assertEqual(len(self.notes()), 1)
        self.clock.now += team.NUDGE_EVERY + 1
        self.fire_hook(fe)
        self.assertEqual(len(self.notes()), 2)

    def test_a_failed_delivery_is_retried_on_the_next_idle(self):
        self.init_team()
        fe = self.hire("fe", "checkout page")
        self.herdr.inject_error(("agent", "prompt"), "agent_not_ready", "busy")
        event = {"data": {"pane_id": fe, "agent_status": "idle"}}
        with mock.patch.dict(os.environ, {"HERDR_PLUGIN_EVENT_JSON": json.dumps(event)}):
            code, _, err = self.run_team("hook-status", pane=fe)
        self.assertEqual(code, 1)
        self.assertIn("could not tell the orchestrator", err)
        self.assertNotIn("nudged", tasks.find(tasks.load(self.project), 1))
        self.fire_hook(fe)
        self.assertEqual(len(self.notes()), 1)

    def test_no_note_when_done_was_run_or_the_member_waits_on_the_human(self):
        self.init_team()
        fe = self.hire("fe", "checkout page")
        self.ok("report", "--needs-you", "which colour?", pane=fe)
        self.fire_hook(fe)
        self.ok("done", "1", "shipped", pane=fe)
        self.fire_hook(fe)
        self.assertEqual(self.notes(), [])

    def test_strangers_the_orchestrator_and_forgotten_teams_are_ignored(self):
        self.init_team()
        fe = self.hire("fe", "checkout page")
        tab = self.herdr.add_tab(self.ws, "9")
        stranger = self.herdr.add_agent(self.herdr.root_pane(tab), session="sess-x")
        self.fire_hook(stranger)
        self.fire_hook(self.orch)
        self.ok("forget")
        self.fire_hook(fe)
        self.assertEqual(self.notes(), [])

    def test_malformed_events_are_ignored(self):
        self.assertEqual(team.event_pane_status("not json"), (None, None))
        self.assertEqual(team.event_pane_status('{"data": {"pane_id": "w1:p1"}}'), ("w1:p1", None))
        self.assertEqual(team.event_pane_status('{"pane_id": "w1:p1", "agent_status": "idle"}'), ("w1:p1", "idle"))


if __name__ == "__main__":
    unittest.main()
