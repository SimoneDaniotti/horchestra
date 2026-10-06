import io
import json
import os
import tempfile
import unittest
from unittest import mock

from tests import helpers  # noqa: F401
import herdr_client as hc
import toggle


def pane(pid, tab="t1", agent=None, label=None, view=None, focused=False):
    p = {"pane_id": pid, "tab_id": tab, "tokens": {}}
    if agent:
        p["agent"] = agent
    if label:
        p["label"] = label
    if view:
        p["tokens"][hc.TOKEN_VIEW] = view
    if focused:
        p["focused"] = True
    return p


def rect(x, y, w, h):
    return {"x": x, "y": y, "width": w, "height": h}


def opened(pid):
    return {"plugin_pane": {"pane": {"pane_id": pid}}}


class Recorder:
    """Records herdr calls; `opens` is the queue of new pane ids to return."""

    def __init__(self, opens=()):
        self.calls = []
        self.opens = list(opens)

    def call(self, *args, **kw):
        self.calls.append(args)
        if args[:3] == ("plugin", "pane", "open"):
            return opened(self.opens.pop(0)) if self.opens else {}
        return {}

    def quiet(self, *args, **kw):
        self.calls.append(args)
        return {}

    def named(self, *prefix):
        return [c for c in self.calls if c[:len(prefix)] == prefix]


def patched(rec, **extra):
    stack = [
        mock.patch.object(toggle.hc, "call", rec.call),
        mock.patch.object(toggle.hc, "call_quiet", rec.quiet),
    ]
    for name, value in extra.items():
        stack.append(mock.patch.object(toggle, name, value))
    return stack


class Patched(unittest.TestCase):
    def use(self, patches):
        for p in patches:
            p.start()
            self.addCleanup(p.stop)


class ClassifyTest(unittest.TestCase):
    def test_map_needs_token_one(self):
        self.assertTrue(toggle.is_map(pane("a", view="1")))
        self.assertFalse(toggle.is_map(pane("a", view="all")))
        self.assertFalse(toggle.is_map(pane("a")))
        self.assertFalse(toggle.is_map({"pane_id": "a", "tokens": None}))

    def test_label_alone_is_not_a_map_or_overview(self):
        self.assertFalse(toggle.is_map(pane("a", label=toggle.MAP_LABEL)))
        self.assertFalse(toggle.is_map(pane("a", label="Agent map")))
        self.assertFalse(toggle.is_overview(pane("a", label=toggle.OVERVIEW_LABEL)))

    def test_overview_needs_token_all(self):
        self.assertTrue(toggle.is_overview(pane("a", view="all")))
        self.assertFalse(toggle.is_overview(pane("a", view="1")))
        self.assertFalse(toggle.is_overview(pane("a")))

    def test_map_panes_and_agent_tabs(self):
        panes = [
            pane("a", tab="t1", agent="claude"),
            pane("m", tab="t1", agent="claude", view="1"),
            pane("b", tab="t2", agent="codex"),
            pane("s", tab="t3"),
            pane("o", tab="t4", view="all"),
            {"tab_id": "t5", "agent": "x"},
            {"pane_id": "n", "agent": "x"},
        ]
        self.assertEqual(toggle.map_panes(panes), ["m"])
        self.assertEqual(toggle.agent_tabs(panes), {"t1", "t2", "t5"})


