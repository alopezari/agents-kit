import { expect, mock, test } from 'claude-code/testing'
import { FEATURES } from '../hooks/features.js'

const KIT = '/home/.agents'
const ROOT = '/work/repo'
// The session sits below the repository root, so a run in the wrong directory shows.
const SESSION_CWD = '/work/repo/hooks'
const HEAD = 'abc1234def5678'
const REPORT = '/work/repo/.git/agents/verify-repo-feature.md'
const REPORT_MTIME = new Date(2026, 8, 30, 14, 5, 9).getTime()
const VERIFY_ARGV = ['python3', KIT + '/hooks/stop_checks.py', 'verify']
const CHECKED_NOTHING = 'verify passed, but it checked nothing: no check printed a ran: line\n'
const PANE = {
  plugin: 'kit',
  component: 'Pane',
  requestId: 'flow',
  viewport: { columns: 120, rows: 40 },
  props: {
    title: 'flow',
    isFocused: true,
    bodyColumns: 100,
    placement: 'dock',
    scroll: { offset: 0, bodyRows: 30 },
    view: {},
  },
} as const

type Run = { exitCode: number; stdout?: string; stderr?: string } | { deny: string }

// A repository on a feature branch, pushed, with green CI, two stamps current and a verify report.
function repo(overrides: Record<string, Run> = {}): Record<string, Run> {
  return {
    'git rev-parse --show-toplevel': { exitCode: 0, stdout: ROOT + '\n' },
    'git branch --show-current': { exitCode: 0, stdout: 'feature\n' },
    'git rev-parse HEAD': { exitCode: 0, stdout: HEAD + '\n' },
    [KIT + '/bin/reports brief']: {
      exitCode: 0,
      stdout: 'phase: validate\nspec           Add the pane\n               /x/spec.md\nRead one in full with cat <path>, or all of them with: ~/.agents/bin/reports\n',
    },
    [KIT + '/bin/reports path verify']: { exitCode: 0, stdout: REPORT + '\n' },
    'python3 /home/.agents/hooks/review_stamp.py check --kind verify': { exitCode: 0 },
    'python3 /home/.agents/hooks/review_stamp.py check --kind verify-empty': { exitCode: 1 },
    'python3 /home/.agents/hooks/review_stamp.py check --kind review': { exitCode: 0 },
    'python3 /home/.agents/hooks/review_stamp.py check --kind validate': { exitCode: 1 },
    'python3 /home/.agents/hooks/review_stamp.py check --kind staging': { exitCode: 1 },
    'git rev-parse --verify --quiet @{u}': { exitCode: 0, stdout: HEAD + '\n' },
    ['git merge-base --is-ancestor ' + HEAD + ' @{u}']: { exitCode: 0 },
    'git status --porcelain': { exitCode: 0, stdout: '' },
    [KIT + '/bin/ci-wait --sha ' + HEAD + ' --once --no-log']: { exitCode: 0, stdout: 'tests: passed\n' },
    ...overrides,
  }
}

const REPORT_TEXT = '# Verify: PASS\n\n```\nran: tests/a.py\nran: tests/test_failed_login.py\nskipped: ruff: not installed\nwarning: slow\nerror: none\n```\n'

