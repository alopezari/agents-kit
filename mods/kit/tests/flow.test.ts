import { expect, mock, test } from 'claude-code/testing'
import { FEATURES } from '../hooks/features.js'

const KIT = '/home/.agents'
const ROOT = '/work/repo'
const REPORT = '/work/repo/.git/agents/verify-repo-feature.md'
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

// A repository on a feature branch, pushed, with green CI, every stamp current and a verify report.
function repo(overrides: Record<string, Run> = {}): Record<string, Run> {
  return {
    'git rev-parse --show-toplevel': { exitCode: 0, stdout: ROOT + '\n' },
    'git branch --show-current': { exitCode: 0, stdout: 'feature\n' },
    'git rev-parse HEAD': { exitCode: 0, stdout: 'abc1234def5678\n' },
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
    'git rev-parse --verify --quiet @{u}': { exitCode: 0, stdout: 'abc1234def5678\n' },
    'git merge-base --is-ancestor HEAD @{u}': { exitCode: 0 },
    'git status --porcelain': { exitCode: 0, stdout: '' },
    [KIT + '/bin/ci-wait --sha abc1234def5678 --once --no-log']: { exitCode: 0, stdout: 'tests: passed\n' },
    ...overrides,
  }
}

// Stubs everything the mod reaches outside itself; returns the argv of every process it ran.
function stub(on, runs: Record<string, Run>, { surfaces = ['terminal'], report = '# Verify: PASS\n\n```\nran: tests/a.py\nskipped: ruff: not installed\n```\n', process = null } = {}) {
  const ran: string[] = []
  const clock = mock.clock(on)
  on('env.get', () => ({ value: '/home' }))
  on('session.cwd', () => ({ value: ROOT }))
  on('session.surfaces', () => ({ value: surfaces }))
  on('session.usage', () => ({ value: { context: { window: 200000, tokens: 50000, percent: 25 } } }))
  on('command.register', () => ({ value: undefined }))
  on('ui.open', () => ({ value: { isPlaced: true } }))
  on('ui.close', () => ({ value: undefined }))
  on('fs.exists', ($, e) => ({ value: report !== '' && e.path === REPORT }))
  on('fs.stat', () => ({ value: { kind: 'file', size: report.length, mtimeMs: 0 } }))
  on('fs.read', () => ({ value: report }))
  on('process.run', process ?? (($, e) => {
    const key = e.argv.join(' ')
    ran.push(key)
    const answer = runs[key]
    if (!answer) return { deny: 'unexpected command: ' + key }
    if ('deny' in answer) return answer
    return { value: { exitCode: answer.exitCode, stdout: answer.stdout ?? '', stderr: answer.stderr ?? '' } }
  }))
  return { ran, clock }
}

async function openPane($, clock) {
  await $.command.run({ command: 'flow', args: '' })
  await clock.settle()
  return $.ui.mount({ ...PANE, surface: 'terminal' })
}

const texts = async (ui) => {
  const found: string[] = []
  const walk = (node) => {
    if (!node || typeof node !== 'object') return
    if (node.type === 'Text') found.push((node.children ?? []).filter((c) => typeof c === 'string').join(''))
    for (const child of node.children ?? []) walk(child)
  }
  walk(await ui.find({ type: 'Box' }))
  return found.join('\n')
}

test('the mod registers exactly the commands features.js describes', async ($, on) => {
  const registered: string[] = []
  on('command.register', ($, e) => {
    registered.push(e.name)
    return { value: undefined }
  })
  on('session.start', () => ({ cwd: ROOT }))
  await $.session.start({ surface: 'terminal', isInteractive: true, cwd: ROOT })
  expect(registered.sort()).toEqual(FEATURES.map((f) => f.command).sort())
})

