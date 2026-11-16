# -*- coding: utf-8 -*-
"""breezeblue.ai 纯协议注册（curl_cffi 浏览器指纹 + HTTP 全链路）。

仅当服务端要求 Cloudflare Turnstile 时，用一次无头浏览器铸 token；其余全部是裸 HTTP。

链路：
  1) POST /api/auth/email/continue {email, password, referral_code}
     - 403 AUTH_SIGNUP_VERIFICATION_REQUIRED {site_key, challenge_context}
         -> Turnstile(sitekey, action=signup, cData=challenge_context) -> token
         -> POST /api/auth/signup-verification {email, token} -> receipt
         -> 重发 continue（带 signup_verification=receipt）
     - 200 {status:"verification_required", verification_email_sent:true}
  2) 邮件里的 /email-action/dispatch?...&oobCode=... 解析 flow / oobCode
  3) POST /api/auth/email/verify {flow_token, oob_code} -> 200 authenticated（下发 breeze_blue_session）
  4) GET /api/me -> 200
"""
import argparse, asyncio, json, os, random, re, sys, time
from urllib.parse import urlparse, parse_qs

import requests as plain_requests
from curl_cffi import requests as cr
import cloakbrowser

ROOT = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(ROOT, "data")
INVITE = os.environ.get("INVITE_CODE", "")   # 你自己的 breezeblue 邀请码
BASE = "https://breezeblue.ai"
AUTH_PAGE = BASE + "/zh/auth/login?code=" + INVITE
IMPERSONATE = os.environ.get("IMPERSONATE", "chrome")
MAIL_KEY = os.environ.get("MAILNEST_KEY", "")
RESI_PROXY = os.environ.get("RESI_PROXY", "")

JS_MINT = """
async ([sitekey, cdata]) => {
  if (!window.turnstile) {
    await new Promise((res, rej) => {
      const s = document.createElement('script');
      s.src = 'https://challenges.cloudflare.com/turnstile/v0/api.js?render=explicit';
      s.onload = res; s.onerror = () => rej(new Error('script failed'));
      document.head.append(s);
    });
  }
  return await new Promise((resolve, reject) => {
    const box = document.createElement('div');
    document.body.append(box);
    window.turnstile.render(box, {
      sitekey, action: 'signup', cData: cdata, language: 'zh-CN',
      callback: t => resolve(t),
      'error-callback': () => reject(new Error('turnstile error')),
    });
    setTimeout(() => reject(new Error('turnstile timeout')), 45000);
  });
}
"""


def px():
    return {"http": RESI_PROXY, "https": RESI_PROXY} if RESI_PROXY else None


def mail_list(email, tries=5):
    s = plain_requests.Session(); s.trust_env = False
    h = {"Authorization": "Bearer " + MAIL_KEY, "Content-Type": "application/json"}
    for i in range(tries):
        try:
            r = s.post("https://mailnest.top/api/v1/email/receive", json={"email": email}, headers=h,
                       timeout=30, proxies={"http": None, "https": None})
            return r.json().get("data") or []
        except Exception:
            time.sleep(2 + i * 2)
    return []


async def mint_token(site_key, cdata):
    kw = dict(headless=True, stealth_args=True, locale="zh-CN", timezone="Asia/Shanghai")
    if RESI_PROXY:
        kw["proxy"] = RESI_PROXY
    prof = os.path.join(DATA, "token-profile")
    os.makedirs(prof, exist_ok=True)
    ctx = await cloakbrowser.launch_persistent_context_async(prof, **kw)
    page = ctx.pages[0] if getattr(ctx, "pages", None) else await ctx.new_page()
    try:
        await page.goto(AUTH_PAGE, wait_until="domcontentloaded", timeout=90000)
        await page.wait_for_timeout(1200)
        return await page.evaluate(JS_MINT, [site_key, cdata])
    finally:
        try:
            await ctx.close()
        except Exception:
            pass


