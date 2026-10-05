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


if __name__ == "__main__":
    unittest.main()
