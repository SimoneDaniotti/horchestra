#!/usr/bin/env python3
"""Toggle the agent map pane in the invoking workspace.

Closes any map pane already in the workspace; otherwise opens one in every
agent tab, docked at the side chosen in the map (shift+W/A/S/D; left by
default) and sized to HORCHESTRA_WIDTH columns (default 32, configurable in
$HERDR_PLUGIN_CONFIG_DIR/width) or, docked top or bottom, `height` rows.

`--dock SIDE` remembers SIDE and moves the open maps there; with
`--overview` it does the same for the all-spaces overview.
"""

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import herdr_client as hc  # noqa: E402

DEFAULT_WIDTH = 32
DEFAULT_HEIGHT = 14
SIDES = ("left", "right", "top", "bottom")
DEFAULT_SIDE = {"map": "left", "overview": "bottom"}
SHRINK = {"left": "left", "right": "right", "top": "up", "bottom": "down"}
GROW = {"left": "right", "right": "left", "top": "down", "bottom": "up"}


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


def read_config(name):
    config_dir = os.environ.get("HERDR_PLUGIN_CONFIG_DIR")
    if not config_dir:
        return None
    try:
        with open(os.path.join(config_dir, name)) as fh:
            return fh.read().strip() or None
    except OSError:
        return None


def target_width():
    raw = os.environ.get("HORCHESTRA_WIDTH") or os.environ.get("AGENTMAP_WIDTH") or read_config("width")
    try:
        return max(16, int(raw)) if raw else DEFAULT_WIDTH
    except ValueError:
        return DEFAULT_WIDTH


def target_height():
    raw = os.environ.get("HORCHESTRA_HEIGHT") or read_config("height")
    try:
        return max(8, int(raw)) if raw else DEFAULT_HEIGHT
    except ValueError:
        return DEFAULT_HEIGHT


def view_side(view):
    """Where `view` ("map" or "overview") docks: the saved choice or its default."""
    side = (read_config(f"{view}_side") or "").lower()
    return side if side in SIDES else DEFAULT_SIDE[view]


def save_side(view, side):
    """Remember the dock side (best effort: no config dir means no memory)."""
    config_dir = os.environ.get("HERDR_PLUGIN_CONFIG_DIR")
    if side not in SIDES or not config_dir:
        return False
    try:
        os.makedirs(config_dir, exist_ok=True)
        with open(os.path.join(config_dir, f"{view}_side"), "w") as fh:
            fh.write(side + "\n")
        return True
    except OSError:
        return False


def layout_for(pane_id):
    edges = hc.call("pane", "edges", "--pane", pane_id).get("edges") or {}
    return edges.get("layout") or {}


def rects(layout):
    return {
        p.get("pane_id"): p.get("rect") or {}
        for p in layout.get("panes") or []
        if isinstance(p, dict)
    }


MAP_LABEL = "horchestra-map"
OVERVIEW_LABEL = "horchestra-overview"
OVERVIEW_SHARE = 0.45  # of the tab's height
# Login shells show as "-zsh"; Windows names carry ".exe".
PLAIN_SHELLS = {"sh", "bash", "zsh", "fish", "dash", "ksh", "mksh", "tcsh", "csh",
                "nu", "xonsh", "elvish", "pwsh", "powershell", "cmd"}


def is_map(pane):
    """A live per-tab map: only the map itself writes this token.

    A label alone is never proof (any pane can carry it, including the
    pre-0.1 "Agent map" title), so label-only panes are not treated as maps.
    """
    return (pane.get("tokens") or {}).get(hc.TOKEN_VIEW) == "1"


def is_overview(pane):
    return (pane.get("tokens") or {}).get(hc.TOKEN_VIEW) == "all"


def is_plain_shell(pane_id):
    """Whether only an idle shell runs in the pane (False when unknown)."""
    info = (hc.call_quiet("pane", "process-info", "--pane", pane_id) or {}).get("process_info")
    procs = info.get("foreground_processes") if isinstance(info, dict) else None
    if not isinstance(procs, list) or not procs:
        return False
    for proc in procs:
        name = proc.get("name") if isinstance(proc, dict) else None
        if not isinstance(name, str):
            return False
        name = os.path.basename(name.lstrip("-")).lower()
        if name.endswith(".exe"):
            name = name[:-4]
        if name not in PLAIN_SHELLS:
            return False
    return True


