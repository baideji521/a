# -*- coding: utf-8 -*-

import sys
import os
import json
import time
import socket
import secrets
import re
import threading
import subprocess
import urllib.request
from http.server import HTTPServer, BaseHTTPRequestHandler
from pathlib import Path
from PyQt5.QtGui import QIcon

from PyQt5.QtCore import (
    Qt,
    QThread,
    pyqtSignal,
    QTimer,
    QPoint,
    QEvent
)

from PyQt5.QtGui import (
    QFont,
    QPalette,
    QColor,
    QKeySequence
)

from PyQt5.QtWidgets import (
    QApplication,
    QMainWindow,
    QWidget,
    QVBoxLayout,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QTextEdit,
    QListWidget,
    QListWidgetItem,
    QLineEdit,
    QProgressBar,
    QMessageBox,
    QFileDialog,
    QGroupBox,
    QComboBox,
    QMenu,
    QAction,
    QAbstractItemView,
    QFrame,
    QSizePolicy,
    QSpacerItem
)


# ============================================================
# 基础路径
# ============================================================

BASE_DIR = Path(__file__).resolve().parent

RUN_DIR = BASE_DIR
BIN_DIR = BASE_DIR / "bin"


def resolve_path(*candidates):
    """按顺序返回第一个存在的文件路径，都不存在则返回第一个候选"""
    for p in candidates:
        if p.exists() and p.is_file():
            return p
    return candidates[0]


# 优先使用根目录已有文件（与 下载.bat 一致），其次 bin / ffmpeg\bin
YTDLP_EXE = resolve_path(
    RUN_DIR / "yt-dlp.exe",
    BIN_DIR / "yt-dlp.exe"
)

COOKIE_FILE = resolve_path(
    RUN_DIR / "cookies.txt",
    BIN_DIR / "cookies.txt"
)
UA_FILE = resolve_path(
    RUN_DIR / "browser_ua.txt",
    BIN_DIR / "browser_ua.txt"
)

FFMPEG_EXE = resolve_path(
    RUN_DIR / "ffmpeg.exe",
    RUN_DIR / "ffmpeg" / "bin" / "ffmpeg.exe",
    BIN_DIR / "ffmpeg.exe"
)
FFPROBE_EXE = resolve_path(
    RUN_DIR / "ffprobe.exe",
    RUN_DIR / "ffmpeg" / "bin" / "ffprobe.exe",
    BIN_DIR / "ffprobe.exe"
)

LINK_FILE = RUN_DIR / "link.txt"

TEMP_DIR = RUN_DIR / "_download_temp"

DEFAULT_OUTPUT_DIR = RUN_DIR / "downloads"

CONFIG_FILE = RUN_DIR / "config.json"

COOKIES_DIR = RUN_DIR / "cookies"


# ============================================================
# Chrome CDP
# ============================================================

CHROME_HOST = "127.0.0.1"
CHROME_PORT = 9222


# ============================================================
# 默认 UA
# ============================================================

DEFAULT_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/151.0.0.0 Safari/537.36"
)


# ============================================================
# 默认配置
# ============================================================

DEFAULT_CONFIG = {
    "window": {
        "x": None,
        "y": None,
        "width": 900,
        "height": 650
    },

    "output_dir": str(DEFAULT_OUTPUT_DIR),

    "video_size": "810x1080",

    "video_codec": "H.264 (libx264)",

    "links": [],

    "bridge_token": "",

    "bridge_port": 47720
}


# ============================================================
# OmniGet 扩展桥接服务器
#
# 协议：
#   GET  /v1/health  → { ok: true, version: 1 }
#   GET  /v1/pair    → { ok: true, token: "xxx" }（配对窗口内）
#   POST /v1/enqueue → { url, protocolVersion } + Bearer token
#                    → { ok: true }
#
# 扩展默认端口 47720，范围 47720-47729
# ============================================================

BRIDGE_PORT_RANGE = list(range(47720, 47730))
BRIDGE_PROTOCOL_VERSION = 1


def is_downloadable_video_url(url):
    """基本 URL 检查，接受所有 http(s) 链接"""
    if not url:
        return False
    url = url.strip()
    return url.startswith("http://") or url.startswith("https://")


class OmniGetBridgeHandler(BaseHTTPRequestHandler):
    """处理 OmniGet 扩展的 HTTP 请求"""

    def log_message(self, format, *args):
        """静默日志，输出到 bridge 日志"""
        if self.server.bridge:
            self.server.bridge.log(f"{self.client_address[0]} - {format % args}")

    def send_json(self, code, data):
        body = json.dumps(data).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def check_auth(self):
        auth = self.headers.get("Authorization", "")
        if not auth.startswith("Bearer "):
            self.send_json(401, {"ok": False, "message": "Missing token"})
            return False
        token = auth[7:].strip()
        if token != self.server.bridge.token:
            self.send_json(403, {"ok": False, "message": "Invalid token"})
            return False
        return True

    def do_GET(self):
        path = self.path.split("?")[0]

        if path == "/v1/health":
            self.send_json(200, {
                "ok": True,
                "version": BRIDGE_PROTOCOL_VERSION
            })
            return

        if path == "/v1/pair":
            # 配对窗口打开时返回 token
            if self.server.bridge.pairing_active:
                self.send_json(200, {
                    "ok": True,
                    "token": self.server.bridge.token
                })
                self.server.bridge.pairing_active = False
                self.server.bridge.log("配对成功")
            else:
                self.send_json(404, {"ok": False, "message": "Pairing window closed"})
            return

        self.send_json(404, {"ok": False, "message": "Not found"})

    def do_POST(self):
        path = self.path.split("?")[0]

        if path == "/v1/enqueue":
            if not self.check_auth():
                return

            try:
                length = int(self.headers.get("Content-Length", 0))
                body = self.rfile.read(length)
                data = json.loads(body.decode("utf-8"))
            except Exception as e:
                self.send_json(400, {"ok": False, "message": str(e)})
                return

            url = data.get("url", "").strip()
            if not url:
                self.send_json(400, {"ok": False, "message": "Missing url"})
                return

            # 扩展发送的 Cookie + UA 一起处理
            cookies = data.get("cookies", [])
            ua = data.get("userAgent", "").strip()

            # 如果有 Cookie，自动更新
            if cookies and self.server.bridge.cookies_callback:
                try:
                    self.server.bridge.cookies_callback(cookies, ua, url)
                except Exception as e:
                    self.server.bridge.log(f"Cookie 回调失败：{e}")
            elif ua and self.server.bridge.ua_callback:
                # 没有 Cookie 但有 UA，也更新
                try:
                    self.server.bridge.ua_callback(ua)
                except Exception as e:
                    self.server.bridge.log(f"UA 回调失败：{e}")

            # 直接加入下载队列（不过滤，不写文件）
            self.server.bridge.log(f"enqueue 收到：{url}")
            self.server.bridge.enqueue_url(url)

            self.send_json(200, {"ok": True})
            return

        if path == "/v1/cookies":
            if not self.check_auth():
                return

            try:
                length = int(self.headers.get("Content-Length", 0))
                body = self.rfile.read(length)
                data = json.loads(body.decode("utf-8"))
            except Exception as e:
                self.send_json(400, {"ok": False, "message": str(e)})
                return

            # 扩展单独发送 Cookie（自动捕获模式）
            cookies = data.get("cookies", [])
            source_url = data.get("sourceUrl", "")
            ua = data.get("userAgent", "").strip()

            if cookies and self.server.bridge.cookies_callback:
                try:
                    self.server.bridge.cookies_callback(cookies, ua, source_url)
                except Exception as e:
                    self.server.bridge.log(f"Cookie 回调失败：{e}")

            self.send_json(200, {"ok": True, "message": "Cookies received"})
            return

        if path == "/v1/ua":
            if not self.check_auth():
                return

            try:
                length = int(self.headers.get("Content-Length", 0))
                body = self.rfile.read(length)
                data = json.loads(body.decode("utf-8"))
            except Exception as e:
                self.send_json(400, {"ok": False, "message": str(e)})
                return

            ua = data.get("userAgent", "").strip()
            if ua and self.server.bridge.ua_callback:
                try:
                    self.server.bridge.ua_callback(ua)
                except Exception as e:
                    self.server.bridge.log(f"UA 回调失败：{e}")

            self.send_json(200, {"ok": True})
            return

        self.send_json(404, {"ok": False, "message": "Not found"})


class OmniGetBridge:
    """OmniGet 扩展桥接服务器"""

    def __init__(self, token=None, port=None, link_file=None, log_callback=None,
                 url_callback=None, cookies_callback=None, ua_callback=None):
        self.token = token or secrets.token_urlsafe(24)
        self.port = port
        self.link_file = link_file or LINK_FILE
        self.log_callback = log_callback
        self.url_callback = url_callback  # 收到 URL 时的回调
        self.cookies_callback = cookies_callback  # 收到 Cookie 时的回调
        self.ua_callback = ua_callback  # 收到 UA 时的回调
        self.pairing_active = False
        self.server = None
        self.thread = None
        self._running = False

    def log(self, msg):
        print(f"[Bridge] {msg}", flush=True)
        if self.log_callback:
            try:
                self.log_callback(f"{msg}")
            except Exception:
                pass

    def enqueue_url(self, url):
        """收到链接：通知 UI 列表 + 触发自动下载（不写文件）"""
        self.log(f"收到链接：{url}")

        # 调用 URL 回调（添加到 UI 列表 + 触发自动下载）
        if self.url_callback:
            try:
                self.url_callback(url)
            except Exception as e:
                self.log(f"URL 回调执行失败：{e}")

    def start_pairing(self):
        """开启配对窗口（约 120 秒）"""
        self.pairing_active = True
        self.log("配对窗口已开启，请在扩展中点击配对")

        def close_pairing():
            time.sleep(120)
            self.pairing_active = False
            self.log("配对窗口已关闭")

        t = threading.Thread(target=close_pairing, daemon=True)
        t.start()

    def start(self):
        """启动桥接服务器（非阻塞）"""
        if self._running:
            return

        # 尝试端口范围
        for port in BRIDGE_PORT_RANGE:
            try:
                self.server = HTTPServer(
                    ("127.0.0.1", port),
                    OmniGetBridgeHandler
                )
                self.server.bridge = self
                self.port = port
                break
            except OSError:
                continue

        if not self.server:
            self.log("所有端口都被占用，桥接服务器启动失败")
            return

        self._running = True
        self.thread = threading.Thread(target=self._serve, daemon=True)
        self.thread.start()
        self.log(f"桥接服务器已启动：http://127.0.0.1:{self.port}")
        self.log(f"Token：{self.token}")

    def _serve(self):
        # 用超时轮询代替阻塞式 handle_request，避免线程被慢请求占死
        self.server.timeout = 1
        while self._running:
            try:
                self.server.handle_request()
            except Exception as e:
                if self._running:
                    self.log(f"服务器异常：{e}")

    def stop(self):
        self._running = False
        if self.server:
            self.server.server_close()
        self.log("桥接服务器已停止")


# ============================================================
# 工具函数
# ============================================================

def read_text_file(path: Path, default=""):
    try:

        if not path.exists():
            return default

        return path.read_text(
            encoding="utf-8",
            errors="ignore"
        ).strip()

    except Exception:

        return default


def write_text_file(path: Path, text: str):

    path.parent.mkdir(
        parents=True,
        exist_ok=True
    )

    path.write_text(
        text,
        encoding="utf-8"
    )


def check_file(path: Path):

    return (
        path.exists()
        and
        path.is_file()
    )


def safe_filename(name: str):

    invalid = '<>:"/\\|?*'

    for c in invalid:
        name = name.replace(c, "_")

    name = name.strip()

    if not name:
        name = "video"

    return name


def detect_platform(url: str):

    u = url.lower()

    if "tiktok.com" in u:
        return "tiktok"

    if "youtube.com" in u or "youtu.be" in u:
        return "youtube"

    if "facebook.com" in u or "fb.watch" in u:
        return "facebook"

    if "instagram.com" in u:
        return "instagram"

    if "twitter.com" in u or "x.com" in u:
        return "twitter"

    return "other"


def extract_video_id_from_url(url):
    """从 URL 中提取视频 ID（用于 DNA 去重）
    返回 (platform, video_id) 或 None
    """
    from urllib.parse import urlparse
    try:
        u = url.lower().strip()
        if "tiktok.com" in u:
            # https://www.tiktok.com/@user/video/7486922641006202154
            import re
            m = re.search(r'/video/(\d+)', url)
            if m:
                return ("tiktok", m.group(1))
        elif "youtube.com" in u or "youtu.be" in u:
            import re
            # shorts, watch, embed, v=
            m = re.search(r'/(?:shorts|watch|embed)/([A-Za-z0-9_-]+)', url)
            if m:
                return ("youtube", m.group(1))
            m = re.search(r'[?&]v=([A-Za-z0-9_-]+)', url)
            if m:
                return ("youtube", m.group(1))
        elif "instagram.com" in u:
            import re
            m = re.search(r'/(?:p|reel|tv)/([A-Za-z0-9_-]+)', url)
            if m:
                return ("instagram", m.group(1))
    except Exception:
        pass
    return None


def ensure_dirs():

    TEMP_DIR.mkdir(
        parents=True,
        exist_ok=True
    )

    DEFAULT_OUTPUT_DIR.mkdir(
        parents=True,
        exist_ok=True
    )


def is_url(text):

    text = text.strip().lower()

    return (
        text.startswith("http://")
        or
        text.startswith("https://")
    )


def parse_text_urls(text):

    urls = []

    for line in text.replace(
        "\r\n",
        "\n"
    ).replace(
        "\r",
        "\n"
    ).split("\n"):

        line = line.strip()

        if not line:
            continue

        if is_url(line):

            urls.append(line)

    return urls


# ============================================================
# 配置管理
# ============================================================

class ConfigManager:

    def __init__(self, path):

        self.path = Path(path)

        self.data = {}

        self.load()

    def load(self):

        self.data = json.loads(
            json.dumps(
                DEFAULT_CONFIG
            )
        )

        try:

            if self.path.exists():

                user_data = json.loads(
                    self.path.read_text(
                        encoding="utf-8"
                    )
                )

                if isinstance(
                    user_data,
                    dict
                ):

                    self.deep_update(
                        self.data,
                        user_data
                    )

        except Exception:

            pass

    @staticmethod
    def deep_update(target, source):

        for key, value in source.items():

            if (
                isinstance(value, dict)
                and
                isinstance(
                    target.get(key),
                    dict
                )
            ):

                ConfigManager.deep_update(
                    target[key],
                    value
                )

            else:

                target[key] = value

    def save(self):

        try:

            self.path.parent.mkdir(
                parents=True,
                exist_ok=True
            )

            temp_file = self.path.with_suffix(
                ".tmp"
            )

            temp_file.write_text(
                json.dumps(
                    self.data,
                    ensure_ascii=False,
                    indent=4
                ),
                encoding="utf-8"
            )

            temp_file.replace(
                self.path
            )

        except Exception:

            pass

    def set(self, key, value):

        self.data[key] = value

        self.save()

    def get(self, key, default=None):

        return self.data.get(
            key,
            default
        )


# ============================================================
# Cookie 管理器
# 复刻 OmniGet 的 Cookie 存储方式：
#   - 按根域名分目录存储
#   - Netscape 格式，支持 #HttpOnly_ 前缀
#   - 维护 _meta.json 注册表
#   - 下载时按 URL 域名选择对应 Cookie
# ============================================================

PLATFORM_COOKIE_DOMAINS = {
    "youtube": [".youtube.com", ".google.com"],
    "instagram": [".instagram.com", ".cdninstagram.com", ".fbcdn.net"],
    "tiktok": [".tiktok.com", ".tiktokcdn.com"],
    "twitter": [".twitter.com", ".x.com"],
    "facebook": [".facebook.com", ".fbcdn.net"],
    "reddit": [".reddit.com"],
    "twitch": [".twitch.tv", ".jtvnw.net"],
    "vimeo": [".vimeo.com", ".vimeocdn.com"],
    "bilibili": [".bilibili.com", ".bilivideo.com"],
    "soundcloud": [".soundcloud.com", ".sndcdn.com"],
    "pinterest": [".pinterest.com"],
}

