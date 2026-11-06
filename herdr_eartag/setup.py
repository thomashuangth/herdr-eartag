"""One-click setup: statusline relay (quotas) for Claude Code and Antigravity, and the prefix+i key.

Both steps are idempotent, keep a backup of the file they edit, and can be undone.
They run only when the user asks: the `setup` action, or the "Enable" line / menu
entry in the panel.
"""
from __future__ import annotations

import json
import os
import re
import shlex
import shutil
import sys
import tempfile
from pathlib import Path
from typing import Any, List, Optional

from . import antigravity, herdr, statusline
from .config import claude_dir, claude_dirs, data_dir, ensure_private_dir, herdr_config_path

PLUGIN_ID = "thomashuangth.eartag"
KEY = "prefix+i"
_BEGIN = "# eartag:begin"
_END = "# eartag:end"


def plugin_id() -> str:
    return os.environ.get("HERDR_PLUGIN_ID") or PLUGIN_ID


# -- statusline relay ---------------------------------------------------------------
# One target per settings file: each Claude Code config dir, and Antigravity (agy).
# Claude Code runs the statusline command through a shell; agy gets a one-line
# wrapper script instead, so the command stays a plain path either way.

class Target:
    def __init__(self, kind: str, settings: Path, scope: str):
        self.kind = kind          # "claude" | "agy"
        self.settings = settings
        self.scope = scope        # "" for ~/.claude

    @property
    def previous(self) -> Path:
        name = "previous.json" if not self.scope else "previous-{}.json".format(self.scope)
        return data_dir() / "statusline" / name


def targets(kind: Optional[str] = None) -> List[Target]:
    found: List[Target] = []
    if kind in (None, "claude"):
        for folder in claude_dirs():
            found.append(Target("claude", folder / "settings.json", statusline.claude_scope(str(folder))))
    if kind in (None, "agy"):
        settings = antigravity.settings_path()
        if settings.parent.is_dir():
            found.append(Target("agy", settings, "agy"))
    return found


def _settings_path() -> Path:
    return claude_dir() / "settings.json"


def tap_install_path() -> Path:
    base = os.environ.get("XDG_DATA_HOME") or os.path.join(os.path.expanduser("~"), ".local", "share")
    return Path(base) / "herdr-eartag" / "statusline_tap.py"


def agy_wrapper_path() -> Path:
    return tap_install_path().with_name("agy_statusline.sh")


def _load_settings(path: Optional[Path] = None) -> Optional[dict]:
    path = path or _settings_path()
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _write_json_atomic(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".settings.", dir=str(path.parent))
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2, ensure_ascii=False)
        fh.write("\n")
    try:
        os.chmod(tmp, os.stat(path).st_mode & 0o777 if path.exists() else 0o600)
    except OSError:
        pass
    os.replace(tmp, path)


def _is_relay(command: str) -> bool:
    return "statusline_tap.py" in command or agy_wrapper_path().name in command


def _target_installed(target: Target) -> bool:
    settings = _load_settings(target.settings) or {}
    current = settings.get("statusLine")
    return isinstance(current, dict) and _is_relay(str(current.get("command", "")))


def statusline_installed(kind: Optional[str] = None) -> bool:
    found = targets(kind)
    return bool(found) and all(_target_installed(t) for t in found)


def refresh_tap() -> None:
    """Keep the installed relay in step with this plugin version (it is our own copy)."""
    tap = tap_install_path()
    if not tap.exists():
        return
    try:
        source = Path(statusline.__file__).read_bytes()
        if tap.read_bytes() != source:
            tap.write_bytes(source)
    except OSError:
        pass


