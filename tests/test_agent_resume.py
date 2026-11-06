from contextlib import suppress
import importlib.util
import json
import multiprocessing
import os
from io import StringIO
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

spec = importlib.util.spec_from_file_location("agent_resume", Path(__file__).parents[1] / "mods/agent-resume/agent_resume.py")
ar = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ar)

BOOT = "boot-now"
SID = "4f1c2a9e-7b3d-4e8a-9c51-2d6f0a8b3e17"
SID2 = "019a2b7c-3d4e-7f10-8a2b-c3d4e5f60718"


def label(agent="claude", session=SID, shell=100, boot=BOOT, at=1, cwd="/work", pid=200):
    return {"agent": agent, "session": session, "cwd": cwd, "transcript": f"/t/{session}.jsonl",
            "pid": pid, "shell": shell, "boot": boot, "at": at}


def terminal(address="t1", pid=300, fg_pid=None, fg_name="fish", lbl=None, state="live", where="api / 1"):
    labels = {"where": where}
    if lbl is not None:
        labels[ar.LABEL] = json.dumps(lbl)
    return {"address": address, "state": state, "pid": pid, "labels": labels,
            "foreground": {"pid": pid if fg_pid is None else fg_pid, "name": fg_name}}


def run_plan(terminals, boot=BOOT, folder_ok=lambda p: True, file_ok=lambda p: True):
    return ar.plan(terminals, boot, folder_ok=folder_ok, file_ok=file_ok)


def paused_claim(runtime, ready, finish_write, finish_process, result):
    """Hold a real claim writer before its PID is published, then keep it alive."""
    original_stamp = ar._stamp

    def stamp(pid):
        if pid == os.getpid():
            ready.set()
            if not finish_write.wait(10):
                raise RuntimeError("claim writer was not released")
        return original_stamp(pid)

    with mock.patch.dict(os.environ, {"XDG_RUNTIME_DIR": runtime}), \
         mock.patch.object(ar, "_stamp", side_effect=stamp):
        result.send(ar.claim("claude", SID))
        finish_process.wait(10)


class LabelTests(unittest.TestCase):
    def test_reads_a_valid_label(self):
        self.assertEqual(ar.read_label({ar.LABEL: json.dumps(label())})["session"], SID)

    def test_ignores_broken_or_foreign_labels(self):
        for raw in ("not json", json.dumps([1]), json.dumps(label(agent="vim")),
                    json.dumps(label(session="x; rm -rf ~")), json.dumps(label(session="short"))):
            self.assertIsNone(ar.read_label({ar.LABEL: raw}), raw)
        self.assertIsNone(ar.read_label({}))
        self.assertIsNone(ar.read_label(None))

    def test_rejects_missing_or_invalid_fields(self):
        bad_fields = {
            "agent": [None, [], {}],
            "session": [None, [], SID + "\n"],
            "cwd": [None, "", 3, True, [], {}, "relative", "/bad\0path"],
            "transcript": [None, "", 3, True, [], {}, "relative", "/bad\0path"],
            "pid": [None, "200", 0, -1, True, [], {}],
            "shell": [None, "100", 0, -1, True, [], {}],
            "at": ["yesterday", -1, True, [], {}, float("nan"), float("inf")],
            "boot": [1, True, [], {}],
        }
        for field, values in bad_fields.items():
            if field not in ("at", "boot"):
                missing = label()
                del missing[field]
                with self.subTest(field=field, missing=True):
                    self.assertIsNone(ar.read_label({ar.LABEL: json.dumps(missing)}))
            for value in values:
                with self.subTest(field=field, value=value):
                    self.assertIsNone(ar.read_label({ar.LABEL: json.dumps({**label(), field: value})}))

    def test_rejects_non_json_label_values(self):
        for raw in (3, True, [], {"agent": "claude"}):
            with self.subTest(raw=raw):
                self.assertIsNone(ar.read_label({ar.LABEL: raw}))

    def test_optional_metadata_and_quoted_paths_still_work(self):
        saved = label(cwd="/it's/a\\folder")
        del saved["at"]
        del saved["boot"]
        self.assertIsNotNone(ar.read_label({ar.LABEL: json.dumps(saved)}))
        self.assertIsNotNone(ar.read_label({ar.LABEL: json.dumps({**saved, "at": None, "boot": None})}))


class LostTests(unittest.TestCase):
    def test_same_program_same_boot_is_not_lost(self):
        self.assertFalse(ar.lost({"pid": 100}, label(shell=100), BOOT))

    def test_another_program_is_lost(self):
        self.assertTrue(ar.lost({"pid": 101}, label(shell=100), BOOT))

    def test_another_boot_is_lost_even_with_a_reused_pid(self):
        self.assertTrue(ar.lost({"pid": 100}, label(shell=100, boot="old"), BOOT))

    def test_unknown_boot_falls_back_to_the_pid(self):
        self.assertFalse(ar.lost({"pid": 100}, label(shell=100, boot=None), None))
        self.assertTrue(ar.lost({"pid": None}, label(shell=100), BOOT))


class ShellTests(unittest.TestCase):
    def test_idle_shells(self):
        for name in ("fish", "-fish", "bash", "/bin/zsh", "sh"):
            self.assertTrue(ar.at_shell(terminal(fg_name=name)), name)

    def test_busy_terminals(self):
        self.assertFalse(ar.at_shell(terminal(fg_name="claude", fg_pid=301)))
        self.assertFalse(ar.at_shell(terminal(fg_name="bash", fg_pid=301)))  # a nested shell
        self.assertFalse(ar.at_shell(terminal(fg_name="vim")))
        self.assertFalse(ar.at_shell({"pid": 300}))
        self.assertFalse(ar.at_shell(terminal(pid=None)))


class CommandTests(unittest.TestCase):
    def test_claude_and_codex(self):
        self.assertEqual(ar.command(label(cwd="/a b/c")), f"cd '/a b/c' && claude --resume {SID}")
        self.assertEqual(ar.command(label(agent="codex", session=SID2)), f"cd '/work' && codex resume {SID2}")

    def test_grok_agy_pi_and_kiro(self):
        self.assertEqual(ar.command(label(agent="grok")), f"cd '/work' && grok --resume {SID}")
        self.assertEqual(ar.command(label(agent="agy")), f"cd '/work' && agy --conversation {SID}")
        self.assertEqual(ar.command(label(agent="pi")), f"cd '/work' && pi --session {SID}")
        self.assertTrue(ar.command(label(agent="kiro", session="sess_0123abcd")).endswith("launch-kiro sess_0123abcd"))
        self.assertTrue(ar.valid_session("custom.id_1"))

    def test_refuses_folders_that_cannot_be_typed_safely(self):
        for cwd in ("/it's", "/back\\slash", "/new\nline", "relative", "", None):
            self.assertIsNone(ar.command(label(cwd=cwd)), repr(cwd))