def dead_views(panes, label):
    """Map/overview panes restored by a Herdr restart as idle shells.

    A restart drops the view token, so these are recognised by their title,
    but only closed when nothing else could be living there: no agent and
    nothing but a plain shell in the foreground.
    """
    return [
        p["pane_id"] for p in panes
        if p.get("pane_id") and p.get("label") == label
        and not (p.get("tokens") or {}).get(hc.TOKEN_VIEW)
        and not p.get("agent") and is_plain_shell(p["pane_id"])
    ]


def close_view(pane_id):
    if hc.call_quiet("plugin", "pane", "close", pane_id) is None:
        hc.call_quiet("pane", "close", pane_id)


def map_panes(panes):
    return [p["pane_id"] for p in panes if p.get("pane_id") and is_map(p)]


def agent_tabs(panes):
    return {p.get("tab_id") for p in panes if p.get("agent") and not is_map(p) and p.get("tab_id")}


def ensure_maps(workspace, tabs, side=None):
    """Open a map in each of `tabs` that does not have one yet."""
    opened = []
    for tab in sorted(t for t in tabs if t):
        panes = hc.list_panes(workspace)
        if any(is_map(p) and p.get("tab_id") == tab for p in panes):
            continue
        anchor = next((p["pane_id"] for p in panes if p.get("tab_id") == tab and not is_map(p)), None)
        if anchor:
            opened.append(open_map(panes, anchor, side))
    return opened


def edge_anchor(boxes, side, skip=()):
    """The pane along `side` of the tab to dock against: outermost, then longest."""
    choices = {pid: r for pid, r in boxes.items() if pid not in skip} or boxes
    if not choices:
        return None

    def key(pid):
        r = choices[pid]
        x, y, w, h = r.get("x", 0), r.get("y", 0), r.get("width", 0), r.get("height", 0)
        return {"left": (x, -h), "right": (-(x + w), -h), "top": (y, -w), "bottom": (-(y + h), -w)}[side]

    return min(choices, key=key)


def dock(entrypoint, anchor, side, size, focus=False):
    """Open plugin pane `entrypoint` beside `anchor` on `side`, `size` cells deep."""
    # Herdr splits only right or down; dock left/top by swapping afterwards.
    # Note: herdr 0.9.x rejects --workspace together with --target-pane.
    opened = hc.call(
        "plugin", "pane", "open",
        "--plugin", os.environ.get("HERDR_PLUGIN_ID", "horchestra"),
        "--entrypoint", entrypoint,
        "--placement", "split",
        "--target-pane", anchor,
        "--direction", "right" if side in ("left", "right") else "down",
        "--focus" if focus else "--no-focus",
    )
    pane_id = ((opened.get("plugin_pane") or {}).get("pane") or {}).get("pane_id")
    if not pane_id:
        return None
    if side in ("left", "top"):
        hc.call_quiet("pane", "swap", "--source-pane", pane_id, "--target-pane", anchor)
    fit(pane_id, anchor, side, size)
    return pane_id


def fit(pane_id, anchor, side, size):
    """Move the divider between `pane_id` and `anchor` until `pane_id` is `size` deep."""
    axis = "width" if side in ("left", "right") else "height"
    for _ in range(3):
        boxes = rects(layout_for(pane_id))
        mine, other = boxes.get(pane_id), boxes.get(anchor)
        if not mine or not other:
            break
        total = mine.get(axis, 0) + other.get(axis, 0)
        excess = mine.get(axis, 0) - size
        if total <= 0 or abs(excess) <= 1:
            break
        direction = SHRINK[side] if excess > 0 else GROW[side]
        if hc.call_quiet("pane", "resize", "--direction", direction,
                         "--amount", f"{abs(excess) / total:.4f}", "--pane", pane_id) is None:
            break