def parse_dispatch(link):
    q = parse_qs(urlparse(link).query)
    flow = (q.get("flow") or [None])[0]
    oob = (q.get("oobCode") or [None])[0]
    cu = (q.get("continueUrl") or [None])[0]
    if cu and not flow:
        flow = (parse_qs(urlparse(cu).query).get("flow") or [None])[0]
    return flow, oob


def mail_links(email, timeout=180):
    """返回所有 dispatch 链接（新的在前）。旧链接只要未被消费仍然有效。"""
    seen, out, end = set(), [], time.time() + timeout
    while time.time() < end:
        mails = sorted(mail_list(email), key=lambda m: str(m.get("received_at") or ""), reverse=True)
        for m in mails:
            blob = " ".join(str(m.get(k) or "") for k in ("body", "html", "body_preview"))
            for l in re.findall(r"https?://[^\s\"'<>]+", blob):
                l = l.replace("&amp;", "&")
                if "email-action/dispatch" in l and l not in seen:
                    seen.add(l)
                    out.append(l)
        if out:
            return out
        time.sleep(4)
    return out


def register(email, password, invite=INVITE):
    s = cr.Session(impersonate=IMPERSONATE, proxies=px(), timeout=45)
    s.headers.update({"accept": "application/json, text/plain, */*",
                      "origin": BASE, "referer": AUTH_PAGE})
    payload = {"email": email, "password": password, "referral_code": invite}
    r = s.post(BASE + "/api/auth/email/continue", json=payload)
    mode = "direct"
    if r.status_code == 403:
        meta = (r.json() or {}).get("meta") or {}
        site_key, cdata = meta.get("site_key"), meta.get("challenge_context")
        if not site_key:
            return {"ok": False, "stage": "no_challenge", "body": r.text[:200]}
        token = asyncio.run(mint_token(site_key, cdata))
        r2 = s.post(BASE + "/api/auth/signup-verification", json={"email": email, "token": token})
        receipt = (r2.json() or {}).get("receipt")
        if not receipt:
            return {"ok": False, "stage": "receipt_failed", "body": r2.text[:200]}
        mode = "captcha"
        payload["signup_verification"] = receipt
        r = s.post(BASE + "/api/auth/email/continue", json=payload)
    if r.status_code != 200:
        return {"ok": False, "stage": "continue_failed", "body": r.text[:200], "mode": mode}

    links = mail_links(email)
    if not links:
        return {"ok": False, "stage": "no_mail", "mode": mode}
    v = None
    for link in links[:4]:
        flow, oob = parse_dispatch(link)
        if not (flow and oob):
            continue
        v = s.post(BASE + "/api/auth/email/verify", json={"flow_token": flow, "oob_code": oob})
        if v.status_code == 200:
            break
    if v is None or v.status_code != 200:
        return {"ok": False, "stage": "verify_failed",
                "body": (v.text[:200] if v is not None else ""), "mode": mode}
    me = s.get(BASE + "/api/me")
    body = {}
    try:
        body = me.json()
    except Exception:
        pass
    cookies = {c.name: c.value for c in s.cookies.jar}
    return {"ok": me.status_code == 200, "stage": "done", "mode": mode,
            "public_id": body.get("public_id"), "referral": body.get("invited_by_referral_code"),
            "own_code": body.get("referral_code"), "cookies": cookies}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--email", required=True)
    ap.add_argument("--password", default="")
    ap.add_argument("--invite", default=INVITE)
    args = ap.parse_args()
    pwd = args.password or ("Bz" + str(random.randint(100000, 999999)) + ".kQe" + str(random.randint(100, 999)))
    res = register(args.email, pwd, args.invite)
    res.update(email=args.email, password=pwd)
    print(json.dumps(res, ensure_ascii=False)[:700])
    if res.get("ok"):
        os.makedirs(DATA, exist_ok=True)
        with open(os.path.join(DATA, "proto_ok.jsonl"), "a", encoding="utf-8") as f:
            f.write(json.dumps(res, ensure_ascii=False) + "\n")


if __name__ == "__main__":
    main()