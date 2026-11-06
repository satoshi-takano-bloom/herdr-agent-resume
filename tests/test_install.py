import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock
from contextlib import redirect_stdout
from io import StringIO

spec = importlib.util.spec_from_file_location("installer", Path(__file__).parents[1] / "scripts/install.py")
installer = importlib.util.module_from_spec(spec)
spec.loader.exec_module(installer)

KEY = {"global": {"ctrl-shift-r": "mod.open:agent-resume"}}
CMD = installer.hook_command("/app/mods/agent-resume/agent_resume.py", "codex")


class KeyTests(unittest.TestCase):
    def test_conflict_leaves_input_unchanged(self):
        original = {"global": {"ctrl-shift-r": "something.else"}}
        with self.assertRaisesRegex(ValueError, "Shortcut conflict"):
            installer.merge_keys(original, KEY)
        self.assertEqual(original["global"]["ctrl-shift-r"], "something.else")

    def test_install_and_uninstall_keep_other_keys(self):
        existing = {"global": {"ctrl-shift-f": "layout.toggle:panel"}}
        merged = installer.merge_keys(existing, KEY)
        self.assertEqual(merged["global"], {"ctrl-shift-f": "layout.toggle:panel", "ctrl-shift-r": "mod.open:agent-resume"})
        self.assertEqual(installer.merge_keys(merged, KEY), merged)
        self.assertEqual(installer.remove_keys(merged, KEY), existing)

    def test_uninstall_leaves_a_key_someone_rebound(self):
        rebound = {"global": {"ctrl-shift-r": "something.else"}}
        self.assertEqual(installer.remove_keys(rebound, KEY), rebound)


class HookTests(unittest.TestCase):
    def test_appends_after_existing_groups(self):
        existing = {"model": "x", "hooks": {"SessionStart": [{"hooks": [{"type": "command", "command": "herdr"}]}],
                                            "Stop": [{"hooks": [{"type": "command", "command": "orca"}]}]}}
        merged = installer.add_hook(existing, CMD)
        self.assertEqual(merged["hooks"]["SessionStart"][0], existing["hooks"]["SessionStart"][0])
        self.assertEqual(merged["hooks"]["SessionStart"][1], {"hooks": [{"type": "command", "command": CMD, "timeout": 10}]})
        self.assertEqual(merged["hooks"]["Stop"], existing["hooks"]["Stop"])
        self.assertEqual(merged["model"], "x")
        self.assertEqual(len(existing["hooks"]["SessionStart"]), 1)

    def test_repeat_install_does_not_add_a_second_hook(self):
        once = installer.add_hook({}, CMD)
        self.assertEqual(installer.add_hook(once, CMD), once)
        moved = installer.add_hook(once, installer.hook_command("/elsewhere/agent_resume.py", "codex"))
        self.assertEqual(len(moved["hooks"]["SessionStart"]), 1)
        self.assertIn("/elsewhere/", moved["hooks"]["SessionStart"][0]["hooks"][0]["command"])

    def test_uninstall_removes_only_ours(self):
        existing = {"hooks": {"SessionStart": [{"hooks": [{"type": "command", "command": "herdr"}]}]}}
        self.assertEqual(installer.remove_hook(installer.add_hook(existing, CMD)), existing)
        self.assertEqual(installer.remove_hook(installer.add_hook({}, CMD)), {"hooks": {}})


