"""Optional rephrasing of asks and agent replies with a local agent CLI.

Off by default. When on, Eartag calls a CLI you already have and are logged in to
(claude, codex or gemini) in its non-interactive mode, under your own account:
no API key, and your provider's billing, quotas and data policies apply.

Results are cached on disk (plugin state dir, mode 0600) so each ask is rephrased
once. Calls run in a background thread and never block the UI.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile
import threading
import time
from typing import Dict, List, Optional, Tuple

from .config import ensure_private_dir, state_dir

BACKENDS = ("claude", "codex", "gemini")
DEFAULT_MODELS = {"claude": "haiku"}  # codex and gemini: the CLI's own default

_SYSTEM = (
    "You rewrite text into a very short label for a narrow terminal sidebar. "
    "Answer with the label only: no quotes, no preamble, no markdown, no tool use. "
    "Keep the language of the input."
)
_CONTEXT_PROMPT = (
    "Below is where a user stands in a conversation with a coding agent. Summarize it "
    "for a {width}-column sidebar, so the user sees at a glance the overall work and "
    "what their latest message does within it.\n"
    "Line 1: 'GOAL: ' then the overall work of this conversation in 2 to 6 words, at "
    "most {width} characters. If a previous goal is given, repeat it word for word "
    "unless the work clearly moved to something else.\n"
    "Line 2: 'STEP: ' then exactly one tag, ': ', then what the latest message asks, as "
    "an imperative, at most {chars} characters. Tags: NEW (starts another piece of "
    "work), CONTINUE (next step or refinement of the current work), FIX (asks to "
    "correct what the agent did), APPROVE (approves, accepts or answers what the agent "
    "proposed or asked: say what is approved or answered, from the agent's message, "
    "never just 'yes'), QUESTION (asks for information or an explanation, no change).\n"
    "Then 0 to 3 lines, each starting with '- ', at most {bullet} characters: the key "
    "specifics only (which page/file/feature, values, constraints). Skip bullets that "
    "would repeat line 2.\n"
    "On lines 2 and after, wrap the 1 or 2 words that matter most (the object, a file, "
    "a value) in curly braces, like {{this}}. Plain text otherwise. Write in the "
    "language of the user's messages.\n\n{text}"
)
STEP_TAGS = ("NEW", "CONTINUE", "FIX", "APPROVE", "QUESTION")
_TITLE_PROMPT = (
    "Give a 2 to 5 word title naming the subject of this request to a coding agent, "
    "at most {chars} characters. Keep the specific nouns (feature, page, file, value); "
    "drop politeness, 'can you', 'I want', filler and verbs when possible. "
    "Request:\n\n{text}"
)
_REPLY_PROMPT = (
    "This is the latest message a coding agent wrote to its user. Summarize it for a "
    "{width}-column sidebar so the user knows at a glance what was done and whether it "
    "is done.\n"
    "Line 1 starts with exactly one tag, then ': ', then the outcome in at most {chars2} "
    "characters. Tags: DONE (the request is completed), PARTIAL (done in part, or with "
    "a caveat), FAILED (could not do it, error, blocked), WAITING (the agent asks the "
    "user a question or needs a decision), WORKING (still in progress).\n"
    "Then 0 to 3 lines starting with '- ', at most {bullet} characters each: concrete "
    "changes or findings (files, features, numbers, tests). Skip what line 1 says.\n"
    "If the tag is WAITING, last line starts with '? ' and gives the question, at most "
    "{chars2} characters.\n"
    "Wrap the 1 or 2 words that matter most on each line (feature, file, number, "
    "result) in curly braces, like {{this}}. Plain text otherwise.\n\nMessage:\n\n{text}"
)


def _cut(text: str, limit: int) -> str:
    """Shorten at a word boundary (never mid-word), with an ellipsis."""
    if len(text) <= limit:
        return text
    head = text[: limit - 1].rsplit(" ", 1)[0].rstrip(" ,;:")
    if head.count("{") > head.count("}"):
        head += "}"
    return head + "…"


def context_input(goal: str, earlier: List[str], before: str, ask: str) -> str:
    """The situation sent with _CONTEXT_PROMPT: previous goal, earlier asks (cut),
    the end of the agent's message the latest ask answers, and the latest ask."""
    parts = []
    if goal.strip():
        parts.append("Previous goal: " + " ".join(goal.split()))
    if earlier:
        parts.append("Earlier messages from the user, oldest first:\n" + "\n".join(
            "- " + _cut(" ".join(t.split()), 300) for t in earlier if t.strip()))
    if before.strip():
        tail = before.strip()
        if len(tail) > 1500:
            tail = "…" + tail[-1500:]
        parts.append("Agent's message just before the latest one:\n" + tail)
    parts.append("Latest message from the user:\n" + ask[:4000])
    return "\n\n".join(parts)


