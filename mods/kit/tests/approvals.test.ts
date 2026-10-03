import { expect, test } from 'claude-code/testing'
import { FEATURES } from '../hooks/features.js'

const KIT = '/home/.agents'
const APPROVE = KIT + '/bin/approve'
const SESSION = 'sess-1'
const DROP = "psql -c 'DROP TABLE runs'"
const BLOCKED = 'PreToolUse:Bash hook error: Blocked by ~/.agents/hooks/guard_bash.py: `DROP DATABASE|TABLE|SCHEMA`: destroys database data.'
const NEEDED = { names: ['sql.drop-table'], what: '`DROP TABLE runs` destroys database data', scope: 'any DROP TABLE' }
const ALLOW_TURN = 'Allow any DROP TABLE until my next message'

type Answer = string | { deny: string }
type Run = { exitCode: number; stdout?: string; stderr?: string } | { deny: string }

// Stubs the session, bin/approve and the dialog. `core` answers the tool call each time the mod runs it, given
// the names granted so far; by default it blocks until sql.drop-table is.
function stub(on, { answer = 'Allow once' as Answer, needed = { exitCode: 0, stdout: JSON.stringify(NEEDED) } as Run,
  grant = { exitCode: 0 } as Run, revoke = { exitCode: 0 } as Run, core = null } = {}) {
  const ran: { argv: string[]; stdin?: string }[] = []
  const asked: any[] = []
  const calls: any[] = []
  const logs: any[] = []
  let granted: string[] = []
  const ok = (run: Run) => !('deny' in run) && run.exitCode === 0
  const answerWith = (run: Run) => ('deny' in run ? run : { value: { stdout: '', stderr: '', ...run } })
  on('env.get', () => ({ value: '/home' }))
  on('session.id', () => ({ value: SESSION }))
  on('session.cwd', () => ({ value: '/work/repo' }))
  on('ui.log', ($, e) => {
    logs.push(e)
    return { value: undefined }
  })
  on('process.run', ($, e) => {
    ran.push({ argv: e.argv, stdin: e.init?.stdin })
    const [command, verb, session, ...names] = e.argv
    if (command !== APPROVE) return { deny: 'unexpected command: ' + e.argv.join(' ') }
    if (verb === 'needed') return answerWith(needed)
    if (verb === 'grant' && session === SESSION && ok(grant)) granted = names
    if (verb === 'revoke' && ok(revoke)) granted = granted.filter((n) => !names.includes(n))
    return answerWith(verb === 'grant' ? grant : revoke)
  })
  on('tool.call', ($, e) => {
    // $.ui.ask is a call of the dialog's tool, through every hook but the asking one.
    if (e.tool === 'AskUserQuestion') {
      asked.push(e)
      if (typeof answer !== 'string') return answer
      return { result: { questions: e.questions, answers: { [e.questions[0].question]: answer } } }
    }
    calls.push(e)
    if (core) return core(e, granted)
    return granted.includes('sql.drop-table') ? { result: { stdout: 'dropped' }, text: 'dropped' } : { isError: true, result: BLOCKED, text: BLOCKED }
  })
  return { ran, asked, calls, logs, granted: () => granted, argvs: () => ran.map((r) => r.argv.join(' ')) }
}

const bash = ($, command = DROP) => $.tool.call({ tool: 'Bash', command, description: 'drop it' })

test('features.js lists the approval dialog the tool.call hook below shows', async () => {
  const hook = FEATURES.find((feature) => feature.hook === 'tool.call')
  expect(hook?.capabilities.map((c) => c.name).join(' ')).toContain('dialog')
  expect(hook?.capabilities.every((c) => c.codex)).toBe(true)
})

