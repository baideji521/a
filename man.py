# -*- coding: utf-8 -*-

import sys
import os
import json
import time
import secrets
import re
import threading
import subprocess
from http.server import HTTPServer, BaseHTTPRequestHandler
from pathlib import Path
from urllib.parse import urlparse

try:
    # Python 3.12+ 提供 typing.override
    from typing import override
except ImportError:
    # 旧版本 Python 下退化为无操作装饰器
    def override(func):
        return func

from PyQt5.QtGui import QIcon

from PyQt5.QtCore import (
    Qt,
    QThread,
    pyqtSignal,
    QTimer,
    QPoint,
    QRect,
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
    QGridLayout,
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
    QSpacerItem,
    QCheckBox,
    QDialog
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
# 候选列表单独定义：自动安装完成后调用 refresh_tool_paths() 重新解析即可生效
YTDLP_CANDIDATES = (
    RUN_DIR / "yt-dlp.exe",
    BIN_DIR / "yt-dlp.exe"
)
FFMPEG_CANDIDATES = (
    RUN_DIR / "ffmpeg.exe",
    RUN_DIR / "ffmpeg" / "bin" / "ffmpeg.exe",
    BIN_DIR / "ffmpeg.exe"
)
FFPROBE_CANDIDATES = (
    RUN_DIR / "ffprobe.exe",
    RUN_DIR / "ffmpeg" / "bin" / "ffprobe.exe",
    BIN_DIR / "ffprobe.exe"
)

YTDLP_EXE = resolve_path(*YTDLP_CANDIDATES)

COOKIE_FILE = resolve_path(
    RUN_DIR / "cookies.txt",
    BIN_DIR / "cookies.txt"
)
UA_FILE = resolve_path(
    RUN_DIR / "browser_ua.txt",
    BIN_DIR / "browser_ua.txt"
)

FFMPEG_EXE = resolve_path(*FFMPEG_CANDIDATES)
FFPROBE_EXE = resolve_path(*FFPROBE_CANDIDATES)


def refresh_tool_paths():
    """自动安装后重新解析三个 exe 路径（模块内其它代码都在调用时读全局变量）"""
    global YTDLP_EXE, FFMPEG_EXE, FFPROBE_EXE
    YTDLP_EXE = resolve_path(*YTDLP_CANDIDATES)
    FFMPEG_EXE = resolve_path(*FFMPEG_CANDIDATES)
    FFPROBE_EXE = resolve_path(*FFPROBE_CANDIDATES)

LINK_FILE = RUN_DIR / "link.txt"

# 便携版 JS 运行时（用于 yt-dlp nsig 解密）
# node 便携版按历史约定放 nodejs\node.exe；
# 程序内置安装的是 Deno 单文件版，放 jsruntime\deno.exe。
PORTABLE_NODE_EXE = RUN_DIR / "nodejs" / "node.exe"
JS_RUNTIME_DIR = RUN_DIR / "jsruntime"
PORTABLE_DENO_EXE = JS_RUNTIME_DIR / "deno.exe"
DENO_ZIP_URL = (
    "https://github.com/denoland/deno/releases/latest/download/"
    "deno-x86_64-pc-windows-msvc.zip"
)

# 捆绑 FFmpeg 的安装位置与下载源（gyan.dev 官方 Windows 构建，essentials 版够用）
FFMPEG_BIN_DIR = RUN_DIR / "ffmpeg" / "bin"
FFMPEG_ZIP_URL = (
    "https://www.gyan.dev/ffmpeg/builds/ffmpeg-release-essentials.zip"
)

# ---- YouTube 下载策略（对齐 OmniGet 的 core/ytdlp.rs）----
# -N：并发分片数。YouTube 的 DASH 是分片流，单连接跑不满带宽。
#     OmniGet 的做法是「基线并发 + 被 429 后自动收敛」，见
#     youtube_concurrent_fragments()：0 次 429 用基线，1 次降 4，≥2 次降 2。
YOUTUBE_CONCURRENT_FRAGMENTS = 8
YOUTUBE_FRAGMENTS_AFTER_ONE_429 = 4
YOUTUBE_FRAGMENTS_AFTER_MANY_429 = 2

# --throttled-rate：低于该速度就判定被限速并重新提取链接（不是限速上限）。
#     取 100K 与 OmniGet 一致：足够低，不会在正常慢速时频繁重新提取。
YOUTUBE_THROTTLED_RATE = "100K"

# 格式选择器：优先 m4a 音轨，合并进 mp4 时可直接复制音频流，
# 省掉 opus → aac 的转码；拿不到 m4a 再退回任意音轨。
YOUTUBE_FORMAT_SELECTOR = "bv*+ba[ext=m4a]/bv*+ba/b"

# SABR 回退顺序：web 提取器有时只给 SABR 格式，普通下载器拉不动。
#     这四个 client 仍然返回渐进式 URL，按可靠性从高到低排。
YOUTUBE_SABR_CLIENT_CASCADE = ("android", "ios", "tv", "web_safari")

# yt-dlp 在缺少 / 过期 PO Token 时实际输出的句子。
#     不用裸 "po_token" 匹配：成功拿到 token 的日志里也有这个词。
YOUTUBE_POT_MISSING_SIGNS = (
    "a po token is required",
    "po token is missing",
    "missing a po token",
    "content is not available on this app",
    "sign in to confirm you're not a bot",
)

# 进程内 429 计数：只用来收敛并发，重启后归零
_youtube_429_hits = 0


def youtube_concurrent_fragments():
    """按本次运行遇到过的 429 次数决定 -N（OmniGet 同款收敛策略）"""
    if _youtube_429_hits >= 2:
        return YOUTUBE_FRAGMENTS_AFTER_MANY_429
    if _youtube_429_hits > 0:
        return YOUTUBE_FRAGMENTS_AFTER_ONE_429
    return YOUTUBE_CONCURRENT_FRAGMENTS


def youtube_pot_missing(text_lower):
    """stderr 是否在说「缺 PO Token」"""
    return any(sign in text_lower for sign in YOUTUBE_POT_MISSING_SIGNS)

TEMP_DIR = RUN_DIR / "_download_temp"

DEFAULT_OUTPUT_DIR = RUN_DIR / "downloads"

CONFIG_FILE = RUN_DIR / "config.json"

COOKIES_DIR = RUN_DIR / "cookies"


# ============================================================
# GUI 日志详细度开关
#
# 运行目录下存在 log.txt  → 详细模式：yt-dlp 原始输出、调试标签全部显示
# 不存在                  → 精简模式：只显示流程节点（开始/完成/跳过/统计）
#
# 出问题时手动建一个空的 log.txt 再重启即可看到全部细节。
# 启动时读取一次，运行期间不再检查。
#
# 注意：本开关只影响 GUI 日志框的显示，不影响 logs/tiktok_debug.log
# （诊断日志始终完整记录），也不影响 stdout 的 print。
# ============================================================

VERBOSE_FLAG_FILE = RUN_DIR / "log.txt"

try:
    VERBOSE_LOG = VERBOSE_FLAG_FILE.exists()
except Exception:
    VERBOSE_LOG = False

# 标记 yt-dlp / ffmpeg 的原始输出行。用不可见控制符，
# 保证不会与真实输出内容冲突；显示前会被剥掉。
LOG_RAW_MARK = "\u0001"

# 精简模式下隐藏的调试类前缀
QUIET_HIDE_PREFIXES = (
    "[DEBUG]",
    "[JS Runtime]",
    "[yt-dlp]",
    "[YT-DLP]",
    "[COMMAND]",
    "[Bridge]",
    "[Cookie]",
    "[UA]",
    "[Referer]",
    "[HEADERS]",
    "[DNA]",
    "[扫描]",
)

# 无论哪种模式都必须显示的内容（出问题时不能被静音）
LOG_ALWAYS_SHOW = (
    "⚠",
    "错误",
    "失败",
    "ERROR",
    "WARNING",
    "Traceback",
    "建议",
    "找不到",
)


def gui_log_visible(text):
    """判断一条日志是否应显示在 GUI 日志框

    详细模式全放行；精简模式只放行流程节点与错误/警告。
    返回 (是否显示, 处理后的文本)。
    """
    raw = text.startswith(LOG_RAW_MARK)
    if raw:
        text = text[len(LOG_RAW_MARK):]

    if VERBOSE_LOG:
        return True, text

    # 错误/警告优先级最高，任何模式都要看到
    if any(k in text for k in LOG_ALWAYS_SHOW):
        return True, text

    # yt-dlp / ffmpeg 原始输出：精简模式一律不显示
    if raw:
        return False, text

    if text.startswith(QUIET_HIDE_PREFIXES):
        return False, text

    return True, text


# ============================================================
# 任务日志（供上传 GitHub 诊断，与 GUI 日志完全分离）
#
# 日志位置：logs/tiktok_debug.log
# 滚动：单文件超过 10MB 后滚动为 tiktok_debug.1.log / .2.log
# 脱敏：永不写入 Cookie 真实值 / Session / Authorization / 密码；
#       关键 Cookie 只记录 present / absent
# ============================================================

TASK_DEBUG_LOG_ENABLED = True
TASK_DEBUG_DIR = RUN_DIR / "logs"
TASK_DEBUG_LOG_FILE = TASK_DEBUG_DIR / "tiktok_debug.log"
TASK_DEBUG_MAX_BYTES = 10 * 1024 * 1024
TASK_DEBUG_BACKUP_COUNT = 2

_task_debug_lock = threading.Lock()

# URL 中需要脱敏的查询参数关键词（命中则值替换为 REDACTED）
TASK_URL_SENSITIVE_QUERY = (
    "token", "session", "sig", "sign", "key",
    "auth", "secret", "pass", "code",
)

# 关键 Cookie：只记录存在性，不记录真实值
TASK_KEY_COOKIES = (
    "sessionid", "msToken", "sid_tt",
    "ttwid", "tt_chain_token", "passport_csrf_token",
)


def task_redact_url(url):
    """脱敏 URL 中的敏感查询参数（值替换为 REDACTED）"""
    try:
        from urllib.parse import urlparse, parse_qsl, urlencode, urlunparse
        parsed = urlparse(str(url))
        if not parsed.query:
            return str(url)
        pairs = []
        for k, v in parse_qsl(parsed.query, keep_blank_values=True):
            if any(s.lower() in k.lower() for s in TASK_URL_SENSITIVE_QUERY):
                pairs.append((k, "REDACTED"))
            else:
                pairs.append((k, v))
        return urlunparse(parsed._replace(query=urlencode(pairs)))
    except Exception:
        return str(url)


# 命令日志中需要脱敏值的参数（后一个元素是敏感值）
TASK_CMD_SENSITIVE_OPTS = ("--cookies", "--username", "--password", "--client-certificate")
TASK_CMD_SENSITIVE_HEADER_PREFIXES = ("cookie:", "authorization:", "x-ms-token", "session")


def task_redact_command(cmd):
    """脱敏 yt-dlp 命令行（供 [COMMAND] 日志）：敏感选项值与敏感 Header 值替换为 REDACTED"""
    try:
        out = []
        redact_next = False
        for part in cmd:
            s = str(part)
            if redact_next:
                out.append("REDACTED")
                redact_next = False
                continue
            if s in TASK_CMD_SENSITIVE_OPTS:
                out.append(s)
                redact_next = True
                continue
            low = s.lower()
            if low.startswith(("cookie:", "authorization:")) or (
                low.startswith(("x-ms-token", "session")) and ":" in s
            ):
                out.append(s.split(":", 1)[0] + ":REDACTED")
                continue
            out.append(s)
        return " ".join(out)
    except Exception:
        return "NA"


def task_debug(*lines):
    """追加写入 logs/tiktok_debug.log（线程安全，超过 10MB 自动滚动）"""
    if not TASK_DEBUG_LOG_ENABLED:
        return
    try:
        with _task_debug_lock:
            TASK_DEBUG_DIR.mkdir(parents=True, exist_ok=True)
            if TASK_DEBUG_LOG_FILE.exists() and TASK_DEBUG_LOG_FILE.stat().st_size > TASK_DEBUG_MAX_BYTES:
                # tiktok_debug.log → tiktok_debug.1.log → tiktok_debug.2.log（备份在日志同目录）
                log_dir = TASK_DEBUG_LOG_FILE.parent
                for i in range(TASK_DEBUG_BACKUP_COUNT, 1, -1):
                    src = log_dir / f"{TASK_DEBUG_LOG_FILE.stem}.{i - 1}{TASK_DEBUG_LOG_FILE.suffix}"
                    dst = log_dir / f"{TASK_DEBUG_LOG_FILE.stem}.{i}{TASK_DEBUG_LOG_FILE.suffix}"
                    if src.exists():
                        os.replace(str(src), str(dst))
                dst = log_dir / f"{TASK_DEBUG_LOG_FILE.stem}.1{TASK_DEBUG_LOG_FILE.suffix}"
                os.replace(str(TASK_DEBUG_LOG_FILE), str(dst))
            with open(TASK_DEBUG_LOG_FILE, "a", encoding="utf-8") as f:
                for line in lines:
                    f.write(str(line) + "\n")
    except Exception:
        pass


def task_debug_section(title, pairs, bordered=False):
    """写入 [TITLE] key=value 区段（只允许传入已脱敏字段）"""
    if bordered:
        task_debug("=" * 60)
    task_debug(f"[{title}]")
    for k, v in pairs:
        task_debug(f"{k}={v}")
    if bordered:
        task_debug("=" * 60)
    task_debug("")


def task_cookie_stats(cookie_path):
    """Cookie 文件统计：存在性 / 大小 / 条数 / mtime / 关键 Cookie 存在性（绝不读取真实值入日志）"""
    stats = {"exists": False, "size": 0, "count": 0, "mtime": "NA"}
    for name in TASK_KEY_COOKIES:
        stats[name] = "absent"
    try:
        p = Path(cookie_path)
        if p.exists():
            st = p.stat()
            stats["exists"] = True
            stats["size"] = st.st_size
            stats["mtime"] = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(st.st_mtime))
            names = set()
            count = 0
            with open(p, "r", encoding="utf-8", errors="ignore") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    parts = line.split("\t")
                    if len(parts) >= 7 and not line.startswith("#"):
                        count += 1
                        names.add(parts[5])
            stats["count"] = count
            for name in TASK_KEY_COOKIES:
                stats[name] = "present" if name in names else "absent"
    except Exception:
        pass
    return stats


_YTDLP_VERSION_CACHE = None


def get_ytdlp_version():
    """读取 yt-dlp 版本号（进程内只探测一次并缓存）

    yt-dlp.exe 是 17MB 的 PyInstaller 包，每次 `--version` 都要解包启动，
    单次开销约 1-3 秒。启动路径上原来会调用两次，这里做记忆化。
    """
    global _YTDLP_VERSION_CACHE
    if _YTDLP_VERSION_CACHE is not None:
        return _YTDLP_VERSION_CACHE

    version = "unknown"
    try:
        result = subprocess.run(
            [str(YTDLP_EXE), "--version"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="ignore",
            timeout=15
        )
        out = (result.stdout or "").strip()
        if out:
            version = out.splitlines()[0]
    except Exception:
        pass

    _YTDLP_VERSION_CACHE = version
    return version


# ============================================================
# JS 运行时（yt-dlp nsig 解密需要）
#
# 独立打包的 yt-dlp 不会自己从 PATH 找运行时，必须显式传绝对路径。
# 检测优先级：便携 node > 便携 deno > PATH > 常见安装目录。
# 都没有时可调用 install_portable_js_runtime() 下载 Deno 单文件版。
# ============================================================


def js_runtime_lookup():
    """检测可用的 JS 运行时，返回 (runtime, 绝对路径)；没有则返回 (None, "")"""
    import shutil
    if sys.platform == "win32":
        if PORTABLE_NODE_EXE.exists():
            return "node", str(PORTABLE_NODE_EXE)
        if PORTABLE_DENO_EXE.exists():
            return "deno", str(PORTABLE_DENO_EXE)
    for runtime, binary in (("node", "node"), ("deno", "deno"), ("bun", "bun")):
        path = shutil.which(binary)
        if path and os.path.exists(path):
            return runtime, str(Path(path).resolve())
    if sys.platform == "win32":
        for path in (r"C:\Program Files\nodejs\node.exe",
                     r"C:\Program Files (x86)\nodejs\node.exe"):
            if os.path.exists(path):
                return "node", path
    return None, ""


def install_portable_js_runtime(log=None):
    """下载 Deno 单文件版到 jsruntime\\deno.exe，返回 (ok, message)

    Deno 的 Windows 发布包是只含一个 exe 的 zip（约 40MB），
    解压出来即用，不需要安装器、不写注册表、不改 PATH。
    """
    import shutil
    import zipfile
    import urllib.request

    def _log(msg):
        if log:
            try:
                log(msg)
            except Exception:
                pass

    tmp_zip = None
    try:
        JS_RUNTIME_DIR.mkdir(parents=True, exist_ok=True)
        tmp_zip = JS_RUNTIME_DIR / "deno_download.zip"

        _log("[JS Runtime] 开始下载 Deno 便携版（约 40MB，走系统代理）…")
        req = urllib.request.Request(
            DENO_ZIP_URL,
            headers={"User-Agent": "man.py-js-runtime"}
        )
        with urllib.request.urlopen(req, timeout=180) as resp, open(tmp_zip, "wb") as f:
            while True:
                chunk = resp.read(256 * 1024)
                if not chunk:
                    break
                f.write(chunk)

        size_mb = tmp_zip.stat().st_size / 1024 / 1024
        if size_mb < 5:
            return False, f"下载内容异常（仅 {size_mb:.1f}MB），已放弃安装"

        _log(f"[JS Runtime] 下载完成（{size_mb:.1f}MB），正在解压…")

        new_exe = JS_RUNTIME_DIR / "deno.exe.new"
        with zipfile.ZipFile(tmp_zip) as zf:
            member = next(
                (n for n in zf.namelist() if n.lower().endswith("deno.exe")),
                None
            )
            if not member:
                return False, "压缩包内未找到 deno.exe"
            with zf.open(member) as src, open(new_exe, "wb") as dst:
                shutil.copyfileobj(src, dst)

        # 解压成功后才替换旧文件，中途失败不破坏已有运行时
        os.replace(str(new_exe), str(PORTABLE_DENO_EXE))

        version = "unknown"
        try:
            r = subprocess.run(
                [str(PORTABLE_DENO_EXE), "--version"],
                capture_output=True, text=True,
                encoding="utf-8", errors="ignore", timeout=30
            )
            out = (r.stdout or "").strip()
            if out:
                version = out.splitlines()[0]
        except Exception:
            pass

        return True, f"已安装便携版 JS 运行时：{PORTABLE_DENO_EXE}（{version}）"

    except Exception as e:
        return False, f"安装失败：{e}"

    finally:
        if tmp_zip is not None:
            try:
                tmp_zip.unlink()
            except Exception:
                pass


# ============================================================
# yt-dlp 版本检查（每天第一次启动查一次，后台线程，不阻塞任何流程）
#
# 只检查、不静默替换：发现新版本时交给 GUI 在空闲时弹提示条，
# 用户点「立即更新」才真正执行 ytdlp_perform_update()。
#
# 关闭方式：在运行目录建一个空的 ytdlp_noupdate.txt 后重启。
# ============================================================

YTDLP_UPDATE_STATE_FILE = TEMP_DIR / ".ytdlp_update_state.json"
YTDLP_UPDATE_DISABLE_FILE = RUN_DIR / "ytdlp_noupdate.txt"

YTDLP_EXE_URL = (
    "https://github.com/yt-dlp/yt-dlp/releases/latest/download/yt-dlp.exe"
)

try:
    YTDLP_UPDATE_CHECK = not YTDLP_UPDATE_DISABLE_FILE.exists()
except Exception:
    YTDLP_UPDATE_CHECK = False


def _ytdlp_latest_release_version():
    """查询 GitHub 最新 release 版本号，失败返回 None"""
    try:
        import urllib.request
        req = urllib.request.Request(
            "https://api.github.com/repos/yt-dlp/yt-dlp/releases/latest",
            headers={"User-Agent": "man.py-update-check"}
        )
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read().decode("utf-8", "ignore"))
        tag = str(data.get("tag_name") or "").strip()
        return tag or None
    except Exception:
        return None


def _load_ytdlp_update_state():
    """读取上次检查结果 {"date": "YYYY-MM-DD", "latest": "..."}"""
    try:
        if YTDLP_UPDATE_STATE_FILE.exists():
            data = json.loads(
                YTDLP_UPDATE_STATE_FILE.read_text(encoding="utf-8")
            )
            if isinstance(data, dict):
                return data
    except Exception:
        pass
    return {}


