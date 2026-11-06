"""Command line entry: herdr pane, actions, event hook, statusline tap, setup."""
from __future__ import annotations

import argparse
import contextlib
import json
import os
import sys
from typing import Any, Iterator, Optional

from . import herdr, prefs
from .config import Config, claude_dirs as config_claude_dirs, config_dir, ensure_private_dir, load_config, state_dir
from .i18n import Tr, detect_language
from .setup import plugin_id as setup_plugin_id


try:
    import fcntl  # type: ignore
except ImportError:  # pragma: no cover
    fcntl = None  # type: ignore


def _tr(cfg: Config) -> Tr:
    return Tr(detect_language() if cfg.language == "auto" else cfg.language)


def _context() -> dict:
    try:
        data = json.loads(os.environ.get("HERDR_PLUGIN_CONTEXT_JSON") or "{}")
        return data if isinstance(data, dict) else {}
    except ValueError:
        return {}


def _find_key(obj: Any, key: str) -> Optional[str]:
    if isinstance(obj, dict):
        if isinstance(obj.get(key), str):
            return obj[key]
        for value in obj.values():
            found = _find_key(value, key)
            if found:
                return found
    elif isinstance(obj, list):
        for value in obj:
            found = _find_key(value, key)
            if found:
                return found
    return None


@contextlib.contextmanager
def _open_lock() -> Iterator[None]:
    path = ensure_private_dir(state_dir()) / ".open.lock"
    with open(path, "a") as fh:
        if fcntl is not None:
            fcntl.flock(fh, fcntl.LOCK_EX)
        try:
            yield
        finally:
            if fcntl is not None:
                fcntl.flock(fh, fcntl.LOCK_UN)


def _alive(pid) -> bool:
    try:
        os.kill(int(pid), 0)
        return True
    except (OSError, ValueError, TypeError):
        return False


def _is_plugin_dir(path: Optional[str]) -> bool:
    from pathlib import Path
    root = os.environ.get("HERDR_PLUGIN_ROOT") or str(Path(__file__).resolve().parents[1])
    try:
        return bool(path) and Path(path).resolve() == Path(root).resolve()
    except OSError:
        return False


def live_panel(target: str) -> Optional[str]:
    """The agent's panel pane if it really runs Eartag. After a herdr restart the
    panel's pane comes back as a plain shell (a ghost): close it and forget it."""
    data = prefs.load()
    panel = data["panels"].get(target)
    if not panel:
        return None
    pane = herdr.pane_get(panel)
    if pane is None:
        return None
    pid = data["pids"].get(panel)
    if pid is not None and not _alive(pid):
        # Close it only if it really looks like our old panel (no agent, still in the
        # plugin's folder); a pane id herdr reused for one of the user's shells is kept.
        if not pane.get("agent") and _is_plugin_dir(pane.get("cwd") or pane.get("foreground_cwd")):
            try:
                herdr.pane_close(panel)
            except herdr.HerdrError:
                pass
        with prefs.locked() as d:
            d["panels"].pop(target, None)
            d["pids"].pop(panel, None)
        return None
    return panel


def panel_owner(pane_id: str) -> Optional[str]:
    """Agent pane whose eartag panel is `pane_id` (None if `pane_id` is not a live panel)."""
    for target, panel in prefs.load()["panels"].items():
        if panel == pane_id:
            if herdr.pane_get(target) is None:
                prune_registry()
                return None
            return target
    return None


def prune_registry() -> None:
    """Drop registry entries whose panel or agent pane no longer exists, and close
    ghost panels (a restored plain shell where an Eartag panel used to be)."""
    for target in list(prefs.load()["panels"]):
        try:
            live_panel(target)
        except herdr.HerdrError:
            pass
    with prefs.locked() as data:
        for target, panel in list(data["panels"].items()):
            try:
                alive = herdr.pane_get(panel) is not None and herdr.pane_get(target) is not None
            except herdr.HerdrError:
                continue
            if not alive:
                data["panels"].pop(target, None)
                data["pids"].pop(panel, None)


