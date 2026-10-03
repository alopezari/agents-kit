import { expect, test } from 'claude-code/testing'

const KIT = '/home/.agents'
const SESSION = 'sess-1'
const DROP = "psql -c 'DROP TABLE runs'"
const BLOCKED = 'PreToolUse:Bash hook error: Blocked by ~/.agents/hooks/guard_bash.py: `DROP DATABASE|TABLE|SCHEMA`: destroys database data.'
const NEEDED = { names: ['sql.drop-table'], what: '`DROP TABLE` destroys database data' }

type Answer = string | { deny: string }

// Stubs the session, bin/approve and the dialog; `core` answers the tool call each time the mod runs it.
function stub(on, { answer = 'Allow once' as Answer, needed = { exitCode: 0, stdout: JSON.stringify(NEEDED) } as any, grant = { exitCode: 0 } as any, core = null } = {}) {
  const ran: { argv: string; stdin?: string }[] = []
  const asked: any[] = []
  const calls: object[] = []
  let approved = false
  on('env.get', () => ({ value: '/home' }))
  on('session.id', () => ({ value: SESSION }))
  on('session.cwd', () => ({ value: '/work/repo' }))
  on('process.run', ($, e) => {
    const argv = e.argv.join(' ')
    ran.push({ argv, stdin: e.init?.stdin })
    if (argv === KIT + '/bin/approve needed') return 'deny' in needed ? needed : { value: { stderr: '', ...needed } }
    if (argv.startsWith(KIT + '/bin/approve grant ')) {
      if (grant.exitCode === 0) approved = true
      return { value: { stdout: '', stderr: '', ...grant } }
    }
    if (argv.startsWith(KIT + '/bin/approve revoke ')) {
      approved = false
      return { value: { exitCode: 0, stdout: '', stderr: '' } }
    }
    return { deny: 'unexpected command: ' + argv }
  })
  on('tool.call', ($, e) => {
    // $.ui.ask is a call of the dialog's tool, through every hook but the asking one.
    if (e.tool === 'AskUserQuestion') {
      asked.push(e)
      if (typeof answer !== 'string') return answer
      return { result: { questions: e.questions, answers: { [e.questions[0].question]: answer } } }
    }
    calls.push(e)
    if (core) return core(e, approved)
    return approved ? { result: { stdout: 'DROP TABLE' }, text: 'DROP TABLE' } : { isError: true, result: BLOCKED, text: BLOCKED }
  })
  return { ran, asked, calls, approved: () => approved }
}

const bash = ($, command = DROP) => $.tool.call({ tool: 'Bash', command, description: 'drop it' })

test('allow once: the call runs with a grant the hook takes back right after', async ($, on) => {
  const { ran, asked, calls, approved } = stub(on)
  const result = await bash($)
  expect(result.text).toBe('DROP TABLE')
  expect(calls.length).toBe(2)
  expect(ran.map((r) => r.argv)).toEqual([KIT + '/bin/approve needed', `${KIT}/bin/approve grant ${SESSION} sql.drop-table`,
    `${KIT}/bin/approve revoke ${SESSION} sql.drop-table`])
  expect(JSON.parse(ran[0].stdin)).toEqual({ tool_name: 'Bash', tool_input: { command: DROP, description: 'drop it' },
    session_id: SESSION, cwd: '/work/repo' })
  expect(approved()).toBe(false)
  expect(asked.length).toBe(1)
  const [question] = asked[0].questions
  expect(question.question).toContain('`DROP TABLE` destroys database data')
  expect(question.question).toContain(DROP)
  expect(question.header).toBe('Approval')
  expect(question.options.map((o) => o.label)).toEqual(['Allow once', 'Allow sql.drop-table for this turn', 'Keep it blocked'])
})

test('allow for this turn: the grant stays until the user writes again', async ($, on) => {
  const { ran, approved } = stub(on, { answer: 'Allow sql.drop-table for this turn' })
  expect((await bash($)).text).toBe('DROP TABLE')
  expect(ran.map((r) => r.argv).filter((a) => a.includes('revoke'))).toEqual([])
  expect(approved()).toBe(true)
})

test('keep it blocked: the block, saying the user declined it, and nothing granted', async ($, on) => {
  const { ran, calls } = stub(on, { answer: 'Keep it blocked' })
  const result = await bash($)
  expect(result.deny).toContain(BLOCKED)
  expect(result.deny).toContain('The user was asked in a dialog and kept it blocked.')
  expect(result.deny).not.toContain('They answered')
  expect(calls.length).toBe(1)
  expect(ran.map((r) => r.argv)).toEqual([KIT + '/bin/approve needed'])
})

test('words typed under Other keep it blocked and reach the agent', async ($, on) => {
  const { ran } = stub(on, { answer: 'only on the scratch copy' })
  const result = await bash($)
  expect(result.deny).toContain('They answered: "only on the scratch copy".')
  expect(ran.some((r) => r.argv.includes('grant'))).toBe(false)
})

