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
const NEEDS_ATTENTION = /couldn't read|^Couldn't|^CI: failed|^Run verify.* failed/

// Each self-review lens becomes the agent type `kit:review-<key>`, briefed from what `bin/triage --lens-briefs` cuts
// out of lenses.md. No edit tools is a convenience for the reviewer, not a guard (Bash can still write): the hooks
// run for subagents too.
const REVIEWER_TOOLS = ['Read', 'Grep', 'Glob', 'Bash']
// Registering takes tens of milliseconds and must land before the first turn (`claude -p` starts one at once), so the
// session start waits for it, but never longer than this.
const REVIEWERS_WAIT_MS = 2_000

// What the pane draws. One gathering runs at a time: a request during one gathers again once it ends.
let shown = null
let gathering = false
let gatherAgain = false
let verifyRun = { state: 'idle' }
// A verify stopped at its deadline holds the button until its child is gone, so two never write one report.
let verifyStopping = false
// Marks run one at a time, in the order pressed, each shown as pending until it lands.
let marking = Promise.resolve()
let pendingMarks = []
// { root, text }: shown while the pane shows that checkout, whatever its branch now: a refusal says the branch moved.
let markFailure = null
// A step's title is cut to this, so its result and evidence stay on the line.
const TITLE_CHARS = 60

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

// { steps } for the guide's steps before the merge, or { failure } when bin/staging can't list them.
async function stagingSteps($, kit, root) {
  const listed = await run($, [kit + '/bin/staging', 'steps', '--json'], root)
  if (listed.failure || listed.exitCode !== 0) return { failure: "couldn't read: " + failureOf(listed) }
  try {
    return { steps: JSON.parse(listed.stdout) }
  } catch (error) {
    return { failure: "couldn't read: bin/staging printed something not JSON: " + messageOf(error) }
  }
}

function stepLine(step) {
  const title = step.title.length > TITLE_CHARS ? step.title.slice(0, TITLE_CHARS - 1) + '…' : step.title
  const result = step.result ? step.result + (step.by ? ' (' + step.by + ')' : '') : 'not marked'
  const evidence = step.evidence.length ? step.evidence.map((path) => path.split('/').pop()).join(', ') : 'no evidence yet'
  return [step.id + ' ' + title, result, evidence].join(' · ')
}

