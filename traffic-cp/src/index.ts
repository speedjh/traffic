// traffic-cp — 트래픽 컨트롤플레인 (Cloudflare Workers + D1)
// campaign_web(FastAPI) 이식: 계정풀(투입 오래된 순 할당) / 워커 API / 캠페인·큐 / 대시보드
import { Hono } from 'hono'
import { fetchKakaoCode } from './outlook'

type Env = { DB: D1Database; BROWSER: any }
const app = new Hono<{ Bindings: Env }>()

// ── 시간 유틸 (KST 기준 날짜/시각) ─────────────────────────────────────────
const KST_OFFSET = 9 * 3600 * 1000
function nowIso(): string { return new Date().toISOString().replace('T', ' ').slice(0, 19) }
function kstToday(): string {
  return new Date(Date.now() + KST_OFFSET).toISOString().slice(0, 10)
}
function addDays(d: string, n: number): string {
  const t = new Date(d + 'T00:00:00Z').getTime() + n * 86400000
  return new Date(t).toISOString().slice(0, 10)
}

// ══════════════════════════════════════════════════════════════════════════
// 계정풀 — 투입이 제일 오래된 순 할당
// ══════════════════════════════════════════════════════════════════════════
const FAIL_LIMIT = 3
const REPORT_MAP: Record<string, [string, boolean]> = {
  success: ['ok', false], traffic_failed: ['ok', false],
  verify_required: ['verify_required', false], bad_credential: ['bad_credential', true],
  blocked: ['blocked', false], login_failed: ['unknown', false],
}

app.get('/api/accounts/kakao/stats', async (c) => {
  const db = c.env.DB
  const total = (await db.prepare('SELECT COUNT(*) n FROM kakao_accounts').first<any>())?.n ?? 0
  const byStatus = await db.prepare('SELECT status, COUNT(*) n FROM kakao_accounts GROUP BY status').all()
  const byLogin = await db.prepare('SELECT login_state, COUNT(*) n FROM kakao_accounts GROUP BY login_state').all()
  const leased = (await db.prepare('SELECT COUNT(*) n FROM kakao_accounts WHERE leased_at IS NOT NULL').first<any>())?.n ?? 0
  const never = (await db.prepare('SELECT COUNT(*) n FROM kakao_accounts WHERE last_used_at IS NULL').first<any>())?.n ?? 0
  const usedToday = (await db.prepare('SELECT COUNT(*) n FROM kakao_accounts WHERE used_date = ?').bind(kstToday()).first<any>())?.n ?? 0
  const obj = (rows: any) => Object.fromEntries((rows.results || []).map((r: any) => [r.status ?? r.login_state, r.n]))
  return c.json({ total, by_status: obj(byStatus), by_login_state: obj(byLogin), leased, never_used: never, used_today: usedToday })
})

app.get('/api/accounts/kakao', async (c) => {
  const limit = Math.min(Number(c.req.query('limit') ?? 1000), 5000)
  const rows = await c.env.DB.prepare(
    `SELECT * FROM kakao_accounts ORDER BY (last_used_at IS NULL) DESC, last_used_at ASC, id ASC LIMIT ?`
  ).bind(limit).all()
  return c.json(rows.results ?? [])
})

// 리스: 투입 오래된 순 1개 점유
app.post('/api/accounts/kakao/lease', async (c) => {
  const b = await c.req.json<any>()
  const serial = b.serial, jobId = b.job_id ?? null
  const db = c.env.DB, now = nowIso(), today = kstToday()
  // 오래된 리스 회수(30분)
  await db.prepare(`UPDATE kakao_accounts SET leased_by=NULL, leased_at=NULL, lease_job_id=NULL
    WHERE leased_at IS NOT NULL AND leased_at < ?`).bind(new Date(Date.now() - 1800000).toISOString().replace('T', ' ').slice(0, 19)).run()
  // 이미 이 기기가 든 리스 재사용
  const held = await db.prepare('SELECT * FROM kakao_accounts WHERE leased_by=? AND leased_at IS NOT NULL').bind(serial).first<any>()
  if (held) {
    if (jobId && held.lease_job_id === jobId) return c.json({ account: leaseView(held) })
    await db.prepare('UPDATE kakao_accounts SET leased_by=NULL, leased_at=NULL, lease_job_id=NULL WHERE id=?').bind(held.id).run()
  }
  const acc = await db.prepare(
    `SELECT * FROM kakao_accounts
     WHERE status='active' AND password!='' AND leased_at IS NULL
       AND (daily_limit<=0 OR used_date IS NULL OR used_date!=? OR used_today<daily_limit)
     ORDER BY (last_used_at IS NULL) DESC, last_used_at ASC, id ASC LIMIT 1`
  ).bind(today).first<any>()
  if (!acc) return c.json({ account: null })
  await db.prepare('UPDATE kakao_accounts SET leased_by=?, leased_at=?, lease_job_id=? WHERE id=?')
    .bind(serial, now, jobId, acc.id).run()
  return c.json({ account: leaseView(acc) })
})
function leaseView(a: any) {
  return { id: a.id, email: a.email, password: a.password, nickname: a.nickname,
    warmed: !!a.warmed_at, login_state: a.login_state, mail_email: a.mail_email, mail_password: a.mail_password }
}

app.post('/api/accounts/kakao/:id/report', async (c) => {
  const id = Number(c.req.param('id'))
  const b = await c.req.json<any>()
  const db = c.env.DB, now = nowIso(), today = kstToday()
  const a = await db.prepare('SELECT * FROM kakao_accounts WHERE id=?').bind(id).first<any>()
  if (!a) return c.json({ error: 'not found' }, 404)
  const [loginState, hardFail] = REPORT_MAP[b.result] ?? ['unknown', false]
  let usedToday = a.used_today, usedDate = a.used_date
  if (usedDate !== today) { usedDate = today; usedToday = 0 }
  usedToday += 1
  let failStreak = a.fail_streak, warmedAt = a.warmed_at, status = a.status
  if (b.result === 'success' || b.result === 'traffic_failed') { failStreak = 0; if (!warmedAt) warmedAt = now }
  else failStreak += 1
  if (hardFail || failStreak >= FAIL_LIMIT) status = 'inactive'
  await db.prepare(`UPDATE kakao_accounts SET login_state=?, last_result=?, last_used_at=?, total_used=total_used+1,
      used_today=?, used_date=?, fail_streak=?, warmed_at=?, status=?, note=COALESCE(?,note),
      leased_by=NULL, leased_at=NULL, lease_job_id=NULL, updated_at=? WHERE id=?`)
    .bind(loginState, b.result, now, usedToday, usedDate, failStreak, warmedAt, status, b.note || null, now, id).run()
  return c.json({ ok: true, id, status, login_state: loginState, fail_streak: failStreak })
})

// 토큰 저장 (client_id + refresh_token)
app.post('/api/accounts/kakao/:id/token', async (c) => {
  const id = Number(c.req.param('id')); const b = await c.req.json<any>()
  await c.env.DB.prepare('UPDATE kakao_accounts SET oauth_client_id=?, oauth_refresh_token=?, oauth_status=? WHERE id=?')
    .bind(b.client_id || '', b.refresh_token || '', 'unchecked', id).run()
  return c.json({ ok: true })
})

// ---- 구분 붙여넣기: 카카오메일----비번----닉----아웃룩메일----아웃룩비번----client_id----refresh_token
app.post('/api/accounts/kakao/tokens/paste', async (c) => {
  const b = await c.req.json<any>()
  const db = c.env.DB
  let matched = 0, saved = 0
  const guid = /^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$/
  const unmatched: string[] = []
  for (const line of String(b.text || '').split('\n')) {
    const parts = line.split(/----|\t|,/).map((s) => s.trim()).filter(Boolean)
    if (parts.length < 2) continue
    const email = parts[0].toLowerCase()
    let clientId = '', refresh = '', mail = ''
    for (const p of parts.slice(1)) {
      if (guid.test(p)) clientId = p
      else if (p.startsWith('M.') && p.length > 40) refresh = p
      else if (p.includes('@') && !p.toLowerCase().includes('kakao')) mail = p
    }
    const acc = await db.prepare('SELECT id FROM kakao_accounts WHERE email=?').bind(email).first<any>()
    if (!acc) { unmatched.push(email); continue }
    matched++
    if (mail) await db.prepare('UPDATE kakao_accounts SET mail_email=? WHERE id=?').bind(mail, acc.id).run()
    if (clientId && refresh) {
      await db.prepare('UPDATE kakao_accounts SET oauth_client_id=?, oauth_refresh_token=?, oauth_status=? WHERE id=?')
        .bind(clientId, refresh, 'unchecked', acc.id).run()
      saved++
    }
  }
  return c.json({ matched, tokens_saved: saved, unmatched })
})

// JSON 대량 임포트(마이그레이션/시딩) — Mac SQLite 에서 내보낸 계정 배열
app.post('/api/accounts/kakao/import-json', async (c) => {
  const b = await c.req.json<any>()
  const rows: any[] = b.accounts || []
  const db = c.env.DB
  let created = 0, updated = 0
  for (const r of rows) {
    const email = String(r.email || '').toLowerCase()
    if (!email.includes('@')) continue
    const ex = await db.prepare('SELECT id FROM kakao_accounts WHERE email=?').bind(email).first<any>()
    const cols = ['password', 'nickname', 'auth_method', 'mail_email', 'mail_password',
      'oauth_client_id', 'oauth_refresh_token', 'oauth_status', 'status', 'login_state',
      'daily_limit', 'used_today', 'used_date', 'total_used', 'fail_streak', 'last_used_at',
      'last_result', 'warmed_at', 'note']
    if (ex) {
      const sets = cols.map((k) => `${k}=?`).join(',')
      await db.prepare(`UPDATE kakao_accounts SET ${sets} WHERE email=?`)
        .bind(...cols.map((k) => r[k] ?? defaultVal(k)), email).run()
      updated++
    } else {
      const all = ['email', ...cols]
      await db.prepare(`INSERT INTO kakao_accounts (${all.join(',')}) VALUES (${all.map(() => '?').join(',')})`)
        .bind(email, ...cols.map((k) => r[k] ?? defaultVal(k))).run()
      created++
    }
  }
  return c.json({ created, updated, total: rows.length })
})
function defaultVal(k: string): any {
  if (['daily_limit'].includes(k)) return 3
  if (['used_today', 'total_used', 'fail_streak'].includes(k)) return 0
  if (['status'].includes(k)) return 'active'
  if (['login_state'].includes(k)) return 'unknown'
  if (['used_date', 'last_used_at', 'warmed_at'].includes(k)) return null
  return ''
}

// 코드 조회 — Phase 2(Browser Rendering)에서 구현. 지금은 토큰 IMAP 경로만 자리표시.
app.get('/api/accounts/kakao/:id/fetch-code', async (c) => {
  const id = Number(c.req.param('id'))
  const wait = Number(c.req.query('wait') ?? 90)
  const a = await c.env.DB.prepare('SELECT mail_email, mail_password FROM kakao_accounts WHERE id=?').bind(id).first<any>()
  if (!a) return c.json({ error: 'not found' }, 404)
  if (!a.mail_email || !a.mail_password) return c.json({ available: false, code: null, detail: 'no_mail_cred' })
  const { code, detail } = await fetchKakaoCode(c.env, a.mail_email, a.mail_password, wait)
  return c.json({ available: true, code, detail: 'cdp:' + detail, method: 'browser-rendering' })
})

// ══════════════════════════════════════════════════════════════════════════
// 기기 데이터 예산 (유심 1일 한도를 24시간에 고르게 분배)
// ══════════════════════════════════════════════════════════════════════════
const MB = 1024 * 1024
// 하루 시작 시 선행 허용치(즉시 시작 가능하도록) — 한도의 2%
const HEAD_START = 0.02
// 하드 상한 — 한도의 97%에서 정지 (백그라운드 트래픽 여유)
const HARD_STOP = 0.97

