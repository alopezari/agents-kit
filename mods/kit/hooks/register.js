// The kit inside Claude Code. Everything here shows or runs the kit's own tools: the state lives in
// .git/agents and the decisions in the Python hooks, so Codex and a session without this mod get the same flow.
import { FEATURES } from './features.js'

const PANE = 'flow'
const PROCESS_TIMEOUT_MS = 30_000
// ci-wait --once makes one GitHub round trip per check source, each bounded at 30 s.
const CI_TIMEOUT_MS = 90_000
const STAMPS = [['verify', 'verify'], ['self-review', 'review'], ['validate', 'validate'], ['staging', 'staging']]
const CI_OUTCOMES = { 0: 'passed', 1: 'failed', 2: 'running', 3: 'no checks', 4: 'unreadable' }
const REPORT_LINE = /^(ran|skipped|warning|error):/
const VERIFY_TAIL_LINES = 8

// What the pane draws. A gathering whose number is no longer the latest is dropped when it finishes.
let shown = null
let latestGathering = 0
let verifyRun = { state: 'idle' }
let paneOpen = false

async function run($, argv, cwd, timeoutMs = PROCESS_TIMEOUT_MS) {
  try {
    return await $.process.run(argv, { cwd, timeoutMs })
  } catch (error) {
    return { failure: String(error?.message ?? error) }
  }
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
    return [lines[0].replace(/^# /, '') + ' · ' + timeOf(mtimeMs), ...lines.filter((line) => REPORT_LINE.test(line))]
  } catch (error) {
    return ["couldn't read: " + String(error?.message ?? error)]
  }
}

async function ciLine($, kit, root, head) {
  const upstream = await run($, ['git', 'rev-parse', '--verify', '--quiet', '@{u}'], root)
  if (upstream.failure) return "CI: couldn't read: " + upstream.failure
  if (upstream.exitCode !== 0) return 'CI: HEAD not pushed (no upstream)'
  const pushed = await run($, ['git', 'merge-base', '--is-ancestor', 'HEAD', '@{u}'], root)
  if (pushed.failure || pushed.exitCode > 1) return "CI: couldn't read: " + failureOf(pushed)
  if (pushed.exitCode === 1) return 'CI: HEAD not pushed'
  const ci = await run($, [kit + '/bin/ci-wait', '--sha', head, '--once', '--no-log'], root, CI_TIMEOUT_MS)
  const outcome = ci.failure ? undefined : CI_OUTCOMES[ci.exitCode]
  if (!outcome) return "CI: couldn't read: " + failureOf(ci)
  const status = await run($, ['git', 'status', '--porcelain'], root)
  const dirty = status.failure || status.exitCode !== 0 || status.stdout.trim() !== ''
  return 'CI: ' + outcome + (dirty ? ', for ' + head.slice(0, 7) + ' without the uncommitted changes' : '')
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

// The facts for the change the session is on: { none } when there is no change to follow, otherwise a
// heading naming the branch and HEAD they describe, and one list of lines per section.
async function collect($) {
  const kit = await kitDir($)
  const cwd = await $.session.cwd()
  const top = await run($, ['git', 'rev-parse', '--show-toplevel'], cwd)
  if (top.failure || top.exitCode !== 0) return { none: 'Not in a git repository: no change to follow.' }
  const root = top.stdout.trim()
  const [branch, head] = await Promise.all([
    run($, ['git', 'branch', '--show-current'], root),
    run($, ['git', 'rev-parse', 'HEAD'], root),
  ])
  const branchName = branch.stdout?.trim()
  const headSha = head.stdout?.trim()
  if (!branchName || !headSha) return { none: 'Detached HEAD: no change to follow.' }
  const [brief, stamps, report, ci, context, gatheredAt] = await Promise.all([
    run($, [kit + '/bin/reports', 'brief'], root),
    Promise.all(STAMPS.map(([label, kind]) => stampLine($, kit, root, label, kind))),
    verifyReportLines($, kit, root),
    ciLine($, kit, root, headSha),
    contextLine($),
    $.clock.now(),
  ])
  let flow
  if (brief.failure || brief.exitCode !== 0) flow = ["couldn't read: " + failureOf(brief)]
  else {
    flow = brief.stdout.split('\n').filter((line) => line && !/^\s/.test(line) && !line.startsWith('Read one in full'))
    if (flow[0] === 'phase: ') return { none: 'On the default branch: no change to follow.' }
  }
  return {
    root,
    heading: branchName + ' @ ' + headSha.slice(0, 7) + ' · gathered ' + timeOf(gatheredAt),
    sections: [['Flow', flow], ['Stamps', stamps], ['Last verify report', report], ['CI and context', [ci, context]]],
  }
}

async function gather($) {
  const mine = ++latestGathering
  const facts = await collect($)
  if (mine !== latestGathering) return
  shown = facts
  $.ui.invalidate('ui.render')
}

function asText(facts) {
  if (facts.none) return facts.none
  return [facts.heading, ...facts.sections.flatMap(([title, lines]) => ['', title + ':', ...lines.map((l) => '  ' + l)])].join('\n')
}

function verifyLines() {
  if (verifyRun.state === 'idle') return []
  if (verifyRun.state === 'running') return ['Run verify: running…']
  return ['Run verify: ' + verifyRun.verdict, ...verifyRun.tail]
}

async function runVerify($) {
  if (verifyRun.state === 'running' || !shown?.root) return
  const root = shown.root
  verifyRun = { state: 'running' }
  $.ui.invalidate('ui.render')
  const kit = await kitDir($)
  let output = ''
  let verdict
  try {
    const child = $.process.spawn({ argv: ['python3', kit + '/hooks/stop_checks.py', 'verify'], cwd: root })
    let step = await child.next()
    for (; !step.done; step = await child.next()) output += step.value.text
    const { code, signal } = step.value
    if (code !== 0) verdict = 'failed (' + (signal ? 'killed by ' + signal : 'exit ' + code) + ')'
  } catch (error) {
    verdict = "failed: couldn't run: " + String(error?.message ?? error)
  }
  if (!verdict) {
    const empty = await run($, ['python3', kit + '/hooks/review_stamp.py', 'check', '--kind', 'verify-empty'], root)
    verdict = empty.exitCode === 0 ? 'passed, but it checked nothing' : 'passed'
  }
  const tail = output.split('\n').filter((line) => line.trim()).slice(-VERIFY_TAIL_LINES)
  verifyRun = { state: 'done', verdict, tail }
  await gather($)
}

export function register(on) {
  on('session.start', async ($, e, next) => {
    await $.command.register({ name: 'flow', description: FEATURES[0].description, immediate: true })
    return next(e)
  })

  on('command.run', { command: 'flow' }, async ($) => {
    if ((await $.session.surfaces()).length === 0) return { text: asText(await collect($)) }
    paneOpen = true
    await $.ui.open({ id: PANE, title: 'flow', focus: true, closeOnEscape: true })
    void gather($)
    return {}
  })

  on('turn.complete', async ($, e, next) => {
    if (paneOpen && !e.agentId) void gather($)
    return next(e)
  })

  on('ui.close', async ($, e, next) => {
    if (e.id === PANE) paneOpen = false
    return next(e)
  })

  on('ui.render', { component: 'Pane' }, async ($, e, next) => {
    if (e.requestId !== PANE) return next(e)
    const { Box, Text, Button } = $.ui.resolve(e)
    const body = !shown
      ? [Text({ children: ['Gathering…'] })]
      : shown.none
        ? [Text({ children: [shown.none] })]
        : [
            Text({ bold: true, children: [shown.heading] }),
            ...shown.sections.flatMap(([title, lines]) => [
              Text({ children: [' '] }),
              Text({ bold: true, children: [title] }),
              ...lines.map((line) => Text({ dimColor: line.includes("couldn't read"), wrap: 'wrap', children: [line] })),
            ]),
          ]
    return Box({
      flexDirection: 'column',
      children: [
        ...body,
        Text({ children: [' '] }),
        Box({
          flexDirection: 'row',
          columnGap: 2,
          children: [
            Button({ key: 'run-verify', label: 'Run verify (v)', hotkey: 'v', onPress: () => runVerify($) }),
            Button({ key: 'refresh', label: 'Refresh (r)', hotkey: 'r', onPress: () => gather($) }),
          ],
        }),
        ...verifyLines().map((line) => Text({ wrap: 'wrap', children: [line] })),
      ],
    })
  })
}
