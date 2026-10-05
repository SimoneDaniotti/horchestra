import os
import tempfile
import unittest

from tests import helpers  # noqa: F401
import team


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


class NameTest(unittest.TestCase):
    def test_agent_names_are_valid_and_stable(self):
        space = team.Space.__new__(team.Space)
        space.root = "/projects/Web Shop"
        name = space.agent_name("Research_Lead")
        self.assertRegex(name, r"^[a-z][a-z0-9_-]{0,31}$")
        self.assertEqual(name, space.agent_name("Research_Lead"))
        space.root = "/projects/123-numbers-first-and-a-very-long-directory-name"
        self.assertRegex(space.agent_name("QA"), r"^[a-z][a-z0-9_-]{0,31}$")


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


class TabNameTest(unittest.TestCase):
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
