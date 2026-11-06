"""Terminal loop for the eartag pane: raw mode, mouse, redraws, menu."""
from __future__ import annotations

import os
import queue
import re
import select
import shutil
import signal
import subprocess
import sys
import termios
import threading
import time
import tty
from pathlib import Path
from typing import List, Optional, Tuple

from . import herdr, prefs
from .config import Config, herdr_border_overhead
from .i18n import Tr
from .panel import PanelModel
from .render import UNICODE, ASCII, Frame, Hit, render
from .theme import Theme

MIN_PANEL_W = 30   # below this the full panel is unreadable: show the strip
MIN_PANEL_H = 18
MIN_AGENT_W = 60   # never squeeze the agent pane below this when opening
_MOUSE_RE = re.compile(rb"\x1b\[<(\d+);(\d+);(\d+)([mM])")
RELOAD_CHECK = 2.0  # seconds between looks at the plugin's own files

# The plugin directory (herdr keeps it at a fixed path across updates).
PLUGIN_ROOT = Path(__file__).resolve().parents[1]


def code_fingerprint(root: Path = PLUGIN_ROOT) -> Optional[Tuple]:
    """Name, mtime and size of every source file; None while the dir is missing
    (herdr replacing it during an update)."""
    try:
        files = sorted((root / "herdr_eartag").glob("*.py")) + [root / "eartag.py"]
        return tuple((f.name, f.stat().st_mtime_ns, f.stat().st_size) for f in files)
    except OSError:
        return None


def code_imports(root: Path = PLUGIN_ROOT) -> bool:
    """True when the new code loads: a broken update must not take the panel down."""
    check = "import sys; sys.path.insert(0, sys.argv[1]); import herdr_eartag.cli, herdr_eartag.tui"
    import tempfile
    try:
        # A fresh bytecode cache: a stale .pyc (same size, same second) would hide the change.
        with tempfile.TemporaryDirectory(prefix="eartag-check-") as cache:
            proc = subprocess.run([sys.executable, "-X", "pycache_prefix=" + cache, "-c", check, str(root)],
                                  cwd=str(root), capture_output=True, timeout=10, stdin=subprocess.DEVNULL)
    except (OSError, subprocess.SubprocessError):
        return False
    return proc.returncode == 0


class Screen:
    def __init__(self, theme: Theme):
        self.theme = theme
        self.fd = sys.stdin.fileno()
        self.saved = None
        self.last = ""

    def __enter__(self) -> "Screen":
        if os.isatty(self.fd):
            self.saved = termios.tcgetattr(self.fd)
            tty.setraw(self.fd)
        self.write("\x1b[?1049h\x1b[?25l\x1b[?7l\x1b[?1000h\x1b[?1003h\x1b[?1006h")
        return self

    def __exit__(self, *exc) -> None:
        self.write("\x1b[?1006l\x1b[?1003l\x1b[?1000l\x1b[?7h\x1b[?25h\x1b[0m\x1b[?1049l")
        if self.saved is not None:
            termios.tcsetattr(self.fd, termios.TCSADRAIN, self.saved)

    @staticmethod
    def write(data: str) -> None:
        sys.stdout.write(data)
        sys.stdout.flush()

    def size(self):
        size = shutil.get_terminal_size((40, 30))
        return size.columns, size.lines

    def _sgr(self, style: str) -> str:
        codes = ["0"]
        for token in style.split():
            if token == "b":
                codes.append("1")
            elif token.startswith("bg:"):
                codes.append(self.theme.bg(token[3:]))
            elif token.startswith("on:"):
                codes.append(self.theme.on(token[3:]))
            else:
                codes.append(self.theme.fg(token))
        if self.theme.paint_background and "bg:" not in style:
            codes.append(self.theme.bg("panel"))
        if not any(c.startswith("38") or (c.isdigit() and (30 <= int(c) <= 37 or 90 <= int(c) <= 97)) for c in codes):
            codes.append(self.theme.fg("text"))
        return "\x1b[" + ";".join(codes) + "m"

    def draw(self, frame: Frame) -> None:
        out: List[str] = []
        for y, line in enumerate(frame.lines):
            out.append("\x1b[{};1H".format(y + 1))
            for text, style in line:
                out.append(self._sgr(style) + text)
            out.append(self._sgr("") + "\x1b[K")  # erase with the panel background
        cols, rows = self.size()
        for y in range(len(frame.lines), rows):
            out.append("\x1b[{};1H".format(y + 1) + self._sgr("") + "\x1b[K")
        data = "".join(out)
        if data != self.last:
            self.last = data
            self.write(data)


