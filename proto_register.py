# -*- coding: utf-8 -*-
"""BreezeBlue（breezeblue.ai）批量注册 · 一条命令跑 N 个号。

自动做三件事：
  1) 验活：MailNest 库存里挑邮箱，receive 返回 00000 才算活箱（D0004/D0006 直接跳过）
  2) 换箱：某个箱发不出信 / 拿不到魔法链接，就换下一个箱重来
  3) 换出口：Turnstile 铸 token 失败或链路传输失败，就换下一个住宅出口重来

用法：
  export MAILNEST_KEY=sk_xxx
  export INVITE_CODE=XXXXXX
  python proto_register.py --count 5 \
      --proxies http://127.0.0.1:7891,http://127.0.0.1:7892
  # 或：--proxy-file exits.txt（每行一个 http://host:port）

只跑单个邮箱（跳过库存）：
  python proto_register.py --email xxx@outlook.com --proxies http://127.0.0.1:7891
"""
import argparse, asyncio, io, json, os, random, re, sys, time
from urllib.parse import urlparse, parse_qs

import requests as plain_requests
from curl_cffi import requests as cr
import cloakbrowser

ROOT = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(ROOT, "data")
INVITE = os.environ.get("INVITE_CODE", "")
BASE = "https://breezeblue.ai"
AUTH_PAGE = BASE + "/zh/auth/login?code=" + INVITE
IMPERSONATE = os.environ.get("IMPERSONATE", "chrome")
MAIL_KEY = os.environ.get("MAILNEST_KEY", "")
MAIL_BASE = "https://mailnest.top"
USED = os.path.join(DATA, "used_emails.txt")
DEAD = os.path.join(DATA, "dead_boxes.txt")
ACCS = os.path.join(DATA, "accounts.jsonl")
FAILS = os.path.join(DATA, "failed.jsonl")

JS_MINT = """
async ([sitekey, cdata]) => {
  if (!window.turnstile) {
    await new Promise((res, rej) => {
      const s = document.createElement('script');
      s.src = 'https://challenges.cloudflare.com/turnstile/v0/api.js?render=explicit';
      s.onload = res; s.onerror = () => rej(new Error('script failed'));
      document.head.appendChild(s);
    });
  }
  return await new Promise((resolve, reject) => {
    const box = document.createElement('div');
    document.body.appendChild(box);
    window.turnstile.render(box, {
      sitekey, action: 'signup', cData: cdata, language: 'zh-CN',
      callback: t => resolve(t),
      'error-callback': () => reject(new Error('turnstile error')),
    });
    setTimeout(() => reject(new Error('turnstile timeout')), 45000);
  });
}
"""


# ---------------- 邮箱 ----------------

def mail_api(path, payload=None, method="POST", tries=4):
    s = plain_requests.Session(); s.trust_env = False
    h = {"Authorization": "Bearer " + MAIL_KEY, "Content-Type": "application/json"}
    for i in range(tries):
        try:
            if method == "POST":
                r = s.post(MAIL_BASE + path, json=payload or {}, headers=h, timeout=30,
                           proxies={"http": None, "https": None})
            else:
                r = s.get(MAIL_BASE + path, headers=h, timeout=30, proxies={"http": None, "https": None})
            return r.json()
        except Exception:
            time.sleep(2 + i * 2)
    return {"code": "NETERR"}


def mail_receive(email):
    j = mail_api("/api/v1/email/receive", {"email": email})
    return j.get("code"), (j.get("data") or [])


def box_alive(email):
    code, _ = mail_receive(email)
    return code == "00000", code


def inventory_boxes(pages=8):
    """拉已购独占箱（having/holding 都算可用）。"""
    out, page = [], 1
    while page <= pages:
        j = mail_api("/api/v1/email/exclusive?page=%d&page_size=100" % page, method="GET")
        if j.get("code") != "00000":
            break
        rows = j.get("data") or []
        if isinstance(rows, dict):
            rows = rows.get("list") or rows.get("items") or []
        if not rows:
            break
        for r in rows:
            if r.get("status") in ("having", "holding") and r.get("email"):
                out.append(r["email"])
        if len(rows) < 100:
            break
        page += 1
    return list(dict.fromkeys(out))


def lines(path):
    return [x.strip() for x in io.open(path, encoding="utf-8")] if os.path.exists(path) else []


def append(path, value):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with io.open(path, "a", encoding="utf-8") as f:
        f.write(value + "\n")


