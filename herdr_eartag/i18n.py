"""User-facing strings (French and English)."""
from __future__ import annotations

import os
import subprocess
import sys
from typing import Dict

STRINGS: Dict[str, Dict[str, str]] = {
    "fr": {
        "status.working": "EN COURS",
        "status.blocked": "EN ATTENTE",
        "status.done": "TERMINÉ",
        "status.idle": "AU REPOS",
        "status.unknown": "INCONNU",
        "now": "MAINTENANT",
        "last_ask": "DERNIÈRE DEMANDE",
        "before": "AVANT",
        "reply": "RÉPONSE",
        "verdict.done": "FAIT",
        "verdict.partial": "PARTIEL",
        "verdict.failed": "ÉCHEC",
        "verdict.waiting": "À TOI",
        "verdict.working": "EN COURS",
        "progress": "AVANCEMENT",
        "files": "FICHIERS",
        "files_git": "FICHIERS (git)",
        "tasks": "tâches",
        "ago": "il y a {d}",
        "just_now": "à l'instant",
        "in": "dans {d}",
        "finished": "terminé",
        "finished_in": "terminé en {d}",
        "your_turn_review": "À TOI · relire le résultat",
        "your_turn_permission": "À TOI · autorisation · {d}",
        "inferred_session": "session déduite du dossier",
        "inferred_fix": "Corriger (relier ce compte à herdr)",
        "rephrasing": "reformulation…",
        "step.new": "nouveau",
        "step.continue": "suite",
        "step.fix": "correctif",
        "step.approve": "validation",
        "step.question": "question",
        "answering": "réponse à :",
        "other_agents_one": "1 autre agent sur {b}",
        "other_agents_many": "{n} autres agents sur {b}",
        "detached_head": "HEAD détachée (aucune branche)",
        "stale_quotas": "quotas relevés {d}",
        "unsupported_1": "Demandes lues pour Claude Code,",
        "unsupported_2": "Codex et Antigravity seulement.",
        "no_agent": "Aucun agent dans ce pane.",
        "waiting_first_1": "Envoie une première demande à",
        "waiting_first_2": "l'agent : le récap s'affiche ici.",
        "quotas_after_reply": "quotas après la prochaine réponse",
        "ctx_detail": "~{used} tokens sur {total}",
        "menu.hide_agent": "Masquer pour cet agent",
        "menu.hide_workspace": "Masquer pour ce workspace",
        "menu.auto_off": "Ne plus ouvrir automatiquement",
        "menu.auto_on": "Ouvrir automatiquement",
        "setup_line": "Activer quotas + prefix+i",
        "menu.setup": "Activer quotas + prefix+i",
        "menu.summarize_start": "Reformuler les demandes ({b})",
        "toast.setup_done": "Eartag configuré",
        "toast.setup_body": "Quotas après la prochaine réponse de l'agent, session exacte dès la prochaine session. Annuler : action undo-setup.",
        "menu.summarize_on": "Reformulation {b} · couper",
        "menu.close": "fermer",
        "menu.esc": "Échap",
        "toast.hidden_agent": "Eartag masqué pour cet agent",
        "toast.hidden_workspace": "Eartag masqué pour ce workspace",
        "toast.auto_off": "Ouverture auto désactivée",
        "toast.reopen": "{key} pour le réafficher",
        "toast.summarize_on": "Reformulation activée ({b})",
        "toast.summarize_on_body": "Tes demandes et les réponses de l'agent sont envoyées à {b} sous ton compte. ⋯ pour couper.",
        # Formats: {wd} weekday, {d} day, {mon} month, {hm} HH:MM; units for durations.
        "fmt.date": "{wd} {d} {mon} {hm}",
        "fmt.seconds": "{n} s", "fmt.minutes": "{n} min", "fmt.hours": "{h} h {m:02d}", "fmt.hours0": "{h} h",
        "fmt.days": "{n} j", "unit.day": "j", "label.week": "7J",
        "days": ["lun.", "mar.", "mer.", "jeu.", "ven.", "sam.", "dim."],
        "months": ["janv.", "févr.", "mars", "avr.", "mai", "juin", "juil.", "août", "sept.", "oct.", "nov.", "déc."],
    },
    "en": {
        "status.working": "WORKING",
        "status.blocked": "WAITING",
        "status.done": "DONE",
        "status.idle": "IDLE",
        "status.unknown": "UNKNOWN",
        "now": "NOW",
        "last_ask": "LAST ASK",
        "before": "BEFORE",
        "reply": "REPLY",
        "verdict.done": "DONE",
        "verdict.partial": "PARTIAL",
        "verdict.failed": "FAILED",
        "verdict.waiting": "YOUR TURN",
        "verdict.working": "WORKING",
        "progress": "PROGRESS",
        "files": "FILES",
        "files_git": "FILES (git)",
        "tasks": "tasks",
        "ago": "{d} ago",
        "just_now": "just now",
        "in": "in {d}",
        "finished": "finished",
        "finished_in": "finished in {d}",
        "your_turn_review": "YOUR TURN · review the result",
        "your_turn_permission": "YOUR TURN · permission · {d}",
        "inferred_session": "session inferred from folder",
        "inferred_fix": "Fix (link this account to herdr)",
        "rephrasing": "rephrasing…",
        "step.new": "new",
        "step.continue": "follow-up",
        "step.fix": "fix",
        "step.approve": "approval",
        "step.question": "question",
        "answering": "answering:",
        "other_agents_one": "1 other agent on {b}",
        "other_agents_many": "{n} other agents on {b}",
        "detached_head": "detached HEAD (no branch)",
        "stale_quotas": "quotas read {d}",
        "unsupported_1": "Asks are read for Claude Code,",
        "unsupported_2": "Codex and Antigravity only.",
        "no_agent": "No agent in this pane.",
        "waiting_first_1": "Send the agent a first ask:",
        "waiting_first_2": "the recap shows up here.",
        "quotas_after_reply": "quotas after the next reply",
        "ctx_detail": "~{used} tokens of {total}",
        "menu.hide_agent": "Hide for this agent",
        "menu.hide_workspace": "Hide for this workspace",
        "menu.auto_off": "Stop opening automatically",
        "menu.auto_on": "Open automatically",
        "setup_line": "Enable quotas + prefix+i",
        "menu.setup": "Enable quotas + prefix+i",
        "menu.summarize_start": "Rephrase asks ({b})",
        "toast.setup_done": "Eartag set up",
        "toast.setup_body": "Quotas after the agent's next reply, exact session from the next session on. Undo: the undo-setup action.",
        "menu.summarize_on": "Rephrasing via {b} · pause",
        "menu.close": "close",
        "menu.esc": "Esc",
        "toast.hidden_agent": "Eartag hidden for this agent",
        "toast.hidden_workspace": "Eartag hidden for this workspace",
        "toast.auto_off": "Auto open turned off",
        "toast.reopen": "{key} to show it again",
        "toast.summarize_on": "Rephrasing on ({b})",
        "toast.summarize_on_body": "Your asks and the agent's replies are sent to {b} under your account. ⋯ to turn it off.",
        "fmt.date": "{wd} {mon} {d} {hm}",
        "fmt.seconds": "{n}s", "fmt.minutes": "{n} min", "fmt.hours": "{h}h {m:02d}m", "fmt.hours0": "{h}h",
        "fmt.days": "{n}d", "unit.day": "d", "label.week": "7D",
        "days": ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"],
        "months": ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"],
    },
}


