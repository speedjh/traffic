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

// 만료 캠페인 자동 paused
async function expirePastCampaigns(db: D1Database) {
  const today = kstToday()
  const rows = await db.prepare(`SELECT id, start_date, days FROM campaigns WHERE status='active'`).all()
  for (const c of rows.results || []) {
    const end = addDays((c as any).start_date, Math.max((c as any).days, 1) - 1)
    if (end < today) await db.prepare(`UPDATE campaigns SET status='paused' WHERE id=?`).bind((c as any).id).run()
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
    const ex = await db.prepare('SELECT id FROM campaigns WHERE id=? OR name=?').bind(r.id ?? -1, r.name).first<any>()
    const vals = [r.name, r.traffic_type, r.keyword || '', r.place_url || '', r.place_name || '',
      r.daily_quota ?? 0, r.start_date || '', r.days ?? 1, r.status || 'paused', r.params_json || '{}']
    if (ex) {
      await db.prepare(`UPDATE campaigns SET name=?, traffic_type=?, keyword=?, place_url=?, place_name=?,
        daily_quota=?, start_date=?, days=?, status=?, params_json=? WHERE id=?`).bind(...vals, ex.id).run()
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
  const id = Number(c.req.param('id'))
  await c.env.DB.prepare(`UPDATE campaigns SET status='paused' WHERE id=?`).bind(id).run()
  await c.env.DB.prepare(`UPDATE jobs SET status='paused' WHERE campaign_id=? AND status='pending'`).bind(id).run()
  return c.json({ ok: true })
})

// ══════════════════════════════════════════════════════════════════════════
// 대시보드 (HTML)
// ══════════════════════════════════════════════════════════════════════════
app.get('/', (c) => c.html(page('대시보드', dashHtml())))
app.get('/accounts', (c) => c.html(page('카카오 계정', accountsHtml())))
app.get('/campaigns', (c) => c.html(page('캠페인', campaignsHtml())))
app.get('/devices', (c) => c.html(page('기기관리', devicesHtml())))

const page = (title: string, body: string) => `<!doctype html><html lang="ko"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>${title} · Traffic CP</title><style>
:root{--bg:#f3f1ec;--panel:#fffdf8;--ink:#1c1a17;--muted:#6b645c;--line:#ddd4c7;--accent:#0f6b5c;--ok:#1f6b3a;--danger:#9b2c2c;--warn:#9a4a16}
*{box-sizing:border-box}body{margin:0;font-family:"Apple SD Gothic Neo",system-ui,sans-serif;color:var(--ink);background:var(--bg)}
.wrap{max-width:1100px;margin:0 auto;padding:22px 18px 48px}.nav{display:flex;gap:16px;align-items:center;margin-bottom:24px;padding-bottom:12px;border-bottom:1px solid var(--line)}
.nav a{color:var(--muted);font-weight:600;text-decoration:none}.nav a.brand{color:var(--ink);font-size:1.1rem}
h1{font-size:1.5rem;margin:0 0 6px}.sub{color:var(--muted);margin:0 0 18px}
.grid{display:grid;grid-template-columns:repeat(4,1fr);gap:12px;margin-bottom:20px}
.card{background:var(--panel);border:1px solid var(--line);border-radius:12px;padding:14px 16px}
.card .label{color:var(--muted);font-size:.8rem;font-weight:600}.card .value{font-size:1.7rem;font-weight:700;margin-top:4px}
.panel{background:var(--panel);border:1px solid var(--line);border-radius:12px;padding:16px;margin-bottom:16px}
table{width:100%;border-collapse:collapse;font-size:.9rem}th,td{text-align:left;padding:8px;border-bottom:1px solid var(--line)}
th{color:var(--muted);font-size:.75rem;text-transform:uppercase}button{padding:7px 12px;border:1px solid var(--line);border-radius:8px;background:var(--accent);color:#fff;font-weight:600;cursor:pointer}
input,textarea,select{padding:7px 9px;border:1px solid var(--line);border-radius:8px;font-family:inherit}
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
.kv{display:grid;grid-template-columns:auto 1fr;gap:2px 8px;font-size:.82rem}.kv b{color:var(--muted);font-weight:600}
.btns{display:flex;flex-wrap:wrap;gap:5px}.btns button{padding:4px 8px;font-size:.76rem;background:#efe9de;color:var(--ink);border:1px solid var(--line)}
.btns button.on{background:var(--accent);color:#fff}
.modal{position:fixed;inset:0;background:#000c;display:none;align-items:center;justify-content:center;z-index:9}
.modal.show{display:flex}.modal img{max-height:92vh;max-width:92vw;border-radius:8px}
</style></head><body><div class="wrap">
<nav class="nav"><a class="brand" href="/">Traffic CP</a><a href="/">대시보드</a><a href="/devices">기기관리</a><a href="/campaigns">캠페인</a><a href="/accounts">카카오 계정</a></nav>
${body}</div></body></html>`

const dashHtml = () => `<h1>대시보드</h1><p class="sub">Cloudflare Workers + D1 컨트롤플레인</p>
<div class="grid" id="stats"></div>
<div class="panel"><h2 style="margin:0 0 10px">기기</h2><table><thead><tr><th>시리얼</th><th>워커</th><th>온라인</th><th>busy job</th><th>마지막 하트비트</th></tr></thead><tbody id="devs"></tbody></table></div>
<script>
async function j(u,o){const r=await fetch(u,o);return r.json()}
async function load(){
 const s=await j('/api/accounts/kakao/stats')
 document.getElementById('stats').innerHTML=[['계정',s.total],['미투입',s.never_used],['오늘투입',s.used_today],['리스중',s.leased]]
   .map(x=>'<div class="card"><div class="label">'+x[0]+'</div><div class="value">'+x[1]+'</div></div>').join('')
 const d=await j('/api/devices')
 document.getElementById('devs').innerHTML=d.map(x=>'<tr><td>'+x.serial+'</td><td>'+(x.worker_id||'')+'</td><td>'+(x.online?'🟢':'⚪')+'</td><td>'+(x.busy_job_id||'')+'</td><td class="muted">'+(x.last_heartbeat||'')+'</td></tr>').join('')||'<tr><td colspan=5 class=muted>연결된 기기 없음</td></tr>'
}
load();setInterval(load,5000)
</script>`

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

const campaignsHtml = () => `<h1>캠페인</h1><p class="sub">설정된 캠페인. 활성화하면 오늘부터 일수×일유입량만큼 큐 생성.</p>
<div class="panel"><table><thead><tr><th>#</th><th>이름</th><th>트래픽</th><th>키워드</th><th>장소</th><th>일유입/일수</th><th>상태</th><th></th></tr></thead><tbody id="rows"></tbody></table></div>
<script>
async function j(u,o){const r=await fetch(u,o);return r.json()}
async function load(){const a=await j('/api/campaigns')
 document.getElementById('rows').innerHTML=a.map(c=>'<tr><td class=muted>'+c.id+'</td><td>'+c.name+'</td><td>'+c.traffic_type+'</td><td>'+(c.keyword||'')+'</td><td>'+(c.place_name||'')+'</td><td>'+c.daily_quota+' / '+c.days+'</td><td><span class="tag '+(c.status==='active'?'tag-ok':'tag-unknown')+'">'+c.status+'</span></td><td>'+(c.status==='active'?'<button onclick="pause('+c.id+')">일시정지</button>':'<button onclick="act('+c.id+')">활성화</button>')+'</td></tr>').join('')}
async function act(id){if(!confirm('활성화하면 오늘부터 큐가 생성됩니다. 계속?'))return;await j('/api/campaigns/'+id+'/activate',{method:'POST'});load()}
async function pause(id){await j('/api/campaigns/'+id+'/pause',{method:'POST'});load()}
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
