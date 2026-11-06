# Agent resume

Restarting the machine kills every agent. Herdr brings each terminal back as a bare shell in its last folder, but it never runs the program again, so the chat that was running there is gone. This mod brings it back: a small session hook notes which session runs in which terminal, and Ctrl+Shift+R types that agent's resume command into each terminal that lost its agent and sits at a shell. A toast says what came back.

![Agent resume](preview.png)

(A real screenshot of a demo Herdr window after a core restart and Ctrl+Shift+R: Claude Code and Codex back on their conversations, the toast at the bottom right. The Claude plan was masked. The mod itself shows only the short popup and the toast.)

## What you see

- Nothing until you press Ctrl+Shift+R. Installed with `--auto`, a restored terminal shows one dim line, `agent-resume: claude --resume <id> in <folder>`, and the agent opens on its old conversation as soon as the shell is up, with no popup or toast.
- Then a popup pill, 340×40 px, centered at the top of the window 44 px down: the theme's `popover` background, a 1 px `border` outline, 10 px corners, 14 px side padding. Inside, 10 px apart: a 12 px braille spinner (`⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏`, 80 ms a frame) in `accent`, and 13 px `foreground` text, "Looking for agents to resume…", truncated with an ellipsis. It closes by itself when the pass ends: within a second when there's nothing to resume, about 5 s after typing when there is (it checks the agents came up).
- In each terminal that gets its agent back, the shell shows the typed command, `cd '<folder>' && claude --resume <id>` (or `codex resume <id>`, `grok --resume <id>`, `agy --conversation <id>`, `pi --session <id>`, `python3 <mod>/agent_resume.py launch-kiro <id>`), and the agent opens on its old conversation, idle.
- Then a toast, one line of body:
  - success, "Resumed 3 agents", body the terminals' `space / tab`, 5 s;
  - warning, "Resumed 2 of 3 agents", body "Not resumed: web / 2: something else is running there", 8 s;
  - warning, "Couldn't resume 1 agent", body the reasons, 8 s (an agent that exited right away reads "exited right away: <its last line>");
  - info, "No agent to resume", body "No terminal lost its agent.", 3 s;
  - info, "Already resuming agents" for a second press while a pass runs;
  - error, "Couldn't resume agents" with the error.

## How it behaves

