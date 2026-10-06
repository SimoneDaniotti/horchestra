import os
import stat
import tempfile
import unittest
from unittest import mock

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

    def test_taken_key_or_bound_action_is_skipped_but_others_added(self):
        taken = CONFIG + '[[keys.command]]\nkey = "prefix+m"\ntype = "shell"\ncommand = "x"\n'
        new, message = install.add_block(taken)
        self.assertIn("SKIPPED  prefix+m", message)
        self.assertNotIn('command = "horchestra.toggle"', new)
        self.assertIn('command = "horchestra.overview"', new)
        for bound_as in ("horchestra.toggle", "agent-map.toggle"):
            bound = CONFIG + '[[keys.command]]\nkey = "prefix+t"\ncommand = "%s"\n' % bound_as
            new, _ = install.add_block(bound)
            self.assertNotIn('key = "prefix+m"', new)
            self.assertIn('key = "prefix+shift+m"', new)
        everything = taken + '[[keys.command]]\nkey = "prefix+shift+m"\ncommand = "y"\n'
        self.assertEqual(install.add_block(everything)[0], everything)

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


def make_plugin_root(path, plugin_id="horchestra",
                     files=("bin/horchestra-team", "bin/agentmap-team", "agents/orchestrator.md")):
    """A fake plugin checkout: a manifest with `plugin_id` plus the linked files."""
    os.makedirs(path, exist_ok=True)
    with open(os.path.join(path, "herdr-plugin.toml"), "w") as fh:
        fh.write(f'id = "{plugin_id}"\nname = "x"\n')
    for rel in files:
        os.makedirs(os.path.dirname(os.path.join(path, rel)), exist_ok=True)
        with open(os.path.join(path, rel), "w") as fh:
            fh.write(rel)
    return path


class LinkTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        home = self.tmp.name
        self.home = home
        self.saved_root = install.ROOT
        install.ROOT = make_plugin_root(os.path.join(home, "plugin"))
        self.saved = (install.LINKS, install.BIN_DIR)
        install.BIN_DIR = os.path.join(home, "bin")
        install.LINKS = [("bin/horchestra-team", os.path.join(home, "bin", "horchestra-team")),
                         ("agents/orchestrator.md", os.path.join(home, "agents", "orchestrator.md"))]

        self.saved_legacy = install.LEGACY_LINKS
        install.LEGACY_LINKS = [("bin/agentmap-team", os.path.join(home, "bin", "agentmap-team"))]

    def tearDown(self):
        install.LINKS, install.BIN_DIR = self.saved
        install.LEGACY_LINKS = self.saved_legacy
        install.ROOT = self.saved_root
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


    def test_legacy_alias_only_refreshed_when_already_installed(self):
        alias = install.LEGACY_LINKS[0][1]
        install.install_links(lambda _line: None)
        self.assertFalse(os.path.lexists(alias))  # fresh install: no alias
        os.makedirs(os.path.dirname(alias), exist_ok=True)
        old = make_plugin_root(os.path.join(self.home, "old-checkout"), "agent-map", files=())
        os.symlink(os.path.join(old, "bin/agentmap-team"), alias)  # dangling, in our checkout
        install.install_links(lambda _line: None)
        self.assertEqual(os.readlink(alias), os.path.join(install.ROOT, "bin/agentmap-team"))
        install.remove_links(lambda _line: None)
        self.assertFalse(os.path.lexists(alias))

    def _link(self, dest, target):
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        os.symlink(target, dest)

    def test_foreign_symlink_with_matching_suffix_is_kept(self):
        dotfiles = os.path.join(self.home, "dotfiles", "claude")
        os.makedirs(os.path.join(dotfiles, "agents"))
        mine = os.path.join(dotfiles, "agents", "orchestrator.md")
        with open(mine, "w") as fh:
            fh.write("mine")
        other = make_plugin_root(os.path.join(self.home, "opt", "other"), "other-plugin")
        team_dest, agent_dest = install.LINKS[0][1], install.LINKS[1][1]
        self._link(agent_dest, mine)
        self._link(team_dest, os.path.join(other, "bin/horchestra-team"))
        alias = install.LEGACY_LINKS[0][1]
        self._link(alias, "/nowhere/bin/agentmap-team")  # dangling, no manifest
        out = []
        install.install_links(out.append)
        install.remove_links(out.append)
        self.assertEqual(os.readlink(agent_dest), mine)
        self.assertEqual(os.readlink(team_dest), os.path.join(other, "bin/horchestra-team"))
        self.assertEqual(os.readlink(alias), "/nowhere/bin/agentmap-team")
        self.assertEqual(sum("SKIPPED" in line for line in out), 2)
        self.assertEqual(sum("kept" in line for line in out), 3)

    def test_link_into_another_horchestra_checkout_is_replaced_and_removed(self):
        other = make_plugin_root(os.path.join(self.home, "older", "horchestra"))
        dest = install.LINKS[0][1]
        self._link(dest, os.path.join(other, "bin/horchestra-team"))
        install.install_links(lambda _line: None)
        self.assertEqual(os.readlink(dest), os.path.join(install.ROOT, "bin/horchestra-team"))
        os.remove(dest)
        self._link(dest, os.path.join(other, "bin/horchestra-team"))
        install.remove_links(lambda _line: None)
        self.assertFalse(os.path.lexists(dest))

    def test_link_resolving_into_plugin_through_a_symlinked_dir_is_ours(self):
        via = os.path.join(self.home, "via")
        os.symlink(install.ROOT, via)
        dest = install.LINKS[0][1]
        self._link(dest, os.path.join(via, "bin", "horchestra-team"))
        self.assertTrue(install.is_ours(dest, "bin/horchestra-team"))

    def test_legacy_suffix_only_counts_when_dangling(self):
        legacy_dir = os.path.join(self.home, "dev", "herdr-agent-map")
        os.makedirs(legacy_dir)
        target = os.path.join(legacy_dir, "orchestrator.md")
        with open(target, "w") as fh:
            fh.write("a live file someone kept")
        dest = install.LINKS[1][1]
        self._link(dest, target)
        self.assertFalse(install.is_ours(dest, "agents/orchestrator.md"))
        os.remove(target)
        self.assertTrue(install.is_ours(dest, "agents/orchestrator.md"))
        self.assertFalse(install.is_ours(dest, "bin/horchestra-team"))


class ConfigWriteTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.tmp.name, "herdr", "config.toml")
        self.env = mock.patch.dict(os.environ, {"XDG_CONFIG_HOME": self.tmp.name})
        self.env.start()
        self.reload = mock.patch.object(install, "reload_config")
        self.reload.start()

    def tearDown(self):
        self.reload.stop()
        self.env.stop()
        self.tmp.cleanup()

    def _write(self, text, mode=0o600):
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        with open(self.path, "w") as fh:
            fh.write(text)
        os.chmod(self.path, mode)

    def _read(self):
        with open(self.path) as fh:
            return fh.read()

    def test_install_and_teardown_preserve_mode_and_leave_no_temp_files(self):
        self._write(CONFIG, 0o640)
        install.install_binding(lambda _line: None)
        self.assertIn(install.BLOCK_START, self._read())
        self.assertEqual(stat.S_IMODE(os.stat(self.path).st_mode), 0o640)
        install.remove_binding(lambda _line: None)
        self.assertEqual(self._read().rstrip(), CONFIG.rstrip())
        self.assertEqual(stat.S_IMODE(os.stat(self.path).st_mode), 0o640)
        self.assertEqual(os.listdir(os.path.dirname(self.path)), ["config.toml"])

    def test_new_config_is_created(self):
        install.install_binding(lambda _line: None)
        self.assertIn(install.BLOCK_START, self._read())

    def test_symlinked_config_is_written_through(self):
        real = os.path.join(self.tmp.name, "dotfiles.toml")
        with open(real, "w") as fh:
            fh.write(CONFIG)
        os.makedirs(os.path.dirname(self.path))
        os.symlink(real, self.path)
        install.install_binding(lambda _line: None)
        self.assertTrue(os.path.islink(self.path))
        with open(real) as fh:
            self.assertIn(install.BLOCK_START, fh.read())

    @unittest.skipIf(install.tomllib is None, "needs tomllib")
    def test_invalid_toml_result_is_refused(self):
        self._write(CONFIG)
        out = []
        self.assertFalse(install.write_config(self.path, CONFIG, CONFIG + "[[broken\n", out.append))
        self.assertEqual(self._read(), CONFIG)
        self.assertTrue(any(line.startswith("ERROR") for line in out))
        # Already-invalid configs are not blocked by the guard.
        self.assertTrue(install.write_config(self.path, "[[x", "[[x\n", out.append))

    def test_failed_replace_keeps_original_and_cleans_up(self):
        self._write(CONFIG)
        out = []
        with mock.patch.object(install.os, "replace", side_effect=OSError("disk full")):
            self.assertFalse(install.write_config(self.path, CONFIG, CONFIG + "\n", out.append))
        self.assertEqual(self._read(), CONFIG)
        self.assertEqual(os.listdir(os.path.dirname(self.path)), ["config.toml"])
        self.assertTrue(any("disk full" in line for line in out))


class ConfigDirAndWriteErrorTest(unittest.TestCase):
    def test_agents_dir_follows_claude_config_dir(self):
        import importlib
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": tmp}):
                fresh = importlib.reload(install)
                self.assertEqual(fresh.AGENTS_DIR, os.path.join(tmp, "agents"))
                dests = [d for _s, d in fresh.LEGACY_LINKS]
                self.assertIn(os.path.join(fresh.DEFAULT_AGENTS_DIR, "orchestrator.md"), dests)
        importlib.reload(install)  # back to the real environment for other tests

    def test_unwritable_config_dir_is_reported_not_raised(self):
        out = []
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "config.toml")
            with open(path, "w") as fh:
                fh.write("[keys]\n")
            with mock.patch("tempfile.mkstemp", side_effect=PermissionError("denied")):
                self.assertFalse(install.write_config(path, "[keys]\n", "[keys]\nx = 1\n", out.append))
            with open(path) as fh:
                self.assertEqual(fh.read(), "[keys]\n")
        self.assertTrue(any("ERROR" in line for line in out))


if __name__ == "__main__":
    unittest.main()
