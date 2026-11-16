# -*- coding: utf-8 -*-
"""BreezeBlue Web 面板：启动批量注册、看进度/日志、管理账号、查库存。

启动：
    export MAILNEST_KEY=sk_xxx
    export INVITE_CODE=XXXXXX
    python panel.py --port 8799
    浏览器打开 http://127.0.0.1:8799
"""
import argparse, csv, io, json, os, threading, time
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

import proto_register as core

ROOT = os.path.dirname(os.path.abspath(__file__))
WEB = os.path.join(ROOT, "web")
DATA = core.DATA

LOGS = deque(maxlen=6000)
LOG_SEQ = [0]
LOG_LOCK = threading.Lock()
JOB = {"running": False, "target": 0, "done": 0, "current": "", "stage": "",
       "started_at": None, "finished_at": None, "error": "", "invite": core.INVITE}
STOP = threading.Event()
PROXY = os.environ.get("RESI_PROXY", "")   # 面板侧访问 breezeblue 用的出口
INV_CACHE = {"at": 0, "rows": []}
CREDITS = {}            # email -> {balance, ok, at}
CRED_LOCK = threading.Lock()
CRED_THREAD = {"running": False}


def credit_refresh(email, row=None):
    """用「邮箱+密码」重新取一次会话（服务端对老号直接 200 authenticated），再读账号态与积分。"""
    row = row or next((r for r in read_accounts() if r.get("email") == email), None)
    if not row:
        return None
    try:
        from curl_cffi import requests as cr
        proxy = PROXY or None
        s = cr.Session(impersonate=core.IMPERSONATE, timeout=30,
                       proxies=core._px(proxy) if proxy else None)
        s.headers.update({"accept": "application/json, text/plain, */*",
                          "origin": core.BASE, "referer": core.AUTH_PAGE})
        payload = {"email": email, "password": row.get("password") or ""}
        r = None
        for attempt in range(3):
            try:
                r = s.post(core.BASE + "/api/auth/email/continue", json=payload)
                break
            except Exception:
                time.sleep(1.5 + attempt)
                s = account_session(row)
        if r is None:
            raise RuntimeError("login transport failed")
        info = {"at": time.time(), "code": r.status_code}
        body = {}
        try:
            body = r.json()
        except Exception:
            pass
        info["state"] = body.get("status")
        if r.status_code == 200 and body.get("status") == "authenticated":
            me = s.get(core.BASE + "/api/me")
            crr = s.get(core.BASE + "/api/credits/summary")
            info.update({"ok": me.status_code == 200, "me": me.status_code})
            if me.status_code == 200:
                try:
                    b = me.json()
                    info.update({"public_id": b.get("public_id"), "own_code": b.get("referral_code"),
                                 "referral": b.get("invited_by_referral_code"),
                                 "email_verified": b.get("email_verified")})
                except Exception:
                    pass
                row["cookies"] = {c.name: c.value for c in s.cookies.jar}
                try:
                    rows = read_accounts()
                    with io.open(core.ACCS, "w", encoding="utf-8") as f:
                        for r_ in rows:
                            f.write(json.dumps(r_, ensure_ascii=False) + "\n")
                except Exception:
                    pass
            if crr.status_code == 200:
                try:
                    c = crr.json()
                    info.update({"balance": c.get("balance"), "daily_available": c.get("daily_claim_available"),
                                 "granted": c.get("granted_total"), "consumed": c.get("consumed_total")})
                except Exception:
                    pass
        else:
            info["ok"] = False
        with CRED_LOCK:
            CREDITS[email] = info
        return info
    except Exception as exc:
        with CRED_LOCK:
            CREDITS[email] = {"at": time.time(), "ok": False, "error": str(exc)[:80]}
        return None


