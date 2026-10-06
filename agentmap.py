#!/usr/bin/env python3
"""Agent map pane: an interactive view of the agents in this workspace.

Runs as a Herdr plugin pane. Parent/child edges come from pane tokens written
by `horchestra-team` (or `horchestra-tag`); untagged agents are listed as roots.
Three views (compact rows, cards, top-down graph) live in views.py; this
module owns data, input, the details box, and drawing to curses.
"""

import curses
import locale
import os
import re
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import activity  # noqa: E402
import herdr_client as hc  # noqa: E402
import views  # noqa: E402

POLL_SECONDS = 3.0
DEBOUNCE_SECONDS = 0.15
HEADER_ROWS = 4
FOOTER_ROWS = 2
DETAILS_ROWS = 8
GRAPH_MIN_WIDTH = 60
MODES = ["auto", "cards", "graph", "compact"]
FRAME_SECONDS = 0.12  # edge animation step
SIGNAL_SHOW = 6.0  # seconds a report/message pulse runs once seen
SIGNAL_FRESH = 30.0  # older signals (e.g. from before the map opened) are not replayed
SIGNAL_STYLES = {"report": "reply", "needs": "alert", "msg": "message"}
MAP_MIN_ROWS = 8  # the activity plot never squeezes the map below this
MIN_LANES = 2
NOTICE_SECONDS = 6.0
ZOOM_KINDS = ("claude", "codex")  # agents zoetrope can draw
DOCK_KEYS = {ord("W"): "top", ord("A"): "left", ord("S"): "bottom", ord("D"): "right"}
TOGGLE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "toggle.py")


# ---- data -----------------------------------------------------------------


def tokens_of(pane):
    tokens = pane.get("tokens")
    return tokens if isinstance(tokens, dict) else {}


def build_forest(panes, own_pane_id, tab_id, collapsed, selected):
    """Turn a flat pane list into NodeView roots (parents from tokens)."""
    nodes, by_terminal, raw = {}, {}, {}
    for pane in panes:
        pane_id = pane.get("pane_id")
        tokens = tokens_of(pane)
        if not pane_id or pane_id == own_pane_id or tokens.get(hc.TOKEN_VIEW):
            continue
        tagged = hc.TOKEN_PARENT in tokens or hc.TOKEN_ROLE in tokens
        if not pane.get("agent") and not tagged:
            continue
        kind = pane.get("agent") or "shell"
        status = pane.get("agent_status")
        needs = bool(tokens.get(hc.TOKEN_NEEDS)) and status != "working"
        detail = (tokens.get(hc.TOKEN_STATUS) or ("needs you" if needs else "")
                  or tokens.get(hc.TOKEN_TASK) or pane.get("terminal_title_stripped") or "")
        node = views.NodeView(
            pane_id, tokens.get(hc.TOKEN_ROLE) or kind, kind, status, detail,
            needs=needs, here=bool(tab_id) and pane.get("tab_id") == tab_id,
        )
        node.collapsed = pane_id in collapsed
        node.selected = pane_id == selected
        nodes[pane_id] = node
        raw[pane_id] = pane
        if pane.get("terminal_id"):
            by_terminal[pane["terminal_id"]] = node

    for key, node in nodes.items():
        ref = tokens_of(raw[key]).get(hc.TOKEN_PARENT)
        parent = (by_terminal.get(ref) or nodes.get(ref)) if ref else None
        if parent is None or parent is node:
            continue
        walk = parent  # refuse edges that would close a cycle
        while walk is not None and walk is not node:
            walk = walk.parent
        if walk is node:
            continue
        node.parent = parent
        parent.children.append(node)

    def order(n):
        return (n.name.lower(), n.key)

    for node in nodes.values():
        node.children.sort(key=order)
    roots = [n for n in nodes.values() if n.parent is None]
    roots.sort(key=lambda n: (not n.children, order(n)))
    return roots, raw


