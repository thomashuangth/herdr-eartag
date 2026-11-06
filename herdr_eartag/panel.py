"""Collects data for one agent pane and turns it into a render.View."""
from __future__ import annotations

import re
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from . import antigravity, claude, codex, efforts, gitinfo, herdr, prefs, statusline, textutil
from .config import Config
from .i18n import Tr
from .render import Bar, View
from .summarize import Summarizer

PANE_POLL = 1.5
SLOW_POLL = 5.0
STALE_QUOTAS = 30 * 60

MODE_TAGS = {
    "default": "normal", "auto": "auto", "plan": "plan",
    "acceptEdits": "edits", "bypassPermissions": "bypass", "dontAsk": "dontask",
}


# Agents whose sessions Eartag reads (asks, replies, files, context, quotas).
READ_AGENTS = ("claude", "codex", "agy")
# Agents whose statusline the relay taps (model, effort, context, quotas).
TAPPED_AGENTS = ("claude", "agy")


def agent_kind(agent: str) -> str:
    agent = (agent or "").lower()
    return "agy" if agent in antigravity.AGENTS else agent


def short_tokens(n: float) -> str:
    if n >= 1_000_000:
        value = n / 1_000_000
        return ("{:.1f}".format(value).rstrip("0").rstrip(".")) + "M"
    if n >= 1000:
        return "{}k".format(int(round(n / 1000)))
    return str(int(n))


def split_verdict(text: str) -> Tuple[str, str]:
    """'DONE: outcome...' -> ('DONE', 'outcome...'); untagged text -> ('', text)."""
    m = re.match(r"^(DONE|PARTIAL|FAILED|WAITING|WORKING)\s*:\s*", text or "")
    return (m.group(1), text[m.end():]) if m else ("", text or "")


