// 텔레그램 알림 — cron(매시 정각)으로 기기 상태를 점검해 작업이 실제로 멈춘 긴급 상황만 알린다.
// (복구·데이터 한도 같은 정상 동작은 알리지 않는다)
// PC 가 꺼져도 동작하도록 PC 쪽(fleet)이 아니라 컨트롤플레인에서 판정한다.
// 문제가 풀리지 않으면 REMIND_HOURS 마다 다시 알린다 — 한 번 알리고 며칠씩 방치되는 일을 막는다.

export type AlertEnv = { DB: D1Database; TELEGRAM_BOT_TOKEN?: string; TELEGRAM_CHAT_ID?: string }

const OFFLINE_MIN = 10        // 하트비트가 이만큼 끊기면 오프라인
const FAIL_STREAK = 5         // 최근 종료 작업이 연속으로 이만큼 실패하면 알림
const LOW_WINDOW_H = 2        // 작업량 저조 판정 구간 (시간)
const LOW_SUCCESS = 8         // 구간 내 성공이 이보다 적으면 저조 (정상 기기는 시간당 20건 안팎)
const REMIND_HOURS = 3        // 문제가 계속되면 이 간격으로 재알림
const KST_OFFSET = 9 * 3600 * 1000

function nowIso(): string { return new Date().toISOString().replace('T', ' ').slice(0, 19) }
function kstHm(utc: string | null): string {
  if (!utc) return '-'
  return new Date(new Date(utc + 'Z').getTime() + KST_OFFSET).toISOString().slice(5, 16).replace('T', ' ')
}
function hoursSince(utc: string | null, now: number): number {
  return utc ? (now - new Date(utc + 'Z').getTime()) / 3600000 : Infinity
}
function dur(h: number): string {
  return h >= 24 ? `${Math.floor(h / 24)}일 ${Math.floor(h % 24)}시간` : `${Math.max(1, Math.floor(h))}시간`
}

export async function sendTelegram(env: AlertEnv, text: string) {
  if (!env.TELEGRAM_BOT_TOKEN || !env.TELEGRAM_CHAT_ID) return
  await fetch(`https://api.telegram.org/bot${env.TELEGRAM_BOT_TOKEN}/sendMessage`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ chat_id: env.TELEGRAM_CHAT_ID, text }),
  })
}

type AlertRow = { key: string; active: number; since: string | null; notified_at: string | null }

