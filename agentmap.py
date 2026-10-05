#!/usr/bin/env python3
"""Agent map pane: an interactive view of the agents in this workspace.

Runs as a Herdr plugin pane. Parent/child edges come from pane tokens written
by `agentmap-team` (or `agentmap-tag`); untagged agents are listed as roots.
Three views (compact rows, cards, top-down graph) live in views.py; this
module owns data, input, the details box, and drawing to curses.
"""

import curses
import locale
import os
import re
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import herdr_client as hc  # noqa: E402
import views  # noqa: E402

POLL_SECONDS = 3.0
DEBOUNCE_SECONDS = 0.15
HEADER_ROWS = 4
FOOTER_ROWS = 2
DETAILS_ROWS = 8
GRAPH_MIN_WIDTH = 60
MODES = ["auto", "cards", "graph", "compact"]


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
    def __init__(self, screen):
        self.screen = screen
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
        self.mode = "auto"
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

    # ---- data ---------------------------------------------------------------

    def refresh(self):
        self.last_refresh = time.monotonic()
        try:
            if self.own_pane:
                # Resolve our location each time: the pane may move.
                me = hc.get_pane(self.own_pane)
                self.workspace_id = me.get("workspace_id") or self.workspace_id
                self.tab_id = me.get("tab_id") or self.tab_id
                if not tokens_of(me).get(hc.TOKEN_VIEW):
                    hc.set_tokens(self.own_pane, {hc.TOKEN_VIEW: "1"})
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
        now = time.monotonic()
        for pane in self.panes:
            key, status = pane.get("pane_id"), pane.get("agent_status")
            if key and self.since.get(key, (None,))[0] != status:
                self.since[key] = (status, now)
        self.watcher.watch_panes(
            p["pane_id"] for p in self.panes if p.get("agent") and p.get("pane_id")
        )
        self.messages.pop(self.selected, None)  # refetch the selected agent's output
        self.rebuild()

    def rebuild(self):
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
        cached = self.messages.get(key)
        if cached and time.monotonic() - cached[0] < POLL_SECONDS:
            return cached[1]
        text = last_message(hc.read_text(key, lines=40))
        self.messages[key] = (time.monotonic(), text)
        return text

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
        if width >= GRAPH_MIN_WIDTH and views.graph_width(self.roots) <= width:
            return "graph"
        return "cards"

    def draw(self):
        self.screen.erase()
        h, w = self.screen.getmaxyx()
        attr = self.styles
        mode = self.effective_mode(w)

        self.put(0, 1, "AGENT MAP", curses.A_BOLD)
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
        top, bottom = HEADER_ROWS, h - footer - details
        view_h = max(0, bottom - top)
        self.hits = []

        if self.error:
            self.put(top, 1, "herdr unavailable, retrying…", attr["status:blocked"])
            self.put(top + 1, 1, self.error, attr["dim"])
        elif not self.order:
            self.put(top, 1, "no agents in this space", attr["dim"])
            self.put(top + 2, 1, "start one with:", attr["dim"])
            self.put(top + 3, 1, "claude --agent orchestrator", attr["dim"])
        else:
            renderer = {"cards": views.render_cards, "graph": views.render_graph,
                        "compact": views.render_compact}[mode]
            canvas, boxes = renderer(self.roots, w)
            self.blit(canvas, boxes, top, view_h, w)

        if details:
            self.draw_details(bottom, details, w)
        if footer:
            self.put(h - 2, 0, "─" * w, attr["dim"])
            self.put(h - 1, 0, "▌", attr["here"])
            self.put(h - 1, 1, "this tab  v view  d info  ↵ focus", attr["dim"])
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

    def draw_details(self, y, rows, w):
        attr = self.styles
        node = self.node(self.selected)
        pane = self.raw.get(self.selected, {})
        title = f" {node.name} " if node else " "
        self.put(y, 0, "─" * w, attr["dim"])
        self.put(y, 2, title, attr["accent"] | curses.A_BOLD)
        if not node:
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
        if self.selected:
            hc.focus_pane(self.selected)

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
        if key == ord("q"):
            return False
        if key in (curses.KEY_UP, ord("k")):
            self.move(-1)
        elif key in (curses.KEY_DOWN, ord("j")):
            self.move(1)
        elif key in (curses.KEY_LEFT, ord("h")):
            self.to_parent()
        elif key in (curses.KEY_RIGHT, ord("l")):
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
        elif key == ord("d"):
            self.show_details = not self.show_details
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
        "kind:claude": cp(6) | bold,
        "kind:codex": cp(3) | bold,
        "kind:gemini": cp(7) | bold,
        "kind:pi": cp(8) | bold,
        "kind:other": bold,
    }


def setup(screen):
    curses.curs_set(0)
    curses.use_default_colors()
    curses.mousemask(curses.ALL_MOUSE_EVENTS)
    curses.mouseinterval(0)
    screen.timeout(200)
    screen.keypad(True)
    app = App(screen)
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
        curses.wrapper(setup)
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
