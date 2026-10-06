import json
import os
import tempfile
import unittest
from unittest import mock

from tests import helpers  # noqa: F401
import activity

SESSION = "11111111-2222-4333-8444-555555555555"
T0 = 1_800_000_000.0  # a fixed epoch second


def stamp(seconds):
    import datetime

    return datetime.datetime.fromtimestamp(T0 + seconds, datetime.timezone.utc).isoformat().replace("+00:00", "Z")


def claude_line(kind, seconds):
    return json.dumps({"type": kind, "timestamp": stamp(seconds), "message": {"content": "x"}}) + "\n"


def codex_line(kind, seconds):
    return json.dumps({"timestamp": stamp(seconds), "type": kind, "payload": {"type": "message"}}) + "\n"


class Clock:
    def __init__(self, now):
        self.now = now

    def __call__(self):
        return self.now


class Tmp(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = tmp.name
        self.claude = os.path.join(self.root, "claude")
        self.codex = os.path.join(self.root, "codex")

    def write(self, path, text, mode="a"):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, mode) as fh:
            fh.write(text)
        return path

    def claude_transcript(self, text="", session=SESSION):
        return self.write(os.path.join(self.claude, "projects", "-home-user-proj", session + ".jsonl"), text)


class TranscriptTest(Tmp):
    def test_counts_only_assistant_messages_with_their_timestamps(self):
        path = self.claude_transcript(claude_line("user", 0) + claude_line("assistant", 5)
                                      + claude_line("attachment", 6) + claude_line("assistant", 9))
        t = activity.Transcript(path, "claude")
        t.poll()
        self.assertEqual(t.times, [T0 + 5, T0 + 9])

    def test_codex_counts_response_items(self):
        path = self.write(os.path.join(self.codex, "x.jsonl"),
                          codex_line("session_meta", 0) + codex_line("response_item", 3) + codex_line("event_msg", 4))
        t = activity.Transcript(path, "codex")
        t.poll()
        self.assertEqual(t.times, [T0 + 3])

    def test_polls_read_only_what_was_appended_and_wait_for_whole_lines(self):
        path = self.claude_transcript(claude_line("assistant", 1))
        t = activity.Transcript(path, "claude")
        t.poll()
        line = claude_line("assistant", 2)
        self.write(path, line[:20])  # the agent is mid-write
        t.poll()
        self.assertEqual(t.times, [T0 + 1])
        self.write(path, line[20:])
        t.poll()
        t.poll()  # nothing new: no double counting
        self.assertEqual(t.times, [T0 + 1, T0 + 2])

    def test_a_replaced_transcript_is_read_again_from_the_start(self):
        path = self.claude_transcript(claude_line("assistant", 1) * 3)
        t = activity.Transcript(path, "claude")
        t.poll()
        self.write(path, claude_line("assistant", 7), mode="w")
        t.poll()
        self.assertEqual(t.times, [T0 + 7])

    def test_a_large_transcript_is_read_from_its_tail_skipping_the_cut_line(self):
        path = self.claude_transcript(claude_line("assistant", 1) * 50 + claude_line("assistant", 99))
        with mock.patch.object(activity, "READ_TAIL", len(claude_line("assistant", 99)) + 10):
            t = activity.Transcript(path, "claude")
            t.poll()
        self.assertEqual(t.times, [T0 + 99])

    def test_bad_lines_and_missing_files_are_skipped(self):
        path = self.claude_transcript("not json\n" + '{"type":"assistant","timestamp":"nope"}\n'
                                      + claude_line("assistant", 4))
        t = activity.Transcript(path, "claude")
        t.poll()
        self.assertEqual(t.times, [T0 + 4])
        gone = activity.Transcript(os.path.join(self.root, "missing.jsonl"), "claude")
        gone.poll()
        self.assertEqual(gone.times, [])


class LocateTest(Tmp):
    def test_finds_claude_and_codex_transcripts_by_session_id(self):
        main = self.claude_transcript(claude_line("assistant", 1))
        rollout = self.write(os.path.join(self.codex, "sessions", "2026", "10", "06",
                                          f"rollout-2026-10-06T10-00-00-{SESSION}.jsonl"), "")
        self.assertEqual(activity.locate("claude", SESSION, self.claude, self.codex), main)
        self.assertEqual(activity.locate("codex", SESSION, self.claude, self.codex), rollout)
        self.assertIsNone(activity.locate("claude", "other", self.claude, self.codex))
        self.assertIsNone(activity.locate("gemini", SESSION, self.claude, self.codex))

    def test_session_ids_cannot_escape_the_transcript_folders(self):
        self.write(os.path.join(self.claude, "projects", "x", "evil.jsonl"), "")
        for bad in ("../x/evil", "x/evil", ".hidden", ""):
            self.assertIsNone(activity.locate("claude", bad, self.claude, self.codex), bad)

    def test_claude_subagent_transcripts_belong_to_the_session(self):
        main = self.claude_transcript(claude_line("assistant", 1))
        folder = os.path.join(main[: -len(".jsonl")], "subagents")
        sub = self.write(os.path.join(folder, "agent-a1.jsonl"), claude_line("assistant", 2))
        self.write(os.path.join(folder, "agent-a1.meta.json"), "{}")
        self.assertEqual(activity.subagent_files("claude", main), [sub])
        self.assertEqual(activity.subagent_files("codex", main), [])


