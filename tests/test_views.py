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


class LiveEdgeTest(unittest.TestCase):
    RENDERERS = ((views.render_compact, 30), (views.render_cards, 30), (views.render_graph, 80))

    def test_every_view_records_one_edge_per_child_from_parent_to_child(self):
        for render, width in self.RENDERERS:
            canvas, boxes = render([team()[0]], width)
            edges = dict(canvas.edges)
            self.assertEqual(set(edges), {"s", "q", "t"}, render.__name__)
            for key, path in edges.items():
                parent_box, child_box = boxes[{"s": "o", "q": "o", "t": "q"}[key]], boxes[key]
                self.assertLessEqual(parent_box[0], path[0][0], render.__name__)  # starts at the parent
                self.assertEqual(path[-1][0], child_box[0], render.__name__)  # ends on the child's row
                for y, x in path:
                    self.assertNotEqual(canvas.get(y, x), " ", f"{render.__name__} gap at {y},{x}")

    def test_only_edges_into_working_children_light_up(self):
        for render, width in self.RENDERERS:
            canvas, _ = render([team()[0]], width)
            idle = {cell for key, path in canvas.edges if key == "t" for cell in path}
            only_s = {cell for key, path in canvas.edges if key == "s" for cell in path} - idle
            self.assertTrue(views.animate_edges(canvas, {"s"}, 0))
            styles = {(y, x): canvas.rows[y][x][1] for y, x in only_s}
            self.assertTrue(set(styles.values()) <= {"flow", "pulse"}, render.__name__)
            self.assertIn("pulse", styles.values(), render.__name__)
            self.assertFalse({canvas.rows[y][x][1] for y, x in idle} & {"flow", "pulse"}, render.__name__)

    def test_nothing_lights_without_working_children(self):
        canvas, _ = views.render_graph([team()[0]], 80)
        before = canvas.text()
        self.assertFalse(views.animate_edges(canvas, set(), 3))
        self.assertEqual(canvas.text(), before)

    def test_the_pulse_travels_from_parent_to_child(self):
        def pulses(frame):
            canvas, _ = views.render_graph([team()[0]], 80)
            views.animate_edges(canvas, {"t"}, frame)
            path = dict(canvas.edges)["t"]
            return [i for i, (y, x) in enumerate(path) if canvas.rows[y][x][1] == "pulse"], len(path)

        (first, length), (second, _) = pulses(0), pulses(1)
        self.assertTrue(first)
        self.assertEqual(second, [i + 1 for i in first if i + 1 < length])  # one cell further from the parent
        self.assertEqual(pulses(0), pulses(views.PULSE_PERIOD))

    def test_pulse_cells_turn_heavy_and_keep_their_shape(self):
        canvas, _ = views.render_graph([team()[0]], 80)
        views.animate_edges(canvas, {"s", "q", "t"}, 0)
        for key, path in canvas.edges:
            for y, x in path:
                ch, style = canvas.rows[y][x]
                if style == "pulse":
                    self.assertIn(ch, "┃━┳┻╋┏┓┗┛┣┫")


class ActivityPlotTest(unittest.TestCase):
    @staticmethod
    def spark(counts, peak):
        return "".join("#" if c else "." for c in counts)

    def test_one_lane_per_agent_with_axis_and_window(self):
        o, s, q, t = team()
        s.selected = True
        lanes = [(o, [1, 0, 2] * 10), (s, [0] * 30), (q, None)]
        canvas, boxes = views.render_activity(lanes, 40, "30m", self.spark)
        rows = canvas.text().splitlines()
        self.assertIn("ACTIVITY", rows[0])
        self.assertIn("last 30m", rows[0])
        self.assertIn("orchestra…", rows[1])  # names fit a quarter of the width
        self.assertIn("#.#", rows[1])
        self.assertIn("no history yet", rows[3])
        self.assertIn("-30m", rows[4])
        self.assertTrue(rows[4].endswith("now"))
        self.assertEqual(boxes["s"], (2, 2, 0, 39))
        self.assertEqual(canvas.rows[2][3][1], "selname")

    def test_lanes_use_the_agent_kind_colour_and_fit_the_width(self):
        o, s, q, t = team()
        width = 36
        spark_w = views.activity_spark_width([(q, None)], width)
        canvas, _ = views.render_activity([(q, [1] * spark_w)], width, "1h", self.spark)
        row = canvas.rows[1]
        self.assertEqual({style for ch, style in row if ch == "#"}, {"kind:codex"})
        self.assertEqual(sum(ch == "#" for ch, _ in row), spark_w)
        self.assertEqual(len(row), width)


class RouteTest(unittest.TestCase):
    def test_routes_follow_the_edges_up_and_down_the_tree(self):
        o, s, q, t = team()
        canvas, _ = views.render_graph([o], 80)
        edges = dict(canvas.edges)
        self.assertEqual(views.route(canvas, o, s), edges["s"])  # down: parent end first
        self.assertEqual(views.route(canvas, s, o), list(reversed(edges["s"])))  # up
        down_two = views.route(canvas, o, t)
        self.assertEqual(down_two[: len(edges["q"])], edges["q"])
        self.assertEqual(down_two[-1], edges["t"][-1])

    def test_siblings_meet_at_their_common_parent(self):
        o, s, q, t = team()
        canvas, boxes = views.render_graph([o], 80)
        path = views.route(canvas, t, s)
        self.assertEqual(path[0], dict(canvas.edges)["t"][-1])
        self.assertEqual(path[-1], dict(canvas.edges)["s"][-1])
        self.assertIn(dict(canvas.edges)["s"][0], path)  # passes the parent's tee
        self.assertFalse(any(a == b for a, b in zip(path, path[1:])), "shared joints appear once in a row")

    def test_hidden_or_unconnected_ends_have_no_route(self):
        o, s, q, t = team()
        q.collapsed = True
        canvas, _ = views.render_graph([o], 80)
        self.assertEqual(views.route(canvas, t, o), [])
        lone = views.NodeView("x", "lone", "claude", "idle")
        canvas, _ = views.render_graph([team()[0], lone], 120)
        self.assertEqual(views.route(canvas, lone, s), [])

    def test_animate_path_uses_the_flow_style_and_its_pulse(self):
        o, s, q, t = team()
        canvas, _ = views.render_cards([o], 30)
        path = views.route(canvas, s, o)
        self.assertTrue(views.animate_path(canvas, path, 0, "reply"))
        styles = {canvas.rows[y][x][1] for y, x in path}
        self.assertEqual(styles, {"reply", "reply_pulse"})
        self.assertFalse(views.animate_path(canvas, [], 0, "reply"))
