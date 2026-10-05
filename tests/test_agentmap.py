import unittest

from tests import helpers  # noqa: F401
import agentmap
import herdr_client as hc


def pane(pid, tab="w1:t1", agent="claude", status="idle", term=None, **tokens):
    return {"pane_id": pid, "tab_id": tab, "agent": agent, "agent_status": status,
            "terminal_id": term or f"term_{pid}", "tokens": tokens}


class ForestTest(unittest.TestCase):
    def test_parent_by_terminal_id_and_here_flag(self):
        panes = [
            pane("w1:p1", **{hc.TOKEN_ROLE: "orchestrator"}),
            pane("w1:p2", tab="w1:t2", **{hc.TOKEN_ROLE: "FE", hc.TOKEN_PARENT: "term_w1:p1"}),
        ]
        roots, _ = agentmap.build_forest(panes, "", "w1:t2", set(), None)
        self.assertEqual([r.name for r in roots], ["orchestrator"])
        child = roots[0].children[0]
        self.assertEqual(child.name, "FE")
        self.assertTrue(child.here)
        self.assertFalse(roots[0].here)

    def test_map_panes_and_plain_shells_are_excluded(self):
        panes = [pane("w1:p1"), pane("w1:p2", **{hc.TOKEN_VIEW: "1"}), pane("w1:p3", agent=None)]
        roots, _ = agentmap.build_forest(panes, "", "", set(), None)
        self.assertEqual([r.key for r in roots], ["w1:p1"])

    def test_cycles_are_broken(self):
        panes = [pane("a", **{hc.TOKEN_PARENT: "term_b"}), pane("b", **{hc.TOKEN_PARENT: "term_a"})]
        roots, _ = agentmap.build_forest(panes, "", "", set(), None)
        self.assertEqual(len(roots), 1)
        self.assertEqual(len(agentmap.visible_order(roots)), 2)

    def test_needs_you_only_while_not_working(self):
        waiting = pane("a", status="idle", **{hc.TOKEN_NEEDS: "1", hc.TOKEN_STATUS: "v1 or v2?"})
        busy = pane("b", status="working", **{hc.TOKEN_NEEDS: "1"})
        roots, _ = agentmap.build_forest([waiting, busy], "", "", set(), None)
        by_key = {r.key: r for r in roots}
        self.assertTrue(by_key["a"].needs)
        self.assertEqual(by_key["a"].detail, "v1 or v2?")
        self.assertFalse(by_key["b"].needs)


class TextTest(unittest.TestCase):
    def test_last_message_prefers_agent_bullets(self):
        screen = "⏺ Implemented the parser.\n❯ \n  ⏵⏵ auto mode on\n  12 tokens\n"
        self.assertEqual(agentmap.last_message(screen), "Implemented the parser.")

    def test_last_message_skips_shell_prompts(self):
        self.assertEqual(agentmap.last_message("user@host project %\n"), "")

    def test_wrap_and_ago(self):
        self.assertEqual(agentmap.wrap("one two three four", 9, 2), ["one two", "three…"])
        self.assertEqual(agentmap.wrap("short", 9, 2), ["short"])
        self.assertEqual(agentmap.wrap("supercalifragilistic", 8, 1), ["superca…"])
        self.assertEqual(agentmap.ago(45), "45s")
        self.assertEqual(agentmap.ago(125), "2m")
        self.assertEqual(agentmap.ago(3725), "1h02")


if __name__ == "__main__":
    unittest.main()