class ActivityTest(Tmp):
    def pane(self, pane_id, kind="claude", status="idle", session=SESSION):
        ref = {"agent": kind, "kind": "id", "source": "herdr:" + kind, "value": session} if session else None
        return {"pane_id": pane_id, "agent": kind, "agent_status": status, "agent_session": ref}

    def test_agent_history_comes_from_its_transcript_and_subagents(self):
        main = self.claude_transcript(claude_line("assistant", 1))
        act = activity.Activity(clock=Clock(T0 + 60), claude_root=self.claude, codex_root=self.codex)
        act.update([self.pane("w1:p1")])
        self.assertTrue(act.has_history("w1:p1"))
        self.assertEqual(act.times("w1:p1"), [T0 + 1])
        self.write(os.path.join(main[: -len(".jsonl")], "subagents", "agent-b.jsonl"), claude_line("assistant", 2))
        self.write(main, claude_line("assistant", 3))
        act.update([self.pane("w1:p1")])
        self.assertEqual(sorted(act.times("w1:p1")), [T0 + 1, T0 + 2, T0 + 3])

    def test_missing_transcripts_are_looked_for_again_later_not_every_poll(self):
        clock = Clock(T0)
        act = activity.Activity(clock=clock, claude_root=self.claude, codex_root=self.codex)
        with mock.patch.object(activity, "locate", wraps=activity.locate) as locate:
            act.update([self.pane("w1:p1")])
            self.claude_transcript(claude_line("assistant", 1))
            clock.now += 1
            act.update([self.pane("w1:p1")])
            self.assertEqual(locate.call_count, 1)
            self.assertFalse(act.has_history("w1:p1"))
            clock.now += activity.LOCATE_RETRY
            act.update([self.pane("w1:p1")])
        self.assertTrue(act.has_history("w1:p1"))

    def test_agents_without_transcripts_are_sampled_while_working(self):
        clock = Clock(T0)
        act = activity.Activity(clock=clock, claude_root=self.claude, codex_root=self.codex)
        for step, status in enumerate(["working", "working", "idle", "working"]):
            clock.now = T0 + step * activity.SAMPLE_EVERY
            act.update([self.pane("w1:p2", kind="pi", status=status, session=None)])
        step = activity.SAMPLE_EVERY
        self.assertEqual(act.times("w1:p2"), [T0, T0 + step, T0 + 3 * step])  # not while idle
        clock.now += activity.KEEP_SECONDS + 10
        act.update([self.pane("w1:p2", kind="pi", status="idle", session=None)])
        self.assertEqual(act.times("w1:p2"), [])

    def test_unknown_panes_have_no_history(self):
        act = activity.Activity(clock=Clock(T0), claude_root=self.claude, codex_root=self.codex)
        act.update([{"pane_id": "w1:p9"}, {"agent": "claude"}])
        self.assertFalse(act.has_history("w1:p9"))
        self.assertEqual(act.times("w1:p9"), [])


class PlotMathTest(unittest.TestCase):
    def test_buckets_count_events_in_aligned_slices(self):
        counts = activity.buckets([100, 101, 159, 160, 30, 500], now=170, window=120, count=2)
        # slices are [60, 120) and [120, 180): aligned to the 60 s slice width
        self.assertEqual(counts, [2, 2])

    def test_buckets_do_not_shift_within_a_slice(self):
        times = [T0 + i * 7 for i in range(100)]
        self.assertEqual(activity.buckets(times, T0 + 600, 600, 20), activity.buckets(times, T0 + 601, 600, 20))

    def test_sparkline_scales_to_the_peak_but_keeps_small_counts_visible(self):
        line = activity.sparkline([0, 1, 50, 100], 100)
        self.assertEqual(line[0], " ")
        self.assertEqual(line[1], activity.LEVELS[1])
        self.assertEqual(line[3], activity.LEVELS[-1])
        self.assertEqual(activity.sparkline([0, 0], 0), "  ")

    def test_window_labels(self):
        self.assertEqual([activity.window_label(w) for w in activity.WINDOWS], ["10m", "30m", "1h", "3h", "12h"])
        self.assertIsNone(activity.parse_time("yesterday"))
        self.assertEqual(activity.parse_time(stamp(0)), T0)


if __name__ == "__main__":
    unittest.main()