def _layout_width(me: str, target: str):
    """Return (my_width, split_width, split_ratio) for the split between target and me."""
    lay = herdr.pane_layout(me)
    rects = {p.get("pane_id"): p.get("rect") or {} for p in lay.get("panes") or []}
    mine = rects.get(me)
    theirs = rects.get(target)
    if not mine or not theirs:
        return None
    best = None
    for split in lay.get("splits") or []:
        if str(split.get("direction")).lower() not in ("right", "horizontal"):
            continue
        r = split.get("rect") or {}
        contains = all(
            r.get("x", 0) <= p["x"] and p["x"] + p["width"] <= r.get("x", 0) + r.get("width", 0)
            and r.get("y", 0) <= p["y"] and p["y"] + p["height"] <= r.get("y", 0) + r.get("height", 0)
            for p in (mine, theirs)
        )
        if contains and (best is None or r["width"] < best["rect"]["width"]):
            best = split
    if best is None:
        return None
    return mine["width"], best["rect"]["width"], float(best.get("ratio", 0.5))


def set_width(me: str, target: str, want: int) -> None:
    """Resize the split between the agent pane (left) and the panel (right)."""
    for _ in range(3):
        try:
            info = _layout_width(me, target)
        except herdr.HerdrError:
            return
        if not info:
            return
        mine, total, _ratio = info
        diff = mine - want
        if abs(diff) <= 1 or total <= 0:
            return
        amount = min(0.5, abs(diff) / float(total))
        try:
            if diff > 0:
                herdr.pane_resize(target, "right", amount)  # agent grows, panel shrinks
            else:
                herdr.pane_resize(me, "left", amount)       # panel grows
        except herdr.HerdrError:
            return


def log_error() -> None:
    """Background failures go to a log file (the pane's own output is the UI)."""
    import traceback
    from .config import ensure_private_dir, state_dir
    try:
        path = ensure_private_dir(state_dir()) / "eartag.log"
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(time.strftime("%Y-%m-%d %H:%M:%S ") + traceback.format_exc() + "\n")
    except OSError:
        pass