def pick_box(pool, blocked, max_tries=8):
    """从池里挑一个能收信的箱；死箱写 dead_boxes。"""
    random.shuffle(pool)
    for email in pool[:max_tries]:
        if email in blocked:
            continue
        ok, code = box_alive(email)
        if ok:
            return email
        append(DEAD, email)
        blocked.add(email)
        print("   [box] %s 不可用 (%s)，换下一个" % (email, code), flush=True)
    return None


# ---------------- 出口 ----------------

def exits_from_args(args):
    out = []
    if args.proxies:
        out += [p.strip() for p in args.proxies.split(",") if p.strip()]
    if args.proxy_file:
        out += lines(args.proxy_file)
    return list(dict.fromkeys(out))


def _px(proxy):
    return {"http": proxy, "https": proxy} if proxy else None


def exit_alive(proxy):
    if not proxy:
        return True
    try:
        r = plain_requests.get("https://api.ipify.org", proxies=_px(proxy), timeout=15)
        return r.status_code == 200 and r.text.strip().count(".") == 3
    except Exception:
        return False


def next_exit(exits, idx):
    if not exits:
        return None, idx
    n = len(exits)
    for i in range(n):
        cand = exits[(idx + i) % n]
        if exit_alive(cand):
            return cand, (idx + i + 1) % n
    return None, idx


# ---------------- Turnstile ----------------

async def mint_token(site_key, cdata, proxy):
    kw = dict(headless=True, stealth_args=True, locale="zh-CN", timezone="Asia/Shanghai")
    if proxy:
        kw["proxy"] = proxy
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


# ---------------- 单次尝试 ----------------

def parse_dispatch(link):
    q = parse_qs(urlparse(link).query)
    flow = (q.get("flow") or [None])[0]
    oob = (q.get("oobCode") or [None])[0]
    cu = (q.get("continueUrl") or [None])[0]
    if cu and not flow:
        flow = (parse_qs(urlparse(cu).query).get("flow") or [None])[0]
    return flow, oob


def wait_dispatch_links(email, timeout=180):
    seen, out, end = set(), [], time.time() + timeout
    while time.time() < end:
        _, mails = mail_receive(email)
        for m in sorted(mails, key=lambda x: str(x.get("received_at") or ""), reverse=True):
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


def attempt(email, password, invite, proxy):
    """跑一次完整链路；返回 {ok, stage, ...}"""
    s = cr.Session(impersonate=IMPERSONATE, proxies=_px(proxy), timeout=45)
    s.headers.update({"accept": "application/json, text/plain, */*",
                      "origin": BASE, "referer": AUTH_PAGE})
    payload = {"email": email, "password": password}
    if invite:
        payload["referral_code"] = invite
    r = s.post(BASE + "/api/auth/email/continue", json=payload)
    mode = "direct"

    if r.status_code == 403:
        body = {}
        try:
            body = r.json()
        except Exception:
            pass
        if body.get("code") == "AUTH_SIGNUP_VERIFICATION_REQUIRED":
            meta = body.get("meta") or {}
            site_key, cdata = meta.get("site_key"), meta.get("challenge_context")
            if not site_key:
                return {"ok": False, "stage": "no_challenge", "mode": mode}
            try:
                token = asyncio.run(mint_token(site_key, cdata, proxy))
            except Exception as exc:
                return {"ok": False, "stage": "captcha_failed", "error": str(exc)[:120], "mode": mode}
            r2 = s.post(BASE + "/api/auth/signup-verification", json={"email": email, "token": token})
            receipt = (r2.json() or {}).get("receipt")
            if not receipt:
                return {"ok": False, "stage": "receipt_failed", "body": r2.text[:160], "mode": mode}
            mode = "captcha"
            payload["signup_verification"] = receipt
            r = s.post(BASE + "/api/auth/email/continue", json=payload)

    if r.status_code == 429:
        wait = 45
        try:
            wait = int((r.json().get("meta") or {}).get("retry_after_seconds") or 45)
        except Exception:
            pass
        return {"ok": False, "stage": "rate_limited", "retry_after": wait, "mode": mode}
    if r.status_code != 200:
        return {"ok": False, "stage": "continue_failed", "body": r.text[:160], "mode": mode}

    links = wait_dispatch_links(email)
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
        return {"ok": False, "stage": "verify_failed", "mode": mode,
                "body": (v.text[:160] if v is not None else "")}

    me = s.get(BASE + "/api/me")
    body = {}
    try:
        body = me.json()
    except Exception:
        pass
    return {"ok": me.status_code == 200, "stage": "done", "mode": mode,
            "public_id": body.get("public_id"), "referral": body.get("invited_by_referral_code"),
            "own_code": body.get("referral_code"),
            "cookies": {c.name: c.value for c in s.cookies.jar}}


