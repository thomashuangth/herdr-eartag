"""Terminal text helpers: display width, wrapping and truncation."""
from __future__ import annotations

import re
import unicodedata
from typing import List

_ZERO_WIDTH_CATEGORIES = {"Mn", "Me", "Cf"}


def char_width(ch: str) -> int:
    if not ch:
        return 0
    code = ord(ch)
    if code < 32 or 0x7F <= code < 0xA0:
        return 0
    if unicodedata.category(ch) in _ZERO_WIDTH_CATEGORIES:
        return 0
    if unicodedata.east_asian_width(ch) in ("W", "F"):
        return 2
    # Most emoji live in these planes and render double width.
    if 0x1F300 <= code <= 0x1FAFF:
        return 2
    return 1


def width(text: str) -> int:
    return sum(char_width(c) for c in text)


def truncate(text: str, max_width: int, ellipsis: str = "…") -> str:
    if max_width <= 0:
        return ""
    if width(text) <= max_width:
        return text
    limit = max_width - width(ellipsis)
    out = []
    used = 0
    for c in text:
        w = char_width(c)
        if used + w > limit:
            break
        out.append(c)
        used += w
    return "".join(out).rstrip() + ellipsis


def pad(text: str, total: int) -> str:
    return text + " " * max(0, total - width(text))


def collapse_ws(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def wrap(text: str, max_width: int, max_lines: int, ellipsis: str = "…") -> List[str]:
    """Greedy word wrap. The last kept line ends with an ellipsis when text was cut."""
    words = collapse_ws(text).split(" ")
    lines: List[str] = []
    current = ""
    i = 0
    while i < len(words):
        word = words[i]
        if not word:
            i += 1
            continue
        candidate = word if not current else current + " " + word
        if width(candidate) <= max_width:
            current = candidate
            i += 1
            continue
        if not current:
            # Single word longer than the line: hard cut it.
            cut = truncate(word, max_width, "") or word[0]
            lines.append(cut)
            words[i] = word[len(cut):]
        else:
            lines.append(current)
            current = ""
        if len(lines) == max_lines:
            break
    else:
        if current:
            lines.append(current)
        return lines[:max_lines]
    # Ran out of lines with text left over.
    last = lines[max_lines - 1]
    if width(last) + width(ellipsis) > max_width:
        last = truncate(last, max_width, ellipsis)
    else:
        last = last.rstrip(" ,;:") + ellipsis
    lines[max_lines - 1] = last
    return lines[:max_lines]


_FILLER_START = [
    # French
    "est-ce que tu pourrais", "est-ce que tu peux", "est-ce qu'on peut", "est-ce que", "est ce que",
    "tu pourrais", "tu peux", "peux-tu", "pourrais-tu", "j'aimerais bien que tu", "j'aimerais que tu",
    "j'aimerais bien", "j'aimerais", "je voudrais que tu", "je voudrais", "je veux que tu", "je veux",
    "il faudrait que tu", "il faudrait", "il faut que tu", "il faut", "on pourrait", "on peut",
    "alors", "ok", "okay", "d'accord", "du coup", "en fait", "sinon", "aussi", "bon", "bah", "ben",
    "et", "mais", "donc", "maintenant", "stp", "s'il te plaît", "s'il te plait", "vas-y", "go",
    # English
    "could you please", "can you please", "could you", "can you", "would you", "please",
    "i would like you to", "i'd like you to", "i would like to", "i'd like to", "i want you to",
    "i want to", "i want", "let's", "lets", "so", "now", "also", "and", "but", "maybe",
]
_FILLER_START.sort(key=len, reverse=True)
_PUNCT = " ,;:.!?-–—'\""


def short_title(text: str, max_width: int) -> str:
    """Cheap subject line for an ask when no LLM rephrasing is available:
    drop leading politeness/filler, keep the first sentence, cut at a word."""
    t = collapse_ws(text)
    t = re.split(r"(?<=[.!?])\s", t, maxsplit=1)[0]
    changed = True
    while changed and t:
        changed = False
        low = t.lower()
        for filler in _FILLER_START:
            if low.startswith(filler) and (len(low) == len(filler) or not low[len(filler)].isalnum()):
                t = t[len(filler):].lstrip(_PUNCT)
                changed = True
                break
    t = t.rstrip(_PUNCT + "?") or collapse_ws(text)
    t = t[:1].upper() + t[1:]
    if width(t) <= max_width:
        return t
    words, out = t.split(" "), ""
    for word in words:
        candidate = (out + " " + word).strip()
        if width(candidate) > max_width - 1:
            break
        out = candidate
    return (out or truncate(t, max_width - 1, "")).rstrip(_PUNCT) + "…"


def last_question(text: str) -> str:
    """The last question an agent's message asks ('' when it asks none), without
    markdown: what a short "yes" or "go" from the user answers."""
    for line in reversed((text or "").splitlines()):
        line = collapse_ws(line.replace("**", "").replace("__", "").replace("`", "").lstrip("#>*-• "))
        if "?" not in line:
            continue
        sentences = re.findall(r"[^.!?]*\?", line)
        if sentences:
            return sentences[-1].strip()
    return ""