def cmd_unhide(args) -> int:
    """Show every hidden agent again (works without the prefix+i key)."""
    with prefs.locked() as data:
        data["hidden_panes"].clear()
        data["hidden_workspaces"].clear()
    opened = 0
    for agent in herdr.agent_list():
        pane_id = agent.get("pane_id")
        if pane_id and not panel_owner(pane_id) and not live_panel(pane_id):
            open_for(pane_id)
            opened += 1
    print("{} panel(s) opened.".format(opened))
    return 0


def cmd_startup(args) -> int:
    """herdr startup hook: clean ghosts left by the previous server, then let the
    agent_detected events reopen real panels."""
    prune_registry()
    return 0


def open_for(target: str) -> Optional[str]:
    prune_registry()
    with _open_lock():
        existing = live_panel(target)
        if existing:
            return existing
        plugin_id = setup_plugin_id()
        result = herdr.open_panel(plugin_id, target, {"EARTAG_TARGET": target})
        panel = None
        pane = result.get("pane") if isinstance(result, dict) else None
        if isinstance(pane, dict) and isinstance(pane.get("pane_id"), str):
            panel = pane["pane_id"]
        panel = panel or _find_key(result, "pane_id")
        if panel and panel != target:
            with prefs.locked() as data:
                data["panels"][target] = panel
        return panel


# -- commands --------------------------------------------------------------------
def cmd_panel(args) -> int:
    from .tui import App
    cfg = load_config()
    target = args.target or os.environ.get("EARTAG_TARGET")
    if not target:
        print("herdr-eartag: missing target pane (EARTAG_TARGET)", file=sys.stderr)
        return 2
    # Only touch our own pane (resize, close, input routing) when herdr launched us as
    # the plugin's pane entrypoint; a manual run inside another pane must not act on it.
    me = os.environ.get("HERDR_PANE_ID") if os.environ.get("HERDR_PLUGIN_ENTRYPOINT_ID") == "panel" else None
    if me == target:
        me = None
    from . import setup
    setup.refresh_tap()
    return App(target, me, cfg, _tr(cfg)).run()


def cmd_toggle(args) -> int:
    focused = args.pane or os.environ.get("HERDR_PANE_ID") or _context().get("focused_pane_id")
    if not focused:
        print("herdr-eartag: no focused pane", file=sys.stderr)
        return 1
    owner = panel_owner(focused)
    if owner:  # toggled from inside the panel itself
        target, panel = owner, focused
    else:
        target, panel = focused, live_panel(focused)
    if panel:
        with prefs.locked() as data:
            data["hidden_panes"][target] = 1
            data["panels"].pop(target, None)
        herdr.pane_close(panel)
        return 0
    with prefs.locked() as data:
        data["hidden_panes"].pop(target, None)
    open_for(target)
    return 0


def cmd_event(args) -> int:
    cfg = load_config()
    try:
        event = json.loads(os.environ.get("HERDR_PLUGIN_EVENT_JSON") or "{}")
    except ValueError:
        event = {}
    pane_id = _find_key(event, "pane_id") or os.environ.get("HERDR_PANE_ID")
    workspace_id = _find_key(event, "workspace_id") or os.environ.get("HERDR_WORKSPACE_ID")
    if not pane_id:
        return 0
    data = prefs.load()
    if not prefs.auto_open_enabled(data, cfg.auto_open):
        return 0
    if prefs.is_hidden(data, pane_id, workspace_id) or panel_owner(pane_id):
        return 0
    pane = herdr.pane_get(pane_id)
    if not pane or not pane.get("agent"):
        return 0
    open_for(pane_id)
    return 0


def cmd_tap(args) -> int:
    from . import statusline
    return statusline.run_tap(args.then)


def cmd_setup(args) -> int:
    from . import setup
    for line in (setup.undo_all() if args.remove else setup.run_all()):
        print(line)
    if not args.remove:
        print("Undo everything with: herdr plugin action invoke {}.undo-setup".format(setup.plugin_id()))
    return 0


