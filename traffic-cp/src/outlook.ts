// 아웃룩 코드리더 (Cloudflare Browser Rendering / Puppeteer)
// campaign_web/outlook_cdp.py 이식: plain 웹로그인(OAuth 미사용) → 받은편지함 DOM 코드 추출.
// proofs('내 계정을 보호하세요') 넛지는 메일 URL 재진입으로 우회.
import puppeteer from '@cloudflare/puppeteer'

const MAIL_URL = 'https://outlook.live.com/mail/0/inbox'
const YEARS = new Set(['2023', '2024', '2025', '2026', '2027', '2028'])

function extractCode(text: string): string | null {
  // 카카오 인증번호는 6~8자리. 이메일 주소 등에 박힌 숫자(앞뒤가 글자/@/.)는 배제.
  const re = /(\d{6,8})/g
  let m: RegExpExecArray | null
  const near: string[] = [], any68: string[] = []
  while ((m = re.exec(text))) {
    const g = m[1]
    if (YEARS.has(g)) continue
    const before = m.index > 0 ? text[m.index - 1] : ' '
    const after = m.index + g.length < text.length ? text[m.index + g.length] : ' '
    // 앞뒤가 숫자/영문/@/. 이면 코드가 아니라 식별자(이메일·URL 등)
    if (/[0-9A-Za-z@._-]/.test(before) || /[0-9A-Za-z@._-]/.test(after)) continue
    const w = text.slice(Math.max(0, m.index - 40), m.index + g.length + 20)
    if (/(인증|verification|code|카카오)/.test(w)) near.push(g)
    any68.push(g)
  }
  return near[0] ?? any68[0] ?? null
}

async function readState(page: any): Promise<any> {
  return await page.evaluate(() => {
    const b = document.body ? document.body.innerText : ''
    const url = location.href
    const vis = (el: any) => !!(el && (el as HTMLElement).offsetParent !== null)
    return {
      url,
      inbox: (url.includes('outlook.live.com/mail') || url.includes('outlook.office.com/mail')) && !b.includes('암호'),
      hasEmail: vis(document.querySelector('input[type=email]') || document.querySelector('input[name=loginfmt]')),
      hasPw: vis(document.querySelector('input[type=password]')),
      stay: b.includes('로그인 상태를 유지') || b.includes('Stay signed in'),
      proofs: url.includes('proofs') || b.includes('내 계정을 보호'),
      marketing: b.includes('무료 계정 만들기') || b.includes('다운로드') || url.includes('microsoft.com'),
    }
  })
}

async function setInput(page: any, selector: string, value: string) {
  await page.evaluate((sel: string, val: string) => {
    const el: any = document.querySelector(sel)
    if (!el) return
    const proto = window.HTMLInputElement.prototype
    const setter = Object.getOwnPropertyDescriptor(proto, 'value')!.set!
    setter.call(el, val)
    el.dispatchEvent(new Event('input', { bubbles: true }))
    el.dispatchEvent(new Event('change', { bubbles: true }))
  }, selector, value)
}

async function clickText(page: any, texts: string[]): Promise<boolean> {
  return await page.evaluate((want: string[]) => {
    const els = Array.from(document.querySelectorAll('a,button,input[type=submit],div[role=button],span'))
    for (const el of els) {
      const t = ((el as HTMLInputElement).value || (el as HTMLElement).innerText || el.getAttribute('aria-label') || '').trim()
      for (const w of want) if (t === w || t.includes(w)) { (el as HTMLElement).click(); return true }
    }
    return false
  }, texts)
}

async function submitForm(page: any) {
  await page.evaluate(() => {
    const n: any = document.querySelector('#idSIButton9') || document.querySelector('button[type=submit]') || document.querySelector('input[type=submit]')
    if (n) n.click()
  })
}

async function ensureLoggedIn(page: any, email: string, password: string, deadlineMs: number): Promise<boolean> {
  await page.goto(MAIL_URL, { waitUntil: 'domcontentloaded' })
  await new Promise((r) => setTimeout(r, 6000))
  let proofsBypass = 0
  while (Date.now() < deadlineMs) {
    const st = await readState(page)
    if (st.inbox) return true
    if (st.proofs) {
      if (++proofsBypass > 3) return false
      await page.goto(MAIL_URL, { waitUntil: 'domcontentloaded' })
      await new Promise((r) => setTimeout(r, 6000))
      continue
    }
    if (st.stay) { await clickText(page, ['예', 'Yes']); await submitForm(page); await new Promise((r) => setTimeout(r, 4000)); continue }
    if (st.hasPw) { await setInput(page, 'input[type=password]', password); await submitForm(page); await new Promise((r) => setTimeout(r, 5000)); continue }
    if (st.hasEmail) {
      const sel = (await page.$('input[type=email]')) ? 'input[type=email]' : 'input[name=loginfmt]'
      await setInput(page, sel, email); await submitForm(page); await new Promise((r) => setTimeout(r, 5000)); continue
    }
    if (st.marketing) {
      const clicked = await clickText(page, ['계속 로그인', 'Outlook에 로그인', '로그인', 'Sign in'])
      if (!clicked) { await page.goto(MAIL_URL, { waitUntil: 'domcontentloaded' }) }
      await new Promise((r) => setTimeout(r, 5000)); continue
    }
    await new Promise((r) => setTimeout(r, 2500))
  }
  return false
}

async function readCode(page: any, deadlineMs: number): Promise<string | null> {
  while (Date.now() < deadlineMs) {
    await page.goto(MAIL_URL, { waitUntil: 'domcontentloaded' })
    await new Promise((r) => setTimeout(r, 9000)) // SPA 메일목록 로드
    // 인증번호 메일 행 클릭
    const opened = await page.evaluate(() => {
      const rows = Array.from(document.querySelectorAll('[role=option],[role=listitem],div[data-convid],div[aria-label]'))
      for (const r of rows) { const t = ((r as HTMLElement).innerText || r.getAttribute('aria-label') || ''); if (t.includes('인증번호')) { (r as HTMLElement).click(); return true } }
      for (const r of rows) { const t = ((r as HTMLElement).innerText || r.getAttribute('aria-label') || ''); if (t.includes('카카오')) { (r as HTMLElement).click(); return true } }
      return false
    })
    await new Promise((r) => setTimeout(r, 4000))
    const body = await page.evaluate(() => document.body ? document.body.innerText : '')
    const code = extractCode(body)
    if (code) return code
  }
  return null
}

export async function fetchKakaoCode(env: any, mailEmail: string, mailPassword: string, waitSecs: number): Promise<{ code: string | null; detail: string }> {
  if (!mailEmail || !mailPassword) return { code: null, detail: 'no_mail_cred' }
  let browser: any
  try {
    browser = await puppeteer.launch(env.BROWSER)
  } catch (e: any) {
    return { code: null, detail: 'browser_launch_failed:' + String(e).slice(0, 80) }
  }
  try {
    const page = await browser.newPage()
    await page.setViewport({ width: 1200, height: 2000 })
    const deadline = Date.now() + Math.min(Math.max(waitSecs, 40), 170) * 1000
    const ok = await ensureLoggedIn(page, mailEmail, mailPassword, deadline)
    if (!ok) return { code: null, detail: 'login_failed_or_risk_screen' }
    const code = await readCode(page, deadline)
    return { code, detail: code ? 'ok' : 'no_code' }
  } catch (e: any) {
    return { code: null, detail: 'error:' + String(e).slice(0, 100) }
  } finally {
    try { await browser.close() } catch {}
  }
}
