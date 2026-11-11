import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

# Never read or write the real ~/.claude*, ~/.codex or ~/.gemini from a test.
os.environ["HOME"] = tempfile.mkdtemp(prefix="eartag-home-")
os.environ.pop("CLAUDE_CONFIG_DIR", None)

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from herdr_eartag import claude, gitinfo, render, statusline, textutil  # noqa: E402
from herdr_eartag.i18n import Tr  # noqa: E402
from herdr_eartag.theme import Theme, read_herdr_theme  # noqa: E402


def _rec(kind, ts="2026-11-02T10:00:00Z", **kw):
    obj = {"type": kind, "timestamp": ts}
    obj.update(kw)
    return json.dumps(obj)


def _user(text, ts="2026-11-02T10:00:00Z", **kw):
    return _rec("user", ts, message={"role": "user", "content": text}, **kw)


def _assistant(tools=(), model="claude-opus-5-5", ts="2026-11-02T10:01:00Z", tokens=1000, effort="medium"):
    content = [{"type": "tool_use", "name": n, "input": i} for n, i in tools] or [{"type": "text", "text": "ok"}]
    return _rec("assistant", ts, effort=effort, message={
        "model": model, "content": content,
        "usage": {"input_tokens": 10, "cache_read_input_tokens": tokens, "cache_creation_input_tokens": 0},
    })


class TextTests(unittest.TestCase):
    def test_wrap_and_ellipsis(self):
        lines = textutil.wrap("Revoir les couleurs de la barre d'encaissement, page Finance et le reste", 32, 2)
        self.assertEqual(len(lines), 2)
        self.assertTrue(lines[-1].endswith("…"))
        self.assertTrue(all(textutil.width(l) <= 32 for l in lines))

    def test_wrap_long_word(self):
        lines = textutil.wrap("a" * 50, 10, 3)
        self.assertTrue(all(textutil.width(l) <= 10 for l in lines))

    def test_wide_chars(self):
        self.assertEqual(textutil.width("日本"), 4)
        self.assertEqual(textutil.width(textutil.truncate("日本語テキスト", 5)), 5)


class PromptTests(unittest.TestCase):
    def test_filters_system_turns(self):
        self.assertIsNone(claude.clean_prompt("<system-reminder>x</system-reminder>"))
        self.assertIsNone(claude.clean_prompt("This session is being continued from a previous conversation"))
        self.assertIsNone(claude.clean_prompt("Another Claude session sent this"))
        self.assertIsNone(claude.clean_prompt("[Request interrupted by user]"))
        self.assertEqual(claude.clean_prompt("Fix the bug [Image #1]"), "Fix the bug")

    def test_slash_command(self):
        text = "<command-name>/review</command-name><command-args>42</command-args>"
        self.assertEqual(claude.clean_prompt(text), "/review 42")
        for builtin in ("/effort", "/clear", "/model"):
            text = "<command-name>{}</command-name><command-args>high</command-args>".format(builtin)
            self.assertIsNone(claude.clean_prompt(text))

    def test_tool_result_is_not_a_prompt(self):
        obj = json.loads(_rec("user", message={"content": [{"type": "tool_result", "content": "x"}]}))
        self.assertIsNone(claude.prompt_from_record(obj))
        obj = json.loads(_user("hello", isMeta=True))
        self.assertIsNone(claude.prompt_from_record(obj))

    def test_tool_label_hides_details(self):
        self.assertEqual(claude.tool_label("Edit", {"file_path": "/secret/dir/a.py"}), "Edit a.py")
        self.assertEqual(claude.tool_label("Bash", {"command": "TOKEN=abc curl x"}), "Bash curl")
        self.assertEqual(claude.tool_label("Bash", {"command": "/usr/bin/git status"}), "Bash git")
        self.assertEqual(claude.tool_label("mcp__srv__do_thing", {}), "do_thing")
        self.assertEqual(claude.tool_label("Bash", {"command": "cd /x/y && npm test"}), "Bash npm")
        self.assertEqual(claude.tool_label("Bash", {"command": "FOO=1 BAR=2 python3 -m x"}), "Bash python3")

    def test_model_name(self):
        self.assertEqual(claude.model_name("claude-opus-5-5"), ("Opus", "5.5"))
        self.assertEqual(claude.model_name("claude-sonnet-4-5-20250929"), ("Sonnet", "4.5"))
        self.assertEqual(claude.model_name("claude-opus-4-8[1m]"), ("Opus", "4.8"))
        self.assertEqual(claude.model_name("claude-3-5-sonnet-20241022"), ("Sonnet", "3.5"))
        self.assertEqual(claude.model_name("claude-fable-5-1"), ("Fable", "5.1"))


class TranscriptTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "s.jsonl"

    def tearDown(self):
        self.tmp.cleanup()

    def write(self, lines, mode="w", newline=True):
        with open(self.path, mode, encoding="utf-8") as fh:
            fh.write("\n".join(lines) + ("\n" if newline else ""))

    def test_parse_and_incremental(self):
        self.write([
            _user("first ask", "2026-11-02T09:00:00Z"),
            _assistant([("Edit", {"file_path": "/x/a.py"})]),
            _rec("permission-mode", permissionMode="auto"),
            _user("second ask", "2026-11-02T10:00:00Z"),
            _assistant([("TodoWrite", {"todos": [{"status": "completed"}, {"status": "pending"}]})], tokens=5000),
        ])
        t = claude.Transcript(self.path)
        self.assertTrue(t.refresh())
        d = t.data
        self.assertEqual([a.text for a in d.asks], ["first ask", "second ask"])
        self.assertEqual(d.permission_mode, "auto")
        self.assertEqual(d.todos, (1, 2, ""))
        self.assertEqual(d.context_tokens, 5010)
        self.assertEqual(list(d.files), ["a.py"])
        self.assertFalse(t.refresh())
        # Partial line: not consumed until complete.
        partial = _user("third ask", "2026-11-02T11:00:00Z")
        self.write([partial[:20]], mode="a", newline=False)
        t.refresh()
        self.assertEqual(len(d.asks), 2)
        with open(self.path, "a", encoding="utf-8") as fh:
            fh.write(partial[20:] + "\n")
        t.refresh()
        self.assertEqual(d.asks[-1].text, "third ask")

    def test_backfill_from_large_file(self):
        filler = _assistant(tokens=1)
        lines = [_user("old ask {}".format(i)) for i in range(3)]
        lines += [filler] * 2000
        lines += [_user("recent ask")]
        self.write(lines)
        with mock.patch.object(claude, "INITIAL_TAIL", 4096), mock.patch.object(claude, "BACKFILL_CHUNK", 64 * 1024):
            t = claude.Transcript(self.path)
            t.refresh()
        texts = [a.text for a in t.data.asks]
        self.assertEqual(texts[-1], "recent ask")
        self.assertIn("old ask 2", texts)

    def test_find_transcript_inferred(self):
        with mock.patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": self.tmp.name}):
            folder = Path(self.tmp.name) / "projects" / claude.encode_project_dir("/a/b.c")
            folder.mkdir(parents=True)
            (folder / "x.jsonl").write_text("")
            path, inferred = claude.find_transcript(None, "/a/b.c")
            self.assertEqual(path.name, "x.jsonl")
            self.assertTrue(inferred)
            path, inferred = claude.find_transcript("x", "/a/b.c")
            self.assertFalse(inferred)
            self.assertEqual(claude.encode_project_dir("/a/b.c"), "-a-b-c")


class StatuslineTests(unittest.TestCase):
    def test_whitelist(self):
        payload = {
            "session_id": "abc-123", "cwd": "/secret", "transcript_path": "/secret/t.jsonl",
            "cost": {"total_cost_usd": 3}, "model": {"id": "claude-opus-5-5", "display_name": "Opus"},
            "effort": {"level": "high"},
            "context_window": {"used_percentage": 29, "context_window_size": 1000000},
            "rate_limits": {"five_hour": {"used_percentage": 28, "resets_at": 1793000000}},
        }
        snap = statusline.extract(payload)
        dumped = json.dumps(snap)
        self.assertNotIn("secret", dumped)
        self.assertNotIn("cost", dumped)
        self.assertEqual(snap["effort"], "high")
        self.assertEqual(snap["rate_limits"]["five_hour"]["used_percentage"], 28)

    def test_record_and_read(self):
        with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(os.environ, {"XDG_STATE_HOME": tmp}):
            statusline.record({"session_id": "s1", "model": {"id": "m"},
                               "context_window": {"context_window_size": 1000000},
                               "rate_limits": {"seven_day": {"used_percentage": 5}}})
            self.assertEqual(statusline.read_session("s1")["model"]["id"], "m")
            self.assertEqual(statusline.read_rate_limits()["rate_limits"]["seven_day"]["used_percentage"], 5)
            self.assertEqual(statusline.read_windows()["m"], 1000000)
            self.assertIsNone(statusline.read_session("../etc/passwd"))
            mode = os.stat(Path(tmp) / "herdr-eartag" / "statusline" / "rate_limits.json").st_mode & 0o777
            self.assertEqual(mode, 0o600)


class GitTests(unittest.TestCase):
    def test_branch_and_detached(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / ".git").mkdir()
            (root / ".git" / "HEAD").write_text("ref: refs/heads/feature/x\n")
            (root / "sub").mkdir()
            info = gitinfo.find(str(root / "sub"))
            self.assertEqual(info.branch, "feature/x")
            self.assertEqual(info.root, root)
            (root / ".git" / "HEAD").write_text("0123456789abcdef\n")
            self.assertTrue(gitinfo.find(str(root)).detached)

    def test_worktree_common_dir(self):
        with tempfile.TemporaryDirectory() as tmp:
            main = Path(tmp) / "main"
            wt_git = main / ".git" / "worktrees" / "wt"
            wt_git.mkdir(parents=True)
            (main / ".git" / "HEAD").write_text("ref: refs/heads/main\n")
            (wt_git / "HEAD").write_text("ref: refs/heads/topic\n")
            (wt_git / "commondir").write_text("../..\n")
            wt = Path(tmp) / "wt"
            wt.mkdir()
            (wt / ".git").write_text("gitdir: {}\n".format(wt_git))
            a, b = gitinfo.find(str(main)), gitinfo.find(str(wt))
            self.assertEqual(a.common_dir, b.common_dir)
            self.assertEqual(b.branch, "topic")


