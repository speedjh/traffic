// 텔레그램 알림 — cron(매시 정각)으로 기기 상태를 점검해 작업이 실제로 멈춘 긴급 상황만 한 번씩 알린다.
// (복구·데이터 한도 같은 정상 동작은 알리지 않는다)
// PC 가 꺼져도 동작하도록 PC 쪽(fleet)이 아니라 컨트롤플레인에서 판정한다.

export type AlertEnv = { DB: D1Database; TELEGRAM_BOT_TOKEN?: string; TELEGRAM_CHAT_ID?: string }

const OFFLINE_MIN = 10        // 하트비트가 이만큼 끊기면 오프라인
const FAIL_STREAK = 5         // 최근 종료 작업이 연속으로 이만큼 실패하면 알림
const KST_OFFSET = 9 * 3600 * 1000

function nowIso(): string { return new Date().toISOString().replace('T', ' ').slice(0, 19) }
function kstHm(utc: string | null): string {
  if (!utc) return '-'
  return new Date(new Date(utc + 'Z').getTime() + KST_OFFSET).toISOString().slice(5, 16).replace('T', ' ')
}

export async function sendTelegram(env: AlertEnv, text: string) {
  if (!env.TELEGRAM_BOT_TOKEN || !env.TELEGRAM_CHAT_ID) return
  await fetch(`https://api.telegram.org/bot${env.TELEGRAM_BOT_TOKEN}/sendMessage`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ chat_id: env.TELEGRAM_CHAT_ID, text }),
  })
}

export async function checkAlerts(env: AlertEnv) {
  const db = env.DB, now = Date.now()
  const devs = (await db.prepare('SELECT serial, label, model, last_heartbeat, paused FROM devices ORDER BY serial').all()).results as any[]
  const states = (await db.prepare('SELECT key, active FROM alert_state').all()).results as any[]
  const active = new Map(states.map((s) => [s.key, !!s.active]))

  const lines: string[] = []
  const offlineNames: string[] = []
  const setState = async (key: string, on: boolean) => {
    await db.prepare(`INSERT INTO alert_state (key, active, since, updated_at) VALUES (?,?,?,?)
      ON CONFLICT(key) DO UPDATE SET active=excluded.active, since=CASE WHEN alert_state.active=excluded.active THEN alert_state.since ELSE excluded.since END, updated_at=excluded.updated_at`)
      .bind(key, on ? 1 : 0, nowIso(), nowIso()).run()
  }

  for (const d of devs) {
    const name = d.label || d.model || d.serial

    // 1) 오프라인 — 일시정지한 기기는 제외
    const hbAge = d.last_heartbeat ? (now - new Date(d.last_heartbeat + 'Z').getTime()) / 60000 : Infinity
    const offline = !d.paused && hbAge > OFFLINE_MIN
    const kOff = `${d.serial}:offline`
    if (offline && !active.get(kOff)) {
      offlineNames.push(`${name}(${kstHm(d.last_heartbeat)})`)
      await setState(kOff, true)
    } else if (!offline && active.get(kOff)) {
      await setState(kOff, false)
    }

    // 2) 연속 실패
    const recent = (await db.prepare(`SELECT status, error_message FROM jobs
      WHERE device_serial=? AND status IN ('success','failed') AND finished_at IS NOT NULL
      ORDER BY finished_at DESC LIMIT ?`).bind(d.serial, FAIL_STREAK).all()).results as any[]
    const streak = recent.length >= FAIL_STREAK && recent.every((r) => r.status === 'failed')
    const kFail = `${d.serial}:fail_streak`
    if (streak && !active.get(kFail)) {
      const err = (recent[0]?.error_message || '').slice(0, 120)
      lines.push(`⚠️ ${name} 작업 ${FAIL_STREAK}회 연속 실패${err ? ` — ${err}` : ''}`)
      await setState(kFail, true)
    } else if (!streak && active.get(kFail) && recent[0]?.status === 'success') {
      await setState(kFail, false)
    }
  }

  // 새로 끊긴 기기 — 전부 끊겼으면 PC/fleet 문제로 묶어서 알린다
  const live = devs.filter((d) => !d.paused)
  if (offlineNames.length && offlineNames.length === live.length) {
    lines.unshift(`🔴 기기 전체 응답 없음 — PC 꺼짐 또는 fleet 중단 추정
   ${offlineNames.join(', ')}`)
  } else if (offlineNames.length) {
    lines.unshift(`🔴 기기 응답 없음 — 워커 중단 또는 기기 연결 끊김
   ${offlineNames.join(', ')}`)
  }
  await setState('_last_run', true)

  if (lines.length) await sendTelegram(env, `[트래픽] ${kstHm(nowIso())}\n` + lines.join('\n'))
}