test('the pane shows the phase, stamps, verify lines, CI and context for the branch it names', async ($, on) => {
  const { clock } = stub(on, repo())
  const ui = await openPane($, clock)
  const shown = await texts(ui)
  expect(shown).toMatch(/feature @ abc1234/)
  expect(shown).toContain('phase: validate')
  expect(shown).toContain('spec           Add the pane')
  expect(shown).not.toContain('/x/spec.md')
  expect(shown).toMatch(/verify: current/)
  expect(shown).toMatch(/self-review: current/)
  expect(shown).toMatch(/validate: not current/)
  expect(shown).toContain('ran: tests/a.py')
  expect(shown).toContain('skipped: ruff: not installed')
  expect(shown).toMatch(/CI: passed/)
  expect(shown).toMatch(/context: 25%/)
})

test('a verify that checked nothing is never shown as a plain current stamp', async ($, on) => {
  const { clock } = stub(on, repo({
    'python3 /home/.agents/hooks/review_stamp.py check --kind verify': { exitCode: 1 },
    'python3 /home/.agents/hooks/review_stamp.py check --kind verify-empty': { exitCode: 0 },
  }))
  const shown = await texts(await openPane($, clock))
  expect(shown).toMatch(/verify: current, but it checked nothing/)
})

test('an unpushed HEAD says so, and CI is not looked up for an older pushed commit', async ($, on) => {
  const { ran, clock } = stub(on, repo({ 'git merge-base --is-ancestor HEAD @{u}': { exitCode: 1 } }))
  const shown = await texts(await openPane($, clock))
  expect(shown).toMatch(/CI: HEAD not pushed/)
  expect(ran.some((argv) => argv.includes('ci-wait'))).toBe(false)
})

test('CI for a HEAD with uncommitted changes says they are not in it', async ($, on) => {
  const { clock } = stub(on, repo({ 'git status --porcelain': { exitCode: 0, stdout: ' M hooks/register.js\n' } }))
  const shown = await texts(await openPane($, clock))
  expect(shown).toMatch(/CI: passed, for abc1234 without the uncommitted changes/)
})

test('each CI outcome of ci-wait is named, and an unknown exit is a read failure', async ($, on) => {
  const outcomes = { 1: 'failed', 2: 'running', 3: 'no checks', 4: 'unreadable', 9: "couldn't read" }
  let code = 1
  const runs = repo()
  const { clock } = stub(on, new Proxy(runs, {
    get: (target, key) => key === KIT + '/bin/ci-wait --sha abc1234def5678 --once --no-log' ? { exitCode: code, stdout: 'x\n' } : target[key],
  }))
  const ui = await openPane($, clock)
  for (const [exit, word] of Object.entries(outcomes)) {
    code = Number(exit)
    await ui.press({ key: 'refresh' })
    await clock.settle()
    expect(await texts(ui)).toMatch(new RegExp('CI: ' + word))
  }
})

test('outside a repository, and on the default branch, there is no change to follow', async ($, on) => {
  const { clock } = stub(on, repo({ 'git rev-parse --show-toplevel': { exitCode: 128, stderr: 'not a git repository' } }))
  expect(await texts(await openPane($, clock))).toMatch(/no change to follow/)
})

test('on the default branch there is no change to follow', async ($, on) => {
  const { clock } = stub(on, repo({ [KIT + '/bin/reports brief']: { exitCode: 0, stdout: 'phase: \n' } }))
  expect(await texts(await openPane($, clock))).toMatch(/no change to follow/)
})

test('without a surface to draw on, /flow prints the same facts as text', async ($, on) => {
  const { clock } = stub(on, repo(), { surfaces: [] })
  const answer = await $.command.run({ command: 'flow', args: '' })
  expect(answer.text).toContain('phase: validate')
  expect(answer.text).toMatch(/CI: passed/)
  expect(answer.text).toMatch(/context: –|context: 25%/)
})

