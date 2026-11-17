# 给同事 AI 的提示词（BreezeBlue 注册，已验证跑通的经验）

把下面整段粘给他的 AI：

---

你现在帮我做 breezeblue.ai 的账号注册。这套流程已经跑通过，**严格按顺序执行，不要自己发明步骤**。

## 0. 目标
成功注册账号，并且 `GET /api/me` 返回 200、`invited_by_referral_code` 是我给的邀请码。

## 1. 环境（缺一不可）
- 邮箱：MailNest 独占箱（https://mailnest.top，邀请码 `UWSBMKO3`）
  - 取库存：`GET /api/v1/email/exclusive?page=1&page_size=100`（status 为 `having`/`holding` 才算可用）
  - 收信：`POST /api/v1/email/receive {"email":"..."}`
  - 返回 `D0004`/`D0006` 是死箱，直接换
- 出口：ipwo 动态住宅（https://www.ipwo.net/?code=Y4UNUPXUD，邀请码 `Y4UNUPXUD`），**只取日本**
- 浏览器：真 Chrome + 指纹伪装（CloakBrowser：`stealth_args=True, humanize=True`），时区 `Asia/Tokyo`、语言 `ja-JP`

## 2. 出口必须走「一跳」（ipwo 是来源 IP 白名单）
- 取号：`GET https://www.ipwo.net/api/proxy/get_proxy_ip?num=1&regions=JP&protocol=socks5&return_type=txt&lb=1&sb=`
- **必须 `protocol=socks5`；`protocol=http` 的口子只握手不通车**
- mihomo 里配 `dialer-proxy` 指向一个已加白的一跳节点，规则 MATCH 到 ipwo 这条
- 端口每次取号都会变（端口 = 会话），取到就用，别缓存

## 3. 开页面之前先体检出口（这一步决定了 80% 的成败）
`python tools/exit_check.py --proxy http://127.0.0.1:7891`
- `PASS`（住宅/移动 ISP，未标代理）→ 继续
- `RISKY`（Datacamp / M247 / OVH / CDN77 这类机房）→ 重新取号，别用
- `REJECT`（被标 proxy/vpn/datacenter）→ 重新取号
判据不是「住宅」两个字，而是：**ASN 是 ISP + 该 IP 没被标代理 + 指纹不是云端浏览器**。

## 4. 注册链路（协议版，可直接 HTTP）
1. `POST /api/auth/email/continue {email, password, referral_code}`
2. 若 403 `AUTH_SIGNUP_VERIFICATION_REQUIRED`：带 `meta.site_key`/`meta.challenge_context` 渲染 Turnstile（action=signup, cData=challenge_context）拿 token
3. `POST /api/auth/signup-verification {email, token}` → 得到 `receipt`
4. 重发 continue，带上 `signup_verification: receipt` → 200
5. 邮件里取 `/email-action/dispatch?...` 链接，解析 `flow` 与 `oobCode`
6. `POST /api/auth/email/verify {flow_token, oob_code}` → 200 `authenticated` + cookie `breeze_blue_session`
7. `GET /api/me` 验证 200

## 5. 铁律（违反就过不了 CF）
- 一个出口只提交**一次**；失败就换 IP，不要原地刷新重试
- 每次都用**新 profile**（不要复用带旧 cookie 的浏览器）
- 时区/语言/系统时间必须跟出口国家一致（偏差 >1 分钟也会挂）
- 只用真 Chrome 指纹；**云端浏览器 / 远程浏览器不可用**（TLS 与 JS 指纹在服务端，CF 一眼识别）
- `challenges.cloudflare.com` 必须能直连，代理不要做 TLS 中间人

## 6. 卡住时的排查顺序
1. 看 Turnstile 的 error code（F12 console）：300030 = 脚本/iframe 被拦；300010/600010 = challenge 执行失败
2. 跑 `tools/exit_check.py`，把输出贴给我
3. 换一个出口重试一次（新号、新 profile）
4. 仍然失败 → 停下来汇报，不要连续重试

## 7. 交付
- 成功账号：邮箱 / 密码 / public_id / 本号邀请码
- 失败记录：每一步的 HTTP 状态码 + stage（continue_failed / captcha_failed / no_mail / verify_failed）

---