def _save_ytdlp_update_state(date_str, latest):
    try:
        YTDLP_UPDATE_STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        YTDLP_UPDATE_STATE_FILE.write_text(
            json.dumps({"date": date_str, "latest": latest}, ensure_ascii=False),
            encoding="utf-8"
        )
    except Exception:
        pass


def _version_is_newer(latest, local):
    """yt-dlp 版本号形如 2026.08.20，能解析成数字就按数字比，否则退化为不等判断"""
    if not latest or not local or local == "unknown":
        return False
    if latest == local:
        return False
    try:
        a = tuple(int(x) for x in latest.split("."))
        b = tuple(int(x) for x in local.split("."))
        return a > b
    except Exception:
        return True


def ytdlp_check_update_today(force=False):
    """每天第一次联网查一次最新版本

    返回 (local, latest, need_update, reason)
    reason: disabled / cached / checked / offline
    """
    local = get_ytdlp_version()

    if not YTDLP_UPDATE_CHECK and not force:
        return local, "", False, "disabled"

    today = time.strftime("%Y-%m-%d")
    state = _load_ytdlp_update_state()

    # 当天已经查过：直接用缓存结果，不再联网（同一天多次开关程序只查一次）
    if not force and state.get("date") == today:
        latest = str(state.get("latest") or "")
        return local, latest, _version_is_newer(latest, local), "cached"

    latest = _ytdlp_latest_release_version()
    if not latest:
        return local, "", False, "offline"

    _save_ytdlp_update_state(today, latest)
    return local, latest, _version_is_newer(latest, local), "checked"


def ytdlp_perform_update():
    """执行更新：优先 yt-dlp 自带 -U，失败回退 GitHub 下载替换"""
    try:
        r = subprocess.run(
            [str(YTDLP_EXE), "-U"],
            capture_output=True, text=True,
            encoding="utf-8", errors="ignore", timeout=300
        )
        out = ((r.stdout or "") + (r.stderr or "")).lower()
        if r.returncode == 0 and ("updated yt-dlp" in out or "latest version" in out):
            return True
    except Exception:
        pass
    # 回退：下载最新 exe 到临时文件，替换成功后才生效（失败保留旧版）
    try:
        import urllib.request
        tmp = Path(str(YTDLP_EXE) + ".new")
        req = urllib.request.Request(
            YTDLP_EXE_URL,
            headers={"User-Agent": "man.py-update-check"}
        )
        with urllib.request.urlopen(req, timeout=300) as resp, open(tmp, "wb") as f:
            f.write(resp.read())
        if tmp.stat().st_size > 1024 * 1024:
            backup = Path(str(YTDLP_EXE) + ".bak")
            try:
                os.replace(str(YTDLP_EXE), str(backup))
            except Exception:
                backup = None
            try:
                os.replace(str(tmp), str(YTDLP_EXE))
                return True
            except Exception:
                if backup and backup.exists():
                    try:
                        os.replace(str(backup), str(YTDLP_EXE))
                    except Exception:
                        pass
        try:
            tmp.unlink()
        except Exception:
            pass
    except Exception:
        pass
    return False


def reset_ytdlp_version_cache():
    """更新完 exe 后清掉版本缓存，下次读取重新探测"""
    global _YTDLP_VERSION_CACHE
    _YTDLP_VERSION_CACHE = None


def ytdlp_install_missing(log=None):
    """yt-dlp.exe 缺失时下载最新版到 YTDLP_EXE，返回 (ok, 版本号或错误信息)

    只在文件不存在时调用（YTDLP_EXE 此时就是 运行目录\\yt-dlp.exe）。
    先写 .new 临时文件，体积校验通过才改名，中途失败不会留下半个 exe。
    """
    import urllib.request

    def _log(msg):
        if log:
            try:
                log(msg)
            except Exception:
                pass

    tmp = Path(str(YTDLP_EXE) + ".new")
    try:
        YTDLP_EXE.parent.mkdir(parents=True, exist_ok=True)

        _log("[YT-DLP] 未检测到 yt-dlp.exe，开始下载最新版（约 18MB，走系统代理）…")
        req = urllib.request.Request(
            YTDLP_EXE_URL,
            headers={"User-Agent": "man.py-update-check"}
        )
        with urllib.request.urlopen(req, timeout=300) as resp, open(tmp, "wb") as f:
            while True:
                chunk = resp.read(256 * 1024)
                if not chunk:
                    break
                f.write(chunk)

        size_mb = tmp.stat().st_size / 1024 / 1024
        if size_mb < 5:
            return False, f"下载内容异常（仅 {size_mb:.1f}MB）"

        os.replace(str(tmp), str(YTDLP_EXE))
        refresh_tool_paths()
        reset_ytdlp_version_cache()

        version = get_ytdlp_version()
        _log(f"[YT-DLP] 下载完成（{size_mb:.1f}MB），版本 {version}")
        return True, version

    except Exception as e:
        return False, f"{e}"

    finally:
        try:
            if tmp.exists():
                tmp.unlink()
        except Exception:
            pass


def ffmpeg_version_text():
    """读取 ffmpeg 版本首行里的版本号，缺失或异常返回 unknown"""
    if not check_file(FFMPEG_EXE):
        return "unknown"
    try:
        r = subprocess.run(
            [str(FFMPEG_EXE), "-hide_banner", "-version"],
            capture_output=True, text=True,
            encoding="utf-8", errors="ignore", timeout=30
        )
        first = (r.stdout or "").strip().splitlines()
        if first:
            m = re.search(r"ffmpeg version (\S+)", first[0])
            return m.group(1) if m else first[0]
    except Exception:
        pass
    return "unknown"


def install_ffmpeg_bundle(log=None):
    """下载 gyan.dev 的 Windows FFmpeg 构建，解出 ffmpeg/ffprobe 到 ffmpeg\\bin

    返回 (ok, message)。压缩包约 105MB，解压后只保留 ffmpeg.exe / ffprobe.exe，
    两个 exe 都成功替换才算安装成功；中途失败不动已有文件。
    """
    import zipfile
    import urllib.request
    import shutil

    def _log(msg):
        if log:
            try:
                log(msg)
            except Exception:
                pass

    tmp_zip = None
    try:
        TEMP_DIR.mkdir(parents=True, exist_ok=True)
        FFMPEG_BIN_DIR.mkdir(parents=True, exist_ok=True)
        tmp_zip = TEMP_DIR / "ffmpeg_download.zip"

        _log("[FFmpeg] 开始下载 Windows 构建（约 105MB，走系统代理）…")
        req = urllib.request.Request(
            FFMPEG_ZIP_URL,
            headers={"User-Agent": "man.py-ffmpeg-install"}
        )
        downloaded = 0
        next_mark = 20 * 1024 * 1024
        with urllib.request.urlopen(req, timeout=600) as resp, open(tmp_zip, "wb") as f:
            while True:
                chunk = resp.read(256 * 1024)
                if not chunk:
                    break
                f.write(chunk)
                downloaded += len(chunk)
                if downloaded >= next_mark:
                    _log(f"[FFmpeg] 已下载 {downloaded / 1024 / 1024:.0f}MB…")
                    next_mark += 20 * 1024 * 1024

        size_mb = tmp_zip.stat().st_size / 1024 / 1024
        if size_mb < 20:
            return False, f"下载内容异常（仅 {size_mb:.1f}MB），已放弃安装"

        _log(f"[FFmpeg] 下载完成（{size_mb:.1f}MB），正在解压…")

        wanted = ("ffmpeg.exe", "ffprobe.exe")
        with zipfile.ZipFile(tmp_zip) as zf:
            names = zf.namelist()
            for exe in wanted:
                member = next(
                    (n for n in names
                     if n.lower().replace("\\", "/").endswith(f"bin/{exe}")),
                    None
                )
                if not member:
                    return False, f"压缩包内未找到 {exe}"

                target = FFMPEG_BIN_DIR / exe
                new_file = FFMPEG_BIN_DIR / (exe + ".new")
                with zf.open(member) as src, open(new_file, "wb") as dst:
                    shutil.copyfileobj(src, dst)
                # 解压成功后才替换旧文件，中途失败不破坏已有 FFmpeg
                os.replace(str(new_file), str(target))

        refresh_tool_paths()

        version = ffmpeg_version_text()
        _log(f"[FFmpeg] 安装完成：{FFMPEG_BIN_DIR}（{version}）")
        return True, f"已安装 FFmpeg / FFprobe 到 {FFMPEG_BIN_DIR}（{version}）"

    except Exception as e:
        return False, f"安装失败：{e}"

    finally:
        if tmp_zip is not None:
            try:
                tmp_zip.unlink()
            except Exception:
                pass


def check_ytdlp_update_async(callback=None, force=False):
    """后台线程做每日版本检查（非阻塞，不影响其它进程）

    callback(local, latest, need_update, reason) 在工作线程里调用，
    GUI 侧需要用信号转回主线程再动界面。
    """
    def _worker():
        try:
            local, latest, need_update, reason = ytdlp_check_update_today(force)
            task_debug(
                f"[yt-dlp] version={local} latest={latest or 'NA'} "
                f"update_check={reason} need_update={need_update}"
            )
            if callback:
                callback(local, latest, need_update, reason)
        except Exception:
            pass

    try:
        t = threading.Thread(target=_worker, daemon=True)
        t.start()
        return t
    except Exception:
        return None


def get_system_proxy_state():
    """检测系统代理状态（只记录，不绑定、不传给 yt-dlp）

    代理由系统 / v2rayN / TUN 负责；程序只在日志中记录 system/default。
    """
    try:
        if sys.platform == "win32":
            import winreg
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                                r"Software\Microsoft\Windows\CurrentVersion\Internet Settings") as k:
                enable, _ = winreg.QueryValueEx(k, "ProxyEnable")
                if enable:
                    server, _ = winreg.QueryValueEx(k, "ProxyServer")
                    return f"system({server})"
    except Exception:
        pass
    if os.environ.get("HTTP_PROXY") or os.environ.get("HTTPS_PROXY"):
        return "env"
    return "default"


# ============================================================
# 组件自检与归类
#
# required    必需：缺任意一项都无法完成下载，启动自检时自动下载补齐
# recommended 推荐：缺了还能跑，但会掉能力（解密不稳 / 没硬编码），提示条一键安装
# data        数据与配置：由浏览器扩展同步或系统提供，程序不下载
#
# installer 字段决定「能不能自动装」：ytdlp / ffmpeg / jsruntime / None
# ============================================================

COMPONENT_GROUPS = ("required", "recommended", "data")

COMPONENT_GROUP_TITLES = {
    "required": "必需组件",
    "recommended": "推荐组件",
    "data": "数据与配置",
}


def cookie_state():
    """Cookie 是否可用（统一文件 / 按域名分类 / 旧版 cookies.txt 任一即可）"""
    return (
        (COOKIES_DIR / "_all_cookies.txt").exists()
        or any((COOKIES_DIR / d / "_default.txt").exists()
               for d in ("tiktok.com", "youtube.com", "instagram.com"))
        or COOKIE_FILE.exists()
    )


def nvenc_state():
    """探测 ffmpeg 是否带 NVENC 硬编码器；ffmpeg 缺失或探测失败返回 UNKNOWN"""
    if not check_file(FFMPEG_EXE):
        return "UNKNOWN"
    try:
        r = subprocess.run(
            [str(FFMPEG_EXE), "-hide_banner", "-encoders"],
            capture_output=True, text=True,
            encoding="utf-8", errors="ignore", timeout=15
        )
        return "OK" if "h264_nvenc" in (r.stdout or "") else "NO"
    except Exception:
        return "UNKNOWN"


def collect_components(probe=True):
    """自检全部组件，按分类返回列表

    每项：group / name / ok / status / detail / path / installer
    probe=False 时跳过要拉子进程的探测（版本号、NVENC），只做文件存在性检查。
    """
    items = []

    # ---- 必需 ----
    items.append({
        "group": "required",
        "name": "Python",
        "ok": True,
        "status": f"OK ({sys.version.split()[0]})",
        "detail": "运行本程序的解释器（含 PyQt5 界面库）",
        "path": sys.executable,
        "installer": None,
    })

    ytdlp_ok = check_file(YTDLP_EXE)
    items.append({
        "group": "required",
        "name": "yt-dlp",
        "ok": ytdlp_ok,
        "status": (
            f"OK ({get_ytdlp_version()})" if ytdlp_ok and probe
            else "OK" if ytdlp_ok else "MISSING"
        ),
        "detail": "解析与下载核心，缺失则任何链接都下不了",
        "path": str(YTDLP_EXE),
        "installer": None if ytdlp_ok else "ytdlp",
    })

    ffmpeg_ok = check_file(FFMPEG_EXE)
    items.append({
        "group": "required",
        "name": "FFmpeg",
        "ok": ffmpeg_ok,
        "status": (
            f"OK ({ffmpeg_version_text()})" if ffmpeg_ok and probe
            else "OK" if ffmpeg_ok else "MISSING"
        ),
        "detail": "音视频合并与转码",
        "path": str(FFMPEG_EXE),
        "installer": None if ffmpeg_ok else "ffmpeg",
    })

    ffprobe_ok = check_file(FFPROBE_EXE)
    items.append({
        "group": "required",
        "name": "FFprobe",
        "ok": ffprobe_ok,
        "status": "OK" if ffprobe_ok else "MISSING",
        "detail": "读取时长 / 分辨率等媒体信息",
        "path": str(FFPROBE_EXE),
        "installer": None if ffprobe_ok else "ffmpeg",
    })

    # ---- 推荐 ----
    runtime, runtime_path = js_runtime_lookup()
    items.append({
        "group": "recommended",
        "name": "JS运行时",
        "ok": bool(runtime),
        "status": f"OK ({runtime})" if runtime else "MISSING",
        "detail": "YouTube nsig 解密；缺失会退回不稳定的 Python solver",
        "path": runtime_path or str(PORTABLE_DENO_EXE),
        "installer": None if runtime else "jsruntime",
    })

    nvenc = nvenc_state() if probe else "UNKNOWN"
    items.append({
        "group": "recommended",
        "name": "NVENC",
        "ok": nvenc == "OK",
        "status": nvenc,
        "detail": "N 卡硬编码加速，取决于显卡与 FFmpeg 构建，无法安装",
        "path": "",
        "installer": None,
    })

    # ---- 数据与配置 ----
    cookie_ok = cookie_state()
    items.append({
        "group": "data",
        "name": "Cookie",
        "ok": cookie_ok,
        "status": "OK" if cookie_ok else "MISSING",
        "detail": "由浏览器扩展同步，登录站点必需",
        "path": str(COOKIES_DIR),
        "installer": None,
    })
    items.append({
        "group": "data",
        "name": "UA",
        "ok": UA_FILE.exists(),
        "status": "OK" if UA_FILE.exists() else "MISSING",
        "detail": "浏览器 User-Agent，缺失时用内置默认值",
        "path": str(UA_FILE),
        "installer": None,
    })
    items.append({
        "group": "data",
        "name": "Extension",
        "ok": bool(UA_FILE.exists() or cookie_ok),
        "status": "OK" if (UA_FILE.exists() or cookie_ok) else "等待扩展同步",
        "detail": "浏览器扩展桥接状态",
        "path": "",
        "installer": None,
    })
    items.append({
        "group": "data",
        "name": "Proxy",
        "ok": True,
        "status": "SYSTEM",
        "detail": "代理由系统 / v2rayN / TUN 负责，程序不绑定端口",
        "path": "",
        "installer": None,
    })

    return items


def missing_required_installers(components=None):
    """必需组件里可自动安装的缺失项，去重后按 yt-dlp → ffmpeg 顺序返回"""
    items = components if components is not None else collect_components(probe=False)
    found = []
    for c in items:
        if c["group"] != "required" or c["ok"] or not c["installer"]:
            continue
        if c["installer"] not in found:
            found.append(c["installer"])
    order = ("ytdlp", "ffmpeg")
    return [name for name in order if name in found]


def check_environment():
    """启动环境检查，返回 [(项目, 状态), ...]

    分组标题以 (标题, "") 的形式插入，状态为空即代表这一行是分组标题。
    """
    rows = []
    components = collect_components()
    for group in COMPONENT_GROUPS:
        group_items = [c for c in components if c["group"] == group]
        if not group_items:
            continue
        rows.append((f"【{COMPONENT_GROUP_TITLES[group]}】", ""))
        for c in group_items:
            rows.append((c["name"], c["status"]))
    return rows



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
        "height": 650,
        "maximized": False
    },

    "output_dir": str(DEFAULT_OUTPUT_DIR),

    "video_size": "810x1080",

    "video_codec": "H.264 (libx264)",

    "links": [],

    "bridge_token": "",

    "bridge_port": 5999,

    # 被用户点「✕」忽略的 yt-dlp 版本号：同一版本不再弹提示条
    "ytdlp_update_skipped_version": "",

    # 用户拒绝安装便携版 JS 运行时后不再提示
    "js_runtime_install_skipped": False
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
# 默认端口 5999。若被占用则依次尝试 5999-6008，
# 扩展侧的自动发现范围（bridge-client.js 的 DEFAULT_PORT_RANGE）必须与此一致。
# ============================================================

BRIDGE_DEFAULT_PORT = 5999
BRIDGE_PORT_RANGE = list(range(BRIDGE_DEFAULT_PORT, BRIDGE_DEFAULT_PORT + 10))
BRIDGE_PROTOCOL_VERSION = 1


def is_downloadable_video_url(url):
    """基本 URL 检查，接受所有 http(s) 链接"""
    if not url:
        return False
    url = url.strip()
    return url.startswith("http://") or url.startswith("https://")


