# 提示词 v2（含指纹浏览器 CloakBrowser）· 直接整段发给同事的 AI

---

我要给 breezeblue.ai 批量注册账号。这套流程已验证跑通，严格按下面顺序执行，不要自己发明步骤。

## 0. 先搞清一件事
之前卡在 Cloudflare Turnstile「验证失败 / 故障排除」，排查结论：**缺指纹浏览器**。
普通 Playwright 启动的 Chrome、云端浏览器、requests/curl，TLS 或 JS 指纹都会被 CF 识别。
必须用 **CloakBrowser**（源码级改指纹的 Chromium），同一出口能 3 秒自动过 Turnstile。

## 1. 装依赖
~~~bash
pip install cloakbrowser>=0.6.4 curl_cffi>=0.17.0 requests
# 首次运行会自动下载自带 Chromium 到 C:\Users\<user>\.cloakbrowser\
# 免费版只允许 1 个并发会话 —— 必须串行跑，一次一个号
# ★ 不需要注册、不需要 key；启动时那行 "Get your key ..." 是广告，忽略即可（只有要多并发才领 key）
~~~

## 2. 邮箱（MailNest 独占箱）
- 站点 https://mailnest.top ，邀请码 UWSBMKO3
- 取库存 GET /api/v1/email/exclusive?page=1&page_size=100 （status = having / holding 才可用）
- 收信 POST /api/v1/email/receive {"email":"..."}；返回 D0004 / D0006 是死箱，直接换
- 请求头带 Authorization: Bearer <sk_...>；**走直连，不要挂代理**

## 3. 出口（ipwo 日本住宅，必须走一跳）
- 站点 https://www.ipwo.net/?code=Y4UNUPXUD ，邀请码 Y4UNUPXUD
- 取号 GET https://www.ipwo.net/api/proxy/get_proxy_ip?num=1&regions=JP&protocol=socks5&return_type=txt&lb=1&sb=
- **必须 protocol=socks5**；protocol=http 的口子只握手不通车
- ipwo 是来源 IP 白名单：mihomo 里给这条 socks5 配 dialer-proxy 指向一个已加白的一跳，规则 MATCH 到它
- 端口每次取号都变（端口=会话），取到立刻用，别缓存

## 4. 开页面之前先体检出口（决定 80% 成败）
~~~bash
python tools/exit_check.py --proxy http://127.0.0.1:7891
~~~
- **PASS**（KDDI / NTT / DOCOMO / SoftBank / Sony / OPTAGE 这类 ISP，未标代理）→ 继续
- **RISKY**（Datacamp / M247 / OVH / CDN77 等机房）→ 重新取号
- **REJECT**（被标 proxy / vpn / tor / datacenter，或根本不通）→ 重新取号

判据不是"是不是住宅"，而是：**ASN 是 ISP + 该 IP 没被标代理 + 指纹不是云端浏览器**。

## 5. 浏览器（关键，别省）
~~~python
import cloakbrowser

ctx = await cloakbrowser.launch_persistent_context_async(
    "data/profiles/<email>",      # 每个号一个独立 profile 目录
    headless=True,
    stealth_args=True,            # ★ 核心：每次启动随机 --fingerprint=<seed>
    humanize=True,                # 拟人化输入节奏
    locale="ja-JP",
    timezone="Asia/Tokyo",        # 必须和出口国家一致
    proxy="http://127.0.0.1:7891" # 上面体检 PASS 的出口
)
page = ctx.pages[0] if ctx.pages else await ctx.new_page()
~~~
**绝不要**：
- 用系统自带 chrome.exe 直接启动（静态指纹，所有号一个"设备"）
- 用云端浏览器 / 远程浏览器（TLS 与 JS 指纹在服务端）
- 用 requests / curl 去点 Turnstile（TLS 指纹是 Python，必挂）
- 复用旧 profile 或旧 cookie

## 6. 注册链路（纯 HTTP，只有 Turnstile 那步借浏览器）
~~~text
1) POST /api/auth/email/continue  {email, password, referral_code}
2) 若 403 AUTH_SIGNUP_VERIFICATION_REQUIRED
     取 meta.site_key / meta.challenge_context
     CloakBrowser 里 turnstile.render({sitekey, action:"signup", cData:challenge_context}) 回调拿 token
3) POST /api/auth/signup-verification  {email, token}   -> {receipt}
4) 重发 continue，body 加 signup_verification: receipt  -> 200，邮件已发
5) 从 mailnest 收信，取 /email-action/dispatch?... 链接，解析 flow 与 oobCode
6) POST /api/auth/email/verify  {flow_token, oob_code}  -> 200 authenticated + cookie breeze_blue_session
7) GET /api/me -> 200，检查 invited_by_referral_code 是否等于邀请码
~~~

## 7. 铁律（违反必挂）
- 一个出口只提交一次；失败换 IP，不要原地刷新重试
- 一个号 = 新 profile + 新出口 + 新指纹种子
- 时区 / 语言 / 系统时间必须与出口国家一致（偏差 >1 分钟也会挂）
- challenges.cloudflare.com 要能直连；代理不要做 TLS 中间人
- 本地代理/CDP 要设 NO_PROXY=127.0.0.1,localhost，否则 localhost 会 502
- 免费版 CloakBrowser 只有 1 个并发 —— 串行跑，单号约 70–90 秒

## 8. 卡住时的排查顺序
1. F12 console 看 Turnstile 错误码：300030 = 脚本/iframe 被拦；300010 / 600010 = challenge 执行失败
2. 跑 tools/exit_check.py，把输出贴给我
3. 换一个 PASS 出口 + 新 profile，再试一次
4. 仍失败就停下汇报（贴 HTTP 状态码 + stage），不要连续重试

## 9. 交付格式
成功：邮箱 / 密码 / public_id / 本号邀请码，并确认 /api/me 200
失败：stage（continue_failed / captcha_failed / no_mail / verify_failed）+ 每步状态码

参考实现（可直接读）：https://github.com/djlancemcyy-dot/breezeblue-register
- proto_register.py —— 协议版 + CloakBrowser 铸 token
- browser_register.py —— 全 UI 版（协议不通时兜底）
- tools/exit_check.py —— 出口体检器