export async function checkAlerts(env: AlertEnv) {
  const db = env.DB, now = Date.now()
  const devs = (await db.prepare('SELECT serial, label, model, last_heartbeat, paused, state, state_detail FROM devices ORDER BY serial').all()).results as any[]
  const states = (await db.prepare('SELECT key, active, since, notified_at FROM alert_state').all()).results as AlertRow[]
  const st = new Map(states.map((s) => [s.key, s]))

  const lines: string[] = []
  const offlineNames: string[] = []
  const notified: string[] = []

  // 상태 전이 기록. 켜질 때 since 를 새로 잡고, 꺼지면 다음에 새 문제로 다시 알린다.
  const setState = async (key: string, on: boolean) => {
    await db.prepare(`INSERT INTO alert_state (key, active, since, updated_at) VALUES (?,?,?,?)
      ON CONFLICT(key) DO UPDATE SET active=excluded.active, since=CASE WHEN alert_state.active=excluded.active THEN alert_state.since ELSE excluded.since END, updated_at=excluded.updated_at`)
      .bind(key, on ? 1 : 0, nowIso(), nowIso()).run()
    const prev = st.get(key)
    if (!prev || !!prev.active !== on) st.set(key, { key, active: on ? 1 : 0, since: nowIso(), notified_at: on ? null : prev?.notified_at ?? null })
  }
  // 문제 지속 중: 처음이거나 마지막 알림 후 REMIND_HOURS 가 지났으면 알린다. 반환값 = 지속 시간(h) 또는 null(이번엔 침묵)
  const due = async (key: string, on: boolean): Promise<number | null> => {
    const prev = st.get(key)
    if (!on) {
      if (prev?.active) await setState(key, false)
      return null
    }
    if (!prev?.active) await setState(key, true)
    const cur = st.get(key)!
    if (hoursSince(cur.notified_at, now) < REMIND_HOURS - 0.1) return null
    notified.push(key)
    return hoursSince(cur.since, now)
  }
  const ongoing = (h: number) => (h >= 1 ? ` (${dur(h)}째 계속)` : '')

  for (const d of devs) {
    const name = d.label || d.model || d.serial

    // 1) 오프라인 — 일시정지한 기기는 제외
    const hbAge = d.last_heartbeat ? (now - new Date(d.last_heartbeat + 'Z').getTime()) / 60000 : Infinity
    const offline = !d.paused && hbAge > OFFLINE_MIN
    const hOff = await due(`${d.serial}:offline`, offline)
    if (hOff !== null) offlineNames.push(`${name}(${kstHm(d.last_heartbeat)})`)
    if (offline || d.paused) {
      // 꺼진 기기는 오프라인 알림 하나로 충분 — 실패·저조 판정 상태는 건드리지 않는다
      continue
    }

    // 2) 연속 실패
    const recent = (await db.prepare(`SELECT status, error_message FROM jobs
      WHERE device_serial=? AND status IN ('success','failed') AND finished_at IS NOT NULL
      ORDER BY finished_at DESC LIMIT ?`).bind(d.serial, FAIL_STREAK).all()).results as any[]
    const kFail = `${d.serial}:fail_streak`
    const streak = recent.length >= FAIL_STREAK && recent.every((r) => r.status === 'failed')
    // 한 번 켜지면 성공이 나올 때까지 유지 (사이사이 release 등으로 깜빡이지 않게)
    const failOn = streak || (!!st.get(kFail)?.active && recent[0]?.status !== 'success')
    const hFail = await due(kFail, failOn)
    if (hFail !== null) {
      const err = (recent[0]?.error_message || '').slice(0, 120)
      lines.push(`⚠️ 휴대폰 ${name} — 작업이 연속으로 실패하고 있습니다${ongoing(hFail)}.\n   → 휴대폰 화면에 팝업·오류가 떠 있는지 확인하고, 안 풀리면 휴대폰을 재부팅해 주세요.${err ? `\n   (오류 내용: ${err})` : ''}`)
    }

    // 3) 작업량 저조 — 켜져 있고 일하는 중인데 성공이 거의 없음.
    //    데이터 한도·배터리 보류(hold)나 작업 없음(idle)은 정상 대기라 제외.
    const waiting = d.state === 'hold' || d.state === 'idle'
    const row = await db.prepare(`SELECT SUM(status='success') s, COUNT(*) n FROM jobs
      WHERE device_serial=? AND status IN ('success','failed') AND finished_at >= datetime('now', ?)`)
      .bind(d.serial, `-${LOW_WINDOW_H} hours`).first<any>()
    const ok = Number(row?.s || 0), total = Number(row?.n || 0)
    const kLow = `${d.serial}:low_output`
    const lowOn = !waiting && ok < LOW_SUCCESS
    // 연속 실패 알림이 이미 같은 문제를 말하고 있으면 저조 알림은 중복이라 생략
    const hLow = await due(kLow, lowOn && !failOn)
    if (hLow !== null) {
      lines.push(`🐢 휴대폰 ${name} — 작업량이 너무 적습니다${ongoing(hLow)}: 최근 ${LOW_WINDOW_H}시간 성공 ${ok}건 / 시도 ${total}건 (정상 40건 안팎).\n   → 휴대폰 화면이 멈춰 있거나 느린지 확인하고, 안 풀리면 휴대폰을 재부팅해 주세요.${d.state_detail ? `\n   (현재 상태: ${String(d.state_detail).slice(0, 60)})` : ''}`)
    }
  }

  // 끊긴 기기 — 전부 끊겼으면 PC/프로그램 문제로 묶어서 알린다 (직원이 보는 문구라 쉬운 말로)
  const live = devs.filter((d) => !d.paused)
  const allOff = live.length > 0 && live.every((d) => st.get(`${d.serial}:offline`)?.active)
  if (offlineNames.length && allOff) {
    lines.unshift(`🔴 휴대폰 전체 작업 멈춤 — PC가 꺼졌거나 작업 프로그램이 꺼졌습니다.\n   → PC가 켜져 있는지, 작업 프로그램 창이 떠 있는지 확인해 주세요.\n   멈춘 휴대폰(마지막 작업 시각): ${offlineNames.join(', ')}`)
  } else if (offlineNames.length) {
    lines.unshift(`🔴 휴대폰 일부 작업 멈춤 — 휴대폰 연결이 빠졌거나 작업 프로그램에 문제가 생겼습니다.\n   → 해당 휴대폰의 USB 선 연결과 전원을 확인해 주세요.\n   멈춘 휴대폰(마지막 작업 시각): ${offlineNames.join(', ')}`)
  }
  await setState('_last_run', true)

  if (lines.length) {
    await sendTelegram(env, `[트래픽] ${kstHm(nowIso())}\n` + lines.join('\n'))
    for (const key of notified) {
      await db.prepare('UPDATE alert_state SET notified_at=? WHERE key=?').bind(nowIso(), key).run()
    }
  }
}
