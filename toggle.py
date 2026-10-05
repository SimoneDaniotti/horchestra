#!/usr/bin/env python3
"""Toggle the agent map pane in the invoking workspace.

Closes any map pane already in the workspace; otherwise opens one docked at
the left edge of the active tab and narrows it to AGENTMAP_WIDTH columns
(default 32, configurable in $HERDR_PLUGIN_CONFIG_DIR/width).
"""

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import herdr_client as hc  # noqa: E402

DEFAULT_WIDTH = 32


def context():
    try:
        ctx = json.loads(os.environ.get("HERDR_PLUGIN_CONTEXT_JSON") or "{}")
    except ValueError:
        ctx = {}
    return ctx if isinstance(ctx, dict) else {}


def find(ctx, *keys):
    for key in keys:
        value = os.environ.get(key.upper()) or ctx.get(key.lower())
        if isinstance(value, str) and value:
            return value
    return None


def target_width():
    config_dir = os.environ.get("HERDR_PLUGIN_CONFIG_DIR")
    raw = os.environ.get("AGENTMAP_WIDTH")
    if not raw and config_dir:
        try:
            with open(os.path.join(config_dir, "width")) as fh:
                raw = fh.read().strip()
        except OSError:
            raw = None
    try:
        return max(16, int(raw)) if raw else DEFAULT_WIDTH
    except ValueError:
        return DEFAULT_WIDTH


def layout_for(pane_id):
    edges = hc.call("pane", "edges", "--pane", pane_id).get("edges") or {}
    return edges.get("layout") or {}


def rects(layout):
    return {
        p.get("pane_id"): p.get("rect") or {}
        for p in layout.get("panes") or []
        if isinstance(p, dict)
    }


def is_map(pane):
    """Live maps carry a token; maps restored as shells keep only the label."""
    return bool((pane.get("tokens") or {}).get(hc.TOKEN_VIEW) or pane.get("label") == "Agent map")


def map_panes(panes):
    return [p["pane_id"] for p in panes if p.get("pane_id") and is_map(p)]


def agent_tabs(panes):
    return {p.get("tab_id") for p in panes if p.get("agent") and not is_map(p) and p.get("tab_id")}


def ensure_maps(workspace, tabs):
    """Open a map in each of `tabs` that does not have one yet."""
    opened = []
    for tab in sorted(t for t in tabs if t):
        panes = hc.list_panes(workspace)
        if any(is_map(p) and p.get("tab_id") == tab for p in panes):
            continue
        anchor = next((p["pane_id"] for p in panes if p.get("tab_id") == tab and not is_map(p)), None)
        if anchor:
            opened.append(open_map(panes, anchor))
    return opened


def open_map(panes, focused):
    """Open a map docked at the left edge of `focused`'s tab."""
    if not focused or not any(p.get("pane_id") == focused for p in panes):
        focused = next((p["pane_id"] for p in panes if p.get("focused")), None)
        focused = focused or (panes[0]["pane_id"] if panes else None)
    if not focused:
        raise hc.HerdrError("no pane to dock against")

    # Dock against the leftmost, tallest pane of the active tab.
    boxes = rects(layout_for(focused))
    anchor = min(
        boxes,
        key=lambda pid: (boxes[pid].get("x", 0), -boxes[pid].get("height", 0)),
        default=focused,
    )

    # Note: herdr 0.9.x rejects --workspace together with --target-pane.
    opened = hc.call(
        "plugin", "pane", "open",
        "--plugin", os.environ.get("HERDR_PLUGIN_ID", "agent-map"),
        "--entrypoint", "map",
        "--placement", "split",
        "--target-pane", anchor,
        "--direction", "right",
        "--no-focus",
    )
    map_pane = ((opened.get("plugin_pane") or {}).get("pane") or {}).get("pane_id")
    if not map_pane:
        return None
    hc.call_quiet("pane", "swap", "--source-pane", map_pane, "--target-pane", anchor)

    # Narrow the map: shift the shared divider left by the excess width.
    width = target_width()
    for _ in range(3):
        boxes = rects(layout_for(map_pane))
        mine, other = boxes.get(map_pane), boxes.get(anchor)
        if not mine or not other:
            break
        total = mine.get("width", 0) + other.get("width", 0)
        excess = mine.get("width", 0) - width
        if total <= 0 or abs(excess) <= 1:
            break
        direction = "left" if excess > 0 else "right"
        amount = f"{abs(excess) / total:.4f}"
        if hc.call_quiet(
            "pane", "resize", "--direction", direction, "--amount", amount, "--pane", map_pane
        ) is None:
            break
    return map_pane


def main():
    ctx = context()
    workspace = find(ctx, "HERDR_WORKSPACE_ID", "workspace_id")
    focused = find(ctx, "HERDR_PANE_ID", "pane_id", "focused_pane_id")
    if not workspace and focused:
        workspace = hc.get_pane(focused).get("workspace_id")
    if not workspace:
        print("agent-map: no workspace context", file=sys.stderr)
        return 1

    panes = hc.list_panes(workspace)
    existing = map_panes(panes)
    if existing:
        for pane_id in existing:
            if hc.call_quiet("plugin", "pane", "close", pane_id) is None:
                hc.call_quiet("pane", "close", pane_id)
        return 0
    # One map per tab that runs an agent, so every agent tab shows the team.
    tabs = agent_tabs(panes)
    if not tabs:
        tab = next((p.get("tab_id") for p in panes if p.get("pane_id") == focused), None)
        tabs = {tab} if tab else set()
    ensure_maps(workspace, tabs)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except hc.HerdrError as exc:
        print(f"agent-map: {exc}", file=sys.stderr)
        sys.exit(1)
