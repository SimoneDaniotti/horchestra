import unittest

from tests import helpers  # noqa: F401
import views


def team():
    o = views.NodeView("o", "orchestrator", "claude", "working", "coordinates")
    s = views.NodeView("s", "slides", "claude", "idle", "deck 40%", here=True)
    q = views.NodeView("q", "QA", "codex", "idle", "which API?", needs=True)
    t = views.NodeView("t", "tests", "pi", "done", "e2e")
    for child in (s, q):
        child.parent = o
        o.children.append(child)
    t.parent = q
    q.children.append(t)
    return o, s, q, t


class RenderTest(unittest.TestCase):
    def test_every_view_draws_every_node(self):
        roots = [team()[0]]
        for render, width in ((views.render_compact, 30), (views.render_cards, 30),
                              (views.render_graph, 80)):
            canvas, boxes = render(roots, width)
            self.assertEqual(set(boxes), {"o", "s", "q", "t"}, render.__name__)
            text = canvas.text()
            for name in ("orchestrator", "slides", "QA", "tests"):
                self.assertIn(name, text, render.__name__)

    def test_cards_nest_children_and_flag_needs(self):
        canvas, boxes = views.render_cards([team()[0]], 30)
        self.assertGreater(boxes["s"][2], boxes["o"][2])  # child indented
        self.assertGreater(boxes["t"][2], boxes["q"][2])
        self.assertIn("! which API?", canvas.text())

    def test_folding_hides_descendants(self):
        o, s, q, t = team()
        q.collapsed = True
        canvas, boxes = views.render_cards([o], 30)
        self.assertNotIn("t", boxes)
        self.assertIn("+1", canvas.text())

    def test_graph_places_children_below_parent(self):
        canvas, boxes = views.render_graph([team()[0]], 80)
        self.assertEqual(boxes["s"][0], views.GRAPH_LEVEL)
        self.assertEqual(boxes["t"][0], 2 * views.GRAPH_LEVEL)
        self.assertLessEqual(views.graph_width([team()[0]]), canvas.width)

    def test_long_names_are_truncated_not_overflowing(self):
        node = views.NodeView("x", "a-very-long-agent-role-name", "claude", "working")
        canvas, boxes = views.render_cards([node], 20)
        self.assertTrue(all(len(line) <= 20 for line in canvas.text().splitlines()))
        self.assertIn("…", canvas.text())


if __name__ == "__main__":
    unittest.main()