def open_map(panes, focused, side=None):
    """Open a map docked at `side` (default: the saved side) of `focused`'s tab."""
    if not focused or not any(p.get("pane_id") == focused for p in panes):
        focused = next((p["pane_id"] for p in panes if p.get("focused")), None)
        focused = focused or (panes[0]["pane_id"] if panes else None)
    if not focused:
        raise hc.HerdrError("no pane to dock against")
    side = side or view_side("map")
    views = {p.get("pane_id") for p in panes if (p.get("tokens") or {}).get(hc.TOKEN_VIEW)}
    anchor = edge_anchor(rects(layout_for(focused)), side, skip=views) or focused
    size = target_width() if side in ("left", "right") else target_height()
    return dock("map", anchor, side, size)


def overviews(panes):
    return [p["pane_id"] for p in panes if p.get("pane_id") and is_overview(p)] + dead_views(panes, OVERVIEW_LABEL)


def toggle_overview(focused, side=None):
    """Close any all-spaces overview, or open one along a side of this tab.

    With `side`, any open overview is moved there instead of toggled.
    """
    panes = [p for p in hc.call("pane", "list").get("panes") or [] if isinstance(p, dict)]
    existing = overviews(panes)
    for pane_id in existing:
        close_view(pane_id)
    if existing and not side:
        return 0
    if existing:
        panes = [p for p in hc.call("pane", "list").get("panes") or [] if isinstance(p, dict)]
    if not focused or focused in existing:
        focused = next((p["pane_id"] for p in panes if p.get("focused")), None)
    if not focused:
        raise hc.HerdrError("no focused pane")
    side = side or view_side("overview")
    layout = layout_for(focused)
    boxes = rects(layout)
    by_id = {p.get("pane_id"): p for p in panes}
    # Never dock against a map column.
    maps = {pid for pid in boxes if is_map(by_id.get(pid, {}))}
    anchor = edge_anchor(boxes, side, skip=maps) or focused
    area = layout.get("area") or {}
    axis = "width" if side in ("left", "right") else "height"
    whole = area.get(axis) or max((r.get("x" if axis == "width" else "y", 0) + r.get(axis, 0)
                                   for r in boxes.values()), default=0)
    dock("overview", anchor, side, max(14, int(whole * OVERVIEW_SHARE)), focus=True)
    return 0


def redock_maps(workspace, focused, side):
    """Remember `side` and move every map in the workspace there."""
    save_side("map", side)
    panes = hc.list_panes(workspace)
    tabs = {p.get("tab_id") for p in panes if is_map(p)} | agent_tabs(panes)
    tabs |= {p.get("tab_id") for p in panes if p.get("pane_id") == focused}
    for pane_id in map_panes(panes) + dead_views(panes, MAP_LABEL):
        close_view(pane_id)
    return ensure_maps(workspace, {t for t in tabs if t}, side)


def main():
    ctx = context()
    workspace = find(ctx, "HERDR_WORKSPACE_ID", "workspace_id")
    focused = find(ctx, "HERDR_PANE_ID", "pane_id", "focused_pane_id")
    args = sys.argv[1:]
    side = args[args.index("--dock") + 1] if "--dock" in args[:-1] else None
    if side is not None and side not in SIDES:
        print(f"horchestra: --dock takes one of {', '.join(SIDES)}", file=sys.stderr)
        return 2
    if "--overview" in args:
        if side:
            save_side("overview", side)
        return toggle_overview(focused, side)
    if not workspace and focused:
        workspace = hc.get_pane(focused).get("workspace_id")
    if not workspace:
        print("horchestra: no workspace context", file=sys.stderr)
        return 1

    if side:
        redock_maps(workspace, focused, side)
        return 0
    panes = hc.list_panes(workspace)
    existing = map_panes(panes) + dead_views(panes, MAP_LABEL)
    if existing:
        for pane_id in existing:
            close_view(pane_id)
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
        print(f"horchestra: {exc}", file=sys.stderr)
        sys.exit(1)
