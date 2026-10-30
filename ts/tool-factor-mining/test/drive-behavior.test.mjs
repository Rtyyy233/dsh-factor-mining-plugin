// 行为测试（0.1.22 死线生命周期 + 0.1.26 矿工门）——node 直跑：
//   node test/drive-behavior.test.mjs
// 覆盖：
//   1) 永续：连续 8 次 turn/end（continue 策略）注入全部发出——
//      「连续 5 次让位真人」分支已删（A.3.4）
//   2) 死线会话级落盘：setDeadline → 到点收官注一次 → finished(wrap-up)
//      落盘 → 重启（二次 applyDrive）不二次收官
//   3) 乙方案：重启时死线已过期 → restart-cancel + 此后静默
//   4) D2：收官/取消后用户真实消息 → 重新接管，恢复常规推进
//   5) 压缩后恢复注入（compaction/end 一次性恢复前缀）
//   6) 矿工门（2026-10-16 旁观注入事故）：写类工具调用取得身份；观察类
//      永不取得；在任者被「用户在场+写」抢占；无在场不抢占
//   7) 重启种子=矿工名册而非绑定表（16 旁观会话全量复活事故的回归测试）；
//      连续联系不上→让位；非在任者的已过点死线静默收官（unheld）
// 环境隔离：USERPROFILE/HOME 指向临时目录后再 import（drive/lane 的
// homedir 派生文件全部落临时目录，零污染真实状态）。
import assert from 'node:assert/strict'
import fs from 'node:fs'
import os from 'node:os'
import path from 'node:path'

const tmpHome = fs.mkdtempSync(path.join(os.tmpdir(), 'drive-test-home-'))
process.env.USERPROFILE = tmpHome
process.env.HOME = tmpHome

const SID = '11111111-2222-3333-4444-555555555555' // 主线（block 1/2）
const OTHER = '99999999-8888-7777-6666-555555555555' // 停机烧死线者（block 3）
const THIRD = '77777777-6666-5555-4444-333333333333' // 无死线对照（block 3）
const FRESH = 'aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee' // 未来死线（block 4）
const RECOV = 'dddddddd-eeee-ffff-0000-111111111111' // 压缩恢复（block 5）
const PRE1 = '12345678-1234-1234-1234-123456789012' // 在任矿工（block 6）
const PRE2 = '87654321-4321-4321-4321-210987654321' // 挑战者（block 6）
const BY = 'abcdef01-2345-6789-abcd-ef0123456789' // 旁观读会话（block 6）
const BOUND = '56789012-3456-7890-1234-567890123456' // 绑定从未挖掘（block 7）
const GONE = '0fedcba9-8765-4321-0fed-cba987654321' // 联系不上者（block 7）
const UNHELD = '13579bdf-0246-8ace-1357-9bdf02468ace' // 非在任过点死线（block 7）
const ALL = [SID, OTHER, THIRD, FRESH, RECOV, PRE1, PRE2, BY, BOUND, GONE, UNHELD]
fs.mkdirSync(path.join(tmpHome, '.dsh'), { recursive: true })
// 绑定表一次写全（lane.ts 的 Map 在 import 时一次性种子——后续改文件
// 不重载；测试进程内全部会话从首帧就在册）。root 各自独立，冲突仅在
// block 6 的 r-pre 上（抢占测试专用）。
fs.writeFileSync(path.join(tmpHome, '.dsh', 'factor-session-bindings.json'),
  JSON.stringify({ [SID]: 'default', [OTHER]: 'r-other', [THIRD]: 'r-third',
                   [FRESH]: 'r-fresh', [RECOV]: 'r-recov',
                   [PRE1]: 'r-pre', [PRE2]: 'r-pre', [BY]: 'r-by',
                   [BOUND]: 'r-bound', [GONE]: 'r-gone',
                   [UNHELD]: 'r-unheld' }), 'utf-8')