class OtherAgentHookTests(unittest.TestCase):
    def test_agy_block_is_added_beside_other_hooks_and_removed(self):
        cmd = installer.hook_command("/app/mods/agent-resume/agent_resume.py", "agy")
        existing = {"herdr": {"PreInvocation": [{"command": "herdr", "type": "command"}]}}
        merged = installer.add_agy_hook(existing, cmd)
        self.assertEqual(merged["herdr"], existing["herdr"])
        self.assertIn("hook agy", merged["agent-resume"]["SessionStart"][0]["command"])
        self.assertEqual(len(merged["agent-resume"]["PreInvocation"]), 1)
        self.assertEqual(installer.add_agy_hook(merged, cmd), merged)
        moved = installer.add_agy_hook(merged, installer.hook_command("/elsewhere/agent_resume.py", "agy"))
        self.assertIn("/elsewhere/", moved["agent-resume"]["SessionStart"][0]["command"])
        self.assertEqual(installer.remove_agy_hook(moved), existing)
        with self.assertRaisesRegex(ValueError, "isn't this mod's"):
            installer.add_agy_hook({"agent-resume": {"SessionStart": [{"command": "mine"}]}}, cmd)

    def test_kiro_entries_keep_the_other_hooks(self):
        cmd = installer.hook_command("/app/agent_resume.py", "kiro")
        existing = {"version": "v1", "hooks": [{"name": "other", "trigger": "Stop", "action": {"type": "command", "command": "x"}}]}
        merged = installer.add_kiro_hook(existing, cmd)
        self.assertEqual(merged["hooks"][0]["name"], "other")
        self.assertEqual([hook["trigger"] for hook in merged["hooks"][1:]], ["SessionStart", "UserPromptSubmit"])
        again = installer.add_kiro_hook(merged, installer.hook_command("/moved/agent_resume.py", "kiro"))
        self.assertEqual(len(again["hooks"]), 3)
        self.assertIn("/moved/", again["hooks"][1]["action"]["command"])
        self.assertEqual(installer.remove_kiro_hook(again)["hooks"], existing["hooks"])

    def test_install_update_uninstall_never_change_kiro_profiles_or_settings(self):
        for default in (None, "kiro", "work"):
            for preexisting in (False, True):
                with self.subTest(default=default, preexisting=preexisting), tempfile.TemporaryDirectory() as directory:
                    home = Path(directory)
                    app = home / "app"
                    app.mkdir()
                    (app / "keys.json").write_text('{}')
                    settings = home / ".kiro/settings/cli.json"
                    settings.parent.mkdir(parents=True)
                    original = json.dumps({"chat.defaultAgent": default, "other": 42})
                    settings.write_text(original)
                    agent = home / ".kiro/agents/kiro.json"
                    if preexisting:
                        agent.parent.mkdir()
                        agent.write_text('{"name":"kiro","prompt":"mine"}')
                    agent_before = agent.read_bytes() if agent.exists() else None
                    base = ["install.py", "--home", str(home), "--app-home", str(app), "--no-adopt"]
                    with mock.patch.object(installer.subprocess, "run") as run:
                        for flag in ([], ["--update"], ["--uninstall"]):
                            with mock.patch("sys.argv", base + flag), redirect_stdout(StringIO()):
                                installer.main()
                            self.assertEqual(settings.read_text(), original)
                            self.assertEqual(agent.read_bytes() if agent.exists() else None, agent_before)
                        self.assertTrue(all(call.args[0][0] == "herdr-app" for call in run.call_args_list))
                    self.assertFalse((home / ".kiro/hooks/agent-resume.json").exists())

    def test_pi_extension_points_at_the_script(self):
        text = installer.pi_extension("/app/mods/agent-resume/agent_resume.py")
        self.assertIn('"/app/mods/agent-resume/agent_resume.py"', text)
        self.assertNotIn("__SCRIPT__", text)
        self.assertIn("hook\", \"pi\"", text)
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            (home / ".pi" / "agent").mkdir(parents=True)
            added = installer.pi_changes(home, "/app/mods/agent-resume/agent_resume.py", False)
            installer.apply_shell(added)
            path = home / ".pi" / "agent" / "extensions" / "herdr-agent-resume.js"
            self.assertEqual(path.read_text(), text)
            self.assertEqual(installer.pi_changes(home, "/app/mods/agent-resume/agent_resume.py", False), [])
            removed = installer.pi_changes(home, "/app/mods/agent-resume/agent_resume.py", True)
            installer.apply_shell(removed)
            self.assertFalse(path.exists())

    def test_opencode_plugin_is_listed_beside_the_other_plugins_and_removed(self):
        mod = "/app/mods/agent-resume"
        with tempfile.TemporaryDirectory() as folder:
            home = Path(folder)
            self.assertEqual(installer.opencode_changes(home, mod, False), [])  # OpenCode isn't set up here
            config = home / ".config" / "opencode"
            config.mkdir(parents=True)
            cli = config / "cli.json"
            original = {"session": {"sidebar": "hide"}, "plugins": ["./herdr-opencode", {"package": "/x", "options": {"a": 1}}]}
            cli.write_text(json.dumps(original))
            [(path, text)] = installer.opencode_changes(home, mod, False)
            self.assertEqual(path, cli)
            self.assertEqual(json.loads(text)["plugins"], [*original["plugins"], mod])
            self.assertEqual(json.loads(text)["session"], original["session"])
            cli.write_text(text)
            self.assertEqual(installer.opencode_changes(home, mod, False), [])  # a repeat changes nothing
            moved = installer.opencode_changes(home, "/elsewhere/mods/agent-resume", False)
            self.assertEqual(json.loads(moved[0][1])["plugins"], [*original["plugins"], "/elsewhere/mods/agent-resume"])
            [(_, text)] = installer.opencode_changes(home, mod, True)
            self.assertEqual(json.loads(text), original)

    def test_opencode_without_a_cli_file_gets_one_and_loses_it_again(self):
        mod = "/app/mods/agent-resume"
        with tempfile.TemporaryDirectory() as folder:
            home = Path(folder)
            (home / ".config" / "opencode").mkdir(parents=True)
            [(path, text)] = installer.opencode_changes(home, mod, False)
            self.assertEqual(json.loads(text), {"plugins": [mod]})
            path.write_text(text)
            self.assertEqual(installer.opencode_changes(home, mod, True), [(path, None)])

    def test_a_cli_file_that_is_not_plain_json_stops_the_install(self):
        with tempfile.TemporaryDirectory() as folder:
            config = Path(folder) / ".config" / "opencode"
            config.mkdir(parents=True)
            (config / "cli.json").write_text("// comment\n{}")
            with self.assertRaisesRegex(ValueError, "cli.json"):
                installer.opencode_changes(Path(folder), "/app/mods/agent-resume", False)

    def test_the_tui_part_is_installed_with_the_mod(self):
        self.assertIn("tui.js", installer.FILES)

    def test_only_agents_set_up_on_this_machine(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            (home / ".grok").mkdir()
            (home / ".gemini" / "config").mkdir(parents=True)
            (home / ".kiro").mkdir()
            found = {name: kind for name, _, _, kind in installer.agent_configs(home)}
            self.assertEqual(found, {"grok": "group", "agy": "agy", "kiro": "kiro"})


class ShellTests(unittest.TestCase):
    SCRIPT = "/app/mods/agent-resume/agent_resume.py"

    def test_block_is_added_once_and_removed_cleanly(self):
        block = installer.snippet("bash", self.SCRIPT)
        for original in ("", "alias ll='ls -l'\n", "export A=1"):
            added = installer.with_block(original, block)
            self.assertEqual(added.count(installer.BEGIN), 1)
            self.assertEqual(installer.with_block(added, block), added)
            self.assertEqual(installer.without_block(added), original if original.endswith("\n") or not original else original + "\n")

    def test_an_older_block_is_replaced_in_place(self):
        text = "a\n\n" + installer.snippet("zsh", "/old/agent_resume.py") + "b\n"
        new = installer.with_block(text, installer.snippet("zsh", self.SCRIPT))
        self.assertTrue(new.startswith("a\n\n" + installer.BEGIN))
        self.assertTrue(new.endswith(installer.END + "\nb\n"))
        self.assertNotIn("/old/", new)

    def test_a_broken_block_changes_nothing(self):
        with self.assertRaisesRegex(ValueError, "not their end"):
            installer.without_block("x\n" + installer.BEGIN + "\nfoo\n")

    def test_reports_a_failed_resume(self):
        for shell, code in (("fish", "$status"), ("bash", "$?"), ("zsh", "$?")):
            self.assertIn(f"here || command python3 '{self.SCRIPT}' after {code}" if shell != "fish"
                          else f"here; or command python3 '{self.SCRIPT}' after {code}", installer.snippet(shell, self.SCRIPT))

    def test_refuses_a_path_with_a_quote(self):
        with self.assertRaises(ValueError):
            installer.snippet("fish", "/it's/agent_resume.py")

    def test_changes_per_mode(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            (home / ".config" / "fish").mkdir(parents=True)
            (home / ".bashrc").write_text("export A=1\n")
            self.assertEqual(installer.shell_changes(home, self.SCRIPT, "refresh"), [])
            self.assertEqual(installer.shell_changes(home, self.SCRIPT, "remove"), [])
            added = installer.shell_changes(home, self.SCRIPT, "add")
            self.assertEqual([p.name for p, _ in added], ["agent-resume.fish", ".bashrc"])
            installer.apply_shell(added)
            self.assertIn("fish_prompt", (home / ".config/fish/conf.d/agent-resume.fish").read_text())
            self.assertEqual(installer.shell_changes(home, self.SCRIPT, "add"), [])
            self.assertEqual(len(installer.shell_changes(home, "/moved/agent_resume.py", "refresh")), 2)
            installer.apply_shell(installer.shell_changes(home, self.SCRIPT, "remove"))
            self.assertFalse((home / ".config/fish/conf.d/agent-resume.fish").exists())
            self.assertEqual((home / ".bashrc").read_text(), "export A=1\n")


class UpdateTests(unittest.TestCase):
    def test_update_refuses_edited_or_extra_files(self):
        with tempfile.TemporaryDirectory() as directory:
            destination = Path(directory)
            (destination / "main.js").write_text("original")
            marker = {"package": installer.PACKAGE, "files": {"main.js": installer.digest(destination / "main.js")}}
            (destination / "jsconfig.json").write_text("generated by Herdr")
            (destination / "__pycache__").mkdir()
            installer.verify_installed(destination, marker)
            (destination / "main.js").write_text("edited")
            with self.assertRaisesRegex(ValueError, "was edited"):
                installer.verify_installed(destination, marker)
            (destination / "main.js").write_text("original")
            (destination / "notes.md").write_text("mine")
            with self.assertRaisesRegex(ValueError, "Extra installed file"):
                installer.verify_installed(destination, marker)


if __name__ == "__main__":
    unittest.main()
