import os
import tempfile
import unittest

from tests import helpers  # noqa: F401
import install

CONFIG = '[keys]\n# prefix = "ctrl+b"\n\n[ui.toast]\n# delivery = "off"\n'


class BlockTest(unittest.TestCase):
    def test_add_then_strip_restores_config(self):
        added, message = install.add_block(CONFIG)
        self.assertIn("added", message)
        self.assertIn(install.BLOCK_START, added)
        self.assertEqual(install.add_block(added)[0], added)  # idempotent
        self.assertEqual(install.strip_block(added).rstrip(), CONFIG.rstrip())

    def test_key_in_use_or_already_bound_is_left_alone(self):
        taken = CONFIG + '[[keys.command]]\nkey = "prefix+m"\ntype = "shell"\ncommand = "x"\n'
        self.assertEqual(install.add_block(taken)[0], taken)
        bound = CONFIG + '[[keys.command]]\nkey = "prefix+t"\ncommand = "horchestra.toggle"\n'
        self.assertEqual(install.add_block(bound)[0], bound)

    def test_commented_key_does_not_count(self):
        self.assertFalse(install.key_in_use('# key = "prefix+m"\n', "prefix+m"))


    def test_pre_rename_block_is_rewritten(self):
        old = CONFIG + ("\n# >>> herdr-orchestra (managed by agent-map.setup; remove with agent-map.teardown)\n"
                        "[[keys.command]]\nkey = \"prefix+m\"\ntype = \"plugin_action\"\n"
                        "command = \"agent-map.toggle\"\ndescription = \"toggle agent map\"\n"
                        "# <<< herdr-orchestra\n")
        new, message = install.add_block(old)
        self.assertIn("updated", message)
        self.assertIn('command = "horchestra.toggle"', new)
        self.assertNotIn("agent-map.toggle", new)
        self.assertEqual(new.count("# >>>"), 1)
        self.assertEqual(install.strip_block(new).rstrip(), CONFIG.rstrip())


class LinkTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        home = self.tmp.name
        self.saved = (install.LINKS, install.BIN_DIR)
        install.BIN_DIR = os.path.join(home, "bin")
        install.LINKS = [("bin/horchestra-team", os.path.join(home, "bin", "horchestra-team")),
                         ("agents/orchestrator.md", os.path.join(home, "agents", "orchestrator.md"))]

    def tearDown(self):
        install.LINKS, install.BIN_DIR = self.saved
        self.tmp.cleanup()

    def test_install_and_teardown_round_trip(self):
        out = []
        install.install_links(out.append)
        for source, dest in install.LINKS:
            self.assertEqual(os.readlink(dest), os.path.join(install.ROOT, source))
        install.remove_links(out.append)
        for _, dest in install.LINKS:
            self.assertFalse(os.path.lexists(dest))

    def test_foreign_files_are_never_replaced(self):
        dest = install.LINKS[1][1]
        os.makedirs(os.path.dirname(dest))
        with open(dest, "w") as fh:
            fh.write("someone else's agent")
        out = []
        install.install_links(out.append)
        install.remove_links(out.append)
        with open(dest) as fh:
            self.assertEqual(fh.read(), "someone else's agent")
        self.assertTrue(any("SKIPPED" in line for line in out))

    def test_legacy_link_is_upgraded(self):
        dest = install.LINKS[1][1]
        os.makedirs(os.path.dirname(dest))
        os.symlink("/old/place/herdr-agent-map/orchestrator.md", dest)
        install.install_links(lambda _line: None)
        self.assertEqual(os.readlink(dest), os.path.join(install.ROOT, "agents/orchestrator.md"))


if __name__ == "__main__":
    unittest.main()