function kstDayFraction(): number {
  const ms = (Date.now() + KST_OFFSET) % 86400000
  return ms / 86400000
}

/** 유심 카운터를 받아 오늘 누적 사용량을 갱신하고 예산 판정을 돌려준다. */
async function updateUsage(db: D1Database, serial: string, counter: number | null, capMb: number) {
  const date = kstToday(), now = nowIso()
  let row = await db.prepare('SELECT * FROM device_usage WHERE serial=? AND date=?').bind(serial, date).first<any>()
  if (!row) {
    // 전날 카운터를 이어받아 자정 이후 증가분만 오늘로 집계
    const prev = await db.prepare('SELECT last_counter FROM device_usage WHERE serial=? ORDER BY date DESC LIMIT 1')
      .bind(serial).first<any>()
    const base = counter != null && prev?.last_counter != null && counter >= prev.last_counter ? prev.last_counter : counter
    await db.prepare(`INSERT INTO device_usage (serial,date,bytes,last_counter,updated_at) VALUES (?,?,?,?,?)`)
      .bind(serial, date, counter != null && base != null ? Math.max(counter - base, 0) : 0, counter, now).run()
    row = await db.prepare('SELECT * FROM device_usage WHERE serial=? AND date=?').bind(serial, date).first<any>()
  } else if (counter != null) {
    // 리부팅/인터페이스 재생성 시 카운터가 줄어들면 증가분 0 으로 취급
    const delta = row.last_counter == null ? 0 : (counter >= row.last_counter ? counter - row.last_counter : 0)
    if (delta > 0 || row.last_counter !== counter) {
      await db.prepare('UPDATE device_usage SET bytes=bytes+?, last_counter=?, updated_at=? WHERE serial=? AND date=?')
        .bind(delta, counter, now, serial, date).run()
      row.bytes += delta
      row.last_counter = counter
    }
  }
  return budgetView(row?.bytes ?? 0, capMb)
}

function budgetView(bytes: number, capMb: number) {
  const usedMb = bytes / MB
  const frac = kstDayFraction()
  const paceMb = capMb * Math.min(1, frac + HEAD_START)   // 지금 시각까지 허용된 누적량
  const hardMb = capMb * HARD_STOP
  const limitMb = Math.min(paceMb, hardMb)
  const over = usedMb >= limitMb
  // 페이스 초과분이 풀리는 데 걸리는 시간 (하드 상한이면 자정까지)
  let waitSecs = 0
  if (over) {
    waitSecs = usedMb >= hardMb
      ? Math.max(60, Math.round((1 - frac) * 86400))
      : Math.max(60, Math.round(((usedMb / capMb - HEAD_START) - frac) * 86400))
  }
  return {
    used_mb: Math.round(usedMb * 10) / 10,
    cap_mb: capMb,
    pace_mb: Math.round(limitMb * 10) / 10,
    pct: capMb > 0 ? Math.round((usedMb / capMb) * 1000) / 10 : 0,
    over,
    exhausted: usedMb >= hardMb,
    wait_secs: waitSecs,
  }
}

async function logEvent(db: D1Database, serial: string, kind: string, message = '') {
  await db.prepare('INSERT INTO device_events (serial, ts, kind, message) VALUES (?,?,?,?)')
    .bind(serial, nowIso(), kind, message.slice(0, 500)).run()
}

// ══════════════════════════════════════════════════════════════════════════
// 워커 API — claim / start / report / heartbeat
// ══════════════════════════════════════════════════════════════════════════
app.post('/api/devices/heartbeat', async (c) => {
  const b = await c.req.json<any>()
  const db = c.env.DB, serial = String(b.serial), now = nowIso()
  await db.prepare(`INSERT INTO devices (serial, host, worker_id, last_heartbeat, busy_job_id)
    VALUES (?,?,?,?,?) ON CONFLICT(serial) DO UPDATE SET host=excluded.host, worker_id=excluded.worker_id,
    last_heartbeat=excluded.last_heartbeat, busy_job_id=excluded.busy_job_id`)
    .bind(serial, b.host || '', b.worker_id || '', now, b.busy_job_id ?? null).run()
  // 기기 상태 (있는 값만 갱신)
  const sets: string[] = [], vals: any[] = []
  const put = (col: string, v: any) => { if (v !== undefined && v !== null) { sets.push(`${col}=?`); vals.push(v) } }
  put('model', b.model); put('battery', b.battery); put('charging', b.charging ? 1 : 0)
  put('screen_on', b.screen_on ? 1 : 0); put('ip', b.ip); put('carrier', b.carrier)
  put('state', b.state); put('state_detail', b.state_detail)
  if (sets.length) await db.prepare(`UPDATE devices SET ${sets.join(',')} WHERE serial=?`).bind(...vals, serial).run()

  const dev = await db.prepare('SELECT paused, daily_cap_mb FROM devices WHERE serial=?').bind(serial).first<any>()
  const capMb = dev?.daily_cap_mb ?? 8000
  const budget = await updateUsage(db, serial, b.data_counter ?? null, capMb)
  // 대기중 원격 명령 전달
  const cmds = await db.prepare(`SELECT id, cmd, args FROM device_commands WHERE serial=? AND status='pending' ORDER BY id LIMIT 5`)
    .bind(serial).all()
  if ((cmds.results || []).length) {
    await db.prepare(`UPDATE device_commands SET status='sent' WHERE serial=? AND status='pending'`).bind(serial).run()
  }
  for (const e of b.events || []) await logEvent(db, serial, String(e.kind || 'info'), String(e.message || ''))
  return c.json({ ok: true, paused: !!dev?.paused, budget, commands: cmds.results || [] })
})

app.post('/api/devices/:serial/cmd/:id/done', async (c) => {
  const b = await c.req.json<any>().catch(() => ({}))
  await c.env.DB.prepare(`UPDATE device_commands SET status=?, done_at=?, result=? WHERE id=?`)
    .bind(b.ok === false ? 'failed' : 'done', nowIso(), String(b.result || '').slice(0, 300), Number(c.req.param('id'))).run()
  return c.json({ ok: true })
})

// 대시보드 → 기기 명령 (wake/home/back/screenshot/reboot/data_toggle/stop_job)
const ALLOWED_CMDS = ['wake', 'home', 'back', 'screenshot', 'reboot', 'data_toggle', 'stop_job', 'screen_fix']
app.post('/api/devices/:serial/cmd', async (c) => {
  const serial = c.req.param('serial'); const b = await c.req.json<any>()
  if (!ALLOWED_CMDS.includes(b.cmd)) return c.json({ error: 'bad cmd' }, 400)
  const r = await c.env.DB.prepare('INSERT INTO device_commands (serial, cmd, args) VALUES (?,?,?)')
    .bind(serial, b.cmd, String(b.args || '')).run()
  return c.json({ ok: true, id: r.meta.last_row_id })
})

app.post('/api/devices/:serial/settings', async (c) => {
  const serial = c.req.param('serial'); const b = await c.req.json<any>()
  const sets: string[] = [], vals: any[] = []
  if (b.daily_cap_mb !== undefined) { sets.push('daily_cap_mb=?'); vals.push(Math.max(0, Number(b.daily_cap_mb))) }
  if (b.paused !== undefined) { sets.push('paused=?'); vals.push(b.paused ? 1 : 0) }
  if (b.label !== undefined) { sets.push('label=?'); vals.push(String(b.label).slice(0, 40)) }
  if (b.note !== undefined) { sets.push('note=?'); vals.push(String(b.note).slice(0, 300)) }
  if (!sets.length) return c.json({ ok: true })
  await c.env.DB.prepare(`UPDATE devices SET ${sets.join(',')} WHERE serial=?`).bind(...vals, serial).run()
  await logEvent(c.env.DB, serial, 'settings', JSON.stringify(b))
  return c.json({ ok: true })
})

// 화면 캡처 업로드 (워커 PC 회선 사용 — 유심 데이터와 무관)
app.post('/api/devices/:serial/screenshot', async (c) => {
  const serial = c.req.param('serial'); const b = await c.req.json<any>()
  if (!b.image) return c.json({ error: 'no image' }, 400)
  await c.env.DB.prepare('UPDATE devices SET screenshot=?, screenshot_at=? WHERE serial=?')
    .bind(String(b.image).slice(0, 700000), nowIso(), serial).run()
  return c.json({ ok: true })
})

app.get('/api/devices/:serial/screen.jpg', async (c) => {
  const r = await c.env.DB.prepare('SELECT screenshot FROM devices WHERE serial=?').bind(c.req.param('serial')).first<any>()
  if (!r?.screenshot) return c.text('no screenshot', 404)
  const bin = Uint8Array.from(atob(r.screenshot), (ch) => ch.charCodeAt(0))
  return new Response(bin, { headers: { 'Content-Type': 'image/jpeg', 'Cache-Control': 'no-store' } })
})

app.get('/api/devices', async (c) => {
  const db = c.env.DB, today = kstToday()
  const rows = await db.prepare('SELECT * FROM devices ORDER BY serial').all()
  const usage = await db.prepare('SELECT * FROM device_usage WHERE date=?').bind(today).all()
  const uMap = new Map((usage.results || []).map((u: any) => [u.serial, u]))
  const staleMs = 90000, now = Date.now()
  const out = (rows.results || []).map((d: any) => {
    const u: any = uMap.get(d.serial) || { bytes: 0, jobs_ok: 0, jobs_fail: 0, job_secs: 0 }
    const budget = budgetView(u.bytes || 0, d.daily_cap_mb ?? 8000)
    const jobs = (u.jobs_ok || 0) + (u.jobs_fail || 0)
    const avgMb = jobs > 0 ? (u.bytes || 0) / MB / jobs : 0
    const remainMb = Math.max(0, (d.daily_cap_mb ?? 8000) * HARD_STOP - (u.bytes || 0) / MB)
    const { screenshot, ...rest } = d
    return {
      ...rest,
      online: d.last_heartbeat ? (now - new Date(d.last_heartbeat + 'Z').getTime()) <= staleMs : false,
      has_screenshot: !!screenshot,
      budget,
      jobs_ok: u.jobs_ok || 0, jobs_fail: u.jobs_fail || 0, job_secs: u.job_secs || 0,
      avg_mb: Math.round(avgMb * 100) / 100,
      // 남은 데이터로 더 처리할 수 있는 예상 작업 수
      jobs_left_est: avgMb > 0 ? Math.floor(remainMb / avgMb) : null,
      jobs_per_day_est: avgMb > 0 ? Math.floor((d.daily_cap_mb ?? 8000) * HARD_STOP / avgMb) : null,
    }
  })
  return c.json(out)
})

// 기기 × 캠페인 작업 집계 (오늘 기준, date 파라미터로 변경 가능)
app.get('/api/devices/stats', async (c) => {
  const date = c.req.query('date') || kstToday()
  const rows = await c.env.DB.prepare(
    `SELECT j.device_serial serial, c.id campaign_id, c.name campaign, c.traffic_type,
            SUM(j.status='success') ok, SUM(j.status='failed') fail,
            COALESCE(SUM(j.bytes_used),0) bytes, COALESCE(SUM(j.duration_secs),0) secs
     FROM jobs j JOIN campaigns c ON c.id=j.campaign_id
     WHERE j.schedule_date=? AND j.device_serial IS NOT NULL AND j.status IN ('success','failed')
     GROUP BY j.device_serial, c.id ORDER BY ok DESC`
  ).bind(date).all()
  return c.json({ date, rows: rows.results || [] })
})

app.get('/api/events', async (c) => {
  const limit = Math.min(Number(c.req.query('limit') ?? 100), 500)
  const rows = await c.env.DB.prepare('SELECT * FROM device_events ORDER BY id DESC LIMIT ?').bind(limit).all()
  return c.json(rows.results || [])
})

