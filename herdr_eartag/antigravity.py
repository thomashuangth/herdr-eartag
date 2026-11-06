"""Antigravity CLI (agy) sessions.

agy keeps a JSONL transcript per conversation at
~/.gemini/antigravity-cli/brain/<conversation id>/.system_generated/logs/transcript.jsonl
(one record per step: USER_INPUT, PLANNER_RESPONSE with text and tool calls, tool
outputs...). Only the tail is read, like Claude Code transcripts. Model, effort,
context and quotas come from agy's statusline JSON through the relay (statusline.py).
"""
from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import List, Optional, Tuple

from .claude import Ask, Transcript, _main_program, clean_prompt, parse_ts

AGENTS = ("agy", "antigravity")
_REQUEST_RE = re.compile(r"<USER_REQUEST>\s*(.*?)\s*(?:</USER_REQUEST>|$)", re.S)
_PATH_KEYS = ("Path", "TargetFile", "AbsolutePath", "FilePath", "File")
_EDIT_WORDS = ("edit", "write", "replace", "create", "patch")


def homes() -> List[Path]:
    gemini = Path(os.path.expanduser("~")) / ".gemini"
    return [gemini / "antigravity-cli", gemini / "antigravity"]


def home() -> Path:
    return homes()[0]


def settings_path() -> Path:
    return home() / "settings.json"


def find_transcript(session_id: Optional[str], cwd: Optional[str]) -> Tuple[Optional[Path], bool]:
    inferred = False
    if not (session_id and re.fullmatch(r"[A-Za-z0-9_-]+", session_id)):
        session_id = _latest_conversation(cwd)
        inferred = True
        if not session_id:
            return None, True
    for base in homes():
        path = base / "brain" / session_id / ".system_generated" / "logs" / "transcript.jsonl"
        if path.exists():
            return path, inferred
    return None, inferred


def _latest_conversation(cwd: Optional[str]) -> Optional[str]:
    """Last conversation started from cwd, from agy's prompt history (tail only)."""
    if not cwd:
        return None
    path = home() / "history.jsonl"
    try:
        size = path.stat().st_size
        with open(path, "rb") as fh:
            fh.seek(max(0, size - 256 * 1024))
            lines = fh.read().splitlines()
    except OSError:
        return None
    for raw in reversed(lines):
        try:
            obj = json.loads(raw)
        except ValueError:
            continue
        if isinstance(obj, dict) and obj.get("workspace") == cwd:
            cid = obj.get("conversationId")
            if isinstance(cid, str) and re.fullmatch(r"[A-Za-z0-9_-]+", cid):
                return cid
    return None


def configured_model() -> Tuple[str, str]:
    """('Gemini 3.8 Flash (Low)', 'low') from agy's settings.json."""
    try:
        data = json.loads(settings_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return "", ""
    model = data.get("model") if isinstance(data, dict) else None
    if not isinstance(model, str):
        return "", ""
    return model, effort_of(model)


def effort_of(model: str) -> str:
    m = re.search(r"\(([A-Za-z]+)\)\s*$", model or "")
    return m.group(1).lower() if m else ""


def _arg(args: dict, key: str) -> str:
    """agy stores each tool argument JSON-encoded: '"\\"/a/b.py\\""'."""
    value = args.get(key)
    if isinstance(value, str):
        try:
            decoded = json.loads(value)
        except ValueError:
            return value
        return decoded if isinstance(decoded, str) else ""
    return ""


def request_text(content: str) -> Optional[str]:
    m = _REQUEST_RE.search(content or "")
    return clean_prompt(m.group(1)) if m else None


def _tool(call: dict) -> Tuple[str, str, Optional[str]]:
    """(name, label, edited path) for a tool call. Labels hold basenames and program
    names only, never arguments."""
    name = str(call.get("name") or "tool")
    args = call.get("args") if isinstance(call.get("args"), dict) else {}
    if name == "run_command":
        program = _main_program(_arg(args, "CommandLine"))
        return name, "{} {}".format(name, program) if program else name, None
    path = next((p for p in (_arg(args, k) for k in _PATH_KEYS) if p), "")
    edited = path if path and any(w in name for w in _EDIT_WORDS) and "read" not in name else None
    return name, "{} {}".format(name, os.path.basename(path)) if path else name, edited


class AgyTranscript(Transcript):
    def _scan_older(self, obj: dict, asks: List[Ask], files: List[Tuple[str, float]]) -> None:
        ts = parse_ts(obj.get("created_at")) or 0.0
        if obj.get("type") == "USER_INPUT":
            text = request_text(str(obj.get("content") or ""))
            if text:
                asks.append(Ask(ts, text))
        for call in obj.get("tool_calls") or []:
            if isinstance(call, dict):
                _, _, edited = _tool(call)
                if edited:
                    files.append((os.path.basename(edited), ts))

    def _ingest_line(self, line: bytes) -> None:
        try:
            obj = json.loads(line)
        except ValueError:
            return
        if not isinstance(obj, dict):
            return
        d = self.data
        kind = obj.get("type")
        ts = parse_ts(obj.get("created_at"))
        if kind == "USER_INPUT":
            text = request_text(str(obj.get("content") or ""))
            if text:
                self._new_ask(ts, text)
            return
        if kind == "CHECKPOINT":  # compaction
            d.context_tokens = None
            return
        if obj.get("source") != "MODEL":
            return
        if ts:
            d.last_activity_ts = ts
        if kind != "PLANNER_RESPONSE":
            return
        tokens = sum(int(obj.get(k) or 0) for k in ("input_tokens", "cache_read_tokens")
                     if isinstance(obj.get(k), int))
        if tokens:
            d.context_tokens = tokens
        text = obj.get("content")
        if isinstance(text, str) and text.strip():
            self._reply_text(ts, text.strip())
        for call in obj.get("tool_calls") or []:
            if not isinstance(call, dict):
                continue
            _, label, edited = _tool(call)
            d.last_tool, d.last_tool_ts = label, ts
            if edited:
                self._edited(edited, ts)