def credit_refresh_all(max_age=300, background=True):
    rows = read_accounts()
    todo = []
    with CRED_LOCK:
        for r in rows:
            e = r.get("email")
            c = CREDITS.get(e)
            if not c or time.time() - c.get("at", 0) > max_age:
                todo.append(r)
    if not todo:
        return 0
    if not background:
        for r in todo:
            credit_refresh(r["email"], r)
        return len(todo)
    if CRED_THREAD["running"]:
        return 0

    def run():
        CRED_THREAD["running"] = True
        try:
            for r in todo:
                credit_refresh(r["email"], r)
                time.sleep(0.4)
        finally:
            CRED_THREAD["running"] = False

    threading.Thread(target=run, daemon=True).start()
    return len(todo)


def log(msg):
    with LOG_LOCK:
        LOG_SEQ[0] += 1
        LOGS.append({"seq": LOG_SEQ[0], "ts": time.strftime("%H:%M:%S"), "line": str(msg)})


def read_accounts():
    rows = []
    if os.path.exists(core.ACCS):
        with io.open(core.ACCS, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rows.append(json.loads(line))
                except Exception:
                    pass
    return rows


def write_json(handler, obj, code=200):
    body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
    handler.send_response(code)
    handler.send_header("content-type", "application/json; charset=utf-8")
    handler.send_header("content-length", str(len(body)))
    handler.end_headers()
    handler.wfile.write(body)


def job_thread(params):
    JOB.update({"running": True, "target": int(params.get("count") or 1), "done": 0,
                "current": "", "stage": "starting", "started_at": time.strftime("%Y-%m-%d %H:%M:%S"),
                "finished_at": None, "error": "", "invite": params.get("invite") or core.INVITE})
    STOP.clear()
    log("[job] start count=%s proxies=%s invite=%s" % (JOB["target"], params.get("proxies"), JOB["invite"]))
    try:
        core.run_batch(count=JOB["target"],
                       proxies=params.get("proxies") or "",
                       invite=params.get("invite") or core.INVITE,
                       password=params.get("password") or "",
                       max_box_tries=int(params.get("max_box_tries") or 8),
                       max_exit_tries=int(params.get("max_exit_tries") or 3),
                       log=log, should_stop=STOP.is_set, state=JOB)
    except Exception as exc:
        JOB["error"] = "%s: %s" % (type(exc).__name__, exc)
        log("[error] " + JOB["error"])
    finally:
        JOB["running"] = False
        if not JOB.get("finished_at"):
            JOB["finished_at"] = time.strftime("%Y-%m-%d %H:%M:%S")


def account_session(row):
    """用台账里的 cookie 复原会话；出口优先用 RESI_PROXY（本机直连 breezeblue 常被掐）。"""
    from curl_cffi import requests as cr
    proxy = PROXY or None
    s = cr.Session(impersonate=core.IMPERSONATE, timeout=30, proxies=core._px(proxy) if proxy else None)
    for name, val in (row.get("cookies") or {}).items():
        try:
            if name.startswith("__Host-") or name.startswith("__Secure-"):
                s.cookies.set(name, val, path="/")
            else:
                s.cookies.set(name, val, domain="breezeblue.ai")
        except Exception:
            pass
    s.headers.update({"accept": "application/json", "user-agent": "Mozilla/5.0"})
    return s


def account_check(email):
    info = credit_refresh(email) or {}
    row = next((r for r in read_accounts() if r.get("email") == email), None)
    if not row:
        return {"ok": False, "error": "account not found"}
    s = account_session(row)
    me = s.get(core.BASE + "/api/me")
    cr_ = s.get(core.BASE + "/api/credits/summary")
    out = {"ok": me.status_code == 200, "me_status": me.status_code}
    if out["ok"]:
        try:
            body = me.json()
            out.update({"public_id": body.get("public_id"), "referral": body.get("invited_by_referral_code"),
                        "own_code": body.get("referral_code"), "email_verified": body.get("email_verified")})
        except Exception:
            pass
    if cr_.status_code == 200:
        try:
            c = cr_.json()
            out["credits"] = c.get("balance")
            out["daily_claim_available"] = c.get("daily_claim_available")
        except Exception:
            pass
    return out


def account_claim(email):
    row = next((r for r in read_accounts() if r.get("email") == email), None)
    if not row:
        return {"ok": False, "error": "account not found"}
    s = account_session(row)
    r = s.post(core.BASE + "/api/credits/daily-claim", json={})
    return {"ok": r.status_code == 200, "status": r.status_code, "body": r.text[:300]}


def account_relogin(email):
    """会话过期时用邮箱密码重新走一遍登录（魔法链接），把新 cookie 写回台账。"""
    rows = read_accounts()
    row = next((r for r in rows if r.get("email") == email), None)
    if not row:
        return {"ok": False, "error": "account not found"}
    from curl_cffi import requests as cr
    s = cr.Session(impersonate=core.IMPERSONATE, proxies=core._px(PROXY) if PROXY else None, timeout=45)
    s.headers.update({"accept": "application/json, text/plain, */*",
                      "origin": core.BASE, "referer": core.AUTH_PAGE})
    r = s.post(core.BASE + "/api/auth/email/continue",
               json={"email": email, "password": row.get("password") or ""})
    if r.status_code not in (200, 403):
        return {"ok": False, "stage": "continue_failed", "status": r.status_code, "body": r.text[:160]}
    body = {}
    try:
        body = r.json()
    except Exception:
        pass
    # 老号 + 正确密码：continue 直接 200 authenticated 并下发会话，无需邮件
    if r.status_code == 200 and (body.get("status") == "authenticated"
                                 or any(c.name == "breeze_blue_session" for c in s.cookies.jar)):
        me = s.get(core.BASE + "/api/me")
        if me.status_code == 200:
            row["cookies"] = {c.name: c.value for c in s.cookies.jar}
            with io.open(core.ACCS, "w", encoding="utf-8") as f:
                for r_ in rows:
                    f.write(json.dumps(r_, ensure_ascii=False) + "\n")
            info = credit_refresh(email, row) or {}
            return {"ok": True, "public_id": info.get("public_id"), "balance": info.get("balance"),
                    "mode": "password"}
    links = core.wait_dispatch_links(email, timeout=120)
    if not links:
        return {"ok": False, "stage": "no_mail"}
    v = None
    for link in links[:4]:
        flow, oob = core.parse_dispatch(link)
        if not (flow and oob):
            continue
        v = s.post(core.BASE + "/api/auth/email/verify", json={"flow_token": flow, "oob_code": oob})
        if v.status_code == 200:
            break
    if v is None or v.status_code != 200:
        return {"ok": False, "stage": "verify_failed"}
    me = s.get(core.BASE + "/api/me")
    if me.status_code != 200:
        return {"ok": False, "stage": "me_failed"}
    row["cookies"] = {c.name: c.value for c in s.cookies.jar}
    with io.open(core.ACCS, "w", encoding="utf-8") as f:
        for r_ in rows:
            f.write(json.dumps(r_, ensure_ascii=False) + "\n")
    info = credit_refresh(email, row)
    return {"ok": True, "public_id": (info or {}).get("public_id"), "balance": (info or {}).get("balance")}


def inventory(limit=240, check=0):
    now = time.time()
    if not check and INV_CACHE["rows"] and now - INV_CACHE["at"] < 120:
        return INV_CACHE["rows"][:limit]
    boxes = core.inventory_boxes()
    used = set(core.lines(core.USED))
    dead = set(core.lines(core.DEAD))
    rows = []
    for e in boxes[:limit]:
        rows.append({"email": e, "used": e in used, "dead": e in dead})
    if check:
        for r in rows[:check]:
            ok, _ = core.box_alive(r["email"])
            r["alive"] = ok
    INV_CACHE.update({"at": now, "rows": rows})
    return rows


class Handler(BaseHTTPRequestHandler):
    server_version = "BreezeBluePanel/1.0"

    def log_message(self, fmt, *args):
        pass

    def _body(self):
        n = int(self.headers.get("content-length") or 0)
        if not n:
            return {}
        try:
            return json.loads(self.rfile.read(n).decode("utf-8"))
        except Exception:
            return {}

    def do_GET(self):
        u = urlparse(self.path)
        if u.path in ("/", "/index.html"):
            p = os.path.join(WEB, "index.html")
            if not os.path.exists(p):
                return write_json(self, {"error": "web/index.html missing"}, 500)
            body = io.open(p, "rb").read()
            self.send_response(200)
            self.send_header("content-type", "text/html; charset=utf-8")
            self.send_header("content-length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if u.path == "/api/state":
            accs = read_accounts()
            credit_refresh_all(background=True)
            total, claimable = 0.0, 0
            with CRED_LOCK:
                for a in accs:
                    c = CREDITS.get(a.get("email")) or {}
                    a["credits"] = c.get("balance")
                    a["credits_ok"] = c.get("ok")
                    a["daily_available"] = c.get("daily_available")
                    a["checked_at"] = c.get("at")
                    if isinstance(c.get("balance"), (int, float)):
                        total += c["balance"]
                    if c.get("daily_available"):
                        claimable += 1
            return write_json(self, {"job": JOB, "accounts": accs,
                                     "stats": {"accounts": len(accs),
                                               "dead_boxes": len(core.lines(core.DEAD)),
                                               "used": len(core.lines(core.USED)),
                                               "credits_total": round(total, 1),
                                               "claimable": claimable,
                                               "credits_pending": sum(1 for a in accs if a.get("credits") is None)}})
        if u.path == "/api/logs":
            q = parse_qs(u.query)
            after = int((q.get("after") or ["0"])[0])
            with LOG_LOCK:
                lines = [x for x in LOGS if x["seq"] > after]
                seq = LOG_SEQ[0]
            return write_json(self, {"seq": seq, "lines": lines})
        if u.path == "/api/inventory":
            q = parse_qs(u.query)
            limit = int((q.get("limit") or ["200"])[0])
            check = int((q.get("check") or ["0"])[0])
            return write_json(self, {"rows": inventory(limit, check)})
        if u.path == "/api/export":
            q = parse_qs(u.query)
            fmt = (q.get("fmt") or ["csv"])[0]
            rows = read_accounts()
            if fmt == "json":
                return write_json(self, rows)
            buf = io.StringIO()
            w = csv.writer(buf)
            w.writerow(["email", "password", "public_id", "own_code", "referral", "created_at"])
            for r in rows:
                w.writerow([r.get("email"), r.get("password"), r.get("public_id"),
                            r.get("own_code"), r.get("referral"), r.get("at")])
            body = buf.getvalue().encode("utf-8-sig")
            self.send_response(200)
            self.send_header("content-type", "text/csv; charset=utf-8")
            self.send_header("content-disposition", 'attachment; filename="breezeblue_accounts.csv"')
            self.send_header("content-length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        return write_json(self, {"error": "not found"}, 404)

    def do_POST(self):
        u = urlparse(self.path)
        body = self._body()
        if u.path == "/api/start":
            if JOB.get("running"):
                return write_json(self, {"ok": False, "error": "job already running"}, 409)
            threading.Thread(target=job_thread, args=(body,), daemon=True).start()
            return write_json(self, {"ok": True})
        if u.path == "/api/stop":
            STOP.set()
            log("[job] stop requested")
            return write_json(self, {"ok": True})
        if u.path == "/api/debug/credits":
            with CRED_LOCK:
                return write_json(self, CREDITS)
        if u.path == "/api/accounts/refresh":
            n = credit_refresh_all(max_age=0, background=False)
            return write_json(self, {"ok": True, "refreshed": n})
        if u.path == "/api/account/check":
            info = credit_refresh(body.get("email")) or {}
            out = account_check(body.get("email"))
            out.update({k: info.get(k) for k in ("balance", "daily_available")})
            return write_json(self, out)
        if u.path == "/api/account/claim":
            return write_json(self, account_claim(body.get("email")))
        if u.path == "/api/account/relogin":
            return write_json(self, account_relogin(body.get("email")))
        return write_json(self, {"error": "not found"}, 404)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8799)
    args = ap.parse_args()
    os.makedirs(DATA, exist_ok=True)
    log("[panel] listening on http://%s:%d" % (args.host, args.port))
    print("BreezeBlue panel -> http://%s:%d" % (args.host, args.port), flush=True)
    ThreadingHTTPServer((args.host, args.port), Handler).serve_forever()


if __name__ == "__main__":
    main()