class PanelModel:
    def __init__(self, target_pane: str, cfg: Config, tr: Tr):
        self.target = target_pane
        self.cfg = cfg
        self.tr = tr
        self.pane: Optional[Dict[str, Any]] = None
        self.pane_missing = False
        self.transcript: Optional[claude.Transcript] = None
        self.claude_base: Optional[Path] = None  # config dir the Claude transcript lives in
        self.inferred = False
        self.git: Optional[gitinfo.GitInfo] = None
        self.agents: List[Dict[str, Any]] = []
        self.tasks: Optional[Tuple[int, int]] = None
        self.session_snap: Optional[Dict[str, Any]] = None
        self.rate_snap: Optional[Dict[str, Any]] = None
        self.git_files: List[str] = []
        self.diff: Optional[Tuple[int, int]] = None
        self._reply_src = ""       # message last sent for rephrasing while working
        self._reply_req_ts = 0.0
        self.others = 0
        self.status = "unknown"
        self.status_since = time.time()
        self._next_pane = 0.0
        self._next_slow = 0.0
        self.summarizer: Optional[Summarizer] = None
        self.needs_setup = False
        self.set_summarize(prefs.summarize_wanted(prefs.load(), cfg.summarize))
        self.error = ""

    def set_summarize(self, on: bool) -> None:
        if not on:
            self.summarizer = None
            return
        if self.summarizer is None:
            setting = self.cfg.summarize if self.cfg.summarize != "off" else "auto"
            summarizer = Summarizer(setting, self.cfg.summarize_model)
            self.summarizer = summarizer if summarizer.available else None

    # -- polling -----------------------------------------------------------------
    @property
    def agent(self) -> str:
        return str((self.pane or {}).get("agent") or "")

    @property
    def kind(self) -> str:
        return agent_kind(self.agent)

    @property
    def reads(self) -> bool:
        return self.kind in READ_AGENTS

    @property
    def cwd(self) -> Optional[str]:
        pane = self.pane or {}
        return pane.get("foreground_cwd") or pane.get("cwd")

    @property
    def session_id(self) -> Optional[str]:
        sess = (self.pane or {}).get("agent_session") or {}
        value = sess.get("value") if isinstance(sess, dict) else None
        return value if isinstance(value, str) else None

    def poll(self, now: Optional[float] = None) -> bool:
        now = now or time.time()
        changed = False
        if now >= self._next_pane:
            self._next_pane = now + PANE_POLL
            try:
                pane = herdr.pane_get(self.target)
                self.error = ""
            except herdr.HerdrError as exc:
                self.error = exc.code
                pane = self.pane
            if pane is None:
                self.pane_missing = True
                return True
            if pane != self.pane:
                changed = True
            self.pane = pane
            status = str(pane.get("agent_status") or "unknown") if pane.get("agent") else "none"
            if status != self.status:
                self.status = status
                self.status_since = now
                changed = True
            changed |= self._poll_transcript()
            snap = statusline.read_session(self._session_key()) if self.kind in TAPPED_AGENTS else None
            rate = self._read_rate()
            if snap != self.session_snap or rate != self.rate_snap:
                self.session_snap, self.rate_snap = snap, rate
                changed = True
        if now >= self._next_slow:
            self._next_slow = now + SLOW_POLL
            changed |= self._poll_slow()
        if self.summarizer and self.summarizer.changed.is_set():
            self.summarizer.changed.clear()
            changed = True
        return changed

    def _session_key(self) -> Optional[str]:
        """Session id for the relay's snapshots (the transcript's own id when herdr gave none)."""
        if self.session_id:
            return self.session_id
        if self.transcript is None:
            return None
        path = self.transcript.path
        return path.parents[2].name if self.kind == "agy" else path.stem

    def _read_rate(self) -> Optional[Dict[str, Any]]:
        if self.kind == "claude":
            base = str(self.claude_base) if self.claude_base else None
            return statusline.read_rate_limits(statusline.claude_scope(base))
        if self.kind == "agy":
            return statusline.read_rate_limits("agy")
        if self.kind == "codex" and self.transcript is not None:
            return self.transcript.data.rate_limits
        return None

    def _poll_transcript(self) -> bool:
        kind = self.kind
        if kind not in READ_AGENTS:
            if self.transcript is not None:
                self.transcript = None
                return True
            return False
        reader = claude.Transcript
        if kind == "claude":
            title = str((self.pane or {}).get("terminal_title_stripped") or "")
            path, inferred = claude.find_transcript(self.session_id, self.cwd, title)
            self.claude_base = claude.config_dir_of(path) if path else None
        elif kind == "agy":
            path, inferred = antigravity.find_transcript(self.session_id, self.cwd)
            reader = antigravity.AgyTranscript
        else:
            path = codex.find_session(self.session_id, self.cwd)
            inferred = not self.session_id
            reader = codex.CodexTranscript
        self.inferred = inferred and path is not None
        if path is None:
            return False
        if self.transcript is None or self.transcript.path != path:
            self.transcript = reader(path)
        return self.transcript.refresh()

    def _poll_slow(self) -> bool:
        before = (self.git, self.others, self.tasks, self.git_files, self.diff)
        self.git = gitinfo.find(self.cwd)
        try:
            self.agents = herdr.agent_list()
        except herdr.HerdrError:
            pass
        self.tasks = claude.read_tasks(self._session_key(), self.claude_base) if self.kind == "claude" else None
        if self.agent and not self.reads and self.git:
            self.git_files = gitinfo.changed_files(self.git.root)
        else:
            self.git_files = []
        self.others = self._others_on_branch()
        self.diff = gitinfo.diff_stat(self.git.root) if self.git else None
        if self.kind in TAPPED_AGENTS:
            from . import setup
            self.needs_setup = setup.needs_setup(self.kind)
        return before != (self.git, self.others, self.tasks, self.git_files, self.diff)

    # -- helpers -----------------------------------------------------------------
    def _clock(self, ts: float, now: float) -> str:
        dt = datetime.fromtimestamp(ts)
        if dt.date() == datetime.fromtimestamp(now).date():
            return dt.strftime("%H:%M")
        return dt.strftime("%d/%m")

    def _ago(self, ts: Optional[float], now: float) -> str:
        if not ts:
            return ""
        if now - ts < 60:
            return self.tr("just_now")
        return self.tr("ago", d=self.tr.duration(now - ts))

    def _date_long(self, ts: float, now: float) -> Tuple[str, str]:
        dt = datetime.fromtimestamp(ts)
        days = self.tr.raw("days")
        months = self.tr.raw("months")
        label = self.tr("fmt.date", wd=days[dt.weekday()], d=dt.day, mon=months[dt.month - 1],
                        hm=dt.strftime("%H:%M"))
        return label, self.tr("in", d=self.tr.duration(max(0, ts - now)))

    def _fill_recap(self, v: View, text: str, inner: int) -> None:
        """Current ask: rephrased title + key points when available, raw text otherwise."""
        v.ask, v.ask_points, v.ask_pending = text, [], False
        v.ask_max_lines = 5
        if not self.summarizer:
            return
        recap = self.summarizer.get("recap3", text, inner)
        if recap:
            lines = recap.split("\n")
            v.ask = lines[0]
            v.ask_points = [l[2:] for l in lines[1:] if l.startswith("- ")]
            v.ask_max_lines = 2
        else:
            v.ask_pending = self.summarizer.pending("recap3", text, inner)
            v.ask_max_lines = 4

    def _reply(self, text: str, inner: int, working: bool = False) -> str:
        """Agent's latest message, condensed: LLM summary, else its first sentences.
        While the agent works its message changes all the time: rephrase at most once a
        minute (each call costs quota) and keep showing the previous summary meanwhile."""
        if self.summarizer:
            source = text
            if working and self._reply_src and time.time() - self._reply_req_ts < 60:
                source = self._reply_src
            elif working:
                self._reply_src, self._reply_req_ts = text, time.time()
            summary = self.summarizer.get("reply3", source, inner)
            if summary:
                return summary
        plain = " ".join(l.strip().lstrip("#>*-• ").strip() for l in text.splitlines() if l.strip())
        return plain.replace("**", "").replace("`", "")

    def _title(self, text: str, room: int) -> str:
        """2-5 word subject for the history list."""
        if self.summarizer:
            title = self.summarizer.get("title", text, room)
            if title:
                return title
        return textutil.short_title(text, room)

    def view(self, width: int, height: int, selected_bar: Optional[str]) -> View:
        tr = self.tr
        cfg = self.cfg
        now = time.time()
        padx = 2 if width >= 30 else 1
        inner = max(10, width - 2 * padx)
        v = View(width=width, height=height, ascii=cfg.icons == "ascii")

        root = self.git.root if self.git else None
        v.project = (root.name if root else (Path(self.cwd).name if self.cwd else "")) or "—"
        data = self.transcript.data if self.transcript else None
        status = self.status if self.status in ("working", "blocked", "done", "idle") else "unknown"
        v.status = status
        v.status_label = tr("status." + status)
        if data and data.permission_mode and self.kind == "claude":
            v.mode_tag = MODE_TAGS.get(data.permission_mode, data.permission_mode)

        snap = self.session_snap or {}
        # Model, effort
        model_id = ""
        snap_model = snap.get("model") or {}
        if isinstance(snap_model, dict):
            model_id = snap_model.get("id") or ""
        if data and data.model and (not model_id or (data.last_activity_ts or 0) > snap.get("ts", 0)):
            model_id = data.model
        v.model, v.version = efforts.display_name(model_id)
        effort = snap.get("effort") or (data.effort if data else "")
        if data and data.effort and (data.last_activity_ts or 0) > snap.get("ts", 0):
            effort = data.effort
        v.effort = effort if v.model and v.model.lower() != "haiku" else ""
        v.effort_level = efforts.dots(model_id, v.effort, self.agent)

        # Footer: machine
        v.host = cfg.host
        if self.git:
            v.branch = self.git.branch or "HEAD"

        # Bars
        # Only show gauges that have a value: an empty "—" bar says nothing.
        v.bars = [b for b in (self._bars(data, now) if self.reads else []) if b.pct is not None]
        v.selected_bar = selected_bar
        if self.reads:
            if self.needs_setup:
                v.footnote_icon = "now"
                v.footnote = tr("setup_line")
                v.footnote_action = "setup"
            elif self.rate_snap and now - float(self.rate_snap.get("ts", now)) > STALE_QUOTAS:
                v.footnote_icon = "stale"
                v.footnote = tr("stale_quotas", d=self._ago(float(self.rate_snap["ts"]), now))
            elif not self.session_snap and not self.rate_snap and self.kind in TAPPED_AGENTS:
                # Relay installed, but Claude has not refreshed its statusline yet.
                v.footnote = tr("quotas_after_reply")

        # Alerts
        if self.git and self.git.detached:
            v.alerts.append(tr("detached_head"))
        elif self.git and self.git.branch:
            others = self.others
            if others == 1:
                v.alerts.append(tr("other_agents_one", b=self.git.branch))
            elif others > 1:
                v.alerts.append(tr("other_agents_many", n=others, b=self.git.branch))

        if not self.agent:
            v.main_kind = "none"
            v.message_lines = [tr("no_agent")]
            return v
        if self.kind != "claude":
            model_id, effort = self._other_agent_model(data)
            v.model, v.version = efforts.display_name(model_id) if model_id else (self.agent.capitalize(), "")
            v.effort = effort
            v.effort_level = efforts.dots(model_id, effort, self.kind)
        if not self.reads:
            v.main_kind = "none"
            v.message_lines = [tr("unsupported_1"), tr("unsupported_2")]
            if self.git_files:
                v.files_title = tr("files_git")
                v.files = [Path(f).name for f in self.git_files]
            return v

        if self.inferred:
            v.inferred_note = tr("inferred_session")
        if status == "done":
            v.notif = (tr("your_turn_review"), "done")
        elif status == "blocked":
            v.notif = (tr("your_turn_permission", d=self.tr.duration(now - self.status_since)), "blocked")

        asks = data.asks if data else []
        if not asks:
            v.main_kind = "none"
            v.message_lines = [tr("waiting_first_1"), tr("waiting_first_2")]
        else:
            current = asks[-1]
            self._fill_recap(v, current.text, inner)
            v.main_title = tr("now") if status == "working" else tr("last_ask")
            v.main_right = tr.short(now - current.ts)
            v.main_kind = "now" if status == "working" else "last"
            if v.ask_pending and status == "working":
                v.activity, v.activity_icon, v.activity_role = tr("rephrasing"), "pending", "sub"
            elif status == "working":
                v.activity = data.last_tool if data else ""
            elif status == "blocked":
                v.activity, v.activity_role = (data.last_tool if data else ""), "blocked"
            else:
                last = data.last_activity_ts if data else None
                if last and last > current.ts:
                    v.activity = tr("finished_in", d=tr.short(last - current.ts))
                else:
                    v.activity = tr("finished")
            if data and data.title:
                v.session_title = data.title
            if data and data.last_reply and (data.last_reply_ts or 0) >= current.ts:
                v.reply_label = tr("progress") if status == "working" else tr("reply")
                v.reply = self._reply(data.last_reply, inner, working=status == "working")
                tag, v.reply = split_verdict(v.reply)
                if tag:
                    tag = "WORKING" if status == "working" else tag  # a question it asked earlier is stale now
                    v.reply_verdict = tag
                    v.reply_verdict_label = tr("verdict." + tag.lower())
            tasks = self.tasks or (data.todos if data else None)
            if tasks and (tasks[0] < tasks[1] or status == "working"):
                v.tasks = (tasks[0], tasks[1])
                v.task_now = tasks[2] if len(tasks) > 2 else ""
                v.tasks_label = tr("tasks")
            v.before_title = tr("before")
            room = inner - 8  # "✓ HH:MM "
            for ask in reversed(asks[:-1][-3:]):
                verdict = ""
                if ask.reply and self.summarizer:  # cached once rephrased: one call per ask
                    verdict, _ = split_verdict(self.summarizer.get("reply3", ask.reply[-4000:], inner) or "")
                v.before.append((self._clock(ask.ts, now), self._title(ask.text, room), verdict))
        if data and data.files:
            v.files_title = tr("files")
            v.files = list(reversed(list(data.files.keys())))
            v.diff = self.diff
        return v

    def _other_agent_model(self, data: Optional[claude.SessionData]) -> Tuple[str, str]:
        """(model id, effort) for agents other than Claude Code, when they expose it."""
        if self.kind == "codex":
            if data and data.model:
                return data.model, data.effort
            path = codex.find_session(self.session_id, self.cwd)
            if path:
                model, effort = codex.model_and_effort(path)
                if model:
                    return model, effort
            return codex.configured()
        if self.kind == "agy":
            snap_model = (self.session_snap or {}).get("model") or {}
            model = snap_model.get("id") if isinstance(snap_model, dict) else ""
            if model:
                return model, str((self.session_snap or {}).get("effort") or antigravity.effort_of(model))
            return antigravity.configured_model()
        return "", ""

    def _bars(self, data: Optional[claude.SessionData], now: float) -> List[Bar]:
        tr = self.tr
        snap = self.session_snap or {}
        ctx = snap.get("context") or {}
        ctx_pct = None
        size = ctx.get("context_window_size") if isinstance(ctx, dict) else None
        used_tokens = ctx.get("current_tokens") if isinstance(ctx, dict) else None
        transcript_newer = data is not None and (data.last_activity_ts or 0) > snap.get("ts", 0) + 5
        if isinstance(ctx, dict) and isinstance(ctx.get("used_percentage"), (int, float)) and not transcript_newer:
            ctx_pct = float(ctx["used_percentage"])
        if data and data.context_window and not size:
            size = data.context_window
        if data and data.context_tokens and (ctx_pct is None):
            model = data.model or (self._other_agent_model(data)[0] if self.kind == "agy" else "")
            window = size if isinstance(size, (int, float)) and size else self._guess_window(model, data.context_tokens)
            used_tokens = data.context_tokens
            ctx_pct = min(100.0, data.context_tokens / window * 100.0)
            size = window
        ctx_bar = Bar("ctx", "CTX", ctx_pct if self.reads else None)
        if ctx_pct is not None:
            if not used_tokens and size:
                used_tokens = ctx_pct / 100.0 * size
            if used_tokens and size:
                ctx_bar.detail_icon = "ctx"
                ctx_bar.detail = tr("ctx_detail", used=short_tokens(used_tokens), total=short_tokens(size))
        bars = [ctx_bar]
        limits = (self.rate_snap or {}).get("rate_limits") or {}
        for key, label, window in (("h5", "5H", "five_hour"), ("d7", tr("label.week"), "seven_day")):
            entry = limits.get(window) if isinstance(limits, dict) else None
            pct = None
            bar = Bar(key, label, None)
            if isinstance(entry, dict) and isinstance(entry.get("used_percentage"), (int, float)):
                pct = float(entry["used_percentage"])
                resets = entry.get("resets_at")
                if isinstance(resets, (int, float)):
                    if resets < now:
                        pct = 0.0
                    else:
                        bar.detail_icon = "reset"
                        bar.detail, bar.detail_sub = self._date_long(float(resets), now)
                        bar.detail_sub = "· " + bar.detail_sub
            bar.pct = pct if self.reads else None
            bars.append(bar)
        return bars

    @staticmethod
    def _guess_window(model: str, tokens: int = 0) -> int:
        known = statusline.read_windows().get(model or "")
        if isinstance(known, (int, float)) and known > 0:
            return int(known)
        if "gemini" in (model or "").lower():
            return 1_048_576
        if "[1m]" in (model or "") or tokens > 200_000:
            return 1_000_000
        return 200_000

    def _others_on_branch(self) -> int:
        if not self.git or not self.git.branch:
            return 0
        count = 0
        for agent in self.agents:
            if agent.get("pane_id") == self.target:
                continue
            other = gitinfo.find(agent.get("foreground_cwd") or agent.get("cwd"))
            if other and other.common_dir == self.git.common_dir and other.branch == self.git.branch:
                count += 1
        return count