# CDN 域名 → 平台主域名映射（视频 URL 通常在 CDN 上，需要找到对应平台的 Cookie）
CDN_TO_PRIMARY_DOMAIN = {
    "tiktokcdn.com": "tiktok.com",
    "cdninstagram.com": "instagram.com",
    "fbcdn.net": "facebook.com",
    "bilivideo.com": "bilibili.com",
    "jtvnw.net": "twitch.tv",
    "vimeocdn.com": "vimeo.com",
    "sndcdn.com": "soundcloud.com",
    "google.com": "youtube.com",  # YouTube 认证 Cookie 在 google.com
    "googlevideo.com": "youtube.com",
    "ytimg.com": "youtube.com",
}


class CookieManager:
    """按域名分类存储 Cookie，复刻 OmniGet 的 Cookie 管理方式

    目录结构：
        cookies/
            <root_domain>/
                _default.txt      # Netscape 格式，yt-dlp 可直接消费
            _meta.json            # 注册表：平台、来源、捕获时间
    """

    def __init__(self, cookies_dir=None):
        self.root = Path(cookies_dir or COOKIES_DIR)
        self.root.mkdir(parents=True, exist_ok=True)

    # ---- 公共 API ----

    def ingest_batch(self, cookies, source_url=None, source_label="Browser extension"):
        """接收扩展发来的 Cookie 列表，按根域名分组写入

        返回 [(domain, count), ...]
        """
        if not cookies:
            return []

        by_root = {}
        for c in cookies:
            root = self.root_domain_of(c.get("domain", ""))
            if not root:
                continue
            by_root.setdefault(root, []).append(c)

        registry = self._load_registry()
        now_ms = int(time.time() * 1000)
        written = []

        for root, group in by_root.items():
            count = self._write_account_file(root, group)
            platform = self._detect_platform(root)

            bucket = registry.setdefault(root, {
                "platform_kind": platform,
                "accounts": [],
            })
            bucket["platform_kind"] = platform

            alias = f"{platform} \u00b7 {time.strftime('%Y-%m-%d')}"

            existing = next(
                (a for a in bucket["accounts"] if a["slug"] == "_default"),
                None
            )
            if existing:
                existing["captured_at_ms"] = now_ms
                existing["cookie_count"] = count
                existing["source_label"] = source_label
                if source_url:
                    existing["source_url"] = source_url
            else:
                bucket["accounts"].append({
                    "slug": "_default",
                    "alias": alias,
                    "source_url": source_url,
                    "source_label": source_label,
                    "captured_at_ms": now_ms,
                    "cookie_count": count,
                })

            written.append((root, count))

        self._save_registry(registry)
        return written

    def get_cookie_args(self, url):
        """根据 URL 返回 yt-dlp 的 --cookies 参数

        优先使用按域名存储的 Cookie，回退到旧的 cookies.txt
        """
        if not url:
            return self._legacy_fallback()

        root = self.root_domain_of(self._extract_host(url))
        cookie_file = self.root / root / "_default.txt"

        # 有按域名的 Cookie → 直接使用
        if cookie_file.exists():
            return ["--cookies", str(cookie_file)]

        # 尝试平台关联域名（如 YouTube 需要 .google.com 的 Cookie）
        platform = self._detect_platform(root)
        for domain in PLATFORM_COOKIE_DOMAINS.get(platform, []):
            d = domain.lstrip(".")
            alt_file = self.root / d / "_default.txt"
            if alt_file.exists():
                return ["--cookies", str(alt_file)]

        # 回退到旧的 cookies.txt
        return self._legacy_fallback()

    def get_combined_cookie_text(self, url):
        """获取指定 URL 的所有相关 Cookie 文本（Netscape 格式）

        合并：目标域名 Cookie + 平台关联域名 Cookie + 旧 cookies.txt
        """
        if not url:
            return ""

        root = self.root_domain_of(self._extract_host(url))
        platform = self._detect_platform(root)

        # 收集所有相关域名
        relevant_domains = {root}
        for domain in PLATFORM_COOKIE_DOMAINS.get(platform, []):
            relevant_domains.add(domain.lstrip("."))

        lines = ["# Netscape HTTP Cookie File", "# Combined by CookieManager", ""]
        seen = set()

        for domain in relevant_domains:
            cookie_file = self.root / domain / "_default.txt"
            if cookie_file.exists():
                try:
                    text = cookie_file.read_text(encoding="utf-8", errors="ignore")
                    for line in text.splitlines():
                        stripped = line.strip()
                        if stripped and not stripped.startswith("#"):
                            if stripped not in seen:
                                seen.add(stripped)
                                lines.append(line)
                except Exception:
                    pass

        # 合并旧 cookies.txt 中不冲突的条目
        if COOKIE_FILE.exists():
            try:
                text = COOKIE_FILE.read_text(encoding="utf-8", errors="ignore")
                for line in text.splitlines():
                    stripped = line.strip()
                    if stripped and not stripped.startswith("#"):
                        key = stripped.split("\t")
                        if len(key) >= 7:
                            cookie_key = (key[0], key[5])
                            if stripped not in seen:
                                seen.add(stripped)
                                lines.append(line)
            except Exception:
                pass

        if len(lines) <= 3:
            return ""
        return "\n".join(lines) + "\n"

    def list_domains(self):
        """列出所有已存储 Cookie 的域名"""
        if not self.root.exists():
            return []
        return [
            d.name for d in self.root.iterdir()
            if d.is_dir() and not d.name.startswith("_")
        ]

    def get_cookie_count(self, domain=None):
        """获取 Cookie 数量"""
        if domain:
            f = self.root / domain / "_default.txt"
            if f.exists():
                return sum(
                    1 for line in f.read_text(encoding="utf-8", errors="ignore").splitlines()
                    if line.strip() and not line.strip().startswith("#")
                )
            return 0

        total = 0
        for d in self.list_domains():
            total += self.get_cookie_count(d)
        return total

    # ---- 内部方法 ----

    @staticmethod
    def root_domain_of(domain):
        """提取根域名：'.www.youtube.com' -> 'youtube.com'"""
        d = domain.strip().lstrip(".").lower()
        if not d:
            return ""
        parts = d.split(".")
        if len(parts) >= 2:
            return ".".join(parts[-2:])
        return d

    @staticmethod
    def _extract_host(url):
        """从 URL 提取主机名"""
        try:
            from urllib.parse import urlparse
            parsed = urlparse(url.strip())
            return parsed.hostname or ""
        except Exception:
            return ""

    @staticmethod
    def _sanitize_field(value):
        """清理字段中的换行和制表符"""
        return (
            str(value)
            .replace("\r", "")
            .replace("\n", "")
            .replace("\t", "")
        )

    def _write_account_file(self, domain, cookies):
        """将 Cookie 写入按域名分类的文件（Netscape 格式 + #HttpOnly_ 前缀）"""
        dir_path = self.root / self._safe_domain_segment(domain)
        dir_path.mkdir(parents=True, exist_ok=True)

        path = dir_path / "_default.txt"
        session_ttl = int(time.time()) + 86400

        lines = ["# Netscape HTTP Cookie File"]
        for c in cookies:
            lines.append(self._format_cookie_line(c, session_ttl))

        path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        return len(cookies)

    @staticmethod
    def _format_cookie_line(c, session_ttl=0):
        """将单个 Cookie 格式化为 Netscape 行（支持 #HttpOnly_ 前缀）"""
        raw_domain = CookieManager._sanitize_field(c.get("domain", ""))
        path_field = CookieManager._sanitize_field(c.get("path", "/"))
        name = CookieManager._sanitize_field(c.get("name", ""))
        value = CookieManager._sanitize_field(c.get("value", ""))

        http_only = c.get("httpOnly", False)
        host_only = c.get("hostOnly", None)
        if host_only is None:
            host_only = not raw_domain.startswith(".")

        http_only_prefix = "#HttpOnly_" if http_only else ""

        if host_only:
            domain = raw_domain.lstrip(".")
            include_subdomains = "FALSE"
        elif raw_domain.startswith("."):
            domain = raw_domain
            include_subdomains = "TRUE"
        else:
            domain = f".{raw_domain}"
            include_subdomains = "TRUE"

        secure = "TRUE" if c.get("secure", False) else "FALSE"

        expires = c.get("expires", 0)
        if not expires or expires < 0:
            expires = session_ttl
        else:
            expires = int(expires)

        return (
            f"{http_only_prefix}{domain}\t{include_subdomains}\t"
            f"{path_field}\t{secure}\t{expires}\t{name}\t{value}"
        )

    @staticmethod
    def _safe_domain_segment(domain):
        """将域名转为安全的目录名"""
        d = domain.strip().lstrip(".").lower()
        safe = ""
        for ch in d:
            if ch.isalnum() or ch in ".-_":
                safe += ch
            else:
                safe += "_"
        return safe or "unknown"

    @staticmethod
    def _detect_platform(root_domain):
        """根据根域名检测平台（支持 CDN 域名）"""
        d = root_domain.lower()
        if d in ("youtube.com", "youtu.be"):
            return "youtube"
        if d in ("google.com", "googlevideo.com", "ytimg.com"):
            return "youtube"
        if d in ("instagram.com", "cdninstagram.com"):
            return "instagram"
        if d in ("tiktok.com", "tiktokcdn.com"):
            return "tiktok"
        if d in ("twitter.com", "x.com"):
            return "twitter"
        if d in ("facebook.com", "fb.watch", "fbcdn.net"):
            return "facebook"
        if d in ("reddit.com",):
            return "reddit"
        if d in ("twitch.tv", "jtvnw.net"):
            return "twitch"
        if d in ("vimeo.com", "vimeocdn.com"):
            return "vimeo"
        if d in ("bilibili.com", "bilivideo.com"):
            return "bilibili"
        if d in ("soundcloud.com", "sndcdn.com"):
            return "soundcloud"
        if d in ("pinterest.com",):
            return "pinterest"
        return "other"

    def _load_registry(self):
        """加载 _meta.json 注册表"""
        path = self.root / "_meta.json"
        if path.exists():
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                if isinstance(data, dict):
                    return data
            except Exception:
                pass
        return {}

    def _save_registry(self, registry):
        """保存 _meta.json 注册表"""
        path = self.root / "_meta.json"
        try:
            tmp = path.with_suffix(".json.tmp")
            tmp.write_text(
                json.dumps(registry, ensure_ascii=False, indent=2),
                encoding="utf-8"
            )
            tmp.replace(path)
        except Exception:
            pass

    def _legacy_fallback(self):
        """回退到旧的 cookies.txt"""
        if COOKIE_FILE.exists():
            return ["--cookies", str(COOKIE_FILE)]
        return []


# ============================================================
# 输出目录输入框
# 支持：
# 1. 手动编辑
# 2. Windows 资源管理器拖入文件夹
# 3. 拖动经过时显示可接受状态
# 4. 不接受普通文件
# ============================================================

class OutputFolderLineEdit(QLineEdit):

    folder_dropped = pyqtSignal(str)

    def __init__(self, parent=None):

        super().__init__(parent)

        # --------------------------------------------------------
        # 关键：同时开启 Qt 控件自身和窗口属性的拖放接收
        # Windows 资源管理器拖文件夹时，部分环境下仅设置
        # setAcceptDrops(True) 不足以稳定收到 Drop 事件。
        # --------------------------------------------------------
        self.setAcceptDrops(True)
        self.setAttribute(Qt.WA_AcceptDrops, True)

        # 输入框保持普通可编辑状态
        self.setReadOnly(False)

    # --------------------------------------------------------
    # 从 MIME 数据中获取第一个本地文件夹
    # --------------------------------------------------------

    def get_dropped_folder(self, mime):

        if mime is None or not mime.hasUrls():
            return None

        for url in mime.urls():

            try:

                if not url.isLocalFile():
                    continue

                local_path = url.toLocalFile()

                if not local_path:
                    continue

                path = Path(local_path)

                if path.exists() and path.is_dir():
                    return str(path)

            except Exception:
                continue

        return None

    # --------------------------------------------------------
    # 统一处理拖放事件
    # 通过 event() 再拦截一次，兼容 Windows 资源管理器
    # 在不同 Qt / Windows 环境下的拖放事件分发差异。
    # --------------------------------------------------------

    def event(self, event):

        event_type = event.type()

        if event_type in (
            QEvent.DragEnter,
            QEvent.DragMove
        ):

            folder = self.get_dropped_folder(
                event.mimeData()
            )

            if folder:

                event.setDropAction(
                    Qt.CopyAction
                )

                event.accept()
                return True

            event.ignore()
            return True

        if event_type == QEvent.Drop:

            folder = self.get_dropped_folder(
                event.mimeData()
            )

            if folder:

                self.setText(folder)
                self.setCursorPosition(len(folder))
                self.setFocus(Qt.MouseFocusReason)

                self.folder_dropped.emit(folder)

                event.setDropAction(
                    Qt.CopyAction
                )

                event.accept()
                return True

            event.ignore()
            return True

        return super().event(event)

    # --------------------------------------------------------
    # 鼠标拖入
    # --------------------------------------------------------

    def dragEnterEvent(self, event):

        folder = self.get_dropped_folder(
            event.mimeData()
        )

        if folder:

            event.setDropAction(
                Qt.CopyAction
            )
            event.accept()
            return

        event.ignore()

    # --------------------------------------------------------
    # 鼠标拖动经过输入框
    # --------------------------------------------------------

    def dragMoveEvent(self, event):

        folder = self.get_dropped_folder(
            event.mimeData()
        )

        if folder:

            event.setDropAction(
                Qt.CopyAction
            )
            event.accept()
            return

        event.ignore()

    # --------------------------------------------------------
    # 鼠标离开
    # --------------------------------------------------------

    def dragLeaveEvent(self, event):
        event.accept()

    # --------------------------------------------------------
    # 松开鼠标
    # --------------------------------------------------------

    def dropEvent(self, event):

        folder = self.get_dropped_folder(
            event.mimeData()
        )

        if folder:

            self.setText(folder)
            self.setCursorPosition(len(folder))
            self.setFocus(Qt.MouseFocusReason)

            self.folder_dropped.emit(folder)

            event.setDropAction(
                Qt.CopyAction
            )
            event.accept()
            return

        event.ignore()


# ============================================================
# Chrome CDP 客户端
# ============================================================

