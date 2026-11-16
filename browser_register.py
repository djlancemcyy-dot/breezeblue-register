# -*- coding: utf-8 -*-
"""UI 版注册：CloakBrowser 指纹浏览器 + 真实页面点击（协议被 Turnstile 拦时的兜底）。

用法:
  export MAILNEST_KEY=sk_xxx
  export RESI_PROXY=http://127.0.0.1:7891     # 可选
  export INVITE_CODE=XXXXXX
  python browser_register.py --email xxx@outlook.com
"""
import argparse, asyncio, json, os, random, re, time

import requests
import cloakbrowser

ROOT = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(ROOT, "data")
INVITE = os.environ.get("INVITE_CODE", "")
BASE = "https://breezeblue.ai"
URL = BASE + "/zh?code=" + INVITE
AUTH = BASE + "/zh/auth/login?code=" + INVITE + "&next=%2Fzh%2Fapp%2Fhome"
MAIL_KEY = os.environ.get("MAILNEST_KEY", "")
RESI_PROXY = os.environ.get("RESI_PROXY", "")


def mails(email, tries=4):
    s = requests.Session(); s.trust_env = False
    h = {"Authorization": "Bearer " + MAIL_KEY, "Content-Type": "application/json"}
    for i in range(tries):
        try:
            r = s.post("https://mailnest.top/api/v1/email/receive", json={"email": email}, headers=h,
                       timeout=30, proxies={"http": None, "https": None})
            return r.json().get("data") or []
        except Exception:
            time.sleep(2 + i * 2)
    return []


def wait_link(email, before, timeout=200):
    end = time.time() + timeout
    while time.time() < end:
        for m in mails(email):
            if str(m.get("received_at")) + str(m.get("subject")) in before:
                continue
            blob = " ".join(str(m.get(k) or "") for k in ("body", "html", "body_preview"))
            for l in re.findall(r"https?://[^\s\"'<>]+", blob):
                if "breezeblue" in l or "firebaseapp" in l:
                    return l.replace("&amp;", "&").rstrip(").,\"'")
        time.sleep(4)
    return None


async def run(email, password):
    prof = os.path.join(DATA, "profiles", email.replace("@", "_at_"))
    os.makedirs(prof, exist_ok=True)
    kw = dict(headless=True, stealth_args=True, locale="zh-CN", timezone="Asia/Shanghai", humanize=True)
    if RESI_PROXY:
        kw["proxy"] = RESI_PROXY
    ctx = await cloakbrowser.launch_persistent_context_async(prof, **kw)
    page = ctx.pages[0] if getattr(ctx, "pages", None) else await ctx.new_page()
    res = {"email": email, "password": password, "ok": False, "stage": "start"}
    try:
        before = {str(m.get("received_at")) + str(m.get("subject")) for m in mails(email)}
        await page.goto(URL, wait_until="domcontentloaded", timeout=90000)
        await page.wait_for_timeout(2500)
        await page.goto(AUTH, wait_until="domcontentloaded", timeout=90000)
        await page.wait_for_timeout(3000)
        await page.locator("input[type=email]").first.fill(email)
        await page.locator("input[type=password]").first.fill(password)
        await page.get_by_role("button", name="继续", exact=True).click()
        res["stage"] = "sent"
        link = wait_link(email, before)
        if not link:
            res["stage"] = "no_mail"
            return res, ctx
        await page.goto(link, wait_until="domcontentloaded", timeout=90000)
        await page.wait_for_timeout(6000)
        me = await page.evaluate("async () => { const r = await fetch('/api/me', {credentials:'include'});"
                                 " return {s: r.status, b: (await r.text()).slice(0, 300)}; }")
        res.update(me=me, ok=(me.get("s") == 200), stage="done", cookies=await ctx.cookies())
        return res, ctx
    except Exception as e:
        res.update(stage="error:" + type(e).__name__, error=str(e)[:200])
        return res, ctx


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--email", required=True)
    ap.add_argument("--password", default="")
    args = ap.parse_args()
    pwd = args.password or ("Bz" + str(random.randint(100000, 999999)) + ".kQe" + str(random.randint(100, 999)))
    res, ctx = await run(args.email, pwd)
    print(json.dumps({k: res.get(k) for k in ("email", "stage", "ok")}, ensure_ascii=False))
    os.makedirs(DATA, exist_ok=True)
    with open(os.path.join(DATA, "browser_result.jsonl"), "a", encoding="utf-8") as f:
        f.write(json.dumps(res, ensure_ascii=False) + "\n")
    try:
        await ctx.close()
    except Exception:
        pass


if __name__ == "__main__":
    asyncio.run(main())
