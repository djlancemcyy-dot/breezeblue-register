# CloakBrowser 说明（要不要注册 / 要不要 key）

## 一句话
**免费 tier 不需要注册、不需要 license key、不需要登录**，装完直接用。
只有「要同时开多个会话」时才需要去领 key。

## 实测状态（我们跑号那台机器）

~~~text
tier: free  |  version: 146.0.7680.177.5
~/.cloakbrowser/config.json       exists: False
~/.cloakbrowser/license.json      exists: False
~/.cloakbrowser/credentials.json  exists: False
env CLOAKBROWSER_LICENSE_KEY:     False
~~~

即：没登录过、没有任何 license 文件、没有环境变量 key，但 Turnstile 照样能过。

## 启动时那行提示是什么意思

~~~text
Running the free binary (v146). The latest binary (v155) is free too, with 1 concurrent session.
Get your key: run  cloakbrowser login  or visit https://cloakbrowser.dev/free
~~~

这是**广告**，不是使用前提。忽略即可。它想说的是：免费版只允许 1 个并发会话，想要更多并发去领 key。

## 免费 vs 领 key

| | 免费（不注册） | 领 key 后 |
|---|---|---|
| 过 Turnstile | ✅ | ✅ |
| 每次启动随机指纹（--fingerprint=<seed>） | ✅ | ✅ |
| 并发会话数 | 1 | 免费 key 也是 1；付费套餐才更多 |
| 是否需要注册/登录 | 不需要 | 需要 cloakbrowser login |

结论：

- **串行跑号（一次一个）→ 不用注册**，单号约 70–90 秒。
- **要并行 3–5 个 → 才需要 key**，否则第二个会话会抛 CloakBrowserLicenseError。

## 比 key 更容易踩的两件事

1. **时区必须跟出口国家一致**：日本出口写 timezone="Asia/Tokyo"、locale="ja-JP"，否则 Turnstile 照样挂。
2. **一个号一个 profile 目录**：复用 profile = 所有号共用一台"设备"，会触发设备级频控。

## 最小可用示例

~~~python
import cloakbrowser

ctx = await cloakbrowser.launch_persistent_context_async(
    "data/profiles/<email>",
    headless=True,
    stealth_args=True,       # ★ 核心：随机 --fingerprint=<seed>
    humanize=True,
    locale="ja-JP",
    timezone="Asia/Tokyo",   # 与出口国家一致
    proxy="http://127.0.0.1:7891",
)
page = ctx.pages[0] if ctx.pages else await ctx.new_page()
~~~
