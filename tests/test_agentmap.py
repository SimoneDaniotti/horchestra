import os
import unittest
import unittest.mock

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


class StubActivity:
    def __init__(self, times):
        self._times = times

    def has_history(self, key):
        return key in self._times

    def times(self, key):
        return list(self._times.get(key, []))


def app_with(panes, overview=False, times=None):
    app = agentmap.App(None, overview=overview)
    app.panes = panes
    app.workspaces = [{"workspace_id": "w1", "label": "api", "number": 1},
                      {"workspace_id": "w2", "label": "web", "number": 2}]
    app.activity = StubActivity(times or {})
    app.rebuild()
    return app


class ActivityLanesTest(unittest.TestCase):
    def setUp(self):
        import time

        self.now = time.time()
        self.panes = [pane("w1:p1", term="t1", agentmap_role="orchestrator"),
                      pane("w1:p2", agentmap_parent="t1", agentmap_role="fe"),
                      dict(pane("w2:p1"), workspace_id="w2")]
        for p in self.panes[:2]:
            p["workspace_id"] = "w1"

    def test_one_lane_per_agent_in_map_order(self):
        app = app_with(self.panes[:2], times={"w1:p1": [self.now], "w1:p2": []})
        lanes = app.lanes(60)
        self.assertEqual([n.name for n, _ in lanes], ["orchestrator", "fe"])
        self.assertEqual(sum(lanes[0][1]), 1)
        self.assertEqual(sum(lanes[1][1]), 0)

    def test_overview_has_one_lane_per_space_summing_its_agents(self):
        app = app_with(self.panes, overview=True,
                       times={"w1:p1": [self.now], "w1:p2": [self.now, self.now]})
        lanes = dict((n.name, c) for n, c in app.lanes(60))
        self.assertEqual(sum(lanes["api"]), 3)
        self.assertIsNone(lanes["web"])  # no history at all

    def test_window_keys_and_toggle(self):
        app = app_with(self.panes[:2])
        app.handle(ord("["))
        app.handle(ord("["))
        self.assertEqual(app.window, 0)
        for _ in range(9):
            app.handle(ord("]"))
        self.assertEqual(app.window, len(agentmap.activity.WINDOWS) - 1)
        app.handle(ord("t"))
        self.assertFalse(app.show_activity)

    def test_lane_window_keeps_the_selected_lane_visible(self):
        app = app_with(self.panes[:2])
        nodes = [agentmap.views.NodeView(f"k{i}", f"n{i}", "claude", "idle") for i in range(10)]
        app.selected = "k8"
        shown = app._lane_window([(n, None) for n in nodes], 3)
        self.assertEqual(len(shown), 3)
        self.assertIn("k8", [n.key for n, _ in shown])


class ZoomTest(unittest.TestCase):
    def zoom(self, raw, selected="w1:p1"):
        app = app_with([])
        app.raw, app.selected = raw, selected
        calls = []
        with unittest.mock.patch.object(hc, "call_quiet", lambda *a, **k: calls.append(a) or {}):
            app.zoom()
        return app.notice[1], calls

    def test_zoom_explains_why_it_cannot_open(self):
        no_session = {"w1:p1": pane("w1:p1")}
        notice, calls = self.zoom(no_session)
        self.assertIn("herdr integration install claude", notice)
        notice, _ = self.zoom({"w1:p1": pane("w1:p1", agent="pi")})
        self.assertIn("Claude and Codex", notice)
        notice, _ = self.zoom({}, selected="ws:w1")
        self.assertIn("select an agent", notice)
        self.assertEqual(calls, [])

    def test_zoom_opens_the_session_over_the_map(self):
        raw = {"w1:p1": dict(pane("w1:p1", agent="codex"),
                             agent_session={"kind": "id", "value": "abc", "agent": "codex", "source": "herdr:codex"})}
        notice, calls = self.zoom(raw)
        self.assertEqual(notice, "")
        self.assertEqual(len(calls), 1)
        self.assertIn("HORCHESTRA_ZOOM_KIND=codex", calls[0])
        self.assertIn("HORCHESTRA_ZOOM_SESSION=abc", calls[0])
        self.assertEqual(calls[0][calls[0].index("--placement") + 1], "overlay")


class AnimationTest(unittest.TestCase):
    def test_animation_can_be_turned_off(self):
        with unittest.mock.patch.dict("os.environ", {"HORCHESTRA_ANIMATE": "0"}):
            self.assertFalse(agentmap.App(None).animate)
        import tempfile

        env = {k: v for k, v in os.environ.items() if k not in ("HORCHESTRA_ANIMATE", "HERDR_PLUGIN_CONFIG_DIR")}
        with unittest.mock.patch.dict("os.environ", env, clear=True):
            self.assertTrue(agentmap.App(None).animate)
        with tempfile.TemporaryDirectory() as config:
            with open(os.path.join(config, "animate"), "w") as fh:
                fh.write("off\n")
            with unittest.mock.patch.dict("os.environ", dict(env, HERDR_PLUGIN_CONFIG_DIR=config), clear=True):
                self.assertFalse(agentmap.App(None).animate)