// ── 캠페인 변경 이력 ─────────────────────────────────────────────────────
const parseParams = (c: any) => { try { return JSON.parse(c?.params_json || '{}') } catch { return {} } }
/** [필드, 화면 이름, 값 추출] — 변경 전/후 스냅샷을 이 기준으로 비교해 달라진 항목만 남긴다 */
const HIST_FIELDS: [string, string, (c: any) => string][] = [
  ['name', '이름', (c) => c.name ?? ''],
  ['status', '상태', (c) => c.status ?? ''],
  ['keyword', '키워드', (c) => c.keyword ?? ''],
  ['keyword2', '2차 키워드', (c) => parseParams(c).keyword2 || c.place_name || ''],
  ['place_url', '장소 URL', (c) => c.place_url ?? ''],
  ['daily_quota', '일유입량', (c) => String(c.daily_quota ?? '')],
  ['days', '일수', (c) => String(c.days ?? '')],
  ['start_date', '시작일', (c) => c.start_date ?? ''],
  ['dwell', '체류(초)', (c) => { const p = parseParams(c); return p.dwell_min != null ? `${p.dwell_min}~${p.dwell_max}` : '' }],
  ['engine', '함많찾을 버전', (c) => c.traffic_type === 'hamman_find' ? (String(parseParams(c).engine || '').toLowerCase() === 'v2' ? 'V2' : 'V1') : ''],
]
const getCampaign = (db: D1Database, id: number) => db.prepare('SELECT * FROM campaigns WHERE id=?').bind(id).first<any>()

/** before=null 이면 생성(채워진 항목 전부 기록), 아니면 달라진 항목만 기록 */
async function recordChanges(db: D1Database, before: any, after: any, action: string, source: string) {
  if (!after) return
  const now = nowIso(), stmts = []
  for (const [field, label, get] of HIST_FIELDS) {
    const nv = get(after), ov = before ? get(before) : null
    if (before ? ov === nv : nv === '') continue
    stmts.push(db.prepare(`INSERT INTO campaign_history (campaign_id,changed_at,action,field,label,old_value,new_value,source)
      VALUES (?,?,?,?,?,?,?,?)`).bind(after.id, now, action, field, label, ov, nv, source))
  }
  if (stmts.length) await db.batch(stmts)
}

// 만료 캠페인 자동 paused
async function expirePastCampaigns(db: D1Database) {
  const today = kstToday()
  const rows = await db.prepare(`SELECT * FROM campaigns WHERE status='active'`).all()
  for (const c of rows.results || []) {
    const end = addDays((c as any).start_date, Math.max((c as any).days, 1) - 1)
    if (end < today) {
      await db.prepare(`UPDATE campaigns SET status='paused' WHERE id=?`).bind((c as any).id).run()
      await recordChanges(db, c, { ...c, status: 'paused' }, '기간 만료', '자동')
    }
  }
}

app.post('/api/worker/claim', async (c) => {
  const b = await c.req.json<any>()
  const db = c.env.DB, serial = b.serial, now = nowIso(), today = kstToday()
  await db.prepare(`INSERT INTO devices (serial,host,worker_id,last_heartbeat) VALUES (?,?,?,?)
    ON CONFLICT(serial) DO UPDATE SET host=excluded.host, worker_id=excluded.worker_id, last_heartbeat=excluded.last_heartbeat`)
    .bind(serial, b.host || '', b.worker_id || '', now).run()
  await expirePastCampaigns(db)
  // 이미 이 기기에 leased/running 인 job
  let job = await db.prepare(`SELECT j.* FROM jobs j WHERE j.device_serial=? AND j.status IN ('leased','running')`).bind(serial).first<any>()
  if (!job) {
    // 기기 일시정지 / 유심 데이터 예산 초과면 새 job 을 주지 않는다
    const dev = await db.prepare('SELECT paused, daily_cap_mb FROM devices WHERE serial=?').bind(serial).first<any>()
    if (dev?.paused) return c.json({ job: null, hold: 'paused' })
    const u = await db.prepare('SELECT bytes FROM device_usage WHERE serial=? AND date=?').bind(serial, today).first<any>()
    const budget = budgetView(u?.bytes ?? 0, dev?.daily_cap_mb ?? 8000)
    if (budget.over) {
      await db.prepare(`UPDATE devices SET state='hold', state_detail=? WHERE serial=?`)
        .bind(budget.exhausted ? '1일 데이터 한도 도달' : '데이터 페이스 대기', serial).run()
      return c.json({ job: null, hold: budget.exhausted ? 'data_cap' : 'data_pace', budget })
    }
    // 캠페인 균등 분배 — 오늘 진행률(처리/일유입량)이 가장 낮은 캠페인 우선
    const camp = await db.prepare(
      `SELECT c.id, c.daily_quota,
              (SELECT COUNT(*) FROM jobs j2 WHERE j2.campaign_id=c.id AND j2.schedule_date=?
                 AND j2.status IN ('success','running','leased')) AS done
       FROM campaigns c
       WHERE c.status='active'
         AND EXISTS (SELECT 1 FROM jobs j3 WHERE j3.campaign_id=c.id AND j3.status='pending'
                      AND j3.schedule_date<=? AND (j3.scheduled_at IS NULL OR j3.scheduled_at<=?))
       ORDER BY (done * 1.0 / MAX(c.daily_quota,1)) ASC, c.id ASC LIMIT 1`
    ).bind(today, today, now).first<any>()
    if (camp) {
      job = await db.prepare(
        `SELECT * FROM jobs WHERE campaign_id=? AND status='pending' AND schedule_date<=?
           AND (scheduled_at IS NULL OR scheduled_at<=?)
         ORDER BY (schedule_date=?) DESC, schedule_date ASC, queue_seq ASC LIMIT 1`
      ).bind(camp.id, today, now, today).first<any>()
    }
    if (!job) job = await db.prepare(
      `SELECT j.* FROM jobs j JOIN campaigns c ON c.id=j.campaign_id
       WHERE j.status='pending' AND c.status='active' AND j.schedule_date<=?
         AND (j.scheduled_at IS NULL OR j.scheduled_at<=?)
       ORDER BY (j.schedule_date=?) DESC, j.schedule_date ASC, j.queue_seq ASC LIMIT 1`
    ).bind(today, now, today).first<any>()
    if (job) {
      await db.prepare(`UPDATE jobs SET status='leased', device_serial=?, worker_id=?, leased_at=? WHERE id=? AND status='pending'`)
        .bind(serial, b.worker_id || '', now, job.id).run()
    }
  }
  if (!job) {
    await db.prepare(`UPDATE devices SET busy_job_id=NULL, state='idle', state_detail='대기 중(작업 없음)' WHERE serial=?`).bind(serial).run()
    return c.json({ job: null, hold: 'no_job' })
  }
  await db.prepare(`UPDATE devices SET busy_job_id=?, state='running', state_detail='' WHERE serial=?`).bind(job.id, serial).run()
  const camp = await db.prepare('SELECT * FROM campaigns WHERE id=?').bind(job.campaign_id).first<any>()
  return c.json({ job: {
    id: job.id, campaign_id: job.campaign_id, campaign_name: camp?.name || '',
    traffic_type: camp?.traffic_type || '', keyword: camp?.keyword || '', place_url: camp?.place_url || '',
    place_name: camp?.place_name || '', params: JSON.parse(camp?.params_json || '{}'),
    schedule_date: job.schedule_date, queue_seq: job.queue_seq, scheduled_at: job.scheduled_at,
  } })
})

app.get('/api/worker/jobs/:id/runnable', async (c) => {
  const id = Number(c.req.param('id'))
  const j = await c.env.DB.prepare(`SELECT j.status, c.status cs, c.start_date, c.days FROM jobs j JOIN campaigns c ON c.id=j.campaign_id WHERE j.id=?`).bind(id).first<any>()
  if (!j) return c.json({ job_id: id, runnable: false })
  const end = addDays(j.start_date, Math.max(j.days, 1) - 1)
  const ok = ['leased', 'running'].includes(j.status) && j.cs === 'active' && end >= kstToday()
  return c.json({ job_id: id, runnable: ok })
})

app.post('/api/worker/jobs/:id/start', async (c) => {
  const id = Number(c.req.param('id'))
  // 워커가 재시작되어 이미 running 인 job 을 다시 집은 경우도 이어서 실행하게 허용
  const r = await c.env.DB.prepare(
    `UPDATE jobs SET status='running', started_at=COALESCE(started_at,?) WHERE id=? AND status IN ('leased','running')`
  ).bind(nowIso(), id).run()
  if (!r.meta.changes) return c.json({ ok: false, detail: 'not runnable' }, 409)
  return c.json({ ok: true, job_id: id, status: 'running' })
})

app.post('/api/worker/jobs/:id/report', async (c) => {
  const id = Number(c.req.param('id')); const b = await c.req.json<any>()
  const db = c.env.DB
  const status = b.status === 'success' ? 'success' : 'failed'
  const bytes = b.bytes_used != null ? Math.max(0, Number(b.bytes_used)) : null
  const secs = b.duration_secs != null ? Math.round(Number(b.duration_secs)) : null
  await db.prepare(`UPDATE jobs SET status=?, finished_at=?, error_message=?, bytes_used=?, duration_secs=? WHERE id=?`)
    .bind(status, nowIso(), b.error_message || null, bytes, secs, id).run()
  await db.prepare(`UPDATE devices SET busy_job_id=NULL WHERE busy_job_id=?`).bind(id).run()
  if (b.serial) {
    const date = kstToday()
    await db.prepare(`INSERT INTO device_usage (serial,date,jobs_ok,jobs_fail,job_secs,updated_at) VALUES (?,?,?,?,?,?)
      ON CONFLICT(serial,date) DO UPDATE SET jobs_ok=jobs_ok+excluded.jobs_ok, jobs_fail=jobs_fail+excluded.jobs_fail,
      job_secs=job_secs+excluded.job_secs, updated_at=excluded.updated_at`)
      .bind(b.serial, date, status === 'success' ? 1 : 0, status === 'success' ? 0 : 1, secs ?? 0, nowIso()).run()
  }
  return c.json({ ok: true, job_id: id, status })
})

app.post('/api/worker/jobs/:id/release', async (c) => {
  const id = Number(c.req.param('id'))
  await c.env.DB.prepare(`UPDATE jobs SET status='pending', device_serial=NULL, worker_id=NULL, leased_at=NULL, started_at=NULL WHERE id=?`).bind(id).run()
  await c.env.DB.prepare('UPDATE devices SET busy_job_id=NULL WHERE busy_job_id=?').bind(id).run()
  return c.json({ ok: true })
})

// ══════════════════════════════════════════════════════════════════════════
// 캠페인 — 생성 시 일수×일유입량 만큼 job 생성(materialize)
// ══════════════════════════════════════════════════════════════════════════
app.get('/api/campaigns', async (c) => {
  const rows = await c.env.DB.prepare('SELECT * FROM campaigns ORDER BY id DESC').all()
  return c.json(rows.results ?? [])
})

app.post('/api/campaigns', async (c) => {
  const b = await c.req.json<any>()
  const db = c.env.DB
  const start = b.start_date || kstToday()
  const r = await db.prepare(`INSERT INTO campaigns (name,traffic_type,keyword,place_url,place_name,daily_quota,start_date,days,status,params_json)
     VALUES (?,?,?,?,?,?,?,?, 'active', ?)`).bind(
    b.name, b.traffic_type, b.keyword || '', b.place_url || '', b.place_name || '',
    b.daily_quota, start, b.days, JSON.stringify(b.params || {})).run()
  const cid = r.meta.last_row_id
  await recordChanges(db, null, await getCampaign(db, Number(cid)), '생성', 'API')
  // materialize
  for (let d = 0; d < b.days; d++) {
    const day = addDays(start, d)
    const seqBase = (await db.prepare('SELECT COALESCE(MAX(queue_seq),0) m FROM jobs WHERE schedule_date=?').bind(day).first<any>())?.m ?? 0
    const stmts = []
    for (let i = 0; i < b.daily_quota; i++) {
      stmts.push(db.prepare('INSERT INTO jobs (campaign_id, schedule_date, queue_seq, status) VALUES (?,?,?,?)')
        .bind(cid, day, seqBase + i + 1, 'pending'))
    }
    if (stmts.length) await db.batch(stmts)
  }
  return c.json({ ok: true, id: cid })
})

