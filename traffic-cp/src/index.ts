// traffic-cp — 트래픽 컨트롤플레인 (Cloudflare Workers + D1)
// campaign_web(FastAPI) 이식: 계정풀(투입 오래된 순 할당) / 워커 API / 캠페인·큐 / 대시보드
import { Hono } from 'hono'

type Env = { DB: D1Database }
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
  const a = await c.env.DB.prepare('SELECT mail_email, oauth_client_id FROM kakao_accounts WHERE id=?').bind(id).first<any>()
  if (!a) return c.json({ error: 'not found' }, 404)
  // TODO Phase2: Cloudflare Browser Rendering 으로 아웃룩 로그인→코드 읽기
  return c.json({ available: false, code: null, detail: 'code_reader_not_deployed_yet(phase2)' })
})

// ══════════════════════════════════════════════════════════════════════════
// 워커 API — claim / start / report / heartbeat
// ══════════════════════════════════════════════════════════════════════════
app.post('/api/devices/heartbeat', async (c) => {
  const b = await c.req.json<any>()
  await c.env.DB.prepare(`INSERT INTO devices (serial, host, worker_id, last_heartbeat, busy_job_id)
    VALUES (?,?,?,?,?) ON CONFLICT(serial) DO UPDATE SET host=excluded.host, worker_id=excluded.worker_id,
    last_heartbeat=excluded.last_heartbeat, busy_job_id=excluded.busy_job_id`)
    .bind(b.serial, b.host || '', b.worker_id || '', nowIso(), b.busy_job_id ?? null).run()
  return c.json({ ok: true })
})

app.get('/api/devices', async (c) => {
  const rows = await c.env.DB.prepare('SELECT * FROM devices ORDER BY serial').all()
  const staleMs = 90000, now = Date.now()
  const out = (rows.results || []).map((d: any) => ({
    ...d, online: d.last_heartbeat ? (now - new Date(d.last_heartbeat + 'Z').getTime()) <= staleMs : false,
  }))
  return c.json(out)
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
    // due 한 pending job 중 가장 앞(오늘 우선 → schedule_date → queue_seq)
    job = await db.prepare(
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
  if (!job) { await db.prepare('UPDATE devices SET busy_job_id=NULL WHERE serial=?').bind(serial).run(); return c.json({ job: null }) }
  await db.prepare('UPDATE devices SET busy_job_id=? WHERE serial=?').bind(job.id, serial).run()
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
  const r = await c.env.DB.prepare(`UPDATE jobs SET status='running', started_at=? WHERE id=? AND status='leased'`).bind(nowIso(), id).run()
  if (!r.meta.changes) return c.json({ ok: false, detail: 'not runnable' }, 409)
  return c.json({ ok: true, job_id: id, status: 'running' })
})

app.post('/api/worker/jobs/:id/report', async (c) => {
  const id = Number(c.req.param('id')); const b = await c.req.json<any>()
  const status = b.status === 'success' ? 'success' : 'failed'
  await c.env.DB.prepare(`UPDATE jobs SET status=?, finished_at=?, error_message=? WHERE id=?`)
    .bind(status, nowIso(), b.error_message || null, id).run()
  await c.env.DB.prepare('UPDATE devices SET busy_job_id=NULL WHERE busy_job_id=?').bind(id).run()
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
</style></head><body><div class="wrap">
<nav class="nav"><a class="brand" href="/">Traffic CP</a><a href="/">대시보드</a><a href="/accounts">카카오 계정</a></nav>
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

app.get('/health', (c) => c.json({ ok: true, ts: nowIso() }))
export default app
