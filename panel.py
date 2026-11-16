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
INV_CACHE = {"at": 0, "rows": []}


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
    from curl_cffi import requests as cr
    s = cr.Session(impersonate=core.IMPERSONATE, timeout=30)
    for c in row.get("cookies") or []:
        s.cookies.set(c, row["cookies"][c], domain="breezeblue.ai")
    s.headers.update({"accept": "application/json", "user-agent": "Mozilla/5.0"})
    return s


def account_check(email):
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
            return write_json(self, {"job": JOB, "accounts": accs,
                                     "stats": {"accounts": len(accs),
                                               "dead_boxes": len(core.lines(core.DEAD)),
                                               "used": len(core.lines(core.USED))}})
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
        if u.path == "/api/account/check":
            return write_json(self, account_check(body.get("email")))
        if u.path == "/api/account/claim":
            return write_json(self, account_claim(body.get("email")))
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
