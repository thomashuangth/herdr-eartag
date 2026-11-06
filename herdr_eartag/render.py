"""Pure layout: View model -> Frame (styled lines + clickable regions).

No terminal I/O here so the layout can be unit tested.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Optional, Tuple

from .textutil import char_width, pad, truncate, width

Segment = Tuple[str, str]  # (text, style tokens)
Line = List[Segment]

UNICODE = {
    "now": "▸", "act": "↳", "before": "≡", "done": "✓", "files": "◆", "alert": "!",
    "llm": "✦", "notif": "»", "infer": "?", "ctx": "◇", "reset": "↻", "stale": "◷",
    "pending": "◐", "hide": "✕", "auto_on": "●", "auto_off": "○",
    "rule": "─", "full": "█", "empty": "░", "sep": "·",
    "ell": "…", "more": "⋯", "point": "•", "session": "◇", "reply": "◈", "v_done": "✓", "v_partial": "◐", "v_failed": "✗", "v_waiting": "?", "v_working": "◐", "working": "●", "blocked": "▲", "st_done": "✓", "idle": "○", "unknown": "?",
}
ASCII = {
    "now": ">", "act": "-", "before": "=", "done": "v", "files": "*", "alert": "!",
    "llm": "*", "notif": ">>", "infer": "?", "ctx": "~", "reset": "@", "stale": "@",
    "pending": "~", "hide": "x", "auto_on": "*", "auto_off": "o",
    "rule": "-", "full": "#", "empty": ".", "sep": "-",
    "ell": "...", "more": "...", "point": "-", "session": "~", "reply": "*", "v_done": "v", "v_partial": "~", "v_failed": "x", "v_waiting": "?", "v_working": "~", "working": "*", "blocked": "^", "st_done": "v", "idle": "o", "unknown": "?",
}

STATUS_ROLES = {"working": "working", "blocked": "blocked", "done": "done", "idle": "idle"}


@dataclass
class Bar:
    key: str          # ctx | h5 | d7
    label: str
    pct: Optional[float]
    detail_icon: str = ""
    detail: str = ""
    detail_sub: str = ""


@dataclass
class View:
    width: int
    height: int
    ascii: bool = False
    project: str = ""
    mode_tag: str = ""
    status: str = "unknown"
    status_label: str = ""
    notif: Optional[Tuple[str, str]] = None        # (text, role)
    inferred_note: str = ""
    inferred_fix: str = ""                          # clickable: runs setup
    main_kind: str = "now"                          # now | last | none
    main_title: str = ""
    main_right: str = ""
    ask: str = ""
    ask_max_lines: int = 3
    ask_points: List[str] = field(default_factory=list)
    session_title: str = ""
    task_now: str = ""
    reply_label: str = ""
    reply_verdict: str = ""      # DONE | PARTIAL | FAILED | WAITING | WORKING (from rephrasing)
    reply_verdict_label: str = ""
    reply: str = ""
    ask_pending: bool = False
    activity: str = ""
    activity_role: str = "ok"
    activity_icon: str = "act"
    tasks: Optional[Tuple[int, int]] = None
    tasks_label: str = "tasks"
    message_lines: List[str] = field(default_factory=list)
    before_title: str = ""
    before: List[Tuple[str, str, str]] = field(default_factory=list)  # (time, title, verdict)
    files_title: str = ""
    files: List[str] = field(default_factory=list)
    diff: Optional[Tuple[int, int]] = None        # uncommitted +insertions -deletions
    alerts: List[str] = field(default_factory=list)
    bars: List[Bar] = field(default_factory=list)
    selected_bar: Optional[str] = None
    host: str = ""
    branch: str = ""
    model: str = ""
    version: str = ""
    effort: str = ""
    effort_level: Optional[Tuple[int, int]] = None   # (filled, total) on the model's own scale
    footnote: str = ""
    footnote_icon: str = ""
    footnote_action: str = ""
    menu: Optional[List[Tuple[str, str, str, str]]] = None   # (action, icon, icon_role, label)
    menu_index: int = 0


@dataclass
class Hit:
    y: int
    x0: int
    x1: int
    action: str


@dataclass
class Frame:
    lines: List[Line]
    hits: List[Hit]


def pct_role(pct: Optional[float]) -> str:
    if pct is None:
        return "dim"
    if pct >= 90:
        return "crit"
    if pct >= 70:
        return "warn"
    return "ok"


def _fit(line: Line, total: int) -> Line:
    """Cut a line to `total` columns and pad it with spaces."""
    out: Line = []
    used = 0
    for text, style in line:
        w = width(text)
        if used + w <= total:
            out.append((text, style))
            used += w
            continue
        room = total - used
        if room > 0:
            out.append((truncate(text, room, ""), style))
            used += width(out[-1][0])
        break
    if used < total:
        out.append((" " * (total - used), ""))
    return out


def _lr(left: Line, right: Line, inner: int) -> Line:
    lw = sum(width(t) for t, _ in left)
    rw = sum(width(t) for t, _ in right)
    if lw + rw + 1 > inner:
        return _fit(left, inner)
    return left + [(" " * (inner - lw - rw), "")] + right


def _bar_cells(pct: Optional[float], cells: int, role: str, text: str) -> Line:
    """A gauge with its value centered inside, the fill drawn as background color."""
    label = text.center(cells)[:cells]
    if pct is None:
        return [(label, "bg:track dim b")]
    filled = int(round(max(0.0, min(100.0, pct)) / 100.0 * cells))
    out: Line = []
    if filled:
        out.append((label[:filled], "bg:{} on:{} b".format(role, role)))
    if filled < cells:
        out.append((label[filled:], "bg:track text b"))
    return out


# -- highlighted text ---------------------------------------------------------------
def rich_wrap(text: str, max_width: int, max_lines: int, base: str, hi: str, ell: str = "…",
              max_spans: int = 2) -> List[Line]:
    """Word-wrap text where {curly} spans are drawn with the `hi` style (key words the
    rephrasing marks), the rest with `base`. The last kept line ends with an ellipsis
    when text is cut."""
    chars: List[Tuple[str, str]] = []
    on = False
    spans = 0
    for c in text:
        if c == "{":
            spans += 1
            on = spans <= max_spans  # only the first few marks: too many and nothing stands out
        elif c == "}":
            on = False
        elif c in "\n\t":
            chars.append((" ", base))
        else:
            chars.append((c, hi if on else base))
    words: List[List[Tuple[str, str]]] = []
    cur: List[Tuple[str, str]] = []
    for c, st in chars:
        if c == " ":
            if cur:
                words.append(cur)
                cur = []
        else:
            cur.append((c, st))
    if cur:
        words.append(cur)

    def w_of(chs) -> int:
        return sum(char_width(c) for c, _ in chs)

    lines: List[List[Tuple[str, str]]] = []
    line: List[Tuple[str, str]] = []
    i = 0
    while i < len(words) and len(lines) < max_lines:
        word = words[i]
        sep = [(" ", base)] if line else []
        if w_of(line) + w_of(sep) + w_of(word) <= max_width:
            line += sep + word
            i += 1
            continue
        if not line:  # a single word wider than the line: hard cut it
            cut, rest = [], list(word)
            while rest and w_of(cut) + char_width(rest[0][0]) <= max_width:
                cut.append(rest.pop(0))
            line, words[i] = cut, rest
            if not rest:
                i += 1
        lines.append(line)
        line = []
    if line and len(lines) < max_lines:
        lines.append(line)
    if i < len(words) and lines:
        last = lines[-1]
        while last and w_of(last) + width(ell) > max_width:
            last.pop()
        while last and last[-1][0] in " ,;:":
            last.pop()
        last.append((ell, last[-1][1] if last else base))
    out: List[Line] = []
    for chs in lines:
        segs: Line = []
        for c, st in chs:
            if segs and segs[-1][1] == st:
                segs[-1] = (segs[-1][0] + c, st)
            else:
                segs.append((c, st))
        out.append(segs)
    return out


VERDICTS = {  # tag -> (icon key, i18n label key, role)
    "DONE": ("v_done", "verdict.done", "done"),
    "PARTIAL": ("v_partial", "verdict.partial", "warn"),
    "FAILED": ("v_failed", "verdict.failed", "crit"),
    "WAITING": ("v_waiting", "verdict.waiting", "working"),
    "WORKING": ("v_working", "verdict.working", "working"),
}


# -- effort and battery --------------------------------------------------------------
def effort_role(level: Optional[Tuple[int, int]]) -> str:
    """Effort gets warmer as it rises: low/medium cool, high/xhigh gold, max red."""
    if not level:
        return "ok"
    n, total = level
    if n >= total and total >= 3:
        return "crit"
    return "warn" if n / float(total) > 0.4 else "ok"


# -- panel ------------------------------------------------------------------------
def render(view: View) -> Frame:
    return _render_panel(view)


def _render_panel(v: View) -> Frame:
    sym = ASCII if v.ascii else UNICODE
    padx = 2 if v.width >= 30 else 1
    inner = max(10, v.width - 2 * padx)
    status_role = STATUS_ROLES.get(v.status, "unknown")
    status_icon = sym.get({"done": "st_done"}.get(v.status, v.status), sym["unknown"])

    # Header
    left: Line = [(v.project or "—", "accent b")]
    if v.mode_tag:
        left.append((" {} {}".format(sym["sep"], v.mode_tag), "sub"))
    badge: Line = [(" {} {} ".format(status_icon, v.status_label), "bg:{} on:{} b".format(status_role, status_role))]
    top: List[Line] = [[], _lr(left, badge, inner), []]
    top.append([(sym["rule"] * inner, "rule")])
    top.append([])
    if v.notif:
        top.append([(sym["notif"] + " ", v.notif[1] + " b"), (v.notif[0], v.notif[1] + " b")])
        top.append([])
    fix_y = -1
    if v.inferred_note:
        top.append([(sym["infer"] + " ", "warn b"), (v.inferred_note, "sub")])
        if v.inferred_fix:
            fix_y = len(top)
            top.append([("  " + sym["now"] + " ", "accent b"), (truncate(v.inferred_fix, inner - 4, sym["ell"]), "accent b")])
        top.append([])

    # Main block
    main: List[Line] = []
    if v.main_kind != "none":
        if v.session_title:
            # What this conversation is about (Claude's own session title).
            main.append([(sym["session"] + " ", "accent"), (truncate(v.session_title, inner - 2, sym["ell"]), "accent")])
            main.append([])
        # Always the same yellow marker, whatever the state: this is where to look.
        main.append(_lr([(sym["now"] + " ", "working b"), (v.main_title, "working b")],
                        [(v.main_right, "sub")], inner))
        for line in rich_wrap(v.ask, inner, v.ask_max_lines, "text b", "working b", sym["ell"]) if v.ask else []:
            main.append(line)
        for point in v.ask_points[:3]:
            for i, line in enumerate(rich_wrap(point, inner - 2, 2, "text", "accent b", sym["ell"])):
                main.append([(sym["point"] + " " if i == 0 else "  ", "accent")] + line)
        if v.activity:
            main.append([(sym[v.activity_icon] + " ", v.activity_role), (v.activity, v.activity_role)])
        if v.tasks and v.tasks[1]:
            done, total = v.tasks
            cells = 10
            filled = int(round(done / total * cells))
            main.append([(v.tasks_label + " ", "sub"), (sym["full"] * filled, "ok"),
                         (sym["empty"] * (cells - filled), "dim"), (" {}/{}".format(done, total), "text")])
            if v.task_now:
                main.append([("  " + sym["act"] + " ", "sub"), (truncate(v.task_now, inner - 4, sym["ell"]), "sub")])
        if v.reply:
            main.append([])
            verdict = VERDICTS.get(v.reply_verdict)
            if verdict:  # what was done, and whether it is done, in one colored word
                icon, _, role = verdict
                main.append([(sym[icon] + " ", role + " b"), (v.reply_verdict_label, role + " b")])
            else:
                main.append([(sym["reply"] + " ", "done b"), (v.reply_label, "done b")])
            hi = (verdict[2] if verdict else "done") + " b"
            lines = v.reply.split("\n")
            for line in rich_wrap(lines[0], inner, 3 if len(lines) == 1 else 2, "text", hi, sym["ell"]):
                main.append(line)
            for extra in lines[1:]:
                if extra.startswith("- "):
                    for i, line in enumerate(rich_wrap(extra[2:], inner - 2, 2, "text", hi, sym["ell"])):
                        main.append([(sym["point"] + " " if i == 0 else "  ", hi.split()[0])] + line)
                elif extra.startswith("? "):
                    for i, line in enumerate(rich_wrap(extra[2:], inner - 2, 2, "working", "working b", sym["ell"])):
                        main.append([("? " if i == 0 else "  ", "working b")] + line)
    for text in v.message_lines:
        main.append([(text, "sub")])

    # Bottom blocks (stick to the footer)
    blocks: List[Tuple[str, List[Line]]] = []
    if v.before:
        b: List[Line] = [[(sym["before"] + " ", "sub b"), (v.before_title, "sub b")]]
        for entry in v.before[:3]:
            when, text = entry[0], entry[1]
            verdict = VERDICTS.get(entry[2] if len(entry) > 2 else "")
            # Real outcome when known (rephrasing on), else a neutral marker: never a fake ✓.
            icon, role = (sym[verdict[0]], verdict[2]) if verdict else (sym["now"], "sub")
            room = inner - width(icon) - width(when) - 2
            b.append([(icon + " ", role), (when + " ", "sub"), (truncate(text, room, sym["ell"]), "text")])
        blocks.append(("before", b))
    if v.files:
        names = ", ".join(v.files)
        right: Line = [(str(len(v.files)), "sub")]
        if v.diff and (v.diff[0] or v.diff[1]):
            right = [("+{}".format(v.diff[0]), "ok b"), (" ", ""), ("−{}".format(v.diff[1]) if not v.ascii
                     else "-{}".format(v.diff[1]), "crit b"), ("  " + str(len(v.files)), "sub")]
        blocks.append(("files", [
            _lr([(sym["files"] + " ", "sub b"), (v.files_title, "sub b")], right, inner),
            [(truncate(names, inner, sym["ell"]), "text")],
        ]))
    if v.alerts:
        blocks.append(("alerts", [[(sym["alert"] + " ", "warn b"), (a, "warn")] for a in v.alerts[:2]]))

    footer: List[Line] = [[], []]  # two blank lines mark the footer
    hits: List[Hit] = []
    sel = next((b for b in v.bars if b.key == v.selected_bar), None)
    detail_line: Optional[Line] = None
    if sel and sel.detail:
        detail_line = [(sym[sel.detail_icon] + " ", "accent b bg:tip"), (sel.detail, "text b bg:tip"),
                       ((" " + sel.detail_sub) if sel.detail_sub else "", "sub bg:tip")]
    machine: Line = []
    for part in (v.host, v.branch):
        if part:
            if machine:
                machine.append((" {} ".format(sym["sep"]), "dim"))
            machine.append((part, "sub"))
    if v.model:
        if machine:
            machine.append((" {} ".format(sym["sep"]), "dim"))
        machine.append((sym["llm"] + " ", "accent"))
        machine.append(((v.model + (" " + v.version if v.version else "")), "sub"))
        if v.effort:
            machine.append((" " + v.effort, effort_role(v.effort_level) if v.effort_level else "accent"))
    mw = sum(width(t) for t, _ in machine)
    if machine and mw < inner:  # centered above the gauges
        machine = [(" " * ((inner - mw) // 2), "")] + machine
    footer.append(machine)
    bar_line: Line = []
    bar_hits: List[Tuple[int, int, str]] = []
    x = 0
    cells = max(4, (inner - sum(len(b.label) + 1 for b in v.bars) - (len(v.bars) - 1)) // max(1, len(v.bars)))
    for i, bar in enumerate(v.bars):
        if i:
            bar_line.append((" ", ""))
            x += 1
        label_role = "accent b" if bar.key == v.selected_bar else "sub b"
        bar_line.append((bar.label + " ", label_role))
        start = x
        x += len(bar.label) + 1
        text = "—" if bar.pct is None else "{}%".format(int(round(bar.pct)))
        if v.ascii and bar.pct is None:
            text = "-"
        bar_line += _bar_cells(bar.pct, cells, pct_role(bar.pct), text)
        x += cells
        bar_hits.append((start, x, "bar:" + bar.key))
    if v.bars:
        footer.append(bar_line)
    # One line under the gauges is always reserved: the hovered/clicked gauge's detail
    # shows there, otherwise the footnote (setup, stale quotas). Nothing ever moves.
    footnote_line: Optional[Line] = None
    if v.footnote:
        icon = (sym[v.footnote_icon] + " ") if v.footnote_icon else ""
        footnote_line = [(icon + v.footnote, "accent b" if v.footnote_action else "dim")]
    footer.append(detail_line or footnote_line or [])
    if detail_line:
        footnote_line = None
    # Bottom-right menu button (no right click: some terminals keep it for themselves).
    buttons: Line = [(" " * max(0, inner - 1), ""), (sym["more"], "sub b")]
    footer.append(buttons)

    # Fit to height: shrink optional parts until everything fits.
    def total() -> int:
        return len(top) + len(main) + sum(len(b) for _, b in blocks) + max(0, len(blocks) - 1) + len(footer)

    for _ in range(12):
        if total() <= v.height:
            break
        if blocks and any(k == "files" for k, _ in blocks):
            blocks = [(k, b) for k, b in blocks if k != "files"]
        elif blocks and any(k == "before" and len(b) > 2 for k, b in blocks):
            blocks = [(k, b[:-1] if k == "before" else b) for k, b in blocks]
        elif blocks:
            blocks.pop(0)
        elif len(main) > 2:
            main.pop()
        else:
            break

    body: List[Line] = top + main
    gap = v.height - total()
    body += [[] for _ in range(max(0, gap))]
    for i, (_, b) in enumerate(blocks):
        if i:
            body.append([])
        body += b
    footer_start = len(body)
    body += footer
    body = body[: v.height]

    lines: List[Line] = []
    for line in body:
        if line and any("bg:tip" in s for _, s in line):
            # Detail tip: background spans one column into the padding.
            lines.append([(" " * (padx - 1), "")] + _fit_bg(line, inner + 2) + [(" " * (padx - 1), "")])
        else:
            lines.append([(" " * padx, "")] + _fit(line, inner) + [(" " * padx, "")])

    bar_y = footer_start + footer.index(bar_line) if v.bars and bar_line in footer else -1
    if 0 <= bar_y < v.height:
        for x0, x1, action in bar_hits:
            hits.append(Hit(bar_y, padx + x0, padx + x1, action))
    if 0 <= fix_y < v.height:
        hits.append(Hit(fix_y, 0, v.width, "setup"))
    if footnote_line is not None and v.footnote_action:
        fy = footer_start + next(i for i, l in enumerate(footer) if l is footnote_line)
        if 0 <= fy < v.height:
            hits.append(Hit(fy, 0, v.width, v.footnote_action))
    by = footer_start + next(i for i, l in enumerate(footer) if l is buttons)
    if 0 <= by < v.height:
        hits.append(Hit(by, padx + inner - 3, v.width, "menu"))
    hits.append(Hit(-1, 0, v.width, "panel"))

    if v.menu:
        _overlay_menu(lines, hits, v, sym, padx, inner, bottom=min(by, v.height) if by >= 0 else v.height)
    return Frame(lines, hits)


def _fit_bg(line: Line, total: int) -> Line:
    out = _fit([(" ", "bg:tip")] + line, total)
    if out and out[-1][1] == "":
        out[-1] = (out[-1][0], "bg:tip")
    return out


def _overlay_menu(lines: List[Line], hits: List[Hit], v: View, sym, padx: int, inner: int,
                  bottom: int = 3) -> None:
    """Menu box opening just above the bottom-right buttons."""
    items = v.menu or []
    box_w = min(inner + 2, max(width(lbl) for _, _, _, lbl in items) + 6)
    x = max(0, v.width - padx - box_w + 1)
    y = max(0, bottom - len(items))
    rows: List[Tuple[Line, Optional[str]]] = []
    for idx, (action, icon, role, label) in enumerate(items):
        if action == "-":
            rows.append(([(" " + sym["rule"] * (box_w - 2) + " ", "dim bg:tip")], None))
            continue
        selected = idx == v.menu_index
        base = "bg:accent on:accent b" if selected else "bg:tip text"
        icon_style = base if selected else "bg:tip " + role
        rows.append(([(" " + icon + " ", icon_style), (pad(label, box_w - 3), base)], action))
    hits.insert(0, Hit(-2, 0, v.width, "menu:outside"))
    for i, (row, action) in enumerate(rows):
        yy = y + i
        if yy >= len(lines):
            break
        lines[yy] = _splice(lines[yy], x, _fit(row, box_w), box_w)
        if action:
            hits.insert(0, Hit(yy, x, x + box_w, "menu:" + action))


def _splice(line: Line, x: int, insert: Line, w: int) -> Line:
    """Replace columns [x, x+w) of a line with `insert`."""
    flat: List[Tuple[str, str]] = []
    for text, style in line:
        for ch in text:
            flat.append((ch, style))
    while len(flat) < x + w:
        flat.append((" ", ""))
    left = flat[:x]
    right = flat[x + w:]
    out: Line = [(c, s) for c, s in left] + insert + [(c, s) for c, s in right]
    return out