def register_one(email, password, invite, exits, exit_idx, max_exit_tries=3):
    """同一个邮箱 + 多个出口重试；captcha/传输失败就换出口。"""
    last = None
    for i in range(max_exit_tries):
        proxy, exit_idx = next_exit(exits, exit_idx)
        if exits and proxy is None:
            return {"ok": False, "stage": "no_exit", "exit_idx": exit_idx}
        print("   [try %d] exit=%s" % (i + 1, proxy or "direct"), flush=True)
        res = attempt(email, password, invite, proxy)
        res["exit"] = proxy
        last = res
        if res.get("ok"):
            return res
        if res.get("stage") == "rate_limited":
            time.sleep(int(res.get("retry_after") or 45))
            continue
        if res.get("stage") in ("no_mail", "verify_failed", "receipt_failed"):
            # 邮箱可能是死箱或链接失效，交由上层换箱
            return res
        time.sleep(random.uniform(1.5, 4))
    return last or {"ok": False, "stage": "exhausted", "exit_idx": exit_idx}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--count", type=int, default=1, help="要成功多少个号")
    ap.add_argument("--email", default="", help="只跑这个邮箱（跳过库存）")
    ap.add_argument("--password", default="", help="指定密码（默认随机）")
    ap.add_argument("--invite", default=INVITE)
    ap.add_argument("--proxies", default="", help="逗号分隔的出口列表 http://host:port")
    ap.add_argument("--proxy-file", default="", help="每行一个出口")
    ap.add_argument("--max-box-tries", type=int, default=8, help="每个号最多换几个箱")
    ap.add_argument("--max-exit-tries", type=int, default=3, help="每次最多换几个出口")
    args = ap.parse_args()

    os.makedirs(DATA, exist_ok=True)
    exits = exits_from_args(args)
    if exits:
        alive = [p for p in exits if exit_alive(p)]
        print("[exit] 可用出口 %d/%d: %s" % (len(alive), len(exits), ", ".join(alive)), flush=True)
        exits = alive or exits

    blocked = set(lines(DEAD))
    used = set(lines(USED))
    pool = [args.email] if args.email else [e for e in inventory_boxes() if e not in used]
    if not pool:
        print("没有可用邮箱：检查 MAILNEST_KEY 或库存")
        return

    done, exit_idx = 0, 0
    while done < args.count:
        box = pick_box(pool, blocked)
        if not box:
            print("库存里没有活箱了，停")
            break
        pool = [e for e in pool if e != box]
        pwd = args.password or ("Bz" + str(random.randint(100000, 999999)) + ".kQe" + str(random.randint(100, 999)))
        print("\n[%d/%d] %s" % (done + 1, args.count, box), flush=True)

        ok = False
        for bx in range(args.max_box_tries):
            res = register_one(box, pwd, args.invite, exits, exit_idx, args.max_exit_tries)
            exit_idx = res.get("exit_idx", exit_idx)
            print("   -> ok=%s stage=%s mode=%s exit=%s" % (res.get("ok"), res.get("stage"),
                                                            res.get("mode"), res.get("exit")), flush=True)
            if res.get("ok"):
                res.update(email=box, password=pwd, at=time.strftime("%Y-%m-%d %H:%M:%S"))
                append(ACCS, json.dumps(res, ensure_ascii=False))
                append(USED, box)
                print("   ✅ %s  public_id=%s  own_code=%s" % (box, res.get("public_id"), res.get("own_code")), flush=True)
                done += 1
                ok = True
                break
            append(FAILS, json.dumps(dict(res, email=box), ensure_ascii=False))
            if res.get("stage") in ("no_mail", "verify_failed", "box_dead"):
                blocked.add(box)
                nxt = pick_box(pool, blocked)
                if not nxt:
                    break
                pool = [e for e in pool if e != nxt]
                box = nxt
                print("   [box] 换箱 -> %s" % box, flush=True)
            elif res.get("stage") == "captcha_failed":
                continue
            else:
                time.sleep(random.uniform(3, 6))
        if not ok and not pool:
            print("没有更多箱可试，停")
            break
        time.sleep(random.uniform(2, 5))

    print("\n完成 %d/%d" % (done, args.count))


if __name__ == "__main__":
    main()
