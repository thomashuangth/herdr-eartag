"""Codex sessions (~/.codex/sessions/**/rollout-*.jsonl).

A rollout holds the asks and agent messages, a `turn_context` record per turn
(model, effort), `token_count` events (context used, window size and the 5h / 7d
rate limits) and the tool calls (apply_patch bodies name the edited files). Only
the tail is read, like Claude Code transcripts.
"""
from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from .claude import Ask, Transcript, _main_program, clean_prompt, parse_ts

TAIL = 512 * 1024
_PATCH_RE = re.compile(r"^\*\*\* (?:Add|Update|Delete) File: (.+?)\s*$", re.M)
_cache = {}


def codex_home() -> Path:
    return Path(os.environ.get("CODEX_HOME") or os.path.join(os.path.expanduser("~"), ".codex"))


def _sessions() -> list:
    base = codex_home() / "sessions"
    try:
        files = list(base.glob("*/*/*/rollout-*.jsonl"))
    except OSError:
        return []
    return sorted(files, key=lambda p: p.stat().st_mtime, reverse=True)


def _session_cwd(path: Path) -> Optional[str]:
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            first = json.loads(fh.readline() or "{}")
    except (OSError, ValueError):
        return None
    payload = first.get("payload") if isinstance(first, dict) else None
    cwd = payload.get("cwd") if isinstance(payload, dict) else None
    return cwd if isinstance(cwd, str) else None


def find_session(session_id: Optional[str], cwd: Optional[str]) -> Optional[Path]:
    files = _sessions()
    if session_id and re.fullmatch(r"[A-Za-z0-9-]+", session_id):
        for path in files:
            if path.name.endswith("{}.jsonl".format(session_id)):
                return path
    if cwd:
        for path in files[:60]:
            if _session_cwd(path) == cwd:
                return path
    return None


def model_and_effort(path: Path) -> Tuple[str, str]:
    """Latest (model, effort) from the session's turn_context records."""
    try:
        stat = path.stat()
    except OSError:
        return "", ""
    key = (str(path), stat.st_size, stat.st_mtime)
    if key in _cache:
        return _cache[key]
    model = effort = ""
    try:
        with open(path, "rb") as fh:
            fh.seek(max(0, stat.st_size - TAIL))
            lines = fh.read().splitlines()
    except OSError:
        return "", ""
    for raw in reversed(lines):
        if b'"turn_context"' not in raw:
            continue
        try:
            obj = json.loads(raw)
        except ValueError:
            continue
        payload = obj.get("payload") if isinstance(obj, dict) else None
        if isinstance(payload, dict):
            model = str(payload.get("model") or "")
            effort = str(payload.get("effort") or payload.get("reasoning_effort") or "")
            break
    _cache.clear()
    _cache[key] = (model, effort)
    return model, effort


def configured() -> Tuple[str, str]:
    """Fallback from ~/.codex/config.toml (model, model_reasoning_effort)."""
    try:
        text = (codex_home() / "config.toml").read_text(encoding="utf-8")
    except OSError:
        return "", ""
    def value(key: str) -> str:
        m = re.search(r'^\s*{}\s*=\s*"([^"]*)"'.format(key), text, re.M)
        return m.group(1) if m else ""
    return value("model"), value("model_reasoning_effort")


def _texts(content: Any) -> str:
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    return "\n".join(str(b.get("text") or "") for b in content
                     if isinstance(b, dict) and isinstance(b.get("text"), str)).strip()


def _rate_limits(raw: Any, ts: Optional[float]) -> Optional[Dict[str, Any]]:
    """Codex primary/secondary windows -> the Claude statusline shape."""
    if not isinstance(raw, dict):
        return None
    out: Dict[str, Any] = {}
    for key in ("primary", "secondary"):
        entry = raw.get(key)
        if not isinstance(entry, dict) or not isinstance(entry.get("used_percent"), (int, float)):
            continue
        minutes = entry.get("window_minutes")
        window = "five_hour" if isinstance(minutes, (int, float)) and minutes <= 24 * 60 else "seven_day"
        item: Dict[str, Any] = {"used_percentage": float(entry["used_percent"])}
        if isinstance(entry.get("resets_at"), (int, float)):
            item["resets_at"] = float(entry["resets_at"])
        out[window] = item
    return {"ts": ts or 0.0, "rate_limits": out} if out else None


