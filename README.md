# BreezeBlue（breezeblue.ai）注册协议分析 + 无头注册工具

> 纯 HTTP 走完整个注册链路；只有服务端强制 Cloudflare Turnstile 时，才用一次无头浏览器铸 token。
> 本文记录的是通过抓包 + 前端 bundle 逆向得出的接口细节。

## 用法一览

```bash
pip install -r requirements.txt

export MAILNEST_KEY=sk_xxx              # 邮箱服务 API Key
export RESI_PROXY=http://127.0.0.1:7891 # 住宅出口（可选但强烈建议）
export INVITE_CODE=XXXXXX               # 要绑定的邀请码（你自己的）

python proto_register.py --email xxx@outlook.com     # 协议版（推荐）
python browser_register.py --email xxx@outlook.com   # UI 版（兜底，真 Chrome 指纹）
```

## 一、协议链路

```
POST /api/auth/email/continue  {email, password, referral_code}
  ├─ 403 AUTH_SIGNUP_VERIFICATION_REQUIRED
  │    meta = {email, site_key, challenge_context}
  │    ↓ Turnstile(sitekey, action="signup", cData=challenge_context) -> token
  │    POST /api/auth/signup-verification {email, token} -> {receipt}
  │    ↓ 重发 continue，带 signup_verification=receipt
  └─ 200 {status:"verification_required", verification_email_sent:true}
        ↓ 邮件里的 dispatch 链接：/email-action/dispatch?mode=verifyEmail&oobCode=...&continueUrl=...?flow=...
POST /api/auth/email/verify {flow_token, oob_code}
  -> 200 {status:"authenticated", user:{...}} + Set-Cookie: breeze_blue_session
GET  /api/me -> 200
```

要点：

1. **是密码 + 邮件魔法链接**，不是验证码。`continue` 通过后页面没有 OTP 输入框。
2. **首提交通常被 Turnstile 拦一次**（403 带 `site_key` / `challenge_context`）。必须用 `/api/auth/signup-verification` 换 `receipt`，再把 `receipt` 回填到 `continue` 的 `signup_verification`。
3. 风控放行时 `continue` 直接 200 并立即发信，连 Turnstile 都不用——所以纯 HTTP 是可行的，浏览器只是兜底。
4. `/api/auth/email/verify` 的两个参数分别来自 dispatch 链接的 `continueUrl?flow=` 与 `oobCode=`（oobCode 一次性，用过即废）。
5. 邀请码在 `continue` 的 `referral_code` 字段里传，注册成功后 `/api/me` 的 `invited_by_referral_code` 会回显。

## 二、其它相关端点

| 端点 | 用途 |
|---|---|
| `POST /api/auth/email/password-reset/{request,inspect,confirm}` | 找回密码 |
| `POST /api/auth/verification/resend` | 重发验证邮件（有 60s 节流） |
| `POST /api/auth/firebase` | Firebase idToken 登录 |
| `GET  /api/me` / `GET /api/credits/summary` / `GET /api/billing/entitlements` | 账号态 / 额度 / 套餐 |
| `POST /api/credits/daily-claim` | 每日领取额度 |

## 三、环境与出口

- **Turnstile 对 IP 很敏感**：国内直连经常一直挂着不解（invisible 模式），换到干净的境外出口（住宅尤佳）几秒自动过。
- 出口建议用**住宅/移动 IP**，数据中心 IP 被拦概率明显更高。
- 本工具默认用 `curl_cffi`（`impersonate=chrome`）复刻浏览器 TLS/JA3，避免纯 `requests` 被指纹识别。

## 四、邮箱

用 [MailNest](https://mailnest.top) 的**独占箱**（买断、永久可收信，`GET /api/v1/email/exclusive` 取库存、`POST /api/v1/email/receive` 收信）。
邀请码：`UWSBMKO3`（走这个链接/码注册有折扣）。

## 五、住宅 IP + 一跳

住宅 IP 用 [ipwo.net](https://www.ipwo.net/?code=Y4UNUPXUD)（邀请码 `Y4UNUPXUD`）。

它的号是**来源 IP 白名单**模式：直连会 `HTTP 403` / `SOCKS5 97`。正确姿势是**先出一跳已加白的节点，再从那一跳去连 ipwo 的 SOCKS5 口子**，出口才是真正的住宅 IP：

```yaml
# mihomo 片段：local -> hop（已加白）-> ipwo socks5 -> 目标
proxies:
  - name: hop
    type: trojan            # 或 vless / ss / socks5，按你那一跳的协议填
    server: HOP_SERVER
    port: 443
    password: HOP_PASSWORD
    sni: HOP_SERVER
    tls: true
    udp: true

  - name: ipwo-resi
    type: socks5            # ★ 必须 socks5；ipwo 的 http 口只握手不通车
    server: IPWO_HOST       # get_proxy_ip?protocol=socks5 返回的 ip
    port: IPWO_PORT
    dialer-proxy: hop       # ★ 关键：先出 hop，再进 ipwo

proxy-groups:
  - name: PROXY
    type: select
    proxies: [ipwo-resi]
rules:
  - MATCH,ipwo-resi
```

取号：

```
GET https://www.ipwo.net/api/proxy/get_proxy_ip?num=1&regions=JP&protocol=socks5&return_type=txt&lb=1&sb=
```

注意：

- 端口每次取号都会变（端口即会话），**取到就用，别缓存**。
- 会话寿命通常只有几分钟，长任务要"用完即弃 + 重新取号"，或起多口自动切换。
- 业务脚本里建议直接用本地 mixed 口（如 `http://127.0.0.1:7891`），把取号/换号交给代理进程。

## 六、文件

| 文件 | 说明 |
|---|---|
| `proto_register.py` | 协议版：HTTP 全链路 + （必要时）无头铸 token |
| `browser_register.py` | UI 版：CloakBrowser 指纹浏览器点完整流程（协议被拦时兜底）|
| `requirements.txt` | 依赖 |

## 七、免责

仅供个人学习与协议研究使用。请自行确认目标站点的服务条款与所在地法律，不要用于批量滥用或商业转售。