// Stubs everything the mod reaches outside itself; returns the argv and options of every process it ran.
function stub(on, runs: Record<string, Run>, { surfaces = ['terminal'], report = REPORT_TEXT, process = null, panes = () => ['flow'], cwd = { value: SESSION_CWD } } = {}) {
  const ran: string[] = []
  const cwds: string[] = []
  const timeouts: number[] = []
  const opened: object[] = []
  const clock = mock.clock(on)
  on('env.get', () => ({ value: '/home' }))
  on('session.cwd', () => (typeof cwd === 'function' ? cwd() : cwd))
  on('session.surfaces', () => ({ value: surfaces }))
  on('session.usage', () => ({ value: { context: { window: 200000, tokens: 50000, percent: 25 } } }))
  on('command.register', () => ({ value: undefined }))
  on('ui.open', ($, e) => {
    opened.push(e)
    return { value: { isPlaced: true } }
  })
  on('ui.panes', () => {
    const ids = panes()
    return ids ? { value: ids.map((id) => ({ id, title: id, isShown: true, isFocused: false, isPlaced: true })) } : { deny: 'no panes here' }
  })
  on('fs.exists', ($, e) => ({ value: report !== '' && e.path === REPORT }))
  on('fs.stat', () => ({ value: { kind: 'file', size: report.length, mtimeMs: REPORT_MTIME } }))
  on('fs.read', () => ({ value: report }))
  on('process.run', process ?? (($, e) => {
    const key = e.argv.join(' ')
    ran.push(key)
    cwds.push(e.init?.cwd)
    timeouts.push(e.init?.timeoutMs)
    const answer = runs[key]
    if (!answer) return { deny: 'unexpected command: ' + key }
    if ('deny' in answer) return answer
    return { value: { exitCode: answer.exitCode, stdout: answer.stdout ?? '', stderr: answer.stderr ?? '' } }
  }))
  return { ran, cwds, timeouts, opened, clock }
}

async function openPane($, clock) {
  await $.command.run({ command: 'flow', args: '' })
  await clock.settle()
  return $.ui.mount({ ...PANE, surface: 'terminal' })
}

function nodes(root, type) {
  const found = []
  const walk = (node) => {
    if (!node || typeof node !== 'object') return
    if (node.type === type) found.push(node)
    for (const child of node.children ?? []) walk(child)
  }
  walk(root)
  return found
}

const texts = async (ui) =>
  nodes(await ui.find({ type: 'Box' }), 'Text')
    .map((node) => (node.children ?? []).filter((c) => typeof c === 'string').join(''))
    .join('\n')

const buttons = async (ui) => nodes(await ui.find({ type: 'Box' }), 'Button').map((node) => node.props?.label ?? node.label)

const isBold = async (ui, pattern) =>
  nodes(await ui.find({ type: 'Box' }), 'Text')
    .filter((node) => pattern.test((node.children ?? []).join('')))
    .map((node) => node.props?.bold ?? node.bold)

test('the mod registers exactly the commands features.js describes, each to run without a turn', async ($, on) => {
  const registered = []
  on('command.register', ($, e) => {
    registered.push(e)
    return { value: undefined }
  })
  on('session.start', () => ({ cwd: ROOT }))
  await $.session.start({ surface: 'terminal', isInteractive: true, cwd: ROOT })
  expect(registered.map((r) => r.name).sort()).toEqual(FEATURES.filter((f) => f.command).map((f) => f.command).sort())
  expect(registered.every((r) => r.immediate === true)).toBe(true)
})

test('every button the pane draws is a capability features.js lists', async ($, on) => {
  const { clock } = stub(on, repo())
  const labels = await buttons(await openPane($, clock))
  const declared = FEATURES.flatMap((f) => f.capabilities.map((c) => c.name.match(/^(.+) button\b/)?.[1]).filter(Boolean))
  expect(labels.sort()).toEqual(declared.sort())
})

test('the pane shows the phase, stamps, verify lines, CI and context for the branch it names', async ($, on) => {
  const { clock, opened, timeouts } = stub(on, repo())
  const ui = await openPane($, clock)
  const shown = await texts(ui)
  expect(shown).toMatch(/^feature @ abc1234 · gathered \d\d:\d\d:\d\d$/m)
  expect(shown).toContain('phase: validate')
  expect(shown).toContain('spec           Add the pane')
  expect(shown).not.toContain('/x/spec.md')
  expect(shown).toMatch(/^verify: current$/m)
  expect(shown).toMatch(/^self-review: current$/m)
  expect(shown).toMatch(/^validate: not current$/m)
  expect(shown).toMatch(/^staging: not current$/m)
  expect(shown).toContain('Verify: PASS · 2026-09-30 14:05:09')
  for (const line of ['ran: tests/a.py', 'skipped: ruff: not installed', 'warning: slow', 'error: none']) expect(shown).toContain(line)
  expect(shown).toMatch(/^CI: passed$/m)
  expect(shown).toMatch(/context: 25%/)
  expect(await isBold(ui, /test_failed_login/)).toEqual([false])
  // Escape hands the keys back without closing the pane, so it keeps refreshing after each turn.
  expect(opened).toEqual([{ id: 'flow', title: 'flow', focus: true }])
  expect(timeouts.length).toBeGreaterThan(10)
  expect([...new Set(timeouts)].sort()).toEqual([30_000, 90_000])
})