const sleep = (ms) => new Promise((r) => setTimeout(r, ms))
const idTailSafe = (id) => String(id).slice(-12)
const statePath = path.join(tmpHome, '.dsh', 'factor-drive-state.json')
const logPath = path.join(tmpHome, '.dsh', 'factor-drive.log')
const readState = () => JSON.parse(fs.readFileSync(statePath, 'utf-8'))
const readLog = () => fs.readFileSync(logPath, 'utf-8')

// 测试用 bundle 按需自建（esbuild 打包 src/drive.ts；不入 npm 包）
{
  const bundle = path.resolve(import.meta.dirname, '../lib/drive.test.bundle.mjs')
  try {
    fs.statSync(bundle)
  } catch {
    const { execFileSync } = await import('node:child_process')
    execFileSync('npx --yes esbuild src/drive.ts --bundle --format=esm --platform=node --outfile=lib/drive.test.bundle.mjs',
      { cwd: path.resolve(import.meta.dirname, '..'), stdio: 'inherit', shell: true })
  }
}

function makeHost(sids = [SID]) {
  const listeners = {}
  const injections = new Map() // sid -> messages
  const disposers = []
  const mkAgent = (sid) => ({
    status: 'idle',
    session: { id: sid },
    inbox: undefined,
    followup(m) {
      if (!injections.has(sid)) injections.set(sid, [])
      injections.get(sid).push(m)
    },
    cancel() {},
    whenIdle() { return Promise.resolve() },
  })
  const agents = new Map(sids.map((s) => [s, mkAgent(s)]))
  const mkCtx = () => ({
    logger: { info() {}, warn() {}, error() {} },
    inject(_deps, cb) {
      cb({
        agents: { get: (id) => agents.get(id) },
        sessions: { flush: async () => {} },
        on: (ev, fn) => { listeners[ev] = fn },
      })
    },
    // cordis effect 语义：立即执行 fn、其返回值才是清理函数（假宿主
    // 此前直接压栈 fn，dispose 时调的是注册函数——清理从未执行，跨块
    // 实例泄漏互相写状态文件）
    effect(fn) { disposers.push(fn()) },
  })
  const service = {
    forRoot: () => service,
    status: async () => ({
      loop: {
        state: 'running',
        strategy: { type: 'continue', key: `continue:1:1`, directive: '继续当前方向' },
      },
    }),
  }
  return { listeners, injections, mkCtx, service, dispose: () => { for (const d of disposers) d() } }
}

const { applyDrive } = await import('../lib/drive.test.bundle.mjs')

async function fireTurnEnd(listeners, n = 1, sid = SID) {
  for (let i = 0; i < n; i++) {
    listeners['session/event']({ id: sid },
      { type: 'turn/end', data: { reason: { kind: 'completed' }, turn: i } })
    await sleep(30)
  }
}

/** 写类工具调用（挖掘动作）——矿工门的取得/续活通道。 */
function fireMine(listeners, sid, name = 'factor_evaluate') {
  listeners['session/event']({ id: sid }, { type: 'tool/call', data: { name } })
}

/** 观察类工具调用（查状态/读 journal 等）——合法旁观行为。 */
function fireRead(listeners, sid, name = 'factor_status') {
  listeners['session/event']({ id: sid }, { type: 'tool/call', data: { name } })
}

// ---- 1. 永续：连续注入不因计数上限让位（需先取得矿工身份） ----
{
  const h = makeHost()
  const handle = applyDrive(h.mkCtx(), h.service,
    { delayMs: 0, wakeIntervalMs: 10_000 })
  fireMine(h.listeners, SID)
  await fireTurnEnd(h.listeners, 8)
  await sleep(120)
  const n = (h.injections.get(SID) ?? []).length
  assert.ok(n >= 8, `连续 8 次 turn/end 后注入数 ${n} < 8——让位分支疑似回归`)
  console.log(`[1] 永续注入 OK（${n} 注，第 6+ 注仍发）`)
  handle.clearDeadline()
  h.dispose()
}