class WidthTest(unittest.TestCase):
    def env(self, **kw):
        base = {k: v for k, v in os.environ.items()
                if k not in ("HORCHESTRA_WIDTH", "AGENTMAP_WIDTH", "HERDR_PLUGIN_CONFIG_DIR")}
        base.update(kw)
        return mock.patch.dict(os.environ, base, clear=True)

    def test_default(self):
        with self.env():
            self.assertEqual(toggle.target_width(), toggle.DEFAULT_WIDTH)

    def test_env_and_legacy_env(self):
        with self.env(HORCHESTRA_WIDTH="40"):
            self.assertEqual(toggle.target_width(), 40)
        with self.env(AGENTMAP_WIDTH="25"):
            self.assertEqual(toggle.target_width(), 25)
        with self.env(HORCHESTRA_WIDTH="50", AGENTMAP_WIDTH="25"):
            self.assertEqual(toggle.target_width(), 50)

    def test_minimum_and_bad_values(self):
        with self.env(HORCHESTRA_WIDTH="3"):
            self.assertEqual(toggle.target_width(), 16)
        with self.env(HORCHESTRA_WIDTH="-9"):
            self.assertEqual(toggle.target_width(), 16)
        with self.env(HORCHESTRA_WIDTH="wide"):
            self.assertEqual(toggle.target_width(), toggle.DEFAULT_WIDTH)

    def test_config_file(self):
        with tempfile.TemporaryDirectory() as d:
            with open(os.path.join(d, "width"), "w") as fh:
                fh.write(" 44\n")
            with self.env(HERDR_PLUGIN_CONFIG_DIR=d):
                self.assertEqual(toggle.target_width(), 44)
            with self.env(HERDR_PLUGIN_CONFIG_DIR=d, HORCHESTRA_WIDTH="20"):
                self.assertEqual(toggle.target_width(), 20)
            with open(os.path.join(d, "width"), "w") as fh:
                fh.write("junk")
            with self.env(HERDR_PLUGIN_CONFIG_DIR=d):
                self.assertEqual(toggle.target_width(), toggle.DEFAULT_WIDTH)

    def test_missing_config_file(self):
        with tempfile.TemporaryDirectory() as d:
            with self.env(HERDR_PLUGIN_CONFIG_DIR=d):
                self.assertEqual(toggle.target_width(), toggle.DEFAULT_WIDTH)


class ContextTest(unittest.TestCase):
    def test_context_tolerates_garbage(self):
        for raw in ("", "{", "[1]", "null"):
            with mock.patch.dict(os.environ, {"HERDR_PLUGIN_CONTEXT_JSON": raw}):
                self.assertEqual(toggle.context(), {})
        with mock.patch.dict(os.environ, {"HERDR_PLUGIN_CONTEXT_JSON": '{"pane_id": "p"}'}):
            self.assertEqual(toggle.context(), {"pane_id": "p"})

    def test_find_prefers_env_then_context(self):
        ctx = {"pane_id": "from-ctx", "workspace_id": 5}
        with mock.patch.dict(os.environ, {"HERDR_PANE_ID": "from-env"}):
            self.assertEqual(toggle.find(ctx, "HERDR_PANE_ID", "pane_id"), "from-env")
        env = {k: v for k, v in os.environ.items() if not k.startswith("HERDR_")}
        with mock.patch.dict(os.environ, env, clear=True):
            self.assertEqual(toggle.find(ctx, "HERDR_PANE_ID", "pane_id"), "from-ctx")
            self.assertIsNone(toggle.find(ctx, "HERDR_WORKSPACE_ID", "workspace_id"))


class RectsTest(Patched):
    def test_layout_and_rects(self):
        layout = {"panes": [{"pane_id": "a", "rect": rect(0, 0, 10, 5)}, {"pane_id": "b"}, "junk"]}
        self.assertEqual(toggle.rects(layout), {"a": rect(0, 0, 10, 5), "b": {}})
        self.assertEqual(toggle.rects({}), {})
        reply = {"edges": {"layout": layout}}
        self.use(patched(Recorder()))
        with mock.patch.object(toggle.hc, "call", lambda *a, **k: reply):
            self.assertEqual(toggle.layout_for("a"), layout)
        with mock.patch.object(toggle.hc, "call", lambda *a, **k: {}):
            self.assertEqual(toggle.layout_for("a"), {})


class PlainShellTest(Patched):
    def check(self, result):
        with mock.patch.object(toggle.hc, "call_quiet", lambda *a, **k: result):
            return toggle.is_plain_shell("p")

    def procs(self, *names):
        return {"process_info": {"foreground_processes": [{"name": n} for n in names]}}

    def test_shells(self):
        self.assertTrue(self.check(self.procs("-zsh")))
        self.assertTrue(self.check(self.procs("/bin/bash")))
        self.assertTrue(self.check(self.procs("PowerShell.exe")))

    def test_non_shells_and_unknown(self):
        self.assertFalse(self.check(self.procs("zsh", "claude")))
        self.assertFalse(self.check(self.procs("vim")))
        self.assertFalse(self.check(None))
        self.assertFalse(self.check({}))
        self.assertFalse(self.check(self.procs()))
        self.assertFalse(self.check({"process_info": {"foreground_processes": [{"name": 3}]}}))
        self.assertFalse(self.check({"process_info": {"foreground_processes": ["zsh"]}}))


