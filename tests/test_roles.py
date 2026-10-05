import json
import os
import tempfile
import unittest

from tests import helpers  # noqa: F401
import roles


def make_role(root, name, text="Own the deck.", skills=()):
    folder = os.path.join(root, roles.ROLES_DIR, name)
    os.makedirs(folder)
    if text is not None:
        with open(os.path.join(folder, "ROLE.md"), "w") as fh:
            fh.write(text)
    for skill in skills:
        os.makedirs(os.path.join(folder, ".claude", "skills", skill))
        with open(os.path.join(folder, ".claude", "skills", skill, "SKILL.md"), "w") as fh:
            fh.write("---\nname: %s\ndescription: x\n---\n" % skill)
    return folder


class LoadTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = self.tmp.name

    def tearDown(self):
        self.tmp.cleanup()

    def test_role_folder_is_used_by_default(self):
        make_role(self.root, "slides", skills=["deck-style"])
        team = {"deny_skills": ["legacy:*"]}
        profile = roles.load(self.root, team, {"role": "slides", "uses_skills": ["slide-kit"],
                                               "deny_skills": ["media-kit*"]})
        self.assertEqual(profile.name, "slides")
        self.assertEqual(profile.text, "Own the deck.")
        self.assertEqual(profile.own_skills, ["deck-style"])
        self.assertEqual(profile.deny, ["legacy:*", "media-kit*"])
        self.assertIn("1 own skill", profile.summary())

    def test_no_folder_means_no_profile(self):
        profile = roles.load(self.root, {}, {"role": "qa"})
        self.assertIsNone(profile.folder)
        self.assertTrue(profile.is_empty)
        self.assertIsNone(roles.settings_for(profile))

    def test_errors(self):
        with self.assertRaises(roles.RoleError):
            roles.load(self.root, {}, {"role": "qa", "profile": "missing"})
        with self.assertRaises(roles.RoleError):
            roles.load(self.root, {}, {"role": "qa", "profile": "../escape"})
        with self.assertRaises(roles.RoleError):
            roles.load(self.root, {}, {"role": "qa", "uses_skills": ["media-kit-cli"],
                                       "deny_skills": ["media-kit*"]})


class LaunchTest(unittest.TestCase):
    def test_claude_args_write_agent_and_settings(self):
        with tempfile.TemporaryDirectory() as root, tempfile.TemporaryDirectory() as state:
            folder = make_role(root, "slides", text="Codeword TULIP.", skills=["deck-style"])
            profile = roles.load(root, {}, {"role": "slides", "uses_skills": ["slide-kit"],
                                            "deny_skills": ["graph-tool"]})
            args = roles.claude_args(root, state, "proj-slides", profile, "You are on a team.")
            self.assertEqual(args[:2], ["--agent", "orchestra-slides"])
            self.assertEqual(args[-2:], ["--add-dir", folder])  # variadic flag last
            with open(os.path.join(root, ".claude", "agents", "orchestra-slides.md")) as fh:
                agent = fh.read()
            for expected in ("name: orchestra-slides", "Codeword TULIP.", "`slide-kit`",
                             "`deck-style`", "`graph-tool`", "You are on a team."):
                self.assertIn(expected, agent)
            with open(args[args.index("--settings") + 1]) as fh:
                settings = json.load(fh)
            self.assertEqual(settings["permissions"]["deny"], ["Skill(graph-tool)"])
            self.assertEqual(settings["env"]["CLAUDE_CODE_ADDITIONAL_DIRECTORIES_CLAUDE_MD"], "1")

    def test_agent_names_are_safe(self):
        self.assertEqual(roles.agent_name("Research Lead!"), "orchestra-research-lead")


class OnlySkillsTest(unittest.TestCase):
    def test_allowlist_denies_everything_else_on_disk(self):
        with tempfile.TemporaryDirectory() as root, tempfile.TemporaryDirectory() as user, \
                tempfile.TemporaryDirectory() as state:
            for base, names in ((user, ["graph-tool", "ui-polish", "slide-kit"]),
                                (os.path.join(root, ".claude", "skills"), ["sql-helper", "chart-kit"])):
                for name in names:
                    os.makedirs(os.path.join(base, name))
                    with open(os.path.join(base, name, "SKILL.md"), "w") as fh:
                        fh.write("---\nname: %s\ndescription: x\n---\n" % name)
            make_role(root, "slides", skills=["deck-style"])
            member = {"role": "slides", "only_skills": ["slide-kit", "sql-helper"]}
            profile = roles.load(root, {}, member, user_skills_dir=user)
            self.assertEqual(profile.allowed, ["slide-kit", "sql-helper", "deck-style"])
            self.assertEqual(sorted(profile.auto_deny), ["graph-tool", "ui-polish", "chart-kit"])
            self.assertIn("only 3 skills", profile.summary())
            body = roles.agent_body(profile, "ctx")
            self.assertIn("Use ONLY these skills", body)
            self.assertIn("`deck-style`", body)
            self.assertEqual(roles.settings_for(profile)["permissions"]["deny"],
                             ["Skill(graph-tool)", "Skill(ui-polish)", "Skill(chart-kit)"])

    def test_allowlist_conflicting_with_deny_is_rejected(self):
        with tempfile.TemporaryDirectory() as root:
            with self.assertRaises(roles.RoleError):
                roles.load(root, {"deny_skills": ["lecture-*"]},
                           {"role": "slides", "only_skills": ["slide-kit"]})

    def test_frontmatter_name_wins_over_folder(self):
        with tempfile.TemporaryDirectory() as base:
            os.makedirs(os.path.join(base, "folder-name"))
            with open(os.path.join(base, "folder-name", "SKILL.md"), "w") as fh:
                fh.write('---\nname: "real-name"\ndescription: x\n---\n')
            self.assertEqual(roles.skill_name(os.path.join(base, "folder-name")), "real-name")


if __name__ == "__main__":
    unittest.main()