class ChromeCDP:

    def __init__(
        self,
        host="127.0.0.1",
        port=9222
    ):

        self.host = host
        self.port = port

        self.ws = None

        self.page = None

        self.message_id = 0

    # --------------------------------------------------------
    # 检测 Chrome
    # --------------------------------------------------------

    def is_available(self):

        try:

            sock = socket.create_connection(
                (
                    self.host,
                    self.port
                ),
                timeout=2
            )

            sock.close()

            return True

        except Exception:

            return False

    # --------------------------------------------------------
    # HTTP JSON
    # --------------------------------------------------------

    def http_get_json(self, path):

        url = (
            f"http://{self.host}:"
            f"{self.port}"
            f"{path}"
        )

        request = urllib.request.Request(
            url,
            headers={
                "User-Agent": DEFAULT_UA
            }
        )

        with urllib.request.urlopen(
            request,
            timeout=5
        ) as response:

            data = response.read()

        return json.loads(
            data.decode(
                "utf-8",
                errors="ignore"
            )
        )

    # --------------------------------------------------------
    # Chrome 版本
    # --------------------------------------------------------

    def get_version(self):

        return self.http_get_json(
            "/json/version"
        )

    # --------------------------------------------------------
    # 页面
    # --------------------------------------------------------

    def get_pages(self):

        try:

            pages = self.http_get_json(
                "/json"
            )

            if not isinstance(
                pages,
                list
            ):

                return []

            return pages

        except Exception as e:

            print(
                f"获取 Chrome 页面失败：{e}"
            )

            return []

    # --------------------------------------------------------
    # 获取页面
    # --------------------------------------------------------

    def get_page(self):

        pages = self.get_pages()

        if not pages:

            raise RuntimeError(
                "Chrome 没有打开任何可用页面。"
            )

        preferred = (
            "tiktok.com",
            "youtube.com",
            "facebook.com",
            "instagram.com",
            "twitter.com",
            "x.com"
        )

        for page in pages:

            page_type = str(
                page.get(
                    "type",
                    ""
                )
            ).lower()

            if page_type != "page":
                continue

            url = str(
                page.get(
                    "url",
                    ""
                )
            ).lower()

            for domain in preferred:

                if domain in url:

                    return page

        for page in pages:

            if str(
                page.get(
                    "type",
                    ""
                )
            ).lower() == "page":

                return page

        raise RuntimeError(
            "Chrome 没有找到普通网页 Target。"
        )

    # --------------------------------------------------------
    # CDP 连接
    # --------------------------------------------------------

    def connect(self):

        try:

            import websocket

        except ImportError:

            raise RuntimeError(
                "缺少 websocket-client。\n\n"
                "请执行：\n"
                "python -m pip install -U websocket-client"
            )

        if not hasattr(
            websocket,
            "create_connection"
        ):

            module_file = getattr(
                websocket,
                "__file__",
                "未知"
            )

            raise RuntimeError(
                "当前 websocket 模块不正确。\n\n"
                f"模块位置：\n{module_file}\n\n"
                "请执行：\n"
                "python -m pip uninstall websocket -y\n"
                "python -m pip install -U websocket-client"
            )

        page = self.get_page()

        self.page = page

        ws_url = page.get(
            "webSocketDebuggerUrl"
        )

        if not ws_url:

            raise RuntimeError(
                "Chrome 页面没有返回 webSocketDebuggerUrl。"
            )

        try:

            self.ws = websocket.create_connection(
                ws_url,
                timeout=15,
                enable_multithread=True
            )

        except TypeError:

            self.ws = websocket.create_connection(
                ws_url,
                timeout=15
            )

        except Exception as e:

            raise RuntimeError(
                "连接 Chrome CDP WebSocket 失败：\n"
                f"{e}"
            )

        self.message_id = 0

        return page

    # --------------------------------------------------------
    # CDP 调用
    # --------------------------------------------------------

    def call(
        self,
        method,
        params=None
    ):

        if self.ws is None:

            raise RuntimeError(
                "Chrome CDP 尚未连接。"
            )

        self.message_id += 1

        message_id = self.message_id

        payload = {
            "id": message_id,
            "method": method
        }

        if params is not None:

            payload["params"] = params

        try:

            self.ws.send(
                json.dumps(
                    payload,
                    ensure_ascii=False
                )
            )

        except Exception as e:

            raise RuntimeError(
                f"CDP 发送失败：{e}"
            )

        while True:

            try:

                response = self.ws.recv()

            except Exception as e:

                raise RuntimeError(
                    f"CDP 接收失败：{e}"
                )

            if not response:
                continue

            try:

                data = json.loads(
                    response
                )

            except Exception:

                continue

            if data.get("id") != message_id:
                continue

            if "error" in data:

                raise RuntimeError(
                    f"CDP {method} 调用失败："
                    f"{data.get('error', {})}"
                )

            return data.get(
                "result",
                {}
            )

    # --------------------------------------------------------
    # Cookie
    # --------------------------------------------------------

    def get_cookies(self):

        urls = []

        try:

            pages = self.get_pages()

            for page in pages:

                if str(
                    page.get(
                        "type",
                        ""
                    )
                ).lower() != "page":

                    continue

                url = str(
                    page.get(
                        "url",
                        ""
                    )
                ).strip()

                if (
                    url.startswith(
                        "http://"
                    )
                    or
                    url.startswith(
                        "https://"
                    )
                ):

                    if url not in urls:

                        urls.append(url)

        except Exception:

            pass

        if self.page:

            current_url = str(
                self.page.get(
                    "url",
                    ""
                )
            ).strip()

            if (
                current_url.startswith(
                    "http://"
                )
                or
                current_url.startswith(
                    "https://"
                )
            ):

                if current_url in urls:

                    urls.remove(
                        current_url
                    )

                urls.insert(
                    0,
                    current_url
                )

        if not urls:

            raise RuntimeError(
                "Chrome 当前没有可读取 Cookie 的 HTTP/HTTPS 页面。"
            )

        urls = urls[:20]

        all_cookies = []

        cookie_keys = set()

        for url in urls:

            try:

                result = self.call(
                    "Network.getCookies",
                    {
                        "urls": [
                            url
                        ]
                    }
                )

                cookies = result.get(
                    "cookies",
                    []
                )

                if not isinstance(
                    cookies,
                    list
                ):

                    continue

                for cookie in cookies:

                    try:

                        key = (
                            str(
                                cookie.get(
                                    "domain",
                                    ""
                                )
                            ),
                            str(
                                cookie.get(
                                    "path",
                                    "/"
                                )
                            ),
                            str(
                                cookie.get(
                                    "name",
                                    ""
                                )
                            )
                        )

                        if key in cookie_keys:
                            continue

                        cookie_keys.add(
                            key
                        )

                        all_cookies.append(
                            cookie
                        )

                    except Exception:

                        continue

            except Exception as e:

                print(
                    f"读取 Cookie 失败："
                    f"{url} -> {e}"
                )

                continue

        if not all_cookies:

            raise RuntimeError(
                "Chrome 没有读取到 Cookie。\n\n"
                "请确认：\n"
                "1. Chrome 已登录目标网站\n"
                "2. Chrome 当前打开目标网站页面\n"
                "3. Chrome 使用 --remote-debugging-port=9222 启动"
            )

        return all_cookies

    # --------------------------------------------------------
    # UA
    # --------------------------------------------------------

    def get_user_agent(self):

        try:

            version = self.get_version()

            ua = version.get(
                "User-Agent",
                ""
            )

            if ua:

                ua = str(
                    ua
                ).strip()

                if ua:
                    return ua

        except Exception as e:

            print(
                f"/json/version 获取 UA 失败：{e}"
            )

        try:

            result = self.call(
                "Runtime.evaluate",
                {
                    "expression":
                        "navigator.userAgent",
                    "returnByValue":
                        True
                }
            )

            value = (
                result
                .get(
                    "result",
                    {}
                )
                .get(
                    "result",
                    {}
                )
                .get(
                    "value",
                    ""
                )
            )

            if value:

                value = str(
                    value
                ).strip()

                if value:
                    return value

        except Exception as e:

            print(
                f"Runtime.evaluate 获取 UA 失败：{e}"
            )

        return DEFAULT_UA

    def close(self):

        try:

            if self.ws:

                self.ws.close()

        except Exception:

            pass

        self.ws = None
        self.page = None


# ============================================================
# Chrome Cookie → Netscape
# ============================================================

def chrome_cookies_to_netscape(cookies):

    lines = [
        "# Netscape HTTP Cookie File",
        "# Generated by Python Chrome CDP",
        "# DO NOT EDIT",
        ""
    ]

    count = 0

    for cookie in cookies:

        try:

            domain = str(
                cookie.get(
                    "domain",
                    ""
                )
            )

            include_subdomains = (
                "TRUE"
                if domain.startswith(".")
                else "FALSE"
            )

            path = str(
                cookie.get(
                    "path",
                    "/"
                )
            )

            secure = (
                "TRUE"
                if cookie.get(
                    "secure",
                    False
                )
                else "FALSE"
            )

            expires = cookie.get(
                "expires",
                0
            )

            try:

                expires = int(
                    float(expires)
                )

            except Exception:

                expires = 0

            if expires < 0:

                expires = 0

            name = str(
                cookie.get(
                    "name",
                    ""
                )
            )

            value = str(
                cookie.get(
                    "value",
                    ""
                )
            )

            domain = domain.replace(
                "\t",
                ""
            ).replace(
                "\r",
                ""
            ).replace(
                "\n",
                ""
            )

            path = path.replace(
                "\t",
                ""
            ).replace(
                "\r",
                ""
            ).replace(
                "\n",
                ""
            )

            name = name.replace(
                "\t",
                ""
            ).replace(
                "\r",
                ""
            ).replace(
                "\n",
                ""
            )

            value = value.replace(
                "\r",
                ""
            ).replace(
                "\n",
                ""
            ).replace(
                "\t",
                ""
            )

            lines.append(
                "\t".join(
                    [
                        domain,
                        include_subdomains,
                        path,
                        secure,
                        str(expires),
                        name,
                        value
                    ]
                )
            )

            count += 1

        except Exception:

            continue

    return (
        "\r\n".join(lines)
        + "\r\n"
    ), count


# ============================================================
# 下载线程
# ============================================================