class OmniGetBridgeHandler(BaseHTTPRequestHandler):
    """处理 OmniGet 扩展的 HTTP 请求"""

    # 高频轮询类端点：成功时不记访问日志（否则每 2 分钟刷屏一次）
    QUIET_ACCESS_PATHS = ("/v1/cookies", "/v1/ua", "/v1/health")

    def log_message(self, format, *args):
        """静默日志，输出到 bridge 日志（高频端点成功时不记录）"""
        if not self.server.bridge:
            return

        line = f"{format % args}"

        if " 200 " in f" {line} " or line.endswith(" 200 -"):
            if any(p in line for p in self.QUIET_ACCESS_PATHS):
                return

        self.server.bridge.log(f"{self.client_address[0]} - {line}")

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

            # 扩展同时发送页面 Referer：按域名保存，供 yt-dlp 作为通用请求参数使用
            referer = str(data.get("referer", "") or "").strip()
            if referer:
                try:
                    self.server.bridge.save_referer(referer)
                except Exception as e:
                    self.server.bridge.log(f"Referer 保存失败：{e}")

            # 扩展可选发送附加 HTTP Headers（自动排除 Cookie/UA/Referer，由程序单独管理）
            extra_headers = data.get("headers", None)
            if extra_headers:
                try:
                    self.server.bridge.save_extra_headers(extra_headers, url or referer)
                except Exception as e:
                    self.server.bridge.log(f"附加 Headers 保存失败：{e}")

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

            # 扩展的「下载时打开应用」开关：为 true 时把主窗口拉到前台
            if data.get("openApp") is True:
                self.server.bridge.activate_app()

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

            # 自动捕获模式下用来源页面 URL 作为 Referer 保存（不伪造，无则不写）
            if source_url:
                try:
                    self.server.bridge.save_referer(source_url)
                except Exception as e:
                    self.server.bridge.log(f"Referer 保存失败：{e}")

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
                 url_callback=None, cookies_callback=None, ua_callback=None,
                 activate_callback=None):
        self.token = token or secrets.token_urlsafe(24)
        self.port = port
        self.link_file = link_file or LINK_FILE
        self.log_callback = log_callback
        self.url_callback = url_callback  # 收到 URL 时的回调
        self.cookies_callback = cookies_callback  # 收到 Cookie 时的回调
        self.ua_callback = ua_callback  # 收到 UA 时的回调
        self.activate_callback = activate_callback  # 需要把窗口拉到前台时的回调
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

    def activate_app(self):
        """扩展的「下载时打开应用」开关：把主窗口拉到前台"""
        if self.activate_callback:
            try:
                self.activate_callback()
            except Exception as e:
                self.log(f"窗口激活回调执行失败：{e}")

    def save_referer(self, referer):
        """按根域名保存扩展同步的页面 Referer → cookies/<域名>/_referer.txt

        全平台通用；下载时若文件存在则作为 --referer 传给 yt-dlp，
        不存在时不伪造。失败仅记日志，不影响其他流程。
        """
        try:
            referer = str(referer or "").strip()
            if not referer:
                return
            host = CookieManager._extract_host(referer)
            root = CookieManager.root_domain_of(host)
            if not root:
                return
            seg = CookieManager._safe_domain_segment(root)
            target_dir = COOKIES_DIR / seg
            target_dir.mkdir(parents=True, exist_ok=True)
            write_text_file(target_dir / "_referer.txt", referer)
            # 同一 Referer 反复上报时只记一次日志，避免日志霸屏
            if referer != getattr(self, "_last_referer", None):
                self._last_referer = referer
                self.log(f"Referer 已更新：{seg}")
        except Exception as e:
            self.log(f"Referer 保存失败：{e}")

    # 扩展附加 Headers 中明确排除的项（由程序单独管理，避免重复/互相覆盖）
    EXTRA_HEADERS_EXCLUDED = ("cookie", "user-agent", "referer")

    def save_extra_headers(self, headers, source_url):
        """按根域名保存扩展附加 HTTP Headers → cookies/<域名>/_headers.txt

        每行格式：Name: value；自动排除 Cookie / User-Agent / Referer。
        """
        try:
            if isinstance(headers, dict):
                items = headers.items()
            elif isinstance(headers, list):
                items = [(h.get("name", ""), h.get("value", "")) for h in headers if isinstance(h, dict)]
            else:
                return
            lines = []
            for name, value in items:
                name = str(name or "").strip()
                value = str(value or "").strip()
                if not name or not value:
                    continue
                if name.lower() in OmniGetBridge.EXTRA_HEADERS_EXCLUDED:
                    continue
                lines.append(f"{name}: {value}")
            if not lines:
                return
            host = CookieManager._extract_host(source_url or "")
            root = CookieManager.root_domain_of(host)
            if not root:
                return
            seg = CookieManager._safe_domain_segment(root)
            target_dir = COOKIES_DIR / seg
            target_dir.mkdir(parents=True, exist_ok=True)
            write_text_file(target_dir / "_headers.txt", "\n".join(lines))
            self.log(f"附加 Headers 已更新：{seg}（{len(lines)} 项）")
        except Exception as e:
            self.log(f"附加 Headers 保存失败：{e}")

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

    if "twitter.com" in u or "//x.com" in u or ".x.com" in u:
        return "twitter"

    # 海外短视频平台扩展
    if "vimeo.com" in u:
        return "vimeo"

    if "reddit.com" in u:
        return "reddit"

    if "pinterest." in u:
        return "pinterest"

    if "twitch.tv" in u:
        return "twitch"

    if "dailymotion.com" in u or "dai.ly" in u:
        return "dailymotion"

    if "snapchat.com" in u:
        return "snapchat"

    return "other"


# 通用平台 other 的 DNA 提取：忽略无意义的通用路径片段（列表页/播放页等）
_GENERIC_PATH_SEGMENTS = {
    "watch", "video", "videos", "home", "index", "play", "player",
    "detail", "details", "share", "embed", "page", "main", "feed",
    "channel", "channels", "user", "users", "profile", "search",
    "default", "view", "content", "media", "clip", "clips",
    "shorts", "live", "story", "stories", "post", "posts", "reel", "reels",
}