test('a verify that checked nothing is never shown as a plain current stamp', async ($, on) => {
  const { clock } = stub(on, repo({
    'python3 /home/.agents/hooks/review_stamp.py check --kind verify': { exitCode: 1 },
    'python3 /home/.agents/hooks/review_stamp.py check --kind verify-empty': { exitCode: 0 },
  }))
  const shown = await texts(await openPane($, clock))
  expect(shown).toMatch(/verify: current, but it checked nothing/)
})

test('a HEAD pushed with green CI, then a new unpushed commit, says not pushed and keeps no green', async ($, on) => {
  let head = HEAD
  const runs = repo()
  const { ran, clock } = stub(on, new Proxy(runs, {
    get: (target, key) => {
      if (key === 'git rev-parse HEAD') return { exitCode: 0, stdout: head + '\n' }
      if (key === 'git merge-base --is-ancestor fff9999aaa0000 @{u}') return { exitCode: 1 }
      return target[key]
    },
  }))
  const ui = await openPane($, clock)
  expect(await texts(ui)).toMatch(/^CI: passed$/m)
  head = 'fff9999aaa0000'
  await ui.press({ key: 'refresh' })
  await clock.settle()
  const shown = await texts(ui)
  expect(shown).toMatch(/^feature @ fff9999/m)
  expect(shown).toMatch(/^CI: HEAD not pushed$/m)
  expect(shown).not.toMatch(/CI: passed/)
  expect(ran.filter((argv) => argv.includes('ci-wait'))).toEqual([KIT + '/bin/ci-wait --sha ' + HEAD + ' --once --no-log'])
})

