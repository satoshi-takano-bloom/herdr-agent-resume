import assert from "node:assert/strict"
import { chmodSync, mkdtempSync, readFileSync, rmSync, writeFileSync, existsSync } from "node:fs"
import { tmpdir } from "node:os"
import { join } from "node:path"
import { afterEach, beforeEach, test } from "node:test"
import plugin from "../mods/agent-resume/tui.js"

const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms))
const STAND_IN = `#!/bin/sh
cat >> "$NOTES"
echo >> "$NOTES"
if [ "$(wc -l < "$NOTES")" -le "$FAILURES" ]; then echo "error: hc didn't answer"; else echo saved; fi
`

let folder, stop, path

function api(route, roots = {}) {
  return {
    ui: { router: { current: () => route.now } },
    data: {
      session: {
        root: (id) => roots[id],
        get: (id) => ({ location: { directory: `/work/${id}` } }),
      },
    },
  }
}

function notes() {
  if (!existsSync(process.env.NOTES)) return []
  return readFileSync(process.env.NOTES, "utf8").trim().split("\n").filter(Boolean).map((line) => JSON.parse(line))
}

beforeEach(() => {
  folder = mkdtempSync(join(tmpdir(), "agent-resume-tui-"))
  writeFileSync(join(folder, "python3"), STAND_IN)
  chmodSync(join(folder, "python3"), 0o755)
  path = process.env.PATH
  process.env.PATH = `${folder}:${path}`
  process.env.NOTES = join(folder, "notes")
  process.env.FAILURES = "0"
  process.env.HC_TERMINAL_ID = "t1"
})

afterEach(() => {
  stop?.()
  stop = undefined
  process.env.PATH = path
  delete process.env.HC_TERMINAL_ID
  rmSync(folder, { recursive: true, force: true })
})

test("notes the shown conversation once, with its folder", async () => {
  stop = plugin.setup(api({ now: { type: "session", sessionID: "ses_one" } }))
  await sleep(1300)
  assert.deepEqual(notes(), [{ session_id: "ses_one", cwd: "/work/ses_one" }])
})

test("a subagent's view belongs to its root conversation", async () => {
  stop = plugin.setup(api({ now: { type: "session", sessionID: "ses_child" } }, { ses_child: "ses_root" }))
  await sleep(300)
  assert.deepEqual(notes(), [{ session_id: "ses_root", cwd: "/work/ses_root" }])
})

test("switching conversations notes the new one", async () => {
  const route = { now: { type: "session", sessionID: "ses_one" } }
  stop = plugin.setup(api(route))
  await sleep(200)
  route.now = { type: "session", sessionID: "ses_two" }
  await sleep(1200)
  assert.deepEqual(notes().map((note) => note.session_id), ["ses_one", "ses_two"])
})

test("a screen without a conversation notes nothing", async () => {
  stop = plugin.setup(api({ now: { type: "home" } }))
  await sleep(300)
  assert.deepEqual(notes(), [])
})

test("does nothing outside a Herdr terminal", async () => {
  delete process.env.HC_TERMINAL_ID
  stop = plugin.setup(api({ now: { type: "session", sessionID: "ses_one" } }))
  await sleep(300)
  assert.deepEqual(notes(), [])
})

test("tries again when noting failed", async () => {
  process.env.FAILURES = "1"
  stop = plugin.setup(api({ now: { type: "session", sessionID: "ses_one" } }))
  await sleep(2600)
  assert.equal(notes().length, 2)
})
