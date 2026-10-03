// The kit inside Claude Code. Everything here shows or runs the kit's own tools: the state stays with them
// and the decisions in the Python hooks, so Codex and a session without this mod get the same flow.
import { FEATURES } from './features.js'

const PANE = 'flow'
const PROCESS_TIMEOUT_MS = 30_000
// ci-wait --once makes one GitHub round trip per check source, each bounded at 30 s.
const CI_TIMEOUT_MS = 90_000
const STAMPS = [['verify', 'verify'], ['self-review', 'review'], ['validate', 'validate'], ['staging', 'staging']]
const CI_OUTCOMES = { 0: 'passed', 1: 'failed', 2: 'running', 3: 'no checks', 4: 'unreadable' }
const REPORT_LINE = /^(ran|skipped|warning|error):/
const VERIFY_TAIL_LINES = 8
// Claude Code refuses a Text string over 10,000 characters; a verify line can be any length.
const LINE_CHARS = 500
// stop_checks.py bounds the repo's verify at 600 s, but not its own git reads and stamp writes around it.
const VERIFY_DEADLINE_MS = 660_000
// What `stop_checks.py verify` prints when the run passed without checking anything.
const CHECKED_NOTHING = 'verify passed, but it checked nothing'
// A gathering whose branch or HEAD moved while it ran is gathered again, this many times in all.
const GATHER_ATTEMPTS = 2
const NEEDS_ATTENTION = /couldn't|failed/

// What the pane draws. One gathering runs at a time: a request during one gathers again once it ends.
let shown = null
let gathering = false
let gatherAgain = false
let verifyRun = { state: 'idle' }

async function run($, argv, cwd, timeoutMs = PROCESS_TIMEOUT_MS) {
  try {
    return await $.process.run(argv, { cwd, timeoutMs })
  } catch (error) {
    return { failure: messageOf(error) }
  }
}

function messageOf(error) {
  return String(error?.message ?? error)
}

function failureOf(result) {
  if (result.failure) return result.failure
  return 'exit ' + result.exitCode + (result.stderr?.trim() ? ': ' + result.stderr.trim().split('\n')[0] : '')
}

async function kitDir($) {
  return (await $.env.get('HOME')) + '/.agents'
}

async function stampLine($, kit, root, label, kind) {
  const check = (stampKind) => run($, ['python3', kit + '/hooks/review_stamp.py', 'check', '--kind', stampKind], root)
  const result = await check(kind)
  if (result.exitCode === 0) return label + ': current'
  if (result.exitCode !== 1) return label + ": couldn't read: " + failureOf(result)
  if (kind === 'verify') {
    const empty = await check('verify-empty')
    if (empty.exitCode === 0) return label + ': current, but it checked nothing'
    if (empty.exitCode !== 1) return label + ": couldn't read: " + failureOf(empty)
  }
  return label + ': not current'
}