test('allow once: the same call runs again with a grant the hook takes back right after', async ($, on) => {
  const { ran, asked, calls, granted } = stub(on)
  const result = await bash($)
  expect(result.text).toBe('dropped')
  expect(calls.length).toBe(2)
  expect(calls[1]).toEqual(calls[0])
  expect(ran.map((r) => r.argv)).toEqual([[APPROVE, 'needed'], [APPROVE, 'grant', SESSION, 'sql.drop-table'],
    [APPROVE, 'revoke', SESSION, 'sql.drop-table']])
  expect(JSON.parse(ran[0].stdin)).toEqual({ tool_name: 'Bash', tool_input: { command: DROP, description: 'drop it' },
    session_id: SESSION, cwd: '/work/repo', deny: BLOCKED })
  expect(granted()).toEqual([])
  expect(asked.length).toBe(1)
  const [question] = asked[0].questions
  expect(question.question).toBe('A kit guard blocked this call: `DROP TABLE runs` destroys database data.\n\n' + DROP + '\n\nAllow it?')
  expect(question.header).toBe('Approval')
  expect(question.options.map((o) => o.label)).toEqual(['Allow once', ALLOW_TURN, 'Keep it blocked'])
})

test('allow until the next message: the grant stays, every name of it', async ($, on) => {
  const names = ['sql.drop-table', 'sql.truncate-table']
  const { ran, granted } = stub(on, {
    answer: 'Allow any DROP TABLE or TRUNCATE TABLE until my next message',
    needed: { exitCode: 0, stdout: JSON.stringify({ names, what: '`DROP TABLE a`, `TRUNCATE TABLE b` destroy database data', scope: 'any DROP TABLE or TRUNCATE TABLE' }) },
  })
  expect((await bash($)).text).toBe('dropped')
  expect(ran.map((r) => r.argv)).toEqual([[APPROVE, 'needed'], [APPROVE, 'grant', SESSION, ...names]])
  expect(granted()).toEqual(names)
})

test('keep it blocked: the block, saying the user declined it, and nothing granted', async ($, on) => {
  const { argvs, calls } = stub(on, { answer: 'Keep it blocked' })
  const result = await bash($)
  expect(result.deny).toBe(BLOCKED + "\nThe user was asked in a dialog and kept it blocked. Don't ask them to approve it again this turn.")
  expect(calls.length).toBe(1)
  expect(argvs()).toEqual([APPROVE + ' needed'])
})

test('words typed under Other keep it blocked and reach the agent', async ($, on) => {
  const { argvs } = stub(on, { answer: 'only on the scratch copy' })
  const result = await bash($)
  expect(result.deny).toContain('They answered: "only on the scratch copy".')
  expect(argvs()).toEqual([APPROVE + ' needed'])
})

test('a dismissed dialog, or a run with no one to ask, leaves the block as it was', async ($, on) => {
  const { argvs, calls } = stub(on, { answer: { deny: 'dismissed' } })
  const result = await bash($)
  expect(result.text).toBe(BLOCKED)
  expect(result.isError).toBe(true)
  expect(calls.length).toBe(1)
  expect(argvs()).toEqual([APPROVE + ' needed'])
})

for (const [why, needed] of [['nothing to approve', { exitCode: 0, stdout: '{"names": [], "what": "", "scope": ""}' }],
  ['bin/approve failing', { exitCode: 1, stdout: '', stderr: 'boom' }], ['bin/approve printing no JSON', { exitCode: 0, stdout: 'not json' }],
  ['bin/approve not starting', { deny: 'no process here' }]] as const) {
  test(`${why} leaves the block without asking`, async ($, on) => {
    const { asked, calls, argvs } = stub(on, { needed })
    expect((await bash($)).text).toBe(BLOCKED)
    expect(argvs()).toEqual([APPROVE + ' needed'])
    expect(asked.length).toBe(0)
    expect(calls.length).toBe(1)
  })
}

test('a grant that fails leaves the block, runs nothing again and tells the user', async ($, on) => {
  const { calls, asked, argvs, logs } = stub(on, { grant: { exitCode: 1, stderr: "approve: couldn't write the approval: disk full" } })
  expect((await bash($)).text).toBe(BLOCKED)
  expect(asked.length).toBe(1)
  expect(calls.length).toBe(1)
  expect(argvs().some((a) => a.includes('revoke'))).toBe(false)
  const line = logs.find((l) => l.text.startsWith("kit: couldn't record"))
  expect(line?.text).toBe("kit: couldn't record your approval, so the call stays blocked: exit 1: approve: couldn't write the approval: disk full")
  expect(line?.to).not.toBe('debug')
})

