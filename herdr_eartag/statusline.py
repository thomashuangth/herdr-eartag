"""Statusline relay for Claude Code and Antigravity (agy).

Both pipe a JSON document to the configured statusline command. The tap
keeps a minimal whitelist of fields (model, effort, context window, rate limits)
so the panel can show quotas, then hands the same JSON to the user's previous
statusline command and prints its output unchanged.

Nothing else is stored: no cwd, no transcript path, no cost, no prompt text.

Quotas belong to an account, so they are stored per scope: "" for ~/.claude,
"claude-<name>-<hash>" for another CLAUDE_CONFIG_DIR, "agy" for Antigravity.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Optional

SESSION_TTL = 14 * 24 * 3600


def snapshots_dir() -> Path:
    # Kept in sync with config.data_dir(); this file must stay standalone because
    # `eartag.py setup` copies it to a stable location outside the plugin checkout.
    base = os.environ.get("XDG_STATE_HOME") or os.path.join(os.path.expanduser("~"), ".local", "state")
    return Path(base) / "herdr-eartag" / "statusline"


def claude_scope(config_dir: Optional[str]) -> str:
    """Rate-limit scope of a Claude Code config dir ("" for the default ~/.claude)."""
    if not config_dir:
        return ""
    home = os.path.expanduser("~")
    try:
        real = os.path.realpath(os.path.expanduser(config_dir))
        default = os.path.realpath(os.path.join(home, ".claude"))
    except OSError:
        return ""
    if real.rstrip("/") == default.rstrip("/"):
        return ""
    name = re.sub(r"[^A-Za-z0-9]", "", os.path.basename(real.rstrip("/")))[:24] or "dir"
    digest = hashlib.sha1(real.encode("utf-8")).hexdigest()[:6]
    return "claude-{}-{}".format(name, digest)


def scope_of(payload: Dict[str, Any]) -> str:
    if payload.get("product") == "antigravity":
        return "agy"
    return claude_scope(os.environ.get("CLAUDE_CONFIG_DIR"))


def _num(value: Any) -> Optional[float]:
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def extract(payload: Dict[str, Any]) -> Dict[str, Any]:
    """Whitelist the fields the panel needs."""
    out: Dict[str, Any] = {"ts": time.time()}
    sid = payload.get("session_id")
    if isinstance(sid, str):
        out["session_id"] = sid
    model = payload.get("model")
    if isinstance(model, dict):
        out["model"] = {k: model[k] for k in ("id", "display_name") if isinstance(model.get(k), str)}
    if isinstance(model, dict) and isinstance(model.get("effort"), str):
        out["effort"] = model["effort"]  # Antigravity
    effort = payload.get("effort")
    if isinstance(effort, dict) and isinstance(effort.get("level"), str):
        out["effort"] = effort["level"]
    elif isinstance(effort, str):
        out["effort"] = effort
    ctx = payload.get("context_window")
    if isinstance(ctx, dict):
        keep = {}
        for key in ("used_percentage", "context_window_size", "total_input_tokens"):
            if _num(ctx.get(key)) is not None:
                keep[key] = ctx[key]
        usage = ctx.get("current_usage")
        if isinstance(usage, dict):
            tokens = sum(int(usage.get(k) or 0) for k in (
                "input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens"
            ) if isinstance(usage.get(k), (int, float)))
            if tokens:
                keep["current_tokens"] = tokens
        if keep:
            out["context"] = keep
    limits = payload.get("rate_limits")
    if isinstance(limits, dict):
        keep_limits = {}
        for window in ("five_hour", "seven_day"):
            entry = limits.get(window)
            if isinstance(entry, dict):
                item = {}
                if _num(entry.get("used_percentage")) is not None:
                    item["used_percentage"] = entry["used_percentage"]
                if _num(entry.get("resets_at")) is not None:
                    item["resets_at"] = entry["resets_at"]
                if item:
                    keep_limits[window] = item
        if keep_limits:
            out["rate_limits"] = keep_limits
    quota = payload.get("quota")
    if isinstance(quota, dict) and "rate_limits" not in out:
        agy = agy_limits(quota, (out.get("model") or {}).get("id", ""))
        if agy:
            out["rate_limits"] = agy
    return out


def _iso_ts(value: Any) -> Optional[float]:
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def agy_limits(quota: Dict[str, Any], model_id: str) -> Dict[str, Any]:
    """Antigravity quotas -> the Claude statusline shape. Gemini models draw on the
    gemini-* buckets, other models (Claude, GPT...) on the 3p-* ones."""
    prefix = "gemini" if "gemini" in (model_id or "").lower() else "3p"
    out: Dict[str, Any] = {}
    for window, suffix in (("five_hour", "5h"), ("seven_day", "weekly")):
        entry = quota.get("{}-{}".format(prefix, suffix))
        if not isinstance(entry, dict) or _num(entry.get("remaining_fraction")) is None:
            continue
        item = {"used_percentage": round((1.0 - float(entry["remaining_fraction"])) * 100.0, 1)}
        resets = _iso_ts(entry.get("reset_time"))
        if resets is not None:
            item["resets_at"] = resets
        out[window] = item
    return out


def _write_json(path: Path, data: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(path.parent, 0o700)
    except OSError:
        pass
    fd, tmp = tempfile.mkstemp(prefix=".tap.", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(data, fh)
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def rate_limits_path(scope: str = "") -> Path:
    name = "rate_limits-{}.json".format(scope) if scope else "rate_limits.json"
    return snapshots_dir() / name


def record(payload: Dict[str, Any]) -> None:
    snap = extract(payload)
    base = snapshots_dir()
    sid = snap.get("session_id")
    if isinstance(sid, str) and sid.replace("-", "").isalnum():
        _write_json(base / "sessions" / "{}.json".format(sid), snap)
    if "rate_limits" in snap:
        _write_json(rate_limits_path(scope_of(payload)), {"ts": snap["ts"], "rate_limits": snap["rate_limits"]})
    _remember_window(base, snap)
    _prune(base / "sessions")


def _remember_window(base: Path, snap: Dict[str, Any]) -> None:
    """Remember context window size per model id, for sessions without a fresh snapshot."""
    model = (snap.get("model") or {}).get("id")
    size = (snap.get("context") or {}).get("context_window_size")
    if not isinstance(model, str) or not isinstance(size, (int, float)) or size <= 0:
        return
    windows = _read(base / "windows.json") or {}
    if windows.get(model) != size:
        windows[model] = size
        _write_json(base / "windows.json", windows)


def read_windows() -> Dict[str, Any]:
    return _read(snapshots_dir() / "windows.json") or {}


def _prune(folder: Path) -> None:
    # Cheap probabilistic cleanup of old per-session snapshots.
    if int(time.time()) % 50:
        return
    cutoff = time.time() - SESSION_TTL
    try:
        for path in folder.glob("*.json"):
            if path.stat().st_mtime < cutoff:
                path.unlink()
    except OSError:
        pass


def run_tap(then: Optional[str]) -> int:
    raw = sys.stdin.buffer.read()
    try:
        payload = json.loads(raw.decode("utf-8", errors="replace") or "{}")
        if isinstance(payload, dict):
            record(payload)
    except Exception:  # never break the user's statusline
        pass
    if not then:
        return 0
    try:
        proc = subprocess.run(then, shell=True, input=raw, stdout=subprocess.PIPE, timeout=30)
    except (OSError, subprocess.SubprocessError):
        return 0
    sys.stdout.buffer.write(proc.stdout)
    sys.stdout.flush()
    return proc.returncode


# -- reading ---------------------------------------------------------------------
def read_session(session_id: Optional[str]) -> Optional[Dict[str, Any]]:
    if not session_id or not session_id.replace("-", "").isalnum():
        return None
    return _read(snapshots_dir() / "sessions" / "{}.json".format(session_id))


def read_rate_limits(scope: str = "") -> Optional[Dict[str, Any]]:
    return _read(rate_limits_path(scope))


def tap_installed() -> bool:
    return (snapshots_dir()).exists()


def _read(path: Path) -> Optional[Dict[str, Any]]:
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else None
    except (OSError, ValueError):
        return None


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    then = None
    if "--then" in argv:
        i = argv.index("--then")
        then = argv[i + 1] if i + 1 < len(argv) else None
    return run_tap(then)


if __name__ == "__main__":
    sys.exit(main())