class DeadViewsTest(unittest.TestCase):
    def test_only_idle_shells_with_label_and_no_token(self):
        panes = [
            pane("dead", label=toggle.MAP_LABEL),
            pane("live", label=toggle.MAP_LABEL, view="1"),
            pane("agent", label=toggle.MAP_LABEL, agent="claude"),
            pane("busy", label=toggle.MAP_LABEL),
            pane("other", label="something"),
            {"label": toggle.MAP_LABEL},
        ]
        with mock.patch.object(toggle, "is_plain_shell", lambda pid: pid != "busy"):
            self.assertEqual(toggle.dead_views(panes, toggle.MAP_LABEL), ["dead"])


class CloseViewTest(Patched):
    def test_plugin_close_then_fallback(self):
        rec = Recorder()
        self.use(patched(rec))
        toggle.close_view("p1")
        self.assertEqual(rec.calls, [("plugin", "pane", "close", "p1")])

        calls = []
        def quiet(*a, **k):
            calls.append(a)
            return None if a[0] == "plugin" else {}
        with mock.patch.object(toggle.hc, "call_quiet", quiet):
            toggle.close_view("p2")
        self.assertEqual(calls, [("plugin", "pane", "close", "p2"), ("pane", "close", "p2")])


class OpenMapTest(Patched):
    def setUp(self):
        self.env = mock.patch.dict(os.environ, {"HORCHESTRA_WIDTH": "32"})
        self.env.start()
        self.addCleanup(self.env.stop)

    def layouts(self, *seq):
        seq = list(seq)
        def layout_for(pid):
            return seq.pop(0) if len(seq) > 1 else seq[0]
        return layout_for

    @staticmethod
    def lay(**boxes):
        return {"panes": [{"pane_id": k, "rect": v} for k, v in boxes.items()]}

    def test_docks_on_leftmost_tallest_and_narrows_left(self):
        rec = Recorder(opens=["M"])
        first = self.lay(a=rect(0, 0, 100, 20), b=rect(0, 20, 100, 10), c=rect(100, 0, 50, 30))
        after_open = self.lay(M=rect(0, 0, 60, 20), a=rect(60, 0, 40, 20))
        done = self.lay(M=rect(0, 0, 32, 20), a=rect(32, 0, 68, 20))
        self.use(patched(rec, layout_for=self.layouts(first, after_open, done)))
        panes = [pane("a", focused=True), pane("b"), pane("c")]
        self.assertEqual(toggle.open_map(panes, "c"), "M")
        opened_call = rec.named("plugin", "pane", "open")[0]
        self.assertIn("a", opened_call[opened_call.index("--target-pane") + 1:][:1])
        self.assertIn("map", opened_call)
        self.assertEqual(rec.named("pane", "swap"),
                         [("pane", "swap", "--source-pane", "M", "--target-pane", "a")])
        resize = rec.named("pane", "resize")
        self.assertEqual(resize, [("pane", "resize", "--direction", "left",
                                   "--amount", "0.2800", "--pane", "M")])

    def test_grows_right_when_too_narrow(self):
        rec = Recorder(opens=["M"])
        first = self.lay(a=rect(0, 0, 100, 20))
        narrow = self.lay(M=rect(0, 0, 20, 20), a=rect(20, 0, 80, 20))
        done = self.lay(M=rect(0, 0, 32, 20), a=rect(32, 0, 68, 20))
        self.use(patched(rec, layout_for=self.layouts(first, narrow, done)))
        toggle.open_map([pane("a")], "a")
        self.assertEqual(rec.named("pane", "resize")[0][3:7], ("right", "--amount", "0.1200", "--pane"))

    def test_no_resize_within_one_column(self):
        rec = Recorder(opens=["M"])
        first = self.lay(a=rect(0, 0, 100, 20))
        near = self.lay(M=rect(0, 0, 33, 20), a=rect(33, 0, 67, 20))
        self.use(patched(rec, layout_for=self.layouts(first, near)))
        toggle.open_map([pane("a")], "a")
        self.assertEqual(rec.named("pane", "resize"), [])

    def test_resize_gives_up_after_three_tries_and_on_failure(self):
        rec = Recorder(opens=["M"])
        first = self.lay(a=rect(0, 0, 100, 20))
        wide = self.lay(M=rect(0, 0, 60, 20), a=rect(60, 0, 40, 20))
        self.use(patched(rec, layout_for=self.layouts(first, wide)))
        toggle.open_map([pane("a")], "a")
        self.assertEqual(len(rec.named("pane", "resize")), 3)

        rec2 = Recorder(opens=["M"])
        rec2.quiet = lambda *a, **k: (rec2.calls.append(a), None if a[1] == "resize" else {})[1]
        self.use(patched(rec2, layout_for=self.layouts(first, wide)))
        toggle.open_map([pane("a")], "a")
        self.assertEqual(len(rec2.named("pane", "resize")), 1)

    def test_missing_rects_stop_resizing(self):
        rec = Recorder(opens=["M"])
        self.use(patched(rec, layout_for=lambda pid: self.lay(a=rect(0, 0, 100, 20))))
        self.assertEqual(toggle.open_map([pane("a")], "a"), "M")
        self.assertEqual(rec.named("pane", "resize"), [])

    def test_unknown_focus_falls_back_to_focused_then_first(self):
        rec = Recorder(opens=["M", "N"])
        seen = []
        def layout_for(pid):
            seen.append(pid)
            return {}
        self.use(patched(rec, layout_for=layout_for))
        toggle.open_map([pane("a"), pane("b", focused=True)], "ghost")
        toggle.open_map([pane("a"), pane("b")], None)
        self.assertEqual(seen[0], "b")
        self.assertEqual(rec.named("plugin", "pane", "open")[0][
            rec.named("plugin", "pane", "open")[0].index("--target-pane") + 1], "b")
        self.assertIn("a", seen)

    def test_no_panes_raises(self):
        self.use(patched(Recorder()))
        with self.assertRaises(hc.HerdrError):
            toggle.open_map([], None)

    def test_open_without_pane_id_returns_none(self):
        rec = Recorder(opens=[])
        self.use(patched(rec, layout_for=lambda pid: {}))
        self.assertIsNone(toggle.open_map([pane("a")], "a"))
        self.assertEqual(rec.named("pane", "swap"), [])


