"""Plugin configuration (INI file in the herdr plugin config dir) and paths."""
from __future__ import annotations

import configparser
import os
import socket
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional

DEFAULT_CONFIG = """\
# herdr-eartag configuration. Lines starting with # are comments.
[eartag]
# Interface language: auto, fr or en.
language = auto
# Open the panel automatically when herdr detects an agent in a pane.
auto_open = true
# Panel width in columns when unfolded.
width = 40
# unicode or ascii (for fonts without box-drawing and geometric symbols).
icons = unicode
# herdr = follow the herdr theme from config.toml, ansi = terminal's 16 colors.
colors = herdr
# theme = paint the panel with the theme's surface color, terminal = keep the terminal background.
background = theme
# Label of this machine in the footer. Empty = "Mac" on macOS, short hostname elsewhere.
host_label =
# Rephrase asks and agent replies into short structured lines with an agent
# CLI you already use: off (default), auto, claude, codex or gemini.
# auto = first one found among claude, codex, gemini.
# When on, the text of each new ask and of the agent's latest reply is sent to
# that provider under your own login.
# Each call counts against your plan's usage or API billing, and the provider's
# data policies apply. Nothing is sent while this is off.
summarize = off
# Empty = haiku for claude, the CLI's own default model for codex and gemini.
summarize_model =
# Extra Claude Code config dirs (CLAUDE_CONFIG_DIR of other accounts), comma
# separated. ~/.claude and ~/.claude-* are found without this.
claude_dirs =
# Key shown in the "hidden" toast. Bind it yourself in herdr config.toml.
toggle_key = prefix+i
"""


def _home() -> Path:
    return Path(os.path.expanduser("~"))


def data_dir() -> Path:
    """Shared dir for statusline snapshots, written outside herdr's plugin env."""
    base = os.environ.get("XDG_STATE_HOME") or str(_home() / ".local" / "state")
    return Path(base) / "herdr-eartag"


def state_dir() -> Path:
    env = os.environ.get("HERDR_PLUGIN_STATE_DIR")
    return Path(env) if env else data_dir() / "plugin-state"


def config_dir() -> Path:
    env = os.environ.get("HERDR_PLUGIN_CONFIG_DIR")
    return Path(env) if env else data_dir() / "plugin-config"


def claude_dir() -> Path:
    env = os.environ.get("CLAUDE_CONFIG_DIR")
    return Path(env) if env else _home() / ".claude"


def claude_dirs() -> List[Path]:
    """Every Claude Code config dir on this machine, default first.

    Several accounts are often run side by side with an alias such as
    `CLAUDE_CONFIG_DIR=~/.claude-work claude`. The panel runs in herdr's environment,
    not the agent's, so it cannot see that variable: it looks at ~/.claude, any
    ~/.claude-* or ~/.claude_* dir that holds transcripts, CLAUDE_CONFIG_DIR, and the
    `claude_dirs` config key (comma separated) for dirs elsewhere."""
    found: List[Path] = []
    seen = set()

    def add(path: Path) -> None:
        path = path.expanduser()
        try:
            real = path.resolve()
        except OSError:
            return
        if real.is_dir() and real not in seen:
            seen.add(real)
            found.append(path)

    add(_home() / ".claude")
    env = os.environ.get("CLAUDE_CONFIG_DIR")
    if env:
        add(Path(env))
    try:
        siblings = sorted(_home().glob(".claude[-_]*"))
    except OSError:
        siblings = []
    for path in siblings:
        if (path / "projects").is_dir():
            add(path)
    for extra in _extra_claude_dirs():
        add(Path(extra))
    return found


def _extra_claude_dirs() -> List[str]:
    path = config_dir() / "config.ini"
    parser = configparser.ConfigParser(inline_comment_prefixes=("#", ";"))
    try:
        parser.read(path, encoding="utf-8")
        value = parser.get("eartag", "claude_dirs", fallback="")
    except (OSError, configparser.Error):
        return []
    return [v.strip() for v in value.split(",") if v.strip()]


def herdr_config_path() -> Path:
    base = os.environ.get("XDG_CONFIG_HOME") or str(_home() / ".config")
    return Path(base) / "herdr" / "config.toml"


def ensure_private_dir(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(path, 0o700)
    except OSError:
        pass
    return path


@dataclass
class Config:
    language: str = "auto"
    auto_open: bool = True
    width: int = 40
    icons: str = "unicode"
    colors: str = "herdr"
    background: bool = True
    host_label: str = ""
    summarize: str = "off"
    summarize_model: str = ""
    toggle_key: str = "prefix+i"

    @property
    def host(self) -> str:
        if self.host_label:
            return self.host_label
        if sys.platform == "darwin":
            return "Mac"
        return socket.gethostname().split(".")[0] or "host"


def load_config(path: Optional[Path] = None) -> Config:
    path = path or config_dir() / "config.ini"
    if not path.exists():
        try:
            ensure_private_dir(path.parent)
            path.write_text(DEFAULT_CONFIG, encoding="utf-8")
        except OSError:
            pass
    parser = configparser.ConfigParser(inline_comment_prefixes=("#", ";"))
    try:
        parser.read(path, encoding="utf-8")
    except (OSError, configparser.Error):
        return Config()
    sec = parser["eartag"] if parser.has_section("eartag") else {}
    cfg = Config()

    def get(key: str, default: str) -> str:
        value = sec.get(key, default) if sec else default
        return (value or "").strip()

    cfg.language = get("language", cfg.language) or "auto"
    cfg.auto_open = get("auto_open", "true").lower() in ("1", "true", "yes", "on")
    try:
        cfg.width = max(28, min(80, int(get("width", "40"))))
    except ValueError:
        pass
    cfg.icons = "ascii" if get("icons", "unicode").lower() == "ascii" else "unicode"
    cfg.colors = "ansi" if get("colors", "herdr").lower() == "ansi" else "herdr"
    cfg.background = get("background", "theme").lower() != "terminal"
    cfg.host_label = get("host_label", "")
    value = get("summarize", "off").lower()
    cfg.summarize = value if value in ("auto", "claude", "codex", "gemini") else "off"
    cfg.summarize_model = get("summarize_model", "")
    cfg.toggle_key = get("toggle_key", "prefix+i") or "prefix+i"
    return cfg


def herdr_border_overhead(path: Optional[Path] = None) -> int:
    """Columns herdr's pane frame takes inside a pane: 2 with borders and gaps (default),
    1 with shared dividers (pane_gaps = false), 0 with pane_borders = "off"."""
    path = path or herdr_config_path()
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return 2
    section, values = "", {}
    for raw in text.splitlines():
        line = raw.split("#", 1)[0].strip()
        if line.startswith("[") and line.endswith("]"):
            section = line.strip("[]").strip()
            continue
        if section == "ui" and "=" in line:
            key, value = line.split("=", 1)
            values[key.strip()] = value.strip().strip('"').lower()
    if values.get("pane_borders") in ("off", "false"):
        return 0
    if values.get("pane_gaps") == "false":
        return 1
    return 2
