"""Pure layouts for the agent map: compact rows, cards, and a top-down graph.

Renderers draw onto a Canvas of (char, style) cells and return the canvas
plus each node's bounding box. Styles are symbolic names that the curses
front end maps to attributes, so layouts can be tested as plain text.
"""

# kind -> (icon, style)
KINDS = {
    "claude": ("✻", "kind:claude"),
    "codex": ("◆", "kind:codex"),
    "gemini": ("✦", "kind:gemini"),
    "agy": ("✦", "kind:gemini"),
    "pi": ("π", "kind:pi"),
    "omp": ("π", "kind:pi"),
    "opencode": ("◇", "kind:other"),
    "cursor": ("◈", "kind:other"),
    "copilot": ("◎", "kind:other"),
    "shell": ("$", "dim"),
}
DEFAULT_KIND = ("▪", "kind:other")

# status -> (glyph, short word)
STATUS = {
    "working": ("●", "working"),
    "blocked": ("▲", "blocked"),
    "done": ("✓", "done"),
    "idle": ("○", "idle"),
    "unknown": ("·", "?"),
}

GRAPH_GAP = 2
GRAPH_LEVEL = 6  # 4 box rows + 2 connector rows
CARD_INDENT = 3


class NodeView:
    """Display data for one agent, independent of Herdr's JSON."""

    __slots__ = ("key", "name", "kind", "status", "detail", "needs", "here",
                 "selected", "collapsed", "children", "parent")

    def __init__(self, key, name, kind, status, detail="", needs=False, here=False):
        self.key = key
        self.name = name
        self.kind = kind
        self.status = status if status in STATUS else "unknown"
        self.detail = detail
        self.needs = needs
        self.here = here
        self.selected = False
        self.collapsed = False
        self.children = []
        self.parent = None

    @property
    def icon(self):
        return KINDS.get(self.kind, DEFAULT_KIND)

    def shown_children(self):
        return [] if self.collapsed else self.children

    def hidden_count(self):
        return sum(1 + c.hidden_count() for c in self.children) if self.collapsed else 0


def subtree_count(node):
    return sum(1 + subtree_count(c) for c in node.children)


class Canvas:
    def __init__(self, width):
        self.width = max(1, width)
        self.rows = []

    def ensure(self, y):
        while len(self.rows) <= y:
            self.rows.append([[" ", None] for _ in range(self.width)])

    def put(self, y, x, text, style=None):
        if y < 0:
            return
        self.ensure(y)
        row = self.rows[y]
        for i, ch in enumerate(text):
            if 0 <= x + i < self.width:
                row[x + i] = [ch, style]

    def get(self, y, x):
        if 0 <= y < len(self.rows) and 0 <= x < self.width:
            return self.rows[y][x][0]
        return " "

    def runs(self, y, x0=0, x1=None):
        """[(x, text, style)] for row y between columns x0 and x1."""
        if y >= len(self.rows):
            return []
        row = self.rows[y][x0:x1]
        out = []
        for i, (ch, style) in enumerate(row):
            if out and out[-1][2] == style:
                out[-1][1] += ch
            else:
                out.append([x0 + i, ch, style])
        return [tuple(r) for r in out]

    def text(self):
        return "\n".join("".join(c[0] for c in row).rstrip() for row in self.rows)


def _fit(text, width):
    if width <= 0:
        return ""
    return text if len(text) <= width else text[: max(0, width - 1)] + "…"


def _border_style(node):
    if node.selected:
        return "sel_here" if node.here else "sel"
    return "here" if node.here else "border"


# ---- compact ---------------------------------------------------------------


def render_compact(roots, width):
    canvas, boxes, y = Canvas(width), {}, 0

    def walk(node, prefix, last, depth):
        nonlocal y
        lead = "" if depth == 0 else prefix + ("└─ " if last else "├─ ")
        x = 1
        if node.selected:
            canvas.put(y, 0, " " * width, "selrow")
        if node.here:
            canvas.put(y, 0, "▌", "here")
        canvas.put(y, x, lead, "rail")
        x += len(lead)
        glyph, _ = STATUS[node.status]
        canvas.put(y, x, glyph, "status:" + node.status)
        icon, icon_style = node.icon
        canvas.put(y, x + 2, icon, icon_style)
        name = node.name + (f" +{node.hidden_count()}" if node.collapsed and node.children else "")
        canvas.put(y, x + 4, _fit(name, width - x - 4), "selname" if node.selected else "name")
        rest = width - (x + 5 + len(name))
        if node.detail and rest > 3:
            canvas.put(y, x + 5 + len(name), _fit(node.detail, rest), "needs" if node.needs else "dim")
        boxes[node.key] = (y, y, 0, width - 1)
        y += 1
        child_prefix = "" if depth == 0 else prefix + ("   " if last else "│  ")
        kids = node.shown_children()
        for i, child in enumerate(kids):
            walk(child, child_prefix, i == len(kids) - 1, depth + 1)

    for root in roots:
        walk(root, "", True, 0)
    return canvas, boxes


# ---- cards -----------------------------------------------------------------


