#!/usr/bin/env python3
"""Zoom pane: one agent's session as a live flow graph, drawn by zoetrope.

Opened from the agent map with `z`, as an overlay over the agent's pane. The
map passes the agent kind and session id in the environment; zoetrope's `zoe`
does the drawing (https://github.com/furkankly/zoetrope). Closing zoe closes
the overlay and restores the layout.
"""

import os
import shutil
import subprocess
import sys

INSTALL_HINT = "brew install furkankly/tap/zoetrope   (or: cargo install zoetrope)"
# Plugin panes may start with a minimal PATH; look where installers put zoe.
CANDIDATES = ("~/.cargo/bin/zoe", "/opt/homebrew/bin/zoe", "/usr/local/bin/zoe",
              "~/.local/bin/zoe", "/home/linuxbrew/.linuxbrew/bin/zoe")


def find_zoe():
    found = shutil.which("zoe")
    if found:
        return found
    for path in CANDIDATES:
        path = os.path.expanduser(path)
        if os.path.isfile(path) and os.access(path, os.X_OK):
            return path
    return None


def hold(message):
    """Show why the zoom failed, in the pane itself, until the user dismisses it."""
    print(f"\n  {message}\n")
    try:
        input("  press enter to close ")
    except (EOFError, KeyboardInterrupt):
        pass
    return 1


def main():
    kind = os.environ.get("HORCHESTRA_ZOOM_KIND", "")
    session = os.environ.get("HORCHESTRA_ZOOM_SESSION", "")
    if kind not in ("claude", "codex") or not session:
        return hold("nothing to zoom into: open this from the agent map with z")
    zoe = find_zoe()
    if not zoe:
        return hold(f"zoom uses zoetrope, which is not installed:\n\n    {INSTALL_HINT}")
    argv = [zoe, "--provider", kind, "--follow", session]
    try:
        code = subprocess.run(argv).returncode
    except KeyboardInterrupt:
        return 0
    except OSError as exc:
        return hold(f"could not run zoe: {exc}")
    if code not in (0, 130):
        return hold(f"zoe exited with status {code}:  {' '.join(argv[1:])}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