class FlagTests(unittest.TestCase):
    def test_keeps_claude_launch_flags_only(self):
        argv = ["--model", "opus", "--dangerously-skip-permissions", "--resume", SID, "fix the bug",
                "--add-dir", "/a", "/b", "--verbose", "-p", "--worktree", "w", "--permission-mode=plan", "--session-id", SID2]
        self.assertEqual(ar.launch_flags("claude", argv), [["--model", "opus"], ["--dangerously-skip-permissions"],
                                                           ["--add-dir", "/a", "/b"], ["--verbose"], ["--permission-mode=plan"]])

    def test_keeps_flags_codex_resume_accepts(self):
        argv = ["-m", "gpt-5", "-c", "model_reasoning_effort=high", "resume", SID2, "--full-auto",
                "-s", "workspace-write", "--model=o3", "-i", "shot.png", "--cd", "/x"]
        self.assertEqual(ar.launch_flags("codex", argv), [["-m", "gpt-5"], ["-c", "model_reasoning_effort=high"],
                                                          ["-s", "workspace-write"], ["--model=o3"]])

    def test_codex_keeps_running_under_herdr(self):
        args = ar.flat(ar.launch_flags("codex", ["--no-daemon", "-p", "matt-oc-implementer"]))
        self.assertEqual(args, ["--no-daemon", "-p", "matt-oc-implementer"])

    def test_edge_cases(self):
        self.assertEqual(ar.launch_flags("claude", ["--model"]), [])                     # no value
        self.assertEqual(ar.launch_flags("claude", ["--model", "--verbose"]), [["--verbose"]])
        self.assertEqual(ar.launch_flags("claude", ["--add-dir", "--verbose"]), [["--verbose"]])
        self.assertEqual(ar.launch_flags("claude", ["--", "--model", "x"]), [])
        self.assertEqual(ar.launch_flags("claude", ["--model", "x" * 5000]), [])
        self.assertEqual(ar.launch_flags("claude", None), [])
        self.assertEqual(len(ar.flat(ar.launch_flags("claude", ["--add-dir", *["/d"] * 200]))), 0)

    def test_a_label_can_only_carry_kept_flags(self):
        raw = label()
        raw["args"] = ["--model", "x", "; rm -rf ~", "--evil", "--verbose"]
        self.assertEqual(ar.read_label({ar.LABEL: json.dumps(raw)})["args"], ["--model", "x", "--verbose"])
        raw["args"] = "--verbose"
        self.assertEqual(ar.read_label({ar.LABEL: json.dumps(raw)})["args"], [])

    def test_resume_command_with_flags(self):
        lbl = {**label(), "args": ["--model", "opus", "--append-system-prompt", "be brief", "--dangerously-skip-permissions"]}
        self.assertEqual(ar.command(lbl), f"cd '/work' && claude --resume {SID} --model opus "
                                          "--append-system-prompt 'be brief' --dangerously-skip-permissions")
        lbl["args"] = ["--append-system-prompt", "don't", "--model", "opus"]
        self.assertEqual(ar.command(lbl), f"cd '/work' && claude --resume {SID} --model opus")  # can't be typed: left out
        self.assertEqual(ar.resume_argv(lbl), ["claude", "--resume", SID, "--append-system-prompt", "don't", "--model", "opus"])
        codex = {**label(agent="codex", session=SID2), "args": ["-c", "a=b", "-s", "workspace-write"]}
        self.assertEqual(ar.command(codex), f"cd '/work' && codex resume {SID2} -c a=b -s workspace-write")

    def test_keeps_flags_the_other_resume_commands_accept(self):
        self.assertEqual(ar.launch_flags("grok", ["--resume", SID, "--model", "grok-4", "--always-approve",
                                                  "-c", "--permission-mode", "plan", "--worktree", "feat", "-p", "fix"]),
                         [["--model", "grok-4"], ["--always-approve"], ["--permission-mode", "plan"]])
        self.assertEqual(ar.launch_flags("agy", ["--conversation", SID, "--continue", "--model", "gemini",
                                                 "--dangerously-skip-permissions", "-p", "fix it"]),
                         [["--model", "gemini"], ["--dangerously-skip-permissions"]])
        self.assertEqual(ar.launch_flags("pi", ["--continue", "--model", "gpt-5", "--session", SID,
                                                "--thinking", "high", "--no-session", "-p", "hi"]),
                         [["--model", "gpt-5"], ["--thinking", "high"]])
        self.assertEqual(ar.launch_flags("kiro", ["chat", "--resume-id", SID, "--model", "claude",
                                                  "--trust-all-tools", "--no-interactive", "hello"]),
                         [["--model", "claude"], ["--trust-all-tools"]])


class FailureTests(unittest.TestCase):
    def test_last_words(self):
        self.assertEqual(ar.last_words("$ claude --resume x\nNo conversation found with session ID: x\n\nuser@host ~> \n"),
                         "No conversation found with session ID: x")
        self.assertIsNone(ar.last_words("user@host ~>"))
        self.assertIsNone(ar.last_words(""))

    def test_start_failed(self):
        back = terminal(pid=300, lbl=label(shell=100))
        self.assertTrue(ar.start_failed(back, ar.read_label(back["labels"]), BOOT))
        running = terminal(pid=300, fg_pid=301, fg_name="claude", lbl=label(shell=100))
        self.assertFalse(ar.start_failed(running, ar.read_label(running["labels"]), BOOT))
        noted = terminal(pid=300, lbl=label(shell=300))  # the hook noted it, then it was quit
        self.assertFalse(ar.start_failed(noted, ar.read_label(noted["labels"]), BOOT))

    def test_after(self):
        env = {"HC_TERMINAL_ID": "t1"}
        self.assertIsNone(ar.after("0", env=env))
        self.assertIsNone(ar.after("130", env=env))  # Ctrl+C: quit on purpose
        self.assertIsNone(ar.after("1", env={}))
        with tempfile.TemporaryDirectory() as run, mock.patch.dict(os.environ, {"XDG_RUNTIME_DIR": run}), \
             mock.patch.object(ar, "boot_id", return_value=BOOT):
            lost_term = terminal(pid=300, lbl=label(shell=100))
            with mock.patch.object(ar, "hc_json", return_value=lost_term):
                self.assertIsNone(ar.after("1", env=env))                 # no claim: `here` didn't start it
                Path(ar._claim_path("claude", SID)).write_text("999999999 1")
                with mock.patch.object(ar, "retry_later") as later:
                    self.assertIn("exited right away (code 1)", ar.after("1", env=env))
                later.assert_called_once_with()
                self.assertFalse(os.path.exists(ar._claim_path("claude", SID)))
                Path(ar._claim_path("claude", SID)).write_text("999999999 1")
                old = time.time() - 60
                os.utime(ar._claim_path("claude", SID), (old, old))
                self.assertIsNone(ar.after("1", env=env))                 # ran a minute: not a failed resume
            Path(ar._claim_path("claude", SID)).write_text("999999999 1")
            with mock.patch.object(ar, "hc_json", return_value=terminal(pid=300, lbl=label(shell=300))):
                self.assertIsNone(ar.after("1", env=env))                 # the hook noted it