def _patched_files(text: str) -> List[str]:
    return _PATCH_RE.findall(text or "") if "*** " in (text or "") else []


class CodexTranscript(Transcript):
    def _ask_text(self, payload: dict) -> Optional[str]:
        kind = payload.get("type")
        if kind == "user_message":
            return clean_prompt(str(payload.get("message") or ""))
        if kind == "item_completed":
            item = payload.get("item") or {}
            if isinstance(item, dict) and item.get("type") == "UserMessage":
                return clean_prompt(_texts(item.get("content")))
        return None

    def _scan_older(self, obj: dict, asks: List[Ask], files: List[Tuple[str, float]]) -> None:
        payload = obj.get("payload")
        if not isinstance(payload, dict):
            return
        ts = parse_ts(obj.get("timestamp")) or 0.0
        if obj.get("type") == "event_msg":
            text = self._ask_text(payload)
            if text and not (asks and asks[-1].text == text and abs(asks[-1].ts - ts) < 5):
                asks.append(Ask(ts, text))
        elif obj.get("type") == "response_item":
            for path in _patched_files(str(payload.get("input") or payload.get("arguments") or "")):
                files.append((os.path.basename(path), ts))

    def _ingest_line(self, line: bytes) -> None:
        try:
            obj = json.loads(line)
        except ValueError:
            return
        if not isinstance(obj, dict) or not isinstance(obj.get("payload"), dict):
            return
        d = self.data
        payload = obj["payload"]
        kind = obj.get("type")
        ts = parse_ts(obj.get("timestamp"))
        if kind == "turn_context":
            if isinstance(payload.get("model"), str):
                d.model = payload["model"]
            effort = payload.get("effort") or payload.get("reasoning_effort")
            if isinstance(effort, str):
                d.effort = effort
            return
        if kind == "response_item":
            ptype = payload.get("type")
            if ptype in ("custom_tool_call", "function_call", "local_shell_call"):
                if ts:
                    d.last_activity_ts = ts
                body = str(payload.get("input") or payload.get("arguments") or "")
                patched = _patched_files(body)
                for path in patched:
                    self._edited(path, ts)
                name = str(payload.get("name") or "tool")
                if patched:
                    d.last_tool = "apply_patch {}".format(os.path.basename(patched[-1]))
                elif name not in ("exec", "shell", "wait"):
                    d.last_tool = name
                d.last_tool_ts = ts
            return
        if kind != "event_msg":
            return
        ptype = payload.get("type")
        text = self._ask_text(payload)
        if text:
            last = d.asks[-1] if d.asks else None
            if not (last and last.text == text and abs(last.ts - (ts or 0)) < 5):
                self._new_ask(ts, text)
            return
        if ts:
            d.last_activity_ts = ts
        if ptype == "agent_message" and isinstance(payload.get("message"), str):
            self._reply_text(ts, payload["message"].strip())
        elif ptype == "item_completed":
            item = payload.get("item") or {}
            itype = item.get("type") if isinstance(item, dict) else None
            if itype == "AgentMessage":
                reply = _texts(item.get("content"))
                if reply and reply != d.last_reply:
                    self._reply_text(ts, reply)
            elif itype == "CommandExecution":
                command = item.get("command")
                line_ = command[-1] if isinstance(command, list) and command else str(command or "")
                program = _main_program(str(line_))
                d.last_tool, d.last_tool_ts = ("shell " + program if program else "shell"), ts
        elif ptype == "task_started" and isinstance(payload.get("model_context_window"), int):
            d.context_window = payload["model_context_window"]
        elif ptype == "token_count":
            info = payload.get("info") or {}
            if isinstance(info, dict):
                last = info.get("last_token_usage") or {}
                if isinstance(last, dict) and isinstance(last.get("input_tokens"), int):
                    d.context_tokens = last["input_tokens"]
                if isinstance(info.get("model_context_window"), int):
                    d.context_window = info["model_context_window"]
            limits = _rate_limits(payload.get("rate_limits"), ts)
            if limits:
                d.rate_limits = limits
        elif ptype == "context_compacted":
            d.context_tokens = None