// ---- 2. 死线收官 + 防双发 + 乙方案重启取消 + D2 重新接管 ----
{
  const h = makeHost()
  const handle = applyDrive(h.mkCtx(), h.service,
    { delayMs: 0, wakeIntervalMs: 50 })
  const iso = new Date(Date.now() + 400).toISOString()
  const r = handle.setDeadline(iso, SID)
  assert.equal(r.ok, true)
  await fireTurnEnd(h.listeners, 1)
  await sleep(600) // 死线到点
  await fireTurnEnd(h.listeners, 1)
  await sleep(100)
  const wraps = (h.injections.get(SID) ?? [])
    .filter((m) => m.content[0].text.includes('收官'))
  assert.equal(wraps.length, 1, `收官注应恰一次，实际 ${wraps.length}`)
  // 到点后再来 turn/end：finished → 静默（不二次收官）
  const before = (h.injections.get(SID) ?? []).length
  await fireTurnEnd(h.listeners, 2)
  await sleep(100)
  assert.equal((h.injections.get(SID) ?? []).length, before, '收官后必须静默')
  console.log('[2] 死线收官一次 + 收官后静默 OK')
  // 状态文件落盘（矿工名册同时在场：default → SID）
  const st = readState()
  assert.equal(st.sessions[SID].finishedHow, 'wrap-up')
  assert.equal(st.miners.default.sessionId, SID, '死线武装应登记矿工名册')
  // 重启（B.2③：收官已送达）：二次 applyDrive → 保持 wrap-up（不二次
  // 收官、不误标 restart-cancel）且静默
  const h2 = makeHost()
  applyDrive(h2.mkCtx(), h2.service, { delayMs: 0, wakeIntervalMs: 50 })
  await sleep(80)
  const st2 = readState()
  assert.equal(st2.sessions[SID].finishedHow, 'wrap-up',
    '收官已送达的重启后保持 wrap-up（防双发）')
  await fireTurnEnd(h2.listeners, 1)
  await sleep(100)
  assert.equal((h2.injections.get(SID) ?? []).length, 0, '收官后的会话必须零注入')
  console.log('[3] 收官后重启不二次收官 OK')
  // D2：用户真实消息 → 重新接管
  h2.listeners['session/event']({ id: SID },
    { type: 'user/message', data: { source: { kind: 'user' } } })
  await fireTurnEnd(h2.listeners, 1)
  await sleep(100)
  assert.ok((h2.injections.get(SID) ?? []).length >= 1,
    '用户真实消息后应恢复常规推进注入')
  const st3 = readState()
  assert.equal(st3.sessions[SID].finished, false)
  console.log('[4] D2 用户消息重新接管 OK')
  h.dispose()
  h2.dispose()
}

// ---- 3. 乙方案：停机期烧掉死线（收官未送达）→ restart-cancel + 静默；
//         无死线会话不受误伤（需自行取得矿工身份） ----
{
  // 预置：OTHER 死线已过期且收官注未发（宿主在到点前就被杀）；名册清空
  fs.writeFileSync(statePath,
    JSON.stringify({ version: 2, sessions: {
      [OTHER]: { deadline: { iso: '2020-01-01T00:00', epochMs: 1577836800000,
        label: '2020-01-01T00:00', setAtMs: 1577836800000, setBy: 'model' } } },
      miners: {} }), 'utf-8')
  const h = makeHost([SID, OTHER, THIRD])
  applyDrive(h.mkCtx(), h.service, { delayMs: 0, wakeIntervalMs: 50 })
  await sleep(80)
  const st = readState()
  assert.equal(st.sessions[OTHER].finishedHow, 'restart-cancel',
    '停机期烧掉死线（收官未发）应标 restart-cancel')
  assert.ok(typeof st.sessions[THIRD] === 'undefined'
    || !st.sessions[THIRD].deadline, '无死线会话不得被种死线')
  await fireTurnEnd(h.listeners, 1, OTHER)
  fireMine(h.listeners, THIRD)
  await fireTurnEnd(h.listeners, 1, THIRD)
  await sleep(100)
  assert.equal((h.injections.get(OTHER) ?? []).length, 0,
    '乙状态会话必须零注入零收官')
  assert.ok((h.injections.get(THIRD) ?? []).length >= 1,
    '取得矿工身份的无死线会话应照常注入')
  h.dispose()
  console.log('[5] 乙方案重启取消+静默 & 不误伤无死线矿工 OK')
}