class PlanTests(unittest.TestCase):
    def test_bad_labels_are_reported_without_blocking_valid_terminals(self):
        with tempfile.TemporaryDirectory() as folder:
            transcript = Path(folder, "session.jsonl")
            transcript.write_text("{}\n")
            good = {**label(cwd=folder), "transcript": str(transcript)}
            missing_cwd = dict(good)
            del missing_cwd["cwd"]
            broken = terminal("bad-json")
            broken["labels"][ar.LABEL] = "{"
            terms = [terminal("missing-cwd", lbl=missing_cwd),
                     terminal("bad-date", lbl={**good, "at": "yesterday"}),
                     terminal("bad-transcript", lbl={**good, "transcript": []}),
                     broken, terminal("good", lbl=good)]
            todo, skipped = ar.plan(terms, BOOT)
        self.assertEqual([item["terminal"] for item in todo], ["good"])
        self.assertEqual({item["terminal"] for item in skipped},
                         {"missing-cwd", "bad-date", "bad-transcript", "bad-json"})
        for item in skipped:
            self.assertTrue(item["reason"].startswith("invalid session label:"), item)
            self.assertIn("agent", item)  # resume's skip logger accepts every item
            self.assertIn("session", item)

    def test_resumes_a_lost_agent_at_an_idle_shell(self):
        todo, skipped = run_plan([terminal(lbl=label(shell=100))])
        self.assertEqual(skipped, [])
        self.assertEqual(todo, [{"terminal": "t1", "where": "api / 1", "agent": "claude", "session": SID,
                                 "cwd": "/work", "command": f"cd '/work' && claude --resume {SID}"}])

    def test_leaves_running_and_unlabeled_terminals_alone(self):
        todo, skipped = run_plan([terminal(lbl=label(shell=300)), terminal("t2"), terminal("t3", state="finished", lbl=label())])
        self.assertEqual((todo, skipped), ([], []))

    def test_skips_a_session_still_open_elsewhere(self):
        todo, skipped = run_plan([terminal("t1", lbl=label(shell=100)), terminal("t2", pid=400, lbl=label(shell=400))])
        self.assertEqual(todo, [])
        self.assertEqual([(s["terminal"], s["reason"]) for s in skipped], [("t1", "open in another terminal")])

    def test_resumes_one_session_once_newest_first(self):
        todo, skipped = run_plan([terminal("t1", lbl=label(shell=1, at=5)), terminal("t2", pid=301, lbl=label(shell=2, at=9))])
        self.assertEqual([t["terminal"] for t in todo], ["t2"])
        self.assertEqual([(s["terminal"], s["reason"]) for s in skipped], [("t1", "resumed in another terminal")])

    def test_reasons(self):
        cases = [
            (terminal(pid=None, lbl=label()), {}, ar.NOT_STARTED),
            (terminal(fg_name="vim", lbl=label()), {}, "something else is running there"),
            (terminal(lbl=label()), {"folder_ok": lambda p: False}, "its folder is gone"),
            (terminal(lbl=label()), {"file_ok": lambda p: False}, "no saved conversation to resume"),
            (terminal(lbl=label(cwd="/it's")), {}, "its folder name can't be typed safely"),
        ]
        for term, checks, reason in cases:
            todo, skipped = run_plan([term], **checks)
            self.assertEqual(todo, [])
            self.assertEqual([s["reason"] for s in skipped], [reason])

    def test_claude_and_codex_with_the_same_id_are_different_sessions(self):
        todo, _ = run_plan([terminal("t1", lbl=label(shell=1)), terminal("t2", pid=301, lbl=label(agent="codex", shell=2))])
        self.assertEqual(sorted(t["agent"] for t in todo), ["claude", "codex"])


    def test_a_terminal_whose_shell_brought_the_agent_back(self):
        for fg in ({"name": "claude"}, {"name": "node-MainThread", "argv": ["node", "/usr/bin/codex"]},
                   {"name": "python3", "argv": ["python3", "/m/agent_resume.py", "here"]}):
            agent = "codex" if "codex" in str(fg) else "claude"
            term = terminal(lbl=label(agent=agent))
            term["foreground"] = {"pid": 301, **fg}
            _, skipped = run_plan([term])
            self.assertEqual([s["reason"] for s in skipped], [ar.BY_SHELL], fg)


class HereTests(unittest.TestCase):
    def decide(self, term, shell=300, **checks):
        return ar.decide_here(term, shell, BOOT, **{"folder_ok": lambda p: True, "file_ok": lambda p: True, **checks})

    def test_brings_back_a_lost_agent(self):
        found, why = self.decide(terminal(lbl=label(shell=100)))
        self.assertIsNone(why)
        self.assertEqual(found["session"], SID)
        found, why = self.decide(terminal(lbl=label(shell=300, boot="old")))
        self.assertIsNone(why)

    def test_leaves_everything_else(self):
        cases = [
            (terminal(lbl=label(shell=100)), {"shell": 301}, "not the terminal's own shell"),  # a nested shell
            (terminal(lbl=label(shell=100), state="finished"), {}, "not the terminal's own shell"),
            (terminal(), {}, "no session noted"),
            (terminal(lbl=label(shell=300)), {}, "agent not lost"),
            (terminal(lbl=label(shell=100)), {"folder_ok": lambda p: False}, "its folder is gone"),
            (terminal(lbl=label(shell=100)), {"file_ok": lambda p: False}, "no saved conversation to resume"),
        ]
        for term, extra, why in cases:
            self.assertEqual(self.decide(term, **extra), (None, why))

    def test_guards(self):
        self.assertEqual(ar.here(env={}), "not in a Herdr terminal")
        self.assertEqual(ar.here(env={"HC_TERMINAL_ID": "t1", "AGENT_RESUME_AUTO": "0"}), "turned off")

    def test_bad_label_is_reported_without_starting_an_agent(self):
        bad = label()
        del bad["cwd"]
        env = {"HC_TERMINAL_ID": "t1"}
        with mock.patch.dict(os.environ, env), \
             mock.patch.object(ar, "hc_json", return_value=terminal(lbl=bad)), \
             mock.patch.object(ar.os, "getppid", return_value=300), \
             mock.patch.object(ar, "claim") as claim, \
             mock.patch.object(ar, "log") as log, \
             mock.patch("sys.stderr", new_callable=StringIO) as err:
            self.assertEqual(ar.main(["agent_resume.py", "here"]), 0)
        claim.assert_not_called()
        self.assertIn("invalid session label: cwd", err.getvalue())
        log.assert_called_once()

    def test_concurrent_claim_and_cleanup_cannot_steal_an_unwritten_claim(self):
        ctx = multiprocessing.get_context("spawn")
        for stale in (False, True):
            with self.subTest(stale=stale), tempfile.TemporaryDirectory() as run, \
                 mock.patch.dict(os.environ, {"XDG_RUNTIME_DIR": run}):
                path = Path(ar._claim_path("claude", SID))
                if stale:
                    path.write_text("999999999 1")
                ready, finish_write, finish_process = ctx.Event(), ctx.Event(), ctx.Event()
                received, sent = ctx.Pipe(duplex=False)
                writer = ctx.Process(target=paused_claim,
                                     args=(run, ready, finish_write, finish_process, sent))
                writer.start()
                sent.close()
                try:
                    self.assertTrue(ready.wait(5), "writer never reached the claim file")
                    # Custom ids may end in '.lock'; cleaning up a different
                    # session must not remove this writer's guard file either.
                    for session in (SID, SID + ".lock"):
                        with mock.patch.object(ar, "hc_json", return_value=terminal(lbl=label(session=session))), \
                             mock.patch.object(ar, "boot_id", return_value=BOOT), \
                             mock.patch.object(ar, "log"):
                            self.assertIsNone(ar.after("1", env={"HC_TERMINAL_ID": "t1"}))
                        self.assertFalse(ar.claim("claude", SID))
                    self.assertTrue(path.exists(), "cleanup removed the active writer's claim")
                    finish_write.set()
                    self.assertTrue(received.poll(5), "writer did not finish its claim")
                    self.assertTrue(received.recv())
                    self.assertEqual(ar.claim_holder("claude", SID), writer.pid)
                    self.assertFalse(ar.claim("claude", SID))
                finally:
                    finish_write.set()
                    finish_process.set()
                    writer.join(5)
                    if writer.is_alive():
                        writer.terminate()
                        writer.join(5)
                    received.close()
                self.assertEqual(writer.exitcode, 0)
                self.assertTrue(ar.claim("claude", SID))  # stale ownership is reclaimable

    def test_claims_a_session_once(self):
        with tempfile.TemporaryDirectory() as run, mock.patch.dict(os.environ, {"XDG_RUNTIME_DIR": run}):
            self.assertIsNone(ar.claim_holder("claude", SID))
            self.assertTrue(ar.claim("claude", SID))
            self.assertEqual(ar.claim_holder("claude", SID), os.getpid())
            self.assertFalse(ar.claim("claude", SID))
            self.assertTrue(ar.claim("codex", SID))
            Path(ar._claim_path("claude", SID)).write_text("999999999 1")  # a gone process
            self.assertIsNone(ar.claim_holder("claude", SID))
            self.assertTrue(ar.claim("claude", SID))

    def test_failed_claim_write_releases_the_lock_for_retry(self):
        with tempfile.TemporaryDirectory() as run, mock.patch.dict(os.environ, {"XDG_RUNTIME_DIR": run}):
            with mock.patch.object(ar, "_stamp", side_effect=OSError("cannot read process")):
                with self.assertRaises(OSError):
                    ar.claim("claude", SID)
            self.assertTrue(ar.claim("claude", SID))


