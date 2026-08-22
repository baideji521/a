# 视频好帮手

多平台视频批量下载工具，支持 TikTok、YouTube、Instagram 等主流视频平台。通过浏览器扩展实时同步 Cookie，配合 yt-dlp 引擎实现高质量视频下载，内置视频指纹去重系统避免重复下载。

## 功能特性

- **多平台支持** — TikTok、YouTube、Instagram、Twitter/X、Reddit、Bilibili、Vimeo、Twitch、Pinterest、SoundCloud 等
- **浏览器扩展联动** — Chrome 扩展自动捕获并同步 Cookie 到客户端，无需手动导出
- **自动队列下载** — 从扩展一键发送链接，客户端自动排队逐个下载，支持批量操作
- **视频 DNA 指纹去重** — 基于视频 ID 的指纹系统，自动跳过已下载的视频
- **预请求反爬绕过** — 下载前模拟用户点击行为，触发平台活跃检测，提高成功率
- **Cookie 智能管理** — 按域名分类存储、自动合并、超限清理，保持 Cookie 文件轻量
- **网络重试机制** — 网络波动自动重试（指数退避），提高下载稳定性
- **JS 挑战解签** — 通过 Node.js 运行时解签 TikTok 的 JavaScript challenge
- **暗色主题 GUI** — 基于 PyQt5 的现代化界面，实时进度显示

## 项目结构

```
下载/
├── man.py                          # 主程序（PyQt5 GUI + 下载引擎）
├── icon.png                        # 应用图标
├── config.ini / config.json        # 配置文件（自动生成）
├── browser_ua.txt                  # 浏览器 User-Agent 记录
├── video_dna.txt                   # 视频 DNA 指纹记录
├── downloaded_links.txt            # 已下载链接历史
│
├── bin/
│   └── yt-dlp.exe                  # yt-dlp 下载引擎
│
├── ffmpeg/
│   └── bin/
│       ├── ffmpeg.exe              # 视频合并/转码
│       └── ffprobe.exe             # 视频信息探测
│
├── nodejs/                         # Node.js 便携版（TikTok JS 解签必需）
│   └── node.exe
│
├── cookies/                        # Cookie 按域名分类存储
│   ├── tiktok.com/
│   │   └── _default.txt
│   ├── youtube.com/
│   ├── instagram.com/
│   └── _meta.json
│
├── downloads/                      # 下载输出目录
│   ├── TikTok/
│   ├── YouTube/
│   └── Instagram/
│
├── _download_temp/                 # 下载临时文件（自动清理）
│
└── 视频好帮手_浏览器扩展/          # Chrome 扩展源码
    ├── manifest.json
    ├── src/
    │   ├── background.js           # 后台服务（Cookie 捕获 + 链接发送）
    │   ├── content-script.js       # 内容脚本（页面交互 + 右键菜单）
    │   └── ...
    ├── popup/                      # 弹出窗口 UI
    └── pages/                      # 选项页
```

## 核心架构

| 模块 | 类名 | 职责 |
|------|------|------|
| 主界面 | `MainWindow` | PyQt5 主窗口，用户交互、链接管理、状态显示 |
| 下载线程 | `DownloadThread` | QThread 工作线程，调用 yt-dlp 执行实际下载 |
| 桥接服务 | `OmniGetBridge` | 本地 HTTP 服务器，接收扩展发来的链接和 Cookie |
| Cookie 管理 | `CookieManager` | 按域名分类写入、合并、自动清理过期 Cookie |
| 配置管理 | `ConfigManager` | 读写 config.json，管理输出路径、视频质量等设置 |
| Chrome CDP | `ChromeCDP` | Chrome DevTools Protocol 连接（备用 Cookie 获取方式） |
| 链接列表 | `LinkListWidget` | 自定义 QListWidget，管理待下载链接队列 |

## 环境要求

### 运行环境

- **Python 3.8+**（开发/源码运行）
- **PyQt5** — GUI 框架
- **Node.js**（便携版或系统安装）— TikTok JS challenge 解签必需

### 依赖文件

| 文件 | 说明 | 必需 |
|------|------|------|
| `bin/yt-dlp.exe` | 视频下载引擎 | 是 |
| `ffmpeg/bin/ffmpeg.exe` | 视频合并/转码 | 是 |
| `ffmpeg/bin/ffprobe.exe` | 视频信息探测 | 是 |
| `nodejs/node.exe` | TikTok JS 解签运行时 | TikTok 下载必需 |

## 快速开始

### 1. 源码运行