def _card(canvas, node, y, x, w):
    style = _border_style(node)
    inner = w - 2
    canvas.put(y, x, "╭" + "─" * inner + "╮", style)
    # Line 1: icon + name on the left, status on the right.
    glyph, word = STATUS[node.status]
    right = f"{glyph} {word}" if inner >= 20 else glyph
    icon, icon_style = node.icon
    canvas.put(y + 1, x, "│", style)
    canvas.put(y + 1, x + 1, " " * inner, None)
    canvas.put(y + 1, x + 2, icon, icon_style)
    name_w = inner - len(right) - 5
    canvas.put(y + 1, x + 4, _fit(node.name, name_w), "selname" if node.selected else "name")
    canvas.put(y + 1, x + w - 1 - len(right) - 1, right, "status:" + node.status)
    canvas.put(y + 1, x + w - 1, "│", style)
    # Line 2: needs-you, reported status, task, or title.
    detail = ("! " + node.detail) if node.needs else node.detail
    canvas.put(y + 2, x, "│", style)
    canvas.put(y + 2, x + 1, " " * inner, None)
    canvas.put(y + 2, x + 2, _fit(detail, inner - 2), "needs" if node.needs else "dim")
    canvas.put(y + 2, x + w - 1, "│", style)
    # Bottom: a tee where the child rail leaves, or a folded count.
    bottom = "╰" + "─" * inner + "╯"
    if node.shown_children():
        bottom = "╰┬" + "─" * (inner - 1) + "╯"
    elif node.collapsed and node.children:
        tag = f" +{node.hidden_count()} "
        bottom = "╰─" + tag + "─" * max(0, inner - 1 - len(tag)) + "╯"
    canvas.put(y + 3, x, bottom, style)


def render_cards(roots, width):
    canvas, boxes = Canvas(width), {}

    def walk(node, x, y):
        w = max(12, width - x)
        _card(canvas, node, y, x, w)
        boxes[node.key] = (y, y + 3, x, x + w - 1)
        y += 4
        kids = node.shown_children()
        rail = x + 1
        last_join = y
        for child in kids:
            for ry in range(last_join, y + 1):
                canvas.put(ry, rail, "│", "rail")
            canvas.put(y, rail, "├─", "rail")
            last_join = y + 1
            y = walk(child, x + CARD_INDENT, y)
        if kids:
            # The last child's join becomes an elbow; clear the rail below it.
            join = boxes[kids[-1].key][0]
            canvas.put(join, rail, "└", "rail")
            for ry in range(join + 1, y):
                if canvas.get(ry, rail) == "│":
                    canvas.put(ry, rail, " ", None)
        return y

    y = 0
    for i, root in enumerate(roots):
        if i:
            y += 1
        y = walk(root, 0, y)
    return canvas, boxes


# ---- graph -----------------------------------------------------------------


def _box_width(node):
    glyph, word = STATUS[node.status]
    status = len("! needs you") if node.needs else len(word) + 2
    return max(12, min(22, max(len(node.name) + 6, status + 4)))


def graph_width(roots):
    widths = [_subtree_width(r) for r in roots]
    return sum(widths) + GRAPH_GAP * 2 * max(0, len(widths) - 1) + 2


def _subtree_width(node):
    kids = node.shown_children()
    own = _box_width(node)
    if not kids:
        return own
    return max(own, sum(_subtree_width(c) for c in kids) + GRAPH_GAP * (len(kids) - 1))


def render_graph(roots, width):
    total = graph_width(roots)
    canvas, boxes = Canvas(max(width, total)), {}

    def place(node, x0, depth):
        sw = _subtree_width(node)
        bw = _box_width(node)
        bx = x0 + (sw - bw) // 2
        y = depth * GRAPH_LEVEL
        _graph_box(canvas, node, y, bx, bw)
        boxes[node.key] = (y, y + 3, bx, bx + bw - 1)
        center = bx + bw // 2
        kids = node.shown_children()
        if not kids:
            return center
        kids_w = sum(_subtree_width(c) for c in kids) + GRAPH_GAP * (len(kids) - 1)
        cx = x0 + (sw - kids_w) // 2
        centers = []
        for child in kids:
            centers.append(place(child, cx, depth + 1))
            cx += _subtree_width(child) + GRAPH_GAP
        canvas.put(y + 3, center, "┬", _border_style(node))
        canvas.put(y + 4, center, "│", "rail")
        bar = y + 5
        lo, hi = min(centers + [center]), max(centers + [center])
        canvas.put(bar, lo, "─" * (hi - lo + 1), "rail")
        for c in centers:
            canvas.put(bar, c, "┬", "rail")
        if len(centers) == 1 and centers[0] == center:
            canvas.put(bar, center, "│", "rail")
        else:
            canvas.put(bar, lo, "┌" if lo in centers else "└", "rail")
            canvas.put(bar, hi, "┐" if hi in centers else "┘", "rail")
            canvas.put(bar, center, "┼" if center in centers else "┴", "rail")
        for child, c in zip(kids, centers):
            canvas.put(boxes[child.key][0], c, "┴", _border_style(child))
        return center

    x = 1 + max(0, (canvas.width - total) // 2)
    for root in roots:
        place(root, x, 0)
        x += _subtree_width(root) + GRAPH_GAP * 2
    return canvas, boxes


def _graph_box(canvas, node, y, x, w):
    style = _border_style(node)
    inner = w - 2
    canvas.put(y, x, "╭" + "─" * inner + "╮", style)
    icon, icon_style = node.icon
    name = _fit(node.name, inner - 4)
    label_x = x + 1 + (inner - len(name) - 2) // 2
    canvas.put(y + 1, x, "│" + " " * inner + "│", style)
    canvas.put(y + 1, label_x, icon, icon_style)
    canvas.put(y + 1, label_x + 2, name, "selname" if node.selected else "name")
    glyph, word = STATUS[node.status]
    status = f"{glyph} {word}"
    if node.needs:
        status = "! needs you"
    elif node.collapsed and node.children:
        status += f" +{node.hidden_count()}"
    status = _fit(status, inner)
    canvas.put(y + 2, x, "│" + " " * inner + "│", style)
    canvas.put(y + 2, x + 1 + (inner - len(status)) // 2, status,
               "needs" if node.needs else "status:" + node.status)
    canvas.put(y + 3, x, "╰" + "─" * inner + "╯", style)
