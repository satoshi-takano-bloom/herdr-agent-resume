# Contributing

Bug reports, test runs on macOS, and small focused changes are all welcome.
Agent resume is a community mod, not part of Herdr: problems in Herdr's own
windows, terminals or mod API belong upstream, and problems in how Claude Code
or Codex resume a session belong to those tools.

## Reporting a problem

Open an issue with the bug form. It asks for your Herdr, agent and shell
versions, and for the output of
`python3 ~/.config/herdr-app/mods/agent-resume/agent_resume.py status`.
Session ids are fine to share; leave out conversation text, and mask folder
names you'd rather not show.

On macOS? The code paths there (`ps`, `sysctl`, polling instead of `pidfd`)
have never run. A "Tried it on my machine" report, even when everything
worked, helps a lot.

## Setting up

You need Python 3.9 or newer (standard library only) and Node 22 or newer for
the toast tests. No packages, no build step. To try a change in the app,
install your checkout over the current one:

```sh
python3 scripts/install.py --dry-run --update
python3 scripts/install.py --update          # add --auto to try automatic resume
```

The installer refuses to overwrite installed files that differ from its
record, so edit in `mods/agent-resume/` and reinstall rather than editing the
installed copy.

### Trying it without touching your own sessions

Run a second herdr-core with its own session name and app home, and
stand-in `claude`/`codex` scripts that pipe a SessionStart payload into
`agent_resume.py hook` the way the real agents do:

```sh
HC_SESSION=agres-test HERDR_APP_HOME=/tmp/agres-app hc server start
HC_SESSION=agres-test hc run --keep --bg -- /bin/fish
HC_SESSION=agres-test hc server stop     # then start again: the terminal comes back as a shell
```

Unset `HC_TERMINAL_ID`, `HC_TERMINAL_SOCKET` and `HC_SCOPE` first when you run
these from inside a Herdr terminal, or `hc` talks to your real core.

## Where things live

- `mods/agent-resume/agent_resume.py`: the hook, the watcher, the resume pass
  and `here` (automatic resume). Decisions are pure functions (`plan`,
  `decide_here`, `lost`, `at_shell`, `command`, `placement`); keep new ones
  that way so they can be tested without herdr-core.
- `mods/agent-resume/main.js`: the Ctrl+Shift+R popup.
- `mods/agent-resume/summary.js`: the toast text, no `herdr` imports.
- `mods/agent-resume/tui.js`: OpenCode's TUI part. It tells `agent_resume.py`
  which conversation a terminal shows; OpenCode's server part can't, because
  one service serves every terminal.
- `scripts/install.py`: the installer, including the shell startup lines for
  `--auto`. Its `FILES` list must name every file in `mods/agent-resume/`
  (CI checks this).
- `RECIPE.md`, `preview.png`: the herdr.dev listing. Keep them in step with
  what the mod does.

## Before opening a pull request

```sh
npm test                           # node toast tests + Python unit tests
herdr-app check mods/agent-resume
```

CI runs the tests on Node 22/24 and Python 3.10/3.13 on Linux, 3.9/3.13 on macOS,
syntax-checks the fish, bash and zsh startup lines, and checks the JSON and
file lists. It can't run Herdr or the agents, so say in the PR what you tried
for real, on which OS, shell and versions.

Some guidelines:

- A behavior change comes with a test in `tests/`.
- The hook must never get in the agent's way: no output, short timeouts,
  every error swallowed into the log. The same goes for `here` and the shell
  that runs it: what needs to wait goes to a detached `retry`.
- Nothing typed or run may come from unchecked input: session ids match
  `SESSION_RE`, folders match `TYPABLE_PATH_RE` before they're typed.
- No network requests, and no changes to files outside the app home,
  `~/.claude/settings.json`, `~/.codex/hooks.json`, `~/.grok/hooks/agent-resume.json`,
  `~/.gemini/config/hooks.json`, `~/.kiro/hooks/agent-resume.json`,
  `~/.pi/agent/extensions/herdr-agent-resume.js`, `~/.config/opencode/cli.json`
  and (with `--auto`) the shell startup files, always backed up first.
- Update `README.md` and `RECIPE.md` when users would notice the change.

By contributing you agree your work is released under the [MIT License](LICENSE).
