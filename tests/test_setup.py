import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

# Never read or write the real ~/.claude*, ~/.codex or ~/.gemini from a test.
os.environ["HOME"] = tempfile.mkdtemp(prefix="eartag-home-")
os.environ.pop("CLAUDE_CONFIG_DIR", None)

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from herdr_eartag import cli  # noqa: E402

# Stand-in for `herdr integration install|uninstall claude`: edits the SessionStart
# hook of $CLAUDE_CONFIG_DIR/settings.json the way herdr does, and logs the call.
FAKE_HERDR = r'''#!/usr/bin/env python3
import json, os, sys
if sys.argv[1:2] != ["integration"] or sys.argv[3:4] != ["claude"]:
    sys.exit(1)
folder = os.environ["CLAUDE_CONFIG_DIR"]
path = os.path.join(folder, "settings.json")
data = json.load(open(path)) if os.path.exists(path) else {}
hooks = data.setdefault("hooks", {})
if sys.argv[2] == "install":
    hooks["SessionStart"] = [{"hooks": [{"type": "command",
        "command": "bash '%s/hooks/herdr-agent-state.sh' session" % folder}]}]
else:
    hooks.pop("SessionStart", None)
json.dump(data, open(path, "w"))
with open(os.path.join(os.path.dirname(sys.argv[0]), "calls.log"), "a") as log:
    log.write("%s %s\n" % (sys.argv[2], folder))
'''


def fake_herdr(tmp: str) -> str:
    path = Path(tmp) / "bin" / "herdr"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(FAKE_HERDR)
    path.chmod(0o755)
    return str(path)


def herdr_calls(tmp: str) -> list:
    log = Path(tmp) / "bin" / "calls.log"
    return [line for line in log.read_text().splitlines() if line] if log.exists() else []