class ProcessTreeTests(unittest.TestCase):
    # 50 hook sh -> 40 claude -> 30 fish (the terminal) -> 20 herdr-core -> 1
    TREE = {50: (40, {"sh"}), 40: (30, {"claude"}), 30: (20, {"fish"}), 20: (1, {"herdr-core"}),
            # 90 claude -p -> 80 bash (a tool) -> 40 claude
            90: (80, {"claude"}), 80: (40, {"bash"}),
            # 70 codex -> 60 node (codex.js) -> 30 fish
            70: (60, {"codex"}), 60: (30, {"node"}),
            # 15 claude in tmux -> 10 tmux server -> 1
            15: (10, {"claude"}), 10: (1, {"tmux: server"}),
            # Kiro's own launcher: 110 helper -> bun -> kiro-cli-chat -> kiro-cli -> fish
            85: (30, {"kiro-cli"}), 95: (85, {"kiro-cli-chat"}), 100: (95, {"bun"}), 110: (100, {"kiro-cli-chat"}),
            # The same launcher under grok, which is a different agent.
            46: (30, {"grok"}), 86: (46, {"kiro-cli"}), 96: (86, {"kiro-cli-chat"}),
            101: (96, {"bun"}), 111: (101, {"kiro-cli-chat"})}

    def lookup(self, pid):
        if pid not in self.TREE:
            return None
        ppid, names = self.TREE[pid]
        return {"ppid": ppid, "names": names, "start": None}

    def test_finds_the_nearest_agent(self):
        self.assertEqual(ar.find_agent("claude", 50, self.lookup), 40)
        self.assertEqual(ar.find_agent("codex", 70, self.lookup), 70)
        self.assertIsNone(ar.find_agent("codex", 50, self.lookup))

    def test_placement(self):
        self.assertEqual(ar.placement(40, 30, self.lookup), "direct")
        self.assertEqual(ar.placement(70, 30, self.lookup), "direct")  # through codex's node wrapper
        self.assertEqual(ar.placement(90, 30, self.lookup), "nested")
        self.assertEqual(ar.placement(15, 30, self.lookup), "outside")
        self.assertEqual(ar.placement(30, 30, self.lookup), "direct")  # the agent is the terminal's program
        self.assertEqual(ar.placement(110, 30, self.lookup), "nested")  # parent kiro-cli counts as another agent
        self.assertEqual(ar.placement(ar.kiro_launcher(110, self.lookup), 30, self.lookup), "direct")
        self.assertEqual(ar.placement(ar.kiro_launcher(111, self.lookup), 30, self.lookup), "nested")  # grok is in between