class RenderTests(unittest.TestCase):
    def _view(self, **kw):
        v = render.View(width=40, height=36, project="shopfront", mode_tag="auto", status="working",
                        status_label="EN COURS",
                        main_kind="now", main_title="MAINTENANT", main_right="depuis 4 min",
                        ask="Revoir les couleurs de la barre d'encaissement, page Finance",
                        activity="Edit barre-finance.html", tasks=(3, 5),
                        before_title="AVANT", before=[("22:00", "Afficher 1,6k dès 1 000 €")] * 3,
                        files_title="FICHIERS", files=["barre-finance.html", "profil2.py"],
                        alerts=["2 autres agents sur main"],
                        bars=[render.Bar("ctx", "CTX", 29), render.Bar("h5", "5H", 92, "reset", "lun. 2 nov. 03:20", "· dans 4 h"),
                              render.Bar("d7", "7J", None)],
                        host="Mac", branch="main", model="Opus", version="5.5", effort="medium")
        for k, val in kw.items():
            setattr(v, k, val)
        return v

    def assertFrameShape(self, frame, w, h):
        self.assertEqual(len(frame.lines), h)
        for line in frame.lines:
            self.assertEqual(sum(textutil.width(t) for t, _ in line), w, line)

    def test_panel_shape(self):
        for h in (12, 20, 36, 60):
            self.assertFrameShape(render.render(self._view(height=h)), 40, h)

    def test_panel_with_detail_and_menu(self):
        v = self._view(selected_bar="h5", menu=[("collapse", "◂", "accent", "Replier en bande"), ("-", "", "", ""),
                                                ("close", "", "sub", "Échap  fermer")])
        frame = render.render(v)
        self.assertFrameShape(frame, 40, 36)
        self.assertTrue(any(h.action == "menu:collapse" for h in frame.hits))
        detail = render.render(self._view(selected_bar="h5"))
        text = "\n".join("".join(t for t, _ in l) for l in detail.lines)
        self.assertIn("lun. 2 nov. 03:20", text)

    def test_bar_hits(self):
        frame = render.render(self._view())
        bars = [h for h in frame.hits if h.action.startswith("bar:")]
        self.assertEqual([h.action for h in bars], ["bar:ctx", "bar:h5", "bar:d7"])
        self.assertTrue(all(h.y >= 0 for h in bars))

    def test_detail_does_not_shift_layout(self):
        plain = render.render(self._view())
        tipped = render.render(self._view(selected_bar="h5"))
        rows = lambda f: ["".join(t for t, _ in l) for l in f.lines]
        bars_row = [i for i, l in enumerate(rows(plain)) if "CTX" in l]
        self.assertEqual(bars_row, [i for i, l in enumerate(rows(tipped)) if "CTX" in l])
        self.assertIn("lun. 2 nov.", rows(tipped)[bars_row[0] + 1])  # reserved line under the gauges
        self.assertIn("Mac · main", rows(tipped)[bars_row[0] - 1])  # machine line right above them

    def test_bottom_buttons(self):
        frame = render.render(self._view())
        actions = {h.action: h for h in frame.hits}
        last = len(frame.lines) - 1
        self.assertEqual(actions["menu"].y, last)
        self.assertNotIn("fold", actions)
        bottom = "".join(t for t, _ in frame.lines[last])
        self.assertIn("⋯", bottom)
        menu = render.render(self._view(menu=[("collapse", "→", "accent", "Replier"), ("close", "", "sub", "Échap")]))
        item_rows = [h.y for h in menu.hits if h.action.startswith("menu:") and h.y >= 0]
        self.assertTrue(item_rows and max(item_rows) < last)  # opens above the buttons