class DownloadThread(QThread):

    log_signal = pyqtSignal(str)

    progress_signal = pyqtSignal(int)

    current_signal = pyqtSignal(
        int,
        int,
        str
    )

    url_finished_signal = pyqtSignal(str, bool)  # url, success

    finished_signal = pyqtSignal(
        int,
        int
    )

    def __init__(
        self,
        urls,
        target_w,
        target_h,
        video_codec,
        output_dir,
        parent=None,
        cookies_dir=None
    ):

        super().__init__(parent)

        self.urls = urls

        self.target_w = target_w

        self.target_h = target_h

        self.video_codec = video_codec

        self.output_dir = Path(
            output_dir
        )

        self.cookies_dir = Path(cookies_dir) if cookies_dir else COOKIES_DIR

        self.stop_flag = False

        # 跳过当前链接（用户在列表中删除了正在下载的链接）
        self.skip_flag = False

        # 被用户手动删除的链接集合
        self.cancelled_urls = set()

        # 当前正在处理的链接
        self.current_url = ""

        self.success = 0

        self.failed = 0

    def stop(self):

        self.stop_flag = True

    def cancel_url(self, url):
        """取消指定链接：如果正在下载则立即中断"""
        try:
            url = str(url).strip()
            if not url:
                return
            self.cancelled_urls.add(url)
            if url == self.current_url:
                self.skip_flag = True
        except Exception:
            pass

    def log(self, text):

        self.log_signal.emit(
            str(text)
        )

    def check_dependencies(self):

        if not check_file(
            YTDLP_EXE
        ):

            raise RuntimeError(
                f"找不到 yt-dlp.exe：\n"
                f"{YTDLP_EXE}"
            )

        if not check_file(
            FFMPEG_EXE
        ):

            raise RuntimeError(
                f"找不到 ffmpeg.exe：\n"
                f"{FFMPEG_EXE}"
            )

        if not check_file(
            FFPROBE_EXE
        ):

            self.log(
                "警告：FFprobe 未找到。"
            )

    def get_ua(self):

        ua = read_text_file(
            UA_FILE
        )

        if not ua:

            ua = DEFAULT_UA

        return ua

    # Cookie 新鲜度阈值（秒）：超过此时间认为可能过期
    COOKIE_STALE_THRESHOLD = 2 * 3600  # 2 小时
    COOKIE_EXPIRED_THRESHOLD = 6 * 3600  # 6 小时

    def _check_cookie_freshness(self, cookie_file):
        """检查 Cookie 文件新鲜度，返回 (age_seconds, status)
        status: 'fresh' / 'stale' / 'expired'
        """
        try:
            mtime = cookie_file.stat().st_mtime
            age = time.time() - mtime
            if age > self.COOKIE_EXPIRED_THRESHOLD:
                return age, "expired"
            elif age > self.COOKIE_STALE_THRESHOLD:
                return age, "stale"
            return age, "fresh"
        except Exception:
            return -1, "unknown"

    def get_cookie_args(self, url=None):
        """根据 URL 获取 yt-dlp 的 cookie 参数

        优先使用统一 Cookie 文件（包含浏览器全部 Cookie，yt-dlp 自动按域名匹配）
        回退到按域名分类存储，最后回退到旧的 cookies.txt
        """
        args = []

        # 优先使用统一 Cookie 文件（扩展自动同步全部 Cookie）
        all_file = self.cookies_dir / "_all_cookies.txt"
        if all_file.exists():
            age, status = self._check_cookie_freshness(all_file)
            if status == "expired":
                self.log(f"⚠ Cookie 可能已过期（{int(age/3600)} 小时前更新），请在浏览器中刷新对应网站")
            elif status == "stale":
                self.log(f"使用统一 Cookie（{int(age/60)} 分钟前更新，{all_file.stat().st_size // 1024}KB）")
            else:
                self.log(f"使用统一 Cookie（{all_file.stat().st_size // 1024}KB）")
            return ["--cookies", str(all_file)]

        # 回退：按域名分类的 Cookie
        mgr = CookieManager(self.cookies_dir)
        if url:
            root = mgr.root_domain_of(mgr._extract_host(url))
            cookie_file = self.cookies_dir / root / "_default.txt"
            if cookie_file.exists():
                age, status = self._check_cookie_freshness(cookie_file)
                if status == "expired":
                    self.log(f"⚠ Cookie 可能已过期（{int(age/3600)} 小时前更新）：{root}")
                else:
                    self.log(f"使用 Cookie：{root}")
                return ["--cookies", str(cookie_file)]

            # 尝试平台关联域名
            platform = mgr._detect_platform(root)
            for domain in PLATFORM_COOKIE_DOMAINS.get(platform, []):
                d = domain.lstrip(".")
                alt_file = self.cookies_dir / d / "_default.txt"
                if alt_file.exists():
                    age, status = self._check_cookie_freshness(alt_file)
                    if status == "expired":
                        self.log(f"⚠ Cookie 可能已过期（{int(age/3600)} 小时前更新）：{d}")
                    else:
                        self.log(f"使用 Cookie：{d}")
                    return ["--cookies", str(alt_file)]

            # CDN 域名映射
            primary = CDN_TO_PRIMARY_DOMAIN.get(root)
            if primary:
                primary_file = self.cookies_dir / primary / "_default.txt"
                if primary_file.exists():
                    age, status = self._check_cookie_freshness(primary_file)
                    if status == "expired":
                        self.log(f"⚠ Cookie 可能已过期（{int(age/3600)} 小时前更新）：{primary}")
                    else:
                        self.log(f"使用 Cookie：{primary}（CDN {root} → {primary}）")
                    return ["--cookies", str(primary_file)]

        # 回退到旧的 cookies.txt
        if COOKIE_FILE.exists():
            age, status = self._check_cookie_freshness(COOKIE_FILE)
            if status == "expired":
                self.log(f"⚠ Cookie 可能已过期（{int(age/3600)} 小时前更新）")
            self.log(f"使用旧版 Cookie：{COOKIE_FILE}")
            args.extend(["--cookies", str(COOKIE_FILE)])

        return args

    def get_video_id(self, url):

        try:

            cmd = [
                str(YTDLP_EXE),
                "--get-id",
                "--skip-download",
                "--no-warnings",
                url
            ]

            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="ignore",
                cwd=str(RUN_DIR)
            )

            if result.returncode == 0:

                values = (
                    result.stdout
                    .strip()
                    .splitlines()
                )

                if values:

                    return safe_filename(
                        values[0]
                    )

        except Exception:

            pass

        return str(
            int(time.time())
        )

    def detect_js_runtime(self):
        """检测可用的 JS 运行时（用于 yt-dlp nsig 解密）"""
        import shutil

        # 优先检测项目目录下的便携版 Node.js（打包后自带）
        if sys.platform == "win32":
            portable_node = RUN_DIR / "nodejs" / "node.exe"
            if portable_node.exists():
                self.log(f"[JS Runtime] 检测到便携版 Node.js: {portable_node}")
                return f"node:{portable_node}"

        runtimes = [
            ("node", "node"),
            ("deno", "deno"),
            ("bun", "bun"),
        ]

        for runtime, binary in runtimes:
            path = shutil.which(binary)
            if path:
                self.log(f"[JS Runtime] 检测到系统 PATH 中的 {runtime}: {path}")
                return f"{runtime}:{path}"

        # Windows 系统安装路径
        if sys.platform == "win32":
            candidates = [
                ("node", r"C:\Program Files\nodejs\node.exe"),
                ("node", r"C:\Program Files (x86)\nodejs\node.exe"),
            ]
            for runtime, path in candidates:
                if os.path.exists(path):
                    self.log(f"[JS Runtime] 检测到固定路径的 {runtime}: {path}")
                    return f"{runtime}:{path}"

        self.log("[JS Runtime] ⚠ 未检测到任何 JS 运行时")
        return None

    def build_ytdlp_args(self, url, job_dir, ua, cookie_args, player_client=None):
        """构建 yt-dlp 命令参数，支持 YouTube player_client 配置"""
        self.log(f"[DEBUG] build_ytdlp_args 被调用: {url[:60]}...")
        output_template = job_dir / "source.%(ext)s"

        cmd = [
            str(YTDLP_EXE),
            "--newline",
            "--no-warnings",
            "--encoding", "utf-8",
            "--user-agent", ua,
            "--ffmpeg-location", str(FFMPEG_EXE),
            "-f", "bv*+ba/b",
            "--merge-output-format", "mp4",
            "--windows-filenames",
            "--socket-timeout", "30",
            "--retries", "5",
            "--fragment-retries", "5",
            "--extractor-retries", "3",
            "--file-access-retries", "3",
            "--retry-sleep", "exp=1:30",
            "--buffer-size", "16M",
            "--trim-filenames", "200",
            "--skip-unavailable-fragments",
            "-o", str(output_template)
        ]

        # JS 运行时（用于 nsig 解密）
        js_runtime = self.detect_js_runtime()
        if js_runtime:
            # 先清除默认的 JS 运行时，再添加我们检测到的（避免 yt-dlp 使用低优先级的 deno）
            cmd.extend(["--no-js-runtimes", "--js-runtimes", js_runtime])
            self.log(f"[yt-dlp] 使用 JS 运行时：{js_runtime}")
        else:
            self.log("[yt-dlp] ⚠ 未检测到 JS 运行时，将使用 native Python solver（可能不稳定）")

        cmd.extend(cookie_args)

        # TikTok 特定参数：添加完整的浏览器请求头
        is_tiktok_cmd = detect_platform(url) == "tiktok"
        if is_tiktok_cmd:
            # 模拟 Chrome 浏览器的完整请求头
            cmd.extend([
                "--add-header", "Accept: text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8",
                "--add-header", "Accept-Language: en-US,en;q=0.9,zh-CN;q=0.8,zh;q=0.7",
                "--add-header", "Accept-Encoding: gzip, deflate, br",
                "--add-header", "Connection: keep-alive",
                "--add-header", "Upgrade-Insecure-Requests: 1",
                "--add-header", "Sec-Fetch-Dest: document",
                "--add-header", "Sec-Fetch-Mode: navigate",
                "--add-header", "Sec-Fetch-Site: none",
                "--add-header", "Sec-Fetch-User: ?1",
            ])
            self.log("[yt-dlp] TikTok 模式：启用完整浏览器请求头")
            
            # TikTok 使用 8 个并发片段（与 OmniGet 一致）
            cmd.extend(["-N", "8"])
            self.log("[yt-dlp] TikTok 模式：启用 8 个并发片段")

        # YouTube 特定参数
        is_youtube = detect_platform(url) == "youtube"
        if is_youtube:
            # 使用指定的 player_client，默认用 default
            client = player_client or "default"
            cmd.extend(["--extractor-args", f"youtube:player_client={client}"])
            # 限速避免被 YouTube 检测
            cmd.extend(["--throttled-rate", "100K"])

        cmd.append(url)
        return cmd

    # 网络重试配置
    NETWORK_MAX_RETRIES = 3  # 网络失败最多重试 3 次
    NETWORK_RETRY_DELAYS = [5, 10, 20]  # 重试间隔（秒），指数退避

    # 可重试的网络错误关键词
    NETWORK_ERROR_INDICATORS = [
        "timeout",
        "timed out",
        "connection reset",
        "connection refused",
        "connection aborted",
        "network is unreachable",
        "no route to host",
        "temporary failure",
        "could not resolve",
        "ssl connection",
        "eof occurred",
        "broken pipe",
        "remote end closed",
        "http error 500",
        "http error 502",
        "http error 503",
        "http error 504",
        "http error 429",
        "unable to connect",
        "urlopen error",
        "incomplete read",
        "chunked encoding",
        "failed to read",
    ]

    def _is_network_error(self, stderr_text):
        """判断是否为网络波动导致的错误（可重试）"""
        text = stderr_text.lower()
        return any(ind in text for ind in self.NETWORK_ERROR_INDICATORS)

    def _cleanup_job_dir(self, job_dir):
        """清理 job_dir 中的残留文件（重试前调用）"""
        try:
            for p in job_dir.iterdir():
                try:
                    if p.is_file():
                        p.unlink()
                except Exception:
                    pass
        except Exception:
            pass

    def download_source(self, url, job_dir):
        """下载视频源，支持网络重试（所有平台）

        单层重试：网络波动自动重试（最多 3 次）
        不再使用 YouTube player_client 轮换，避免被检测为异常行为
        """
        ua = self.get_ua()
        cookie_args = self.get_cookie_args(url)
        is_youtube = detect_platform(url) == "youtube"
        platform = detect_platform(url)
        
        # 需要预请求的平台列表（模拟用户点击，避免被识别为机器人）
        platforms_need_prefetch = ["tiktok", "instagram", "twitter", "x", "reddit"]
        
        # 预请求：模拟用户点击视频链接
        if platform in platforms_need_prefetch:
            try:
                import urllib.request
                import random
                
                self.log(f"[{platform.upper()}] 预请求视频链接...")
                req = urllib.request.Request(url)
                req.add_header("User-Agent", ua)
                req.add_header("Referer", f"https://www.{platform}.com/")
                
                # 加载 Cookie
                all_cookie_file = COOKIES_DIR / "_all_cookies.txt"
                if all_cookie_file.exists():
                    cookie_content = all_cookie_file.read_text(encoding="utf-8")
                    cookie_header = []
                    for line in cookie_content.splitlines():
                        if line.strip() and not line.startswith("#"):
                            parts = line.strip().split("\t")
                            if len(parts) >= 7 and platform in parts[0]:
                                cookie_header.append(f"{parts[5]}={parts[6]}")
                    if cookie_header:
                        req.add_header("Cookie", "; ".join(cookie_header))
                
                # 发送请求（不关心结果，只是为了让平台认为用户活跃）
                try:
                    response = urllib.request.urlopen(req, timeout=5)
                    self.log(f"[{platform.upper()}] 预请求成功 (HTTP {response.status})")
                except Exception:
                    self.log(f"[{platform.upper()}] 预请求完成")
                
                # 随机延迟 2-4 秒（给 TikTok 足够时间更新会话状态）
                delay = random.uniform(2.5,3.0)
                self.log(f"[{platform.upper()}] 等待 {delay:.1f} 秒...")
                time.sleep(delay)
            except Exception as e:
                self.log(f"[{platform.upper()}] 预请求失败（忽略）: {e}")

        # 网络重试循环
        for net_attempt in range(self.NETWORK_MAX_RETRIES + 1):
            if self.stop_flag or self.skip_flag:
                return False

            # 网络重试时清理残留文件，确保 yt-dlp 从头开始
            if net_attempt > 0:
                delay = self.NETWORK_RETRY_DELAYS[min(net_attempt - 1, len(self.NETWORK_RETRY_DELAYS) - 1)]
                self.log(f"")
                self.log(f"[网络重试 {net_attempt}/{self.NETWORK_MAX_RETRIES}] 等待 {delay} 秒后重试...")
                time.sleep(delay)
                self._cleanup_job_dir(job_dir)
                # 重新获取 cookie（可能已刷新）
                cookie_args = self.get_cookie_args(url)

            cmd = self.build_ytdlp_args(url, job_dir, ua, cookie_args, player_client=None)

            self.log("")
            if net_attempt == 0:
                self.log("开始下载：")
            else:
                self.log(f"网络重试下载（第 {net_attempt} 次）：")
            
            # 调试：显示 URL 编码信息
            import urllib.parse
            parsed = urllib.parse.urlparse(url)
            self.log(f"[DEBUG] URL: {url}")
            self.log(f"[DEBUG] 用户名: {parsed.path.split('/')[1] if '/' in parsed.path else 'N/A'}")
            self.log(f"[DEBUG] URL 长度: {len(url)}")
            self.log(url)

            process = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace",
                cwd=str(RUN_DIR),
                bufsize=1
            )

            stderr_output = []

            for line in iter(process.stdout.readline, ""):
                if self.stop_flag or self.skip_flag:
                    try:
                        process.terminate()
                    except Exception:
                        pass
                    return False

                line = line.rstrip()
                if line:
                    stderr_output.append(line)
                    self.log(line)

                    if "%" in line:
                        try:
                            progress = int(line.split("%")[0].split()[-1])
                            if 0 <= progress <= 100:
                                self.progress_signal.emit(progress)
                        except ValueError:
                            pass

            process.wait()

            if process.returncode == 0:
                return True

            # 分析错误原因
            stderr_text = "\n".join(stderr_output).lower()

            # 判断是否可重试的网络错误
            if self._is_network_error(stderr_text):
                if net_attempt < self.NETWORK_MAX_RETRIES:
                    self.log(f"[网络] 下载失败（疑似网络波动），将自动重试")
                    continue  # 继续下一次网络重试
                else:
                    self.log(f"[网络] 已重试 {self.NETWORK_MAX_RETRIES} 次仍然失败")
                    return False

            # TikTok 特定错误：提示用户刷新 Cookie
            is_tiktok = detect_platform(url) == "tiktok"
            tiktok_errors = [
                "unable to extract universal data",
                "unexpected response from webpage",
                "http error 403",
            ]
            if is_tiktok and any(err in stderr_text for err in tiktok_errors):
                self.log(f"")
                self.log(f"[TikTok] ⚠ 下载失败，可能是 Cookie 过期或 TikTok 反爬机制拦截")
                self.log(f"[TikTok] 建议操作：")
                self.log(f"  1. 在浏览器中打开 TikTok 并刷新页面")
                self.log(f"  2. 等待扩展自动同步新 Cookie（约 5-10 秒）")
                self.log(f"  3. 右键点击失败的链接 → 重试下载")
                self.log(f"")

            # 其他错误（不可重试）直接返回
            return False

        return False

    def find_source_file(
        self,
        job_dir
    ):

        files = list(
            job_dir.glob(
                "*.mp4"
            )
        )

        if files:

            return files[0]

        files = [
            p
            for p in job_dir.iterdir()
            if (
                p.is_file()
                and
                p.suffix.lower()
                in (
                    ".mkv",
                    ".webm",
                    ".mov",
                    ".mp4"
                )
            )
        ]

        if files:

            return files[0]

        return None

    def probe_video(
        self,
        file_path
    ):

        if not check_file(
            FFPROBE_EXE
        ):

            return "", ""

        try:

            cmd = [
                str(FFPROBE_EXE),
                "-v",
                "error",
                "-select_streams",
                "v:0",
                "-show_entries",
                "stream=width,height",
                "-of",
                "csv=s=x:p=0",
                str(file_path)
            ]

            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="ignore"
            )

            value = result.stdout.strip()

            if "x" in value:

                w, h = value.split(
                    "x",
                    1
                )

                return w.strip(), h.strip()

        except Exception:

            pass

        return "", ""

    def get_video_codec_args(self):

        codec = self.video_codec

        if codec == "H.264 (libx264)":

            return [
                "-c:v",
                "libx264"
            ]

        if codec == "H.264 (NVIDIA NVENC)":

            return [
                "-c:v",
                "h264_nvenc"
            ]

        if codec == "H.264 (Intel QSV)":

            return [
                "-c:v",
                "h264_qsv"
            ]

        if codec == "H.264 (AMD AMF)":

            return [
                "-c:v",
                "h264_amf"
            ]

        if codec == "H.265 (libx265)":

            return [
                "-c:v",
                "libx265"
            ]

        if codec == "H.265 (NVIDIA NVENC)":

            return [
                "-c:v",
                "hevc_nvenc"
            ]

        if codec == "H.265 (Intel QSV)":

            return [
                "-c:v",
                "hevc_qsv"
            ]

        if codec == "H.265 (AMD AMF)":

            return [
                "-c:v",
                "hevc_amf"
            ]

        if codec == "VP9 (libvpx-vp9)":

            return [
                "-c:v",
                "libvpx-vp9"
            ]

        if codec == "AV1 (libsvtav1)":

            return [
                "-c:v",
                "libsvtav1"
            ]

        if codec == "AV1 (NVIDIA NVENC)":

            return [
                "-c:v",
                "av1_nvenc"
            ]

        if codec == "AV1 (Intel QSV)":

            return [
                "-c:v",
                "av1_qsv"
            ]

        if codec == "AV1 (AMD AMF)":

            return [
                "-c:v",
                "av1_amf"
            ]

        return [
            "-c:v",
            "libx264"
        ]

    def convert_video(
        self,
        source_file,
        final_file
    ):

        vf = (
            f"scale={self.target_w}:"
            f"{self.target_h}:"
            f"force_original_aspect_ratio=increase,"
            f"crop={self.target_w}:"
            f"{self.target_h}:"
            f"(iw-{self.target_w})/2:"
            f"(ih-{self.target_h})/2"
        )

        codec_args = (
            self.get_video_codec_args()
        )

        cmd = [
            str(FFMPEG_EXE),

            "-y",

            "-i",
            str(source_file),

            "-map",
            "0:v:0",

            "-map",
            "0:a:0?",

            "-vf",
            vf
        ]

        cmd.extend(
            codec_args
        )

        if self.video_codec in (
                "H.264 (libx264)",
                "H.265 (libx265)"
        ):

            cmd.extend(
                [
                    "-preset",
                    "medium",

                    "-crf",
                    "18"
                ]
            )

        elif self.video_codec in (
                "H.264 (NVIDIA NVENC)",
                "H.265 (NVIDIA NVENC)"
        ):

            cmd.extend(
                [
                    "-preset",
                    "medium",

                    "-cq",
                    "18",

                    "-b:v",
                    "0"
                ]
            )

        elif self.video_codec in (
                "H.264 (Intel QSV)",
                "H.265 (Intel QSV)"
        ):

            cmd.extend(
                [
                    "-global_quality",
                    "18"
                ]
            )

        elif self.video_codec in (
                "H.264 (AMD AMF)",
                "H.265 (AMD AMF)"
        ):

            cmd.extend(
                [
                    "-quality",
                    "quality",

                    "-qp_i",
                    "18",

                    "-qp_p",
                    "18"
                ]
            )

        elif self.video_codec == "VP9 (libvpx-vp9)":

            cmd.extend(
                [
                    "-b:v",
                    "0",

                    "-crf",
                    "30"
                ]
            )

        elif self.video_codec == "AV1 (libsvtav1)":

            cmd.extend(
                [
                    "-crf",
                    "30"
                ]
            )

        elif self.video_codec == "AV1 (NVIDIA NVENC)":

            cmd.extend(
                [
                    "-preset",
                    "medium",

                    "-cq",
                    "25",

                    "-b:v",
                    "0"
                ]
            )

        elif self.video_codec == "AV1 (Intel QSV)":

            cmd.extend(
                [
                    "-global_quality",
                    "25"
                ]
            )

        elif self.video_codec == "AV1 (AMD AMF)":

            cmd.extend(
                [
                    "-quality",
                    "quality"
                ]
            )

        elif "VP9" in self.video_codec:

            cmd.extend(
                [
                    "-b:v",
                    "0",

                    "-crf",
                    "30"
                ]
            )

        elif "AV1" in self.video_codec:

            cmd.extend(
                [
                    "-crf",
                    "30"
                ]
            )

        cmd.extend(
            [
                "-pix_fmt",
                "yuv420p",

                "-c:a",
                "aac",

                "-b:a",
                "192k",

                "-ar",
                "48000",

                "-movflags",
                "+faststart",

                str(final_file)
            ]
        )

        self.log("")

        self.log(
            "开始 FFmpeg 转换..."
        )

        self.log(
            f"输出尺寸："
            f"{self.target_w} × "
            f"{self.target_h}"
        )

        self.log(
            f"视频编码："
            f"{self.video_codec}"
        )

        process = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            cwd=str(RUN_DIR),
            bufsize=1
        )

        for line in iter(
            process.stdout.readline,
            ""
        ):

            if self.stop_flag or self.skip_flag:

                try:

                    process.terminate()

                except Exception:

                    pass

                return False

            line = line.rstrip()

            if line:

                if (
                    "frame=" in line
                    or
                    "time=" in line
                    or
                    "error" in line.lower()
                    or
                    "failed" in line.lower()
                ):

                    self.log(line)

        process.wait()

        return (
            process.returncode == 0
        )

    def verify_video(
        self,
        file_path
    ):

        if not check_file(
            FFPROBE_EXE
        ):

            return True

        try:

            cmd = [
                str(FFPROBE_EXE),

                "-v",
                "error",

                "-show_entries",
                "stream=codec_type",

                "-of",
                "csv=p=0",

                str(file_path)
            ]

            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="ignore"
            )

            text = (
                result.stdout
                .lower()
            )

            has_video = (
                "video" in text
            )

            has_audio = (
                "audio" in text
            )

            self.log(
                f"视频轨："
                f"{'存在' if has_video else '不存在'}"
            )

            self.log(
                f"音频轨："
                f"{'存在' if has_audio else '不存在'}"
            )

            return has_video

        except Exception as e:

            self.log(
                f"FFprobe 检测失败：{e}"
            )

            return False

    def process_one(
        self,
        url,
        index,
        total
    ):

        self.current_url = url

        self.current_signal.emit(
            index,
            total,
            url
        )

        platform = detect_platform(
            url
        )

        platform_dir = (
            self.output_dir /
            platform
        )

        platform_dir.mkdir(
            parents=True,
            exist_ok=True
        )

        job_dir = (
            TEMP_DIR /
            f"job_{index}_{int(time.time())}"
        )

        job_dir.mkdir(
            parents=True,
            exist_ok=True
        )

        try:

            self.log("")

            self.log(
                "=" * 60
            )

            self.log(
                f"[下载] "
                f"{index} / {total}"
            )

            self.log(
                f"[下载] 平台：{platform}"
            )

            self.log(url)

            ok = self.download_source(
                url,
                job_dir
            )

            if not ok:

                raise RuntimeError(
                    "yt-dlp 下载失败"
                )

            source_file = (
                self.find_source_file(
                    job_dir
                )
            )

            if source_file is None:

                raise RuntimeError(
                    "下载完成，但找不到视频文件"
                )

            self.log(
                f"原始文件："
                f"{source_file.name}"
            )

            w, h = self.probe_video(
                source_file
            )

            if w and h:

                self.log(
                    f"原始尺寸："
                    f"{w} × {h}"
                )

            video_id = (
                self.get_video_id(
                    url
                )
            )

            final_file = (
                platform_dir /
                f"video_{video_id}.mp4"
            )

            number = 1

            while final_file.exists():

                final_file = (
                    platform_dir /
                    f"video_{video_id}_{number}.mp4"
                )

                number += 1

            ok = self.convert_video(
                source_file,
                final_file
            )

            if not ok:

                raise RuntimeError(
                    "FFmpeg 转换失败"
                )

            if not final_file.exists():

                raise RuntimeError(
                    "最终文件不存在"
                )

            size = (
                final_file.stat()
                .st_size
            )

            if size <= 0:

                raise RuntimeError(
                    "最终文件大小为 0"
                )

            if not self.verify_video(
                final_file
            ):

                raise RuntimeError(
                    "最终视频验证失败"
                )

            self.log("")

            self.log(
                "[完成] 处理成功"
            )

            self.log(
                f"[完成] 最终文件："
                f"{final_file}"
            )

            self.log(
                f"[完成] 文件大小："
                f"{size / 1024 / 1024:.2f} MB"
            )

            return True

        except Exception as e:

            self.log("")

            self.log(
                f"[失败] 处理失败：{e}"
            )

            return False

        finally:

            try:

                if job_dir.exists():

                    for p in job_dir.iterdir():

                        try:

                            if p.is_file():

                                p.unlink()

                        except Exception:

                            pass

                    try:

                        job_dir.rmdir()

                    except Exception:

                        pass

            except Exception:

                pass

    def run(self):

        try:

            ensure_dirs()

            self.output_dir.mkdir(
                parents=True,
                exist_ok=True
            )

            self.check_dependencies()

            total = len(
                self.urls
            )

            if total == 0:

                self.finished_signal.emit(
                    0,
                    0
                )

                return

            for index, url in enumerate(
                self.urls,
                1
            ):

                if self.stop_flag:

                    break

                url = url.strip()

                if not url:
                    continue

                # 已被手动删除的链接直接跳过
                if url in self.cancelled_urls:

                    self.log(
                        f"[已取消] 跳过已删除链接："
                        f"{url[:60]}"
                    )

                    continue

                self.skip_flag = False

                ok = self.process_one(
                    url,
                    index,
                    total
                )

                # 下载中被手动删除：不计入成败，不通知 UI
                if url in self.cancelled_urls:

                    self.log(
                        f"[已取消] 已中断：{url[:60]}"
                    )

                else:

                    # 发送单个 URL 完成信号
                    self.url_finished_signal.emit(url, ok)

                    if ok:

                        self.success += 1

                    else:

                        self.failed += 1

                progress = int(
                    index /
                    total *
                    100
                )

                self.progress_signal.emit(
                    progress
                )

            self.finished_signal.emit(
                self.success,
                self.failed
            )

        except Exception as e:

            self.log(
                f"下载任务异常：{e}"
            )

            self.finished_signal.emit(
                self.success,
                self.failed + 1
            )