class HookTests(unittest.TestCase):
    ENV = {"HC_TERMINAL_ID": "t7"}

    def payload(self, **extra):
        return json.dumps({"hook_event_name": "SessionStart", "session_id": SID, "transcript_path": "/t/x.jsonl",
                           "cwd": "/work", "source": "startup", **extra})

    def test_guards(self):
        self.assertEqual(ar.hook("claude", self.payload(), env={}), "not in a Herdr terminal")
        self.assertEqual(ar.hook("claude", "{", env=self.ENV), "input is not JSON")
        self.assertEqual(ar.hook("claude", self.payload(hook_event_name="Stop"), env=self.ENV), "not SessionStart")
        self.assertEqual(ar.hook("claude", self.payload(agent_id="sub"), env=self.ENV), "subagent")
        self.assertEqual(ar.hook("claude", self.payload(session_id="$(boom)"), env=self.ENV), "no usable session id")
        self.assertEqual(ar.hook("claude", self.payload(transcript_path=None), env=self.ENV),
                         "no transcript: the session can't be resumed")
        self.assertEqual(ar.hook("codex", self.payload(), env={**self.ENV, "CODEX_THREAD_ID": SID2}), "codex sub-thread")

    def test_saves_a_direct_session(self):
        with mock.patch.object(ar, "find_agent", return_value=40), \
             mock.patch.object(ar, "hc_json", return_value={"state": "live", "pid": 30}), \
             mock.patch.object(ar, "placement", return_value="direct"), \
             mock.patch.object(ar, "remember") as remember:
            self.assertEqual(ar.hook("codex", self.payload(), env={**self.ENV, "CODEX_THREAD_ID": SID}), "saved")
        remember.assert_called_once_with("t7", "codex", SID, "/work", "/t/x.jsonl", 40, 30)

    def saved(self, agent, raw, env=None):
        with mock.patch.object(ar, "find_agent", return_value=40), \
             mock.patch.object(ar, "hc_json", return_value={"state": "live", "pid": 30}), \
             mock.patch.object(ar, "placement", return_value="direct"), \
             mock.patch.object(ar, "remember") as remember:
            outcome = ar.hook(agent, raw, env={**self.ENV, **(env or {})})
        return outcome, remember

    def test_grok_agy_pi_and_kiro_payloads(self):
        outcome, remember = self.saved("grok", json.dumps({
            "hookEventName": "session_start", "sessionId": SID, "cwd": "/work", "transcriptPath": "/t/x.jsonl"}))
        self.assertEqual(outcome, "saved")
        remember.assert_called_once_with("t7", "grok", SID, "/work", "/t/x.jsonl", 40, 30)

        outcome, remember = self.saved("grok", json.dumps({"hook_event_name": "SessionStart", "cwd": "/work"}),
                                       env={"GROK_SESSION_ID": SID})
        self.assertEqual(outcome, "saved")
        self.assertTrue(remember.call_args[0][4].endswith(f"/.grok/sessions/%2Fwork/{SID}/updates.jsonl"))

        outcome, remember = self.saved("agy", json.dumps({
            "conversationId": SID, "workspacePaths": ["/work"], "transcriptPath": "/t/x.jsonl"}))
        self.assertEqual(outcome, "saved")
        remember.assert_called_once_with("t7", "agy", SID, "/work", "/t/x.jsonl", 40, 30)

        outcome, remember = self.saved("agy", json.dumps({
            "hookEventName": "PreInvocation", "conversationId": SID, "workspacePaths": ["/repo"],
            "transcriptPath": "/t/x.jsonl"}))
        self.assertEqual(outcome, "saved")
        self.assertEqual(remember.call_args[0][3], "/repo")

        outcome, remember = self.saved("pi", self.payload())
        self.assertEqual(outcome, "saved")
        remember.assert_called_once_with("t7", "pi", SID, "/work", "/t/x.jsonl", 40, 30)

        outcome, remember = self.saved("kiro", json.dumps({
            "hook_event_name": "SessionStart", "session_id": "sess_" + SID, "cwd": "/proj"}))
        self.assertEqual(outcome, "saved")
        self.assertEqual(remember.call_args[0][3], "/proj")
        self.assertTrue(remember.call_args[0][4].endswith(f"/sess_{SID}/session.json"))

        outcome, remember = self.saved("kiro", json.dumps({
            "hook_event_name": "UserPromptSubmit", "session_id": "sess_" + SID, "cwd": "/proj", "prompt": "hi"}))
        self.assertEqual(outcome, "saved")

        self.assertEqual(ar.hook("grok", json.dumps({"hook_event_name": "SessionStart", "session_id": SID,
                                                    "subagentType": "explore", "transcript_path": "/t/x.jsonl",
                                                    "cwd": "/work"}), env=self.ENV), "subagent")
        self.assertIsNone(ar.default_transcript("claude", SID, "/work"))
        self.assertIsNone(ar.default_transcript("pi", SID, "/work"))

    def test_a_repeat_of_the_same_session_does_not_note_it_again(self):
        lbl = label(agent="agy", shell=30, pid=40)
        terminal = {"state": "live", "pid": 30, "labels": {ar.LABEL: json.dumps(lbl)}}
        with mock.patch.object(ar, "find_agent", return_value=40), \
             mock.patch.object(ar, "hc_json", return_value=terminal), \
             mock.patch.object(ar, "placement", return_value="direct"), \
             mock.patch.object(ar, "remember") as remember:
            outcome = ar.hook("agy", json.dumps({"conversationId": SID, "workspacePaths": ["/work"],
                                                 "transcriptPath": lbl["transcript"]}), env=self.ENV)
        self.assertEqual(outcome, "already noted")
        remember.assert_not_called()

    def test_hook_stdout_stays_quiet_except_where_the_agent_requires_it(self):
        with mock.patch.object(ar, "hook", return_value="saved"), \
             mock.patch.object(ar, "log"), \
             mock.patch("sys.stdin", StringIO("{}")):
            for agent, expected in (("claude", ""), ("agy", "{}\n"), ("pi", "saved\n"), ("opencode", "saved\n")):
                with mock.patch("sys.stdout", new_callable=StringIO) as out:
                    self.assertEqual(ar.main(["agent_resume.py", "hook", agent]), 0)
                self.assertEqual(out.getvalue(), expected, agent)

    def test_process_ancestry_preserves_outer_session(self):
        # Real find_agent/placement logic; only the OS and label write are mocked.
        for agent, name in (("claude", "claude"), ("codex", "codex"), ("kiro", "kiro-cli")):
            for nested in (False, True):
                tree = {30: {"ppid": 1, "names": {"fish"}},
                        40: {"ppid": 30, "names": {name}},
                        70: {"ppid": 40, "names": {"bash"}},
                        90: {"ppid": 70, "names": {name}},
                        100: {"ppid": 90 if nested else 40, "names": {"sh"}}}
                if agent == "kiro":
                    tree[100]["ppid"] = 110
                    tree[110] = {"ppid": 120, "names": {"kiro-cli-chat"}}
                    tree[120] = {"ppid": 130, "names": {"bun"}}
                    tree[130] = {"ppid": 90 if nested else 40, "names": {"kiro-cli-chat"}}
                terminal = {"state": "live", "pid": 30, "foreground": {"pid": 40, "name": name}}
                sid = "sess_" + SID if agent == "kiro" else SID
                with self.subTest(agent=agent, nested=nested), \
                     mock.patch.object(ar, "_proc_linux", side_effect=tree.get), \
                     mock.patch.object(ar.os.path, "isdir", return_value=True), \
                     mock.patch.object(ar.os, "getppid", return_value=100), \
                     mock.patch.object(ar, "hc_json", return_value=terminal), \
                     mock.patch.object(ar, "remember") as remember:
                    outcome = ar.hook(agent, self.payload(session_id=sid), env=self.ENV)
                if nested:
                    self.assertEqual(outcome, "agent runs nested")
                    remember.assert_not_called()
                else:
                    self.assertEqual(outcome, "saved")
                    self.assertEqual(remember.call_args.args[5], 40)

    def test_kiro_only_accepts_v3_sessions(self):
        self.assertEqual(ar.hook("kiro", self.payload(), env=self.ENV), "Kiro V2 is not supported")
        self.assertIsNone(ar.read_label({ar.LABEL: json.dumps(label(agent="kiro"))}))
        self.assertFalse(ar.is_session_event("kiro", {"hook_event_name": "agentSpawn"}))

    def test_kiro_metadata_location_and_forced_engine(self):
        with tempfile.TemporaryDirectory() as home:
            sid = "sess_" + SID
            predicted = Path(ar.default_transcript("kiro", sid, "/proj", home))
            self.assertEqual(predicted.name, "session.json")
            metadata = Path(home, ".kiro", "sessions", "multiple-roots", sid, "session.json")
            metadata.parent.mkdir(parents=True)
            metadata.write_text('{}')
            self.assertEqual(ar.default_transcript("kiro", sid, "/proj", home), str(metadata))
            saved = {**label(agent="kiro", session=sid), "args": ["--v2", "--agent-engine", "v2", "--mode", "spec"]}
            self.assertEqual(ar.resume_argv(saved), ["kiro-cli", "chat", "--v3", "--resume-id", sid, "--mode", "spec"])

    def test_skips_nested_agents(self):
        with mock.patch.object(ar, "find_agent", return_value=90), \
             mock.patch.object(ar, "hc_json", return_value={"state": "live", "pid": 30}), \
             mock.patch.object(ar, "placement", return_value="nested"), \
             mock.patch.object(ar, "remember") as remember:
            self.assertEqual(ar.hook("claude", self.payload(), env=self.ENV), "agent runs nested")
        remember.assert_not_called()


class KiroLifecycleTests(unittest.TestCase):
    def test_exit_without_a_prompt_forgets_session_and_failure_allows_retry(self):
        original = label(agent="kiro", session="sess_" + SID, shell=10, pid=11)
        current = {**original, "shell": 30, "pid": 40}
        terminal = {"pid": 30, "labels": {ar.LABEL: json.dumps(current)}}
        for code, elapsed, restore in ((0, 1, False), (130, 1, False), (1, 1, True), (1, 60, False)):
            with self.subTest(code=code, elapsed=elapsed), \
                 mock.patch.object(ar, "hc_json", return_value=terminal), \
                 mock.patch.object(ar, "hc") as hc:
                ar.kiro_finished("t7", original, 30, 40, code, elapsed)
                value = hc.call_args.args[0][-1]
                if restore:
                    self.assertEqual(json.loads(value.split("=", 1)[1]), original)
                else:
                    self.assertEqual(value, ar.LABEL + "=")

    def test_old_job_cannot_clear_a_replacement(self):
        original = label(agent="kiro", session="sess_" + SID)
        for shell, pid in ((30, 99), (99, 40)):
            terminal = {"pid": shell, "labels": {ar.LABEL: json.dumps({**original, "pid": pid})}}
            with mock.patch.object(ar, "hc_json", return_value=terminal), mock.patch.object(ar, "hc") as hc:
                ar.kiro_finished("t7", original, 30, 40, 0, 60)
                hc.assert_not_called()

    def test_wrapper_records_child_before_waiting_and_keeps_launch_flags(self):
        saved = {**label(agent="kiro", session="sess_" + SID), "args": ["--mode", "spec"]}
        def run(argv, cwd, started):
            self.assertIn("--v3", argv)
            started(40)
            return 0
        with mock.patch.object(ar, "run_as_job", side_effect=run), \
             mock.patch.object(ar, "remember") as remember, \
             mock.patch.object(ar, "kiro_finished") as finish:
            self.assertEqual(ar.run_kiro("t7", saved, 30), 0)
        self.assertEqual(remember.call_args.kwargs, {"watcher": False, "launch_args": ["--mode", "spec"]})
        self.assertEqual(finish.call_args.args[:5], ("t7", saved, 30, 40, 0))