class App:
    def __init__(self, target: str, me: Optional[str], cfg: Config, tr: Tr):
        self.target = target
        self.me = me
        self.cfg = cfg
        self.tr = tr
        self.theme = Theme.load(cfg.colors, cfg.background)
        self.model = PanelModel(target, cfg, tr)
        # Widths below are content columns; herdr's frame takes a few more inside the pane.
        self.overhead = herdr_border_overhead()
        self.pinned_bar: Optional[str] = None
        self.hover_bar: Optional[str] = None
        self.menu_open = False
        self.menu_index = 0
        self.frame: Optional[Frame] = None
        self.dirty = True
        self.running = True
        self.resized = False
        self.next_tick = 0.0
        # Hot reload: when the plugin's files change (herdr plugin update, or edits to
        # a linked dev copy), the panel re-executes itself in place with the new code.
        self.code = code_fingerprint()
        self.code_seen = self.code
        self.next_code_check = time.time() + RELOAD_CHECK
        self.reload = False
        # herdr calls (resize, focus, notify, setup) run here so the input loop
        # never stops reading the pane while herdr is busy.
        self._jobs: "queue.Queue" = queue.Queue()
        threading.Thread(target=self._worker, daemon=True).start()

    def _worker(self) -> None:
        while True:
            job = self._jobs.get()
            try:
                job()
            except Exception:  # a failed herdr call must never kill the panel
                log_error()
            self.dirty = True

    def later(self, job) -> None:
        self._jobs.put(job)

    # -- lifecycle ---------------------------------------------------------------
    def register(self) -> None:
        if not self.me:
            return
        with prefs.locked() as data:
            data["panels"][self.target] = self.me
            data["pids"][self.me] = os.getpid()
        if os.environ.pop("EARTAG_RELOADED", None):
            return  # same pane after a hot reload: keep the width the user left it at
        try:
            # No title on the pane border: herdr labels plugin panes with their title.
            herdr.call("pane", "rename", self.me, "--clear")
        except herdr.HerdrError:
            pass
        # Full width when the agent keeps enough room, else the narrowest readable panel.
        self.resize_to(self.cfg.width if self._room_for(self.cfg.width) else MIN_PANEL_W)

    def resize_to(self, columns: int) -> None:
        set_width(self.me, self.target, columns + self.overhead)

    def _room_for(self, panel_width: int) -> bool:
        """True when the split can give `panel_width` columns and keep the agent readable."""
        if not self.me:
            return True
        try:
            info = _layout_width(self.me, self.target)
        except herdr.HerdrError:
            return True
        if not info:
            return True
        _mine, total, _ratio = info
        return total - panel_width - self.overhead - 1 >= MIN_AGENT_W

    def unregister(self) -> None:
        if not self.me:
            return
        try:
            with prefs.locked() as data:
                if data["panels"].get(self.target) == self.me:
                    data["panels"].pop(self.target, None)
                data["pids"].pop(self.me, None)
        except OSError:
            pass

    def close_self(self) -> None:
        self.running = False
        self.unregister()
        if self.me:
            try:
                herdr.pane_close(self.me)
            except herdr.HerdrError:
                pass

    def check_code(self, now: float) -> None:
        """Ask for a reload once the files changed and stayed put for one check."""
        if now < self.next_code_check:
            return
        self.next_code_check = now + RELOAD_CHECK
        current = code_fingerprint()
        if current is None or current == self.code:
            self.code_seen = current
            return
        if current != self.code_seen:  # still being written: wait for it to settle
            self.code_seen = current
            return
        if code_imports():
            self.reload = True
            self.running = False
        else:
            self.code = current  # broken: keep running the old code until the next change

    def exec_new_code(self) -> None:
        """Replace this process with the updated panel (same pane, pid and env)."""
        os.environ["EARTAG_RELOADED"] = "1"
        entry = PLUGIN_ROOT / "eartag.py"
        try:
            os.chdir(str(PLUGIN_ROOT))  # the old cwd may be a replaced directory
            os.execv(sys.executable, [sys.executable, str(entry)] + sys.argv[1:])
        except OSError:
            log_error()

    # -- actions -----------------------------------------------------------------
    def menu_items(self):
        sym = ASCII if self.cfg.icons == "ascii" else UNICODE
        auto = prefs.auto_open_enabled(prefs.load(), self.cfg.auto_open)
        tr = self.tr
        setup_item = [("setup", sym["now"], "accent", tr("menu.setup"))] if self.model.needs_setup else []
        return setup_item + [
            ("hide_agent", sym["hide"], "blocked", tr("menu.hide_agent")),
            ("hide_workspace", sym["hide"], "blocked", tr("menu.hide_workspace")),
            ("-", "", "", ""),
            ("auto", sym["auto_off"] if auto else sym["auto_on"], "sub",
             tr("menu.auto_off") if auto else tr("menu.auto_on")),
        ] + self._summarize_items(sym) + [
            ("-", "", "", ""),
            ("close", "", "sub", "{}  {}".format(tr("menu.esc"), tr("menu.close"))),
        ]

    def _summarize_items(self, sym):
        if self.model.summarizer is not None:
            label = self.tr("menu.summarize_on", b=self.model.summarizer.backend)
            return [("summarize", sym["pending"], "accent", label)]
        from .summarize import resolve_backend
        found = resolve_backend(self.cfg.summarize if self.cfg.summarize != "off" else "auto")
        if found:
            return [("summarize", sym["auto_off"], "sub", self.tr("menu.summarize_start", b=found[0]))]
        return []

    def run_action(self, action: str) -> None:
        self.menu_open = False
        self.dirty = True
        tr = self.tr
        if action == "hide_agent":
            with prefs.locked() as data:
                data["hidden_panes"][self.target] = time.time()
            herdr.notify(tr("toast.hidden_agent"), tr("toast.reopen", key=self.cfg.toggle_key))
            self.close_self()  # exits: synchronous on purpose
        elif action == "hide_workspace":
            ws = (self.model.pane or {}).get("workspace_id")
            if ws:
                with prefs.locked() as data:
                    data["hidden_workspaces"][ws] = time.time()
            herdr.notify(tr("toast.hidden_workspace"), tr("toast.reopen", key=self.cfg.toggle_key))
            self.close_self()
        elif action == "summarize":
            on = self.model.summarizer is None
            with prefs.locked() as data:
                data["summarize"] = on
            self.model.set_summarize(on)
            if on and self.model.summarizer is not None:
                backend = self.model.summarizer.backend
                self.later(lambda: herdr.notify(tr("toast.summarize_on", b=backend), tr("toast.summarize_on_body", b=backend)))
        elif action == "setup":
            def do_setup() -> None:
                from . import setup
                lines = setup.run_all()
                self.model.needs_setup = setup.needs_setup(self.model.kind)
                body = tr("toast.setup_body") if not self.model.needs_setup else " ".join(lines)
                herdr.notify(tr("toast.setup_done"), body)
            self.later(do_setup)
        elif action == "auto":
            with prefs.locked() as data:
                current = prefs.auto_open_enabled(data, self.cfg.auto_open)
                data["auto_open"] = not current
            if current:
                self.later(lambda: herdr.notify(tr("toast.auto_off"), tr("toast.reopen", key=self.cfg.toggle_key)))

    # -- input -------------------------------------------------------------------
    def hit_at(self, x: int, y: int) -> Optional[Hit]:
        if not self.frame:
            return None
        for hit in self.frame.hits:
            if hit.y in (-1, -2) or hit.y == y:
                if hit.y >= 0 and not (hit.x0 <= x < hit.x1):
                    continue
                return hit
        return None

    def on_mouse(self, button: int, x: int, y: int, press: bool) -> None:
        motion = bool(button & 32)
        base = button & 3
        if button & 64:  # wheel
            return
        hit = self.hit_at(x, y)
        action = hit.action if hit else ""
        if motion:
            hover = action[4:] if action.startswith("bar:") else None
            if self.menu_open and action.startswith("menu:"):
                items = [a for a, _, _, _ in self.menu_items()]
                name = action[5:]
                if name in items and items.index(name) != self.menu_index:
                    self.menu_index = items.index(name)
                    self.dirty = True
            if hover != self.hover_bar:
                self.hover_bar = hover
                self.dirty = True
            return
        # Act on release: herdr keeps the pointer captured for this pane between press
        # and release, so all slow work must happen after the gesture is over.
        if press:
            return
        if base != 0:
            return
        if self.menu_open:
            if action.startswith("menu:") and action != "menu:outside":
                name = action[5:]
                if name == "close":
                    self.menu_open = False
                    self.dirty = True
                else:
                    self.run_action(name)
            else:
                self.menu_open = False
                self.dirty = True
            self.give_back_focus()
            return
        if action == "menu":
            self.menu_open = True
            self.menu_index = 0
            self.dirty = True
            return  # keep focus: the open menu takes arrows and Enter
        elif action.startswith("bar:"):
            key = action[4:]
            self.pinned_bar = None if self.pinned_bar == key else key
            self.dirty = True
        elif action == "setup":
            self.run_action("setup")
        self.give_back_focus()

    def on_key(self, key: bytes) -> None:
        items = [a for a, _, _, _ in self.menu_items()]
        if self.menu_open:
            if key in (b"\x1b", b"q"):
                self.menu_open = False
            elif key in (b"\x1b[A", b"k"):
                self.menu_index = self._step(items, -1)
            elif key in (b"\x1b[B", b"j"):
                self.menu_index = self._step(items, 1)
            elif key in (b"\r", b"\n", b" "):
                name = items[self.menu_index]
                if name == "close":
                    self.menu_open = False
                else:
                    self.run_action(name)
            if not self.menu_open:
                self.give_back_focus()
            self.dirty = True
            return
        if key == b"\x1b[29~":  # Menu key
            self.menu_open = True
            self.menu_index = 0
        elif key == b"\x1b":
            self.pinned_bar = None
            self.give_back_focus()
        self.dirty = True

    def give_back_focus(self) -> None:
        """Clicking the panel focuses it in herdr; hand the keyboard back to the agent."""
        if not self.me or not self.running:
            return
        self.later(lambda: herdr.pane_focus(self.target))

    def forward_text(self, raw: bytes) -> None:
        text = raw.decode("utf-8", errors="ignore")
        printable = "".join(c for c in text if c.isprintable())
        if self.me and printable:
            self.later(lambda: herdr.send_text(self.target, printable))
        self.give_back_focus()

    def _step(self, items, delta: int) -> int:
        i = self.menu_index
        for _ in range(len(items)):
            i = (i + delta) % len(items)
            if items[i] != "-":
                return i
        return self.menu_index

    def parse_input(self, data: bytes) -> None:
        pos = 0
        while pos < len(data):
            m = _MOUSE_RE.match(data, pos)
            if m:
                self.on_mouse(int(m.group(1)), int(m.group(2)) - 1, int(m.group(3)) - 1, m.group(4) == b"M")
                pos = m.end()
                continue
            if data[pos:pos + 1] == b"\x1b":
                seq = re.match(rb"\x1b\[[0-9;]*[A-Za-z~]", data[pos:])
                if seq:
                    self.on_key(seq.group(0))
                    pos += len(seq.group(0))
                    continue
                self.on_key(b"\x1b")
                pos += 1
                continue
            if not self.menu_open:
                # Text typed while the panel has focus belongs to the agent: pass it on.
                end = pos
                while end < len(data) and data[end:end + 1] != b"\x1b":
                    end += 1
                self.forward_text(data[pos:end])
                pos = end
                continue
            self.on_key(data[pos:pos + 1])
            pos += 1

    # -- main loop ---------------------------------------------------------------
    def build(self, screen: Screen) -> Frame:
        cols, rows = screen.size()
        view = self.model.view(cols, rows, self.pinned_bar or self.hover_bar)
        if self.menu_open:
            view.menu = self.menu_items()
            view.menu_index = self.menu_index
        return render(view)

    def run(self) -> int:
        def on_winch(*_):
            self.resized = True

        def on_term(*_):
            self.running = False

        signal.signal(signal.SIGWINCH, on_winch)
        signal.signal(signal.SIGTERM, on_term)
        signal.signal(signal.SIGHUP, on_term)
        self.register()
        fd = sys.stdin.fileno()
        with Screen(self.theme) as screen:
            while self.running:
                now = time.time()
                if self.model.poll(now):
                    self.dirty = True
                if self.model.pane_missing:
                    self.close_self()
                    break
                self.check_code(now)
                if self.reload:
                    break
                if now >= self.next_tick:  # relative times ("for 4 min") move on
                    self.next_tick = now + 15
                    self.dirty = True
                if self.resized:
                    self.resized = False
                    screen.last = ""
                    self.dirty = True
                if self.dirty:
                    self.dirty = False
                    self.frame = self.build(screen)
                    screen.draw(self.frame)
                try:
                    ready, _, _ = select.select([fd], [], [], 0.25)
                except (InterruptedError, OSError):
                    continue
                if ready:
                    try:
                        data = os.read(fd, 4096)
                    except OSError:
                        data = b""
                    if not data:
                        break
                    self.parse_input(data)
        if self.reload:
            self.exec_new_code()  # only returns if exec failed
        self.unregister()
        return 0
