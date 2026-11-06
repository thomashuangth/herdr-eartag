"""Effort / reasoning levels per model family, and model display names.

Each family has its own scale, so the strip's dots read "2 of 5" for Claude
medium and "1 of 2" for Gemini low. Unknown values fall back to a generic scale.
"""
from __future__ import annotations

import re
from typing import List, Optional, Tuple

SCALES = {
    # Claude Code /effort and statusline effort.level
    "claude": ["low", "medium", "high", "xhigh", "max"],
    # OpenAI reasoning effort (Codex model_reasoning_effort, API reasoning.effort)
    "openai": ["minimal", "low", "medium", "high", "xhigh"],
    # Gemini thinking level
    "gemini": ["low", "high"],
    # Grok reasoning effort
    "grok": ["low", "high"],
}
GENERIC = ["minimal", "low", "medium", "high", "xhigh", "max"]
NO_EFFORT = {"none", "off", "disabled", ""}


def family(model_id: str, agent: str = "") -> str:
    m = (model_id or "").lower()
    if m.startswith("claude") or any(k in m for k in ("opus", "sonnet", "haiku", "fable")):
        return "claude"
    if m.startswith(("gpt", "o1", "o3", "o4", "codex")) or agent == "codex":
        return "openai"
    if m.startswith("gemini") or agent == "gemini":
        return "gemini"
    if m.startswith("grok") or agent == "grok":
        return "grok"
    if agent == "claude":
        return "claude"
    return ""


def scale_for(model_id: str, effort: str, agent: str = "") -> List[str]:
    """The scale `effort` belongs to for this model, or [] when there is no effort."""
    effort = (effort or "").lower()
    if effort in NO_EFFORT:
        return []
    scale = SCALES.get(family(model_id, agent), [])
    if effort in scale:
        return scale
    return GENERIC if effort in GENERIC else []


def display_name(model_id: str) -> Tuple[str, str]:
    """'claude-opus-5-5' -> ('Opus', '5.5'), 'gpt-6.1-sol' -> ('GPT', '6.1'),
    'gemini-2.5-pro' -> ('Gemini', '2.5'), 'o3' -> ('o3', '')."""
    if not model_id:
        return "", ""
    mid = model_id.split("[", 1)[0].strip()
    # Antigravity names models for people: 'Gemini 3.8 Flash (Low)', 'Claude Sonnet 4.6 (Thinking)'.
    mid = re.sub(r"\s*\([^)]*\)\s*$", "", mid)
    low = re.sub(r"\s+", "-", mid.lower())
    m = re.match(r"^(?:claude-)?(opus|sonnet|haiku|fable)-(\d+)\.(\d+)", low)
    if m:
        return m.group(1).capitalize(), m.group(2) + "." + m.group(3)
    m = re.match(r"^gpt-?(\d+(?:\.\d+)?)", low)
    if m:
        return "GPT", m.group(1)
    m = re.match(r"^(gemini|grok|mistral|deepseek|qwen|llama|kimi|glm)[-_]?v?(\d+(?:\.\d+)?)?", low)
    if m:
        name = {"deepseek": "DeepSeek", "glm": "GLM"}.get(m.group(1), m.group(1).capitalize())
        return name, m.group(2) or ""
    m = re.match(r"^(?:claude-)?([a-z]+)-(\d+)(?:-(\d{1,2}))?(?:-\d{8})?$", low)
    if m:
        return m.group(1).capitalize(), m.group(2) + ("." + m.group(3) if m.group(3) else "")
    m = re.match(r"^claude-(\d+)(?:-(\d))?-([a-z]+)", low)  # legacy: claude-3-5-sonnet
    if m:
        return m.group(3).capitalize(), m.group(1) + ("." + m.group(2) if m.group(2) else "")
    return mid, ""


def dots(model_id: str, effort: str, agent: str = "") -> Optional[Tuple[int, int]]:
    """(filled, total) for the strip, or None when the model has no effort."""
    scale = scale_for(model_id, effort, agent)
    if not scale:
        return None
    return scale.index(effort.lower()) + 1, len(scale)