function markStep($, id, result) {
  if (!shown?.root) return marking
  // What the pressed button showed: a gathering can move the pane before this mark's turn comes.
  const { root, branch } = shown
  const label = id + ' ' + (result === 'PASS' ? 'Pass' : 'Fail')
  pendingMarks.push(label)
  $.ui.invalidate('ui.render')
  marking = marking.then(async () => {
    try {
      const kit = await kitDir($)
      // --branch: the checkout itself may have moved since the pane showed this step.
      const marked = await run($, [kit + '/bin/staging', 'mark', id, result, '--by', 'user', '--branch', branch], root)
      if (marked.failure || marked.exitCode !== 0) markFailure = { root, text: `Couldn't mark ${label}: ` + failureOf(marked) }
      else if (markFailure?.root === root) markFailure = null
    } finally {
      pendingMarks.splice(pendingMarks.indexOf(label), 1)
    }
    await gather($)
  }).catch((error) => {
    // A broken chain would leave every later press doing nothing.
    markFailure = { root, text: `Marked ${label}, or not: the pane failed while it ran: ` + messageOf(error) }
    $.ui.invalidate('ui.render')
  })
  return marking
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

// The checkout at `cwd`: { root, branch, head, inMain? }, or { none } saying why there is no change to follow.
async function checkout($, cwd) {
  const top = await run($, ['git', 'rev-parse', '--show-toplevel'], cwd)
  if (top.failure) return { none: "Couldn't read the repository: " + top.failure }
  // git exits 128 for "not a git repository", and for a refused or broken one too.
  if (top.exitCode !== 0 && /not a git repository/.test(top.stderr)) return { none: 'Not in a git repository: no change to follow.' }
  if (top.exitCode !== 0) return { none: "Couldn't read the repository: " + failureOf(top) }
  const at = await branchAt($, top.stdout.trim())
  if (at.none || at.branch) return at
  return (await mainCheckout($, at.root)) ?? { none: 'Detached HEAD: no change to follow.' }
}

async function branchAt($, root) {
  const [branch, head] = await Promise.all([
    run($, ['git', 'branch', '--show-current'], root),
    run($, ['git', 'rev-parse', 'HEAD'], root),
  ])
  if (head.failure || head.exitCode !== 0) return { none: "Couldn't read HEAD: " + failureOf(head) }
  if (branch.failure || branch.exitCode !== 0) return { none: "Couldn't read the branch: " + failureOf(branch) }
  return { root, branch: branch.stdout.trim(), head: head.stdout.trim() }
}

// validate step 7 frees the branch by detaching the session's worktree, and the user checks it out in the main one.
async function mainCheckout($, root) {
  const listed = await run($, ['git', 'worktree', 'list', '--porcelain'], root)
  if (listed.failure || listed.exitCode !== 0) return { none: "Detached HEAD, and couldn't find the main checkout: " + failureOf(listed) }
  const first = listed.stdout.split('\n\n')[0]
  const main = /^worktree (.+)$/m.exec(first)?.[1]
  if (!main || main === root || /^bare$/m.test(first)) return null
  const at = await branchAt($, main)
  return at.none || at.branch ? { ...at, inMain: !at.none } : null
}

function clip(text) {
  return text.length > LINE_CHARS ? text.slice(0, LINE_CHARS - 1) + '…' : text
}

function nameOf(at) {
  return at.branch + ' @ ' + at.head.slice(0, 7) + (at.inMain ? ' in the main checkout' : '')
}

function sameCheckout(a, b) {
  return !a.none && !b.none && a.root === b.root && a.branch === b.branch && a.head === b.head
}

// The facts for the change the session is on: { none } when there is no change to follow, otherwise a
// heading naming the branch and HEAD they describe, and one list of lines per section.
async function collect($) {
  const kit = await kitDir($)
  for (let attempt = 1; ; attempt++) {
    const at = await checkout($, await $.session.cwd())
    if (at.none) return at
    // The brief comes first: on the default branch it is the only answer, and CI can take ninety seconds.
    const brief = await run($, [kit + '/bin/reports', 'brief'], at.root)
    let flow
    if (brief.failure || brief.exitCode !== 0) flow = ["couldn't read: " + failureOf(brief)]
    else {
      flow = brief.stdout.split('\n').filter((line) => line && !/^\s/.test(line) && !line.startsWith('Read one in full'))
      if (flow[0] === 'phase: ') {
        return { none: at.inMain ? 'Detached HEAD, and the main checkout is on the default branch: no change to follow.' : 'On the default branch: no change to follow.' }
      }
    }
    const [stamps, report, staging, ci, context, gatheredAt] = await Promise.all([
      Promise.all(STAMPS.map(([label, kind]) => stampLine($, kit, at.root, label, kind))),
      verifyReportLines($, kit, at.root),
      stagingSteps($, kit, at.root),
      ciLine($, kit, at.root, at.head),
      contextLine($),
      $.clock.now(),
    ])
    // Read again from the session's directory: Claude Code's /cd can move it while this gathers.
    const after = await checkout($, await $.session.cwd())
    if (after.none) return after
    const moved = !sameCheckout(at, after)
    if (moved && attempt < GATHER_ATTEMPTS) continue
    return {
      root: at.root,
      branch: at.branch,
      heading: nameOf(at) + ' · gathered ' + timeOf(gatheredAt) + (moved ? ' · the checkout moved while gathering: Refresh' : ''),
      sections: [['Flow', flow], ['Stamps', stamps], ['Last verify report', report], ['CI and context', [ci, context]]],
      staging,
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
  try {
    $.ui.invalidate('ui.render')
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
  if (facts.none) return clip(facts.none)
  return [clip(facts.heading), ...facts.sections.flatMap(([title, lines]) => ['', title + ':', ...lines.map((l) => '  ' + clip(l))])].join('\n')
}

function verifyLines() {
  if (verifyRun.state === 'idle') return []
  if (verifyRun.state === 'running') return ['Run verify: running…']
  return ['Run verify ' + verifyRun.verdict, ...(verifyStopping ? ['stopping it: the button works again once it has ended'] : []), ...verifyRun.tail]
}

// One verify on the checkout the pane follows now: its verdict and the last lines of its own output.
async function verifyHere($) {
  const at = await checkout($, await $.session.cwd())
  if (at.none) return { verdict: 'did not run: ' + at.none, tail: [] }
  const kit = await kitDir($)
  const startedAt = await $.clock.now()
  const named = (verdict) => ({ verdict: 'on ' + nameOf(at) + ' at ' + timeOf(startedAt) + ': ' + verdict, tail })
  const tail = []
  const partial = { stdout: '', stderr: '' }
  let checkedNothing = false
  // The marker is stop_checks.py's own, on stderr; the repo's verify output reaches stdout.
  const keep = (line, stream) => {
    if (stream === 'stderr' && line.startsWith(CHECKED_NOTHING)) checkedNothing = true
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
        verifyStopping = true
        child.return()
          .catch((error) => $.ui.log('flow: stopping verify: ' + messageOf(error), { to: 'debug' }))
          .finally(() => {
            verifyStopping = false
            $.ui.invalidate('ui.render')
          })
        return named('failed (no result after ' + VERIFY_DEADLINE_MS / 1000 + ' s)')
      }
      if (step.done) {
        keep(partial.stdout, 'stdout')
        keep(partial.stderr, 'stderr')
        const { code, signal } = step.value
        if (code !== 0) return named('failed (' + (signal ? 'killed by ' + signal : 'exit ' + code) + ')')
        return named(checkedNothing ? 'passed, but it checked nothing' : 'passed')
      }
      const stream = step.value.stream === 'stderr' ? 'stderr' : 'stdout'
      const lines = (partial[stream] + step.value.text).split('\n')
      partial[stream] = lines.pop().slice(0, LINE_CHARS + 1)
      lines.forEach((line) => keep(line, stream))
    }
  } finally {
    timer.abort()
  }
}

async function runVerify($) {
  if (verifyRun.state === 'running' || verifyStopping) return
  verifyRun = { state: 'running' }
  $.ui.invalidate('ui.render')
  try {
    verifyRun = { state: 'done', ...(await verifyHere($)) }
  } catch (error) {
    verifyRun = { state: 'done', verdict: "failed: couldn't run: " + messageOf(error), tail: [] }
  }
  await gather($)
}

function reviewerPrompt(lens) {
  return [
    `You are one reviewer in the kit's self-review, with one lens: ${lens.title}. You read files and run read-only`,
    'commands; you never edit files. The spawn prompt gives the base ref, the goal, the spec when there is one, and how',
    "to report. The repository's own AGENTS.md or CLAUDE.md, when it has one, holds its conventions: read it when a",
    'finding depends on them.',
    '',
    lens.brief,
  ].join('\n')
}

async function registerReviewers($) {
  const kit = await kitDir($)
  const briefs = await run($, [kit + '/bin/triage', '--lens-briefs'], kit)
  if (briefs.failure || briefs.exitCode !== 0) {
    return $.ui.log('kit: no review agents: bin/triage --lens-briefs: ' + failureOf(briefs), { to: 'debug' })
  }
  let lenses
  try {
    lenses = JSON.parse(briefs.stdout)
  } catch (error) {
    return $.ui.log('kit: no review agents: bin/triage --lens-briefs printed something not JSON: ' + messageOf(error), { to: 'debug' })
  }
  if (!Array.isArray(lenses)) {
    return $.ui.log('kit: no review agents: bin/triage --lens-briefs printed JSON that is not a list of lenses', { to: 'debug' })
  }
  for (const lens of lenses) {
    await $.agent
      .register({
        name: 'review-' + lens.key,
        description: `The self-review's ${lens.title} lens: a read-only reviewer of a change. Give it the base ref, ` +
          'the goal, the spec path and the evidence instruction.',
        prompt: reviewerPrompt(lens),
        tools: REVIEWER_TOOLS,
        omitClaudeMd: true,
      })
      .catch((error) => $.ui.log(`kit: review-${lens.key} not registered: ` + messageOf(error), { to: 'debug' }))
  }
}

// A kit hook's PreToolUse deny as Claude Code words it when next(e) hands it back: the call never ran. Only the
// start counts, so a tool's own error that quotes a deny is never run again.
const KIT_BLOCK = /^PreToolUse:\S+ hook error: Blocked by ~\/\.agents\/hooks\//
const ALLOW_ONCE = 'Allow once'
const KEEP_BLOCKED = 'Keep it blocked'
// The user approves what they read: a cut preview says how much it leaves out.
const PREVIEW_CHARS = 2_000

async function approveHelper($, kit, args, stdin = '') {
  try {
    const done = await $.process.run([kit + '/bin/approve', ...args], { stdin, timeoutMs: PROCESS_TIMEOUT_MS })
    return done.exitCode === 0 ? done : { failure: failureOf(done) }
  } catch (error) {
    return { failure: messageOf(error) }
  }
}

function previewOf(tool, input) {
  const call = tool === 'Bash' ? String(input.command) : tool + ' ' + JSON.stringify(input, null, 2)
  if (call.length <= PREVIEW_CHARS) return call
  return call.slice(0, PREVIEW_CHARS) + `… (${call.length - PREVIEW_CHARS} more characters not shown)`
}

// The guards decide; this only carries the user's answer to them, as their next message would.
async function askToLiftBlock($, e, next) {
  const blocked = await next(e)
  if (!blocked.isError || !KIT_BLOCK.test(String(blocked.text ?? ''))) return blocked
  const { tool, tool_use_id, agentId, ...input } = e
  const kit = await kitDir($)
  const session = await $.session.id()
  const asked = await approveHelper($, kit, ['needed'],
    JSON.stringify({ tool_name: tool, tool_input: input, session_id: session, cwd: await $.session.cwd(), deny: blocked.text }))
  let needed = null
  try {
    needed = asked.failure ? null : JSON.parse(asked.stdout)
  } catch (error) {
    asked.failure = 'printed something not JSON: ' + messageOf(error)
  }
  if (asked.failure) $.ui.log('kit: approve needed: ' + asked.failure, { to: 'debug' })
  if (!needed?.names?.length) return blocked
  const allowTurn = `Allow ${needed.scope} until my next message`
  const answer = await $.ui
    .ask(`A kit guard blocked this call: ${needed.what}.\n\n${previewOf(tool, input)}\n\nAllow it?`,
      { header: 'Approval', options: [ALLOW_ONCE, allowTurn, KEEP_BLOCKED] })
    .catch(() => null)  // dismissed, interrupted, or a -p run with no one to ask: the block stands, as without the mod
  const outcome = answer === null ? 'dismissed, or no one to ask' : [ALLOW_ONCE, allowTurn, KEEP_BLOCKED].includes(answer) ? answer : 'answered in their own words'
  $.ui.log(`kit: approval dialog for ${needed.names.join(', ')}: ${outcome}`, { to: 'debug' })
  if (answer === null) return blocked
  if (answer !== ALLOW_ONCE && answer !== allowTurn) {
    const said = answer === KEEP_BLOCKED ? '' : ` They answered: ${JSON.stringify(answer)}.`
    return { deny: `${blocked.text}\nThe user was asked in a dialog and kept it blocked.${said} Don't ask them to approve it again this turn.` }
  }
  const granted = await approveHelper($, kit, [answer === ALLOW_ONCE ? 'once' : 'grant', session, ...needed.names])
  if (granted.failure) {
    $.ui.log("kit: couldn't record your approval, so the call stays blocked: " + granted.failure)
    return blocked
  }
  try {
    return await next(e)
  } finally {
    if (answer === ALLOW_ONCE) {
      const revoked = await approveHelper($, kit, ['revoke', session, ...needed.names])
      if (revoked.failure) $.ui.log(`kit: couldn't take back the one-time approval of ${needed.scope}, so it lasts until your next message: ` + revoked.failure)
    }
  }
}

export function register(on) {
  on('tool.call', { tool: ['Bash', /^mcp__/] }, askToLiftBlock)

  on('session.start', async ($, e, next) => {
    const flow = FEATURES.find((feature) => feature.command === 'flow')
    await $.command.register({ name: 'flow', description: flow.description, immediate: true })
    const waited = new AbortController()
    const registering = registerReviewers($)
      .catch((error) => $.ui.log('kit: review agents: ' + messageOf(error), { to: 'debug' }))
      .finally(() => waited.abort())
    const timer = $.clock.sleep(REVIEWERS_WAIT_MS, { signal: waited.signal }).then(() => 'timed out', () => 'registered')
    if ((await Promise.race([registering.then(() => 'registered'), timer])) === 'timed out') {
      await $.ui.log('kit: review agents: still registering after 2 s, so the session started without them', { to: 'debug' })
    }
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
    if (!e.agentId) {
      $.ui.panes()
        .then((panes) => panes.some((pane) => pane.id === PANE) && gather($))
        .catch((error) => $.ui.log('flow: refresh after the turn: ' + messageOf(error), { to: 'debug' }))
    }
    return next(e)
  })

  on('ui.render', { component: 'Pane' }, async ($, e, next) => {
    if (e.requestId !== PANE) return next(e)
    const { Box, Text, Button } = $.ui.resolve(e)
    const line = (text) => Text({ bold: NEEDS_ATTENTION.test(text), wrap: 'wrap', children: [clip(text)] })
    const refreshing = shown && gathering ? ' · refreshing…' : ''
    const top = !shown
      ? [Text({ children: ['Gathering…'] })]
      : shown.none
        ? [line(shown.none + refreshing)]
        : [Text({ bold: true, children: [clip(shown.heading + refreshing)] })]
    const sections = (shown?.sections ?? []).flatMap(([title, lines]) => [
      Text({ children: [' '] }),
      Text({ dimColor: true, children: [title] }),
      ...lines.map((text, i) => (title === 'Flow' && i === 0 ? Text({ bold: true, children: [clip(text)] }) : line(text))),
    ])
    // A press starts the work and returns: Claude Code skips a hook still running after 10 s.
    // Run verify only where there is a change: elsewhere a press would do nothing.
    const buttons = [
      ...(shown?.root ? [Button({ key: 'run-verify', label: 'Run verify', hotkey: 'v', plain: true, onPress: () => void runVerify($) })] : []),
      Button({ key: 'refresh', label: 'Refresh', hotkey: 'r', plain: true, onPress: () => void gather($) }),
    ]
    // One row per step before the merge, each with its own Pass and Fail: what a press records is the user's.
    // The buttons lead the row, so they line up whatever the step's line holds.
    const staging = shown?.staging
    const failure = markFailure && markFailure.root === shown?.root ? markFailure.text : null
    const steps = staging?.steps ?? []
    const stagingRows = !staging || (!staging.failure && !steps.length && !failure && !pendingMarks.length) ? [] : [
      Text({ children: [' '] }),
      Text({ dimColor: true, children: ['Staging before the merge'] }),
      ...(pendingMarks.length ? [Text({ dimColor: true, children: ['Marking ' + pendingMarks.join(', ') + '…'] })] : []),
      ...(failure ? [line(failure)] : []),
      ...(staging.failure ? [line(staging.failure)] : steps.map((step) => Box({
        flexDirection: 'row',
        columnGap: 2,
        children: [
          ...['PASS', 'FAIL'].map((result) => Button({
            key: `staging-${step.id}-${result}`,
            label: result === 'PASS' ? 'Pass' : 'Fail',
            plain: true,
            onPress: () => void markStep($, step.id, result),
          })),
          Text({ bold: step.result === 'FAIL', wrap: 'wrap', children: [clip(stepLine(step))] }),
        ],
      }))),
    ]
    // The buttons and the run they started come first: a long report scrolls the bottom of the pane away.
    return Box({
      flexDirection: 'column',
      children: [
        ...top,
        Box({ flexDirection: 'row', columnGap: 2, children: buttons }),
        ...verifyLines().map(line),
        ...stagingRows,
        ...sections,
      ],
    })
  })
}
