// Agent resume for OpenCode. OpenCode's server part runs in one service shared by every
// terminal, so only this TUI part knows which conversation a terminal shows. It tells
// agent_resume.py, the way Claude Code's SessionStart hook does for Claude Code.
import { spawn } from "node:child_process"
import { dirname, join } from "node:path"
import { fileURLToPath } from "node:url"

const SCRIPT = join(dirname(fileURLToPath(import.meta.url)), "agent_resume.py")
const POLL_MS = 1000
const RETRY_DELAYS_MS = [1000, 3000, 10000]

function note(session_id, cwd) {
  return new Promise((resolve) => {
    const child = spawn("python3", [SCRIPT, "hook", "opencode"], { stdio: ["pipe", "pipe", "ignore"] })
    let outcome = ""
    child.stdout.on("data", (chunk) => { outcome += chunk })
    child.on("error", () => resolve(false))
    child.on("close", () => resolve(outcome !== "" && !outcome.startsWith("error")))
    child.stdin.on("error", () => {})
    child.stdin.end(JSON.stringify({ session_id, cwd }))
  })
}

function setup(api) {
  if (!process.env.HC_TERMINAL_ID) return

  let noted
  let wanted
  let attempt = 0
  let retryAt = 0
  let busy = false

  function shownSession() {
    const route = api.ui.router.current()
    if (!route || route.type !== "session" || !route.sessionID) return ""
    // A subagent's view belongs to its root conversation.
    try { return api.data.session.root(route.sessionID) || route.sessionID } catch { return route.sessionID }
  }

  async function sync() {
    if (busy) return
    const session = shownSession()
    if (session !== wanted) {
      wanted = session
      attempt = 0
      retryAt = 0
    }
    if (!wanted || wanted === noted || Date.now() < retryAt) return
    const info = api.data.session.get(wanted)
    busy = true
    const target = wanted
    const ok = await note(target, info?.location?.directory || process.cwd())
    busy = false
    if (ok) {
      noted = target
      return
    }
    retryAt = Date.now() + RETRY_DELAYS_MS[Math.min(attempt, RETRY_DELAYS_MS.length - 1)]
    attempt += 1
  }

  const timer = setInterval(() => void sync(), POLL_MS)
  void sync()
  return () => clearInterval(timer)
}

export default { id: "herdr-agent-resume", setup }