OC_SID = "ses_e6499f92fffeXxp4nswJpod8Fm"


def oc_label(**extra):
    saved = label(agent="opencode", session=OC_SID)
    saved["transcript"] = None
    return {**saved, **extra}


def fake_opencode(folder, script):
    path = Path(folder) / "opencode"
    path.write_text("#!/bin/sh\n" + script)
    path.chmod(0o755)


class OpenCodeTests(unittest.TestCase):
    ENV = {"HC_TERMINAL_ID": "t7"}

    def decide(self, term, session_ok):
        return ar.decide_here(term, 300, BOOT, folder_ok=lambda p: True, session_ok=session_ok)

    def test_a_label_needs_no_transcript_because_the_conversation_is_in_a_database(self):
        self.assertEqual(ar.read_label({ar.LABEL: json.dumps(oc_label())})["session"], OC_SID)
        self.assertIsNone(ar.read_label({ar.LABEL: json.dumps(label(session=OC_SID) | {"transcript": None})}))

    def test_hook_notes_the_session_without_a_transcript(self):
        raw = json.dumps({"session_id": OC_SID, "cwd": "/work"})
        with mock.patch.object(ar, "find_agent", return_value=40), \
             mock.patch.object(ar, "hc_json", return_value={"state": "live", "pid": 30}), \
             mock.patch.object(ar, "placement", return_value="direct"), \
             mock.patch.object(ar, "remember") as remember:
            self.assertEqual(ar.hook("opencode", raw, env=self.ENV), "saved")
        remember.assert_called_once_with("t7", "opencode", OC_SID, "/work", None, 40, 30)

    def test_switching_conversations_replaces_the_note(self):
        noted = {"state": "live", "pid": 30, "labels": {ar.LABEL: json.dumps(oc_label(pid=40, shell=30))}}
        with mock.patch.object(ar, "find_agent", return_value=40), \
             mock.patch.object(ar, "hc_json", return_value=noted), \
             mock.patch.object(ar, "placement", return_value="direct"), \
             mock.patch.object(ar, "remember") as remember:
            same = json.dumps({"session_id": OC_SID, "cwd": "/work"})
            self.assertEqual(ar.hook("opencode", same, env=self.ENV), "already noted")
            other = json.dumps({"session_id": "ses_other000000000000", "cwd": "/work"})
            self.assertEqual(ar.hook("opencode", other, env=self.ENV), "saved")
        self.assertEqual(remember.call_args.args[2], "ses_other000000000000")

    def test_resume_command_keeps_launch_conditions_only(self):
        args = ar.flat(ar.launch_flags("opencode", [
            "--auto", "--standalone", "--server", "http://x", "--log-level", "debug", "--print-logs",
            "--prompt", "do work", "-c", "--continue", "-s", "ses_old000000000", "/some/folder"]))
        self.assertEqual(args, ["--auto", "--standalone", "--server", "http://x", "--log-level", "debug", "--print-logs"])
        self.assertEqual(ar.resume_argv(oc_label(args=["--auto"])), ["opencode", "--session", OC_SID, "--auto"])

    def test_the_decision_follows_the_services_answer(self):
        term = terminal(lbl=oc_label(shell=100))
        self.assertEqual(self.decide(term, lambda s: True)[1], None)
        self.assertEqual(self.decide(term, lambda s: False), (None, "no saved conversation to resume"))
        self.assertEqual(self.decide(term, lambda s: None), (None, ar.NO_SERVICE))

    def test_status_reports_a_missing_conversation_and_a_silent_service(self):
        term = terminal(fg_name="zsh", lbl=oc_label(shell=100))
        for answer, reason in ((False, "no saved conversation to resume"), (None, ar.NO_SERVICE)):
            todo, skipped = ar.plan([term], BOOT, folder_ok=lambda p: True, session_ok=lambda s: answer)
            self.assertEqual(todo, [])
            self.assertEqual(skipped[0]["reason"], reason)
        todo, _ = ar.plan([term], BOOT, folder_ok=lambda p: True, session_ok=lambda s: True)
        self.assertEqual(todo[0]["command"], f"cd '/work' && opencode --session {OC_SID}")

    def test_reading_the_service(self):
        cases = (
            ("echo '{\"id\":\"x\"}'\n", True),
            ("echo '{\"_tag\":\"SessionNotFoundError\"}'\necho 'HTTP 404 Not Found' >&2\nexit 1\n", False),
            ("echo 'Error: Server process exited with code 1' >&2\nexit 1\n", None),
            ("echo 'HTTP 500 Internal Server Error' >&2\nexit 1\n", None),
        )
        for script, answer in cases:
            with self.subTest(answer=answer), tempfile.TemporaryDirectory() as folder:
                fake_opencode(folder, script)
                with mock.patch.dict(os.environ, {"PATH": f"{folder}{os.pathsep}{os.environ['PATH']}"}):
                    self.assertIs(ar.opencode_saved(OC_SID), answer)

    def test_a_missing_opencode_command_is_not_a_missing_conversation(self):
        with tempfile.TemporaryDirectory() as folder, mock.patch.dict(os.environ, {"PATH": folder}):
            self.assertIsNone(ar.opencode_saved(OC_SID))

class LaunchFlagReadingTests(unittest.TestCase):
    def test_reads_a_running_programs_exact_words(self):
        words = [sys.executable, "-c", "import time; time.sleep(30)", "--setting-sources", "", "two words"]
        child = subprocess.Popen(words)
        self.addCleanup(child.wait)
        self.addCleanup(child.kill)
        for _ in range(100):  # exec has happened once the words are the program's own
            argv = ar.proc_argv(child.pid)
            if argv and argv[1:] == words[1:]:
                break
            time.sleep(0.02)
        self.assertEqual(argv[1:], words[1:])

    def test_a_process_that_is_gone_has_no_words(self):
        child = subprocess.Popen([sys.executable, "-c", ""])
        child.wait()
        self.assertIsNone(ar.proc_argv(child.pid))

    def remember(self, argv):
        with mock.patch.object(ar, "proc_argv", return_value=argv), \
             mock.patch.object(ar, "hc") as hc, \
             mock.patch.object(ar, "boot_id", return_value=BOOT):
            ar.remember("t7", "opencode", OC_SID, "/work", None, 40, 30, watcher=False)
        return json.loads(hc.call_args.args[0][2].split("=", 1)[1])

    def test_flags_that_could_not_be_read_are_marked(self):
        self.assertEqual(self.remember(["opencode", "--auto", "--prompt", "x"])["args"], ["--auto"])
        saved = self.remember(None)
        self.assertTrue(saved["args_unread"])
        self.assertNotIn("args", saved)

    def test_unread_flags_stop_the_resume_instead_of_widening_or_changing_what_runs(self):
        term = terminal(fg_name="zsh", lbl=oc_label(shell=100, args_unread=True))
        found, why = ar.decide_here(term, 300, BOOT, folder_ok=lambda p: True, session_ok=lambda s: True)
        self.assertEqual((found, why), (None, ar.UNREAD_FLAGS))
        _, skipped = ar.plan([term], BOOT, folder_ok=lambda p: True, session_ok=lambda s: True)
        self.assertEqual(skipped[0]["reason"], ar.UNREAD_FLAGS)