```bash
# 安装依赖
pip install PyQt5

# 确保以下文件就位
# bin/yt-dlp.exe
# ffmpeg/bin/ffmpeg.exe
# ffmpeg/bin/ffprobe.exe
# nodejs/node.exe（可选，TikTok 需要）

# 启动
python man.py
```

### 2. 安装浏览器扩展

1. 打开 Chrome，访问 `chrome://extensions/`
2. 开启「开发者模式」
3. 点击「加载已解压的扩展程序」
4. 选择 `视频好帮手_浏览器扩展` 目录
5. 扩展安装完成后，在任意支持的视频网站页面右键即可看到「视频好帮手」菜单

### 3. 配对使用

1. 启动 `man.py`，程序会自动开启本地桥接服务
2. 在浏览器中打开视频网站（如 TikTok、YouTube）
3. 扩展会自动捕获当前页面的 Cookie 并同步到客户端
4. 右键点击视频页面 → 「发送到视频好帮手」，或使用快捷键 `Alt+O`
5. 链接自动进入客户端下载队列，开始下载

## 打包部署

### 使用 PyInstaller 打包

```powershell
pip install pyinstaller
pyinstaller --onefile --windowed --icon=icon.png --name="视频好帮手" man.py
```

### 部署清单

将以下内容复制到目标电脑：

```
视频好帮手/
├── 视频好帮手.exe          # 主程序
├── nodejs/node.exe         # Node.js 便携版（TikTok 必需）
├── bin/yt-dlp.exe          # 下载引擎
└── ffmpeg/                 # FFmpeg 工具集
```

> 详见 [打包部署说明.md](打包部署说明.md)

## 工作原理

### 下载流程

```
浏览器扩展                    本地桥接服务                   下载引擎
    │                            │                            │
    │── 捕获 Cookie ────────────>│                            │
    │   (webRequest API)         │── 写入 cookies/ ──────────>│
    │                            │   (按域名分类)              │
    │── 发送视频链接 ────────────>│                            │
    │   (HTTP POST)              │── 加入下载队列              │
    │                            │── 预请求（模拟用户点击）────>│
    │                            │── yt-dlp 下载 ────────────>│
    │                            │   (--cookies + JS runtime)  │
    │                            │<── 下载完成 ───────────────│
    │                            │── 指纹去重记录              │
    │                            │── 自动取下一个链接           │
```

### Cookie 管理策略

- **按域名分类**：每个平台独立存储（`cookies/tiktok.com/_default.txt`）
- **增量合并**：新 Cookie 与已有 Cookie 合并，不覆盖其他平台
- **自动清理**：Cookie 文件超过 8000 条时，按域名保留最新 500 条
- **Session Cookie 处理**：过期时间为 0 的 Cookie 自动延长 24 小时

### 反爬绕过机制

- **预请求**：下载前用浏览器 UA + Cookie 向目标 URL 发送请求，模拟用户点击
- **完整请求头**：TikTok 下载时附加 `Sec-Fetch-*`、`Accept`、`Referer` 等完整浏览器请求头
- **JS 运行时**：通过 Node.js 解签 TikTok 的 nsig/sig challenge
- **延迟策略**：预请求后随机等待 2.5-3.0 秒再启动 yt-dlp

## 快捷键

| 快捷键 | 功能 |
|--------|------|
| `Alt+O` | 在浏览器中将当前页面发送到视频好帮手 |
| `Ctrl+V` | 在客户端粘贴板区域粘贴链接自动添加 |
| `Delete` | 删除选中的链接 |

## 故障排除

| 问题 | 原因 | 解决方案 |
|------|------|----------|
| TikTok 下载失败率高 | 未检测到 Node.js | 确保 `nodejs/node.exe` 存在 |
| 提示 Cookie 不存在 | 扩展未配对 | 在浏览器中打开视频网站，等待自动同步 |
| yt-dlp 找不到 | 未包含 yt-dlp.exe | 确保 `bin/yt-dlp.exe` 就位 |
| 程序卡住不继续 | 线程竞争（已修复） | 升级到最新版本 |
| 重复下载已有视频 | 指纹库未扫描 | 点击「扫描」按钮重建指纹库 |

## 技术栈

- **GUI**: PyQt5 (Python 3.8+)
- **下载引擎**: yt-dlp
- **视频处理**: FFmpeg / FFprobe
- **JS 解签**: Node.js (便携版)
- **浏览器扩展**: Chrome Manifest V3
- **通信协议**: 本地 HTTP Bridge (localhost)
- **Cookie 格式**: Netscape Cookie File