class ThemeTests(unittest.TestCase):
    def test_reads_herdr_theme_and_custom(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.toml"
            path.write_text('[ui]\nx = 1\n[theme]\nname = "Rose Pine"  # comment\n[theme.custom]\naccent = "#ff0000"\n')
            name, custom = read_herdr_theme(path)
            self.assertEqual(name, "rose-pine")
            self.assertEqual(custom, {"accent": "#ff0000"})

    def test_contrast(self):
        t = Theme({"yellow": "#f6c177", "green": "#31748f", "panel_bg": "#191724", "text": "#e0def4"})
        self.assertEqual(t.on("working"), "38;2;25;23;36")
        self.assertEqual(t.on("idle"), "38;2;224;222;244")
        self.assertEqual(Theme({"yellow": "yellow"}).fg("working"), "33")


class EffortTests(unittest.TestCase):
    def test_scales_per_family(self):
        from herdr_eartag import efforts
        self.assertEqual(efforts.dots("claude-opus-5-5", "medium"), (2, 5))
        self.assertEqual(efforts.dots("gpt-6.1-sol", "low", "codex"), (2, 5))
        self.assertEqual(efforts.dots("gemini-3-pro", "low"), (1, 2))
        self.assertEqual(efforts.dots("grok-4", "high"), (2, 2))
        self.assertIsNone(efforts.dots("claude-haiku-5-5", ""))
        self.assertIsNone(efforts.dots("gpt-6.1", "none"))
        self.assertEqual(efforts.dots("mistral-large", "high"), (4, 6))  # generic scale

    def test_display_names(self):
        from herdr_eartag.efforts import display_name
        self.assertEqual(display_name("gpt-6.1-sol"), ("GPT", "6.1"))
        self.assertEqual(display_name("gemini-2.5-pro"), ("Gemini", "2.5"))
        self.assertEqual(display_name("deepseek-v3"), ("DeepSeek", "3"))
        self.assertEqual(display_name("claude-opus-5-5[1m]"), ("Opus", "5.5"))
        self.assertEqual(display_name("o3"), ("o3", ""))

    def test_codex_session(self):
        from herdr_eartag import codex
        with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(os.environ, {"CODEX_HOME": tmp}):
            day = Path(tmp) / "sessions" / "2026" / "11" / "02"
            day.mkdir(parents=True)
            f = day / "rollout-2026-11-02T10-00-00-abc-123.jsonl"
            f.write_text("\n".join([
                json.dumps({"type": "session_meta", "payload": {"cwd": "/p"}}),
                json.dumps({"type": "turn_context", "payload": {"model": "gpt-6.1-sol", "effort": "low"}}),
                json.dumps({"type": "turn_context", "payload": {"model": "gpt-6.1-sol", "effort": "high"}}),
            ]) + "\n")
            self.assertEqual(codex.find_session("abc-123", None), f)
            self.assertEqual(codex.find_session(None, "/p"), f)
            self.assertEqual(codex.model_and_effort(f), ("gpt-6.1-sol", "high"))


class ThemeToneTests(unittest.TestCase):
    def test_rose_pine_matches_mockups(self):
        from herdr_eartag.palettes import PALETTES
        t = Theme(PALETTES["rose-pine"])
        self.assertEqual(t.palette["panel"], "#1f1d2e")
        self.assertEqual(t.palette["mid"], "#3f3c55")   # mockup rails #403d52
        self.assertEqual(t.palette["line"], "#514e68")  # mockup separators #524f67
        self.assertEqual(Theme(PALETTES["rose-pine"], paint_background=False).bg("panel"), "49")
        self.assertEqual(Theme(PALETTES["terminal"]).bg("panel"), "49")


class I18nTests(unittest.TestCase):
    def test_generic_detection(self):
        from herdr_eartag import i18n
        for env, expected in (("fr_FR.UTF-8", "fr"), ("en_GB.UTF-8", "en"), ("de_DE.UTF-8", "en"), ("pt-BR", "en")):
            with mock.patch.dict(os.environ, {"LC_ALL": env}):
                self.assertEqual(i18n.detect_language(), expected)
        with mock.patch.dict(i18n.STRINGS, {"de": {"now": "JETZT"}}), mock.patch.dict(os.environ, {"LC_ALL": "de_DE.UTF-8"}):
            self.assertEqual(i18n.detect_language(), "de")
            self.assertEqual(Tr("de")("now"), "JETZT")
            self.assertEqual(Tr("de")("before"), "BEFORE")  # missing keys fall back to English

    def test_key_placeholder(self):
        tr = Tr("fr")
        self.assertIn("prefix+i", tr("toast.reopen", key="prefix+i"))

    def test_durations(self):
        tr = Tr("fr")
        self.assertEqual(tr.duration(4 * 60), "4 min")
        self.assertEqual(tr.duration(4 * 3600 + 5 * 60), "4 h 05")
        self.assertEqual(Tr("en").duration(3 * 86400), "3d")


if __name__ == "__main__":
    unittest.main()


class SummarizeTests(unittest.TestCase):
    def test_resolve_backend(self):
        from herdr_eartag import summarize
        found = {"codex": "/bin/codex", "gemini": "/bin/gemini"}
        with mock.patch.object(summarize.shutil, "which", side_effect=lambda n: found.get(n)):
            self.assertIsNone(summarize.resolve_backend("off"))
            self.assertEqual(summarize.resolve_backend("auto"), ("codex", "/bin/codex"))
            self.assertEqual(summarize.resolve_backend("gemini"), ("gemini", "/bin/gemini"))
            self.assertIsNone(summarize.resolve_backend("claude"))

    def test_commands_are_read_only_and_unpersisted(self):
        from herdr_eartag.summarize import build_command
        argv, stdin = build_command("claude", "claude", "haiku", "P", "/tmp/o")
        self.assertIn("--no-session-persistence", argv)
        self.assertEqual(argv[argv.index("--tools") + 1], "")  # no tool at all
        self.assertIn("--strict-mcp-config", argv)
        self.assertEqual(stdin, "P")
        argv, stdin = build_command("codex", "codex", "", "P", "/tmp/o")
        self.assertIn("--ephemeral", argv)
        self.assertEqual(argv[argv.index("--sandbox") + 1], "read-only")
        self.assertEqual(argv[-1], "-")
        self.assertTrue(stdin.endswith("P"))
        argv, stdin = build_command("gemini", "gemini", "", "P", "/tmp/o")
        self.assertEqual(argv[argv.index("--approval-mode") + 1], "plan")
        self.assertIsNone(stdin)

    def test_config_values(self):
        from herdr_eartag.config import load_config
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.ini"
            path.write_text("[eartag]\nsummarize = Codex\n")
            self.assertEqual(load_config(path).summarize, "codex")
            path.write_text("[eartag]\nsummarize = yes please\n")
            self.assertEqual(load_config(path).summarize, "off")


class HerdrCallTests(unittest.TestCase):
    def test_errors_on_stderr_are_parsed(self):
        from herdr_eartag import herdr
        proc = mock.Mock(returncode=1, stdout="",
                         stderr='{"error":{"code":"pane_not_found","message":"pane w1:p9 not found"},"id":"cli:pane:get"}\n')
        with mock.patch.object(herdr.subprocess, "run", return_value=proc):
            self.assertIsNone(herdr.pane_get("w1:p9"))
            with self.assertRaises(herdr.HerdrError) as ctx:
                herdr.call("pane", "close", "w1:p9")
            self.assertEqual(ctx.exception.code, "pane_not_found")


class RichTextTests(unittest.TestCase):
    def test_braces_become_highlights(self):
        lines = render.rich_wrap("Fix {login} on {mobile Safari} now", 40, 2, "text", "done b")
        self.assertEqual(lines, [[("Fix ", "text"), ("login", "done b"), (" on ", "text"),
                                 ("mobile", "done b"), (" ", "text"), ("Safari", "done b"), (" now", "text")]])

    def test_wrap_limits_and_ellipsis(self):
        lines = render.rich_wrap("one two three four five six seven eight", 10, 2, "text", "hi")
        self.assertEqual(len(lines), 2)
        self.assertTrue(all(sum(textutil.width(t) for t, _ in l) <= 10 for l in lines))
        self.assertTrue(lines[-1][-1][0].endswith("…"))
        self.assertEqual(render.rich_wrap("x" * 25, 10, 3, "text", "hi")[0], [("x" * 10, "text")])

    def test_verdict_header(self):
        v = render.View(width=40, height=40, project="p", status="done", status_label="TERMINÉ",
                        main_kind="last", main_title="DERNIÈRE DEMANDE", ask="Fix {login}",
                        reply="Login {fixed}\n- {auth.py} patched\n? Deploy now?",
                        reply_verdict="DONE", reply_verdict_label="FAIT")
        rows = ["".join(t for t, _ in l).strip() for l in render.render(v).lines]
        self.assertIn("✓ FAIT", rows)
        self.assertIn("Login fixed", rows)
        self.assertIn("• auth.py patched", rows)
        self.assertIn("? Deploy now?", rows)


class ColorTests(unittest.TestCase):
    def test_effort_colors(self):
        self.assertEqual(render.effort_role((2, 5)), "ok")     # medium
        self.assertEqual(render.effort_role((3, 5)), "warn")   # high
        self.assertEqual(render.effort_role((5, 5)), "crit")   # max
        self.assertEqual(render.effort_role((2, 2)), "warn")   # Gemini high

    def test_ascii_mode(self):
        v = render.View(width=40, height=36, ascii=True, project="p", status="done", status_label="DONE",
                        main_kind="last", main_title="LAST ASK", ask="Fix login", reply="Fixed",
                        reply_verdict="DONE", reply_verdict_label="DONE")
        text = "".join(t for l in render.render(v).lines for t, _ in l)
        self.assertTrue(all(ord(c) < 128 for c in text), text)


    def test_inferred_session_matches_the_terminal_title(self):
        home = Path(os.environ["HOME"])
        cwd = "/w/proj"
        made = []
        for i, (folder, title) in enumerate(((".claude", "Older one"), (".claude-work", "Login rework"),
                                             (".claude-work", ""))):
            d = home / folder / "projects" / claude.encode_project_dir(cwd)
            d.mkdir(parents=True, exist_ok=True)
            path = d / "s{}.jsonl".format(i)
            path.write_text((_rec("ai-title", aiTitle=title) + "\n") if title else _user("hi") + "\n")
            os.utime(path, (1000 + i, 1000 + i))
            made.append(path)
        try:
            self.assertEqual(claude.find_transcript(None, cwd, "Login rework")[0], made[1])
            self.assertEqual(claude.find_transcript(None, cwd, "Older one")[0], made[0])
            self.assertEqual(claude.find_transcript(None, cwd, "Claude Code")[0], made[2])
            self.assertEqual(claude.find_transcript(None, cwd, "")[0], made[2])
            self.assertEqual(claude.config_dir_of(made[1]).name, ".claude-work")
            self.assertEqual(claude.find_transcript("s1", None)[0], made[1])
        finally:
            import shutil
            shutil.rmtree(home / ".claude", ignore_errors=True)
            shutil.rmtree(home / ".claude-work", ignore_errors=True)


class OtherAgentTests(unittest.TestCase):
    def _write(self, lines):
        tmp = tempfile.mkdtemp()
        path = Path(tmp) / "t.jsonl"
        path.write_text("\n".join(json.dumps(l) for l in lines) + "\n")
        return path

    def test_antigravity_transcript(self):
        from herdr_eartag import antigravity
        path = self._write([
            {"type": "USER_INPUT", "source": "USER_EXPLICIT", "created_at": "2026-11-06T19:32:19Z",
             "content": "<USER_REQUEST>\nMake the calendar a tool\n</USER_REQUEST>\n<ADDITIONAL_METADATA>x"},
            {"type": "PLANNER_RESPONSE", "source": "MODEL", "created_at": "2026-11-06T19:33:00Z",
             "input_tokens": 100, "cache_read_tokens": 900,
             "tool_calls": [{"name": "edit_file", "args": {"Path": json.dumps("/p/secret/maquettes.py")}},
                            {"name": "read_file", "args": {"Path": json.dumps("/p/other.py")}},
                            {"name": "run_command", "args": {"CommandLine": json.dumps("git status --short")}}]},
            {"type": "PLANNER_RESPONSE", "source": "MODEL", "created_at": "2026-11-06T19:34:00Z",
             "content": "Done: the calendar is a tool."},
        ])
        t = antigravity.AgyTranscript(path)
        t.refresh()
        d = t.data
        self.assertEqual([a.text for a in d.asks], ["Make the calendar a tool"])
        self.assertEqual(list(d.files), ["maquettes.py"])
        self.assertEqual(d.last_tool, "run_command git")
        self.assertEqual(d.context_tokens, 1000)
        self.assertEqual(d.last_reply, "Done: the calendar is a tool.")
        self.assertEqual(antigravity.effort_of("Gemini 3.8 Flash (Low)"), "low")

    def test_codex_rollout(self):
        from herdr_eartag import codex
        ask = {"type": "UserMessage", "content": [{"type": "text", "text": "Fix the bar"}]}
        path = self._write([
            {"timestamp": "2026-10-25T18:04:10Z", "type": "turn_context",
             "payload": {"model": "gpt-6.1-sol", "effort": "low"}},
            {"timestamp": "2026-10-25T18:04:10Z", "type": "event_msg",
             "payload": {"type": "item_completed", "item": ask}},
            {"timestamp": "2026-10-25T18:04:11Z", "type": "response_item",
             "payload": {"type": "custom_tool_call", "name": "apply_patch",
                         "input": "*** Begin Patch\n*** Update File: /p/src/bar.rs\n@@\n*** End Patch"}},
            {"timestamp": "2026-10-25T18:04:12Z", "type": "event_msg",
             "payload": {"type": "token_count",
                         "info": {"last_token_usage": {"input_tokens": 47500}, "model_context_window": 475000},
                         "rate_limits": {"primary": {"used_percent": 31.0, "window_minutes": 300, "resets_at": 1},
                                         "secondary": {"used_percent": 18.0, "window_minutes": 10080}}}},
            {"timestamp": "2026-10-25T18:04:13Z", "type": "event_msg",
             "payload": {"type": "item_completed",
                         "item": {"type": "AgentMessage", "content": [{"type": "Text", "text": "Fixed."}]}}},
        ])
        t = codex.CodexTranscript(path)
        t.refresh()
        d = t.data
        self.assertEqual([a.text for a in d.asks], ["Fix the bar"])
        self.assertEqual((d.model, d.effort), ("gpt-6.1-sol", "low"))
        self.assertEqual(list(d.files), ["bar.rs"])
        self.assertEqual((d.context_tokens, d.context_window), (47500, 475000))
        self.assertEqual(d.rate_limits["rate_limits"]["five_hour"]["used_percentage"], 31.0)
        self.assertEqual(d.rate_limits["rate_limits"]["seven_day"]["used_percentage"], 18.0)
        self.assertEqual(d.last_reply, "Fixed.")


class PanelModelTests(unittest.TestCase):
    def test_view_from_a_real_transcript(self):
        from herdr_eartag.config import Config
        from herdr_eartag.panel import PanelModel
        with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(os.environ, {
                "CLAUDE_CONFIG_DIR": tmp, "XDG_STATE_HOME": tmp, "HERDR_PLUGIN_STATE_DIR": tmp}):
            cwd = str(Path(tmp) / "proj")
            folder = Path(tmp) / "projects" / claude.encode_project_dir(cwd)
            folder.mkdir(parents=True)
            (folder / "s1.jsonl").write_text("\n".join([
                _rec("ai-title", aiTitle="Login rework"),
                _user("Fix the login button", "2026-11-02T10:00:00Z"),
                _rec("assistant", "2026-11-02T10:01:00Z", message={
                    "model": "claude-opus-5-5", "content": [{"type": "text", "text": "Login fixed."}],
                    "usage": {"input_tokens": 100}}),
            ]) + "\n")
            pane = {"pane_id": "w1:p1", "agent": "claude", "agent_status": "done", "cwd": cwd,
                    "workspace_id": "w1", "agent_session": {"value": "s1"}}
            with mock.patch("herdr_eartag.herdr.pane_get", return_value=pane), \
                    mock.patch("herdr_eartag.herdr.agent_list", return_value=[]):
                m = PanelModel("w1:p1", Config(language="en"), Tr("en"))
                m.poll()
                v = m.view(40, 40, None)
            self.assertEqual(v.ask, "Fix the login button")
            self.assertEqual(v.session_title, "Login rework")
            self.assertEqual(v.reply, "Login fixed.")
            self.assertEqual((v.model, v.version), ("Opus", "5.5"))
            frame = render.render(v)
            self.assertEqual(len(frame.lines), 40)


class ImprovementTests(unittest.TestCase):
    def _rows(self, v):
        return ["".join(t for t, _ in l).strip() for l in render.render(v).lines]

    def test_history_icons_follow_real_outcome(self):
        v = render.View(width=40, height=36, project="p", status="idle", status_label="AU REPOS",
                        main_kind="last", main_title="DERNIÈRE DEMANDE", ask="x", before_title="AVANT",
                        before=[("10:00", "Fix login", "DONE"), ("09:00", "Deploy", "FAILED"), ("08:00", "Old", "")])
        rows = self._rows(v)
        self.assertIn("✓ 10:00 Fix login", rows)
        self.assertIn("✗ 09:00 Deploy", rows)
        self.assertIn("▸ 08:00 Old", rows)  # unknown outcome: neutral, never a fake ✓

    def test_at_most_two_highlights_per_line(self):
        line = render.rich_wrap("{a} {b} {c} {d}", 40, 1, "text", "hi")[0]
        self.assertEqual([t for t, st in line if st == "hi"], ["a", "b"])

    def test_diff_stat_next_to_files(self):
        v = render.View(width=40, height=36, project="p", files_title="FICHIERS", files=["a.py", "b.py"], diff=(120, 30))
        self.assertTrue(any(r.startswith("◆ FICHIERS") and r.endswith("+120 −30  2") for r in self._rows(v)))

    def test_diff_stat_parsing(self):
        with tempfile.TemporaryDirectory() as tmp:
            subprocess = __import__("subprocess")
            run = lambda *a: subprocess.run(["git", "-C", tmp, *a], capture_output=True, check=True)
            run("init", "-q"); run("config", "user.email", "t@t"); run("config", "user.name", "t")
            Path(tmp, "f.txt").write_text("a\nb\n"); run("add", "."); run("commit", "-qm", "x")
            Path(tmp, "f.txt").write_text("a\nc\nd\n")
            self.assertEqual(gitinfo.diff_stat(Path(tmp)), (2, 1))

    def test_ghost_panel_is_closed(self):
        from herdr_eartag import cli, prefs
        with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(os.environ, {"HERDR_PLUGIN_STATE_DIR": tmp}):
            with prefs.locked() as d:
                d["panels"]["w1:p1"] = "w1:p2"
                d["pids"]["w1:p2"] = 999999999  # no such process: a shell restored by herdr
            root = str(Path(cli.__file__).resolve().parents[1])
            with mock.patch("herdr_eartag.herdr.pane_get", return_value={"pane_id": "w1:p2", "cwd": root}), \
                    mock.patch("herdr_eartag.herdr.pane_close") as close:
                self.assertIsNone(cli.live_panel("w1:p1"))
                close.assert_called_once_with("w1:p2")
            self.assertEqual(prefs.load()["panels"], {})
            # Same situation, but the pane is a user's shell elsewhere: forgotten, not closed.
            with prefs.locked() as d:
                d["panels"]["w1:p1"] = "w1:p2"
                d["pids"]["w1:p2"] = 999999999
            with mock.patch("herdr_eartag.herdr.pane_get", return_value={"pane_id": "w1:p2", "cwd": "/home/u/work"}), \
                    mock.patch("herdr_eartag.herdr.pane_close") as close:
                self.assertIsNone(cli.live_panel("w1:p1"))
                close.assert_not_called()

    def test_reply_rephrased_at_most_once_a_minute_while_working(self):
        from herdr_eartag.config import Config
        from herdr_eartag.panel import PanelModel
        with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(os.environ, {"HERDR_PLUGIN_STATE_DIR": tmp}):
            m = PanelModel("w1:p1", Config(), Tr("en"))
            m.summarizer = mock.Mock()
            m.summarizer.get.return_value = None
            m._reply("first", 36, working=True)
            m._reply("second", 36, working=True)
            asked = [c.args[1] for c in m.summarizer.get.call_args_list]
            self.assertEqual(asked, ["first", "first"])
            m._reply("final", 36, working=False)
            self.assertEqual(m.summarizer.get.call_args_list[-1].args[1], "final")


class ContextRecapTests(unittest.TestCase):
    """The current ask is read in context: overall goal + what this message does."""

    def test_normalize_and_split(self):
        from herdr_eartag import summarize
        raw = ("**GOAL:** Quota gauges rework\nSTEP: APPROVE: Validate {stacked bars}\n"
               "- keep {5h} first\n- b\n- c\n- d")
        out = summarize.normalize("context", raw, 36)
        goal, tag, step, points = summarize.split_context(out)
        self.assertEqual((goal, tag, step), ("Quota gauges rework", "APPROVE", "Validate {stacked bars}"))
        self.assertEqual(points, ["keep {5h} first", "b", "c"])
        # A model that forgets the prefixes still gives a usable step.
        goal, tag, step, points = summarize.split_context(summarize.normalize("context", "Fix {login}", 36))
        self.assertEqual((goal, tag, step, points), ("", "", "Fix {login}", []))
        # Unknown tags are dropped, the text is kept.
        self.assertEqual(summarize.split_context("GOAL: g\nSTEP: MAYBE: do it")[1:3], ("", "MAYBE: do it"))
        self.assertEqual(summarize.split_context("GOAL: g\nSTEP: fix: do it")[1:3], ("FIX", "do it"))

    def test_context_input(self):
        from herdr_eartag import summarize
        body = summarize.context_input("Login rework", ["Fix the login", "x" * 900],
                                       "a" * 3000 + "Shall I also patch the tests?", "yes go")
        self.assertIn("Previous goal: Login rework", body)
        self.assertIn("- Fix the login", body)
        self.assertNotIn("x" * 400, body)                       # earlier asks are cut
        self.assertIn("Shall I also patch the tests?", body)    # the tail of the agent's message
        self.assertNotIn("a" * 2000, body)
        self.assertTrue(body.rstrip().endswith("yes go"))
        bare = summarize.context_input("", [], "", "Fix it")
        self.assertNotIn("Previous goal", bare)
        self.assertNotIn("Agent's message", bare)

    def test_cache_key_ignores_soft_context(self):
        from herdr_eartag.summarize import Summarizer
        with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(os.environ, {"HERDR_PLUGIN_STATE_DIR": tmp}):
            s = Summarizer("off")
            s.binary = "/bin/true"
            with mock.patch.object(s, "call", return_value="GOAL: g\nSTEP: NEW: do") as call:
                self.assertIsNone(s.get("context", "prompt one", 36, key_text="ask|reply"))
                self.assertTrue(s.changed.wait(5))
                self.assertEqual(s.get("context", "prompt two", 36, key_text="ask|reply"), "GOAL: g\nSTEP: NEW: do")
                self.assertEqual(call.call_count, 1)
                self.assertIn("prompt one", call.call_args.args[0])
            self.assertEqual(s.peek("context", "ask|reply", 36), "GOAL: g\nSTEP: NEW: do")
            self.assertIsNone(s.peek("context", "other", 36))

    def test_last_question(self):
        self.assertEqual(textutil.last_question("Done.\n\nShould I **also** update `tests`? Or stop."),
                         "Should I also update tests?")
        self.assertEqual(textutil.last_question("All good. Want me to push it?"), "Want me to push it?")
        self.assertEqual(textutil.last_question("Fixed the bug."), "")

    def _model(self, tmp, lines, summarizer=None):
        from herdr_eartag.config import Config
        from herdr_eartag.panel import PanelModel
        cwd = str(Path(tmp) / "proj")
        folder = Path(tmp) / "projects" / claude.encode_project_dir(cwd)
        folder.mkdir(parents=True)
        (folder / "s1.jsonl").write_text("\n".join(lines) + "\n")
        pane = {"pane_id": "w1:p1", "agent": "claude", "agent_status": "done", "cwd": cwd,
                "workspace_id": "w1", "agent_session": {"value": "s1"}}
        with mock.patch("herdr_eartag.herdr.pane_get", return_value=pane), \
                mock.patch("herdr_eartag.herdr.agent_list", return_value=[]):
            m = PanelModel("w1:p1", Config(language="en"), Tr("en"))
            if summarizer is not None:
                m.summarizer = summarizer
            m.poll()
            return m, m.view(40, 40, None)

    def _reply(self, text, ts):
        return _rec("assistant", ts, message={"model": "claude-opus-5-5", "content": [{"type": "text", "text": text}],
                                              "usage": {"input_tokens": 100}})

    def _transcript(self):
        return [
            _rec("ai-title", aiTitle="Old title"),
            _user("Rework the quota gauges", "2026-11-02T10:00:00Z"),
            self._reply("Plan: stacked bars, 5h first. Shall I go ahead?", "2026-11-02T10:01:00Z"),
            _user("yes go", "2026-11-02T10:02:00Z"),
        ]

    def test_panel_shows_goal_and_step(self):
        summarizer = mock.Mock()

        def get(kind, text, width, key_text=None):
            if kind == "context":
                return "GOAL: Quota gauges rework\nSTEP: APPROVE: Go with {stacked bars}\n- {5h} first"
            return None
        summarizer.get.side_effect = get
        summarizer.peek.return_value = None
        summarizer.pending.return_value = False
        with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(os.environ, {
                "CLAUDE_CONFIG_DIR": tmp, "XDG_STATE_HOME": tmp, "HERDR_PLUGIN_STATE_DIR": tmp}):
            m, v = self._model(tmp, self._transcript(), summarizer)
        self.assertEqual(v.session_title, "Quota gauges rework")   # replaces Claude's ai-title
        self.assertEqual(v.ask, "Go with {stacked bars}")
        self.assertEqual(v.ask_points, ["{5h} first"])
        self.assertEqual(v.ask_kind, "approval")
        call = [c for c in summarizer.get.call_args_list if c.args[0] == "context"][-1]
        prompt = call.args[1]
        self.assertIn("Shall I go ahead?", prompt)        # what the "yes" answers
        self.assertIn("- Rework the quota gauges", prompt)
        self.assertIn("Previous goal: Old title", prompt)  # seeded from the ai-title
        self.assertEqual(call.kwargs["key_text"].split("\x00")[0], "yes go")
        rows = self._rows(v)
        self.assertIn("◇ Quota gauges rework", rows)
        self.assertIn("Go with stacked bars", rows)

    def test_no_llm_short_answer_shows_the_question(self):
        with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(os.environ, {
                "CLAUDE_CONFIG_DIR": tmp, "XDG_STATE_HOME": tmp, "HERDR_PLUGIN_STATE_DIR": tmp}):
            m, v = self._model(tmp, self._transcript())
        self.assertEqual(v.ask, "yes go")
        self.assertEqual(v.ask_reply_to, "Shall I go ahead?")
        self.assertEqual(v.session_title, "Old title")
        self.assertIn("↳ answering: Shall I go ahead?", self._rows(v))

    def test_render_kind_on_title_line(self):
        v = render.View(width=40, height=30, project="p", status="done", status_label="DONE",
                        main_kind="last", main_title="LAST ASK", main_right="4m", ask="Go",
                        ask_kind="approval")
        self.assertTrue(any(r.startswith("▸ LAST ASK · approval") and r.endswith("4m") for r in self._rows(v)))

    def _rows(self, v):
        return ["".join(t for t, _ in l).strip() for l in render.render(v).lines]