class OpenCodeHereTests(unittest.TestCase):
    """What a starting shell does, with a stand-in `opencode` answering for the service."""
    ALIVE = "echo '{}'\n"
    GONE = "echo '{\"_tag\":\"SessionNotFoundError\"}'\nexit 1\n"
    SILENT = "exit 1\n"

    def run_here(self, service):
        with tempfile.TemporaryDirectory() as folder:
            fake_opencode(folder, service)
            env = {"HC_TERMINAL_ID": "t1", "PATH": f"{folder}{os.pathsep}{os.environ['PATH']}", "XDG_RUNTIME_DIR": folder}
            term = terminal(pid=300, lbl=oc_label(shell=100, cwd=folder, args=["--auto"]))
            with mock.patch.dict(os.environ, env), \
                 mock.patch.object(ar, "hc_json", return_value=term), \
                 mock.patch.object(ar, "boot_id", return_value=BOOT), \
                 mock.patch.object(ar.os, "getppid", return_value=300), \
                 mock.patch.object(ar, "needs_own_job", return_value=False), \
                 mock.patch.object(ar, "_default_signals"), \
                 mock.patch.object(ar.os, "chdir"), \
                 mock.patch.object(ar.os, "execvp", side_effect=SystemExit) as start, \
                 mock.patch.object(ar, "retry_later") as later, \
                 mock.patch.object(ar, "log"), \
                 mock.patch("sys.stderr", new_callable=StringIO) as err, \
                 suppress(SystemExit):  # exec never returns
                ar.main(["agent_resume.py", "here"])
        return start, later, err.getvalue()

    def test_an_existing_conversation_comes_back_with_its_flags(self):
        start, later, _ = self.run_here(self.ALIVE)
        start.assert_called_once_with("opencode", ["opencode", "--session", OC_SID, "--auto"])
        later.assert_not_called()

    def test_a_deleted_conversation_is_not_created_anew(self):
        start, later, _ = self.run_here(self.GONE)
        start.assert_not_called()
        later.assert_not_called()

    def test_a_silent_service_leaves_the_shell_free_and_tries_again_later(self):
        start, later, said = self.run_here(self.SILENT)
        start.assert_not_called()
        later.assert_called_once_with()
        self.assertIn(ar.NO_SERVICE, said)

    def test_a_core_that_does_not_answer_is_tried_again_later(self):
        with mock.patch.dict(os.environ, {"HC_TERMINAL_ID": "t1"}), \
             mock.patch.object(ar, "hc_json", side_effect=ar.HcError("hc get: no core")), \
             mock.patch.object(ar, "retry_later") as later, \
             mock.patch.object(ar, "log"), \
             mock.patch("sys.stderr", new_callable=StringIO):
            self.assertEqual(ar.main(["agent_resume.py", "here"]), 0)
        later.assert_called_once_with()

    def test_unread_flags_are_said_and_not_tried_again(self):
        term = terminal(pid=300, lbl=oc_label(shell=100, cwd="/", args_unread=True))
        with mock.patch.dict(os.environ, {"HC_TERMINAL_ID": "t1"}), \
             mock.patch.object(ar, "hc_json", return_value=term), \
             mock.patch.object(ar, "boot_id", return_value=BOOT), \
             mock.patch.object(ar.os, "getppid", return_value=300), \
             mock.patch.object(ar, "retry_later") as later, \
             mock.patch.object(ar, "log"), \
             mock.patch("sys.stderr", new_callable=StringIO) as err:
            ar.main(["agent_resume.py", "here"])
        later.assert_not_called()
        self.assertIn(ar.UNREAD_FLAGS, err.getvalue())


class RetryTests(unittest.TestCase):
    WAITING = {"resumed": [], "skipped": [{"reason": ar.NO_SERVICE}]}
    DONE = {"resumed": [{"terminal": "t1"}], "skipped": [{"reason": "its folder is gone"}]}

    def retry(self, *passes):
        with tempfile.TemporaryDirectory() as run, mock.patch.dict(os.environ, {"XDG_RUNTIME_DIR": run}), \
             mock.patch.object(ar.time, "sleep"), \
             mock.patch.object(ar, "log"), \
             mock.patch.object(ar, "resume", side_effect=passes) as resume:
            return ar.retry(), resume.call_count

    def test_stops_once_nothing_is_waiting_any_more(self):
        self.assertEqual(self.retry(self.WAITING, self.DONE), ("done", 2))

    def test_gives_up_after_a_few_passes(self):
        passes = [self.WAITING] * len(ar.RETRY_DELAYS)
        self.assertEqual(self.retry(*passes), ("gave up", len(ar.RETRY_DELAYS)))

    def test_an_agent_that_exited_right_away_is_worth_another_pass(self):
        failed = {"resumed": [], "skipped": [{"reason": f"{ar.EXITED}: boom"}]}
        self.assertEqual(self.retry(failed, self.DONE), ("done", 2))

    def test_a_core_that_does_not_answer_is_worth_another_pass(self):
        self.assertEqual(self.retry(ar.HcError("hc ls: no core"), self.DONE), ("done", 2))

    def test_a_retry_that_cannot_start_stays_out_of_the_shells_way(self):
        with tempfile.TemporaryDirectory() as run, mock.patch.dict(os.environ, {"XDG_RUNTIME_DIR": run}), \
             mock.patch.object(ar.subprocess, "Popen", side_effect=OSError("no python")), \
             mock.patch.object(ar, "log") as log:
            ar.retry_later()
        self.assertIn("no python", log.call_args.kwargs["outcome"])

    def test_only_one_retry_runs(self):
        with tempfile.TemporaryDirectory() as run, mock.patch.dict(os.environ, {"XDG_RUNTIME_DIR": run}):
            held = ar.runtime_lock("retry")
            self.addCleanup(os.close, held)
            self.assertEqual(ar.retry(), "already retrying")

    def test_it_runs_outside_the_terminal_that_asked(self):
        env = {"HC_TERMINAL_ID": "t1", "HC_TERMINAL_SOCKET": "/run/t1", "HC_SCOPE": "t1"}
        started = []
        real = ar.subprocess.Popen

        def popen(argv, *args, **options):  # `ps` still has to run, to tell shells apart
            if argv[-1] != "retry":
                return real(argv, *args, **options)
            started.append(options)

        with tempfile.TemporaryDirectory() as run, mock.patch.dict(os.environ, {**env, "XDG_RUNTIME_DIR": run}), \
             mock.patch.object(ar.subprocess, "Popen", side_effect=popen):
            ar.retry_later()
            self.assertTrue(ar.shell_asked({"address": "t1", "pid": os.getppid()}))
        self.assertFalse(env.keys() & started[0]["env"].keys())
        self.assertTrue(started[0]["start_new_session"])