// ---- 4. B.2①④：未来死线跨重启存活；clear 后重启恢复常规推进 ----
{
  const h = makeHost([SID, OTHER, THIRD, FRESH])
  const handle = applyDrive(h.mkCtx(), h.service, { delayMs: 0, wakeIntervalMs: 50 })
  const iso = new Date(Date.now() + 60_000).toISOString()
  assert.equal(handle.setDeadline(iso, FRESH).ok, true)
  // 重启（死线在未来）：二次 applyDrive 后 status 仍见武装死线
  const h2 = makeHost([SID, OTHER, THIRD, FRESH])
  const handle2 = applyDrive(h2.mkCtx(), h2.service, { delayMs: 0, wakeIntervalMs: 50 })
  await sleep(80)
  const st = handle2.status()
  const row = (st.deadlines ?? []).find((d) => d.session === idTailSafe(FRESH))
  assert.ok(row && row.iso === iso && !row.finished,
    `未来死线应跨重启存活并保持武装：${JSON.stringify(row)}`)
  // 死线会话在死线前照常推进（不被误标）
  await fireTurnEnd(h2.listeners, 1, FRESH)
  await sleep(100)
  assert.ok((h2.injections.get(FRESH) ?? []).length >= 1,
    '未来死线武装期间常规推进不受影响')
  // B.2④ clear：清死线+清全部 finished → 重启后恢复常规推进
  handle2.clearDeadline()
  const h3 = makeHost([SID, OTHER, THIRD, FRESH])
  applyDrive(h3.mkCtx(), h3.service, { delayMs: 0, wakeIntervalMs: 50 })
  await sleep(80)
  const st3 = readState()
  for (const [id, rec] of Object.entries(st3.sessions ?? {})) {
    assert.ok(!rec.deadline && !rec.finished, `clear 后仍有残留：${id}`)
  }
  await fireTurnEnd(h3.listeners, 1, FRESH)
  await sleep(100)
  assert.ok((h3.injections.get(FRESH) ?? []).length >= 1,
    'clear 后（含重启）应恢复常规推进')
  console.log('[6] 未来死线跨重启存活 & clear 全清恢复 OK')
  h.dispose()
  h2.dispose()
  h3.dispose()
}