async function verifyReportLines($, kit, root) {
  const path = await run($, [kit + '/bin/reports', 'path', 'verify'], root)
  if (path.failure || path.exitCode !== 0) return ["couldn't read: " + failureOf(path)]
  const file = path.stdout.trim()
  try {
    if (!(await $.fs.exists(file))) return ['no verify run yet']
    const { mtimeMs } = await $.fs.stat(file)
    const lines = (await $.fs.read(file)).split('\n')
    return [lines[0].replace(/^# /, '') + ' · ' + dateTimeOf(mtimeMs), ...lines.filter((line) => REPORT_LINE.test(line))]
  } catch (error) {
    return ["couldn't read: " + messageOf(error)]
  }
}

async function ciLine($, kit, root, head) {
  const upstream = await run($, ['git', 'rev-parse', '--verify', '--quiet', '@{u}'], root)
  if (upstream.exitCode === 1) return 'CI: HEAD not pushed (no upstream)'
  if (upstream.failure || upstream.exitCode !== 0) return "CI: couldn't read: " + failureOf(upstream)
  const pushed = await run($, ['git', 'merge-base', '--is-ancestor', head, '@{u}'], root)
  if (pushed.failure || pushed.exitCode > 1) return "CI: couldn't read: " + failureOf(pushed)
  if (pushed.exitCode === 1) return 'CI: HEAD not pushed'
  const ci = await run($, [kit + '/bin/ci-wait', '--sha', head, '--once', '--no-log'], root, CI_TIMEOUT_MS)
  const outcome = ci.failure ? undefined : CI_OUTCOMES[ci.exitCode]
  if (!outcome) return "CI: couldn't read: " + failureOf(ci)
  const reason = ci.exitCode === 4 ? (ci.stderr.trim() || ci.stdout.trim()).split('\n')[0] : ''
  const line = 'CI: ' + outcome + (reason ? ': ' + reason : '')
  const status = await run($, ['git', 'status', '--porcelain'], root)
  if (status.failure || status.exitCode !== 0) {
    return line + ", for " + head.slice(0, 7) + "; couldn't tell whether there are uncommitted changes: " + failureOf(status)
  }
  return line + (status.stdout.trim() !== '' ? ', for ' + head.slice(0, 7) + ' without the uncommitted changes' : '')
}

async function contextLine($) {
  try {
    const { context } = await $.session.usage()
    return 'context: ' + (context?.percent === undefined ? '–' : Math.round(context.percent) + '%')
  } catch {
    return 'context: –'
  }
}

function timeOf(ms) {
  return new Date(ms).toTimeString().slice(0, 8)
}

function dateTimeOf(ms) {
  const date = new Date(ms)
  const pad = (n) => String(n).padStart(2, '0')
  return date.getFullYear() + '-' + pad(date.getMonth() + 1) + '-' + pad(date.getDate()) + ' ' + timeOf(ms)
}

// The checkout at `cwd`: { root, branch, head }, or { none } saying why there is no change to follow.
async function checkout($, cwd) {
  const top = await run($, ['git', 'rev-parse', '--show-toplevel'], cwd)
  if (top.failure) return { none: "Couldn't read the repository: " + top.failure }
  if (top.exitCode !== 0) return { none: 'Not in a git repository: no change to follow.' }
  const root = top.stdout.trim()
  const [branch, head] = await Promise.all([
    run($, ['git', 'branch', '--show-current'], root),
    run($, ['git', 'rev-parse', 'HEAD'], root),
  ])
  if (head.failure || head.exitCode !== 0) return { none: "Couldn't read HEAD: " + failureOf(head) }
  if (branch.failure || branch.exitCode !== 0) return { none: "Couldn't read the branch: " + failureOf(branch) }
  if (!branch.stdout.trim()) return { none: 'Detached HEAD: no change to follow.' }
  return { root, branch: branch.stdout.trim(), head: head.stdout.trim() }
}

function clip(text) {
  return text.length > LINE_CHARS ? text.slice(0, LINE_CHARS) + '…' : text
}

function nameOf(at) {
  return at.branch + ' @ ' + at.head.slice(0, 7)
}

// The facts for the change the session is on: { none } when there is no change to follow, otherwise a
// heading naming the branch and HEAD they describe, and one list of lines per section.
async function collect($) {
  const kit = await kitDir($)
  const cwd = await $.session.cwd()
  for (let attempt = 1; ; attempt++) {
    const at = await checkout($, cwd)
    if (at.none) return at
    // The brief comes first: on the default branch it is the only answer, and CI can take ninety seconds.
    const brief = await run($, [kit + '/bin/reports', 'brief'], at.root)
    let flow
    if (brief.failure || brief.exitCode !== 0) flow = ["couldn't read: " + failureOf(brief)]
    else {
      flow = brief.stdout.split('\n').filter((line) => line && !/^\s/.test(line) && !line.startsWith('Read one in full'))
      if (flow[0] === 'phase: ') return { none: 'On the default branch: no change to follow.' }
    }
    const [stamps, report, ci, context, gatheredAt] = await Promise.all([
      Promise.all(STAMPS.map(([label, kind]) => stampLine($, kit, at.root, label, kind))),
      verifyReportLines($, kit, at.root),
      ciLine($, kit, at.root, at.head),
      contextLine($),
      $.clock.now(),
    ])
    const after = await checkout($, cwd)
    const moved = after.none !== undefined || nameOf(after) !== nameOf(at)
    if (moved && attempt < GATHER_ATTEMPTS) continue
    return {
      root: at.root,
      heading: nameOf(at) + ' · gathered ' + timeOf(gatheredAt) + (moved ? ' · the checkout moved while gathering: Refresh' : ''),
      sections: [['Flow', flow], ['Stamps', stamps], ['Last verify report', report], ['CI and context', [ci, context]]],
    }
  }
}

async function collectOrSayWhy($) {
  try {
    return await collect($)
  } catch (error) {
    return { none: "Couldn't gather the flow: " + messageOf(error) }
  }
}

async function gather($) {
  if (gathering) {
    gatherAgain = true
    return
  }
  gathering = true
  $.ui.invalidate('ui.render')
  try {
    do {
      gatherAgain = false
      shown = await collectOrSayWhy($)
      $.ui.invalidate('ui.render')
    } while (gatherAgain)
  } finally {
    gathering = false
  }
  $.ui.invalidate('ui.render')
}

function asText(facts) {
  if (facts.none) return facts.none
  return [facts.heading, ...facts.sections.flatMap(([title, lines]) => ['', title + ':', ...lines.map((l) => '  ' + l)])].join('\n')
}

function verifyLines() {
  if (verifyRun.state === 'idle') return []
  if (verifyRun.state === 'running') return ['Run verify: running…']
  return ['Run verify ' + verifyRun.verdict, ...verifyRun.tail]
}

// One verify on the checkout the session is in now: its verdict and the last lines of its own output.
async function verifyHere($) {
  const at = await checkout($, await $.session.cwd())
  if (at.none) return { verdict: 'did not run: ' + at.none, tail: [] }
  const kit = await kitDir($)
  const startedAt = await $.clock.now()
  const named = (verdict) => ({ verdict: 'on ' + nameOf(at) + ' at ' + timeOf(startedAt) + ': ' + verdict, tail })
  const tail = []
  let partial = ''
  let checkedNothing = false
  const keep = (line) => {
    if (line.startsWith(CHECKED_NOTHING)) checkedNothing = true
    if (!line.trim()) return
    tail.push(line)
    if (tail.length > VERIFY_TAIL_LINES) tail.shift()
  }
  const child = $.process.spawn({ argv: ['python3', kit + '/hooks/stop_checks.py', 'verify'], cwd: at.root })
  const timer = new AbortController()
  const deadline = $.clock.sleep(VERIFY_DEADLINE_MS, { signal: timer.signal }).then(() => 'deadline', () => 'cancelled')
  try {
    for (;;) {
      const step = await Promise.race([child.next(), deadline])
      if (step === 'deadline') {
        // Not awaited: a child stuck mid-step finishes its return only after that step.
        child.return().catch((error) => $.ui.log('flow: stopping verify: ' + messageOf(error), { to: 'debug' }))
        return named('failed (no result after ' + VERIFY_DEADLINE_MS / 1000 + ' s, stopped)')
      }
      if (step.done) {
        keep(partial)
        const { code, signal } = step.value
        if (code !== 0) return named('failed (' + (signal ? 'killed by ' + signal : 'exit ' + code) + ')')
        return named(checkedNothing ? 'passed, but it checked nothing' : 'passed')
      }
      const lines = (partial + step.value.text).split('\n')
      partial = lines.pop().slice(0, LINE_CHARS + 1)
      lines.forEach(keep)
    }
  } finally {
    timer.abort()
  }
}

async function runVerify($) {
  if (verifyRun.state === 'running') return
  verifyRun = { state: 'running' }
  $.ui.invalidate('ui.render')
  try {
    verifyRun = { state: 'done', ...(await verifyHere($)) }
  } catch (error) {
    verifyRun = { state: 'done', verdict: "failed: couldn't run: " + messageOf(error), tail: [] }
  }
  await gather($)
}

export function register(on) {
  on('session.start', async ($, e, next) => {
    const flow = FEATURES.find((feature) => feature.command === 'flow')
    await $.command.register({ name: 'flow', description: flow.description, immediate: true })
    return next(e)
  })

  on('command.run', { command: 'flow' }, async ($) => {
    if ((await $.session.surfaces()).length === 0) return { text: asText(await collectOrSayWhy($)) }
    // Without closeOnEscape: Escape hands the keys back to the prompt and the pane stays, refreshing after each turn.
    await $.ui.open({ id: PANE, title: 'flow', focus: true })
    void gather($)
    return {}
  })

  on('turn.complete', async ($, e, next) => {
    // Asked each time: a pane whose drawing threw is dropped without a ui.close this mod hears.
    if (!e.agentId && (await $.ui.panes()).some((pane) => pane.id === PANE)) void gather($)
    return next(e)
  })

  on('ui.render', { component: 'Pane' }, async ($, e, next) => {
    if (e.requestId !== PANE) return next(e)
    const { Box, Text, Button } = $.ui.resolve(e)
    const line = (text) => Text({ bold: NEEDS_ATTENTION.test(text), wrap: 'wrap', children: [clip(text)] })
    const refreshing = shown && gathering ? ' · refreshing…' : ''
    const body = !shown
      ? [Text({ children: ['Gathering…'] })]
      : shown.none
        ? [line(shown.none + refreshing)]
        : [
            Text({ bold: true, children: [shown.heading + refreshing] }),
            ...shown.sections.flatMap(([title, lines]) => [
              Text({ children: [' '] }),
              Text({ dimColor: true, children: [title] }),
              ...lines.map((text, i) => (title === 'Flow' && i === 0 ? Text({ bold: true, children: [clip(text)] }) : line(text))),
            ]),
          ]
    // Run verify only where there is a change: elsewhere a press would do nothing.
    const buttons = [
      ...(shown?.root ? [Button({ key: 'run-verify', label: 'Run verify', hotkey: 'v', plain: true, onPress: () => runVerify($) })] : []),
      Button({ key: 'refresh', label: 'Refresh', hotkey: 'r', plain: true, onPress: () => gather($) }),
    ]
    return Box({
      flexDirection: 'column',
      children: [
        ...body,
        ...(verifyRun.state === 'idle' ? [] : [Text({ children: [' '] }), ...verifyLines().map(line)]),
        Text({ children: [' '] }),
        Box({ flexDirection: 'row', columnGap: 2, children: buttons }),
      ],
    })
  })
}