def extract_video_id_from_url(url):
    """从 URL 中提取视频 ID（用于 DNA 去重，覆盖海外主流短视频平台）
    返回 (platform, video_id) 或 None
    """
    try:
        u = url.lower().strip()
        import re
        if "tiktok.com" in u:
            # https://www.tiktok.com/@user/video/7486922641006202154
            m = re.search(r'/video/(\d+)', url)
            if m:
                return ("tiktok", m.group(1))
        elif "youtube.com" in u or "youtu.be" in u:
            # shorts, watch, embed, live, v=, youtu.be/ID
            m = re.search(r'/(?:shorts|watch|embed|live)/([A-Za-z0-9_-]+)', url)
            if m:
                return ("youtube", m.group(1))
            m = re.search(r'[?&]v=([A-Za-z0-9_-]+)', url)
            if m:
                return ("youtube", m.group(1))
            m = re.search(r'youtu\.be/([A-Za-z0-9_-]{5,})', url)
            if m:
                return ("youtube", m.group(1))
        elif "facebook.com" in u or "fb.watch" in u:
            # watch?v=123456、/videos/123456、/reel/123456
            m = re.search(r'[?&]v=(\d+)', url)
            if m:
                return ("facebook", m.group(1))
            m = re.search(r'/(?:videos|reel)/(\d+)', url)
            if m:
                return ("facebook", m.group(1))
        elif "instagram.com" in u:
            m = re.search(r'/(?:p|reel|tv)/([A-Za-z0-9_-]+)', url)
            if m:
                return ("instagram", m.group(1))
        elif "twitter.com" in u or "//x.com" in u or ".x.com" in u:
            # https://x.com/user/status/1234567890
            m = re.search(r'/status/(\d+)', url)
            if m:
                return ("twitter", m.group(1))
        elif "vimeo.com" in u:
            # vimeo.com/123456789、/channels/xxx/123456、/video/123456
            m = re.search(r'vimeo\.com/(?:channels/[^/]+/|video/)?(\d+)', url)
            if m:
                return ("vimeo", m.group(1))
        elif "reddit.com" in u:
            # /r/xxx/comments/abc123/title
            m = re.search(r'/comments/([a-zA-Z0-9]+)', url)
            if m:
                return ("reddit", m.group(1))
        elif "pinterest." in u:
            # /pin/123456789/（含各国域名如 pinterest.co.uk）
            m = re.search(r'/pin/(\d+)', url)
            if m:
                return ("pinterest", m.group(1))
        elif "twitch.tv" in u:
            # /videos/123456、/clip/AbcDef、clips.twitch.tv/AbcDef
            m = re.search(r'/(?:videos|clip)/([A-Za-z0-9_-]+)', url)
            if m:
                return ("twitch", m.group(1))
            m = re.search(r'clips\.twitch\.tv/([A-Za-z0-9_-]+)', url)
            if m:
                return ("twitch", m.group(1))
        elif "dailymotion.com" in u or "dai.ly" in u:
            # /video/x8abc123、dai.ly/x8abc123
            m = re.search(r'(?:/video/|dai\.ly/)(x[a-z0-9]+)', url)
            if m:
                return ("dailymotion", m.group(1))
        elif "snapchat.com" in u:
            # /spotlight/xxx、story.snapchat.com/@user/xxx
            m = re.search(r'/spotlight/([A-Za-z0-9_-]+)', url)
            if m:
                return ("snapchat", m.group(1))
            m = re.search(r'story\.snapchat\.com/@[^/]+/([A-Za-z0-9_-]+)', url)
            if m:
                return ("snapchat", m.group(1))
        else:
            # 未识别平台 → 归类 other：取链接最后一段无特殊符号（纯字母/数字/_/-）的内容作为标记，同样支持去重
            from urllib.parse import urlparse
            path = urlparse(url.strip()).path
            for seg in reversed([s for s in path.split('/') if s]):
                if re.fullmatch(r'[A-Za-z0-9_-]{4,80}', seg) and seg.lower() not in _GENERIC_PATH_SEGMENTS:
                    return ("other", seg)
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

    @override
    def event(self, a0):

        if a0 is None:
            return False

        event_type = a0.type()

        if event_type in (
            QEvent.DragEnter,
            QEvent.DragMove
        ):

            folder = self.get_dropped_folder(
                a0.mimeData()
            )

            if folder:

                a0.setDropAction(
                    Qt.CopyAction
                )

                a0.accept()
                return True

            a0.ignore()
            return True

        if event_type == QEvent.Drop:

            folder = self.get_dropped_folder(
                a0.mimeData()
            )

            if folder:

                self.setText(folder)
                self.setCursorPosition(len(folder))
                self.setFocus(Qt.MouseFocusReason)

                self.folder_dropped.emit(folder)

                a0.setDropAction(
                    Qt.CopyAction
                )

                a0.accept()
                return True

            a0.ignore()
            return True

        return super().event(a0)

    # --------------------------------------------------------
    # 鼠标拖入
    # --------------------------------------------------------

    @override
    def dragEnterEvent(self, a0):

        if a0 is None:
            return

        folder = self.get_dropped_folder(
            a0.mimeData()
        )

        if folder:

            a0.setDropAction(
                Qt.CopyAction
            )
            a0.accept()
            return

        a0.ignore()

    # --------------------------------------------------------
    # 鼠标拖动经过输入框
    # --------------------------------------------------------

    @override
    def dragMoveEvent(self, a0):

        if a0 is None:
            return

        folder = self.get_dropped_folder(
            a0.mimeData()
        )

        if folder:

            a0.setDropAction(
                Qt.CopyAction
            )
            a0.accept()
            return

        a0.ignore()

    # --------------------------------------------------------
    # 鼠标离开
    # --------------------------------------------------------

    @override
    def dragLeaveEvent(self, a0):
        if a0 is not None:
            a0.accept()

    # --------------------------------------------------------
    # 松开鼠标
    # --------------------------------------------------------

    @override
    def dropEvent(self, a0):

        if a0 is None:
            return

        folder = self.get_dropped_folder(
            a0.mimeData()
        )

        if folder:

            self.setText(folder)
            self.setCursorPosition(len(folder))
            self.setFocus(Qt.MouseFocusReason)

            self.folder_dropped.emit(folder)

            a0.setDropAction(
                Qt.CopyAction
            )
            a0.accept()
            return

        a0.ignore()


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

        # 进度映射状态（详见 _reset_url_progress）
        self._prog_done = 0
        self._prog_total = 1
        self._prog_streams = 0
        self._prog_expected = 1
        self._prog_value = 0

        # 下载详情日志节流状态（详见 _log_download_detail）
        self._detail_ts = 0.0
        self._detail_pct = -1.0

    # --------------------------------------------------------
    # 进度映射
    #
    # yt-dlp 打印的百分比是「当前媒体流」的进度，不是整体进度：
    # YouTube 走 bv*+ba 会先下视频流 0→100，再下音频流 0→100，
    # 直接透传会让进度条走满后倒回去重走一遍。
    #
    # 这里统一折算成「整批任务」的百分比：
    #   (已完成 URL 数 + 当前 URL 内部完成比例) / URL 总数
    # 并保证单调不回退。
    # --------------------------------------------------------

    _PROGRESS_RE = re.compile(r"\[download\]\s+(\d{1,3}(?:\.\d+)?)%")

    # 详情行：[download]  45.2% of  120.34MiB at    5.67MiB/s ETA 00:13
    _PROGRESS_DETAIL_RE = re.compile(
        r"\[download\]\s+(?P<pct>\d{1,3}(?:\.\d+)?)%"
        r"(?:\s+of\s+~?\s*(?P<size>\S+))?"
        r"(?:\s+at\s+(?P<speed>\S+))?"
        r"(?:\s+ETA\s+(?P<eta>\S+))?"
    )

    # ffmpeg 转码进度：从它自己的输出里取总时长与已处理时长
    _FFMPEG_DURATION_RE = re.compile(r"Duration:\s+(\d+:\d\d:\d\d(?:\.\d+)?)")
    _FFMPEG_TIME_RE = re.compile(r"time=(\d+:\d\d:\d\d(?:\.\d+)?)")

    @staticmethod
    def _hms_to_seconds(text):
        """把 HH:MM:SS.xx 转成秒，解析失败返回 0"""
        try:
            hours, minutes, seconds = str(text).split(":")
            return int(hours) * 3600 + int(minutes) * 60 + float(seconds)
        except Exception:
            return 0.0

    def _log_download_detail(self, line):
        """把 yt-dlp 的进度行整理成一条人看得懂的日志（精简模式下也显示）

        每秒最多一条，百分比跳变 ≥5% 时补一条，100% 一定收尾输出一条，
        既能看到实时速度/剩余时间，又不会把日志刷爆。
        """
        match = self._PROGRESS_DETAIL_RE.search(line)
        if not match:
            return

        try:
            pct = float(match.group("pct"))
        except (TypeError, ValueError):
            return

        now = time.time()
        if pct < 100.0:
            if now - self._detail_ts < 1.0 and pct - self._detail_pct < 5.0:
                return

        self._detail_ts = now
        self._detail_pct = pct

        parts = [f"{pct:.1f}%"]

        size = match.group("size")
        if size:
            parts.append(f"共 {size}")

        speed = match.group("speed")
        if speed and speed.lower() != "unknown":
            parts.append(speed)

        eta = match.group("eta")
        if eta and eta.lower() != "unknown":
            parts.append(f"剩余 {eta}")

        stream = ""
        if self._prog_expected > 1:
            current = min(max(self._prog_streams, 1), self._prog_expected)
            stream = f"流 {current}/{self._prog_expected} · "

        self.log(f"[下载] {stream}" + " · ".join(parts))

    def _download_stage_text(self, line):
        """把 yt-dlp 的阶段行翻译成中文提示，非阶段行返回 None"""
        if line.startswith("[download] Destination:"):
            name = line.split("Destination:", 1)[1].strip()
            return f"[下载] 开始下载：{Path(name).name}"

        if "has already been downloaded" in line:
            return "[下载] 文件已存在，跳过下载"

        if line.startswith("[Merger]"):
            return "[下载] 音视频下载完成，正在合并…"

        if line.startswith("[ExtractAudio]"):
            return "[下载] 正在提取音轨…"

        return None

    def _reset_url_progress(self, index, total):
        """开始处理某个 URL 时重置进度基准（index 从 1 开始）"""
        self._prog_total = max(int(total), 1)
        self._prog_done = min(max(int(index) - 1, 0), self._prog_total)
        self._prog_streams = 0
        self._prog_expected = 1
        self._prog_value = int(self._prog_done / self._prog_total * 100)
        self._detail_ts = 0.0
        self._detail_pct = -1.0

    def _emit_url_progress(self, fraction):
        """fraction 为当前 URL 的完成比例（0.0-1.0）

        下载完成后还有合并 / 转码 / 校验，所以下载阶段最高只走到 95%，
        剩下的 5% 留给后处理，由 _sync_url_progress 在该 URL 结束时补齐。
        """
        fraction = min(max(float(fraction), 0.0), 0.95)

        value = int(
            (self._prog_done + fraction) / self._prog_total * 100
        )
        value = min(max(value, 0), 100)

        if value > self._prog_value:
            self._prog_value = value
            self.progress_signal.emit(value)

    def _sync_url_progress(self, value):
        """URL 处理结束时把基准对齐到已完成比例"""
        value = min(max(int(value), 0), 100)
        self._prog_value = value
        self.progress_signal.emit(value)

    def _emit_post_progress(self, fraction):
        """后处理（转码）阶段：把当前 URL 的进度从 95% 推到 100%"""
        fraction = min(max(float(fraction), 0.0), 1.0)

        value = int(
            (self._prog_done + 0.95 + 0.05 * fraction)
            / self._prog_total * 100
        )
        value = min(max(value, 0), 100)

        if value > self._prog_value:
            self._prog_value = value
            self.progress_signal.emit(value)

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

    def get_referer(self, url):
        """获取该 URL 对应平台的请求 Referer（全平台通用）

        优先使用扩展同步的实际页面地址（cookies/<根域名>/_referer.txt）；
        缺失时返回 None，不伪造、不为此打开浏览器。
        """
        try:
            root = CookieManager.root_domain_of(
                CookieManager._extract_host(url)
            )
            if root:
                referer = read_text_file(
                    self.cookies_dir / root / "_referer.txt"
                )
                if referer:
                    return referer
        except Exception:
            pass
        return None

    def get_extra_headers(self, url):
        """读取扩展同步的附加 HTTP Headers（全平台通用）

        来源：cookies/<根域名>/_headers.txt（每行 Name: value）；
        保存时已排除 Cookie / User-Agent / Referer，这里再双保险过滤一次。
        返回 ["Name: value", ...]，无则返回空列表。
        """
        headers = []
        try:
            root = CookieManager.root_domain_of(
                CookieManager._extract_host(url)
            )
            if not root:
                return headers
            text = read_text_file(self.cookies_dir / root / "_headers.txt")
            if not text:
                return headers
            for line in text.splitlines():
                line = line.strip()
                if not line or ":" not in line:
                    continue
                name = line.split(":", 1)[0].strip()
                if name.lower() in ("cookie", "user-agent", "referer"):
                    continue
                headers.append(line)
        except Exception:
            pass
        return headers

    def resolve_cookie_file(self, url):
        """解析扩展同步的原始 Cookie 文件路径（只查找，不产生日志）

        优先级与 get_cookie_args 一致：统一 _all_cookies.txt → 按域名 →
        平台关联域名 → CDN 映射 → 旧版 cookies.txt；都不存在返回 None。
        """
        try:
            all_file = self.cookies_dir / "_all_cookies.txt"
            if all_file.exists():
                return all_file
            mgr = CookieManager(self.cookies_dir)
            if url:
                root = mgr.root_domain_of(mgr._extract_host(url))
                cookie_file = self.cookies_dir / root / "_default.txt"
                if cookie_file.exists():
                    return cookie_file
                platform = mgr._detect_platform(root)
                for domain in PLATFORM_COOKIE_DOMAINS.get(platform, []):
                    alt_file = self.cookies_dir / domain.lstrip(".") / "_default.txt"
                    if alt_file.exists():
                        return alt_file
                primary = CDN_TO_PRIMARY_DOMAIN.get(root)
                if primary:
                    primary_file = self.cookies_dir / primary / "_default.txt"
                    if primary_file.exists():
                        return primary_file
            if COOKIE_FILE.exists():
                return COOKIE_FILE
        except Exception:
            pass
        return None

    @staticmethod
    def make_session_cookie_copy(src_path, task_id):
        """复制扩展 Cookie 为临时会话副本，供 yt-dlp 使用

        yt-dlp 可能回写/修改 --cookies 文件，用副本避免破坏扩展原始文件；
        原始文件只读不写。复制失败时退回原始文件（不影响下载）。
        """
        try:
            src = Path(src_path)
            if not src.exists():
                return None
            TEMP_DIR.mkdir(parents=True, exist_ok=True)
            dst = TEMP_DIR / f"_session_cookie_{task_id}.txt"
            dst.write_bytes(src.read_bytes())
            return dst
        except Exception:
            try:
                return Path(src_path)
            except Exception:
                return None

    @staticmethod
    def remove_session_cookie_copy(path, original=None):
        """任务结束后删除临时 Cookie 副本（绝不删除扩展原始文件）"""
        try:
            if not path:
                return
            p = Path(path)
            if original and p == Path(original):
                return
            if p.name.startswith("_session_cookie_") and p.parent == TEMP_DIR:
                p.unlink()
        except Exception:
            pass

    def js_runtime_info(self):
        """检测 JS 运行时，返回 (runtime, 绝对路径)；不存在返回 (None, "")

        统一走模块级 js_runtime_lookup()，与环境检查、提示条安装共用同一套优先级。
        """
        return js_runtime_lookup()

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
        """检测可用的 JS 运行时（用于 yt-dlp nsig 解密）

        统一走 js_runtime_info()：检测绝对路径并验证文件存在，
        确保真正以 --js-runtimes runtime:path 传给 yt-dlp。
        """
        runtime, path = self.js_runtime_info()
        if runtime:
            self.log(f"[JS Runtime] runtime={runtime} path={path} enabled=true")
            return f"{runtime}:{path}"
        self.log("[JS Runtime] enabled=false（未检测到 JS 运行时）")
        return None

    def build_ytdlp_args(self, url, job_dir, ua, cookie_args, player_client=None,
                         referer=None, concurrency=None, extra_args=None,
                         simplify_format=False):
        """构建 yt-dlp 命令参数（统一 Runner）

        支持 YouTube player_client / 通用 Referer 双通道 / 扩展附加 Headers /
        JS Runtime / TikTok 并发配置；代理不主动设置，遵循系统环境代理。

        extra_args：重试时按错误追加的参数（如 --force-ipv4）。
        simplify_format：去掉 -f 与 --merge-output-format，退回 yt-dlp 默认格式，
        用于「requested format not available」这类只能靠放弃指定格式解决的失败。
        """
        self.log(f"[DEBUG] build_ytdlp_args 被调用: {url[:60]}...")
        output_template = job_dir / "source.%(ext)s"

        is_youtube = detect_platform(url) == "youtube"

        # 格式选择器：YouTube 走 m4a 优先的选择器（合并 mp4 时音频免转码）
        if simplify_format:
            format_args = []
        elif is_youtube:
            format_args = ["-f", YOUTUBE_FORMAT_SELECTOR,
                           "--merge-output-format", "mp4"]
        else:
            format_args = ["-f", "bv*+ba/b", "--merge-output-format", "mp4"]

        cmd = [
            str(YTDLP_EXE),
            "--newline",
            "--no-warnings",
            "--no-playlist",
            "--no-check-certificates",
            "--no-mtime",
            "--encoding", "utf-8",
            "--user-agent", ua,
            "--ffmpeg-location", str(FFMPEG_EXE),
            *format_args,
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

        # JS 运行时（用于 nsig 解密）：必须传绝对路径，独立打包的 yt-dlp 无法自己发现 PATH
        js_runtime = self.detect_js_runtime()
        if js_runtime:
            # 先清除默认的 JS 运行时，再添加我们检测到的（避免 yt-dlp 使用低优先级的 deno）
            cmd.extend(["--no-js-runtimes", "--js-runtimes", js_runtime])
            self.log(f"[yt-dlp] 使用 JS 运行时：{js_runtime}")
        else:
            self.log("[yt-dlp] ⚠ 未检测到 JS 运行时，将使用 native Python solver（可能不稳定）")

        cmd.extend(cookie_args)

        # Referer：全平台通用请求参数。有扩展同步的有效值时传递，缺失不伪造。
        # 双通道：--referer（提取器请求）+ --add-header（媒体/分片请求），值相同不冲突。
        if referer:
            cmd.extend(["--referer", referer])
            cmd.extend(["--add-header", f"Referer: {referer}"])

        # 扩展附加 Headers（已排除 Cookie/UA/Referer，由程序单独管理，避免重复覆盖）
        for header in self.get_extra_headers(url):
            cmd.extend(["--add-header", header])

        # 非 YouTube 平台：分块下载（参考 OmniGet；YouTube 不加以避免触发风控）
        if not is_youtube:
            cmd.extend(["--http-chunk-size", "10M"])

        # TikTok 特定参数：添加完整的浏览器请求头
        is_tiktok_cmd = detect_platform(url) == "tiktok"
        if is_tiktok_cmd:
            # 模拟 Chrome 浏览器的完整请求头（Referer 已在上方通用处理，不重复）
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
            
            # TikTok 并发片段数：默认 8，下载阶段失败后降为 4（仅作下载阶段并发降级）
            workers = concurrency or 8
            cmd.extend(["-N", str(workers)])
            self.log(f"[yt-dlp] TikTok 模式：启用 {workers} 个并发片段")

        # YouTube 特定参数（对齐 OmniGet 的 download_video 基线）
        if is_youtube:
            # 只在没被上层级联改写时才带 player_client；
            # simplify_format 时 OmniGet 会把 player_client 一起摘掉，交给默认逻辑
            if not simplify_format:
                client = player_client or "default"
                cmd.extend(["--extractor-args", f"youtube:player_client={client}"])

            # 并发分片：YouTube 的 DASH 流是分片下载，单连接跑不满带宽。
            # 被 429 过的话自动收敛，避免越重试越被限。
            workers = concurrency or youtube_concurrent_fragments()
            cmd.extend(["-N", str(workers)])

            # --throttled-rate 不是限速，而是「速度低于该值就判定被限速并重新提取」。
            cmd.extend(["--throttled-rate", YOUTUBE_THROTTLED_RATE])

        # 重试级联追加的参数（--force-ipv4 / --restrict-filenames / 备用 client 等）
        if extra_args:
            cmd.extend(extra_args)

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

    def _plan_youtube_retry(self, output_text, attempt, state):
        """按 yt-dlp 的真实报错决定下一次要改什么参数（OmniGet 的错误级联）

        直接改写 state：player_client / extra_args / simplify_format / wait。
        返回 True 表示参数确实变了、值得再试；False 表示这个失败换参数也没用。
        """
        global _youtube_429_hits

        text = (output_text or "").lower()
        extra = state.setdefault("extra_args", [])
        changed = False

        if "http error 429" in text:
            _youtube_429_hits += 1
            # 限流退避比普通网络重试激进得多：10s / 20s / 40s
            state["wait"] = 10 * (2 ** attempt)
            client = "mweb" if attempt == 0 else "ios"
            state["player_client"] = client
            self.log(f"[yt-dlp] 429 限流：等待 {state['wait']}s 并切到 "
                     f"player_client={client}（-N 收敛到 "
                     f"{youtube_concurrent_fragments()}）")
            changed = True
        elif "nsig" in text:
            # nsig 解密失败：换一个不需要解签名的 client
            client = "ios" if attempt == 0 else "mweb"
            state["player_client"] = client
            self.log(f"[yt-dlp] nsig 解密失败：切到 player_client={client}")
            changed = True

        # SABR-only：web 提取器给的格式普通下载器拉不动，按级联换 client
        if "sabr" in text and attempt < len(YOUTUBE_SABR_CLIENT_CASCADE):
            client = YOUTUBE_SABR_CLIENT_CASCADE[attempt]
            state["player_client"] = client
            self.log(f"[yt-dlp] 只剩 SABR 格式：切到 player_client={client}")
            changed = True

        if ("http error 403" in text or "forbidden" in text) \
                and "--force-ipv4" not in extra:
            extra.append("--force-ipv4")
            self.log("[yt-dlp] 403 Forbidden：追加 --force-ipv4")
            changed = True

        if ("invalid argument" in text or "errno 22" in text) \
                and "--restrict-filenames" not in extra:
            extra.append("--restrict-filenames")
            self.log("[yt-dlp] 文件名被系统拒绝：追加 --restrict-filenames")
            changed = True

        # 指定格式拿不到 / 后处理失败：放弃 -f 与 player_client，退回 yt-dlp 默认。
        # 只做一次，简化过还失败说明不是格式的问题。
        if (("requested format" in text and "not available" in text)
                or "postprocessing" in text) and not state.get("simplify_format"):
            state["simplify_format"] = True
            state["player_client"] = None
            self.log("[yt-dlp] 格式/后处理失败：去掉 -f 与 player_client，"
                     "退回 yt-dlp 默认格式")
            changed = True

        # PO Token：本地没有 provider 就换不出来，如实报出来而不是假装重试
        if youtube_pot_missing(text):
            self.log("[yt-dlp] ⚠ YouTube 要求 PO Token，本机未配置 provider。"
                     "换 client 只是碰运气，稳定方案是登录 Cookie 或自建 "
                     "bgutil-ytdlp-pot-provider。")

        return changed

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
        """下载视频源

        TikTok：并发降级 -N 8 → -N 4（见 _download_tiktok），不预请求、不依赖浏览器是否打开
        其他平台：原版流程（预请求 + 网络波动自动重试，最多 3 次）
        所有任务都以 task_id 记录到 logs/tiktok_debug.log（已脱敏）
        """
        platform = detect_platform(url)

        if platform == "tiktok":
            return self._download_tiktok(url, job_dir)

        return self._download_generic(url, job_dir, platform)

    def _download_generic(self, url, job_dir, platform):
        """原版下载流程（非 TikTok 平台）：预请求 + 网络重试，叠加任务日志"""
        task_id = secrets.token_hex(3).upper()
        task_start = time.time()
        self._task_log_start(task_id, url, platform)

        ua = self.get_ua()
        referer = self.get_referer(url)
        is_youtube = platform == "youtube"

        # Cookie 临时副本：避免 yt-dlp 回写破坏扩展原始 Cookie 文件（原始只读）
        source_cookie_file = self.resolve_cookie_file(url)
        session_cookie_file = None
        if source_cookie_file:
            session_cookie_file = self.make_session_cookie_copy(source_cookie_file, task_id)
            cookie_args = ["--cookies", str(session_cookie_file or source_cookie_file)]
        else:
            cookie_args = []

        # 需要预请求的平台列表（模拟用户点击，避免被识别为机器人）
        # 注意：TikTok 不做任何预请求/后台访问，浏览器活跃由用户 + 扩展负责
        platforms_need_prefetch = ["instagram", "twitter", "x", "reddit"]
        
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

        result = "FAILED"
        final_class = None
        attempts = 0
        # YouTube 重试状态：由 _plan_youtube_retry 按 stderr 逐次改写
        yt_state = {}

        try:
            # 网络重试循环（原版）
            for net_attempt in range(self.NETWORK_MAX_RETRIES + 1):
                if self.stop_flag or self.skip_flag:
                    result = "ABORTED"
                    return False

                # 网络重试时清理残留文件，确保 yt-dlp 从头开始
                if net_attempt > 0:
                    delay = self.NETWORK_RETRY_DELAYS[min(net_attempt - 1, len(self.NETWORK_RETRY_DELAYS) - 1)]
                    # 429 退避比普通网络重试长，取两者较大值
                    delay = max(delay, yt_state.pop("wait", 0))
                    self.log(f"")
                    self.log(f"[网络重试 {net_attempt}/{self.NETWORK_MAX_RETRIES}] 等待 {delay} 秒后重试...")
                    time.sleep(delay)
                    self._cleanup_job_dir(job_dir)
                    # 网络重试不刷新 Cookie（保留当前会话副本，避免无脑刷新）

                attempts = net_attempt + 1
                self._task_log_request(task_id, cookie_args, ua, referer, attempts, None)

                cmd = self.build_ytdlp_args(
                    url, job_dir, ua, cookie_args,
                    player_client=yt_state.get("player_client"),
                    referer=referer,
                    extra_args=yt_state.get("extra_args"),
                    simplify_format=yt_state.get("simplify_format", False),
                )

                if net_attempt == 0:
                    self.log(f"开始下载：{url}")
                else:
                    self.log(f"网络重试（第 {net_attempt} 次）：{url}")

                ok, output_text = self._run_ytdlp(cmd)

                if ok:
                    result = "SUCCESS"
                    return True

                if self.stop_flag or self.skip_flag:
                    result = "ABORTED"
                    return False

                # 分析错误原因
                stderr_text = output_text.lower()

                # YouTube：先按 OmniGet 的错误级联调参数（换 player_client /
                # 追加 --force-ipv4 / 退回默认格式），再决定是否重试
                if is_youtube and net_attempt < self.NETWORK_MAX_RETRIES:
                    if self._plan_youtube_retry(output_text, net_attempt, yt_state):
                        continue

                # 判断是否可重试的网络错误（原版）
                if self._is_network_error(stderr_text):
                    if net_attempt < self.NETWORK_MAX_RETRIES:
                        self.log(f"[网络] 下载失败（疑似网络波动），将自动重试")
                        continue  # 继续下一次网络重试
                    else:
                        self.log(f"[网络] 已重试 {self.NETWORK_MAX_RETRIES} 次仍然失败")
                        final_class = "YTDLP"
                        return False

                # 不猜测错误原因：记录 yt-dlp 真实错误，
                # 只有明确的认证/登录/Cookie 提示才标 AUTH / COOKIE
                final_class = self._classify_error(output_text)
                error_message = self._first_error_message(output_text)
                task_debug_section("ERROR", [
                    ("task_id", task_id),
                    ("attempt", attempts),
                    ("class", final_class),
                    ("message", error_message),
                ])
                self.log(f"[ERROR] class={final_class}")
                self.log(f"[ERROR] message={error_message}")

                # 其他错误（不可重试）直接返回
                return False

            return False
        finally:
            self.remove_session_cookie_copy(session_cookie_file, source_cookie_file)
            self._task_log_end(task_id, result, attempts, task_start, final_class)

    def _run_ytdlp(self, cmd):
        """执行 yt-dlp 命令（原版内联循环原样提取），返回 (是否成功, 完整输出文本)"""
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
                return False, "\n".join(stderr_output)

            line = line.rstrip()
            if line:
                stderr_output.append(line)
                # 标记为原始输出：精简模式下不进 GUI 日志（错误/警告仍会显示）
                self.log(LOG_RAW_MARK + line)

                # 新媒体流开始：文件名含 .f<格式号>. 说明是分离的视频/音频流，
                # 这种情况 yt-dlp 会依次下两条流，各自 0→100
                if line.startswith("[download] Destination:"):
                    if re.search(r"\.f\d+\.", line):
                        self._prog_expected = 2
                    self._prog_streams += 1
                    self._detail_pct = -1.0

                # 阶段提示与进度详情：精简模式下也显示，让下载过程可见
                stage = self._download_stage_text(line)
                if stage:
                    self.log(stage)
                else:
                    self._log_download_detail(line)

                match = self._PROGRESS_RE.search(line)
                if match:
                    try:
                        stream_frac = float(match.group(1)) / 100.0
                    except ValueError:
                        stream_frac = None
                    if stream_frac is not None:
                        finished_streams = max(self._prog_streams - 1, 0)
                        self._emit_url_progress(
                            (finished_streams + stream_frac)
                            / self._prog_expected
                        )

        process.wait()
        return process.returncode == 0, "\n".join(stderr_output)

    # ---- TikTok 两阶段重试（唯一的 TikTok 专用增强）----
    # 阶段 1 RESOLVE：网页解析/提取。失败 → Recovery：无条件重读扩展
    #   Cookie/UA/Referer 重建请求，指数退避 2/6/12 秒，最多 3 次，
    #   -N 始终保持 8（解析失败与并发无关）。
    # 阶段 2 DOWNLOAD：媒体分片下载。失败 → 等 5 秒降为 -N 4 重试一次，
    #   仍失败则任务失败，不刷新 Cookie、不继续降级。
    # 不管理代理、不连接浏览器、禁止无限重试。
    TIKTOK_RESOLVE_MAX_RECOVERIES = 3  # RESOLVE Recovery 上限（不含首次）
    TIKTOK_RESOLVE_BACKOFF = (2, 6, 12)  # RESOLVE Recovery 前的指数退避（秒）
    TIKTOK_DOWNLOAD_RETRY_DELAY = 5  # DOWNLOAD 并发降级前等待（秒）
    TIKTOK_RESOLVE_WORKERS = 8  # RESOLVE 阶段并发（解析失败与并发无关，保持 8）
    TIKTOK_DOWNLOAD_FALLBACK_WORKERS = 4  # DOWNLOAD 失败后的降级并发（仅降一次）

    @staticmethod
    def _node_path():
        """查找 node.exe（便携版优先，其次系统 PATH）"""
        import shutil
        portable = RUN_DIR / "nodejs" / "node.exe"
        if portable.exists():
            return str(portable)
        return shutil.which("node") or ""

    def _js_runtime_spec(self):
        """实际传给 yt-dlp 的 js-runtimes 参数（静默检测，逻辑与 js_runtime_info 一致）"""
        runtime, path = self.js_runtime_info()
        if runtime:
            return f"--no-js-runtimes --js-runtimes {runtime}:{path}"
        return "NA"

    @staticmethod
    def _detect_challenge_solver(output_text):
        """从 yt-dlp 实际输出判断 challenge solver（记录真实运行状态，不猜测）"""
        if "native Python implementation" in output_text:
            return "native_python"
        m = re.search(r"\[jsc:(\w+)\]", output_text)
        if m:
            return m.group(1)
        if "Solving JS challenge" in output_text:
            return "unknown"
        return "NA"

    # ---- TikTok Native Extractor（参考 OmniGet tonhowtf/omniget）----
    # 原生 HTTP 请求 + JSON 解析直接获取视频 URL，绕过 yt-dlp JS Challenge；
    # 失败时无条件回退现有 yt-dlp 流程（含全部重试机制）。

    @staticmethod
    def _tiktok_extract_post_id(url):
        """从 TikTok URL 提取视频 ID（移植自 OmniGet extract_post_id）"""
        try:
            parsed = urlparse(url)
            segments = [s for s in parsed.path.split('/') if s]
            if (len(segments) >= 3
                    and segments[0].startswith('@')
                    and segments[1] in ('video', 'photo')
                    and segments[2].isdigit()):
                return segments[2]
        except Exception:
            pass
        return None

    def _tiktok_resolve_short_link(self, url):
        """解析 TikTok 短链到完整 URL（移植自 OmniGet resolve_short_link）"""
        try:
            import http.client
            parsed = urlparse(url)
            conn_cls = http.client.HTTPSConnection if parsed.scheme == 'https' else http.client.HTTPConnection
            conn = conn_cls(parsed.hostname, timeout=15)
            path = parsed.path
            if parsed.query:
                path += '?' + parsed.query
            conn.request("GET", path, headers={
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko)",
            })
            resp = conn.getresponse()
            if resp.status in (301, 302, 303, 307, 308):
                location = resp.getheader("Location", "")
                if location:
                    clean = location.split('?')[0]
                    conn.close()
                    return clean
            body = resp.read(4096).decode('utf-8', errors='ignore')
            conn.close()
            if body.startswith('<a href="https://'):
                url_part = body.split('<a href="')[1].split('"')[0]
                return url_part.split('?')[0]
        except Exception:
            pass
        return url

    @staticmethod
    def _tiktok_is_captcha_page(html):
        """检测 TikTok 验证码页面（移植自 OmniGet is_captcha_page）"""
        return ("verify-bar-close" in html
                or "captcha_verify" in html
                or "tiktok-verify-page" in html
                or "verify/page" in html
                or ("Verify to continue" in html
                    and "__UNIVERSAL_DATA_FOR_REHYDRATION__" not in html))

    @staticmethod
    def _tiktok_is_valid_play_addr(url):
        """验证视频 URL 有效性（移植自 OmniGet is_valid_play_addr）"""
        if not url or not isinstance(url, str):
            return False
        if not url.startswith("http://") and not url.startswith("https://"):
            return False
        if "verify" in url or "captcha" in url:
            return False
        return True

    @staticmethod
    def _tiktok_parse_cookies(cookie_file_path):
        """从 Netscape Cookie 文件解析 TikTok Cookie 并构建 Cookie header 字符串"""
        try:
            import http.cookiejar as cj
            jar = cj.MozillaCookieJar()
            jar.load(str(cookie_file_path), ignore_discard=True, ignore_expires=True)
            parts = []
            for c in jar:
                if c.domain and 'tiktok' in c.domain:
                    parts.append(f"{c.name}={c.value}")
            if parts:
                return "; ".join(parts), len(parts)
        except Exception:
            pass
        return None, 0

    def _tiktok_native_extract(self, url):
        """原生 HTTP 获取 TikTok 视频详情（curl_cffi Session，不带 Cookie 避免 WAF）

        返回 dict: {author, filename_base, post_id, video_url, duration, session} 或 None
        session 用于后续下载（自动携带页面响应 Cookie）
        """
        task_debug("[NATIVE] START")
        task_debug(f"[NATIVE] python={sys.executable}")

        post_id = self._tiktok_extract_post_id(url)
        if not post_id:
            canonical = self._tiktok_resolve_short_link(url)
            post_id = self._tiktok_extract_post_id(canonical)
            if not post_id:
                task_debug("[NATIVE] FAILED reason=Cannot extract post_id from URL")
                return None

        fetch_url = f"https://www.tiktok.com/@i/video/{post_id}"
        task_debug(f"[NATIVE] fetch_url={fetch_url} post_id={post_id}")

        try:
            from curl_cffi import requests as _cffi
            task_debug("[NATIVE] curl_cffi=OK")
        except ImportError as e:
            task_debug(f"[NATIVE] FAILED reason=curl_cffi not installed")
            task_debug(f"[NATIVE] EXCEPTION type=ImportError message={e}")
            task_debug(f"[NATIVE] python={sys.executable}")
            task_debug(f"[NATIVE] sys.path={sys.path[:3]}")
            return None

        try:
            headers = {
                "Referer": "https://www.tiktok.com/",
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8",
                "Accept-Language": "en-US,en;q=0.9,zh-CN;q=0.8,zh;q=0.7",
                "Accept-Encoding": "gzip, deflate, br",
                "Sec-Fetch-Dest": "document",
                "Sec-Fetch-Mode": "navigate",
                "Sec-Fetch-Site": "none",
                "Sec-Fetch-User": "?1",
                "Upgrade-Insecure-Requests": "1",
            }

            # 关键：使用 Session 不带 Cookie 请求，避免触发 WAF；
            # Session 自动捕获页面响应 Cookie（ttwid 等），供后续下载使用
            session = _cffi.Session(impersonate="chrome131")
            resp = session.get(
                fetch_url,
                headers=headers,
                timeout=30,
            )

            task_debug(f"[NATIVE] HTTP={resp.status_code} html_len={len(resp.text)}")

            if resp.status_code != 200:
                task_debug(f"[NATIVE] FAILED reason=HTTP_{resp.status_code}")
                return None

            html = resp.text

            if not html:
                task_debug("[NATIVE] FAILED reason=empty response")
                return None

            if self._tiktok_is_captcha_page(html):
                task_debug("[NATIVE] FAILED reason=captcha page detected")
                return None

            # 检测 WAF 挑战页面
            if "SlardarWAF" in html or "wafchallenge" in html:
                task_debug("[NATIVE] FAILED reason=WAF challenge page")
                return None

            marker = '__UNIVERSAL_DATA_FOR_REHYDRATION__'
            if marker not in html:
                task_debug(f"[NATIVE] universal_data=NOT_FOUND")
                task_debug(f"[NATIVE] FAILED reason={marker} not found in HTML")
                return None
            task_debug(f"[NATIVE] universal_data=FOUND")

            try:
                json_str = html.split(
                    f'<script id="{marker}" type="application/json">'
                )[1].split('</script>')[0]
                data = json.loads(json_str)
            except (IndexError, json.JSONDecodeError) as e:
                task_debug(f"[NATIVE] FAILED reason=JSON parse error: {e}")
                return None

            video_detail = data.get("__DEFAULT_SCOPE__", {}).get("webapp.video-detail")
            if not video_detail:
                task_debug("[NATIVE] FAILED reason=webapp.video-detail not found")
                return None

            status_msg = video_detail.get("statusMsg", "")
            if status_msg:
                task_debug(f"[NATIVE] FAILED reason=statusMsg={status_msg}")
                return None

            status_code = video_detail.get("statusCode")
            if status_code is not None and status_code != 0:
                task_debug(f"[NATIVE] FAILED reason=statusCode={status_code}")
                return None

            detail = video_detail.get("itemInfo", {}).get("itemStruct")
            if not detail:
                task_debug("[NATIVE] itemStruct=NOT_FOUND")
                task_debug("[NATIVE] FAILED reason=itemStruct not found")
                return None
            task_debug("[NATIVE] itemStruct=FOUND")

            if detail.get("isContentClassified"):
                task_debug("[NATIVE] FAILED reason=age-restricted content")
                return None

            if not detail.get("author"):
                task_debug("[NATIVE] FAILED reason=no author info")
                return None

            author = (detail.get("author", {}).get("uniqueId")
                      or detail.get("author", {}).get("unique_id")
                      or "unknown")
            filename_base = f"tiktok_{safe_filename(author)}_{post_id}"

            video_url = None
            vdata = detail.get("video", {})

            pa = vdata.get("playAddr")
            if isinstance(pa, str) and self._tiktok_is_valid_play_addr(pa):
                video_url = pa

            if not video_url:
                ul = vdata.get("play_addr", {}).get("urlList", [])
                if isinstance(ul, list):
                    for u in ul:
                        if isinstance(u, str) and self._tiktok_is_valid_play_addr(u):
                            video_url = u
                            break

            if not video_url:
                da = vdata.get("downloadAddr")
                if isinstance(da, str) and self._tiktok_is_valid_play_addr(da):
                    video_url = da

            if not video_url:
                bi = vdata.get("bitrateInfo", [])
                if isinstance(bi, list):
                    for b in bi:
                        ul2 = b.get("PlayAddr", {}).get("UrlList", [])
                        if isinstance(ul2, list):
                            for u in ul2:
                                if isinstance(u, str) and self._tiktok_is_valid_play_addr(u):
                                    video_url = u
                                    break
                        if video_url:
                            break

            if not video_url:
                task_debug("[NATIVE] video_url=NOT_FOUND")
                task_debug("[NATIVE] FAILED reason=no valid video URL in any fallback")
                return None

            duration = vdata.get("duration")
            task_debug(f"[NATIVE] video_url=FOUND url_len={len(video_url)}")
            task_debug(f"[NATIVE] OK author={author} post_id={post_id} dur={duration}")

            return {
                "author": author,
                "filename_base": filename_base,
                "post_id": post_id,
                "video_url": video_url,
                "duration": duration,
                "session": session,
            }
        except Exception as e:
            task_debug(f"[NATIVE] FAILED reason=exception")
            task_debug(f"[NATIVE] EXCEPTION type={type(e).__name__} message={e}")
            return None

    def _tiktok_native_download(self, video_url, output_path, session=None):
        """原生 HTTP 直接下载 TikTok 视频（使用 session 携带页面响应 Cookie）"""
        task_debug(f"[NATIVE_DL] START url_len={len(video_url)} output={output_path} session={'yes' if session else 'no'}")
        try:
            from curl_cffi import requests as _cffi
            task_debug("[NATIVE_DL] curl_cffi=OK")
        except ImportError as e:
            task_debug(f"[NATIVE_DL] FAILED reason=curl_cffi not installed")
            task_debug(f"[NATIVE_DL] EXCEPTION type=ImportError message={e}")
            return False

        try:
            headers = {
                "Referer": "https://www.tiktok.com/",
                "Accept": "*/*",
                "Accept-Language": "en-US,en;q=0.9",
                "Accept-Encoding": "identity",
            }

            # 使用 extract 阶段的 session（自动携带 ttwid 等 Cookie）
            if session:
                resp = session.get(
                    video_url,
                    headers=headers,
                    timeout=120,
                    stream=True,
                )
            else:
                resp = _cffi.get(
                    video_url,
                    impersonate="chrome131",
                    headers=headers,
                    timeout=120,
                    stream=True,
                )

            task_debug(f"[NATIVE_DL] HTTP={resp.status_code}")

            if resp.status_code != 200:
                task_debug(f"[NATIVE_DL] FAILED reason=HTTP_{resp.status_code}")
                return False

            ct = resp.headers.get('Content-Type', '')
            task_debug(f"[NATIVE_DL] content_type={ct}")

            if 'text/html' in ct or 'application/json' in ct:
                task_debug(f"[NATIVE_DL] FAILED reason=unexpected content-type={ct}")
                return False

            total = int(resp.headers.get('Content-Length', 0))
            task_debug(f"[NATIVE_DL] size={total}")

            output_path = Path(output_path)
            downloaded = 0
            with open(output_path, 'wb') as f:
                for chunk in resp.iter_content(chunk_size=262144):
                    if self.stop_flag or self.skip_flag:
                        task_debug(f"[NATIVE_DL] FAILED reason=user stopped/skipped")
                        return False
                    if chunk:
                        f.write(chunk)
                        downloaded += len(chunk)
                        if total > 0:
                            self._emit_url_progress(downloaded / total)

            if total > 0 and downloaded < total * 0.95:
                task_debug(f"[NATIVE_DL] FAILED reason=incomplete {downloaded}/{total}")
                return False

            task_debug(f"[NATIVE_DL] SUCCESS size={downloaded}")
            self._emit_url_progress(1.0)
            return True
        except Exception as e:
            task_debug(f"[NATIVE_DL] FAILED reason=exception")
            task_debug(f"[NATIVE_DL] EXCEPTION type={type(e).__name__} message={e}")
            return False

    def _download_tiktok(self, url, job_dir):
        """TikTok Native + yt-dlp fallback 两阶段下载：
        Native HTTP 优先获取页面解析视频 URL 直接下载；
        Native 失败→回退现有 yt-dlp 流程（含全部重试机制）。
        """
        task_id = secrets.token_hex(3).upper()
        task_start = time.time()
        self._task_log_start(task_id, url, "tiktok")
        task_debug_section("TIKTOK TASK START", [
            ("task_id", task_id),
            ("url", task_redact_url(url)),
            ("time", time.strftime("%Y-%m-%d %H:%M:%S")),
        ], bordered=True)

        # ---- TikTok Native Extractor（第一优先级）----
        # 原生 HTTP 直接获取页面 → 解析 JSON → 提取视频 URL → 直接下载
        # 成功则跳过 yt-dlp，失败则无条件落入下方现有 yt-dlp 流程
        native_used = False
        task_debug(f"[NATIVE] task_id={task_id} attempting native extract")
        task_debug(f"[NATIVE] python={sys.executable}")
        native_info = self._tiktok_native_extract(url)
        if native_info:
            video_url = native_info["video_url"]
            filename = f"{native_info['filename_base']}.mp4"
            output_path = job_dir / filename
            task_debug(f"[NATIVE] task_id={task_id} extract OK, attempting native download")
            self.log(f"[TikTok Native] 页面解析成功，直接下载中...")
            native_session = native_info.get("session")
            if self._tiktok_native_download(video_url, output_path, session=native_session):
                native_used = True
                task_debug(f"[NATIVE] task_id={task_id} native download SUCCESS")
                self.log(f"[TikTok Native] 下载完成！")
                result = "SUCCESS"
                final_class = None
                final_category = None
                final_reason = None
                last_error = None
                return True
            else:
                task_debug(f"[NATIVE] task_id={task_id} native download FAILED, falling back to yt-dlp")
                task_debug("[FALLBACK] YTDLP")
                self.log(f"[TikTok Native] 直接下载失败，回退 yt-dlp...")
                try:
                    if output_path.exists():
                        output_path.unlink()
                except Exception:
                    pass
        else:
            task_debug(f"[NATIVE] task_id={task_id} native extract FAILED, falling back to yt-dlp")
            task_debug("[FALLBACK] YTDLP")
            self.log(f"[TikTok Native] 页面解析失败，使用 yt-dlp...")

        # ---- 现有 yt-dlp 流程（fallback）----
        result = "FAILED"
        final_class = None
        final_category = None
        final_reason = None
        attempts = 0
        last_error = None
        last_stage = "RESOLVE"
        # 首次失败记录（即使最终恢复成功也保留，便于判断“第一次失败是什么、最终有没有恢复”）
        initial_class = None
        initial_reason = None
        initial_message = None
        workers = self.TIKTOK_RESOLVE_WORKERS
        refreshed_cookie = False
        # Cookie 文件状态用容器持有（避免嵌套函数赋值导致外层引用不一致，
        # 确保 finally 能拿到最新副本路径完成清理）
        cookie_state = {"source": None, "session": None}
        ua = cookie_args = referer = None

        def _read_state():
            """重新读取扩展同步的 Cookie / UA / Referer，重建 Cookie 临时副本"""
            nonlocal ua, referer, cookie_args, refreshed_cookie
            ua = self.get_ua()
            referer = self.get_referer(url)
            cookie_state["source"] = self.resolve_cookie_file(url)
            if cookie_state["source"]:
                self.remove_session_cookie_copy(cookie_state["session"], cookie_state["source"])
                cookie_state["session"] = self.make_session_cookie_copy(cookie_state["source"], task_id)
                cookie_args = (["--cookies", str(cookie_state["session"] or cookie_state["source"])])
            else:
                cookie_args = []
            refreshed_cookie = True

        try:
            attempt = 0
            resolve_recoveries = 0
            download_downgraded = False
            while True:
                if self.stop_flag or self.skip_flag:
                    result = "ABORTED"
                    return False

                attempt += 1
                attempts = attempt

                if attempt == 1:
                    action = "INITIAL_READ"
                    _read_state()
                elif last_stage == "RESOLVE":
                    # RESOLVE Recovery：无条件重读 Cookie/UA/Referer 重建请求（日志已证明
                    # 扩展刷新后 Cookie 条数/会话状态确实变化），-N 始终保持 8。
                    action = "REFRESH_BROWSER_STATE"
                    self._cleanup_job_dir(job_dir)
                    _read_state()
                else:
                    # DOWNLOAD Recovery：只降并发重建命令，不刷新 Cookie（媒体下载与会话无关）；
                    # 同时清理 job_dir 残留的 .part（损坏的部分文件会导致续传反复失败）
                    action = "CONCURRENCY_DOWNGRADE"
                    self._cleanup_job_dir(job_dir)
                    ua = self.get_ua()
                    referer = self.get_referer(url)
                    refreshed_cookie = False

                retry_reason = last_error if attempt > 1 else None
                self._task_log_request(
                    task_id, cookie_args, ua, referer, attempt, workers,
                    action=action, retry_reason=retry_reason,
                    js_runtime_args=self._js_runtime_spec(),
                )
                self._task_log_cookie_ua_referer(task_id, cookie_args, ua, referer,
                                                 cookie_state["source"], refreshed_cookie)

                cmd = self.build_ytdlp_args(
                    url, job_dir, ua, cookie_args,
                    referer=referer, concurrency=workers
                )

                # 下载前记录实际请求环境（方便判断 Node 是否真的参与）
                self._task_log_js_runtime(task_id, attempt)
                task_debug_section("YTDLP", [
                    ("task_id", task_id),
                    ("attempt", attempt),
                    ("stage", "RESOLVE+DOWNLOAD"),
                    ("concurrency", workers),
                    ("retries", 5),
                    ("extractor_retries", 3),
                    ("fragment_retries", 5),
                ])
                task_debug(f"[COMMAND] task_id={task_id} {task_redact_command(cmd)}")

                if attempt == 1:
                    self.log(f"开始下载：{url}")
                elif last_stage == "RESOLVE":
                    self.log(f"TikTok RESOLVE 恢复（第 {resolve_recoveries} 次）：{url}")
                else:
                    self.log(f"TikTok 重试（-N {workers}）：{url}")

                task_debug(f"[RESOLVE] task_id={task_id} attempt={attempt} status=START")
                ok, output_text = self._run_ytdlp(cmd)

                # 从 yt-dlp 实际输出记录 challenge solver 与失败阶段真实状态
                solver = self._detect_challenge_solver(output_text)
                task_debug(f"[CHALLENGE] task_id={task_id} attempt={attempt} challenge_solver={solver}")

                if ok:
                    task_debug(f"[RESOLVE] task_id={task_id} attempt={attempt} status=SUCCESS")
                    task_debug(f"[DOWNLOAD] task_id={task_id} attempt={attempt} status=SUCCESS")
                    task_debug(f"[YTDLP RESULT] task_id={task_id} attempt={attempt} "
                               f"resolve=SUCCESS download=SUCCESS error_class=NA")
                    result = "SUCCESS"
                    # 成功时最终错误状态必须为 NONE（即使中途发生过失败）
                    final_class = None
                    final_category = None
                    final_reason = None
                    last_error = None
                    return True

                if self.stop_flag or self.skip_flag:
                    result = "ABORTED"
                    return False

                # 记录真实错误与失败阶段（不猜测）
                final_class = self._classify_error(output_text)
                error_message = self._first_error_message(output_text)
                last_stage = self._detect_failed_stage(output_text)
                final_category = self._task_error_category(last_stage, final_class)
                final_reason = self._resolve_reason(output_text) if last_stage == "RESOLVE" else final_class
                if last_stage == "RESOLVE":
                    task_debug(f"[RESOLVE] task_id={task_id} attempt={attempt} status=FAILED")
                else:
                    task_debug(f"[RESOLVE] task_id={task_id} attempt={attempt} status=SUCCESS")
                    task_debug(f"[DOWNLOAD] task_id={task_id} attempt={attempt} status=FAILED")
                task_debug(f"[YTDLP RESULT] task_id={task_id} attempt={attempt} "
                           f"resolve={'FAILED' if last_stage == 'RESOLVE' else 'SUCCESS'} "
                           f"download={'FAILED' if last_stage == 'DOWNLOAD' else 'NA'} "
                           f"error_class={final_category} reason={final_reason}")
                task_debug_section("ERROR", [
                    ("task_id", task_id),
                    ("attempt", attempt),
                    ("class", final_class),
                    ("category", final_category),
                    ("stage", last_stage),
                    ("reason", final_reason),
                    ("message", error_message),
                    ("action", action),
                    ("challenge_solver", solver),
                ])
                task_debug(
                    "[YTDLP STDERR]",
                    *output_text.splitlines()[-40:],
                    ""
                )
                self.log(f"[ERROR] class={final_category} stage={last_stage} reason={final_reason}")
                self.log(f"[ERROR] message={error_message}")
                last_error = error_message
                if initial_class is None:
                    initial_class = final_category
                    initial_reason = final_reason
                    initial_message = error_message

                # 决定下次尝试的策略（RESOLVE 失败≠DOWNLOAD 失败，严格区分）：
                # RESOLVE 失败 → Recovery：刷新 Cookie/UA/Referer + 重建，指数退避，-N 保持 8，最多 3 次；
                # DOWNLOAD 失败 → 等 5 秒降 -N 8→4 重试一次，仍失败即终止。
                if last_stage == "RESOLVE":
                    if resolve_recoveries >= self.TIKTOK_RESOLVE_MAX_RECOVERIES:
                        break
                    resolve_recoveries += 1
                    delay = self.TIKTOK_RESOLVE_BACKOFF[min(resolve_recoveries - 1, len(self.TIKTOK_RESOLVE_BACKOFF) - 1)]
                    workers = self.TIKTOK_RESOLVE_WORKERS
                    task_debug_section("TIKTOK RECOVERY", [
                        ("task_id", task_id),
                        ("stage", "RESOLVE"),
                        ("attempt", attempt + 1),
                        ("reason", final_reason),
                        ("action", "REFRESH_COOKIE"),
                        ("action", "REFRESH_UA"),
                        ("action", "REFRESH_REFERER"),
                        ("action", "REBUILD_YTDLP_ARGS"),
                        ("concurrency", workers),
                        ("wait", f"{delay}s"),
                    ])
                    self.log(f"[TikTok] RESOLVE 失败（{final_reason}），等待 {delay} 秒后刷新 Cookie/UA/Referer 重建请求重试（-N {workers}）...")
                    time.sleep(delay)
                else:
                    if download_downgraded:
                        break
                    download_downgraded = True
                    workers = self.TIKTOK_DOWNLOAD_FALLBACK_WORKERS
                    task_debug_section("TIKTOK RECOVERY", [
                        ("task_id", task_id),
                        ("stage", "DOWNLOAD"),
                        ("attempt", attempt + 1),
                        ("reason", final_class),
                        ("action", "CONCURRENCY_DOWNGRADE"),
                        ("action", "REBUILD_YTDLP_ARGS"),
                        ("concurrency", workers),
                        ("wait", f"{self.TIKTOK_DOWNLOAD_RETRY_DELAY}s"),
                    ])
                    self.log(f"[TikTok] DOWNLOAD 失败，等待 {self.TIKTOK_DOWNLOAD_RETRY_DELAY} 秒后以 -N {workers} 重试...")
                    time.sleep(self.TIKTOK_DOWNLOAD_RETRY_DELAY)

            self.log("")
            if last_stage == "RESOLVE":
                self.log(f"[TikTok] ⚠ RESOLVE 已完成 {self.TIKTOK_RESOLVE_MAX_RECOVERIES} 次 Recovery 仍失败，结束任务")
                self.log("[TikTok] 建议：在浏览器中打开 TikTok 页面，等扩展同步最新 Cookie 后，右键失败链接 → 重试")
            else:
                self.log(f"[TikTok] ⚠ DOWNLOAD 已降为 -N {self.TIKTOK_DOWNLOAD_FALLBACK_WORKERS} 仍失败，结束任务")
            return False
        finally:
            self.remove_session_cookie_copy(cookie_state["session"], cookie_state["source"])
            task_debug_section("TIKTOK TASK END", [
                ("task_id", task_id),
                ("result", result),
                ("attempts", attempts),
                ("elapsed", f"{time.time() - task_start:.1f}s"),
                ("initial_error_class", initial_class or "NONE"),
                ("initial_error", initial_reason or "NONE"),
                ("initial_message", (initial_message or "NONE") if result != "SUCCESS" else "NONE"),
                ("final_error_class", final_category or "NONE"),
                ("final_error", final_reason or "NONE"),
                ("final_failed_stage", last_stage if result not in ("SUCCESS", "ABORTED") else "NA"),
            ], bordered=True)
            self._task_log_end(task_id, result, attempts, task_start,
                               final_category, final_reason,
                               initial_class, initial_reason)

    # ---- 错误分类（细化版，参考 OmniGet）----
    # 按顺序匹配：EXTRACTOR / HTTP_403 / HTTP_429 / TIMEOUT / COOKIE / SESSION /
    # AUTH / FORMAT / FFMPEG / NETWORK / DOWNLOAD，其余记 YTDLP，不猜测。
    AUTH_ERROR_INDICATORS = [
        "login required",
        "sign in to confirm",
        "please sign in",
        "authentication required",
        "only available for registered",
        "premium members",
    ]

    COOKIE_ERROR_INDICATORS = [
        "cookies file",
        "cookie file",
        "unable to read cookie",
        "invalid cookie",
    ]

    EXTRACTOR_ERROR_INDICATORS = [
        "unable to extract",
        "unexpected response from webpage",
        "unable to download webpage",
        "unable to extract universal data",
        "rehydration",
        "failed to extract",
        "extraction aborted",
        "unable to extract challenge",
        "unable to solve js challenge",
        "unsupported url",
    ]

    SESSION_ERROR_INDICATORS = [
        # 用短语匹配，避免 session/verify 等子串误分类（日志准确性，不影响恢复策略）
        "session error",
        "session expired",
        "invalid session",
        "session cookies are not supported",
        "csrf",
        "captcha",
    ]

    FORMAT_ERROR_INDICATORS = [
        "requested format is not available",
        "no video formats found",
        "format unavailable",
        "unable to extract formats",
    ]

    DOWNLOAD_ERROR_INDICATORS = [
        "unable to download video data",
        "unable to download fragments",
        "incomplete read",
        "fragment is missing",
        "connection reset",
        "content too short",
    ]

    def _classify_error(self, output_text):
        """错误分类（细化版）：返回具体错误类别，不猜测"""
        text = output_text.lower()
        if any(ind in text for ind in self.EXTRACTOR_ERROR_INDICATORS):
            return "EXTRACTOR"
        if "http error 403" in text or "forbidden" in text:
            return "HTTP_403"
        if "http error 429" in text or "too many requests" in text:
            return "HTTP_429"
        if "timeout" in text or "timed out" in text:
            return "TIMEOUT"
        if any(ind in text for ind in self.COOKIE_ERROR_INDICATORS):
            return "COOKIE"
        if any(ind in text for ind in self.SESSION_ERROR_INDICATORS):
            return "SESSION"
        if any(ind in text for ind in self.AUTH_ERROR_INDICATORS):
            return "AUTH"
        if any(ind in text for ind in self.FORMAT_ERROR_INDICATORS):
            return "FORMAT"
        if "ffmpeg" in text and "error" in text:
            return "FFMPEG"
        if self._is_network_error(text):
            return "NETWORK"
        if any(ind in text for ind in self.DOWNLOAD_ERROR_INDICATORS):
            return "DOWNLOAD"
        return "YTDLP"

    # 任务级错误分类：在细分类基础上按失败阶段映射为
    # TIKTOK_RESOLVE / TIKTOK_DOWNLOAD / COOKIE / NETWORK / YTDLP / FFMPEG / UNKNOWN
    def _task_error_category(self, stage, fine_class):
        """细分类 → 任务级分类（日志用）；不猜测，无法判断记 UNKNOWN"""
        if fine_class == "COOKIE":
            return "COOKIE"
        if fine_class in ("NETWORK", "TIMEOUT"):
            return "NETWORK"
        if fine_class == "FFMPEG":
            return "FFMPEG"
        if stage == "RESOLVE":
            return "TIKTOK_RESOLVE"
        if stage == "DOWNLOAD":
            return "TIKTOK_DOWNLOAD"
        if fine_class:
            return "YTDLP"
        return "UNKNOWN"

    def _resolve_reason(self, output_text):
        """RESOLVE 失败的具体原因码（保留原始错误语义，供 [TIKTOK RECOVERY] 使用）"""
        text = (output_text or "").lower()
        if "universal data" in text or "rehydration" in text:
            return "UNIVERSAL_DATA"
        if "unexpected response from webpage" in text:
            return "WEBPAGE_RESPONSE"
        if "http error 403" in text or "forbidden" in text:
            return "HTTP_403"
        if "unable to download webpage" in text:
            return "WEBPAGE_DOWNLOAD"
        if "js challenge" in text:
            return "CHALLENGE"
        return self._classify_error(output_text)

    # RESOLVE 阶段标记：出现以下行说明已进入媒体下载阶段（解析已成功）
    DOWNLOAD_STAGE_MARKERS = (
        "[download] destination:",
        "has already been downloaded",
        "merging formats",
    )

    @classmethod
    def _detect_failed_stage(cls, output_text):
        """判定失败发生在 RESOLVE（网页解析/提取）还是 DOWNLOAD（媒体下载）阶段

        依据 yt-dlp 实际输出：出现 [download] Destination 等标记即已进入下载阶段。
        """
        text = output_text.lower()
        if any(m in text for m in cls.DOWNLOAD_STAGE_MARKERS):
            return "DOWNLOAD"
        return "RESOLVE"

    @staticmethod
    def _first_error_message(output_text):
        """提取最后一条 ERROR 行作为错误摘要（截断 300 字符）"""
        lines = [l.strip() for l in output_text.splitlines() if l.strip()]
        for line in reversed(lines):
            if line.upper().startswith("ERROR"):
                return line[:300]
        return lines[-1][:300] if lines else "NA"

    # ---- 任务日志辅助（只写 logs/tiktok_debug.log，不影响 GUI 日志） ----

    def _task_log_start(self, task_id, url, platform):
        """[TASK START] + [ENVIRONMENT]（全平台通用）"""
        import shutil
        task_debug_section("TASK START", [
            ("task_id", task_id),
            ("platform", platform),
            ("url", task_redact_url(url)),
            ("start_time", time.strftime("%Y-%m-%d %H:%M:%S")),
        ], bordered=True)
        node_path = shutil.which("node") or ""
        if not node_path and (RUN_DIR / "nodejs" / "node.exe").exists():
            node_path = str(RUN_DIR / "nodejs" / "node.exe")
        task_debug_section("ENVIRONMENT", [
            ("python_version", sys.version.split()[0]),
            ("os", "Windows" if sys.platform == "win32" else sys.platform),
            ("yt_dlp_path", str(YTDLP_EXE)),
            ("yt_dlp_version", get_ytdlp_version()),
            ("node_path", node_path or "NA"),
            ("ffmpeg_path", str(FFMPEG_EXE)),
        ])

    def _task_log_request(self, task_id, cookie_args, ua, referer, attempt, concurrency,
                          action=None, retry_reason=None, js_runtime_args=None):
        """[REQUEST] + [ATTEMPT]：Cookie 只记存在性/条数/mtime/大小/关键项存在性，不记真实值"""
        cookie_path = cookie_args[1] if cookie_args else None
        if cookie_path:
            stats = task_cookie_stats(cookie_path)
        else:
            stats = {"exists": False, "size": 0, "count": 0, "mtime": "NA"}
            for name in TASK_KEY_COOKIES:
                stats[name] = "absent"
        pairs = [
            ("task_id", task_id),
            ("cookie", "true" if cookie_args else "false"),
            ("ua", "true" if ua else "false"),
            ("referer", "true" if referer else "false"),
            ("concurrency", concurrency if concurrency is not None else "NA"),
            ("action", action or "NA"),
            ("js_runtime_args", js_runtime_args or "NA"),
            ("cookie_exists", str(stats["exists"]).lower()),
            ("cookie_count", stats["count"]),
            ("cookie_size", stats["size"]),
            ("cookie_mtime", stats["mtime"]),
            ("cookie_header", "REDACTED" if cookie_args else "NA"),
        ]
        if retry_reason:
            pairs.append(("retry_reason", retry_reason))
        for name in TASK_KEY_COOKIES:
            pairs.append((name, stats.get(name, "absent")))
        task_debug_section("REQUEST", pairs)
        task_debug_section("ATTEMPT", [
            ("task_id", task_id),
            ("attempt", attempt),
            ("concurrency", concurrency if concurrency is not None else "NA"),
        ], bordered=True)

    def _task_log_js_runtime(self, task_id, attempt):
        """[JS RUNTIME]：下载前记录 Node 检测状态与实际传给 yt-dlp 的参数"""
        runtime, path = self.js_runtime_info()
        task_debug_section("JS RUNTIME", [
            ("task_id", task_id),
            ("attempt", attempt),
            ("enabled", "true" if runtime else "false"),
            ("runtime", runtime or "NA"),
            ("path", path or "NA"),
            ("js_runtime_args", self._js_runtime_spec()),
        ])

    def _task_log_cookie_ua_referer(self, task_id, cookie_args, ua, referer,
                                    source_cookie_file=None, refreshed=False):
        """[COOKIE] / [UA] / [REFERER] / [PROXY]：只记元信息，绝不记 Cookie 内容"""
        cookie_path = cookie_args[1] if cookie_args else None
        if source_cookie_file is None and cookie_path:
            source_cookie_file = cookie_path
        stats = task_cookie_stats(source_cookie_file) if source_cookie_file else None
        cookie_pairs = [
            ("task_id", task_id),
            ("source", "extension" if source_cookie_file else "none"),
            ("file", Path(source_cookie_file).name if source_cookie_file else "NA"),
            ("session_copy", Path(cookie_path).name if cookie_path else "NA"),
            ("exists", str(bool(stats and stats["exists"])).lower()),
            ("size", stats["size"] if stats else 0),
            ("mtime", stats["mtime"] if stats else "NA"),
            ("refreshed", str(refreshed).lower()),
        ]
        task_debug_section("COOKIE", cookie_pairs)
        task_debug_section("UA", [
            ("task_id", task_id),
            ("source", "extension" if UA_FILE.exists() else "default"),
            ("length", len(ua) if ua else 0),
        ])
        task_debug_section("REFERER", [
            ("task_id", task_id),
            ("source", "extension" if referer else "NA"),
            ("value", task_redact_url(referer) if referer else "NA"),
        ])
        task_debug_section("PROXY", [
            ("task_id", task_id),
            ("enabled", "false"),
            ("source", "system"),
            ("value", get_system_proxy_state()),
        ])

    def _task_log_end(self, task_id, result, attempts, task_start, final_class,
                      final_reason=None, initial_class=None, initial_reason=None):
        """[TASK END]：成功时最终错误状态必须为 NONE，同时保留首次失败记录"""
        success = result == "SUCCESS"
        task_debug_section("TASK END", [
            ("task_id", task_id),
            ("result", result),
            ("attempts", attempts),
            ("elapsed", f"{time.time() - task_start:.1f}s"),
            ("initial_error_class", initial_class or "NONE"),
            ("initial_error", initial_reason or "NONE"),
            ("final_error_class", "NONE" if success else (final_class or "UNKNOWN")),
            ("final_error", "NONE" if success else (final_reason or (final_class or "UNKNOWN"))),
        ], bordered=True)

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

        total_seconds = 0.0
        last_log_ts = 0.0

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

                # 总时长只出现在 ffmpeg 打印的输入信息里，取一次即可
                if total_seconds <= 0:
                    duration_match = self._FFMPEG_DURATION_RE.search(line)
                    if duration_match:
                        total_seconds = self._hms_to_seconds(
                            duration_match.group(1)
                        )

                time_match = self._FFMPEG_TIME_RE.search(line)

                if time_match:
                    # 转码进度：每秒最多一条日志，同时把进度条从 95% 推到 100%
                    done_seconds = self._hms_to_seconds(time_match.group(1))
                    now = time.time()

                    if total_seconds > 0:
                        fraction = min(done_seconds / total_seconds, 1.0)
                        self._emit_post_progress(fraction)

                        if now - last_log_ts >= 1.0 or fraction >= 1.0:
                            last_log_ts = now
                            self.log(
                                f"[转码] {fraction * 100:.1f}%"
                                f"（{done_seconds:.0f}s / {total_seconds:.0f}s）"
                            )
                    elif now - last_log_ts >= 1.0:
                        last_log_ts = now
                        self.log(f"[转码] 已处理 {done_seconds:.0f}s")

                elif (
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

        self._reset_url_progress(index, total)

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

            self.log(
                f"[下载] "
                f"{index} / {total}  "
                f"{platform}  "
                f"{url}"
            )

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

            # 统一命名：{时间戳}_{平台}_{视频ID}.mp4（如 202608230032_tiktok_7647864743050448150.mp4）
            ts = time.strftime("%Y%m%d%H%M")

            final_file = (
                platform_dir /
                f"{ts}_{platform}_{video_id}.mp4"
            )

            number = 1

            while final_file.exists():

                final_file = (
                    platform_dir /
                    f"{ts}_{platform}_{video_id}_{number}.mp4"
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

                    # 手动停止或跳过：不计入成败
                    if self.stop_flag or self.skip_flag:
                        pass
                    elif ok:
                        self.success += 1
                    else:
                        self.failed += 1

                progress = int(
                    index /
                    total *
                    100
                )

                self._sync_url_progress(
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
            if fg and fg.color() and fg.color().name().lower() == "#f38ba8":
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
# 环境依赖面板
#
# 按【必需 / 推荐 / 数据与配置】分组列出每个组件的状态、说明与路径，
# 缺失且能自动装的给「自动安装」按钮，安装动作复用主窗口的组件安装流程
# （进度与结果同步显示在主窗口通知条与日志里）。
# ============================================================

class EnvironmentDialog(QDialog):

    # 自检结果（后台线程 → GUI 线程）
    components_ready = pyqtSignal(list)

    def __init__(self, parent):
        super().__init__(parent)

        self.main = parent

        self.setWindowTitle("环境依赖")
        self.setWindowIcon(QIcon(str(BASE_DIR / "icon.png")))
        self.setMinimumWidth(680)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(14, 12, 14, 12)
        layout.setSpacing(10)

        self.hint = QLabel("正在检测…")
        self.hint.setWordWrap(True)
        layout.addWidget(self.hint)

        self.groups_layout = QVBoxLayout()
        self.groups_layout.setSpacing(10)
        layout.addLayout(self.groups_layout)

        layout.addStretch(1)

        btn_row = QHBoxLayout()
        btn_row.addStretch(1)

        self.refresh_btn = QPushButton("重新检测")
        self.refresh_btn.setCursor(Qt.PointingHandCursor)
        self.refresh_btn.clicked.connect(self.refresh)
        btn_row.addWidget(self.refresh_btn)

        close_btn = QPushButton("关闭")
        close_btn.setCursor(Qt.PointingHandCursor)
        close_btn.clicked.connect(self.close)
        btn_row.addWidget(close_btn)

        layout.addLayout(btn_row)

        self.components_ready.connect(self._render)
        parent.component_installed_signal.connect(self._on_component_installed)

        self.refresh()

    def refresh(self):
        """后台线程重新自检：读版本号与探测 NVENC 要拉子进程，不能卡界面"""
        self.refresh_btn.setEnabled(False)
        self.hint.setText("正在检测…（读取版本与 NVENC 支持需要几秒）")

        def _worker():
            try:
                items = collect_components()
            except Exception as e:
                items = [{
                    "group": "required",
                    "name": "自检",
                    "ok": False,
                    "status": f"失败：{e}",
                    "detail": "",
                    "path": "",
                    "installer": None,
                }]
            self.components_ready.emit(items)

        threading.Thread(target=_worker, daemon=True).start()

    def _on_component_installed(self, _name, _ok, _info):
        self.refresh()

    def _clear_groups(self):
        while self.groups_layout.count():
            item = self.groups_layout.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.setParent(None)

    def _render(self, items):
        self._clear_groups()

        missing = [
            c["name"] for c in items
            if c["group"] == "required" and not c["ok"]
        ]
        self.hint.setText(
            "必需组件缺失：" + "、".join(missing) + "（可点右侧按钮自动安装）"
            if missing else "必需组件齐全，可以正常下载"
        )

        for group in COMPONENT_GROUPS:
            group_items = [c for c in items if c["group"] == group]
            if not group_items:
                continue

            box = QGroupBox(COMPONENT_GROUP_TITLES[group])
            grid = QGridLayout(box)
            grid.setContentsMargins(10, 8, 10, 8)
            grid.setHorizontalSpacing(10)
            grid.setVerticalSpacing(6)

            for row, component in enumerate(group_items):
                name_label = QLabel(component["name"])
                name_label.setMinimumWidth(84)
                grid.addWidget(name_label, row, 0)

                status_label = QLabel(component["status"])
                status_label.setMinimumWidth(160)
                status_label.setStyleSheet(
                    "color: #a6e3a1;" if component["ok"] else "color: #f38ba8;"
                )
                grid.addWidget(status_label, row, 1)

                detail_label = QLabel(component["detail"])
                detail_label.setObjectName("smallLabel")
                detail_label.setWordWrap(True)
                if component["path"]:
                    detail_label.setToolTip(component["path"])
                grid.addWidget(detail_label, row, 2)

                if component["installer"]:
                    install_btn = QPushButton("自动安装")
                    install_btn.setCursor(Qt.PointingHandCursor)
                    install_btn.clicked.connect(
                        lambda _checked=False, n=component["installer"]:
                            self._install(n)
                    )
                    grid.addWidget(install_btn, row, 3)

            grid.setColumnStretch(2, 1)
            self.groups_layout.addWidget(box)

        self.refresh_btn.setEnabled(True)

    def _install(self, name):
        label, size = MainWindow.COMPONENT_INSTALLERS.get(name, (name, ""))
        self.hint.setText(f"正在安装 {label}（{size}），进度见主窗口通知条与日志…")
        self.main.start_component_install(name)


# ============================================================
# 主窗口
# ============================================================

class MainWindow(QMainWindow):

    # 桥接服务器线程安全信号
    bridge_log_signal = pyqtSignal(str)
    bridge_url_signal = pyqtSignal(str)
    bridge_cookies_signal = pyqtSignal(list, str, str)  # cookies, ua, source_url
    bridge_ua_signal = pyqtSignal(str)
    bridge_activate_signal = pyqtSignal()  # 扩展请求「下载时打开应用」：把主窗口拉到前台
    # 环境检查结果（后台线程 → GUI 线程）：env_rows, ytdlp_version
    env_ready_signal = pyqtSignal(list, str)
    # yt-dlp 每日版本检查结果：local, latest, need_update, reason
    ytdlp_update_signal = pyqtSignal(str, str, bool, str)
    # yt-dlp 更新执行完毕：ok, version
    ytdlp_updated_signal = pyqtSignal(bool, str)
    # 组件自动安装完毕：组件名(ytdlp/ffmpeg/jsruntime), ok, 版本号或错误信息
    component_installed_signal = pyqtSignal(str, bool, str)
    # 后台线程往日志框输出（线程安全）
    worker_log_signal = pyqtSignal(str)

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

        # 自动下载暂停状态（从配置读取，控制队列调度，不影响当前任务）
        self.auto_download_paused = bool(
            self.config.get("auto_download_paused", False)
        )

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
            "视频批量下载工具"
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

        self.setMinimumSize(
            760,
            560
        )

        window_cfg = self.config.get(
            "window",
            {}
        )

        width = max(
            int(
                window_cfg.get(
                    "width",
                    900
                )
                or 900
            ),
            760
        )

        height = max(
            int(
                window_cfg.get(
                    "height",
                    650
                )
                or 650
            ),
            560
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
            and
            self._geometry_visible(
                x,
                y,
                width,
                height
            )
        ):

            self.move(
                x,
                y
            )

        # 上次退出时是最大化：先置状态，等 show() 时直接以最大化呈现
        if window_cfg.get("maximized"):

            self.setWindowState(
                self.windowState()
                | Qt.WindowMaximized
            )

    @staticmethod
    def _geometry_visible(x, y, width, height):
        """判断窗口区域是否仍落在某块屏幕的可用范围内。

        显示器数量或分辨率变化后，旧坐标可能完全在屏幕外，
        导致窗口打开后不可见，此时应回退到系统默认位置。
        """

        rect = QRect(
            x,
            y,
            width,
            height
        )

        for screen in QApplication.screens():

            visible = (
                screen
                .availableGeometry()
                .intersected(rect)
            )

            # 至少露出一块能用鼠标抓住标题栏的区域
            if (
                visible.width() >= 160
                and
                visible.height() >= 40
            ):

                return True

        return False

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

        # 标题栏：标题与状态胶囊放在同一个网格单元里 ——
        # 标题按窗口宽度真正居中，状态胶囊贴右，互不影响。
        top_layout = QGridLayout()

        top_layout.setContentsMargins(0, 0, 0, 0)

        title = QLabel(
            "视频批量下载工具"
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

        self.lbl_status = QLabel(
            "✦ 准备就绪"
        )

        self.lbl_status.setObjectName(
            "status"
        )

        self.lbl_status.setProperty(
            "state",
            "idle"
        )

        top_layout.addWidget(
            title,
            0, 0,
            Qt.AlignCenter
        )

        # 环境依赖入口：分组查看必需 / 推荐组件自检结果，并安装缺失项
        self.env_btn = QPushButton("环境")

        self.env_btn.setObjectName(
            "noticeAction"
        )

        self.env_btn.setCursor(
            Qt.PointingHandCursor
        )

        self.env_btn.setToolTip(
            "查看必需 / 推荐组件的自检结果，缺失项可一键自动安装"
        )

        self.env_btn.clicked.connect(
            self.open_environment_dialog
        )

        top_layout.addWidget(
            self.env_btn,
            0, 0,
            Qt.AlignLeft | Qt.AlignVCenter
        )

        top_layout.addWidget(
            self.lbl_status,
            0, 0,
            Qt.AlignRight | Qt.AlignVCenter
        )

        main_layout.addLayout(
            top_layout
        )

        # 通知条：默认隐藏，空闲时用于提示 yt-dlp 新版本 / 缺少 JS 运行时
        main_layout.addWidget(
            self._build_notice_bar()
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

        # 当前任务标签：居右，紧跟在「停止」之后
        self.lbl_current = QLabel(
            "等待任务"
        )

        self.lbl_current.setMinimumWidth(
            230
        )

        self.lbl_current.setAlignment(
            Qt.AlignRight | Qt.AlignVCenter
        )

        control_layout.addWidget(
            self.lbl_current
        )

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

        progress_layout.addWidget(
            self.progress
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

        # 暂停自动下载复选框（只控制队列调度，不中断当前任务）：统计行居右
        self.chk_auto_pause = QCheckBox(
            "暂停自动下载"
        )

        self.chk_auto_pause.setChecked(
            self.auto_download_paused
        )

        self.chk_auto_pause.toggled.connect(
            self.on_auto_pause_changed
        )

        stats_layout.addWidget(
            self.chk_auto_pause
        )

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

    # ========================================================
    # 顶部通知条：yt-dlp 新版本提示 / JS 运行时缺失提示
    # 只在空闲（没有下载任务在跑）时弹出，忙时挂起到任务结束
    # ========================================================

    def _build_notice_bar(self):

        self.notice_bar = QFrame()

        self.notice_bar.setObjectName(
            "noticeBar"
        )

        notice_layout = QHBoxLayout(
            self.notice_bar
        )

        notice_layout.setContentsMargins(12, 6, 8, 6)

        notice_layout.setSpacing(8)

        self.notice_label = QLabel("")

        self.notice_label.setObjectName(
            "noticeText"
        )

        self.notice_label.setWordWrap(True)

        notice_layout.addWidget(
            self.notice_label,
            1
        )

        self.notice_action_btn = QPushButton("")

        self.notice_action_btn.setObjectName(
            "noticeAction"
        )

        self.notice_action_btn.setCursor(
            Qt.PointingHandCursor
        )

        self.notice_action_btn.clicked.connect(
            self._on_notice_action
        )

        notice_layout.addWidget(
            self.notice_action_btn
        )

        self.notice_close_btn = QPushButton("✕")

        self.notice_close_btn.setObjectName(
            "noticeClose"
        )

        self.notice_close_btn.setFixedWidth(26)

        self.notice_close_btn.setCursor(
            Qt.PointingHandCursor
        )

        self.notice_close_btn.clicked.connect(
            self._on_notice_dismiss
        )

        notice_layout.addWidget(
            self.notice_close_btn
        )

        self._notice_action = None
        self._notice_dismiss = None
        self._notice_queue = []

        self.notice_bar.setVisible(False)

        return self.notice_bar

    def show_notice(self, text, action_text=None, action=None,
                    dismiss=None, kind="info"):
        """立即显示通知条；kind 决定配色：info / warn / ok"""
        self.notice_label.setText(text)

        self._notice_action = action
        self._notice_dismiss = dismiss

        if action_text and action:
            self.notice_action_btn.setText(action_text)
            self.notice_action_btn.setEnabled(True)
            self.notice_action_btn.setVisible(True)
        else:
            self.notice_action_btn.setVisible(False)

        self.notice_bar.setProperty("kind", kind)

        style = self.notice_bar.style()
        if style is not None:
            style.unpolish(self.notice_bar)
            style.polish(self.notice_bar)

        self.notice_bar.setVisible(True)

    def set_notice_text(self, text):
        self.notice_label.setText(text)

    def hide_notice(self):
        self.notice_bar.setVisible(False)
        self._notice_action = None
        self._notice_dismiss = None

    def _auto_hide_notice(self):
        """成功类提示自动消失，并让位给队列里的下一条"""
        self.hide_notice()
        self.flush_pending_notice()

    def _on_notice_action(self):
        action = self._notice_action
        if action:
            action()

    def _on_notice_dismiss(self):
        dismiss = self._notice_dismiss
        self.hide_notice()
        if dismiss:
            dismiss()
        # 腾出位置后再弹队列里的下一条（延后一轮事件循环，避免重入）
        QTimer.singleShot(0, self.flush_pending_notice)

    def _is_idle(self):
        """当前没有下载任务在跑"""
        thread = self.download_thread
        return not (thread is not None and thread.isRunning())

    def queue_notice(self, text, action_text=None, action=None,
                     dismiss=None, kind="info"):
        """空闲且当前没有别的提示占位时立刻弹；否则排队，等腾出位置再弹"""
        payload = (text, action_text, action, dismiss, kind)

        if self._is_idle() and not self.notice_bar.isVisible():
            self.show_notice(*payload)
        else:
            self._notice_queue.append(payload)

    def flush_pending_notice(self):
        """空闲且提示条空闲时，弹出排队中的下一条提示"""
        if not self._notice_queue:
            return

        if not self._is_idle() or self.notice_bar.isVisible():
            return

        self.show_notice(*self._notice_queue.pop(0))

    # ========================================================
    # 环境依赖面板入口
    # ========================================================

    def open_environment_dialog(self):
        """打开（或复用）环境依赖面板，每次打开都重新自检一次"""
        dialog = getattr(self, "_env_dialog", None)

        if dialog is None:
            dialog = EnvironmentDialog(self)
            self._env_dialog = dialog
        else:
            dialog.refresh()

        dialog.show()
        dialog.raise_()
        dialog.activateWindow()

    # ========================================================
    # yt-dlp 每日版本检查 → 通知条
    # ========================================================

    def _on_ytdlp_update_checked(self, local, latest, need_update, reason):
        """每日版本检查结果（GUI 线程）"""
        self.log(
            f"[YT-DLP] update_check={reason} "
            f"local={local} latest={latest or 'NA'}"
        )

        if not need_update:
            return

        if self.config.get("ytdlp_update_skipped_version", "") == latest:
            self.log(f"[YT-DLP] 新版本 {latest} 已被忽略，不再提示")
            return

        self.queue_notice(
            f"yt-dlp 有新版本可更新：{local} → {latest}",
            "立即更新",
            lambda: self._start_ytdlp_update(latest),
            lambda: self.config.set("ytdlp_update_skipped_version", latest),
            "info"
        )

    def _start_ytdlp_update(self, latest):
        """更新 yt-dlp：后台线程执行，避免卡界面；下载中不动 exe"""
        if not self._is_idle():
            self.set_notice_text(
                "正在下载视频，等当前任务结束后再点更新"
            )
            return

        self.notice_action_btn.setEnabled(False)

        self.set_notice_text(f"正在更新 yt-dlp → {latest} …")

        self.log(f"[YT-DLP] 开始更新 → {latest}")

        def _worker():
            ok = ytdlp_perform_update()
            if ok:
                reset_ytdlp_version_cache()
            self.ytdlp_updated_signal.emit(ok, get_ytdlp_version())

        threading.Thread(
            target=_worker,
            daemon=True
        ).start()

    def _on_ytdlp_updated(self, ok, version):
        if ok:
            self.log(f"[YT-DLP] 更新完成，当前版本 {version}")
            self.show_notice(
                f"yt-dlp 已更新到 {version}",
                kind="ok"
            )
            QTimer.singleShot(6000, self._auto_hide_notice)
        else:
            self.log("[YT-DLP] 更新失败，继续使用当前版本")
            self.show_notice(
                "yt-dlp 更新失败（网络或文件占用），仍在使用当前版本",
                "重试",
                lambda: self._start_ytdlp_update(version),
                None,
                "warn"
            )

    # ========================================================
    # 组件缺失 → 自动下载安装（通知条同步显示进度与结果）
    #
    # 必需组件（yt-dlp / FFmpeg）在启动自检后按顺序自动补齐；
    # 推荐组件（JS 运行时）仍由提示条上的按钮触发，走同一套安装流程。
    # ========================================================

    COMPONENT_INSTALLERS = {
        "ytdlp": ("yt-dlp", "约 18MB"),
        "ffmpeg": ("FFmpeg / FFprobe", "约 105MB"),
        "jsruntime": ("JS 运行时（Deno）", "约 40MB"),
    }

    def start_auto_install(self, names):
        """按顺序自动安装缺失组件（一个装完再装下一个，避免抢带宽）"""
        self._install_queue = list(names)
        self._install_next()

    def _install_next(self):
        if not self._install_queue:
            self._start_daily_update_check()
            return
        self.start_component_install(self._install_queue.pop(0))

    def start_component_install(self, name):
        """下载并安装单个组件：后台线程执行，不阻塞界面"""
        label, size = self.COMPONENT_INSTALLERS[name]

        self.log(f"[自检] {label} 缺失，开始自动下载（{size}）")

        self.show_notice(
            f"缺少 {label}，正在自动下载（{size}）…",
            kind="warn"
        )

        def _worker():
            if name == "ytdlp":
                ok, info = ytdlp_install_missing(
                    log=self.worker_log_signal.emit
                )
            elif name == "ffmpeg":
                ok, info = install_ffmpeg_bundle(
                    log=self.worker_log_signal.emit
                )
            else:
                ok, info = install_portable_js_runtime(
                    log=self.worker_log_signal.emit
                )
            self.component_installed_signal.emit(name, ok, info)

        threading.Thread(
            target=_worker,
            daemon=True
        ).start()

    def _on_component_installed(self, name, ok, info):
        label, _size = self.COMPONENT_INSTALLERS.get(name, (name, ""))

        if ok:
            self.log(f"[自检] {label} 安装完成：{info}")
            self.show_notice(
                f"{label} 已自动安装完成（{info}）",
                kind="ok"
            )
            QTimer.singleShot(6000, self._auto_hide_notice)
            self._install_next()
        else:
            self.log(f"[自检] {label} 自动下载失败：{info}")
            self.show_notice(
                f"{label} 自动下载失败（{info}）",
                "重试",
                lambda: self.start_component_install(name),
                None,
                "warn"
            )

    def _start_daily_update_check(self):
        """每天第一次启动检查 yt-dlp 版本：后台线程，不阻塞界面也不打断下载；
        有新版本时不静默替换，交给通知条在空闲时弹出，由用户决定是否更新。"""
        if not check_file(YTDLP_EXE):
            return

        check_ytdlp_update_async(
            callback=lambda local, latest, need, reason:
                self.ytdlp_update_signal.emit(local, latest or "", need, reason)
        )

    # ========================================================
    # JS 运行时缺失 → 通知条一键安装便携版
    # ========================================================

    def _check_js_runtime_notice(self):
        runtime, path = js_runtime_lookup()

        if runtime:
            self.log(f"[JS Runtime] {runtime} → {path}")
            return

        self.log(
            "[JS Runtime] 未检测到（YouTube 等站点会退回 native Python solver）"
        )

        if self.config.get("js_runtime_install_skipped", False):
            return

        self.queue_notice(
            "未检测到 JS 运行时，YouTube 解密会退回不稳定的 Python solver",
            "安装便携版",
            lambda: self.start_component_install("jsruntime"),
            lambda: self.config.set("js_runtime_install_skipped", True),
            "warn"
        )

    def apply_style(self):

        self.setStyleSheet(
            """
            QMainWindow {
                background: #1e1e2e;
            }

            QWidget {
                color: #cdd6f4;
                font-family: "Microsoft YaHei";
                font-size: 13px;
            }

            QLabel#title {
                color: #cba6f7;
                font-size: 18px;
                font-weight: bold;
                padding-left: 2px;
            }

            QLabel#status {
                color: #a6adc8;
                background: rgba(166, 173, 200, 0.10);
                border: 1px solid #585b70;
                border-radius: 11px;
                padding: 3px 14px;
                font-size: 12px;
                font-weight: bold;
            }

            QLabel#status[state="idle"] {
                color: #a6adc8;
                background: rgba(166, 173, 200, 0.10);
                border-color: #585b70;
            }

            QLabel#status[state="busy"] {
                color: #89b4fa;
                background: rgba(137, 180, 250, 0.16);
                border-color: #89b4fa;
            }

            QLabel#status[state="ok"] {
                color: #a6e3a1;
                background: rgba(166, 227, 161, 0.16);
                border-color: #a6e3a1;
            }

            QLabel#status[state="warn"] {
                color: #f9e2af;
                background: rgba(249, 226, 175, 0.16);
                border-color: #f9e2af;
            }

            QLabel#status[state="stop"] {
                color: #f38ba8;
                background: rgba(243, 139, 168, 0.16);
                border-color: #f38ba8;
            }

            QLabel#smallLabel {
                color: #a6adc8;
            }

            QLabel#sectionTitle {
                color: #cdd6f4;
                font-size: 13px;
                font-weight: bold;
            }

            QLabel#countLabel {
                color: #7f849c;
                margin-left: 8px;
            }

            QLabel#hint {
                color: #6c7086;
                font-size: 11px;
            }

            /* ---------- 顶部通知条 ---------- */

            QFrame#noticeBar {
                background: rgba(137, 180, 250, 0.12);
                border: 1px solid #89b4fa;
                border-radius: 8px;
            }

            QFrame#noticeBar[kind="warn"] {
                background: rgba(249, 226, 175, 0.12);
                border-color: #f9e2af;
            }

            QFrame#noticeBar[kind="ok"] {
                background: rgba(166, 227, 161, 0.12);
                border-color: #a6e3a1;
            }

            QLabel#noticeText {
                color: #cdd6f4;
                font-size: 12px;
            }

            QPushButton#noticeAction {
                background: #89b4fa;
                border: none;
                border-radius: 6px;
                color: #1e1e2e;
                font-size: 12px;
                font-weight: bold;
                padding: 4px 14px;
                min-height: 24px;
            }

            QPushButton#noticeAction:hover {
                background: #a6c8ff;
            }

            QPushButton#noticeAction:disabled {
                background: #45475a;
                color: #7f849c;
            }

            QPushButton#noticeClose {
                background: transparent;
                border: none;
                color: #a6adc8;
                font-size: 13px;
                padding: 2px;
                min-height: 24px;
            }

            QPushButton#noticeClose:hover {
                color: #f38ba8;
            }

            /* ---------- 容器 ---------- */

            QFrame#settingFrame,
            QFrame#linkFrame {
                background: #313244;
                border: 1px solid #45475a;
                border-radius: 10px;
            }

            /* ---------- 输入控件 ---------- */

            QLineEdit,
            QComboBox {
                background: #1e1e2e;
                border: 1px solid #45475a;
                border-radius: 6px;
                color: #cdd6f4;
                padding: 5px 10px;
                min-height: 27px;
                selection-background-color: #89b4fa;
                selection-color: #1e1e2e;
            }

            QLineEdit:hover,
            QComboBox:hover {
                border: 1px solid #6c7086;
                background: #232336;
            }

            QLineEdit:focus,
            QComboBox:focus,
            QComboBox:on {
                border: 1px solid #89b4fa;
                background: #1e1e2e;
            }

            QLineEdit:disabled,
            QComboBox:disabled {
                background: #262636;
                color: #6c7086;
                border-color: #3a3a4c;
            }

            QLineEdit[readOnly="true"] {
                background: #262636;
                color: #a6adc8;
            }

            QComboBox::drop-down {
                border: none;
                width: 22px;
                margin-right: 2px;
            }

            QComboBox QAbstractItemView {
                background: #313244;
                color: #cdd6f4;
                selection-background-color: #45475a;
                selection-color: #cba6f7;
                border: 1px solid #585b70;
                border-radius: 6px;
                padding: 4px;
                outline: none;
            }

            /* ---------- 按钮 ---------- */

            QPushButton {
                background: #45475a;
                border: 1px solid #585b70;
                border-radius: 6px;
                color: #cdd6f4;
                padding: 6px 14px;
                min-height: 28px;
            }

            QPushButton:hover {
                background: #585b70;
                border: 1px solid #7f849c;
            }

            QPushButton:pressed {
                background: #313244;
                border-color: #585b70;
            }

            QPushButton:focus {
                border: 1px solid #89b4fa;
            }

            QPushButton:disabled {
                background: #2c2c3d;
                color: #585b70;
                border-color: #3a3a4c;
            }

            QPushButton#primaryButton {
                background: qlineargradient(x1:0, y1:0, x2:0, y2:1,
                    stop:0 #9dc1fb, stop:1 #89b4fa);
                border: 1px solid #b4befe;
                border-radius: 6px;
                color: #1e1e2e;
                font-weight: bold;
                padding-left: 22px;
                padding-right: 22px;
            }

            QPushButton#primaryButton:hover {
                background: qlineargradient(x1:0, y1:0, x2:0, y2:1,
                    stop:0 #c2cbff, stop:1 #b4befe);
            }

            QPushButton#primaryButton:pressed {
                background: #74c7ec;
                border-color: #89dceb;
            }

            QPushButton#primaryButton:disabled {
                background: #2c2c3d;
                color: #585b70;
                border-color: #3a3a4c;
            }

            /* ---------- 复选框 ---------- */

            QCheckBox {
                color: #cdd6f4;
                spacing: 7px;
            }

            QCheckBox::indicator {
                width: 15px;
                height: 15px;
                border: 1px solid #585b70;
                border-radius: 4px;
                background: #1e1e2e;
            }

            QCheckBox::indicator:hover {
                border-color: #89b4fa;
            }

            QCheckBox::indicator:checked {
                background: #89b4fa;
                border-color: #b4befe;
            }

            QCheckBox::indicator:disabled {
                background: #2c2c3d;
                border-color: #3a3a4c;
            }

            /* ---------- 链接列表 ---------- */

            QListWidget#linkList {
                background: #1e1e2e;
                border: 1px solid #45475a;
                border-radius: 8px;
                color: #cdd6f4;
                padding: 4px;
                outline: none;
            }

            QListWidget#linkList::item {
                padding: 6px 8px;
                border-radius: 5px;
                margin: 1px 2px;
            }

            QListWidget#linkList::item:hover {
                background: #2a2a3c;
            }

            QListWidget#linkList::item:selected {
                background: #45475a;
                color: #cba6f7;
            }

            /* ---------- 日志 ---------- */

            QTextEdit#logEdit {
                background: #181825;
                border: 1px solid #45475a;
                border-radius: 8px;
                color: #a6adc8;
                padding: 8px;
                font-family: "Cascadia Mono", "JetBrains Mono", Consolas,
                             "Microsoft YaHei";
                font-size: 12px;
                selection-background-color: #45475a;
                selection-color: #cdd6f4;
            }

            /* ---------- 进度条 ---------- */

            QProgressBar {
                background: #181825;
                border: 1px solid #45475a;
                border-radius: 10px;
                text-align: center;
                color: #cdd6f4;
                font-size: 11px;
                font-weight: bold;
                min-height: 20px;
                max-height: 20px;
            }

            QProgressBar::chunk {
                background: qlineargradient(x1:0, y1:0, x2:0, y2:1,
                    stop:0 #7a99f2, stop:1 #4c6ed6);
                border-radius: 9px;
            }

            /* ---------- 菜单 ---------- */

            QMenu {
                background: #313244;
                color: #cdd6f4;
                border: 1px solid #585b70;
                border-radius: 8px;
                padding: 5px;
            }

            QMenu::item {
                padding: 7px 26px;
                border-radius: 5px;
                margin: 1px 2px;
            }

            QMenu::item:selected {
                background: #45475a;
                color: #89b4fa;
            }

            QMenu::item:disabled {
                color: #585b70;
            }

            QMenu::separator {
                height: 1px;
                background: #45475a;
                margin: 4px 8px;
            }

            /* ---------- 提示气泡 ---------- */

            QToolTip {
                background: #181825;
                color: #cdd6f4;
                border: 1px solid #585b70;
                border-radius: 6px;
                padding: 5px 8px;
                font-size: 12px;
            }

            /* ---------- 对话框 ---------- */

            QDialog,
            QMessageBox {
                background: #1e1e2e;
            }

            QMessageBox QLabel {
                color: #cdd6f4;
            }

            QMessageBox QPushButton {
                min-width: 78px;
            }

            /* ---------- 滚动条 ---------- */

            QScrollBar:vertical {
                background: transparent;
                width: 10px;
                margin: 2px 2px 2px 0px;
                border-radius: 5px;
            }

            QScrollBar::handle:vertical {
                background: #45475a;
                border-radius: 5px;
                min-height: 24px;
            }

            QScrollBar::handle:vertical:hover {
                background: #6c7086;
            }

            QScrollBar::handle:vertical:pressed {
                background: #89b4fa;
            }

            QScrollBar:horizontal {
                background: transparent;
                height: 10px;
                margin: 0px 2px 2px 2px;
                border-radius: 5px;
            }

            QScrollBar::handle:horizontal {
                background: #45475a;
                border-radius: 5px;
                min-width: 24px;
            }

            QScrollBar::handle:horizontal:hover {
                background: #6c7086;
            }

            QScrollBar::handle:horizontal:pressed {
                background: #89b4fa;
            }

            QScrollBar::add-line:vertical,
            QScrollBar::sub-line:vertical,
            QScrollBar::add-line:horizontal,
            QScrollBar::sub-line:horizontal {
                width: 0px;
                height: 0px;
            }

            QScrollBar::add-page:vertical,
            QScrollBar::sub-page:vertical,
            QScrollBar::add-page:horizontal,
            QScrollBar::sub-page:horizontal {
                background: none;
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

        self.save_timer.timeout.connect(
            self.auto_save_settings
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

        if getattr(self, "_restoring", True):
            return

        if not hasattr(self, "save_timer"):
            return

        self.save_timer.start()

    def moveEvent(self, event):

        super().moveEvent(event)

        # 拖动窗口后延迟落盘，避免异常退出丢失位置
        self.schedule_save()

    def resizeEvent(self, event):

        super().resizeEvent(event)

        self.schedule_save()

    def _current_window_geometry(self):
        """采集用于下次还原的窗口几何。

        - 位置取 frameGeometry()：move() 定位的是窗口框架左上角，
          若存回 x()/y()（客户区坐标），每次重启窗口都会向下偏移一个标题栏高度。
        - 最大化时存 normalGeometry()，还原后取消最大化才能回到合理大小。
        """

        maximized = (
            self.isMaximized()
            or
            self.isFullScreen()
        )

        if maximized:

            geo = self.normalGeometry()

            return {
                "x": geo.x(),
                "y": geo.y(),
                "width": geo.width(),
                "height": geo.height(),
                "maximized": True
            }

        frame = self.frameGeometry()

        return {
            "x": frame.x(),
            "y": frame.y(),
            "width": self.width(),
            "height": self.height(),
            "maximized": False
        }

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

            data["auto_download_paused"] = bool(
                getattr(self, "auto_download_paused", False)
            )

            data["window"] = (
                self._current_window_geometry()
            )

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

        port = self.config.get("bridge_port", BRIDGE_DEFAULT_PORT)

        # 连接线程安全信号
        self.bridge_log_signal.connect(self._on_bridge_log)
        self.bridge_url_signal.connect(self._on_bridge_url)
        self.bridge_cookies_signal.connect(self._on_bridge_cookies)
        self.bridge_ua_signal.connect(self._on_bridge_ua)
        self.bridge_activate_signal.connect(self._on_bridge_activate)

        self.bridge = OmniGetBridge(
            token=token,
            port=port,
            link_file=LINK_FILE,
            log_callback=lambda msg: self.bridge_log_signal.emit(msg),
            url_callback=lambda url: self.bridge_url_signal.emit(url),
            cookies_callback=lambda c, u, s: self.bridge_cookies_signal.emit(c, u, s),
            ua_callback=lambda ua: self.bridge_ua_signal.emit(ua),
            activate_callback=lambda: self.bridge_activate_signal.emit()
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
                paused = getattr(self, 'auto_download_paused', False)

                # 指纹去重：本地已存在该视频文件（暂停时不做去重拦截，链接先入列表，恢复调度时再检查）
                if not paused and self._check_fingerprint(url):
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
                    if paused:
                        # 暂停时链接已在列表：仅提示，确保用户看得到
                        self._qlog(f"[自动下载] 已暂停，链接已在队列中等待：{url}")
                    else:
                        self._qlog(f"[提示] 链接已在队列中，忽略：{url}")
                    return
                
                # 如果之前失败过，允许重新尝试
                if url in self.tried_urls:
                    self.tried_urls.discard(url)
                    self._qlog(f"[重试] 重新尝试之前失败的链接：{url}")
                
                self.link_list.addItem(url)
                self.link_list.urls_changed.emit()
                # 新链接入列后滚动到底部，确保用户能看见新增项（尤其暂停等待时）
                try:
                    self.link_list.scrollToBottom()
                    if self.link_list.count() > 0:
                        self.link_list.setCurrentRow(self.link_list.count() - 1)
                except Exception:
                    pass
                if getattr(self, 'auto_download_paused', False):
                    self._qlog(f"[自动下载] 已暂停，新任务加入队列等待：{url}")
                else:
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

    def _on_bridge_activate(self):
        """扩展勾选「下载时打开应用」：把主窗口拉到前台（主线程）"""
        self.log("[Bridge] 收到「下载时打开应用」请求，正在唤醒窗口")
        try:
            if self.isMinimized() or not self.isVisible():
                self.showNormal()
            self.setWindowState(
                (self.windowState() & ~Qt.WindowMinimized) | Qt.WindowActive
            )
            self.raise_()
            self.activateWindow()

            # Windows 有前台锁：非前台进程调用 SetForegroundWindow（activateWindow
            # 底层就是它）会被系统拒绝，只闪任务栏图标。需要先把自己的线程附加到
            # 当前前台线程的输入队列，借用它的“同意”才能真正抢到焦点。
            if sys.platform == "win32":
                self._force_foreground_windows()
        except Exception as e:
            print(f"[Bridge] 窗口激活失败：{e}", flush=True)

    def _force_foreground_windows(self):
        """Windows 专用：绕过前台锁，把窗口真正切到最前"""
        import ctypes

        user32 = ctypes.windll.user32
        kernel32 = ctypes.windll.kernel32
        hwnd = int(self.winId())

        SW_RESTORE = 9
        fg_hwnd = user32.GetForegroundWindow()
        fg_tid = user32.GetWindowThreadProcessId(fg_hwnd, None) if fg_hwnd else 0
        cur_tid = kernel32.GetCurrentThreadId()

        attached = False
        try:
            if fg_tid and fg_tid != cur_tid:
                attached = bool(user32.AttachThreadInput(fg_tid, cur_tid, True))

            if user32.IsIconic(hwnd):
                user32.ShowWindow(hwnd, SW_RESTORE)
            user32.BringWindowToTop(hwnd)
            user32.SetForegroundWindow(hwnd)

            # SwitchToThisWindow 不受前台锁限制，作为最后兜底
            if user32.GetForegroundWindow() != hwnd:
                try:
                    user32.SwitchToThisWindow(hwnd, True)
                except Exception:
                    pass
        finally:
            if attached:
                user32.AttachThreadInput(fg_tid, cur_tid, False)

    def on_auto_pause_changed(self, checked):
        """勾选/取消「暂停自动下载」：只控制队列调度，不触碰当前任务"""
        self.auto_download_paused = bool(checked)
        # 立即保存配置（不影响其他字段，直接写当前状态）
        try:
            self.config.data["auto_download_paused"] = bool(checked)
            self.config.save()
        except Exception as e:
            print(f"[自动下载] 保存暂停状态失败：{e}", flush=True)

        if checked:
            self._qlog("[自动下载] 已暂停，队列等待中")
            # 仅在没有任务运行时更新状态标签，避免覆盖“下载中”
            if not (self.download_thread and self.download_thread.isRunning()):
                self.set_status("paused")
        else:
            self._qlog("[自动下载] 已恢复，继续处理队列")
            # 立即从现有列表中寻找待下载任务并恢复执行（不干扰正在运行的线程）
            QTimer.singleShot(200, self.auto_start_download)

    def auto_start_download(self):
        """自动触发下载(静默模式,不弹对话框)"""
        try:
            # 暂停自动下载：不启动新任务，列表任务保留，队列等待（不影响正在运行的任务）
            if getattr(self, 'auto_download_paused', False):
                print("[自动下载] 已暂停，队列等待中", flush=True)
                return

            # 如果下载线程正在运行,不重复触发(下载完成后会自动检查队列)
            if (
                self.download_thread
                and
                self.download_thread.isRunning()
            ):
                print("[自动下载] 下载进行中,等待完成后自动继续", flush=True)
                return
    
            # 只选择尚未尝试过且指纹不存在的链接(失败的不自动重试)
            all_urls = self.link_list.get_urls()
            urls = [
                u for u in all_urls
                if u not in self.tried_urls
                and not self._check_fingerprint(u)
            ]
            # 清理列表中被指纹拦截的重复链接（如暂停期间入列的）：记录跳过并从列表移除
            for u in all_urls:
                if u not in urls and u not in self.tried_urls and self._check_fingerprint(u):
                    self.link_list.remove_url(u)
                    self._qlog(f"[跳过] 本地已有该视频，跳过：{u}")
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
        """桥接收到 Cookie → 按域名合并写入（不覆盖其他平台）

        扩展每 2 分钟以及每次 Service Worker 冷启动都会重发一份全量 Cookie，
        内容通常完全没变。这里先按内容指纹去重：与上次相同则直接返回，
        既不重写文件也不打日志，避免日志霸屏和无意义的磁盘写入。
        """
        try:
            if not cookies:
                self.log("[Bridge] 收到空 Cookie，跳过")
                return

            # 内容指纹去重（只比 Cookie 本体，不含 UA/来源页）
            import hashlib
            try:
                fingerprint = hashlib.sha256(
                    json.dumps(
                        sorted(
                            (
                                str(c.get("domain", "")),
                                str(c.get("path", "")),
                                str(c.get("name", "")),
                                str(c.get("value", "")),
                            )
                            for c in cookies
                        ),
                        ensure_ascii=False,
                    ).encode("utf-8")
                ).hexdigest()
            except Exception:
                fingerprint = None

            if fingerprint and fingerprint == getattr(self, "_last_cookie_fingerprint", None):
                # 内容未变化：静默跳过（print 保留，便于详细排查）
                print(f"[Bridge] Cookie 内容未变化，跳过（{len(cookies)} 条）", flush=True)
                return

            self._last_cookie_fingerprint = fingerprint

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
        """桥接收到 UA 时更新本地文件（UA 未变化则静默跳过）"""
        try:
            ua = (ua or "").strip()
            if not ua:
                return

            if ua == getattr(self, "_last_bridge_ua", None):
                return

            self._last_bridge_ua = ua

            UA_FILE.parent.mkdir(parents=True, exist_ok=True)
            UA_FILE.write_text(ua + "\n", encoding="utf-8")
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

    # 右上角状态胶囊：state ∈ idle / busy / ok / warn / stop
    STATUS_PRESETS = {
        "ready":    ("✦ 准备就绪",   "idle"),
        "waiting":  ("✦ 随时待命",   "idle"),
        "running":  ("▶ 正在下载",   "busy"),
        "done":     ("✓ 全部完成",   "ok"),
        "saved":    ("✓ 目录已更新", "ok"),
        "paused":   ("⏸ 队列已暂停", "warn"),
        "stopping": ("◼ 正在收尾",   "warn"),
        "stopped":  ("◼ 已停止",     "stop"),
    }

    def set_status(self, key):
        """更新右上角状态标签（文案 + 配色）"""
        text, state = self.STATUS_PRESETS.get(
            key,
            self.STATUS_PRESETS["ready"]
        )

        self.lbl_status.setText(text)
        self.lbl_status.setProperty("state", state)

        # 动态属性变更后必须重新计算样式，否则配色不刷新
        style = self.lbl_status.style()
        style.unpolish(self.lbl_status)
        style.polish(self.lbl_status)

    def log(
        self,
        text
    ):

        visible, text = gui_log_visible(
            str(text)
        )

        if not visible:
            return

        self.log_edit.append(
            text
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
        """日志区域右键菜单：全选、复制、删除、清空"""
        menu = QMenu(self)

        action_select_all = QAction("全选", self)
        action_copy = QAction("复制", self)
        action_delete = QAction("删除", self)
        action_clear = QAction("清空", self)

        has_selection = self.log_edit.textCursor().hasSelection()
        action_copy.setEnabled(has_selection)
        action_delete.setEnabled(has_selection)
        action_clear.setEnabled(bool(self.log_edit.toPlainText()))

        menu.addAction(action_select_all)
        menu.addAction(action_copy)
        menu.addSeparator()
        menu.addAction(action_delete)
        menu.addAction(action_clear)

        action_select_all.triggered.connect(self.log_edit.selectAll)
        action_copy.triggered.connect(self.log_copy_selected)
        action_delete.triggered.connect(self.log_delete_selected)
        action_clear.triggered.connect(self.log_clear)

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

        if VERBOSE_LOG:
            self.log(
                "日志模式：详细（检测到 log.txt，显示 yt-dlp 原始输出与全部调试信息）"
            )
        else:
            self.log(
                "日志模式：精简（只显示流程与错误；"
                "需要详细输出时在运行目录建一个空的 log.txt 后重启）"
            )

        # 启动环境检查：ffmpeg -encoders 与 yt-dlp --version 都要拉起子进程，
        # 合计 1-5 秒。放后台线程执行，窗口先出来，结果到了再补进日志。
        self.env_ready_signal.connect(self._on_env_ready)
        self.ytdlp_update_signal.connect(self._on_ytdlp_update_checked)
        self.ytdlp_updated_signal.connect(self._on_ytdlp_updated)
        self.component_installed_signal.connect(self._on_component_installed)
        self.worker_log_signal.connect(self.log)

        self.log("")
        self.log("环境检查：正在后台检测（不阻塞界面）…")

        def _env_worker():
            try:
                rows = check_environment()
                version = get_ytdlp_version()
            except Exception as e:
                rows = [("环境检查", f"失败：{e}")]
                version = "unknown"
            self.env_ready_signal.emit(rows, version)

        threading.Thread(
            target=_env_worker,
            daemon=True
        ).start()

        self.log(
            f"yt-dlp："
            f"{'正常' if check_file(YTDLP_EXE) else '未找到（自检后会自动下载最新版）'}"
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

        # 指纹统计不在这里输出：此时 _build_fingerprint_set() 尚未执行，
        # 打出来会是 0/0 与界面标签不一致。改由扫描完成后统一输出一次。

        self.log("")

    def _on_env_ready(self, env_rows, ytdlp_version):
        """后台环境检查完成（GUI 线程）：补全启动日志"""
        self.log("")
        self.log("=" * 30)
        self.log("ENVIRONMENT CHECK")
        self.log("=" * 30)
        for name, status in env_rows:
            # 状态为空的行是分组标题（【必需组件】等），原样输出
            if status:
                self.log(f"{name:<10}: {status}")
            else:
                self.log(name)
        self.log("=" * 30)
        self.log("")

        # [YT-DLP] 路径与版本：出现解析异常时可立即判断 yt-dlp 是否过旧
        self.log(f"[YT-DLP] path={YTDLP_EXE}")
        self.log(f"[YT-DLP] version={ytdlp_version}")
        if YTDLP_UPDATE_CHECK:
            self.log("[YT-DLP] update_check=已开启（每天第一次启动查一次，"
                     "发现新版本只提示不自动替换）")
        else:
            self.log("[YT-DLP] update_check=已关闭（删除运行目录的 "
                     "ytdlp_noupdate.txt 后重启可恢复）")

        task_debug_section("YT-DLP", [
            ("path", str(YTDLP_EXE)),
            ("version", ytdlp_version),
            ("source", "local"),
        ])
        task_debug_section("ENVIRONMENT CHECK", env_rows)

        # 必需组件缺失：启动自检后按顺序自动下载补齐（通知条显示进度与结果）
        pending = missing_required_installers()

        # 推荐组件：JS 运行时缺失时给出一键安装提示条（排在自动安装提示后面）
        self._check_js_runtime_notice()

        if pending:
            self.start_auto_install(pending)
        else:
            self._start_daily_update_check()

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

            self.set_status("saved")

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

        self.set_status("running")

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
                    item.setForeground(QColor("#f38ba8"))
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
                    if fg and fg.color() and fg.color().name().lower() == "#f38ba8":
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

    _KNOWN_PLATFORMS = {"youtube", "tiktok", "instagram", "facebook", "twitter", "vimeo", "reddit", "pinterest", "twitch", "dailymotion", "snapchat", "other"}

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

                # 指纹识别：从后往前找平台标识，从平台名到末尾即为指纹（分割规则：取最后出现的 平台_）
                # 前缀不影响判断：时间戳、任意字符均被过滤，如 202608230032_tiktok_xxx 或 hhh_jjjjj____tiktok_xxx → tiktok_xxx
                fp = None
                best_idx = -1
                for plat in self._KNOWN_PLATFORMS:
                    idx = stem.rfind(f"{plat}_")
                    while idx > 0 and stem[idx - 1] != "_":
                        # 平台标识前必须是下划线或位于文件名开头，避免误匹配单词内部（如 mytiktok_）
                        idx = stem.rfind(f"{plat}_", 0, idx)
                    if idx >= 0 and len(stem) > idx + len(plat) + 1 and idx > best_idx:
                        best_idx = idx
                        fp = stem[idx:]
                if fp:
                    fingerprints.add(fp)
                    continue

                # 旧命名兼容：video_{视频ID}.mp4，从目录推断平台
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
        """启动/手动扫描时：扫描下载目录（含子目录）所有 mp4，一次性建立内存指纹库"""
        self.fingerprint_set, total, valid, no_fp_list = self._scan_fingerprints()
        self._no_fingerprint_files = no_fp_list
        self._total_mp4_count = total
        self._valid_fingerprint_count = valid
        self._update_stats_labels()

        # 与界面标签同一时刻、同一数据源输出，避免两处数字不一致
        self.log(
            f"目录视频：{total:,}  有效指纹：{valid:,}"
            f"  内存指纹库：{len(self.fingerprint_set):,} 条"
        )

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
        """下载成功后：新视频 DNA 直接存入内存库（不扫描磁盘，下次判重直接查内存）"""
        info = extract_video_id_from_url(url)
        if info:
            platform, vid = info
            fp = f"{platform}_{vid}"
            if fp not in self.fingerprint_set:
                self.fingerprint_set.add(fp)
                self._valid_fingerprint_count = len(self.fingerprint_set)
                self._total_mp4_count += 1
                self._update_stats_labels()
                self.log(f"[DNA] 已存入内存：{fp}")

    def _do_scan(self):
        """用户点击[扫描]按钮：重新扫描输出目录并重建指纹库"""
        output_dir = self.output_edit.text().strip()
        if not output_dir:
            output_dir = str(DEFAULT_OUTPUT_DIR)

        self.log(f"[扫描] 开始扫描：{output_dir}")
        # 统计数字由 _build_fingerprint_set 统一输出，此处不再重复
        self._build_fingerprint_set()
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
                    is_red = fg and fg.color() and fg.color().name().lower() == "#f38ba8"
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

            self.set_status("stopping")

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

        self.set_status("done")

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

        # 进入空闲：补弹之前挂起的提示（如 yt-dlp 新版本）
        self.flush_pending_notice()

        # 手动停止：不自动继续
        if self._stopped_by_user:
            self.set_status("stopped")
            self.log("已手动停止任务")
            self._qlog("[队列] 任务已手动停止")
            return

        self.set_status("done")

        # 自动下载队列:检查是否还有未尝试的链接
        remaining_urls = [
            u for u in self.link_list.get_urls()
            if u not in self.tried_urls
        ]
        if remaining_urls:
            # 自动队列已暂停：当前任务正常完成后不启动下一个，任务保留在列表
            if getattr(self, 'auto_download_paused', False):
                self._qlog("[自动下载] 当前任务完成，自动队列已暂停")
                self.set_status("paused")
                return
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
            self.set_status("waiting")
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
        QColor("#1e1e2e")
    )

    palette.setColor(
        QPalette.WindowText,
        QColor("#cdd6f4")
    )

    palette.setColor(
        QPalette.Base,
        QColor("#181825")
    )

    palette.setColor(
        QPalette.AlternateBase,
        QColor("#313244")
    )

    palette.setColor(
        QPalette.Text,
        QColor("#cdd6f4")
    )

    palette.setColor(
        QPalette.Button,
        QColor("#45475a")
    )

    palette.setColor(
        QPalette.ButtonText,
        QColor("#cdd6f4")
    )

    palette.setColor(
        QPalette.Highlight,
        QColor("#89b4fa")
    )

    palette.setColor(
        QPalette.HighlightedText,
        QColor("#1e1e2e")
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