def split_context(text: str) -> Tuple[str, str, str, List[str]]:
    """Normalized context recap -> (goal, tag, step, points). Tag is one of STEP_TAGS
    or '' when the model gave none or an unknown one."""
    goal, tag, step, points = "", "", "", []
    for line in (text or "").split("\n"):
        upper = line.upper()
        if upper.startswith("GOAL:"):
            goal = line[5:].strip()
        elif upper.startswith("STEP:"):
            step = line[5:].strip()
            m = re.match(r"^([A-Za-z]+)\s*:\s*", step)
            if m and m.group(1).upper() in STEP_TAGS:
                tag, step = m.group(1).upper(), step[m.end():]
        elif line.startswith("- "):
            points.append(line[2:])
        elif not step:
            step = line
    return goal, tag, step, points[:3]


def normalize(kind: str, raw: str, width: int) -> str:
    """Clean a model answer. One line for titles; structured lines otherwise:
    context = 'GOAL: ' line + 'STEP: ' line + '- ' bullets; reply = first line +
    '- ' bullets + '? ' question."""
    lines = [l.replace("**", "").replace("__", "").strip().strip('"').strip() for l in (raw or "").splitlines()]
    lines = [l.lstrip("*#").strip() for l in lines if l.strip()]
    if not lines:
        return ""
    if kind == "context":
        goal, step, bullets = "", "", []
        for l in lines:
            body = l.lstrip("-•* ").strip()
            upper = body.upper()
            if upper.startswith("GOAL:") and not goal:
                goal = " ".join(body[5:].replace("{", "").replace("}", "").split())
            elif upper.startswith("STEP:") and not step:
                step = "STEP: " + body[5:].strip()
            elif not step and not l.startswith(("-", "•")):
                step = body
            elif body:
                bullets.append("- " + body)
        out = ["GOAL: " + _cut(goal, width)] if goal else []
        if step:
            out.append(_cut(step, width * 3))
        return "\n".join(out + [_cut(b, width * 2) for b in bullets[:3]]) if step else ""
    if kind != "reply3":
        # Single-line kinds never carry highlight braces.
        return " ".join(" ".join(lines).replace("{", "").replace("}", "").split())[: width * 3]
    head = lines[0].lstrip("-•* ").strip()
    bullets, question = [], ""
    for l in lines[1:]:
        if l.startswith("?"):
            question = "? " + l.lstrip("? ").strip()
        elif l.lstrip("-•* ").strip():
            bullets.append("- " + l.lstrip("-•* ").strip())
    out = [_cut(head, width * 3)] + [_cut(b, width * 2) for b in bullets[:3]]
    if question and kind == "reply3":
        out.append(_cut(question, width * 2))
    return "\n".join(out)


def resolve_backend(setting: str) -> Optional[Tuple[str, str]]:
    """('claude', '/path/to/claude') for the configured backend, or None.

    setting: off | auto | claude | codex | gemini. auto picks the first CLI found
    on PATH in the order claude, codex, gemini.
    """
    setting = (setting or "off").lower()
    if setting == "off":
        return None
    names = BACKENDS if setting == "auto" else (setting,)
    for name in names:
        if name in BACKENDS:
            path = shutil.which(name)
            if path:
                return name, path
    return None


def build_command(backend: str, binary: str, model: str, prompt: str, out_file: str) -> Tuple[List[str], Optional[str]]:
    """argv and stdin for one non-interactive, read-only, unpersisted call."""
    full = _SYSTEM + "\n\n" + prompt
    if backend == "claude":
        # No tools, no MCP servers, no skills, no hooks: the ask text comes from a
        # transcript and must never be able to make this call act on anything.
        argv = [binary, "-p", "--no-session-persistence", "--system-prompt", _SYSTEM,
                "--tools", "", "--strict-mcp-config", "--disable-slash-commands",
                "--permission-mode", "dontAsk",
                "--settings", '{"disableAllHooks": true}', "--output-format", "text"]
        if model:
            argv += ["--model", model]
        return argv, prompt
    if backend == "codex":
        argv = [binary, "exec", "--skip-git-repo-check", "--ephemeral", "--sandbox", "read-only",
                "--color", "never", "--output-last-message", out_file]
        if model:
            argv += ["--model", model]
        return argv + ["-"], full
    if backend == "gemini":
        # --skip-trust: headless runs refuse untrusted folders; ours is an empty private dir.
        argv = [binary, "--skip-trust", "--approval-mode", "plan", "--output-format", "text"]
        if model:
            argv += ["--model", model]
        return argv + ["--prompt", full], None
    raise ValueError(backend)