app.post('/api/campaigns/import-json', async (c) => {
  // 캠페인 '설정'만 이전(잡 자동생성 안 함). id 보존, 이름 기준 upsert.
  const b = await c.req.json<any>()
  const rows: any[] = b.campaigns || []
  const db = c.env.DB
  let created = 0, updated = 0
  for (const r of rows) {
    const ex = await db.prepare('SELECT * FROM campaigns WHERE id=? OR name=?').bind(r.id ?? -1, r.name).first<any>()
    const vals = [r.name, r.traffic_type, r.keyword || '', r.place_url || '', r.place_name || '',
      r.daily_quota ?? 0, r.start_date || '', r.days ?? 1, r.status || 'paused', r.params_json || '{}']
    if (ex) {
      await db.prepare(`UPDATE campaigns SET name=?, traffic_type=?, keyword=?, place_url=?, place_name=?,
        daily_quota=?, start_date=?, days=?, status=?, params_json=? WHERE id=?`).bind(...vals, ex.id).run()
      await recordChanges(db, ex, await getCampaign(db, ex.id), '설정 가져오기', 'API')
      updated++
    } else if (r.id) {
      await db.prepare(`INSERT INTO campaigns (id,name,traffic_type,keyword,place_url,place_name,daily_quota,start_date,days,status,params_json)
        VALUES (?,?,?,?,?,?,?,?,?,?,?)`).bind(r.id, ...vals).run()
      created++
    } else {
      await db.prepare(`INSERT INTO campaigns (name,traffic_type,keyword,place_url,place_name,daily_quota,start_date,days,status,params_json)
        VALUES (?,?,?,?,?,?,?,?,?,?)`).bind(...vals).run()
      created++
    }
  }
  return c.json({ created, updated, total: rows.length })
})

app.post('/api/campaigns/:id/activate', async (c) => {
  // 설정만 있는 캠페인을 활성화하며 오늘부터 잡 생성(materialize)
  const id = Number(c.req.param('id')); const db = c.env.DB
  const camp = await db.prepare('SELECT * FROM campaigns WHERE id=?').bind(id).first<any>()
  if (!camp) return c.json({ error: 'not found' }, 404)
  const start = kstToday()
  await db.prepare(`UPDATE campaigns SET status='active', start_date=? WHERE id=?`).bind(start, id).run()
  await recordChanges(db, camp, await getCampaign(db, id), '활성화', 'API')
  await db.prepare('DELETE FROM jobs WHERE campaign_id=? AND status=?').bind(id, 'pending').run()
  for (let d = 0; d < camp.days; d++) {
    const day = addDays(start, d)
    const seqBase = (await db.prepare('SELECT COALESCE(MAX(queue_seq),0) m FROM jobs WHERE schedule_date=?').bind(day).first<any>())?.m ?? 0
    const stmts = []
    for (let i = 0; i < camp.daily_quota; i++)
      stmts.push(db.prepare('INSERT INTO jobs (campaign_id,schedule_date,queue_seq,status) VALUES (?,?,?,?)').bind(id, day, seqBase + i + 1, 'pending'))
    if (stmts.length) await db.batch(stmts)
  }
  return c.json({ ok: true, materialized: camp.daily_quota * camp.days })
})

app.post('/api/campaigns/:id/pause', async (c) => {
  await pauseCampaign(c.env.DB, Number(c.req.param('id')), 'API')
  return c.json({ ok: true })
})

// ── 캠페인 큐 유지 (campaign_web scheduler 이식) ─────────────────────────
const campaignEnd = (camp: any) => addDays(camp.start_date, Math.max(camp.days, 1) - 1)

/** 기간 밖·지난 날짜의 대기 job 을 지우고, 오늘~종료일 각 날짜 job 수를 일유입량에 맞춘다. */
async function syncCampaignJobs(db: D1Database, id: number) {
  const camp = await db.prepare('SELECT * FROM campaigns WHERE id=?').bind(id).first<any>()
  if (!camp) return
  const today = kstToday(), end = campaignEnd(camp)
  await db.prepare(`DELETE FROM jobs WHERE campaign_id=? AND status IN ('pending','paused')
      AND (schedule_date<? OR schedule_date>? OR schedule_date<?)`).bind(id, camp.start_date, end, today).run()
  if (camp.status !== 'active') return
  const cnt = await db.prepare(`SELECT schedule_date d, COUNT(*) n FROM jobs WHERE campaign_id=? AND status!='paused'
      AND schedule_date BETWEEN ? AND ? GROUP BY schedule_date`).bind(id, today, end).all()
  const have = new Map((cnt.results || []).map((r: any) => [r.d, r.n]))
  for (let day = camp.start_date > today ? camp.start_date : today; day <= end; day = addDays(day, 1)) {
    const diff = camp.daily_quota - (have.get(day) ?? 0)
    if (diff > 0) {
      const seqBase = (await db.prepare('SELECT COALESCE(MAX(queue_seq),0) m FROM jobs WHERE schedule_date=?').bind(day).first<any>())?.m ?? 0
      const stmts = []
      for (let i = 0; i < diff; i++)
        stmts.push(db.prepare('INSERT INTO jobs (campaign_id,schedule_date,queue_seq,status) VALUES (?,?,?,?)').bind(id, day, seqBase + i + 1, 'pending'))
      for (let i = 0; i < stmts.length; i += 500) await db.batch(stmts.slice(i, i + 500))
    } else if (diff < 0) {
      await db.prepare(`DELETE FROM jobs WHERE id IN (SELECT id FROM jobs WHERE campaign_id=? AND schedule_date=? AND status='pending'
          ORDER BY queue_seq DESC LIMIT ?)`).bind(id, day, -diff).run()
    }
  }
}

/** 중단(일시정지): 남은 대기/리스 job 을 paused 로 보관. 실행 중 job 은 워커 runnable 검사에서 멈춘다. */
async function pauseCampaign(db: D1Database, id: number, source = '대시보드') {
  const before = await getCampaign(db, id)
  await db.prepare(`UPDATE campaigns SET status='paused', updated_at=? WHERE id=?`).bind(nowIso(), id).run()
  await recordChanges(db, before, await getCampaign(db, id), '중단', source)
  await db.prepare(`UPDATE jobs SET status='paused', device_serial=NULL, worker_id=NULL, leased_at=NULL
      WHERE campaign_id=? AND status IN ('pending','leased')`).bind(id).run()
}

/** 재개: 오늘부터 N일로 기간을 새로 잡고 이전 대기분을 정리한 뒤 큐 생성. */
async function resumeCampaign(db: D1Database, id: number, quota: number, days: number) {
  if (!(quota >= 1 && days >= 1 && days <= 365)) throw new Error('일유입량·일수는 1 이상이어야 합니다 (일수 최대 365)')
  const before = await getCampaign(db, id)
  await db.prepare(`DELETE FROM jobs WHERE campaign_id=? AND status IN ('pending','paused','leased')`).bind(id).run()
  await db.prepare(`UPDATE campaigns SET status='active', daily_quota=?, days=?, start_date=?, updated_at=? WHERE id=?`)
    .bind(quota, days, kstToday(), nowIso(), id).run()
  await recordChanges(db, before, await getCampaign(db, id), '재개', '대시보드')
  await syncCampaignJobs(db, id)
}

// ══════════════════════════════════════════════════════════════════════════
// 대시보드 (HTML)
// ══════════════════════════════════════════════════════════════════════════
// 트래픽 종류 (campaign_web/traffic.py TRAFFIC_TYPES 와 동일)
const TRAFFIC_TYPES: { key: string; label: string; description: string }[] = [
  { key: 'daum_jawan', label: '다음 자완', description: '다음앱 검색 후 파워링크·관련검색어·장소 제외, 결과 글(블로그/웹/뉴스 등)을 클릭합니다.' },
  { key: 'daum_click', label: '다음 클릭형', description: '다음앱 검색 → 장소(플레이스) 클릭 → 카카오맵 전환만 확인하고 종료합니다. 맵 안 추가 액션 없음.' },
  { key: 'daum_dwell', label: '다음 체류형', description: '다음앱에서 장소 클릭 후 카카오맵으로 들어가 업체 상세 탭을 클릭·스크롤하며 체류합니다.' },
  { key: 'daum_full', label: '다음 풀플로우', description: '다음앱 장소 클릭 → 카카오맵 전환 + 업체 클릭 + 상세 스크롤까지 수행합니다.' },
  { key: 'gs_click', label: '우리동네GS 클릭', description: '우리동네GS 앱에서 하단 검색 → 키워드 입력 → GS25배달 구역 상품 1개를 클릭합니다.' },
  { key: 'kakao_search', label: '카카오맵 일반트래픽', description: "카카오맵 검색 → 업체 진입 → 업체명 클릭 후 메뉴/사진/후기/블로그를 랜덤 클릭·스크롤하며 체류합니다. 앱 데이터 삭제·IP 변경 후 진입할 때 카카오 계정으로 로그인하며, 로테이션마다 '투입이 가장 오래된' 계정으로 교체됩니다. 체류시간은 캠페인에서 설정." },
  { key: 'kakao_route', label: '카카오맵 길찾기', description: '카카오맵 앱을 직접 켜고 검색 → 업체 진입 → 도착 → 출발지 입력 → 경로 결과까지 진행합니다.' },
  { key: 'hamman_find', label: '함많찾을', description: 'Chrome에서 네이버 검색 → 결과 클릭·체류 → 2차 키워드 검색·클릭·체류 → Chrome 초기화 → IP 변경.' },
]
const trafficLabel = (k: string) => TRAFFIC_TYPES.find((t) => t.key === k)?.label ?? k
/** 함많찾을은 params.engine 으로 V1/V2 스크립트가 갈린다 (campaign_web/traffic.py) */
const isV2 = (paramsJson: any) => { try { return String(JSON.parse(paramsJson || '{}').engine || '').toLowerCase() === 'v2' } catch { return false } }
const campLabel = (type: string, paramsJson: any) =>
  type === 'hamman_find' ? (isV2(paramsJson) ? '함많찾을 V2' : '함많찾을 V1') : trafficLabel(type)
// 생성 화면 선택지 — 함많찾을 V2 는 traffic_type=hamman_find + params.engine=v2 로 저장
const HAMMAN_V2_KEY = 'hamman_find_v2'
const FORM_TYPES = TRAFFIC_TYPES.flatMap((t) => t.key !== 'hamman_find' ? [t] : [
  { key: HAMMAN_V2_KEY, label: '함많찾을 V2 (권장)', description: 'Chrome DevTools로 링크 목적지를 확인해 글만 정밀 클릭(광고·플레이스 제외) + 쿠키·스토리지만 초기화하고 캐시는 유지해 건당 데이터 6~11MB. 흐름은 V1과 동일.' },
  { ...t, label: '함많찾을 V1', description: t.description + ' (구버전 · pm clear 로 매번 전체 초기화, 건당 30~40MB)' },
])
const DWELL_TYPES = ['kakao_search', 'hamman_find', HAMMAN_V2_KEY]

