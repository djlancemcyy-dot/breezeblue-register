# -*- coding: utf-8 -*-
"""出口体检器：注册前先看这条出口到底能不能用。

用法：
  python tools/exit_check.py --proxy http://127.0.0.1:7891
  python tools/exit_check.py --proxy-file exits.txt        # 每行一个 host:port 或 http://host:port
  python tools/exit_check.py --proxy socks5://127.0.0.1:1080 --json

判读：
  PASS    住宅/移动 ISP 出口，且未被标记为代理/机房  -> 直接用
  RISKY   机房 IP（Datacamp / M247 / OVH / CDN77 …）-> 真 Chrome 指纹下偶尔能过，不建议
  REJECT  被明确标记 proxy/vpn/tor/datacenter      -> 一定过不了，换号
"""
import argparse, json, re, sys, time

HOSTING_HINTS = ("datacamp", "m247", "ovh", "cdn77", "vultr", "digitalocean", "linode",
                 "amazon", "aws", "google", "microsoft", "azure", "cloudflare", "hetzner",
                 "contabo", "leaseweb", "oracle", "choopa", "alibaba", "tencent", "gcore",
                 "zenlayer", "hosting", "server", "colo", "data center", "datacenter")
ISP_HINTS = ("kddi", "ntt", "docomo", "softbank", "sony", "optage", "chubu", "iij", "ocn",
             "plala", "jcom", "biglobe", "so-net", "rakuten", "uq ", "au ", "comcast", "cox",
             "charter", "verizon", "at&t", "shaw", "bell", "rogers", "telstra", "singtel",
             "china mobile", "china telecom", "china unicom", "cmcc", "chinanet", "unicom")

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/142.0.0.0 Safari/537.36")


def _get(url, proxy, timeout=20):
    """优先 curl_cffi（能走 socks5），失败退回 requests。"""
    try:
        from curl_cffi import requests as cr
        p = {"http": proxy, "https": proxy} if proxy else None
        r = cr.get(url, impersonate="chrome", proxies=p, timeout=timeout)
        return r.status_code, r.text
    except Exception:
        pass
    try:
        import requests
        p = {"http": proxy, "https": proxy} if proxy else None
        r = requests.get(url, headers={"user-agent": UA}, proxies=p, timeout=timeout)
        return r.status_code, r.text
    except Exception as exc:
        return 0, str(exc)[:120]


def normalize(p):
    p = (p or "").strip()
    if p.lower() in ("direct", "none", "-"):
        return ""      # 不走代理，直连体检
    if not p:
        return ""
    if "://" not in p:
        p = "http://" + p
    return p


def check(proxy):
    proxy = normalize(proxy)
    t0 = time.time()
    code, ip = _get("https://api.ipify.org", proxy)
    ip = (ip or "").strip()
    if code != 200 or ip.count(".") != 3:
        ip = ""
    latency = int((time.time() - t0) * 1000)

    country = city = org = ""
    flags = {}
    if ip:
        _, t2 = _get("https://ipwho.is/" + ip, None, 15)
        try:
            d = json.loads(t2)
            country = d.get("country_code") or ""
            city = d.get("city") or ""
            org = ((d.get("connection") or {}).get("org")
                   or (d.get("connection") or {}).get("isp") or "")
            sec = d.get("security") or {}
            for k in ("proxy", "vpn", "tor", "hosting"):
                if k in sec:
                    flags["is_" + k] = bool(sec[k])
        except Exception:
            pass
        _, t3 = _get("https://api.ipapi.is/?q=" + ip, None, 15)
        try:
            d = json.loads(t3)
            for k in ("is_proxy", "is_vpn", "is_tor", "is_datacenter", "is_abuser"):
                if k in d:
                    flags[k] = bool(d[k])
            a = (d.get("asn") or {}).get("org") or ""
            if a and not org:
                org = a
        except Exception:
            pass

    low = (org or "").lower()
    hosting = any(h in low for h in HOSTING_HINTS)
    isp = any(h in low for h in ISP_HINTS)
    marked = any(flags.get(k) for k in ("is_proxy", "is_vpn", "is_tor", "is_abuser"))
    dc_flag = bool(flags.get("is_datacenter")) or bool(flags.get("is_hosting"))

    if not ip:
        verdict, why = "REJECT", "出口不通（拿不到出口 IP）"
    elif marked:
        verdict, why = "REJECT", "已被标记为代理/VPN/Tor/滥用"
    elif hosting or dc_flag:
        verdict, why = "RISKY", "机房 IP（CF 风险高，真 Chrome 偶过）"
    elif isp:
        verdict, why = "PASS", "住宅/移动 ISP，未标记代理"
    else:
        verdict, why = "RISKY", "ASN 无法确认为住宅 ISP"

    return {"proxy": proxy or "direct", "ip": ip, "country": country, "city": city, "org": org,
            "latency_ms": latency, "flags": flags, "verdict": verdict, "reason": why}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--proxy", default="", help='http://host:port / socks5://host:port / direct')
    ap.add_argument("--proxy-file", default="")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    targets = []
    if args.proxy:
        targets.append(args.proxy)
    if args.proxy_file:
        with open(args.proxy_file, encoding="utf-8") as f:
            targets += [x.strip() for x in f if x.strip() and not x.startswith("#")]
    if not targets:
        ap.error("给 --proxy 或 --proxy-file")

    rows = [check(t) for t in targets]
    if args.json:
        print(json.dumps(rows, ensure_ascii=False, indent=2))
    else:
        print("%-26s %-16s %-3s %-34s %-8s %s" % ("PROXY", "EXIT_IP", "CC", "ORG", "VERDICT", "NOTE"))
        for r in rows:
            print("%-26s %-16s %-3s %-34s %-8s %s" % (
                r["proxy"][:26], r["ip"] or "-", r["country"] or "-",
                (r["org"] or "-")[:34], r["verdict"], r["reason"]))
        bad = [r for r in rows if r["verdict"] != "PASS"]
        print("\n可用 %d/%d；建议：只用 PASS 的出口，一个出口只提交一次" % (len(rows) - len(bad), len(rows)))
    sys.exit(0 if any(r["verdict"] == "PASS" for r in rows) else 2)


if __name__ == "__main__":
    main()