class EnsureMapsTest(Patched):
    def test_opens_only_for_tabs_without_map(self):
        panes = [
            pane("a1", tab="t1", agent="claude"),
            pane("m1", tab="t1", view="1"),
            pane("a2", tab="t2", agent="codex"),
            pane("s2", tab="t2"),
        ]
        opened_for = []
        def fake_open(ps, anchor):
            opened_for.append(anchor)
            return "new-" + anchor
        with mock.patch.object(toggle.hc, "list_panes", lambda ws: panes), \
                mock.patch.object(toggle, "open_map", fake_open):
            result = toggle.ensure_maps("w1", {"t1", "t2", "", None})
        self.assertEqual(opened_for, ["a2"])
        self.assertEqual(result, ["new-a2"])

    def test_anchor_never_a_map_and_tab_without_panes_skipped(self):
        panes = [pane("m", tab="t1", view="all"), pane("x", tab="t1")]
        anchors = []
        with mock.patch.object(toggle.hc, "list_panes", lambda ws: panes), \
                mock.patch.object(toggle, "open_map", lambda ps, a: anchors.append(a) or a):
            toggle.ensure_maps("w1", {"t1", "gone"})
        self.assertEqual(len(anchors), 1)  # "gone" has no panes: skipped

    def test_rechecks_tabs_between_opens(self):
        states = [[pane("a", tab="t1", agent="c")],
                  [pane("a", tab="t1", agent="c"), pane("m", tab="t1", view="1")]]
        opened_for = []
        with mock.patch.object(toggle.hc, "list_panes", lambda ws: states.pop(0)), \
                mock.patch.object(toggle, "open_map", lambda ps, a: opened_for.append(a)):
            toggle.ensure_maps("w1", ["t1", "t1"])
        self.assertEqual(opened_for, ["a"])