// ---- 5. 压缩后恢复注入（2026-10-07）：compaction/end 武装一次性恢复
//         前缀 + 主动排注（防"压缩完没人推"）；占用遥测进运维日志 ----
{
  // RECOV 已在首帧绑定表（lane import 时种子）；需先取得矿工身份
  const h = makeHost([SID, OTHER, THIRD, FRESH, RECOV])
  applyDrive(h.mkCtx(), h.service, { delayMs: 0, wakeIntervalMs: 50 })
  fireMine(h.listeners, RECOV)
  await sleep(80)
  // 占用遥测先到（真实时序：request/context 与 assistant/message 在压缩前流入）
  h.listeners['session/event']({ id: RECOV },
    { type: 'request/context', data: { contextWindow: 200000 } })
  h.listeners['session/event']({ id: RECOV },
    { type: 'assistant/message',
      data: { usage: { inputTokens: 123000, cacheReadTokens: 45000 } } })
  // compaction/start：只记日志——不武装（注入器自身在扫描驱动会话，
  // 这里只看恢复标记不出现）
  h.listeners['session/event']({ id: RECOV }, { type: 'compaction/start', data: {} })
  await sleep(80)
  assert.ok(!readLog().includes('+ctx-recovery'),
    '压缩开始不得武装恢复')
  // compaction/end：武装恢复 + 无 pending 定时器时主动排一发
  h.listeners['session/event']({ id: RECOV }, { type: 'compaction/end', data: {} })
  await sleep(150)
  const msgs = h.injections.get(RECOV) ?? []
  const k = msgs.findIndex((m) => m.content[0].text.includes('压缩为摘要'))
  assert.ok(k >= 0, '压缩结束后应有一注携带恢复前缀')
  const rtext = msgs[k].content[0].text
  assert.ok(rtext.includes('factor_trail_summary')
    && rtext.includes('factor_journal_read'), '恢复注应含恢复步骤（trail 摘要+journal）')
  assert.ok(rtext.endsWith('继续当前方向'), '恢复注尾部必须是引擎指令本体')
  // 运维日志：压缩事件 + 恢复注行同行带占用估计与恢复标记
  const log = readLog()
  assert.ok(log.includes('compaction/end'), '压缩事件应落运维日志')
  assert.ok(log.includes('ctx=168000/200000 +ctx-recovery'),
    '恢复注日志行应带占用/窗口估计与恢复标记')
  // 一次性消费：恢复注之后的常规注入不得再带恢复前缀
  await fireTurnEnd(h.listeners, 1, RECOV)
  await sleep(100)
  const msgs2 = h.injections.get(RECOV) ?? []
  assert.ok(msgs2.length > k + 1, '恢复注后常规推进应继续')
  for (let i = k + 1; i < msgs2.length; i++) {
    assert.ok(!msgs2[i].content[0].text.includes('压缩为摘要'),
      `恢复前缀必须一次性（第 ${i} 注仍在携带）`)
  }
  console.log('[7] 压缩后恢复注入：武装/主动排程/一次性/占用遥测 OK')
  h.dispose()
}

// ---- 6. 矿工门核心（2026-10-16 旁观注入事故）----------------------------
//    写类调用取得身份；观察类永不取得；无用户在场不得抢占在任者；
//    用户在场+写类调用=换矿工（抢占）；被抢占者立即停驱。
{
  const h = makeHost([PRE1, PRE2, BY])
  applyDrive(h.mkCtx(), h.service, { delayMs: 0, wakeIntervalMs: 10_000 })
  // PRE1 写类调用 → 取得 r-pre 矿工身份 → 回合边界被注入
  fireMine(h.listeners, PRE1)
  await fireTurnEnd(h.listeners, 1, PRE1)
  await sleep(80)
  assert.ok((h.injections.get(PRE1) ?? []).length >= 1, '写类调用应取得矿工身份并被驱动')
  // PRE2 无用户在场的写类调用 → 不得抢占在任者
  fireMine(h.listeners, PRE2)
  await fireTurnEnd(h.listeners, 1, PRE2)
  await sleep(80)
  assert.equal((h.injections.get(PRE2) ?? []).length, 0,
    '无用户在场的挑战者不得抢占在任矿工')
  const pre1Before = (h.injections.get(PRE1) ?? []).length
  // 用户在 PRE2 真实发言 + 写类调用 → 抢占成功
  h.listeners['session/event']({ id: PRE2 },
    { type: 'user/message', data: { source: { kind: 'user' } } })
  fireMine(h.listeners, PRE2)
  await fireTurnEnd(h.listeners, 1, PRE1) // 被抢占者的回合边界：不再注入
  await fireTurnEnd(h.listeners, 1, PRE2) // 新任矿工：照常注入
  await sleep(120)
  assert.equal((h.injections.get(PRE1) ?? []).length, pre1Before,
    '被抢占者必须立即停驱')
  assert.ok((h.injections.get(PRE2) ?? []).length >= 1,
    '用户在场+写类调用的挑战者应接管驱动权')
  assert.ok(readLog().includes('miner-claim root=r-pre'), '矿工名册变更应落运维日志')
  // BY 只做观察类调用 → 永不取得身份 → 永不注入
  fireRead(h.listeners, BY)
  fireRead(h.listeners, BY, 'factor_trail_summary')
  await fireTurnEnd(h.listeners, 2, BY)
  await sleep(80)
  assert.equal((h.injections.get(BY) ?? []).length, 0,
    '观察类工具调用（查状态/读trail）不得使会话被注入')
  console.log('[8] 矿工门：写类取得/观察类不取得/在场抢占/被抢占停驱 OK')
  h.dispose()
}

