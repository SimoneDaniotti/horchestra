import contextlib
import fcntl
import os
import tempfile
import unittest
from unittest import mock

from tests import helpers  # noqa: F401
from tests.fakeherdr import FakeClock
from tests.test_journeys import JourneyCase
import herdr_client as hc
import outbox

DIM = "\x1b[2m"
RESET = "\x1b[0m"
GREY = "\x1b[0m\x1b[38;2;136;136;136m"
RULE = GREY + "─" * 60 + RESET
LABELLED_RULE = GREY + "─" * 50 + " orchestrator ─" + RESET
FOOTER = "  \x1b[38;2;255;193;7m⏵⏵ auto mode on" + RESET + " (shift+tab to cycle)"


def claude_screen(box, history=()):
    """A Claude Code bottom screen as `herdr pane read --format ansi` gives it."""
    return "\n".join([*history, "", LABELLED_RULE, *box, RULE, FOOTER])


EMPTY = claude_screen(["❯\xa0"])
PLACEHOLDER = claude_screen(["❯\xa0" + RESET + DIM + 'Try "how does <filepath> work?"' + RESET])
DRAFT = claude_screen(["❯\xa0half a thought"])


class DraftTest(unittest.TestCase):
    def test_an_empty_box_has_no_draft(self):
        self.assertEqual(outbox.draft_in(EMPTY), "")

    def test_the_dim_placeholder_is_not_a_draft(self):
        self.assertEqual(outbox.draft_in(PLACEHOLDER), "")

    def test_typed_text_is_a_draft(self):
        self.assertEqual(outbox.draft_in(DRAFT), "half a thought")

    def test_a_draft_wraps_onto_following_lines(self):
        screen = claude_screen(["❯\xa0first line of a long", "  prompt that wraps"])
        self.assertEqual(outbox.draft_in(screen), "first line of a long prompt that wraps")

    def test_past_prompts_in_the_conversation_do_not_count(self):
        screen = claude_screen(["❯\xa0"], history=["❯ fix the login page", "⏺ Done."])
        self.assertEqual(outbox.draft_in(screen), "")

    def test_codex_box(self):
        self.assertEqual(outbox.draft_in("› " + DIM + "Ask Codex to do anything" + RESET + "\n\n  footer"), "")
        self.assertEqual(outbox.draft_in("› rename the module\n\n  footer"), "rename the module")

    def test_a_screen_without_an_input_box_has_no_draft(self):
        self.assertEqual(outbox.draft_in(""), "")
        self.assertEqual(outbox.draft_in("Do you trust the files in this folder?\n1. Yes"), "")

    def test_dim_text_after_bold_then_reset(self):
        line = "❯ \x1b[1mbold\x1b[22m " + DIM + "hint" + "\x1b[22m typed"
        self.assertEqual(outbox.draft_in(line), "bold typed")