test("a revoke that fails keeps the call's result and tells the user the approval lasts until their next message", async ($, on) => {
  const { logs } = stub(on, { revoke: { exitCode: 1, stderr: 'boom' } })
  expect((await bash($)).text).toBe('dropped')
  const line = logs.find((l) => l.text.startsWith("kit: couldn't take back"))
  expect(line?.text).toBe("kit: couldn't take back the one-time approval of any DROP TABLE, so it lasts until your next message: exit 1: boom")
  expect(line?.to).not.toBe('debug')
})

test('a call no kit guard blocked passes untouched, without running anything', async ($, on) => {
  const { ran, asked } = stub(on, { core: () => ({ isError: true, result: 'exit 1', text: 'Exit code 1\nno such table' }) })
  expect((await bash($)).text).toBe('Exit code 1\nno such table')
  expect(ran).toEqual([])
  expect(asked).toEqual([])
})

test('a tool error that only quotes a kit block is never run again', async ($, on) => {
  const { ran, calls } = stub(on, { core: () => ({ isError: true, result: 'x', text: 'Exit code 1\n' + BLOCKED }) })
  expect((await bash($)).isError).toBe(true)
  expect(ran).toEqual([])
  expect(calls.length).toBe(1)
})

test('blocked again after the grant (a later rule): that block comes back and a once-grant is taken back', async ($, on) => {
  const other = 'PreToolUse:Bash hook error: Blocked by ~/.agents/hooks/guard_bash.py: `sudo`: runs with root privileges.'
  const { argvs, granted } = stub(on, { core: (e, names) => ({ isError: true, result: 'x', text: names.length ? other : BLOCKED }) })
  expect((await bash($, 'sudo ' + DROP)).text).toBe(other)
  expect(argvs().at(-1)).toBe(`${APPROVE} revoke ${SESSION} sql.drop-table`)
  expect(granted()).toEqual([])
})

test('allow once takes the grant back even when the second run throws, and the error still reaches the caller', async ($, on) => {
  const { argvs, granted } = stub(on, {
    core: (e, names) => {
      if (names.length) throw new Error('interrupted')
      return { isError: true, result: BLOCKED, text: BLOCKED }
    },
  })
  // The harness rejects a stub that throws with a message of its own: the rejection is what reaches the caller.
  const outcome = await bash($).then(() => 'resolved', () => 'rejected')
  expect(outcome).toBe('rejected')
  expect(argvs().at(-1)).toBe(`${APPROVE} revoke ${SESSION} sql.drop-table`)
  expect(granted()).toEqual([])
})

test('an MCP write shows the tool and its whole input, and what a turn approval covers', async ($, on) => {
  const blocked = 'PreToolUse:mcp__linear__save_issue hook error: Blocked by ~/.agents/hooks/guard_mcp.py: `save_issue` writes to linear.'
  const { ran, asked, calls } = stub(on, {
    needed: { exitCode: 0, stdout: JSON.stringify({ names: ['linear'], what: '`save_issue` writes to linear, which other people see', scope: 'every linear write' }) },
    core: (e, names) => (names.includes('linear') ? { result: { id: 'L-1' }, text: 'L-1' } : { isError: true, result: blocked, text: blocked }),
  })
  const result = await $.tool.call({ tool: 'mcp__linear__save_issue', title: 'Fix login', body: 'Steps' })
  expect(result.text).toBe('L-1')
  expect(calls[1]).toEqual(calls[0])
  expect(JSON.parse(ran[0].stdin).tool_input).toEqual({ title: 'Fix login', body: 'Steps' })
  const [question] = asked[0].questions
  expect(question.question).toContain('mcp__linear__save_issue {\n  "title": "Fix login",\n  "body": "Steps"\n}')
  expect(question.options[1].label).toBe('Allow every linear write until my next message')
})

test('an input up to 2,000 characters is shown whole; a longer one says how much is cut', async ($, on) => {
  const exact = DROP + ' -- ' + 'x'.repeat(2_000 - DROP.length - 4)
  const long = exact + 'y'.repeat(500)
  const { asked } = stub(on)
  await bash($, exact)
  await bash($, long)
  expect(asked[0].questions[0].question).toContain(exact + '\n\nAllow it?')
  expect(asked[0].questions[0].question).not.toContain('more characters')
  expect(asked[1].questions[0].question).toContain(exact + '… (500 more characters not shown)')
  expect(asked[1].questions[0].question).not.toContain('y'.repeat(500))
})