- **Noting sessions.** A session hook runs `agent_resume.py hook claude|codex|grok|agy|pi|kiro|opencode`. Inside a Herdr terminal (`HC_TERMINAL_ID` set), it sets the label `agent-resume` on that terminal to compact JSON: `agent`, `session`, `cwd`, `transcript`, `args`, `pid` (the agent), `shell` (the terminal's program), `boot` (Linux `boot_id`, macOS `kern.boottime`) and `at`.
  - Claude Code, Codex and Grok send Claude-shaped JSON (`session_id`, `transcript_path`, `cwd`, `hook_event_name`). Grok also sends camelCase (`sessionId`, `transcriptPath`, `hookEventName`) and `GROK_SESSION_ID`.
  - Antigravity sends `conversationId`, `workspacePaths` and `transcriptPath`, on `SessionStart` and on `PreInvocation`. The hook prints `{}`, because Antigravity reads stdout as JSON.
  - Pi has no shell hook. `scripts/pi_extension.js` is installed at `~/.pi/agent/extensions/herdr-agent-resume.js` and sends the same Claude-shaped JSON from `session_start` and `agent_end`, once the session file exists.
  - Kiro V3 sends `session_id` and `cwd` on `SessionStart` and `UserPromptSubmit`. Its global hooks apply to the built-in agent and custom profiles. Session metadata lives at `~/.kiro/sessions/<workspace-hash>/<session>/session.json`; existing buckets are searched before computing the single-workspace hash. Only V3 `sess_` ids are accepted, and resume explicitly selects `--v3`. V3 defers hooks until a prompt, including on resume. Both auto-resume and the typed `launch-kiro <session>` wrapper therefore run Kiro as a foreground child, record its new PID and shell, and clear its label on exit. Quick startup failures restore the old label for retry; a core shutdown leaves the label persisted. The nearest Kiro helper can be promoted through its runtime/launcher chain, stopping at shells and the first CLI launcher; normal ancestry checks then reject nested agents of every kind, including Kiro inside Kiro. Foreground state never substitutes an unrelated PID. Grok and Antigravity use their own transcript fallbacks (`~/.grok/sessions/<encoded-cwd>/<id>/updates.jsonl`, `~/.gemini/antigravity-cli/brain/<id>/.system_generated/logs/transcript.jsonl`).
  - OpenCode V2 has no shell hook, and its server part runs in one service shared by every terminal. `mods/agent-resume/tui.js`, listed in `plugins` of `~/.config/opencode/cli.json`, runs inside each TUI, polls the shown conversation every second and sends `session_id` and `cwd`, so `/sessions` changes the note. `transcript` is `null`: the conversation is in a database behind the service. OpenCode creates a conversation at the first message, so a TUI nobody typed in isn't noted.
  - `args` are the agent's launch flags worth keeping, read from `/proc/<pid>/cmdline` on Linux and `sysctl KERN_PROCARGS2` on macOS, and filtered through `KEEP_FLAGS`: an allowlist per agent with each flag's number of values, limited to flags that agent's resume command accepts. Prompts, session pickers (`--resume`, `--continue`, `--session-id`), `-p`/`--print`, `--worktree` and unknown flags are dropped. A label read back is filtered again, so a label can never carry more.
  - Claude Code runs it on start, `/resume`, `/clear` and compact.
  - Codex runs it at the first turn of a session, not when it opens, so a Codex window nobody typed into isn't noted. There's nothing to resume in it anyway.
- It doesn't note:
  - Claude subagents (`agent_id` in the hook input);
  - Codex sub-threads (`CODEX_THREAD_ID` differs from the session);
  - sessions without a transcript, except OpenCode's;
  - an agent with another agent between it and the terminal's program (a `claude -p` run by an agent's tool);
  - an agent the terminal's program isn't above at all (tmux, ssh).
