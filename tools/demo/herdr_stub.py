#!/usr/bin/env python3
"""A stand-in `herdr` CLI for the README demo: answers the map's read calls
from the JSON state file that run_demo.py keeps rewriting (DEMO_STATE).

Only what the agent map asks for is implemented; writes are acknowledged and
ignored. Output matches the real CLI's shape: {"id", "result"} on stdout.
"""

import json
import os
import sys


def main(args):
    with open(os.environ["DEMO_STATE"]) as fh:
        state = json.load(fh)
    panes = state["panes"]
    by_id = {p["pane_id"]: p for p in panes}
    head = args[:2]
    if head == ["pane", "read"]:
        pane = by_id.get(args[2], {})
        sys.stdout.write(pane.get("screen", "") + "\n")
        return 0
    if head == ["pane", "get"]:
        result = {"type": "pane_info", "pane": by_id.get(args[2], {"pane_id": args[2]})}
    elif head == ["pane", "list"]:
        result = {"type": "pane_list", "panes": panes}
    elif head == ["workspace", "get"]:
        result = {"type": "workspace_info", "workspace": state["workspace"]}
    elif head == ["workspace", "list"]:
        result = {"type": "workspace_list", "workspaces": [state["workspace"]]}
    elif head == ["tab", "list"]:
        result = {"type": "tab_list", "tabs": state["tabs"]}
    else:  # report-metadata and anything else the map writes
        result = {}
    print(json.dumps({"id": "cli:demo", "result": result}))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