def _match(locale: str) -> str:
    """'pt_BR.UTF-8' -> 'pt' if a 'pt' table exists, else English."""
    code = locale.replace("-", "_").split(".")[0].split("_")[0].lower()
    return code if code in STRINGS else "en"


def detect_language() -> str:
    """Language of the system, among the translations in STRINGS (English otherwise).
    Adding a translation to STRINGS is enough for it to be picked up."""
    for var in ("LC_ALL", "LC_MESSAGES", "LANG", "LANGUAGE"):
        value = os.environ.get(var, "")
        if value and not value.upper().startswith(("C.", "C:", "POSIX")) and value.upper() != "C":
            return _match(value.split(":")[0])
    if sys.platform == "darwin":
        try:
            out = subprocess.run(
                ["defaults", "read", "-g", "AppleLocale"],
                capture_output=True, text=True, timeout=2,
            ).stdout.strip()
            if out:
                return _match(out)
        except (OSError, subprocess.SubprocessError):
            pass
    return "en"


class Tr:
    def __init__(self, lang: str):
        lang = (lang or "").replace("-", "_").split("_")[0].lower()
        self.lang = lang if lang in STRINGS else "en"
        self.s = STRINGS[self.lang]

    def __call__(self, _name: str, **kw) -> str:
        # `_name`, not `key`: callers pass key=... for the keybinding placeholder.
        text = self.s.get(_name) or STRINGS["en"].get(_name) or _name
        return text.format(**kw) if kw else text

    def raw(self, key: str):
        return self.s.get(key, STRINGS["en"][key])

    def short(self, seconds: float) -> str:
        """Compact age for the right of a title line: <1m, 4m, 1h20, 3j / 3d."""
        s = max(0, int(seconds))
        if s < 60:
            return "<1m"
        m = s // 60
        if m < 60:
            return "{}m".format(m)
        h, m = divmod(m, 60)
        if h < 24:
            return "{}h{:02d}".format(h, m) if m else "{}h".format(h)
        return "{}{}".format(h // 24, self("unit.day"))

    def duration(self, seconds: float) -> str:
        s = max(0, int(seconds))
        if s < 60:
            return self("fmt.seconds", n=s)
        m = s // 60
        if m < 60:
            return self("fmt.minutes", n=m)
        h, m = divmod(m, 60)
        if h < 24:
            return self("fmt.hours", h=h, m=m) if m else self("fmt.hours0", h=h)
        return self("fmt.days", n=h // 24)