// ---- 7. 重启种子=矿工名册而非绑定表；联系不上让位；非在任死线静默 ----
{
  // 7a 回归核心：绑定表里 11 个会话全部在册，但重启后只有名册上的
  //    矿工被驱动——BOUND（绑定从未挖掘）零注入（10-16 实录：19 条
  //    绑定复活 16 个旁观会话）。
  const h = makeHost(ALL)
  applyDrive(h.mkCtx(), h.service, { delayMs: 0, wakeIntervalMs: 50 })
  await sleep(80)
  await fireTurnEnd(h.listeners, 1, BOUND)
  await fireTurnEnd(h.listeners, 1, BY)
  await sleep(100)
  assert.equal((h.injections.get(BOUND) ?? []).length, 0,
    '绑定但从未挖掘的会话重启后不得被注入（旁观复活事故回归）')
  assert.equal((h.injections.get(BY) ?? []).length, 0,
    '只读旁观会话重启后不得被注入')
  assert.ok((h.injections.get(PRE2) ?? []).length >= 1,
    '名册上的在任矿工重启后照常驱动')
  h.dispose()
  console.log('[9] 重启种子=名册：绑定≠注入目标 OK')

  // 7b 连续联系不上（agent 缺失且无重物化通道）→ 让出矿工身份
  fs.writeFileSync(statePath, JSON.stringify({ version: 2, sessions: {},
    miners: { 'r-gone': { sessionId: GONE, lastFactorCallAt: Date.now() } } }), 'utf-8')
  const h2 = makeHost([SID]) // GONE 无 agent
  applyDrive(h2.mkCtx(), h2.service, { delayMs: 0, wakeIntervalMs: 50 })
  await sleep(300) // ≥ RESUME_FAIL_DROP(3) 轮扫描
  assert.ok(readLog().includes('miner-claim-dropped root=r-gone'),
    '连续联系不上应让出矿工身份（防每5min无限重试）')
  assert.equal(readState().miners['r-gone'], undefined,
    '让位后名册应删除该条')
  h2.dispose()
  console.log('[10] 联系不上连击让位 OK')

  // 7c 非在任矿工的已过点死线：静默收官（unheld）——不注入旁观会话。
  //    （启动时已过点走乙方案 restart-cancel，见 block 3；本块测运行中
  //    过点：死线在未来 150ms，扫描到点后标记 unheld。）
  fs.writeFileSync(statePath, JSON.stringify({ version: 2, sessions: {
    [UNHELD]: { deadline: { iso: new Date(Date.now() + 150).toISOString(),
      epochMs: Date.now() + 150, label: 'soon', setAtMs: Date.now(),
      setBy: 'model' } } },
    miners: {} }), 'utf-8')
  const h3 = makeHost([UNHELD, SID])
  applyDrive(h3.mkCtx(), h3.service, { delayMs: 0, wakeIntervalMs: 50 })
  await sleep(400) // 死线到点 + 扫描轮到
  assert.equal(readState().sessions[UNHELD].finishedHow, 'unheld',
    '非在任者的运行中过点死线应静默收官')
  await fireTurnEnd(h3.listeners, 1, UNHELD)
  await sleep(100)
  assert.equal((h3.injections.get(UNHELD) ?? []).length, 0,
    '非在任者不得收到收官注（注入它=注入旁观对话）')
  h3.dispose()
  console.log('[11] 非在任过点死线静默收官 OK')
}

fs.rmSync(tmpHome, { recursive: true, force: true })
console.log('ALL DRIVE BEHAVIOR TESTS PASSED')