class RedockTest(unittest.TestCase):
    def test_shift_wasd_moves_the_view_through_a_detached_toggle(self):
        for overview in (False, True):
            app = app_with([pane("w1:pM", tab="w1:t1"), pane("w1:p1", tab="w1:t1")], overview=overview)
            app.panes[0]["tokens"][hc.TOKEN_VIEW] = "1"
            app.own_pane, app.tab_id, app.workspace_id = "w1:pM", "w1:t1", "w1"
            spawned = []
            with unittest.mock.patch.object(agentmap.subprocess, "Popen",
                                            lambda argv, **kw: spawned.append((argv, kw))):
                for key in "WASD":
                    app.handle(ord(key))
            sides = [argv[argv.index("--dock") + 1] for argv, _ in spawned]
            self.assertEqual(sides, ["top", "left", "bottom", "right"])
            argv, kw = spawned[0]
            self.assertEqual(argv[1], agentmap.TOGGLE)
            self.assertEqual("--overview" in argv, overview)
            self.assertTrue(kw["start_new_session"])
            self.assertEqual(kw["env"]["HERDR_PANE_ID"], "w1:p1")  # a pane in this tab, not the map
            self.assertIn("moving to the right", app.notice[1])

    def test_footer_and_help_mention_zoom_and_docking(self):
        keys = dict((k, t) for k, t in agentmap.help_lines(False) if t)
        self.assertIn("z", keys)
        self.assertIn("W A S D (shift)", keys)
        self.assertIn("maps", keys["W A S D (shift)"])
        self.assertIn("overview", dict((k, t) for k, t in agentmap.help_lines(True) if t)["W A S D (shift)"])


class SignalFlowTest(unittest.TestCase):
    def setUp(self):
        self.panes = [pane("w1:p1", term="t1", agentmap_role="orchestrator"),
                      pane("w1:p2", agentmap_parent="t1", agentmap_role="fe"),
                      pane("w1:p3", agentmap_parent="t1", agentmap_role="be")]
        self.app = app_with(self.panes)

    def signal(self, key, kind, to=None, epoch=1_800_000_000):
        next(p for p in self.app.panes if p["pane_id"] == key)["tokens"][hc.TOKEN_SIGNAL] = \
            hc.signal_value(kind, to, now=epoch)

    def test_fresh_signals_become_flows_once(self):
        self.signal("w1:p2", "report")
        self.app.collect_signals(now=1_800_000_002)
        self.app.collect_signals(now=1_800_000_003)  # same signal again: no second flow
        self.assertEqual([f[:3] for f in self.app.flows], [("w1:p2", None, "reply")])
        self.signal("w1:p2", "needs", epoch=1_800_000_010)
        self.signal("w1:p3", "msg", to="w1:p2", epoch=1_800_000_010)
        self.app.collect_signals(now=1_800_000_011)
        self.assertEqual([f[:3] for f in self.app.flows][1:],
                         [("w1:p2", None, "alert"), ("w1:p3", "w1:p2", "message")])

    def test_old_signals_from_before_the_map_opened_are_not_replayed(self):
        self.signal("w1:p2", "report")
        self.app.collect_signals(now=1_800_000_000 + agentmap.SIGNAL_FRESH + 1)
        self.assertEqual(self.app.flows, [])
        self.app.collect_signals(now=1_800_000_000 + agentmap.SIGNAL_FRESH + 2)
        self.assertEqual(self.app.flows, [])

    def test_flows_draw_toward_the_parent_or_receiver_and_expire(self):
        import time

        end = time.monotonic() + 5
        for flow, path_ends in ((("w1:p2", None, "reply", end), ("w1:p2", "w1:p1")),
                                (("w1:p3", "w1:p2", "message", end), ("w1:p3", "w1:p2"))):
            canvas, _ = agentmap.views.render_graph(self.app.roots, 80)
            self.app.flows = [flow]
            self.assertTrue(self.app.draw_flows(canvas, 0))
            path = agentmap.views.route(canvas, *(self.app.node(k) for k in path_ends))
            self.assertEqual({canvas.rows[y][x][1].replace("_pulse", "") for y, x in path}, {flow[2]})
        self.app.flows = [("w1:p2", None, "reply", time.monotonic() - 1), ("ghost", None, "reply", end)]
        canvas, _ = agentmap.views.render_graph(self.app.roots, 80)
        self.assertFalse(self.app.draw_flows(canvas, 0))
        self.assertEqual([f[0] for f in self.app.flows], ["ghost"])  # expired one dropped


class SignalFormatTest(unittest.TestCase):
    def test_round_trip_and_malformed_values(self):
        self.assertEqual(hc.parse_signal(hc.signal_value("msg", "w1:p2", now=5)), ("msg", "w1:p2", 5))
        self.assertEqual(hc.parse_signal(hc.signal_value("report", now=7.9)), ("report", None, 7))
        for bad in (None, "", "msg w1:p2", "shout - 5", "msg - soon", 42):
            self.assertIsNone(hc.parse_signal(bad), bad)
        self.assertLessEqual(len(hc.signal_value("msg", "w123:p456")), 80)  # Herdr token value limit