test('a dismissed dialog, or a run with no one to ask, leaves the block as it was', async ($, on) => {
  const { ran, calls } = stub(on, { answer: { deny: 'dismissed' } })
  const result = await bash($)
  expect(result.text).toBe(BLOCKED)
  expect(result.isError).toBe(true)
  expect(calls.length).toBe(1)
  expect(ran.some((r) => r.argv.includes('grant'))).toBe(false)
})

for (const [why, needed] of [['nothing to approve', { exitCode: 0, stdout: '{"names": [], "what": ""}' }],
  ['bin/approve failing', { exitCode: 1, stdout: '', stderr: 'boom' }], ['bin/approve printing no JSON', { exitCode: 0, stdout: 'not json' }],
  ['bin/approve not starting', { deny: 'no process here' }]] as const) {
  test(`${why} leaves the block without asking`, async ($, on) => {
    const { asked, calls } = stub(on, { needed })
    expect((await bash($, 'sudo -n true')).text).toBe(BLOCKED)
    expect(asked.length).toBe(0)
    expect(calls.length).toBe(1)
  })
}

test('a grant that fails leaves the block and runs nothing again', async ($, on) => {
  const { calls } = stub(on, { grant: { exitCode: 2, stderr: 'approve: not a session id' } })
  expect((await bash($)).text).toBe(BLOCKED)
  expect(calls.length).toBe(1)
})

test('a call no kit guard blocked passes untouched, without running anything', async ($, on) => {
  const { ran, asked } = stub(on, { core: () => ({ isError: true, result: 'exit 1', text: 'Exit code 1\nno such table' }) })
  expect((await bash($)).text).toBe('Exit code 1\nno such table')
  expect(ran).toEqual([])
  expect(asked).toEqual([])
})

test('blocked again after the grant (another guard): that block comes back and a once-grant is taken back', async ($, on) => {
  const other = 'PreToolUse:Bash hook error: Blocked by ~/.agents/hooks/guard_bash.py: `sudo`: runs with root privileges.'
  const { ran, approved } = stub(on, { core: (e, ok) => ({ isError: true, result: ok ? other : BLOCKED, text: ok ? other : BLOCKED }) })
  expect((await bash($, 'sudo psql -c "DROP TABLE runs"')).text).toBe(other)
  expect(ran.at(-1).argv).toBe(`${KIT}/bin/approve revoke ${SESSION} sql.drop-table`)
  expect(approved()).toBe(false)
})

test('an MCP write shows the tool and its input', async ($, on) => {
  const blocked = 'PreToolUse:mcp__linear__save_issue hook error: Blocked by ~/.agents/hooks/guard_mcp.py: `save_issue` writes to linear.'
  const { ran, asked } = stub(on, {
    needed: { exitCode: 0, stdout: JSON.stringify({ names: ['linear'], what: '`save_issue` writes to linear, which other people see' }) },
    core: (e, ok) => (ok ? { result: { id: 'L-1' }, text: 'L-1' } : { isError: true, result: blocked, text: blocked }),
  })
  const result = await $.tool.call({ tool: 'mcp__linear__save_issue', title: 'Fix login' })
  expect(result.text).toBe('L-1')
  expect(JSON.parse(ran[0].stdin).tool_input).toEqual({ title: 'Fix login' })
  expect(asked[0].questions[0].question).toContain('mcp__linear__save_issue {"title":"Fix login"}')
})

test('a tool error that only quotes a kit block is never run again', async ($, on) => {
  const { ran, calls } = stub(on, { core: () => ({ isError: true, result: 'x', text: 'Exit code 1\n' + BLOCKED }) })
  expect((await bash($)).isError).toBe(true)
  expect(ran).toEqual([])
  expect(calls.length).toBe(1)
})

test('allow once takes the grant back even when the second run throws', async ($, on) => {
  const { ran, approved } = stub(on, {
    core: (e, ok) => {
      if (ok) throw new Error('interrupted')
      return { isError: true, result: BLOCKED, text: BLOCKED }
    },
  })
  await bash($).catch(() => null)
  expect(ran.at(-1).argv).toBe(`${KIT}/bin/approve revoke ${SESSION} sql.drop-table`)
  expect(approved()).toBe(false)
})

test('a long input is shown up to 2,000 characters, saying how much is cut', async ($, on) => {
  const long = DROP + ' -- ' + 'x'.repeat(2_500)
  const { asked } = stub(on)
  await bash($, long)
  const question = asked[0].questions[0].question
  expect(question).toContain(long.slice(0, 2_000) + '… (' + (long.length - 2_000) + ' more characters not shown)')
  expect(question).not.toContain(long)
})