class Summarizer:
    def __init__(self, setting: str = "off", model: str = ""):
        resolved = resolve_backend(setting)
        self.backend, self.binary = resolved if resolved else ("", None)
        self.model = model or DEFAULT_MODELS.get(self.backend, "")
        self._cache: Dict[str, str] = {}
        self._pending: set = set()
        self._failed: Dict[str, float] = {}
        self._lock = threading.Lock()
        self._path = state_dir() / "summaries.json"
        self._load()
        self.changed = threading.Event()

    @property
    def available(self) -> bool:
        return self.binary is not None

    def _load(self) -> None:
        try:
            with open(self._path, encoding="utf-8") as fh:
                data = json.load(fh)
            if isinstance(data, dict):
                self._cache = {str(k): str(v) for k, v in data.items()}
        except (OSError, ValueError):
            pass

    def _save(self) -> None:
        ensure_private_dir(self._path.parent)
        items = list(self._cache.items())[-500:]
        fd, tmp = tempfile.mkstemp(prefix=".sum.", dir=str(self._path.parent))
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(dict(items), fh)
        os.chmod(tmp, 0o600)
        os.replace(tmp, self._path)

    @staticmethod
    def _key(kind: str, text: str, width: int) -> str:
        return hashlib.sha256("{}|{}|{}".format(kind, width, text).encode("utf-8")).hexdigest()[:24]

    def get(self, kind: str, text: str, width: int, key_text: Optional[str] = None) -> Optional[str]:
        """Cached summary, or None (and a background request is started).

        key_text, when given, is what the cache is keyed on instead of text: the
        parts of the input that define the result, without soft context that may
        differ from one panel run to the next."""
        if not self.available or not text.strip():
            return None
        key = self._key(kind, text if key_text is None else key_text, width)
        with self._lock:
            if key in self._cache:
                return self._cache[key]
            if key in self._pending or time.time() - self._failed.get(key, 0) < 600:
                return None
            self._pending.add(key)
        threading.Thread(target=self._run, args=(key, kind, text, width), daemon=True).start()
        return None

    def peek(self, kind: str, key_text: str, width: int) -> Optional[str]:
        """Cached summary only: never starts a request."""
        with self._lock:
            return self._cache.get(self._key(kind, key_text, width))

    def pending(self, kind: str, text: str, width: int) -> bool:
        with self._lock:
            return self._key(kind, text, width) in self._pending

    def call(self, prompt: str) -> str:
        """One blocking call to the backend. Returns '' on any failure."""
        if not self.binary:
            return ""
        workdir = ensure_private_dir(state_dir() / "llm-workdir")
        fd, out_file = tempfile.mkstemp(prefix=".out.", dir=str(workdir))
        os.close(fd)
        try:
            argv, stdin = build_command(self.backend, self.binary, self.model, prompt, out_file)
            proc = subprocess.run(
                argv, input=stdin if stdin is not None else "", capture_output=True, text=True,
                timeout=90, cwd=str(workdir),
                # Own session: herdr must not mistake the panel pane for an agent pane.
                start_new_session=True,
            )
            if proc.returncode != 0:
                return ""
            text = proc.stdout
            if self.backend == "codex":
                with open(out_file, encoding="utf-8", errors="replace") as fh:
                    text = fh.read()
            return text.strip()
        except (OSError, subprocess.SubprocessError, ValueError):
            return ""
        finally:
            try:
                os.unlink(out_file)
            except OSError:
                pass

    def _run(self, key: str, kind: str, text: str, width: int) -> None:
        template = {"title": _TITLE_PROMPT, "reply3": _REPLY_PROMPT, "context": _CONTEXT_PROMPT}[kind]
        chars = {"title": width}.get(kind, width * 2 - 4)
        raw = self.call(template.format(chars=chars, chars2=width * 2 - 4, width=width,
                                        bullet=width - 2, text=text[:8000] if kind == "context" else text[:4000]))
        result = normalize(kind, raw, width)
        with self._lock:
            self._pending.discard(key)
            if not result:
                self._failed[key] = time.time()
            else:
                self._cache[key] = result
                try:
                    self._save()
                except OSError:
                    pass
        self.changed.set()