class OutboxCase(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        env = mock.patch.dict(os.environ, {"HORCHESTRA_STATE_DIR": tmp.name})
        env.start()
        self.addCleanup(env.stop)
        self.screens = {"w1:p1": EMPTY}
        self.sent = []
        self.couriers = []
        for name, value in (("read_box", lambda pane: self.screens.get(pane, "")),
                            ("start_courier", self.couriers.append)):
            patcher = mock.patch.object(outbox, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.clock = FakeClock()
        self.logged = []

    def prompt(self, pane, text):
        self.sent.append((pane, text))

    def run_courier(self, pane="w1:p1", prompt=None, on_sleep=None):
        def sleep(seconds):
            self.clock.sleep(seconds)
            if on_sleep:
                on_sleep(self.clock.monotonic())

        outbox.courier(pane, prompt or self.prompt, clock=self.clock.monotonic, sleep=sleep,
                       log=self.logged.append)


class SendTest(OutboxCase):
    def test_an_empty_box_gets_the_message_now(self):
        self.assertTrue(outbox.send("w1:p1", "report", self.prompt))
        self.assertEqual(self.sent, [("w1:p1", "report")])
        self.assertEqual(self.couriers, [])

    def test_a_draft_holds_the_message_and_starts_a_courier(self):
        self.screens["w1:p1"] = DRAFT
        self.assertFalse(outbox.send("w1:p1", "report", self.prompt))
        self.assertEqual(self.sent, [], "nothing is typed into the human's draft")
        self.assertEqual(outbox.pending("w1:p1"), ["report"])
        self.assertEqual(self.couriers, ["w1:p1"])

    def test_later_messages_wait_behind_queued_ones(self):
        self.screens["w1:p1"] = DRAFT
        outbox.send("w1:p1", "first", self.prompt)
        self.screens["w1:p1"] = EMPTY
        self.assertFalse(outbox.send("w1:p1", "second", self.prompt))
        self.assertEqual(self.sent, [])
        self.assertEqual(outbox.pending("w1:p1"), ["first", "second"])

    def test_queues_are_per_pane(self):
        self.screens["w1:p1"] = DRAFT
        outbox.send("w1:p1", "held", self.prompt)
        self.screens["w1:p2"] = EMPTY
        self.assertTrue(outbox.send("w1:p2", "direct", self.prompt))
        self.assertEqual(self.sent, [("w1:p2", "direct")])

    def test_errors_from_a_direct_send_propagate(self):
        def broken(pane, text):
            raise hc.HerdrError("no agent", "agent_not_found")

        with self.assertRaises(hc.HerdrError):
            outbox.send("w1:p1", "report", broken)


class CourierTest(OutboxCase):
    def test_sends_in_order_once_the_box_stays_empty(self):
        self.screens["w1:p1"] = DRAFT
        outbox.enqueue("w1:p1", "first")
        outbox.enqueue("w1:p1", "second")

        def human(now):
            if now >= 1010:
                self.screens["w1:p1"] = EMPTY  # the human sent their prompt

        self.run_courier(on_sleep=human)
        self.assertEqual(self.sent, [("w1:p1", "first"), ("w1:p1", "second")])
        self.assertEqual(outbox.pending("w1:p1"), [])
        self.assertFalse(os.path.exists(outbox.queue_path("w1:p1")))

    def test_waits_while_the_human_keeps_typing(self):
        self.screens["w1:p1"] = DRAFT
        outbox.enqueue("w1:p1", "report")
        looks = []

        def human(now):
            looks.append(now)
            if len(looks) == 3:
                self.screens["w1:p1"] = EMPTY  # cleared for a moment...
            elif len(looks) == 4:
                self.screens["w1:p1"] = DRAFT  # ...and typing again
            elif len(looks) == 20:
                self.screens["w1:p1"] = EMPTY

        self.run_courier(on_sleep=human)
        self.assertEqual(self.sent, [("w1:p1", "report")])
        self.assertGreaterEqual(len(looks), 20, "sent only after the box stayed empty")

    def test_keeps_the_message_and_retries_when_the_agent_is_restarting(self):
        outbox.enqueue("w1:p1", "report")
        attempts = []

        def flaky(pane, text):
            attempts.append(text)
            if len(attempts) == 1:
                raise hc.HerdrError("agent target w1:p1 not found", "agent_not_found")
            self.sent.append((pane, text))

        with mock.patch.object(outbox, "pane_gone", return_value=False):
            self.run_courier(prompt=flaky)
        self.assertEqual(self.sent, [("w1:p1", "report")])
        self.assertIn("will retry", self.logged[0])

    def test_drops_the_queue_when_the_pane_closed(self):
        outbox.enqueue("w1:p1", "report")

        def gone(pane, text):
            raise hc.HerdrError("agent target w1:p1 not found", "agent_not_found")

        with mock.patch.object(outbox, "pane_gone", return_value=True):
            self.run_courier(prompt=gone)
        self.assertEqual(outbox.pending("w1:p1"), [])
        self.assertIn("pane closed", self.logged[0])

    def test_gives_up_waiting_but_keeps_the_queue(self):
        self.screens["w1:p1"] = DRAFT
        outbox.enqueue("w1:p1", "report")
        self.run_courier()
        self.assertEqual(self.sent, [])
        self.assertEqual(outbox.pending("w1:p1"), ["report"])
        self.assertIn("stay queued", self.logged[0])

    def test_only_one_courier_per_pane(self):
        outbox.enqueue("w1:p1", "report")
        path = outbox.queue_path("w1:p1") + ".run"
        with open(path, "a") as held:
            fcntl.flock(held, fcntl.LOCK_EX)
            self.run_courier()
            fcntl.flock(held, fcntl.LOCK_UN)
        self.assertEqual(self.sent, [], "the running courier owns the queue")
        self.run_courier()
        self.assertEqual(self.sent, [("w1:p1", "report")])

    def test_the_courier_role_is_free_before_anyone_can_enqueue_again(self):
        # Else a message queued as the courier exits would wait for a courier
        # that is gone, while the one it started saw the role taken and quit.
        outbox.enqueue("w1:p1", "report")
        run_path = outbox.queue_path("w1:p1") + ".run"
        real_locked, free_at_unlock = outbox.locked, []

        @contextlib.contextmanager
        def watching(path):
            with real_locked(path):
                yield
                with open(run_path, "a") as probe:
                    try:
                        fcntl.flock(probe, fcntl.LOCK_EX | fcntl.LOCK_NB)
                        fcntl.flock(probe, fcntl.LOCK_UN)
                        free_at_unlock.append(True)
                    except BlockingIOError:
                        free_at_unlock.append(False)

        with mock.patch.object(outbox, "locked", watching):
            self.run_courier()
        self.assertEqual(self.sent, [("w1:p1", "report")])
        self.assertTrue(free_at_unlock[-1])


class ReportWhileTypingJourneyTest(JourneyCase):
    """A member finishes a task while the human is typing to the orchestrator."""

    def setUp(self):
        super().setUp()
        self.couriers = []
        patcher = mock.patch.object(outbox, "start_courier", self.couriers.append)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_the_report_waits_for_the_humans_prompt(self):
        self.init_team()
        fe = self.hire("fe", "frontend")
        self.ok("assign", "fe", "build", "the", "form")
        before = len(self.herdr.prompts_to(self.orch))

        self.herdr.set_screen(self.orch, DRAFT)
        out = self.ok("done", "2", "the form is built", pane=fe)
        self.assertIn("task #", out)
        self.assertEqual(len(self.herdr.prompts_to(self.orch)), before, "the draft is left alone")
        self.assertEqual(self.couriers, [self.orch])

        self.herdr.set_screen(self.orch, EMPTY)
        clock = FakeClock()
        outbox.courier(self.orch, outbox.herdr_prompt, clock=clock.monotonic, sleep=clock.sleep,
                       log=lambda _msg: None)
        self.assertEqual(self.herdr.prompts_to(self.orch)[-1],
                         "[horchestra] message from fe: task #2 done: the form is built")

    def test_a_message_to_a_member_being_typed_to_says_it_is_queued(self):
        self.init_team()
        fe = self.hire("fe", "frontend")
        self.herdr.set_screen(fe, DRAFT)
        out = self.ok("message", "fe", "please add tests")
        self.assertIn("queued", out)
        self.assertNotIn("[horchestra] message from orchestrator: please add tests", self.herdr.prompts_to(fe))