# ============================================================
# 自定义链接列表
# ============================================================

class LinkListWidget(QListWidget):

    urls_changed = pyqtSignal()

    # 用户手动删除链接时发射（携带被删除的 URL 列表）
    urls_removed = pyqtSignal(list)

    # 用户右键重试失败链接时发射（携带需要重试的 URL 列表）
    retry_download = pyqtSignal(list)

    def __init__(
        self,
        parent=None
    ):

        super().__init__(
            parent
        )

        self.setAcceptDrops(
            True
        )

        self.setDragEnabled(
            False
        )

        self.setSelectionMode(
            QAbstractItemView.ExtendedSelection
        )

        self.setContextMenuPolicy(
            Qt.CustomContextMenu
        )

        self.customContextMenuRequested.connect(
            self.show_context_menu
        )

    def dragEnterEvent(
        self,
        event
    ):

        if (
            event.mimeData().hasUrls()
            or
            event.mimeData().hasText()
        ):

            event.acceptProposedAction()

        else:

            event.ignore()

    def dragMoveEvent(
        self,
        event
    ):

        if (
            event.mimeData().hasUrls()
            or
            event.mimeData().hasText()
        ):

            event.acceptProposedAction()

        else:

            event.ignore()

    def dropEvent(
        self,
        event
    ):

        added = False

        mime = event.mimeData()

        if mime.hasUrls():

            for url in mime.urls():

                if url.isLocalFile():

                    path = Path(
                        url.toLocalFile()
                    )

                    if (
                        path.is_file()
                        and
                        path.suffix.lower()
                        == ".txt"
                    ):

                        try:

                            text = path.read_text(
                                encoding="utf-8",
                                errors="ignore"
                            )

                            if self.add_text(
                                text
                            ):

                                added = True

                        except Exception:
                            pass

                    else:

                        try:

                            if path.is_file():

                                text = path.read_text(
                                    encoding="utf-8",
                                    errors="ignore"
                                )

                                if self.add_text(
                                    text
                                ):

                                    added = True

                        except Exception:
                            pass

                else:

                    text = url.toString()

                    if is_url(text):

                        if self.add_url(
                            text
                        ):

                            added = True

        if mime.hasText():

            text = mime.text()

            if self.add_text(
                text
            ):

                added = True

        if added:

            self.urls_changed.emit()

        event.acceptProposedAction()

    def add_url(
        self,
        url
    ):

        url = url.strip()

        if not is_url(url):
            return False

        existing = set(
            self.get_urls()
        )

        if url in existing:

            return False

        self.addItem(
            url
        )

        return True

    def remove_url(
        self,
        url
    ):
        """从列表中移除指定 URL"""
        url = url.strip()
        if not url:
            return False

        for i in range(self.count()):
            item = self.item(i)
            if item and item.text().strip() == url:
                self.takeItem(i)
                self.urls_changed.emit()
                return True

        return False

    def add_text(
        self,
        text
    ):

        added = False

        for url in parse_text_urls(
            text
        ):

            if self.add_url(
                url
            ):

                added = True

        return added

    def get_urls(self):
        urls = [
            self.item(i).text().strip()
            for i in range(
                self.count()
            )
            if self.item(i)
            and
            self.item(i).text().strip()
        ]
        # 调试：打印 URL 长度
        if urls:
            print(f"[DEBUG] get_urls() 返回 {len(urls)} 个 URL，第一个 URL 长度: {len(urls[0])}", flush=True)
        return urls

    def paste_text(self):

        clipboard = (
            QApplication.clipboard()
        )

        text = clipboard.text()

        if not text:
            return

        if self.add_text(
            text
        ):

            self.urls_changed.emit()

    def _get_failed_urls_from_selection(self):
        """从当前选中项中找出失败的（红色）链接"""
        failed = []
        for item in self.selectedItems():
            fg = item.foreground()
            if fg and fg.color() and fg.color().name().lower() == "#ff6b6b":
                failed.append(item.text().strip())
        return failed

    def show_context_menu(
        self,
        pos
    ):

        menu = QMenu(
            self
        )

        action_paste = QAction(
            "粘贴",
            self
        )

        action_delete = QAction(
            "删除选中",
            self
        )

        action_select_all = QAction(
            "全选",
            self
        )

        action_clear = QAction(
            "清空",
            self
        )

        action_copy = QAction(
            "复制选中",
            self
        )

        action_retry = QAction(
            "重试下载",
            self
        )

        # 仅当选中有失败（红色）链接时才启用重试
        failed_urls = self._get_failed_urls_from_selection()
        action_retry.setEnabled(len(failed_urls) > 0)

        menu.addAction(
            action_paste
        )

        menu.addSeparator()

        menu.addAction(
            action_copy
        )

        menu.addAction(
            action_delete
        )

        menu.addAction(
            action_select_all
        )

        menu.addSeparator()

        menu.addAction(
            action_retry
        )

        menu.addSeparator()

        menu.addAction(
            action_clear
        )

        action_paste.triggered.connect(
            self.paste_text
        )

        action_delete.triggered.connect(
            self.delete_selected
        )

        action_select_all.triggered.connect(
            self.selectAll
        )

        action_clear.triggered.connect(
            self.clear_with_signal
        )

        action_copy.triggered.connect(
            self.copy_selected
        )

        action_retry.triggered.connect(
            lambda: self.retry_download.emit(failed_urls)
        )

        menu.exec_(
            self.mapToGlobal(pos)
        )

    def delete_selected(self):

        rows = sorted(
            [
                index.row()
                for index in
                self.selectedIndexes()
            ],
            reverse=True
        )

        removed = []

        for row in rows:

            item = self.item(row)

            if item:

                removed.append(
                    item.text().strip()
                )

            self.takeItem(
                row
            )

        if rows:

            self.urls_changed.emit()

            if removed:

                self.urls_removed.emit(
                    removed
                )

    def copy_selected(self):

        values = [
            item.text()
            for item in
            self.selectedItems()
        ]

        if values:

            QApplication.clipboard().setText(
                "\n".join(values)
            )

    def clear_with_signal(self):

        if self.count():

            removed = self.get_urls()

            self.clear()

            self.urls_changed.emit()

            if removed:

                self.urls_removed.emit(
                    removed
                )

    def keyPressEvent(
        self,
        event
    ):

        if (
            event.matches(
                QKeySequence.Paste
            )
        ):

            self.paste_text()

            return

        if event.key() == Qt.Key_Delete:

            self.delete_selected()

            return

        if (
            event.key() == Qt.Key_A
            and
            event.modifiers()
            & Qt.ControlModifier
        ):

            self.selectAll()

            return

        super().keyPressEvent(
            event
        )


# ============================================================
# 主窗口
# ============================================================