def _install_target(target: Target) -> str:
    path = target.settings
    label = "Antigravity" if target.kind == "agy" else str(path.parent)
    settings = _load_settings(path)
    if settings is None:
        return "Statusline ({}): cannot read {}, nothing changed.".format(label, path)
    if _target_installed(target):
        return "Statusline ({}): relay already installed.".format(label)
    current = settings.get("statusLine")
    previous = target.previous
    ensure_private_dir(previous.parent)
    previous.write_text(json.dumps({"statusLine": current}), encoding="utf-8")
    os.chmod(previous, 0o600)
    backup = path.with_name(path.name + ".herdr-eartag.bak")
    if path.exists() and not backup.exists():
        shutil.copyfile(path, backup)
    tap = tap_install_path()
    command = "{} {}".format(shlex.quote(sys.executable or "python3"), shlex.quote(str(tap)))
    prev_cmd = str(current.get("command") or "") if isinstance(current, dict) else ""
    keeps = bool(prev_cmd) and (target.kind == "agy" or current.get("type") == "command")
    if target.kind == "agy" and isinstance(current, dict) and current.get("enabled") is False:
        keeps = False
    if keeps:
        command += " --then " + shlex.quote(prev_cmd)
    new = dict(current) if isinstance(current, dict) else {}
    if target.kind == "agy":
        wrapper = agy_wrapper_path()
        wrapper.write_text("#!/bin/sh\n# Eartag statusline relay for Antigravity.\nexec {}\n".format(command),
                           encoding="utf-8")
        os.chmod(wrapper, 0o700)
        new.setdefault("type", "")
        new["command"] = str(wrapper)
        new["enabled"] = True
    else:
        new["type"] = "command"
        new["command"] = command
    settings["statusLine"] = new
    _write_json_atomic(path, settings)
    kept = " Your statusline still runs as before." if keeps else ""
    return "Statusline ({}): relay added to {}.{}".format(label, path, kept)


def install_statusline() -> str:
    tap = tap_install_path()
    ensure_private_dir(tap.parent)
    shutil.copyfile(Path(statusline.__file__), tap)
    os.chmod(tap, 0o700)
    return "\n".join(_install_target(t) for t in targets())


def _remove_target(target: Target) -> str:
    label = "Antigravity" if target.kind == "agy" else str(target.settings.parent)
    settings = _load_settings(target.settings)
    if settings is None or not _target_installed(target):
        return "Statusline ({}): relay not installed.".format(label)
    try:
        previous = json.loads(target.previous.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        previous = {"statusLine": None}
    if previous.get("statusLine"):
        settings["statusLine"] = previous["statusLine"]
    else:
        settings.pop("statusLine", None)
    _write_json_atomic(target.settings, settings)
    return "Statusline ({}): previous statusline restored.".format(label)


def remove_statusline() -> str:
    return "\n".join(_remove_target(t) for t in targets())


# -- keybinding ---------------------------------------------------------------------
def _config_text() -> str:
    try:
        return herdr_config_path().read_text(encoding="utf-8")
    except OSError:
        return ""


def keybinding_state() -> str:
    """installed | conflict (prefix+i used by something else) | missing"""
    text = _config_text()
    if "{}.toggle".format(plugin_id()) in text:
        return "installed"
    if re.search(r'["\']{}["\']'.format(re.escape(KEY)), text):
        return "conflict"
    return "missing"


def install_keybinding() -> str:
    state = keybinding_state()
    if state == "installed":
        return "Key: {} already set.".format(KEY)
    if state == "conflict":
        return "Key: {} is already used in {}; bind {}.toggle yourself.".format(KEY, herdr_config_path(), plugin_id())
    path = herdr_config_path()
    text = _config_text()
    if path.exists():
        backup = path.with_name(path.name + ".herdr-eartag.bak")
        if not backup.exists():
            shutil.copyfile(path, backup)
    block = (
        "\n{begin} (added by the Eartag plugin; remove this block to unbind)\n"
        "[[keys.command]]\n"
        'key = "{key}"\n'
        'type = "plugin_action"\n'
        'command = "{pid}.toggle"\n'
        'description = "eartag panel"\n'
        "{end}\n"
    ).format(begin=_BEGIN, end=_END, key=KEY, pid=plugin_id())
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(("" if not text or text.endswith("\n") else "\n") + block)
    _reload_herdr()
    return "Key: {} now shows or hides the panel.".format(KEY)


def remove_keybinding() -> str:
    text = _config_text()
    pattern = re.compile(r"\n?{}.*?{}\n?".format(re.escape(_BEGIN), re.escape(_END)), re.S)
    if not pattern.search(text):
        return "Key: nothing added by Eartag."
    herdr_config_path().write_text(pattern.sub("\n", text).rstrip("\n") + "\n", encoding="utf-8")
    _reload_herdr()
    return "Key: {} removed.".format(KEY)


def _reload_herdr() -> None:
    try:
        herdr.call("server", "reload-config")
    except herdr.HerdrError:
        pass


# -- all -----------------------------------------------------------------------------
def needs_setup(kind: Optional[str] = None) -> bool:
    return not statusline_installed(kind) or keybinding_state() == "missing"


def run_all() -> List[str]:
    return [install_statusline(), install_keybinding()]


def undo_all() -> List[str]:
    return [remove_statusline(), remove_keybinding()]