test('CI for a HEAD with uncommitted changes says they are not in it, and a failed status says so', async ($, on) => {
  let status: Run = { exitCode: 0, stdout: ' M hooks/register.js\n' }
  const runs = repo()
  const { clock } = stub(on, new Proxy(runs, { get: (target, key) => (key === 'git status --porcelain' ? status : target[key]) }))
  const ui = await openPane($, clock)
  expect(await texts(ui)).toMatch(/CI: passed, for abc1234 without the uncommitted changes/)
  status = { deny: 'index locked' }
  await ui.press({ key: 'refresh' })
  await clock.settle()
  const shown = await texts(ui)
  expect(shown).toMatch(/CI: passed, for abc1234; couldn't tell whether there are uncommitted changes: .*index locked/)
  expect(shown).not.toMatch(/without the uncommitted changes/)
})

test('each CI outcome of ci-wait is named, an unreadable one with its reason, and an unknown exit is a read failure', async ($, on) => {
  const outcomes = { 1: /CI: failed$/m, 2: /CI: running$/m, 3: /CI: no checks$/m, 4: /CI: unreadable: gh: not logged in$/m, 9: /CI: couldn't read/ }
  let code = 1
  const runs = repo()
  const { clock } = stub(on, new Proxy(runs, {
    get: (target, key) => key === KIT + '/bin/ci-wait --sha ' + HEAD + ' --once --no-log' ? { exitCode: code, stdout: '', stderr: 'gh: not logged in\n' } : target[key],
  }))
  const ui = await openPane($, clock)
  for (const [exit, shown] of Object.entries(outcomes)) {
    code = Number(exit)
    await ui.press({ key: 'refresh' })
    await clock.settle()
    expect(await texts(ui)).toMatch(shown)
  }
})

test('without an upstream CI is not pushed, and a failed upstream read says why', async ($, on) => {
  let upstream: Run = { exitCode: 1 }
  const runs = repo()
  const { clock } = stub(on, new Proxy(runs, { get: (target, key) => (key === 'git rev-parse --verify --quiet @{u}' ? upstream : target[key]) }))
  const ui = await openPane($, clock)
  expect(await texts(ui)).toMatch(/^CI: HEAD not pushed \(no upstream\)$/m)
  upstream = { exitCode: 128, stderr: 'fatal: bad config\n' }
  await ui.press({ key: 'refresh' })
  await clock.settle()
  expect(await texts(ui)).toMatch(/^CI: couldn't read: exit 128: fatal: bad config$/m)
})

test('each kind of no change to follow says which, and a failed read is never one of them', async ($, on) => {
  const cases: [Record<string, Run>, RegExp][] = [
    [{ 'git rev-parse --show-toplevel': { exitCode: 128, stderr: 'not a git repository' } }, /^Not in a git repository: no change to follow\.$/m],
    [{ 'git rev-parse --show-toplevel': { deny: 'git hung' } }, /^Couldn't read the repository: .*git hung$/m],
    [{ 'git rev-parse --show-toplevel': { exitCode: 128, stderr: 'fatal: detected dubious ownership' } }, /^Couldn't read the repository: exit 128: fatal: detected dubious ownership$/m],
    [{ 'git rev-parse HEAD': { exitCode: 128, stderr: "fatal: ambiguous argument 'HEAD'" } }, /^Couldn't read HEAD: exit 128: fatal: ambiguous argument 'HEAD'$/m],
    [{ 'git branch --show-current': { deny: 'git hung' } }, /^Couldn't read the branch: .*git hung$/m],
    [{ 'git branch --show-current': { exitCode: 0, stdout: '\n' } }, /^Detached HEAD: no change to follow\.$/m],
    [{ [KIT + '/bin/reports brief']: { exitCode: 0, stdout: 'phase: \n' } }, /^On the default branch: no change to follow\.$/m],
  ]
  let runs = repo()
  const { ran, clock } = stub(on, new Proxy({}, { get: (target, key) => runs[key] }))
  const ui = await openPane($, clock)
  for (const [overrides, said] of cases) {
    runs = repo(overrides)
    ran.length = 0
    await ui.press({ key: 'refresh' })
    await clock.settle()
    expect(await texts(ui)).toMatch(said)
    expect(await buttons(ui)).toEqual(['Refresh'])
    expect(ran.some((argv) => argv.includes('ci-wait'))).toBe(false)
  }
})

test('without a surface to draw on, /flow prints the same facts as text', async ($, on) => {
  const { opened } = stub(on, repo(), { surfaces: [] })
  const answer = await $.command.run({ command: 'flow', args: '' })
  expect(answer.text).toMatch(/^feature @ abc1234 · gathered \d\d:\d\d:\d\d$/m)
  expect(answer.text).toContain('phase: validate')
  expect(answer.text).toMatch(/^ {2}verify: current$/m)
  expect(answer.text).toMatch(/^ {2}staging: not current$/m)
  expect(answer.text).toContain('ran: tests/a.py')
  expect(answer.text).toMatch(/^ {2}CI: passed$/m)
  expect(answer.text).toMatch(/^ {2}context: 25%$/m)
  expect(opened).toEqual([])
})

test('a source that fails says why, and the other sections still show', async ($, on) => {
  const { clock } = stub(on, repo({ [KIT + '/bin/reports brief']: { deny: 'reports exploded' } }))
  const shown = await texts(await openPane($, clock))
  expect(shown).toMatch(/couldn't read: .*reports exploded/)
  expect(shown).toMatch(/verify: current/)
  expect(shown).toMatch(/CI: passed/)
})

test('a gathering the host fails to answer says so instead of gathering forever', async ($, on) => {
  const { clock } = stub(on, repo(), { cwd: { deny: 'no session' } })
  const ui = await openPane($, clock)
  const shown = await texts(ui)
  expect(shown).toMatch(/^Couldn't gather the flow: .*no session$/m)
  expect(shown).not.toContain('Gathering…')
  expect(await isBold(ui, /^Couldn't gather/)).toEqual([true])
  expect(await isBold(ui, /^Run verify/)).toEqual([])
})

test('no verify report yet says so', async ($, on) => {
  const { clock } = stub(on, repo(), { report: '' })
  expect(await texts(await openPane($, clock))).toContain('no verify run yet')
})

test('a line longer than Claude Code draws is clipped', async ($, on) => {
  const { clock } = stub(on, repo(), { report: '# Verify: FAIL\n\nerror: ' + 'x'.repeat(20_000) + '\n' })
  const lines = (await texts(await openPane($, clock))).split('\n')
  const error = lines.find((line) => line.startsWith('error: '))
  expect(error.length).toBe(500)
  expect(error.endsWith('…')).toBe(true)
})

test('a checkout that moves while gathering is gathered again, and says so when it keeps moving', async ($, on) => {
  let heads = [HEAD, 'fff9999aaa0000', 'fff9999aaa0000', 'fff9999aaa0000']
  const runs = repo({
    'git merge-base --is-ancestor fff9999aaa0000 @{u}': { exitCode: 1 },
  })
  const { clock } = stub(on, new Proxy(runs, {
    get: (target, key) => (key === 'git rev-parse HEAD' ? { exitCode: 0, stdout: (heads.length > 1 ? heads.shift() : heads[0]) + '\n' } : target[key]),
  }))
  const ui = await openPane($, clock)
  let shown = await texts(ui)
  expect(shown).toMatch(/^feature @ fff9999 · gathered \d\d:\d\d:\d\d$/m)
  expect(shown).toMatch(/^CI: HEAD not pushed$/m)
  heads = ['1111111aaaa', '2222222bbbb', '3333333cccc', '4444444dddd', '5555555eeee']
  await ui.press({ key: 'refresh' })
  await clock.settle()
  shown = await texts(ui)
  expect(shown).toMatch(/^feature @ 3333333 · .*the checkout moved while gathering: Refresh$/m)
})

test('a session that moves to another checkout while gathering is gathered there', async ($, on) => {
  const OTHER = '/work/other'
  let cwdReads = 0
  const runs = repo()
  const roots: string[] = []
  const { clock } = stub(on, runs, {
    cwd: () => ({ value: ++cwdReads === 1 ? SESSION_CWD : OTHER }),
    process: ($, e) => {
      const key = e.argv.join(' ')
      if (key === KIT + '/bin/reports brief') roots.push(e.init?.cwd)
      if (key === 'git rev-parse --show-toplevel') return { value: { exitCode: 0, stdout: (e.init?.cwd === OTHER ? OTHER : ROOT) + '\n', stderr: '' } }
      const answer = runs[key]
      return { value: { exitCode: answer.exitCode, stdout: answer.stdout ?? '', stderr: answer.stderr ?? '' } }
    },
  })
  await openPane($, clock)
  expect(roots).toEqual([ROOT, OTHER])
})

test('a HEAD that can no longer be read once gathered says so, never that the checkout moved', async ($, on) => {
  let headReads = 0
  const runs = repo()
  const { clock } = stub(on, new Proxy(runs, {
    get: (target, key) => (key === 'git rev-parse HEAD' && ++headReads % 2 === 0 ? { exitCode: 128, stderr: 'fatal: bad object HEAD' } : target[key]),
  }))
  const shown = await texts(await openPane($, clock))
  expect(shown).toMatch(/^Couldn't read HEAD: exit 128: fatal: bad object HEAD$/m)
  expect(shown).not.toMatch(/moved/)
})

test('a Refresh during a gathering gathers again after it, and the newer facts stay', async ($, on) => {
  const runs = repo()
  let briefs = 0
  let clock
  // The first gathering's brief answers late with the old phase, the second's at once with the new one.
  ;({ clock } = stub(on, runs, {
    process: async ($, e) => {
      const key = e.argv.join(' ')
      if (key === KIT + '/bin/reports brief') {
        briefs += 1
        if (briefs === 1) {
          await clock.sleep(5000)
          return { value: { exitCode: 0, stdout: 'phase: build\n', stderr: '' } }
        }
        return { value: { exitCode: 0, stdout: 'phase: create-pr\n', stderr: '' } }
      }
      const answer = runs[key]
      return { value: { exitCode: answer.exitCode, stdout: answer.stdout ?? '', stderr: answer.stderr ?? '' } }
    },
  }))
  await $.command.run({ command: 'flow', args: '' })
  const ui = await $.ui.mount({ ...PANE, surface: 'terminal' })
  await ui.press({ key: 'refresh' })
  await ui.press({ key: 'refresh' })
  await clock.settle()
  expect(briefs).toBe(1)
  await clock.advance(5000)
  await clock.settle()
  const shown = await texts(ui)
  expect(briefs).toBe(2)
  expect(shown).toContain('phase: create-pr')
  expect(shown).not.toContain('phase: build')
  expect(shown).not.toContain('refreshing…')
})

test('a main-session turn refreshes the open pane; a subagent turn, or a closed pane, does not', async ($, on) => {
  let panes = ['flow']
  const { ran, clock } = stub(on, repo(), { panes: () => panes })
  on('turn.complete', () => ({ text: '' }))
  await openPane($, clock)
  const briefs = () => ran.filter((argv) => argv === KIT + '/bin/reports brief').length
  expect(briefs()).toBe(1)
  await $.turn.complete({ turnId: 't1', reason: 'answer', answer: '', durationMs: 1, isAborted: false })
  await clock.settle()
  expect(briefs()).toBe(2)
  await $.turn.complete({ turnId: 't2', reason: 'answer', answer: '', durationMs: 1, isAborted: false, agentId: 'helper' })
  await clock.settle()
  panes = []
  await $.turn.complete({ turnId: 't3', reason: 'answer', answer: '', durationMs: 1, isAborted: false })
  await clock.settle()
  expect(briefs()).toBe(2)
  // A host that can't list its panes still completes the turn.
  panes = null
  expect(await $.turn.complete({ turnId: 't4', reason: 'answer', answer: '', durationMs: 1, isAborted: false })).toEqual({ text: '' })
})

// The button's verify run, answered by a stubbed spawn that writes `output` and exits with `code`.
type Outcome = { code?: number | null; signal?: string; output?: string; chunks?: [string, string][]; reject?: string }

function stubVerify(on, outcome: () => Outcome) {
  const spawned = []
  on('process.spawn', async function* ($, e) {
    spawned.push(e)
    const { code = 0, signal = null, output = '', chunks = [['stdout', output]], reject } = outcome()
    // A deny makes the mod's call reject, as a program that can't start does.
    if (reject) return { deny: reject }
    for (const [stream, text] of chunks) yield { stream, text }
    return { value: { code, signal } }
  })
  return spawned
}

test('Run verify runs stop_checks.py verify at the repository root, and shows its own passing run', async ($, on) => {
  const { clock } = stub(on, repo())
  const spawned = stubVerify(on, () => ({ output: 'ran: tests/a.py\nran: tests/b.py\n' }))
  const ui = await openPane($, clock)
  await ui.press({ key: 'run-verify' })
  await clock.settle()
  expect(spawned.map((e) => [e.argv, e.cwd])).toEqual([[VERIFY_ARGV, ROOT]])
  const shown = await texts(ui)
  expect(shown).toMatch(/^Run verify on feature @ abc1234 at \d\d:\d\d:\d\d: passed$/m)
  expect(shown).toContain('ran: tests/b.py')
})

test("Run verify's checked nothing comes from its own run, never from a stamp another run wrote", async ($, on) => {
  let empty = 1
  const runs = repo()
  const { clock } = stub(on, new Proxy(runs, {
    get: (target, key) => key === 'python3 /home/.agents/hooks/review_stamp.py check --kind verify-empty' ? { exitCode: empty } : target[key],
  }))
  let output = 'ran: nothing to check: no changed files\n'
  let chunks = [['stdout', output], ['stderr', CHECKED_NOTHING]]
  stubVerify(on, () => ({ output, chunks }))
  const ui = await openPane($, clock)
  await ui.press({ key: 'run-verify' })
  await clock.settle()
  expect(await texts(ui)).toMatch(/^Run verify on feature @ abc1234 at .*: passed, but it checked nothing$/m)
  // A terminal verify that checked nothing stamps the change: this run checked something.
  empty = 0
  // The repo's own output can say anything: only stop_checks.py's stderr line decides.
  chunks = [['stdout', 'ran: tests/a.py\n' + CHECKED_NOTHING]]
  await ui.press({ key: 'run-verify' })
  await clock.settle()
  expect(await texts(ui)).toMatch(/^Run verify on feature @ abc1234 at .*: passed$/m)
  // stdout's half line and stderr's line arrive interleaved: each stream keeps its own.
  chunks = [['stdout', 'ran: nothing to'], ['stderr', CHECKED_NOTHING], ['stdout', ' check: no changed files\n']]
  await ui.press({ key: 'run-verify' })
  await clock.settle()
  const shown = await texts(ui)
  expect(shown).toMatch(/: passed, but it checked nothing$/m)
  expect(shown).toContain('ran: nothing to check: no changed files')
})

test('Run verify that fails, is killed or cannot start is a failure with its reason, and the button works again', async ($, on) => {
  const { clock } = stub(on, repo())
  let outcome: Outcome = { code: 1, output: 'error: tests/a.py failed\n' }
  stubVerify(on, () => outcome)
  const ui = await openPane($, clock)
  const pressed = async () => {
    await ui.press({ key: 'run-verify' })
    await clock.settle()
    return texts(ui)
  }
  expect(await pressed()).toMatch(/^Run verify on feature @ abc1234 at .*: failed \(exit 1\)$/m)
  outcome = { code: 1, output: 'verify timed out after 600s.\n' }
  let shown = await pressed()
  expect(shown).toMatch(/: failed \(exit 1\)$/m)
  expect(shown).toContain('verify timed out after 600s.')
  outcome = { code: null, signal: 'SIGTERM', output: 'ran: tests/a.py\n' }
  expect(await pressed()).toMatch(/: failed \(killed by SIGTERM\)$/m)
  outcome = { reject: 'python3: not found' }
  shown = await pressed()
  expect(shown).toMatch(/^Run verify failed: couldn't run: .*python3: not found$/m)
  expect(shown).not.toMatch(/Run verify.*passed/)
  outcome = { output: 'ran: tests/a.py\n' }
  expect(await pressed()).toMatch(/: passed$/m)
})

// Each settle while the stuck child waits costs about a second of real time.
test('Run verify that never ends is stopped at its deadline as a failure, and a second press while it runs starts nothing', { timeout: 20_000 }, async ($, on) => {
  const { clock } = stub(on, repo())
  let spawns = 0
  let release
  on('process.spawn', async function* () {
    spawns += 1
    if (spawns === 1) await new Promise((resolve) => (release = resolve))
    yield { stream: 'stdout', text: 'ran: tests/a.py\n' }
    return { value: { code: 0, signal: null } }
  })
  const ui = await openPane($, clock)
  // A press returns while verify runs: Claude Code skips a press hook still running after 10 s.
  await ui.press({ key: 'run-verify' })
  await clock.settle()
  await ui.press({ key: 'run-verify' })
  expect(await texts(ui)).toMatch(/Run verify: running/)
  await clock.advance(660_000)
  await clock.settle()
  expect(spawns).toBe(1)
  let shown = await texts(ui)
  expect(shown).toMatch(/: failed \(no result after 660 s\)$/m)
  expect(shown).toContain('stopping it: the button works again once it has ended')
  // Until the stopped child is gone, a press starts nothing: two verifies would write one report.
  await ui.press({ key: 'run-verify' })
  await clock.settle()
  expect(spawns).toBe(1)
  release()
  await clock.settle()
  expect(await texts(ui)).not.toContain('stopping it')
  await ui.press({ key: 'run-verify' })
  await clock.settle()
  expect(spawns).toBe(2)
  expect(await texts(ui)).toMatch(/: passed$/m)
})

const TRIAGE_ARGV = KIT + '/bin/triage --lens-briefs'
const LENSES = [
  { key: 'correctness', title: 'Correctness', brief: 'You are reviewing whether the change does what it should.\n- Every path?' },
  { key: 'ux-a11y-i18n', title: 'UX, accessibility and i18n', brief: 'You are reviewing what a person sees.' },
]

// A session start with triage answering `triage`, and each agent.register answered by `answer`.
async function startWithReviewers($, on, triage: Run, answer = (spec) => ({ value: { agent: 'kit:' + spec.name } })) {
  const registered = []
  const logged: { text: string; to: string }[] = []
  const { cwds, timeouts, clock } = stub(on, { [TRIAGE_ARGV]: triage })
  on('agent.register', ($, e) => {
    registered.push(e)
    return answer(e)
  })
  on('ui.log', ($, e) => {
    logged.push({ text: e.text, to: e.to })
    return undefined
  })
  on('session.start', () => ({ cwd: ROOT }))
  await $.session.start({ surface: 'terminal', isInteractive: true, cwd: ROOT })
  await clock.settle()
  return { registered, logged, cwds, timeouts }
}

test('session start registers one reviewer per lens bin/triage prints, briefed with its text and without edit tools', async ($, on) => {
  const { registered, logged, cwds, timeouts } = await startWithReviewers($, on, { exitCode: 0, stdout: JSON.stringify(LENSES) })
  expect(registered.map((spec) => spec.name)).toEqual(['review-correctness', 'review-ux-a11y-i18n'])
  for (const [spec, lens] of registered.map((spec, i) => [spec, LENSES[i]])) {
    expect(spec.prompt).toContain('one lens: ' + lens.title)
    expect(spec.prompt).toContain(lens.brief)
    expect(spec.prompt).toContain('never edit')
    expect(spec.prompt).toContain('AGENTS.md')
    expect(spec.description).toContain(lens.title)
    expect(spec.tools).toEqual(['Read', 'Grep', 'Glob', 'Bash'])
    expect(spec.omitClaudeMd).toBe(true)
  }
  expect([cwds, timeouts]).toEqual([[KIT], [30_000]])
  expect(logged).toEqual([])
  // Every agent the mod registers carries a prefix features.js lists.
  const prefixes = FEATURES.filter((feature) => feature.agentPrefix).map((feature) => feature.agentPrefix + '-')
  expect(prefixes).toEqual(['review-'])
  expect(registered.every((spec) => prefixes.some((prefix) => spec.name.startsWith(prefix)))).toBe(true)
})

for (const [why, triage, cause] of [
  ['triage fails', { exitCode: 1, stderr: "triage: lenses.md doesn't match LENS_TITLES: no key in LENS_TITLES for Haptics\n" }, /Haptics/],
  ['triage prints something other than JSON', { exitCode: 0, stdout: 'Risk: low\n' }, /not JSON/],
  ['triage prints JSON that is not a list of lenses', { exitCode: 0, stdout: '{}' }, /not a list of lenses/],
  ['triage can not start', { deny: 'no python3' }, /no python3/],
] as const) {
  test(`when ${why}, no reviewer is registered and the session starts, with one debug line saying why`, async ($, on) => {
    const { registered, logged } = await startWithReviewers($, on, triage)
    expect(registered).toEqual([])
    expect(logged.length).toBe(1)
    expect(logged[0].text).toMatch(cause)
    expect(logged[0].to).toBe('debug')
  })
}

test('a reviewer the host refuses is logged, and the next one is still registered', async ($, on) => {
  const answer = (spec) => (spec.name === 'review-correctness' ? { deny: 'schema: bad name' } : { value: { agent: 'kit:' + spec.name } })
  const { registered, logged } = await startWithReviewers($, on, { exitCode: 0, stdout: JSON.stringify(LENSES) }, answer)
  expect(registered.map((spec) => spec.name)).toEqual(['review-correctness', 'review-ux-a11y-i18n'])
  expect(logged.length).toBe(1)
  expect(logged[0].text).toMatch(/review-correctness.*schema: bad name/)
  expect(logged[0].to).toBe('debug')
})

test('session start does not wait for the reviewers: a triage that never answers holds nothing up', async ($, on) => {
  stub(on, {}, { process: () => new Promise(() => {}) })
  on('session.start', () => ({ cwd: ROOT }))
  const started = await Promise.race([
    $.session.start({ surface: 'terminal', isInteractive: true, cwd: ROOT }).then(() => 'started'),
    new Promise((resolve) => setTimeout(() => resolve('held up'), 2_000)),
  ])
  expect(started).toBe('started')
})