class MainWindow(QMainWindow):

    # 桥接服务器线程安全信号
    bridge_log_signal = pyqtSignal(str)
    bridge_url_signal = pyqtSignal(str)
    bridge_cookies_signal = pyqtSignal(list, str, str)  # cookies, ua, source_url
    bridge_ua_signal = pyqtSignal(str)

    def __init__(self):

        super().__init__()

        self.setWindowIcon(
            QIcon(str(BASE_DIR / "icon.png"))
        )

        self.config = ConfigManager(
            CONFIG_FILE
        )

        self.download_thread = None

        # 自动下载队列状态
        self.tried_urls = set()
        self.current_download_url = ""
        self._stopped_by_user = False
        self._auto_mode = False

        # 内存指纹库（基于输出目录 MP4 文件扫描）
        self.fingerprint_set = set()
        self._total_mp4_count = 0
        self._valid_fingerprint_count = 0
        self._no_fingerprint_files = []
        self._stat_total = 0
        self._stat_new = 0
        self._stat_skipped = 0
        self._stat_failed = 0

        self._restoring = True

        self.setWindowTitle(
            "🌎  视频批量下载工具 🌎"
        )

        self.setup_window()

        self.setup_ui()

        self.load_config_to_ui()

        self._restoring = False

        self.load_status_to_log()

        # 启动 OmniGet 桥接服务器
        self.setup_bridge()

        self.install_auto_save()

        # 启动后自动扫描输出目录一次
        self._build_fingerprint_set()

    def setup_window(self):

        window_cfg = self.config.get(
            "window",
            {}
        )

        width = int(
            window_cfg.get(
                "width",
                900
            )
            or 900
        )

        height = int(
            window_cfg.get(
                "height",
                650
            )
            or 650
        )

        self.resize(
            width,
            height
        )

        x = window_cfg.get(
            "x"
        )

        y = window_cfg.get(
            "y"
        )

        if (
            isinstance(x, int)
            and
            isinstance(y, int)
        ):

            self.move(
                x,
                y
            )

        self.setMinimumSize(
            760,
            560
        )

    def setup_ui(self):

        central = QWidget()

        self.setCentralWidget(
            central
        )

        main_layout = QVBoxLayout(
            central
        )

        main_layout.setContentsMargins(
            14,
            12,
            14,
            12
        )

        main_layout.setSpacing(
            9
        )

        top_layout = QHBoxLayout()

        title = QLabel(
            "                                               🎬        视频批量下载工具       🎬"
        )

        title.setObjectName(
            "title"
        )

        title.setFont(
            QFont(
                "Microsoft YaHei",
                18,
                QFont.Bold
            )
        )

        top_layout.addWidget(
            title
        )

        top_layout.addStretch()

        self.lbl_status = QLabel(
            "就绪"
        )

        self.lbl_status.setObjectName(
            "status"
        )

        top_layout.addWidget(
            self.lbl_status
        )

        main_layout.addLayout(
            top_layout
        )

        setting_frame = QFrame()

        setting_frame.setObjectName(
            "settingFrame"
        )

        setting_layout = QHBoxLayout(
            setting_frame
        )

        setting_layout.setContentsMargins(
            10,
            7,
            10,
            7
        )

        size_label = QLabel(
            "尺寸"
        )

        size_label.setObjectName(
            "smallLabel"
        )

        self.size_combo = QComboBox()

        self.size_combo.addItem(
            "810 × 1080",
            (810, 1080)
        )

        self.size_combo.addItem(
            "1080 × 1440",
            (1080, 1440)
        )

        self.size_combo.addItem(
            "720 × 1280",
            (720, 1280)
        )

        self.size_combo.addItem(
            "1080 × 1920",
            (1080, 1920)
        )

        self.size_combo.addItem(
            "720 × 720",
            (720, 720)
        )

        self.size_combo.addItem(
            "1080 × 1080",
            (1080, 1080)
        )

        self.size_combo.addItem(
            "1920 × 1080",
            (1920, 1080)
        )

        setting_layout.addWidget(
            size_label
        )

        setting_layout.addWidget(
            self.size_combo
        )

        codec_label = QLabel(
            "编码"
        )

        codec_label.setObjectName(
            "smallLabel"
        )

        self.codec_combo = QComboBox()

        self.codec_combo.addItems(
            [
                "H.264 (libx264)",
                "H.264 (NVIDIA NVENC)",
                "H.264 (Intel QSV)",
                "H.264 (AMD AMF)",

                "H.265 (libx265)",
                "H.265 (NVIDIA NVENC)",
                "H.265 (Intel QSV)",
                "H.265 (AMD AMF)",

                "VP9 (libvpx-vp9)",

                "AV1 (libsvtav1)",
                "AV1 (NVIDIA NVENC)",
                "AV1 (Intel QSV)",
                "AV1 (AMD AMF)"
            ]
        )

        setting_layout.addSpacing(
            10
        )

        setting_layout.addWidget(
            codec_label
        )

        setting_layout.addWidget(
            self.codec_combo
        )

        setting_layout.addStretch()

        # ====================================================
        # 输出目录
        # ====================================================

        output_label = QLabel(
            "输出"
        )

        output_label.setObjectName(
            "smallLabel"
        )

        # 使用支持文件夹拖放的输入框
        self.output_edit = OutputFolderLineEdit(
            self
        )

        # 保持可编辑
        self.output_edit.setReadOnly(
            False
        )

        self.output_edit.setMinimumWidth(
            220
        )

        # 文件夹拖放成功后自动保存
        self.output_edit.folder_dropped.connect(
            self.output_folder_dropped
        )

        self.btn_output = QPushButton(
            "选择"
        )

        self.btn_output.clicked.connect(
            self.select_output_dir
        )

        self.btn_scan = QPushButton(
            "扫描"
        )

        self.btn_scan.clicked.connect(
            self._do_scan
        )

        setting_layout.addWidget(
            output_label
        )

        setting_layout.addWidget(
            self.output_edit
        )

        setting_layout.addWidget(
            self.btn_output
        )

        setting_layout.addWidget(
            self.btn_scan
        )

        main_layout.addWidget(
            setting_frame
        )

        # ====================================================
        # OmniGet 扩展桥接
        # ====================================================

        bridge_frame = QFrame()

        bridge_frame.setObjectName(
            "settingFrame"
        )

        bridge_layout = QHBoxLayout(
            bridge_frame
        )

        bridge_layout.setContentsMargins(
            10,
            7,
            10,
            7
        )

        bridge_label = QLabel(
            "🔗 好帮手 扩展"
        )

        bridge_label.setObjectName(
            "smallLabel"
        )

        bridge_layout.addWidget(
            bridge_label
        )

        self.lbl_bridge_status = QLabel(
            "未启动"
        )

        self.lbl_bridge_status.setObjectName(
            "smallLabel"
        )

        bridge_layout.addWidget(
            self.lbl_bridge_status
        )

        bridge_layout.addSpacing(
            10
        )

        token_label = QLabel(
            "Token:"
        )

        token_label.setObjectName(
            "smallLabel"
        )

        bridge_layout.addWidget(
            token_label
        )

        self.txt_bridge_token = QLineEdit()

        self.txt_bridge_token.setReadOnly(
            True
        )

        self.txt_bridge_token.setMinimumWidth(
            200
        )

        bridge_layout.addWidget(
            self.txt_bridge_token
        )

        self.btn_bridge_pair = QPushButton(
            "配对"
        )

        self.btn_bridge_pair.clicked.connect(
            self.start_bridge_pairing
        )

        bridge_layout.addWidget(
            self.btn_bridge_pair
        )

        self.btn_bridge_copy = QPushButton(
            "复制 Token"
        )

        self.btn_bridge_copy.clicked.connect(
            self.copy_bridge_token
        )

        bridge_layout.addWidget(
            self.btn_bridge_copy
        )

        bridge_layout.addStretch()

        main_layout.addWidget(
            bridge_frame
        )

        link_frame = QFrame()

        link_frame.setObjectName(
            "linkFrame"
        )

        link_layout = QVBoxLayout(
            link_frame
        )

        link_layout.setContentsMargins(
            10,
            8,
            10,
            8
        )

        link_layout.setSpacing(
            6
        )

        link_top = QHBoxLayout()

        link_title = QLabel(
            "视频链接"
        )

        link_title.setObjectName(
            "sectionTitle"
        )

        link_top.addWidget(
            link_title
        )

        self.lbl_count = QLabel(
            "0 条"
        )

        self.lbl_count.setObjectName(
            "countLabel"
        )

        link_top.addWidget(
            self.lbl_count
        )

        link_top.addStretch()

        self.btn_open_output = QPushButton(
            "打开输出文件夹"
        )

        self.btn_open_output.clicked.connect(
            self.open_output_folder
        )

        link_top.addWidget(
            self.btn_open_output
        )

        link_layout.addLayout(
            link_top
        )

        self.link_list = LinkListWidget()

        self.link_list.setObjectName(
            "linkList"
        )

        self.link_list.setMinimumHeight(
            115
        )

        self.link_list.setMaximumHeight(
            160
        )

        self.link_list.urls_changed.connect(
            self.links_changed
        )

        self.link_list.urls_removed.connect(
            self.on_urls_removed
        )

        self.link_list.retry_download.connect(
            self.on_retry_download
        )

        link_layout.addWidget(
            self.link_list
        )

        hint = QLabel(
            "支持拖入 URL / TXT，也可直接 Ctrl+V 粘贴链接或 TXT 内容"
        )

        hint.setObjectName(
            "hint"
        )

        link_layout.addWidget(
            hint
        )

        main_layout.addWidget(
            link_frame
        )

        control_layout = QHBoxLayout()

        self.btn_download = QPushButton(
            "▶  开始下载"
        )

        self.btn_download.setObjectName(
            "primaryButton"
        )

        self.btn_download.clicked.connect(
            lambda: self.start_download()
        )

        self.btn_stop = QPushButton(
            "■  停止"
        )

        self.btn_stop.setEnabled(
            False
        )

        self.btn_stop.clicked.connect(
            self.stop_download
        )

        control_layout.addWidget(
            self.btn_download
        )

        control_layout.addWidget(
            self.btn_stop
        )

        control_layout.addStretch()

        main_layout.addLayout(
            control_layout
        )

        progress_layout = QHBoxLayout()

        self.progress = QProgressBar()

        self.progress.setValue(
            0
        )

        self.progress.setTextVisible(
            True
        )

        self.lbl_current = QLabel(
            "等待任务"
        )

        self.lbl_current.setMinimumWidth(
            230
        )

        progress_layout.addWidget(
            self.progress
        )

        progress_layout.addWidget(
            self.lbl_current
        )

        main_layout.addLayout(
            progress_layout
        )

        # ========================================================
        # 统计信息区域
        # ========================================================

        stats_layout = QHBoxLayout()

        self.lbl_stat_videos = QLabel(
            "目录视频：0"
        )

        self.lbl_stat_videos.setObjectName(
            "smallLabel"
        )

        stats_layout.addWidget(
            self.lbl_stat_videos
        )

        stats_layout.addSpacing(
            15
        )

        self.lbl_stat_fps = QLabel(
            "有效指纹：0"
        )

        self.lbl_stat_fps.setObjectName(
            "smallLabel"
        )

        stats_layout.addWidget(
            self.lbl_stat_fps
        )

        stats_layout.addSpacing(
            15
        )

        self.lbl_stat_tasks = QLabel(
            "总数：0  新增：0  已跳过：0  失败：0"
        )

        self.lbl_stat_tasks.setObjectName(
            "smallLabel"
        )

        stats_layout.addWidget(
            self.lbl_stat_tasks
        )

        stats_layout.addStretch()

        main_layout.addLayout(
            stats_layout
        )

        self.log_edit = QTextEdit()

        self.log_edit.setObjectName(
            "logEdit"
        )

        self.log_edit.setReadOnly(
            True
        )

        self.log_edit.setContextMenuPolicy(
            Qt.CustomContextMenu
        )

        self.log_edit.customContextMenuRequested.connect(
            self.show_log_context_menu
        )

        self.log_edit.setMinimumHeight(
            220
        )

        main_layout.addWidget(
            self.log_edit,
            1
        )

    def apply_style(self):

        self.setStyleSheet(
            """
            QMainWindow {
                background: #2b2b2b;
            }

            QWidget {
                color: #dedede;
                font-family: "Microsoft YaHei";
                font-size: 13px;
            }

            QLabel#title {
                color: #f1f1f1;
                font-size: 18px;
                font-weight: bold;
            }

            QLabel#status {
                color: #9a9a9a;
                padding-right: 4px;
            }

            QLabel#smallLabel {
                color: #aaaaaa;
            }

            QLabel#sectionTitle {
                color: #eeeeee;
                font-size: 13px;
                font-weight: bold;
            }

            QLabel#countLabel {
                color: #888888;
                margin-left: 8px;
            }

            QLabel#hint {
                color: #777777;
                font-size: 11px;
            }

            QFrame#settingFrame,
            QFrame#linkFrame {
                background: #323232;
                border: 1px solid #424242;
                border-radius: 6px;
            }

            QLineEdit,
            QComboBox {
                background: #242424;
                border: 1px solid #4a4a4a;
                border-radius: 4px;
                color: #dddddd;
                padding: 5px 8px;
                min-height: 27px;
            }

            QLineEdit:focus,
            QComboBox:focus {
                border: 1px solid #666666;
            }

            QComboBox QAbstractItemView {
                background: #303030;
                color: #eeeeee;
                selection-background-color: #505050;
                border: 1px solid #555555;
            }

            QPushButton {
                background: #414141;
                border: 1px solid #555555;
                border-radius: 4px;
                color: #dddddd;
                padding: 6px 12px;
                min-height: 28px;
            }

            QPushButton:hover {
                background: #4b4b4b;
            }

            QPushButton:pressed {
                background: #353535;
            }

            QPushButton:disabled {
                background: #303030;
                color: #666666;
                border-color: #3a3a3a;
            }

            QPushButton#primaryButton {
                background: #555555;
                border: 1px solid #707070;
                color: #ffffff;
                font-weight: bold;
                padding-left: 20px;
                padding-right: 20px;
            }

            QPushButton#primaryButton:hover {
                background: #626262;
            }

            QListWidget#linkList {
                background: #242424;
                border: 1px solid #444444;
                border-radius: 4px;
                color: #dddddd;
                padding: 4px;
            }

            QListWidget#linkList::item {
                padding: 5px;
                border-radius: 3px;
            }

            QListWidget#linkList::item:selected {
                background: #505050;
                color: #ffffff;
            }

            QTextEdit#logEdit {
                background: #202020;
                border: 1px solid #414141;
                border-radius: 4px;
                color: #bcbcbc;
                padding: 7px;
                font-family: Consolas, "Microsoft YaHei";
                font-size: 12px;
            }

            QProgressBar {
                background: #202020;
                border: 1px solid #414141;
                border-radius: 4px;
                text-align: center;
                color: #dddddd;
                height: 20px;
            }

            QProgressBar::chunk {
                background: #666666;
                border-radius: 3px;
            }

            QMenu {
                background: #303030;
                color: #dddddd;
                border: 1px solid #555555;
            }

            QMenu::item {
                padding: 7px 28px;
            }

            QMenu::item:selected {
                background: #4b4b4b;
            }

            QScrollBar:vertical {
                background: #252525;
                width: 10px;
            }

            QScrollBar::handle:vertical {
                background: #555555;
                border-radius: 5px;
            }

            QScrollBar::add-line:vertical,
            QScrollBar::sub-line:vertical {
                height: 0px;
            }
            """
        )

    def load_config_to_ui(self):

        output_dir = self.config.get(
            "output_dir",
            str(DEFAULT_OUTPUT_DIR)
        )

        self.output_edit.setText(
            output_dir
        )

        size_value = self.config.get(
            "video_size",
            "810x1080"
        )

        for i in range(
            self.size_combo.count()
        ):

            data = self.size_combo.itemData(
                i
            )

            if data:

                value = (
                    f"{data[0]}x{data[1]}"
                )

                if value == size_value:

                    self.size_combo.setCurrentIndex(
                        i
                    )

                    break

        codec = self.config.get(
            "video_codec",
            "H.264 (libx264)"
        )

        index = self.codec_combo.findText(
            codec
        )

        if index >= 0:

            self.codec_combo.setCurrentIndex(
                index
            )

        links = self.config.get(
            "links",
            []
        )

        if isinstance(
            links,
            list
        ):

            for url in links:

                if is_url(
                    str(url)
                ):

                    self.link_list.add_url(
                        str(url)
                    )

        self.update_count()

    def install_auto_save(self):

        self.size_combo.currentIndexChanged.connect(
            self.auto_save_settings
        )

        self.codec_combo.currentIndexChanged.connect(
            self.auto_save_settings
        )

        self.output_edit.textChanged.connect(
            self.auto_save_settings
        )

        self.save_timer = QTimer(
            self
        )

        self.save_timer.setSingleShot(
            True
        )

        self.save_timer.setInterval(
            300
        )

        self.link_list.urls_changed.connect(
            self.schedule_save
        )

        self.size_combo.currentIndexChanged.connect(
            self.schedule_save
        )

        self.codec_combo.currentIndexChanged.connect(
            self.schedule_save
        )

        self.output_edit.textChanged.connect(
            self.schedule_save
        )

    def schedule_save(self):

        if self._restoring:
            return

        self.save_timer.start()

    def auto_save_settings(self):

        if self._restoring:
            return

        self.save_settings()

    def save_settings(self):

        try:

            data = self.config.data

            data["output_dir"] = (
                self.output_edit.text().strip()
            )

            size = (
                self.size_combo.currentData()
            )

            if size:

                data["video_size"] = (
                    f"{size[0]}x{size[1]}"
                )

            data["video_codec"] = (
                self.codec_combo.currentText()
            )

            data["links"] = (
                self._get_non_failed_urls()
            )

            data["window"] = {
                "x": self.x(),
                "y": self.y(),
                "width": self.width(),
                "height": self.height()
            }

            self.config.save()

        except Exception:

            pass

    # ========================================================
    # OmniGet 桥接服务器
    # ========================================================

    def setup_bridge(self):
        """初始化并启动桥接服务器"""
        # 创建 Cookie 管理器（复刻 OmniGet 按域名存储方式）
        self.cookie_manager = CookieManager(COOKIES_DIR)

        # 从配置读取或生成 token
        token = self.config.get("bridge_token", "")
        if not token:
            token = secrets.token_urlsafe(24)
            self.config.set("bridge_token", token)

        port = self.config.get("bridge_port", 47720)

        # 连接线程安全信号
        self.bridge_log_signal.connect(self._on_bridge_log)
        self.bridge_url_signal.connect(self._on_bridge_url)
        self.bridge_cookies_signal.connect(self._on_bridge_cookies)
        self.bridge_ua_signal.connect(self._on_bridge_ua)

        self.bridge = OmniGetBridge(
            token=token,
            port=port,
            link_file=LINK_FILE,
            log_callback=lambda msg: self.bridge_log_signal.emit(msg),
            url_callback=lambda url: self.bridge_url_signal.emit(url),
            cookies_callback=lambda c, u, s: self.bridge_cookies_signal.emit(c, u, s),
            ua_callback=lambda ua: self.bridge_ua_signal.emit(ua)
        )

        self.bridge.start()

        # 更新 UI
        self.txt_bridge_token.setText(token)
        self.lbl_bridge_status.setText("已启动")

        # 显示已存储的 Cookie 域名
        domains = self.cookie_manager.list_domains()
        if domains:
            self.log(f"[Cookie] 已加载 {len(domains)} 个域名的 Cookie：{', '.join(domains)}")

    # ---- 线程安全的槽函数（在主线程执行） ----

    def _on_bridge_log(self, msg):
        """桥接日志（主线程）"""
        try:
            self.log(f"[Bridge] {msg}")
        except Exception:
            pass

    def _qlog(self, msg):
        """队列关键事件：同时输出到控制台和 UI 日志"""
        print(msg, flush=True)
        try:
            self.log(msg)
        except Exception:
            pass

    def _on_bridge_url(self, url):
        """桥接收到 URL（主线程）→ 直接添加到列表 + 触发下载"""
        try:
            if hasattr(self, "link_list") and self.link_list is not None:
                # 指纹去重：本地已存在该视频文件
                if self._check_fingerprint(url):
                    self._qlog(f"[跳过] 本地已有该视频，跳过：{url}")
                    self._stat_total += 1
                    self._stat_skipped += 1
                    self._update_task_stats(
                        self._stat_total, self._stat_new,
                        self._stat_skipped, self._stat_failed
                    )
                    return
                
                # 检查是否已经在列表中（避免完全重复）
                existing_urls = self.link_list.get_urls()
                if url in existing_urls and url not in self.tried_urls:
                    self._qlog(f"[提示] 链接已在队列中，忽略：{url}")
                    return
                
                # 如果之前失败过，允许重新尝试
                if url in self.tried_urls:
                    self.tried_urls.discard(url)
                    self._qlog(f"[重试] 重新尝试之前失败的链接：{url}")
                
                self.link_list.addItem(url)
                self.link_list.urls_changed.emit()
                self._qlog(f"[自动下载] 已添加链接：{url}")
                QTimer.singleShot(500, self.auto_start_download)
            else:
                print("[自动下载] link_list 尚未初始化", flush=True)
        except Exception as e:
            print(f"[自动下载] 处理链接失败：{e}", flush=True)
            self.log(f"[自动下载] 处理链接失败：{e}")

    def _on_bridge_cookies(self, cookies, ua, source_url):
        """桥接收到 Cookie（主线程）"""
        self.on_bridge_cookies_received(cookies, ua, source_url)

    def _on_bridge_ua(self, ua):
        """桥接收到 UA（主线程）"""
        self.on_bridge_ua_received(ua)

    def auto_start_download(self):
        """自动触发下载(静默模式,不弹对话框)"""
        try:
            # 如果下载线程正在运行,不重复触发(下载完成后会自动检查队列)
            if (
                self.download_thread
                and
                self.download_thread.isRunning()
            ):
                print("[自动下载] 下载进行中,等待完成后自动继续", flush=True)
                return
    
            # 只选择尚未尝试过且指纹不存在的链接(失败的不自动重试)
            urls = [
                u for u in self.link_list.get_urls()
                if u not in self.tried_urls
                and not self._check_fingerprint(u)
            ]
            if not urls:
                print("[自动下载] 没有待下载的链接", flush=True)
                return
    
            self._qlog(f"[自动下载] 开始下载({len(urls)} 个链接)...")
            self.start_download(auto=True, urls=urls)
        except Exception as e:
            print(f"[自动下载] 触发失败:{e}", flush=True)
            self.log(f"[自动下载] 触发失败:{e}")

    def start_bridge_pairing(self):
        """开启配对窗口"""
        if hasattr(self, "bridge") and self.bridge:
            self.bridge.start_pairing()
            self.log("[Bridge] 配对窗口已开启，请在扩展中点击「配对」按钮")
        else:
            self.log("[Bridge] 桥接服务器未启动")

    def copy_bridge_token(self):
        """复制 Token 到剪贴板"""
        token = self.txt_bridge_token.text()
        if token:
            from PyQt5.QtWidgets import QApplication
            clipboard = QApplication.clipboard()
            clipboard.setText(token)
            self.log("[Bridge] Token 已复制到剪贴板")

    def on_bridge_cookies_received(self, cookies, ua, source_url=""):
        """桥接收到 Cookie → 按域名合并写入（不覆盖其他平台）"""
        try:
            if not cookies:
                self.log("[Bridge] 收到空 Cookie，跳过")
                return

            # 1. 按域名分类存储（OmniGet 方式：每个平台独立文件）
            written = self.cookie_manager.ingest_batch(
                cookies,
                source_url=source_url,
                source_label="OmniGet Bridge"
            )
            total_count = sum(count for _, count in written)
            domains = [d for d, _ in written]
            self.log(f"[Bridge] Cookie 已按域名存储：{', '.join(domains[:5])}{'...' if len(domains) > 5 else ''} ({total_count} 条)")

            # 2. 更新统一 Cookie 文件（合并模式：保留不在本次批次中的域名）
            all_file = COOKIES_DIR / "_all_cookies.txt"
            COOKIES_DIR.mkdir(parents=True, exist_ok=True)
            session_ttl = int(time.time()) + 86400
            
            # 提取本次批次的根域名集合
            incoming_roots = set()
            for c in cookies:
                root = CookieManager.root_domain_of(c.get("domain", ""))
                if root:
                    incoming_roots.add(root)
            
            # 读取现有文件，保留不在 incoming_roots 中的 Cookie
            preserved_lines = []
            if all_file.exists():
                try:
                    existing_content = all_file.read_text(encoding="utf-8")
                    for line in existing_content.splitlines():
                        trimmed = line.strip()
                        if not trimmed or trimmed.startswith('#'):
                            continue
                        # 解析域名
                        effective = trimmed
                        if trimmed.startswith("#HttpOnly_"):
                            effective = trimmed[10:]
                        parts = effective.split('\t')
                        if len(parts) >= 7:
                            domain = parts[0]
                            root = CookieManager.root_domain_of(domain)
                            if root and root not in incoming_roots:
                                preserved_lines.append(line)
                except Exception:
                    pass
            
            # 写入：保留的旧 Cookie + 新的 Cookie
            lines = ["# Netscape HTTP Cookie File"]
            lines.extend(preserved_lines)
            for c in cookies:
                lines.append(CookieManager._format_cookie_line(c, session_ttl))
            
            # 如果文件太大（超过 8MB），清理过期的 Cookie
            if len(lines) > 8000:  # 约 8MB
                self.log(f"[Bridge] ⚠ Cookie 文件过大 ({len(lines)} 条)，开始清理...")
                # 按域名分组，每个域名只保留最新的 500 条
                domain_cookies = {}
                for line in lines:
                    if line.startswith("#") or not line.strip():
                        continue
                    parts = line.split('\t')
                    if len(parts) >= 7:
                        domain = parts[0]
                        if domain not in domain_cookies:
                            domain_cookies[domain] = []
                        domain_cookies[domain].append(line)
                
                # 重新构建，每个域名最多 500 条
                cleaned_lines = ["# Netscape HTTP Cookie File"]
                for domain, cookie_list in domain_cookies.items():
                    # 按过期时间排序，保留最新的
                    cleaned_lines.extend(cookie_list[-500:])
                
                lines = cleaned_lines
                self.log(f"[Bridge] 清理完成：{len(lines)} 条")
            
            all_file.write_text("\n".join(lines) + "\n", encoding="utf-8")
            
            new_count = len(lines) - 1 - len(preserved_lines)  # 减去标题行和保留行
            total_count = len(lines) - 1
            file_size_kb = all_file.stat().st_size / 1024
            self.log(f"[Bridge] 统一 Cookie 已合并更新：新增 {new_count} 条，共 {total_count} 条 ({file_size_kb:.1f}KB)")

            # 如果有 UA，也更新
            if ua:
                self.on_bridge_ua_received(ua)

            # 显示来源
            if source_url:
                self.log(f"[Bridge] Cookie 来源：{source_url[:60]}...")

        except Exception as e:
            self.log(f"[Bridge] Cookie 存储失败：{e}")

    def on_bridge_ua_received(self, ua):
        """桥接收到 UA 时更新本地文件"""
        try:
            if not ua:
                return

            UA_FILE.parent.mkdir(parents=True, exist_ok=True)
            UA_FILE.write_text(ua.strip() + "\n", encoding="utf-8")
            self.log(f"[Bridge] UA 已更新：{ua[:50]}...")

        except Exception as e:
            self.log(f"[Bridge] UA 更新失败：{e}")

    def moveEvent(
        self,
        event
    ):

        super().moveEvent(
            event
        )

        if not self._restoring:

            self.schedule_save()

    def resizeEvent(
        self,
        event
    ):

        super().resizeEvent(
            event
        )

        if not self._restoring:

            self.schedule_save()

    def closeEvent(
        self,
        event
    ):

        self.save_settings()

        # 停止桥接服务器
        if hasattr(self, "bridge") and self.bridge:
            try:
                self.bridge.stop()
            except Exception:
                pass

        if (
            self.download_thread
            and
            self.download_thread.isRunning()
        ):

            self.download_thread.stop()

            self.download_thread.wait(
                2000
            )

        event.accept()

    def log(
        self,
        text
    ):

        self.log_edit.append(
            str(text)
        )

        scrollbar = (
            self.log_edit
            .verticalScrollBar()
        )

        scrollbar.setValue(
            scrollbar.maximum()
        )

    def log_clear(self):

        self.log_edit.clear()

    def show_log_context_menu(self, pos):
        """日志区域右键菜单：全选、复制、删除"""
        menu = QMenu(self)

        action_select_all = QAction("全选", self)
        action_copy = QAction("复制", self)
        action_delete = QAction("删除", self)

        has_selection = self.log_edit.textCursor().hasSelection()
        action_copy.setEnabled(has_selection)
        action_delete.setEnabled(has_selection)

        menu.addAction(action_select_all)
        menu.addAction(action_copy)
        menu.addSeparator()
        menu.addAction(action_delete)

        action_select_all.triggered.connect(self.log_edit.selectAll)
        action_copy.triggered.connect(self.log_copy_selected)
        action_delete.triggered.connect(self.log_delete_selected)

        menu.exec_(self.log_edit.mapToGlobal(pos))

    def log_copy_selected(self):
        """复制日志中选中的文本"""
        cursor = self.log_edit.textCursor()
        if cursor.hasSelection():
            QApplication.clipboard().setText(cursor.selectedText())

    def log_delete_selected(self):
        """删除日志中选中的文本"""
        cursor = self.log_edit.textCursor()
        if cursor.hasSelection():
            cursor.removeSelectedText()

    def load_status_to_log(self):

        self.log(
            "视频批量下载工具启动"
        )

        self.log(
            "=" * 60
        )

        self.log(
            f"运行目录：{RUN_DIR}"
        )

        self.log(
            f"yt-dlp："
            f"{'正常' if check_file(YTDLP_EXE) else '未找到'}"
        )

        self.log(
            f"FFmpeg："
            f"{'正常' if check_file(FFMPEG_EXE) else '未找到'}"
        )

        self.log(
            f"FFprobe："
            f"{'正常' if check_file(FFPROBE_EXE) else '未找到'}"
        )

        # 检查 Cookie 状态（优先检查统一 Cookie 文件）
        all_cookie_file = COOKIES_DIR / "_all_cookies.txt"
        has_unified = all_cookie_file.exists()
        has_classified = any((COOKIES_DIR / d).exists() for d in ["youtube.com", "tiktok.com", "instagram.com"])
        has_old = COOKIE_FILE.exists()
        
        if has_unified:
            cookie_count = len(all_cookie_file.read_text(encoding="utf-8").splitlines()) - 2  # 减去注释行
            self.log(f"Cookie：已存在（统一文件 {cookie_count} 条）")
        elif has_classified:
            self.log("Cookie：已存在（按域名分类）")
        elif has_old:
            self.log("Cookie：已存在（旧版 cookies.txt）")
        else:
            self.log("Cookie：不存在")

        self.log(
            f"UA："
            f"{'已存在' if UA_FILE.exists() else '不存在'}"
        )

        self.log(
            f"输出目录："
            f"{self.output_edit.text()}"
        )

        self.log(
            f"内存指纹库：{len(self.fingerprint_set)} 条记录"
        )

        self.log(
            f"目录视频：{self._total_mp4_count}  有效指纹：{self._valid_fingerprint_count}"
        )

        self.log("")

    def update_count(self):

        count = self.link_list.count()

        self.lbl_count.setText(
            f"{count} 条"
        )

    def links_changed(self):

        self.update_count()

        self.schedule_save()

    def paste_links(self):

        self.link_list.paste_text()

    def clear_links(self):

        if not self.link_list.count():
            return

        self.link_list.clear()

        self.links_changed()

    # ========================================================
    # 输出目录
    # ========================================================

    def select_output_dir(self):

        current = self.output_edit.text().strip()

        if not current:

            current = str(
                DEFAULT_OUTPUT_DIR
            )

        directory = QFileDialog.getExistingDirectory(
            self,
            "选择输出文件夹",
            current
        )

        if not directory:
            return

        self.output_edit.setText(
            directory
        )

        try:

            Path(
                directory
            ).mkdir(
                parents=True,
                exist_ok=True
            )

        except Exception as e:

            QMessageBox.warning(
                self,
                "创建目录失败",
                str(e)
            )

        self.save_settings()

    # ========================================================
    # 拖入输出文件夹后的处理
    # ========================================================

    def output_folder_dropped(
        self,
        directory
    ):

        directory = str(
            directory
        ).strip()

        if not directory:
            return

        try:

            path = Path(
                directory
            )

            if not path.is_dir():

                return

            # 确保目录可用
            path.mkdir(
                parents=True,
                exist_ok=True
            )

            self.output_edit.setText(
                str(path)
            )

            self.save_settings()

            self.lbl_status.setText(
                "输出目录已更新"
            )

        except Exception as e:

            QMessageBox.warning(
                self,
                "输出目录错误",
                str(e)
            )

    def open_output_folder(self):

        directory = self.output_edit.text().strip()

        if not directory:

            directory = str(
                DEFAULT_OUTPUT_DIR
            )

        path = Path(
            directory
        )

        try:

            path.mkdir(
                parents=True,
                exist_ok=True
            )

        except Exception:

            pass

        try:

            if sys.platform.startswith(
                "win"
            ):

                os.startfile(
                    str(path)
                )

            elif sys.platform == "darwin":

                subprocess.Popen(
                    [
                        "open",
                        str(path)
                    ]
                )

            else:

                subprocess.Popen(
                    [
                        "xdg-open",
                        str(path)
                    ]
                )

        except Exception as e:

            QMessageBox.warning(
                self,
                "打开失败",
                str(e)
            )

    def start_download(self, auto=False, urls=None):

        self._auto_mode = auto

        self._stopped_by_user = False

        if not auto:

            # 手动点击「开始下载」：清除尝试记录，重新下载全部链接
            self.tried_urls.clear()
            # 清除上次失败的红色链接
            self.clear_failed_links()

        if (
            self.download_thread
            and
            self.download_thread.isRunning()
        ):

            return

        if urls is None:

            urls = (
                self.link_list.get_urls()
            )
        
        # 调试：打印接收到的 URL
        if urls:
            print(f"[DEBUG] start_download 收到 {len(urls)} 个 URL:", flush=True)
            for i, u in enumerate(urls[:3]):  # 只打印前3个
                print(f"  [{i}] 长度={len(u)}: {u}", flush=True)

        if not urls:

            if not auto:
                QMessageBox.warning(
                    self,
                    "没有链接",
                    "请先拖入视频链接、TXT，"
                    "或者直接粘贴视频链接。"
                )

            return

        if not check_file(
            YTDLP_EXE
        ):

            if not auto:
                QMessageBox.critical(
                    self,
                    "错误",
                    f"找不到 yt-dlp.exe：\n\n"
                    f"{YTDLP_EXE}"
                )
            else:
                self._qlog("[自动下载] 找不到 yt-dlp.exe，跳过")

            return

        if not check_file(
            FFMPEG_EXE
        ):

            if not auto:
                QMessageBox.critical(
                    self,
                    "错误",
                    f"找不到 FFmpeg：\n\n"
                    f"{FFMPEG_EXE}"
                )
            else:
                self._qlog("[自动下载] 找不到 FFmpeg，跳过")

            return

        size = (
            self.size_combo.currentData()
        )

        if not size:

            if not auto:
                QMessageBox.warning(
                    self,
                    "尺寸错误",
                    "没有选择输出尺寸。"
                )
            else:
                self._qlog("[自动下载] 未选择输出尺寸，跳过")

            return

        target_w, target_h = size

        codec = (
            self.codec_combo.currentText()
        )

        output_dir = (
            self.output_edit.text().strip()
        )

        if not output_dir:

            output_dir = str(
                DEFAULT_OUTPUT_DIR
            )

            self.output_edit.setText(
                output_dir
            )

        try:

            Path(
                output_dir
            ).mkdir(
                parents=True,
                exist_ok=True
            )

        except Exception as e:

            QMessageBox.critical(
                self,
                "输出目录错误",
                str(e)
            )

            return

        self.save_settings()

        # 初始化/更新任务统计
        if not auto:
            # 手动下载：重置所有统计
            self._update_task_stats(total=len(urls), new=0, skipped=0, failed=0)
        else:
            # 自动下载：累加总数（保留之前的统计）
            self._stat_total += len(urls)
            self._update_task_stats(
                self._stat_total, self._stat_new,
                self._stat_skipped, self._stat_failed
            )

        self.progress.setValue(
            0
        )

        self.lbl_status.setText(
            "下载中"
        )

        self.lbl_current.setText(
            f"准备处理 {len(urls)} 个任务"
        )

        self.btn_download.setEnabled(
            False
        )

        self.btn_stop.setEnabled(
            True
        )

        self.log("")

        self.log(
            "=" * 60
        )

        self.log(
            "开始批量下载"
        )

        self.log(
            f"链接数量：{len(urls)}"
        )

        self.log(
            f"输出尺寸："
            f"{target_w} × {target_h}"
        )

        self.log(
            f"视频编码：{codec}"
        )

        self.log(
            f"输出目录：{output_dir}"
        )

        self.download_thread = (
            DownloadThread(
                urls,
                target_w,
                target_h,
                codec,
                output_dir,
                cookies_dir=COOKIES_DIR
            )
        )

        self.download_thread.log_signal.connect(
            self.log
        )

        self.download_thread.progress_signal.connect(
            self.progress.setValue
        )

        self.download_thread.current_signal.connect(
            self.update_current
        )

        self.download_thread.finished_signal.connect(
            self.download_finished
        )

        self.download_thread.url_finished_signal.connect(
            self.on_url_download_finished
        )

        self.download_thread.start()

    def update_current(
        self,
        current,
        total,
        url
    ):

        self.current_download_url = url

        self.lbl_current.setText(
            f"{current}/{total}  "
            f"{url}"
        )

    def on_url_download_finished(self, url, success):
        """单个 URL 完成：成功→移出列表+记录；失败→标红且不自动重试"""
        try:
            # 调试：打印接收到的 URL 和 Cookie 状态
            all_cookie_file = COOKIES_DIR / "_all_cookies.txt"
            cookie_size = all_cookie_file.stat().st_size if all_cookie_file.exists() else 0
            print(f"[DEBUG] on_url_download_finished 收到 URL: 长度={len(url)}, Cookie大小={cookie_size}KB, 结果={'成功' if success else '失败'}", flush=True)
            
            if self.current_download_url == url:
                self.current_download_url = ""

            if not hasattr(self, "link_list") or self.link_list is None:
                return

            if success:
                # 下载成功，从列表移除 + 加入指纹库
                self.link_list.remove_url(url)
                self._add_fingerprint(url)
                self._stat_new += 1
                self._update_task_stats(
                    self._stat_total, self._stat_new,
                    self._stat_skipped, self._stat_failed
                )
                self._qlog(f"[完成] 已完成，从列表移除：{url}")
            else:
                # 失败：保留在列表供用户查看，标红且不自动重试
                self.tried_urls.add(url)
                self.mark_url_failed(url)
                self._stat_failed += 1
                self._update_task_stats(
                    self._stat_total, self._stat_new,
                    self._stat_skipped, self._stat_failed
                )
                self._qlog(f"[失败] 下载失败（可右键删除或手动重试）：{url}")
        except Exception as e:
            self.log(f"[自动下载] 处理链接完成事件失败：{e}")

    def mark_url_failed(self, url):
        """将失败的链接在列表中标红"""
        try:
            for i in range(self.link_list.count()):
                item = self.link_list.item(i)
                if item and item.text().strip() == url:
                    item.setForeground(QColor("#ff6b6b"))
                    break
        except Exception:
            pass

    def clear_failed_links(self):
        """清除列表中所有失败的（红色）链接"""
        try:
            failed_urls = []
            for i in range(self.link_list.count() - 1, -1, -1):
                item = self.link_list.item(i)
                if item:
                    fg = item.foreground()
                    if fg and fg.color() and fg.color().name().lower() == "#ff6b6b":
                        failed_urls.append(item.text().strip())
                        self.link_list.takeItem(i)
            if failed_urls:
                self.link_list.urls_changed.emit()
                self.tried_urls -= set(failed_urls)
                self._qlog(f"[清理] 已清除 {len(failed_urls)} 个失败链接")
        except Exception:
            pass

    def on_retry_download(self, urls):
        """用户右键重试失败的链接"""
        try:
            if not urls:
                return

            # 从 tried_urls 中移除，允许重新下载
            for url in urls:
                self.tried_urls.discard(url)

            # 恢复链接颜色为默认（取消标红）
            for i in range(self.link_list.count()):
                item = self.link_list.item(i)
                if item and item.text().strip() in urls:
                    item.setData(Qt.ForegroundRole, None)

            self._qlog(f"[手动重试] 开始重新下载 {len(urls)} 个失败链接...")
            self.start_download(auto=True, urls=urls)
        except Exception as e:
            self._qlog(f"[手动重试] 启动失败：{e}")

    # ---- 内存指纹系统 ----

    _KNOWN_PLATFORMS = {"youtube", "tiktok", "instagram", "facebook", "twitter", "other"}

    def _scan_fingerprints(self):
        """递归扫描输出目录下所有 MP4，从文件名提取 video_id，结合父目录平台名构建指纹库
        返回 (fingerprint_set, total_count, valid_count, no_fingerprint_list)
        """
        output_dir = self.output_edit.text().strip() if hasattr(self, 'output_edit') else ""
        if not output_dir:
            output_dir = str(DEFAULT_OUTPUT_DIR)
        output_path = Path(output_dir)

        fingerprints = set()
        total = 0
        no_fp_files = []

        if not output_path.exists():
            return fingerprints, total, 0, no_fp_files

        try:
            for mp4_file in output_path.rglob("*.mp4"):
                total += 1
                stem = mp4_file.stem
                if stem.startswith("video_") and len(stem) > 6:
                    vid = stem[6:]
                    # 从父目录名推断平台
                    parent_name = mp4_file.parent.name.lower()
                    if parent_name in self._KNOWN_PLATFORMS:
                        fp = f"{parent_name}_{vid}"
                    else:
                        # 向上查找已知平台目录
                        found_platform = None
                        for part in mp4_file.relative_to(output_path).parts[:-1]:
                            if part.lower() in self._KNOWN_PLATFORMS:
                                found_platform = part.lower()
                                break
                        if found_platform:
                            fp = f"{found_platform}_{vid}"
                        else:
                            # 无法识别平台，尝试从视频ID格式推断
                            fp = None
                            no_fp_files.append(mp4_file.name)

                    if fp:
                        fingerprints.add(fp)
                else:
                    no_fp_files.append(mp4_file.name)
        except Exception as e:
            print(f"[扫描] 扫描失败：{e}", flush=True)

        return fingerprints, total, len(fingerprints), no_fp_files

    def _build_fingerprint_set(self):
        """构建内存指纹库并更新统计标签"""
        self.fingerprint_set, total, valid, no_fp_list = self._scan_fingerprints()
        self._no_fingerprint_files = no_fp_list
        self._total_mp4_count = total
        self._valid_fingerprint_count = valid
        self._update_stats_labels()

        if no_fp_list:
            for fname in no_fp_list[:10]:
                self.log(f"[扫描] {fname} → 无法识别指纹")
            if len(no_fp_list) > 10:
                self.log(f"[扫描] ... 还有 {len(no_fp_list) - 10} 个文件无法识别指纹")

    def _check_fingerprint(self, url):
        """检查 URL 对应的视频是否已存在于内存指纹库"""
        info = extract_video_id_from_url(url)
        if info:
            platform, vid = info
            fp = f"{platform}_{vid}"
            return fp in self.fingerprint_set
        return False

    def _add_fingerprint(self, url):
        """下载成功后将指纹加入内存库"""
        info = extract_video_id_from_url(url)
        if info:
            platform, vid = info
            fp = f"{platform}_{vid}"
            self.fingerprint_set.add(fp)
            self._valid_fingerprint_count = len(self.fingerprint_set)
            self._total_mp4_count += 1
            self._update_stats_labels()

    def _do_scan(self):
        """用户点击[扫描]按钮：重新扫描输出目录并重建指纹库"""
        output_dir = self.output_edit.text().strip()
        if not output_dir:
            output_dir = str(DEFAULT_OUTPUT_DIR)

        self.log(f"[扫描] 开始扫描：{output_dir}")
        self._build_fingerprint_set()
        self.log(f"[扫描] 找到 MP4：{self._total_mp4_count}")
        self.log(f"[扫描] 有效指纹：{self._valid_fingerprint_count}")
        if self._no_fingerprint_files:
            self.log(f"[扫描] 无指纹：{len(self._no_fingerprint_files)}")
        self.log("[扫描] 完成")

    def _update_stats_labels(self):
        """更新 GUI 统计标签"""
        try:
            if hasattr(self, 'lbl_stat_videos') and self.lbl_stat_videos:
                self.lbl_stat_videos.setText(
                    f"目录视频：{getattr(self, '_total_mp4_count', 0):,}"
                )
            if hasattr(self, 'lbl_stat_fps') and self.lbl_stat_fps:
                self.lbl_stat_fps.setText(
                    f"有效指纹：{getattr(self, '_valid_fingerprint_count', 0):,}"
                )
        except Exception:
            pass

    def _update_task_stats(self, total=0, new=0, skipped=0, failed=0):
        """更新下载任务统计"""
        self._stat_total = total
        self._stat_new = new
        self._stat_skipped = skipped
        self._stat_failed = failed
        try:
            if hasattr(self, 'lbl_stat_tasks') and self.lbl_stat_tasks:
                self.lbl_stat_tasks.setText(
                    f"总数：{total}  新增：{new}  已跳过：{skipped}  失败：{failed}"
                )
        except Exception:
            pass

    def _get_non_failed_urls(self):
        """获取列表中非失败（非红色）的 URL，用于持久化保存"""
        result = []
        try:
            for i in range(self.link_list.count()):
                item = self.link_list.item(i)
                if item:
                    fg = item.foreground()
                    is_red = fg and fg.color() and fg.color().name().lower() == "#ff6b6b"
                    if not is_red:
                        url = item.text().strip()
                        if url:
                            result.append(url)
        except Exception:
            pass
        return result

    def on_urls_removed(self, removed_urls):
        """用户手动删除链接 → 立即生效（取消对应下载）"""
        try:
            removed = {
                u.strip() for u in removed_urls
                if u and u.strip()
            }
            if not removed:
                return

            for u in removed:
                self.tried_urls.discard(u)

            thread = self.download_thread
            if thread and thread.isRunning():
                for u in removed:
                    if u == self.current_download_url:
                        self.log(f"[队列] 正在中止当前下载：{u[:60]}")
                    thread.cancel_url(u)

            self.log(f"[队列] 已移除 {len(removed)} 个链接")
        except Exception as e:
            self.log(f"[队列] 处理删除失败：{e}")

    def stop_download(self):

        if self.download_thread:

            self._stopped_by_user = True

            self.download_thread.stop()

            self.btn_stop.setEnabled(
                False
            )

            self.lbl_status.setText(
                "正在停止"
            )

            self.log(
                "正在停止任务..."
            )

    def download_finished(
        self,
        success,
        failed
    ):

        self.btn_download.setEnabled(
            True
        )

        self.btn_stop.setEnabled(
            False
        )

        self.progress.setValue(
            100
        )

        self.lbl_status.setText(
            "任务完成"
        )

        self.lbl_current.setText(
            "任务完成"
        )

        self.log("")

        self.log(
            "=" * 60
        )

        self.log(
            "任务完成"
        )

        self.log(
            f"成功：{success}"
        )

        self.log(
            f"失败：{failed}"
        )

        self.log(
            f"输出目录："
            f"{self.output_edit.text()}"
        )

        self.current_download_url = ""

        # 更新任务统计
        self._update_task_stats(
            self._stat_total, self._stat_new,
            self._stat_skipped, self._stat_failed
        )

        # 手动停止：不自动继续
        if self._stopped_by_user:
            self._qlog("[队列] 任务已手动停止")
            return

        # 自动下载队列:检查是否还有未尝试的链接
        remaining_urls = [
            u for u in self.link_list.get_urls()
            if u not in self.tried_urls
        ]
        if remaining_urls:
            # 还有链接,继续下载
            self.log("")
            self._qlog(f"[自动下载] 列表中还有 {len(remaining_urls)} 个链接,继续下载...")
        
            # 先确保线程已停止,再延迟启动下一个
            if self.download_thread and self.download_thread.isRunning():
                self.download_thread.wait(5000)  # 最多等待5秒
                    
            QTimer.singleShot(1000, self.auto_start_download)
            return

        # 没有更多链接
        if getattr(self, '_auto_mode', False):
            # 自动模式：静默完成，等待新链接
            if self.link_list.count():
                self._qlog("[自动下载] 队列空闲（红色为失败链接，可右键删除或手动重试）")
            else:
                self._qlog("[自动下载] 列表已空，等待新链接...")
            self.lbl_status.setText("等待中")
        else:
            # 手动模式：显示完成对话框
            QMessageBox.information(
                self,
                "任务完成",
                f"批量下载完成。\n\n"
                f"成功：{success}\n"
                f"失败：{failed}\n\n"
                f"输出目录：\n"
                f"{self.output_edit.text()}"
            )