const esc = (s: any) => String(s ?? '').replace(/[&<>"']/g, (ch) =>
  ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' } as any)[ch])
const badge = (s: string) => `<span class="badge ${esc(s)}">${esc(s)}</span>`
/** KST 하루를 finished_at(UTC 문자열) 비교 범위로 변환 */
function kstDayUtcRange(day: string): [string, string] {
  const s = new Date(day + 'T00:00:00Z').getTime() - KST_OFFSET
  const f = (t: number) => new Date(t).toISOString().replace('T', ' ').slice(0, 19)
  return [f(s), f(s + 86400000)]
}
/** UTC 'YYYY-MM-DD HH:MM:SS' → KST 'YYYY-MM-DD HH:MM' */
const kstStamp = (utc: string) => { const t = new Date(String(utc).replace(' ', 'T') + 'Z').getTime(); return isNaN(t) ? String(utc) : new Date(t + KST_OFFSET).toISOString().replace('T', ' ').slice(0, 16) }
const toNum = (v: any) => (v === undefined || v === null || String(v).trim() === '' ? null : Number(v))

app.get('/', async (c) => c.html(page('대시보드', await dashHtml(c.env.DB), '/')))
app.get('/accounts', (c) => c.html(page('카카오 계정', accountsHtml(), '/accounts')))
app.get('/campaigns', (c) => c.redirect('/', 302))
app.get('/devices', (c) => c.html(page('기기관리', devicesHtml(), '/devices')))

// ── 캠페인 생성 ──
app.get('/campaigns/new', (c) => c.html(page('캠페인 생성', campaignFormHtml(null), '/campaigns/new')))
app.post('/campaigns/new', async (c) => {
  const f: any = await c.req.parseBody()
  try {
    const name = String(f.name || '').trim(), picked = String(f.traffic_type || '')
    const type = picked === HAMMAN_V2_KEY ? 'hamman_find' : picked
    const quota = Number(f.daily_quota), days = Number(f.days), start = String(f.start_date || kstToday())
    if (!name) throw new Error('캠페인 이름을 입력하세요')
    if (!TRAFFIC_TYPES.some((t) => t.key === type)) throw new Error('지원하지 않는 트래픽 종류')
    if (!(quota >= 1 && days >= 1 && days <= 365)) throw new Error('일유입량·일수는 1 이상이어야 합니다 (일수 최대 365)')
    const placeName = String(f.place_name || '').trim()
    const params: any = {}
    if (DWELL_TYPES.includes(type)) {
      let dmin = toNum(f.dwell_min) ?? 15, dmax = toNum(f.dwell_max) ?? 20
      if (dmax < dmin) [dmin, dmax] = [dmax, dmin]
      params.dwell_min = dmin; params.dwell_max = dmax
      if (type === 'hamman_find' && placeName) params.keyword2 = placeName
    }
    if (picked === HAMMAN_V2_KEY) params.engine = 'v2'
    const r = await c.env.DB.prepare(`INSERT INTO campaigns (name,traffic_type,keyword,place_url,place_name,daily_quota,start_date,days,status,params_json)
       VALUES (?,?,?,?,?,?,?,?, 'active', ?)`).bind(name, type, String(f.keyword || '').trim(), String(f.place_url || '').trim(),
      placeName, quota, start, days, JSON.stringify(params)).run()
    await recordChanges(c.env.DB, null, await getCampaign(c.env.DB, Number(r.meta.last_row_id)), '생성', '대시보드')
    await syncCampaignJobs(c.env.DB, Number(r.meta.last_row_id))
    return c.redirect('/', 303)
  } catch (e: any) {
    return c.html(page('캠페인 생성', campaignFormHtml(e.message || String(e)), '/campaigns/new'), 400)
  }
})

// ── 캠페인 상세 / 설정 수정 ──
app.get('/campaigns/:id', async (c) => {
  const html = await campaignDetailHtml(c.env.DB, Number(c.req.param('id')), c.req.query('applied') ? '변경사항이 즉시 반영됩니다.' : null, null)
  return html ? c.html(page('캠페인 설정', html, '')) : c.text('campaign not found', 404)
})
app.post('/campaigns/:id/settings', async (c) => {
  const id = Number(c.req.param('id')), db = c.env.DB
  const f: any = await c.req.parseBody()
  const camp = await db.prepare('SELECT * FROM campaigns WHERE id=?').bind(id).first<any>()
  if (!camp) return c.text('campaign not found', 404)
  try {
    if (!['active', 'paused'].includes(camp.status)) throw new Error('진행 중이거나 일시정지된 캠페인만 수정할 수 있습니다')
    const params = JSON.parse(camp.params_json || '{}')
    const sets: string[] = [], vals: any[] = []
    let pacing = false
    if (f.daily_quota !== undefined || f.days !== undefined) {
      const quota = toNum(f.daily_quota) ?? camp.daily_quota, days = toNum(f.days) ?? camp.days
      if (!(quota >= 1 && days >= 1 && days <= 365)) throw new Error('일유입량·일수는 1 이상이어야 합니다 (일수 최대 365)')
      sets.push('daily_quota=?', 'days=?'); vals.push(quota, days); pacing = true
    }
    if (f.keyword !== undefined) { sets.push('keyword=?'); vals.push(String(f.keyword).trim()) }
    if (f.place_url !== undefined) { sets.push('place_url=?'); vals.push(String(f.place_url).trim()) }
    const dual = f.keyword2 !== undefined ? f.keyword2 : f.place_name
    let touchedParams = false
    if (dual !== undefined) {
      const v = String(dual).trim()
      sets.push('place_name=?'); vals.push(v)
      if (camp.traffic_type === 'hamman_find' || 'keyword2' in params) {
        if (v) params.keyword2 = v; else delete params.keyword2
        touchedParams = true
      }
    }
    if (f.engine !== undefined && camp.traffic_type === 'hamman_find') {
      if (String(f.engine) === 'v2') params.engine = 'v2'; else delete params.engine
      touchedParams = true
    }
    const dmin = toNum(f.dwell_min), dmax = toNum(f.dwell_max)
    if (dmin !== null || dmax !== null) {
      let a = dmin ?? Number(params.dwell_min ?? 15), b = dmax ?? Number(params.dwell_max ?? 20)
      if (b < a) [a, b] = [b, a]
      params.dwell_min = a; params.dwell_max = b; touchedParams = true
    }
    if (touchedParams) { sets.push('params_json=?'); vals.push(JSON.stringify(params)) }
    if (!sets.length) throw new Error('변경할 항목이 없습니다')
    sets.push('updated_at=?'); vals.push(nowIso())
    await db.prepare(`UPDATE campaigns SET ${sets.join(',')} WHERE id=?`).bind(...vals, id).run()
    await recordChanges(db, camp, await getCampaign(db, id), '설정 수정', '대시보드')
    if (pacing) {
      await expirePastCampaigns(db)
      await syncCampaignJobs(db, id)
    }
    return c.redirect(`/campaigns/${id}?applied=1`, 303)
  } catch (e: any) {
    return c.html(page('캠페인 설정', (await campaignDetailHtml(db, id, null, e.message || String(e)))!, ''), 400)
  }
})

// ── 중단 / 재개 ──
app.post('/campaigns/:id/pause', async (c) => {
  await pauseCampaign(c.env.DB, Number(c.req.param('id')))
  return c.redirect('/', 303)
})
app.get('/campaigns/:id/resume', async (c) => {
  const id = Number(c.req.param('id'))
  const camp = await c.env.DB.prepare('SELECT * FROM campaigns WHERE id=?').bind(id).first<any>()
  if (!camp) return c.text('campaign not found', 404)
  if (!['paused', 'stopped'].includes(camp.status)) return c.redirect(`/campaigns/${id}`, 303)
  return c.html(page('캠페인 재개', resumeHtml(camp, null), ''))
})
app.post('/campaigns/:id/resume', async (c) => {
  const id = Number(c.req.param('id')), db = c.env.DB
  const f: any = await c.req.parseBody()
  const camp = await db.prepare('SELECT * FROM campaigns WHERE id=?').bind(id).first<any>()
  if (!camp) return c.text('campaign not found', 404)
  try {
    if (!['paused', 'stopped'].includes(camp.status)) throw new Error('일시정지된 캠페인이 아닙니다')
    await resumeCampaign(db, id, Number(f.daily_quota), Number(f.days))
    return c.redirect('/', 303)
  } catch (e: any) {
    return c.html(page('캠페인 재개', resumeHtml(camp, e.message || String(e)), ''), 400)
  }
})

// ── 작업큐 ──
app.get('/queue', async (c) => {
  const day = /^\d{4}-\d{2}-\d{2}$/.test(c.req.query('day') || '') ? c.req.query('day')! : kstToday()
  const rows = await c.env.DB.prepare(`SELECT j.*, c.name campaign_name, c.traffic_type, c.params_json FROM jobs j JOIN campaigns c ON c.id=j.campaign_id
      WHERE j.schedule_date=? ORDER BY j.queue_seq ASC LIMIT 3000`).bind(day).all()
  return c.html(page('작업큐', queueHtml(day, rows.results || []), '/queue'))
})

const NAV: [string, string][] = [['/', '대시보드'], ['/campaigns/new', '캠페인 생성'], ['/queue', '작업큐'], ['/devices', '기기관리'], ['/accounts', '카카오 계정']]

const page = (title: string, body: string, path = '') => `<!doctype html><html lang="ko"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>${esc(title)} · Campaign Queue</title><style>
:root{--bg:#f3f1ec;--panel:#fffdf8;--ink:#1c1a17;--muted:#6b645c;--line:#ddd4c7;--accent:#0f6b5c;--accent-soft:#d7efe9;--ok:#1f6b3a;--danger:#9b2c2c;--warn:#9a4a16;--shadow:0 10px 30px rgba(40,30,20,.08);--radius:14px}
*{box-sizing:border-box}body{margin:0;font-family:"Avenir Next","Segoe UI","Apple SD Gothic Neo",sans-serif;color:var(--ink);min-height:100vh;
background:radial-gradient(ellipse at top left,#efe6d6 0%,transparent 45%),linear-gradient(180deg,#f7f4ee 0%,var(--bg) 100%)}
a{color:var(--accent);text-decoration:none}a:hover{text-decoration:underline}
.wrap{max-width:1100px;margin:0 auto;padding:24px 20px 48px}.nav{display:flex;flex-wrap:wrap;gap:16px;align-items:center;margin-bottom:28px;padding-bottom:14px;border-bottom:1px solid var(--line)}
.nav a{color:var(--muted);font-weight:600}.nav a.brand{color:var(--ink);font-weight:700;font-size:1.15rem;letter-spacing:-.02em}.nav a.active,.nav a:hover{color:var(--accent)}
h1{font-size:1.7rem;margin:0 0 8px;letter-spacing:-.03em}.sub{color:var(--muted);margin:0 0 22px}
.grid{display:grid;grid-template-columns:repeat(4,1fr);gap:12px;margin-bottom:22px}@media(max-width:800px){.grid{grid-template-columns:repeat(2,1fr)}}
.card{background:var(--panel);border:1px solid var(--line);border-radius:var(--radius);padding:16px 18px;box-shadow:var(--shadow)}
.card .label{color:var(--muted);font-size:.82rem;font-weight:600}.card .value{font-size:1.8rem;font-weight:700;margin-top:4px;letter-spacing:-.03em}
.panel{background:var(--panel);border:1px solid var(--line);border-radius:var(--radius);padding:18px;box-shadow:var(--shadow);margin-bottom:18px;overflow-x:auto}
.panel h2{margin:0 0 12px;font-size:1.1rem}
table{width:100%;border-collapse:collapse;font-size:.92rem}th,td{text-align:left;padding:10px 8px;border-bottom:1px solid var(--line);vertical-align:top}
th{color:var(--muted);font-size:.78rem;text-transform:uppercase;letter-spacing:.04em}
button{padding:7px 12px;border:1px solid var(--line);border-radius:8px;background:var(--accent);color:#fff;font-weight:600;cursor:pointer}
.badge{display:inline-block;padding:2px 8px;border-radius:999px;font-size:.78rem;font-weight:700;background:#eee7dc;color:var(--muted)}
.badge.pending{background:#efe8d7;color:#7a6230}.badge.running,.badge.leased{background:#d9ebff;color:#13508a}
.badge.success{background:#dff3e5;color:var(--ok)}.badge.failed{background:#f6dede;color:var(--danger)}
.badge.active{background:var(--accent-soft);color:var(--accent)}.badge.paused{background:#fff0d6;color:#8a5a00}
.badge.stopped{background:#ececec;color:#666}.badge.online{background:#dff3e5;color:var(--ok)}.badge.offline{background:#eee;color:#888}
.btn{display:inline-block;border:none;cursor:pointer;border-radius:10px;padding:10px 14px;font-weight:700;font-size:.92rem;background:var(--accent);color:#fff}
.btn.secondary{background:#ebe4d8;color:var(--ink)}.btn.danger{background:var(--danger);color:#fff}.btn:hover{filter:brightness(1.05);text-decoration:none}
.form-grid{display:grid;grid-template-columns:1fr 1fr;gap:14px}@media(max-width:700px){.form-grid{grid-template-columns:1fr}}
.field{display:flex;flex-direction:column;gap:6px}.field.full{grid-column:1/-1}label{font-weight:700;font-size:.86rem}
input,select,textarea{border:1px solid var(--line);border-radius:10px;padding:10px 12px;font:inherit;background:#fff}
.help{color:var(--muted);font-size:.85rem;line-height:1.45}
.error{background:#fdeceb;color:var(--danger);border:1px solid #f0c0c0;padding:10px 12px;border-radius:10px;margin-bottom:14px}
.ok{background:#e7f6ec;color:var(--ok);border:1px solid #b8dfc4;padding:10px 12px;border-radius:10px;margin-bottom:14px;font-weight:600}
.traffic-cards{display:grid;gap:8px}.traffic-option{border:1px solid var(--line);border-radius:12px;padding:10px 12px;display:grid;grid-template-columns:auto 1fr;gap:10px;align-items:start;background:#fff;cursor:pointer}
.traffic-option:has(input:checked){border-color:var(--accent);background:var(--accent-soft)}.traffic-option strong{display:block;margin-bottom:2px}
.traffic-option .desc{color:var(--muted);font-size:.86rem;line-height:1.4;font-weight:400}
.actions{display:flex;gap:10px;margin-top:16px}
.queue-chip{display:inline-flex;align-items:center;gap:6px;border:1px solid var(--line);border-radius:999px;padding:4px 10px;margin:3px;font-size:.82rem;background:#fff}
tr.clickable-row{cursor:pointer}tr.clickable-row:hover td{background:#f3eee4}
table.kv th{width:140px;color:var(--muted);font-size:.82rem;text-transform:none;letter-spacing:0;font-weight:700;padding:8px 8px 8px 0}
table.kv td{border-bottom:1px solid var(--line);padding:8px 0}
.alert{background:#fff0d6;border:1px solid #e6c98a;color:#8a5a00;padding:12px 14px;border-radius:10px;margin-bottom:16px;font-weight:600}
.tag{display:inline-block;padding:1px 7px;border-radius:999px;font-size:.72rem;font-weight:700}
.tag-ok{background:#d9efe2;color:var(--ok)}.tag-unknown{background:#ece7de;color:var(--muted)}
.tag-verify_required{background:#f6e3cf;color:var(--warn)}.tag-bad{background:#f3d9d9;color:var(--danger)}
.muted{color:var(--muted)}code{background:#eee7db;padding:1px 5px;border-radius:5px;font-size:.82rem}
.board{display:grid;grid-template-columns:repeat(auto-fill,minmax(270px,1fr));gap:14px}
.dev{background:var(--panel);border:1px solid var(--line);border-radius:14px;padding:12px;display:flex;flex-direction:column;gap:8px}
.dev.off{opacity:.55}.dev h3{margin:0;font-size:.98rem;display:flex;justify-content:space-between;align-items:center;gap:6px}
.shot{width:100%;aspect-ratio:9/19.5;object-fit:cover;background:#151311;border-radius:10px;border:1px solid var(--line);cursor:zoom-in}
.noshot{display:flex;align-items:center;justify-content:center;color:#8d857a;font-size:.8rem}
.bar{height:9px;background:#e6ded1;border-radius:99px;overflow:hidden}.bar>i{display:block;height:100%;background:var(--accent)}
.bar>i.warn{background:var(--warn)}.bar>i.danger{background:var(--danger)}
div.kv{display:grid;grid-template-columns:auto 1fr;gap:2px 8px;font-size:.82rem}div.kv b{color:var(--muted);font-weight:600}
.btns{display:flex;flex-wrap:wrap;gap:5px}.btns button{padding:4px 8px;font-size:.76rem;background:#efe9de;color:var(--ink);border:1px solid var(--line)}
.btns button.on{background:var(--accent);color:#fff}
.modal{position:fixed;inset:0;background:#000c;display:none;align-items:center;justify-content:center;z-index:9}
.modal.show{display:flex}.modal img{max-height:92vh;max-width:92vw;border-radius:8px}
</style></head><body><div class="wrap">
<nav class="nav"><a class="brand" href="/">Campaign Queue</a>${NAV.map(([h, t]) => `<a href="${h}"${h === path ? ' class="active"' : ''}>${t}</a>`).join('')}</nav>
${body}</div></body></html>`

// ── 옛 campaign_web 화면 (대시보드 / 캠페인 생성·상세·재개 / 작업큐) ──
async function dashHtml(db: D1Database) {
  await expirePastCampaigns(db)
  const today = kstToday(), [ds, de] = kstDayUtcRange(today)
  const n = async (sql: string, ...v: any[]) => (await db.prepare(sql).bind(...v).first<any>())?.n ?? 0
  const pending = await n(`SELECT COUNT(*) n FROM jobs j JOIN campaigns c ON c.id=j.campaign_id
      WHERE j.schedule_date=? AND j.status='pending' AND c.status='active'`, today)
  const running = await n(`SELECT COUNT(*) n FROM jobs j JOIN campaigns c ON c.id=j.campaign_id
      WHERE j.status IN ('leased','running') AND c.status='active'`)
  const success = await n(`SELECT COUNT(*) n FROM jobs WHERE status='success' AND finished_at>=? AND finished_at<?`, ds, de)
  const failed = await n(`SELECT COUNT(*) n FROM jobs WHERE status='failed' AND finished_at>=? AND finished_at<?`, ds, de)
  const camps = (await db.prepare(`SELECT c.*,
      (SELECT COUNT(*) FROM jobs j WHERE j.campaign_id=c.id AND j.schedule_date=? AND j.status!='paused') planned,
      (SELECT COUNT(*) FROM jobs j WHERE j.campaign_id=c.id AND j.status='success' AND j.finished_at>=? AND j.finished_at<?) done,
      (SELECT COUNT(*) FROM jobs j WHERE j.campaign_id=c.id AND j.status='failed' AND j.finished_at>=? AND j.finished_at<?) fail
    FROM campaigns c ORDER BY c.id DESC`).bind(today, ds, de, ds, de).all()).results || []
  const devs = (await db.prepare('SELECT * FROM devices ORDER BY serial').all()).results || []
  const online = (d: any) => !!d.last_heartbeat && Date.now() - new Date(d.last_heartbeat + 'Z').getTime() <= 90000
  const preview = (await db.prepare(`SELECT j.queue_seq, j.status, c.name FROM jobs j JOIN campaigns c ON c.id=j.campaign_id
      WHERE j.schedule_date=? ORDER BY j.queue_seq ASC LIMIT 40`).bind(today).all()).results || []

  const campRows = camps.map((c: any) => `<tr class="clickable-row" onclick="location.href='/campaigns/${c.id}'" title="설정 보기">
    <td><strong><a href="/campaigns/${c.id}" onclick="event.stopPropagation()">${esc(c.name)}</a></strong></td>
    <td>${esc(campLabel(c.traffic_type, c.params_json))}</td><td>${badge(c.status)}</td><td>${c.daily_quota}</td>
    <td>${c.done}/${c.planned} <span class="muted">(fail ${c.fail})</span></td>
    <td>${esc(c.start_date)} · ${c.days}일</td>
    <td onclick="event.stopPropagation()">${c.status === 'active'
      ? `<form method="post" action="/campaigns/${c.id}/pause" style="display:inline"><button class="btn danger" type="submit">중단</button></form>`
      : ['paused', 'stopped'].includes(c.status) ? `<a class="btn secondary" href="/campaigns/${c.id}/resume">재개</a>` : ''}</td></tr>`).join('')

  return `<h1>오늘 집행 현황</h1>
<p class="sub">${today} · 오늘 진행률이 낮은 캠페인부터 고르게 배분됩니다. 캠페인 이름을 누르면 설정을 볼 수 있습니다.</p>
${devs.some(online) ? '' : '<div class="alert">워커가 오프라인입니다. 윈도우 PC에서 <code>python fleet.py</code> 를 실행하세요. 캠페인 재개만으로는 폰 작업이 시작되지 않습니다.</div>'}
<div class="grid">
  <div class="card"><div class="label">대기(오늘)</div><div class="value">${pending}</div></div>
  <div class="card"><div class="label">실행중</div><div class="value">${running}</div></div>
  <div class="card"><div class="label">성공(오늘)</div><div class="value">${success}</div></div>
  <div class="card"><div class="label">실패(오늘)</div><div class="value">${failed}</div></div>
</div>
<div class="panel"><h2>캠페인</h2>${camps.length ? `<table><thead><tr><th>이름</th><th>트래픽</th><th>상태</th><th>일유입량</th><th>오늘 진행</th><th>기간</th><th></th></tr></thead>
<tbody>${campRows}</tbody></table>` : '<p class="muted">등록된 캠페인이 없습니다. <a href="/campaigns/new">캠페인을 만들어보세요</a>.</p>'}</div>
<div class="panel"><h2>디바이스 (워커) <a href="/devices" style="font-size:.85rem;font-weight:600">기기관리</a></h2>${devs.length ? `<table>
<thead><tr><th>Serial</th><th>Host</th><th>상태</th><th>Busy Job</th><th>Heartbeat</th></tr></thead><tbody>${devs.map((d: any) => `<tr>
<td>${esc(d.label || d.serial)}${d.label ? ` <span class="muted">${esc(d.serial)}</span>` : ''}</td><td>${esc(d.host)}</td><td>${badge(online(d) ? 'online' : 'offline')}</td>
<td>${d.busy_job_id ?? '-'}</td><td class="muted">${esc(d.last_heartbeat || '-')}</td></tr>`).join('')}</tbody></table>`
    : '<p class="muted">아직 heartbeat 한 워커가 없습니다. <code>python fleet.py</code></p>'}</div>
<div class="panel"><h2>오늘 큐 미리보기 <a href="/queue" style="font-size:.85rem;font-weight:600">전체 보기</a></h2>${preview.length
    ? `<div>${preview.map((j: any) => `<span class="queue-chip"><strong>#${j.queue_seq}</strong> ${esc(j.name)} ${badge(j.status)}</span>`).join('')}</div>`
    : '<p class="muted">오늘 큐가 비어 있습니다.</p>'}</div>`
}

function campaignFormHtml(error: string | null) {
  const today = kstToday()
  return `<h1>캠페인 생성</h1>
<p class="sub">일유입량 × 일수만큼 작업이 생성되고, 다른 캠페인과 섞여 큐에 들어갑니다.</p>
${error ? `<div class="error">${esc(error)}</div>` : ''}
<form class="panel" method="post" action="/campaigns/new">
  <div class="form-grid">
    <div class="field"><label>캠페인 이름</label><input name="name" required placeholder="예: A_왕코등갈비" /></div>
    <div class="field"><label>일유입량</label><input type="number" name="daily_quota" min="1" value="200" required />
      <div class="help">하루에 실행할 작업 횟수. 200이면 매일 200회.</div></div>
    <div class="field"><label>시작일자</label><input type="date" name="start_date" value="${today}" required /></div>
    <div class="field"><label>일수</label><input type="number" name="days" min="1" value="5" required />
      <div class="help">시작일부터 N일. 5일이면 시작일~시작일+4일.</div></div>
    <div class="field full"><label>트래픽 종류</label><div class="traffic-cards">
      ${FORM_TYPES.map((t, i) => `<label class="traffic-option"><input type="radio" name="traffic_type" value="${t.key}"${i === 0 ? ' checked' : ''} required />
        <span><strong>${esc(t.label)}</strong><span class="desc">${esc(t.description)}</span></span></label>`).join('')}
    </div></div>
    <div class="field"><label>키워드</label><input name="keyword" placeholder="검색 키워드" /></div>
    <div class="field"><label>장소 URL (클릭/체류/풀)</label><input name="place_url" placeholder="https://place.map.kakao.com/..." /></div>
    <div class="field full"><label>업체명 / 2차 키워드</label><input name="place_name" placeholder="카카오맵 업체명 또는 함많찾을 2차 검색어" />
      <div class="help">함많찾을에서는 2차 검색 키워드로 사용합니다.</div></div>
    <div class="field" id="dwell-min-field"><label>체류 최소(초)</label><input type="number" name="dwell_min" min="1" step="1" value="15" /></div>
    <div class="field" id="dwell-max-field"><label>체류 최대(초)</label><input type="number" name="dwell_max" min="1" step="1" value="20" />
      <div class="help">카카오맵 일반트래픽·함많찾을에서 페이지 체류 시간 범위입니다.</div></div>
  </div>
  <div class="actions"><button class="btn" type="submit">생성하고 큐에 넣기</button><a class="btn secondary" href="/">취소</a></div>
</form>
<script>(function(){const radios=document.querySelectorAll('input[name="traffic_type"]');
const minF=document.getElementById('dwell-min-field'),maxF=document.getElementById('dwell-max-field');
function sync(){const v=document.querySelector('input[name="traffic_type"]:checked');const show=v&&${JSON.stringify(DWELL_TYPES)}.includes(v.value);
minF.style.display=show?'':'none';maxF.style.display=show?'':'none'}
radios.forEach(r=>r.addEventListener('change',sync));sync()})()</script>`
}

async function campaignDetailHtml(db: D1Database, id: number, flash: string | null, error: string | null) {
  const c = await db.prepare('SELECT * FROM campaigns WHERE id=?').bind(id).first<any>()
  if (!c) return null
  const today = kstToday(), [ds, de] = kstDayUtcRange(today)
  const n = async (sql: string, ...v: any[]) => (await db.prepare(sql).bind(...v).first<any>())?.n ?? 0
  const done = await n(`SELECT COUNT(*) n FROM jobs WHERE campaign_id=? AND status='success' AND finished_at>=? AND finished_at<?`, id, ds, de)
  const failed = await n(`SELECT COUNT(*) n FROM jobs WHERE campaign_id=? AND status='failed' AND finished_at>=? AND finished_at<?`, id, ds, de)
  const pending = await n(`SELECT COUNT(*) n FROM jobs WHERE campaign_id=? AND schedule_date=? AND status='pending'`, id, today)
  const planned = (await n(`SELECT COUNT(*) n FROM jobs WHERE campaign_id=? AND schedule_date=? AND status!='paused'`, id, today)) || c.daily_quota
  const hist = (await db.prepare(`SELECT * FROM campaign_history WHERE campaign_id=? ORDER BY changed_at DESC, id DESC LIMIT 200`)
    .bind(id).all()).results || []
  const fails = (await db.prepare(`SELECT id, finished_at, error_message, device_serial FROM jobs WHERE campaign_id=? AND status='failed'
      AND finished_at>=? AND finished_at<? ORDER BY finished_at DESC LIMIT 8`).bind(id, ds, de).all()).results || []
  const params = JSON.parse(c.params_json || '{}')
  const kw2 = params.keyword2 || c.place_name || ''
  const isHam = c.traffic_type === 'hamman_find', showDwell = DWELL_TYPES.includes(c.traffic_type)
  const editable = ['active', 'paused'].includes(c.status)
  return `<h1>${esc(c.name)}</h1>
<p class="sub">등록 시 세팅한 캠페인 설정입니다. <a href="/">← 대시보드</a></p>
${flash ? `<div class="ok">${esc(flash)}</div>` : ''}${error ? `<div class="error">${esc(error)}</div>` : ''}
<div class="panel"><h2>기본</h2><table class="kv">
  <tr><th>상태</th><td>${badge(c.status)}</td></tr>
  <tr><th>트래픽</th><td>${esc(campLabel(c.traffic_type, c.params_json))} <span class="muted">(${esc(c.traffic_type)})</span></td></tr>
  <tr><th>일유입량</th><td>${c.daily_quota}회/일</td></tr>
  <tr><th>기간</th><td>${esc(c.start_date)} ~ ${campaignEnd(c)} (${c.days}일)</td></tr>
  <tr><th>생성시각</th><td class="muted">${esc(c.created_at)}</td></tr>
</table></div>
${editable ? `<form class="panel" method="post" action="/campaigns/${id}/settings">
  <h2>유입·일수 수정</h2>
  <p class="help" style="margin:0 0 12px">저장하면 남은 대기 작업 큐가 바로 다시 맞춰집니다. (실행 중·완료 job은 유지)
    <strong style="display:block;margin-top:6px;color:var(--ok)">즉시 반영됩니다.</strong></p>
  <div class="form-grid">
    <div class="field"><label>일유입량</label><input type="number" name="daily_quota" min="1" value="${c.daily_quota}" required />
      <div class="help">하루에 실행할 작업 횟수.</div></div>
    <div class="field"><label>진행 일수</label><input type="number" name="days" min="1" max="365" value="${c.days}" required />
      <div class="help">시작일(${esc(c.start_date)})부터 N일. 종료일 = 시작일+N−1.
      ${c.status === 'paused' ? '일시정지 상태에서는 설정만 바뀌고, 재개 시 반영됩니다.' : ''}</div></div>
  </div>
  <div class="actions"><button class="btn" type="submit">저장 (즉시 반영)</button></div>
</form>
<form class="panel" method="post" action="/campaigns/${id}/settings">
  <h2>키워드·체류 수정</h2>
  <p class="help" style="margin:0 0 12px">저장하면 DB에 바로 반영되고, <strong>다음에 claim 되는 job부터</strong> 새 키워드로 실행됩니다.
    (대기 큐 재구축 불필요 · 이미 실행 중인 job은 기존 값 유지)
    <strong style="display:block;margin-top:6px;color:var(--ok)">즉시 반영됩니다.</strong></p>
  <div class="form-grid">
    <div class="field"><label>키워드</label><input name="keyword" value="${esc(c.keyword)}" placeholder="검색 키워드" />
      <div class="help">1차 검색어. 함많찾을·다음·카카오 공통.</div></div>
    <div class="field"><label>${isHam ? '2차 키워드' : '업체명 / 2차 키워드'}</label>
      <input name="keyword2" value="${esc(kw2)}" placeholder="예: 수유소고기 청가숯불구이" />
      <div class="help">${isHam ? '함많찾을 2차 검색어 (<code>--keyword2</code>). place_name 과 함께 저장됩니다.' : '카카오맵 업체명 등. 함많찾을이면 2차 검색어로도 씁니다.'}</div></div>
    ${isHam ? `<div class="field full"><label>함많찾을 버전</label><select name="engine">
      <option value="v2"${isV2(c.params_json) ? ' selected' : ''}>V2 — 정밀 클릭 + 캐시 유지 (권장)</option>
      <option value="v1"${isV2(c.params_json) ? '' : ' selected'}>V1 — 구버전 (pm clear)</option></select>
      <div class="help">다음에 claim 되는 job부터 해당 버전 스크립트로 실행됩니다. 워커 재시작 불필요.</div></div>` : ''}
    <div class="field full"><label>장소 URL</label><input name="place_url" value="${esc(c.place_url)}" placeholder="https://place.map.kakao.com/..." /></div>
    ${showDwell ? `<div class="field"><label>체류 최소(초)</label><input type="number" name="dwell_min" min="1" step="1" value="${esc(params.dwell_min ?? 15)}" /></div>
    <div class="field"><label>체류 최대(초)</label><input type="number" name="dwell_max" min="1" step="1" value="${esc(params.dwell_max ?? 20)}" />
      <div class="help">카카오맵 일반트래픽·함많찾을 체류 시간 범위.</div></div>` : ''}
  </div>
  <div class="actions"><button class="btn" type="submit">저장 (즉시 반영)</button></div>
</form>` : ''}
<div class="panel"><h2>검색·장소</h2><table class="kv">
  <tr><th>키워드</th><td>${esc(c.keyword || '—')}</td></tr>
  <tr><th>${isHam ? '2차 키워드' : '업체명'}</th><td>${esc(kw2 || '—')}</td></tr>
  <tr><th>장소 URL</th><td>${c.place_url ? `<a href="${esc(c.place_url)}" target="_blank" rel="noopener">${esc(c.place_url)}</a>` : '—'}</td></tr>
</table></div>
<div class="panel"><h2>변경 이력</h2>${hist.length ? `<table><thead><tr><th>시각(KST)</th><th>구분</th><th>항목</th><th>이전</th><th></th><th>변경</th><th>출처</th></tr></thead><tbody>
  ${hist.map((h: any) => `<tr><td class="muted" style="white-space:nowrap">${esc(kstStamp(h.changed_at))}</td><td>${esc(h.action)}</td><td><strong>${esc(h.label || h.field)}</strong></td>
    <td class="muted">${h.old_value == null ? '—' : esc(h.old_value || '(비어 있음)')}</td><td class="muted">→</td><td>${esc(h.new_value || '(비어 있음)')}</td><td class="muted">${esc(h.source)}</td></tr>`).join('')}
  </tbody></table>` : '<p class="muted">기록된 변경 이력이 없습니다.</p>'}</div>
<div class="panel"><h2>추가 파라미터</h2>${Object.keys(params).length
    ? `<table class="kv">${Object.entries(params).map(([k, v]) => `<tr><th>${esc(k)}</th><td>${esc(typeof v === 'object' ? JSON.stringify(v) : v)}</td></tr>`).join('')}</table>`
    : '<p class="muted">추가 파라미터 없음</p>'}</div>
<div class="panel"><h2>오늘 진행</h2>
  <p>${done} / ${planned} 성공 <span class="muted">(실패 ${failed}, 대기 ${pending})</span></p>
  ${fails.length ? `<h3 style="font-size:.95rem;margin:14px 0 8px">최근 실패</h3><table><thead><tr><th>Job</th><th>시각</th><th>기기</th><th>원인</th></tr></thead><tbody>
  ${fails.map((j: any) => `<tr><td>#${j.id}</td><td class="muted">${esc(j.finished_at)}</td><td class="muted">${esc(j.device_serial || '-')}</td><td>${esc(j.error_message || '—')}</td></tr>`).join('')}</tbody></table>` : ''}
  <div class="actions" style="margin-top:12px">
    ${c.status === 'active' ? `<form method="post" action="/campaigns/${id}/pause" style="display:inline"><button class="btn danger" type="submit">중단</button></form>`
      : ['paused', 'stopped'].includes(c.status) ? `<a class="btn secondary" href="/campaigns/${id}/resume">재개</a>` : ''}
    <a class="btn secondary" href="/">대시보드</a>
  </div>
</div>`
}

function resumeHtml(c: any, error: string | null) {
  const today = kstToday()
  return `<h1>캠페인 재개</h1>
<p class="sub"><strong>${esc(c.name)}</strong> 을(를) 다시 돌리려면 일유입량과 진행 일수를 다시 입력하세요.
  시작일은 오늘(${today})로 잡히고, 종료일은 오늘부터 N일입니다.</p>
${error ? `<div class="error">${esc(error)}</div>` : ''}
<div class="panel" style="margin-bottom:16px"><table class="kv">
  <tr><th>이전 일유입량</th><td>${c.daily_quota}회/일</td></tr>
  <tr><th>이전 기간</th><td>${esc(c.start_date)} ~ ${campaignEnd(c)} (${c.days}일)</td></tr>
  <tr><th>상태</th><td>${badge(c.status)}</td></tr>
</table></div>
<form class="panel" method="post" action="/campaigns/${c.id}/resume">
  <div class="form-grid">
    <div class="field"><label>일유입량</label><input type="number" name="daily_quota" min="1" value="${c.daily_quota}" required />
      <div class="help">하루에 실행할 작업 횟수. 생성 시와 동일합니다.</div></div>
    <div class="field"><label>진행 일수</label><input type="number" name="days" min="1" max="365" value="${c.days}" required />
      <div class="help">오늘부터 N일. 5일이면 오늘~오늘+4일.</div></div>
  </div>
  <div class="actions"><button class="btn" type="submit">재개하고 큐에 넣기</button><a class="btn secondary" href="/">취소</a></div>
</form>`
}

function queueHtml(day: string, jobs: any[]) {
  return `<h1>작업큐</h1>
<p class="sub">날짜별 작업 목록. 워커는 오늘 진행률이 가장 낮은 캠페인의 job 부터 가져갑니다.</p>
<form class="panel" method="get" action="/queue" style="display:flex;gap:10px;align-items:end">
  <div class="field" style="margin:0"><label>일자</label><input type="date" name="day" value="${day}" /></div>
  <button class="btn" type="submit">보기</button>
</form>
<div class="panel"><h2>${day} · ${jobs.length} jobs</h2>${jobs.length ? `<table>
<thead><tr><th>Seq</th><th>캠페인</th><th>트래픽</th><th>예정시각</th><th>상태</th><th>기기</th></tr></thead><tbody>
${jobs.map((j) => `<tr><td>${j.queue_seq}</td><td>${esc(j.campaign_name)}</td><td>${esc(campLabel(j.traffic_type, j.params_json))}</td>
<td class="muted">${esc(j.scheduled_at || '-')}</td><td>${badge(j.status)}</td><td class="muted">${esc(j.device_serial || '-')}</td></tr>`).join('')}
</tbody></table>` : '<p class="muted">이 날짜의 job이 없습니다.</p>'}</div>`
}

const accountsHtml = () => `<h1>카카오 계정</h1><p class="sub">할당은 <b>투입 오래된 순</b> — 표 위에서부터 나갑니다.</p>
<div class="grid" id="stats"></div>
<div class="panel"><h2 style="margin:0 0 8px">아웃룩 API 토큰 붙여넣기</h2>
<p class="muted" style="margin:0 0 8px;font-size:.85rem">한 줄에 한 계정: <code>카카오메일----비번----닉----아웃룩메일----아웃룩비번----client_id----refresh_token</code></p>
<textarea id="tok" rows="3" style="width:100%;font-family:monospace;font-size:.8rem"></textarea>
<div style="margin-top:8px"><button onclick="paste()">저장</button> <span id="tmsg" class="muted"></span></div></div>
<div class="panel"><h2 style="margin:0 0 10px">계정 목록 <span class="muted" id="cnt"></span></h2>
<table><thead><tr><th>#</th><th>이메일</th><th>닉</th><th>상태</th><th>로그인</th><th>오늘/제한</th><th>누적</th><th>마지막투입</th></tr></thead><tbody id="rows"></tbody></table></div>
<script>
async function j(u,o){const r=await fetch(u,o);return r.json()}
async function load(){
 const s=await j('/api/accounts/kakao/stats')
 document.getElementById('stats').innerHTML=[['전체',s.total],['미투입',s.never_used],['오늘투입',s.used_today],['리스중',s.leased]]
   .map(x=>'<div class="card"><div class="label">'+x[0]+'</div><div class="value">'+x[1]+'</div></div>').join('')
 const a=await j('/api/accounts/kakao?limit=2000')
 document.getElementById('cnt').textContent='('+a.length+'건)'
 document.getElementById('rows').innerHTML=a.map((x,i)=>'<tr><td class=muted>'+(i+1)+'</td><td>'+x.email+'</td><td>'+(x.nickname||'')+'</td><td>'+x.status+'</td><td><span class="tag tag-'+(x.login_state)+'">'+x.login_state+'</span>'+(x.warmed_at?' <span class="tag tag-ok">예열</span>':'')+(x.oauth_status==='valid'?' <span class="tag tag-ok">API✓</span>':'')+'</td><td>'+x.used_today+' / '+(x.daily_limit>0?x.daily_limit:'∞')+'</td><td>'+x.total_used+'</td><td class=muted>'+(x.last_used_at||'미투입')+'</td></tr>').join('')
}
async function paste(){document.getElementById('tmsg').textContent='저장중…'
 const r=await j('/api/accounts/kakao/tokens/paste',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({text:document.getElementById('tok').value})})
 document.getElementById('tmsg').textContent='매칭 '+r.matched+' · 토큰 '+r.tokens_saved+(r.unmatched.length?' · 미매칭 '+r.unmatched.length:'');setTimeout(load,800)}
load()
</script>`

const devicesHtml = () => `<h1>기기관리</h1><p class="sub">기기별 화면·상태·유심 데이터(1일 한도)·작업 실적. 한도는 자정~자정 24시간에 고르게 분배됩니다.</p>
<div class="grid" id="sum"></div>
<div class="board" id="board"></div>
<div class="panel" style="margin-top:16px"><h2 style="margin:0 0 10px">오늘 기기별 작업 <span class="muted" id="statdate"></span></h2>
<table><thead><tr><th>기기</th><th>캠페인</th><th>성공</th><th>실패</th><th>데이터</th><th>평균/건</th></tr></thead><tbody id="stats"></tbody></table></div>
<div class="panel"><h2 style="margin:0 0 10px">최근 이벤트</h2><table><thead><tr><th>시각</th><th>기기</th><th>종류</th><th>내용</th></tr></thead><tbody id="evs"></tbody></table></div>
<div class="modal" id="modal" onclick="this.classList.remove('show')"><img id="big"></div>
<script>
async function j(u,o){const r=await fetch(u,o);return r.json()}
const mb=v=>v>=1024?(v/1024).toFixed(2)+'GB':Math.round(v)+'MB'
const hhmm=s=>s?s.slice(11,16):''
function badge(d){
 if(!d.online)return '<span class="tag tag-bad">오프라인</span>'
 if(d.paused)return '<span class="tag tag-unknown">일시정지</span>'
 if(d.budget.exhausted)return '<span class="tag tag-bad">한도도달</span>'
 if(d.state==='hold')return '<span class="tag tag-verify_required">데이터대기</span>'
 if(d.busy_job_id)return '<span class="tag tag-ok">작업중</span>'
 return '<span class="tag tag-ok">대기</span>'
}
async function load(){
 const d=await j('/api/devices')
 const on=d.filter(x=>x.online)
 const used=d.reduce((a,x)=>a+x.budget.used_mb,0), ok=d.reduce((a,x)=>a+x.jobs_ok,0), fail=d.reduce((a,x)=>a+x.jobs_fail,0)
 document.getElementById('sum').innerHTML=[['연결 기기',on.length+' / '+d.length],['오늘 성공',ok],['오늘 실패',fail],['오늘 데이터',mb(used)]]
  .map(x=>'<div class="card"><div class="label">'+x[0]+'</div><div class="value">'+x[1]+'</div></div>').join('')
 document.getElementById('board').innerHTML=d.map(x=>{
  const pct=Math.min(100,x.budget.pct), cls=pct>=90?'danger':pct>=70?'warn':''
  const shot=x.has_screenshot?'<img class="shot" src="/api/devices/'+x.serial+'/screen.jpg?t='+Date.now()+'" onclick="zoom(\\''+x.serial+'\\')">':'<div class="shot noshot">화면 캡처 없음</div>'
  return '<div class="dev'+(x.online?'':' off')+'"><h3><span>'+(x.label||x.model||x.serial)+'</span>'+badge(x)+'</h3>'
   +shot
   +'<div class="bar"><i class="'+cls+'" style="width:'+pct+'%"></i></div>'
   +'<div class="kv"><b>데이터</b><span>'+mb(x.budget.used_mb)+' / '+mb(x.budget.cap_mb)+' ('+x.budget.pct+'%) · 지금 허용 '+mb(x.budget.pace_mb)+'</span>'
   +'<b>배터리</b><span>'+(x.battery!=null?x.battery+'%':'-')+(x.charging?' ⚡충전':' 🔋미충전')+'</span>'
   +'<b>화면</b><span>'+(x.screen_on?'켜짐':'꺼짐')+'</span>'
   +'<b>작업</b><span>성공 '+x.jobs_ok+' · 실패 '+x.jobs_fail+'</span>'
   +'<b>건당</b><span>'+(x.avg_mb?x.avg_mb+'MB':'-')+(x.jobs_left_est!=null?' · 남은 한도로 ~'+x.jobs_left_est+'건':'')+'</span>'
   +'<b>1일예상</b><span>'+(x.jobs_per_day_est!=null?x.jobs_per_day_est+'건':'-')+'</span>'
   +'<b>IP</b><span>'+(x.ip||'-')+'</span>'
   +'<b>상태</b><span class="muted">'+(x.state_detail||x.state||'')+'</span>'
   +'<b>하트비트</b><span class="muted">'+hhmm(x.last_heartbeat)+'</span></div>'
   +'<div class="btns"><button onclick="cmd(\\''+x.serial+'\\',\\'wake\\')">화면켜기</button>'
   +'<button onclick="cmd(\\''+x.serial+'\\',\\'home\\')">홈</button>'
   +'<button onclick="cmd(\\''+x.serial+'\\',\\'back\\')">뒤로</button>'
   +'<button onclick="cmd(\\''+x.serial+'\\',\\'screenshot\\')">캡처</button>'
   +'<button onclick="cmd(\\''+x.serial+'\\',\\'data_toggle\\')">IP변경</button>'
   +'<button class="'+(x.paused?'on':'')+'" onclick="pause(\\''+x.serial+'\\','+(x.paused?0:1)+')">'+(x.paused?'재개':'일시정지')+'</button>'
   +'<button onclick="cap(\\''+x.serial+'\\','+x.budget.cap_mb+')">한도수정</button>'
   +'<button onclick="cmd(\\''+x.serial+'\\',\\'reboot\\')">재부팅</button></div>'
   +'<div class="muted" style="font-size:.72rem">'+x.serial+(x.carrier?' · '+x.carrier:'')+'</div></div>'
 }).join('')||'<p class="muted">연결된 기기가 없습니다.</p>'
 const s=await j('/api/devices/stats')
 document.getElementById('statdate').textContent='('+s.date+')'
 document.getElementById('stats').innerHTML=s.rows.map(r=>'<tr><td>'+r.serial+'</td><td>'+r.campaign+'</td><td>'+r.ok+'</td><td>'+r.fail+'</td><td>'+mb(r.bytes/1048576)+'</td><td>'+((r.ok+r.fail)?((r.bytes/1048576)/(r.ok+r.fail)).toFixed(1)+'MB':'-')+'</td></tr>').join('')||'<tr><td colspan=6 class=muted>오늘 완료된 작업 없음</td></tr>'
 const e=await j('/api/events?limit=40')
 document.getElementById('evs').innerHTML=e.map(x=>'<tr><td class=muted>'+x.ts.slice(5,16)+'</td><td>'+x.serial+'</td><td>'+x.kind+'</td><td class=muted>'+x.message+'</td></tr>').join('')||'<tr><td colspan=4 class=muted>없음</td></tr>'
}
function zoom(s){document.getElementById('big').src='/api/devices/'+s+'/screen.jpg?t='+Date.now();document.getElementById('modal').classList.add('show')}
async function cmd(s,c){await j('/api/devices/'+s+'/cmd',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({cmd:c})});alert('명령 전송: '+c+' (최대 30초 내 실행)')}
async function pause(s,v){await j('/api/devices/'+s+'/settings',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({paused:v})});load()}
async function cap(s,cur){const v=prompt('1일 데이터 한도 (MB)',cur);if(!v)return;await j('/api/devices/'+s+'/settings',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({daily_cap_mb:Number(v)})});load()}
load();setInterval(load,7000)
</script>`

app.get('/health', (c) => c.json({ ok: true, ts: nowIso() }))
export default app