def build_overview(panes, workspaces, own_pane_id, own_workspace, collapsed, selected):
    """One root per space (workspace), with that space's agent tree under it."""
    by_ws = {}
    for pane in panes:
        by_ws.setdefault(pane.get("workspace_id"), []).append(pane)
    roots, raw = [], {}
    for ws in sorted(workspaces, key=lambda w: w.get("number") or 0):
        wid = ws.get("workspace_id")
        if not wid:
            continue
        team, team_raw = build_forest(by_ws.get(wid, []), own_pane_id, None, collapsed, selected)
        count = len(visible_order(team))
        key = "ws:" + wid
        node = views.NodeView(key, ws.get("label") or wid, "space", ws.get("agent_status"),
                              f"{count} agent{'s' * (count != 1)}" if count else "no agents",
                              here=wid == own_workspace)
        node.collapsed = key in collapsed
        node.selected = key == selected
        for child in team:
            child.parent = node
        node.children = team
        roots.append(node)
        raw.update(team_raw)
    return roots, raw


def animation_enabled():
    """Edge animation: HORCHESTRA_ANIMATE, else the plugin config file `animate`."""
    raw = os.environ.get("HORCHESTRA_ANIMATE")
    config_dir = os.environ.get("HERDR_PLUGIN_CONFIG_DIR")
    if raw is None and config_dir:
        try:
            with open(os.path.join(config_dir, "animate")) as fh:
                raw = fh.read()
        except OSError:
            pass
    return (raw or "1").strip().lower() not in ("0", "off", "false", "no")


def herdr_prefix():
    """The user's Herdr prefix key, for the help overlay (best effort)."""
    path = os.path.join(os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config"),
                        "herdr", "config.toml")
    try:
        import tomllib

        with open(path, "rb") as fh:
            prefix = (tomllib.load(fh).get("keys") or {}).get("prefix")
        return prefix if isinstance(prefix, str) and prefix else "ctrl+b"
    except (OSError, ValueError, ImportError):
        return "ctrl+b"


def help_lines(overview):
    prefix = herdr_prefix()
    return [
        ("KEYS IN THIS MAP", None),
        ("w s  ↑ ↓  k j", "move selection"),
        ("a d  ← →  h l", "parent / child"),
        ("Enter, 2×click", "go to space / agent" if overview else "focus agent's pane"),
        ("click, wheel", "select / move"),
        ("Space", "fold / unfold"),
        ("v", "cycle views"),
        ("i", "details box"),
        ("t", "activity plot"),
        ("[ ]", "activity: shorter / longer"),
        ("z", "zoom into agent (zoetrope)"),
        ("W A S D (shift)", "dock " + ("overview" if overview else "maps") + ": top / left / bottom / right"),
        ("r", "refresh"),
        ("q", "close this map"),
        ("?", "show / hide help"),
        ("", None),
        (f"HERDR KEYS (prefix = {prefix})", None),
        ("prefix m", "maps in this space"),
        ("prefix M", "all-spaces overview"),
        ("prefix z", "zoom: wide graph"),
    ]


def visible_order(roots):
    out = []

    def walk(node):
        out.append(node)
        for child in node.shown_children():
            walk(child)

    for root in roots:
        walk(root)
    return out


CHROME = re.compile(r"^(❯|>|⏵|\?|esc|ctrl|shift|tab to|↑|↓|─|━|═|\$|%)", re.I)
SHELL_PROMPT = re.compile(r"^\S+@\S+.*[%$#]\s*$")


def last_message(text):
    """Best-effort last thing the agent said, from its recent screen."""
    lines = [l.strip() for l in text.splitlines() if l.strip()]
    for line in reversed(lines):
        if line[:1] in ("⏺", "•", "●") and len(line) > 2:
            return line[1:].strip()
    for line in reversed(lines):
        letters = sum(ch.isalnum() for ch in line)
        if letters >= 8 and letters / max(1, len(line)) > 0.5 and not CHROME.match(line) \
                and not SHELL_PROMPT.match(line) and "tokens" not in line.lower():
            return line
    return ""


def ago(seconds):
    seconds = int(max(0, seconds))
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        return f"{seconds // 60}m"
    return f"{seconds // 3600}h{(seconds % 3600) // 60:02d}"


def wrap(text, width, lines):
    words, out, cur = text.split(), [], ""
    for word in words:
        if not cur:
            cur = word if len(word) <= width else word[: max(1, width - 1)] + "…"
        elif len(cur) + len(word) + 1 <= width:
            cur = f"{cur} {word}"
        else:
            out.append(cur)
            cur = word[:width]
        if len(out) == lines:
            break
    if cur and len(out) < lines:
        out.append(cur)
    if len(out) == lines and len(" ".join(out)) < len(text):
        out[-1] = out[-1][: max(0, width - 1)] + "…"
    return out


# ---- app --------------------------------------------------------------------


