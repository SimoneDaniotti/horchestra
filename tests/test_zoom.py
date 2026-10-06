import os
import subprocess
import unittest
from unittest import mock

from tests import helpers  # noqa: F401
import zoom

ENV = {"HORCHESTRA_ZOOM_KIND": "claude", "HORCHESTRA_ZOOM_SESSION": "abc-123"}


class ZoomPaneTest(unittest.TestCase):
    def run_main(self, env, zoe="/opt/zoe", returncode=0):
        runs = []

        def fake_run(argv):
            runs.append(argv)
            return subprocess.CompletedProcess(argv, returncode)

        with mock.patch.dict(os.environ, env, clear=True), \
                mock.patch.object(zoom, "find_zoe", return_value=zoe), \
                mock.patch.object(zoom.subprocess, "run", fake_run), \
                mock.patch("builtins.input", return_value="") as asked, \
                mock.patch("builtins.print") as printed:
            code = zoom.main()
        shown = " ".join(str(c.args[0]) for c in printed.call_args_list if c.args)
        return code, runs, asked.called, shown

    def test_follows_the_session_live_in_zoe(self):
        code, runs, held, _ = self.run_main(ENV)
        self.assertEqual((code, held), (0, False))
        self.assertEqual(runs, [["/opt/zoe", "--provider", "claude", "--follow", "abc-123"]])

    def test_missing_zoe_shows_how_to_install_and_waits(self):
        code, runs, held, shown = self.run_main(ENV, zoe=None)
        self.assertEqual((code, runs, held), (1, [], True))
        self.assertIn("brew install furkankly/tap/zoetrope", shown)

    def test_without_a_session_it_explains_instead_of_running(self):
        code, runs, held, shown = self.run_main({"HORCHESTRA_ZOOM_KIND": "pi"})
        self.assertEqual((code, runs, held), (1, [], True))
        self.assertIn("agent map", shown)

    def test_a_failing_zoe_keeps_its_error_on_screen(self):
        code, _, held, shown = self.run_main(ENV, returncode=2)
        self.assertEqual((code, held), (1, True))
        self.assertIn("status 2", shown)
        self.assertEqual(self.run_main(ENV, returncode=130)[2], False)  # ctrl+c is a normal exit

    def test_finds_zoe_outside_a_minimal_path(self):
        with mock.patch.object(zoom.shutil, "which", return_value=None), \
                mock.patch.object(zoom.os.path, "isfile", lambda p: p == "/opt/homebrew/bin/zoe"), \
                mock.patch.object(zoom.os, "access", return_value=True):
            self.assertEqual(zoom.find_zoe(), "/opt/homebrew/bin/zoe")
        with mock.patch.object(zoom.shutil, "which", return_value=None), \
                mock.patch.object(zoom.os.path, "isfile", return_value=False):
            self.assertIsNone(zoom.find_zoe())


if __name__ == "__main__":
    unittest.main()