class Core:
    """A stand-in for herdr-core with one terminal that lost its agent. Typing starts the agent."""

    def __init__(self, lbl, not_up=0, on_send=lambda: None):
        self.shell = os.getpid()  # a live process, as a real shell would be
        self.term = terminal(fg_name="zsh", pid=self.shell, lbl=lbl)
        self.not_up = not_up
        self.on_send = on_send
        self.sent = []

    def json(self, args, **_):
        if args[0] == "ls":
            if self.not_up:
                self.not_up -= 1
                raise ar.HcError("hc ls: no core")
            return {"terminals": [{"address": "t1", "state": "live"}]}
        return self.term

    def run(self, args, **_):
        if args[0] == "send":
            self.sent.append(args[2])
            self.on_send()
            self.term = terminal(pid=self.shell, fg_pid=400, fg_name="opencode",
                                 lbl=oc_label(shell=self.shell, pid=400))
        return ""


class PassTests(unittest.TestCase):
    """Real passes, as `retry` runs them on its own while shells start up."""
    FIRST_SILENT = 'if [ -f "$0.asked" ]; then echo "{}"; exit 0; fi\ntouch "$0.asked"\nexit 1\n'

    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.folder = folder.name
        fake_opencode(self.folder, "echo '{}'\n")
        env = {"XDG_RUNTIME_DIR": self.folder, "PATH": f"{self.folder}{os.pathsep}{os.environ['PATH']}"}
        for patcher in (mock.patch.dict(os.environ, env),
                        mock.patch.object(ar, "boot_id", return_value=BOOT),
                        mock.patch.object(ar, "log")):
            patcher.start()
            self.addCleanup(patcher.stop)

    def lost_label(self, **extra):
        return oc_label(shell=100, cwd=self.folder, **extra)

    def asked(self, core):
        ar.note_asked("t1", core.shell)
        return core

    def run_with(self, core, action):
        with mock.patch.object(ar, "hc_json", side_effect=core.json), \
             mock.patch.object(ar, "hc", side_effect=core.run), \
             mock.patch.object(ar, "shell_age", return_value=100), \
             mock.patch.object(ar.time, "sleep"):
            return action()

    def test_retry_gets_a_second_pass_when_the_service_was_silent(self):
        fake_opencode(self.folder, self.FIRST_SILENT)
        core = self.asked(Core(self.lost_label(args=["--auto"])))
        self.assertEqual(self.run_with(core, ar.retry), "done")
        self.assertEqual(core.sent, [f"cd '{self.folder}' && opencode --session {OC_SID} --auto"])

    def test_retry_gets_a_second_pass_when_the_core_was_not_up(self):
        core = self.asked(Core(self.lost_label(), not_up=1))
        self.assertEqual(self.run_with(core, ar.retry), "done")
        self.assertEqual(len(core.sent), 1)

    def test_retry_types_nothing_into_a_shell_that_may_still_start_the_agent_itself(self):
        core = Core(self.lost_label())
        self.assertEqual(self.run_with(core, ar.retry), "done")
        self.assertEqual(core.sent, [])
        out = self.run_with(core, lambda: ar.resume(unattended=True))
        self.assertEqual(out["skipped"][0]["reason"], ar.NOT_ASKED)

    def test_a_shell_that_asked_before_the_restart_is_not_this_shell(self):
        core = Core(self.lost_label())
        ar.note_asked("t1", os.getppid())
        self.run_with(core, ar.retry)
        self.assertEqual(core.sent, [])

    def test_retry_never_drops_a_flag_to_type_the_command(self):
        core = self.asked(Core(self.lost_label(args=["--log-level", "it's"])))
        self.assertEqual(self.run_with(core, ar.retry), "done")
        self.assertEqual(core.sent, [])
        out = self.run_with(core, lambda: ar.resume(unattended=True))
        self.assertEqual(out["skipped"][0]["reason"], ar.UNTYPABLE_FLAGS)

    def test_a_person_pressing_the_key_still_gets_the_session_without_that_flag(self):
        core = Core(self.lost_label(args=["--log-level", "it's"]))
        self.run_with(core, ar.resume)
        self.assertEqual(core.sent, [f"cd '{self.folder}' && opencode --session {OC_SID}"])

    def test_a_pass_leaves_the_claim_to_the_agent_it_starts(self):
        held = []  # Kiro's launcher claims the session itself once the typed command runs
        core = self.asked(Core(self.lost_label(), on_send=lambda: held.append(ar.claim_holder("opencode", OC_SID))))
        self.run_with(core, ar.retry)
        self.assertEqual(held, [None])


class WatchTests(unittest.TestCase):
    """A shutdown ends agents and terminals a moment apart: only a shell that outlives the
    grace means the person quit."""

    def watch(self, shell_alive):
        waited = []

        def alive(pid):
            self.assertEqual(waited, [ar.EXIT_GRACE])  # judged after the grace, not before
            return shell_alive

        term = terminal(pid=30, lbl=label(pid=40, shell=30))
        with tempfile.TemporaryDirectory() as run, mock.patch.dict(os.environ, {"XDG_RUNTIME_DIR": run}), \
             mock.patch.object(ar, "wait_exit"), \
             mock.patch.object(ar.time, "sleep", side_effect=waited.append), \
             mock.patch.object(ar, "alive", side_effect=alive), \
             mock.patch.object(ar, "hc_json", return_value=term), \
             mock.patch.object(ar, "hc") as hc:
            return ar.watch("t1", 40, 30), hc

    def test_a_shell_gone_during_the_grace_keeps_the_note(self):
        outcome, hc = self.watch(False)
        self.assertEqual(outcome, "terminal went with it")
        hc.assert_not_called()

    def test_a_shell_that_outlives_the_grace_means_the_person_quit(self):
        outcome, hc = self.watch(True)
        self.assertEqual(outcome, f"forgot {SID}")
        hc.assert_called_once_with(["label", "t1", f"{ar.LABEL}="])


class ProcessStartTests(unittest.TestCase):
    """A stale claim must not match a new process that got the same pid."""

    def test_shell_age(self):
        self.assertLess(ar.shell_age(os.getpid()), 86400 * 30)

    def test_a_claim_from_a_process_that_had_this_pid_before_is_stale(self):
        with tempfile.TemporaryDirectory() as runtime, mock.patch.dict(os.environ, {"XDG_RUNTIME_DIR": runtime}):
            self.assertTrue(ar.claim("opencode", OC_SID))
            self.assertEqual(ar.claim_holder("opencode", OC_SID), os.getpid())
            Path(ar._claim_path("opencode", OC_SID)).write_text(f"{os.getpid()} 1")
            self.assertIsNone(ar.claim_holder("opencode", OC_SID))


class ClaudeRegistryTests(unittest.TestCase):
    def test_reads_a_running_session(self):
        with tempfile.TemporaryDirectory() as home:
            os.makedirs(os.path.join(home, ".claude", "sessions"))
            os.makedirs(os.path.join(home, ".claude", "projects", "-work"))
            Path(home, ".claude", "projects", "-work", f"{SID}.jsonl").write_text("{}\n")
            Path(home, ".claude", "sessions", "40.json").write_text(json.dumps(
                {"pid": 40, "sessionId": SID, "cwd": "/work", "kind": "interactive"}))
            self.assertEqual(ar.claude_session_of(40, home), (SID, "/work", os.path.join(home, ".claude", "projects", "-work", f"{SID}.jsonl")))
            self.assertIsNone(ar.claude_session_of(41, home))
            Path(home, ".claude", "sessions", "40.json").write_text(json.dumps(
                {"pid": 40, "sessionId": SID, "cwd": "/work", "kind": "background"}))
            self.assertIsNone(ar.claude_session_of(40, home))


if __name__ == "__main__":
    unittest.main()