class App:
    def __init__(self, screen, overview=False):
        self.screen = screen
        self.overview = overview
        self.workspaces = []
        self.show_help = False
        self.own_pane = os.environ.get("HERDR_PANE_ID", "")
        self.workspace_id = os.environ.get("HERDR_WORKSPACE_ID", "")
        self.tab_id = os.environ.get("HERDR_TAB_ID", "")
        self.workspace_label = ""
        self.panes = []
        self.raw = {}
        self.tabs = {}
        self.roots = []
        self.order = []
        self.selected = None
        self.collapsed = set()
        self.mode = "graph" if overview else "auto"
        self.show_details = True
        self.scroll_y = 0
        self.scroll_x = 0
        self.hits = []  # [(y0, y1, x0, x1, key)] in screen coordinates
        self.error = ""
        self.last_refresh = 0.0
        self.last_click = (0.0, None)
        self.since = {}  # pane_id -> (status, monotonic time first seen)
        self.messages = {}  # pane_id -> (monotonic time, text)
        self.styles = {}
        self.watcher = hc.EventWatcher()
        self.activity = activity.Activity()
        self.show_activity = True
        self.window = 1  # index into activity.WINDOWS
        self.animate = animation_enabled()
        self.animating = False
        self.last_draw = 0.0
        self.notice = (0.0, "")
        self._lanes_cache = (None, [])
        self.signals = {}  # pane_id -> epoch of the last signal seen
        self.flows = []  # [(from key, to key or None for the parent, style, monotonic end)]

    # ---- data ---------------------------------------------------------------

    def refresh(self):
        self.last_refresh = time.monotonic()
        try:
            if self.own_pane:
                # Resolve our location each time: the pane may move.
                me = hc.get_pane(self.own_pane)
                self.workspace_id = me.get("workspace_id") or self.workspace_id
                self.tab_id = me.get("tab_id") or self.tab_id
                view = "all" if self.overview else "1"
                if tokens_of(me).get(hc.TOKEN_VIEW) != view:
                    hc.set_tokens(self.own_pane, {hc.TOKEN_VIEW: view})
            if self.overview:
                self.workspaces = [w for w in hc.call("workspace", "list").get("workspaces") or []
                                   if isinstance(w, dict)]
                self.workspace_label = f"all spaces ({len(self.workspaces)})"
                self.panes = [p for p in hc.call("pane", "list").get("panes") or [] if isinstance(p, dict)]
                self.tabs = {}
                self.error = ""
                self._after_refresh()
                return
            if not self.workspace_id:
                raise hc.HerdrError("no workspace context")
            ws = hc.get_workspace(self.workspace_id)
            self.workspace_label = ws.get("label") or self.workspace_id
            self.panes = hc.list_panes(self.workspace_id)
            tabs = hc.call_quiet("tab", "list", "--workspace", self.workspace_id) or {}
            self.tabs = {t.get("tab_id"): t.get("label") or "" for t in tabs.get("tabs") or []}
            self.error = ""
        except hc.HerdrError as exc:
            self.error = str(exc)[:120]
            return
        self._after_refresh()

    def _after_refresh(self):
        now = time.monotonic()
        for pane in self.panes:
            key, status = pane.get("pane_id"), pane.get("agent_status")
            if key and self.since.get(key, (None,))[0] != status:
                self.since[key] = (status, now)
        self.watcher.watch_panes(
            p["pane_id"] for p in self.panes if p.get("agent") and p.get("pane_id")
        )
        self.collect_signals()
        try:
            self.activity.update(self.panes)
        except (OSError, ValueError, TypeError):
            pass  # the plot is a nicety; never let it take the map down
        self.messages.pop(self.selected, None)  # refetch the selected agent's output
        self.rebuild()

    def collect_signals(self, now=None):
        """Turn new report/message signals on panes into short-lived flows."""
        now = time.time() if now is None else now
        for pane in self.panes:
            key = pane.get("pane_id")
            signal = hc.parse_signal(tokens_of(pane).get(hc.TOKEN_SIGNAL))
            if not key or not signal or self.signals.get(key) == signal[2]:
                continue
            self.signals[key] = signal[2]
            kind, to, epoch = signal
            if now - epoch <= SIGNAL_FRESH:
                self.flows.append((key, to, SIGNAL_STYLES[kind], time.monotonic() + SIGNAL_SHOW))

    def draw_flows(self, canvas, frame):
        """Pulses from a reporting/messaging agent to its parent or receiver."""
        now = time.monotonic()
        self.flows = [f for f in self.flows if f[3] > now]
        shown = False
        for src, dst, style, _ in self.flows:
            node = self.node(src)
            target = (node.parent if node else None) if dst is None else self.node(dst)
            if node and target:
                shown |= views.animate_path(canvas, views.route(canvas, node, target), frame, style)
        return shown

    def rebuild(self):
        if self.overview:
            self.roots, self.raw = build_overview(
                self.panes, self.workspaces, self.own_pane, self.workspace_id,
                self.collapsed, self.selected)
        else:
            self.roots, self.raw = build_forest(
                self.panes, self.own_pane, self.tab_id, self.collapsed, self.selected
            )
        self.order = visible_order(self.roots)
        keys = [n.key for n in self.order]
        if self.selected not in keys:
            here = [n.key for n in self.order if n.here]
            self.selected = (here or keys or [None])[0]
            for node in self.order:
                node.selected = node.key == self.selected

    def node(self, key):
        return next((n for n in self.order if n.key == key), None)

    def message_for(self, key):
        if key.startswith("ws:"):
            return ""
        cached = self.messages.get(key)
        if cached and time.monotonic() - cached[0] < POLL_SECONDS:
            return cached[1]
        text = last_message(hc.read_text(key, lines=40))
        self.messages[key] = (time.monotonic(), text)
        return text

    def lanes(self, width):
        """[(node, counts or None)] for the activity plot."""
        window = activity.WINDOWS[self.window]
        now = time.time()
        stamp = (window, width, int(now), id(self.roots), self.selected)
        if self._lanes_cache[0] == stamp:
            return self._lanes_cache[1]  # animation frames redraw far more often than this changes
        if self.overview:
            groups = [(root, [n.key for n in self._subtree(root)]) for root in self.roots]
        else:
            groups = [(n, [n.key]) for n in self.order]
        nodes = [g[0] for g in groups]
        count = views.activity_spark_width([(n, None) for n in nodes], width) or 1
        out = []
        for node, keys in groups:
            if not any(self.activity.has_history(k) for k in keys):
                out.append((node, None))
                continue
            times = [t for k in keys for t in self.activity.times(k)]
            out.append((node, activity.buckets(times, now, window, count)))
        self._lanes_cache = (stamp, out)
        return out

    def _subtree(self, node):
        out = []
        for child in node.children:
            out.append(child)
            out.extend(self._subtree(child))
        return out

    def flash(self, text):
        self.notice = (time.monotonic(), text)

    # ---- drawing ------------------------------------------------------------

    def put(self, y, x, text, attr=0):
        h, w = self.screen.getmaxyx()
        if y < 0 or y >= h or x >= w or x < 0:
            return
        text = text[: max(0, w - x - (1 if y == h - 1 else 0))]
        try:
            self.screen.addstr(y, x, text, attr)
        except curses.error:
            pass

    def effective_mode(self, width):
        if self.mode != "auto":
            return self.mode
        if self.overview:
            return "graph"  # spaces side by side; scrolls horizontally
        if width >= GRAPH_MIN_WIDTH and views.graph_width(self.roots) <= width:
            return "graph"
        return "cards"

    def draw(self):
        self.screen.erase()
        h, w = self.screen.getmaxyx()
        attr = self.styles
        mode = self.effective_mode(w)

        self.put(0, 1, "ALL SPACES" if self.overview else "AGENT MAP", curses.A_BOLD)
        tag = f"[{mode}]" if self.mode == "auto" else f"[{mode}*]"
        self.put(0, w - len(tag) - 1, tag, attr["dim"])
        self.put(1, 1, self.workspace_label, attr["accent"] | curses.A_BOLD)
        x = 1
        counts = {}
        for node in self.order:
            counts[node.status] = counts.get(node.status, 0) + 1
        needs = sum(1 for n in self.order if n.needs)
        if needs:
            self.put(2, x, f"!{needs}", attr["needs"])
            x += len(str(needs)) + 2
        for status in ("blocked", "working", "done", "idle"):
            if counts.get(status):
                text = f"{views.STATUS[status][0]}{counts[status]}"
                self.put(2, x, text, attr["status:" + status])
                x += len(text) + 1
        self.put(3, 0, "─" * w, attr["dim"])

        details = DETAILS_ROWS if self.show_details and h >= HEADER_ROWS + FOOTER_ROWS + DETAILS_ROWS + 8 else 0
        footer = FOOTER_ROWS if h > 12 else 0
        lanes, plot = [], 0
        if self.show_activity and self.order and not self.show_help and not self.error:
            lanes = self.lanes(w)
            spare = h - HEADER_ROWS - footer - details - MAP_MIN_ROWS - 3  # title, axis, rule
            if spare >= min(MIN_LANES, len(lanes)):
                lanes = self._lane_window(lanes, spare)
                plot = len(lanes) + 3
            else:
                lanes = []
        top, bottom = HEADER_ROWS, h - footer - details - plot
        view_h = max(0, bottom - top)
        self.hits = []
        self.animating = False

        if self.show_help:
            details = 0
            self.draw_help(top, h - footer, w)
        elif self.error:
            self.put(top, 1, "herdr unavailable, retrying…", attr["status:blocked"])
            self.put(top + 1, 1, self.error, attr["dim"])
        elif not self.order:
            self.put(top, 1, "no spaces" if self.overview else "no agents in this space", attr["dim"])
            self.put(top + 2, 1, "start one with:", attr["dim"])
            self.put(top + 3, 1, "claude --agent orchestrator", attr["dim"])
        else:
            renderer = {"cards": views.render_cards, "graph": views.render_graph,
                        "compact": views.render_compact}[mode]
            canvas, boxes = renderer(self.roots, w)
            frame = int(time.monotonic() / FRAME_SECONDS) if self.animate else 0
            if self.animate:
                working = {n.key for n in self.order if n.status == "working"}
                self.animating = views.animate_edges(canvas, working, frame)
            # Drawn over the working pulses: replies are rarer and short-lived.
            # Still shown (without motion) when animation is off, until they expire.
            flowing = self.draw_flows(canvas, frame)
            self.animating = self.animating or flowing
            self.blit(canvas, boxes, top, view_h, w)

        if plot:
            self.draw_activity(lanes, bottom, w)
        if details and not self.show_help:
            self.draw_details(h - footer - details, details, w)
        if footer:
            self.put(h - 2, 0, "─" * w, attr["dim"])
            self.put(h - 1, 0, "▌", attr["here"])
            stamp, notice = self.notice
            if notice and time.monotonic() - stamp < NOTICE_SECONDS:
                self.put(h - 1, 1, notice, attr["status:blocked"])
            else:
                here = "this space" if self.overview else "this tab"
                # Most important first: narrow maps cut the end of this line.
                self.put(h - 1, 1, f"{here}  ? keys  z zoom  wasd move  v view", attr["dim"])
        self.last_draw = time.monotonic()
        self.screen.noutrefresh()
        curses.doupdate()

    def blit(self, canvas, boxes, top, view_h, w):
        """Draw the canvas, scrolled so the selected node is visible."""
        box = boxes.get(self.selected)
        if box:
            y0, y1, x0, x1 = box
            if y0 < self.scroll_y:
                self.scroll_y = y0
            elif y1 >= self.scroll_y + view_h:
                self.scroll_y = y1 - view_h + 1
            if x0 < self.scroll_x:
                self.scroll_x = x0
            elif x1 >= self.scroll_x + w:
                self.scroll_x = x1 - w + 1
        self.scroll_y = max(0, min(self.scroll_y, max(0, len(canvas.rows) - view_h)))
        self.scroll_x = max(0, min(self.scroll_x, max(0, canvas.width - w)))
        for row in range(view_h):
            cy = self.scroll_y + row
            for x, text, style in canvas.runs(cy, self.scroll_x, self.scroll_x + w):
                self.put(top + row, x - self.scroll_x, text, self.styles.get(style, 0))
        for key, (y0, y1, x0, x1) in boxes.items():
            self.hits.append((top + y0 - self.scroll_y, top + y1 - self.scroll_y,
                              x0 - self.scroll_x, x1 - self.scroll_x, key))

    def _lane_window(self, lanes, rows):
        """At most `rows` lanes, keeping the selected one in view."""
        if len(lanes) <= rows:
            return lanes
        keys = [n.key for n, _ in lanes]
        sel = self.selected
        if self.overview:
            node = self.node(sel)
            while node is not None and node.parent is not None:
                node = node.parent
            sel = node.key if node else sel
        i = keys.index(sel) if sel in keys else 0
        start = max(0, min(i - rows // 2, len(lanes) - rows))
        return lanes[start:start + rows]

    def draw_activity(self, lanes, y, w):
        self.put(y, 0, "─" * w, self.styles["dim"])
        canvas, boxes = views.render_activity(
            lanes, w, activity.window_label(activity.WINDOWS[self.window]), activity.sparkline)
        for row in range(len(canvas.rows)):
            for x, text, style in canvas.runs(row):
                self.put(y + 1 + row, x, text, self.styles.get(style, 0))
        for key, (y0, y1, x0, x1) in boxes.items():
            self.hits.append((y + 1 + y0, y + 1 + y1, x0, x1, key))

    def draw_help(self, top, bottom, w):
        """Key list; descriptions that do not fit beside a key wrap below it."""
        attr = self.styles
        lines = help_lines(self.overview)
        key_w = max(len(k) for k, t in lines if t) + 2
        desc_x = 2 + key_w
        row = top
        for key, text in lines:
            if row >= bottom:
                break
            if text is None:
                self.put(row, 1, key, attr["accent"] | curses.A_BOLD)
                row += 1
                continue
            self.put(row, 2, key, curses.A_BOLD)
            if len(text) <= w - desc_x - 1:
                self.put(row, desc_x, text, attr["dim"])
                row += 1
                continue
            row += 1
            for part in wrap(text, max(8, w - 7), 2):
                if row >= bottom:
                    break
                self.put(row, 6, part, attr["dim"])
                row += 1

    def draw_details(self, y, rows, w):
        attr = self.styles
        node = self.node(self.selected)
        pane = self.raw.get(self.selected, {})
        title = f" {node.name} " if node else " "
        self.put(y, 0, "─" * w, attr["dim"])
        self.put(y, 2, title, attr["accent"] | curses.A_BOLD)
        if not node:
            return
        if node.key.startswith("ws:"):
            self.put(y + 1, 1, f"space · {node.detail} · Enter to switch to it"[: w - 2], attr["dim"])
            return
        tokens = tokens_of(pane)
        status, since = self.since.get(node.key, (node.status, time.monotonic()))
        tab = self.tabs.get(pane.get("tab_id"), "") or pane.get("tab_id", "")
        line = f"{node.kind} · {views.STATUS[node.status][1]} {ago(time.monotonic() - since)} · tab {tab}"
        self.put(y + 1, 1, line[: w - 2], attr["dim"])
        row = y + 2
        label_w = 7
        fields = []
        if node.needs:
            fields.append(("needs", tokens.get(hc.TOKEN_STATUS) or "needs you", "needs"))
        elif tokens.get(hc.TOKEN_STATUS):
            fields.append(("status", tokens[hc.TOKEN_STATUS], "name"))
        if tokens.get(hc.TOKEN_TASK):
            fields.append(("task", tokens[hc.TOKEN_TASK], None))
        if tokens.get(hc.TOKEN_PROFILE):
            fields.append(("role", tokens[hc.TOKEN_PROFILE], None))
        message = self.message_for(node.key)
        if message:
            fields.append(("last", message, None))
        for label, text, style in fields:
            budget = y + rows - row
            if budget <= 0:
                break
            chunk = wrap(text, max(8, w - label_w - 2), min(2, budget))
            self.put(row, 1, label, attr["dim"])
            for i, part in enumerate(chunk):
                self.put(row + i, label_w + 1, part, attr.get(style, 0) if style else 0)
            row += len(chunk)

    # ---- input --------------------------------------------------------------

    def select(self, key):
        self.selected = key
        for node in self.order:
            node.selected = node.key == key

    def move(self, delta):
        keys = [n.key for n in self.order]
        if keys:
            i = keys.index(self.selected) if self.selected in keys else 0
            self.select(keys[max(0, min(len(keys) - 1, i + delta))])

    def toggle_fold(self):
        node = self.node(self.selected)
        if node and node.children:
            self.collapsed ^= {node.key}
            self.rebuild()

    def to_parent(self):
        node = self.node(self.selected)
        if node and node.parent:
            self.select(node.parent.key)

    def to_child(self):
        node = self.node(self.selected)
        if node and node.collapsed:
            self.toggle_fold()
        elif node and node.children:
            self.select(node.children[0].key)

    def activate(self):
        if not self.selected:
            return
        if self.selected.startswith("ws:"):
            hc.call_quiet("workspace", "focus", self.selected[3:])
        else:
            hc.focus_pane(self.selected)

    def zoom(self):
        """Open the selected agent's session in zoetrope, over its pane."""
        pane = self.raw.get(self.selected) or {}
        ref = pane.get("agent_session") if isinstance(pane.get("agent_session"), dict) else {}
        kind, session = pane.get("agent"), ref.get("value") if ref.get("kind") == "id" else None
        if not pane:
            self.flash("select an agent to zoom into")
            return
        if kind not in ZOOM_KINDS:
            self.flash("zoom works for Claude and Codex agents")
            return
        if not session:
            self.flash(f"no session id yet: herdr integration install {kind}")
            return
        base = ("plugin", "pane", "open",
                "--plugin", os.environ.get("HERDR_PLUGIN_ID", "horchestra"),
                "--entrypoint", "zoom",
                "--env", f"HORCHESTRA_ZOOM_KIND={kind}",
                "--env", f"HORCHESTRA_ZOOM_SESSION={session}")
        # An overlay covers the active pane (this map, since its key was just
        # pressed) and closing it returns here. Herdr refuses an overlay when
        # this pane is not the active one; a tab beside the agent still works.
        if hc.call_quiet(*base, "--placement", "overlay", "--focus") is not None:
            return
        workspace = pane.get("workspace_id") or self.workspace_id
        if hc.call_quiet(*base, "--placement", "tab", "--workspace", workspace, "--focus") is None:
            self.flash("could not open the zoom pane")

    def redock(self, side):
        """Move this view (every map in the space, or the overview) to `side`.

        A detached toggle does the move: it closes this pane on the way.
        """
        mine = [p for p in self.panes if p.get("tab_id") == self.tab_id
                and p.get("pane_id") != self.own_pane and not tokens_of(p).get(hc.TOKEN_VIEW)]
        env = dict(os.environ, HERDR_WORKSPACE_ID=self.workspace_id,
                   HERDR_PANE_ID=(mine[0]["pane_id"] if mine else self.own_pane))
        argv = [sys.executable, TOGGLE, "--dock", side] + (["--overview"] if self.overview else [])
        try:
            subprocess.Popen(argv, env=env, cwd=os.path.dirname(TOGGLE), start_new_session=True,
                             stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except OSError as exc:
            self.flash(f"could not move: {exc}")
            return
        self.flash(f"moving to the {side}…")

    def on_mouse(self):
        try:
            _, mx, my, _, bstate = curses.getmouse()
        except curses.error:
            return
        if bstate & getattr(curses, "BUTTON4_PRESSED", 0):
            self.move(-1)
            return
        if bstate & getattr(curses, "BUTTON5_PRESSED", 0):
            self.move(1)
            return
        if not bstate & (curses.BUTTON1_PRESSED | curses.BUTTON1_CLICKED | curses.BUTTON1_DOUBLE_CLICKED):
            return
        key = next((k for y0, y1, x0, x1, k in self.hits if y0 <= my <= y1 and x0 <= mx <= x1), None)
        if key is None:
            return
        now = time.monotonic()
        repeat = self.last_click[1] == key and now - self.last_click[0] < 0.4
        self.select(key)
        self.last_click = (now, key)
        if bstate & curses.BUTTON1_DOUBLE_CLICKED or repeat:
            self.activate()

    def handle(self, key):
        if self.show_help and key in (ord("?"), ord("q"), 27):
            self.show_help = False
            return True
        if key == ord("?"):
            self.show_help = True
            return True
        if key == ord("q"):
            return False
        if key in (curses.KEY_UP, ord("w"), ord("k")):
            self.move(-1)
        elif key in (curses.KEY_DOWN, ord("s"), ord("j")):
            self.move(1)
        elif key in (curses.KEY_LEFT, ord("a"), ord("h")):
            self.to_parent()
        elif key in (curses.KEY_RIGHT, ord("d"), ord("l")):
            self.to_child()
        elif key in (curses.KEY_HOME, ord("g")):
            self.move(-len(self.order))
        elif key in (curses.KEY_END, ord("G")):
            self.move(len(self.order))
        elif key in (curses.KEY_ENTER, 10, 13):
            self.activate()
        elif key == ord(" "):
            self.toggle_fold()
        elif key == ord("v"):
            self.mode = MODES[(MODES.index(self.mode) + 1) % len(MODES)]
            self.scroll_x = self.scroll_y = 0
        elif key == ord("i"):
            self.show_details = not self.show_details
        elif key == ord("t"):
            self.show_activity = not self.show_activity
        elif key == ord("["):
            self.window = max(0, self.window - 1)
        elif key == ord("]"):
            self.window = min(len(activity.WINDOWS) - 1, self.window + 1)
        elif key == ord("z"):
            self.zoom()
        elif key in DOCK_KEYS:
            self.redock(DOCK_KEYS[key])
        elif key == ord("r"):
            self.refresh()
        elif key == curses.KEY_MOUSE:
            self.on_mouse()
        elif key == curses.KEY_RESIZE:
            self.sync_size()
        return True

    # ---- loop ---------------------------------------------------------------

    def sync_size(self):
        """Follow pane resizes even if SIGWINCH never reaches curses."""
        try:
            size = os.get_terminal_size(sys.__stdout__.fileno())
        except OSError:
            return False
        if (size.lines, size.columns) == self.screen.getmaxyx():
            return False
        curses.resizeterm(size.lines, size.columns)
        return True

    def run(self):
        self.watcher.start()
        self.refresh()
        self.draw()
        while True:
            key = self.screen.getch()
            if key != -1:
                if not self.handle(key):
                    return
                self.draw()
                continue
            if self.sync_size():
                self.draw()
            if self.animating and time.monotonic() - self.last_draw >= FRAME_SECONDS:
                self.draw()
            if self.watcher.changed.is_set():
                time.sleep(DEBOUNCE_SECONDS)
                self.watcher.changed.clear()
                self.refresh()
                self.draw()
            elif time.monotonic() - self.last_refresh >= POLL_SECONDS:
                self.refresh()
                self.draw()


def make_styles():
    """Map views.py style names to curses attributes."""
    rich = curses.COLORS >= 256
    palette = {
        1: curses.COLOR_YELLOW,   # working
        2: curses.COLOR_RED,      # blocked / needs you
        3: curses.COLOR_GREEN,    # done, codex
        4: -1,                    # idle / default
        5: curses.COLOR_CYAN,     # this tab, accent
        6: 208 if rich else curses.COLOR_RED,      # claude orange
        7: curses.COLOR_BLUE,     # gemini
        8: curses.COLOR_MAGENTA,  # pi
    }
    for pair, color in palette.items():
        curses.init_pair(pair, color, -1)
    cp, bold, dim = curses.color_pair, curses.A_BOLD, curses.A_DIM
    return {
        None: 0,
        "dim": dim,
        "name": bold,
        "selname": bold | curses.A_REVERSE,
        "selrow": curses.A_REVERSE,
        "rail": dim,
        "border": dim,
        "here": cp(5),
        "sel": bold,
        "sel_here": cp(5) | bold,
        "accent": cp(5),
        "needs": cp(2) | bold,
        "status:working": cp(1) | bold,
        "status:blocked": cp(2) | bold,
        "status:done": cp(3) | bold,
        "status:idle": dim,
        "status:unknown": dim,
        "flow": cp(1),
        "pulse": cp(1) | bold,
        "reply": cp(3),
        "reply_pulse": cp(3) | bold,
        "alert": cp(2),
        "alert_pulse": cp(2) | bold,
        "message": cp(8),
        "message_pulse": cp(8) | bold,
        "kind:claude": cp(6) | bold,
        "kind:codex": cp(3) | bold,
        "kind:gemini": cp(7) | bold,
        "kind:pi": cp(8) | bold,
        "kind:other": bold,
    }


def setup(screen, overview=False):
    curses.curs_set(0)
    curses.use_default_colors()
    curses.mousemask(curses.ALL_MOUSE_EVENTS)
    curses.mouseinterval(0)
    screen.timeout(int(FRAME_SECONDS * 1000))
    screen.keypad(True)
    app = App(screen, overview=overview)
    app.styles = make_styles()
    app.run()


def main():
    locale.setlocale(locale.LC_ALL, "")
    os.environ.setdefault("ESCDELAY", "25")
    # Inherited LINES/COLUMNS would pin curses to the launch size and make it
    # ignore pane resizes; always ask the terminal instead.
    os.environ.pop("LINES", None)
    os.environ.pop("COLUMNS", None)
    curses.use_env(False)
    try:
        curses.wrapper(setup, "--all" in sys.argv[1:])
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
