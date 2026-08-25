# 视频好帮手

多平台视频批量下载工具。由两部分组成：

- **桌面客户端** `man.py` — PyQt5 GUI + 下载引擎，内置本地 HTTP 桥接服务
- **浏览器扩展** `视频好帮手_浏览器扩展/` — Chrome Manifest V3 扩展，负责嗅探媒体、捕获 Cookie、一键把链接推送给客户端

两端通过 `127.0.0.1` 上的带 Token 认证的 HTTP 协议通信，Cookie 以 Netscape 格式按域名落盘后交给 yt-dlp 使用。

---

## 目录

- [功能特性](#功能特性)
- [支持平台](#支持平台)
- [环境要求](#环境要求)
- [快速开始](#快速开始)
- [项目结构](#项目结构)
- [核心架构](#核心架构)
- [桥接协议](#桥接协议)
- [Cookie 管理](#cookie-管理)
- [指纹去重（DNA）](#指纹去重dna)
- [TikTok 下载策略](#tiktok-下载策略)
- [配置文件](#配置文件)
- [日志与脱敏](#日志与脱敏)
- [打包部署](#打包部署)
- [故障排除](#故障排除)
- [技术栈](#技术栈)

---

## 功能特性

**客户端**

- **批量队列下载** — 链接列表支持粘贴/拖放/扩展推送，逐个串行下载，支持暂停与失败重试
- **指纹去重** — 扫描输出目录已有 MP4 建立内存指纹库，重复视频自动跳过（见 [指纹去重](#指纹去重dna)）
- **TikTok Native Extractor** — 基于 `curl_cffi` 的 Chrome TLS 指纹模拟直连提取，绕过 JS Challenge；失败自动回退 yt-dlp
- **NVENC 硬件编码** — 启动时探测 `h264_nvenc`，可选 H.264 (NVIDIA NVENC) / H.264 (libx264)
- **环境自检** — 启动时检查 Python / FFmpeg / NVENC / Node.js / yt-dlp 版本 / Cookie / 扩展配对状态
- **反爬绕过** — 下载前预请求模拟用户点击、附带完整浏览器请求头、按域名注入 Referer
- **日志脱敏** — Cookie 值、Token、Session、Authorization 永不落盘（见 [日志与脱敏](#日志与脱敏)）

**扩展（v1.6.9，MV3）**

- **自动配对** — 每分钟轮询 `47720-47729` 端口发现客户端并取回 Token，无需手动输入
- **Cookie 自动同步** — 标签页切换/更新时按平台捕获 Cookie 推送给客户端，2 分钟一次定时刷新
- **媒体嗅探** — `webRequest` 监听 26 种 MIME + 24 种扩展名 + 平台 CDN 主机，自动过滤埋点/像素请求
- **HLS 分组** — 同一 m3u8 的分片自动归组展示
- **页面主世界注入** — 猴补丁 `fetch` / `XMLHttpRequest` 抓取 `playAddr` / `videoId` 等内部响应
- **快捷键** — `Alt+O` 发送当前页面
- **右键菜单** — 下载、复制地址、查看视频标题、TikTok 搜索
- **协议兜底** — 桥接不可用时通过 `视频好帮手://` 自定义协议唤起客户端
- **10 种语言** — en / zh_CN / zh_TW / ja / ru / fr / es / it / pt / el

## 支持平台

**客户端指纹识别 + 下载**（`extract_video_id_from_url()`，man.py:960）

TikTok、YouTube（watch / shorts / embed / live / youtu.be）、Facebook（fb.watch）、Instagram（p / reel / tv）、Twitter/X、Vimeo、Reddit、Pinterest、Twitch（videos / clip）、Dailymotion（dai.ly）、Snapchat（spotlight / story）。

未识别平台归类为 `other`，取 URL 末段有效路径片段作为标记，仍参与去重。实际下载能力由 yt-dlp 决定，不限于上表。

**扩展内容脚本注入**（12 个站点）

TikTok、YouTube、YouTube-nocookie、Instagram、X、Twitter、Bilibili、Vimeo、Twitch、Reddit、Pinterest、SoundCloud。

**扩展 host_permissions** 额外覆盖 Hotmart、Bluesky、Telegram、Udemy、SoundCloud 及各平台 CDN 域（googlevideo / cdninstagram / fbcdn / tiktokcdn / twimg / redditmedia / jtvnw / pinimg / vimeocdn / bilivideo / sndcdn 等），Pinterest 覆盖 14 个国别域名。

## 环境要求

| 依赖 | 说明 | 必需性 |
|------|------|--------|
| Python 3.8+ | 源码运行 | 源码运行必需 |
| PyQt5 | GUI 框架 | 必需 |
| `curl_cffi` | TikTok Native Extractor（TLS 指纹模拟） | 强烈推荐 |
| `yt-dlp.exe` | 下载引擎，位于根目录或 `bin/` | 必需 |
| `ffmpeg.exe` / `ffprobe.exe` | 合并/转码/探测，位于根目录或 `ffmpeg/bin/` | 必需 |
| `node.exe` | yt-dlp 回退路径的 JS 解签运行时 | TikTok 回退时必需 |
| Chrome / Edge (Chromium) | 加载 MV3 扩展 | 联动功能必需 |

**JS 运行时探测顺序**：`nodejs/node.exe`（便携版）→ 系统 `PATH` 中的 `node` → `C:\Program Files\nodejs\node.exe` → `C:\Program Files (x86)\nodejs\node.exe`。找到后以 `--js-runtimes "node:<path>"` 传给 yt-dlp。

**可执行文件查找顺序**（`resolve_path()`，man.py:70）：根目录优先，其次 `bin/` 或 `ffmpeg/bin/`。

代理由系统 / v2rayN / TUN 负责，程序不绑定任何代理端口（环境自检显示 `Proxy = SYSTEM`）。

## 快速开始

### 1. 安装 Python 依赖

```powershell
pip install PyQt5 curl_cffi
```

### 2. 就位外部工具

```
下载/
├── bin/yt-dlp.exe
├── ffmpeg/bin/{ffmpeg.exe, ffprobe.exe}
└── nodejs/node.exe        # 可选，TikTok 回退路径需要
```

### 3. 启动客户端

```powershell
python man.py
```

无命令行参数。首次运行自动创建 `cookies/`、`downloads/`、`_download_temp/`、`logs/`，并在 `47720-47729` 范围内绑定第一个可用端口启动桥接服务。

### 4. 安装浏览器扩展

1. 打开 `chrome://extensions/`
2. 开启「开发者模式」
3. 「加载已解压的扩展程序」→ 选择 `视频好帮手_浏览器扩展` 目录

### 5. 配对

扩展每分钟自动扫描一次 `47720-47729` 端口的 `/v1/pair`。若需立即配对，在客户端点击「配对」按钮打开配对窗口，扩展下一次轮询即可取回 Token。也可用「复制 Token」按钮手动粘贴到扩展选项页。

配对成功后：

- 打开任意支持的视频网站，扩展自动捕获并同步 Cookie
- 右键「视频好帮手_下载」或按 `Alt+O` 把当前页面推入客户端队列
- 客户端自动排队下载，重复视频按指纹跳过

## 项目结构

```
下载/
├── man.py                       # 主程序（7343 行，唯一入口）
├── yuanshiman.py                # 旧版备份（4858 行，不参与运行）
├── icon.png                     # 应用图标
├── README.md
├── 打包部署说明.md
├── .gitignore                   # 白名单模式，排除所有 cookie / UA 文件
│
├── config.json                  # 运行时配置（自动生成，含 bridge token）
├── config.ini                   # 旧版 QSettings 残留，现行代码不读取
├── link.txt                     # 链接文件
├── browser_ua.txt               # 扩展同步的 User-Agent（gitignore）
│
├── bin/yt-dlp.exe
├── ffmpeg/bin/{ffmpeg,ffplay,ffprobe}.exe
│
├── cookies/                     # 按域名分类的 Netscape cookie（gitignore）
│   ├── _meta.json               # 域名注册表
│   ├── _all_cookies.txt         # 合并总表
│   └── <domain>/
│       ├── _default.txt         # 该域名的 cookie
│       └── _referer.txt         # 扩展同步的页面 Referer（可选）
│
├── downloads/                   # 默认输出目录
├── _download_temp/              # 下载临时文件与会话 cookie
├── logs/tiktok_debug.log        # 脱敏任务日志（10MB × 2 滚动）
│
├── test_tiktok_native.py        # Native Extractor 测试
├── test_native_diagnose.py      # 诊断脚本
├── _debug_tiktok.py             # 调试脚本
│
└── 视频好帮手_浏览器扩展/
    ├── manifest.json            # MV3, v1.6.9
    ├── src/
    │   ├── background.js        # Service Worker 总控（1206 行）
    │   ├── content-script.js    # 页面注入与媒体上报（612 行）
    │   ├── bridge-client.js     # 桥接协议客户端（387 行）
    │   ├── media-sniffer.js     # webRequest 媒体嗅探（376 行）
    │   ├── detect.js            # URL → {platform, contentType, supported}
    │   ├── cookie-capture.js    # 手动保存 Cookie 流程
    │   ├── cookies.js
    │   ├── cookies-domains.json # 平台 → cookie 域名映射
    │   ├── send-via-scheme.js   # 视频好帮手:// 协议兜底
    │   ├── blocked-hosts.js     # 埋点/统计域名黑名单
    │   ├── hls-grouping.js      # m3u8 分片归组
    │   ├── sniffer-{storage,toggle,filters}.js
    │   ├── action-{click,feedback,title}.js
    │   ├── cookie-summary.js / open-app-toggle.js
    │   └── context-menu.js      # 4 项右键菜单
    ├── popup/{popup.html,popup.js,popup.css}
    ├── pages/{options,error}.{html,js,css}
    ├── _locales/{en,zh_CN,zh_TW,ja,ru,fr,es,it,pt,el}/messages.json
    ├── icons/{active,inactive}-{16,24,32,48,128}.png
    └── docs/*.png               # 引导截图
```

## 核心架构

### 客户端类（man.py）

| 行号 | 类 | 职责 |
|------|-----|------|
| 528 | `OmniGetBridgeHandler` | HTTP 路由与 Bearer Token 校验 |
| 698 | `OmniGetBridge` | 桥接服务生命周期、配对窗口、Referer / Headers 落盘 |
| 1100 | `ConfigManager` | `config.json` 读写与 deep-merge |
| 1247 | `CookieManager` | Netscape cookie 分域写入、合并、超限清理、`_meta.json` 注册 |
| 1592 | `OutputFolderLineEdit` | 支持拖放文件夹的输出路径输入框 |
| 1777 | `DownloadThread` | QThread 下载线程，构造 yt-dlp 命令 / 执行 TikTok Native |
| 4277 | `LinkListWidget` | 待下载链接队列列表 |
| 4751 | `MainWindow` | 主窗口，进度、日志、统计、扫描、配对 |

> 早期版本的 `ChromeCDP`（Chrome DevTools Protocol 取 Cookie）已移除，环境自检中显示为 `Chrome CDP = DISABLED`。

### 扩展 Service Worker（background.js）

事件注册：`onInstalled`(123) / `contextMenus.onClicked`(151) / `onStartup`(583) / `tabs.onActivated`(617) / `tabs.onUpdated`(621) / `windows.onFocusChanged`(633) / `runtime.onMessage`(639)。

核心函数：`handleSendToApp`(819) 发送链接、`updateBadge`(807) 角标、`refreshTabAction`(913) 图标状态、`capturePlatformCookies`(1035)、`scanOpenTabsForCookies`(1076)、`scanAllPlatformsForCookies`(1105)。

两个 `alarms`：

- `视频好帮手-autopair` — 1 分钟一次，未配对时轮询 `/v1/pair`
- `视频好帮手-cookie-refresh` — 2 分钟一次，刷新平台 Cookie

Cookie 自动捕获防抖：`1500 ms` 防抖 + 同平台 `60 s` 最小间隔。

`onMessage` 处理 6 类消息：`getDetectedMedia`、`toggleSniffer`、`sendTo视频好帮手`、`injectNetworkInterceptor`、`scanYtInternalState`、`readPageTitle`。后三者通过 `chrome.scripting.executeScript({ world: "MAIN" })` 注入页面主世界，命中 `videoId` / `playAddr` 的响应体前 50000 字符经 `__og-net` CustomEvent 回传内容脚本。

## 桥接协议

服务端 `OmniGetBridge`，绑定 `127.0.0.1`，端口在 `47720-47729` 中取第一个可用。协议版本 `1`。

除 `/v1/health` 与 `/v1/pair` 外，所有接口要求 `Authorization: Bearer <token>`；缺失返回 `401`，不匹配返回 `403`。

### `GET /v1/health`

```json
{ "ok": true, "version": 1 }
```

用于端口发现。

### `GET /v1/pair`

配对窗口打开时返回 Token 并**立即关闭窗口**（一次性）：

```json
{ "ok": true, "token": "xxx" }
```

窗口关闭时返回 `404 { "ok": false, "message": "Pairing window closed" }`。

### `POST /v1/enqueue`

推送链接。可同时携带 Cookie / UA / Referer / 附加 Header，一次请求完成全部同步。

```json
{
  "url": "https://www.tiktok.com/@user/video/7486922641006202154",
  "protocolVersion": 1,
  "cookies": [{ "domain": ".tiktok.com", "name": "...", "value": "...", "path": "/", "secure": true, "httpOnly": false, "expires": 0, "hostOnly": false, "sameSite": "no_restriction" }],
  "userAgent": "Mozilla/5.0 ...",
  "referer": "https://www.tiktok.com/@user",
  "headers": { "Accept-Language": "zh-CN,zh;q=0.9" }
}
```

服务端处理顺序：保存 Referer → 保存附加 Headers（自动排除 Cookie / UA / Referer，由程序单独管理）→ Cookie 回调（无 Cookie 但有 UA 时只更新 UA）→ 入队并触发自动下载。

链接不写文件、不做平台过滤，仅要求 `http://` 或 `https://` 前缀。

响应：`{ "ok": true }`

### `POST /v1/cookies`

自动捕获模式下单独推送 Cookie。`sourceUrl` 会作为 Referer 保存（不伪造，无则不写）。

```json
{ "cookies": [...], "sourceUrl": "https://www.tiktok.com/...", "userAgent": "Mozilla/5.0 ..." }
```

响应：`{ "ok": true, "message": "Cookies received" }`

### `POST /v1/ua`

仅更新 User-Agent。

```json
{ "userAgent": "Mozilla/5.0 ..." }
```

### 协议兜底：`视频好帮手://`

桥接不可用时，扩展将 URL 去掉 `http(s)://` 前缀后拼成 `视频好帮手://<url>`（magnet / p2p 链接拼为 `视频好帮手:magnet:...`），在后台标签页打开唤起客户端，1500 ms 后关闭该标签页。

## Cookie 管理

- **分域存储** — `cookies/<根域名>/_default.txt`，每个平台独立，互不覆盖
- **增量合并** — 新 Cookie 与已有条目按 `(domain, name, path)` 合并
- **合并总表** — 同步生成 `cookies/_all_cookies.txt`
- **域名注册表** — `cookies/_meta.json` 记录别名、来源页面、更新时间
- **Referer 落盘** — `cookies/<域名>/_referer.txt`，存在时作为 `--referer` 传给 yt-dlp，不存在则不伪造
- **超限清理** — 单文件超过 8000 条时按域名保留最新 500 条
- **Session Cookie** — `expires = 0` 的条目自动延长 24 小时
- **平台域名映射**（man.py:1218）— 如 `youtube → [.youtube.com, .google.com]`、`instagram → [.instagram.com, .cdninstagram.com, .fbcdn.net]`

扩展侧优先 `fetch(src/cookies-domains.json)` 取映射，失败回退内置 `DEFAULT_PLATFORM_COOKIE_DOMAINS`。手动保存流程用当前标签页 hostname 的二级根域执行 `chrome.cookies.getAll({ domain: root })`。

> `cookies/`、`browser_ua.txt`、`*.cookie(s)`、`cookies.txt`、`_all_cookies.txt` 已全部写入 `.gitignore`，不会入库。

## 指纹去重（DNA）

指纹库**仅存在于内存**，不持久化到文件。

**构建**（`_scan_fingerprints()`，man.py:6914）— 递归 `rglob("*.mp4")` 扫描输出目录，从文件名解析指纹：

- 主规则：从后往前找 `<平台>_` 标识（平台名前必须是 `_` 或位于开头，避免 `mytiktok_` 误匹配），取从平台名到末尾的部分。前缀不影响判断，`202608230032_tiktok_7486922641006202154` 与 `hhh_jjjjj____tiktok_7486922641006202154` 均得到 `tiktok_7486922641006202154`
- 旧命名兼容：`video_<视频ID>.mp4` 从父目录（或向上逐级）推断平台名
- 两条规则均失败时记入「无法识别指纹」列表并在日志中列出前 10 条

**查重**（`_check_fingerprint()`）— 入队链接经 `extract_video_id_from_url()` 得到 `<platform>_<video_id>`，命中内存库则跳过。暂停状态下不拦截，链接先入列表，恢复调度时再检查并移除重复项。

**写入**（`_add_fingerprint()`）— 下载成功后直接加入内存集合，不重新扫描磁盘。

**重建** — 点击「扫描」按钮重新扫描输出目录。已识别平台集合：`youtube / tiktok / instagram / facebook / twitter / vimeo / reddit / pinterest / twitch / dailymotion / snapchat / other`。

> 根目录的 `video_dna.txt` 是历史遗留文件，当前代码不读写。

## TikTok 下载策略

TikTok 走双路径，Native 优先：

**路径 A：Native Extractor**（`curl_cffi`，man.py:2638）

1. `_tiktok_resolve_short_link()` 展开 `vm.tiktok.com` / `vt.tiktok.com` 短链
2. `_tiktok_extract_post_id()` 从 URL 提取 post id
3. `_tiktok_parse_cookies()` 读取分域 cookie 文件
4. 用 Chrome TLS 指纹发起请求，`_tiktok_is_captcha_page()` 检测验证码页，`_tiktok_is_valid_play_addr()` 校验直链有效性
5. `_tiktok_native_download()` 直连下载

**路径 B：回退 yt-dlp**（`build_ytdlp_args()`，man.py:2180）

- `--cookies cookies/tiktok.com/_default.txt`
- `--js-runtimes node:<path>` 解签 nsig / sig challenge
- 附加 `Sec-Fetch-*`、`Accept`、`Referer` 等完整浏览器请求头
- 预请求模拟用户点击后随机等待 2.5–3.0 秒再启动

未安装 `curl_cffi` 时直接走路径 B（不影响其他平台）。

## 配置文件

`config.json` 由 `ConfigManager` 自动生成与维护：

```json
{
  "window": { "x": null, "y": null, "width": 900, "height": 650 },
  "output_dir": "<项目目录>/downloads",
  "video_size": "810x1080",
  "video_codec": "H.264 (libx264)",
  "links": [],
  "bridge_token": "",
  "bridge_port": 5999
}
```

注意事项：

- `bridge_token` 为 `secrets.token_urlsafe(24)` 随机生成，**属于敏感凭据**，`.gitignore` 已排除 `config.json`
- `DEFAULT_CONFIG` 中的 `bridge_port: 5999` 是过时默认值，实际绑定始终走 `BRIDGE_PORT_RANGE = range(47720, 47730)`
- 历史版本曾同时写入大小写重复键 `bridge_*` 与 `Bridge_*`，属冗余，可安全删除大写形式后重启
- `config.ini`（`[window] geometry` / `[video] size_index` / `[output] dir`）是旧 QSettings 残留，现行代码不读取
- 默认 UA：`Chrome/151.0.0.0`（`DEFAULT_UA`，man.py:471），扩展同步后以 `browser_ua.txt` 为准

## 日志与脱敏

任务日志写入 `logs/tiktok_debug.log`，单文件 10 MB 后滚动，保留 2 份备份。与 GUI 日志完全分离，便于上传诊断。

脱敏规则：

- **URL 查询参数** — 参数名含 `token / session / sig / sign / key / auth / secret / pass / code` 时值替换为 `REDACTED`
- **关键 Cookie** — `sessionid / msToken / sid_tt / ttwid / tt_chain_token / passport_csrf_token` 只记录 present / absent，不记录真实值
- **命令行** — `--cookies / --username / --password / --client-certificate` 的值替换为 `REDACTED`
- **请求头** — `Cookie: / Authorization: / X-MS-Token / Session` 开头的 Header 值替换为 `REDACTED`

## 打包部署

```powershell
pip install pyinstaller
pyinstaller --onefile --windowed --icon=icon.png --name="视频好帮手" man.py
```

部署清单：

```
视频好帮手/
├── 视频好帮手.exe
├── bin/yt-dlp.exe
├── ffmpeg/bin/{ffmpeg.exe, ffprobe.exe}
└── nodejs/node.exe        # TikTok 回退路径需要
```

`cookies/`、`downloads/`、`config.json` 首次运行自动创建。扩展需在目标机器上单独加载。

详见 [打包部署说明.md](打包部署说明.md)。

## 故障排除

| 现象 | 原因 | 解决 |
|------|------|------|
| TikTok 失败率高 | `curl_cffi` 未安装，Native Extractor 未启用 | `pip install curl_cffi` |
| TikTok 回退也失败 | 未检测到 Node.js | 放置 `nodejs/node.exe` 或安装 Node.js |
| 环境自检 `Cookie: MISSING` | 扩展未配对或未同步 | 打开视频网站等待自动捕获，或在 popup 点「保存网站 Cookie」 |
| 环境自检 `Extension: 等待扩展同步` | 尚未收到任何 Cookie / UA | 检查扩展是否已配对（popup 状态） |
| `yt-dlp: MISSING` | 可执行文件缺失 | 放置到根目录或 `bin/` |
| 扩展始终配对失败 | 客户端未启动，或 47720-47729 被占用 | 启动客户端；检查端口占用与防火墙 |
| 重复下载已有视频 | 指纹库未扫描，或文件名不含平台标识 | 点「扫描」重建；检查日志中「无法识别指纹」列表 |
| 遇到 TikTok 验证码 | 触发风控 | 在浏览器完成验证，重新同步 Cookie 后重试 |
| 下载速度异常 | 代理未生效 | 程序不绑定代理端口，由系统 / v2rayN / TUN 层负责 |

## 技术栈

- **GUI** — PyQt5（Python 3.8+）
- **下载引擎** — yt-dlp
- **TLS 指纹模拟** — curl_cffi（Chrome impersonate）
- **视频处理** — FFmpeg / FFprobe，可选 NVIDIA NVENC 硬件编码
- **JS 解签** — Node.js（yt-dlp `--js-runtimes`）
- **浏览器扩展** — Chrome Manifest V3，原生 ESM，无构建步骤
- **通信** — 本地 HTTP Bridge（`127.0.0.1:47720-47729`，Bearer Token）
- **Cookie 格式** — Netscape Cookie File