# ============================================================
# 全局样式
# ============================================================

def apply_application_style(
    app
):

    app.setStyle(
        "Fusion"
    )

    palette = QPalette()

    palette.setColor(
        QPalette.Window,
        QColor("#2b2b2b")
    )

    palette.setColor(
        QPalette.WindowText,
        QColor("#dddddd")
    )

    palette.setColor(
        QPalette.Base,
        QColor("#202020")
    )

    palette.setColor(
        QPalette.AlternateBase,
        QColor("#303030")
    )

    palette.setColor(
        QPalette.Text,
        QColor("#dddddd")
    )

    palette.setColor(
        QPalette.Button,
        QColor("#414141")
    )

    palette.setColor(
        QPalette.ButtonText,
        QColor("#dddddd")
    )

    palette.setColor(
        QPalette.Highlight,
        QColor("#555555")
    )

    palette.setColor(
        QPalette.HighlightedText,
        QColor("#ffffff")
    )

    app.setPalette(
        palette
    )


# ============================================================
# main
# ============================================================

def main():

    ensure_dirs()

    app = QApplication(
        sys.argv
    )

    # ========================================================
    # 设置全局程序图标
    # QMessageBox 等弹窗也使用 icon.png
    # ========================================================

    icon_path = BASE_DIR / "icon.png"

    if icon_path.exists():

        app.setWindowIcon(
            QIcon(str(icon_path))
        )

    app.setApplicationName(
        "视频批量下载工具"
    )

    app.setApplicationVersion(
        "V4.0"
    )

    apply_application_style(
        app
    )

    window = MainWindow()

    window.apply_style()

    window.show()

    sys.exit(
        app.exec_()
    )


if __name__ == "__main__":

    main()