class SetupTests(unittest.TestCase):
    def test_install_chain_and_remove(self):
        with tempfile.TemporaryDirectory() as tmp:
            env = {
                "HOME": tmp,  # never touch the real ~/.claude*, ~/.gemini
                "CLAUDE_CONFIG_DIR": str(Path(tmp) / "claude"),
                "XDG_DATA_HOME": str(Path(tmp) / "share"),
                "XDG_STATE_HOME": str(Path(tmp) / "state"),
                "XDG_CONFIG_HOME": str(Path(tmp) / "config"),
                "HERDR_BIN_PATH": fake_herdr(tmp),
            }
            herdr_cfg = Path(env["XDG_CONFIG_HOME"]) / "herdr" / "config.toml"
            herdr_cfg.parent.mkdir(parents=True)
            herdr_original = '[theme]\nname = "nord"\n'
            herdr_cfg.write_text(herdr_original)
            settings = Path(env["CLAUDE_CONFIG_DIR"]) / "settings.json"
            settings.parent.mkdir()
            original = {"model": "opus", "statusLine": {"type": "command", "command": "echo 'mine'", "padding": 1}}
            settings.write_text(json.dumps(original))
            with mock.patch.dict(os.environ, env), redirect_stdout(io.StringIO()), \
                    mock.patch("herdr_eartag.setup._reload_herdr") as reload:
                self.assertEqual(cli.main(["setup"]), 0)
                data = json.loads(settings.read_text())
                self.assertEqual(data["model"], "opus")
                self.assertEqual(data["statusLine"]["padding"], 1)
                command = data["statusLine"]["command"]
                self.assertIn("statusline_tap.py", command)
                self.assertIn("--then", command)
                self.assertTrue(Path(str(settings) + ".herdr-eartag.bak").exists())
                self.assertIn('command = "thomashuangth.eartag.toggle"', herdr_cfg.read_text())
                self.assertTrue(reload.called)
                from herdr_eartag import setup
                self.assertTrue(setup.herdr_hook_installed(settings.parent))
                # Second run is idempotent.
                self.assertEqual(cli.main(["setup"]), 0)
                self.assertEqual(json.loads(settings.read_text())["statusLine"]["command"], command)
                self.assertEqual(herdr_cfg.read_text().count("eartag.toggle"), 1)

                # The installed relay records the snapshot and prints the previous statusline.
                payload = json.dumps({"session_id": "s1", "cwd": "/private",
                                      "rate_limits": {"five_hour": {"used_percentage": 12}}})
                out = subprocess.run(command, shell=True, input=payload.encode(), capture_output=True, env={**os.environ, **env})
                self.assertEqual(out.stdout.decode().strip(), "mine")
                snap = Path(env["XDG_STATE_HOME"]) / "herdr-eartag" / "statusline" / "sessions" / "s1.json"
                self.assertTrue(snap.exists())
                self.assertNotIn("/private", snap.read_text())

                self.assertEqual(cli.main(["setup", "--remove"]), 0)
                self.assertEqual(json.loads(settings.read_text()), original)
                self.assertEqual(herdr_cfg.read_text(), herdr_original)

    def test_every_claude_dir_and_antigravity(self):
        from herdr_eartag import setup, statusline
        with tempfile.TemporaryDirectory() as tmp:
            env = {
                "HOME": tmp,
                "XDG_DATA_HOME": str(Path(tmp) / "share"),
                "XDG_STATE_HOME": str(Path(tmp) / "state"),
                "XDG_CONFIG_HOME": str(Path(tmp) / "config"),
            }
            home = Path(tmp)
            (home / ".claude").mkdir()
            (home / ".claude-work" / "projects").mkdir(parents=True)
            (home / ".claude-empty").mkdir()  # no transcripts: not a config dir
            (home / ".claude.json").write_text("{}")
            agy = home / ".gemini" / "antigravity-cli"
            agy.mkdir(parents=True)
            agy_original = {"model": "Gemini 3.8 Flash (Low)",
                            "statusLine": {"type": "", "command": "/x/agy-status.py", "enabled": True}}
            (agy / "settings.json").write_text(json.dumps(agy_original))
            with mock.patch.dict(os.environ, env), redirect_stdout(io.StringIO()):
                os.environ.pop("CLAUDE_CONFIG_DIR", None)
                kinds = [(t.kind, t.settings.parent.name) for t in setup.targets()]
                self.assertEqual(kinds, [("claude", ".claude"), ("claude", ".claude-work"),
                                         ("agy", "antigravity-cli")])
                self.assertTrue(setup.needs_setup("agy"))
                setup.install_statusline()
                self.assertTrue(setup.statusline_installed())
                work = json.loads((home / ".claude-work" / "settings.json").read_text())
                self.assertIn("statusline_tap.py", work["statusLine"]["command"])
                agy_cfg = json.loads((agy / "settings.json").read_text())
                self.assertEqual(agy_cfg["model"], agy_original["model"])
                wrapper = Path(agy_cfg["statusLine"]["command"])
                self.assertEqual(wrapper, setup.agy_wrapper_path())
                self.assertIn("--then /x/agy-status.py", wrapper.read_text())

                # Quotas of each account land in their own file.
                tap = str(setup.tap_install_path())
                limits = {"five_hour": {"used_percentage": 40}}
                for config_dir in (home / ".claude", home / ".claude-work"):
                    subprocess.run([sys.executable, tap], env={**os.environ, "CLAUDE_CONFIG_DIR": str(config_dir)},
                                   input=json.dumps({"rate_limits": limits}).encode(), check=True)
                agy_payload = {"product": "antigravity", "session_id": "c1",
                               "model": {"id": "Gemini 3.8 Flash (Low)", "effort": "low"},
                               "quota": {"gemini-5h": {"remaining_fraction": 0.25,
                                                       "reset_time": "2026-11-07T01:53:19Z"},
                                         "3p-5h": {"remaining_fraction": 1}}}
                subprocess.run([sys.executable, tap], input=json.dumps(agy_payload).encode(), check=True)
                self.assertTrue(statusline.read_rate_limits(""))
                self.assertTrue(statusline.read_rate_limits(statusline.claude_scope(str(home / ".claude-work"))))
                agy_limits = statusline.read_rate_limits("agy")["rate_limits"]
                self.assertEqual(agy_limits["five_hour"]["used_percentage"], 75.0)
                self.assertEqual(statusline.read_session("c1")["effort"], "low")

                setup.remove_statusline()
                self.assertEqual(json.loads((agy / "settings.json").read_text()), agy_original)
                self.assertNotIn("statusLine", json.loads((home / ".claude-work" / "settings.json").read_text()))

    def test_key_conflict_is_left_alone(self):
        from herdr_eartag import setup
        with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(os.environ, {"XDG_CONFIG_HOME": tmp}):
            cfg = Path(tmp) / "herdr" / "config.toml"
            cfg.parent.mkdir()
            text = '[keys]\nsomething = "prefix+i"\n'
            cfg.write_text(text)
            self.assertEqual(setup.keybinding_state(), "conflict")
            setup.install_keybinding()
            self.assertEqual(cfg.read_text(), text)

    def test_relay_never_breaks_on_garbage(self):
        out = subprocess.run([sys.executable, str(ROOT / "herdr_eartag" / "statusline.py"), "--then", "echo ok"],
                             input=b"not json", capture_output=True,
                             env={**os.environ, "XDG_STATE_HOME": tempfile.mkdtemp()})
        self.assertEqual(out.returncode, 0)
        self.assertEqual(out.stdout.decode().strip(), "ok")


if __name__ == "__main__":
    unittest.main()