def cmd_doctor(args) -> int:
    cfg = load_config()
    print("python      ", sys.version.split()[0])
    print("herdr       ", herdr.herdr_bin())
    print("config      ", config_dir() / "config.ini")
    print("state       ", state_dir())
    from . import setup
    print("statusline  ", "relay installed" if setup.statusline_installed() else "relay missing (run setup)")
    print("key         ", setup.KEY, setup.keybinding_state())
    for target in setup.targets():
        print("relay       ", target.kind, target.settings,
              "installed" if setup._target_installed(target) else "missing (run setup)")
    for folder in config_claude_dirs():
        print("session hook", folder,
              "installed" if setup.herdr_hook_installed(folder) else "missing (run setup)")
    print("language    ", cfg.language, "->", _tr(cfg).lang)
    try:
        agents = herdr.agent_list()
        print("agents      ", len(agents))
        for agent in agents:
            sess = (agent.get("agent_session") or {}).get("value")
            print("   ", agent.get("pane_id"), agent.get("agent"), agent.get("agent_status"),
                  "session" if sess else "no session id")
    except herdr.HerdrError as exc:
        print("herdr       ", "unreachable:", exc)
    data = prefs.load()
    from .summarize import resolve_backend
    backend = resolve_backend(cfg.summarize)
    if cfg.summarize == "off":
        print("rephrasing   off")
    else:
        print("rephrasing  ", cfg.summarize, "->", backend[0] if backend else "no CLI found on PATH",
              "(paused from menu)" if not prefs.summarize_enabled(data) else "")
    print("auto_open   ", prefs.auto_open_enabled(data, cfg.auto_open))
    print("hidden      ", ", ".join(data["hidden_panes"]) or "-")
    print("panels      ", ", ".join("{}->{}".format(k, v) for k, v in data["panels"].items()) or "-")
    return 0


def cmd_preview(args) -> int:
    """Render the panel once to stdout (for development and bug reports)."""
    from .panel import PanelModel
    from .render import render
    from .theme import Theme
    from .tui import Screen
    cfg = load_config()
    if args.icons:
        cfg.icons = args.icons
    model = PanelModel(args.target, cfg, _tr(cfg) if not args.lang else Tr(args.lang))
    model.poll()
    model._next_pane = 0
    model.poll()
    frame = render(model.view(args.width, args.height, args.bar))
    screen = Screen(Theme.load(cfg.colors, cfg.background))
    for line in frame.lines:
        if args.plain:
            sys.stdout.write("".join(t for t, _ in line).rstrip() + "\n")
        else:
            sys.stdout.write("".join(screen._sgr(s) + t for t, s in line) + "\x1b[0m\n")
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="eartag.py", description="herdr eartag panel")
    sub = parser.add_subparsers(dest="cmd")
    p = sub.add_parser("panel", help="run the panel (herdr pane entrypoint)")
    p.add_argument("--target")
    p.set_defaults(func=cmd_panel)
    p = sub.add_parser("toggle", help="show or hide the panel for the focused agent")
    p.add_argument("--pane")
    p.set_defaults(func=cmd_toggle)
    p = sub.add_parser("unhide", help="show every hidden agent again and open their panels")
    p.set_defaults(func=cmd_unhide)
    p = sub.add_parser("startup", help="herdr startup hook (cleans panels left by a restart)")
    p.set_defaults(func=cmd_startup)
    p = sub.add_parser("event", help="herdr event hook (auto-open)")
    p.set_defaults(func=cmd_event)
    p = sub.add_parser("tap", help="statusline relay (reads Claude Code statusline JSON on stdin)")
    p.add_argument("--then", help="previous statusline command to run and print")
    p.set_defaults(func=cmd_tap)
    p = sub.add_parser("setup", help="statusline relay (quotas) + prefix+i key; --remove to undo")
    p.add_argument("--remove", action="store_true")
    p.set_defaults(func=cmd_setup)
    p = sub.add_parser("doctor", help="print diagnostics")
    p.set_defaults(func=cmd_doctor)
    p = sub.add_parser("preview", help="render once for a pane id")
    p.add_argument("target")
    p.add_argument("--width", type=int, default=40)
    p.add_argument("--height", type=int, default=36)
    p.add_argument("--bar")
    p.add_argument("--lang")
    p.add_argument("--icons")
    p.add_argument("--plain", action="store_true")
    p.set_defaults(func=cmd_preview)
    args = parser.parse_args(argv)
    if not getattr(args, "func", None):
        parser.print_help()
        return 2
    try:
        return int(args.func(args) or 0)
    except herdr.HerdrError as exc:
        print("herdr-eartag: {}".format(exc), file=sys.stderr)
        return 1