class ToggleOverviewTest(Patched):
    def run_overview(self, panes, focused, rec, layouts):
        """Run toggle_overview with `pane list` returning `panes`."""
        base_call = rec.call
        def call(*a, **k):
            if a == ("pane", "list"):
                rec.calls.append(a)
                return {"panes": panes}
            return base_call(*a, **k)
        rec.call = call
        seq = list(layouts)
        def layout_for(pid):
            return seq.pop(0) if len(seq) > 1 else seq[0]
        self.use(patched(rec, layout_for=layout_for))
        return toggle.toggle_overview(focused)

    @staticmethod
    def lay(area_h, **boxes):
        return {"area": {"height": area_h},
                "panes": [{"pane_id": k, "rect": v} for k, v in boxes.items()]}

    def test_closes_existing_overviews(self):
        rec = Recorder()
        panes = [pane("o1", view="all"), pane("m", view="1"), pane("o2", label=toggle.OVERVIEW_LABEL),
                 pane("a", agent="claude")]
        with mock.patch.object(toggle, "is_plain_shell", lambda pid: True):
            self.assertEqual(self.run_overview(panes, "a", rec, [{}]), 0)
        closed = [c[3] for c in rec.named("plugin", "pane", "close")]
        self.assertEqual(sorted(closed), ["o1", "o2"])
        self.assertEqual(rec.named("plugin", "pane", "open"), [])

    def test_opens_below_widest_bottom_pane_that_is_not_a_map(self):
        rec = Recorder(opens=["O"])
        panes = [pane("m", view="1"), pane("a"), pane("b"), pane("c")]
        first = self.lay(100, m=rect(0, 0, 90, 100), a=rect(0, 0, 30, 100),
                         b=rect(30, 50, 20, 50), c=rect(50, 50, 40, 50))
        # m is the widest bottom pane but a map column; c wins among the rest.
        first["panes"][1]["rect"] = rect(0, 50, 30, 50)
        done = self.lay(100, O=rect(50, 55, 40, 45), c=rect(50, 50, 40, 5))
        start = self.lay(100, O=rect(50, 75, 40, 25), c=rect(50, 50, 40, 25))
        self.assertEqual(self.run_overview(panes, "a", rec, [first, start, done]), 0)
        call = rec.named("plugin", "pane", "open")[0]
        self.assertEqual(call[call.index("--target-pane") + 1], "c")
        self.assertEqual(call[call.index("--direction") + 1], "down")
        self.assertIn("overview", call)
        # target = 45 of 100; mine is 25 high of 50 total -> grow up by 20/50.
        self.assertEqual(rec.named("pane", "resize"),
                         [("pane", "resize", "--direction", "up", "--amount", "0.4000", "--pane", "O")])

    def test_shrinks_down_when_too_tall(self):
        rec = Recorder(opens=["O"])
        panes = [pane("a")]
        first = self.lay(100, a=rect(0, 0, 80, 100))
        tall = self.lay(100, O=rect(0, 25, 80, 75), a=rect(0, 0, 80, 25))
        done = self.lay(100, O=rect(0, 55, 80, 45), a=rect(0, 0, 80, 55))
        self.run_overview(panes, "a", rec, [first, tall, done])
        self.assertEqual(rec.named("pane", "resize"),
                         [("pane", "resize", "--direction", "down", "--amount", "0.3000", "--pane", "O")])

    def test_minimum_height_and_area_fallback(self):
        rec = Recorder(opens=["O"])
        first = {"panes": [{"pane_id": "a", "rect": rect(0, 0, 80, 20)}]}  # no area: use bottom
        # target = max(14, int(20 * .45)) = 14; mine 6 of 12 -> up by 8/12
        mid = self.lay(20, O=rect(0, 14, 80, 6), a=rect(0, 0, 80, 6))
        mid["panes"][1]["rect"] = rect(0, 0, 80, 6)
        self.run_overview([pane("a")], "a", rec, [first, mid, mid, mid])
        self.assertEqual(rec.named("pane", "resize")[0][5], "0.6667")

    def test_uses_focused_pane_from_list_when_none_given(self):
        rec = Recorder(opens=["O"])
        panes = [pane("a"), pane("b", focused=True)]
        first = self.lay(40, b=rect(0, 0, 80, 40))
        self.run_overview(panes, None, rec, [first, {}])
        call = rec.named("plugin", "pane", "open")[0]
        self.assertEqual(call[call.index("--target-pane") + 1], "b")

    def test_no_focus_anywhere_raises(self):
        with self.assertRaises(hc.HerdrError):
            self.run_overview([pane("a")], None, Recorder(), [{}])

    def test_open_failure_returns_zero_without_resize(self):
        rec = Recorder(opens=[])
        first = self.lay(40, a=rect(0, 0, 80, 40))
        self.assertEqual(self.run_overview([pane("a")], "a", rec, [first]), 0)
        self.assertEqual(rec.named("pane", "resize"), [])