- **Forgetting quit sessions.** The hook starts a detached watcher per agent process (`agent_resume.py watch`, a per-pid lock keeps it to one). It waits for the agent to exit (`pidfd` on Linux, else a 2 s poll), waits 10 s more, and if the terminal's program is still the same live process, removes the label: the person quit the agent. If that program died too (reboot, logout, core stop, pane closed), the label stays. A core stop also stops the watchers with the terminals.
- **A pass** (Ctrl+Shift+R, or `agent_resume.py resume`):
  - reads every live local terminal (`hc ls`, then `hc get` 8 at a time);
  - validates saved session data before checking paths or sorting terminals; an invalid label is reported as "invalid session label: <reason>" for that terminal, while valid terminals continue. Automatic resume reports the same problem in its terminal and log;
  - a labeled terminal lost its agent when its label is from another boot, or its program isn't the process the agent ran under;
  - resumes, newest first, each lost one whose foreground is the terminal's own shell (fish, bash, zsh, sh, dash, ksh), typing the command with `hc send` 150 ms apart, after the shell has run at least 1.5 s. The command is the agent's resume words, then its `args`, each in single quotes unless plain; a flag with a value that can't go between single quotes is left out;
  - 5 s after the last send, re-reads each: a terminal back at its shell whose label is still lost failed, reported as "exited right away: <the last line before the prompt>" (`hc read`);
  - waits up to 12 s, re-reading every 0.5 s, for restored shells that haven't started yet;
  - skips, with the reason in the toast: something else running there; its folder gone; its folder name not typable (a quote, a backslash, a control character); no saved conversation (the transcript doesn't exist, or OpenCode's service says so); OpenCode's service not answering; launch flags that couldn't be read; a session still open in another terminal or resumed once already in this pass (those two aren't shown).
- **Automatic resume** (opt-in, `install.py --auto`). A marked block in the shell's startup (fish: `conf.d/agent-resume.fish`; bash: end of `.bashrc`; zsh: end of `.zshrc`) runs `agent_resume.py here` once, at the first prompt of an interactive shell with `HC_TERMINAL_ID` set and `AGENT_RESUME_AUTO` not `0`. It:
  - reads this terminal (`hc get`, 3 s), and goes on only if the shell is the terminal's own program and its label is lost (as in a pass), its folder exists and its conversation exists (the transcript, or `opencode api get /api/session/<id>`, 10 s);
  - claims the session in `$XDG_RUNTIME_DIR/herdr-agent-resume-<uid>/claim-<agent>-<session>` (pid and start time). A per-session lock covers checking the old owner and writing the new one, and also guards failed-start cleanup: concurrent shells cannot claim the same session while its owner is still being written. The lock is released before launching the agent; the saved process identity protects the claim while the agent runs;
  - prints the dim line, `chdir`s to the session's folder and starts the agent with its `args`, nothing typed. When the shell gave it its own process group (bash), it `exec`s the agent. When it shares the shell's (fish runs prompt events without job control), it forks the agent into a new process group, makes that the terminal's foreground (`tcsetpgrp`), waits, and hands the terminal back, so Herdr sees the agent in front; Ctrl+Z is resumed at once there, as there's no shell job to stop;
  - exits with the agent's code. On a non-zero code other than 130/143, the block runs `agent_resume.py after <code>`: if the agent ran under 10 s and no hook noted the session, it prints one yellow line ("claude exited right away (code 1), so session … didn't come back. It will be tried again shortly.") and logs `failed`;
  - otherwise does nothing and prints nothing, except one line when the agent can't be started, its flags couldn't be read or OpenCode's service didn't answer.
  - leaves what failed only for now to a detached `agent_resume.py retry`: `hc` not answering, OpenCode's service not answering, or an agent that exited right away. It runs a pass 15, 45 and 105 s later, one `retry` at a time, and stops when no terminal is left waiting for one of those. Its passes skip a session whose flag can't be typed, rather than drop the flag.
  A pass skips, without saying so, a terminal whose foreground is already that agent or `agent_resume.py here`, or whose session is claimed by a live process ("resumed by its shell").
- Before typing, a pass claims the session the way `here` does, then reads the terminal again, and gives the claim back when the pass ends: an agent that opened in that moment would take the typed text as a prompt.
- After a resumed agent starts, its hook notes it again with the new shell, so a second press does nothing.
- Only one pass runs at a time; a second press reports "Already resuming agents".

## Built from

- `mods/agent-resume/mod.json`: `{"place": "none", "title": "Agent resume", "overlay": {"kind": "popup", "at": "top", "offset": 44, "width": 340, "height": 40, "frame": "none"}}`. Not a pane or a layout block: `mod.open:agent-resume` opens it as that popup.
- `keys.json`: `{"global": {"ctrl-shift-r": "mod.open:agent-resume"}}`.
- `main.js`: the popup view. `init` runs `exec("python3", [appHome() + "/mods/agent-resume/agent_resume.py", "resume"])` in `cx.spawn`, parses the JSON, posts `notify(summary(out))` and always ends with `closeOverlay(props().overlay)`. `render` is an `h_flex` with a `div().frames(SPIN, 80)` spinner and a `truncate()` label. Herdr functions: `exec`, `appHome`, `theme`, `props`, `closeOverlay`, `notify`.
- `summary.js`: the toast text, no `herdr` imports.
- `agent_resume.py`: Python 3.8+, standard library only. Commands `hook`, `watch`, `resume`, `status` (a dry run), `here` (automatic resume), `adopt` (notes running Claude Code sessions from `~/.claude/sessions/<pid>.json`). It runs `hc` (`ls`, `get`, `label`, `send`), reads `/proc` (or `ps`, `sysctl`), and writes only `data/agent-resume/log.jsonl` (trimmed to 256 KiB) and lock files under `$XDG_RUNTIME_DIR`. It never reaches the network.
- **Installing also needs the hooks.** For each agent whose directory already exists, append its hook after the ones already there (Codex trusts hooks by position), backing the file up first. Claude Code and Codex (and Grok, whose file is Claude-shaped) get one `SessionStart` group:

  ```json
  { "hooks": [{ "type": "command", "command": "python3 '/home/you/.config/herdr-app/mods/agent-resume/agent_resume.py' hook claude", "timeout": 10 }] }
  ```

  (`hook codex` in Codex's file, `hook grok` in `~/.grok/hooks/agent-resume.json`.) Antigravity gets a named `agent-resume` block in `~/.gemini/config/hooks.json` with `SessionStart` and `PreInvocation`. Kiro V3 gets global `SessionStart` and `UserPromptSubmit` hooks in `~/.kiro/hooks/agent-resume.json`. The installer never creates or edits Kiro profiles or changes `chat.defaultAgent`; uninstall removes only this mod's global hooks. Pi gets the extension above, with the installed script path filled in. `scripts/install.py` does all of it: copy, key, the hooks, `adopt`; `--update`, `--uninstall`, `--dry-run`; `--auto` / `--no-auto` add or remove the shell startup block (backed up first; `--update` refreshes a block that's there). An `agent-resume.json` that holds nothing else is deleted on uninstall.

## Watch out

- **The mod runs nothing by itself.** The shipped layouts run no mod in the background; the person presses Ctrl+Shift+R after Herdr opens, or installs with `--auto` so the shell does it.
- Codex shows "Hooks need review" the next time it starts after install. Until the person trusts the hook, Codex sessions aren't noted.
- `hc send` from inside a Herdr terminal wraps the text in `<hc from="tN">…</hc>` for agents. The pass drops `HC_TERMINAL_ID` and `HC_SCOPE` for its sends, so they're plain even when run from a terminal.
- The `cd` matters. `claude --resume <id>` finds a session only from its project folder, and Codex works in whatever folder it's started in. A restored shell starts in the terminal's last folder, which isn't always the session's: the agent may have moved into a worktree, or the terminal was started elsewhere.
- Session ids are checked against `^[A-Za-z0-9][A-Za-z0-9._-]{7,127}$` before they're typed (the extra `.` and `_` are for Pi custom ids and Kiro's `sess_` ids); folders go between single quotes.
- The hook must never get in the agent's way: it prints nothing, gives `hc` 3 s, and swallows every error into the log.
- `claude --resume` may continue under a new id; the hook follows it.
- A folder Claude Code hasn't been told to trust stops at its trust prompt.
- Labels live in herdr-core with the terminal, so removing a terminal (`hc rm`) or closing its pane forgets the session too.
- Tested on Linux with Herdr app 0.1.10, herdr-core 0.1.15, Claude Code 2.1.316, Codex 0.165.0 and fish 4. Tested on macOS with OpenCode 2.2.5 and zsh.

## Done when

- `herdr-app check mods/agent-resume` passes, `npm test` passes (Python and node tests), `herdr-app status` shows `errors: []`.
- Starting `claude` in a Herdr terminal sets its `agent-resume` label; `/exit` removes it about 10 s later.
- A Codex session's label appears after its first message.
- An OpenCode session's label appears within a second or two of opening a conversation, and changes with `/sessions`.
- `hc server stop`, then `hc server start` (run outside Herdr): each terminal comes back as a shell; Ctrl+Shift+R types the resume commands, the agents open on their conversations, and the toast reads "Resumed N agents". Pressing again says "No agent to resume".
- With `--auto`: after the stop and start, each restored fish/bash/zsh shell starts its agent with no key press; Ctrl+Shift+R then says "No agent to resume"; `--no-auto` leaves the startup files as they were.
- A session quit before the stop doesn't come back. A terminal running something else is skipped with that reason, and resumed on the next press once it's free.