test('a source that fails says why, and the other sections still show', async ($, on) => {
  const { clock } = stub(on, repo({ [KIT + '/bin/reports brief']: { deny: 'reports exploded' } }))
  const shown = await texts(await openPane($, clock))
  expect(shown).toMatch(/couldn't read: .*reports exploded/)
  expect(shown).toMatch(/verify: current/)
  expect(shown).toMatch(/CI: passed/)
})

test('no verify report yet says so', async ($, on) => {
  const { clock } = stub(on, repo(), { report: '' })
  expect(await texts(await openPane($, clock))).toContain('no verify run yet')
})

test('an older gathering that finishes last never replaces a newer one', async ($, on) => {
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
  await clock.settle()
  await clock.advance(5000)
  const shown = await texts(ui)
  expect(shown).toContain('phase: create-pr')
  expect(shown).not.toContain('phase: build')
})

// The button's verify run, answered by a stubbed spawn that writes `output` and exits with `code`.
function stubVerify(on, outcome: { code?: number; output?: string; reject?: string }) {
  on('process.spawn', async function* () {
    // A deny makes the mod's call reject, as a program that can't start does.
    if (outcome.reject) return { deny: outcome.reject }
    yield { stream: 'stdout', text: outcome.output ?? '' }
    return { value: { code: outcome.code ?? 0, signal: null } }
  })
}

test('Run verify shows a passing run with its own output', async ($, on) => {
  const { clock } = stub(on, repo())
  stubVerify(on, { code: 0, output: 'ran: tests/a.py\nran: tests/b.py\n' })
  const ui = await openPane($, clock)
  await ui.press({ key: 'run-verify' })
  await clock.settle()
  const shown = await texts(ui)
  expect(shown).toMatch(/Run verify: passed/)
  expect(shown).toContain('ran: tests/b.py')
})

test('Run verify that checked nothing says so instead of passed', async ($, on) => {
  const { clock } = stub(on, repo({
    'python3 /home/.agents/hooks/review_stamp.py check --kind verify': { exitCode: 1 },
    'python3 /home/.agents/hooks/review_stamp.py check --kind verify-empty': { exitCode: 0 },
  }))
  stubVerify(on, { code: 0, output: 'ran: nothing to check: no changed files\n' })
  const ui = await openPane($, clock)
  await ui.press({ key: 'run-verify' })
  await clock.settle()
  const shown = await texts(ui)
  expect(shown).toMatch(/Run verify: passed, but it checked nothing/)
})

test('Run verify that fails, times out or cannot start is a failure with its reason', async ($, on) => {
  const { clock } = stub(on, repo())
  let outcome: { code?: number; output?: string; reject?: string } = { code: 1, output: 'error: tests/a.py failed\n' }
  on('process.spawn', async function* () {
    // A deny makes the mod's call reject, as a program that can't start does.
    if (outcome.reject) return { deny: outcome.reject }
    yield { stream: 'stdout', text: outcome.output ?? '' }
    return { value: { code: outcome.code ?? 0, signal: null } }
  })
  const ui = await openPane($, clock)
  await ui.press({ key: 'run-verify' })
  await clock.settle()
  expect(await texts(ui)).toMatch(/Run verify: failed \(exit 1\)/)
  outcome = { code: 1, output: 'verify timed out after 600s.\n' }
  await ui.press({ key: 'run-verify' })
  await clock.settle()
  expect(await texts(ui)).toContain('verify timed out after 600s.')
  outcome = { reject: 'python3: not found' }
  await ui.press({ key: 'run-verify' })
  await clock.settle()
  const shown = await texts(ui)
  expect(shown).toMatch(/Run verify: failed: couldn't run: .*python3: not found/)
  expect(shown).not.toMatch(/Run verify: passed/)
})

test('a second press while verify runs starts nothing', async ($, on) => {
  const { clock } = stub(on, repo())
  let spawns = 0
  on('process.spawn', async function* () {
    spawns += 1
    await clock.sleep(10000)
    yield { stream: 'stdout', text: 'ran: tests/a.py\n' }
    return { value: { code: 0, signal: null } }
  })
  const ui = await openPane($, clock)
  // A press resolves once its handler finishes, and the first one waits for verify: don't await it yet.
  const first = ui.press({ key: 'run-verify' })
  await clock.settle()
  await ui.press({ key: 'run-verify' })
  expect(await texts(ui)).toMatch(/Run verify: running/)
  await clock.advance(10000)
  await first
  await clock.settle()
  expect(spawns).toBe(1)
  expect(await texts(ui)).toMatch(/Run verify: passed/)
})