class HerdrHookTests(unittest.TestCase):
    def test_hook_per_account_and_undo_keeps_herdrs_own(self):
        from herdr_eartag import setup
        with tempfile.TemporaryDirectory() as tmp:
            env = {"HOME": tmp, "HERDR_BIN_PATH": fake_herdr(tmp),
                   "XDG_DATA_HOME": str(Path(tmp) / "share"),
                   "XDG_STATE_HOME": str(Path(tmp) / "state"),
                   "XDG_CONFIG_HOME": str(Path(tmp) / "config")}
            home = Path(tmp)
            main, work = home / ".claude", home / ".claude-work"
            (work / "projects").mkdir(parents=True)
            main.mkdir()
            # The main account already has herdr's hook (installed by the user).
            own = {"hooks": {"SessionStart": [{"hooks": [{"type": "command",
                   "command": "bash '/x/.claude/hooks/herdr-agent-state.sh' session"}]}]}}
            (main / "settings.json").write_text(json.dumps(own))
            (work / "settings.json").write_text(json.dumps({"theme": "auto"}))
            with mock.patch.dict(os.environ, env):
                os.environ.pop("CLAUDE_CONFIG_DIR", None)
                self.assertEqual(setup.herdr_hooks_missing(), [work])
                self.assertIn("herdr now reports", setup.install_herdr_hooks())
                self.assertEqual(setup.herdr_hooks_missing(), [])
                self.assertEqual(herdr_calls(tmp), ["install {}".format(work)])
                setup.install_herdr_hooks()  # idempotent
                self.assertEqual(len(herdr_calls(tmp)), 1)
                # Undo removes only what Eartag added, and leaves the file as it was.
                setup.remove_herdr_hooks()
                self.assertEqual(json.loads((work / "settings.json").read_text()), {"theme": "auto"})
                self.assertEqual(json.loads((main / "settings.json").read_text()), own)
                self.assertEqual(setup.remove_herdr_hooks(), "Session hook: nothing added by Eartag.")

    def test_failed_install_is_reported_not_recorded(self):
        from herdr_eartag import setup
        with tempfile.TemporaryDirectory() as tmp:
            env = {"HOME": tmp, "HERDR_BIN_PATH": str(Path(tmp) / "no-herdr"),
                   "XDG_DATA_HOME": str(Path(tmp) / "share")}
            (Path(tmp) / ".claude").mkdir()
            with mock.patch.dict(os.environ, env):
                os.environ.pop("CLAUDE_CONFIG_DIR", None)
                self.assertIn("failed", setup.install_herdr_hooks())
                self.assertFalse(setup.herdr_hooks_path().exists())

    def test_fix_line_only_when_account_lacks_hook(self):
        from herdr_eartag.render import View, render
        frame = render(View(width=40, height=30, inferred_note="session inferred from folder",
                            inferred_fix="Fix (link this account to herdr)"))
        rows = [i for i, line in enumerate(frame.lines) if "Fix (link" in "".join(t for t, _ in line)]
        self.assertEqual(len(rows), 1)
        self.assertTrue(any(h.action == "setup" and h.y == rows[0] for h in frame.hits))
        plain = render(View(width=40, height=30, inferred_note="session inferred from folder"))
        self.assertFalse(any(h.action == "setup" for h in plain.hits))


class HotReloadTests(unittest.TestCase):
    def test_fingerprint_changes_and_import_check(self):
        from herdr_eartag import tui
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            pkg = root / "herdr_eartag"
            pkg.mkdir()
            (root / "eartag.py").write_text("")
            (pkg / "__init__.py").write_text("")
            (pkg / "cli.py").write_text("X = 1\n")
            (pkg / "tui.py").write_text("")
            before = tui.code_fingerprint(root)
            self.assertIsNotNone(before)
            self.assertTrue(tui.code_imports(root))
            (pkg / "cli.py").write_text("X = (\n")  # broken update
            self.assertNotEqual(tui.code_fingerprint(root), before)
            self.assertFalse(tui.code_imports(root))
            self.assertIsNone(tui.code_fingerprint(root / "gone"))

    def test_waits_for_files_to_settle_then_reloads(self):
        from herdr_eartag import tui
        app = tui.App.__new__(tui.App)
        app.code, app.code_seen = ("a",), ("a",)
        app.next_code_check, app.reload, app.running = 0, False, True
        with mock.patch.object(tui, "code_fingerprint", return_value=("b",)), \
                mock.patch.object(tui, "code_imports", return_value=True):
            app.check_code(10)  # first sight of the change: wait for it to settle
            self.assertFalse(app.reload)
            app.check_code(20)  # unchanged since: reload
            self.assertTrue(app.reload)
            self.assertFalse(app.running)
        app.reload, app.running, app.code, app.code_seen = False, True, ("a",), ("b",)
        with mock.patch.object(tui, "code_fingerprint", return_value=("b",)), \
                mock.patch.object(tui, "code_imports", return_value=False):
            app.check_code(30)  # broken code: keep running the old one
            self.assertFalse(app.reload)
            self.assertEqual(app.code, ("b",))