class MainTest(Patched):
    def run_main(self, panes, env, argv=("toggle.py",), rec=None):
        rec = rec or Recorder()
        base = {k: v for k, v in os.environ.items() if not k.startswith("HERDR_")}
        base.update(env)
        self.use([mock.patch.dict(os.environ, base, clear=True),
                  mock.patch.object(toggle.sys, "argv", list(argv)),
                  mock.patch.object(toggle.hc, "list_panes", lambda ws: panes),
                  mock.patch.object(toggle.hc, "call_quiet", rec.quiet),
                  mock.patch.object(toggle, "is_plain_shell", lambda pid: False)])
        return toggle.main(), rec

    def test_closes_maps_when_present(self):
        panes = [pane("a", agent="c"), pane("m", view="1")]
        code, rec = self.run_main(panes, {"HERDR_WORKSPACE_ID": "w1", "HERDR_PANE_ID": "a"})
        self.assertEqual(code, 0)
        self.assertEqual(rec.named("plugin", "pane", "close"), [("plugin", "pane", "close", "m")])

    def test_opens_one_map_per_agent_tab(self):
        panes = [pane("a", tab="t1", agent="c"), pane("b", tab="t2", agent="c")]
        anchors = []
        with mock.patch.object(toggle, "open_map", lambda ps, a: anchors.append(a)):
            code, _ = self.run_main(panes, {"HERDR_WORKSPACE_ID": "w1"})
        self.assertEqual((code, anchors), (0, ["a", "b"]))

    def test_no_agents_uses_focused_tab(self):
        panes = [pane("s1", tab="t1"), pane("s2", tab="t2")]
        anchors = []
        with mock.patch.object(toggle, "open_map", lambda ps, a: anchors.append(a)):
            self.run_main(panes, {"HERDR_WORKSPACE_ID": "w1", "HERDR_PANE_ID": "s2"})
        self.assertEqual(anchors, ["s2"])

    def test_workspace_resolved_from_pane(self):
        panes = [pane("m", view="1")]
        rec = Recorder()
        with mock.patch.object(toggle.hc, "get_pane", lambda pid: {"workspace_id": "w9"}):
            code, rec = self.run_main(panes, {"HERDR_PANE_ID": "m"}, rec=rec)
        self.assertEqual(code, 0)
        self.assertEqual(len(rec.named("plugin", "pane", "close")), 1)

    def test_no_context_returns_error(self):
        with mock.patch.object(toggle.sys, "stderr", io.StringIO()):
            code, _ = self.run_main([], {})
        self.assertEqual(code, 1)

    def test_overview_flag_dispatches(self):
        with mock.patch.object(toggle, "toggle_overview", lambda f: ("ov", f)):
            code, _ = self.run_main([], {"HERDR_PANE_ID": "p"}, argv=("toggle.py", "--overview"))
        self.assertEqual(code, ("ov", "p"))

    def test_context_json_supplies_ids(self):
        ctx = json.dumps({"workspace_id": "w1", "pane_id": "a"})
        panes = [pane("m", view="1")]
        code, rec = self.run_main(panes, {"HERDR_PLUGIN_CONTEXT_JSON": ctx})
        self.assertEqual(code, 0)
        self.assertEqual(len(rec.named("plugin", "pane", "close")), 1)


if __name__ == "__main__":
    unittest.main()
