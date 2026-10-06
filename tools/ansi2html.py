#!/usr/bin/env python3
"""Render `herdr pane read --format ansi` output as a terminal-styled HTML page.

usage: ansi2html.py in.ansi out.html [--title TITLE] [--trim]
"""
import html
import re
import sys

BASE = {30: "#1e2030", 31: "#ff6b6b", 32: "#7ee787", 33: "#f2cc60", 34: "#6cb6ff",
        35: "#d2a8ff", 36: "#56d4dd", 37: "#c9d1d9"}
BRIGHT = {90: "#6e7681", 91: "#ffa198", 92: "#56d364", 93: "#e3b341", 94: "#79c0ff",
          95: "#d2a8ff", 96: "#b3f0ff", 97: "#ffffff"}
FG, BG = "#c9d1d9", "#0d1117"
CELL = 8.43  # px per terminal column at 14px Menlo


def xterm256(n):
    if n < 8:
        return BASE[30 + n]
    if n < 16:
        return BRIGHT[90 + n - 8]
    if n < 232:
        n -= 16
        steps = [0, 95, 135, 175, 215, 255]
        return "#%02x%02x%02x" % (steps[n // 36], steps[(n // 6) % 6], steps[n % 6])
    g = 8 + (n - 232) * 10
    return "#%02x%02x%02x" % (g, g, g)


def parse(text):
    """Yield rows of [(char, style)] where style is a hashable tuple."""
    rows = []
    state = {"fg": None, "bg": None, "bold": False, "dim": False, "rev": False, "ul": False}
    for line in text.split("\n"):
        row = []
        pos = 0
        for m in re.finditer(r"\x1b\[([0-9;]*)m|\x1b\][^\x07]*\x07|\x1b\[[0-9;?]*[A-Za-z]", line):
            for ch in line[pos:m.start()]:
                row.append((ch, tuple(sorted(state.items()))))
            pos = m.end()
            if not m.group(0).endswith("m") or m.group(1) is None:
                continue
            codes = [int(c) if c else 0 for c in m.group(1).split(";")] or [0]
            i = 0
            while i < len(codes):
                c = codes[i]
                if c == 0:
                    state.update(fg=None, bg=None, bold=False, dim=False, rev=False, ul=False)
                elif c == 1:
                    state["bold"] = True
                elif c == 2:
                    state["dim"] = True
                elif c == 4:
                    state["ul"] = True
                elif c == 7:
                    state["rev"] = True
                elif c == 22:
                    state.update(bold=False, dim=False)
                elif c == 27:
                    state["rev"] = False
                elif c == 39:
                    state["fg"] = None
                elif c == 49:
                    state["bg"] = None
                elif c in BASE:
                    state["fg"] = BASE[c]
                elif c in BRIGHT:
                    state["fg"] = BRIGHT[c]
                elif 40 <= c <= 47:
                    state["bg"] = BASE[c - 10]
                elif c in (38, 48) and i + 2 < len(codes) and codes[i + 1] == 5:
                    state["fg" if c == 38 else "bg"] = xterm256(codes[i + 2])
                    i += 2
                elif c in (38, 48) and i + 4 < len(codes) and codes[i + 1] == 2:
                    state["fg" if c == 38 else "bg"] = "#%02x%02x%02x" % tuple(codes[i + 2:i + 5])
                    i += 4
                i += 1
        for ch in line[pos:]:
            row.append((ch, tuple(sorted(state.items()))))
        rows.append(row)
    return rows


def css(style):
    s = dict(style)
    fg, bg = s["fg"] or FG, s["bg"]
    if s["rev"]:
        fg, bg = (bg or BG), (s["fg"] or FG)
    out = [f"color:{fg}"]
    if bg:
        out.append(f"background:{bg}")
    if s["bold"]:
        out.append("font-weight:700")
    if s["dim"]:
        out.append("opacity:.55")
    if s["ul"]:
        out.append("text-decoration:underline")
    return ";".join(out)


def main():
    src, dst = sys.argv[1], sys.argv[2]
    title = sys.argv[sys.argv.index("--title") + 1] if "--title" in sys.argv else ""
    rows = parse(open(src, encoding="utf-8").read())
    if "--rows" in sys.argv:
        a, b = sys.argv[sys.argv.index("--rows") + 1].split(":")
        rows = rows[int(a or 0):int(b) if b else None]
    if "--squeeze" in sys.argv:  # collapse runs of blank rows to one
        out = []
        for r in rows:
            blank = not "".join(c for c, _ in r).strip()
            if not (blank and out and not "".join(c for c, _ in out[-1]).strip()):
                out.append(r)
        rows = out
    while rows and not "".join(c for c, _ in rows[-1]).strip():
        rows.pop()
    while rows and not "".join(c for c, _ in rows[0]).strip():
        rows.pop(0)
    if "--trim" in sys.argv:
        used = [i for r in rows for i, (c, st) in enumerate(r) if c.strip() or dict(st)["bg"] or dict(st)["rev"]]
        lo, hi = (max(0, min(used) - 2), max(used) + 3) if used else (0, 0)
        rows = [r[lo:hi] for r in rows]
    # Herdr reports the padding after a highlighted name as highlighted;
    # the terminal does not draw it that way, so drop reverse on trailing spaces.
    for r in rows:
        i = 0
        while i < len(r):
            st = r[i][1]
            j = i
            while j < len(r) and r[j][1] == st:
                j += 1
            if dict(st)["rev"]:
                k = j
                while k > i and r[k - 1][0] == " ":
                    k -= 1
                plain = tuple(sorted({**dict(st), "rev": False}.items()))
                for m in range(k, j):
                    r[m] = (" ", plain)
            i = j
    body = []
    for row in rows:
        # One fixed-width cell per character, so fallback glyphs (✻ ◆ ✦ π)
        # cannot shift the box drawing out of its grid.
        parts, run, cur = [], "", None

        def flush():
            if run:
                parts.append(f'<i style="{css(cur)};width:{len(run) * CELL:.2f}px">'
                             f'{html.escape(run).replace(" ", "&nbsp;")}</i>')

        for ch, st in row:
            safe = " " <= ch <= "~" or "\u2500" <= ch <= "\u259f"  # ASCII, box drawing, blocks
            if not safe:
                flush()
                run, cur = "", None
                parts.append(f'<i style="{css(st)};width:{CELL}px;text-align:center">{html.escape(ch)}</i>')
                continue
            if st != cur:
                flush()
                run, cur = "", st
            run += ch
        flush()
        body.append("<b>" + "".join(parts) + "</b>")
    width = max((len(r) for r in rows), default=10)
    head = f'<div class="title">{html.escape(title)}</div>' if title else ""
    page = f"""<!doctype html><html><head><meta charset="utf-8"><style>
body{{margin:0;background:#010409;}}
.win{{display:inline-block;margin:18px;background:{BG};border:1px solid #30363d;border-radius:10px;
box-shadow:0 8px 30px rgba(0,0,0,.5);overflow:hidden}}
.title{{font:12px -apple-system,Helvetica,sans-serif;color:#8b949e;padding:7px 14px;border-bottom:1px solid #21262d;background:#161b22}}
pre{{margin:0;padding:12px 16px;font:14px/1 Menlo,"SF Mono",Monaco,monospace;color:{FG}}}
pre b{{display:block;height:17px;white-space:nowrap;font-weight:inherit}}
pre i{{display:inline-block;height:17px;line-height:17px;font-style:normal;overflow:visible;vertical-align:top;white-space:pre}}
</style></head><body><div class="win">{head}<pre>{"".join(body)}</pre></div></body></html>"""
    open(dst, "w", encoding="utf-8").write(page)
    print(width, len(rows))


if __name__ == "__main__":
    main()
