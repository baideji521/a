#!/usr/bin/env python3
"""把手机账号的 DeepSeek 包装成一个 OpenAI 兼容的本地模型服务。

起了这个之后，dsh（deepseek-harness）就能像调 Ollama 一样调它：

    python ds_bridge.py                       # 听 0.0.0.0:11999（局域网可访问）
    python ds_bridge.py --host 127.0.0.1      # 只给本机用
    curl http://127.0.0.1:11999/v1/models

对外暴露两个口子（只有这两个，dsh 运行时只用第一个）：
    POST /v1/chat/completions   强制 SSE 流式，delta.content / delta.reasoning_content
    GET  /v1/models             给 dsh 的 Models 页面探测用

几个刻意的取舍：
  * 每个账号自己串行（一把锁 + 最小间隔），绝不并发打同一个账号；账号之间
    并行，由 Pool 挑人（模型名带 @账号 后缀就钉住那个号）。
  * 多轮对话按"历史前缀"复用 DeepSeek 会话，只把新增那一轮发上去，
    不然 agent loop 每轮都要重传整个 transcript，又慢又费。
  * DeepSeek 网页版接口本身没有 function calling，工具调用是靠提示词
    约定 JSON 再翻译成 tool_calls 的，属于模拟，不是原生（见 TOOL_PROTOCOL）。
"""
import argparse
import base64
import hashlib
import json
import mimetypes
import pathlib
import re
import socket
import subprocess
import sys
import tempfile
import threading
import time
import traceback

import requests


# 2026-09-27 第326步（用户要求「以后只认 ds_bridge.ini」）：配置文件只有一个名字。
# 2026-09-25 第56步起桥自己读这份 ini（原先靠 GUI 启动时灌），命令行
# `python ds_bridge.py` 起的桥必须自己读，否则提醒词 / 限流 / 硬拦线 /
# 停用名单会静默退回出厂值。旧名 ds_gui.ini 已弃用，不再回落 ——
# 回落只会让「改了旧文件却不生效」这种坑重新出现。
INI_FILE = pathlib.Path(__file__).with_name("ds_bridge.ini")


# 2026-09-27 第330步（用户要求「设置台里的设置每次热生效，不是重启」）：
# ini 和分组文件都按 (mtime_ns, size) 热检 —— 每个请求入口扫一眼，
# 变过就重放一遍配置。桥自己从不写这两个文件，所以不会自触发。
_INI_HOT = {"key": None}    # ds_bridge.ini 上次读到的 (mtime_ns, size)
_GRP_HOT = {"key": None}    # _pool_groups.json 上次读到的 (mtime_ns, size)


def _ini_unescape(s):
    r"""QSettings 把非 ASCII 写成 \xHHHH（码点）或 \xHH（字节），两种都还原。

    算法与 dsweb/extra.py::_qt_unescape 同源：转义 -> 字节 -> UTF-8 解码。
    """
    BS = chr(92)
    if not isinstance(s, str) or BS not in s:
        return s
    try:
        raw = bytearray()
        i, n = 0, len(s)
        while i < n:
            c = s[i]
            if c == BS and i + 1 < n:
                d = s[i + 1]
                if d == "x":
                    j, h = i + 2, ""
                    while j < n and len(h) < 4 and s[j] in "0123456789abcdefABCDEF":
                        h += s[j]
                        j += 1
                    if h:
                        cp = int(h, 16)
                        if len(h) <= 2:
                            raw.append(cp & 255)
                        else:
                            raw.extend(chr(cp).encode("utf-8"))
                        i = j
                        continue
                elif d == "n":
                    raw.append(10)
                    i += 2
                    continue
            raw.extend(c.encode("utf-8"))
            i += 1
        return raw.decode("utf-8")
    except Exception:
        return s


def _ini_read():
    """读 ds_bridge.ini，返回 {section: {key: 值}}。读不到就给空表，不抛。"""
    try:
        text = INI_FILE.read_text(encoding="utf-8")
    except OSError:
        return {}
    out, sec = {}, None
    for line in text.splitlines():
        s = line.strip()
        if not s or s[0] in ";#":
            continue
        if s.startswith("[") and s.endswith("]"):
            sec = s[1:-1]
            out.setdefault(sec, {})
            continue
        if sec is None or "=" not in s:
            continue
        k, v = s.split("=", 1)
        out[sec][k.strip()] = _ini_unescape(v)
    return out


def _ini_apply(pool, reset_cursor=True):
    """把 ds_bridge.ini 里 [bridge] 那份配置灌进池子。

    reset_cursor=True（启动路径）时顺便恢复 rr_active；False（热重放）
    时不动游标，免得把正在轮的那一组切回 ini 里的旧位置。
    """
    try:
        _st = INI_FILE.stat()
        _INI_HOT["key"] = (_st.st_mtime_ns, _st.st_size)
    except OSError:
        _INI_HOT["key"] = None
    br = _ini_read().get("bridge", {})

    def f(key, dflt=0.0):
        try:
            return float(br.get(key, dflt))
        except (TypeError, ValueError):
            return dflt

    def s(key, dflt):
        v = br.get(key)
        return dflt if v is None else str(v)

    def ratio(key, dflt):
        try:
            v = float(br.get(key, dflt))
        except (TypeError, ValueError):
            return dflt
        return v if 0.0 < v < 1.0 else dflt

    def csv(raw):
        # QSettings 读到不带引号的逗号值会给 list，字符串也一样认
        if isinstance(raw, (list, tuple)):
            parts = list(raw)
        else:
            parts = str(raw or "").split(",")
        return [p.strip().strip(chr(34)).strip() for p in parts if p and p.strip()]

    # 2026-09-30（用户口径：桥动不动就掉）：send_budget 是「按预算裁尾部」的
    # 目标值，但它跟 _ask() 的硬闸门 HARD_LIMIT_CHARS 是两套东西，谁也不看谁。
    # 实测 ini 里 send_budget_k=1000 -> send_budget=1000000 字，是闸门(100000)
    # 的 10 倍 —— 于是 build_prompt 按 100 万字裁出来的包，到 _ask() 必然被拒。
    # 表现就是「动不动就掉」（其实是拒绝服务）。这里把预算夹到闸门之下：
    # 即使 ini 把 send_budget_k 写错，也裁不到闸门以上去。
    _sb = int(max(0.0, f("send_budget_k", 300.0)) * 1000)
    _lim = int(getattr(Bridge, "HARD_LIMIT_CHARS", 0) or 0)
    if _lim > 0 and _sb > _lim:
        _sb = _lim

    pool.apply(
        min_interval=f("min_interval", 3.0),
        COOLDOWN=f("cooldown", 45.0),
        RATE_BACKOFF=(f("backoff1", 20.0), f("backoff2", 60.0)),
        turn_limit=f("turn_limit", 0.0),
        standing_note=s("standing_note", CHUNK_REMINDER),
        pool_note=s("pool_note", POOL_NOTE_DEFAULT),
        format_note=s("format_note", ""),
        attach_note=s("attach_note", ATTACH_NOTE),
        handoff_note=s("handoff_note", HANDOFF_PROMPT),
        # 2026-10-03：下面这批原来是写死在代码里的提示词，控制台改不到。
        # 用户口径「应该所有的提示词都得在控制台 因为好管理」。
        # 一律「ini 非空优先，回落出厂常量」—— ini 里没这些键时行为与改前一致。
        tool_protocol=s("tool_protocol", TOOL_PROTOCOL),
        checkpoint_instruction=s("checkpoint_instruction", CHECKPOINT_INSTRUCTION),
        salvage_note=s("salvage_note", SALVAGE_NOTE),
        ctx_empty_note=s("ctx_empty_note", CTX_EMPTY_NOTE),
        nudge_body=s("nudge_body", NUDGE_BODY),
        attach_note_no_cp=s("attach_note_no_cp", ATTACH_NOTE_NO_CP),
        attach_note_local=s("attach_note_local", ATTACH_NOTE_LOCAL),
        attach_ctx_lines=s("attach_ctx_lines", ATTACH_CTX_LINES),
        limits={m: int(max(0.0, f("limit_" + m, 0.0)) * 1000) for m in MODELS},
        windows={m: int(max(0.0, f("window_" + m, 0.0)) * 1000) for m in MODELS},
        compact_threshold_ratio=ratio("compact_threshold_ratio", 0.8),
        compact_retain_ratio=ratio("compact_retain_ratio", 0.16),
        send_budget=_sb,
    )
    pool.reload(disabled=csv(br.get("disabled")))
    # 每号自己那份：ini 里是 [bridge] 下的 pace_<slug>\<key>（QSettings 把
    # bridge/pace_x/y 的斜杠折成分组反斜杠），不是一个 [pace_x] 段。
    for slug in list(pool.bridges):
        ov = {}
        for k, v in br.items():
            if not k.startswith("pace_"):
                continue
            head, sep, tail = k[5:].partition(chr(92))
            if not sep:
                head, sep, tail = k[5:].partition("/")
            if sep and head == slug:
                ov[tail] = v
        if not ov:
            continue

        def g(key):
            try:
                return float(ov.get(key, 0.0))
            except (TypeError, ValueError):
                return 0.0

        out = {}
        if g("min_interval"):
            out["min_interval"] = g("min_interval")
        if g("cooldown"):
            out["COOLDOWN"] = g("cooldown")
        if g("backoff1") or g("backoff2"):
            out["RATE_BACKOFF"] = (g("backoff1") or 20.0, g("backoff2") or 60.0)
        if g("limit_k"):
            out["limit_chars"] = int(g("limit_k") * 1000)
        if g("turn_limit"):
            out["turn_limit"] = g("turn_limit")
        if g("search"):
            out["search"] = True
        hn = str(ov.get("handoff_note") or "").strip()
        if hn:
            out["handoff_note"] = hn
        if out:
            pool.set_pace(slug, out)
    # 上次停在哪个号，这次接着轮（只有启动路径恢复游标；热重放不动它，
    # 免得把正在跑的一组切回 ini 里的旧位置）
    if reset_cursor:
        try:
            want = str(br.get("rr_active") or "").strip()
            if want:
                pool.set_cursor(want)
        except Exception:
            pass


def _ini_auto_resync(pool):
    """每个请求入口扫一眼：ds_bridge.ini / 分组文件变过就热重放配置。

    一次 stat（mtime_ns + size）的代价，变了才真重放。这样设置台
    存盘后，下一发请求就吃到新值 —— 不用重启桥，也不用设置台通知。
    重放不动轮询游标（reset_cursor=False），免得把正在跑的那一组
    切回 ini 里的旧位置。整段包 BaseException：热重放出问题绝不能
    把这一发请求带崩。
    """
    try:
        st = INI_FILE.stat()
        ikey = (st.st_mtime_ns, st.st_size)
    except OSError:
        ikey = None
    try:
        st = GROUPS_FILE.stat()
        gkey = (st.st_mtime_ns, st.st_size)
    except OSError:
        gkey = None
    ini_changed = ikey != _INI_HOT.get("key")
    grp_changed = gkey != _GRP_HOT.get("key")
    if not ini_changed and not grp_changed:
        return
    try:
        _INI_HOT["key"] = ikey
        _GRP_HOT["key"] = gkey
        if ini_changed:
            _ini_apply(pool, reset_cursor=False)
        if grp_changed:
            # 组表本身 _groups_root() 已热读；组变了要重贴的是
            # 「这条提醒归 pool_note 还是 standing_note」—— 即 restamp。
            with pool.lock:
                for _b in list(pool.bridges.values()):
                    pool._stamp(_b)
            pool._grp_sync()
            # 2026-09-27 第379步（用户口径）：「可以在切组动手脚，新建分组的
            # 时候把配置补上」。组表一变，dsh 那份 patch 也得跟着变 —— 新建、
            # 删除、改名一个组，dsh 选择器里的「分组·<名>」条目要立刻出现或
            # 消失。挂在这里是因为它已经是「每发请求入口的热检点」，而且这
            # 一句在 pool.lock 之外（sync 内部自己要拿锁，同锁会死锁）。
            # 挂在组表变更支而不是 switch_save 里，是为了把设置台改盘那条路
            # 也一起覆盖：那条路根本不经过桥的切组对话。
            pool.sync_dsh_patch_now()
            # 2026-10-02（用户口径「还有新建分组 切换分组的时候」）：
            # 建组/切组时也把自动压缩确认一遍 —— 换组可能换到另一个 preset，
            # 那个 preset 里的 auto 也得是关的。幂等，已关就什么都不写。
            try:
                ensure_auto_compact_off()
            except BaseException:                # noqa: BLE001
                pass
    except BaseException:
        pass


from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import ds_api




# ===== 本机根：所有落盘路径的唯一来源（2026-10-01 第473步）=====
#
# 用户口径：「换电脑还会出现同样的问题吗」+「赶紧的别重复做事就行」。
#
# 病根：全文件 17 处直接写死 C:/Users/Lenovo/Desktop/teste/...，5 处写死
# dsh-preview/...。换台电脑（用户名不是 Lenovo、或盘符不是 C）这些全指空 ——
# 而且是**静默**的：写失败被 except OSError 吞掉，桥照跑，只是没记录、
# 没台账、认亲表全空。表现跟今天修好的那些 bug 一样，很难查。
#
# 两条来源，跟 _work/README.md 已确立的约定一致：
#   WORK_ROOT —— 数据根 = ds_bridge.ini 的 workdir（换电脑改一行就行）
#   PREV_ROOT —— 程序根 = 桥所在目录的上一级（__file__ 定位，
#                跟用户名/盘符无关 —— 桥在 <PREV>/ds/ds_bridge.py）
#
# 必须放文件最前面：下面几百行处 pathlib.Path(...) 常量就要求值了，
# 而原来读 ini 的 _ini_read() 在 94 行、_own_root() 更在 4079 行 ——
# 常量在它们之前求值，够不着。所以这里先做一份最小实现。
_SELF = pathlib.Path(__file__).resolve()
PREV_ROOT = _SELF.parent.parent          # <...>/dsh-preview


def _boot_ini_value(key):
    # 从 ds_bridge.ini 读一个键（最小实现，供根解析用）。
    try:
        _f = _SELF.with_name("ds_bridge.ini")
        for ln in _f.read_text(encoding="utf-8",
                              errors="replace").splitlines():
            if "=" not in ln:
                continue
            k, v = ln.split("=", 1)
            if k.strip() == key:
                return v.strip().strip(chr(34))
    except OSError:
        pass
    return ""


def _boot_root():
    # 数据根。ini 的 workdir 优先；读不到就在程序根旁边找 teste/。
    w = _boot_ini_value("workdir")
    if w:
        p2 = pathlib.Path(w.replace(chr(92), "/"))
        if p2.is_dir():
            return p2
    for cand in (PREV_ROOT / "teste", PREV_ROOT):
        if cand.is_dir():
            return cand
    return _SELF.parent


WORK_ROOT = _boot_root()

# 2026-10-02（用户口径「把分组信息直接放到 ds 目录下，下次删掉工作区也不爱是」）：
# **分组配置的落点从工作区根搬到 ds/，跟 ds_bridge.ini 同寿命。**
# 为什么：分组、组窗口、组根这三份是**池子策略**，不是工作区数据。
# 挂在工作区下时，删一次工作区/换一次电脑，分组就跟着没了 ——
# 而它们本来该跟 ini 一样「配一次，一直在」。
_CONF_DIR = INI_FILE.parent          # <...>/ds/


def _conf_file(name):
    """分组类配置的落点：ds/ 下。

    老位置（工作区根）有、新位置没有时，**自动搬一次** ——
    现有的分组不该因为这次搬家而重建。搬失败就退回新路径，不抛。
    """
    new = _CONF_DIR / name
    old = WORK_ROOT / name
    try:
        if not new.exists() and old.is_file():
            new.parent.mkdir(parents=True, exist_ok=True)
            new.write_bytes(old.read_bytes())
    except OSError:
        pass
    return new


# ===== 事件门：池子一动就落一行 =====
# 池子的每个判断点在 ds_bridge 里都是先知道的 —— dsh 的每一发请求都从
# do_POST 的 resolve() 过，那是唯一入口。与其让守护每隔几秒去读日志猜，
# 不如在条件成立的那一刻就把事件写下来：零延迟，且不依赖任何轮询进程活着。
# JSON Lines，一行一件事，追加写；写失败绝不冒泡。
EVENT_FILE = _conf_file("_relay_events.jsonl")   # 落 ds/ —— 见 MARKS_FILE 注
EVENT_MAX = 8 * 1024 * 1024

# 2026-09-22 定（按实测数据，不是拍脑袋）：
# 用 ds_api 直连量过上游真实的吐出节奏 ——
#   首片 1.44s；片间隔中位 0.000s、最大 0.167s；
#   5.7 秒吐 1128 片（198 片/秒），实际 1896 字（334 字/秒），每片只有 1-4 个字。
# 手机 App 收到的是同一个流，它靠在渲染端攒帧、按屏幕刷新率刷屏，
# 并不会把 1128 片各自当成一次网络事件。
# 所以基准就是「一帧 ≈ 一次屏幕刷新」：16ms。
# 攒够 STREAM_CHUNK_SEC 秒就发一帧；STREAM_CHUNK_CHARS 只是兜底上限，
# 防止那种一次给几百字的碎片把单帧撑爆。
# 正文流式的「安全边界」：正文碎片里一旦出现这些开头，就停止外吐，
# 留到最后交给解析器判定 —— 否则可能把半截工具标记当正文发出去（撤不回来）。
# 实测（2742 条思考 + 全部正文）里正文几乎不含这些，所以正常聊天不受影响。
STREAM_HOLD = ("```", "<|", "<｜", "<|D", "```json")
# 末尾保留多少字符不发（够装下一个标记开头），确认安全后再吐。
STREAM_KEEP = 24

STREAM_CHUNK_SEC = 0.0          # 0 = 不节流，上游来一片就发一片（2026-09-22 用户要求：
                                # 先试逐片发送，与「上游本来就是每片 1 字」保持一致）
STREAM_CHUNK_CHARS = 0          # 0 = 不看字数上限
_HDR_SEEN = set()   # [临时探针] 记录 dsh 请求头，判身份用
HDR_PROBE = WORK_ROOT / "_hdr_probe.log"
BODY_PROBE = _conf_file("_body_probe.log")   # [临时探针] body 字段，判 sessionId/purpose/user 是否进 body
# 2026-10-03：**工具名探针** —— 把桥每轮收到的工具表名字落盘（去重，只写名字）。
# 用途：回答「tools=51 了，为什么 has_tool('get_range_context_compact') 还是 False」。
TOOLNAMES_PROBE = _conf_file("_toolnames.log")
_TOOLNAMES_SEEN = set()
# 2026-10-03：曾在此加过 OUT_PROBE（出口帧探针），用来查「下游窗口看不到消息」。
# 它抓到了决定性证据（usage 帧 prompt_tokens=4285316 > contextWindow=131072），
# 结论落定后已撤除 —— 那探针每帧写一次盘，46 MB / 数小时。
_ev_lock = threading.Lock()
_ev_seq = [0]


# ===== 回空策略：外置文件，随时能改，改完下一次空值就生效 =====
# 为什么是文件而不是写死：空值的处置会随账号、时段、上游状态反复调，
# 写死一次就要改代码、重启桥（重启会切断正在跑的模型通路）。
# 读法：每次用到先 stat() 一次，mtime/size 没变就用缓存 —— 一次系统调用，
# 不读盘。文件坏了、键写错了、类型不对，一律退回出厂默认，绝不抛。

# 2026-10-02（同分组）：**空回复策略也搬到 ds/。**
# 它跟分组、组根一样是**池子策略**（这一发太大要不要保会话、会话废了怎么报），
# 不是工作区数据。挂在工作区下删一次工作区就没了，而它本该跟 ini 同寿命。
POLICY_FILE = _conf_file("_empty_policy.json")
POLICY_DEFAULTS = {
    "enabled": True,
    "keep_chars": 100000,
    "keep_images": True,
    "keep_max_times": 1,
    "cooldown": [15.0, 30.0, 60.0, 120.0, 180.0],
    "yield_window": 90.0,
    "drop_after_streak": 0,
    "no_think_retry": True,
    "no_call_retry": True,     # 有正文但没有工具调用时，也贴提醒重发一次
    "txt_attach": True,        # 超长时把被裁掉的老历史转成 txt 附件（实测整份进上下文）
    "txt_attach_chars": 80000,   # 客户端全文超过这么多字才走附件
    "txt_attach_keep_chars": 30000,  # 走附件时 inline 只保留最近这么多字的消息
    "checkpoint_on_switch": True,
    "checkpoint_min_chars": 80000,
    "empty_yield": True,
    # 2026-10-03（用户口径「配置文件只能放 ds 目录中」+ 今晚那次静默退化）：
    # **默认值改用实测调好的数，不再依赖配置文件存在。**
    # 原来默认是 150000 / "ai" —— 而 _empty_policy.json 一丢（删工作区/换机），
    # 桥就静默回落到那两个旧值，行为跟用户调过的不同，且没有任何提示。
    # 现在默认 = 用户的取值，文件**在不在都一样**；文件只用于临时覆盖。
    "handoff_mode": "local",   # 不问老号要交接，本地代写（原来默认 "ai"）
    "delta_context": True,     # 换号只发增量（原来没这一项，默认 False）
    # 2026-10-01 第436步（用户设计）：**固定组窗口**。
    # 一个组在各号上各有一个同名窗口（如「剪辑」），走到哪个号就续哪个号的它。
    # 缓存挂在 sid 上且要几轮才建起来（实测 A1/A2 cached=0，A3=58.8%），
    # 每换一个窗口就重新烧两轮。固定一个就不再重建。
    #
    # 2026-10-03（用户口径「最主要的就不需要有开关，把这个开关功能去掉，
    # 也不需要配置文件，本来就是这套流程」）：**原来的 fixed_group_window
    # 开关已删除，这段逻辑改为无条件执行。**
    # 删掉的理由见 plan() 里那段注释：默认为假的开关会把整套窗口身份机制
    # 悄悄关掉，而文件一丢（删工作区/换机）桥就回落，且没有任何提示。
    # 这里保留一个说明性的占位键，读老配置时不至于当成未知字段报错。
}
_pol_lock = threading.Lock()
_pol_cache = {"key": None, "root": dict(POLICY_DEFAULTS)}


def _policy_root():
    """整份策略。文件变了才重读。"""
    try:
        st = POLICY_FILE.stat()
        key = (st.st_mtime_ns, st.st_size)
    except OSError:
        key = None
    with _pol_lock:
        if key is not None and key == _pol_cache["key"]:
            return _pol_cache["root"]
        data = dict(POLICY_DEFAULTS)
        if key is not None:
            try:
                raw = json.loads(POLICY_FILE.read_text(encoding="utf-8"))
                if isinstance(raw, dict):
                    for k in POLICY_DEFAULTS:
                        if k in raw:
                            data[k] = raw[k]
                    cd = raw.get("cooldown")
                    if isinstance(cd, list) and cd:
                        data["cooldown"] = [float(x) for x in cd]
                    acc = raw.get("accounts")
                    data["accounts"] = acc if isinstance(acc, dict) else {}
            except (OSError, ValueError, TypeError):
                pass
        # 2026-09-30 加（结构性防护，用户口径「为何这个窗口还不能运行」）：
        # **把体积三兄弟的顺序摆正，死区不可能再出现。**
        #
        # 实测的坑：txt_attach_chars(150000) > HARD_LIMIT_CHARS(100000)，
        # 于是客户端 10 万~15 万之间卡死 —— 闸门拒绝，但附件模式要超过 15 万
        # 才触发，整段历史只能内联。2026-09-30 23:33 [483] 就是这么卡住的，
        # 而且改了配置也未必有人记得同时改另一个。
        #
        # 正确次序（必须严格递增）：
        #     闸门(100000)   <=  附件触发(txt_attach_chars)
        #     <=  保留内联(txt_attach_keep_chars 属于附件模式内部，不受此限)
        # 这里只强制前两者：附件触发点**不得高于**闸门，高了就压到闸门之下，
        # 保证「大到会被拒」的一定先走附件。文件里写错也会被这里纠正。
        _gate = int(getattr(Bridge, "HARD_LIMIT_CHARS", 0) or 0)
        if _gate > 0 and bool(data.get("txt_attach", True)):
            try:
                _tc = int(_num(data, "txt_attach_chars",
                               POLICY_DEFAULTS["txt_attach_chars"]))
            except Exception:              # noqa: BLE001
                _tc = int(POLICY_DEFAULTS["txt_attach_chars"])
            if _tc > _gate:
                data["txt_attach_chars"] = _gate
        # 换号检查点同理：它决定「多大的对话在换号时生成检查点」，
        # 高于闸门就永远等不到那一刻。
        try:
            _ck = int(_num(data, "checkpoint_min_chars",
                           POLICY_DEFAULTS["checkpoint_min_chars"]))
        except Exception:                  # noqa: BLE001
            _ck = int(POLICY_DEFAULTS["checkpoint_min_chars"])
        if _gate > 0 and _ck > _gate:
            data["checkpoint_min_chars"] = _gate
        _pol_cache["key"] = key
        _pol_cache["root"] = data
        return data


def _num(d, key, dflt, cast=int):
    """取数值配置：None/缺省/坏值才回默认 —— **0 是合法值，不当缺省**。

    2026-09-24 加：`x.get(k) or DEFAULT` 那个写法会把 0 悄悄吃掉。
    yield_window / checkpoint_min_chars / send_budget / txt_attach_chars
    都栽在这上面（写 0 等于没写）。以后取值一律走这里。
    """
    v = (d or {}).get(key)
    if v is None:
        return dflt
    try:
        return cast(v)
    except (TypeError, ValueError):
        return dflt


def empty_policy(slug=""):
    """这个账号该用哪套。accounts 里写了就盖在全局上面。"""
    data = _policy_root()
    ov = (data.get("accounts") or {}).get(slug) if slug else None
    if isinstance(ov, dict):
        merged = dict(data)
        merged.pop("accounts", None)
        for k, v in ov.items():
            merged[k] = v
        return merged
    return data


# ===== 轮询分组：外置文件，随时能改，改完下一次挑人就生效 =====
# 2026-09-25 加。为什么要分组：有的号适合顶流量、有的留着做备援，还有的
# 要单独伺候某类活。分组让「谁跟谁轮流」变成可配置，而不是只能全体转一圈。
# 跟 _empty_policy.json 一个路子：单独文件、热读、坏了退回默认、绝不抛。
#
# 没有这个文件时 = 一个隐式组「全部账号」= 加分组之前的行为，逐字段一致。
# 分组为什么不写进 ds_auth.json：那是凭据，会被 GUI / ds_token.py 整个
# 重写；分组是池子策略。加个账号不该把分组冲掉。
GROUPS_FILE = _conf_file("_pool_groups.json")
GROUP_DEFAULTS = {"meta_mode": "rotate", "default": "", "groups": []}
# 隐式组 id。显式组的 id 一律不许以下划线开头，把这个名字留出来。
GID_REST = "_rest"
_grp_lock = threading.Lock()
_grp_cache = {"key": None, "root": dict(GROUP_DEFAULTS)}




# ===== 固定组窗口（2026-10-01 第436步，用户设计）=====
# 用户口径：「正常要保证一个分组上边一个窗口 例如剪辑分组 6个号 那必须保证
# 6个号上游都有这个剪辑窗口 而且必须固定窗口名 例如没有剪辑窗口 可以先发个
# 剪辑 把窗口名固定以后再把缓存建立起来 以后就走哪个id就走哪个id的组的窗口
# 除非换组」
#
# 为什么这是对的（两条实测）：
#   1) 缓存挂在 sid 上，且要几轮才建立起来。同一段内容走桥发三次：
#      A1(新窗口) cached=0 / A2(同窗口) cached=0 / A3(同窗口) cached=13270 (58.8%)。
#      换窗口 = 缓存归零，重新烧两轮。
#   2) 同一 sid 持续对话，上游不重放全文。往固定窗口连发 5 轮，
#      上游计数稳定在 ~10100（10138/10132/10101/10122/10117），没有累积增长。
#
# 代价量化（2026-10-01 当天）：剪辑组平均 nc 28~31（窗口浅），
# test 组 74~90（窗口深）—— 窗口越少越深，缓存越暖。
#
# 文件形状（组名 -> 号 -> 上游 sid）：
#   {"剪辑": {"309": "sid...", "779": "sid..."}, "test": {"020": "sid..."}}
# 跟 _pool_groups.json 同目录、同风格：热读、坏了退回空表、绝不抛。
GROUP_SESS_FILE = _conf_file("_group_sessions.json")
_gss_cache = {"key": None, "root": {}}
_gss_lock = threading.Lock()


def _group_sessions():
    """整份「组名 -> 号 -> sid」。文件变了才重读。坏值一律丢掉。"""
    try:
        st = GROUP_SESS_FILE.stat()
        key = (st.st_mtime_ns, st.st_size)
    except OSError:
        key = None
    with _gss_lock:
        if key is not None and key == _gss_cache["key"]:
            return _gss_cache["root"]
        data = {}
        if key is not None:
            try:
                raw = json.loads(GROUP_SESS_FILE.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                raw = None
            if isinstance(raw, dict):
                for gname, per in raw.items():
                    if not isinstance(per, dict):
                        continue
                    g = str(gname or "").strip()
                    if not g:
                        continue
                    row = {}
                    for slug, sid in per.items():
                        s = str(slug or "").strip()
                        v = str(sid or "").strip()
                        if s and v:
                            row[s] = v
                    if row:
                        data[g] = row
        _gss_cache["key"] = key
        _gss_cache["root"] = data
        return data


def group_session_of(group, slug):
    """这个组里的这个号，登记的固定窗口 sid。没有返回空串。"""
    g = str(group or "").strip()
    s = str(slug or "").strip()
    if not g or not s:
        return ""
    return (_group_sessions().get(g) or {}).get(s, "")


def group_session_set(group, slug, sid):
    """登记（或清除）「组+号」的固定窗口。sid 空 = 删除这条登记。

    原子替换：先写同目录临时文件再 replace。桥对这个文件做
    (mtime_ns, size) 热检，半截文件会被当成坏值整份丢掉。
    整个包 BaseException —— 登记失败绝不能把这一轮请求带塌。
    """
    g = str(group or "").strip()
    s = str(slug or "").strip()
    if not g or not s:
        return False
    try:
        try:
            raw = json.loads(GROUP_SESS_FILE.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            raw = None
        if not isinstance(raw, dict):
            raw = {}
        per = raw.get(g)
        if not isinstance(per, dict):
            per = {}
        v = str(sid or "").strip()
        if v:
            per[s] = v
        else:
            per.pop(s, None)
        if per:
            raw[g] = per
        else:
            raw.pop(g, None)
        tmp = GROUP_SESS_FILE.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(raw, ensure_ascii=False, indent=1),
                       encoding="utf-8")
        tmp.replace(GROUP_SESS_FILE)
        return True
    except BaseException:            # noqa: BLE001
        return False


def _notes_of(g):
    """组对象上的 notes 子字典（组专属提醒）。

    2026-09-25 第52步：提醒按组区分。空串一律丢掉 —— 组里留空 = 跟随全局，
    界面上把某一格清空就等于「这组用全局那份」，不用再加开关。
    """
    raw = g.get("notes")
    if not isinstance(raw, dict):
        return {}
    out = {}
    for k in ("standing_note", "pool_note", "format_note",
              "handoff_note", "attach_note"):
        v = str(raw.get(k) or "").strip()
        if v:
            out[k] = v
    return out


def _groups_root():
    """整份分组配置。文件变了才重读。坏值一律丢掉，不留半截。"""
    try:
        st = GROUPS_FILE.stat()
        key = (st.st_mtime_ns, st.st_size)
    except OSError:
        key = None
    with _grp_lock:
        if key is not None and key == _grp_cache["key"]:
            return _grp_cache["root"]
        data = dict(GROUP_DEFAULTS)
        if key is not None:
            try:
                raw = json.loads(GROUPS_FILE.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                raw = None
            if isinstance(raw, dict):
                mm = str(raw.get("meta_mode") or "").strip().lower()
                if mm in ("rotate", "pin", "merge"):
                    data["meta_mode"] = mm
                data["default"] = str(raw.get("default") or "").strip()
                rows, seen = [], set()
                gs = raw.get("groups")
                for g in (gs if isinstance(gs, list) else []):
                    if not isinstance(g, dict):
                        continue
                    # 2026-09-27 第377步（用户口径）：「组id我都说了 是不可控的
                    # 可以删掉了」「生成组的时候就把组名当id用」「我说的是 g1 g2
                    # 在控制台自动生成的那个」。**组名就是主键**，不再有自动编号。
                    # 老文件里残留的 "id" 字段一律忽略；只有 name 整个缺失时才退回
                    # 旧 id —— 不然那份配置会被整条丢掉，账号就凭空少一组。
                    key = str(g.get("name") or g.get("id") or "").strip()
                    # key 要当模型名后缀用：不能空、不能带 @、不能重、
                    # 不能占下划线开头的隐式名字。
                    if not key or "@" in key or key.startswith("_") \
                            or key in seen:
                        continue
                    seen.add(key)
                    slugs = []
                    for s in (g.get("slugs") or []):
                        s = str(s).strip()
                        if s and s not in slugs:
                            slugs.append(s)
                    rows.append({"id": key,
                                 "name": key,
                                 "slugs": slugs,
                                 "turn_limit": _num(g, "turn_limit", 0.0, float),
                                 "enabled": g.get("enabled") is not False,
                                 # 2026-10-01 第454步：**组自己的工作区根。**
                                 # 用户口径「剪辑组 工作区是f盘的a目录 那我所有的
                                 # 数据就在a目录 哪个组呢 剪辑组 那就以剪辑命名目录」。
                                 # 这里必须显式收进白名单 —— 上面这个 rows 是**按字段
                                 # 重建**的（不是原样透传），漏一个字段就等于把它删了。
                                 # 实测踩到：配置里写了 root=F:/a，group_root_of()
                                 # 却读不到，因为 _groups_root() 重建时把它丢了，
                                 # 于是静默回退到桥的 workdir（看着像"没生效"）。
                                 "root": str(g.get("root") or "").strip(),
                                 "notes": _notes_of(g)})
                data["groups"] = rows
        _grp_cache["key"] = key
        _grp_cache["root"] = data
        return data


def pool_groups(slugs=()):
    """算出这一刻实际生效的组表：**只有自定义组**，没有自动分组。

    返回 [{"id", "name", "slugs", "turn_limit", "enabled", "implicit"}, ...]，
    顺序就是配置里写的次序。implicit 一律 False，保留这个键只是让
    老界面不用改。

    规矩（2026-09-25 改）：
      * 一个账号只归第一个提到它的组 —— 后写的组不能把人抢走；
      * 没被任何组提到的账号**不进任何组**，因此完全不参与轮询；
      * 一个显式组都没有（文件没配 / 被删 / 组全停用）-> 返回空表，
        调用方据此判定「没号可轮」。这是用户明确要的：没配组就没号可用。

    slugs 传进来的是池子里现有的账号（顺序 = ds_auth.json 次序）。
    """
    cfg = _groups_root()
    have = [s for s in slugs]
    # 池子还空着（__init__ 里 self.bridges 尚未建）时不给任何组。
    # 早先这里会兜出一个 slugs=[] 的 _rest，_grp_sync 就把它当成
    # 「当前组」钉住了 —— 等 reload 灌进真账号，池子居然从剩余组
    # 开跑，主力组一次都轮不到。空池 = 没有组，定组推迟到 reload 之后。
    if not have:
        return []
    rows = []
    for g in cfg.get("groups") or []:
        if not g.get("enabled"):
            continue
        # 2026-10-02 B 方案：去掉跨组抢号。原来 s not in seen 让先写的组独占，
        # 后面的组拿到空表就被丢 —— 实测 test 抢走 020/483 后，测试2 只剩
        # 020(被抢) + 339(不存在) = 空 -> 整组从 /relay 消失、面板不显示。
        # 现在每组各自筛，只在组内去重；同一号出现在多组是允许的。
        members = []
        for s in (g.get("slugs") or []):
            if s in have and s not in members:
                members.append(s)
        if not members:
            continue          # 空组（或全是不存在的号）不参与轮换
        rows.append({"id": g["id"], "name": g["name"], "slugs": members,
                     "turn_limit": float(g.get("turn_limit") or 0.0),
                     "enabled": True, "implicit": False})
    # 2026-09-25 新规矩：**没有自动分组**。没被任何自定义组收进去的
    # 账号完全不参与轮询 —— 不再兜出「未分组 / 全部」这种隐式组，也不再把
    # 整池当一个大组。一个显式组都没有 = 没有号可轮，resolve 直接报错。
    return rows


def group_of(slugs=()):
    """slug → 组 id。给状态展示用；不在任何组里的返回空串。"""
    m = {}
    for g in pool_groups(slugs):
        for s in g["slugs"]:
            m.setdefault(s, g["id"])
    return m


def group_ids():
    """显式组的 id 集合 —— resolve() 拿它分辨「后缀是组还是账号」。"""
    return {g["id"] for g in (_groups_root().get("groups") or [])}


def group_by_ref(ref):
    """把 @后缀 解析成组 id：**先认组名，再认组 id**，认不出返回空串。

    2026-09-27 第375步（用户口径）：「不是有组名吗？如果有组名则按组名，
    而不按 @020 算」。组名是人起的、看得见的名字（test / 测试1），
    以前只有组 id（g1/g2）能当后缀用，组名写进 @ 会被当成账号名 ——
    认不出直接 KeyError，等于「有组名也没法用」。现在组名排在账号前面。

    顺序：组名 -> 组 id。两个都认不出才轮到账号名。
    """
    ref = (ref or "").strip()
    if not ref:
        return ""
    gs = _groups_root().get("groups") or []
    for g in gs:
        if str(g.get("name") or "") == ref:
            return str(g.get("id") or "")
    for g in gs:
        if str(g.get("id") or "") == ref:
            return str(g.get("id") or "")
    return ""


def group_cfg():
    """整份配置的只读快照，给界面看。"""
    cfg = _groups_root()
    return {"meta_mode": cfg.get("meta_mode") or "rotate",
            "default": cfg.get("default") or "",
            "groups": [{"id": g["id"], "name": g["name"],
                        "slugs": list(g["slugs"]),
                        "turn_limit": g["turn_limit"],
                        "enabled": g["enabled"]}
                       for g in (cfg.get("groups") or [])],
            "file": str(GROUPS_FILE)}


def group_window_by_name(ds, gname, note=None):
    """在上游按**窗口名**找这个组窗口，不靠 id、不靠指纹。

    用户口径（2026-10-01 第444步）：
      「上游的id窗口你固定了 例如剪辑组 339 窗口是剪辑
        轻易不要换窗口要写死
        但是窗口可能内部号会变但是窗口名肯定不会变」

    ## 为什么按名字找

    窗口 id 会变（换号、重开），内容指纹会变（压缩、历史重写），
    **只有名字是桥自己钉死的、且存在上游侧**。桥崩了、进程重启了，
    名字还在。按名字找 = 把身份从「内存/本地文件」搬到上游。

    ## 为什么必须挑 title_type == USER

    实测 309 号有 **10 个**叫「剪辑」的窗口，其中 6 个 title_type=USER、
    4 个 SYSTEM（自动标题碰巧命中）。只看名字会挑错，必须认手工改名那个
    —— rename_session 写出来的就是 USER，且 SYSTEM 不会覆盖它。

    ## 为什么取 updated_at 最新

    实测同名 USER 窗口可能不止一个（309 有 6 个，是历史累积）。
    在用的那个一定最近被写过 —— 拿它。四个号实测全部命中：
      309 78bd3229(17:41) / 779 0b54dc56(17:21) /
      113 b2cfd993(17:21) / 310 81203ae1(17:21>16:59)

    返回 sid 字符串；找不到返回空串。任何异常都吞掉 —— 找窗口失败
    绝不能把这一轮带塌，调用方会退回原路径。
    """
    try:
        g = str(gname or "").strip()
        if not g or ds is None:
            return ""
        # 2026-10-01 第444步：count 取 **100**（上游硬上限）。
        # 实测 ds_api 的 count=120 -> biz_code=1 ILLEGAL_COUNT，100 可用；
        # 而 309 号在 count=60 时已经 has_more=True —— 也就是说窗口一多，
        # **桥会看不见自己那个组窗口**，以为没有就新建一个。这正是 309
        # 攒出 10 个同名「剪辑」窗口的机制，也是「窗口名写死」这条方案的
        # 唯一命门：名字锚点查不到，等于不存在。
        rows, _ = ds.list_sessions(count=100)
        cand = [r for r in rows
                if str(r.get("title") or "") == g
                and str(r.get("title_type") or "") == "USER"]
        if not cand:
            return ""
        cand.sort(key=lambda r: float(r.get("updated_at") or 0.0), reverse=True)
        hit = str(cand[0].get("id") or "")
        if note and len(cand) > 1:
            note("  ⇢ 同名 USER 窗口 %d 个，取最近活跃的 %s"
                 % (len(cand), hit[:8]))
        return hit
    except BaseException:            # noqa: BLE001
        return ""


# ===== 组轮次标记：每个 id 一条记号，换号再记一笔，下次从本地推算（2026-10-02）=====
#
# 用户口径（原话，逐字）：
#   「每个id对应窗口起始做个标记 换号做个标记 下次轮到在从本地推算 中间经过了多少历史 在去燥推给他」
#   「不叫全局轮次 应该是针对每个组的轮次」
#   「都是本地数据去校验他最后的上下文到哪里了缺了多少步 而把他补上」
#   「每次缓存他到哪一个数字了 就知道他缺多少了」
#
# ## 为什么必须落在本地
#
# 限流/回空的号**读不到也写不进**（`_just_emptied` 那一路），任何存在上游窗口里的
# 记号都会在「这个号暂时不可用」的那一刻失效。而本地磁盘随时可写、可读、可推算。
# 所以记号只存本地，桥重启也不丢（落 `_marks.json`）。
#
# ## 为什么是**组**轮次不是全局轮次
#
# 各组轮转互相独立（剪辑组 4 个号自己转，test 组 2 个号自己转）。用全局轮次算，
# 剪辑组涨得快的时候会把 test 组的缺口算大一截 —— 那个数字是假的。
# 组轮次 = 这一组从开天辟地到现在，一共流过多少轮。只有**同组**的号才互相看得见，
# 所以只有组内轮次才是可比刻度。`_ledger.json` 本来也是按组存的，口径一致。
#
# ## 为什么不用 mid
#
# 用户裁定：「窗口那个mid是不做数的」。mid 上游每开一个新窗口就从 1 重来，
# 换过窗口的号 mid 会跳回小数字，拿它算缺口会得出负数。本模块**一次都不读 mid**。
#
# ## 记号长什么样
#
#   _marks.json = {组名: {'n': 组轮次, 'acct': {号: {'on':n, 'off':n, 'at':ts}}}}
#
#   n    组轮次，单调递增，只加不减
#   on   这个号**最后一次干活时的组轮次**（每轮都刷新，见下）
#   off  历史字段，留作诊断（不再参与算缺口）
#
# 缺口 = 当前组轮次 - 该号 on。轮到它时把这个区间的轮次捞出来去燥给它。
#
# ## on 为什么每轮都要刷新（实测修出来的，不是设计的）
#
# 第一版把 on 记在「轮到它的那一刻」，off 记在「换号那一刻」。拿剪辑组
# 真实轮转回放（2651 轮）直接暴露了错：缺口中位数 32、99% 的轮次都 >0。
# 原因是有号回空/限流时**同一个号会被连续轮到十几次**（实测 309 连跑 11 轮、
# 4 轮），那期间它自己明明在看历史，off 却停在换号那一刻不动 —— 于是
# 每轮都白算出一段「缺口」，越连跑越大。
#
# 正确语义：缺口 = **我上次干活之后，别人跑了多少轮**。
# 所以 on 就是「我上次那一轮」的组轮次，每轮刷新一次，off 退化成诊断字段。
# 这样同一个号连跑 11 轮，第 2~11 轮的缺口都是 0 —— 它没缺任何东西。
#
# 第一次见到的号（没有 on）：on=当前轮次，缺口 0 —— **不补**。
# 新号没有历史可缺，凭空补一段别人的会污染它。这一条是刻意的。
# 2026-10-03（用户口径「只要桥必须得肯定在 ds 目录下比较放心」）：
# **落点从工作区根搬到 ds/，跟 ds_bridge.ini 同寿命。**
# 病：这三份（marks/ledger/conclusions）是**桥的记忆** —— 组走到第几步、
# 哪个号缺多少轮。挂在工作区下时，删一次工作区就全归零，而且表现是
# 「缺口算成 0、该补的一步都不补」，很难查到是文件没了。
# 跟 _pool_groups.json 那批走同一条路（_conf_file 自带老位置自动搬一次）。
MARKS_FILE = _conf_file("_marks.json")
_marks_lock = threading.Lock()
_marks_cache = {"t": None, "d": None}


def _marks_load():
    """读 _marks.json。坏文件当空——标记丢了只是退化成「不补」，不是崩。"""
    if _marks_cache["d"] is not None:
        return _marks_cache["d"]
    d = {}
    try:
        raw = json.loads(MARKS_FILE.read_text(encoding="utf-8"))
        if isinstance(raw, dict):
            d = raw
    except (OSError, ValueError):
        d = {}
    _marks_cache["d"] = d
    return d


def _marks_save(d):
    """写回。先写临时文件再替换——桥随时可能被杀，半截 json 比没有更坏。"""
    try:
        tmp = MARKS_FILE.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(d, ensure_ascii=False, indent=1),
                       encoding="utf-8")
        tmp.replace(MARKS_FILE)
    except OSError:
        pass
    _marks_cache["d"] = d


def mark_turn(group):
    """组轮次 +1。**这个组的每一轮都走这里**，谁干的活都算。

    返回新的组轮次。组名空的话不记（未分组号不进轮换，没有组轮次可言）。
    """
    g = str(group or "").strip()
    if not g:
        return 0
    with _marks_lock:
        d = _marks_load()
        cell = d.get(g)
        if not isinstance(cell, dict):
            cell = {"n": 0, "acct": {}}
            d[g] = cell
        cell["n"] = int(cell.get("n") or 0) + 1
        if not isinstance(cell.get("acct"), dict):
            cell["acct"] = {}
        _marks_save(d)
        return cell["n"]


def _mark_cell(d, g):
    """取（必要时建）某个组的记性格子。"""
    cell = d.get(g)
    if not isinstance(cell, dict):
        cell = {"n": 0, "acct": {}}
        d[g] = cell
    if not isinstance(cell.get("acct"), dict):
        cell["acct"] = {}
    return cell


def mark_on(group, slug):
    """轮到这个号了，记一笔：**我上次那一轮是第几轮**。

    每轮都要调（不是只在换号时调）。理由见文件头「on 为什么每轮都要刷新」。
    """
    g, s = str(group or "").strip(), str(slug or "").strip()
    if not g or not s:
        return 0
    with _marks_lock:
        d = _marks_load()
        cell = _mark_cell(d, g)
        acct = cell["acct"]
        n = int(cell.get("n") or 0)
        row = acct.get(s)
        if not isinstance(row, dict):
            row = {}
            acct[s] = row
        row["on"] = n
        row["at"] = round(time.time(), 3)
        _marks_save(d)
        return n


def mark_off(group, slug):
    """换号时记一笔诊断：这个号**干到第几轮**交出去的。

    2026-10-02：**不再参与算缺口**（缺口由 on 算，见文件头）。留着是因为
    排查时「谁在几轮交出去的」比 on 更直观，而且它是既有 _marks.json
    里的字段，删了会让老文件读起来像坏了。
    """
    g, s = str(group or "").strip(), str(slug or "").strip()
    if not g or not s:
        return 0
    with _marks_lock:
        d = _marks_load()
        cell = d.get(g)
        if not isinstance(cell, dict):
            return 0
        acct = cell.get("acct")
        if not isinstance(acct, dict):
            return 0
        n = int(cell.get("n") or 0)
        row = acct.get(s)
        if not isinstance(row, dict):
            row = {}
            acct[s] = row
        row["off"] = n
        if "on" not in row:
            row["on"] = 0
        row["at"] = round(time.time(), 3)
        _marks_save(d)
        return n


def _mark_off_of(d, g, s):
    """从已加载的 marks 里取某个号的 off（没有就 0）。给 mark_snap_done 用。"""
    try:
        cell = (d or {}).get(g)
        if not isinstance(cell, dict):
            return 0
        acct = cell.get("acct")
        if not isinstance(acct, dict):
            return 0
        row = acct.get(s)
        if not isinstance(row, dict):
            return 0
        return int(row.get("off") or 0)
    except BaseException:            # noqa: BLE001
        return 0


def mark_snap_done(group, slug):
    """记下：这个号**这一次接手**已经取过快照了。

    用户口径：「如果连续空回复就不用一直快照 就快照一次就行」。

    ## 为什么记 off 而不是 at（第一版记 at，实测每 12 秒发一次）

    第一版记的是 `at`（该号最后活动时刻），判据 `snap_at != at 就取`。
    **实测 20:01:40 / 20:01:52 / 20:02:04 每 12 秒发一次** —— 因为
    `mark_on` 每轮都把 `at` 刷成当前时间，于是下一轮必然 `snap_at != at`，
    永远判「该取」。

    `off` 的语义才是「一次接手」的边界：**它只在换号（这个号交出去）时更新**。
    所以：
        取快照 -> 记 snap_off = 当时的 off
        下次轮到 -> off 没变 = 它还没交出去过 = 同一次接手 -> 跳过
                    off 变了 = 它离开过又被轮回来   -> 才取
    同号连跑 N 轮时 off 一直是旧值 -> 一次都不重复取。
    """
    g, s = str(group or "").strip(), str(slug or "").strip()
    if not g or not s:
        return False
    try:
        with _marks_lock:
            d = _marks_load()
            cell = _mark_cell(d, g)
            acct = cell["acct"]
            row = acct.get(s)
            if not isinstance(row, dict):
                row = {}
                acct[s] = row
            row["snap_off"] = int(_mark_off_of(d, g, s))
            row["snap_at"] = round(time.time(), 3)   # 留个时间戳好排查
            _marks_save(d)
        return True
    except BaseException:            # noqa: BLE001
        return False


def mark_snap_pending(group, slug):
    """这个号该不该取快照。→ (该取吗, at, 说明)。

    用户口径：「快照是按空缺时间 补 只要换号就按下一个号上次结束时间到当前
    时间的快照」「如果连续空回复就不用一直快照 就快照一次就行」。

    判据（用 `off`，不用 `at` —— 见 mark_snap_done 的注释，第一版用 at
    实测每 12 秒发一次）：
        snap_off == 当前 off  -> 同一次接手，已经取过 -> 跳过
        snap_off != 当前 off  -> 它离开过又被轮回来 -> 该取
        从来没有 snap_off     -> 首次 -> 该取

    at = 0 表示桥这边没有这个号的记录（首次接手/新窗口）-> 取全量 from=0。
    """
    g, s = str(group or "").strip(), str(slug or "").strip()
    if not g or not s:
        return False, 0.0, "no slug"
    try:
        at = float(mark_on_time(g, s) or 0.0)
    except BaseException:            # noqa: BLE001
        at = 0.0
    snap_off, cur_off = None, 0
    try:
        d = _marks_load()
        cell = d.get(g)
        if isinstance(cell, dict):
            acct = cell.get("acct")
            if isinstance(acct, dict):
                row = acct.get(s)
                if isinstance(row, dict):
                    snap_off = row.get("snap_off")
        cur_off = _mark_off_of(d, g, s)
    except BaseException:            # noqa: BLE001
        snap_off, cur_off = None, 0
    if snap_off is not None:
        try:
            if int(snap_off) == int(cur_off):
                return False, at, ("off=%d 已经为这一次接手取过" % cur_off)
        except BaseException:        # noqa: BLE001
            pass
    if at > 0:
        return True, at, "from=%d (off=%d)" % (int(at * 1000), cur_off)
    return True, 0.0, "from=0（首次接手，全量）"


def mark_on_time(group, slug):
    """这个号**上次干活那一刻**的墙上时间（time.time()）。→ float 或 0。

    给快照当区间起点用（`mark_snap_pending` 读它）。

    2026-10-02 补：这个函数在做快照池那一轮加过，但后来从备份恢复整个文件时
    丢了 —— 而 mark_snap_pending 还在调它，于是抛 NameError 被 except 吞掉，
    `at` 恒为 0，日志里表现为「快照：跳过（at=0 已经取过）」：
    0 == 0 被判成「已经取过」，快照永远发不出去。**静默失效**。
    教训：恢复备份之后要按名字逐个核对这轮加过的函数还在不在。

    为什么不用组轮次换算时间：轮次是桥自己数的，没有墙上时间语义；
    而快照是按**墙上时间**取的，工具要的是 Unix 毫秒。两者要对上，
    只能都回到时间轴。

    mark_on 每次轮到该号都刷新 `at` —— 所以读到的是「它最后一次干活」的
    时刻，用这个当 from 正好等于「它离开之后新增的那一段」。
    """
    g, s = str(group or "").strip(), str(slug or "").strip()
    if not g or not s:
        return 0.0
    try:
        d = _marks_load()
        cell = d.get(g)
        if not isinstance(cell, dict):
            return 0.0
        acct = cell.get("acct")
        if not isinstance(acct, dict):
            return 0.0
        row = acct.get(s)
        if not isinstance(row, dict):
            return 0.0
        return float(row.get("at") or 0.0)
    except BaseException:            # noqa: BLE001
        return 0.0


def work_breakpoint_time(group, slug, fallback=0.0):
    """这个号**上一轮结束的时刻** —— 给快照当 from 用。→ float 或 fallback。

    ## 用户口径（逐字）
      「每次把这轮id的会话结束时间 和下一次的起始时间对上就行」

    ## 为什么不用台账（2026-10-03 改）
    原来读 `_steps.md` 最后一行的时刻。而台账是模型手写的 —— 它一停更，
    这个值就冻住：实测读到 `01:39`，而当时已是 14:20，区间 **11 小时**。
    压缩 11 小时 / 一千多条事件必然失败：

        Error: summarization produced no text summary content

    失败没被感知 -> pending_checkpoint 仍空 -> 每 2-5 秒重发同一个请求
    -> dsh 报 `Repeated tool call detected: consecutive_calls: 5, 8`
    -> 彻底卡死。**快照从昨天到今天一次都没真正加上。**

    ## 现在读哪
    桥自己的事件存档 `_relay_replies.jsonl`（本组那份）。每条带 `t`（时间）
    和 `slug`（哪个号），实测 2581 条。取**本号最后一条事件的时刻**。

    实测边界（10-03）：
        483 最后一轮 13:51:04  ->  020 接着 13:51:36
    区间就是几十秒到几分钟，正是「上次结束 -> 现在」这一段。

    取不到就返回 fallback（调用方退回旧口径，行为不劣化）。
    """
    s = str(slug or "").strip()
    if not s:
        return float(fallback or 0.0)
    best = 0.0
    try:
        # 本组存档优先；找不到再退回桥根那份
        paths = []
        try:
            _p = grp_path("replies", s)
            if _p:
                paths.append(_p)
        except BaseException:            # noqa: BLE001
            pass
        paths.append(REPLY_FILE)
        for _p in paths:
            try:
                txt = pathlib.Path(_p).read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            for ln in txt.splitlines():
                if not ln.strip():
                    continue
                # 只做最小解析：不用 json.loads（几万行，全解析太慢）
                if ('"slug": "%s"' % s) not in ln and ('"slug":"%s"' % s) not in ln:
                    continue
                m = re.search(r'"t":\s*([0-9]+(?:\.[0-9]+)?)', ln)
                if not m:
                    continue
                try:
                    t = float(m.group(1))
                except (TypeError, ValueError):
                    continue
                if t > best:
                    best = t
            if best > 0:
                break
    except BaseException:            # noqa: BLE001
        return float(fallback or 0.0)
    if best <= 0:
        return float(fallback or 0.0)
    # 防御：明显在未来（时钟漂移）就退回 fallback
    if best > time.time() + 300:
        return float(fallback or 0.0)
    return best


def mark_gap(group, slug):
    """这个号缺了多少轮。→ (缺口轮数, 上次 off, 当前组轮次)。

    没记号的号返回 0 —— 新号不补。
    """
    g, s = str(group or "").strip(), str(slug or "").strip()
    if not g or not s:
        return 0, 0, 0
    d = _marks_load()
    cell = d.get(g)
    if not isinstance(cell, dict):
        return 0, 0, 0
    acct = cell.get("acct")
    if not isinstance(acct, dict):
        return 0, 0, 0
    row = acct.get(s)
    if not isinstance(row, dict) or "on" not in row:
        return 0, 0, 0
    n = int(cell.get("n") or 0)
    # 缺口 = **别人**在我上次干活之后跑了多少轮。
    #
    # 减 1 的道理：mark_turn 是把组轮次 +1 **之后**才轮到本轮的，而 on
    # 记的是我上一次那一轮的编号。两者相减会把我自己上一次那一轮也算进去
    # （实测恒多出 1，连跑 11 轮的号每轮都报「缺 1 轮」）。那 1 轮是我自己
    # 跑的，不是缺口。所以减掉它。
    on = int(row.get("on") or 0)
    return max(0, n - on - 1), on, n


def _group_slugs(group):
    """这个组有哪些号。取不到就空——上层会退回「只有自己」。"""
    g = str(group or "").strip()
    if not g:
        return []
    try:
        for _gg in (_groups_root().get("groups") or []):
            if str(_gg.get("name") or "") == g:
                return [str(x) for x in (_gg.get("slugs") or [])]
    except BaseException:
        pass
    return []


def marks_slice(group, slug, cap=400):
    """把「这个号缺的那些轮」从本组事件流里捞出来 —— 只有缺的部分。

    按组轮次切片，**不按文件行号**（文件会轮转重命名）、**不按时间戳**
    （实测同秒内多轮，时间戳分不开）。组轮次是唯一可靠的刻度。
    """
    gap, off, n = mark_gap(group, slug)
    if gap <= 0:
        return [], 0, off, n
    want = set(_group_slugs(group)) or None
    try:
        rows = [j for j in grp_read("events", want or ()) if j.get("k") == "turn"]
    except BaseException:
        return [], gap, off, n
    if not rows:
        return [], gap, off, n
    rows.sort(key=lambda t: t.get("t") or 0)
    # 取**最近** gap 轮：从末尾往前数 gap 条就是缺的那一段。
    # 为什么不从 off 的位置往后数：事件文件会轮转，绝对下标不可靠；
    # 而「最近 gap 轮」在任何时候都等于「我上次离开后发生的那 gap 轮」——
    # 只要 gap 是准的。gap 的准头由 mark_off 保证。
    sel = rows[-min(gap, cap):]
    return sel, gap, off, n

# ===== 交接抽取器：把「缺的那几步」变成接手方能直接用的（2026-10-01 第450步）=====
#
# 用户口径：「其实过程不重要 知道结果 及坑 就可以了 还有用的哪些工具」
#           「多维度的去分析」
#
# ## 数据源
#
# _relay_events.jsonl 的 turn 行 —— 九个维度全来自它，字段 100% 齐全。
#
# ## 九维 -> 五层
#
#   1 干活率  tcalls>0 比例      -> 这号在干活还是空转
#   2 时间    ts 排序            -> 谁先谁后
#   3 空回复  fragments          -> 排掉无效轮次
#   4 工具    从 head/tail 抽    -> 这一组用过哪些工具
#   5 产出    think/chars        -> 谁做得多，以谁为准
#   6 重复率  head 前 120 字     -> 同一件事发多次，去重归零
#   7 连续    同 slug 连轴       -> 找该让位的地方
#   8 streak  空回复计数         -> 刚从坑里爬出来，别压重活
#   9 跨号重复 调用签名          -> 核心：别人做过，不用重做
#
# 实测 4559 个 turn：跨号重复 116 种，白跑 870 次工具调用。
# 其中 read 被 6 个号跑了 136 次 —— 六个号反复读同一个文件。

HX_HARD = re.compile(r'(不要|必须|绝对|禁止|不能|别再|只准|一定要|切记)')
HX_PIT = re.compile(r'(因为|所以|导致|发现|其实|注意|教训|踩过|不支持|不存在|'
                    r'拿不到|读不到|无法|失败|报错|超时|冲突|覆盖|丢了|失效)')
HX_BAD = re.compile(r'[{}<>=\[\]]|def |import |await |return ')


def _hx_clean(s):
    s = re.sub(r'```.*?```', ' ', s or '', flags=re.S)
    s = re.sub(r'`[^`]{0,80}`', ' ', s)
    s = re.sub(r'<\|\|[^>]*?\|\|>', ' ', s)
    return s


def _hx_skeleton(s):
    """一句话的**结论骨架**：抓谓词，不抓词。

    为什么不能用关键词（实测踩到）：我先试过"去停用词 + 字符 bigram Top8"，
    结果 200 条句子算出 200 个不同骨架，**一条都没并上** —— 因为
    「磁盘余量」和「盘余量工」是不同的 bigram，而它们说的是同一件事。

    这批句子的共性不是"词相同"，是**谓词相同**：
        磁盘余量：本轮无工具回执，无法实测，给不了
        磁盘余量、文件数量：本轮工具通道无回执，拿不到实测数据，给不了，不猜
        工具通道无回执，磁盘余量、文件数量、网络连接、天气这四项都拿不到
    全是「工具通道无回执 -> 拿不到/给不了 -> 不猜」这一个事实。
    主体（磁盘/文件/网络/天气）是**变量**，谓词才是身份。

    所以骨架 = 命中的谓词位，跟主体无关。
    """
    t = s or ''
    # 谓词位：因 -> 果 -> 态度。命中哪几个就是哪一类。
    slots = []
    if re.search(r'(无回执|没回执|没有给我真回执|通道无回执|无工具回执|'
                 r'未给回执|无真回执)', t):
        slots.append('NOCALLBACK')
    if re.search(r'(拿不到|读不到|取不到|无法实测|实测拿不到|无从统计|给不了)',
                 t):
        slots.append('CANNOT')
    if re.search(r'(不猜|不能猜|不能凭猜测|不编|不能编)', t):
        slots.append('NOGUESS')
    if slots:
        return '+'.join(slots)
    # 没命中谓词位就退回"前 24 字"（这时候逐字去重更准）
    return 'RAW:' + (t.strip()[:24])


def _hx_pick(rows, pat, lo=18, hi=200, cap=14):
    out, seen, skel = [], set(), {}
    for t in rows:
        blob = str(t.get('head') or '') + chr(10) + str(t.get('tail') or '')
        for sent in re.split(r'[\n。；]', _hx_clean(blob)):
            s = sent.strip()
            if len(s) < lo or len(s) > hi:
                continue
            if HX_BAD.search(s) or s.count(chr(34)) + s.count(chr(39)) > 4:
                continue
            if not pat.search(s):
                continue
            k = s[:36]
            if k in seen:
                continue
            # 语义骨架去重：同骨架只留**信息最多**的那条（最长且最具体）
            sk = _hx_skeleton(s)
            if sk and sk in skel:
                old = skel[sk]
                if len(s) > len(old):
                    try:
                        out[out.index(old)] = s
                        skel[sk] = s
                    except ValueError:
                        pass
                continue
            seen.add(k)
            if sk:
                skel[sk] = s
            out.append(s)
            if len(out) >= cap:
                return out
    return out


def _hx_sig(t):
    b = str(t.get('head') or '') + chr(10) + str(t.get('tail') or '')
    m = re.search(r'"name"\s*:\s*"([a-z_]+)"\s*,\s*"arguments"\s*:\s*\{(.*?)\}', b, re.S)
    if m:
        return m.group(1), re.sub(r'\s+', ' ', m.group(2))[:100]
    m2 = re.search(r'invoke name="([a-z_]+)"', b)
    if m2:
        return m2.group(1), re.sub(r'\s+', ' ', b)[:100]
    return None


# ===== 历史坐标核验：交接里的坐标，按本机现状打上「还算数吗」（2026-10-02）=====
#
# 通用问题（用户口径「不要针对性 要通用性」）：
#
#   交接是**历史**。历史里写的是「上一棒在哪台机器上、对着什么干活」。
#   而接手方在**本机**。历史里的坐标可能已经不算数了 —— 路径没了、
#   端口没人听、任务号早收尾、台账文件搬走了。**而交接没告诉它哪些还算数。**
#
#   下一棒分不出来，就只能一条条试：试不通 -> 退回读台账 -> 台账也没有
#   -> 空转。实测剪辑组 09:00~16:00 整整 7 小时就是这么瘫的
#   （重新调查率 8% -> 91%，实际干活 21% -> 0%），
#   而 17:00 之后自动恢复 —— 因为那时任务坐标换成了本机存在的。
#
# ## 为什么不针对具体的东西写判据
#
#   不能写「检测 F 盘」。那只是一个案例：剪辑组的历史来自另一台机器
#   （F:/工具/dsh-preview/chat、F:/test/_work/剪辑）。换台电脑、换盘符、
#   任务迁移，同一个病换张脸又来。判据必须跟**坐标的形态**走，不跟案例走。
#
#   实测本组历史里的坐标形态与失效率（2651 轮）：
#     盘符路径  56% 的轮次提到，其中 60% 本机不存在
#     端口号    11% 的轮次提到，:6100 提到 138 次但本机没在听
#     本地 URL  10%
#     步号/任务号 15% / 8%
#
# ## 做法
#
#   **只标注，不删改。** 历史原样留着（那是事实），在后面附一条本机核验结果。
#   判断「这条还重不重要」交给接手方 —— 它跟我同源，比我的正则准。
#   这是 conc_top 早就定过的口径：
#     「判『这几条说的是不是一回事』靠理解，不是字符串相似度 ——
#       交给接手的模型（同源），比我写的正则准」。
#
#   核验失败一律按「未知」处理，不报成「不存在」——
#   桥自己没权限/盘没插/瞬时失败都会让 exists() 为假，
#   谎报「不存在」比不报更坏（会让接手方放弃一条其实能走的路）。
_COORD_PATH = re.compile(r'[A-Za-z]:[\\/][^\s"\'<>|,;)`\]]{2,90}')
_COORD_URL = re.compile(r'https?://(?:127\.0\.0\.1|localhost):(\d{2,5})[^\s"\'<>|,;)]*')
_COORD_HOSTPORT = re.compile(r'\b(?:127\.0\.0\.1|localhost):(\d{2,5})\b')


def _coord_clean_path(x):
    """把抽出来的路径擦干净：去尾部标点、统一斜杠。"""
    s = str(x or '').replace(chr(92), '/')
    s = s.rstrip('`').rstrip(')').rstrip(']').rstrip('}').rstrip(',')
    s = s.rstrip('.').rstrip('。').rstrip('；').rstrip('，')
    return s.rstrip('/')


def _coord_port_alive(port):
    """本机这个端口有没有人在听。"""
    try:
        s = socket.create_connection(('127.0.0.1', int(port)), timeout=0.35)
        s.close()
        return True
    except BaseException:            # noqa: BLE001
        return False


def recent_intent_note(messages, n=3, cap=4000):
    """取这一发 messages 里**最近几条**原文 —— 还原「现在要什么」。

    用户口径（原话，逐字）：
      「并且附带当前最近没经过快照压缩的详细事件让ai知道该做什么了」
      「你就当你是下一棒你需要知道当前什么状态 他们在做什么」

    2026-10-02 补：这个函数在做快照池那一轮加过，后来从备份恢复整个文件时丢了，
    而 mark_handoff 里 [_intent] 那段还在用它 —— 于是整段交接少了一半。
    （同一次恢复一共丢了三个：mark_on_time / recent_intent_note / 启动挂载点。）

    ## 为什么必须有这一段

    快照是**过去某个时刻压的**，它不知道「现在」被要求干什么。
    实测（剪辑组 08:57 那份 13611 字的八段快照）里，
    `Primary Request and Intent` 那节写的是：
        - 剪辑 thread (closed): remove the watermark ...
    **它自己就标着 (closed)** —— 接手方读它，以为要干的是个已收尾的活。
    而那时真正的新要求还没被压进任何快照。

    ## 只取「要什么」这一路

    实测把 tool 段也照收的话，一条工具结果原文就占满整段 —— 而它是最没信息
    的那类：接手方要的是「你现在被要求干什么」，不是「上一棒跑的命令回显」。
    system 段同理（那是每轮都有的规矩，不用复述）。
    所以只留 user（要求）+ assistant（上一棒的结论/判断）。

    n   ：取最近几条（默认 3）。**注意不是"最后 n 条"** —— 见下面的 2026-10-03 修。
    cap ：总字数上限，超了从**最早**的丢（保最近的）。

    2026-10-03 修（用户口径「我给他们发任务纠正以后下一个 ai 又收不到任务了」
    「智普清言分组现在换 id 交接都不正常了，像在一直循环读取」）：
    **取数从「最后 n 条」改成「从后往前找最近的 user / 有效 assistant」。**

    病（有实证，不是推测）：换号那一刻，最后 4 条几乎**必然是工具结果**。
    从组存档 _relay_msgs.jsonl 里取真实的一发复现（09:30:52，4127 条消息）：

        msgs[-4:] = [assistant(空), tool "(no background jobs)",
                     tool "No files found", tool "No files found"]
        → role 不是 user/assistant 的 3 条被跳过
        → assistant 那条正文为空，也被跳过
        → 返回空串

    后果：mark_handoff 里 `if _intent:` 恒假 → **交接里"现在要做什么"整段消失**，
    只剩「硬约束/坑/已做过」（全是历史）。下一棒拿到一堆历史却不知道目标，
    于是到处 read/glob 找线索 → 2 分钟用满 10 条额度 → 换号 → 下一棒同样困惑
    → **无限循环读取**。模型自己也说了：
        「没有可执行的任务内容——本轮消息只有工具协议和目录约定，
          没有你具体要我做的事」（2026-10-03 09:24:46）

    修法：**从后往前扫全量 messages**，分别收集最近的 user 和有效 assistant，
    各取到 n 条为止。这样不管中间夹了多少工具结果，用户发的任务/纠正都能进交接。
    开头 220 条是系统提示（很大），从后往前扫天然避开它 —— 但仍加一道
    MAX_SCAN 上限，避免超长对话把这一发拖慢。
    """
    MAX_SCAN = 6000         # 从后往前最多扫这么多条
    # 为什么是 6000 而不是 400：实测（智普清言组 09:30 那一发，4159 条消息）
    # 最近一条 user 在**离末尾 807 条**处 —— 400 扫不到，交接里就没有"要做什么"。
    # 用户的原话就是这么被埋掉的：
    #   「现在开始最终任务是把上传文件这个bug给我修复掉 而不是一直理解上下文
    #     你们已经够了解了请直接开始 …把这个任务给我加到台账中」
    # 它上面压着 807 条 tool 结果（末尾 20 条的 role 序列几乎全是 tool）。
    # 代价实测：全扫 4159 条 = 0.17ms（20 次平均），每轮调一次可忽略。
    # 取 6000 是留余量；再长就靠 cap 截，不会让交接无限膨胀。
    try:
        msgs = [m for m in (messages or []) if isinstance(m, dict)]
        if not msgs:
            return ""
        rows = []
        seen_u = seen_a = 0
        for m in reversed(msgs[-MAX_SCAN:]):
            role = str(m.get("role") or "").lower()
            if role == "user":
                if seen_u >= n:
                    continue
            elif role == "assistant":
                if seen_a >= n:
                    continue
            else:
                continue
            txt = _text_of(m.get("content"))
            txt = (txt or "").strip()
            if not txt:
                continue
            # 纯工具调用块的 assistant 消息剥壳后往往是 JSON，没阅读价值。
            if txt.startswith("```json") or txt.startswith('{"tool_calls"'):
                continue
            if role == "user":
                label = "用户"
                seen_u += 1
            else:
                label = "上一棒"
                seen_a += 1
            rows.append((label, txt))
            if seen_u >= n and seen_a >= n:
                break
        if not rows:
            return ""
        rows.reverse()               # 恢复时间顺序（从早到晚）
        keep, total = [], 0
        for label, txt in reversed(rows):
            if keep and total + len(txt) > cap:
                break
            keep.append((label, txt))
            total += len(txt)
        keep.reverse()
        out = ["【现在在做什么 —— 最近 %d 条原文】" % len(keep), ""]
        for label, txt in keep:
            body = txt if len(txt) <= 1200 else (txt[:1200] + " …(截断)")
            out.append("── %s ──" % label)
            out.append(body)
            out.append("")
        out.append("以上是**现在**的要求和最近进展；下面是这段时间的结论。")
        return chr(10).join(out)
    except BaseException:            # noqa: BLE001
        return ""


def coord_check(text, cap=6):
    """把一段文本里的坐标抽出来，按本机现状核验。

    返回一个**可直接贴进 prompt 的短段**；一个坐标都没有时返回空串。
    """
    t = str(text or '')
    if not t:
        return ""
    rows = []
    seen = set()
    # 1) 盘符路径 —— 唯一能硬核验的（存在/不存在）
    n_path = 0
    for raw in _COORD_PATH.findall(t):
        p = _coord_clean_path(raw)
        if not p or p in seen or len(p) < 5:
            continue
        if '$' in p or '{' in p or '*' in p or '?' in p:
            continue                       # 带变量/通配的，核不了，别瞎报
        seen.add(p)
        n_path += 1
        if n_path > cap:
            break
        try:
            ok = pathlib.Path(p).exists()
        except BaseException:            # noqa: BLE001
            ok = None
        rows.append(('path', p, ok))
    # 2) 本地端口 —— 服务和桥都在 127.0.0.1 上，这个核验很硬
    seen_port = set()
    n_port = 0
    for m in _COORD_HOSTPORT.finditer(t):
        p = m.group(1)
        if p in seen_port or len(p) < 3:
            continue
        seen_port.add(p)
        n_port += 1
        if n_port > 4:
            break
        rows.append(('port', '127.0.0.1:' + p, _coord_port_alive(p)))
    if not rows:
        return ""
    lines = []
    for kind, what, ok in rows:
        if ok is True:
            mark = "在  " if kind == 'path' else "在听"
        elif ok is False:
            mark = "没有" if kind == 'path' else "没听"
        else:
            mark = "未知"
        lines.append("  [%s] %s" % (mark, what))
    body = ["〔历史坐标的本机核验 —— 决定哪些还能照着走〕", ""] + lines + ["",
            "上面是桥拿**本机现状**核过的。交接是历史，历史里的坐标",
            "不一定还算数 —— 标「本机没有 / 本机没在听」的别再照着找。",
            "要接着干，按标「本机在 / 本机在听」的那几条走。"]
    return chr(10).join(body)


# 缺口的三种给法用到的阈值（2026-10-02）
#
# HX_GIVE_CHARS：缺口素材小于这个就**原文照给**。按实测，缺口轮数中位 0、
#   P90 是 7 轮、P99 是 36 轮，每轮 turn 约 600 字（head400+tail200），
#   所以 24000 字 ≈ 40 轮，能覆盖 P99 以上。这个体积进 prompt 完全放得下
#   （HARD_LIMIT_CHARS 是 100000）。
# HX_KEEP_ROWS：走指针那条路时，仍**原文附上最近这么多轮** ——
#   接手方要接着往下干，最近几轮是当前处境，必须给；
#   更早的用指针指向存档，他需要时自己读。
HX_GIVE_CHARS = 24000
HX_KEEP_ROWS = 8


def hx_gap_pointer(rows, slug, group):
    """大缺口时给**指针**，不给正文尾巴。

    为什么（用户口径「不压缩怎么传过去 2个号还好点如果n个号体积会很大」，
    同时又「必须把历史补齐」）：

      同一段历史，n 个号轮转时会被**各传一遍** —— 那才是体积的大头
      （实测每号每次补约 907 字，n=20 时单轮合计 11 万字，超上限）。
      而正文本来就一直在本机存档里，谁也不缺。缺的只是**知道去哪读**。

    所以大缺口时只报三件事：
      1. 缺了哪一段（轮数 + 时间区间）
      2. 去哪读（按组的存档路径 —— 必须用 grp_path 取，不能用桥根那份）
      3. 怎么读（按 slug 过滤的检索方法）

    这跟 gap_index_note 是同一个思路（「不传数据、只传指针」），
    这里只是把它的口径搬到「按组轮次算出来的缺口」上。

    **不编数字**：拿不到精确条数就说拿不到。
    gap_index_note 的注释里记着这个教训 —— 原来稳态路径每轮喊一个假缺口
    （「约 30004 字被裁掉」），把窗口推进了「每轮读存档 -> 读不完 ->
    下轮缺口还在」的无限空转。所以这里只报**已知的**：
    轮数（我们自己算的，准）和时间区间（从事件流取的，准）。
    """
    try:
        rows = list(rows or [])
        if not rows:
            return ""
        s = str(slug or "")
        t0 = str(rows[0].get('ts') or "")
        t1 = str(rows[-1].get('ts') or "")
        # 存档路径按组取 —— 给错路径等于让人白翻（第479步踩过）
        try:
            fp = str(grp_path("replies", s) or REPLY_FILE)
        except BaseException:        # noqa: BLE001
            fp = str(REPLY_FILE)
        out = [
            "【缺口的那 %d 轮 —— 正文不在这里，但读得到】" % len(rows),
            "",
            "你缺的是 %s ~ %s 这 %d 轮（本组轮次推出来的，准）。"
            % (t0 or '?', t1 or '?', len(rows)),
            "**正文没有丢** —— 全文一直在本机存档里。要重放当时怎么干的，",
            "按下面去读；不需要就别读（翻存档费 token）。",
            "",
            "  文件：%s" % fp,
            "  检索：按 slug=\"%s\" 过滤；一行一个 JSON，" % s,
            "        字段 ts(时分秒) / slug / text(全文) / head / tail",
        ]
        if t0 and t1:
            out.append("  时间：%s ~ %s" % (t0, t1))
        out += [
            "",
            "最近 %d 轮的正文已直接附在上面，从那里接着干。" % HX_KEEP_ROWS,
        ]
        return chr(10).join(out)
    except BaseException:            # noqa: BLE001
        return ""


def hx_compact_rows(rows, cap=240):
    """把缺口的历史压到能传得动的体积 —— **但只压真重复,不压演进**。

    用户口径：「但是增量肯定要压缩的不压缩怎么传过去 2个号还好点如果n个号
    体积会很大」+「知道交接的时候为什么必须把历史补齐吗？不补齐的话
    如果用户让你在测试历史中的方案 你就不知道无从下手」。

    **两句都要满足**：既压得下去，又一条方案链都不能丢。

    ## 判据：按「动作签名」压，不按「结论骨架」压

    我先前的 hx_dedup_rows 是按结论骨架（_hx_skeleton）压的，实测证伪：
    骨架分不出「同一命令跑 30 遍」和「试了 A 不行、试 B 不行」——
    两者都算 CANNOT+NOGUESS，于是方案演进被压成一条。

    换成**动作签名**（工具名 + 参数原文）后实测（剪辑组 09:00~09:30，231 轮）：
      不同动作 77 种，真正重复执行的只有 5 种（都只 x2）
      压缩率 67%（231 -> 77）
      而演进链的关键步**全部保留**：read ai-task.js / node --check /
      stopSelectors 正则修正 / :6100 v1/state 往返 / PONG 验证

    为什么这个判据对：**「同一个工具、同一份参数」才是真重复** ——
    重跑第二遍不会得到新东西。参数只要变一个字，就是新的一次尝试，
    那是方案的一部分，必须留着。

    ## 怎么压

    同一签名出现 N 次 -> 只留**最早**一条（最早那条参数最全、上下文最完整），
    并在它上面标 `rep=N`。次数本身是信息：
      「这条命令被跑了 30 遍」和「跑过 1 遍」对判断要不要换路子不一样。

    没有签名的轮（纯正文结论）**一律保留** —— 那正是「过程不重要，结果和坑
    才重要」里要的那部分，且它本来就不占体积（正文才几十~几百字）。

    ## cap 兜底

    压完仍超 cap 时，从**最早**的丢（保最近的）。
    为什么不保最早的：接手的号要接着往下干，最近那几轮才是当前处境。
    但这是最后手段 —— 正常压完只有几十条，够得着 cap 的情况很少。
    """
    try:
        rows = list(rows or [])
    except BaseException:            # noqa: BLE001
        return []
    if not rows:
        return []
    out, seen = [], {}
    for r in rows:
        sg = _hx_sig(r)
        if not sg:
            out.append(r)               # 无动作签名 = 正文轮，一律留
            continue
        # 签名 = (工具名, 参数前 100 字)。跟 handoff_extract 里
        # 「已做过」那节用的是同一个判据，口径统一。
        key = (sg[0], sg[1][:100])
        if key in seen:
            seen[key]['n'] += 1
            continue
        cell = {'n': 1}
        seen[key] = cell
        out.append(r)
    # 把重复次数标回那条留下的轮子上（不改正文，加一个字段）
    for r in out:
        sg = _hx_sig(r)
        if not sg:
            continue
        cell = seen.get((sg[0], sg[1][:100]))
        if cell and cell['n'] > 1:
            try:
                r['_rep'] = cell['n']
            except BaseException:        # noqa: BLE001
                pass
    if len(out) > cap:
        out = out[-cap:]                # 兜底：超了从最早丢，保最近
    return out

# 2026-10-02 删：hx_dedup_rows / hx_new_rows 两个按结论骨架压的已移除。
#
# 它们做的事是「把交接压小」：前者按结论骨架把同类压成一条，
# 后者把「以前给过这个号」的结论丢掉。两个都被实测否掉了 ——
#
#   用户口径：「知道交接的时候为什么必须把历史补齐吗？不补齐的话
#   如果用户让你在测试历史中的方案 你就不知道无从下手」
#
# 剪辑组 09:00~09:24 那 45 轮里**没有一条是重复**，是一条完整的方案链：
#   定位(读 ai-task.js:484 / node --check) -> 找到根因(正则字符类越界)
#   -> 修掉(376-379 行) -> 打通(:6100 往返) -> 验证(时区/JSON) -> 交付
# 每一步的工具、参数、结论都不同，而两个压缩函数**专杀这种链子**：
# 一个压掉分叉与演进，一个让历史越接越薄。
#
# 现在交接**原文照给**，缺多少轮给多少轮。体积由 marks_slice 的 cap 兜底
# （只裁最远的，保最近的），不按「内容像不像」压 ——
# 那等于替接手方判断哪段历史没用，而它要测的恰恰可能是我认为没用的那段。

def mark_handoff(slug, bridge=None):
    """标记法交接：这个号缺的那几轮 -> 去燥后的交接正文。**每轮都该调**。

    用户口径（原话）：「每个id对应窗口起始做个标记 换号做个标记 下次轮到
    在从本地推算 中间经过了多少历史 在去燥推给他」。

    返回空串 = 这个号没缺任何东西，不用给交接。**空串是正常结果**，
    不是失败 —— 连续干活的号本来就不缺。

    ## 为什么单独抽成函数

    原来这段代码长在 `if _my in swap_pending` 里面，于是**只有换号那一轮**
    才算交接。实测剪辑组 2651 轮里只有 317 次换号（平均 8.4 轮才换一次），
    剩下 68% 的轮次根本不进那段 —— 这就是「每轮都在缺步」的直接原因。
    标记法要成立，就必须**每轮都算**：缺口是轮次差算出来的，跟换不换号无关。
    """
    s = str(slug or "").strip()
    if not s:
        return ""
    try:
        if not bool(empty_policy(s).get("handoff_extract", True)):
            return ""
    except BaseException:            # noqa: BLE001
        pass
    try:
        g = group_name_of(s)
        # **先算缺口，再刷 on。** 顺序就是标记法成立的前提：on 是「我上次
        # 那一轮是几号」，先写后读的话读到的是刚刷新的自己，缺口恒 0。
        rows, gap, on_, n_ = marks_slice(g, s)
        mark_on(g, s)
        if not rows:
            return ""
        # ===== 缺口的三种给法（2026-10-02）=====
        #
        # 用户口径两句话都要满足，它们看着矛盾，其实分工不同：
        #   「知道交接的时候为什么必须把历史补齐吗？不补齐的话 如果用户让你
        #    在测试历史中的方案 你就不知道无从下手」  -> 历史不能丢
        #   「但是增量肯定要压缩的不压缩怎么传过去 2个号还好点如果n个号
        #    体积会很大」                            -> 体积必须控
        #
        # 同时满足的办法不是「压得更狠」，而是**按体积分三档**：
        #
        #   1. 缺口小（<= HX_GIVE_CHARS）  -> 原文照给。
        #      这一段就是接手方要重放的东西，一个字都不动。
        #   2. 缺口大                      -> 给**指针**，不给正文尾巴。
        #      正文一直在本机存档（_relay_replies.jsonl）里，接手方读得到。
        #      这也是解决 n 个号体积的正路：同一段历史不给 n 个号各传一遍。
        #      机制复用现成的 gap_index_note 思路（「不传数据、只传指针」）。
        #   3. 同一动作重复执行            -> 留一条 + 次数（hx_compact_rows）。
        #      实测只压 4%（真重复本来就少），所以它**不是主力**，
        #      只在第 1 档里顺手做一下。
        #
        # 为什么 2 不能像旧代码那样「只留尾巴」：旧路径 _tail_items 只取尾部，
        # 丢的是**中间**那段（注释自己记着「一次换号丢 39%~67%」），
        # 而方案链的中段恰恰在那儿。丢了还**不告诉接手方丢了**，
        # 他只能凭猜补 —— 「越补越偏」就是这么来的。
        # 给指针就不会：他知道缺哪一段、去哪读、怎么读。
        _raw = len(rows)
        _chars = sum(len(str(r.get('head') or '')) +
                     len(str(r.get('tail') or '')) for r in rows)
        _ptr = ""
        if _chars > HX_GIVE_CHARS:
            # 大缺口：正文换指针
            try:
                _ptr = hx_gap_pointer(rows, s, g)
            except BaseException:        # noqa: BLE001
                _ptr = ""
            rows = rows[-HX_KEEP_ROWS:]
        else:
            rows = hx_compact_rows(rows)
        mem = _group_slugs(g) or [s]
        hx = handoff_extract(mem, rows=rows)
        if _ptr:
            hx = (hx + chr(10) + chr(10) + _ptr) if hx else _ptr
        # 2026-10-02（用户口径「并且附带当前最近没经过快照压缩的详细事件让ai
        # 知道该做什么了」）：**把「现在在做什么」排在最前。**
        #
        # 快照是**过去某个时刻压的**，它不知道「现在」被要求干什么 ——
        # 实测那份八段快照里 Primary Request and Intent 自己标着 (closed)，
        # 接手方读了以为要干的是个已收尾的活。所以这一段必须排第一：
        # 先知道要什么，再读进展，否则读起来是断的。
        _intent = ""
        try:
            _intent = recent_intent_note(messages)
        except BaseException:        # noqa: BLE001
            _intent = ""
        if _intent:
            hx = (_intent + chr(10) + chr(10) + hx) if hx else _intent
        _ded = len(rows)
        # 2026-10-02：交接是历史，历史里的坐标可能在本机已经不成立。
        # 附一段本机核验，让接手方一眼看出哪些还能照着走 —— 见 coord_check
        # 的注释（剪辑组 09:00~16:00 瘫 7 小时，就是因为分不出这个）。
        if hx:
            try:
                _ck = coord_check(hx)
                if _ck:
                    hx = hx + chr(10) + chr(10) + _ck
            except BaseException:        # noqa: BLE001
                pass
        if bridge is not None:
            try:
                bridge.note("  交接：标记法补 %d 轮缺口（on=%s n=%s），"
                            "素材 %d 条 -> 压缩 %d 条"
                            % (gap, on_, n_, _raw, _ded))
            except BaseException:    # noqa: BLE001
                pass
        return hx or ""
    except BaseException:            # noqa: BLE001
        return ""

# 2026-10-03：交接/压缩前的事件清洗。
#
# 用户口径（逐字）：
#   「过滤根本用不到插件 桥就能过滤 我都说了 后期换电脑换版本你这样搞
#     没办法适配」-> 所以写在桥里，不写插件。
#   「记住保留最近几条别过滤 那样能让ai更快适应」-> 最近 N 条一个字不动。
#
# 为什么必须清（实测 1158 条事件 / 648223 字）：
#   · 纯工具调用 JSON 589 条 (51%) —— 光秃秃的 {"tool_calls":...}，
#     没有工作内容，只稀释压缩引擎的注意力
#   · 历次压缩产物 —— 它们的错误结论会被下一轮继承并强化，
#     实测快照永远说「no task instruction has been issued」，
#     模型读完不知道干什么，只能去 glob/read 全盘扫描
#   清完 648223 -> 132862 字（省 80%），留下的正是工作结论：
#     「当前状态已清楚：删除还没测。先读 CRUD 脚本，再补测删除端点。」
HX_RECENT_KEEP = 30          # 最近这么多条事件原样保留，不过滤

# 压缩产物的开头特征（本项目历次快照/检查点）
_HX_CP_HEADS = ("## Primary Request and Intent", "## Progress Cursor")


def strip_tool_calls(text):
    """剥掉正文里内嵌的工具调用 JSON 块，保留「人的话」。

    实测（原长 -> 剥后）：
        3472 -> 47   「两个脚本已读，签名封装可复用。现在补测"删除对话"…」
        3188 -> 59   「列表响应被截断（只读 2500 字节导致 JSON 不完整）…修脚本」
    剥掉的是机器的话，留下的是**结论与下一步**。
    """
    t = str(text or "")
    # ```json ... ``` 围栏块（工具调用都是这个形状）
    t = re.sub(r"```(?:json)?\s*\{[\s\S]*?\}\s*```", " ", t)
    # 尾部无围栏的裸 JSON
    t = re.sub(r"\{\s*" + chr(34) + r"tool_calls" + chr(34) + r"[\s\S]*$", " ", t)
    # 落单的围栏开头
    t = re.sub(r"```(?:json)?\s*$", " ", t, flags=re.M)
    return " ".join(t.split()).strip()


def is_machine_noise(text):
    """这条事件是不是纯机器噪音（该整条丢掉）。

    判据两条，都要从严：
      · 历次压缩产物（开头就是 8 段式标题 / 压缩指令）
      · 剥掉工具调用后不足 20 字（说明没有正文）
        阈值 20 而不是 40：中文一字一字符，密度远高于英文。
        「当前状态已清楚：删除还没测。」= 14 字，信息量足够，不能被误杀。
    """
    t = str(text or "").strip()
    if not t:
        return True
    for h in _HX_CP_HEADS:
        if t.startswith(h):
            return True
    head = t[:400]
    if "Act as a compaction engine" in head or "Condense the conversation ABOVE" in head:
        return True
    return len(strip_tool_calls(t)) < 20


def clean_event_rows(rows, keep_recent=HX_RECENT_KEEP):
    """事件清洗：最近 keep_recent 条原样留，更早的剥机器噪音。

    行结构是 dict，正文在 'text'（有的在 'tail'）。**只动正文，不动其它字段**
    —— 时间、slug、k 这些下游还要用（work_breakpoint_time 就靠 t/slug）。

    返回清洗后的新列表（不改原对象，避免影响调用方别处）。
    """
    try:
        n = len(rows)
        if n == 0:
            return rows
        cut = max(0, n - int(keep_recent or 0))
        out = []
        for i, r in enumerate(rows):
            if not isinstance(r, dict):
                continue
            if i >= cut:
                # 最近几条：一个字不动
                out.append(r)
                continue
            body = r.get("text")
            if body is None:
                body = r.get("tail")
            if body is None:
                out.append(r)
                continue
            if is_machine_noise(body):
                continue                      # 整条丢
            cleaned = strip_tool_calls(body)
            if not cleaned:
                continue
            nr = dict(r)
            if r.get("text") is not None:
                nr["text"] = cleaned
            else:
                nr["tail"] = cleaned
            out.append(nr)
        return out or rows
    except BaseException:            # noqa: BLE001
        return rows                   # 清洗出问题绝不拖垮交接


def handoff_extract(slugs=(), limit=1200, rows=None):
    """把「缺的那几步」变成接手方能直接用的五节交接。

    2026-10-02：新增 rows 参数 —— 传进来就直接用这份素材，不再自己按
    slugs 去捞最近 limit 条。**这是「标记法」的落点**：调用方先用
    marks_slice() 算出「这个号缺的那几轮」，把那段塞进来，抽取器就只
    分析缺的那段，而不是一律分析最近 1200 行（那会把该号已经看过的
    历史又算一遍，也是交接体积忽大忽小的来源）。

    不传 rows 时行为跟以前完全一样：自己捞最近 limit 条。
    """
    want = set(str(s) for s in (slugs or ()) if s)
    if not want:
        return ''
    try:
        # 2026-10-01 第458步：**桥自己判断读哪份事件流。**
        # 用户口径「后期要桥能自动区分」。grp_read 按 slugs 推出组，
        # 读那个组目录下的事件；没搬过的组自动回落桥根那份。
        if rows is None:
            rows = [j for j in grp_read('events', want) if j.get('k') == 'turn']
            if not rows:
                return ''
            rows.sort(key=lambda t: t.get('t') or 0)
            rows = rows[-limit:]
        else:
            rows = [r for r in rows if r.get('k') == 'turn']
            if not rows:
                return ''
            rows.sort(key=lambda t: t.get('t') or 0)
        # 2026-10-03：清洗 —— 最近 HX_RECENT_KEEP 条原样留，更早的剥掉
        # 工具调用 JSON 与历次压缩产物。理由见 clean_event_rows 的注释。
        # 放在 hard/pit 抽取**之前**：那些抽取也吃 rows，不清就会被噪声带偏。
        rows = clean_event_rows(rows)
        hard = _hx_pick(rows, HX_HARD)
        pit = _hx_pick(rows, HX_PIT)
        # 2026-10-01 第465步（用户口径「必须要完美还能优化」）：
        # **《坑》和《硬约束》也要滤环境噪声。**
        # 实测「坑」14 行里有 17 处是「工具通道无回执/拿不到」的复述 ——
        # 同一件事连说十几轮，把真正要拦的坑挤没了。
        # 判据跟 conc_top 共用 CONC_NOISE：讲通道/回执+拿不到 = 环境噪声。
        hard = [s for s in hard if not CONC_NOISE.search(str(s))]
        pit = [s for s in pit if not CONC_NOISE.search(str(s))]
        # 同一件事去重：纯机械的（逐字相同 / 前 40 字相同），语义仍交给接手方
        _ded = []
        _dk = set()
        for s in pit:
            _k = _hx_skeleton(str(s)) or str(s)[:40]
            if _k in _dk:
                continue
            _dk.add(_k)
            _ded.append(s)
        pit = _ded
        calls = {}
        for t in rows:
            sg = _hx_sig(t)
            if not sg:
                continue
            k = (sg[0], sg[1][:100])
            c = calls.setdefault(k, {'n': 0, 'slugs': set(), 'ts': []})
            c['n'] += 1
            c['slugs'].add(str(t.get('slug') or ''))
            c['ts'].append(t.get('ts') or '')
        shared = [(k, v) for k, v in calls.items() if len(v['slugs']) > 1]
        shared.sort(key=lambda kv: -kv[1]['n'])
        by = {}
        for t in rows:
            s = str(t.get('slug') or '')
            d = by.setdefault(s, {'n': 0, 'tool': 0})
            d['n'] += 1
            if t.get('tcalls'):
                d['tool'] += 1
        out = ['【本组交接 —— 本地台账算出，不是谁复述的】']
        # ── 第一件事：这组在干什么、干到哪了 ──
        # 用户口径「想想你自己就知道该怎么做了」：我接手时第一眼找的是任务，
        # 不是状态。放在最前面，因为后面全是"别踩什么"，没有任务就不知道往哪走。
        # 2026-10-02（用户口径「我需要质量上去」）：**现状优先，流水补充。**
        #
        # 原来这里只贴 task_rows（= _steps.md 的尾巴）。实测出的错：
        # 它给的「下一步」是 08:20 那条「下一步跑 p10」，而 p10 早已跑完 ——
        # 接手方照它走就会**重跑一个已完成的步骤**（任务书明令禁止的事）。
        # 真结论写在 _task_progress.md，而 task_rows 根本不读那个文件。
        #
        # 所以顺序改成：
        #   ① [当前进度]  来自 _task_progress.md 的「进行中」段  <- 权威、是现状
        #   ② [近期流水]  来自 _steps.md 的尾巴               <- 补充、是历史
        _pr = progress_rows(slugs=want)
        if _pr:
            out.append('')
            out.append('── 当前进度（★任务台账，权威）──')
            out.append(_pr)
        _tk = task_rows(slugs=want)
        if _tk:
            out.append('')
            # 2026-10-02：**流水里要把同一任务的过期行摘掉。**
            # 实测自相矛盾：上面「当前进度」说"不要重跑 p10"，
            # 下面流水却还挂着 08:20 那条「下一步跑 p10」—— 接手方信哪个？
            # 判据（纯机械，不做语义判断）：「当前进度」里出现过的任务名，
            # 其流水行不再重复贴 —— 现状已经讲了，流水再讲一遍只会打架。
            _cur_names = set()
            if _pr:
                # 取「当前进度」里出现的**任务号前缀**（R2-E77-p10 -> R2-E77）。
                # 实测第一版踩坑：只用完整名比对，而流水行叫 `R2-E77`、
                # 台账里叫 `R2-E77-p10` —— 「R2-E77」子串虽在，
                # 但方向反了（我判的是 name in row，实际要 row 前缀在 name 里）。
                # 现在两边都按 R2-E<数字> 的前缀取，再比前缀。
                for _m in re.finditer(r'R2-E\d+', _pr):
                    _cur_names.add(_m.group(0))
            def _same_task(nm):
                _mm = re.match(r'(R2-E\d+)', nm or '')
                return bool(_mm and _mm.group(1) in _cur_names)
            _tk2 = [r for r in _tk if not _same_task(r['name'])] or _tk
            out.append('── 近期流水（步骤台账，最近 %d 步；只作补充，'
                       '与上面冲突时以上面为准）──' % len(_tk2))
            for r in _tk2:
                out.append('%s@第%s步 [%s] %s' % (r['name'], r['step'],
                                                  r['when'], r['what']))
        # 2026-10-01 第452步：**各号近期并进任务节，别单独漂在后面。**
        # 它讲的是"这个组几个号各干了多少"，属于任务视图；
        # 原来接在任务节后面自成一行，看着像第六节的开头。
        seg = []
        for s in sorted(by, key=lambda x: -by[x]['n']):
            d = by[s]
            seg.append('%s %d轮/干活%d%%' % (s, d['n'], round(100.0 * d['tool'] / max(1, d['n']))))
        if seg:
            if _tk:
                out.append('本组各号：' + '；'.join(seg))
            else:
                out.append('各号近期：' + '；'.join(seg))
        # 2026-10-01 第451步：结论句原文照给。
        #
        # 见 conc_top 的注释：判「这几条说的是不是一回事」靠理解，
        # 不是字符串相似度 —— 交给接手的模型（同源），比我写的正则准。
        #
        # 2026-10-01 第452步：**但重复到 45% 就不叫"照给"了，叫偷懒。**
        # 实测这一节 22 行 682 字里 358 字是同一件事的复述（同一个号连说
        # 几十轮"磁盘拿不到"）。我原以为"让接手方自己归并"是尊重同源，
        # 其实是把该我干的活塞给它 —— **完美交接不该让接手方替我收拾桌子。**
        #
        # 现在按**谓词骨架**只留每类里信息最全的一条。这仍然不涉及语义判断
        # （骨架是纯机械的谓词位匹配），只是把"逐字重复"放宽到"同类重复"。
        # 真正的语义归并（这几条是不是同一个根因）还是留给接手方。
        conc = conc_top(want, cap=22)
        if conc:
            _picked, _seen_sk = [], {}
            for ts, sg, s in conc:
                sk = _hx_skeleton(s) or s[:30]
                if sk in _seen_sk:
                    old = _seen_sk[sk]
                    if len(s) > len(old[2]):
                        _picked[_picked.index(old)] = (ts, sg, s)
                        _seen_sk[sk] = (ts, sg, s)
                    continue
                _seen_sk[sk] = (ts, sg, s)
                _picked.append((ts, sg, s))
            if _picked:
                # 2026-10-01 第452步：**按时间排。** 实测输出是 14:35 → 14:42
                # → 14:36 这种乱序 —— 因为同行替换（信息更全的顶掉旧的）时
                # 是就地替换，位置留在原来的坑里，跟 ts 已经对不上了。
                # 标了"按时间顺序"就必须真按时间，否则接手方读起来是断的。
                _picked.sort(key=lambda x: x[0] or '')
                out += ['', '── 单句结论（按时间顺序，同类已并，原文未改）──']
                for ts, sg, s in _picked:
                    out.append('[%s %s] %s' % (ts, sg, s))
        if hard:
            out += ['', '── 硬约束（用户原话，不要违反）──']
            for i, s in enumerate(hard, 1):
                out.append('%d. %s' % (i, s))
        if pit:
            out += ['', '── 已经踩过的坑（别再踩）──']
            for i, s in enumerate(pit, 1):
                out.append('%d. %s' % (i, s))
        if shared:
            # 2026-10-01 第465步（用户口径「必须要完美还能优化」）：
            # **只列「有产出的动作」，不列「读文件」。**
            # 实测原来 12 行里 4 行是「- read x14 次 … _pending.md」——
            # 那对交接没用：谁都知道要看台账，重复读一遍也不叫「重做」。
            # 真正要拦的是「同一个 write/pwsh 又跑一遍」（有副作用）。
            # 另外洗掉参数里的 DSML 标签/围栏 —— 那是解析残留，读不懂。
            _RO = ('read', 'glob', 'grep', 'job_list')
            _rows = []
            for k, v in shared:
                _tool = k[0]
                if _tool in _RO:
                    continue                    # 只读动作，不算「做过」
                # 2026-10-01 第465步：**DSML 是全角竖线！**
                # 实测残留长这样：`<｜｜DSML｜｜ calls> <｜｜DSML｜｜ invoke name=...>`
                # 我上一版写的正则用的是 ASCII 竖线（\|），一条都没匹上，
                # 所以「已做过」里照旧糊着一坨标签。判据要照着真实字节写：
                # 全角 ｜ = U+FF5C。半角也留着，两种都洗。
                _args = re.sub(r'<[\|\uff5c]{2}[^>]*?>', ' ', k[1])
                _args = re.sub(r'\uff5c{2}\s*DSML\s*\uff5c{2}', ' ', _args)
                _args = re.sub(r'^```\w*\s*', '', _args)
                _args = re.sub(r'\s+', ' ', _args).strip()
                if _args.startswith('calls') or len(_args) < 12:
                    continue                    # 洗完全是垃圾的不要
                _rows.append((_tool, v, _args[:90]))
            if _rows:
                out += ['', '── 本组已经做过的（不要再做一遍）──']
                for _t, v, _a in _rows[:10]:
                    out.append('- %s x%d 次（%s 都跑过）：%s'
                               % (_t, v['n'], '/'.join(sorted(v['slugs'])), _a))
        # 2026-10-01 第453步：**后台 job 给指引，不给快照。**
        #
        # 用户问「交接的时候当前任务是否在后台跑」。先把这件事本身看懂：
        #
        #   job_id 是**桥级资源**（形如 pwsh-2），不是号私有的。
        #   实证（06:50~06:54）：310 起了 pwsh-2 -> 换号到 779 ->
        #   779 接手后照样 job_output/job_kill 同一个 id，最后把它收掉。
        #   **换号不影响 job，接手方本来就能接着操作。**
        #
        # 所以：
        #   ✗ 不把"后台在跑什么"冻结进交接 —— job 是活的、交接是快照，
        #     写进去立刻过期，反而误导（它可能早跑完了）。
        #   ✓ 只给一句指引：本组用过 job，去 job_list 查。
        #   接手方要的是「该去哪查」，不是「别人替我查好的结果」。
        #
        # 只在有证据时给（最近这些轮出现过 job_* 调用），没用过的组不塞废话。
        # 2026-10-01 第453步：**改成无条件给指引。**
        #
        # 我先试过"只在有证据时给"（扫最近 1200 轮的 head/tail 找 job_* 调用），
        # 结果命中 1 次 —— **判据本身不可靠**：job 调用是工具调用，被 head 400 /
        # tail 200 字截掉了，真正有 job 的那批轮次（06:50、18:48）也不在窗口里。
        #
        # 拿不可靠的数据凑一节，比不给更糟（会漏报）。而这一节的内容本来就是
        # **通用指引**、不依赖具体证据 —— 那就无条件给，成本只有一行。
        out += ['', '── 后台任务 ──']
        out.append('本组可能还有后台 job 在跑（job_id 形如 pwsh-N）。'
                   'job 是**桥级资源、跨号通用**：换号后你照样能操作上一个号'
                   '起的 job。开工前先 job_list 看一眼，别重复起一个；'
                   '用不上的用 job_kill 收掉（实测接手方正是这么做的）。')
        # 2026-10-01 第465步（用户口径「必须要完美还能优化」）：
        # **「最后一步」要给「在干什么」，不是一坨工具调用 JSON。**
        # 实测输出是这样的：
        #   [21:47:09 309] ```json {"tool_calls": [{"name": "read", "arguments":
        #     {"file_path": "..."}}, ...
        # 接手方读到这个是懵的 —— 它要的是「上一棒停在干什么事」，
        # 不是「它最后调了哪个工具」。工具调用从里面的 name 就能还原成人话。
        last = rows[-1] if rows else None
        if last:
            # 2026-10-02 修（用户口径「最后一步是错的」）。
            #
            # 原来在这里拿 head400+tail200 现拼。实测最近 3300 轮：
            #   · 95.3% 被判成「工具调用」，其中 669 轮正文提不出东西，
            #     那一节只剩「调了 pwsh/read」—— 等于没说；
            #   · 1253 轮里 tail 开头就出现在 head 里，即 head/tail 是
            #     **同一个长 JSON 被截了两次**，拼起来读不通；
            #   · 截断落在一个 token 中间（实测「_work（对齐」被腰斩）。
            # 根因是按字符截、不认语义，事后无法还原。
            #
            # 现在优先用事件里存好的 lastact（正文完整那一刻算的）。
            # 老事件没有这个字段，才退回旧拼法 —— 只为不让历史行开天窗。
            _act = ' '.join(str(last.get('lastact') or '').split())
            out += ['', '── 最后一步 ──']
            if _act:
                out.append('[%s %s] %s' % (last.get('ts'), last.get('slug'), _act))
            else:
                _blob = (str(last.get('head') or '') + ' '
                         + str(last.get('tail') or ''))
                _names = re.findall(r'"name"\s*:\s*"([a-z_]+)"', _blob)
                _names += re.findall(r'invoke name="([a-z_]+)"', _blob)
                _desc = ' '.join(_blob.split())[:200]
                if _names:
                    _desc = '调了 ' + '/'.join(dict.fromkeys(_names))
                out.append('[%s %s] %s' % (last.get('ts'), last.get('slug'), _desc))
        return chr(10).join(out)
    except BaseException:
        # 2026-10-01 第467步：**这里原来是裸的 return ''，于是把自己写的
        # bug 吞了一整轮没人发现。** 实测：上一轮改「已做过」那节时写了
        #   _args = re.sub(r'...', ' ', _args)
        # 右边用了还没赋值的变量 -> UnboundLocalError 被这句 except 吃掉 ->
        # handoff_extract 对**所有组**返回空串，日志里一个字都没有。
        # 是搬完数据发现「交接 0 字」才查出来的。交接是换号唯一的信息来源，
        # 它空了必须响。
        try:
            import traceback as _tb
            sys.stderr.write('handoff_extract 异常（交接将为空）: '
                             + ''.join(_tb.format_exc().splitlines(True)[-3:])
                             + chr(10))
        except BaseException:
            pass
        return ''


# ===== 结论句库：产生的那一刻就抽出来存好（2026-10-01 第451步）=====
#
# 用户问：「想想你自己怎么工作的 你是什么算法产生的 该用什么算法能模仿你」
#
# 我是**每一轮拿当前上下文重新推理** —— 上下文不是记忆，是工作台。
# 工作台摆什么，决定我这一轮能干什么。而摆的东西有用程度差别巨大：
#   任务目标    每轮都看，决定方向
#   刚踩的坑    一眼扫到就绕开，扫不到必踩     <- 最值钱
#   文件状态    按需去查，不用记住
#   过程叙事    几乎不用
#   原始全文    基本不读，是噪声
# 我不是「读完再干」，是「边干边反复回来扫」。
# 所以交接最该有的是**扫一眼就生效**的句子。
#
# ## 判据：能不能脱离上下文单句生效
#   「不要用三反引号」          -> 扫到照做   OK
#   「上次超时是因为全 DEX 扫描」-> 扫到避开   OK
#   「我这一轮读了三份文件」     -> 要上下文才懂 NO
#
# ## 为什么不从 _relay_events 的 head/tail 抽
# 实测不行：head 400 字、tail 200 字，是**按字符截**的不是按语义。
# 7142 条候选里祈使句只有 1 条，因果句 72 条前 4 条还是同一句复述。
# 素材本身不够，再好的算法也抽不出东西 —— 所以在产生的那一刻抽。
CONC_FILE = _conf_file("_conclusions.jsonl")   # 落 ds/ —— 见 MARKS_FILE 注
CONC_MAX = 8 * 1024 * 1024
_conc_lock = threading.Lock()

CONC_IMPER = re.compile(r'(不要|别用|禁止|必须|务必|切记|只准|避免|严禁|绝对不|不能|不得)[^\n]{4,120}')
CONC_CAUSE = re.compile(r'[^\n]{6,90}(?:因为|由于|导致|所以|原因是|根因|所致)[^\n]{6,110}')
CONC_DEAD = re.compile(r'[^\n]{6,80}(?:超时|不支持|不存在|假阴性|已废|装不上|读不到|拿不到)[^\n]{0,90}')
# 2026-10-01 第465步（用户口径「必须要完美还能优化」）：
# **滤掉「环境噪声」类结论。**
#
# 实测交接里「坑」14 行有 17 处、单句结论 12 行里大半都是同一类：
#   「工具通道无回执，实测值拿不到，不猜」
#   「本轮我发出的 pwsh 调用均无执行结果返回」
#   「在此之前我只报拿不到，不填猜测值」
# 它们讲的是**当时那个窗口的工具通道坏了**，不是这个任务的坑。
# 对下一棒毫无价值 —— 通道早好了，而且它也不会重踩这个。
#
# 判据：一句话里同时出现「通道/回执/实测值」和「拿不到/无法/不猜」，
# 就判为环境噪声。单出现一个不算（可能在讲真有价值的事）。
CONC_NOISE = re.compile(
    r'(通道|回执|实测值|工具结果).{0,40}(拿不到|无法|收不到|没有结果|不猜|不编造)'
    r'|(拿不到|无法|收不到|没有结果).{0,40}(通道|回执|实测值)'
    r'|本轮(我)?(发出|无).{0,30}(pwsh|工具|命令)'
    r'|只报.{0,10}拿不到')


CONC_NEG = re.compile(r'(本轮我|这一轮我|我核对|我这一轮|无法实测|给不了|不猜|本轮无)|(<\|)')


def conc_sentences(text, cap=6):
    out, seen = [], set()
    try:
        body = re.sub(r'```.*?```', ' ', text or '', flags=re.S)
        body = re.sub(r'`[^`]{0,60}`', ' ', body)
        for raw in re.split(r'[\n。；！？]', body):
            s = raw.strip().lstrip('-*># ').strip()
            if len(s) < 12 or len(s) > 170:
                continue
            if CONC_NEG.search(s):
                continue
            if re.search(r'[{}<>=\[\]]|def |import |await |return ', s):
                continue
            if s.count(chr(34)) + s.count(chr(39)) > 3:
                continue
            score = 0
            if CONC_DEAD.search(s):
                score = 3
            elif CONC_CAUSE.search(s):
                score = 2
            elif CONC_IMPER.search(s):
                score = 2
            if not score:
                continue
            k = s[:40]
            if k in seen:
                continue
            seen.add(k)
            out.append((score, s))
        out.sort(key=lambda x: -x[0])
        return [s for _, s in out[:cap]]
    except BaseException:
        return []


def conc_save(slug, sid, text, ts=None):
    try:
        sents = conc_sentences(text)
        if not sents:
            return 0
        row = {'t': time.time(), 'ts': ts or time.strftime('%H:%M:%S'),
               'slug': str(slug or ''), 'sid': str(sid or ''),
               'sents': sents}
        line = json.dumps(row, ensure_ascii=False, default=str) + chr(10)
        # 2026-10-01 第461步修：**_f 必须在用之前赋值。**
        # 实测踩到：改成分组目录时只把「打开写入」那行换成了 _f，
        # 上面的轮转判断（_f.stat()）还在 _f 被赋值之前 —— NameError，
        # 而整个函数包着 except BaseException -> **静默什么都不写**。
        # 后果：结论句库一直是空的（emit/msgs 都写进去了，只有它没有）。
        # 这类「异常被吞掉」的错误不报错、不留痕，只能靠实测发现。
        _f = grp_path("conclusions", slug, create=True) or CONC_FILE
        with _conc_lock:
            try:
                if _f.stat().st_size > CONC_MAX:
                    bak = _f.with_name(_f.name + '.' + time.strftime('%Y%m%d_%H%M%S'))
                    _f.rename(bak)
                    old = sorted(_f.parent.glob(_f.name + '.*'), key=lambda x: x.stat().st_mtime)
                    for x in old[:-3]:
                        x.unlink()
            except OSError:
                pass
            with _f.open('a', encoding='utf-8') as f:
                f.write(line)
        return len(sents)
    except BaseException:
        return 0


def conc_top(slugs=(), limit=300, cap=28):
    """给这几个号攒「单句可生效」的清单。

    ## 2026-10-01 第451步：这里**不做语义去重**，交给接手方。

    用户口径：「想想你自己怎么工作的 该用什么算法能模仿你 他们是你的兄弟姐妹」。

    我在这个去重上连着栽了三次，记下来免得再犯：
      · 字符 bigram Top8   -> 200 条句子算出 200 个骨架，一条没并上
        （「磁盘余量」和「盘余量工」是不同的 bigram）
      · 谓词骨架 NOCALLBACK+CANNOT -> 好一些，但同主体不同侧面还是散着
      · 只按前 36 字去重     -> 「磁盘余量：…给不了」和「文件数量：…给不了」
        前半句不同、后半句一样，一条都拦不住

    根子不在算法，在**我把"同一件事"定义成了"句子像"**。而实际上
    「磁盘拿不到、进程拿不到、配置拿不到」不是三个坑，是**一个事实：
    工具通道坏了**。判「是不是同一个根因」靠的是理解，不是字符串相似度。

    而这恰恰是我最擅长、最难写成正则的事 —— 所以别在正则上磨了：
    **桥负责收集，去重交给接手的模型。** 接手的是同一个模型，
    它判「这几条说的是不是一回事」比我写的任何正则都准。

    所以这里只做两件机器该做的事：按时间取最近、**原文照给**。
    不去重、不摘要、不改写 —— 我看到的和我自己写的一模一样，
    这样它才能像我一样推理。
    """
    want = set(str(s) for s in (slugs or ()) if s)
    if not want:
        return []
    try:
        # 2026-10-01 第458步：桥自己判断读哪份结论句库（同 handoff_extract）
        rows = []
        for j in grp_read('conclusions', want, limit=limit):
            for s in (j.get('sents') or []):
                rows.append((str(j.get('ts') or ''), str(j.get('slug') or ''), s))
        # 原样去重只做"逐字完全相同"这一层（纯机械，不涉语义）
        out, seen = [], set()
        for ts, slug, s in reversed(rows):
            k = s
            if k in seen:
                continue
            # 2026-10-01 第465步：滤掉「环境噪声」——「通道拿不到/无回执/只报拿不到」
            # 这类句子讲的是当时那个窗口的工具通道坏了，不是任务的坑。
            # 实测「坑」里有 17 处是同一句的复述，对下一棒毫无价值（通道早好了）。
            if CONC_NOISE.search(s):
                continue
            seen.add(k)
            out.append((ts, slug, s))
            if len(out) >= cap:
                break
        out.reverse()
        return out
    except BaseException:
        return []


# ===== 任务视图：交接里「这组在干什么、干到哪了」（2026-10-01 第452步）=====
#
# 用户口径：「想想你自己就知道该怎么做了」
#
# 我自己接手一件事，第一眼找的是**我在干什么**，然后才是别踩什么。
# 而第450步那版交接五节全是「状态」，没有一节说任务 —— 这是最大的缺陷。
#
# ## 任务从哪来
#
# LEDGER_STEPS（_steps.md）：行格式固定
#   任务名@第N步 | 时间 | 干了什么 | 判据/验证 | 回滚点
# 458620 字 / 1983 行 / 311 条步骤行，任务名天然聚合（账号池 86、托盘 60…）。
#
# ## 两个坑（实测踩到）
#
# 1. **交接行混进任务台账。** 最近 12 步里有 6 步是 309@交接 / 483@交接 ——
#    那是旧的「本地代写交接」整段被当成一步写进去，把真正的任务行挤掉了。
#    这类行必须滤掉：任务名是**账号号**（纯数字）且描述含「交接给下一棒」的，
#    不是任务，是桥自己的交接动作。
#
# 2. **任务名不等于组名。** 组叫「剪辑」，任务是「账号池」「托盘」「元宝」…
#    按组名匹配任务名会一个都匹配不到。所以这里**不按组过滤**，
#    直接把最近的任务行给过去 —— 接手方自己认得哪个是它在干的活。
TASK_STEPS = re.compile(r'^\s*([^|@]{1,40}?)@第(\d+)步\s*\|\s*([^|]*)\|\s*([^|]*)')
TASK_NOISE = re.compile(r'交接给下一棒|本地代写交接|本地代写')


def progress_rows(slugs=(), max_chars=900):
    """读「★任务台账」(`_task_progress.md`) 的「进行中」段 —— 这才是**当前**进度。

    2026-10-02 加（用户口径「我需要质量上去」）。

    为什么必须有这个：交接原来只读 `_steps.md` 的尾巴，而那是**流水**。
    实测（21:34 那条交接）：
      · 它给的「下一步」是 `R2-E77@第10步` 08:20 写的「下一步跑 p10」；
      · 可 p10 **20:45 之前就跑完了**（yb_r2e77_p10.json 14638B 在）。
      · 真实结论写在 `_task_progress.md` 里，而 task_rows **根本不读那个文件**。
    后果：接手方照交接走会**重跑一个已完成的步骤** —— 而任务书明令禁止。
    这不是美观问题，是会把接手方带偏的事实错误。

    所以交接的第一节改成：**先给任务台账的「进行中」段（现状），
    再给步骤台账的尾巴（流水）**。现状优先，流水补充。
    """
    try:
        want = [str(s) for s in (slugs or ()) if s]
        # 走 grp_path —— 与 events/steps/pending 同一套解析，换组/换机不会断。
        cands = [grp_path("progress", want[0] if want else "")]
        for c in cands:
            if not c:
                continue
            c = pathlib.Path(str(c))
            if not c.is_file():
                continue
            t = c.read_text(encoding="utf-8", errors="replace")
            # 「进行中」段优先；没有就退回「先做这三件」段（很多台账把
            # 下一步写在那里）。**实测踩到**：只取「进行中」时，
            # 「下一步」那格写的是"见下方「先做这三件」"—— 而那段被 max_chars
            # 截掉了，等于没给下一步。所以两段都取。
            seg = ""
            m = re.search(r"## 进行中(.*?)(?=\n## |\Z)", t, flags=re.S)
            if m:
                seg = m.group(1).strip()
            for pat in (r"## 先做这三件?(.*?)(?=\n## |\Z)",
                        r"## 本轮真正的下一步(.*?)(?=\n## |\Z)"):
                m2 = re.search(pat, t, flags=re.S)
                if m2 and m2.group(1).strip():
                    seg = (seg + chr(10) + chr(10)
                           + m2.group(0).strip()) if seg else m2.group(0).strip()
            if seg:
                seg = re.sub(r"\n{3,}", chr(10) + chr(10), seg)
                # 2026-10-02：**不许腰斩表格。** 实测 max_chars 正好切在
                # 「| 任务书说 | 」这种半行上，接手方看到的是一张断表。
                # 超长就按**空行**切，退回最近一个完整段落。
                if len(seg) > max_chars:
                    cut = seg.rfind(chr(10) + chr(10), 0, max_chars)
                    seg = seg[:cut] if cut > max_chars // 2 else seg[:max_chars]
                return seg
            # 兜底：整份台账就是任务台账（小），直接给
            body = t.strip()
            if body:
                return body[:max_chars]
    except BaseException:
        pass
    return ""


def task_rows(limit=10, max_chars=110, slugs=()):
    """从步骤台账里取「每个任务干到哪了」。纯本地读文件。

    2026-10-01 第452步：**按任务名聚合，每个任务只留最近一步。**

    为什么（实测）：直接取最近 14 行，结果 8 行是 R2-E10…R2-E19 那批
    元宝逆向的细碎单步（每个都叫 @第1步），把主线任务（元宝 Phase 6）
    整个淹没了 —— 2819 字里有效的不到三分之一。
    接手方要的是「这个项目走到哪了」，不是「最近有哪几条记录」。
    所以按任务名分组、各取最后一条，再按时间倒序。

    为什么截到 110 字：一条步骤行的细节（脚本名、统计数字）接手方按需
    去读文件就行，交接里只需要"干了什么"这个粒度。
    """
    try:
        # 2026-10-01 第458步：**读本组的步骤台账。**
        # 实测踩到：两个组抽出来的「任务在干什么」一模一样 —— 因为这里
        # 一直读全局 _steps.md。那正是串组：剪辑组的交接里带着 test 组的任务。
        # 现在按 slugs 推组，读组目录下的 _steps.md；没搬过就回落全局那份。
        want = [str(s) for s in (slugs or ()) if s]
        p = grp_path("steps", want[0] if want else "") or LEDGER_STEPS
        p = pathlib.Path(str(p))
        if not p.is_file():
            return []
        latest = {}
        order = []
        txt = p.read_text(encoding='utf-8', errors='replace')
        for ln in txt.splitlines():
            if '@第' not in ln or '|' not in ln:
                continue
            if TASK_NOISE.search(ln):
                continue
            m = TASK_STEPS.match(ln)
            if not m:
                continue
            name = m.group(1).strip()
            step = m.group(2)
            when = m.group(3).strip()
            what = m.group(4).strip()
            if name.isdigit():
                continue
            if len(what) < 8:
                continue
            if name not in latest:
                order.append(name)
            latest[name] = {'name': name, 'step': step, 'when': when,
                            'what': what[:max_chars]}
        # 2026-10-01 第452步：**按时间倒序，不按文件位置。**
        #
        # 实测踩到：用"最后出现的位置"排序，结果 R2-E10…R2-E19 那批旧的
        # 单步排在最前，主线「元宝 Phase 6.2」（真正的当前工作）被压到最后。
        # 原因：那些老行散落在文件各处、位置靠后，而主线行是后追加但位置更早。
        # 接手方要的是"最近在动什么"，那就该按 when 排。
        rows = [latest[n] for n in order]
        rows.sort(key=lambda r: r.get('when') or '', reverse=True)
        return rows[:limit]
    except BaseException:            # noqa: BLE001
        return []


# ===== 组工作区根：从流量里学（2026-10-01 第454步）=====
#
# 用户口径：「不是硬编码要桥自适应」。
#
# ## 为什么不能写死
#
# 我先前做成 group.root="F:/a" —— 那是死值：换台机器、换个项目就废，
# 而且没人知道该填什么。**自适应 = 从它实际干活的地方学。**
#
# ## 学什么
#
# 每轮请求都带工具调用，工具调用里全是绝对路径。
# 实测 4563 个 turn，按组统计路径分布：
#   组 剪辑 : C:/Users 1379 | F:/test 635 | F:/工具 479
#   组 test : C:/Users 4071 | C:/tools 22 | D:/Doubao 8
# 出现最多的那个前缀，就是这个组在干活的地方。
#
# ## 怎么定「根」
#
# 不取最长公共前缀（Windows 上会退化成 C:/ 这种没用的）。
# 取**出现频次最高的前两级**（C:/Users、F:/a、D:/Proj…），
# 按见过的次数投票，够多才认。
GRP_ROOT_FILE = _conf_file("_group_roots.json")
GRP_ROOT_MIN = 3
_grp_root_lock = threading.Lock()


def _grp_root_all():
    try:
        if GRP_ROOT_FILE.is_file():
            d = json.loads(GRP_ROOT_FILE.read_text(encoding="utf-8"))
            return d if isinstance(d, dict) else {}
    except BaseException:            # noqa: BLE001
        pass
    return {}


def group_root_learned(group_name):
    """这个组学到的工作区根。没学到返回空串。"""
    try:
        g = str(group_name or "").strip()
        if not g:
            return ""
        d = _grp_root_all()
        cell = d.get(g) or {}
        cand = cell.get("candidates") or {}
        if isinstance(cand, dict) and cand:
            best = max(cand.items(), key=lambda kv: kv[1])
            if best[1] >= GRP_ROOT_MIN:
                return best[0]
        return str(cell.get("root") or "")
    except BaseException:            # noqa: BLE001
        return ""


def group_root_learn(group_name, root):
    """记一次：这个组又在这个根下干活了。"""
    try:
        g = str(group_name or "").strip()
        r = _norm_root(root)
        if not g or not r:
            return
        with _grp_root_lock:
            d = _grp_root_all()
            cell = d.setdefault(g, {})
            cand = cell.setdefault("candidates", {})
            cand[r] = int(cand.get(r) or 0) + 1
            cell["root"] = r
            cell["seen"] = int(cell.get("seen") or 0) + 1
            cell["at"] = round(time.time(), 3)
            tmp = GRP_ROOT_FILE.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(d, ensure_ascii=False, indent=1),
                           encoding="utf-8")
            tmp.replace(GRP_ROOT_FILE)
    except BaseException:            # noqa: BLE001
        pass


# ===== 分组独立工作区：每组一个根，组内一切数据在 <root>/<组名>/ 下（第454步）=====
#
# 用户口径：
#   「全是win所以 别管哪个地方都需要自适应 例如 剪辑组 工作区是f盘的a目录
#     那我所有的数据就在a目录 哪个组呢 剪辑组 那就以剪辑命名目录
#     所有的分类在剪辑目录新建或者使用
#     这样不会串窗口及一些乱七八糟的bug 保持每个分组的独立性」
#
# ## 病根
#
# 改之前：**所有组的数据挤在桥所在的一个目录里。**
# 剪辑组和 test 组共用同一份 _relay_events.jsonl / _conclusions.jsonl /
# _pending.md / _steps.md —— 于是：
#   · 交接抽取要把全部事件捞出来再按 slug 过滤（混着别人的）
#   · 台账里不同组的行互相挤（实测 _steps.md 里 309@交接 那种噪声）
#   · 换个组干活路径却没变，模型分不清自己该在哪写
#
# ## 结构
#
#   <组根>/<组名>/            <- 一组一个目录，这就是它的工作区
#        ├── _临时/          临时文件
#        ├── _bak/           回滚点备份
#        ├── 脚本/ 输出/ 归档/
#        ├── _steps.md       本组步骤台账（独立）
#        └── _pending.md     本组全局台账（独立）
#
# ## 根从哪来（三级，窄的赢）
#
#   1) 分组配置里该组的 root   —— 显式指定，最准（用户可写 F:/a）
#   2) 该组号请求里的证据根     —— 自适应，跟着对端机器走
#   3) 桥本机 workdir          —— 兜底
#
# 全 Windows，盘符路径就够；但判断一律走 _norm_root，
# 以后真要接 Mac/Linux 只改那一个函数。
GROUP_ROOT_KEY = "root"


def group_root_of(group_name, slug="", evid_root=""):
    """这个组的工作区根 —— **自适应，不靠硬编**。

    用户口径：「不是硬编码要桥自适应」。

    我先前把它做成"配置里写 root=F:/a" —— 那是死值，换台机器/换个项目就废。
    正确的自适应是**从实际干活的地方学**：

      第 1 轮：不知道 -> 用本轮证据根（工具调用里出现的路径）
      第 2 轮：从流量里学到 "F:/a" -> 记住
      第 N 轮：直接用记住的，不再猜

    实测每个组的流量里路径分布很明确（4563 个 turn 统计）：
      组 剪辑 : C:/Users 1379 | F:/test 635 | F:/工具 479
      组 test : C:/Users 4071 | C:/tools 22 | D:/Doubao 8
    所以"学"是有料的，不需要谁来告诉桥。

    ## 三级，窄的赢

      1) 学到的（group_roots.json，桥自己写的）
      2) 本轮证据根（_evid_root_from_messages 抽的）
      3) 桥本机 workdir（兜底）

    配置里的 root 仍然认（想手工钉死某组时有用），但**不是必须的**。
    """
    g = str(group_name or "").strip()
    # 1) 手工钉死（可选）
    try:
        for gg in (_groups_root().get("groups") or []):
            if str(gg.get("name") or "") == g:
                r = _norm_root(str(gg.get(GROUP_ROOT_KEY) or ""))
                if r:
                    return r
                break
    except BaseException:            # noqa: BLE001
        pass
    # 2) 显式记下的（建组/改路径时写进 _group_roots.json）
    r = _norm_root(group_root_learned(g))
    if r:
        return r
    # 3) 本轮证据 —— **只用，不学。**
    # 2026-10-01 第459步：砍掉了「从流量统计猜根」（group_root_observe）。
    # 三次改判据都失败：覆盖率一路走到底 -> 太深；分叉数+cov -> 量纲不同
    # 相加，永远停在 C:/Users；覆盖率拐点 -> 候选里全是带反引号/文件名
    # 的垃圾路径。
    # 根子不是判据，是**「用统计猜一个语义值」本身就不该自动化**：
    # 猜错了没有反馈（写到 C:/Users 也没人报错）、代价还大（六个号
    # 全往错地方写），而正确答案用户一句话就能给。
    # 所以根只有两个确定来源：配置（建组/改路径时写） + 本机 workdir 兜底。
    r = _norm_root(evid_root)
    if r:
        return r
    # 4) 兜底
    try:
        r = _norm_root((_ini_read().get("codex") or {}).get("workdir") or "")
        if r:
            return r
    except BaseException:            # noqa: BLE001
        pass
    return ""


def group_dir_of(group_name, slug="", evid_root="", root=""):
    """这个组的工作区目录 = <根>/<组名>。空组名返回空串。

    组名做文件名安全处理（去斜杠冒号等），但**保留中文** ——
    用户要的就是「以剪辑命名目录」。
    """
    g = str(group_name or "").strip()
    if not g:
        return ""
    safe = "".join(ch for ch in g if ch not in '\\/:*?"<>|').strip()
    if not safe:
        return ""
    # 2026-10-01 第457步：**显式传进来的 root 优先。**
    #
    # 实测踩到：group_move_data("剪辑", root=".../_TESTWORK") 结果落到了
    # 配置里的根（teste）下 —— 因为这里把 root 当 evid_root 转给
    # group_root_of()，而那个函数**先查配置**，显式值被配置顶掉了。
    #
    # 语义要分清：
    #   · root 参数 = 调用方**已经决定好了**，直接用它
    #   · evid_root = 一条"证据"，让 group_root_of 按优先级去挑
    # 所以我加一个 by_force 参数贯穿，显式值不再下探。
    if root:
        r = _norm_root(root)
    else:
        r = group_root_of(g, slug=slug, evid_root=evid_root)
    if not r:
        return ""
    return r + "/" + safe


# ===== 本机根上下文：让 split_calls 能把相对路径补成绝对（2026-10-01 第459步）=====
#
# 问题（用户问「换电脑以后是否还能正常调用工具」）：
#   工具本身没问题 —— 桥不定义工具，客户端每轮传来，模型输出 tool_calls
#   原样交回去执行。**唯一的机器相关点是参数里的路径。**
#
#   而模型填路径靠的是〔本机目录〕段。实测它经常不照做：
#     .state/ds_bridge.log:32339  模型回「真实工作区在 F:/test」
#     而本机根本没有 F 盘 —— 那是别台机器的根。
#   提醒再多也没用（那段末尾本来就写着「唯一一份绝对路径来源」）。
#
# 更好的办法：**不让模型负责拼绝对路径。**
#   模型填 F:/a/剪辑/x.py 或 x.py 都行，桥在把 tool_calls 交回客户端之前
#   统一把路径**锚定到本轮的〔本机目录〕根**上。模型填错了也会被纠正。
#
# 为什么用 threading.local：桥是 ThreadingHTTPServer，一个请求一个线程，
# 两个组同时来请求时模块级变量会互相覆盖（把 A 组的根用到 B 组头上，
# 正是要防的串组）。threading.local 天然按线程隔离。
_ROOT_CTX = threading.local()


def set_request_root(root, group="", slug=""):
    """记下本轮请求的〔本机目录〕根 + 组/号。dir_block_now 每轮调。

    2026-10-01 第459步补（用户口径：「主要不是一个模型是好几个 每个都告诉一遍吗」）：
    **光记根不够，还要记组。** 因为要好几个号轮流上，
    如果锚定用的是「本轮请求抽出来的根」，同一个组的不同号、
    不同轮次可能锚出不同结果 —— 那就还是要每个号各自踩一遍。
    记下组之后，anchor_paths 改问 group_root_of(组)，
    同组六个号必然锚到同一个地方，且不随当轮内容漂。
    """
    try:
        _ROOT_CTX.root = _norm_root(root) or ""
        _ROOT_CTX.group = str(group or "")
        _ROOT_CTX.slug = str(slug or "")
    except BaseException:
        pass


def get_request_root():
    """本轮请求的本机根。没设过返回空串。"""
    try:
        return str(getattr(_ROOT_CTX, "root", "") or "")
    except BaseException:
        return ""


# 哪些工具参数是路径（按参数名判，不按工具名 —— 桥不认工具名）
PATH_ARGS = ("file_path", "path", "filepath", "filename", "dir",
             "directory", "cwd", "workdir", "output", "target")
# 盘符路径 / UNC / 已经是绝对路径的，不动
_ABS_RE = re.compile(r'^(?:[A-Za-z]:[/\\]|[/\\]{2}|/)')


# 必填但可以安全补默认值的字段（说明性，不影响执行语义）。
# 真正决定「干什么」的字段（code / command / file_path…）缺了不能瞎填 ——
# 那是模型没干活，该让它重来，补一个假的反而更糟。
FILLABLE = {
    "description": "执行",
    "reason": "",
}


def fill_required(calls, tools):
    """按本轮工具表查必填字段，缺的**只补说明性那些**。返回补了几处。

    2026-10-01 第462步（用户实测报错：missing required property "description"）。

    病根不在桥：**是模型自己漏的**。日志实证 ——
        21:30:38 [309] ← {"name": "run_code", "arguments": {"code": ...}}
    只给 code 没给 description，dsh 直接拒。
    而 TOOL_PROTOCOL 里早写着「必填字段一个都不能少，实测最常漏的是 pwsh
    的 description」—— 提醒了也没用，模型照漏。

    所以按 anchor_paths 同一个思路：**与其提醒，不如在出口纠一次。**
    桥手上有 req[tools] 的完整 schema（required 数组），查得出缺哪个。
    只补 FILLABLE 里那些说明性字段；code/command 这类缺了不补 ——
    补个假的等于让模型假装干了活，比报错更危险。
    """
    try:
        if not calls or not tools:
            return 0
        req_map = {}
        for t in tools:
            fn = t.get("function") if isinstance(t, dict) else None
            fn = fn or (t if isinstance(t, dict) else {})
            nm = str(fn.get("name") or "")
            if not nm:
                continue
            req_map[nm] = ((fn.get("parameters") or {}).get("required") or [])
        n = 0
        for c in calls:
            try:
                fn = c.get("function") or {}
                nm = str(fn.get("name") or "")
                need = req_map.get(nm)
                if not need:
                    continue
                args = fn.get("arguments")
                if isinstance(args, str):
                    try:
                        args = json.loads(args or "{}")
                    except ValueError:
                        continue
                if not isinstance(args, dict):
                    continue
                for k in need:
                    if k in args:
                        continue
                    if k in FILLABLE:
                        args[k] = FILLABLE[k]
                        n += 1
                fn["arguments"] = json.dumps(args, ensure_ascii=False)
            except BaseException:        # noqa: BLE001
                continue
        return n
    except BaseException:            # noqa: BLE001
        return 0


def anchor_paths(calls):
    """把工具调用里的**相对路径**锚定到本轮本机根。返回改了几处。

    只动满足三条的：
      1) 参数名在 PATH_ARGS 里（file_path / path / cwd…）
      2) 值是非空的字符串
      3) 值**不是**绝对路径（没有盘符、不是 / 开头、不是 UNC）

    绝对路径一律不碰 —— 模型可能故意写别的地方（比如 C:/Windows/Temp），
    桥没有立场改它。只补「模型偷懒只写了文件名」那种。
    """
    # 2026-10-01 第459步：**优先用「组根」，不用「本轮证据根」。**
    # 理由见 set_request_root 的注释：好几个号轮流上，组根是六个号
    # 共用的那一个值；证据根会随当轮请求内容漂，锚出来就各不一样。
    root = ""
    try:
        _g = str(getattr(_ROOT_CTX, "group", "") or "")
        _s = str(getattr(_ROOT_CTX, "slug", "") or "")
        if _g:
            root = _norm_root(group_root_of(_g, slug=_s)) or ""
    except BaseException:
        root = ""
    if not root:
        root = get_request_root()          # 兜底：没记组时用本轮的根
    if not root or not calls:
        return 0
    n = 0
    for c in calls:
        try:
            args = c.get("arguments") if isinstance(c, dict) else None
            if not isinstance(args, dict):
                continue
            for k in list(args.keys()):
                if str(k).lower() not in PATH_ARGS:
                    continue
                v = args.get(k)
                if not isinstance(v, str) or not v.strip():
                    continue
                v = v.strip()
                if _ABS_RE.match(v):
                    continue                    # 已经是绝对路径，不动
                if v in (".", ".."):
                    continue
                # 相对路径 -> 拼到本机根下（统一正斜杠）
                args[k] = root + "/" + v.replace(chr(92), "/").lstrip("/")
                n += 1
        except BaseException:            # noqa: BLE001
            continue
    return n


# ===== 按组解析路径：7 个文件跟着组走（2026-10-01 第458步）=====
#
# 用户口径：
#   「每个剪辑目录下每个目录干什么的都有分类」
#   「代码中 可能有没分类的目录 到时候迁移是个问题 有硬编码 或者没有在剪辑目录下」
#
# ## 扫描结果
#
# 桥里 23 处硬编码绝对路径，按迁移会不会断分三类：
#
#   A 该跟组走（7 个文件 / 80 处引用）—— 真障碍
#       _relay_events / _relay_prompts / _relay_msgs / _relay_replies
#       _conclusions / _pending.md / _steps.md
#   B 全局的（4 个）—— 不能跟组走
#       _pool_groups（它本身就是所有组的表）/ _group_sessions /
#       _group_roots / _empty_policy
#       跟组走会变成鸡生蛋：要读它才知道该去哪读它。
#   C dsh 自己的（5 处）—— 跟分组无关，换机靠重装 dsh，不是搬分组包
#
# ## 为什么不做成常量
#
# A 类共 80 处引用。把 80 处都改成查组再拼路径是灾难：每处都要传组名、
# 每处都可能漏。所以让**解析发生在取路径这一刻**，调用形态只变一个词：
#
#   EVENT_FILE.read_text()   ->   grp_path(events, slug).read_text()
#
# ## 谁跟组、谁不跟
#
# 写：调用方一定知道是哪一组（emit_reply/save_raw_messages/conc_save
#     第一个参数就是 slug），显式传，不猜。
# 读：可能只读一组（交接），也可能要读全部（跨组统计 / 打包），
#     所以 grp_path() 的 slug 传空 = 回落桥根目录那份（兼容老数据）。
#
# ## 兼容
#
# 组目录下没有那个文件时，**回落桥根目录下那份** ——
# 迁移是渐进的：没搬的组照旧能用，搬了的组自动走新位置。
# 2026-10-01 第461步（用户口径「分组下准备建几个目录都存放什么文件 是不是
#   干净明了」）：**桥的运行记录统一放 _sys/ 子目录。**
#
# 为什么分层：原来桥写的（_relay_*.jsonl）和模型的产出（脚本/输出/临时）
# 混在同一层，一眼看不出归属 —— 模型不知道哪些能删、哪些碰不得，
# 人也不知道哪个文件是谁写的。分三层之后：
#
#   <组>\_sys\   桥的运行记录（只读，删了交接就瞎）
#   <组>\_bak\   备份
#   <组>\其余    模型的工作台
#
# 两个台账（_pending/_steps）**不进 _sys** —— 它们是桥和模型共写的，
# 放在组根目录下模型才好找（提示词里也是这么写的）。
GRP_SYS_DIR = "_sys"
# 2026-10-03（用户口径：「超过1000直接删掉，下次访问的时候没这个窗口
# 默认就传全文，也不用添加别的逻辑了」）：
# **组窗口的消息数上限。超了就删掉那个上游窗口。**
#
# 为什么看这个数：实测同一个号 020 的两个窗口 ——
#     智普清言  version=2894   history=4,104,256 字   <- 已废，上游返回空
#     020      version=12     history=43,576 字      <- 健康
# 差 94 倍。version 是上游给的窗口消息数，不用桥自己数、不受重启影响。
#
# 删掉之后不用加任何逻辑：桥原本就是「按 title 找不到就新建」，
# 新窗口为空 -> 默认传全文。
WINDOW_MAX_ROUNDS = 1000


GRP_KINDS = {
    "events": GRP_SYS_DIR + "/_relay_events.jsonl",
    "prompts": GRP_SYS_DIR + "/_relay_prompts.jsonl",
    "msgs": GRP_SYS_DIR + "/_relay_msgs.jsonl",
    "replies": GRP_SYS_DIR + "/_relay_replies.jsonl",
    "conclusions": GRP_SYS_DIR + "/_conclusions.jsonl",
    # 2026-10-01 第469步：pending/steps 也进 _sys/。
    # 原来这两个平铺在组目录下，而 events/prompts/msgs 在 _sys/ 下 —— 两套位置并存。
    # 我按 _sys/ 写文件，而 grp_path 找的是组目录根，于是 grp_path(steps,309)
    # 一路回落到桥根那份，**分组白做**。统一进 _sys/：桥的运行记录都在一起，
    # 组目录根留给模型的工作台。
    "pending": GRP_SYS_DIR + "/_pending.md",
    "steps": GRP_SYS_DIR + "/_steps.md",
    # 2026-10-02：**「★任务台账」也要跟组走。**
    # 它是「当前任务干到哪了」的唯一权威（_steps.md 是流水、_pending.md 是进程单），
    # 交接的第一节要读它。不登记在这里就只能硬编码路径 —— 那正是换电脑会断的病。
    "progress": GRP_SYS_DIR + "/_task_progress.md",
}

# 2026-10-01 第461步：**桥根目录下的老位置。**
#
# 实测踩到：GRP_KINDS 加了 _sys/ 前缀之后，回落值变成了
#   <桥根>/_sys/_relay_events.jsonl   <- 这个文件根本不存在
# 而老数据在
#   <桥根>/_relay_events.jsonl       <- 真正的历史在这里
# 于是**没设 root 的组会看不到自己的历史**（交接、结论句库全空）。
#
# 所以回落必须用老名字：_sys 只是**组目录内部**的分层，
# 桥根目录下那批文件（历史数据）位置不变。
GRP_LEGACY = {
    "events": "_relay_events.jsonl",
    "prompts": "_relay_prompts.jsonl",
    "msgs": "_relay_msgs.jsonl",
    "replies": "_relay_replies.jsonl",
    "conclusions": "_conclusions.jsonl",
    "pending": "_pending.md",
    "steps": "_steps.md",
    "progress": "_task_progress.md",
}


def grp_path(kind, slug="", create=False):
    """这个（组, 文件）该读写的路径。

    kind 见 GRP_KINDS；slug 空 = 桥根目录下那份（全局/兼容）。
    create=True 时组目录不存在就建（写入侧用）。

    回落规则：组目录下已经有这个文件 -> 用它；没有 -> 用桥根那份。
    迁移因此是渐进的：搬过的组走新位置，没搬的照旧。
    """
    try:
        name = GRP_KINDS.get(str(kind))
        if not name:
            return None
        base = pathlib.Path(str(MSGS_FILE)).parent
        # 回落用**老位置**（桥根目录下那个平铺的名字），不是 _sys/ 那份
        fallback = base / GRP_LEGACY.get(str(kind), name)
        s = str(slug or "").strip()
        if not s:
            return fallback
        g = group_name_of(s)
        if not g:
            return fallback
        d = group_dir_of(g, slug=s)
        if not d:
            return fallback
        tgt = pathlib.Path(d) / name
        if tgt.is_file():
            return tgt
        if create:
            try:
                tgt.parent.mkdir(parents=True, exist_ok=True)
            except OSError:
                return fallback
            return tgt
        return fallback
    except BaseException:            # noqa: BLE001
        try:
            return (pathlib.Path(str(MSGS_FILE)).parent
                    / GRP_LEGACY.get(str(kind), ""))
        except BaseException:
            return None




# 组目录里模型用的六个工作台子目录。桥**预先建好** ——
# 理由：报错成本高于多几个空目录。模型拿到就能写，不用先处理环境。
GRP_WORK_DIRS = ("_临时", "脚本", "输出", "素材", "归档", "_bak")


def grp_ensure(group_name, slug="", root=""):
    """把这个组的工作目录骨架建出来。返回 (ok, 组目录或错因)。

    2026-10-01 第461步（用户口径「必须要清晰明了 做完美以后在重启」）：
    提示词里承诺了七个目录，但桥只建 _sys/（它自己要写的那份）。
    自检发现：说了的 _bak/_临时/脚本/输出/素材/归档 **一个都没建** ——
    模型真去写就成了「目录不存在」。

    桥替模型建好，理由是**报错成本高于多几个空目录**：
    模型拿到目录就能用，不用先花一轮 Test-Path + New-Item。
    """
    try:
        d = group_dir_of(group_name, slug=slug, root=root)
        if not d:
            return False, "算不出组目录（根未定）"
        base = pathlib.Path(d)
        base.mkdir(parents=True, exist_ok=True)
        for sub in GRP_WORK_DIRS:
            (base / sub).mkdir(parents=True, exist_ok=True)
        (base / GRP_SYS_DIR).mkdir(parents=True, exist_ok=True)
        return True, d
    except BaseException as exc:            # noqa: BLE001
        return False, "%s: %s" % (type(exc).__name__, str(exc)[:120])


def has_tool(tools, name):
    """这一轮 dsh 给没给某个工具。

    2026-10-01 第462步（用户实测「在ptc模式下发切组报错提问」）：
    **PTC 模式下 dsh 只暴露 run_code 一个工具**，报错原文：
        Error: unknown tool "ask_user_question":
        only `run_code` is callable directly
    所以桥造 tool_call 之前必须先看这个工具在不在本轮的工具表里 ——
    不在就别发，发了就是一个红叉（用户看到报错，桥白绕一轮）。
    """
    try:
        for t in (tools or []):
            fn = t.get("function") if isinstance(t, dict) else None
            fn = fn or (t if isinstance(t, dict) else {})
            if str(fn.get("name") or "") == str(name):
                return True
    except BaseException:            # noqa: BLE001
        pass
    return False


def ws_from_messages(messages):
    """从这一轮请求里读回「dsh 的工作区路径」。

    2026-10-01 第462步。桥上一轮回了一个 pwsh 取工作区，dsh 执行完会把
    结果作为**工具结果**放进下一轮请求 —— 这个函数就是从那儿把它捞出来。

    只认我们自己发的那条命令的痕迹（_WS_MARK 标记），不误抓模型自己
    跑 Get-Location 的结果。捞不到返回空串 —— 调用方据此什么都不做，
    **绝不用桥自己的目录顶替**（那就是「落在桥身上」，而 dsh 跟桥是两套）。
    """
    try:
        for m in (messages or []):
            if not isinstance(m, dict):
                continue
            if str(m.get("role") or "") not in ("tool", "function", "user"):
                continue
            t = _text_of(m.get("content"))
            if _WS_MARK not in t:
                continue
            re_pat = r'([A-Za-z]:[/' + chr(92) + chr(92) + r'][^"' + chr(39) + r'<>|*?:,)' + chr(93) + r']{1,120})'
            for mm in re.finditer(re_pat, t):
                cand = _norm_root(mm.group(1).rstrip("." + chr(92) + "r" + chr(92) + "n" + chr(34) + chr(39)))
                if cand:
                    return cand
        return ""
    except BaseException:
        return ""


# 每个 kind 的「全部可能位置」——**不做选择，全读**。
#
# 2026-10-01 第464步（自己当接手方自检时发现，用户口径「你自己想办法」）：
#
# 这个洞的性质值得记下来：**它是「路径选择」逻辑的固有风险。**
# 原来写的是「组目录有就用它，没有才回落桥根」——每步逻辑都对，
# 但组目录一旦被建出来（哪怕只有今天 4 条新记录），
# 桥根下那 4564 条历史就**永远读不到了**，而且**不报错、不留痕**。
# 交接从 4871 字掉到 1772 字，三节全空，只有站在接手方逐条核对才发现。
#
# 堵法不是「记得两份都读」（那是靠人记住规则，迟早再漏），
# 而是：**列出所有可能的位置，一个不落地读；再用自检确认没漏。**
def grp_all_paths(kind):
    """这个 kind 的数据**可能存在的所有位置**（存在的才算）。

    slug 为空时也一样列全 —— 全局视角要看的是「所有地方」。
    """
    out = []
    try:
        name = GRP_KINDS.get(str(kind))
        legacy = GRP_LEGACY.get(str(kind), name)
        if not name:
            return out
        base = pathlib.Path(str(MSGS_FILE)).parent
        # 1) 桥根下（老位置，永远在里面）
        out.append(base / legacy)
        # 2) 每个组目录下（新位置）
        try:
            for g in (_groups_root().get("groups") or []):
                d = group_dir_of(str(g.get("name") or ""), slug="")
                if d:
                    out.append(pathlib.Path(d) / name)
        except BaseException:            # noqa: BLE001
            pass
    except BaseException:            # noqa: BLE001
        pass
    # 去重 + 只留存在的
    seen, keep = set(), []
    for p in out:
        try:
            if p in seen or not p.is_file():
                continue
            seen.add(p)
            keep.append(p)
        except BaseException:            # noqa: BLE001
            continue
    return keep


def grp_coverage(kind):
    """自检：这个 kind 的数据分布在几处、各多少行。给排查用。

    用它就能一眼看出「有没有哪份被漏掉」——比靠人记规则可靠。
    """
    out = []
    for p in grp_all_paths(kind):
        try:
            n = sum(1 for ln in p.read_text(encoding="utf-8",
                                            errors="replace").splitlines()
                    if ln.strip())
        except OSError:
            n = -1
        out.append({"path": str(p), "lines": n})
    return out


def grp_read(kind, slugs=(), limit=0):
    """按组读一个文件 —— **桥自己判断读哪个文件、要不要合并**。

    用户口径：「后期要桥能自动区分」。

    判据就是 slug（每个 reader 本来就收 slugs/slug）：
      1) 从 slugs 推出涉及的组（group_name_of）
      2) 只有一组 -> 读那个组目录下的文件；没有就回落桥根那份
      3) 多组 -> 每个组各读一份再合并（同一路径只读一次）
      4) slugs 为空 -> 读桥根那份（全局视角：统计 / 打包用）

    调用方不用知道文件在哪，只说「我要什么、跟哪些号有关」。
    没搬过数据的组自动走桥根那份 —— 迁移是渐进的。
    """
    out = []
    try:
        want = set(str(s) for s in (slugs or ()) if s)
        # 2026-10-01 第467步：**改成按组读，不再全读。**
        #
        # 第464步那条「不做路径选择、全读」是为了修「选错路径就静默丢数据」。
        # 它确实修好了漏读，但等数据按 slug 物理分好组之后，它变成了**串读**：
        # 问剪辑组也把 test 组那份一起读进来，隔离当场失效
        # （实测：grp_read('events', {309}) 返回 3398 条，其中混着 test 的）。
        #
        # 现在分清两件事：
        #   · 选路径 —— 由 slug 的组归属决定（下面 1~3 行）
        #   · 兜底   —— 组目录那份不存在时，回落桥根那份（迁移是渐进的，
        #              没搬过的组照样能读到）
        # 关键是**兜底只发生在该组目录为空时**，不再无条件全读。
        _paths = []
        if want:
            _gk = set()
            for _s in want:
                try:
                    _gn = group_name_of(_s)
                except BaseException:      # noqa: BLE001
                    _gn = ''
                if _gn:
                    _gk.add(_gn)
            for _g in _gk:
                try:
                    _d = group_dir_of(_g, slug='')
                except BaseException:      # noqa: BLE001
                    _d = ''
                if not _d:
                    continue
                _f = pathlib.Path(_d) / GRP_KINDS.get(str(kind), str(kind))
                if _f.is_file():
                    _paths.append(_f)
        if not _paths:
            # 该组还没搬过数据 -> 回落桥根那份（以及任何存在的旧位置）
            _paths = grp_all_paths(kind)
        else:
            # 组目录已存在，但桥根那份可能还有这个组的旧行没搬完 ——
            # 只补桥根那一份，**不补别的组的目录**（那才是串读）。
            _base = pathlib.Path(str(MSGS_FILE)).parent / GRP_LEGACY.get(
                str(kind), GRP_KINDS.get(str(kind), str(kind)))
            if _base.is_file() and _base not in _paths:
                _paths.append(_base)
        for p in _paths:
            try:
                txt = p.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            lines = txt.splitlines()
            if limit and len(lines) > limit:
                lines = lines[-limit:]
            for ln in lines:
                ln = ln.strip()
                if not ln:
                    continue
                try:
                    j = json.loads(ln)
                except ValueError:
                    continue
                if want and str(j.get("slug") or "") not in want:
                    continue
                out.append(j)
        return out
    except BaseException:
        return out


# ===== 分组打包：换电脑时把整组数据搬过去（2026-10-01 第456步）=====
#
# 用户口径：「以后换电脑 直接把分组的数据打包过去就可以了」。
#
# ## 为什么需要专门做
#
# 一个组的数据散在 8 个文件里，其中 6 个是**混着所有组**的 jsonl：
#   _pool_groups.json      分组定义      （含所有组）
#   _group_sessions.json   组窗口登记     （含所有组）
#   _group_roots.json      学到的组路径   （含所有组）
#   _relay_msgs.jsonl      客户端原样消息  （混）
#   _relay_prompts.jsonl   桥发出的 prompt （混）
#   _relay_replies.jsonl   回复全文       （混）
#   _relay_events.jsonl    事件流         （混）
#   _conclusions.jsonl     结论句库       （混）
#
# 直接整个拷过去会把**别的组**一起带过去，那正是「串窗口」的来源。
# 所以要能按组切。
#
# ## 切分依据
#
# 每个 jsonl 行都带 slug（实测 6407 行 0 解析失败），
# 而 slug→组 的映射在 _pool_groups.json 里。按这个切，不会漏也不会串。
#
# ## 包里放什么
#
#   manifest.json   包的元信息：组名、号、导出时间、源机器、各文件行数
#   group.json      这个组的分组定义 + 组窗口登记 + 学到的路径
#   *.jsonl         按组过滤后的各类记录
#
# **不含**：账号凭据（ds_auth.json）—— 那是机器级的，换机器要重新登录。
PACK_DIR_NAME = "_packs"


def _jsonl_split(path, slugs, keep_keys=None):
    """把一个 jsonl 按 slug 过滤出来。返回 (保留行, 总行, 命中行)。"""
    out, total, hit = [], 0, 0
    try:
        p = pathlib.Path(str(path))
        if not p.is_file():
            return out, 0, 0
        want = set(str(s) for s in (slugs or ()))
        for ln in p.read_text(encoding="utf-8", errors="replace").splitlines():
            ln = ln.strip()
            if not ln:
                continue
            total += 1
            try:
                j = json.loads(ln)
            except ValueError:
                continue
            if str(j.get("slug") or "") in want:
                hit += 1
                # keep_keys 用于瘦身：只留指定字段（回复全文太大时用）
                if keep_keys:
                    j = dict((k, j[k]) for k in keep_keys if k in j)
                out.append(j)
    except BaseException:            # noqa: BLE001
        pass
    return out, total, hit


def group_pack(group_name, out_dir="", with_bodies=False):
    """把一个组打包成一个目录。返回 (ok, 路径或错因)。

    with_bodies=False 时不带回复全文（_relay_replies 的 text 字段动辄几万字），
    只带它的摘要字段 —— 因为交接用的是结论句库和事件流，不是全文。
    需要全文时显式传 True。
    """
    try:
        g = str(group_name or "").strip()
        if not g:
            return False, "没指定分组名"
        slugs = []
        gdef = None
        try:
            for gg in (_groups_root().get("groups") or []):
                if str(gg.get("name") or "") == g:
                    gdef = gg
                    slugs = [str(x) for x in (gg.get("slugs") or [])]
                    break
        except BaseException:            # noqa: BLE001
            pass
        if gdef is None:
            return False, "没找到这个分组：%s" % g
        base = pathlib.Path(out_dir) if out_dir else pathlib.Path(".")
        dst = base / PACK_DIR_NAME / (g + "_" + time.strftime("%Y%m%d_%H%M"))
        dst.mkdir(parents=True, exist_ok=True)
        stats = {}
        # 1) 分组定义 + 组窗口登记 + 学到的路径：只取这一组
        meta = {}
        try:
            gs = json.loads(GROUPS_FILE.read_text(encoding="utf-8"))
            meta["group_def"] = gdef
            others = [x for x in (gs.get("groups") or [])
                      if str(x.get("name") or "") != g]
            meta["other_group_names"] = [str(x.get("name") or "") for x in others]
        except BaseException:            # noqa: BLE001
            pass
        try:
            reg = json.loads(GROUP_SESS_FILE.read_text(encoding="utf-8"))
            meta["windows"] = reg.get(g) or {}
        except BaseException:            # noqa: BLE001
            pass
        try:
            roots = _grp_root_all()
            meta["root_learned"] = roots.get(g) or {}
        except BaseException:            # noqa: BLE001
            pass
        # 2) 各类 jsonl 按 slug 切
        jobs = [
            ("msgs", B_MSGS := MSGS_FILE, None),
            ("prompts", MSGLOG_FILE, None),
            ("events", EVENT_FILE, None),
            ("conclusions", CONC_FILE, None),
            ("replies", REPLY_FILE,
             None if with_bodies else
             ("t", "ts", "k", "slug", "sid", "chars", "think",
              "turns_here", "prompt", "whole", "fragments", "images",
              "ask_head")),
        ]
        for tag, src, keep in jobs:
            rows, total, hit = _jsonl_split(src, slugs, keep)
            stats[tag] = {"total": total, "kept": hit}
            if rows:
                fp = dst / (tag + ".jsonl")
                with fp.open("w", encoding="utf-8") as f:
                    for r in rows:
                        f.write(json.dumps(r, ensure_ascii=False,
                                           default=str) + chr(10))
        manifest = {"group": g,
                    "slugs": slugs,
                    "exported_at": time.strftime("%Y-%m-%d %H:%M:%S"),
                    "with_bodies": bool(with_bodies),
                    "stats": stats,
                    "note": "不含账号凭据；换机器要重新登录"}
        (dst / "group.json").write_text(
            json.dumps(meta, ensure_ascii=False, indent=1), encoding="utf-8")
        (dst / "manifest.json").write_text(
            json.dumps(manifest, ensure_ascii=False, indent=1),
            encoding="utf-8")
        return True, str(dst)
    except BaseException as exc:            # noqa: BLE001
        return False, "%s: %s" % (type(exc).__name__, str(exc)[:150])


def group_unpack(pack_dir, new_root=""):
    """把打包好的组落到本机。返回 (ok, 说明)。

    做三件事：
      1) 分组定义并进 _pool_groups.json（重名就更新，不重复加）
      2) 组窗口登记并进 _group_sessions.json —— **但 sid 是旧机器的窗口**，
         在新机器上多半无效，所以保留（按名字找得到就复用），
         找不到桥会自己重建。
      3) 各类 jsonl **追加**进本机对应文件（不覆盖，去重按行的 t+slug）

    new_root 非空时同时把组路径改成它 —— 换电脑时就是这一步。
    """
    try:
        p = pathlib.Path(str(pack_dir))
        if not p.is_dir():
            return False, "找不到这个包目录：%s" % pack_dir
        man = {}
        try:
            man = json.loads((p / "manifest.json").read_text(encoding="utf-8"))
        except BaseException:            # noqa: BLE001
            return False, "包里没有 manifest.json（不是分组包？）"
        g = str(man.get("group") or "").strip()
        slugs = [str(x) for x in (man.get("slugs") or [])]
        if not g:
            return False, "manifest 里没写组名"
        done = []
        # 1) 分组定义
        try:
            meta = json.loads((p / "group.json").read_text(encoding="utf-8"))
        except BaseException:            # noqa: BLE001
            meta = {}
        gdef = meta.get("group_def") or {"name": g, "slugs": slugs}
        try:
            raw = json.loads(GROUPS_FILE.read_text(encoding="utf-8"))
            if not isinstance(raw, dict):
                raw = {"meta_mode": "pin", "default": "", "groups": []}
        except BaseException:            # noqa: BLE001
            raw = {"meta_mode": "pin", "default": "", "groups": []}
        gs = raw.get("groups")
        if not isinstance(gs, list):
            gs = []
        gdef = dict(gdef)
        gdef["name"] = g
        gdef["slugs"] = slugs
        # 2026-10-01 第456步：**换机导入时路径要写进组定义，不能只写学习记录。**
        # 实测踩到：new_root 只进了 _group_roots.json，组定义里 root 还是 None。
        # 结果 group_root_of() 能算对（走学习记录），但那条链是脆的 ——
        # 学习记录被清掉 / 换台机器没这份文件，就退化成"自适应"，路径丢了。
        # 组定义是**跟着包一起搬**的，写在这里才搬得走。
        if new_root:
            gdef["root"] = _norm_root(new_root)
        elif not gdef.get("root"):
            gdef["root"] = ""
        gs = [x for x in gs
              if not (isinstance(x, dict)
                      and (str(x.get("name") or "") == g
                           or str(x.get("id") or "") == g))]
        gs.append(gdef)
        raw["groups"] = gs
        tmp = GROUPS_FILE.with_name(GROUPS_FILE.name + ".tmp")
        tmp.write_text(json.dumps(raw, ensure_ascii=False, indent=1),
                       encoding="utf-8")
        tmp.replace(GROUPS_FILE)
        done.append("分组定义")
        # 2) 组窗口登记
        wins = meta.get("windows") or {}
        if wins:
            try:
                reg = json.loads(GROUP_SESS_FILE.read_text(encoding="utf-8"))
                if not isinstance(reg, dict):
                    reg = {}
                cur = reg.setdefault(g, {})
                for s, sid in wins.items():
                    cur.setdefault(str(s), str(sid))
                _t = GROUP_SESS_FILE.with_suffix(".json.tmp")
                _t.write_text(json.dumps(reg, ensure_ascii=False, indent=1),
                              encoding="utf-8")
                _t.replace(GROUP_SESS_FILE)
                done.append("窗口登记(%d)" % len(wins))
            except BaseException:        # noqa: BLE001
                pass
        # 3) 学到的路径
        lr = meta.get("root_learned") or {}
        if lr:
            try:
                with _grp_root_lock:
                    d = _grp_root_all()
                    d[g] = lr
                    if new_root:
                        d[g]["root"] = _norm_root(new_root)
                    _t = GRP_ROOT_FILE.with_suffix(".json.tmp")
                    _t.write_text(json.dumps(d, ensure_ascii=False, indent=1),
                                  encoding="utf-8")
                    _t.replace(GRP_ROOT_FILE)
                done.append("路径")
            except BaseException:        # noqa: BLE001
                pass
        # 4) 各类 jsonl 追加（按 t+slug 去重）
        jl = [("msgs", MSGS_FILE), ("prompts", MSGLOG_FILE),
              ("events", EVENT_FILE), ("conclusions", CONC_FILE),
              ("replies", REPLY_FILE)]
        for tag, dst in jl:
            src = p / (tag + ".jsonl")
            if not src.is_file():
                continue
            try:
                seen = set()
                if dst.is_file():
                    for ln in dst.read_text(encoding="utf-8",
                                             errors="replace").splitlines():
                        ln = ln.strip()
                        if not ln:
                            continue
                        try:
                            j = json.loads(ln)
                        except ValueError:
                            continue
                        seen.add((str(j.get("t") or ""),
                                  str(j.get("slug") or "")))
                add = 0
                with dst.open("a", encoding="utf-8") as f:
                    for ln in src.read_text(encoding="utf-8",
                                            errors="replace").splitlines():
                        ln = ln.strip()
                        if not ln:
                            continue
                        try:
                            j = json.loads(ln)
                        except ValueError:
                            continue
                        k = (str(j.get("t") or ""),
                             str(j.get("slug") or ""))
                        if k in seen:
                            continue
                        seen.add(k)
                        f.write(ln + chr(10))
                        add += 1
                if add:
                    done.append("%s(+%d)" % (tag, add))
            except BaseException:        # noqa: BLE001
                pass
        return True, "装好了「%s」：%s" % (g, "、".join(done))
    except BaseException as exc:            # noqa: BLE001
        return False, "%s: %s" % (type(exc).__name__, str(exc)[:150])


# ===== 组目录落地：切组时把该组的数据搬进 <根>/<组名>/（第457步）=====
#
# 用户口径：
#   「数据是手动签」       -> 不用桥自动分文件，是明确落一次
#   「更新路径是在切组那更新一次就好了」-> 只在切组/改路径时搬，不是每轮查
#   「都是同样的格式 目录 只是上游目录换了而已」-> 结构不变，只换根
#
# ## 布局（切过去之后）
#
#   <根>/<组名>/
#        _relay_msgs.jsonl       本组的客户端原样消息
#        _relay_prompts.jsonl    本组发出的 prompt
#        _relay_replies.jsonl    本组回复全文
#        _relay_events.jsonl     本组事件流
#        _conclusions.jsonl      本组结论句库
#
# **格式跟桥根目录下的那些一模一样** —— 只是换了目录。
# 所以读取端不用改：给个根，拼同样的文件名就行。
#
# ## 为什么是搬不是拷
#
# 用户口径「数据是手动签」= 这是明确的一次动作，不是后台同步。
# 搬完桥根目录下那份就没了，不会两边都有、以后对不上。
# 搬之前先备份到 <根>/<组名>/_bak/。
#
# ## 各文件怎么切
#
# 5 个 jsonl 都带 slug（实测 6407 行 0 解析失败），按组里的号过滤。
#
# ## 幂等
#
# 重复切同一组不会重复搬：目标文件已存在时**合并去重**（按 t+slug），
# 不是覆盖也不是重复追加。
# 2026-10-01 第460步（用户口径）：**砍掉桥自己搬数据。**
#
# 「根本不用真搬 搬也是手动切组以后把这台电脑的剪辑目录复制到另一个
#   dsh 工作目录下在更新路径」
#
# 流程只有三步，全是用户可控的动作：
#   1. 手动切组（一次）
#   2. 把这台电脑的 <组名> 目录整个复制到另一台的 dsh 工作目录下
#      （复制文件夹是资源管理器的事，比桥写代码搬可靠，而且看得见）
#   3. ##切组## -> 【更新组路径】（一次）
#
# 所以 group_move_data() 那套删了。留着反而有害：桥会顺手搬一半，
# 用户复制完发现两边都有、或者搬了一半 —— 比不搬更难查。
# **搬是人的动作，桥只认路径。**
#
# 桥要做的只有：grp_path() 按根解析 + 目录不存在就回落桥根那份
# （迁移期间没复制过去的组照旧能用）。
# ===== 回复全记录：不管上游回什么，都原样留一份 =====
# 事件流（_relay_events.jsonl）只留摘要，正文写这里。
# 为什么分开：正文动辄几万字，混进事件流会把 8 MiB 的轮转窗口撑爆，
# 后面想按时间翻事件就翻不动了。这里单独轮转，32 MiB 一档。
REPLY_FILE = _conf_file("_relay_replies.jsonl")   # 落 ds/ —— 见 MARKS_FILE 注

# ===== 原始 messages 存档（2026-10-01 第442步）=====
# 用户口径：「你先看看怎么存怎么去燥」
#
# 为什么存：上游窗口里存的是**桥拼好的整个 prompt**（含台账/规则/格式提醒），
# 不是客户端真正发来的对话。要做「换号只发新号缺的、且干净」，必须有原始数据。
#
# 存什么：客户端原样发来的 messages（req["messages"]），**不做任何加工**。
# 为什么不存 prompt：prompt 是拼装结果，反向解析不可靠。
#
# 一行一轮：{"t", "ts", "slug", "model", "ntools", "messages":[...]}
MSGS_FILE = _conf_file("_relay_msgs.jsonl")
MSGS_MAX = 16 * 1024 * 1024        # 超过就整份轮转
MSGS_TAIL = 600                     # 读的时候最多看最后这么多行
_msgs_lock = threading.Lock()


def save_raw_messages(messages, slug="", model="", tools=None):
    """把这一轮客户端发来的 messages 原样落一行。失败绝不冒泡。

    存的是**原样** —— 去噪是读取时的事，存储不替调用方做决定。
    """
    try:
        if not isinstance(messages, list) or not messages:
            return
        row = {"t": time.time(),
               "ts": time.strftime("%H:%M:%S"),
               "slug": str(slug or ""),
               "model": str(model or ""),
               "ntools": len(tools or []),
               "messages": messages}
        line = json.dumps(row, ensure_ascii=False, default=str) + chr(10)
        # 2026-10-01 第458步：**写进本组自己的文件。**
        # 用户口径「所有的都要在组内完成 后期不串组不污染环境」。
        # grp_path 会在组目录不存在时回落桥根那份 —— 没搬过的组照旧能用。
        _f = grp_path("msgs", slug, create=True) or MSGS_FILE
        with _msgs_lock:
            try:
                if _f.stat().st_size > MSGS_MAX:
                    bak = _f.with_name(
                        _f.name + "." + time.strftime("%Y%m%d_%H%M%S"))
                    _f.rename(bak)
                    old = sorted(_f.parent.glob(_f.name + ".*"),
                                 key=lambda x: x.stat().st_mtime)
                    for x in old[:-3]:
                        x.unlink()
            except OSError:
                pass
            with _f.open("a", encoding="utf-8") as f:
                f.write(line)
    except BaseException:            # noqa: BLE001
        pass

# ===== 增量上下文：用真实历史取代代写交接（2026-10-01 第443步）=====
# 用户口径：「同样的历史不在重发 只发他不知道的 而且去燥」
#           「基于当前上游id的最近上下文 来定位发多少给他 把历史去燥给他」
#
# ## 为什么（实测依据）
#
# 四个剪辑组窗口的「最后一发」两两对比：
#   113 vs 779:  相同 214 / 220 行，差异  7 (3%)
#   113 vs 310:  相同 212 / 220 行，差异  9 (4%)
#   309 vs 779:  相同 190 / 236 行，差异 46 (19%)
#
# **四个窗口里 96% 是同一份内容的副本。** 差异全来自「上一棒是谁」那句代写交接。
#
# 所以代写交接做了三件坏事：
#   1. 让四个窗口各自不同 —— 破坏「缓存一致」
#   2. 每轮重复 —— 是噪声源
#   3. 占约 40% 体积 —— 纯浪费
#
# ## 怎么做
#
# 拿「客户端真正发来的 messages」（_relay_msgs.jsonl）当来源，
# 而不是桥自己拼的交接文本。同组的号做同样的活，看到同样的历史。
#
# ## 开关
#
# _empty_policy.json 的 delta_context（默认 False）。
# 关着时一个字节都不执行，行为与改动前完全一致。


def _msg_text(m):
    """一条 message 的纯文本（content 可能是 str 或分段数组）。"""
    c = m.get("content") if isinstance(m, dict) else None
    if isinstance(c, str):
        return c
    if isinstance(c, list):
        parts = []
        for seg in c:
            if isinstance(seg, str):
                parts.append(seg)
            elif isinstance(seg, dict):
                parts.append(str(seg.get("text") or ""))
        return " ".join(parts)
    return ""


# 桥自己拼的注入抬头。见到就整条丢 —— 它们是元数据不是对话。
INJECT_HEADS = ("【交接缺口", "【本地代写交接】", "【当前步骤", "【台账",
                "【系统】", "〔上下文.txt", "〔台账文件", "〔当前步骤")


def _is_injected(t):
    """这条消息是不是桥自己拼进去的（不是真实对话）。"""
    s = (t or "").lstrip()
    return any(s.startswith(h) for h in INJECT_HEADS)


def denoise_messages(msgs, keep_tools=True):
    """把一段 messages 去噪成「真实对话」。

    规则（都来自实测，不是猜的）：
      1. system 段 -> 丢。那是环境注入（AGENTS.md / runtime context），不是对话。
      2. 桥拼的注入（【交接缺口】等）-> 整条丢。
      3. 连续重复的同一段 -> 只留最后一次。
      4. 工具结果 -> 默认保留（它是真实产出），但压掉重复调用的。
    返回 [{"role", "content"}]。
    """
    out = []
    seen = {}
    for m in (msgs or []):
        if not isinstance(m, dict):
            continue
        role = str(m.get("role") or "")
        if role == "system":
            continue
        t = _msg_text(m)
        if not t.strip():
            continue
        if _is_injected(t):
            continue
        key = (role, t)
        # 同一段只留最后一次（重复投递没有增量价值）
        if key in seen:
            out[seen[key]] = None
        seen[key] = len(out)
        out.append({"role": role, "content": t})
    return [x for x in out if x is not None]


def build_delta_context(group, slug, n_rounds=8, budget=24000):
    """给这个组、这个号，攒一份「真实的最近上下文」。

    来源：_relay_msgs.jsonl —— 客户端**原样发来**的 messages，未经桥加工。
    取同组最近 n_rounds 轮，去噪后拼起来，按 budget 裁。

    取「同组」而不是「本号」：同组的号在做同一件事，
    谁跑过都算数（这正是「缓存一致 => 内容共享」）。

    读不到返回空串 —— 调用方据此退回原行为。
    """
    try:
        if not (grp_path("msgs", slug) or MSGS_FILE).is_file():
            return ""
        g = str(group or "").strip()
        if not g:
            return ""
        # 2026-10-01 第443步修：pool_groups(slugs=()) 是按**在线账号**过筛的，
        # 裸调返回空。这里要的是配置里的组成员，直接读 _groups_root()。
        members = set()
        try:
            for gg in (_groups_root().get("groups") or []):
                if str(gg.get("name") or "") == g:
                    members.update(str(s) for s in (gg.get("slugs") or []))
                    break
        except Exception:            # noqa: BLE001
            pass
        if not members:
            members = {str(slug or "")}
        with _msgs_lock:
            _mf = grp_path("msgs", slug, create=True) or MSGS_FILE
        raw = _mf.read_text(encoding="utf-8", errors="replace")
        picked = []
        for ln in raw.splitlines():
            ln = ln.strip()
            if not ln:
                continue
            try:
                j = json.loads(ln)
            except ValueError:
                continue
            if str(j.get("slug") or "") not in members:
                continue
            picked.append(j)
        if not picked:
            return ""
        # 2026-10-01 第443步：跨轮去重。
        # 实测：客户端每一轮把**整段历史**重发一遍（第4轮的 messages 里含
        # 第1、2、3轮的原文，逐字相同）。所以「取最后 3 轮再拼」= 同一份内容
        # 出现 3 次。这里先按「每轮只取本轮新增」压一遍，再交给 denoise。
        merged = []
        _seen_round = set()
        for j in picked[-n_rounds:]:
            for m in (j.get("messages") or []):
                t = _msg_text(m)
                sig = (str(m.get("role") or ""), t)
                if sig in _seen_round:
                    continue
                _seen_round.add(sig)
                merged.append(m)
        clean = denoise_messages(merged)
        if not clean:
            return ""
        lines = []
        for m in clean:
            who = "用户" if m["role"] == "user" else "助手"
            lines.append("[" + who + "] " + m["content"].rstrip())
        body = (chr(10) + chr(10)).join(lines)
        if len(body) > budget:
            body = body[-budget:]
        return ("【本组最近的真实上下文 —— 这些是实际发生过的，不是摘要】" + chr(10)
                + body)
    except BaseException:            # noqa: BLE001
        return ""

# ===== 台账底座：每轮落盘桥发出的 prompt 正文 + 上游步号（2026-10-01 第444步）=====
#
# 用户口径：「数据表 台账上下功夫了 每次缓存他到哪一个数字了 就知道他缺多少了」
#
# ## 为什么落在这里
#
# `_ask()` 是**唯一发送收口**（Phase A Safety Gate 定的）。所有真正打上游的
# prompt 都从这儿过，客户端发的、附件、检查点、交接，一个不漏。
#
# ## 为什么必须存正文
#
# 实测（第443步）：本地现有的三份数据都没有正文 ——
#   sessions.json        每步只记 32 位 md5，数得出步数，还原不出内容
#   _relay_events.jsonl  只记 head 400 + tail 200，覆盖率 0.76%
#   _relay_msgs.jsonl    只有**客户端发来的**，没有桥拼好的
# 而换号要「补上缺的那几步」，缺的就是正文。桥拼好的 prompt 是唯一
# 完整存在过的形态，发完就丢 —— 这里把它接住。
#
# ## 数字口径
#
# 用**上游 message_id** 当步号，不用本地 md5。原因：
#   上游 message_id 每个窗口从 1 开始、奇数递增，**跨号天然可比**；
#   本地 keys 是内容 md5，四个号各跑各的，共有步数只有 11-14，量不出缺口。
# 实测六个窗口：309 到 17、779/113/310 到 9、020 到 65、483 到 25。
#
# ## 代价
#
# 一行 = 一份完整 prompt（实测 24k~82k 字）。按 16 MiB 轮转，
# 保留最近 3 份备份 —— 和 _relay_msgs 同一套规矩。
MSGLOG_FILE = _conf_file("_relay_prompts.jsonl")   # 落 ds/ —— 见 MARKS_FILE 注
MSGLOG_MAX = 16 * 1024 * 1024
_msglog_lock = threading.Lock()


def save_sent_prompt(prompt, slug="", sid="", mid=None, model="",
                     kind="ask", extra=None):
    """把这一轮真正发出去的 prompt 正文落一行。失败绝不冒泡。

    mid 是上游回的 message_id —— **这就是台账上的那个数字**。
    还没回来时（发送前）mid 为 None，回来后再补一行 update 把数字补上。
    """
    try:
        # 2026-10-01 第444步修：stamp 行**没有正文**（它只补一个步号），
        # 原来这里无条件 return，把自己的补记挡住了 —— 台账里 mid 永远是
        # None，数字补不上。改成：stamp 允许空正文，其它 kind 才要求有正文。
        _k = str(kind or "")
        if not prompt and _k != "stamp":
            return
        row = {"t": round(time.time(), 3),
               "ts": time.strftime("%H:%M:%S"),
               "slug": str(slug or ""),
               "sid": str(sid or ""),
               "mid": mid,
               "model": str(model or ""),
               "kind": str(kind or ""),
               "chars": len(prompt),
               "prompt": prompt}
        if extra:
            row.update(extra)
        line = json.dumps(row, ensure_ascii=False, default=str) + chr(10)
        # 2026-10-01 第458步：写进本组的 prompts 文件（同 save_raw_messages）
        _f = grp_path("prompts", slug, create=True) or MSGLOG_FILE
        with _msglog_lock:
            try:
                if _f.stat().st_size > MSGLOG_MAX:
                    bak = _f.with_name(
                        _f.name + "." + time.strftime("%Y%m%d_%H%M%S"))
                    _f.rename(bak)
                    old = sorted(_f.parent.glob(_f.name + ".*"),
                                 key=lambda x: x.stat().st_mtime)
                    for x in old[:-3]:
                        x.unlink()
            except OSError:
                pass
            with _f.open("a", encoding="utf-8") as f:
                f.write(line)
    except BaseException:            # noqa: BLE001
        pass


def ledger_of(slug="", sid=""):
    """台账：这个（号, 窗口）缓存到第几号了。返回最后一次的 mid。

    只读最后 N 行 —— 整份可能很大，不为了一个数字全读。
    """
    try:
        # 2026-10-01 第458步：读**本组**的 prompt 流水（不隔离会读到别组的数）
        _pf = grp_path("prompts", slug, create=True) or MSGLOG_FILE
        if not _pf.is_file():
            return None
        s = str(slug or "")
        d = str(sid or "")
        with _msglog_lock:
            size = _pf.stat().st_size
            step = 4 * 1024 * 1024
            with _pf.open("rb") as f:
                back = min(step, size)
                f.seek(size - back)
                raw = f.read().decode("utf-8", "replace")
        for ln in reversed(raw.splitlines()):
            ln = ln.strip()
            if not ln or not ln.startswith("{"):
                continue
            try:
                j = json.loads(ln)
            except ValueError:
                continue
            if s and str(j.get("slug") or "") != s:
                continue
            if d and str(j.get("sid") or "") != d:
                continue
            return j.get("mid")
        return None
    except BaseException:            # noqa: BLE001
        return None


def ledger_rows(limit=400, use="main"):
    """台账全表：每个（号, 窗口）当前到第几号。给界面和排查看。

    2026-10-01 第444步：**只出主对话窗口**（use="main"）。
    util 窗口（dsh 生成标题/摘要共用窗口）单列一档，混在一起会把
    「util 窗口轮换」误读成「主对话跳窗口丢上下文」—— 实测踩过。
    use="all" 出全部，每行带 use 字段。
    """
    try:
        # 2026-10-01 第458步：读全部组（界面要看全局）—— 每组各读一份再合并。
        # grp_read 传空 slugs = 全局那份；但为了让分组后的数据也进来，
        # 这里显式把**所有组的文件**都读一遍。
        _all = []
        try:
            for _g in (_groups_root().get("groups") or []):
                _d = group_dir_of(str(_g.get("name") or ""), slug="")
                if _d:
                    _q = pathlib.Path(_d) / GRP_KINDS["prompts"]
                    if _q.is_file() and _q not in _all:
                        _all.append(_q)
        except BaseException:
            _all = []
        if not _all and MSGLOG_FILE.is_file():
            _all = [MSGLOG_FILE]
        with _msglog_lock:
            raw = "".join(p.read_text(encoding="utf-8", errors="replace")
                             for p in _all if p.is_file())
        seen = {}
        for ln in raw.splitlines()[-limit:]:
            ln = ln.strip()
            if not ln or not ln.startswith("{"):
                continue
            try:
                j = json.loads(ln)
            except ValueError:
                continue
            _u = str(j.get("use") or "main")
            _sid_j = str(j.get("sid") or "")
            # 2026-10-01 第444步：sid 空的行是「开新会话那一发」的 ask 行 ——
            # 那时 session 还是 None，_ask() 拿不到 id。它没有身份，不进行缓存表
            # （否则台账里会多出一条「到第 None 号」的脏行）。
            if not _sid_j:
                continue
            # 老行没有 use 字段：判不了用途，当主对话
            key = (_u, str(j.get("slug") or ""), _sid_j)
            prev = seen.get(key)
            rec = {"use": _u, "slug": key[1], "sid": key[2],
                   "mid": j.get("mid"), "chars": j.get("chars"),
                   "ts": j.get("ts"), "kind": j.get("kind")}
            # 2026-10-01 第444步修：**要留最新的那个数字，不是第一个。**
            # 原来写的是「带 mid 的优先」，于是同一窗口第一条 stamp（14）
            # 把后面所有更大的（16…40）全挡住了 —— 台账显示的永远是历史
            # 最低水位，缺口会被算大。现在按行序取最后一个带 mid 的。
            # 没有 mid 的行（util 窗口的 ask 行、以及只在途没回来的一发）
            # 不进表 —— 台账只回答「缓存到第几号」，没有号的行不构成答案。
            if _sid_j and rec["mid"]:
                seen[key] = rec
        rows = list(seen.values())
        if use != "all":
            rows = [r for r in rows if r["use"] == use]
        # 2026-10-01 第447步（用户口径「不用删窗口…想用了本地建过来就又活了
        # 相当于云记录」）：**这里不再去上游核实窗口死活。**
        #
        # 我一度加过一层「拉上游窗口列表、把已删的行滤掉」——那是错的思路：
        # 它让台账的正确性挂在上游查得到查不到上面（实测 has_more 翻不到时
        # 就会把活的判成死的）。窗口是**云记录**：弃用了就躺在那里，
        # 哪天要用本地按名字把它找回来，它就又活了。不需要谁去维护生死。
        #
        # 台账只回答「本地记到第几号」，纯本地，一个字都不问上游。
        # 找窗口那一步（按名字）在 plan() 里做，那是使用时刻的事，
        # 不是台账时刻的事 —— 两件事分开。
        return sorted(rows, key=lambda r: (r["slug"], r["sid"]))
    except BaseException:            # noqa: BLE001
        return []



# ===== 本地账本：以「组名」为主键，窗口是云记录（2026-10-01 第447步）=====
#
# 用户口径：
#   「别管怎么轮 都是本地数据去校验他最后的上下文到哪里了缺了多少步 而把他补上」
#   「不用删窗口 因为这个窗口弃用了那天想用了本地建过来就又活了 相当于云记录」
#
# ## 为什么要单独一层
#
# 之前的台账（_relay_prompts.jsonl）是**流水**：每轮记一行完整 prompt。
# 它回答不了「这个组现在到第几步了」—— 得先扫全表、按 sid 聚合，而 sid
# 会随窗口重建而变，于是同一件事被记成好几条线（实测 309 有 3 条：
# 47c27cb4 到 40、686099c2 到 24、488aa8e9 到 26）。
#
# 账本要的是**局面**不是流水：一组一行，主键是组名（不是 sid ——
# sid 会变，组名不会，这正是「窗口名肯定不会变」那句）。
#
# ## 一行是什么
#
#   {组名: {号: {"sid", "mid", "at", "window"}}}
#
#   sid    = 当前认定的窗口 id（**缓存**，失效了按名字重取）
#   mid    = 本地记到上游的第几号（水位）
#   at     = 最后一次更新时刻
#   window = 窗口名（就是组名，写下来是为了自解释）
#
# ## 缺口怎么算
#
#   组内最远的 mid  -  本号的 mid   =  缺几步
#
# 纯本地减法，不问上游。上游只负责收下补的内容。
LEDGER_FILE = _conf_file("_ledger.json")   # 本地账本（组->步号）—— 落 ds/，见 MARKS_FILE 注
_ledger_lock = threading.Lock()


def _ledger_root():
    """读整本账。坏了当空账，绝不让它把请求带塌。"""
    try:
        if LEDGER_FILE.is_file():
            d = json.loads(LEDGER_FILE.read_text(encoding="utf-8"))
            return d if isinstance(d, dict) else {}
    except BaseException:            # noqa: BLE001
        pass
    return {}


def ledger_put(group, slug, sid="", mid=None):
    """记一笔：这个组的这个号，走到第几号了、窗口是哪个。

    只增不减：mid 只往大了写（上游步号单调增，回退一定是异常，
    写回去会把缺口算小、该补的不补）。sid 变了就跟着换（窗口重建）。
    """
    try:
        g = str(group or "").strip()
        s = str(slug or "").strip()
        if not g or not s:
            return
        with _ledger_lock:
            d = _ledger_root()
            cell = d.setdefault(g, {}).setdefault(s, {})
            if sid:
                cell["sid"] = str(sid)
            if mid is not None:
                try:
                    m = int(mid)
                except (TypeError, ValueError):
                    m = None
                if m is not None:
                    old = cell.get("mid")
                    # 只往大了写：步号回退 = 换了新窗口（从 2 开始），
                    # 那种情况由调用方显式 reset，不在这里悄悄降。
                    if old is None or m > int(old):
                        cell["mid"] = m
            cell["window"] = g
            cell["at"] = round(time.time(), 3)
            tmp = LEDGER_FILE.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(d, ensure_ascii=False, indent=1),
                           encoding="utf-8")
            tmp.replace(LEDGER_FILE)
    except BaseException:            # noqa: BLE001
        pass


def ledger_reset(group, slug, sid=""):
    """换窗口：水位归零。**只在确认换了新窗口时调** —— 步号从 1 重新数。"""
    try:
        g = str(group or "").strip()
        s = str(slug or "").strip()
        if not g or not s:
            return
        with _ledger_lock:
            d = _ledger_root()
            cell = d.setdefault(g, {}).setdefault(s, {})
            cell["mid"] = 0
            if sid:
                cell["sid"] = str(sid)
            cell["window"] = g
            cell["at"] = round(time.time(), 3)
            tmp = LEDGER_FILE.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(d, ensure_ascii=False, indent=1),
                           encoding="utf-8")
            tmp.replace(LEDGER_FILE)
    except BaseException:            # noqa: BLE001
        pass


def ledger_gaps(group, slugs=()):
    """这个组里谁落后了 —— 纯本地减法。

    返回 [{"slug", "mid", "gap", "sid"}]，gap = 组内最远 - 自己。
    """
    try:
        g = str(group or "").strip()
        d = _ledger_root()
        m = d.get(g) or {}
        if not m:
            return []
        want = [str(s) for s in (slugs or ()) if str(s)] or list(m.keys())
        rows = []
        for s in want:
            c = m.get(s) or {}
            rows.append({"slug": s,
                         "mid": int(c.get("mid") or 0),
                         "sid": str(c.get("sid") or "")})
        if not rows:
            return []
        top = max(r["mid"] for r in rows)
        for r in rows:
            r["gap"] = top - r["mid"]
        return sorted(rows, key=lambda r: -r["mid"])
    except BaseException:            # noqa: BLE001
        return []



REPLY_MAX = 32 * 1024 * 1024
REPLY_HEAD = 400
REPLY_TAIL = 200
_rp_lock = threading.Lock()


def emit_reply(kind, slug="", sid="", text="", think=0, extra=None):
    """一整条回复原样留档。永不抛 —— 记日志不能把轮询搞挂。"""
    try:
        row = {"t": round(time.time(), 3),
               "ts": time.strftime("%H:%M:%S"),
               "k": kind, "slug": slug, "sid": sid,
               "chars": len(text or ""), "think": int(think or 0),
               "text": text or ""}
        if extra:
            row.update(extra)
        with _rp_lock:
            try:
                _rf = grp_path("replies", slug, create=True) or REPLY_FILE
                if _rf.is_file() and _rf.stat().st_size > REPLY_MAX:
                    # 2026-09-30（用户口径「轮询不可能天天丢数据」「不丢数据是最后
                    # 一道防线」）：原来轮转是**破坏性**的 —— 改名成 .1，而 .1 若
                    # 已存在先 unlink。于是第 3 档开始时第 1 档已被删，历史永久丢。
                    # 改成按日期归档、**永不覆盖**：同名就加序号，一个都不删。
                    # 代价只是磁盘；收益是「任何一次交接都能回溯到全部历史」。
                    _stamp = time.strftime("%Y%m%d-%H%M%S",
                                           time.localtime(
                                               _rf.stat().st_mtime))
                    bak = _rf.with_name(
                        _rf.name + "." + _stamp)
                    _n = 1
                    while bak.exists():
                        bak = _rf.with_name(
                            _rf.name + "." + _stamp + "-" + str(_n))
                        _n += 1
                    _rf.rename(bak)
                with _rf.open("a", encoding="utf-8") as f:
                    f.write(json.dumps(row, ensure_ascii=False, default=str)
                            + chr(10))
            except OSError:
                pass
    except Exception:
        pass


def rp_head(text, n=REPLY_HEAD):
    """摘要用：正文开头一段。"""
    return (text or "")[:n]


def rp_tail(text, n=REPLY_TAIL):
    """摘要用：正文末尾一段。"""
    s = text or ""
    return s[-n:] if len(s) > n else ""





def rp_act(text, n=280):
    """「这一轮在干什么」—— 在正文还完整的时候算好，留给交接用。

    2026-10-02 修（用户口径「最后一步是错的」）。

    原做法：交接的「最后一步」拿 events 里的 head 400 + tail 200 现拼。
    实测统计最近 3300 轮，**95.3% 的轮次判成「工具调用」**，其中 669 轮
    正文提不出任何东西 —— 那一节只剩「调了 pwsh/read」，等于没说。
    更糟的是 1253 轮里 tail 的开头就出现在 head 里：head 和 tail 是
    **同一个 blob 被截了两次**（一个长 JSON 工具调用），拼起来读不通。

    根因：head/tail 是**按字符**截的，不认语义。截完再还原语义不现实。
    所以在这里（text 还完整）就把「在干什么」算出来，存进 events，
    交接直接取用。存 280 字，比原来的 600 字还省。
    """
    t = (text or "").strip()
    if not t:
        return ""
    try:
        names = re.findall(r'"name"\s*:\s*"([a-z_]+)"', t)
        names += re.findall(r'invoke name="([a-z_]+)"', t)
        prose = _act_prose(t)
        tg = _act_targets(t)
        if names:
            head = '调了 ' + '/'.join(dict.fromkeys(names))
            if tg:
                head += '（' + '、'.join(tg) + '）'
            if len(prose) > 20:
                return (head + '；' + prose)[:n]
            return head[:n]
        return prose[:n]
    except BaseException:
        return ' '.join(t.split())[:n]


def _act_targets(t, cap=3):
    """从工具调用参数里抠出「动了哪些东西」——文件短名 / 搜索式 / 说明。

    为什么需要：光写「调了 read」等于没说（实测 669/3300 轮就长这样）。
    参数里其实有真意图：读了哪个文件、搜的什么式样、这一步想干嘛。

    上游给两种形态，都要认（实测两种都出现过）：
      1) JSON：{"name":"read","arguments":{"file_path":"..."}}
      2) DSML：<｜｜DSML｜｜ parameter name="file_path" ...>值<｜｜DSML｜｜ parameter>
    第 2 种第一次漏了 —— 只写 JSON 的键值对匹配，DSML 那几条就退回
    「调了 read」，正是要修的那个毛病。

    pwsh 的 description（「查看本组全局台账尾部」）最值钱，优先取。
    只取文件名不含路径 —— 交接里长路径会把一行挤爆。
    """
    out = []
    try:
        # 工具自报的用途，最接近「在干什么」
        for m in re.finditer(r'[\"\']description[\"\']\s*:\s*[\"\']([^\"\']{4,60})[\"\']', t):
            out.append(m.group(1).strip())
        # JSON 形态的 file_path / path / pattern
        for m in re.finditer(r'[\"\'](?:file_path|path)[\"\']\s*:\s*[\"\']([^\"\']+)[\"\']', t):
            v = m.group(1).replace(chr(92), '/').rstrip('/')
            out.append(v.rsplit('/', 1)[-1] or v)
        for m in re.finditer(r'[\"\']pattern[\"\']\s*:\s*[\"\']([^\"\']+)[\"\']', t):
            out.append('搜 ' + m.group(1)[:40])
        # DSML 形态：parameter name="X" ...>值<
        for m in re.finditer(r'parameter\s+name=\"(file_path|path|pattern)\"[^>]*>(.*?)<', t, flags=re.S):
            key, val = m.group(1), m.group(2).strip()
            if not val:
                continue
            if key == 'pattern':
                out.append('搜 ' + val[:40])
            else:
                v = val.replace(chr(92), '/').rstrip('/')
                out.append(v.rsplit('/', 1)[-1] or v)
    except BaseException:
        return []
    seen, res = set(), []
    for x in out:
        if x and x not in seen:
            seen.add(x); res.append(x)
    return res[:cap]

def _act_prose(t):
    """从正文里剥出「人话」部分：去掉工具调用块，只留散句。

    踩过两次，都是「剥不干净」而不是「剥多了」：
      1) DSML 的记号是**全角竖线** U+FF5C（「｜｜DSML｜｜」），不是 ASCII
         的 ||。只写 || 的版本对全角一个都不匹配，工具调用原文整段漏进
         「在干什么」，交接里出现一坨「｜｜DSML｜｜ invoke name="read"」。
      2) 光剥**标签**不够：标签之间的**参数值是原样文本**，会漏成
         「< C:/.../_pending.md </ < .../_steps.md </ < 165」。
         所以必须按**块**删，不是按标签删。
    """
    s = re.sub(r'```.*?```', ' ', t, flags=re.S)
    # 按块删 DSML 工具调用：从「｜｜DSML｜｜ calls」到「/calls」整段不要
    s = re.sub(r'[｜|]{1,2}\s*DSML\s*[｜|]{1,2}\s*calls[｜|]{1,2}[^>]*?>.*?'
               r'[｜|]{1,2}\s*DSML\s*[｜|]{1,2}\s*/?\s*calls[｜|]{1,2}[^>]*?>',
               ' ', s, flags=re.S | re.I)
    # 没有 <calls> 包裹的散装 invoke：逐个删到 </invoke>
    s = re.sub(r'[｜|]{1,2}\s*DSML\s*[｜|]{1,2}\s*invoke\b.*?'
               r'[｜|]{1,2}\s*DSML\s*[｜|]{1,2}\s*/\s*invoke[｜|]{1,2}[^>]*?>',
               ' ', s, flags=re.S | re.I)
    # 2026-10-02（第三次踩）：**「<｜｜DSML｜｜ calls>」后面跟的是裸 JSON 数组。**
    # 实测漏成：「调了 read/pwsh（...）；[{"name": "read", "arguments": {...}}」
    # —— 标签删了，标签**后面**的 JSON 没删。这是三种上游形态里的第三种：
    #   · ```json {...} ```         （JSON 代码块）
    #   · <｜｜DSML｜｜ invoke name=..>  （纯 DSML）
    #   · <｜｜DSML｜｜ calls> + 裸 JSON （DSML 头 + JSON 体） <- 本次漏的
    # 所以：先把裸工具调用 JSON 整段删掉（它必定含 "name"+"arguments"）。
    #
    # **顺序很关键（实测踩到）**：上面那句 \`\`\`json...\`\`\` 的删除在**非贪婪**下
    # 遇到正文里有多个代码块时可能只吃掉前半段，残留的 JSON 尾巴会漏出去
    # （实测漏成：...注释。 Path x.log; Write-Host \"EXIT=...\", "description": ...）。
    # 所以这里用**更宽的**匹配：从 "tool_calls" 起一直吃到最后一个 } 或行尾。
    s = re.sub(r'\{?\s*"tool_calls"\s*:\s*\[.*', ' ', s, flags=re.S)
    s = re.sub(r'\[\s*\{\s*"name"\s*:.*', ' ', s, flags=re.S)
    s = re.sub(r'\{\s*"name"\s*:\s*"[a-z_]+"\s*,\s*"arguments"\s*:.*', ' ', s, flags=re.S)
    # **未闭合的代码块**：交接读的是 head400+tail200，代码块常常**没有结尾栅栏** ——
    # 实测这一种漏成：`...；[{"name": "read", "arguments": ...`（裸 JSON 没被删，
    # 因为它在 \`\`\`json 之后、而栅栏没出现，第一句删除匹配不上）。
    # 所以：出现 \`\`\`json 就直接把**它之后的一切**当工具调用丢掉。
    s = re.sub(r'```\s*json\b\s*(?=[\{\[]).*', ' ', s, flags=re.S | re.I)
    # 三反引号后面**没跟 { 或 [** 的不算工具块，别删（见下）。
    # 剩下的零散标签 + 以 DSML/invoke/parameter 起头的整行
    s = re.sub(r'</?[A-Za-z_][^>]{0,200}>', ' ', s)
    s = re.sub(r'^.*?[｜|]{1,2}\s*DSML\b.*$', ' ', s, flags=re.M | re.I)
    s = re.sub(r'^\s*[｜|]{0,2}\s*(invoke|parameter|calls)\b.*$',
               ' ', s, flags=re.M | re.I)
    s = ' '.join(s.split())
    # 兜底：剥完几乎只剩路径的，说明本来就是纯工具调用 -> 不留
    if s.count('/') > 2 and len(s) < 200:
        rest = re.sub(r'[A-Za-z]:[\\/][^\s<]*', ' ', s)
        rest = re.sub(r'[<｜|>]', ' ', rest)
        if len(rest.split()) <= 2:
            return ''
    return s
# note() 里哪些前缀要顺带落成事件（2026-09-22 加，账号池被动观测）。
# 用前缀不用全文：日志里混着回复正文的回声，全文匹配实测把「只思考不落笔」
# 顶出过 3 次假命中。note() 只收桥内部消息，前缀是固定原文，所以干净。
# 注意「上游不是正常结束」不在表里 —— 它已经有专门的 upfin 事件。
NOTE_EVENTS = (
    ("  ⚠ 交接撞上限", "handoff_cut"),
    ("  ⟳ 指纹去重", "fpclash"),
    ("  ⚠ 思考档模型", "noreason"),
    ("  ⟳ 只思考不落笔", "nothink"),
    ("  ⟳ 重发拿到", "rescue"),
    ("  ⊘ 只思考无正文", "thinline"),
    ("  ⊘ 只有思考", "dropthink"),
    ("续接旧会话", "resume"),
    ("  ⟳ 压缩完成：检查点", "compact_done"),
    ("  ⚠ 上游判定发送过于频繁", "ratelimit"),
    ("  ⚠ 空回复按限流处理", "ratelimit"),
    ("  ⚠ 空回复 -> 按频繁限流处理", "ratelimit"),
    ("  ⊘ 空回复但 quasi_status=", "upstream_cut"),
)


def emit(kind, **fields):
    # 永不抛异常：记日志不能把轮询搞挂。
    try:
        with _ev_lock:
            _ev_seq[0] += 1
            row = {"n": _ev_seq[0], "t": round(time.time(), 3),
                   "ts": time.strftime("%H:%M:%S"), "k": kind}
            row.update(fields)
            # 2026-10-01 第458步：写进本组的事件流。
            # emit(kind, **fields) 的 slug 就在 fields 里（调用方都传了），
            # 从 row 里取 —— 不用改 5000 多处调用点。
            _ef = grp_path("events", row.get("slug"), create=True) or EVENT_FILE
            try:
                if (_ef.is_file()
                        and _ef.stat().st_size > EVENT_MAX):
                    bak = _ef.with_suffix(".jsonl.1")
                    if bak.is_file():
                        bak.unlink()
                    _ef.rename(bak)
                with _ef.open("a", encoding="utf-8") as f:
                    f.write(json.dumps(row, ensure_ascii=False,
                                       default=str) + chr(10))
            except OSError:
                pass
    except Exception:
        pass
# 模型名 → (深度思考, 联网搜索)。dsh 的 settings.yaml 里写哪个就走哪套开关。
MODELS = {
    "deepseek-chat": (False, False),
    "deepseek-reasoner": (True, False),
    "deepseek-chat-search": (False, True),
    "deepseek-reasoner-search": (True, True),
}

ROLE_LABEL = {
    "system": "系统",
    "developer": "系统",
    "user": "用户",
    "assistant": "助手",
    "tool": "工具结果",
    "function": "工具结果",
}

TOOL_PROTOCOL = r"""

你现在接在一个工具执行器后面。可以调用下面列出的工具。

要调用工具时，整条回复只输出一个 JSON 代码块，不要有任何别的文字：

```json
{"tool_calls": [{"name": "工具名", "arguments": {"参数名": "参数值"}}]}
```

规则：
- arguments 必须是 JSON 对象，严格符合该工具的参数表，不要自己编字段。
- **你要是改用原生标记（invoke / parameter）输出**：parameter 的 name 直接写
  **参数名**（如 code、description），**不要**写 arguments。arguments 只是上面
  那个 JSON 外壳的键名，没有任何工具的参数叫它；写成 `parameter name="arguments"`
  会让执行器收到 `{"arguments": {...}}`，报 `missing required property "code"`，
  整轮白跑一次。
- **必填字段一个都不能少**：参数表里标了 required 的字段，每个都要给。
  实测最常漏的是 pwsh 的 description —— 只给 command 会报
  missing required property，命令根本没跑，那一轮白烧。
- **别把外壳名当参数名**：arguments、input、parameters 这三个词是上面那个
  JSON 外壳的键名，没有任何工具的**参数**叫它们。原生标记里每个 parameter
  标签后面跟的，就是该工具参数表里的字段名（command、code、description、
  file_path 这类）。把整张参数表塞进一层 arguments 里，执行器只会看到参数表
  里多了一个叫 arguments 的字段，找不到 command，直接整条拒收。
- 一次可以放多个工具调用，但只输出一个 JSON 代码块。
- 不需要调用工具时，正常用自然语言回答，不要输出上面那种 JSON。
- 工具的执行结果会在下一轮以「工具结果」的身份给你。

关于 Windows 路径和转义（这里最容易出错，务必照做）：
- 路径统一写正斜杠：`F:/工具/a.html`。不要写反斜杠 —— `\t`、`\n`、`\b`、`\u`
  在 JSON 里都是转义序列，`...\尝试\tank.html` 会被解析成一个制表符加
  `ank.html`，文件就写到别的名字上去了，后面再读就「not found」。
- 参数值里的换行直接写 `\n`，不要写真实换行；不要在 JSON 字符串里嵌套
  未转义的双引号。
- 内容里出现 ``` 没关系，不影响解析；但别用反引号模板拼接来省事。

关于长内容（这条是硬限制，违反了文件一定写不成）：
- 单次回复有输出长度上限，一个参数值超过约 4000 字符就会在中途被砍断，
  JSON 解不开，工具调用整个丢失 —— 你会看到「文件没写成」「路径不存在」，
  但那不是文件系统坏了，是这条消息根本没送出去。
- 所以写长文件必须**分多轮**：第一轮 write 只写前 4000 字符以内的一段，
  拿到成功回执后，下一轮再用追加的方式写下一段，直到写完。
- 一轮只写一段，不要在同一个 JSON 里放好几段大内容。
- 拆分点选在行边界上（`\n` 之后），不要在一个字符串字面量或标签中间断开。

可用工具：

""".strip()


def _text_of(content):
    """content 可能是字符串，也可能是 OpenAI 的多段数组。

    图片这里只留一个占位标记，真正的图片走 _images_of 收集后当附件上传
    （标记要稳定，SessionCache.key 也用这个函数算哈希）。
    """
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    parts = []
    for seg in content if isinstance(content, list) else []:
        if isinstance(seg, str):
            parts.append(seg)
        elif isinstance(seg, dict):
            if seg.get("type") in (None, "text", "input_text"):
                parts.append(seg.get("text") or "")
            elif seg.get("type") in ("image_url", "image", "input_image"):
                parts.append("〔见随附图片〕")
            else:
                parts.append(json.dumps(seg, ensure_ascii=False))

    return "".join(parts)


MAX_IMAGES = 6                 # 一轮最多带几张，别一次上传一堆
MAX_IMAGE_BYTES = 12 << 20     # 单张 12MB 顶天

# 运行时产生的东西都塞进 .state/，别把脚本目录搞乱
STATE = pathlib.Path(__file__).with_name(".state")


def state_file(name, legacy=None):
    """.state/ 下的文件路径。老位置有同名文件就顺手搬过来，只搬一次。"""
    STATE.mkdir(parents=True, exist_ok=True)
    dest = STATE / name
    if legacy:
        old = pathlib.Path(__file__).with_name(legacy)
        if old.is_file() and not dest.exists():
            try:
                old.replace(dest)
            except OSError:
                pass
    return dest


_DATA_URL = re.compile(r"^data:([\w/+.-]+);base64,(.*)$", re.S)


def _decode_image(url):
    """把 image_url 变成 (mime, 原始字节, 文件名)。认 data: URL 和本地路径。"""
    if not isinstance(url, str) or not url:
        return None
    m = _DATA_URL.match(url.strip())
    if m:
        mime = m.group(1)
        try:
            raw = base64.b64decode(m.group(2), validate=False)
        except (ValueError, TypeError):
            return None
        ext = mimetypes.guess_extension(mime) or ".png"
        return mime, raw, f"image_{_short_id()}{ext}"
    if url.startswith(("http://", "https://")):
        return None                       # 远端图不代抓，交给调用方
    p = pathlib.Path(url)
    if p.is_file():
        mime = mimetypes.guess_type(p.name)[0] or "image/png"
        return mime, p.read_bytes(), p.name
    return None


def _images_of(content):
    """从一条消息的 content 里把图片抠出来。"""
    out = []
    for seg in content if isinstance(content, list) else []:
        if not isinstance(seg, dict):
            continue
        if seg.get("type") not in ("image_url", "image", "input_image"):
            continue
        url = seg.get("image_url") or seg.get("url") or seg.get("source") \
            or seg.get("data")
        if isinstance(url, dict):
            url = url.get("url") or url.get("data") or url.get("path")
        got = _decode_image(url)
        if got and len(got[1]) <= MAX_IMAGE_BYTES:
            out.append(got)
    return out


def images_in(messages):
    out = []
    for m in messages:
        out.extend(_images_of(m.get("content")))
    return out[:MAX_IMAGES]


# 2026-10-01 第472步（用户实测「窗口一直报错：unknown tool "pwsh"」）：
#
# **PTC 模式下必须换一套调用写法，而原来的协议文本从来不知道这件事。**
#
# 病根（实测全史 254 次 unknown tool 报错，两个方向相反的坑）：
#   · 09-26~09-30 共 233 次：模型调 run_code 被拒
#       （那时不是 PTC，工具表里有 pwsh/read/... 而没有 run_code）
#   · 10-01 4 次：模型调 pwsh 被拒
#       （现在是 PTC，工具表里**只有 run_code**）
# 模型自己 07:46 的回复把这件事说得很准：
#   「这个执行器不接受这个顶层名——它的实际工具名直接就是 pwsh / read /
#     glob / skill 这些。所以那两轮报的都是 unknown tool "run_code"」
# 而现在反过来了。
#
# 桥这边的问题：TOOL_PROTOCOL 教的是「name 写工具名」，**PTC 下没有那个工具**。
# has_tool() 只在桥自己造调用时用了两处（问工作区、切组弹窗），
# **模型自己造的 tool_call 完全没走这道判断** —— 原样透传给 dsh，必然被拒。
#
# 为什么不事后包装（把模型的 pwsh 改写成 run_code）：
#   那等于桥替模型改写它的工具调用，会丢掉「模型自己想调什么」的语义。
#   **在源头把格式教对**才对 —— 模型本来就该知道 PTC 下怎么写。
PTC_PROTOCOL = r'''

⚠ 本轮是 **PTC 模式**（工具表里只有 run_code 一个）。

**不要直接调其它工具名。** 直接写 name=pwsh 会被执行器拒掉，
报 unknown tool，那一轮完全白跑。所有工具都必须从 run_code 里调。

最外层永远是一个 run_code，参数是 code + description（两个都必填）：

```json
{"tool_calls": [{"name": "run_code", "arguments": {
  "description": "执行 PowerShell 查看目录",
  "code": "const r = await tools.pwsh({ command: \"Get-ChildItem\", description: \"列目录\" }); return r;"
}}]}
```

里面用 `await tools.<真正的工具名>({...})` 调你要的工具：

    const r = await tools.pwsh({ command: "...", description: "..." });
    const r = await tools.read({ file_path: "..." });
    const r = await tools.grep({ pattern: "...", path: "..." });

要点：
- **内层参数表就是该工具自己的参数表**（pwsh 要 command+description，
  read 要 file_path，grep 要 pattern）。别把 arguments 当参数名。
- 一段 code 里可以调多次，也可以处理返回值后再 return。
- 整条回复只输出那一个 JSON 代码块，别加解释文字。
'''


def detect_mode(tools):
    """判定本轮是 PTC 还是标准模式。**全桥唯一判据，谁都别再自己猜。**

    返回 "ptc" 或 "standard"。

    ## 为什么要收成一个函数

    2026-10-03 诊断（见 工具协议-诊断报告.md 第 5.1 节）：
    同一个问题「本轮是不是 PTC」，原代码里有**四处各自判断**，口径互不相同：

        _proto_for      L4817   has_tool("run_code") and not has_tool("pwsh")
        _ask_workspace  L14186  注释说 ntools==1，代码里压根没判（硬写 run_code）
        _rc_emit        L14099  has_tool(_tools_now, tool)
        _switch_emit    L14234  has_tool(_tools_now, SWITCH_TOOL)

    四处不一致的代价是实测出来的：快照指令 140 次 100% 被拒
    （`Error: unknown tool "run_code"`），因为 _rc_emit 当年「无条件包 run_code」。
    现在统一到这里，其余地方只准读它的结果。

    ## 判据（按可靠性排序，命中即返回）

    PTC 的特征是 **dsh 只放行 run_code 一个工具**，其余工具必须从 code 里调。
    所以：

      1. **工具表里有 run_code** → PTC。
         这是 dsh 侧 PTC 的直接标志，不需要别的佐证。
         注意：**不能要求「且没有 pwsh」**。实测真实请求（_toolnames.log，24 轮）
         ntools=50/51 且 run_code 与 pwsh 会同时出现在工具清单文本里 —— 旧判据
         在这种情况下会判成「非 PTC」，于是发标准协议、模型写 name=pwsh、
         被执行器拒。这正是「经常报错」的根。

      2. **工具表里没有 run_code，但有 pwsh** → 标准模式。
         实测 24/24 轮都是这一形态（ntools=50/51）。

      3. 两者都没有 → 按标准模式（保守）。
         标准协议教的是「name 写真实工具名」，在任何非 PTC 的表下都成立；
         宁可发标准协议，也不要错发 PTC 协议 —— 后者会诱导模型把所有调用
         都套进 run_code，而表里连 run_code 都没有，必然全灭。

      4. tools 为空/读不到 → 标准模式。
    """
    try:
        if not tools:
            return "standard"
        names = set()
        for t in tools:
            fn = t.get("function") if isinstance(t, dict) else None
            fn = fn or (t if isinstance(t, dict) else {})
            nm = str(fn.get("name") or "").strip()
            if nm:
                names.add(nm)
        if not names:
            return "standard"
        # PTC 的直接标志：run_code 在场。
        if "run_code" in names:
            return "ptc"
        return "standard"
    except BaseException:            # noqa: BLE001
        # 判定本身绝不能把请求搞挂 —— 出任何意外都退回标准模式。
        return "standard"


def is_ptc(tools):
    """布尔便利版。"""
    return detect_mode(tools) == "ptc"


def _proto_for(tools):
    """按本轮模式选协议文本。

    2026-10-01 第472步（用户实测「窗口一直报错 unknown tool pwsh」）。
    2026-10-03 改：判据改走 detect_mode（唯一判据），不再在这里自己猜。
    """
    if detect_mode(tools) == "ptc":
        return PTC_PROTOCOL
    return pget("tool_protocol", TOOL_PROTOCOL)


def tools_block(tools):
    """把 OpenAI 的 tools 定义摊成给模型看的清单。

    2026-10-01 第424步：**加厚协议、瘦身清单。**
    实测固定开销 13419 字/发，其中工具表 8087 字（tools=50），schema 占 91%。
    这里换成 _schema_brief() 的紧凑形式，同一个工具表省掉大头。

    注意：TOOL_PROTOCOL **不能**跟着瘦 —— 它是「怎么调」的指令，而且历史
    上正是因为写得不够硬才出过错误（模型把参数写在 name 层、忘给必填项）。
    省字只省数据（JSON 样板），不省规矩。
    """
    lines = [_proto_for(tools)]
    for t in tools or []:
        fn = t.get("function") if isinstance(t, dict) else None
        fn = fn or (t if isinstance(t, dict) else {})
        name = fn.get("name") or "未命名"
        desc = " ".join((fn.get("description") or "").split())
        lines.append(f"\n### {name}\n{desc}\n参数：{_schema_brief(fn.get('parameters'))}")
    return "\n".join(lines)


def _schema_brief(schema):
    """把 JSON Schema 摊成一行紧凑的参数说明（省字数，信息不丢）。

    2026-10-01 第424步（用户口径「缓存命中率如何优化」「命中率有点低啊」）：
    实测每发固定开销 13419 字，其中**工具表 8087 字**（tools=50 时），
    而 schema 部分占其中约 91%。原来的输出是整份 JSON Schema 单行紧凑 JSON：

        {"type": "object", "properties": {"command": {"type": "string",
         "description": "要执行的 PowerShell 命令"}, ...}, "required": [...]}

    这些 {"type":"object","properties":...} 样板对模型没有任何信息量 ——
    它需要的是「参数叫什么、什么类型、哪些必填」。摊平后同一份信息
    大约只要三分之一字数，而且**更好读**（模型不用先解析 JSON 再理解）。

    产出形式（一个参数一段，顿号分隔）：
        command:str* 要执行的 PowerShell 命令; description:str* 简短描述; timeoutMs:num
        （* = 必填；无参数写「无」）
    """
    if not isinstance(schema, dict):
        return "无"
    props = schema.get("properties")
    if not isinstance(props, dict) or not props:
        return "无"
    req = schema.get("required")
    req = set(req) if isinstance(req, (list, tuple)) else set()
    _t = {"string": "str", "number": "num", "integer": "int",
          "boolean": "bool", "array": "arr", "object": "obj", "null": "null"}
    parts = []
    for name, spec in props.items():
        spec = spec if isinstance(spec, dict) else {}
        ty = spec.get("type")
        if isinstance(ty, list):
            ty = "/".join(str(x) for x in ty)
        ty = _t.get(str(ty), str(ty or "any"))
        star = "*" if name in req else ""
        d = str(spec.get("description") or "").strip()
        # 描述里换行会破坏「一行一工具」的紧凑性，压成空格
        d = " ".join(d.split())
        if len(d) > 60:
            d = d[:60] + "…"
        seg = f"{name}:{ty}{star}"
        if d:
            seg += " " + d
        # enum 是有用信息，保留
        en = spec.get("enum")
        if isinstance(en, list) and en:
            seg += " 取值=" + "/".join(str(x) for x in en[:8])
        parts.append(seg)
    return "; ".join(parts)


def tools_brief(tools):
    """工具清单的**精简版**：只有名字 + 参数名 + 必填标记，没有描述与类型细节。

    2026-10-01 第426步（用户口径「你说的是每次只发一次工具表 除非换号 那可不可以
    理解为 每次只发一次检查点换号之前不用一直发呢」）：

    实测（50 工具规模）inline 工具表占 **5004 字**，是 prompt 里最大的一块；
    而真实轮次的 prompt 中位数只有 **13942 字** —— 工具表占了 36%。
    精简版只要 **1028 字**，省 **3976 字/发（79%）**。

    为什么敢精简：模型需要的是「**有哪些工具、哪个必填**」，用来决定调谁；
    具体某个参数什么类型、描述是什么，它一旦决定要调，`tools` 字段（每轮都由
    客户端原样传来，桥不碰）里有完整定义。**描述是「选工具」用的，不是「填参数」
    用的** —— 而选工具靠的是名字 + 语义，那部分在参数表里本来就没有。

    什么情况下必须给完整版：**开新会话那一发**（上游会话里没有过这份表），
    以及换号接手。见 tail_of 的 tools_full 参数。
    """
    lines = []
    for t in tools or []:
        fn = t.get("function") if isinstance(t, dict) else None
        fn = fn or (t if isinstance(t, dict) else {})
        name = fn.get("name") or "未命名"
        sch = fn.get("parameters") if isinstance(fn.get("parameters"), dict) else {}
        props = sch.get("properties")
        props = list(props.keys()) if isinstance(props, dict) else []
        req = sch.get("required")
        req = set(req) if isinstance(req, (list, tuple)) else set()
        if props:
            arg = ", ".join(("*" + p if p in req else p) for p in props)
            lines.append("- " + name + "(" + arg + ")")
        else:
            lines.append("- " + name + "()")
    if not lines:
        return ""
    return ("可用工具（名字(参数)，* = 必填；调用的写法见上面协议）：" + chr(10)
            + chr(10).join(lines))
def tools_catalog(tools):
    """只列工具清单与参数 —— **不含协议**。

    2026-09-22 加（用户口径）：走附件时协议（TOOL_PROTOCOL）必须留 inline，
    因为那是「怎么做」的指令；清单是数据，长且基本不变，进 工具表.txt。

    2026-10-01 第424步：schema 改用 _schema_brief() 摊平输出（省字数，
    见该函数注释）。工具**名字与用途描述照旧完整保留** —— 少一个字都可能
    让模型选错工具，省字只能省在 JSON 样板上。
    """
    lines = []
    for t in tools or []:
        fn = t.get("function") if isinstance(t, dict) else None
        fn = fn or (t if isinstance(t, dict) else {})
        name = fn.get("name") or "未命名"
        desc = " ".join((fn.get("description") or "").split())
        lines.append(f"### {name}\n{desc}\n参数：{_schema_brief(fn.get('parameters'))}")
    return "\n\n".join(lines)


def render_turn(msg):
    """单条 OpenAI 消息 → 给 DeepSeek 看的一段文本。"""
    role = msg.get("role") or "user"
    text = _text_of(msg.get("content"))

    # 助手上一轮发起的工具调用，回放成同样的 JSON，模型才认得自己的动作
    calls = msg.get("tool_calls")
    if role == "assistant" and calls:
        shaped = [{"name": (c.get("function") or {}).get("name"),
                   "arguments": _loose_json((c.get("function") or {}).get("arguments"))}
                  for c in calls]
        body = json.dumps({"tool_calls": shaped}, ensure_ascii=False)
        text = (text + "\n" if text else "") + f"```json\n{body}\n```"

    if role in ("tool", "function"):
        name = msg.get("name") or msg.get("tool_call_id") or ""
        head = f"工具结果（{name}）" if name else "工具结果"
        return f"【{head}】\n{text}"

    return f"【{ROLE_LABEL.get(role, role)}】\n{text}"


def _loose_json(raw):
    """arguments 在 OpenAI 协议里是字符串，解不开就原样留着。"""
    if isinstance(raw, (dict, list)):
        return raw
    try:
        return json.loads(raw or "{}")
    except (TypeError, json.JSONDecodeError):
        return raw or {}


# 2026-09-22 加：最近一次 build_prompt 收出来的三份附件内容。
# run() 拿它们去生成 txt 并上传 —— 大块不再"省略"，而是随附件一起发。
#   LAST_DROPPED -> 上下文.txt（被裁掉的老历史）
#   LAST_TOOLS   -> 工具表.txt（工具清单与参数，不含协议）
#   LAST_LEDGER  -> 台账.txt（本窗口的台账，表头带窗口号）
LAST_DROPPED = []
LAST_TOOLS = []
LAST_LEDGER = []

# 附件按「帐号 + 内容 hash」缓存 file id。附件 id 是按帐号归属的，
# 所以键必须带 slug；换号即查不到 -> 自动重传。
_ATTACH_CACHE = {}

# 2026-09-22 加：走附件时这段贴在【提醒】之后、末尾之前 ——
# reminder_block 是故意贴末尾的（越靠近末尾模型越照做），附件说明也一样。
# 用户口径：GUI 里那份常驻提醒没提附件，模型就不知道附件干嘛的。
# 2026-09-24 加：检查点那几行单独拎成一个常量，好跟着 empty_policy 的
# checkpoint_on_switch 一起开关。原来它硬编码在 ATTACH_NOTE 里，于是
# 开关关掉之后换号不再生成 检查点.txt，说明里却还写着「先读它的
# Current Work」—— 模型照着去找一个根本不存在的附件，白烧一轮。
ATTACH_CP_LINES = (
    "  检查点.txt —— 你的状态快照（8 段式）。本轮若有这一份，就先读它的\n"
    "               「Current Work」「Next Step」，从那里接着做；\n"
    "               不要重做已完成且已验证的步骤。\n")
ATTACH_NOTE = (
    "【本次请求的附件 —— 它们就是你的上下文，不是要你分析的新文档】\n"
    + ATTACH_CP_LINES +
    "  上下文.txt —— 你之前的完整对话历史，按时间顺序接在前文之前。那是历史，\n"
    "               不是新任务；不要复述、不要重做已完成且已验证的步骤。\n"
    "  工具表.txt —— 你可调用的工具清单与参数。要执行动作就照它调用工具。\n"
    "  台账.txt   —— 全局进度台账（所有窗口共用）。每轮把「活跃」整栏贴给你，\n"
    "               里面有别的窗口登记的行；要登记就照它的行格式在「活跃」末尾追加。\n"
    "  这些文件都是本轮请求的一部分；没读到就等于没看上下文，别凭空补。\n"
    "  读取请求的附件的文字就不要在会话中复述了，附件对于你来说很重要所有的答案都在里边请牢记")

# 关掉检查点时用的那一版：同一份文案，去掉检查点那几行。因为是从
# ATTACH_NOTE 本体切出来的，永不与它分叉。
ATTACH_NOTE_NO_CP = ATTACH_NOTE.replace(ATTACH_CP_LINES, "")

# 2026-09-26 第93步：local 交接模式不上传 上下文.txt（用户口径：不需要上下文了，
# 直接 检查点 + 工具表 + 台账）。同样从 ATTACH_NOTE 本体切出来，永不与它分叉。
ATTACH_CTX_LINES = (
    "  上下文.txt —— 你之前的完整对话历史，按时间顺序接在前文之前。那是历史，\n"
    "               不是新任务；不要复述、不要重做已完成且已验证的步骤。\n")
ATTACH_NOTE_LOCAL = ATTACH_NOTE.replace(ATTACH_CTX_LINES, "")




def _cp_enabled():
    """换号时到底会不会生成检查点（empty_policy 热开关，默认开）。"""
    try:
        return bool(_policy_root().get("checkpoint_on_switch", True))
    except Exception:
        return True


def snapshot_block(text):
    """把快照包成**既成背景**的一段文本 —— 照 dsh 的 CHECKPOINT_PREAMBLE 做法。

    2026-10-03（用户口径：「应该让他认为他就干了这么多活就到这里了」）。

    ## 为什么不再做成附件

    dsh 自己的压缩是**把摘要直接铺进消息流**，并配一句：
        "This is an automatically generated checkpoint ... Treat the captured
         context as established background and build on it without restating it.
         Continue the task directly from the messages that follow..."
    模型读完就是「我干到这儿了，接着干」—— 实测一切正常。

    而桥把快照做成了**附件文件**，说明里写「你缺的那一段历史，先读它」——
    模型于是进入「我有个文件要读」的状态：找不到就 glob、read、重复调用。
    实测一次请求里重复 read 5 次、44 份快照 84% 在自我引用。

    改成文本后：没有「找文件」这个动作，读到即背景。
    """
    body = str(text or "").strip()
    if not body:
        return ""
    return ("〔工作背景 —— 这是你自己此前的工作状态，直接接着做〕" + chr(10)
            + "把它当作**既成事实**：不要复述它、不要分析它、不要去找对应的文件。" + chr(10)
            + "从它描述的「下一步」直接开始干活。" + chr(10)
            + chr(10) + body + chr(10)
            + "〔工作背景结束 —— 以上即当前进度，继续执行〕")


def attach_note_for(names):
    """按**这一发实际挂上去的附件**生成说明。

    2026-10-02 第482步（用户口径「新窗口还是白搭」）：

    **这是同一个坑第三次出现，必须根治。**
      第 1 次（2026-09-24，见上面 ATTACH_CP_LINES 注释）：开关关掉后不再生成
        检查点.txt，说明里却还写着「先读它的 Current Work」-> 模型白找一轮。
      第 2 次：local 交接模式去掉 上下文.txt，说明没跟着去。
      第 3 次（本次）：第425步起「续接同一上游会话」时**工具表和上下文都不挂**
        （省 token），但那两行的说明照旧在 —— 实测窗口每轮都在找
        「检查点.txt / 上下文.txt / 工具表.txt / 台账.txt」这四个附件，
        找不到就再找，**连续几十轮空转**。这就是「新窗口也白搭」的真因：
        跟窗口新旧无关，是桥的文字在骗它。

    改法：**不再拼一份固定文案，而是按实际挂了哪几份生成。**
    names 是这一发真正上传的附件标签集合，例如 {"检查点","台账"}。

    没挂的就不提；一份都没挂就返回空串（调用方据此不发这一段）。
    """
    try:
        have = set(names or ())
    except TypeError:
        have = set()
    if not have:
        return ""
    L = ["【本次请求的附件 —— 它们就是你的上下文，不是要你分析的新文档】"]
    L.append("（本轮实际只挂了这几份；**没列出来的就不存在，不要去找**。）")
    # 兼容两种写法：改名过程中「检查点」是旧名。两边都认，
    # 免得下次再改名时又静默少一行（这个坑踩过一次了）。
    if "快照" in have or "检查点" in have:
        # 2026-10-02：检查点换成快照（用户口径「不要检查点了 有快照了要他干嘛」）。
        # 两者都是 ## Primary Request and Intent 开头的八段式，区别只在产生方式：
        #   检查点 = 桥主动问老号要（已删）｜快照 = dsh 按空缺时间压出来的（现在这条）。
        L.append("  快照.txt —— **你缺的那一段历史**（dsh 压缩产物，八段式）。"
                 "先读它的「Current Work」「Next Step」，从那里接着做；"
                 "不要重做已完成且已验证的步骤。")
    if "上下文" in have:
        L.append("  上下文.txt —— 你之前的完整对话历史，按时间顺序接在前文之前。"
                 "那是历史，不是新任务；不要复述、不要重做已完成且已验证的步骤。")
    if "工具表" in have:
        L.append("  工具表.txt —— 你可调用的工具清单与参数。要执行动作就照它调用工具。")
    if "台账" in have:
        L.append("  台账.txt —— 全局进度台账。要登记就照它的行格式在「活跃」末尾追加。")
    L.append("  这些文件都是本轮请求的一部分；没读到就等于没看上下文，别凭空补。")
    return chr(10).join(L)


def attach_note_default():
    """附件说明的默认文案：检查点那份只在真会生成时才列。

    checkpoint_min_chars 那一档（开关开着但对话没到门槛）在这一层判断不了 ——
    那是每次换号按当时字数算的，而这份说明是每个号 stamp 一次。所以保留时
    用了「本轮若有」的口径，让模型自己先看文件在不在。
    """
    # 2026-10-03（用户口径「光附件提示就3个 我醉了」）：**收成一份。**
    # 真正常用的是 attach_note_for(names) —— 它按**实际挂了哪几份**动态生成，
    # 本身就不会提到没挂的文件，所以「关检查点」「local 模式」这些差异
    # 根本不需要预先剪出另一份文案来。
    # 原来那三份（NO_CP / LOCAL / CTX_LINES）只是喂一份马上被替换掉的占位文案。
    # 常量与配置键保留（防回归），但这里统一走同一份。
    return pget("attach_note", ATTACH_NOTE)

# 2026-09-23 用户口径：换号那一发 4 个文档都要传。对话很短、没有被裁掉的历史
# 时，上下文.txt 会因正文为空而不落盘（_upload_attach 对空串返回 ""），模型收到的
# 文件集就和上一段说明对不上。用这份占位顶上，让文件集恒定。
CTX_EMPTY_NOTE = "（本次没有被裁掉的更早历史 —— 全部内容都在 inline 提示里。）"


def tail_of(keys, items, sid, note, attach=False, attach_note=None, tools=None,
            format_note=None, dir_block=None, peer_root=None,
            slug="", messages=None, tools_full=True):
    """prompt 尾部 —— **全项目唯一一处**定义尾部有哪些段、什么顺序。

    2026-09-24 加。以前 build_prompt 和 _hand_prompt 各拼一遍，两处慢慢分叉：
      · 换号那一发（走 _hand_prompt）漏了附件说明 —— 模型收到几个无名附件；
      · 走附件时台账本该进 台账.txt（build_prompt 会跳过 inline），
        但 _hand_prompt 照旧内联 —— 同一份台账既内联又挂附件。
    以后要加/改/换尾部任何一段，**只改这一个函数**，两条路径自动一致。

    顺序（越靠末尾越被照做）：台账(仅非附件路) → 附件说明 → 工具清单 →
    〔格式提醒〕（内含 常驻/池提醒 + 本机目录 + 格式提醒 三块） → 【助手】。
    第413步起提醒不再单开一段：常驻/池提醒并进〔格式提醒〕，整个 prompt 只有
    这一个提醒段，路径也只在这一段里出现。格式提醒仍是绝对末尾
    （用户口径：不管什么时候都要发、且放最后）。
    """
    # 2026-09-27 第406步（用户口径：「主要提示有硬编目录」）：常驻/池提醒、
    # 附件说明、格式提醒里写死的绝对路径都是按桥本机根（ini workdir）写的，
    # 别台电脑接进来时模型会照抄，在 A 机的目录名下干活。这一轮若从请求里
    # 认出了对端根，就把这些硬编前缀统一换掉；认不出（本机自己用）原样不动。
    # 2026-09-27 第418步：dir_block_now() 一直存在却没有任何调用点 ——
    # tail_of 的 dir_block 默认 None，两个调用方都只传 format_note/peer_root，
    # 于是〔本机目录〕段从来没进过提示词。调用方没传时这里自己算
    # （要 slug + 本轮 messages 才能走证据链）；传了以调用方为准。
    if dir_block is None and messages:
        try:
            dir_block = dir_block_now(slug, messages)
        except Exception:        # noqa: BLE001
            dir_block = ""
    if peer_root:
        note = retarget_roots(note, peer_root)
        if attach_note is not None:
            attach_note = retarget_roots(attach_note, peer_root)
        if format_note is not None:
            format_note = retarget_roots(format_note, peer_root)
        # 2026-10-01 第421步（用户口径：「我桥走哪个模型本地就在工作区根据模型走哪个
        # 目录，你提示词给我改错了」）：dir_block **绝不能**进 retarget_roots。
        # 它是 dir_block_now() 刚刚用**本轮证据链**算出来的「这台机器的根」，
        # 本身就是权威答案；再拿 peer_root 去替换，等于把对端路径写到本机 AI 头上。
        # 实证（.state/ds_bridge.log:70064，2026-09-30 14:18，号 779，组=剪辑）：
        #   模型回「本机根目录 = F:/test」——而本机没有 F 盘（Get-PSDrive 实测），
        #   F:/test 是别台机器的根。日志里 F:/test 共出现 219 次。
        # 于是 AI 拿着别人的根找目录，认不出就自建/改错目录 = 串号。
    blocks = []
    if not attach:
        # 2026-09-25：台账不再按窗口过滤，故不再传窗口 id（见 ledger_block）。
        blocks.append(ledger_block(slug=slug))
    # 2026-09-27 第413步（用户口径：「把所有提醒的相关提示词全部规整，不要在
    # 提示中显示 —— 例如常驻提示、例如池提示、例如交接提示」）：常驻/池提醒
    # 不再单开一段〔提醒〕，改并入末尾**同一个**〔格式提醒〕段（见下）。于是
    # 注入提示里只剩一个提醒段，不再各占一段、各带一个抬头。
    if attach:
        _an = pget("attach_note", ATTACH_NOTE) if attach_note is None \
            else attach_note
        if _an:
            blocks.append(_an)
    if tools:
        # 2026-09-24 加：工具清单**每一发都带**。以前只有 build_prompt（新会话
        # 第一发 / 全文重发）带，delta 与 _hand_prompt 都不带 —— 而换号时
        # 「认不出会话」那条返回的是 session=None（开新会话），那一发的 prompt
        # 就是 _hand_prompt，于是新会话从头到尾没有工具清单，模型只能瞎猜
        # read_file / shell / powershell / Bash（实测原话：「当前会话未暴露
        # 任何可执行工具」）。另外 dsh 压缩会重写历史，连复用会话里那份也会被
        # 摘要顶掉 —— 所以必须每发都带，不能只靠「第一发进过会话」）。
        #
        # 2026-10-01 第426步（用户口径「每次只发一次工具表 除非换号」）：
        # **"每发都带"保留，"每发带全"取消。**
        #
        # 上面那条结论（必须每发都带）是实测出来的，不动它。但"带"不等于
        # "带全"：实测 50 工具规模下完整工具表 5004 字，而真实 prompt 中位数
        # 只有 13942 字 —— 工具表独占 36%。精简版（名字+参数名+必填标记）
        # 只要 1028 字，**省 3976 字/发（79%）**。
        #
        # 判据由调用方给（tools_full）：**开新会话 / 换号接手**时必须给完整版
        # （上游会话里没有过这份表，模型要照着它选工具、填参数）；
        # **续接同一上游会话**时上游已经有完整定义，这里的清单只起"提醒有哪些
        # 工具"的作用 —— 精简版足够，而且描述对"想起来能调什么"没有增量信息。
        if tools_full:
            blocks.append(tools_block(tools))
        else:
            _tb = tools_brief(tools)
            if _tb:
                # 协议仍然每发都带（那是"怎么调"的规矩，出过错，不能省）。
                blocks.append(_proto_for(tools) + chr(10) + chr(10) + _tb)
    # 格式提醒：独立一段，**无条件**每轮贴在最末尾（用户口径 2026-09-27：
    # 「格式提醒放在最后 不管什么时候都需要发」）。不区分池号/固定号、
    # 不区分是否带附件、不依赖 tools —— 只要非空就发。报错过的格式规范
    # 都往这里加，上游每一发都能在最后看到。空串 = 设置台里清空了，不发。
    # 本机目录块（2026-09-27 第354步，用户要求）：桥按证据算出「这次请求来自哪台
    # 机器、根目录在哪、当前组对应哪个目录」，每轮贴一段，让别台电脑上的 AI 不必
    # 猜路径。只报告事实，不改配置、不建目录、不替换正文。放在格式提醒之前，
    # 保证格式提醒仍是绝对末尾。
    # 2026-09-27 第410步（用户口径：「把所有关于路径的提示全部放到格式提醒再拼接，
    # 别的地方出现不是很规则」）：本机目录块不再单独成段，改**并入格式提醒**这一
    # 段里 —— 于是全部绝对路径在 prompt 里只有这一个出现点，别处一律只写相对名
    # 并指回这里。格式提醒仍是绝对末尾、仍无条件每轮发。
    _fmt_parts = []
    _rem = reminder_block(note)
    if _rem.startswith("〔提醒〕"):
        # 抬头去掉：已经挂在〔格式提醒〕下面，不必再套一层〔提醒〕。
        _rem = _rem[len("〔提醒〕"):].strip()
    if _rem:
        _fmt_parts.append(_rem)
    if dir_block and str(dir_block).strip():
        _fmt_parts.append(str(dir_block).strip())
    if format_note and str(format_note).strip():
        _fmt_parts.append(str(format_note).strip())
    if _fmt_parts:
        blocks.append("〔格式提醒〕" + chr(10) + (chr(10) * 2).join(_fmt_parts))
    blocks.append("【助手】")
    return [b for b in blocks if b and b.strip()]


def build_prompt(messages, tools, note="", budget=0, attach=False, keep=0,
                 attach_note=None, sid="", format_note=None, peer_root=None,
                 slug=""):
    """整段 transcript 摊平成一个 prompt（新会话第一发用这个）。

    budget > 0 时只取**尾部**那些消息，让正文不超过这个字数。超长的重发会被
    上游打空，而空一次就是「冷却 + 换号 + 交接」一整条级联（实测 285770 /
    328838 字那两次全在 25 万字以上，那一档空率 13.6%）。

    attach=True 时（2026-09-22 加，用户实测通过）：三个大块全进附件，
    inline 只留**指令**：
        上下文.txt <- LAST_DROPPED（被裁掉的老历史）
        工具表.txt <- LAST_TOOLS  （工具清单与参数，**不含协议**）
        台账.txt   <- LAST_LEDGER （本窗口的台账，表头带窗口号）
    inline 留下：TOOL_PROTOCOL（怎么做）+ 附件说明 + 最近 keep 字的消息
                 + reminder_block（本轮要求）+【助手】。
    协议与提醒必须内联 —— 那是「怎么做」的指令，进附件等于让模型先去读文件
    才知道怎么动手。实测（C:/Users/Lenovo/Desktop/teste/_dbg/three-txt-test-20260922.txt）：
    60 万字历史进附件、inline 只 260 字，多跳任务 3/3 全对，与全内联无差。
    """
    del LAST_DROPPED[:]
    del LAST_TOOLS[:]
    del LAST_LEDGER[:]
    _k, _i = SessionCache.fingerprint(messages)
    msgs = list(messages or [])
    lost = 0
    _bud = budget
    if attach and keep > 0:
        # 走附件时 inline 只保留最近一小段：这才是"上游输入上限再也顶不到"的关键。
        _bud = min(budget, keep) if budget > 0 else keep
    if _bud > 0 and len(msgs) > 1:
        keepm, total = [], 0
        for m in reversed(msgs):
            n = len(render_turn(m))
            if keepm and total + n > _bud:
                break
            keepm.append(m)
            total += n
        keepm.reverse()
        lost = len(msgs) - len(keepm)
        if lost:
            LAST_DROPPED.extend(render_turn(m) for m in msgs[:lost])
        msgs = keepm
    chunks = []
    if attach:
        LAST_TOOLS.append(tools_catalog(tools) if tools else "")
        LAST_LEDGER.append(ledger_block(slug=slug))
    if lost:
        if attach:
            chunks.append(f"〔上下文.txt 里是更早的 {lost} 条（约 "
                          f"{sum(len(x) for x in LAST_DROPPED)} 字）。以下是最近的增量。〕")
        else:
            chunks.append(f"（更早的 {lost} 条已省略 —— 以交接单和台账为准，"
                          f"不要凭猜测补，也不要重做已完成且已验证的步骤）")
    chunks += [render_turn(m) for m in msgs]
    chunks += tail_of(_k, _i, sid, note, attach, attach_note, tools,
                      format_note=format_note, peer_root=peer_root,
                      slug=slug, messages=messages)
    return "\n\n".join(c for c in chunks if c.strip())


# 复用会话时只发增量，整个 TOOL_PROTOCOL 不会再出现在上下文近处 —— 几十轮之后
# 模型就把「分段写」忘了，又去一次性塞两万字。所以每轮补一句便宜的提醒。
# 这只是出厂默认值：GUI 的「常驻提醒」直接编辑这段文字，改完立刻生效，
# 清空就一句都不发。发出去的永远是对话框里能看到的那段，没有藏起来的补充。
# 换号交接：老号把「干了什么」吐出来，原样贴给接手的号。
# 2026-09-21 加。之前换号只发 delta，而 delta 是按指纹切的、不含 assistant，
# 于是接手方看不到上一棒的结论，把已经做完的活又做一遍（实测五个账号停在
# 五个不同步骤）。这里改成换号那一刻直接问老号要一段交接。
HANDOFF_PROMPT = (
    "# 【交接】\n"
    "\n"
    "你现在要把当前任务交给下一个 AI。\n"
    "\n"
    "下一个 AI 看不到历史对话、推理、工具调用和执行过程，只能看到你输出的这段正文。用户会把它原样交给下一个 AI。\n"
    "\n"
    "**这不是工作汇报，而是可直接恢复执行的状态包。**\n"
    "\n"
    "直接写给接手方，用“你”指代接手方。不要写“我做了什么”、不要写建议、不要写模糊描述。\n"
    "\n"
    "必须让接手方看完后能够**从当前实际断点直接继续执行，而不是重新调查整个任务。**\n"
    "\n"
    "## 必须包含 5 部分\n"
    "\n"
    "### 1. 当前状态\n"
    "\n"
    "写清：\n"
    "\n"
    "* 整体任务目标\n"
    "* **任务名 + 第几步**，写成 `任务名@第N步` —— 接手方和用户都靠它定位\n"
    "* 当前步骤\n"
    "* 实际做到哪里\n"
    "* 当前正在处理什么\n"
    "* 当前阻塞\n"
    "* 文件、进程、后台任务、输出的实际状态\n"
    "\n"
    "### 2. 已完成\n"
    "\n"
    "只写真正完成并验证过的事项。\n"
    "\n"
    "尽量带：\n"
    "\n"
    "* 步骤\n"
    "* 文件 / 函数\n"
    "* 实际修改\n"
    "* 执行命令\n"
    "* 验证结果\n"
    "\n"
    "已完成且状态未变化的事项标记：\n"
    "\n"
    "**不要重复执行**\n"
    "\n"
    "做过但没验证的标记：\n"
    "\n"
    "**未验证**\n"
    "\n"
    "### 3. 已知坑 / 不要重复\n"
    "\n"
    "只写实际试过且确认失败或无效的方法。\n"
    "\n"
    "写清：\n"
    "\n"
    "* 方法\n"
    "* 实际结果\n"
    "* 报错 / 原因\n"
    "* 为什么不要再走\n"
    "\n"
    "禁止猜测不存在的坑。\n"
    "\n"
    "### 4. 立即继续执行\n"
    "\n"
    "这是最重要的部分。\n"
    "\n"
    "直接告诉接手方，分两小节写：\n"
    "\n"
    "**第一步（马上做）** —— 具体到文件 / 函数 / 命令：\n"
    "\n"
    "* 现在第一步做什么\n"
    "* 修改哪个文件 / 函数\n"
    "* 改成什么\n"
    "* 执行什么命令\n"
    "* 使用哪个 Job / Subagent / PID / 句柄\n"
    "* 哪些结果直接复用\n"
    "* 哪些不要重复\n"
    "\n"
    "**再往后（第 N+1、N+2 步）** —— 一两句说清路线：\n"
    "\n"
    "* 第一步做完接着做什么\n"
    "* 中间有没有需要用户拍板的关口（有就写明卡在哪、要用户答什么）\n"
    "* 到哪一步算这条线走完\n"
    "\n"
    "不要写“继续看看”“进一步分析”“然后处理”。\n"
    "\n"
    "必须达到**接手方看完即可开始执行**的程度。\n"
    "\n"
    "### 5. 验收\n"
    "\n"
    "写客观、可检查的完成条件：\n"
    "\n"
    "* 文件 / 内容\n"
    "* 命令 / exit code\n"
    "* 测试结果\n"
    "* 数据变化\n"
    "* 进程状态\n"
    "* 输出文件\n"
    "* UI 实际结果\n"
    "\n"
    "不能写“确保没问题”“功能正常”这种无法直接检查的结论。\n"
    "\n"
    "## 硬规则\n"
    "\n"
    "* 只写亲自读取、搜索、修改、执行、测试或验证过的事实。\n"
    "* 函数名、变量名、路径、命令、参数、Job、PID、句柄、输出、报错原文按实际字符写。\n"
    "* 不确定或未验证必须标记“未验证”，不得编造。\n"
    "* 实际状态与交接文本冲突时，实际状态优先；只重新检查受影响部分。\n"
    "* 已完成且状态未变化的工作直接复用，不重复劳动。\n"
    "* 正在运行的任务必须写实际状态以及 PID / 句柄 / Job（如果存在）。\n"
    "* 任务名与步号必须写全（`任务名@第N步`）。用户报问题时只会说这个，别用「刚才那步」。\n"
    "* 必须写**回滚点**：这一步开始之前可恢复的状态（git 提交号，或备份文件的实际路径）。没有就明写「无」。\n"
    "* 交接内容必须让接手方直接继续，不等待用户推动。\n"
    "* 全文 ≤3000 字；优先保留**立即继续执行、验收、当前状态、运行任务、已知坑**。\n"
    "* 最终只输出交接正文，不解释本提示。\n"
    "\n"
    "**目标：让下一个 AI 少问、少查、少重复，直接从当前断点继续完成任务。**")


POOL_NOTE_DEFAULT = (
    "11. **接手先定位断点，不做全盘重查**\n"
    "    先根据交接内容确定当前任务、实际断点和下一步，只检查会影响当前下一步的关键状态。不要为了“确认一下”重新扫描整个项目、重复读取全部文件或重复测试已确认结果。\n"
    "\n"
    "12. **实际状态优先于交接文本**\n"
    "    交接内容是恢复执行的状态基线，不是绝对事实。若实际文件、进程、任务或输出与交接一致，直接复用；只有发现变化、异常或无法确认一致时，重新检查受影响部分。\n"
    "\n"
    "13. **直接从断点继续**\n"
    "    已完成且状态未变化的步骤直接复用；不要重新启动已经完成的任务、重新修改已经完成的文件或重复执行没有变化的测试。立即执行交接中明确的下一步。\n"
    "\n"
    "14. **只接管真实任务**\n"
    "    只认领能够确认归属的任务。已有相关 Job、Subagent、PowerShell、进程或后台任务优先判断是否可以继续复用，避免重复启动。`job_list` 只有取得 lossless JSON 后才能从进程表移除任务。\n"
    "\n"
    "15. **后台任务保持可追踪**\n"
    "    自己启动的后台任务记录实际 PID、句柄、Job 和用途。完成或不再需要时正常收尾，不留下无主进程。状态变化时只处理受影响任务。\n"
    "\n"
    "16. **台账只记录真实状态变化**\n"
    "    只有实际启动、接管或结束后台任务时才更新全局台账 `_pending.md`（绝对路径见〔格式提醒〕的本机目录段）。运行任务追加到「活跃」末尾：\n"
    "    `- [ ] owner | 名称 | 启动时间 | 用途 | 句柄`\n"
    "    `owner` 写**项目或模块名**（例如「元宝控制台」「账号池」），要能跨轮稳定复述；不要拿会变的东西（窗口号、时间戳、临时 id）当归属。\n"
    "    每轮表头把「活跃」**整栏**（含别的窗口的行）贴给你 —— 那是让你看见全局在跑什么、别重复起同一批任务；**只准改 owner 是你自己那几条**，别人的行一律不碰。完成后把该行移入「已收尾」。只做增量修改，不删除无关记录或重写整个文件。修改前先备份到〔格式提醒〕标出的「备份目录（回滚点）」。\n"
    "\n"
    "17. **交接异常先恢复状态**\n"
    "    发现上一 AI 遗留的错误、异常进程、未收尾任务、未回包或状态不一致时，先处理实际异常，再继续当前任务。不要在状态明确异常时直接依赖旧交接继续执行。\n"
    "\n"
    "18. **避免并发冲突**\n"
    "    启动或接管任务前确认不会产生文件、端口、进程、资源或输出冲突。无依赖且无冲突可以并行；有冲突则复用已有任务或等待其正常收尾。\n"
    "\n"
    "19. **连续完成，不等待用户推动**\n"
    "    确认断点后直接完成当前任务范围内剩余工作。遇到错误按第 7 条自动修复、重新执行和验证。只有缺少关键输入、必要权限或确实无法自行解决时才暂停询问用户。\n"
    "\n"
    "20. **一次收尾**\n"
    "    任务真正完成后一次性汇报实际完成内容、关键修改、验证结果、运行结果和剩余问题。正常执行过程中不频繁汇报，不把未经验证的内容描述为已完成。\n"
    "\n"
    "21. **每一步都要能被定位和回滚**\n"
    "    开工前先给这一步编号：`任务名@第N步`。说清「要改什么、怎么算成」；做完说清「实际结果」。\n"
    "    报问题时只用这个标识（`任务名@第N步`），不要用「刚才那步」「上一步」—— 接手方和用户按形容词是定位不到的。\n"
    "    动文件之前先确认回滚点：git 提交号，或把文件备份到〔格式提醒〕标出的「备份目录（回滚点）」（命名 `原名.任务N步.<时间戳>.bak`）。\n"
    "    没有回滚点就不动文件。有了回滚点，发现问题直接回到那一步，不要从头重做。\n"
    "    步骤记录只**追加**到〔格式提醒〕标出的「步骤台账」，一行一步：`任务名@第N步 | 时间 | 干了什么 | 判据/验证 | 回滚点`。只增不改，别人的行不碰。")

# 交接正文的上限：太长会挤掉接手方的正常上下文
HANDOFF_MAX = 12000
# 交接请求最多为「等老号冷却」睡这么久。wait_left() 里那个 2 秒的回完间隔不该
# 触发本地代写（实测 38 次本地代写里 35 次是这么来的）；真限流冷却超过这个数
# 才放弃、直接用本地代写，免得把接手那一轮挂太久。
HANDOFF_COOL_MAX = 20.0
  # 2026-09-22 由 8000 提到 12000。全日志实测原始交接最大 8925 字
  # （17:25:14 779），次大 8568（01:23:18 310）—— 8000 时最近 20 次
  # 撞了 2 次。上游输出天花板远高于此：同窗口最长正文 31304 字且结尾
  # 完整（020 22:02:34），top15 没有一条被上游砍断，桥也不设 max_tokens。
  # 所以这个数纯粹是「给接手方留多少上下文」的取舍，不是上游限制。
  # 12000 字 ≈3000 token，占 131072 窗口的 2.3%，比实测最大值留 3000 字余量。

# 压缩检查点的抬头。客户端定期让模型输出这种 8 段式状态快照
# （## Primary Request and Intent 开头），实测 19 次、1.3万~3.7万字。
# 它是质量最高的交接材料 —— 比任何一条普通回复都全，而且本来就在产生。
CHECKPOINT_HEAD = "## Primary Request and Intent"

# 代写交接里最多放多少检查点。检查点原文可到 3.7 万字（~9k token），
# 全塞进去会挤掉接手方的正常上下文；16000 字（~4k token）够覆盖
# 它 8 段里的主要部分，头 60% 保任务/概念，尾 40% 保当前/下一步。
CHECKPOINT_MAX = 16000

# 换号时让老号生成「检查点」的指令 —— 产 8 段式状态快照。换号时桥自己发，
# 不等 dsh 到 80% 才压，拿到的是新鲜检查点，再上传成「检查点.txt」附件替代
# 「上下文.txt」原始历史。
#
# 2026-09-24 改（用户口径）：**首句不再照抄 dsh 的 COMPACTION_INSTRUCTION**。
# 原来这里逐字用了 dsh 那句 "You are now acting as a compaction engine for this
# AI coding assistant. ..." —— 于是桥自己发的检查点指令躺在会话历史里，下一轮
# 被自己的 compact_probe() 按同一句话认成「dsh 发起的压缩调用」，106 次统计全错。
# 判据虽然已经拆掉，这句措辞仍然是个雷：以后谁再加字符串匹配就会再踩一次。
# 8 段式结构说明保持原样（模型靠它产出格式），只换掉开头那句自我描述。
CHECKPOINT_INSTRUCTION = 'Produce a state snapshot of the conversation ABOVE. The snapshot is read back as the working state itself, so write it as an impersonal statement of fact - not as a handoff, not addressed to anyone, with no "you" and no "next model".\n\nOutput EXACTLY the Markdown structure below: keep every section, in order. Use terse bullets, not prose paragraphs. Write "(none)" for an empty section - never drop a section.\n\nSTART with this section, before all the others - it is a machine-readable cursor and must be copied VERBATIM from the ledger, never paraphrased:\n\n## Progress Cursor\n- task: [task name exactly as it appears in the ledger]\n- step: [the step id, copied verbatim from the ledger]\n- last_verified: [the most recent check that actually passed, with its evidence]\n- next_action: [the single next action]\n\nUse "(none)" for any cursor field you cannot fill from the ledger. Do NOT invent a step id.\n\n## Primary Request and Intent\n- [the user\'s original and evolving goals; quote verbatim where the exact wording matters]\n\n## Key Technical Concepts\n- [technologies, frameworks, patterns, and conventions in play]\n\n## Files and Code\n- [exact path: why it matters, key changes or snippets]\n\n## Errors and Fixes\n- [error: how it was resolved, plus any related user feedback]\n\n## Pending Jobs\n- [explicitly requested work not yet completed]\n\n## Current Work\n- [precisely what was in progress at this snapshot]\n\n## Next Step\n- [the single next action, directly in line with the most recent request, or "(none)"]\n\n## Critical Context\n- [decisions and their rationale, constraints, user preferences, open questions, data needed to continue]\n\nRules:\n- Write concise engineering prose. Preserve exact file paths, commands, error strings, identifiers, numeric values, function signatures, and syntax fragments.\n- Capture user feedback and explicit instructions faithfully, especially corrections.\n- Describe the WORK ONLY. Do not record that a snapshot/compaction/context-retrieval tool was invoked - that is tooling, not work. If the only visible action is such a tool call, write "(none)" for Current Work and Next Step.\n- Do NOT mention this request or that the context was compacted.\n- Output only the snapshot text: do not call any tool or take any other action.'



CHUNK_REMINDER = (
    "1. **文件修改与参数限制**\n"
    "   文件修改必须使用工具；单参数值 ≤4000 字符，超过则分多轮写入，不得截断、省略或伪造。\n"
    "\n"
    "2. **严格限制修改范围**\n"
    "   改前先读目标文件，别凭记忆猜。保持现有结构、命名、接口、数据格式、调用关系和有效设计。不顺手重构、不修改无关代码、不升级依赖、不格式化无关文件。\n"
    "\n"
    "3. **先整体判断，再连续执行**\n"
    "   任务开始时一次性理解目标、现状、依赖、主要步骤、风险和验收条件，形成执行计划后连续推进。正常执行期间不要反复重新规划；只有出现新情况、状态变化、错误、依赖变化或原计划无法继续时才重新判断。\n"
    "\n"
    "4. **实际状态优先**\n"
    "   首次处理目标文件必须读取实际内容。已读取且确认期间未变化时直接复用。只有文件可能被其他进程/AI 修改、准备覆盖写入、状态发生变化或无法确认一致时才重新读取。禁止根据文件名、旧上下文或搜索结果猜测实际内容。\n"
    "\n"
    "5. **工具调用以有效推进为准**\n"
    "   已有信息足够就继续执行；相同信息不重复获取；能一次完成的操作尽量合并；无依赖且无冲突的任务可以并行。减少的是无效、重复调用，不得为了减少调用而省略必要操作。\n"
    "\n"
    "6. **复杂操作优先脚本化**\n"
    "   复杂命令、JSON、正则、特殊字符、中文路径、多行内容等优先使用脚本执行，减少命令行转义错误和反复试错。脚本先落盘成文件，别内联长 JSON/字符串。\n"
    "\n"
    "7. **执行结果自动控制流程**\n"
    "   执行代码、脚本、命令、测试或构建时预置明确成功条件，由执行程序根据结果控制流程：\n"
    "   **成功且达到成功条件 → 继续；报错、异常退出、非预期返回码或关键结果缺失 → 立即停止依赖该结果的后续步骤，并把完整执行结果回执当前 AI。**\n"
    "   当前 AI 自动分析、修复、重新执行并验证；成功后从中断点继续。错误未解决时不得继续依赖错误结果执行。\n"
    "   这里的“返回”是返回当前 AI 的执行循环，不是返回用户。\n"
    "\n"
    "8. **阶段性有效验证**\n"
    "   验证实际修改和实际运行结果，但不要对每个微小动作重复验证。按当前任务的阶段、依赖关系和风险进行必要验证；状态未变化且结果已确认时直接复用。验证失败立即进入第 7 条的修复流程。\n"
    "\n"
    "9. **自主连续完成**\n"
    "   当前任务范围内能够自行判断、执行、修复和验证的直接完成，不采用“完成一步→等待用户→再做下一步”。只有缺少关键输入、必要权限或确实无法自行解决时才询问用户。\n"
    "\n"
    "10. **速度不能牺牲质量**\n"
    "    优化目标是减少无效等待、重复读取、重复执行和用户交互，而不是机械追求最低工具调用次数。必要的读取、执行、测试、验证和修复不得省略。任务未真正完成不得提前结束；除非用户要求，不频繁汇报过程，完成后统一汇报。")


# 关掉思考重发时贴在 prompt 末尾的一句。不重传整段上下文，只加这一句 ——
# 省一次全量重传，也让上游看到的是「上一条空回复之后被要求重来」。
NUDGE_BODY = ("\n\n【必须给正文】你上一条只输出了思考，正文是空的。"
              "现在把结论直接写出来：要调工具就输出工具调用 JSON 块，"
              "不要复述推理过程。")


def _looks_like_question(text):
    """正文是在向用户提问（要选择/要确认）时，不要贴 NUDGE 重发。

    2026-09-27 第336步（用户实测）：模型问「你要 A 还是 B？」这类选择问句时，
    正文非空、没有工具调用，正好踩中 no_call_retry 分支。NUDGE_BODY 的措辞
    是给「只思考不落笔」那条分支写的（「你上一条正文是空的」），模型收到后
    把「等你选」读成「催我干活」，自己挑一个往下做 —— 表现为「我还没选，
    它就继续执行了」。

    只认「明确在问」的形态，宁可漏判也不误判：
      · 整段以问号收尾（？/ ?）
      · 尾部 200 字里命中选择/确认类措辞
    """
    t = (text or "").strip()
    if not t:
        return False
    if t[-1:] in "？?":
        return True
    tail = t[-200:]
    for kw in ("请选择", "请选", "要不要", "要我", "你希望", "请问",
               "选哪", "选一个", "哪个", "确认一下"):
        if kw in tail:
            return True
    return False


# ===== 本机目录证据（2026-09-27 第354步）=====
# 用户口径：可能用别的电脑连这个桥，各机根目录不同，不能让提醒词里写死的
# 家机路径把别台的 AI 带偏。桥自己从请求里找证据，算出「这台机器是谁、根在哪」，
# 每轮贴一段事实。只报告，不改配置、不替换正文、不建目录。
#
# 证据优先级（已核实 dsh 从不发 sessionId，见 window_tag 注释，所以不靠它）：
#   1) system 段里的 working directory / checkout —— 第一轮就有，最稳；
#   2) 本轮工具调用参数与工具结果里的绝对路径 —— 用来交叉印证；
#   3) 都没有就用桥自己的 workdir（说明是本机，或对方没给线索）。

# 只认真实存在的盘符前缀，避免把 URL、注释里的斜杠串当成路径。
_ROOT_RE = re.compile(
    # 2026-10-02 修：原来是
    #   ([A-Za-z]:[\\/](?:[^\r\n\"'<>|*?]|[\\/])*?)
    # 而 [^\r\n\"'<>|*?] **已经包含 / 和 \**，后面那个 |[\\/] 是冗余分支。
    # 于是每个斜杠有两种吃法，找不到 _work 锚点时要试 2^n 种组合
    # （实测斜杠 22 个 = 826ms，每多 2 个翻 4 倍）。
    # test 组消息里全是长路径，斜杠数 >30 -> 单次 search 几分钟，
    # 桥一个线程占满 GIL → 端口在听但连不上（半死）。
    # 删掉冗余分支后语义不变（已用 8 个用例逐一对比）。
    r"([A-Za-z]:[\\/][^\r\n\"'<>|*?]*?)"
    r"(?=[\\/](?:_work|_pending\.md|_steps\.md|_dsh3081)(?:[\\/]|$))",
    re.IGNORECASE)


def _norm_root(p):
    """统一成 C:/a/b 形式：反斜杠转正斜杠、合并重复斜杠、去尾斜杠。

    2026-10-01 第455步（实测踩到）：原来只做了「反斜杠转正斜杠」，
    **没合并重复斜杠** —— 用户输 F:\a\b（或从资源管理器复制来的路径）
    会变成 'F://a//b'，这个路径发给模型就废了。
    测「更新组路径」时才发现：switch_set_root('剪辑', r'F:\a\b')
    返回 'F://a//b'。

    合并要小心别碰盘符：「C://」是 "盘符 + 斜杠"，规范化后应是 "C:/"。
    所以按段拆再拼，而不是简单 replace("//", "/")。
    """
    s = str(p or "").strip().strip('"').strip("'")
    if not s or len(s) < 3 or s[1] != ":":
        return ""
    s = s.replace(chr(92), "/")
    drive = s[:2]                       # "C:"
    rest = s[2:]
    parts = [x for x in rest.split("/") if x]
    if not parts:
        return ""
    return drive + "/" + "/".join(parts)


def _own_root():
    """桥自己这台机器的根目录 —— ini 里的 workdir。

    2026-09-27 第406步：提示词（常驻/池提醒、格式提醒、附件说明）里写死的
    绝对路径就是按这个根写的。别台电脑接入时要把这个前缀换成对端的根，
    所以这里必须给出「桥本机的根」作为被替换对象。
    """
    try:
        return _norm_root((_ini_read().get("codex") or {}).get("workdir") or "")
    except Exception:            # noqa: BLE001
        return ""


def retarget_roots(text, peer_root):
    """把文本里桥本机的根目录前缀换成对端根目录。

    2026-09-27 第406步（用户口径：「主要提示有硬编目录」）。提示词里写死的
    C:/Users/Lenovo/Desktop/teste/... 是本机根；B 机接入时模型会照抄这个路径，
    在 A 机的目录名下干活。这里把前缀整体换成这一轮从请求里认出来的对端根。

    只在 peer_root 非空、与桥本机根不同、且文本里确实出现时替换；否则原样返回。
    正反斜杠与大小写都认。
    """
    t = str(text or "")
    own = _own_root()
    pr = _norm_root(peer_root) if peer_root else ""
    if not t or not own or not pr or pr == own:
        return t
    pat = re.compile(re.escape(own).replace("/", r"[\\/]"), re.IGNORECASE)
    return pat.sub(lambda _m: pr, t)


# 2026-09-27 第399步（用户口径）：别台电脑用这个桥时，它自己那份 system 段里
# 会写「你的工作目录是 X」。X 下头还没有 _work/_pending.md 时，上面那个只认
# 锚点的 _ROOT_RE 抽不出来，于是退回桥本机的 workdir —— B 机就拿到了 A 机的
# 路径，新建分组会在 A 机的目录名下干活。这里补一条：工作目录声明里的路径
# 直接认，不看它下头有没有锚点。
_WD_RE = re.compile(
    r"(?:working[ _]?directory|workingDirectory|workdir|cwd|工作目录)"
    r"\s*(?:is|是|为|[:=])?\s*"
    r"([A-Za-z]:[\\/][^\r\n\"'<>|*?]+)",
    re.IGNORECASE)


def _evid_root_from_text(text):
    """从一段文本里找根目录：先认工作目录声明，再看 _work 这类已知锚点。"""
    if not text:
        return ""
    m = _WD_RE.search(text)
    if m:
        # 声明后面常跟句号/逗号/右括号，剥掉再规范化。
        return _norm_root(m.group(1).rstrip(".,;:)]}>"))
    m = _ROOT_RE.search(text)
    if m:
        return _norm_root(m.group(1))
    return ""


def _evid_root_from_messages(messages):
    """从本轮 messages 里抽根目录。返回 (root, 来源说明)。

    只在 system/工具结果里找 —— user 正文里的路径是聊天内容，不算证据。
    """
    anchor, scan = "", ""
    for m in (messages or []):
        if not isinstance(m, dict):
            continue
        role = str(m.get("role") or "")
        c = m.get("content")
        if isinstance(c, list):
            parts = []
            for seg in c:
                if isinstance(seg, dict):
                    parts.append(str(seg.get("text") or ""))
                else:
                    parts.append(str(seg))
            c = " ".join(parts)
        c = str(c or "")
        if not c:
            continue
        if role == "system" and not anchor:
            anchor = _evid_root_from_text(c)
            if anchor:
                scan = "system 段的工作目录"
        # 工具结果 / 助手正文里的绝对路径也算旁证，但优先级低于 system
        if not scan and role in ("tool", "function", "assistant"):
            hit = _evid_root_from_text(c)
            if hit:
                scan = "本轮工具调用/结果里的路径"
    return anchor or "", scan


def _stray_group_dirs():
    """找出「平行空壳目录」—— 窗口照老路径自己建的那种。

    2026-10-02 第481步（用户贴出目录列表：「都归到这里边了把」）：

    实测窗口在 C:/Users/Lenovo/Desktop/teste/test/ 下建了 7 个空目录
    （_bak/_sys/_临时/归档/素材/脚本/输出），**一个文件都没有**。
    而真正的组目录是 .../teste/_work/test/（2350 个文件 4.5GB）。

    **危害：空目录比没有更坏** —— 它让窗口以为自己找对了地方，
    于是在里面翻来翻去，翻不到就再建、再翻。实测因此连续几十轮空转
    （189 次重试错路径）。

    判据只用**位置**，不猜内容：
        正牌组目录 = WORK_ROOT / "_work" / <组名>
        平行空壳   = WORK_ROOT / <组名>          <- 第470步前的老算法
    所以凡是在 WORK_ROOT 下、名字等于某个组名、且**不是 _work 那条路**的，
    就是空壳。（我在这个函数上绕了三次：先按 MSGS_FILE.parent 找桥根
    -> 推成 Desktop；再按「空目录」判 -> 被 _sys 空子目录骗过。
    最后还是回到「位置」这一条最可靠。）

    返回 [{"path": str, "files": int}]。
    """
    out = []
    try:
        wr = pathlib.Path(str(WORK_ROOT))
        names = set()
        try:
            for gg in (_groups_root().get("groups") or []):
                nm = str(gg.get("name") or "").strip()
                if nm:
                    names.add(nm)
        except BaseException:        # noqa: BLE001
            pass
        for cand in wr.iterdir():
            if not cand.is_dir():
                continue
            if cand.name.startswith("_") or cand.name.startswith("."):
                continue
            if cand.name not in names:
                continue
            n = sum(1 for x in cand.rglob("*") if x.is_file())
            out.append({"path": str(cand), "files": n})
    except BaseException:            # noqa: BLE001
        pass
    return out


def dir_block_for(root, group_name="", slug=""):
    """生成〔本机目录〕段。root 为空则不生成。

    group_name 为空表示这一号不在任何组里（固定号）。

    2026-10-01 第454步（用户口径「以组命名目录…保持每个分组的独立性」）：
    **组内一切都在 <根>/<组名>/ 下**，不再散在根目录里。
      <根>/<组名>/_临时/     本组临时文件
      <根>/<组名>/_bak/      回滚点备份
      <根>/<组名>/脚本 输出 归档
      <根>/<组名>/_steps.md  本组步骤台账（独立，不跟别的组混）
      <根>/<组名>/_pending.md 本组全局台账
    这么分的原因见 group_root_of 的注释：所有组挤一个目录会串。
    """
    r = _norm_root(root)
    if not r:
        return ""
    g = "".join(ch for ch in str(group_name or "").strip()
                 if ch.isalnum() or ch in "_-")
    L = ["〔本机目录〕以下路径是**你这台机器**上的，不是别人的：",
         "  本机根目录 = " + r]
    if g:
        d = r + "/" + g
        # 2026-10-01 第461步（用户口径「分组下准备建几个目录都存放什么文件
        #   是不是干净明了」）：**按「谁写的」分层，一眼看出哪些能删。**
        #
        # 原来桥的数据（_relay_*.jsonl）和模型的产出（脚本/输出/临时）混在
        # 同一层，看不出归属，也就不知道哪些能删、哪些碰不得。
        # 现在分三层：
        #   _sys/   桥写的运行记录 —— **只读，别动，删了交接就瞎了**
        #   _bak/   备份 —— 桥和你都可能写
        #   其余    模型的工作台（脚本/输出/素材/归档/临时）
        L.append("  当前组（=当前项目）= " + g)
        L.append("  **本组工作目录 = " + d + "**（桥已建好）")
        L.append("  你就在这个目录下干活。里面分两区：")
        L.append("  ── 工作台（随便写、随便删）──")
        L.append("     " + d + "/_临时/      过程中的临时文件")
        L.append("     " + d + "/脚本/       脚本")
        L.append("     " + d + "/输出/       产物")
        L.append("     " + d + "/素材/       输入材料")
        L.append("     " + d + "/归档/       做完了的东西")
        L.append("  ── 桥的运行记录（**别动，删了交接就瞎**）──")
        L.append("     " + d + "/_sys/              本组事件流/消息/回复/结论存档")
        # 2026-10-01 第477步（用户实测报错「glob search failed: rg:
        #   C:/Users/Lenovo/Desktop/teste/test: 系统找不到指定的文件」）：
        # **这两行原来漏了 _sys/，报的是不存在的路径。**
        # 第469步我把 _steps.md / _pending.md 统一挪进 _sys/ 了（GRP_KINDS 改了），
        # 但这段提示词没跟着改 —— 窗口照着它去 <组目录>/_steps.md 找，文件不在，
        # 于是自己瞎猜路径（实测它猜成 teste/test，并在一条消息里重试了 189 次）。
        # 教训：路径**唯一的来源**必须跟实际落盘位置一致，否则它比不给还糟。
        L.append("     " + d + "/_sys/_steps.md    步骤台账（只增不改）")
        L.append("     " + d + "/_sys/_pending.md  全局台账（只改 owner 是自己的行）")
        L.append("  备份（回滚点）= " + d + "/_bak/"
                 + "（命名 原名.任务N步.<时间戳>.bak）")
        L.append("  **本组的一切产出都写在本组工作目录下，不要写别处、不要跨组。**")
    else:
        d = r + "/_work/_pinned/" + (slug or "?")
        L.append("  你不在任何组里（固定号" + (slug or "?") + "）")
        L.append("  工作目录 = " + d + "（不存在就自己建）")
        L.append("  临时文件 = " + d + "/_临时/")
    if g:
        # 2026-10-01 第478步（用户连贴两次报错）：**明确点名作废路径，
        # 光给正确路径不够。**
        #
        # 实测：窗口上下文里堆了 189 处 `C:/Users/Lenovo/Desktop/teste/test`
        # （那是第470步前的老算法算出来的），它照着那些**历史残留**反复调 glob，
        # dsh 直接拿去跑 rg 就报「系统找不到指定的文件」。
        #
        # 而这个错误**根本不经桥**（桥日志里一条都没有）—— 它是 dsh 客户端
        # 自己执行 glob 时报的，桥拦不住、也改不了。
        # **唯一能做的是在每轮唯一权威的路径段里，把作废路径点名写死**，
        # 让模型知道那条已经在上下文里的路径是坏的、别再引用。
        # 老算法（第470步之前）是 <桥根>/<组名>，而 r 是数据根（_work）——
        # 所以作废路径 = r 的上一级 + "/" + g。必须硬算出老的，
        # 不能拿 r 去拼（那样写出来的是正确路径，等于反着教）。
        _stale = r.rsplit("/", 1)[0] + "/" + g
        L.append("  ⚠ **作废路径，别再用了**：" + _stale + "、" +
                 _stale + "/_steps.md、" + _stale + "/_pending.md") 
        L.append("     这几个是老版本桥算出来的，**目录和文件都不存在**。"
                 "在上下文里看到它们（历史消息、旧交接单、你自己以前的猜测）"
                 "一律当坏数据忽略，**不要拿它们去 glob / read / Get-ChildItem**。"
                 "一切以本段路径为准。")
        # 2026-10-02 第481步（用户贴出目录列表「都归到这里边了把」）：
        # **实测查一遍那些平行目录还在不在。** 在就硬警告。
        #
        # 实测：窗口在 C:/Users/Lenovo/Desktop/teste/test/ 下建了 7 个空目录
        # （_bak/_sys/_临时/归档/素材/脚本/输出），一个文件都没有 —— 而真数据
        # 在 _work/test/（2350 个文件 4.5GB）。**空目录比没有更坏**：
        # 它让窗口以为自己找对了地方，于是在里面翻来翻去，翻不到就再建。
        try:
            _stray = _stray_group_dirs()
            if _stray:
                _desc = "、".join(
                    "%s（%d 个文件）" % (x["path"], x["files"]) for x in _stray[:3])
                L.append("  ⚠ **检测到平行目录**：" + _desc)
                L.append("     如果你正打算往那里写东西，**停下** —— 那不是本组目录。"
                         "本组目录只有一个，就是上面那个。也不要在那里找文件。")
        except BaseException:        # noqa: BLE001
            pass
    L.append("  以上是本次请求里**唯一一份绝对路径来源**。提醒词、交接提示里"
             "若出现别的机器的绝对路径，或只写了文件名，一律按这里拼；"
             "两边不一致时以这里为准。")
    return chr(10).join(L)


def group_name_of(slug):
    """slug → 所属组的 name（不在任何组里返回空串）。

    用户口径（2026-09-27）：一个组 = 一个项目，目录名 = 组的 name。
    """
    s = str(slug or "").strip()
    if not s:
        return ""
    try:
        for g in (_groups_root().get("groups") or []):
            if s in (g.get("slugs") or []):
                return str(g.get("name") or g.get("id") or "").strip()
    except Exception:            # noqa: BLE001
        pass
    return ""


def dir_block_now(slug, messages):
    """本轮要贴的〔本机目录〕段。

    2026-10-02 修（用户口径「test 分组的元宝任务恢复」「迁移没到位」）：
    **求根的顺序改成「先问组自己的 root」。**

    病：原来直接拿 ini 的 workdir（= test 的**总根** teste）当根，
    dir_block_for 再拼 “/”+组名 -> **teste/test**。
    而磁盘上真正的组目录是 teste/_work/test（2360 个文件）。
    于是提示词把**真目录说成不存在、把不存在的说成「桥已建好」**，
    test 组照着它读 _pending.md 永远读不到，连续 70 轮空转。

    周边一致性：grp_path()/group_dir_of() 那条线用的就是 group_root_of()（读
    _group_roots.json / _pool_groups.json，两处都写着 teste/_work）。
    本函数原来绕开了它，所以“台账落对地方，提示词指到别处”。
    """
    root, src = _evid_root_from_messages(messages)
    head = ""
    if not root:
        # 先问组自己的 root（与 grp_path 同源）
        try:
            _g0 = group_name_of(slug)
            if _g0:
                root = _norm_root(group_root_of(_g0, slug=slug))
        except BaseException:        # noqa: BLE001
            root = ""
    if not root:
        try:
            root = _norm_root((_ini_read().get("codex") or {}).get("workdir") or "")
        except Exception:        # noqa: BLE001
            root = ""
        # 2026-10-01 第421步（用户口径：「我桥走哪个模型本地就在工作区根据模型走
        # 哪个目录，你提示词给我改错了」）：原来这句叫模型「如果你不是这台机器，
        # 请以自己环境的实际根目录为准」—— 它跟本段末尾那句「以上是本轮唯一的
        # 绝对路径来源」**直接打架**，等于当场授权模型自己另选目录。
        # 串号就是这么来的：模型绕开〔本机目录〕段，按它自己记忆里的路径干活
        # （实证 .state/ds_bridge.log:32339，2026-09-28 06:31，号 309：
        #  「环境与交接提示不符：本机目录不存在，真实工作区在 F:/test」——
        #  本机根本没有 F 盘）。改成只陈述「这一段是按哪条证据算的」，不给出
        #  第二条路线：目录归属由 slug→组 决定（见 group_name_of），模型没得选。
        head = ("（本段按本条请求自带的证据算出：本轮 messages 里没有工作目录声明，"
                "故取桥本机配置的 workdir。它仍然是**本机**的根，不是别台的。）"
                + chr(10))
    # 2026-10-01 第459步：**顺手把本轮的根记进上下文。**
    # anchor_paths() 要用它把模型填的相对路径锚定成绝对路径 ——
    # 见 set_request_root 的注释：与其反复提醒模型，不如在出口纠一次。
    # 放在这里是因为这是**唯一一处**算出「本轮本机根」的地方，
    # 两处都从这里取，不会出现两个来源打架。
    _gname = group_name_of(slug)
    try:
        set_request_root(root, group=_gname, slug=slug)
    except BaseException:            # noqa: BLE001
        pass
    # 2026-10-01 第461步：**把组目录骨架建出来。**
    # 提示词里列的那七个目录必须真实存在，否则模型写进去就报错。
    # 每轮调一次是幂等的（exist_ok），开销就是几次 stat —— 可忽略。
    if _gname:
        try:
            grp_ensure(_gname, slug=slug)
        except BaseException:        # noqa: BLE001
            pass
    blk = dir_block_for(root, group_name_of(slug), slug)
    return (head + blk) if blk else ""


# ===== 隐式切组命令（2026-09-27 第356步，用户设计）=====
# 用户口径：在对话里发 ##切组##，桥自己认出这一发，不走上游，直接用 dsh
# 自己的 ask_user_question 弹选择框让用户点。全程桥自己造响应，不问网页。
#
# 判据全在 messages 里，桥不存任何状态：多窗口并发、桥重启都不会串。
# 问过工作区的号（问一次就够，避免反复骚扰）
_WS_ASKED = set()

# 桥问工作区时埋在命令里的标记（认自己的结果，不误抓模型的）
_WS_MARK = "__DSH_WS_PROBE__"

SWITCH_CMD = "##切组##"
SWITCH_TOOL = "ask_user_question"
SWITCH_NEW = "【新建分组】"
# 2026-09-27 第365步：对话框被关掉 / 勾了空就收手，别再原样重问。
# 之前「空答复就重问同一问」会无限循环：用户连按两次取消，
# 桥就一直重发同一个勾选框，看着像卡死。
SWITCH_CANCEL = "对话框被关掉了，这次没改。要重来就再发 ##切组##。"
# 2026-09-27 第366步（用户口径）：第 1 问里多一个「查看分组状态」。
# 点它不出轮次 —— 它不算一次回答（_switch_answers 会跳过），看完原样回到
# 第 1 问，所以连点几次也不会把轮次带偏。
SWITCH_INFO = "【查看分组状态】"
# 2026-09-27 第372步（用户口径）：要能删分组。删的是分组本身，号不受影响。
SWITCH_DEL = "【删除分组】"
# 2026-10-01 第455步（用户口径「切组还差一个命令 就是更新组路径
#   就是在已有的分组换电脑直接更新路径就ok了」）：
# **换电脑不用删组重建，直接更新路径。**
# 走和删除一样的多轮流程：选组 -> 选新根 -> 确认。
SWITCH_ROOT = "【更新组路径】"
SWITCH_ROOT_OK = "确认更新"

# ---- ##路由## 隐式查地址命令（2026-09-27 第373步）----
# 用户口径：「新增 ##路由## 选择内地址就把内网的所有地址贴出来 如果选择
# 局域网就输出局域网地址 如果选择外网就贴外网地址 监控台及 dsh地址」。
ROUTE_CMD = "##路由##"
ROUTE_IN = "【内网】"
ROUTE_LAN = "【局域网】"
ROUTE_WAN = "【外网】"
ROUTE_CANCEL = "对话框被关掉了，这次没查。要重来就再发 ##路由##。"
# ---- ##模型## 隐式查模型命令（2026-09-27 第419步）----
# 用户口径：「##模型## 获取配置模型，手动去配置也行，桥给配置也行」。
# 跟 ##路由## 同位置、同单轮形态：不弹框，桥自己把清单和配置贴出来。
MODEL_CMD = "##模型##"
MODEL_CANCEL = "对话框被关掉了，这次没查。要重来就再发 ##模型##。"
# dsh 界面（DeepSeek Harness Web GUI）听的端口。
DSH_PORT = 3080


def _msg_text(m):
    c = m.get("content")
    if isinstance(c, list):
        c = " ".join(str(s.get("text") or "") if isinstance(s, dict)
                     else str(s) for s in c)
    return str(c or "")


def _last_user_text(messages):
    for m in reversed(messages or []):
        if isinstance(m, dict) and str(m.get("role") or "") == "user":
            return _msg_text(m)
    return ""


def _switch_start(messages):
    """最后一条含 ##切组## 的 user 消息下标；没有就返回 len(messages)。

    这一轮切组只看它之后的材料：重发 ##切组## 即等于重开一轮，
    别的对话、上一回的答案都不会混进来。
    """
    msgs = messages or []
    for i in range(len(msgs) - 1, -1, -1):
        m = msgs[i]
        if not isinstance(m, dict):
            continue
        if str(m.get("role") or "") == "user" \
                and SWITCH_CMD in _msg_text(m):
            return i
    return len(msgs)


def _looks_like_answer(t):
    """dsh 把 ask_user_question 的答复塞回来的样子。"""
    t = t or ""
    return ('"answers"' in t) or ('"selected"' in t) \
        or ('"id":"swq' in t) or ('"id": "swq' in t)


def _switch_unpack(t):
    """把 dsh 的答复正文摊平成「用户点了什么」的纯文本。

    形如 {"answers":[{"id":"swq1","selected":["组:g1"]}]} 就取出
    selected；取不出东西就原样返回，让后面的匹配照旧跑。
    """
    t = t or ""
    # 2026-09-27 第365步：dsh 关掉对话框时回的是
    # Error: the user cancelled ask_user_question 这类文本，
    # 当成「空答复」处理，后面的轮次逻辑就知道该收手了。
    low = t.lower()
    if ("cancel" in low) or ("取消" in t):
        return ""
    if not _looks_like_answer(t):
        return t
    try:
        o = json.loads(t)
    except Exception:            # noqa: BLE001
        return t
    if not isinstance(o, dict):
        return t
    parts = []
    for a in (o.get("answers") or []):
        if not isinstance(a, dict):
            continue
        sel = a.get("selected")
        if sel is None:
            sel = a.get("answer")
        if isinstance(sel, str):
            parts.append(sel)
        elif isinstance(sel, (list, tuple)):
            parts.extend(str(x) for x in sel)
        # 2026-09-27 第370步：dsh 把「自己在输入框里敲的名字」放在 custom 里
        # （实测答复形如 {"selected":[],"custom":"test"}）。原来只收
        # label/value/text，这条答复被摊平成空串，第4轮判成「名字没给全」，
        # 用户明明写了名字却收到「对话框被关掉了」。custom 一并收下。
        for k in ("custom", "label", "value", "text"):
            v = a.get(k)
            if isinstance(v, str) and v and v not in parts:
                parts.append(v)
    # selected 全空 = 用户把框关了或什么都没勾，回空串让上层收手。
    return "\n".join(parts) if parts else ""


def _switch_count(messages):
    """这一轮切组问到第几问了：轮次 = 这一轮里已经收回来的答复条数。

    2026-09-27 第363步：先反向定位最后一条含 ##切组## 的 user 消息，
    只看它之后的材料。原来数的是整个 messages，同一个窗口连发几次
    ##切组## 之后轮次会一路往上跳，永远回不到第 1 问。

    2026-09-27 第364步：改成只认「答复条数」。实测用户连发 ##切组##
    只会收到「一个号都没认出来」，根因是 dsh 重发同一发请求时把同一份
    swq tool_call 又存了一遍 —— 按「个数」数会每重发一次多跳一问，
    跳到第 3 问之后就再也对不上，每一发都掉进「没认出来」的分支。
    数答复则不受重发、不落 tool_call 的历史影响：收到几个回答就问下一问。
    """
    return len(_switch_answers(messages))


def _switch_answer(messages):
    """最后一条工具结果的正文 = 用户刚点的东西。"""
    for m in reversed(messages or []):
        if not isinstance(m, dict):
            continue
        r = str(m.get("role") or "")
        if r in ("tool", "function"):
            return _msg_text(m)
        if r == "user":
            return ""
    return ""


def switch_groups():
    """可选分组：配置里 enabled 的组，全部列出来，不做任何隐式排除。

    2026-09-27 第368步：删掉残留的 dflt 变量和「g1 不由人选」的旧注释 ——
    那是硬排除 g1 时代的遗留，第358步改成只按 enabled 过滤后就没人用了。
    """
    out = []
    try:
        cfg = _groups_root()
        for g in (cfg.get("groups") or []):
            gid = str(g.get("id") or "")
            # 2026-09-27 第358步（用户实测：当前分组 test 没被识别到）。
            # 原来硬编码排除 g1 和 default，但用户唯一的组恰好就叫 g1，
            # 于是候选列表永远是空的。改成只按 enabled 过滤。
            if not g.get("enabled"):
                continue
            # 2026-10-01 第456步：**带上 root。**
            # 这里是第三个"按字段重建"的白名单（前两个：_groups_root、
            # group_pack 里的 gdef）—— 每重建一次就丢一次字段。
            # 实测踩到两次：先 _groups_root 丢 root，改完这里又丢，
            # 表现都是"配置里明明写了，界面/对话框显示成 (未设)"。
            # 凡是把配置对象"重新组装"的地方，都要把 root 带上。
            out.append({"id": gid,
                        "name": str(g.get("name") or gid),
                        "slugs": [str(s) for s in (g.get("slugs") or [])],
                        "root": str(g.get("root") or "")})
    except Exception:            # noqa: BLE001
        pass
    return out


def switch_q1(groups):
    # 2026-09-27 第371步（用户口径）：选项里不要露内部编号 g1/g2 —— 又难看，
    # 又容易被当成组名。label 只写组名；内部 id 仍旧放在 value 里：
    # 第2轮要靠它认组，@组id 的模型名也仍旧按 id 路由。
    opts = [{"label": "切到分组「%s」" % g["name"],
             "value": "组:" + g["id"],
             "description": "号：" + ("、".join(g["slugs"]) or "空")}
            for g in groups]
    opts.append({"label": SWITCH_NEW,
                 "value": SWITCH_NEW,
                 "description": "勾选几个号，新建一个轮询分组"})
    if groups:
        # 2026-10-01 第455步：有组才能更新路径。放在删除前面 ——
        # 换电脑时这是常用操作，删除是危险操作，危险的排最后。
        opts.append({"label": SWITCH_ROOT,
                     "value": SWITCH_ROOT,
                     "description": "换电脑了 / 工作区挪了：改这个组的工作目录，"
                                    "不用删组重建"})
        # 2026-09-27 第372步：有组才能删。排在新建后面，免得手滑点到。
        opts.append({"label": SWITCH_DEL,
                     "value": SWITCH_DEL,
                     "description": "删掉一个分组（号不受影响）"})
    opts.append({"label": SWITCH_INFO,
                 "value": SWITCH_INFO,
                 "description": "看看每个组现在有哪些号、目录在哪、接口地址"})
    q = ("现在没有可选分组。" if not groups else "") + "要切到哪一组？"
    return [{"id": "swq1", "header": "切换分组", "question": q,
             "options": opts, "multi_select": False}]


def switch_q2(slugs, taken):
    opts = []
    for s in slugs:
        opts.append({"label": "号 %s%s" % (s, "（已在别的组）" if s in taken else ""),
                     "value": s,
                     "description": "把它放进新分组"})
    opts.append({"label": SWITCH_INFO, "value": SWITCH_INFO,
                 "description": "看看每个组现在有哪些号、目录在哪、接口地址"})
    return [{"id": "swq2", "header": "勾选号",
             "question": "勾选要放进新分组的号（可多选）：",
             "options": opts, "multi_select": True}]


def switch_q3(picked):
    stem = "-".join(picked) if picked else "新组"
    return [{"id": "swq3", "header": "分组名",
             "question": "新分组叫什么名字？目录名就用它。"
                         "对话框能输入就直接写名字，不能就选一个现成的：",
             "options": [{"label": "组_" + stem, "value": "组_" + stem,
                          "description": "用勾选的号拼一个名字"},
                         {"label": "项目" + stem, "value": "项目" + stem,
                          "description": "用勾选的号拼一个名字"},
                         {"label": SWITCH_INFO, "value": SWITCH_INFO,
                          "description": "看看每个组现在有哪些号、目录在哪、接口地址"}],
             "multi_select": False}]


def switch_qdel(groups):
    """第372步：删哪一组。只删分组，号不受影响，会变成未分组。"""
    opts = [{"label": "删掉分组「%s」" % g["name"],
             "value": "删:" + g["id"],
             "description": "号：" + ("、".join(g["slugs"]) or "空")}
            for g in groups]
    opts.append({"label": SWITCH_INFO, "value": SWITCH_INFO,
                 "description": "看看每个组现在有哪些号、目录在哪、接口地址"})
    return [{"id": "swq9", "header": "删除分组",
             "question": "要删掉哪一组？只删分组，号会回到未分组。",
             "options": opts, "multi_select": False}]


def switch_qdel_ok(g):
    """第372步：删之前再确认一次 —— 删了要重新建，值得多点一下。"""
    return [{"id": "swq10", "header": "确认删除",
             "question": "确认删掉分组「%s」？号（%s）会变成未分组，账号本身不动。"
                         % (g["name"], "、".join(g["slugs"]) or "空"),
             "options": [{"label": "不删了", "value": "取消",
                          "description": "什么都不动"},
                         {"label": "确认删除", "value": "确认删除",
                          "description": "把这个分组从分组表里移掉"}],
             "multi_select": False}]


def _switch_pick_group(ans, groups):
    """从回答里认出用户点了哪一组。先认内部编号（最准），再认整名。"""
    a = ans or ""
    for g in (groups or []):
        if g["id"] and ("删:" + g["id"]) in a:
            return g
    for g in (groups or []):
        if g["name"] and g["name"] in a:
            return g
    return None


def switch_set_root(gid, new_root):
    """给已有分组换工作区路径。返回 (ok, 结果说明)。

    2026-10-01 第455步（用户口径「切组还差一个命令 就是更新组路径
      就是在已有的分组换电脑直接更新路径就ok了」）。

    **换电脑不用删组重建** —— 号、窗口、台账、交接全都还在，
    换的只是这个组的工作区在哪。

    做两件事：
      1) 写进分组配置的 root（显式钉死，优先级最高）
      2) 清掉这个组**学到的**旧根（_group_roots.json）——
         不清的话旧机器学到的根票数还很高，会把新路径压回去。

    路径一律过 _norm_root 规范化（反斜杠转正斜杠、去尾斜杠）。
    """
    try:
        key = str(gid or "").strip()
        _raw = str(new_root or "").strip()
        if not key:
            return False, "没指定是哪个分组"
        # 2026-10-01 第461步：**空值 = 清空，回到兜底。**
        # 原来空值一律拒绝，于是「清了配置但 _group_roots.json 里那条
        # manual 记录还留着」，组根继续指向旧路径 —— 两个地方存同一个值
        # 就会这样。现在空值把两处一起清掉，配置是唯一真源。
        if not _raw:
            root = ""
        else:
            root = _norm_root(_raw)
        if _raw and not root:
            return False, ("路径不合法：%r（要形如 F:/a 或 C:/Users/xxx/项目）"
                           % (new_root,))
        try:
            raw = json.loads(GROUPS_FILE.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            return False, "读不到分组表：%s" % exc
        if not isinstance(raw, dict):
            return False, "分组表格式不对"
        gs = raw.get("groups")
        if not isinstance(gs, list):
            return False, "分组表里没有分组"
        hit = False
        for g in gs:
            if not isinstance(g, dict):
                continue
            if (str(g.get("name") or "") == key
                    or str(g.get("id") or "") == key):
                g["root"] = root
                g["name"] = key
                if "id" in g:
                    g["id"] = key
                hit = True
                break
        if not hit:
            return False, "没找到这个分组：%s" % key
        tmp = GROUPS_FILE.with_name(GROUPS_FILE.name + ".tmp")
        tmp.write_text(json.dumps(raw, ensure_ascii=False, indent=1),
                       encoding="utf-8")
        tmp.replace(GROUPS_FILE)
        # 2026-10-01 第461步：**两处一起改，配置是唯一真源。**
        # root 为空时把 _group_roots.json 里这条**删掉**（不是写空值）——
        # 否则 group_root_learned() 会读出一个空 root，group_root_of()
        # 还要再判一次空，多一层没必要的分支。
        try:
            with _grp_root_lock:
                d = _grp_root_all()
                if root:
                    d[key] = {"candidates": {root: GRP_ROOT_MIN},
                             "root": root, "seen": 1,
                             "at": round(time.time(), 3), "manual": True}
                else:
                    d.pop(key, None)
                _t = GRP_ROOT_FILE.with_suffix(".json.tmp")
                _t.write_text(json.dumps(d, ensure_ascii=False, indent=1),
                              encoding="utf-8")
                _t.replace(GRP_ROOT_FILE)
        except BaseException:            # noqa: BLE001
            pass
        return True, (root or "（已清空，回到兜底）")
    except BaseException as exc:            # noqa: BLE001
        return False, "%s: %s" % (type(exc).__name__, str(exc)[:120])


def switch_qroot(groups):
    """第455步：更新哪一组的路径。"""
    opts = []
    for g in groups:
        cur = _norm_root(str(g.get("root") or "")) or "(未设，按自适应算)"
        opts.append({"label": "改组「%s」的路径" % g["name"],
                     "value": "根:" + g["id"],
                     "description": "当前：" + cur})
    opts.append({"label": SWITCH_INFO, "value": SWITCH_INFO,
                 "description": "看看每个组现在有哪些号、目录在哪、接口地址"})
    return [{"id": "swq11", "header": "更新组路径",
             "question": "要改哪一组的工作目录？（换电脑/挪工作区时用，"
                         "号和历史都不动）",
             "options": opts, "multi_select": False}]


def switch_qroot_new(g, guess=""):
    """第455步：新的路径是什么。

    guess 是从本轮请求里抽到的**这台电脑**的工作目录 —— 换电脑时
    那多半就是正确答案，直接给成第一选项，省一次手工输入。
    """
    opts = []
    if guess:
        opts.append({"label": guess, "value": guess,
                     "description": "当前这台电脑的工作目录（推荐）"})
    opts.append({"label": "取消", "value": "取消",
                 "description": "不改了"})
    return [{"id": "swq12", "header": "新路径",
             "question": "分组「%s」的新工作目录是什么？"
                         "对话框能输入就直接写完整路径（形如 F:/a）；"
                         "不能输入就从下面选：" % g["name"],
             "options": opts, "multi_select": False}]


def switch_qroot_ok(g, root):
    """第455步：改之前确认。"""
    old = _norm_root(str(g.get("root") or "")) or "(未设)"
    return [{"id": "swq13", "header": "确认改路径",
             "question": "把分组「%s」的工作目录从 %s 改成 %s？"
                         "号、窗口、台账都不动，只改路径。"
                         % (g["name"], old, root),
             "options": [{"label": "不改了", "value": "取消",
                          "description": "什么都不动"},
                         {"label": SWITCH_ROOT_OK, "value": SWITCH_ROOT_OK,
                          "description": "写进分组配置并清掉旧的学习记录"}],
             "multi_select": False}]


def switch_del(gid):
    """第372步：从 _pool_groups.json 里删掉一个分组，返回 (ok, 错因)。

    原子替换，跟 switch_save 一个写法。号不在这个文件里，所以删分组
    不碰任何账号，它们只是变回未分组。
    """
    try:
        raw = json.loads(GROUPS_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        return False, "读不到分组表：%s" % exc
    if not isinstance(raw, dict):
        return False, "分组表格式不对"
    gs = raw.get("groups")
    if not isinstance(gs, list):
        return False, "分组表里没有分组"
    # 2026-09-27 第377步：盘上不再写 id，主键是组名。传入的 key 同时比
    # name 和残留 id —— 老文件里那条 id=g1/name=test 也能照样删掉。
    keep = [g for g in gs
            if not (isinstance(g, dict)
                    and (str(g.get("name") or "") == gid
                         or str(g.get("id") or "") == gid))]
    if len(keep) == len(gs):
        return False, "没找到这个分组"
    raw["groups"] = keep
    if str(raw.get("default") or "") == gid:
        raw["default"] = ""
    tmp = GROUPS_FILE.with_name(GROUPS_FILE.name + ".tmp")
    tmp.write_text(json.dumps(raw, ensure_ascii=False, indent=1),
                   encoding="utf-8")
    tmp.replace(GROUPS_FILE)
    return True, ""


def _switch_answers(messages):
    """本次切组对话里用户点过的所有回答，按时间顺序。

    反向扫到 ##切组## 那条 user 消息为止 —— 别的对话的历史不能混进来，
    不然第一轮就把上一回的答案当输入。
    """
    out = []
    for m in reversed(messages or []):
        if not isinstance(m, dict):
            continue
        r = str(m.get("role") or "")
        t = _msg_text(m)
        if r == "user":
            # 第373步：遇到另一个隐式命令也要停 —— 不然 ##路由## 的答复会被
            # 当成切组的回答，轮次直接串掉。
            if SWITCH_CMD in t or ROUTE_CMD in t or MODEL_CMD in t:
                break
            # 2026-09-27 第364步：dsh 有时把对话框的答复当一条 user
            # 消息塞回来（不带 tool_call）。只认长得像答复的，别的 user
            # 消息（压缩摘要、系统提醒）不能当答案，不然会乱跳轮次。
            if _looks_like_answer(t):
                out.append(_switch_unpack(t))
            continue
        if r in ("tool", "function"):
            # 2026-10-01 第462步：**跳过「问工作区」的结果。**
            # 那个 pwsh 调用是桥自己发的（前缀 wsq），不是用户在答切组的问题；
            # 不跳过的话它会被算成一次回答，用户同时用 ##切组## 时轮次就串了。
            if _WS_MARK in t:
                continue
            # 2026-10-01 第463步（用户口径：**「我点跳过肯定是结束会话了 怎么还
            #   一直蹦」**）：**取消/跳过不是回答，不能算轮次。**
            #
            # 实测：用户点取消，dsh 回
            #   Error: the user cancelled ask_user_question
            # _switch_unpack 把它摊成空串，**但空串仍然被 append 进列表** ——
            # _switch_count 数的是列表长度，于是取消也算一次回答：
            #   轮次 0 -> 问第1问 -> 取消 -> 轮次1 -> 问第2问 -> 取消 -> 轮次2
            # 用户看到的就是「一直蹦」，而且永远关不掉。
            #
            # 取消的语义是「结束这次对话」，那就**整个停掉**：
            # 这里标一个哨兵，_switch_dialog 看到就收手。
            _low = (t or "").lower()
            if ("cancel" in _low) or ("取消" in (t or "")):
                out.append("\x00CANCEL")
                continue
            out.append(_switch_unpack(t))
    out.reverse()
    return out


def _switch_real(answers):
    """去掉不算「回答」的项（点【查看分组状态】只看一眼，不该带偏轮次）。"""
    return [a for a in (answers or []) if SWITCH_INFO not in (a or "")]


def _switch_wants_info(answers):
    """用户**刚点的这一下**是【查看分组状态】。

    2026-09-27 第367步：以前写成「历史里出现过就算」，结果点完状态再回答
    切组，第二次请求又被状态分支抓住，永远回第 1 问。只认最后一条回答。
    """
    a = [x for x in (answers or []) if (x or "").strip()]
    return bool(a) and SWITCH_INFO in a[-1]


def _win_ipv4_list():
    """物理网卡的 IPv4，按路由 metric 升序。拿不到回空列表。

    2026-10-03 修：原来 _lan_ip() 连 8.8.8.8 探「主用网卡」。装了
    Throne / clash 这类 tun 代理后，8.8.8.8 走的是隧道网卡，于是返回
    **172.19.0.1** —— 隧道内部地址，局域网里别的机器根本连不上，
    ##路由## 的【局域网】那栏因此贴出一条谁也用不了的地址。

    现在读 Windows 路由表：只要有默认网关、网卡名不含 tun/tap/cfw/
    vpn/wintun/loopback、地址不是 169.254.* 的，才算数。
    """
    q = ("$r = Get-NetRoute -DestinationPrefix '0.0.0.0/0' "
         "-ErrorAction SilentlyContinue "
         "| Where-Object { $_.NextHop -ne '0.0.0.0' } "
         "| Sort-Object RouteMetric, ifIndex; "
         "foreach ($x in $r) { "
         "$a = Get-NetAdapter -ifIndex $x.ifIndex -ErrorAction SilentlyContinue; "
         # 2026-10-03（用户口径：「我的网卡是无线网 ip 都不对」）：
         # **必须筛 Status**。断开的网卡（网线没插的以太网）在路由表里
         # 仍留一条 metric=0 的幽灵路由，实测被当成首选 → 给出谁也连不上的
         # 死地址。只有 Up 的网卡才算数。
         "if (-not $a -or $a.Status -ne 'Up') { continue }; "
         "$n = $a.Name; "
         "if ($n -match 'tun|tap|cfw|vpn|wintun|loopback') { continue }; "
         "$ip = (Get-NetIPAddress -ifIndex $x.ifIndex -AddressFamily IPv4 "
         "-ErrorAction SilentlyContinue "
         "| Where-Object { $_.IPAddress -notlike '169.254.*' } "
         "| Select-Object -First 1).IPAddress; "
         "if ($ip) { $ip } }")
    try:
        done = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", q],
            capture_output=True, text=True, timeout=20,
            creationflags=0x08000000)
        raw = done.stdout or ""
    except Exception:            # noqa: BLE001
        return []
    out = []
    for line in raw.splitlines():
        ip = line.strip()
        if re.fullmatch(r"\d{1,3}(?:\.\d{1,3}){3}", ip) and ip not in out:
            out.append(ip)
    return out


def _lan_ip():
    """本机在局域网里的地址，拿不到就回 127.0.0.1。"""
    for ip in _win_ipv4_list():
        return ip
    # 退路：拿不到路由表时，仍用老办法（至少给个值）。
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            s.connect(("8.8.8.8", 80))
            return s.getsockname()[0]
        finally:
            s.close()
    except Exception:            # noqa: BLE001
        return "127.0.0.1"


def switch_status(slugs, cur_gid, root):
    """全部分组状态：有谁、哪些号、目录在哪、接口地址。

    2026-09-27 第366步（用户口径）：对话框里能看状态，切组收场也要贴一份。
    目录名 = 组的 name（用户定的口径），所以这里按 name 拼。
    """
    r = _norm_root(root)
    L = ["**当前分组状态**", ""]
    try:
        rows = _groups_root().get("groups") or []
    except Exception:            # noqa: BLE001
        rows = []
    have = [str(s) for s in (slugs or []) if s]
    seen = set()
    # 2026-09-27 第372步（用户口径）：正文里那串缩进看着糊，改成表格。
    # 不露内部编号 g1/g2；目录名 = 组名。
    if rows:
        L.append("| 分组 | 号 | 目录 | 状态 |")
        L.append("| --- | --- | --- | --- |")
        for g in rows:
            gid = str(g.get("id") or "")
            name = str(g.get("name") or gid)
            mem = [str(s) for s in (g.get("slugs") or []) if str(s) in have]
            seen.update(mem)
            tail = "当前" if gid == cur_gid else ""
            if g.get("enabled") is False:
                tail = (tail + " 已停用").strip()
            # 2026-09-27 第403步（用户口径）：目录列必须显示**完整**路径。
            # 原来只拿 r 当开关、吐相对名，root 抽到了也不拼 —— 用户永远看到
            # 相对 _work/<组名>/，分不出是哪台机器的根。这里把 r 拼上。
            d = (r + "/_work/%s/" % name) if r else ""
            L.append("| %s | %s | %s | %s |"
                     % (name, "、".join(mem) or "空", d, tail))
    else:
        L.append("现在一个分组都没有（没配组 = 没号可轮）。")
    rest = [s for s in have if s not in seen]
    if rest:
        L.append("")
        L.append("未分组（固定号，不参与任何组）：" + "、".join(rest))
        if r:
            L.append("目录 %s/_work/_pinned/<号>/" % r)
    if r:
        L.append("目录基准（本组目录都挂这下面）= " + r)
    ip = _lan_ip()
    L.append("")
    L.append("分组表 %s" % GROUPS_FILE)
    line = "接口 http://127.0.0.1:11999/v1"
    if ip and ip != "127.0.0.1":
        line += "　局域网 http://%s:11999/v1" % ip
    L.append(line)
    L.append("模型名：不带后缀 = 全池挑号；@组id = 走该组；@号 = 钉死某个号")
    return chr(10).join(L)


def route_q1():
    """第373步：##路由## 第 1 问 —— 查哪一档地址。"""
    return [{"id": "swq1", "header": "查地址",
             "question": "要查哪一档的地址？",
             "options": [
                 {"label": ROUTE_IN, "value": ROUTE_IN,
                  "description": "本机回环地址：桥、监控台、dsh 都在 127.0.0.1"},
                 {"label": ROUTE_LAN, "value": ROUTE_LAN,
                  "description": "局域网地址：同一网段别的机器能访问的那套"},
                 {"label": ROUTE_WAN, "value": ROUTE_WAN,
                  "description": "外网地址：公网可访问的那套"}],
             "multi_select": False}]


def _all_lan_ips():
    """本机所有非回环 IPv4，带上网卡名，按地址排好序。"""
    # 2026-10-03：先用路由表口径（排除 tun/tap/cfw/vpn），它才认得全物理网卡。
    # 原来只用 gethostname 解析，实测在这台机器上只回一个地址（.203），
    # 以太网 .98 漏掉了 —— 而用户的口径是「把内网的所有地址贴出来」。
    out = list(_win_ipv4_list())
    try:
        import socket as _s
        for info in _s.getaddrinfo(_s.gethostname(), None, _s.AF_INET):
            ip = info[4][0]
            if ip and ip != "127.0.0.1" and not ip.startswith("169.254.") \
                    and ip not in out:
                out.append(ip)
    except Exception:            # noqa: BLE001
        pass
    # 主用那块网卡（UDP 探出去拿到的那块）排最前，其余按字符串排。
    # 2026-10-03：**dsh 认的排前面**。不认的 IP 打开只有 HTML 空壳，
    # 应用调 /api 全 403，等于没法用。实测 .203 不被认、.98 被认。
    # 排序：被 dsh 信任的在前，其余按原顺序。
    # 注意 main（UDP 探出去那块网卡）只能当**同一档内**的次序，
    # 不能盖过信任判断 —— 否则 .203 又会被顶回第一位（实测踩过）。
    if out:
        _trust = {i: _dsh_trusts(i) for i in out}
        _main = _lan_ip()
        out.sort(key=lambda i: (0 if _trust.get(i) else 1,
                                0 if i == _main else 1, i))
    return out


def _dsh_port_busy(port=None):
    """dsh 界面在不在听。"""
    try:
        return port_busy("127.0.0.1", port or DSH_PORT)
    except Exception:            # noqa: BLE001
        return False


def _dsh_trusts(ip, port=None):
    """这个 IP 当 Host 时，dsh 的 /api 围栏放不放行。

    2026-10-03（用户口径：「能进你没看到没连到 dsh 内部吗 只是一个网页空壳子」）：
    dsh 的 Host 白名单是**启动那一刻**用 networkInterfaces() 拍的快照
    （dsh-web-app/lib/index.js:84 resolveLanTrust）。本机昨天 07:25 起 dsh 时
    WLAN 的 192.168.3.203 还没上线，白名单里只有以太网 192.168.3.98。
    之后 .203 上来了，dsh 从没重读 —— 于是 .203 打开只有 HTML 空壳，
    所有 /api 被 403 挡掉，应用永远起不来。

    实测判据：拿 IP 当 Host 打 /api，403 = 被围栏拦，其它 = 放行
    （404 只是没这个路由，说明围栏过了）。
    """
    import http.client as _hc
    p = port or DSH_PORT
    try:
        c = _hc.HTTPConnection("127.0.0.1", p, timeout=4)
        c.putrequest("GET", "/api", skip_host=True, skip_accept_encoding=True)
        c.putheader("Host", "%s:%d" % (ip, p))
        c.endheaders()
        r = c.getresponse()
        r.read(120)
        c.close()
        return r.status != 403
    except Exception:            # noqa: BLE001
        return False


# 2026-09-27 第374步（用户口径：「没有加token啊」）：监控台和 dsh 界面
# 都自带鉴权，裸地址打不开 —— 监控台 8791 裸访问直接 401，dsh 3080 要带
# ?token= 才能进。token 跟 dsweb/api.py 的 _dsh_token() 同源：dsh 每次启动
# 把随机 token 写进 .state/web.log，托盘再抓进 .state/last-url.txt。
# 这里按同样顺序读，取最后一条匹配；读不到就只贴地址、不编 token。
ROUTE_STATE = PREV_ROOT / ".state"
ROUTE_CRED = PREV_ROOT / ".credentials.yaml"
_ROUTE_CRED_RE = re.compile(
    r"client-connection/browser-session:.*?secret:\s*([A-Za-z0-9_-]+)", re.S)


def _dsh_cookie(host):
    """第424步：按 dsh 的算法现签一个浏览器 cookie；签不出来回空。

    为什么走这条而不是继续追 ?token=：dsh 的 launch token 是进程内存里的
    随机值（processLaunchToken 用 WeakMap），进程一换就作废；但 cookie 的
    签名密钥是**落盘**的（.credentials.yaml 的 client-connection/browser-session
    .payload.secret），initializeSecret 只在记录不存在时才新建。第424步实测：
    拿这份 secret 现签的 cookie 打 3080，本地和局域网都回 200。

    host 传浏览器实际访问 3080 用的 authority（如 192.168.3.203:3080）——
    cookie 名和签名载荷都绑 authority，写错就 401。
    """
    import hmac as _hmac
    try:
        txt = ROUTE_CRED.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return "", ""
    m = _ROUTE_CRED_RE.search(txt)
    if not m:
        return "", ""
    raw = m.group(1)
    try:
        secret = base64.urlsafe_b64decode(raw + "=" * (-len(raw) % 4))
    except Exception:                                       # noqa: BLE001
        return "", ""

    def _b64u(b):
        return base64.urlsafe_b64encode(b).decode().rstrip("=")

    name = "dsh-auth-" + _b64u(hashlib.sha256(host.encode()).digest())
    now = int(time.time() * 1000)
    body = _b64u(json.dumps(
        {"version": 1, "authority": host,
         "issuedAt": now, "expiresAt": now + 7 * 24 * 3600 * 1000},
        separators=(",", ":")).encode())
    sig = _b64u(_hmac.new(secret, body.encode(), hashlib.sha256).digest())
    return name, "v1.%s.%s" % (body, sig)


_ROUTE_TOKEN_RE = re.compile(r"token=([A-Za-z0-9_-]{16,})")
_ROUTE_TOK_OK = ""      # 第423步：上一次实测真能用的 token
_ROUTE_TOK_AT = 0.0     # 以及实测时间（30 秒内不重复打）


def _route_token_alive(tok):
    """带 tok 打一发 /?token=，dsh 回 303 才算数（401 就是过期）。"""
    try:
        r = requests.get("http://127.0.0.1:%d/?token=%s" % (DSH_PORT, tok),
                         allow_redirects=False, timeout=3)
    except Exception:                                   # noqa: BLE001
        return False
    return r.status_code == 303


def _route_token():
    """拿 dsh 当前**真能用**的启动 token；拿不到回空串。

    第423步：dsh 的 launch token 是每个进程随机生成、只留在内存里的
    （dsh-client-connection/lib/index.js:240 的 processLaunchToken 用
    WeakMap + randomBytes）。磁盘上那些文件是托盘从**当时那个**进程的
    输出里刮下来的 —— 进程一换，旧值全变成死链，而新进程的值没人抓过。

    所以这里不再「读出最后一个就算数」，改成**实测**：候选挨个对活着的
    3080 打一枪，只有真能换到 cookie（303）的才认。全都不过就回空串 ——
    宁可如实说读不到，也不再贴一条点开就是 401 的死链。
    """
    global _ROUTE_TOK_OK, _ROUTE_TOK_AT
    now = time.time()
    if _ROUTE_TOK_OK and now - _ROUTE_TOK_AT < 30:
        return _ROUTE_TOK_OK
    if not _dsh_port_busy():
        return ""
    cands = []
    for n in ("token.txt", "web.log", "last-url.txt"):
        try:
            t = (ROUTE_STATE / n).read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        hits = _ROUTE_TOKEN_RE.findall(t)
        if n == "token.txt" and hits:
            hits = hits[-1:]            # 托盘现抓的那个，只有一个值
        for h in reversed(hits):        # 越靠后越新，先试新的
            if h not in cands:
                cands.append(h)
    for c in cands:
        if _route_token_alive(c):
            _ROUTE_TOK_OK, _ROUTE_TOK_AT = c, now
            return c
    return ""


def route_report(scope):
    """第373步：按选中的档位贴地址。三档都带上监控台和 dsh。

    第374步：监控台和 dsh 的地址一律带上 ?token= —— 不带的话点开是 401，
    等于没贴。

    外网地址这台机器上没配（没有公网入口），如实说没有，不编。
    """
    L = ["**地址一览**", ""]
    scope = scope or ""
    lan = _all_lan_ips()
    # 第374步：监控台和 dsh 都要 token，拼在问号后面。
    _tok = _route_token()
    _tq = ("?token=" + _tok) if _tok else ""
    want_in = ROUTE_IN in scope
    want_lan = ROUTE_LAN in scope
    want_wan = ROUTE_WAN in scope
    if not (want_in or want_lan or want_wan):
        want_in = want_lan = True

    if want_in:
        L.append("**内网（本机回环，只有这台机器能用）**")
        L.append("| 服务 | 地址 |")
        L.append("| --- | --- |")
        L.append("| 账号池桥 | http://127.0.0.1:11999/v1 |")
        L.append("| 监控台 | http://127.0.0.1:%d/%s |"
                 % (DASHBOARD_PORT, _tq))
        L.append("| dsh 界面 | http://127.0.0.1:%d/%s |" % (DSH_PORT, _tq))
        # 2026-10-03：**免 token 直进**。桥能拿 .credentials.yaml 里的签名密钥
        # 现签一个浏览器 cookie，所以这条不依赖 dsh 的启动 token —— launch token
        # 是进程内存里的随机值，谁重启谁换，而 cookie 密钥是落盘的。
        # 上面那条带 ?token= 的万一过期，点这条一定能进。
        L.append("| dsh 界面（免 token 直进） | http://127.0.0.1:11999/dsh |")
        L.append("")

    if want_lan:
        L.append("**局域网（同网段别的机器能用）**")
        if not lan:
            L.append("没探到局域网地址。")
        else:
            L.append("| 服务 | 地址 |")
            L.append("| --- | --- |")
            for ip in lan:
                L.append("| 账号池桥 | http://%s:11999/v1 |" % ip)
                L.append("| 监控台 | http://%s:%d/%s |"
                         % (ip, DASHBOARD_PORT, _tq))
                L.append("| dsh 界面 | http://%s:%d/%s |"
                         % (ip, DSH_PORT, _tq))
                L.append("| dsh 界面（免 token 直进） | http://%s:11999/dsh |"
                         % ip)
            L.append("")
            L.append("一共 %d 个局域网地址：%s" % (len(lan), "、".join(lan)))
        L.append("")

    if want_wan:
        L.append("**外网（公网能访问）**")
        L.append("这台机器上没有配公网入口 —— 桥、监控台、dsh 都只听在本机/局域网，")
        L.append("外网要能进，得另做端口映射或反代（比如把 11999 / %d / %d "
                 % (DASHBOARD_PORT, DSH_PORT))
        L.append("映射到公网 IP 上）。配好之前，外网地址为空。")
        L.append("")

    L.append("说明：账号池桥 11999 是模型接口，API Key 随便填非空串（如 sk-local），")
    L.append("不需要 token；监控台 %d 是设置台+监控台；dsh 界面 %d %s。"
             % (DASHBOARD_PORT, DSH_PORT,
                "正在跑" if _dsh_port_busy() else "没在跑"))
    if _tok:
        L.append("监控台和 dsh 地址后面那串 ?token= 就是它们的钥匙，换机器/换浏览器")
        L.append("直接整条复制过去，别只复制到端口号。")
    else:
        L.append("没读到 dsh 的启动 token（.state/web.log 和 last-url.txt 都没有），")
        L.append("这两条地址现在不带 token，点开会是 401；等 dsh 起来后再查一次。")
    return chr(10).join(L)


def model_report(pool):
    """第419步：##模型## 的正文 —— 模型名清单 + DSH 配置片段。

    用户口径：「##模型## 获取配置模型，手动去配置也行，桥给配置也行」。
    所以给两份：能用哪些名字；以及直接抄进 DSH 的 provider 配置。
    只读，不改任何配置、不写文件。
    """
    L = ["**模型与配置一览**", ""]
    ip = _lan_ip()
    lan = ("http://%s:11999/v1" % ip) if ip and ip != "127.0.0.1" else ""
    L.append("接口地址（本机）：http://127.0.0.1:11999/v1")
    if lan:
        L.append("接口地址（局域网，别台机器用这个）：%s" % lan)
    L.append("API Key：随便填一个非空串，比如 sk-local（桥不做鉴权）。")
    L.append("协议：openai-completions")
    L.append("")

    # ---- 不带后缀：整池挑号 ----
    L.append("**一、整池轮询（不带后缀，桥自动挑号）**")
    L.append("")
    L.append("| 模型名 | 含义 |")
    L.append("| --- | --- |")
    for m, (label, _think) in pool.MODEL_LABEL.items():
        L.append("| %s | %s |" % (m, label))
    L.append("")

    # ---- @组 ----
    try:
        grps = [g for g in pool._groups() if not g.get("implicit")]
    except Exception:                        # noqa: BLE001
        grps = []
    L.append("**二、指定分组（@组名，只在这个组里轮）**")
    L.append("")
    if grps:
        L.append("| 分组 | 号 | 模型名写法 |")
        L.append("| --- | --- | --- |")
        for g in grps:
            gname = str(g.get("name") or g.get("id") or "")
            mem = "、".join(str(s) for s in (g.get("slugs") or [])) or "空号"
            L.append("| %s | %s | 四个模型名都加 @%s |" % (gname, mem, gname))
        L.append("")
        L.append("例：deepseek-chat@%s"
                 % str(grps[0].get("name") or grps[0].get("id") or ""))
    else:
        L.append("现在一个分组都没有。发 ##切组## 建组，建完再来查。")
    L.append("")

    # ---- @号 ----
    L.append("**三、钉死某个号（@号，只走它）**")
    L.append("")
    try:
        slugs = [s for s in pool.bridges.keys() if s]
    except Exception:                        # noqa: BLE001
        slugs = []
    if slugs:
        L.append("可用号：%s" % "、".join(slugs))
        L.append("")
        L.append("例：deepseek-reasoner@%s" % slugs[0])
    else:
        L.append("池里现在没有号。")
    L.append("")

    # ---- DSH 配置片段 ----
    L.append("**四、DSH 配置（手填用这段）**")
    L.append("")
    L.append("桥已经自动写好了 $DSH_HOME/cordis.patch.yml，正常不用手改。")
    L.append("要手填或核对，照下面这段：")
    L.append("")
    base = lan or "http://127.0.0.1:11999/v1"
    L.append("- id: llm-pi-ai")
    L.append("  config:")
    L.append("    providers:")
    L.append("      ds-bridge:")
    L.append("        displayName: 账号池（自动挑号）")
    L.append("        api: openai-completions")
    L.append("        baseURL: %s" % base)
    L.append("        apiKeyEnv: LOCAL_LLM_TOKEN")
    L.append("        compat:")
    L.append("          thinkingFormat: deepseek")
    L.append("        models:")
    for m in pool.MODEL_LABEL:
        L.append("          - id: %s" % m)
        L.append("            name: %s" % m)
        L.append("            contextWindow: %d" % pool.context_window(m))
        L.append("            input:")
        L.append("              - text")
        L.append("              - image")
    L.append("")
    L.append("别台机器接入时：baseURL 换成上面那条局域网地址，`@组名`")
    L.append("要在**那台机器**的 cordis.patch.yml 里也写上对应 provider。")
    return chr(10).join(L)


def _switch_clean(s):
    """组名消毒：要能当目录名用，只留字母数字下划线横杠和汉字。"""
    s = re.sub(r'[^0-9A-Za-z_\-\u4e00-\u9fff]', '', s or "")
    return s[:32]


def _switch_name(ans):
    """从 dsh 回来的回答里抠出用户想要的组名。"""
    t = (ans or "").strip()
    if not t:
        return ""
    for pre in ("组_", "项目"):
        if t.startswith(pre):
            return _switch_clean(t[len(pre):])
    m = re.search(r'"(?:label|answer|value)"\s*:\s*"([^"]*)"', t)
    if m:
        return _switch_name(m.group(1))
    return _switch_clean(t)


def _switch_pick(ans, slugs):
    """从回答里认出用户勾了哪些号。"""
    out = []
    for s in (slugs or []):
        if not s:
            continue
        if s in (ans or "") and s not in out:
            out.append(s)
    return out


def switch_save(name, slugs):
    """新建一个分组写进 _pool_groups.json，返回它的 id。

    原子替换：先写同目录临时文件再 replace。桥对这个文件做
    (mtime_ns, size) 热检，半截文件会被当成坏值整份丢掉。
    """
    try:
        raw = json.loads(GROUPS_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raw = None
    if not isinstance(raw, dict):
        # 2026-09-27 第368步：不再凭空塞 default=g1。新建分组只往 groups 里
        # 追加一条，不给整份配置留一个指向不存在组的默认值。
        raw = {"meta_mode": "pin", "default": "", "groups": []}
    gs = raw.get("groups")
    if not isinstance(gs, list):
        gs = []
    # 2026-09-27 第377步（用户口径）：「生成组的时候就把组名当id用」
    # 「我说的是 g1 g2 在控制台自动生成的那个」。**不再自动编号** ——
    # 你起的名字就是这一组的 id（同时也是 @后缀、default、grp_top 和
    # 状态格子的 key）。既然名字成了主键，它就必须唯一：重名直接拒绝，
    # 既不悄悄改名，也不让后来的那组被 _groups_root 静默丢掉。
    key = str(name or "").strip()
    if not key:
        raise ValueError("分组名不能为空")
    for g in gs:
        if isinstance(g, dict) and (str(g.get("name") or "") == key
                                    or str(g.get("id") or "") == key):
            raise ValueError("已经有一个叫「%s」的分组了" % key)
    # 落盘不写 id 字段了 —— 它就是名字本身，留着只会和 name 打架。
    gs.append({"name": key, "slugs": list(slugs),
               "turn_limit": 0.0, "enabled": True, "notes": {}})
    raw["groups"] = gs
    tmp = GROUPS_FILE.with_name(GROUPS_FILE.name + ".tmp")
    tmp.write_text(json.dumps(raw, ensure_ascii=False, indent=1),
                   encoding="utf-8")
    tmp.replace(GROUPS_FILE)
    return key


def reminder_block(*notes):
    """每轮重申的要求，全部合成一段〔提醒〕贴在末尾 —— 越靠近末尾模型越照做。

    多段要求也只开一个〔提醒〕，别各自单开一段，那样模型看着像两个互不相干的
    插话，反而容易只挑一条执行。
    """
    body = "\n".join(n.strip() for n in notes if n and n.strip())
    return f"〔提醒〕{body}" if body else ""


# 台账「活跃」栏的路径与条数上限。只读不写：桥接不去改别人的文件。
# 2026-10-01 第458步：**改名。** 这个名字原来被两个文件共用：
#   本地账本（_ledger.json）和全局台账（_pending.md）都叫 LEDGER_FILE。
# 后者把前者覆盖了 —— 于是 ledger_put() 一直往 _pending.md 里写 JSON，
# **把真台账冲掉了**（实测 _pending.md 只剩 962 B 的账本 JSON，
# 原本是 9 万字的台账）。查硬编码路径时才发现这个撞名。
# 台账本身文件先不动，只把常量名分开。
LEDGER_PENDING = WORK_ROOT / "_pending.md"
LEDGER_MAX = 10
# 已收尾贴多少条（只留 owner|名称，控体积）。126 条全贴太长，但
# 「做过什么」必须看得见 —— 看不见的代价是整段任务白跑一遍。
LEDGER_DONE_MAX = 15

# 2026-09-25 加。步骤台账：**每轮注入**，这是唯一能让窗口知道「现在在哪一步」
# 的载体。原来这一步只存在于检查点附件里（换号/压缩那一刻才生成，模型还得
# 自己去读），于是新窗口根本不知道前一个窗口停在哪 —— 用户原话
# 「我怎么让他知道在那一步做呢」。
# 要改当前步骤，就往这个文件末尾追加一行，下一轮所有窗口都看得到。
LEDGER_STEPS = WORK_ROOT / "_steps.md"


def handoff_to_steps(frm, to, text, note=None, slug=""):
    """报告改动2（第98步）：把这一棒交出的交接单落一行到 _steps.md。

    为什么：交接单以前只拼进 prompt 就没了 —— 用户看不到（不在界面上）、
    事后查不到（_relay_replies.jsonl 的 ask_head 只留前 300 字）、别的 AI
    拿不到。而 _steps.md 是**每轮注入**的（ledger_block 的「当前步骤」段），
    写进去接手方读得到、用户也看得到。

    只追加、只写一行、失败静默 —— 落盘是加分项，绝不能把换号卡住。
    """
    try:
        _t = time.strftime("%Y-%m-%d %H:%M")
        _head = " ".join((text or "").split())[:180]
        if not _head:
            _head = "（交接单为空）"
        _why = (" | " + note) if note else ""
        line = (f"{frm}@交接 | {_t} | 交接给下一棒 -> {to}{_why} | "
                f"接手方从这里开始：{_head} | 回滚点见上一行")
        _sf = grp_path("steps", slug or frm, create=True) or LEDGER_STEPS
        with _sf.open("a", encoding="utf-8") as fh:
            fh.write("\n" + line)
    except Exception:            # noqa: BLE001
        pass


STEPS_MAX = 12
LEDGER_WIDTH = 220


# 老行行首第 2 段是个**不稳定**的窗口标签：`- [ ] w-3f9a21c4 | owner | ...`。
# 2026-09-25 起规则 16 不再要求写它（写项目名），但文件只增不删，存量行上
# 还挂着几千个。渲染时统一抹掉：只删这 8 位 id 和紧跟的竖线，`- [x] ` 复选
# 框原样保留 —— 否则模型看到的 10 条范例全是窗口号，规则形同虚设。
# 改的只是贴给模型的文本，不碰文件本身。
LEDGER_TAG_RE = re.compile(r"^- \[([ xX])\]\s+(?:w-[0-9a-f]{8}\s*\|\s*)?")


def window_tag(keys, items, sid=""):
    """算这个窗口的稳定短 id。**2026-09-25 起已无人调用**，留作参考。

    优先用 dsh 的 sessionId（稳定、压缩不变、换号不变、每窗口唯一）；
    没有 sid 才退回「第一条 user 消息指纹」——那是老行为，可能串窗口。

    为什么弃用：dsh 从不把 sessionId 发给桥（实测 1266 次请求里 body/header
    都没有），所以永远走指纹回退；而 dsh 每压缩一次上下文就用
    surfaceOp=replace 重写消息列表开头，指纹随之改变 —— 单会话实测变了 143
    次。拿它当台账归属键，等于每轮换个身份。台账现在按 owner（项目名）归属。
    """
    if sid:
        return "w-" + hashlib.sha1(("sid:" + sid).encode()).hexdigest()[:8]
    for k, m in zip(keys, items):
        if (m.get("role") or "") == "user":
            return "w-" + hashlib.sha1(k.encode()).hexdigest()[:8]
    return ""


def ledger_block(win="", slug=""):
    """把台账贴给模型 —— 活跃**全部**贴，已收尾贴尾部（只留 owner|名称）。

    2026-09-25 第一次改（用户口径）。原来按 `w-xxxxxxxx` 只贴本窗口那几条，
    但那个窗口 id 是「第一条 user 消息的指纹」，而 dsh 每压缩一次上下文就用
    surfaceOp=replace 把消息列表开头整段换掉 —— 实测单个会话换了 143 次，
    最快间隔 98 秒。于是每个窗口每轮拿到的 id 都不同，过滤结果恒为 0 条。
    id 不再印在表头，也不再当过滤键。win 保留只为调用点不用改。

    2026-09-25 第二次改。起因：一个窗口**白跑了一遍已经做完的活**。两处：

    1）**写明这是文件、真源在哪、别另存副本。** 实测 `C:/Users/Lenovo/Desktop/teste/_3txt/台账.txt`
    第 1 行就是本函数的旧表头 `〔台账·活跃·w-7cc5a0ee〕` —— 有窗口把注入的
    提示块**当成台账文件**，复制一份到 `_3txt/`，然后在副本里登记（那份的
    `w-bf06fa26` 谁也读不到）；真源里同一件事早在 09-24 22:xx 就以
    `w-d38e0a3c` 登记并收尾了。
    为什么必须写在这里：`ATTACH_NOTE` 里的路径只在**走附件那一轮**才贴；
    `POOL_NOTE_DEFAULT` 规则 16 只发给**不参与轮询的固定号**（见 _stamp），
    轮询号一条都收不到 —— **本函数是唯一对所有账号每轮都可见的载体。**

    2）**已收尾也贴（尾部若干条，只留 owner|名称）。** 原来只贴 `## 活跃`，
    于是「这件事已经做完了」从来没出现在模型能看到的任何地方 —— 这就是
    白跑那趟的直接原因。只留前两段是为控体积，但「做过什么」必须看得见。

    截取取**尾部**：协议是「在末尾追加一行」，新条目都在末尾；原来从文件头
    取前 N 行，等于把最新的全截掉（实测 w-ba243dd5 那条 09-24 11:44 落在
    活跃第 25 位，从未进过 prompt）。

    读不到、没这一段、或一条都没有，就整块不发 —— 台账出问题绝不能把这一轮
    请求带塌。
    """
    try:
        # 2026-10-01 第468步：**这里原来传 create=True** —— 那是给「写」用的。
        # 用在「读」上，组目录一建好就会把已有的台账整份屏蔽掉：
        #   grp_path('steps','309',create=True) -> _work/剪辑/_steps.md（不存在）
        #   -> 读不到 -> ledger_block 返回空 -> 模型这一轮看不到任何台账。
        # 实测就是这个原因让 ledger_block('', '309') 返回 0 字。
        # 读路径只认「已经存在的」，不存在才回落桥根那份。
        _pd = grp_path("pending", slug) or LEDGER_PENDING
        lines = _pd.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return ""
    act, done, sec = [], [], ""
    for ln in lines:
        s = ln.strip()
        if s.startswith("## "):
            sec = "a" if s.startswith("## 活跃") else ("d" if "已收尾" in s else "")
            continue
        if not sec or not s.startswith("- ["):
            continue
        s = LEDGER_TAG_RE.sub(lambda m: "- [%s] " % m.group(1), s)
        if sec == "a":
            act.append(s[:LEDGER_WIDTH])
        else:
            body = s[6:] if len(s) > 6 else s
            segs = [x.strip() for x in body.split("|")]
            done.append((s[:6] + " | ".join(segs[:2]))[:LEDGER_WIDTH])
    # 步骤台账：跳过表头（## 之前和正文引用块），取尾部若干行原样贴。
    steps = []
    # 2026-10-01 第476步（用户口径「他找不到他原来的任务的历史了」）：
    # **任务历史必须读「组内那份 + 桥根那份」，不能只读一份。**
    #
    # 实测病根（我这轮自己造的）：第470步按组拆 _steps.md 时，
    # 真任务行（账号池@第5步）与交接样板行（309@交接）**分反了** ——
    # 真任务行的行首是**项目名**（账号池/托盘/轮询池/元宝），不是账号号，
    # 我按「行首是账号号就归该组」去分，于是样板全进各组、真任务全留桥根。
    # 数字：桥根 269 行里 232 条是真任务；三组各 732/423/346 行**全是样板**。
    # 后果：ledger_block 的样板过滤器把组内那份清成 0 行 ——
    # **模型一条任务历史都看不到**，表现就是「找不到原来的任务了」。
    #
    # 为什么不把真任务再按组分一次：任务名是项目名，**没有可靠的组归属**。
    # 账号池/托盘/轮询池/元宝 是 test 组在干的，但那是从内容推断的，
    # 再猜一次就是重犯第470步的错。**任务台账本来就跨组共享**
    # （记的是「这台机器上每个项目干到哪了」），各组都该看得到。
    # 所以：两份都读、合并、逐行去重，再走同一个样板过滤器。
    _sl = []
    _seen_lines = set()
    # 2026-10-02 第480步（用户口径「你是不是没把内容直接搬到test目录下并校验啊」）：
    # **顺序要对调：组内那份排在后。**
    # ledger_block 只取尾部 STEPS_MAX(12) 行 —— 谁是后读的，尾窗就落在谁身上。
    # 原来是 (桥根, 组内)：桥根那份的真任务排在后，于是**尾窗全是别组的活 / 桥自身的活**，
    # 本组的任务被挤到看不见（实测 test 组看到的全是「分组隔离落地/PTC协议修正」这类）。
    # 组内那份的文件里，本组任务本来就整理在**末尾**（见 _work/<组>/_sys/_steps.md 的分节），
    # 所以把组内排在后，尾窗就正好落在「本组任务」那一段上。
    for _cand in (LEDGER_STEPS, grp_path("steps", slug))[::-1]:
        if not _cand:
            continue
        try:
            for _ln in pathlib.Path(_cand).read_text(
                    encoding="utf-8", errors="replace").splitlines():
                if _ln not in _seen_lines:
                    _seen_lines.add(_ln)
                    _sl.append(_ln)
        except OSError:
            continue
    _body = False
    for ln in _sl:
        s = ln.strip()
        if s.startswith("## "):
            _body = True
            continue
        if not _body or not s or s.startswith(">"):
            continue
        # 2026-10-01 第468步（多维度实测抓到的真凶）：**滤掉「本地代写交接」样板行。**
        #
        # 实测：_steps.md 1419 条正文行里 **1161 条（81.8%）**是这种行 ——
        #   310@交接 | 2026-10-01 17:17 | 交接给下一棒 -> 下一棒 | 本地代写 |
        #   接手方从这里开始：【本地代写交接】上一棒（310）没能自己交出交接…
        # 它每一行几百字，内容是**桥自己**上一轮写的交接样板，**不是任务进展**。
        #
        # 而这里原来是**原样贴、只截宽度、只取尾部 12 行**，于是：
        #   尾部 12 行几乎必然全是交接样板 -> 每轮往上游灌 ~2600 字纯复述。
        # 实测后果（19 时那一小时，空回复率 40.8%）：
        #   19:13:01 [309] ← 无正文！prompt=5488 字 片段=[]
        #   19:13:08 [309] ← 无正文！prompt=5488 字 片段=[]   ← 同一份，逐字重试
        #   19:13:16 / 19:13:22 …同一份反复，换号(309->483)也照样空
        #   **`片段=[]` = 上游连 THINKING 都不给** —— 它对这种高度重复的
        #   内容直接静默，而桥把静默判成「限流」-> 换号 -> 再灌同一份 -> 死循环。
        #   这就是用户说的「全组冷却」的真身。
        #
        # 判据（跟 task_rows 的 TASK_NOISE 同一把尺，别处已经验证过）：
        #   任务名是账号号（纯数字）或含「交接」字样，且整行是本地代写样板。
        # 滤掉后 STEPS_MAX 那 12 行才真的落在「任务进展」上。
        _mname = re.match(r'^([^|@]{1,40}?)@', s)
        _nm = (_mname.group(1).strip() if _mname else "")
        if ("本地代写" in s) or ("交接给下一棒" in s) or (
                _nm and ("交接" in _nm or _nm.isdigit())):
            continue
        steps.append(s[:LEDGER_WIDTH])
    steps = steps[-STEPS_MAX:]

    act = act[-LEDGER_MAX:]
    done = done[-LEDGER_DONE_MAX:]
    if not act and not done:
        return ""
    # 2026-09-27 第413步（用户口径：「所有关于路径的都放到格式提醒」）：这里
    # 原来直接印 str(LEDGER_PENDING)/str(LEDGER_STEPS) —— 那是桥本机的绝对路径，
    # 等于在〔格式提醒〕之外又散落两处路径。改成相对名，绝对路径只在〔格式提醒〕
    # 的本机目录段出现一次。
    out = ["〔台账文件 `_pending.md`〕要登记就往这个文件里追加；"
           "别另存副本 —— 副本没人读得到，会重复干活。"
           "（绝对路径见〔格式提醒〕的本机目录段）"]
    if steps:
        out.append("〔当前步骤 `_steps.md`〕要改就在这个文件末尾追加一行")
        out += steps
    if act:
        out.append("〔活跃·最近 " + str(len(act)) + " 条〕")
        out += act
    if done:
        out.append("〔已收尾·最近 " + str(len(done)) + " 条〕**这些已经做完了，别重做**")
        out += done
    return "\n".join(out)





def parse_tool_calls(text, tools=None):
    """从回复里抠出模型想调的工具。抠不出来就返回 None，当普通文本处理。

    两种形状都认：
      1. 我们在提示词里约定的 ```json {"tool_calls":[...]} ```
      2. DeepSeek 自己训出来的原生标记（`<
         以及 `<｜tool▁sep｜>名字` + json 代码块）。模型经常无视我们的约定、
         直接用它自己的格式，不认这个就等于工具调用全丢。
    """
    return split_calls(text, tools)[0]


# 模型常无视 TOOL_PROTOCOL，直接按 dsh 系统提示的约定输出「【工具调用】名字 + json 围栏块」。
DSH_MARK = re.compile(r'【工具调用】\s*([A-Za-z_][\w.-]*)')


def _from_dsh_marker(text, tools=None):
    """认「【工具调用】run_code + ```json {参数}```」这种形状。

    dsh 系统提示里的工具调用约定就是「【工具调用】名字」，模型常照它输出、
    无视桥的 TOOL_PROTOCOL（"tool_calls" 那套）。这里按「【工具调用】名字 + 紧跟的
    json 围栏块」解析，那个 json 块是参数本身（不带 name/arguments 外壳）。
    """
    text = text or ""
    m = DSH_MARK.search(text)
    if not m:
        return None, []
    name = m.group(1)
    if tools and not any((t.get("function") or {}).get("name") == name
                         for t in tools):
        return None, []          # 名字不在工具表里，不认，免得误吞正文
    fm = re.search(r'```(?:json)?\s*\n?(.*?)```', text[m.end():], re.S)
    if not fm:
        return None, []
    raw = fm.group(1).strip()
    try:
        args = json.loads(raw)
    except json.JSONDecodeError:
        return None, []
    if not isinstance(args, dict):
        return None, []
    call = _mk(0, name, args)
    return [call], [(m.start(), m.end() + fm.end())]


def split_calls(text, tools=None):
    """返回 (calls, 去掉调用标记后剩下的正文)。

    模型常写「一段文字介绍 + 一个工具调用」。以前只把 calls 拿走、正文整段丢掉，
    客户端就只看得到思考和命令，看不到那段介绍。这里顺手把调用占的区间抠掉，
    剩下的还给调用方当正文发出去。
    """
    calls, spans = _from_json(text)
    if not calls:
        calls, spans = _from_native(text, tools)
    if not calls:
        calls, spans = _from_dsh_marker(text, tools)
    # 2026-10-02 加：PTC 下模型常只写内层那一行（漏了 {"tool_calls":[...]} 外壳），
    # 前三条都落空 -> 当正文 -> 白烧一轮。判据卡死，见 _from_bare_await。
    if not calls:
        calls, spans = _from_bare_await(text, tools)
    if not calls:
        return None, text or ""
    # 2026-10-01 第459步：**交回客户端之前，把相对路径锚定到本轮本机根。**
    # 见 anchor_paths 的注释：模型经常不照〔本机目录〕段填，
    # 与其反复提醒，不如在出口处纠一次 —— 它填 x.py 也能落在对的地方。
    try:
        anchor_paths(calls)
    except BaseException:            # noqa: BLE001
        pass
    return calls, _drop_spans(text or "", spans)


# 抠掉调用区间后，紧贴在它前后的外壳（```json 围栏、DSML 标签）也不是正文。
# 只吃紧邻的那几个，不做全文清洗 —— 否则正文里合法的代码围栏会被误删。
SHELL_FENCE = r'`{3,}[ \t]*\w*'
SHELL_TAG = r'<[^\n<>]{0,100}?(?:DSML|｜|▁)[^\n<>]{0,40}?>?'


def _eat_head(s):
    while True:
        t = s.rstrip()
        m = re.search(SHELL_FENCE + r'\Z', t) or re.search(SHELL_TAG + r'\Z', t)
        if not m:
            return s
        s = t[:m.start()]


def _eat_tail(s):
    while True:
        t = s.lstrip()
        m = re.match(SHELL_FENCE, t) or re.match(SHELL_TAG, t)
        if not m:
            return s
        s = t[m.end():]


def _drop_spans(text, spans):
    for a, b in sorted(spans, reverse=True):
        text = _eat_head(text[:a]) + "\n\n" + _eat_tail(text[b:])
    return re.sub(r'\n{3,}', "\n\n", text).strip()


def _mk(i, name, args):
    if not isinstance(args, str):
        args = json.dumps(args if args is not None else {}, ensure_ascii=False)
    return {"index": i, "id": f"call_{i}_{_short_id()}", "type": "function",
            "function": {"name": name, "arguments": args}}


def _from_json(text):
    """扫出文本里所有能解出来的 JSON 对象，挑带 tool_calls 的那个。

    不用「```json ... ```」这种成对围栏去截：工具参数里经常有 HTML/JS，
    里面自带反引号甚至 ``` ，围栏会在半路被提前闭合，JSON 就被切断了。
    改成从每个 '{' 处试 raw_decode —— 它按 JSON 语法自己认字符串和转义，
    嵌套括号、反引号、代码块都不会干扰。
    """
    text = text or ""
    dec = json.JSONDecoder()
    i = 0
    while True:
        start = text.find("{", i)
        if start < 0:
            return None, []
        try:
            obj, end = dec.raw_decode(text, start)
        except ValueError:
            i = start + 1
            continue
        i = end
        if not isinstance(obj, dict):
            continue
        items = obj.get("tool_calls")
        if isinstance(obj.get("tool_call"), dict):
            items = [obj["tool_call"]]
        if not isinstance(items, list) or not items:
            # 2026-09-22 加：模型有时不写外面那层 tool_calls，直接给单个调用，
            # 而且外面裹的是 ```js / ```python / 无语言围栏（实测 18:51:23，483 号
            # 写的就是 ```js）。这里顺手认这种单调用形状。
            # 判据卡得很死：必须有 name，且必须有 arguments/input/parameters 之一，
            # 免得把普通代码块里的 {"name": ...} 误当成工具调用去执行。
            _nm = obj.get("name")
            _ar = obj.get("arguments")
            if _ar is None:
                _ar = obj.get("input")
            if _ar is None:
                _ar = obj.get("parameters")
            if isinstance(_nm, str) and _nm and _ar is not None \
                    and set(obj) <= {"name", "arguments", "input", "parameters",
                                     "id", "type", "index"}:
                items = [{"name": _nm, "arguments": _ar}]
        if not isinstance(items, list) or not items:
            continue
        out = []
        for n, it in enumerate(items):
            if not isinstance(it, dict):
                continue
            name = it.get("name") or (it.get("function") or {}).get("name")
            if not name:
                continue
            args = it.get("arguments")
            if args is None:
                args = (it.get("function") or {}).get("arguments")
            out.append(_mk(n, name, args))
        if out:
            return out, [(start, end)]



# DeepSeek 原生标记。分隔符里那些竖线字符（｜、▁）和收尾标记的写法版本之间会变，
# 所以只锚定 invoke/parameter 这两个关键字和 name="..."，值取到下一个 parameter
# 之前，再把尾巴上那截标签壳子削掉。
NATIVE_INVOKE = re.compile(
    r'invoke\s+name="([^"]+)"(.*?)(?=invoke\s+name="|\Z)', re.S)
NATIVE_PARAM = re.compile(
    r'parameter\s+name\s*=\s*"([^"]+)"(?:[^>\n]{0,200}>)?', re.S)

NATIVE_SEP = re.compile(
    r'sep[^>]*>\s*([A-Za-z_][\w.-]*)\s*```(?:json)?\s*(\{.*?\})\s*```', re.S)
# 值末尾常见的收尾壳：</...parameter>、<
# 标签截断剩下的半截 `<
NATIVE_TAIL = re.compile(
    r'(?:<[^<]{0,60}?(?:DSML|parameter|invoke|calls|｜|▁)[^<]{0,30}?>'
    r'|<[^<>]{0,40}?(?:DSML|｜|▁)[^<>]{0,20}?)\s*\Z', re.S)



# 2026-09-25 加。模型会把 TOOL_PROTOCOL 里那个 JSON 外壳照搬进原生标记 ——
# 参数名直接写成 arguments，值是一整个 JSON 对象：
#   <|DSML| parameter name="arguments">{"code": "..."}
# 于是客户端收到 {"arguments": {"code": ...}}，找不到 code，报：
#   Error: invalid arguments: missing required property "code"
# 实测很频繁（04:14、04:15、04:20 多轮），模型在「arguments 嵌套」和
# 「参数名直写」之间来回，每次错就白烧一整轮。
#
# 只在这三个键**独占**时才拆，绝不碰多键的正常调用 —— 参数名本来就是工具
# 自己定义的词，arguments/input/parameters 是 OpenAI/Anthropic 的外壳名，
# 没有任何工具的**参数**叫这三个。
WRAP_KEYS = ("arguments", "input", "parameters")


def _unwrap_args(args):
    """参数表被整个包进 arguments 一层时拆开；拆不动就原样返回。

    两种形态都要认（2026-10-02 补第二种）：

      ① 只有包裹键一层：
             {"arguments": {"code": "...", "description": "..."}}
         实测很频繁（04:14、04:15、04:20 多轮），原来只认这一种。

      ② **包裹键 + 别的顶层键**（实测 20:0x 抓到）：
             {"arguments": "{\"code\": \"...\", \"description\": \"...\"}",
              "description": "执行"}
         模型把 code/description 包进了 arguments，却把另一个 description
         留在外面。原来判据是 `len(args) != 1` -> 直接原样返回 ->
         dsh 收到 {"arguments":..., "description":...}，找不到 code，报
             Error: invalid arguments: missing required property "code"
         白烧一轮。
         现在：把包裹键里的内容**摊平**出来，跟其他顶层键合并
         （冲突时**以包裹键里的为准** —— 那才是模型真正想传的参数）。
    """
    if not isinstance(args, dict) or not args:
        return args
    # 找包裹键（最多一个；多个说明不是这个形态）
    _wk = [k for k in args if k in WRAP_KEYS]
    if len(_wk) != 1:
        return args
    k = _wk[0]
    v = args[k]
    if isinstance(v, str):
        try:
            v = json.loads(v)
        except (ValueError, TypeError):
            return args
    if not isinstance(v, dict) or not v:
        return args
    if len(args) == 1:
        return v
    # 摊平：包裹键里的键值优先，其余的补进来。
    # 摊完还是得**有内容**才替换（空 dict 会让调用变成没有参数）。
    merged = dict(v)
    for k2, v2 in args.items():
        if k2 == k:
            continue
        merged.setdefault(k2, v2)
    return merged if merged else args


def _native_params(body):
    """从一个 invoke 的正文里抠出所有 parameter，收尾标记写法随便变都能扛。"""
    hits = list(NATIVE_PARAM.finditer(body))
    args = {}
    for i, m in enumerate(hits):
        end = hits[i + 1].start() if i + 1 < len(hits) else len(body)
        val = body[m.end():end]
        # 削掉值后面粘着的收尾标签；可能连着好几层（parameter + invoke + calls）
        for _ in range(4):
            trimmed = NATIVE_TAIL.sub("", val)
            if trimmed == val:
                break
            val = trimmed
        args[m.group(1)] = _coerce(val.strip().lstrip(">").strip())
    return _unwrap_args(args)


def _infer_tool(params, tools):
    """模型漏写 invoke 那一层时，按参数名反推是哪个工具。

    判据：拿每个工具 parameters.properties 的键去和实际出现的参数名比，
    重合最多的就是它。一个都对不上就返回 None —— 宁可不认，也别认错工具
    （认错会把参数喂给别的工具，比不调用危险）。
    """
    if not tools or not params:
        return None
    keys = set(params)
    best, best_n = None, 0
    for t in tools:
        fn = t.get("function") if isinstance(t, dict) else None
        fn = fn or (t if isinstance(t, dict) else {})
        name = fn.get("name")
        props = (fn.get("parameters") or {}).get("properties") or {}
        if not name or not isinstance(props, dict) or not props:
            continue
        n = len(keys & set(props))
        if n > best_n:
            best, best_n = name, n
    return best


def _from_native(text, tools=None):
    text = text or ""
    out, spans = [], []
    for i, m in enumerate(NATIVE_INVOKE.finditer(text)):
        out.append(_mk(i, m.group(1), _native_params(m.group(2))))
        spans.append(m.span())
    if out:
        return out, spans

    # 2026-09-22 加（实测 18:05 / 18:09 两次）：模型经常漏写 invoke 那一层，
    # 只给 calls + 若干个 parameter。原来这里直接落空 -> parse_tool_calls 返回
    # None -> 安全网 looks_truncated 反而命中（TRUNC_HINT 正好匹配 calls 标记）
    # -> 一整条完整调用被误判成「被截断」，白烧两次接续，最后把裸标记交给客户端。
    # 现在按参数名反推工具，补上那一层。
    _hits = list(NATIVE_PARAM.finditer(text))
    if _hits:
        _params = _native_params(text[_hits[0].start():])
        _name = _infer_tool(_params, tools)
        if _name and _params:
            return ([_mk(0, _name, _params)],
                    [(_hits[0].start(), len(text))])

    for i, m in enumerate(NATIVE_SEP.finditer(text)):
        out.append(_mk(i, m.group(1), m.group(2)))
        spans.append(m.span())
    return (out, spans) if out else (None, [])



NATIVE_AWAIT = re.compile(
    r'await\s+tools\.([A-Za-z_][\w]*)\s*\(', re.S)


def _from_bare_await(text, tools=None):
    """认「裸的 await tools.<名>({...})」—— 模型漏了外面那层 JSON 壳。

    2026-10-02 加。实测（19:18:02 [113]）：
        const r = await tools.pwsh({ command: "Get-ChildItem -Path 'C:/...'", description: "..." })
    模型直接写了 PTC 程序里的那一行，**没有包 {"tool_calls":[...]}**。
    而 _from_json 要求有一个能 raw_decode 出来的 JSON 对象 ——
    { command: ... } 里 command 没有引号，不是合法 JSON；
    所以三个解析器全落空 -> 当正文 -> 贴提醒重发一次 -> 还认不出 -> 放行。
    那一轮白烧（最近 2500 行里 4 次）。

    ## 判据卡得很死（防误判）

    正文里出现 `await tools.xxx(` 这种形态，**可能只是模型在举例**。所以：
      ① 工具名必须是**本轮工具表里真有的**（tools 参数给了才判）；
      ② 参数表必须能解析出来（括号配平 + 内容像 {k: v}）；
      ③ 整段正文里**只认第一处**，且要求它前后没有别的 await tools.
    三条都过才认。任何一条不过就返回 None，让上层照原样当正文。
    """
    text = text or ""
    hits = list(NATIVE_AWAIT.finditer(text))
    if not hits:
        return None, []
    # ③ 只在一处出现时才算（多处说明是在示范/列表里，不是真调用）
    if len(hits) > 1:
        return None, []
    # ④ **那段代码必须独占正文** —— 前后不能有成句的散文。
    #
    # 2026-10-02 收紧（实测）：不加这条的话，「你可以这样调用：
    # const r = await tools.pwsh({ command: "ls" }) 然后看结果。」
    # 也会被认成真调用 —— 那是**误判**，会白跑一个命令。
    # 误判比漏判糟：漏判只是回到「当正文」的现状，误判会执行模型根本没打算执行的东西。
    #
    # 判据：代码两侧的残留文本里，不能有「像句子的东西」——
    #   中文逗号/句号/问号、或者 6 个以上连续中文（说明在讲话，不在写代码）。
    _head = text[:hits[0].start()]
    _PROSE = re.compile(r'[\u4e00-\u9fff]{6,}|[\uff0c\u3002\uff1f\uff01\uff1b]')
    if _PROSE.search(_head):
        return None, []
    m = hits[0]
    name = m.group(1)
    # ① 工具名要在本轮工具表里
    if tools is not None:
        try:
            if not has_tool(tools, name):
                return None, []
        except BaseException:        # noqa: BLE001
            pass
    # ② 找配平的参数括号
    i = m.end() - 1          # 指到 '('
    depth, j = 0, i
    in_s, esc, quote = False, False, ""
    while j < len(text):
        ch = text[j]
        if in_s:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == quote:
                in_s = False
        else:
            if ch in "\"'`":
                in_s, quote = True, ch
            elif ch == "(":
                depth += 1
            elif ch == ")":
                depth -= 1
                if depth == 0:
                    j += 1
                    break
        j += 1
    if depth != 0:
        return None, []
    args_src = text[i + 1:j - 1].strip()
    if not args_src:
        return None, []
    # 参数是 JS 对象字面量（键可以没引号）—— 宽松解析：键补引号后当 JSON 读。
    args = _loose_js_args(args_src)
    if not args:
        return None, []
    return [_mk(0, name, args)], [(m.start(), j)]


def _loose_js_args(src):
    """把 JS 对象字面量松散地读成 dict。读不出来返回 None（宁可放过不误判）。"""
    s = (src or "").strip()
    if not (s.startswith("{") and s.endswith("}")):
        return None
    try:
        v = json.loads(s)
        return v if isinstance(v, dict) else None
    except (ValueError, TypeError):
        pass
    # 键补引号：{ command: "x" } -> {"command": "x"}
    #
    # 2026-10-02 修（实测抓到的）：原来这里还有一句 `fixed.replace("'", '"')`，
    # 想顺便把单引号字符串也转了 —— **那是错的**：
    #   { command: "Get-ChildItem -Path 'C:/x'", ... }
    #   \__ 值里的单引号被换成双引号，把外面那层双引号字符串截断了 -> JSON 解不开。
    # 实测两个真实样本（19:18:02 / 18:20:05）全卡在这一步。
    # 现在只补键的引号，值原样交给 json.loads（值本来就是双引号包裹的）。
    try:
        fixed = re.sub(r'([{,]\s*)([A-Za-z_$][\w$]*)(\s*:)', r'\1"\2"\3', s)
        v = json.loads(fixed)
        return v if isinstance(v, dict) else None
    except (ValueError, TypeError):
        pass
    # 值也用单引号包着的场合（较少）：把**成对**的单引号换成双引号，再试。
    try:
        fixed2 = re.sub(r'([{,]\s*)([A-Za-z_$][\w$]*)(\s*:)', r'\1"\2"\3', s)
        _SQ = chr(39)          # 单引号
        _DQ = chr(34)          # 双引号
        _pat = _SQ + "([^" + _SQ + _DQ + "]*)" + _SQ
        fixed2 = re.sub(_pat, _DQ + chr(92) + "1" + _DQ, fixed2)
        v = json.loads(fixed2)
        return v if isinstance(v, dict) else None
    except (ValueError, TypeError):
        return None


def _coerce(val):
    """参数值都是字符串，看着像数字/布尔/JSON 的还回原类型，别喂错类型给工具。"""
    if val in ("true", "false", "null"):
        return {"true": True, "false": False, "null": None}[val]
    if re.fullmatch(r"-?\d+", val):
        return int(val)
    if re.fullmatch(r"-?\d*\.\d+", val):
        return float(val)
    if val[:1] in "[{" and val[-1:] in "]}":
        try:
            return json.loads(val)
        except json.JSONDecodeError:
            return val
    return val


# 上游单次输出有长度上限（实测两万多字符）。写长文件时，整个文件塞进一个 JSON
# 字符串参数、还要双重转义，很容易正好在中途被砍断 —— 于是 JSON 解不开、工具调用
# 丢失，模型只好重新来一遍，就是所谓的「断层」。
TRUNC_HINT = re.compile(
    r'^[ \t]*\{\s*"tool_calls"|invoke\s+name="|(?:calls|invoke)[^\n<>]{0,40}>',
    re.S | re.I | re.M)


def looks_truncated(text, tools):
    """给了工具、文本里起了工具调用、整体解析不出来、尾部还留着没写完的结构
    -> 判定被截断。2026-09-26 第72/73步收紧：TRUNC_HINT 判别力极低（1193 条
    语料命中 1102 条），只靠 parse 失败做第一道闸仍会误报 —— 第71步实测
    11 次「回复被截断」全是误判，其中 chars=2277 那条是正文里夹了 json 示例
    的正常回复。所以再加第二道闸，并且：
      ① 整段话以句末标点收尾 -> 是写完的话，不是被砍断，直接放行；
      ② 用【最后一个】TRUNC_HINT 命中切段 —— 真截断的调用块就在尾部，段内
         括号/方括号不配平、全文 <invoke> 没闭合、或围栏数为奇才算数。
    """
    if not tools:
        return False
    t = text or ""
    if not TRUNC_HINT.search(t):
        return False
    if parse_tool_calls(t, tools) is not None:
        return False
    if t.rstrip()[-1:] in "。！？.!?":
        return False
    m = None
    for _m in TRUNC_HINT.finditer(t):
        m = _m
    # 2026-09-27 第338步（证据：第337i回放 2719 条 turn 语料）：距末尾闸。
    # 真截断的调用块必在【尾部】—— 语料里真截断的「最后一个 HINT 命中点到
    # 末尾」全部 <=400（max=400），误判的（正文里夹示例/模板）全部 >=402
    # （min=402），两侧完全分离。命中点离末尾太远 -> 不是被砍断，直接放行。
    if (len(t) - m.start()) > 400:
        return False
    seg = t[m.start():]
    if seg.count("{") > seg.count("}"):
        return True
    if seg.count("[") > seg.count("]"):
        return True
    if t.count("<invoke") > t.count("</invoke>"):
        return True
    return seg.count("```") % 2 == 1

def stitch(head, tail):
    """接续时模型常把结尾重复一遍，把重叠部分去掉再拼。"""
    head, tail = head or "", (tail or "").lstrip()
    for n in range(min(400, len(head), len(tail)), 20, -1):
        if head.endswith(tail[:n]):
            return head + tail[n:]
    return head + tail


# 接续到底也没拼出完整调用时，别把那一大坨半截 JSON 当正文吐给客户端 ——
# 用户会看到满屏源码，模型下一轮也会被自己的垃圾输出带偏，还常常误判成
# 「文件系统坏了」。换成一句明确的指令，把它推回分段写的正道上。
SALVAGE_NOTE = r"""
[桥接] 这一轮的工具调用**没能解析出来**，自动接续也没能补完，所以**没有任何工具被执行**，磁盘上什么都没变。文件系统是正常的 —— 不要去查磁盘，也不要重读文件找原因。

两种原因，桥分不清是哪一种，但处理办法一样：
  A. 内容太长，回复在中途被上游砍断；
  B. 大段内容塞进一个 JSON 字符串，里面的引号或反斜杠转义出错（实测 6 千字的单参数就炸过）。

重来一次，二选一：

1) 要写长内容（多行代码、长文本）-> 改用**原生格式，不做 JSON 转义**。桥同样认这种写法：

<invoke name="write">
<parameter name="file_path">目标文件的全路径</parameter>
<parameter name="content">第一行
第二行 —— 里面的双引号、反斜杠、模板串都原样写，不用转义</parameter>
</invoke>

   要点：一个 invoke 只放一个工具；参数按上面那样换行写；除了这段不要输出别的内容。

2) 或者**分段写**：一轮只写不超过 3000 字符的一段，先用 write 写第一段、拿到成功回执后，下一轮再追加下一段，直到写完。拆分点选在行边界上，不要在字符串或标签中间断开。

不要再试图把整个文件塞进一个参数里。
"""


# 2026-10-03（用户口径「应该所有的提示词都得在控制台 因为好管理」）：
# 模块级函数（_proto_for / attach_note_default / tail_of / salvage …）拿不到
# AccountPool 实例，读不到 self.conf。这张表就是给它们的接通层：
#   · 出厂值 = 各常量，所以「没被灌过」时行为与改造前逐字一致；
#   · AccountPool.__init__ / apply() 之后会把 ini 里的值灌进来；
#   · 读取一律走 pget()，空串 = 没设 = 回落到厂值。
#
# 注意：本表**必须**放在所有提示词常量定义之后（最后一个常量是 SALVAGE_NOTE）。
# 引用它的函数定义在前面没关系 —— 它们是运行时才读。
PROMPTS = {
    "tool_protocol": TOOL_PROTOCOL,
    "checkpoint_instruction": CHECKPOINT_INSTRUCTION,
    "salvage_note": SALVAGE_NOTE,
    "ctx_empty_note": CTX_EMPTY_NOTE,
    "nudge_body": NUDGE_BODY,
    "attach_note": ATTACH_NOTE,
    "attach_note_no_cp": ATTACH_NOTE_NO_CP,
    "attach_note_local": ATTACH_NOTE_LOCAL,
    "attach_ctx_lines": ATTACH_CTX_LINES,
    "standing_note": CHUNK_REMINDER,
    "pool_note": POOL_NOTE_DEFAULT,
    "format_note": "",
    "handoff_note": HANDOFF_PROMPT,
}


def pget(key, dflt):
    """读一份提示词：配置里非空就用配置，否则回落出厂值。"""
    try:
        v = PROMPTS.get(key)
    except BaseException:            # noqa: BLE001
        return dflt
    return dflt if v is None or str(v) == "" else str(v)


def pset(key, value, factory):
    """灌一份提示词。空串 -> 回落到厂值（界面上清空 = 用出厂那份）。"""
    try:
        PROMPTS[key] = factory if (value is None or str(value) == "") else str(value)
    except BaseException:            # noqa: BLE001
        pass


def salvage(text, tools):
    """解析彻底失败时给客户端看的内容。"""
    return pget("salvage_note", SALVAGE_NOTE) \
        if looks_truncated(text, tools) else text


# 上游限流。网页接口的说法是「消息发送过于频繁」，HTTP 层可能是 429。
# 这种错误绝对不能立刻重试 —— 重试就是加速封号，必须先退避。
RATE_HINT = re.compile(
    r"过于频繁|太频繁|频繁操作|请稍后|稍后再试|429|rate.?limit|too many requests",
    re.I)

# 按需联网（2026-09-22）：最新用户消息里出现网址才开搜索。
URL_HINT = re.compile(r"https?://|www\.", re.I)


def last_user_text(messages):
    """最新一条 user/developer 消息的文本 —— 判「用户是不是给了链接」。"""
    for m in reversed(messages or []):
        if (m.get("role") or "") in ("user", "developer"):
            return _text_of(m.get("content"))
    return ""


def is_rate_limited(exc):
    return bool(RATE_HINT.search(f"{exc}"))


# 2026-10-02 加：**上游断流**。
#
# 用户报错原文：
#   [桥接] 上游失败：ChunkedEncodingError: Response ended prematurely
#   这一轮没有任何工具被执行。要继续就重发一次。
#
# 实测栈：ds_api.py:473 iter_lines -> urllib3 read_chunked ->
# 「Response ended prematurely」：**上游吐到一半把连接掉了**。
# 同时段另有 biz_code=7 rate limit reached —— 限流的第二种表现（第一种是
# 明确回 biz_code=7，桥已经会认）。
STREAM_CUT_HINT = re.compile(
    r"Response ended prematurely|ChunkedEncodingError|"
    r"IncompleteRead|ProtocolError|Connection broken",
    re.I)


def is_stream_cut(exc):
    """这个错是不是「上游吐到一半断了」。

    与限流分开：限流要冷却 + 换号；断流只需退避重发（多半是网络抖动）。
    """
    return bool(STREAM_CUT_HINT.search(f"{type(exc).__name__}: {exc}"))


# 2026-10-01：单发在途多久算「卡住」。**不是性能阈值，是粗上限** ——
# 远远超过任何合理单发（实测正常 10~90 秒），撞上就是真挂死。
# 用户口径：「为何需要时间统计，还有别的方法」—— 对的，不需要分布，
# 只需要一个「远超合理值」的上限，配合「在途时长可见」这两件事。
STUCK_SEC = 600.0


class RequestTooLargeError(RuntimeError):
    """请求体积超过桥的硬上限（Phase A / 2026-09-30 加）。

    这个是**桥自己**判的，不是上游返回的 —— 唯一发送收口 _ask() 在真正打
    HTTP 之前检查 prompt 长度，超过 HARD_LIMIT_CHARS 就抛这个。它不触发限流
    冷却、不触发换号、不触发「开新会话重试」—— 那些都是治限流的药，治不了
    「这一发本身就太大」这个病。
    """
    pass


def session_gone(exc):
    """这个错是不是「那个会话没了」—— 只有这种才值得抛掉记录、重开新会话。

    网络层的错（连接被重置、读超时、代理抽风）跟会话在不在毫无关系。以前一律
    当「会话失效」处理，于是：发张截图请求变大 → 传输失败 → 记录被抹掉 →
    下一条消息重开新会话，把几十万字上下文整个重发一遍。日志里连着几次
    「新会话 38 万字 图片=2」就是这么来的。
    """
    if isinstance(exc, requests.exceptions.RequestException) \
            and not isinstance(exc, requests.exceptions.HTTPError):
        return False              # 传输层故障，会话还在，留着记录下次接着用
    return True







def _short_id():
    return hashlib.sha1(repr(time.time()).encode()).hexdigest()[:12]


# 网页版接口不返回 token 计数，只能估：中日韩字按 1 个 token 算，其余按 4 字符
# 一个 token。数量级对得上，用来看「这轮花了多少」够了，别当账单使。
CJK = re.compile(r"[\u3040-\u30ff\u3400-\u9fff\uf900-\ufaff\uff00-\uffef]")


def est_tokens(text):
    text = text or ""
    if not text:
        return 0
    cjk = len(CJK.findall(text))
    return max(1, cjk + (len(text) - cjk) // 4)


def dsh_tokens(chars):
    """按 dsh 的口径折 token：4 字符 = 1 token（`dsh-token-meter:16`）。

    回报 usage 时用它，好让 dsh 的用量条和我们给的 contextWindow
    （= 输入上限 / 4）落在同一把尺子上。
    """
    return max(0, int(chars) // 4)



# 联网搜索时正文里带的是 [citation:3] 这种裸标记，客户端不认，原样显示很难看
CITE = re.compile(r"\[citation:(\d+)\]")


def cite_links(text, refs):
    """把正文里的 [citation:N] 换成能点的 [N] 链接。"""
    if not refs:
        return text

    def link(m):
        i = int(m.group(1))
        url = (refs[i - 1].get("url") or "") if 1 <= i <= len(refs) else ""
        return rf"[\[{i}\]]({url})" if url else f"[{i}]"

    return CITE.sub(link, text or "")


def sources_block(refs):
    """末尾那份来源清单。

    ds_gui 是自己渲染 refs 的，桥接这边没有额外通道能把来源传给客户端，
    所以直接写进 Markdown 正文 —— dsh / Codex 都能渲染。
    """
    if not refs:
        return ""
    lines = []
    for i, r in enumerate(refs, 1):
        url = r.get("url") or ""
        name = (r.get("title") or url or "").strip().replace("\n", " ")
        site = r.get("site_name") or ""
        lines.append(f"{i}. [{name}]({url})" + (f" — {site}" if site else ""))
    return (f"\n\n---\n\n**参考来源 {len(refs)} 条**\n\n" + "\n".join(lines))





# ====================== Responses 协议（Codex 用这个）======================
#
# Codex CLI 从 2026 年 2 月起彻底去掉了 wire_api="chat"（本机 0.154 实测直接
# 报「`wire_api = "chat"` is no longer supported」），只认 /v1/responses。
# 所以这里把 Responses 的请求翻译成内部那套 messages，再把回复按
# Responses 的事件流吐回去，中间那层（会话复用、工具模拟、图片上传）全复用。

def responses_to_messages(req):
    """Responses 的 instructions + input[] → 内部 messages + tools。"""
    messages = []
    if (req.get("instructions") or "").strip():
        messages.append({"role": "system", "content": req["instructions"]})

    raw = req.get("input")
    if isinstance(raw, str):
        raw = [{"role": "user", "content": raw}]

    names = {}          # call_id → 工具名，function_call_output 里只有 call_id
    for it in raw or []:
        if isinstance(it, str):
            messages.append({"role": "user", "content": it})
            continue
        if not isinstance(it, dict):
            continue
        kind = it.get("type") or "message"

        if kind in ("function_call", "custom_tool_call"):
            cid = it.get("call_id") or it.get("id") or "call"
            names[cid] = it.get("name") or ""
            messages.append({"role": "assistant", "content": "", "tool_calls": [{
                "id": cid, "type": "function",
                "function": {"name": it.get("name") or "",
                             "arguments": it.get("arguments") or "{}"}}]})
        elif kind in ("function_call_output", "custom_tool_call_output"):
            out = it.get("output")
            if isinstance(out, dict):
                out = out.get("content") or json.dumps(out, ensure_ascii=False)
            cid = it.get("call_id") or ""
            messages.append({"role": "tool", "tool_call_id": cid,
                             "name": names.get(cid, ""),
                             "content": out or ""})
        elif kind == "reasoning":
            continue                       # 自己的思考不回灌
        else:
            messages.append({"role": it.get("role") or "user",
                             "content": _responses_content(it.get("content"))})


    tools = []
    for t in req.get("tools") or []:
        if not isinstance(t, dict):
            continue
        if t.get("type") in (None, "function", "custom"):
            # Responses 里 name/parameters 是平铺的，转成 chat 那种嵌套形状
            tools.append({"type": "function", "function": {
                "name": t.get("name") or (t.get("function") or {}).get("name"),
                "description": t.get("description") or "",
                "parameters": t.get("parameters")
                or (t.get("function") or {}).get("parameters") or {}}})
    return messages, tools


def _responses_content(content):
    """Responses 的 content 段落 → chat 那种多段数组，好让下游统一处理。"""
    if content is None or isinstance(content, str):
        return content
    out = []
    for seg in content if isinstance(content, list) else []:
        if isinstance(seg, str):
            out.append({"type": "text", "text": seg})
        elif isinstance(seg, dict):
            t = seg.get("type")
            if t in ("input_text", "output_text", "text", "summary_text"):
                out.append({"type": "text", "text": seg.get("text") or ""})
            elif t in ("input_image", "image_url", "image"):
                out.append({"type": "image_url",
                            "image_url": {"url": seg.get("image_url")
                                          or seg.get("url") or ""}})
    return out


def revent(kind, **fields):
    body = {"type": kind}
    body.update(fields)
    return (f"event: {kind}\n"
            f"data: {json.dumps(body, ensure_ascii=False)}\n\n").encode()



# ============================== 会话复用 ==============================

def _owner_tag(ds):
    """会话表按账号分组存，用 token 的哈希当组名，别把 token 本身写进文件。"""
    token = (getattr(ds, "cfg", None) or {}).get("token") or ""
    return hashlib.sha1(token.encode()).hexdigest()[:12] if token else "anon"


def last_archived_reply(slug, max_bytes=2 * 1024 * 1024):
    """从回复存档里捞这个号最近一条非空正文。

    桥刚重启时 Bridge.last_reply / last_checkpoint 都是空的，交接只能产出一个
    206 字的标题空壳（实测 15:35 那三次）。存档是落盘的，读一遍就能补上。
    """
    if not slug:
        return "", 0.0
    # 2026-10-01 第458步：读**本组**的回复存档（隔离，否则会在别组的正文里找）
    _af = grp_path("replies", slug, create=True) or REPLY_FILE
    try:
        with _af.open("rb") as f:
            f.seek(0, 2)
            size = f.tell()
            start = max(0, size - max_bytes)
            f.seek(start)
            blob = f.read().decode("utf-8", "replace")
    except OSError:
        return "", 0.0
    lines = blob.splitlines()
    if start:                      # 首行可能被切掉一半，不能用
        lines = lines[1:]
    text, at = "", 0.0
    for line in lines:
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if row.get("slug") != slug:
            continue
        body = (row.get("text") or "").strip()
        if body:
            text, at = body, float(row.get("t") or 0.0)
    return text, at


# ===== 交接缺口检索（2026-09-30 用户口径：轮询不可能天天丢数据）=====
# 设计要点：**不传数据，传指针**。
#   换号那一发体积上限（HANDOFF_BUDGET 3 万字）不变，但从「把中段丢掉」
#   改成「中段仍在 _relay_replies.jsonl 里，交接单给出缺口索引 + 检索方法」。
#   于是零丢失 + 小体积同时成立。
#
# 历史沿革：这个文件原来只有 last_archived_reply() 从尾部捞**一条**，
# 用来在桥重启后补 last_reply。全量正文其实一直在盘上（30MB+），
# 只是没人按区间去读。这两个函数补上这个能力。


def archive_files(slug=""):
    """回复存档（主档 + 带时间戳的历史档），按文件名排序 = 时间序。

    2026-09-30 之前轮转是破坏性的（.1 会被覆盖），所以只能拿到最近的；
    改过之后每个归档都带时间戳且永不删除，这里按名排序天然就是时间序。

    2026-10-01 第458步：**收 slug，按组找存档。** 用户口径「要完全隔离
    每个项目都不一样 如果不隔离乱了 白瞎 token」—— 在别组的正文里捞，
    捞出来的东西既浪费上下文又误导接手方。
    slug 空 = 全局那份（兼容/统计用）。
    """
    out = []
    _main = grp_path("replies", slug) or REPLY_FILE
    try:
        base = _main.parent
        pat = _main.name + "*"
        for p in sorted(base.glob(pat)):
            if p.is_file():
                out.append(p)
    except OSError:
        pass
    if not out and _main.is_file():
        out = [_main]
    return out


def archive_scan(slug="", since=0.0, until=0.0, kinds=None, limit=0,
                 with_text=True, max_bytes=0):
    """按条件扫存档，返回 [{t, ts, k, slug, sid, text}]（时间升序）。

    这是「零丢失」的读取端：交接单只给缺口区间，接手方（或桥自己）用这个
    把丢掉的那些原文捞回来。

    slug   只取这个号（空 = 全部）
    since/until  时间戳区间（0 = 不限）
    kinds  只取这些 k（None = 全部）
    limit  >0 时只留**最近** limit 条（先全扫再截尾）
    max_bytes >0 时只扫各档最后这么多字节（大档快速取尾部用）
    """
    rows = []
    for p in archive_files(slug):   # 458步：按组找存档
        try:
            with p.open("rb") as f:
                if max_bytes and max_bytes > 0:
                    f.seek(0, 2)
                    size = f.tell()
                    start = max(0, size - max_bytes)
                    f.seek(start)
                    blob = f.read().decode("utf-8", "replace")
                    lines = blob.splitlines()
                    if start:
                        lines = lines[1:]      # 首行可能被切断
                else:
                    blob = f.read().decode("utf-8", "replace")
                    lines = blob.splitlines()
        except OSError:
            continue
        for ln in lines:
            if not ln.strip():
                continue
            try:
                row = json.loads(ln)
            except ValueError:
                continue
            t = float(row.get("t") or 0.0)
            if slug and row.get("slug") != slug:
                continue
            if since and t < since:
                continue
            if until and t > until:
                continue
            if kinds and row.get("k") not in kinds:
                continue
            body = (row.get("text") or "")
            if not with_text:
                body = ""
            rows.append({"t": t, "ts": row.get("ts") or "", "k": row.get("k") or "",
                         "slug": row.get("slug") or "", "sid": row.get("sid") or "",
                         "chars": int(row.get("chars") or len(body)),
                         "text": body})
    rows.sort(key=lambda r: r["t"])
    if limit and len(rows) > limit:
        rows = rows[-limit:]
    return rows


def archive_index(slug="", since=0.0, until=0.0, kinds=None, head=160):
    """只取**索引**（不含全文），给交接单用 —— 这才是「数据量小」的关键。

    返回 [{t, ts, k, chars, head}]，head 是正文开头一小段，够接手方判断
    「这条要不要去翻全文」，但远小于原文。
    """
    rows = archive_scan(slug=slug, since=since, until=until, kinds=kinds,
                        with_text=True)
    out = []
    for r in rows:
        _t = r["text"] or ""
        out.append({"t": r["t"], "ts": r["ts"], "k": r["k"],
                    "chars": r["chars"],
                    "head": _t[:head].replace(chr(10), " ").strip()})
    return out


# 工具调用残渣的剥壳器（2026-09-30）。
# 实测：全库存 9592 条 turn 里 **8565 条（89.3%）** 开头就是工具调用文本 ——
# 模型一轮常输出「一段正文 + 工具块」，桥把工具块摘去执行了，但存 last_reply
# 和存存档时留的是**原始未清洗文本**。于是换号交接拿到的是 run_code 脚本源码、
# present 的 JSON，而不是「任务做到哪、下一步是什么」。接手方**以为自己拿到了
# 交接**，实际拿到噪音 —— 这比空交接更危险。
#
# 关键：残渣**前面**往往有真正的结论（"桥其实已起来…继续验证并处理剩余项。"）。
# 所以不是整条丢弃，而是**剥掉工具块、留下正文**；正文不足才判为无效。
_RESIDUE_PATS = (
    # 原生标记：DSML calls …（分隔符字符会变，锚关键字）
    re.compile(r'<[^\n<>]{0,40}?(?:DSML|｜|▁)[^\n<>]{0,40}?>.*?\Z', re.S),
    re.compile(r'<\|[^\n<>]{0,40}?(?:DSML|tool)[^\n<>]{0,40}?>.*?\Z', re.S),
    # ```json {"tool_calls": …} 围栏块（含无围栏的裸 JSON）
    re.compile(r'```[ \t]*(?:json)?[ \t]*\r?\n?[ \t]*\{\s*"tool_calls".*?\Z', re.S),
    re.compile(r'\{\s*"tool_calls"\s*:.*?\Z', re.S),
)


def strip_tool_residue(text):
    """剥掉工具调用残渣，返回剩下的真正文。

    与 _drop_spans 的分工：那套是「已知调用区间」时精确抠除（解析路径用），
    这套是「不知道区间、只有一段可疑文本」时的兜底（存档/交接路径用）。
    两者不互相替代，因为存档路径拿不到 spans。
    """
    s = text or ""
    if not s.strip():
        return ""
    # 从每个可疑起点切掉到结尾：工具块总在尾部，正文在前面。
    cut = len(s)
    for pat in _RESIDUE_PATS:
        for m in pat.finditer(s):
            if m.start() < cut:
                cut = m.start()
    s = s[:cut]
    # 收尾壳：围栏、裸标签
    s = re.sub(r'```[ \t]*\w*[ \t]*\Z', '', s)
    s = re.sub(r'<[^\n<>]{0,60}?(?:DSML|｜|▁)[^\n<>]{0,40}?>?[ \t]*\Z', '', s)
    s = re.sub(r'\n{3,}', '\n\n', s)
    return s.strip()


# 剥完剩多少字才算「有自己的结论」。取值依据（2026-09-30 对全库存
# 9592 条 turn 实测，剥后正文长度分布）：
#     p25=0  p50=0  p75=95  p90=304  p95=1137
# **一半以上的轮次剥完是 0 字**（纯工具调用、一句话都没有）—— 判据的真正
# 分界在「是不是 0」，不在「够不够长」。
#
# 第一版取 60，实测**误杀 1075 条**，样本全是有价值的状态陈述：
#     "脚本内引号嵌套出错。改用 write 落盘脚本再执行。"     (27 字)
#     "桥进程重启后没起来（11435 无监听）。先抓启动错误。"   (28 字)
#     "C7 日志格式已生效（验证清单第 3 项通过）…"           (59 字)
# 这些正是接手方最需要的「上一棒卡在哪」。所以阈值降到 12 ——
# 能凑出一句完整的话就留；真正要挡的只是「剥完为空」和「只剩嗯/好」。
# 12 是「一句话」的下限，不是内容质量线。
RESIDUE_MIN_CHARS = 12


def looks_like_tool_residue(text):
    """这条正文是不是「本质上是工具调用、没有可用结论」。"""
    s = (text or "").strip()
    if not s:
        return True
    body = strip_tool_residue(s)
    # 剥完还剩多少「自己的话」
    return len(body) < RESIDUE_MIN_CHARS
class Keys(list):
    """这一轮的消息指纹，**外加**一份判别性指纹 nc。

    keys 在桥里是当不透明凭据传的：plan() 造它，run()/remember()/affinity()/
    checkpoint_dump() 只经手不解读。所以把 nc 挂成属性，比到处加参数安全 ——
    十来个调用点一个都不用改签名。

    nc = 去掉公共消息（system 提示 / AGENTS.md 提醒 / 运行时快照）之后的指纹。
    认亲只看 nc；去重仍看全量 keys。**两者不能合一**：公共消息必须留在 keys
    里，否则 _delta_items 会把 10 万字系统提示当成「没见过的新消息」每轮重发。
    """

    __slots__ = ("nc",)

    def __init__(self, keys, nc):
        super().__init__(keys)
        self.nc = list(nc)


class SessionCache:

    """记住「已经发过哪些消息」→ DeepSeek 的 (session_id, 末条消息 id)。

    客户端每轮都把完整 messages 重发一遍。这里按消息逐条算指纹，存成一个集合；
    下一轮进来时找「重合最多」的那条会话，只把它还没见过的几条发上去。

    刻意**不**要求严格前缀。dsh 压缩上下文时会把前面几十条重写成一条摘要，
    前缀必然对不上 —— 那就是每压缩一次就白开一个新窗口、还要把整段历史重传的
    原因。按重合度认会话的话，压缩后靠近末尾那些原样保留的消息还能把它认回来。

    只给非 assistant 消息算指纹：我们发出去的助手回复和客户端回灌的形状不一定
    一样（JSON 代码块 vs tool_calls 字段），拿它对比会无谓地对不上，一对不上就
    只能开新会话 —— 那就是你在会话列表里看到一堆新窗口的原因。

    整张表落盘（按账号分组），所以重启桥接也能接着用旧会话。
    """

    FILE = state_file("sessions.json", "ds_bridge_sessions.json")

    # 落盘是「整文件读 → 改自己那格 → 整文件写」。多账号同时收尾时，各自的
    # 实例锁互不相干，两边都拿着改之前的快照去写，后写的那个把前一个刚存的
    # 记录整格盖回旧值 —— 表现就是会话莫名其妙丢了、下一轮重开窗口。
    # 所以文件这一层要一把所有实例共用的锁。
    _FILE_LOCK = threading.RLock()


    # 只重合一条不算同一个对话：所有 dsh 对话都共用同一段系统提示，
    # 光凭它就认亲的话，新开的对话会被塞进别人的窗口。
    MIN_OVERLAP = 2

    # 2026-09-25 加。上面那条 2 其实挡不住 —— 因为它数的正是「公共消息」。
    # 证据（C:/Users/Lenovo/Desktop/teste/_串窗口证据链.md）：
    #   · dsh 每轮注入 system 提示 / AGENTS.md <system-reminder> /
    #     Current runtime context，这三条在**所有窗口之间逐字相同**；
    #   · 从 sessions/*.zstd 原文解出的指纹 adb9cf247f4c9a72（2034 字符的
    #     <system-reminder>）在 5/6 个账号的会话行里都出现，83bb6425c2427a5d
    #     （390 字符的 Current runtime context）同样 5/6；
    #   · 一个真实新窗口（sessions.json 里 35aeea28 那行）总共只有 3 个 key，
    #     拿它逐账号跑 match()：在 a849b5faaaec 上重合 2 → 认成 3324 keys 的
    #     老会话，在 d77512a20424 上重合 2 → 认成 3070 keys 的老会话。
    # 重合 2 恰好等于 MIN_OVERLAP，于是新窗口必然被塞进别人的老历史。
    #
    # 所以改成只数**判别性指纹** nc（公共消息一律不算）：新窗口的 nc 只有
    # 「它自己那句话」，跟老会话重合 0 → 老老实实开新会话；真续接的 nc
    # 重合几百条 → 照样认得出。
    MIN_NC = 3

    # 2026-10-01 第427步：压力旁路要求的最小重合条数。
    # 取值依据见 _same_talk 注释 —— 成功续接中位 36、认不出时中位 24，
    # 8 是远低于两者的保守下限；MIN_NC=3 太低（0.3% 的真新窗口能撞上）。
    STRESS_MIN_NC = 8

    # 2026-09-25 交叉验证加。只「有重合」远远不够 —— 用户天天说的词、dsh 的
    # 注入通知、同一个文件的读取结果，在**不同窗口之间逐字相同**：实测 23 个
    # 真实会话里 254 个指纹出现在 ≥2 个会话中，其中 59 条正文 >1000 字（同文件
    # 读取），还有 继续(9会话)/好的(7)/可以(5)/好(4)/嗯(4)，以及
    # 「The approval policy changed…」(15 会话) 那条注入通知。
    #
    # 拿 15 个会话交叉验证（每个当老行、别的会话开头当新窗口，210 个组合）：
    #   n>=1                  -> 真续接 15/15，误判 90   <- 43% 误判率，不能用
    #   n>=3                  -> 真续接 15/15，误判 4
    #   n>=3  且碰尾 8         -> 真续接 15/15，误判 0    <- 但新窗口前 3 轮全重发
    #   n>=min(3,行长) 且碰尾 8  -> 真续接 15/15，误判 0   <- 选这个
    #
    # 尾部判据的道理：能接上，是因为你知道**我刚才最后在干什么**。同读一个
    # 文件、同说一句「继续」，那是散落在行中间的重合，碰不到尾部。
    TAIL_NC = 8

    # 2026-09-25 又加。**光有尾部判据会误伤 dsh 的压缩请求** —— 实测：
    # dsh 的摘要器按设计回放的是「已遮蔽区域」= 最老那批消息（见
    # dsh-compaction-basic README「摘要机制」），永远碰不到行的尾部。于是
    # 每压缩一次就「认不出这段对话…开新会话…实发 140417 字」，40 秒里连发
    # 4 次；日志里 [309] 在 04:14-04:15 就是 续接/认不出 交替刷屏。
    # 实测三种形状：正常续接 重合199/占0.97/碰尾；压缩请求 重合98/占0.48/
    # 不碰尾；压缩请求带尾部几条 重合101/占0.49/碰尾。
    # 所以补一条**覆盖度**：重合占该行 30% 以上也算同一段对话 —— 压缩请求
    # 靠这条救回来。交叉验证（210 个组合）里 30% 这一档同样是 真续接 15/15、
    # 误判 0。
    RATIO_NC = 0.30

    # 换号后拿到的是**本账号自己的**旧会话，它只见过换出前那一棒的进度。
    # 光看重合度会认成它，于是这一棒又从头干一遍 —— 实测出现过 483 干到
    # 步骤1484、换号给 113 后从步骤1458 继续，倒退 26 步还重做了已验过的活。
    # 所以认亲之后还要看新鲜度：命中的那条明显比别的账号旧，就当认不出，
    # 开新会话 + 全量重发，让新账号拿到最新那一棒的完整历史。

    # 认亲之后过一道新鲜度闸：命中那条比「全表最新重合行」旧超过这个秒数，
    # 就当认不出。900s 是当初定的值，三个用例实测 PASS。
    STALE_SEC = 900.0

    # dsh 生成标题/摘要那类一次性小请求共用的窗口，用满这么多次再换一个
    UTIL_TAG = "__util__"
    UTIL_USES = 20


    # 2026-10-01 第471步（用户口径「分组要干净」）：**行上限 64 -> 128。**
    #
    # 64 是当初拍脑袋定的。实测每号真实会话数 49~77 条，64 刚好切在中间：
    #   afe69962cff3  76 条 -> 12 条被裁掉，其中 8 条是 nc>=20 的大行（最大 nc=152）
    #   999480675b35  68 条 -> 4 条被裁
    # 被裁的正是「更早、但指纹更全」的行 —— 而认亲要的就是这种行。
    # 裁掉它们等于把「这段对话以前接过」这件事忘掉，下一轮只能重发全文。
    #
    # 128 的余量：按实测最大的号（77 条）留近一倍，短期内不用再动；
    # 内存代价可忽略（128 条 x 几百个 hash，每号几十 KB）。
    # _load / remember 都取 [-self.limit:]，所以改这里一处就够。
    def __init__(self, limit=128, owner="", path=None):
        # limit 是表里最多留几条对话。留太少的话，老对话的记录会被挤掉，
        # 你回头去接它就又开一个新窗口 —— 所以宁可多留，一行几 KB 不值钱。
        self.limit = limit
        self.owner = owner or "?"
        self.path = pathlib.Path(path) if path else self.FILE
        self.lock = threading.Lock()
        self.rows = self._load()

    def _load(self):
        with self._FILE_LOCK:
            try:
                raw = json.loads(self.path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                return []
        rows = raw.get(self.owner) if isinstance(raw, dict) else None
        out = []
        for r in rows or []:
            if isinstance(r, dict) and r.get("session") and r.get("keys"):
                # nc 是 2026-09-25 加的。老行没有这个字段 → 存成 ()，
                # match() 见到空的 nc 一律不认（宁可重发一次全文，也不能接错）。
                _nc = r.get("nc")
                out.append({"keys": tuple(r["keys"]),
                            "nc": tuple(_nc) if isinstance(_nc, list) else (),
                            "session": r["session"],
                            "parent": r.get("parent"),
                            "uses": int(r.get("uses") or 0),
                            "at": float(r.get("at") or 0.0),
                            "tag": r.get("tag") or ""})
        return out[-self.limit:]

    def _save(self):
        """整个文件是 {账号: [记录…]}，只覆盖自己那一格，别踩别的账号。"""
        with self._FILE_LOCK:
            try:
                raw = json.loads(self.path.read_text(encoding="utf-8"))
                if not isinstance(raw, dict):
                    raw = {}
            except (OSError, ValueError):
                raw = {}
            raw[self.owner] = [{"keys": list(r["keys"]),
                                "nc": list(r.get("nc") or ()),
                                "session": r["session"],
                                "parent": r["parent"], "uses": r.get("uses", 0),
                                "at": r.get("at", 0.0),
                                "tag": r.get("tag", "")} for r in self.rows]
            try:
                self.path.write_text(json.dumps(raw, ensure_ascii=False),
                                     encoding="utf-8")
            except OSError:
                pass


    # ── 公共消息：所有窗口都一样的那几条，认亲时必须排除 ────────────────
    # 只收有证据的。这三类是从 sessions/*.zstd 原文里解出来、又回去对上
    # sessions.json 指纹的（见 C:/Users/Lenovo/Desktop/teste/_串窗口证据链.md）：
    #   · role == "system"        —— 系统提示（同模型下逐字相同）
    #   · "<system-reminder>"     —— AGENTS.md / 技能目录（实测 5/6 账号）
    #   · "Current runtime context" —— 运行时快照（实测 5/6 账号）
    # 刻意**没有**收 "## Referenced sessions"：它的指纹实测只出现在 1 个会话
    # 里（每窗口各一份），是判别性的，收掉只会削弱认亲。
    COMMON_PREFIX = (
        "<system-reminder>",
        "Current runtime context",
    )

    @staticmethod
    def _is_common(role, text):
        """这条消息是不是「所有窗口都一样」的公共消息。"""
        if role == "system":
            return True
        head = text[:64]
        for p in SessionCache.COMMON_PREFIX:
            if head.startswith(p):
                return True
        return False

    @staticmethod
    def _fp(role, text):
        """一条消息的指纹。role + 正文，不含位置 —— 见 fingerprint 的说明。"""
        h = hashlib.sha256()
        h.update(role.encode())
        h.update(b"\x01")
        h.update(text.encode())
        return h.hexdigest()[:32]

    @classmethod
    def with_nc(cls, keys, items):
        """把 (keys, items) 包成带判别性指纹 nc 的 Keys。

        nc = 去掉公共消息之后剩下的指纹。认亲只用 nc，去重仍用全量 keys ——
        公共消息必须留在 keys 里，否则每轮都会把 10 万字系统提示当新消息重发。
        """
        nc = []
        for m in items:
            role = m.get("role") or ""
            text = _text_of(m.get("content"))
            if not cls._is_common(role, text):
                nc.append(cls._fp(role, text))
        return Keys(keys, nc)

    @staticmethod
    def fingerprint(messages):
        """→ (keys, items)，只算非 assistant 的那些消息。

        刻意不把模型名算进去：思考/联网是每条消息自己带的开关，同一个 DeepSeek
        会话完全可以混着用。把模型名混进指纹的话，客户端一换模型全部 key 就变了，
        match() 什么都对不上 → 白开一个新会话、还要把几十万字上下文重传一遍。

        认亲不要用这里的 keys，用 with_nc() 包出来的 .nc —— 原因见 MIN_NC。
        """
        keys, items = [], []
        for m in messages:
            if (m.get("role") or "") == "assistant":
                continue
            keys.append(SessionCache._fp(m.get("role") or "",
                                         _text_of(m.get("content"))))
            items.append(m)
        return tuple(keys), items


    def _same_talk(self, want, rnc, stress=0.0, why_out=None):
        """这一行和 want 是不是**同一段对话** —— match() 和 remember() 共用一把尺。

        want 是本轮的判别性指纹（match 里已剔掉最后一条）。判据见 MIN_NC / TAIL_NC。
        remember() 也必须用这把尺：它原来比「全量 keys 重合 >= 2」，而 system 和
        AGENTS.md 提醒这两条公共消息在所有行里都有，于是每开一个新会话就会把
        该账号上**别的对话的行全删掉** —— 行没了下一轮就认不出，又开新会话，
        又删一行，这就是 churn 的来源（实测 dc2b17df 3070 keys 被 24 keys 的新行挤掉）。

        2026-10-01 第427步（用户口径：「认亲 还是不完美 要结合 是否空回复 是否限流
        及换号频率来做增量」）：**加一条运行时压力的旁路判据。**

        为什么要加：实测全量 11057 次请求，认不出 711 次（6.4%）。但这些
        失败并不是门槛太高 —— 统计「认不出」前最近一次记录的判别性重合度，
        中位数是 **24 条**（不是 1~2 条），而成功续接的中位是 36 条；
        重合度 <3（会被 MIN_NC 卡掉）的只占 **0.3%**。

        也就是说卡住的是**尾部判据**：TAIL_NC=8 要求重合的指纹必须落在对方
        nc 的最后 8 条里。dsh 每轮会重写/裁剪历史，尾部那 8 条经常整体换掉，
        于是「明明重合 24 条，却因为没碰到尾部」被判成认不出。

        代价不对称：认不出 -> 开新会话 -> 全量重发（实测 6 万~18 万字）。
        而这个号如果正在被限流 / 连着空回复 / 刚被轮换过来，重发全文
        更容易再撞一次空 —— 一次空就进冷却 + 换号，级联下去。
        所以压力大的时候，宁可**接上旧会话**（重合 24 条已经是强证据），
        也不要重发全文。

        stress 由调用方给，取值 0.0 ~ 1.0：
            0.0 = 健康（本号空闲、无冷却、无空回复）-> 判据完全不变
            >0  = 有压力，到 0.5 以上才启用旁路
        """
        if not rnc:
            return False
        n = len(want & set(rnc))
        if n < min(self.MIN_NC, len(rnc)):
            return False
        # 满足其一就算同一段对话：
        #   · 碰到尾部   —— 正常续接（你知道我最后在干什么）
        #   · 覆盖 >= 30% —— dsh 的压缩请求：它回放的是最老的已遮蔽区域，
        #                   碰不到尾部，但覆盖度很高（实测 0.48）
        #   · 压力旁路   —— 见上，只在 stress >= 0.5 且重合量够硬时生效
        if want & set(rnc[-self.TAIL_NC:]):
            if why_out is not None:
                why_out[:] = ["tail", n]
            return True
        if n / len(rnc) >= self.RATIO_NC:
            if why_out is not None:
                why_out[:] = ["ratio", n]
            return True
        if stress >= 0.5 and n >= self.STRESS_MIN_NC:
            # 2026-10-01 第432步：这条路径原来**不写任何日志**，于是
            # 「它到底有没有工作」只能靠猜。现在把命中原因带出去，
            # 由 Bridge 那侧记进 ds_bridge.log（SessionCache 没有 note()，
            # 2026-10-01 第423步踩过这个坑）。
            if why_out is not None:
                why_out[:] = ["stress", n]
            return True
        if why_out is not None:
            why_out[:] = ["", n]
        return False
    def match(self, keys, fresh=True, stress=0.0):
        """挑重合消息最多的那条会话；判别性指纹 nc 重合不到 MIN_NC 条就算认不出来。

        只比 nc（去掉公共消息后的指纹）。拿全量 keys 比会误配 —— 每个新窗口
        天然跟每条老会话重合 ≥2 条公共消息，恰好越过 MIN_OVERLAP，详见 MIN_NC。

        fresh=False 跳过新鲜度闸，返回「认出来的那行」—— plan() 拿它决定
        「续接旧会话 + 全文重发」还是「开新会话」。

        stress 透传给 _same_talk（2026-10-01 第427步，用户口径「认亲 还是不完美
        要结合 是否空回复 是否限流 及换号频率来做增量」）：本号压力大时放宽
        尾部判据，避免「认不出 -> 重发全文 -> 又空一次 -> 换号」的级联。
        """
        nc = getattr(keys, "nc", None)
        want = set(keys if nc is None else nc[:-1])
        with self.lock:
            ranked = []
            for row in self.rows:
                if row.get("tag"):
                    continue                    # 共用窗口不参与主对话的认亲
                rnc = row.get("nc") or ()
                if not rnc:
                    # 2026-09-25 之前的行没存 nc，没法验证重合的是不是公共消息
                    # —— 一律不认。代价是这些老会话重发一次全文；下一次
                    # remember() 就会给新行补上 nc。方向宁可重发也不接错。
                    continue
                # 判据见 _same_talk：门槛随行长浮动 + 必须碰到尾部。
                # 尾部判据是交叉验证里把误判从 90 压到 0 的那一条。
                _why = []
                if not self._same_talk(want, rnc, stress, _why):
                    continue
                ranked.append((len(want & set(rnc)), len(row["keys"]), row,
                               _why[0] if _why else "", _why[1] if len(_why) > 1 else 0))
            if not ranked:
                self.last_why = ("none", 0)
                return None
            ranked.sort(key=lambda t: (t[0], t[1]))
            hit = dict(ranked[-1][2])
            # 2026-10-01 第432步：把命中原因挂到返回值上，Bridge 侧记日志用。
            hit["_why"] = ranked[-1][3]
            hit["_why_n"] = ranked[-1][4]
        if not fresh:
            return hit
        # 认亲成功也要看新鲜度：命中那条是本账号的旧存档，别的号已经跑到前面去了
        ceil = self._freshness_ceiling(want)
        if ceil and ceil - float(hit.get("at") or 0.0) > self.STALE_SEC:
            return None
        return hit

    def _freshness_ceiling(self, want):
        """全表（含别的账号）里跟这段对话重合的行，最新的那个 at。

        必须读文件，不能只看 self.rows —— self.rows 只有本账号那几行，
        而这里要的正是「别的账号已经干到多新了」。
        """
        with self._FILE_LOCK:
            try:
                raw = json.loads(self.path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                return 0.0
        if not isinstance(raw, dict):
            return 0.0
        best = 0.0
        for rows in raw.values():
            for r in rows or []:
                if not isinstance(r, dict) or not r.get("keys"):
                    continue
                # 同上：新鲜度也要按判别性指纹算，否则公共消息会把「全表最新」
                # 抬到跟本次请求毫无关系的另一段对话上。
                if len(want & set(r.get("nc") or ())) < self.MIN_NC:
                    continue
                at = float(r.get("at") or 0.0)
                if at > best:
                    best = at
        return best


    def latest(self):
        """本账号时间最近的那条对话，认不出来时拿它兜底。

        按记录里的 at（写入时刻）挑，而不是按列表顺序猜 —— 那个号如果同时
        跑过别的对话，顺序靠不住，时间才靠得住。老记录没有 at 就当 0，
        这时退回列表顺序（越靠后越新）。
        """
        with self.lock:
            best, best_key = None, None
            for i, row in enumerate(self.rows):
                if row.get("tag") or not row.get("session"):
                    continue
                key = (float(row.get("at") or 0.0), i)
                if best_key is None or key > best_key:
                    best, best_key = row, key
            return dict(best) if best is not None else None


    def overlap(self, keys):
        """这批消息和本账号已有会话最多重合几条 —— 账号池拿它认亲。

        0 表示这个账号没见过这段对话。挑账号时必须先问这一句：DeepSeek 的
        session id 属于具体账号，把一段聊到一半的对话换个号发，等于换了个
        空白上下文。
        """
        # 同上：认账号也要剔掉最后一条 —— 否则「同一句开场白」会让每个号都
        # 回一个 ≥MIN_NC，池子就以为每个账号都见过这段对话。
        # 2026-09-25 再修：**剔掉本轮最后一条判别性消息**再比。
        #
        # 理由：新窗口的判别性指纹恰好只有「它自己刚说的那句话」一条。而用户
        # 习惯用同一句开场白（实测会打 `@轮询`），只要那几个字以前在同一个账号
        # 上发过，重合就是 1 = MIN_NC —— 新窗口立刻被认成老会话。实测：
        #   新窗口也打 @轮询 → 判别性重合 1 → 续接 SESSION_A（串）
        # 剔掉最后一条后，新窗口的 want 变成空集 → 重合 0 → 老老实实开新会话。
        #
        # 为什么不误伤真续接：续接时前面的历史消息都在 want 里，只少最后一条
        # （实测仍重合 3）。也不误伤短会话：单句会话的第二发实测仍重合 1。
        nc = getattr(keys, "nc", None)
        want = set(keys if nc is None else nc[:-1])
        with self.lock:
            best = 0
            for row in self.rows:
                if row.get("tag"):
                    continue
                # 2026-09-25：同上按 nc 算。用全量 keys 的话，每个账号都会因为
                # 公共消息回一个 ≥2，池子就以为「每个号都见过这段对话」。
                rnc = row.get("nc") or ()
                if not rnc:
                    continue
                n = len(want & set(rnc))
                if n < min(self.MIN_NC, len(rnc)):
                    continue
                if not (want & set(rnc[-self.TAIL_NC:])):
                    continue
                best = max(best, n)
            return best


    def remember(self, keys, session, parent):
        """一条对话只留一条记录，已发过的 key 只增不减。

        原来只按 session id 去重，于是同一个 dsh 对话每开一次新会话就多留一行
        （实测表里 11 行有 5 行首 key 相同）。`match()` 挑中哪条就发到哪个
        DeepSeek 会话去 —— 两个窗口各存一半历史，内容当然对不上。所以：
        同一个 session 的旧行合并（key 取并集），同一条对话分叉出去的旧行直接
        删掉。**不**把别的 session 的 key 算进这一行 —— 那些内容并没有发到
        这个会话里，认作已发就会漏上下文。
        """
        with self.lock:
            fresh = set(keys)
            # 2026-09-25：nc 跟 keys 一样只增不减地并进这一行，认亲才有料可比。
            nc_new = list(getattr(keys, "nc", None) or keys)
            # 2026-10-01（命中率 + 质量）：把「认不出那一刻」预登记的锚点指纹
            # 一并并进这一行。压缩会把历史重写成摘要，客户端接下来 1-2 轮发来的
            # 结构跟刚建的行可能仍重合不够 —— 预先并入这份指纹，下一轮就能直接
            # 续上，不用再全文重发一遍。信号来自「我刚认不出」这个**事实**，
            # 不做任何文案匹配（COMPACT_MARK 那套误判史见 L4120-4126）。
            # 判「同一条对话分叉」也要用剔掉最后一条之后的判别性集合，
            # 跟 match() 保持一致。
            #
            # 2026-10-01 第435步（用户报错：UnboundLocalError: seen_nc）：
            # **这四个赋值必须在 if _anc 之前。**
            # 原因：下面的 if _anc 分支要往 merged_nc / seen_nc 里并锚点指纹，
            # 而这四个原本排在它后面 —— 第434步搬迁锚点方法时顺序被打乱了。
            # 只要 _anc 非空（即上一轮走过「认不出」）就必报 UnboundLocalError，
            # 而这个异常路径**恰恰是压缩后重接那条最重要的路**。
            fresh_nc = set(nc_new[:-1])
            merged, seen = list(keys), set(keys)
            foreign = []   # 并进来的别 sid 指纹（增量时排除）
            merged_nc, seen_nc = list(nc_new), set(nc_new)
            try:
                _anc = self._take_anchor(keys)
            except Exception:              # noqa: BLE001
                _anc = None
            if _anc:
                for k in (_anc[0] or ()):
                    if k not in fresh:
                        fresh.add(k)
                for k in (_anc[1] or ()):
                    if k not in seen_nc:
                        seen_nc.add(k)
                        merged_nc.append(k)
            # 2026-10-01 第423步（用户口径「最主要的是轮询号交接是否正常 缓存命中率
            # 如何优化」）：**退化行（nc < MIN_NC）不许独立成行。**
            #
            # 病根：_same_talk() 要求 n >= min(MIN_NC=3, len(rnc))。新行本身
            # nc=1 时 n=1 < 3 -> 判 false -> 走 else 保留老行、新行也没并进去。
            # 结果是**两边都在表里、两边都认不上**：下一轮 match() 认不出 ->
            # 开新会话 -> 全量重发 -> 命中率 0；这一轮又 remember 一条新的退化行。
            # 实测 sessions.json：280 行里 37 行 nc<3（13.2%），今天 41 行里
            # 8 行（19.5%）；nc=0 有 15 行、nc=1 有 4 行、nc=2 有 18 行。
            # 活体证据（309 = 999480675b35）：10:57:21 nc=1/keys=2、10:53:01
            # nc=1/keys=2，日志侧同一分钟**17 秒内连续三次**「认不出这段对话…实发 18533 字」。
            #
            # 改法：nc 不够认亲时，**并进本号最新那条像样的行**（它是这段对话
            # 最近一次真实落点）。并进去之后，下一轮用那条行的几百条 nc 就能
            # 直接续上 —— 这正是「轮询交接」该有的样子。
            # 找不到可并的行（本号第一条会话）才老老实实独立成行，这是正常冷启动。
            _weak = len(set(nc_new)) < self.MIN_NC
            _host = None
            if _weak:
                _best = -1.0
                for _r in self.rows:
                    if _r.get("tag") or _r["session"] == session:
                        continue
                    if len(_r.get("nc") or ()) < self.MIN_NC:
                        continue
                    _at = float(_r.get("at") or 0.0)
                    if _at > _best:
                        _best, _host = _at, _r
            keep = []
            for r in self.rows:
                if r.get("tag"):
                    keep.append(r)
                elif _host is not None and r is _host:
                    # 退化行并进这里：keys / nc 取并集，at 刷新成现在。
                    #
                    # 2026-10-01 第441步（用户口径：「你缓存怎么给每个
                    # 窗口增量的」）：**并进来的 keys 必须标记成「别的 sid 的」。**
                    #
                    # 病：`_delta_items` 拿这一行的 keys 当「这个 sid 已经发过什么」，
                    # 但 `_host` 是**另一个 sid 的行** —— 它的 keys 并进来之后，
                    # 这个 sid 会误以为那些内容也发过，于是**扣多了**，
                    # 算出来的增量偏小 —— 该发的没发。
                    #
                    # 实测：309 的固定窗口 7d8c5bbf，桥记 keys=402，
                    # 而**上游窗口里只有 40 条消息**（直接读 history_messages 数的）。
                    # 差 10 倍。同理 310：桥记 284。
                    #
                    # 改法：并进来的 key 放进 `merged`（认亲还要用），
                    # 同时记进 `foreign` —— 计算增量时把它们排除。
                    # 这样认亲行为不变，但增量不再被别的 sid 污染。
                    for k in r["keys"]:
                        if k not in seen:
                            seen.add(k)
                            merged.append(k)
                            foreign.append(k)
                    for k in (r.get("nc") or ()):
                        if k not in seen_nc:
                            seen_nc.add(k)
                            merged_nc.append(k)
                    # 注意：SessionCache 上没有 note()（那是 Bridge 的），
                    # 这里不能记日志 —— 2026-10-01 单元测试实测会
                    # AttributeError 把 remember() 整个炸掉。
                elif r["session"] == session:
                    for k in r["keys"]:
                        if k not in seen:
                            seen.add(k)
                            merged.append(k)
                    for k in (r.get("nc") or ()):
                        if k not in seen_nc:
                            seen_nc.add(k)
                            merged_nc.append(k)
                elif self._same_talk(fresh_nc, r.get("nc") or ()):
                    # 2026-09-25 修：这里原来比的是**全量 keys 重合 >= 2**，而
                    # system / AGENTS.md 提醒两条公共消息在所有行里都有 ——
                    # 于是「新开一个会话」会把该账号上**别的对话的行全删掉**。
                    # 实测：dc2b17df（3070 keys，at 00:55）在 03:51:39 被一条
                    # 24 keys 的新行挤掉；03:50 刚匹配过的 ca2ea982、03:51
                    # 匹配过的 57927b71 也都不见了。行没了 → 下一轮认不出 →
                    # 又开新会话 → 又删一行：那串 churn 就是这么来的。
                    #
                    # 2026-10-01 再修（用户口径：命中率 + 质量）。
                    # 09-25 那次把「误删别人」治住了，但**同一段对话**的旧行
                    # 仍然被这条 pass 丢掉。实测 020 在 07:15 就是这么断的：
                    #   07:14:50  续接 931f1c0d（重合 27）  <- 表里有这个 18-key 行
                    #   07:15:28  认不出 → 开新会话 → remember(9eec1b1f, 3 keys)
                    #             → _same_talk 判「同一段对话」→ 931f1c0d 被 pass 掉
                    #   07:15:36  认不出（老行没了，无从比对）
                    #   07:15:49  才靠新行 686aec3b 重新接上
                    # 中间两轮只能全量重发（实发 87451 / 157722 字），
                    # 客户端那个「缓存命中率」就是被这两发打到接近 0 的。
                    #
                    # 改法：**不删，改成并进新行**（keys / nc 取并集）。
                    # 这才是函数开头那句「已发过的 key 只增不减」的真正含义 ——
                    # 原实现对「同 session」成立，对「同对话不同 session」恰恰相反。
                    #
                    # 不担心行膨胀：并进来的是同一段对话的历史指纹，本来就该留；
                    # 不同对话的行走 else 分支照旧保留。
                    for k in r["keys"]:
                        if k not in seen:
                            seen.add(k)
                            merged.append(k)
                    for k in (r.get("nc") or ()):
                        if k not in seen_nc:
                            seen_nc.add(k)
                            merged_nc.append(k)
                elif _weak and not (r.get("nc") or ()):
                    # 2026-10-01 第423步续（**保守清理**）：
                    # 只丢 nc 完全为空的行（认亲时 rnc 为空直接 continue，
                    # 它永远是死行，且不承载任何判别性信息）。
                    # **不**丢 nc=1/2 的行 —— 那可能是本号某段短对话的唯一记录，
                    # 丢掉它等于把「短对话也能续上」这条路堵死，得不偿失。
                    # 实测存量：nc=0 有 15 行，这些是纯孤儿。
                    pass        # 丢掉：不加入 keep
                else:
                    keep.append(r)
            keep.append({"keys": tuple(merged), "nc": tuple(merged_nc),
                         "foreign": tuple(foreign),
                         "session": session,
                         "parent": parent, "uses": 0,
                         "at": time.time(), "tag": ""})
            self.rows = keep[-self.limit:]
            self._save()


    # ===== 新锚点（2026-10-01 第434步）=====
    # 病：remember() 里调 self._take_anchor(keys)，而那个方法定义在
    # **Bridge** 类上 —— 跨类调用，永远抛 AttributeError。
    # 而调用处包在 try/except 里，于是**静默失效**：锚点从来没登记过，
    # 「压缩后那一两轮不用全量重发」这个收益一直拿不到。
    # 实测：调 _take_anchor 直接报
    #   SessionCache object has no attribute _take_anchor
    # 锚点本来就该归存行的地方（SessionCache），这里把它安家。
    def mark_anchor(self, keys):
        """把这一发的指纹标成「新锚点」，等下一次 remember 用掉。"""
        try:
            self._anchor_keys = list(keys)
            self._anchor_nc = list(getattr(keys, "nc", None) or keys)
        except Exception:            # noqa: BLE001
            self._anchor_keys = None
            self._anchor_nc = None

    def _take_anchor(self, keys):
        """取出待登记的锚点（同一轮只能被用掉一次）。"""
        _a = getattr(self, "_anchor_keys", None)
        if not _a:
            return None
        self._anchor_keys = None
        _n = getattr(self, "_anchor_nc", None)
        self._anchor_nc = None
        return (_a, _n)


    def rows_for_session(self, session, exclude_foreign=True):
        """按 sid 找那一行（固定组窗口要用它的 keys 算增量）。找不到返回 None。

        2026-10-01 第436步加。固定窗口走的是「不看指纹、直接用 sid」这条路，
        所以不需要认亲，但**需要知道这个 sid 上已经发过哪些消息** ——
        否则每轮都会把整段历史重发一遍，等于白建缓存。

        2026-10-01 第441步（用户口径：「你缓存怎么给每个窗口增量的」）：
        **exclude_foreign=True 时，剔掉「并进来的别 sid 指纹」。**

        病：`remember()` 会把同一段对话的旧行并进来，而那些旧行可能属于
        **另一个 sid**。并进来的 key 被当成「这个 sid 发过」后，
        `_delta_items` 会**扣多**，算出的增量偏小 —— 该发的没发。

        实测：309 的固定窗口 7d8c5bbf，桥记 keys=402，
        而上游窗口里只有 **40 条消息**（直接读 history_messages 数的）。差 10 倍。
        """
        s = str(session or "").strip()
        if not s:
            return None
        with self.lock:
            for r in self.rows:
                if str(r.get("session") or "") == s:
                    out = dict(r)
                    if exclude_foreign:
                        fo = set(r.get("foreign") or ())
                        if fo:
                            out["keys"] = tuple(
                                k for k in (r.get("keys") or ())
                                if k not in fo)
                    return out
        return None

    def util(self):
        """取那个共用窗口；没有或已用满就返回 None（让上层开个新的）。"""
        with self.lock:
            for r in self.rows:
                if r.get("tag") == self.UTIL_TAG \
                        and r.get("uses", 0) < self.UTIL_USES:
                    return dict(r)
            return None

    def used_util(self, session, parent):
        with self.lock:
            for r in self.rows:
                if r["session"] == session:
                    r["uses"] = r.get("uses", 0) + 1
                    r["parent"] = parent
                    r["tag"] = self.UTIL_TAG
                    self._save()
                    return
            self.rows = [r for r in self.rows
                         if r.get("tag") != self.UTIL_TAG or
                         r.get("uses", 0) < self.UTIL_USES]
            self.rows.append({"keys": (self.UTIL_TAG,), "session": session,
                              "parent": parent, "uses": 1,
                              "at": time.time(), "tag": self.UTIL_TAG})
            del self.rows[:-self.limit]
            self._save()


    def forget(self, session):
        """会话在服务端没了（比如被清空过），把它从表里踢掉。"""
        with self.lock:
            self.rows = [r for r in self.rows if r["session"] != session]
            self._save()




# ============================== 服务本体 ==============================

class Bridge:
    LOG_FILE = state_file("ds_bridge.log", "ds_bridge.log")


    def __init__(self, ds, min_interval=3.0, log=True, name="", slug=""):
        self.ds = ds
        self.min_interval = min_interval
        self.log = log
        # 账号身份：name 是 ds_auth.json 里那个人类看的名字，slug 是模型名里
        # 用来钉住账号的 ASCII 后缀（deepseek-reasoner@acct1）。
        self.name = name or (getattr(ds, "cfg", None) or {}).get("name") or "账号"
        self.slug = slug or "acct"
        self.enabled = True              # 界面上取消「入池」就临时不派活给它
        self.fails = 0
        self.last_error = ""
        # 每轮都重申的固定要求，GUI 的「常驻提醒」直接改这段；无界面跑的时候
        # 就用 CHUNK_REMINDER 这个出厂值。
        self.standing_note = CHUNK_REMINDER
        # 格式提醒：无条件每轮注入，专门放「报错过的格式规范」。GUI 里可编辑，
        # 空串 = 不发这段。与池/固定号无关，谁跑都带。
        self.format_note = ""
        # 附件说明（走附件时贴在提醒之后）。GUI 里可编辑，空串 = 不发这段。
        self.attach_note = ATTACH_NOTE
        self.attach_note_local = False   # 第93步：local 交接模式置真，附件说明去掉 上下文.txt
        self.sid = ""                  # 稳定窗口身份：dsh 的 sessionId，do_POST 每轮写入
        self.cache = SessionCache(owner=_owner_tag(ds))
        self.gate = threading.Lock()     # 单账号内串行，坚决不并发
        # 2026-10-01：在途请求的起始时刻。run()/continue_until_parsable()
        # 拿到 gate 就记一笔，status() 借此算出「这一发已经跑了多久」——
        # 挂死时 /health 会显示 inflight_secs 一路涨，是**可见的事实**。
        self._inflight_at = 0.0
        self.last_call = 0.0
        self.last_had_tool = False   # 上一轮正文是否带工具调用（有代码要执行）
        self.cool_until = 0.0            # 被限流后强制拉开间隔到这个时刻
        self._fp_at = 0.0                # 上次报「指纹去重」的时刻（同号限流用）
        self._empties = {}               # session → 连着空回复几次
        self.empty_streak = 0            # 本账号连着空回复几次，成功即清零
        self.empty_at = 0.0              # 最后一次空回复的时刻，让位窗口用它
        self.turns_here = 0              # 「真交互」条数：换人时清零；换号阈值、界面「成功轮数」都看它
        # 一次真实交互 = 1 条：重试/接续都在同一轮 run() 里，不额外涨；
        # 空回复 / 压缩 / 工具页不落在这个分支，本来就不占条数。
        self.turn_limit = 0.0            # 本账号单独的轮询条数；0 = 跟随全局
        # 2026-10-01 第429步：指回自己所属的 Pool（由 Pool 构造/reload 时回填）。
        # stress() 靠它问到真实的轮次上限；没有时为 None，退回账号自己那层。
        self.pool = None
        # 2026-10-01 第440步：已校验过标题的 sid。
        # 固定组窗口每轮都要用，但“标题对不对”只需查一次。
        # 2026-10-02 第484步：从 set 改成 **dict（sid -> 下次可校验的时间戳）**。
        # set 的语义是「校验过就永不重试」，但只有成功才 add ——
        # 失败路径什么都不记，于是每发请求都白打一次上游（见 plan() 里的注释）。
        self._gs_checked = {}
        # 2026-10-01 第440步：已校验过标题的 sid（避免每发都查一次）
        self.search = False              # 本账号是否开模型自带联网搜索；默认关
        # 重发全文时的字符预算：超了就只留尾部这么多字。0 = 不裁（老行为）。
        self.send_budget = 300000
        self.last_chars = 0              # 最近一轮客户端发来的上下文字符数
        self.peak_chars = 0              # 峰值，界面上显示，方便定上限
        self.last_reply = ""             # 最近一次非空正文；老号交不出交接时用它代写
        self.last_reply_at = 0.0         # 上面那次的时刻
        # 最近一次压缩检查点。客户端定期让模型做上下文压缩，产物是完整状态
        # 快照 —— 交接时它比 last_reply 有价值得多，优先用它。
        self.last_checkpoint = ""
        self.last_checkpoint_at = 0.0
        self.pending_checkpoint = ""   # 换号时生成的新鲜检查点，run() 里上传成附件
        self.cl_report_at = 0.0          # 最近一次按 context_length_exceeded 报给 dsh 的时刻
        # ---------- 压缩闸（2026-09-23） ----------
        # 症状：换号本就要重传一大发，dsh 到阈值又要压一次，压完再换号 ——
        # 换号和压缩互相点火。压缩烧掉大量上游配额，还每次重写消息列表，
        # 桥认亲的锚点跟着漂。用户口径：压过一次后先看下一轮是否正常回正文；
        #   正常回 -> 关闸，这条对话不再压缩上下文；
        #   压完仍频繁换号 -> 也关闸，不再压缩。
        # 最近一次空回复的判定："keep"=按太大处理（值得让 dsh 压一次）、
        # "dropped"=会话废了（压缩没用，别让 dsh 循环重试）、""=没空过
        self.last_empty = ""




    def note(self, *a, **extra):
        # 多账号并行时日志会交叉，每行都得带账号名，否则没法看出是谁在干活
        _msg = " ".join(str(x) for x in a)
        line = f"[{self.name}] " + _msg

        # 2026-09-22 加：被动观测。判断点本来就打一行 note，这里按前缀顺带
        # 落成事件 —— 监控端只读 _relay_events.jsonl，不用去 grep 日志。
        try:
            # 两边都 strip：note 的前缀自带两个空格缩进，只 strip 一边就永远不匹配
            # （2026-09-22 踩过：noreason 一次没落，日志里明明有那行）。
            _m = _msg.strip()
            for _pfx, _kind in NOTE_EVENTS:
                if _m.startswith(_pfx.strip()):
                    emit(_kind, slug=getattr(self, "slug", ""), msg=_m[:400], **extra)
                    break
        except Exception:
            pass
        stamp = time.strftime("[%H:%M:%S]")
        # pythonw 启动时没有控制台，sys.stdout 是 None，直接 print 会炸
        if self.log and sys.stdout is not None:
            print(stamp, line, flush=True)

        # 日志一直写文件：GUI 里没有控制台，出问题只能靠这个复盘
        try:
            with self.LOG_FILE.open("a", encoding="utf-8") as fh:
                fh.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {line}\n")
        except OSError:
            pass


    def wait_left(self):
        """现在派活给它还得先等几秒（最小间隔 + 限流冷却）。"""
        now = time.time()
        return max(0.0, self.min_interval - (now - self.last_call),
                   self.cool_until - now)

    def busy(self):
        return self.gate.locked()

    def affinity(self, keys):
        """这段对话在本账号这边最多认出几条 —— 0 就是没见过。"""
        return self.cache.overlap(keys)

    def status(self):
        """给界面用的一行状态。"""
        wait = self.wait_left()
        if not self.enabled:
            state = "已停用"
        elif self.busy():
            state = "工作中"
        elif wait > self.min_interval + 0.5:
            state = f"冷却 {wait:.0f}s"
        elif self.last_error:
            state = "上次出错"
        else:
            state = "空闲"
        return {"name": self.name, "slug": self.slug, "enabled": self.enabled,
                "state": state, "turns": self.turns_here, "fails": self.fails,
                "sessions": len(self.cache.rows), "wait": wait,
                "pace": (self.min_interval, self.COOLDOWN,
                         tuple(self.RATE_BACKOFF)),
                "limits": dict(self.limits),
                "last_chars": self.last_chars,
                "peak_chars": self.peak_chars,
                "empty_streak": self.empty_streak,
                "last_empty": self.last_empty,
                "turns_here": self.turns_here,
                "turn_limit": self.turn_limit,
                "error": self.last_error,
                # 2026-10-01（用户口径：「为何需要时间统计，还有别的方法」）：
                # 不去统计「正常请求该多久」，而是把**当前这一发已经跑了多久**
                # 直接报出来 —— 于是「卡住」是一个**可见的事实**，不是猜的。
                # 只有真在忙（gate 被占）时才报；空闲时恒为 0，老时间戳不会
                # 残留成假信息。
                "inflight_secs": (round(time.time() - self._inflight_at, 1)
                                  if (self.busy() and self._inflight_at) else 0.0)}


    def set_ds(self, ds):
        """换账号。会话表按账号分组，切过去就读那个账号自己那一格。"""
        with self.gate:
            self.ds = ds
            self.name = (getattr(ds, "cfg", None) or {}).get("name") or self.name
            self.cache = SessionCache(owner=_owner_tag(ds))




    UTIL_KEYS = (SessionCache.UTIL_TAG,)
    UTIL_MAX = 2500          # 标题/摘要那类一次性小请求的长度上限
    # 空回复时判断「是这一发太大」还是「会话废了」的门槛。
    # 定低了会误判：实测 3.6 万字符的请求照样能空，那是会话坏了，不是太大 ——
    # 而误判成「太大」就会让 dsh 一遍遍压缩重试，压到 3 万多还是空，死在原地。
    # 参照实测：11 万字符正常、24 万字符才开始偶发空回复。
    EMPTY_KEEP_CHARS = 100000

    # 空回复本身就是限流信号。dsh 收到 429 会退避重试，但它那套档位是按
    # 「上游明确报 429」设计的，对「上游静默返回空」来说来得太快 —— 重试进来
    # 立刻又打上游，还是空，于是连着空。所以桥接自己按连续空回复次数拉开冷却：
    # dsh 重试进来时 _pace() 会先等这一段，上游那边真正喘口气。
    EMPTY_COOLDOWN = (15.0, 30.0, 60.0, 120.0, 180.0)

    # 2026-09-24 加：**不在轮询池**的号，同一个会话连着空这么多次就判定会话已废，
    # 丢掉它、下一轮自动开新会话。轮询号不看这个（见空回复段那里的注释）。
    EMPTY_DROP_FIXED = 3


    # 每个模型的输入上限（字符）。Pool 会按界面上的设置覆盖它。
    # 0 = 不限制。
    limits = {}

    # dsh 只靠错误文案认「上下文超了」这件事：dsh-llm 里的 isContextWindowExceededError
    # 拿正则匹配 provider 的报错（`dsh-llm/lib/index.js:133-146`），认出来之后
    # compaction-basic 的 agent/request-error 钩子（`dsh-compaction-basic:804`）会
    # 先裁工具结果、再压缩历史、然后重试这一轮。所以这句话的措辞必须踩中那些正则，
    # 别随便改 —— 改了 dsh 就只会当普通错误显示，不会自己压缩。
    # 2026-09-22 整块删除：OVERSIZE_TEMPLATE 不再存在。
    # 它是「用英文报错指挥 dsh 去压缩上下文」的控制通道，副作用是压缩会重写消息列表，
    # 从而打断桥自己的 window_tag 和 SessionCache 认亲。现在由桥自己按 send_budget 裁。

    # 2026-09-22 整块删除：EMPTY_OVERFLOW_TEMPLATE 与 empty_overflow() 不再存在。
    # 它同样是「用英文报错指挥 dsh 压缩上下文」的控制通道。上游对太大一发静默回空时，
    # 现在按 empty_upstream(502) 报，由桥自己裁短下一发，不要求客户端压缩。

    # 上游限流造成的空回复用这句。措辞里必须带 429 / rate limit ——
    # dsh 的 pi-ai 适配器靠它认出 RATE_LIMIT（`dsh-llm-pi-ai:1294`），
    # 而 RATE_LIMIT 在默认可重试集合里
    # （`dsh-llm/lib/types/retry-policy.js:16`），dsh 会退避重试这一轮；
    # 反观 CONTEXT_WINDOW_EXCEEDED 会让它去裁工具结果 + 压缩历史，
    # 那个动作治不了限流，还会把上下文换成摘要，越压越傻。
    RATE_LIMIT_TEMPLATE = (
        "rate limit (429): the model returned an empty completion for this "
        "request ({chars} chars). "
        "上游判定发送过于频繁，静默返回了空内容。这一发并不大，属于频率问题、"
        "不是上下文超限 —— 请退避后重试这一轮，不要压缩上下文。")

    def rate_limited_msg(self, chars=None):
        """上游限流时给 dsh 的说法：当成 429，让它退避重试。"""
        return self.RATE_LIMIT_TEMPLATE.format(
            chars=self.last_chars if chars is None else chars)

    def empty_cooldown(self):
        """连着空第几次 → 这一轮之后要冷多久。越连越久，封顶在最后一档。"""
        n = max(1, int(self.empty_streak or 1))
        table = empty_policy(getattr(self, "slug", "")).get("cooldown")
        if not table:
            table = list(self.EMPTY_COOLDOWN)
        table = [float(x) for x in table]
        return table[min(n, len(table)) - 1]

    def input_chars(self, messages):
        """客户端这一轮发来的全部文本量。

        量的是 messages 全文而不是我们实际发上去的增量：DeepSeek 那边会话里
        累积的就是全文，而且要让 dsh 压缩的正是它自己那份上下文。
        """
        return sum(len(_text_of(m.get("content"))) for m in messages or [])

    # ---------- 压缩检测点（2026-09-21） ----------
    #
    # dsh 到上下文 80% 时会把整段对话重发一遍、末尾追加一条压缩指令，让模型吐
    # 一份 8 段式快照，再拿快照替换掉前面的历史（dsh-compaction-basic）。这条链
    # 上有三件事必须能拆开、而且都落到日志里：
    #   1. 到底为什么触发 —— dsh 自己的节奏，还是被我们回的超限错误逼出来的；
    #   2. 触发时这一发多大 —— 客户端全文，跟 dsh 的阈值差多少；
    #   3. 触发后怎么处理 —— 见 oversize()/run() 里对应的三处（放行、不占条数、
    #      产物当交接首选）。日志只加两种行：⟳ 压缩调用 与 ⟳ 压缩完成。

    @staticmethod
    def oversize(self, model, messages, tools=None):
        """超了就返回给 dsh 看的错误文案，没超返回空串。

        只拦「带工具的」请求，也就是 agent loop 那一类。**不能**拦没有工具的
        请求：dsh 的压缩本身就是一次无工具的 LLM 调用，它要把整段历史发过来
        让模型总结；把它也拦了的话，dsh 想压缩→被拦→压不动→这一轮直接失败，
        死锁在原地（实测：111616 字那轮就是这么挂的）。标题/摘要那些小请求
        同理，一律放过。
        """
        if not tools:
            return ""
        # 不在轮询序列里的号（enabled=False，只能靠 @slug 钉住选中）不受
        # 这条硬拦约束 —— 它本来就不参与轮换，上限是按轮询节奏定的。
        if not getattr(self, "enabled", True):
            return ""
        # 2026-09-24 改（用户口径）：**桥不再对压缩做任何特殊处理**。
        # 压缩完全交给 dsh 自己管（它有自己的 compaction-basic 和阈值）。
        # 原来这里先把「压缩调用」认出来放行、还挂了个压缩闸 —— 那套判据
        # 靠 COMPACT_MARK 字符串匹配，而桥自己的换号检查点指令**逐字照抄**
        # 了 dsh 的同一句话，于是自己发的指令下一轮被自己认成压缩调用，
        # 106 次「压缩调用」全是这么来的（compact_why 一律报未知），
        # 连带 compact_n 计数失真、压缩闸乱关。整块拆掉。
        # 保留的只是「超限就自己裁尾部」这条，不拦、不报、不干预。
        limit = int(self.limits.get(model) or 0)
        if limit <= 0:
            return ""
        chars = self.input_chars(messages)
        # 被拦下的那一轮也要记，不然界面上看不到「刚才那发到底多大」
        self.last_chars = chars
        self.peak_chars = max(self.peak_chars, chars)
        if chars <= limit:
            return ""
        # 2026-09-22 删控制通道（用户要求：压缩上下文上传，不换窗口）。
        # 原来这里回一句英文（OVERSIZE_TEMPLATE）让 dsh 去压缩上下文 —— 那是拿报错
        # 当控制通道。但它有个致命副作用：dsh 压缩会重写消息列表，而桥的 window_tag
        # 锚在「第一条 user 消息」上、SessionCache 锚在逐条消息的 sha256 上，压一次
        # 两样全断 —— 台账旧号变成「不存在的窗口」、认亲变成「认不出这段对话」。
        # 现在改成：桥自己按 send_budget 裁尾部（build_prompt 的 budget 已经在做），
        # 不拦、不报、不让 dsh 动手。
        self.note(f"✂ 客户端上下文 {chars} 字 > 上限 {limit} 字（{model}）"
                  f" —— 不再报给 dsh，改由桥自己按预算 "
                  f"{int(getattr(self, 'send_budget', 0) or 0)} 字裁尾部后上传")
        return ""


    def is_util(self, messages, tools=None):
        """这一发是不是 dsh 生成标题/摘要那种一次性小请求？

        判据必须和 plan() 里那个 util 分支**完全一致** —— do_POST 靠它决定
        要不要把换号交接取走。取早了，交接就落在一条根本不拼交接的请求上。

        2026-09-22 加。配合 do_POST 里的「只有真的会拼进 prompt 才取走」。
        """
        if tools:
            return False
        try:
            _k, items = SessionCache.fingerprint(messages)
        except Exception:
            return False
        return (len(items) <= 2
                and sum(len(_text_of(m.get("content"))) for m in items)
                <= self.UTIL_MAX)



    # 「交接单 + 小增量」时，增量部分的字符上限。换号接手首发用它压住体积：
    # 全文重发实测 22-25 万字，一空就被 90 秒让位窗口踢走，账号永远追不上。
    HANDOFF_BUDGET = 30000

    def _delta_items(self, keys, items, known):
        """切出「这个会话还没见过的那几条」，指纹撞车按位置兜。

        指纹只由 role+正文算、不含位置，正文相同的 key 必然相同。原来只要 key
        在 known 里就丢，同一批里正文重复时真消息会被一起误杀（实测 94 次）。
        现在：本批唯一的 key 在 known 里就丢；本批重复的只丢靠前的、留最后一条。
        返回 (delta, c, drop, resc, fb)。
        """
        c, last = {}, {}
        for k in keys:
            c[k] = c.get(k, 0) + 1
        for i, k in enumerate(keys):
            last[k] = i
        delta, drop, resc = [], 0, 0
        for i, (k, m) in enumerate(zip(keys, items)):
            if k in known and (c[k] == 1 or last[k] != i):
                drop += 1
                continue
            if k in known:
                resc += 1
            delta.append(m)
        fb = not delta
        if fb:
            delta = items[-1:]
        elif items and items[-1] not in delta:
            resc += 1
            delta.append(items[-1])
        return delta, c, drop, resc, fb

    def _tail_items(self, items, budget):
        """从最后一条往前取，累计字符不超过 budget。最新一条永远保留。"""
        out, total = [], 0
        for m in reversed(items):
            n = len(_text_of(m.get("content")))
            if out and total + n > budget:
                break
            out.append(m)
            total += n
        out.reverse()
        return out

    def attach_note_for_send(self):
        """本轮真正要贴的附件说明。

        2026-10-03（用户口径「光附件提示就3个 我醉了」）：**收成一份。**
        原来这里按 local 模式把「上下文.txt」那一行剪掉，是为了对付
        「local 不上传上下文、说明里却还列着它」—— 那个矛盾现在由
        attach_note_for(names) 解决：它按**实际挂了哪几份**生成，
        没挂的压根不会出现。所以这里不再需要预先剪一份文案。
        三份派生常量与配置键保留（防回归），但统一走同一份。
        """
        _an = getattr(self, "attach_note", None)
        if _an is None:
            _an = pget("attach_note", ATTACH_NOTE)
        return _an


    def _downgrade_to_handoff(self, keys, items, known, attach, tools, why,
                              full_chars=0):
        """体积超闸门时，把「全量重发」降级成「交接单 + 小增量」。

        2026-09-30 加（用户口径：桥动不动就掉）。

        背景：_ask() 里的 Safety Gate（HARD_LIMIT_CHARS）只会**拒绝**，
        抛 RequestTooLargeError。可客户端不会自己裁短重试 —— 它把这个
        异常原样甩到界面上（「[桥接]上游失败：RequestTooLargeError…」
        「这一轮没有任何工具被执行」），用户只能手动重发。表现就是
        「桥动不动就掉」，实际是**拒绝服务**。

        plan() 里本来就有现成的「交接单 + 小增量」路径（换号接手那条），
        它发出去的量稳定在 HANDOFF_BUDGET(30000) 字级别，远低于闸门。
        所以这里不再让大包一路走到 _ask() 去撞墙，而是**在计划阶段就改走
        小增量** —— 用户无感，活照干。

        返回 (prompt, sel) 或 None（连小增量都压不下去，交给 _ask() 拒）。
        """
        # 2026-10-01 修（3 小时测试抓到的真 bug）：
        # **必须先用 full_chars 判断「真的超限了吗」**，再决定降不降级。
        #
        # 原来这里只查 lim<=0 就往下走 —— 等于**无条件降级**。于是：
        #   日志实测「全文 9729 字：超过上限 100000 字 —— 自动改走小增量」
        #   9729 根本没有超过 100000，却照样被降级。
        # 后果（这才是要命的）：
        #   本该全文重发的一发（比如 9729 字的正常续接）被硬切成
        #   「交接单 + 小增量」，接手方**只拿到 3 条增量**，看不到完整历史。
        #   3 小时测试里这种误降级发生了 66 次（downgrade=66），
        #   其中大量是 9700~11000 字这种**根本没超限**的小请求。
        #
        # 判据：full_chars > 0 且 full_chars <= lim -> 没超限，直接返回 None，
        # 让调用方走原来的全量路径。full_chars == 0（老调用方没传）时保持
        # 原来的保守行为，避免影响别处。
        lim = int(getattr(self, "HARD_LIMIT_CHARS", 0) or 0)
        if lim <= 0:
            return None
        if full_chars and full_chars <= lim:
            return None                      # 没超限：不降级，别动
        try:
            _d, _kc, _kd, _kr, _kf = self._delta_items(keys, items, known)
            self._gap_full = list(_d)        # 交给 _hand_prompt 算缺口
            sel = self._tail_items(_d, self.HANDOFF_BUDGET)
        except Exception:
            return None
        if not sel:
            return None
        prompt = self._hand_prompt(keys, items, sel, attach=attach, tools=tools)
        if len(prompt) >= lim:
            # 小增量本身还是太大（罕见：单条消息就超大）—— 逐步砍尾巴。
            # 注意 len(sel)==1 时 1//2 == 0，直接 break 会把超限的 prompt
            # 原样返回（等于降级失败却没报失败）。所以候选里必须含 0，
            # 由 _p2 的长度判断是否真的压下去了。
            _fits = None
            for _cut in (len(sel) // 2, len(sel) // 4):
                if _cut <= 0 or _cut >= len(sel):
                    continue
                _s = sel[-_cut:]
                _p2 = self._hand_prompt(keys, items, _s, attach=attach, tools=tools)
                if len(_p2) < lim:
                    _fits = (_s, _p2)
                    break
            if _fits is None:
                # 砍到 0 条都还超限 = 超的是固定开销（tail_of/工具表/附件说明），
                # 不是消息体。这时候再降级也没意义，交回 _ask() 的闸门去拒。
                _empty = self._hand_prompt(keys, items, [], attach=attach, tools=tools)
                if len(_empty) >= lim:
                    return None
                # 固定开销没超，但按条砍压不下去（单条就超大）——
                # 空增量还能过的话就用它，至少把这一轮接上，别整个拒掉。
                _fits = ([], _empty)
            sel, prompt = _fits
        self.note(f"  ⤵ {why}：超过上限 {lim} 字 —— "
                  f"自动改走「交接单 + 小增量」（{len(sel)} 条 / "
                  f"{len(prompt)} 字），不拒绝、不换号、不冷却")
        return prompt, sel

    def gap_index_note(self, full_items, sent_items):
        """被裁掉的那一段「缺口索引」—— 零丢失方案的核心。

        2026-09-30（用户口径「轮询不可能天天丢数据」）。

        背景：_tail_items(delta, HANDOFF_BUDGET) 只取尾部，实测一次换号
        丢 39%~67% 的 delta（19/55、4/12、14/30 条）。丢掉的那些**没有真的
        消失** —— 全量正文一直在 _relay_replies.jsonl 里（已 30MB+）。
        问题是接手方不知道自己缺了东西，于是凭猜测补，越补越偏。

        这里不传数据、只传**指针**：告诉接手方「缺哪一段、去哪读、怎么读」。
        正文体积几乎不涨（几百字），但信息不再丢失。
        """
        try:
            if not full_items:
                return ""
            _full = list(full_items)
            # sent_items=None 表示「调用方已经裁过、但没回传条数」——稳态那条路
            # 就是这样（只挂 _gap_full，不传 sel）。
            #
            # 2026-09-30 修：这时**不能当成「一条都没发」**。第一版这么写，
            # 结果缺口声明说「只传了最近 0 条」「前面 86 条没有传给你」——
            # 而实际上前面 14 条已经发出去了，**声明本身在撒谎**。
            # 交接文本一旦不可信，接手方要么白读一遍存档，要么从此不信缺口段。
            # 所以：拿不到 sent 就只报总量 + 检索方法，不编造「传了几条」。
            _unknown = sent_items is None
            _sent = list(sent_items) if not _unknown else []
            _n_drop = len(_full) - len(_sent) if not _unknown else 0
            if not _unknown and _n_drop <= 0:
                return ""
            # 送出的是尾部，所以丢的是前面 _n_drop 条。
            # 拿不到 sent 时无法精确算「丢了多少」——这时报**总量**（保守：
            # 说多了顶多让接手方多查一次，说少了会让他以为拿全了）。
            _drop = _full[:_n_drop] if not _unknown else _full
            _c = sum(len(_text_of(m.get('content'))) for m in _drop)
            _from = self.slug or '?'
            _t0 = _t1 = ''
            # 时间窗：存档里按 slug 找最近这批，给个可读的区间
            try:
                _rows = archive_scan(slug=_from, limit=max(80, _n_drop * 3),
                                     with_text=False)
                if _rows:
                    _t0 = _rows[0].get('ts') or ''
                    _t1 = _rows[-1].get('ts') or ''
            except Exception:
                pass
            # 2026-10-01 第479步：**存档路径也要按组。**
            # 原来写死 REPLY_FILE（桥根那份），而这个号自己的回复存在
            # <组目录>/_sys/_relay_replies.jsonl 里 —— 给错路径等于让人白翻。
            _fp = str(grp_path("replies", _from) or REPLY_FILE)
            if _unknown:
                # 2026-10-01 第479步（用户口径「去看看测试任务继续窗口怎么样了」）：
                # **稳态这条路原来在撒谎，并因此把窗口推进了死循环。**
                #
                # 病：稳态（最常走的）不传 sent_items，_unknown=True，
                # 于是上面 _drop = _full 把**全部增量**当成丢失，_c 就是总量。
                # 实测后果（日志 + 窗口思考反复念）：
                #   "有交接缺口提示，说上一棒增量被裁掉约15004字"
                #   "约 30004 字被裁掉"  "约20003字被裁掉"
                # 窗口每轮被告知「你有几万字没拿到」-> 每轮去读存档 ->
                # 存档几万行读不完 -> 下一轮缺口还在 -> **无限空转**。
                # 实测 483 在 23:36~23:38 连续几十轮全是 pwsh 读文件、
                # 零真结论（每轮都记「本轮正文是纯工具调用…保留上一次真结论」）。
                #
                # 而且它跟真实缺口差一个数量级：同类日志有「18 条中未传 3 条」
                # 也有「37 条中未传 35 条」—— 这个数本来就不可信。
                #
                # 改法：**拿不到 sent 就老实说拿不到，不编数字。**
                # 只报「共有 N 条」+ 检索方法，不报「丢了多少字」。
                # 零丢失承诺不变（存档一直在），但不再每轮喊一个假缺口。
                _head_line = (
                    f"这一轮上一棒的增量共有 {len(_full)} 条。"
                    f"**哪些已传、哪些没传，桥这一发没能精确统计** ——"
                    f"所以这里**不报缺口数字**（报了也是猜的）。")
            else:
                _head_line = (
                    f"这一轮换号时，上一棒共有 {len(_full)} 条增量，"
                    f"因体积上限只传了**最近 {len(_sent)} 条**；"
                    f"**前面 {_n_drop} 条（约 {_c} 字）没有传给你。**")
            _out = [
                "【交接缺口 —— 必须知道】",
                _head_line,
                "",
                ("增量本身**没有丢失**，全文一直在本地存档里。"
                 "**只有当你确实发现少了某一段时**，才按下面方法去读；"
                 "没发现缺就别读 —— 每轮去翻几万行存档是白烧 token。"),
                f"  文件：{_fp}",
                f"  检索：按 slug=\"{_from}\" 过滤；一次 turn 一行 JSON，",
                "        字段 t(时间戳) / ts(时分秒) / k(类型) / slug / sid / text(全文)",
            ]
            if _t0:
                _out.append(f"  该号最近这批的时间范围：{_t0} ~ {_t1}")
            _out += [
                "",
                "**不要凭猜测补全缺失内容。**读不到就以台账和交接单为准。",
            ]
            return chr(10).join(_out)
        except Exception:
            return ""

    def _hand_prompt(self, keys, items, sel, attach=False, tools=None):
        """「交接单 + 小增量」那发的小 prompt。交接单由 _wh() 贴在最前面。"""
        parts = [render_turn(m) for m in sel]
        # 2026-09-30：被 _tail_items 裁掉的那段，在这里补一份**缺口索引**。
        # 只传指针不传数据，几百字换「零丢失 + 接手方知道自己缺什么」。
        # 2026-10-02 删：**不再每轮贴「交接缺口」段。**
        #
        # 理由（用户口径：「桥要干净」「删」）：
        #   原来这里 parts.insert(0, gap_index_note(...)) 把一段 300 字的
        #   缺口声明**插在 prompt 最前面**，而它自己说的全是废话：
        #     「没能精确统计」「不报缺口数字（报了也是猜的）」
        #     「增量没有丢失」「只有确实发现少了某一段时才去读」
        #   即：不报数字、说没丢、又叫人别读 —— 纯噪声，还把真正的任务挤到后面。
        #
        # 实测后果：窗口连续几十轮「读台账找任务」，占 55%% 的轮次；
        # 它自己反复说「用户给了提示，但没有明确的任务」。
        #
        # gap_index_note() 函数本身**保留**（换号真丢了一大段时还能手工用），
        # 只断掉每轮自动插入这个调用点。
        # parts = [render_turn(m) for m in sel]  上面那行保留
        parts += tail_of(keys, items, self.sid, self.standing_note, attach,
                         self.attach_note_for_send(), tools,
                         format_note=getattr(self, "format_note", ""),
                         peer_root=getattr(self, "_peer_root", ""),
                         slug=getattr(self, "slug", ""), messages=sel)
        return "\n\n".join(p for p in parts if p.strip())

    def stress(self):
        """本号当前的「压力」0.0 ~ 1.0 —— 认亲放宽的输入（2026-10-01 第427步）。

        用户口径：「认亲 还是不完美 要结合 是否空回复 是否限流 及换号频率来做增量」。
        这里把三个信号合成一个数，全部来自桥手上的**实时状态**，不新增采集：

          1. 限流冷却  —— cool_until 还没到点（正在被上游罚）
          2. 空回复    —— empty_streak 连着空了几次
          3. 轮次占比  —— turns_here / turn_limit，这个号在本组快到轮换点了

        为什么用「或」而不是「与」：三个都出现才放宽，等于永远不触发
        （实测三者同时发生的窗口很窄）。任何一个够强就给压力。

        返回值刻意是连续量而不是布尔：调用方按 >= 0.5 启用旁路，
        以后要调松紧只改一处。
        """
        s = 0.0
        try:
            now = time.time()
            # 1) 正在冷却 = 上游刚罚过，重发全文极可能再罚一次
            left = float(getattr(self, "cool_until", 0.0) or 0.0) - now
            if left > 0:
                cd = float(getattr(self, "COOLDOWN", 45.0) or 45.0) or 45.0
                # 冷却剩得越多，压力越大；封顶 1.0
                s = max(s, min(1.0, left / cd + 0.5))
            # 2) 连续空回复 = 这个号现在吐不出东西
            es = int(getattr(self, "empty_streak", 0) or 0)
            if es >= 1:
                s = max(s, min(1.0, 0.5 + 0.25 * es))
            # 3) 轮次快用满 = 马上就要被换走，此时重发全文多半白费
            #
            # 2026-10-01 第429步（自查发现的**死代码**）：原来这里读的是
            # self.turn_limit —— 那是 **Bridge 自己的**那一层（默认 0.0，
            # 见 __init__），而真实生效的上限在 Pool._limit_of() 里，
            # 是「账号 > 组 > 池子全局」**三级回退**的结果。
            # 实测：本机 6 个号的 turn_limit 全是 0.0（/health 输出），
            # 于是 tl > 0 恒假 —— **这一项从来没生效过**，
            # 压力旁路因此一次都没被触发（日志里 stress=0 全程）。
            #
            # 现在改成问池子要真实上限。拿不到（没有 self.pool，比如单元测试
            # 直接 new 一个 Bridge）就退回账号自己那层 —— 保持原行为，不炸。
            tl = 0.0
            _pool = getattr(self, "pool", None)
            if _pool is not None:
                try:
                    tl = float(_pool._limit_of(self) or 0.0)
                except Exception:            # noqa: BLE001
                    tl = 0.0
            if tl <= 0:
                tl = float(getattr(self, "turn_limit", 0.0) or 0.0)
            th = float(getattr(self, "turns_here", 0) or 0)
            if tl > 0 and th > 0:
                r = th / tl
                if r >= 0.8:
                    s = max(s, min(1.0, 0.5 + 0.5 * (r - 0.8) / 0.2))
            # 4) **刚被轮换过来** = 用户第427步原话里的「换号频率」，
            #    到 2026-10-01 第466步才补上 —— 前三项实测全废：
            #      · 空回复  实测 4563 轮里 0 次      -> 永假
            #      · 轮次占比 换号那刻 turns_here 归零 -> 恰好在最需要放宽时=0
            #      · 限流冷却 全史仅 69 次            -> 覆盖 1.5% 的轮次
            #    **而真正的换号原因 81% 是「条数用满(10/10)」**（switch 事件 414/510）。
            #
            #    这条链子是：条数用满 -> _handoff 把 turns_here 清零 -> 换到新号 ->
            #    新号没见过这段对话 -> 尾部判据卡死（重合中位 24 条，但碰不到
            #    对方 nc 末尾 8 条）-> 认不出 -> 开新窗 -> **全量重发 19~30 万字**
            #    （实测样本：113 在 90 秒内连开三窗，分别 306682/198687/197100 字）。
            #    -> 新窗活 2~5 轮 -> 又用满 -> 再换。每 10 轮白烧 20 万字。
            #
            #    **所以「刚接手」本身就是压力**：这一发要是认不出，代价是全文重发，
            #    而这个号刚被轮换过来、上下文最空，重发全文最容易再撞一次空。
            #    跟第427步注释里写的取舍完全一致（代价不对称，宁可接上旧的）。
            #
            #    判据用**接手时间戳**而不是 turns_here —— turns_here 在换号时被
            #    清零，拿它判必然漏。_handoff() 里已经记了 _accepted_at，直接用。
            #    接手时间戳在 Pool.swap_pending 里：_handoff() 写的是
            #        swap_pending[接手方 slug] = (老号, time.time(), rr_since)
            #    注意是 **Pool** 上的字典、按 slug 存 —— Bridge.stress() 里
            #    self 是 Bridge，得先拿到 pool。（我第一版写成 self._accepted_at，
            #    那个字段根本不存在 -> 读恒 0 -> 又是一段死代码，自查时抓掉。）
            _acc = 0.0
            _pool2 = getattr(self, "pool", None)
            if _pool2 is not None:
                _sp = getattr(_pool2, "swap_pending", None) or {}
                _cell = _sp.get(getattr(self, "slug", ""))
                if _cell:
                    try:
                        _acc = float(_cell[1] or 0.0)
                    except (TypeError, IndexError, ValueError):
                        _acc = 0.0
            if _acc > 0:
                _age = now - _acc
                _win = 180.0        # 接手后 3 分钟内算「还在适应期」
                if 0 <= _age < _win:
                    # 越刚接手压力越大：0 秒 -> 1.0，180 秒 -> 0.5（刚好够触发旁路）
                    s = max(s, 0.5 + 0.5 * (1.0 - _age / _win))
        except Exception:            # noqa: BLE001
            return 0.0
        return max(0.0, min(1.0, s))

    def plan(self, model, messages, tools, attach=False):
        """决定这次是接着老会话发一轮，还是开新会话重发全文。

        返回 (keys, session, parent, prompt, images)。keys 是这轮的消息指纹，
        请求成功后拿它去 remember()。
        """
        keys, items = SessionCache.fingerprint(messages)
        # 2026-10-01 第427步：本号当前压力（限流/空回复/轮次占比），
        # 认亲时用它决定要不要放宽尾部判据。健康时恒为 0.0，行为不变。
        _st = self.stress()
        # 2026-09-27 第406步（用户口径「主要提示有硬编目录」）：记下这一轮请求
        # 来自哪台机器。tail_of 拿它把提示词里桥本机的硬编根前缀换成对端的，
        # 别台电脑接入时才不会照抄 A 机路径。认不出（本机自用）就是空串，不换。
        self._peer_root = _evid_root_from_messages(messages)[0] or ""
        # 2026-09-25：再算一份判别性指纹 nc 包进 keys。认亲只看 nc —— 用全量
        # keys 的话，新窗口靠 system + AGENTS.md 两条公共消息就能越过 MIN_OVERLAP。
        keys = SessionCache.with_nc(keys, items)
        # 走附件时 inline 只保留最近这么多字的消息（用户口径：三个大块都进附件、
        # inline 永远很短，上游输入上限再也顶不到）。两个分支都要用，所以在入口算一次。
        _keep = int(empty_policy(getattr(self, "slug", "")).get(
            "txt_attach_keep_chars", 30000) or 30000)
        # 换号交接：上一棒吐的正文，贴在这次 prompt 最前面。
        # 只有换号那一轮有值（do_POST 里设的）。
        #
        # 2026-09-21 修：**不在这里清空**。util 分支（dsh 生成标题/摘要那种小请求）
        # 会路过这里但不用交接，若开头就清空，交接就被一个小请求白吃掉，
        # 真正接手的那一轮反而拿不到。改成谁用了谁清。
        hand = getattr(self, "handoff_note", "") or ""

        def _wh(p):
            # 2026-10-02 删：**不再把「交接缺口」段插到 prompt 最前面。**
            #
            # 这才是每轮真正在跑的那条路（稳态主干，_hand_prompt 只管换号）。
            # 原来这里 _txt = gap_index_note(...) 后 `p = _txt + 空行 + p`，
            # 于是每一发的开头都是那段 300 字废话：
            #   「没能精确统计」「不报缺口数字（报了也是猜的）」
            #   「增量没有丢失」「确实发现少了某一段时才去读」
            # 不报数字 + 说没丢 + 叫人别读 = 纯噪声，还把任务挤到后面。
            #
            # 实测（用户口径「一直循环读醉了」）：窗口 55%% 的轮次在
            # 「读台账找任务」，它自己反复说「没有明确的任务」。
            #
            # gap_index_note() 函数保留，只断掉两个自动插入点。
            # 2026-10-02：**进度要求每轮贴在 prompt 最前面。**
            #
            # 为什么不放 pool_note：实测 12 轮，那条要求落在 prompt 的
            # 82%~96% 位置（前面是客户端发来的几万字历史），模型一条都没报。
            # 位置决定注意力 —— 所以跟交接单一样贴最前面。
            #
            # pool_note 其余规矩仍留在末尾〔格式提醒〕里，不动那条规矩
            #（用户 2026-09-27：「格式提醒放在最后 不管什么时候都需要发」）。
            # 2026-10-02：进度要求已并回 pool_note 第 21 条（按条数走，不另开一段）。
            # 2026-10-02：**交接正文也要过根目录改写。**
            #
            # 通用问题（不是某一个案例）：`note` / `attach_note` / `format_note`
            # 三条走 tail_of 时都过了 retarget_roots，唯独**交接**没有 ——
            # 而交接恰恰是唯一一段**从历史里来的**文本，它最可能带着
            # 别的机器/别的时期的根。
            #
            # 实测（剪辑组 _relay_prompts.jsonl，19:59 那份含交接的 prompt）:
            #   交接段 6000 字里有 9 处绝对路径，其中一处是 `F:/工具/a.html`。
            #   而本机没有 F 盘 —— 下一棒照着它找，只会得到「路径不存在」。
            # 这正是「下一棒的空白」：它站在本机，手里却是一份别处的坐标。
            #
            # 判据不针对任何具体路径：**凡是进 prompt 的历史文本，都按
            # 本轮的根改写一遍**。跟 4155 那三条一个道理、同一套机制，
            # 不再另造一套。认不出对端根时 retarget_roots 原样返回，
            # 本机自己用不受影响。
            _h = hand
            try:
                _pr = getattr(self, "_peer_root", "") or ""
                if _h and _pr:
                    _h = retarget_roots(_h, _pr)
            except BaseException:        # noqa: BLE001
                _h = hand
            # 2026-10-03（用户口径：「要让他认为就是他干的活」）：
            # **快照在这里编进 prompt，不再事后硬拼。**
            #
            # 实测：pending_checkpoint 只在「发送那一刻」（11035）被读，
            # 而 plan() 里一处都没有 —— prompt 组装时看不到快照，只能事后拼，
            # 而写入(11691)与清空(11186)常在同一发里前后脚：
            #   收到快照 102 次，只内联成功 10 次，丢 92 次。
            #
            # _wh() 是 plan() 里「往 prompt 最前塞内容」的唯一出口，
            # 交接单走的就是它。快照接在交接单**之前** ——
            # 快照是「我干到哪了」，最贴近当前位置，该最先被看到。
            # 只读不写：清空仍由发送成功那条路（11186）负责，谁用了谁清。
            _snap = ""
            try:
                _cp = getattr(self, "pending_checkpoint", "") or ""
                if _cp:
                    _snap = snapshot_block(_cp)
            except BaseException:        # noqa: BLE001
                _snap = ""
            _head = chr(10) + chr(10)
            _parts = [x for x in (_snap, _h) if x]
            _body = _head.join(_parts)
            return (_body + _head + p) if _body else p

        if not tools and len(items) <= 2 and sum(
                len(_text_of(m.get("content"))) for m in items) <= self.UTIL_MAX:
            # dsh 生成对话标题、摘要那类一次性小请求。它跟主对话没有半点关系，
            # 每来一条就开一个新窗口的话，会话列表里会全是「继续 xxx」的碎片。
            # 统一塞进一个共用窗口，用满 UTIL_USES 次再换一个。
            row = self.cache.util() or {}
            return (self.UTIL_KEYS, row.get("session"), row.get("parent"),
                    build_prompt(messages, tools), images_in(messages))

        # ===== 固定组窗口（2026-10-01 第436步，用户设计）=====
        # 用户口径：「以后就走哪个 id 就走哪个 id 的组的窗口 除非换组」。
        #
        # 位置刻意放在 util 分支**之后**、指纹认亲**之前**：
        #   · 放 util 之后 —— 标题/摘要那种小请求不该动固定窗口；
        #   · 放认亲之前 —— 固定窗口的语义就是「不看指纹，直接用」。
        #
        # 2026-10-03（用户口径「**最主要的就不需要有开关**，把这个开关功能去掉，
        # 也不需要配置文件，本来就是这套流程」）：
        # **开关和 _empty_policy.json 一起删掉，这段无条件执行。**
        #
        # 为什么删：它本来不是"可选优化"，是**这套桥的窗口身份机制**本身 ——
        # 名字是身份、id 只是解析结果。做成开关的代价是：
        #   · _empty_policy.json 一丢（删工作区/换机/误删），桥静默回落到
        #     指纹认亲，而**没有任何提示**。实测就是这样丢的（回收站里那份
        #     写着 fixed_group_window: true，文件没了之后桥"忘了"用户开过）。
        #   · 回落之后的表现是：认不出 -> 开新会话 -> 上下文爆 -> 更认不出，
        #     日志里 [309] 出现 256 次「新会话」。
        #   · 一个"默认为假"的开关，会把整套设计悄悄关掉，且看不出来。
        #
        # 所以判据只看「有没有锚定名」，不再看任何策略文件。
        #
        # 2026-10-03（用户口径「**不认亲不看组名 只看自己 id 名**」）：
        # **锚定名 = 号自己的 id，不再用组名。**
        #
        # 为什么：窗口身份本来就该按"这个号自己的工作窗口"认，不该按"它跟谁
        # 一组"认。用组名当锚定名的两个毛病：
        #   · 没分组的号（309/779/113/310）锚定名是空 -> 整段跳过 -> 退回
        #     指纹认亲 -> 认不出就开新会话 -> 上下文爆 -> 更认不出（死循环）。
        #   · 一个号跳组（或组改名）就等于换窗口，缓存全废、重烧两轮。
        # 而号 id 是不变的、且天然隔离 —— 113 的 token 只能看到 113 的窗口，
        # 它物理上去不了别号的窗口，所以 id 名就是最稳的身份。
        #
        # 组名不再参与锚定：组只决定"轮转到谁"，不决定"用哪个窗口"。
        # 2026-10-03（用户口径「我固定号访问就是固定 id 访问，组就是组访问，
        # 他们不搭嘎」）：**锚定名取 resolve 记下的 anchor，不是 slug。**
        #
        # anchor 由 resolve 按「这一发是怎么进来的」设：
        #   @组名 -> 组名（组窗口）    @号名 -> 号 id（这个号自己的窗口）
        # 取 slug 会把分组语义丢掉 —— 分组发言时 slug 是组内轮到的那个号名，
        # 于是桥去建一个以号名命名的新窗口，真正的组窗口被晾在一边
        # （实测 04:02:33「新建「020」/020」，而「智普清言」窗口还在）。
        # fallback 到 slug：直接构造 Bridge 的场合（单测/工具）没有 anchor。
        _gname = str(getattr(self, "anchor", "") or
                     getattr(self, "slug", "") or "").strip()
        if _gname:
            # 2026-10-01 第445步（用户口径「最重要的是按窗口名定位不是按那些
            # 死id定位」）：**名字是身份，id 只是它的一次解析结果。**
            #
            # 之前把本地登记（_group_sessions.json 里的 sid）当成了身份，
            # 于是上游一删窗口，本地就成了一堆死 id，桥拿着它们反复发、
            # 反复吃静默空（实测 sessions.json 13 行**全是**已删窗口，
            # 每轮 7 秒空转到把闸门占满）。id 会失效，名字不会。
            #
            # 所以改成**先按名字解析，登记表只做加速**：
            #   · 登记里那个 id 仍在上游存在 -> 直接用（省一次 list_sessions）
            #   · 不在或没登记 -> 按名字找；找到就用并回写
            #   · 名字也找不到 -> 交给下面的新建分支
            # 这样任何「死 id」最多影响一发，名字一解析就自愈。
            _fsid = group_session_of(_gname, getattr(self, "slug", ""))
            _name_hit = ""
            try:
                _name_hit = group_window_by_name(
                    self.ds, _gname, note=self.note)
            except BaseException:        # noqa: BLE001
                _name_hit = ""
            if _name_hit:
                if _name_hit != _fsid:
                    # 名字解析出来的才是真的；本地登记过期就纠正它
                    if _fsid:
                        self.note(f"  ⇢ 本地登记的 {_fsid[:8]} 已不是「{_gname}」"
                                  f" 的活窗口，改用按名字找到的 {_name_hit[:8]}")
                    group_session_set(_gname, getattr(self, "slug", ""),
                                      _name_hit)
                    # 2026-10-01 第447步：换了窗口 = 水位作废，账本归零重数。
                    # 不归零的话，新窗口（从第 1 号开始）会顶着老窗口的高水位，
                    # 缺口算成 0，该补的一步都不补。
                    try:
                        ledger_reset(_gname, getattr(self, "slug", ""),
                                     sid=_name_hit)
                    except BaseException:    # noqa: BLE001
                        pass
                _fsid = _name_hit
            elif _fsid:
                # 名字找不到但这个 id 还在登记里：**不要信它。**
                # 上游没有叫这个名字的窗口，说明登记是死的（窗口被删/
                # 被改名/别处写入）。清掉登记，让下面的新建分支接管。
                self.note(f"  ⊘ 登记里的 {_fsid[:8]} 在上游没有对应的"
                          f"「{_gname}」窗口，判为死登记，丢弃")
                try:
                    group_session_set(_gname, getattr(self, "slug", ""), "")
                except BaseException:    # noqa: BLE001
                    pass
                _fsid = ""
            if _fsid:
                # 2026-10-01 第440步（用户口径：「如果建的窗口名字一样
                # 则跳过 不一样在改」）：这里顺手校验一次标题。
                #
                # **节流**：同一个 sid 只校验一次（记在内存）。
                # 不节流的话每一发都要多一次 list_sessions 调用，
                # 那是在拖慢正常请求 —— 不值得。
                # 校验失败不记入（下次还会试）；成功校验过的才记。
                # 2026-10-02 第484步（用户口径「监控台一直断还有桥怎么解决 我需要根治」）：
                # **失败也要记账，否则这是一个紧循环。**
                #
                # 原实现：except: pass -> 什么都没记 -> 下一次请求又调一遍
                # list_sessions（上游 HTTP，timeout=30）。上游一慢/一拒，
                # 每个请求都白等一次；请求再因客户端超时重发而翻倍。
                #
                # 实测佐证：日志里「固定窗口标题是…」**0 次** ——
                # 说明这段从来没成功走完过，每次都从 except 出去了。
                #
                # 改法：不管成功失败都记时间戳，_gs_checked 从 set 变 dict。
                # 失败的重试间隔给 10 分钟 —— 标题校验本来就不急，
                # 而它现在挡在**每发请求**的路上。
                _now = time.time()
                _last = self._gs_checked.get(_fsid, 0.0)
                if (_now - _last) >= 600.0:
                    try:
                        _rr, _ = self.ds.list_sessions(count=100)
                        _m0 = [r for r in _rr if str(r.get("id")) == str(_fsid)]
                        _tt = str(_m0[0].get("title") or "") if _m0 else ""
                        if _tt and _tt != _gname:
                            self.ds.rename_session(_fsid, _gname)
                            self.note("  ⇢ 固定窗口标题是「" + _tt
                                      + "」，不是组名，已改成「"
                                      + _gname + "」")
                        # 成功（哪怕标题本来就对）：记一个很久以后再校验的哨兵值，
                        # 等价于永久跳过。
                        self._gs_checked[_fsid] = _now + 10 ** 9
                    except Exception:        # noqa: BLE001
                        # **失败也记！** 否则下一发请求立刻又打一次上游。
                        self._gs_checked[_fsid] = _now
                self.note(f"  ⇢ 固定组窗口：组「{_gname}」的 {self.slug} "
                          f"-> {_fsid[:8]}（不看指纹，直接用）")
                _known = set()
                try:
                    _kr = self.cache.rows_for_session(_fsid)
                    if _kr:
                        _known = set(_kr.get("keys") or ())
                except Exception:            # noqa: BLE001
                    _known = set()
                _d, _kc, _kd, _kr2, _kf = self._delta_items(keys, items, _known)
                self._gap_full = list(_d)
                _sel = self._tail_items(_d, self.HANDOFF_BUDGET)
                parts = [render_turn(m) for m in _sel]
                parts += tail_of(keys, items, _fsid, self.standing_note, attach,
                                 self.attach_note_for_send(), tools,
                                 format_note=getattr(self, "format_note", ""),
                                 peer_root=getattr(self, "_peer_root", ""),
                                 slug=getattr(self, "slug", ""), messages=_sel,
                                 tools_full=False)
                prompt = chr(10).join(p for p in parts if p.strip())
                self.handoff_note = ""
                self.attach_note_local = False
                return (keys, _fsid, None, _wh(prompt), images_in(_sel))
            # 没有登记 -> 走下面的正常认亲；认到了由 remember 登记，
            # 认不到则新建并登记（见 _ensure_group_window）。

        row = self.cache.match(keys, stress=_st)
        if row:
            # 2026-09-25 加。这条路径以前**完全静默** —— 整个 ds_bridge.log 里
            # 83 条续接记录全是「太旧」分支，新鲜命中的一条都没有，所以误配
            # 在日志里根本看不见。现在两个数都打：判别性重合（作数的那个）
            # 和裸重合（含公共消息，虚高）。裸重合突然掉到个位数 = 认亲可疑。
            _nc_hit = len(set(keys.nc) & set(row.get("nc") or ()))
            _raw_hit = len(set(keys) & set(row["keys"]))
            self.note(f"  续接会话 {str(row['session'])[:8]}"
                      f"（判别性重合 {_nc_hit} 条 / 裸重合 {_raw_hit} 条）")
            # 2026-10-01 第432步：**压力旁路命中要单独记一行。**
            # 这条路径原先不写日志，导致「它有没有工作过」无法回答
            # （2026-10-01 自查时发现 stress 全程=0、日志 0 条，
            #  只能靠构造场景证明它会放宽）。现在只要它真的接管了认亲，
            # 日志里就有据可查：命中的是尾部判据还是压力旁路、重合几条。
            if str(row.get("_why") or "") == "stress":
                self.note(f"  ⚑ 压力旁路接管认亲：本号压力 {_st:.2f}"
                          f"（≥0.5 启用），重合 {row.get('_why_n')} 条"
                          f"（≥{SessionCache.STRESS_MIN_NC} 条才放行），"
                          f"未碰尾部 {SessionCache.TAIL_NC} 条 -> 不重发全文")
            # 只发这个会话还没见过的那几条。用集合而不是「前缀之后的部分」，
            # dsh 压缩上下文把前面重写成摘要之后照样能算对。
            #
            # 2026-09-21 修：切的位置必须回到 messages 里按**下标**找，不能拿
            # items 去过滤 —— items 是 fingerprint() 滤掉 assistant 之后的列表，
            # 拿它当源，助手回复（结论、判断、发现了什么）永远发不出去。
            # 表现：换号后新模型只看到用户消息和工具结果，看不到上一棒的结论，
            # 于是把已经做完的活又做一遍（实测五个账号停在五个不同步骤）。
            # 认亲仍然只认非 assistant 的指纹，sessions.json 格式不变。
            known = set(row["keys"])
            # 只发非 assistant 的增量。助手回复不塞进来 —— 换号时另有一份
            # 专门的【交接】（见 handoff_dump），那份是提炼过的结论；把原始
            # 回复也塞进来只会让接手方自己去 10 万字里捞重点（实测 delta
            # 会从 46K 涨到 107K）。
            #
            # 2026-09-21 修指纹撞车：指纹只由 role+正文算，不含位置，正文相同
            # 的 key 必然相同。原来只要 key 在 known 里就丢，于是同一批里正文
            # 重复、且该正文已发过时，几条会被**一起**丢掉 —— 真消息被误杀
            # （实测 94 次）。现在按位置分：
            #   本批唯一的 key → 在 known 里就丢（正常去重，行为不变）
            #   本批重复的 key → 只丢靠前的，留最后一条（最可能是新的那条）
            # 代价：撞车组会多发 1 条已发过的内容，最多每组 1 条，且内容重复
            # 本身无害；换来的是不再静默吃掉新消息。
            # 切增量 + 指纹撞车兜底都收进 _delta_items，换号接手那条路也用它。
            delta, _c, _drop, _resc, _fb = self._delta_items(keys, items, known)
            _dupk = [k for k, n in _c.items() if n > 1]
            _lostk = [k for k in _dupk if k in known]
            if _lostk:
                # 稳态下「同一段工具输出重复出现」会让这里每轮都成立（实测
                # 113 号每 69 秒一条）。真出事只有两种情况：走了兜底（delta
                # 全空），或最新一条没发出去。这两种一律报；其余按 FP_QUIET
                # 限流，免得把事件流和看板时间线淹掉。
                _newest_sent = bool(items) and items[-1] in delta
                _alert = _fb or not _newest_sent
                _t = time.time()
                if _alert or (_t - self._fp_at) > self.FP_QUIET:
                    self._fp_at = _t
                    _who = {}
                    for _k, _m in zip(keys, items):
                        if _k in _lostk and _k not in _who:
                            _who[_k] = ((_m.get('role') or '?'),
                                        _text_of(_m.get('content'))[:40])
                    _fbz = '是' if _fb else '否'
                    _what = ' | '.join(
                        f'{r}:{t!r}' for r, t in list(_who.values())[:3])
                    self.note(
                        f'  ⟳ 指纹去重：本批 {len(keys)} 条中 {len(_dupk)} 组正文重复，'
                        f'其中 {len(_lostk)} 组已发过 → 去重 {_drop} 条'
                        f'（发出 {len(delta)} 条，兜底={_fbz}）'
                        f' 留后 {_resc} 条 | {_what}')
            # 2026-09-30 加（用户口径：继续优化 / 桥动不动就掉）：
            # **稳态这条路原来完全没有体积控制。**
            # 上一轮只给「认不出、开新会话」和「续接旧会话：太旧」两条**偶发**
            # 分支加了降级，忘了这条**每轮都走**的主干（实测 0 处 send_budget、
            # 0 处 _tail_items）。于是 23:21:42 [483] 续接会话 103854 字直接
            # 撞上 _ask() 的 100000 闸门被拒 —— 界面红字、这一轮没有任何工具
            # 被执行。偶发路径修好了，主干照样掉。
            #
            # 这里不能照搬「降级成交接单+小增量」：稳态的 delta 是**这一轮真正
            # 的新内容**（用户刚说的话 + 新工具结果），砍掉就等于丢新消息。
            # 所以按预算裁**尾部**（_tail_items 保证最新一条永远保留），
            # 超出的**老** delta 转成缺口索引 —— 不丢，只是不塞进这一发。
            _bud = int(getattr(self, "send_budget", 0) or 0)
            _lim = int(getattr(self, "HARD_LIMIT_CHARS", 0) or 0)
            # 预算要留出固定开销（tail_of 的工具表/台账/格式提醒约 6k 字），
            # 否则 message 部分刚好用满预算 + 固定开销就超闸门。
            _head = 8000 if (_bud or _lim) else 0
            if _lim > 0:
                _head = max(_head, int(_lim * 0.08))
            _dl_bud = (_bud - _head) if _bud > 0 else 0
            if _dl_bud <= 0 < _lim:
                _dl_bud = max(1000, _lim - _head)
            if _dl_bud > 0 and delta:
                _dl_full = list(delta)
                _trimmed = self._tail_items(delta, _dl_bud)
                if len(_trimmed) < len(_dl_full):
                    self._gap_full = _dl_full          # 缺口索引用
                    self.note(f"  ⇣ 稳态增量超预算：{len(_dl_full)} 条里只发"
                              f"最近 {len(_trimmed)} 条（预算 {_dl_bud} 字），"
                              f"其余转缺口索引，不丢")
                    delta = _trimmed
            parts = [render_turn(m) for m in delta]
            # 2026-09-24 改：尾部统一交给 tail_of —— 以前这里自己拼一遍，
            # 于是附件说明在这一路（**最常走的一路**）也缺席，模型收到无名附件。
            # 发出去的就是界面上那一段，不再额外拼写死的文字 —— 否则同一条规则
            # 会出现两遍（写死的一段 + 常驻提醒里同样意思的一条）。
            # 2026-10-01 第426步：**这一路是"续接同一上游会话"，发精简工具表。**
            # 上游会话里已经有过完整定义，这里的清单只负责"提醒能调什么"。
            # 实测省 3976 字/发（工具表 5004 -> 1028）。这是最常走的一路
            # （上面注释自己写了「最常走的一路」），所以收益也最大。
            parts += tail_of(keys, items, self.sid, self.standing_note, attach,
                         self.attach_note_for_send(), tools,
                         format_note=getattr(self, "format_note", ""),
                         peer_root=getattr(self, "_peer_root", ""),
                         slug=getattr(self, "slug", ""), messages=messages,
                         tools_full=False)
            prompt = "\n\n".join(p for p in parts if p.strip())
            self.handoff_note = ""
            self.attach_note_local = False          # 真用掉了才清
            return (keys, row["session"], row["parent"],
                    _wh(prompt), images_in(delta))

        # 认亲成功但被新鲜度闸判「旧」：这条旧会话其实还活着（本地缓存都在）。
        # 不弃它 —— 接着用同一个 session、把全文重发进去：
        #   1) 保住它自己的推理链（连续性 = 智商）；
        #   2) 全文里带上了别的号的 assistant 结论（不再重做活）。
        # 代价：这一发会大一点（旧会话历史 + 全文），但白捡连续性。
        # 2026-09-22 改。
        row = self.cache.match(keys, fresh=False, stress=_st)
        if row:
            # 2026-09-22 方案1：手上有交接单时只发「交接单 + 小增量」，不再全文
            # 重发。全文重发实测 22-25 万字，一空就被 90 秒让位窗口踢走，dsh
            # 压缩的收益落到下一个号头上 —— 这个号永远追不上（779 实测停在
            # 步骤285、全局347，滞后 62 步，56 分钟没成功过一次）。
            # 没交接单只能全文重发：接手方缺的是别的号的结论，那份只有交接单
            # 或者完整 transcript 里有。
            if hand.strip():
                _d, _kc, _kd, _kr, _kf = self._delta_items(
                    keys, items, set(row["keys"]))
                self._gap_full = list(_d)        # 缺口索引用
                sel = self._tail_items(_d, self.HANDOFF_BUDGET)
                prompt = self._hand_prompt(keys, items, sel, attach=attach, tools=tools)
                self.note(f"  续接旧会话 {row['session'][:8]}：太旧，但有交接单 —— "
                          f"只发交接单 + 小增量（{len(sel)}/{len(_d)} 条 / "
                          f"{len(prompt)} 字），不全文重发")
                self.handoff_note = ""
                self.attach_note_local = False
                return (keys, row["session"], row["parent"],
                        _wh(prompt), images_in(sel))
            _bud = int(getattr(self, "send_budget", 0) or 0)
            _keep = int(empty_policy(getattr(self, "slug", "")).get(
                "txt_attach_keep_chars", 30000) or 30000)
            _p = build_prompt(messages, tools, self.standing_note, budget=_bud, attach=attach, attach_note=self.attach_note_for_send(),
                             keep=_keep, sid=self.sid, peer_root=getattr(self, "_peer_root", ""),
                             slug=getattr(self, "slug", ""))
            self.note(f"  续接旧会话 {row['session'][:8]}：太旧，全文重发补上别的号的进展"
                      + (f"（按预算 {_bud} 字裁过，实发 {len(_p)} 字）" if _bud else ""))
            # 2026-09-30：全量超闸门就别送到 _ask() 去撞墙，先自己降级成小增量。
            _dg = self._downgrade_to_handoff(
                keys, items, set(row["keys"]), attach, tools,
                f"续接旧会话 {row['session'][:8]} 全文 {len(_p)} 字",
                full_chars=len(_p))
            if _dg:
                _p, _sel = _dg
                self.handoff_note = ""
                self.attach_note_local = False
                return (keys, row["session"], row["parent"],
                        _wh(_p), images_in(_sel))
            self.handoff_note = ""
            self.attach_note_local = False              # 真用掉了才清
            return (keys, row["session"], row["parent"],
                    _wh(_p), images_in(messages))

        # 真认不出（本账号没见过这段对话）→ 开新会话，**不**借最近那条。
        # 借了就等于把这段对话整段倒进别人的上下文里（实测出过 299644 字那种），
        # 表现是答非所问、接着别人的活往下干。开新会话只是重发一次全文，值。
        if hand.strip():
            # 同理：有交接单就只发交接单 + 小增量，不把整段 transcript 灌进新会话。
            _d, _kc, _kd, _kr, _kf = self._delta_items(keys, items, set())
            self._gap_full = list(_d)            # 缺口索引用
            sel = self._tail_items(_d, self.HANDOFF_BUDGET)
            prompt = self._hand_prompt(keys, items, sel, attach=attach, tools=tools)
            self.note(f"  认不出这段对话，{self.name} 开新会话，但有交接单 —— "
                      f"只发交接单 + 小增量（{len(sel)} 条 / {len(prompt)} 字）")
            self.cache.mark_anchor(keys)          # 见 _mark_new_anchor 注释
            self.handoff_note = ""
            self.attach_note_local = False
            return (keys, None, None, _wh(prompt), images_in(sel))
        _bud = int(getattr(self, "send_budget", 0) or 0)
        _p = build_prompt(messages, tools, self.standing_note, budget=_bud, attach=attach, attach_note=self.attach_note_for_send(),
                             keep=_keep, sid=self.sid, peer_root=getattr(self, "_peer_root", ""),
                             slug=getattr(self, "slug", ""))
        self.note(f"  认不出这段对话，{self.name} 开新会话（不借别人的）"
                  + (f"（按预算 {_bud} 字裁过，实发 {len(_p)} 字）" if _bud else ""))
        self.cache.mark_anchor(keys)              # 见 _mark_new_anchor 注释
        # 2026-09-30：这一路是「不借别人的、开新会话」，全量最大（实测 179182 字），
        # 也正是 22:50:38 被 Safety Gate 拒掉那一发走的路。超闸门就降级成小增量。
        _dg = self._downgrade_to_handoff(
            keys, items, set(), attach, tools,
            f"认不出、开新会话，全文 {len(_p)} 字",
            full_chars=len(_p))
        if _dg:
            _p, _sel = _dg
            self.handoff_note = ""
            self.attach_note_local = False
            return (keys, None, None, _wh(_p), images_in(_sel))
        self.handoff_note = ""
        self.attach_note_local = False              # 真用掉了才清
        return (keys, None, None,
                _wh(_p), images_in(messages))


    def _upload_images(self, images):

        """图片存临时文件再走 DeepSeek 的附件上传，拿回 file_ids。"""
        tmp = pathlib.Path(tempfile.mkdtemp(prefix="ds_bridge_img_"))
        ids = []
        try:
            for mime, raw, name in images:
                p = tmp / name
                p.write_bytes(raw)
                ids.append(self.ds.upload(str(p), mime))
            self.ds.wait_files(ids)
            self.note(f"  图片 {len(ids)} 张已上传并解析完")
            return ids
        finally:
            for p in tmp.glob("*"):
                p.unlink(missing_ok=True)
            tmp.rmdir()

    def _upload_attach(self, label, text):
        """把一段文本传成附件，返回 file id（空文本返回 ""）。

        label 是 上下文 / 工具表 / 台账 —— 只用于日志和文件名。

        2026-09-22 加（用户口径：先体检、再过门、最后才生成 txt 上传；不降级）。
        实测：1.2M 字 / 3.5MB 的 txt 上传 1 秒、服务端解析 3 秒，埋在正中间的密钥
        被原样召回；三份同时挂也能用，多跳任务与全内联无差。详见 _dbg/three-txt-test。

        缓存键带 slug：**附件 id 是按帐号归属的**，换号之后那份在新号上不存在，
        必须重新上传。

        不做降级：上游本来就支持附件；真到用不了的地步，裁剪那份上下文也是残的、
        救不了这一轮，所以让错误直接暴露（由 run() 的兜底报给客户端）。
        """
        text = text or ""
        if not text.strip():
            return ""
        h = hashlib.sha1(text.encode("utf-8")).hexdigest()
        key = (str(getattr(self, "slug", "")), label, h)
        hit = _ATTACH_CACHE.get(key)
        if hit:
            self.note(f"  {label} {len(text)} 字命中附件缓存 -> {hit}")
            return hit
        d = STATE / "attach"
        d.mkdir(parents=True, exist_ok=True)
        f = d / (label + "-" + h[:16] + ".txt")
        f.write_text(text, encoding="utf-8")
        t0 = time.time()
        fid = self.ds.upload(str(f))
        self.ds.wait_files([fid], timeout=600)
        self.note(f"  {label} {len(text)} 字 -> 附件 {fid}"
                  f"（上传+解析 {time.time() - t0:.0f}s）")
        if len(_ATTACH_CACHE) > 32:
            _ATTACH_CACHE.clear()
        _ATTACH_CACHE[key] = fid
        return fid

    def _pace(self):
        """只等限流冷却。

        2026-09-24 改（用户口径）：**请求间隔不在这儿管了** ——
        它改由 ds_api.pace_gate() 统一管，口径也变了：
          · **不分账号**：上游看的是同一个用户，间隔按全局算；
          · 从「回完再歇 min_interval」改成「相邻两发**请求发出**之间至少隔 min_interval」；
          · 上一发耗时已经 ≥ 间隔，这一发**立即放行**，不再叠加。
        这里只剩限流冷却（cool_until）—— 那个永远等。
        """
        wait = self.cool_until - time.time()
        if wait > 0:
            self.note(f"  限流冷却中，等 {wait:.0f}s 再发")
            time.sleep(wait)

    COOLDOWN = 45.0        # 被限流后，之后的请求至少再拉开这么久
    RATE_BACKOFF = (20.0, 60.0)   # 同一轮里退避重试的等待秒数
    FP_QUIET = 300.0       # 「指纹去重」告警的最小间隔；稳态下别每轮刷屏
    # Phase A / 2026-09-30：桥侧发送硬上限（字符）。任何请求超过它直接拒发，
    # 不给上游一次「静默回空」的机会。0 = 不限。
    # 依据实测：11 万字符开始偶发空回复，24 万必空；用户建议 6 万/4.5 万/8 万三档。
    HARD_LIMIT_CHARS = 100000
    # 2026-09-30：可观测计数 —— /health 透出，用来核实闸门在真实流量里是否在工作。
    GATE_TRIPS = 0
    GATE_LAST_CHARS = 0
    # 2026-09-30：空回复 / 换号交接链的运行态计数，同样 /health 透出。
    # 空回复段有两条互斥判决（keep=这一发太大，保留会话让 dsh 压；
    # dropped=按频繁限流保留会话），交接段有一次本地代写兜底。
    # 这三个数一直是 0 说明链路没被触发；一直涨说明上游/会话确实在出问题 ——
    # 不用翻日志就能分清「闸门拦住了」和「空回复在发生」。
    EMPTY_KEEP = 0
    EMPTY_DROPPED = 0
    HANDOFF_LOCAL = 0

    def _ask(self, **kw):
        """打上游一次。撞上限流就退避重试，不是限流就直接抛出去。

        Phase A / 2026-09-30 加 Safety Gate：**唯一发送收口**。任何请求在真正
        打 HTTP 之前先过这里 —— prompt 超过 HARD_LIMIT_CHARS 直接拒绝发送，
        抛 RequestTooLargeError，不做缩包。这是为了堵死「一发太大 → 上游静默
        回空 → 桥误判限流 → 换号后新号也回空 → 全组冷却」这条炸桥链。
        """
        _p = kw.get("prompt") or ""
        _lim = int(getattr(self, "HARD_LIMIT_CHARS", 0) or 0)
        if _lim > 0 and len(_p) > _lim:
            Bridge.GATE_TRIPS += 1
            Bridge.GATE_LAST_CHARS = len(_p)
            # 2026-09-30 加：拒绝时**说清是哪一种超限**。
            #
            # 实测发现一个 5 万字宽的死区（2026-09-30 23:33 [483] 卡死）：
            #   txt_attach_chars = 150000  <- 客户端上下文超过它才走附件模式
            #   HARD_LIMIT_CHARS = 100000  <- 超过它就拒绝
            # 于是客户端 10 万~15 万之间：闸门拒绝，而附件模式**根本不会触发**，
            # 整段历史只能内联 —— 太大发不出、又不够小去走附件，卡死。
            # 这一档拒绝时如果不点明，下次还得从头查一遍。
            _why = ""
            try:
                _pol = empty_policy(getattr(self, "slug", ""))
                _tc = _num(_pol, "txt_attach_chars", 150000)
                if bool(_pol.get("txt_attach", True)) and _tc > _lim:
                    _why = (f"｜提示：客户端上下文在 {_lim}~{int(_tc)} 字之间会"
                            f"卡死（闸门拒绝但附件模式不触发）。"
                            f"把 txt_attach_chars 降到 {_lim} 以下。")
            except Exception:          # noqa: BLE001
                pass
            self.note(f"  ✗ Safety Gate：请求 {len(_p)} 字 > 硬上限 {_lim} 字，"
                      f"拒绝发送（不缩包、不换号、不重试）{_why}")
            raise RequestTooLargeError(
                f"请求 {len(_p)} 字超过桥的硬上限 {_lim} 字，已在 _ask() "
                f"闸门拦下，未发送。请开新窗口或减少上下文。{_why}")
        # 2026-10-01 第444步：台账底座 —— 发出的 prompt 连同上游回的步号落盘。
        # 位置就在闸门之后、真正打 HTTP 之前：**过了闸门的一定发得出去**，
        # 被闸门拦下的不该进台账（它没到上游，步号永远不会存在）。
        # 把关的东西全包进 BaseException：存档绝不能把这一发带塌。
        _lg_slug = str(getattr(self, "slug", "") or "")
        _lg_sid = str(kw.get("session") or "")
        _lg_model = str(getattr(self, "model", "") or "")
        _lg_kind = "attach" if kw.get("file_ids") else "ask"
        # 2026-10-01 第444步：**标出这个窗口是干什么用的。**
        # 不标的话台账会把两件不相干的事记成一条线：实测 18:46 主窗口
        # 47c27cb4 好好地停在第 40 号，紧接着出现 686099c2 的「第 2 号」，
        # 看起来像「跳窗口丢了 38 步」—— 其实那是 **util 窗口**（dsh 生成
        # 标题/摘要的小请求共用窗口，用满 UTIL_USES=20 次就轮换一个），
        # 跟主对话半点关系都没有。按用途分开，缺口才不会算错。
        _lg_use = "main"
        try:
            if not kw.get("session"):
                _lg_use = "new"
            else:
                for _r in self.cache.rows:
                    if _r.get("session") == _lg_sid:
                        _lg_use = ("util" if _r.get("tag")
                                   == SessionCache.UTIL_TAG else "main")
                        break
        except BaseException:            # noqa: BLE001
            _lg_use = "main"
        try:
            save_sent_prompt(_p, slug=_lg_slug, sid=_lg_sid, mid=None,
                             model=_lg_model, kind=_lg_kind,
                             extra={"th": 1 if kw.get("thinking") else 0,
                                    "sr": 1 if kw.get("search") else 0,
                                    "gate": _lim, "use": _lg_use})
        except BaseException:            # noqa: BLE001
            pass
        # 2026-10-02：**断流重试探针**。
        # 包一层 on_delta，记下这一发到底收到过东西没有。
        # 收到过 -> 不能重发（会重复）；从零收 -> 可以重发。
        _got_any = [False]
        _od0 = kw.get("on_delta")
        if callable(_od0):
            def _od_probe(kind, text, _f=_od0, _g=_got_any):
                if text:
                    _g[0] = True
                return _f(kind, text)
            kw["on_delta"] = _od_probe

        for i, wait in enumerate((0.0,) + self.RATE_BACKOFF):
            if wait:
                self.note(f"  上游限流，等 {wait:.0f}s 后第 {i} 次重试")
                time.sleep(wait)
            try:
                _res = self.ds.ask(**kw)
                # 上游回的 message_id 就是台账上那个数字 —— 把它补记一行。
                # 只在真拿到时补：回空/异常时 mid 是 None，补了反而污染台账。
                try:
                    if isinstance(_res, tuple) and len(_res) >= 3 and _res[2]:
                        _mid = _res[2]
                        _real_sid = _res[1] or _lg_sid
                        self.note(f"  ▤ 台账：{_lg_slug} 缓存到第 {_mid} 号"
                                  f"（{_lg_sid[:8] or '新窗口'}，{len(_p)} 字）")
                        # 2026-10-01 第447步：写**本地账本**（以组名为主键）。
                        # 这是「本地数据校验走到哪一步」的唯一落点 ——
                        # 台账流水回答"发过什么"，账本回答"到哪了"。
                        try:
                            _lgrp = group_name_of(_lg_slug)
                            if _lgrp:
                                ledger_put(_lgrp, _lg_slug,
                                           sid=str(_real_sid), mid=_mid)
                        except BaseException:    # noqa: BLE001
                            pass
                        save_sent_prompt(
                            "", slug=_lg_slug, sid=str(_real_sid), mid=_mid,
                            model=_lg_model, kind="stamp",
                            extra={"chars": len(_p), "use": _lg_use})
                except BaseException:    # noqa: BLE001
                    pass
                return _res
            except Exception as exc:              # noqa: BLE001
                if not is_rate_limited(exc):
                    # 2026-10-02（用户报错：Response ended prematurely）：
                    # **上游断流也重试**，但只有「一个字都没收到」时。
                    #
                    # 为什么分这么细：收到半截再重发，上游会从头吐，
                    # 客户端就会看到重复内容（甚至两份不同的工具调用）。
                    # 零收获时重发则完全无害：对端什么都没给我们。
                    if not is_stream_cut(exc):
                        raise
                    if _got_any[0]:
                        # 已经吐了一半，不重发：交给上层当截断处理
                        self.note("  ⚠ 上游断流（已收到部分内容）"
                                  "，不重发 —— 交给上层按截断处理")
                        raise
                    if i >= len(self.RATE_BACKOFF):
                        self.note(f"  ⚠ 上游断流（零收获），"
                                  f"重试 {i} 次仍失败，放弃")
                        raise
                    self.note(f"  ↺ 上游断流（零收获），"
                              f"等 {self.RATE_BACKOFF[i]:.0f}s 后重发"
                              f"（第 {i + 1}/{len(self.RATE_BACKOFF)} 次）")
                    last = exc
                    continue
                self.cool_until = time.time() + self.COOLDOWN
                if i == 0:
                    # 监控用：一次限流事件只记一次（后面的 i 是退避重试）
                    self.note("  ⚠ 上游判定发送过于频繁，进入冷却")
                last = exc
        raise RuntimeError(
            f"上游限流，退避重试后仍然失败：{last}。"
            f"账号被 DeepSeek 判定发送过于频繁，先停一会儿再用。")


    # 2026-09-22 改（用户实测）：原文写「上一条回复因为长度上限被截断了」，但**从模型
    # 自己的视角，它上一条是一个工具调用、正等着工具结果** —— 桥把它截断在中途，也
    # 从来没执行过它。提示与模型所见矛盾，模型于是开始琢磨这个矛盾（实测 thinking:
    # "The user is asking me to continue… But I haven't produced text that got truncated
    # — my last message was a tool call."），推理把整轮预算烧光 → 正文空 → 桥放弃。
    # 现在把真相说清楚：被截断的是那个工具调用 JSON，而且它没有执行。
    CONT_PROMPT = (
        "【继续】你上一条消息里的**工具调用 JSON 没有写完就被长度上限截断了**，"
        "所以它没有被执行，也不会有工具结果返回给你 —— 不要等结果、不要重新发起它。"
        "现在只做一件事：紧接着你上一条的最后一个字符往下写，把剩余字符补完，直到"
        "那个 JSON 工具调用闭合。不要重复已输出的内容、不要重新开始、不要加解释、"
        "不要重新开代码围栏。你的回复第一个字符就应该是缺失内容的下一个字符。")

    # 接续要是几乎没进展，重复同一句话只会继续原地打转，换一句更硬的
    STALL_PROMPT = (
        "【继续·严格】你上一条又没接上（要么空、要么只回几个字）。重申事实：那个工具调用"
        "被长度上限截断在中途，**没有执行、不会有结果**——不用等、不用道歉、不用解释。"
        "现在只做一件事：从断点继续输出剩余字符，把 JSON 补闭合"
        "（结尾的引号、花括号、方括号、``` 围栏）。已经输出过的字符一个都不要再写。"
        "你的回复第一个字符就应该是缺失内容的下一个字符。")

    MIN_GAIN = 200        # 一次接续少于这么多字符，算原地打转
    JUNK_PROMPT_MAX = 20  # 接续回信短于这么多字且不含 JSON 结构字符 -> 当空话丢弃，不拼进正文

    def continue_until_parsable(self, model, text, session, parent, tries=3):
        """回复被截断就自动接着要，拼到能解析出工具调用为止。

        返回 (拼好的正文, session, parent)。试到上限还不行就原样返回，
        让上层照旧当普通文本处理。

        次数刻意压得低：接续是额外请求，撞限流的账全记在账号头上。但是**不能
        低于 3** —— 第 1 次还没起用严格提示（stalled 是这轮结果才置上的），
        真正能发出 STALL_PROMPT 的最早是第 2 次。只有 2 次的话那句「换严格提示
        再试」刚打出来循环就结束了，等于从来没试过严格提示（实测 6 次里 4 次
        白说）。3 次才让严格提示真有一次机会。
        """

        thinking, search = MODELS.get(model, (False, False))
        stalled = False
        _shell_at = None          # 首次拼入片段起点；收尾时若仍解析不出调用就给它套可见壳
        for i in range(tries):
            with self.gate:
                self._inflight_at = time.time()  # 在途计时起点
                self._pace()
                self.note(f"↻ 回复被截断，第 {i + 1} 次接续（已有 {len(text)} 字）")
                try:
                    more, sid, mid = self.ds.ask(
                        self.STALL_PROMPT if stalled else self.CONT_PROMPT,
                        session=session, parent=parent,
                        thinking=thinking, search=search, quiet=True)
                except Exception as exc:              # noqa: BLE001
                    if is_rate_limited(exc):
                        # 接续本来就是额外的请求，撞上限流就立刻收手
                        self.cool_until = time.time() + self.COOLDOWN
                        self.note("  接续撞上限流，停止接续")
                        break
                    raise
                # 回完计时：接续的响应收完这一刻才记 last_call（同 run()）
                self.last_call = time.time()
                self.last_had_tool = True   # 接续就是为了补全工具调用，必有代码

            if not (more or "").strip():
                # 2026-09-22 改（用户实测）：原来这里直接 break —— 「接续拿到空回复，
                # 放弃」，把半截内容原样交回 dsh；dsh 看到没有工具调用就判 completed
                # （dsh-agent-loop:1117），任务当场停住，用户看到的就是「窗口坏掉」。
                # 而接续拿到空回复，多半只是「整轮预算又烧在推理上」——换严格提示再要
                # 一次往往就出正文了。所以不再立刻放弃，记一次 stall 继续循环。
                if stalled:
                    self.note("  接续连续拿到空回复，放弃")
                    break
                if i + 1 < tries:
                    self.note("  接续拿到空回复，换严格提示再试")
                stalled = True
                continue
            before = len(text)
            _add = (more or "").strip()
            if len(_add) <= self.JUNK_PROMPT_MAX \
                    and not any(c in _add for c in '{}[]":,`'):
                self.note(f"  接续只回了 {len(_add)} 字空话，丢弃不拼")
                if stalled:
                    self.note("  接续连续拿到空话，放弃")
                    break
                if i + 1 < tries:
                    self.note("  接续拿到空话，换严格提示再试")
                stalled = True
                continue
            text = stitch(text, more)
            _sg = len(text) - before
            self.note("  接续拼入 " + str(_sg) + " 字（片段 " + str(len(_add)) + " 字），尾部：" + _add[-30:])
            session, parent = sid, mid
            if parse_tool_calls(text):
                self.note(f"  接续成功，拼完共 {len(text)} 字")
                break
            if _shell_at is None:
                _shell_at = before
            gain = len(text) - before
            if gain < self.MIN_GAIN:
                if stalled:
                    self.note(f"  接续连续两次只多了 {gain} 字，放弃")
                    break
                if i + 1 < tries:
                    self.note(f"  接续只多了 {gain} 字，换严格提示再试")
                stalled = True
            else:
                stalled = False
        if _shell_at is not None and not parse_tool_calls(text):
            _head, _tail = text[:_shell_at], text[_shell_at:]
            if _tail.strip():
                text = (_head + "\n"
                        + "【桥·以下为自动接续补入，不是新指令，看到可忽略】\n"
                        + _tail + "\n"
                        + "【桥·接续补入结束】")
                self.note("  接续未拼完工具调用，已给拼入片段加可见壳")
        return text, session, parent


    @staticmethod
    def clip_sections(body, limit):
        """按 `## ` 分段裁剪本地代写底稿，优先保尾部关键段。

        2026-09-26 第96步（报告改动3）：原来是盲目「头 60% + 尾 40%」。实测
        检查点 41767 字 > CHECKPOINT_MAX 16000，被砍 62%，而留住的头 60% 全是
        背景（Primary Request / Key Technical Concepts），真正要命的 Current Work
        / Next Step / Critical Context 都在尾部，正好被砍。现在按 `## ` 切段，
        从尾往前装，装不下就停 —— 尾部四段（Pending Jobs / Current Work /
        Next Step / Critical Context）必然留下，背景段让位。
        """
        if limit <= 0 or len(body) <= limit:
            return body
        _parts = re.split(r"(?m)^(?=## )", body)
        if len(_parts) <= 1:
            # 没有 `## ` 分段（普通回复）—— 退回头尾各留一段。
            _h = int(limit * 0.4)
            return (body[:_h] + "\n…（本地代写，中段略）…\n"
                    + body[-(limit - _h):])
        _head, _secs = _parts[0], _parts[1:]
        _keep, _used = [], len(_head)
        for _s in reversed(_secs):
            if _used + len(_s) > limit and _keep:
                break
            _keep.append(_s)
            _used += len(_s)
        _keep.reverse()
        _drop = len(_secs) - len(_keep)
        _out = _head + "".join(_keep)
        if _drop:
            _out = ("…（本地代写：前面省略 %d 段背景，保留尾部 %d 段）…\n\n"
                    % (_drop, len(_keep))) + _out
        return _out

    def local_handoff(self, why=""):
        """老号交不出交接时，用本地已有数据替它写一段。

        触发场景实测 2/15 换号，全是同一条链：上游零片段 → 桥判「会话废了」
        → forget() 删行 → 换号时 match() 认不出来。老号此刻多半正被限流，
        再向它发请求只会更糟；但它自己已经落在本地的东西还在 —— 最后一次
        成功回复就是它的最终进度陈述。

        返回空串是最坏的选择：接手方拿到空交接，且不知道自己没拿到，
        会把上一棒做完的活再做一遍。
        """
        Bridge.HANDOFF_LOCAL += 1
        out = [
            f"【本地代写交接】上一棒（{self.slug}）没能自己交出交接，"
            f"下面这段是桥用它本地已有数据替你写的。",
            "",
        ]
        if why:
            out += [f"原因：{why}", ""]
        # 压缩检查点优先：客户端定期让模型做上下文压缩，产物是 8 段式状态
        # 快照（Primary Request and Intent / Current Work / Next Step …），
        # 实测 1.3万~3.7万字。比任何一条普通回复都全，交接时应该用它。
        _body, _when, _label = "", "", ""
        if self.last_checkpoint:
            _body = self.last_checkpoint
            _when = time.strftime("%H:%M", time.localtime(self.last_checkpoint_at)) \
                if self.last_checkpoint_at else ""
            _label = "压缩检查点"
        elif self.last_reply:
            _body = self.last_reply
            _when = time.strftime("%H:%M", time.localtime(self.last_reply_at)) \
                if self.last_reply_at else ""
            _label = "最后一次成功回复"
        if not _body:
            # 桥刚重启时上面两个字段都是空的 —— 去落盘存档里捞一条兜底，
            # 不然交接只剩 206 字空壳，新号等于没拿到交接。
            try:
                _rt, _rat = last_archived_reply(getattr(self, "slug", ""))
                if _rt:
                    _body = _rt
                    _when = time.strftime("%H:%M", time.localtime(_rat)) if _rat else ""
                    _label = ("压缩检查点" if _rt.startswith(CHECKPOINT_HEAD)
                              else "最后一次成功回复")
            except Exception:          # noqa: BLE001
                pass
        if _body:
            # 2026-09-26 第96步：按段裁，保尾部关键段（不再是盲目砍头 60%）。
            _body = self.clip_sections(_body, CHECKPOINT_MAX)
            out += [
                f"## 上一棒{_label}"
                + (f"（{_when}）" if _when else "")
                + " —— 它的最终状态，先按这个对齐：",
                "",
                _body,
                "",
            ]
        else:
            out += ["## 上一棒没有留下可用的回复正文或检查点", ""]
        out += [
            f"## 上一棒计数：真交互 {self.turns_here} 条，"
            f"失败 {self.fails} 次，最近错误：{self.last_error or '无'}",
            "",
        ]
        # 这里**不**贴台账：接手方那一轮的 prompt 末尾本来就有 ledger_block
        # （plan() L1278 拼的），交接正文是前置的，再贴一次同一块会出现两遍。
        out += [
            "## 你接手后怎么做",
            "1. 上面那次回复里若写了「下一步」，从那里继续；",
            "2. 拿不准就按台账「活跃」栏里属于本窗口的行恢复；",
            "3. 不要重新调查整个任务，不要重做已完成且已验证的步骤。",
        ]
        _txt = "\n".join(out)
        # 2026-09-26 第99步：本地代写的交接单同样落一行到 _steps.md。
        # 报告改动2 只覆盖了 handoff_dump（老号亲笔那一路），但本地上传模式下
        # 走的是这一路 —— 不落盘的话，「交接单不落盘」在 local 模式下原样存在：
        # 用户看不到、事后查不到、别的 AI 拿不到。
        handoff_to_steps(getattr(self, "slug", "?"), "下一棒", _txt,
                         note="本地代写", slug=getattr(self, "slug", ""))
        return _txt


    def local_checkpoint(self, why=""):
        """本地代写的「检查点」——发成 检查点.txt 附件，不发任何上游请求。

        底稿与 local_handoff 同源（压缩检查点 > 最后一次成功回复 > 落盘存档），
        接手方在正文里看到它，附件里也能再拿到一份。取不到就返回空串，
        _upload_attach 对空串不产文件，附件集退化成三份。
        """
        body = ""
        if self.last_checkpoint:
            body = self.last_checkpoint
        elif self.last_reply:
            body = self.last_reply
        else:
            try:
                body = last_archived_reply(getattr(self, "slug", ""))[0]
            except Exception:          # noqa: BLE001
                body = ""
        body = (body or "").strip()
        if not body:
            return ""
        # 2026-09-26 第96步：同上，按段裁，保 Current Work / Next Step。
        body = self.clip_sections(body, CHECKPOINT_MAX)
        return body

    def checkpoint_dump(self, old, keys, model):
        """换号时向老号要一份「检查点」（8 段式状态快照），上传成附件用。

        失败 / 空一律返回空串，绝不把轮换卡住。
        """
        try:
            row = old.cache.match(keys, fresh=False)
            sess = row.get("session") if row else None
            if not sess:
                return ""
            parent = row.get("parent")
            thinking, search = MODELS.get(model, (False, False))
            cool_left = max(0.0, old.cool_until - time.time())
            if cool_left > HANDOFF_COOL_MAX:
                return ""
            left = old.wait_left()
            if left > 0:
                time.sleep(min(left, HANDOFF_COOL_MAX))
            old.last_call = time.time()
            # 2026-10-01 修：**必须拿老号自己的 gate 再打上游**。
            #
            # requests.Session 不是线程安全的。老号可能正在忙 —— 它自己的
            # run() 持有 old.gate、用着 old.s 在流式读。这里不串行的话，
            # 就是两个线程同时用一个 Session，连接池状态会被踩坏，
            # 表现是**永久挂死**：pythonw 活着、端口在听、/health 不响应、
            # 日志长时间无输出、上游连接停在 CLOSE_WAIT。
            #
            # 为什么加锁不会自锁：调用方是 Handler.do_POST，它**不持有任何
            # bridge 的 gate**（gate 只在 run() 内部才取）。而且 old 与 new
            # 是两个不同的号，old.gate 与 self.gate 不是同一把。
            #
            # 拿不到锁的代价：交接/检查点本来就是"能拿到就赚"的额外请求，
            # 老号忙就等它忙完；等不到会走 except 分支退回本地代写。
            with old.gate:
                text, _sid, _mid = old.ds.ask(
                    prompt=pget("checkpoint_instruction",
                                CHECKPOINT_INSTRUCTION),
                    session=sess, parent=parent,
                    thinking=thinking, search=search, quiet=True,
                    file_ids=[], on_delta=None, stop=None)
            text = (text or "").strip()
            if not text:
                return ""
            self.note(f"  ✓ 检查点：{old.slug} 生成 {len(text)} 字")
            return text
        except Exception as exc:             # noqa: BLE001
            self.note(f"  ✗ 检查点失败（{type(exc).__name__}），跳过")
            return ""

    def handoff_dump(self, old, keys, model, since=0.0):
        """换号那一刻，向老号要一段交接：做了什么、到哪、下一步。

        delta 是按指纹切的、不含 assistant，接手方看不到上一棒的结论，
        会把做完的活再做一遍。这里直接问，比从历史里猜靠得住。
        失败一律静默跳过 —— 交接失败绝不能把轮换卡住。
        """
        try:
            # fresh=False：这里要的是「老号讲它自己干了什么」，不是让它接着干。
            # 陈旧会话（别的号已经跑到前面去了）照样能讲清它自己那些步骤，用
            # fresh=True 会把它判成「没有可用会话」，直接退化成本地代写（实测 12 次）。
            row = old.cache.match(keys, fresh=False)
            sess = row.get("session") if row else None
            if not sess:
                self.note(f"  交接：{old.slug} 没有可用会话，改用本地代写")
                # 会话被 forget() 判废删除后这里必然认不出来（实测 2/15 换号）。
                # 返回空串等于「静默零信息」—— 接手方不知道自己没拿到交接，
                # 会把上一棒做完的活再做一遍。所以用本地数据替它写一段。
                emit("handoff", frm=getattr(old, "slug", ""),
                     verdict="no_session", chars=0,
                     why="会话被 forget() 判废删除")
                return old.local_handoff("会话被 forget() 判废删除，认不出")
            parent = row.get("parent")
            thinking, search = MODELS.get(model, (False, False))
            # 不走 old._ask：那里有 RATE_BACKOFF=(20,60) 的 sleep，A 若在冷却
            # 会让接手那一轮在 HTTP 上挂 80 秒。交接拿不到可以接受，阻塞不行 ——
            # 所以直连 ds.ask，撞限流就抛上来被吞掉，立即跳过。
            # wait_left() 里其实混了两段：回完计时的 min_interval（全局 2 秒）和
            # 限流冷却 cool_until。原来只要 left > 0 就跳过 —— 实测 38 次本地代写
            # 里有 35 次只是因为那 2 秒的 min_interval，白白把 4-6K 字的真交接单
            # 换成了本地代写。现在分开判：真冷却太久才放弃；只是回完间隔就等它
            # 过去再问。等待有上限，不能把接手那一轮挂死。
            left = old.wait_left()
            cool_left = max(0.0, old.cool_until - time.time())
            if cool_left > HANDOFF_COOL_MAX:
                self.note(f"  交接：{old.slug} 还在限流冷却 {cool_left:.0f}s，改用本地代写")
                emit("handoff", frm=getattr(old, "slug", ""),
                     verdict="cooling", chars=0,
                     wait=round(cool_left, 1))
                return old.local_handoff(f"还在限流冷却 {cool_left:.0f}s，不宜再发请求")
            if left > 0:
                self.note(f"  交接：{old.slug} 还要等 {left:.0f}s"
                          f"（回完间隔，不是限流），等它再要")
                time.sleep(min(left, HANDOFF_COOL_MAX))
            self.note(f"  交接：向 {old.slug} 要交接（会话 {sess}）")
            old.last_call = time.time()
            # 提示词按账号取：界面里可以给每个号单独写一份，
            # 没写的跟随全局，全局也没设就用出厂那段。
            ask_prompt = (getattr(old, "handoff_prompt", "")
                          or pget("handoff_note", HANDOFF_PROMPT))
            # 时间区间：老号可能干了几个小时，接手方要知道「你从几点到几点」。
            # since 是它拿到通道的时刻（resolve 里记的）；拿不到就写「本次」。
            try:
                _t0 = time.strftime("%H:%M", time.localtime(since)) if since else ""
                _t1 = time.strftime("%H:%M", time.localtime())
            except (ValueError, OSError):
                _t0, _t1 = "", ""
            if _t0:
                ask_prompt = (f"【时间】你负责这一段是从 {_t0} 到 {_t1}。"
                              f"写第 1 段「现状」时把时间区间带上。\n\n"
                              + ask_prompt)
            # 2026-10-01 修：同上 —— 用老号的 Session 就必须拿老号的 gate。
            with old.gate:
                text, _sid, _mid = old.ds.ask(
                    prompt=ask_prompt, session=sess, parent=parent,
                    thinking=thinking, search=search, quiet=True,
                    file_ids=[], on_delta=None, stop=None)
            text = (text or "").strip()
            if not text:
                self.note(f"  交接：{old.slug} 回了空，改用本地代写")
                emit("handoff", frm=getattr(old, "slug", ""),
                     verdict="empty", chars=0)
                return old.local_handoff("老号这次回了空")
            raw_len = len(text)
            cut = 0
            if raw_len > HANDOFF_MAX:
                # 从中间砍，头尾都留。直接 text[:MAX] 会把第 4、5 段
                # （你的任务、验收）切掉 —— 那两段恰恰是接手方最需要的，
                # 留下的全是「已完成」这种知道也没用的。
                head = int(HANDOFF_MAX * 0.6)
                tail = HANDOFF_MAX - head
                text = (text[:head] + "\n…（中略）…\n"
                        + text[-tail:])
                cut = raw_len - len(text)
            if cut:
                # 自诊断：这一刀是**我们自己**砍的（HANDOFF_MAX），不是上游砍的。
                # 把「还差多少」直接写出来 —— 连续撞上限就说明这个常数该提了，
                # 不用再去翻原始字数。（2026-09-22 加）
                self.note("  ⚠ 交接撞上限：原文 " + str(raw_len) + " 字 > "
                          + "HANDOFF_MAX " + str(HANDOFF_MAX) + "，砍掉 "
                          + str(cut) + " 字（"
                          + str(round(100.0 * cut / raw_len)) + "%），"
                          + "再多 " + str(raw_len - HANDOFF_MAX)
                          + " 字就不砍了")
            # 「交出 N 字」是截断后的长度。原文多长只有这里知道 ——
            # 不记下来就没法判断中段到底丢了多大一块（2026-09-21 加）。
            self.note(f"  交接：{old.slug} 交出 {len(text)} 字"
                      + (f"（原文 {raw_len} 字，中段丢 {cut} 字）" if cut else ""))
            emit("handoff", frm=getattr(old, "slug", ""),
                 verdict="upstream", chars=len(text),
                 raw=raw_len, cut=cut)
            # 2026-09-26 第98步（报告改动2）：交接单落一行到 _steps.md。
            handoff_to_steps(getattr(old, "slug", "?"),
                             getattr(self, "slug", "?"), text,
                             slug=getattr(old, "slug", ""))
            return text
        except Exception as exc:             # noqa: BLE001
            self.note(f"  交接失败（{type(exc).__name__}: {exc}），改用本地代写")
            try:
                emit("handoff", frm=getattr(old, "slug", ""),
                     verdict="error", chars=0,
                     why=type(exc).__name__)
                return old.local_handoff(f"交接请求抛了 {type(exc).__name__}")
            except Exception:                # noqa: BLE001
                return ""                  # 兜底也炸就还是空，绝不能再往上抛


    def run(self, model, messages, tools, on_delta, stop, no_think=False):
        """真正打上游。返回 (正文, session_id, message_id, usage)。

        no_think=True 强制关掉深度思考，给「只思考不落笔」兜底。
        """
        thinking, _ = MODELS.get(model, (False, False))
        if no_think:
            thinking = False
        # 2026-09-22 联网开关：每号单独设（GUI 账号池里默认关）。
        # 编码 agent 走 web_fetch/run_code 本地请求，用不上模型自带 search。
        search = self.search
        # 2026-09-22 加（用户口径）：先体检 —— 尺寸到了才走附件这条路。
        # 这一句排在所有判断之前、生成/上传之前；体检不过就一次都不生成、不上传。
        _pol = empty_policy(getattr(self, "slug", ""))
        _attach = (bool(_pol.get("txt_attach", True)) and (
            self.input_chars(messages)
            > _num(_pol, "txt_attach_chars", 150000))
        ) or bool(getattr(self, "pending_checkpoint", "")) \
            or bool(getattr(self, "handoff_note", ""))
        # 2026-09-26 第94步：plan() 会消费并清掉 attach_note_local（谁用了谁清），
        # 而附件上传在 plan() 之后 —— 不清点抄下来的话，上传时永远读到 False，
        # 上下文.txt 照旧会上传。
        _local_attach = getattr(self, "attach_note_local", False)
        # ===== 固定组窗口：首次自动建立（2026-10-01 第438步，用户指出更简的办法）=====
        # 用户口径：「你不会直接让他回剪辑两个字」。
        #
        # 之前绕了两圈：
        #   第436步：new_session -> rename 失败（空窗口）-> 登记 -> 下一轮补改名
        #   第437步：new_session -> ask("好") -> rename(组名)  [0.56+0.74+0.09s]
        #
        # 现在（实测确认）：**让模型只回「组名」两个字，系统自动生成的标题就是组名。**
        #   发「只回复两个字：剪辑」 -> 模型回「剪辑」 -> 窗口标题 = 「剪辑」
        # 标题是按**模型回复的内容**生成的，不是按用户发的话 ——
        # 对照实验：同样让模型回「剪辑」，发「剪辑」得「剪辑需求咨询」，
        # 发「只回复两个字：剪辑」得「剪辑」。
        #
        # 所以 rename 这一步可以整个去掉，只剩：
        #   new_session() -> ask("只回复两个字：<组名>") -> 登记
        #
        # 保留 rename 作为**兜底**：万一某个组名的自动标题没生成准
        # （比如组名是「test」这种英文，模型可能回别的东西），
        # 发完立刻查一次标题，不对就 rename 纠正。
        #
        # 触发条件（**只看结果，不看时机** —— 用户口径 2026-10-01）：
        #   · 开关开着
        #   · 本号在某个组里（固定号没有「组窗口」这个概念）
        #   · 本组+本号**还没有**登记的 sid
        #
        # 「别管什么时候 如果轮询到没有该分组的id的窗口没有这个窗口 就自动新建」
        # —— 原来这里还有一个 `session is None` 的条件（只在「本轮决定开新会话」
        # 时才建），那是我自己加的，它造成「第一次轮到的号要走两轮才吃到缓存」。
        # 现在**去掉它**：只要轮到这个号、而它没有本组窗口，就建。
        # 不管本轮是认亲成功、失败、还是走 util，统一在这一处收口。
        #
        # 整个过程包 BaseException —— 建窗口失败绝不能把这一轮带塌。
        #
        # 2026-10-03（同 plan()，用户口径「不认亲不看组名 只看自己 id 名」）：
        # **锚定名 = 号自己的 id。** 组名不再参与 —— 组只决定轮转到谁。
        # 2026-10-03（同 plan()）：**锚定名取 anchor，不是 slug。**
        _gsgroup = str(getattr(self, "anchor", "") or
                       getattr(self, "slug", "") or "").strip()
        _gs_on = bool(_gsgroup)
        # 2026-10-01 第444步（用户口径「窗口名肯定不会变…轻易不要换窗口要写死」）：
        # **建之前先按名字去上游找一次。** 找到就用它，找不到才新建。
        #
        # 这条是「轻易不要换窗口」的落点，也是 309 攒出 6 个同名窗口的根因：
        # 原条件只看**本地登记**空不空，本地一空就新建 —— 哪怕上游那个
        # 「剪辑」窗口好好地在。本地登记会因为换机器、清文件、跨进程丢，
        # 而上游窗口不会。所以判据必须以上游为准。
        if _gs_on and not group_session_of(
                _gsgroup, getattr(self, "slug", "")):
            _found = ""
            try:
                _found = group_window_by_name(
                    self.ds, _gsgroup, note=self.note)
            except BaseException:        # noqa: BLE001
                _found = ""
            if _found:
                # 2026-10-03（用户口径「超过1000直接删掉」）：
                # **找到之后先看窗口多大。**
                # 实测废掉的那个窗口 version=2894 / 4.1 MB，上游对它返回空，
                # 于是桥每轮取快照都失败、反复重试。删掉它，桥下次找不到
                # 就会新建一个空窗口 —— 不用加别的逻辑。
                try:
                    _cs, _ = self.ds.history(_found)
                    _ver = int((_cs or {}).get("version") or 0)
                    if _ver > WINDOW_MAX_ROUNDS:
                        self.note(f"  ♻ 窗口「{_gsgroup}」已 {_ver} 条"
                                  f"（上限 {WINDOW_MAX_ROUNDS}），删除重建")
                        try:
                            self.ds.delete_session(_found)
                            self.note(f"  ♻ 已删除旧窗口 {_found[:8]}")
                        except BaseException as _de:        # noqa: BLE001
                            self.note("  ⚠ 删除旧窗口失败："
                                      + type(_de).__name__ + "：" + str(_de)[:60])
                        # 删掉之后当没找到 -> 走下面的新建逻辑
                        group_session_set(_gsgroup, getattr(self, "slug", ""), "")
                        _found = ""
                    else:
                        self.note(f"  ⇢ 窗口「{_gsgroup}」{_ver} 条，在限内，采用")
                except BaseException as _he:            # noqa: BLE001
                    # 查不到次数就用它 —— 绝不因为查不了就把窗口丢了
                    self.note("  · 窗口次数查询失败（沿用该窗口）："
                              + type(_he).__name__ + "：" + str(_he)[:50])
            if _found:
                group_session_set(_gsgroup, getattr(self, "slug", ""), _found)
                self.note(f"  ⇢ 上游已有「{_gsgroup}」窗口 {_found[:8]}，"
                          f"直接采用（不新建、不改名）")
                # 已找到 → 本轮不再走下面的新建逻辑
                _found_done = True
            else:
                _found_done = False
        else:
            _found_done = False
        if _gs_on and not _found_done and not group_session_of(
                _gsgroup, getattr(self, "slug", "")):
            try:
                _ns = self.ds.new_session()
                if _ns:
                    # 2026-10-01 第439步（用户口径：「只回复"test"不要修改大小写
                    # 不要自行添加任何字体解释」）：**不再指望自动标题。**
                    #
                    # 实测穷举结果（都失败）：
                    #   发 只回复"test"                  -> 标题 Test reply
                    #   发 只回复"test"…不要修改大小写 -> 标题 只回复test
                    #   发 只回复两个字：剪辑         -> 标题 只回复剪辑 / 剪辑 / 剪辑组名回复（不确定）
                    #   模型回复每次都精确正确，**但标题生成器把提问也吃进去了**。
                    #   对照：用户问得很长+模型只回“剪辑” -> 标题「数据库性能分析只回剪辑」。
                    #
                    # 结论：标题 = （用户提问 + 模型回复）整段摘要，**两边都吃**，
                    # 而且英文还会被首字母大写化（test -> Test）。
                    # 所以只靠改提示词永远做不到「标题 == 组名」。**rename 必须是主路径。**
                    #
                    # 先发一条极短的：目的仅是让窗口非空（空窗口
                    # 不让改名，实测 EMPTY_CHAT_SESSION）。内容无所谓。
                    try:
                        self.ds.ask("好", session=_ns, thinking=False)
                    except Exception:        # noqa: BLE001
                        pass
                    # 2026-10-01 第440步（用户口径：「如果建的窗口名字一样
                    # 则跳过 不一样在改」）：**先查实际标题，一样就不动。**
                    #
                    # 为什么要查而不是直接改：
                    #   自动标题**有时会恰好就是组名**（中文组名碰巧命中过好几次），
                    #   那种情况就不必多一次写操作。不一样才改。
                    #
                    # 为什么不能只看“有没有登记”：
                    #   登记只说明桥记过这个 sid，说不明上游那个窗口的标题对不对
                    #   （可能是别人/别的程序改过，也可能历史遗留）。
                    #   **以实查到的标题为准**，不以桥自己的记录为准。
                    _fixed = False
                    _title_now = ""
                    try:
                        _rows, _ = self.ds.list_sessions(count=100)
                        _me = [r for r in _rows
                               if str(r.get("id")) == str(_ns)]
                        _title_now = str(_me[0].get("title") or "") if _me else ""
                    except Exception:        # noqa: BLE001
                        _title_now = ""
                    if _title_now == _gsgroup:
                        # 2026-10-01 第444步（用户口径「如果我自己新建分组他也要
                        # 识别自动新建分组名」）：**名字对还不够，必须是 USER。**
                        #
                        # 自动标题碰巧等于组名时 title_type 是 SYSTEM，而
                        # group_window_by_name() **只认 USER**（实测 309 有 10 个
                        # 叫「剪辑」的窗口，6 USER + 4 SYSTEM，只看名字会挑错）。
                        # 这里若放过 SYSTEM，下次按名字就找不到自己 —— 名字锚点
                        # 当场失效，又退回「看不见就新建」的老路。
                        # 所以名字即使对，也补一次 rename 把它钉成 USER。
                        _tt = ""
                        try:
                            _tt = str(_me[0].get("title_type") or "") if _me else ""
                        except Exception:            # noqa: BLE001
                            _tt = ""
                        if _tt == "USER":
                            _fixed = True
                            self.note("  ⇢ 窗口标题已是「" + _gsgroup
                                      + "」且已锁定（USER），不改")
                        else:
                            try:
                                self.ds.rename_session(_ns, _gsgroup)
                                _fixed = True
                                self.note("  ⇢ 窗口标题已是「" + _gsgroup
                                          + "」但未锁定（SYSTEM），补一次 rename 锁定")
                            except Exception as _re2:    # noqa: BLE001
                                self.note("  ⚠ 锁定标题失败（"
                                          + type(_re2).__name__ + "）："
                                          + str(_re2)[:60])
                    else:
                        try:
                            self.ds.rename_session(_ns, _gsgroup)
                            _fixed = True
                            self.note("  ⇢ 窗口标题是「" + _title_now
                                      + "」，不是组名，已改成「"
                                      + _gsgroup + "」")
                        except Exception as _rne:      # noqa: BLE001
                            self.note("  ⚠ 窗口改名失败（"
                                      + type(_rne).__name__ + "），标题可能不是组名："
                                      + str(_rne)[:60])
                    group_session_set(_gsgroup, getattr(self, "slug", ""), _ns)
                    self.note("  ⇢ 固定组窗口：新建「" + _gsgroup
                              + "」/" + str(self.slug) + " -> " + _ns[:8])
                    # 2026-10-03（用户口径：「新建完去本地获取该项目的上下文
                    # 喂养该窗口」）：**新窗口一开就把本地上下文喂进去。**
                    #
                    # 为什么必须喂：新窗口是空的，模型睁眼一片空白 ——
                    # 它会 glob/read 全盘扫描找线索（实测 read 2652 次 /
                    # glob 1538 次）。喂了它就直接知道项目在干什么、干到哪了。
                    #
                    # 为什么用本地不用上游：旧窗口可能刚被删（超 1000 轮那条路
                    # 就是先删再建），上游没得取。而本地存档一直在。
                    #
                    # handoff_extract 是桥自己的抽取器，产出「接手方能用的几节」，
                    # 实测本组 4573 字。取不到就跳过，绝不影响建窗。
                    try:
                        _slugs = []
                        try:
                            for _g in (_groups_root().get("groups") or []):
                                if str(_g.get("name") or _g.get("id") or "") == _gsgroup:
                                    _slugs = [str(x) for x in (_g.get("slugs") or [])]
                                    break
                        except BaseException:        # noqa: BLE001
                            _slugs = []
                        if not _slugs:
                            _slugs = [str(self.slug)]
                        _ctx = ""
                        try:
                            _ctx = handoff_extract(_slugs, limit=400)
                        except BaseException:        # noqa: BLE001
                            _ctx = ""
                        if _ctx and _ctx.strip():
                            _feed = (
                                "【本项目已有的上下文 —— 你接手前它就是这么过来的】"
                                + chr(10) + chr(10) + _ctx.strip() + chr(10) + chr(10)
                                + "以上是既成事实。直接接着做，不要复述它、"
                                + "不要重新调查整个项目。")
                            try:
                                self.ds.ask(_feed, session=_ns, thinking=False,
                                            quiet=True)
                                self.note("  ⇢ 已喂养本地上下文 %d 字到新窗口「%s」"
                                          % (len(_feed), _gsgroup))
                            except BaseException as _fe:    # noqa: BLE001
                                self.note("  ⚠ 喂养失败（不影响建窗）："
                                          + type(_fe).__name__ + "："
                                          + str(_fe)[:60])
                        else:
                            self.note("  · 本地上下文为空，跳过喂养")
                    except BaseException as _ce:            # noqa: BLE001
                        self.note("  ⚠ 取本地上下文失败（不影响建窗）："
                                  + type(_ce).__name__ + "：" + str(_ce)[:60])
            except BaseException as _gse:      # noqa: BLE001
                self.note("  ⚠ 固定组窗口建立失败（"
                          + type(_gse).__name__ + "），本轮照旧开新会话："
                          + str(_gse)[:80])


        keys, session, parent, prompt, images = self.plan(
            model, messages, tools, attach=_attach)
        # ===== 2026-09-30 统一兜底：plan() 之后、_ask() 之前，只此一处 ===== 
        # 用户口径：「没有任何变化」—— 前面几轮分别修了偶发分支、稳态分支、
        # 死区配置，但 108441 那一发照样被拒。根因是**每条分支各有各的算法**：
        #   · 稳态分支：_delta_items 的 known 取自行缓存；缓存行短（实测
        #     4f4c4669 只有 37 个 key）时 delta 近似全量重发，而 client
        #     上下文本身没到附件线（<=80000），于是 attach=False、内联全上。
        #   · 附件分支只影响 build_prompt，稳态分支根本不走 build_prompt。
        # 结论：**逐条分支去修永远会漏**。这里加一道与分支无关的收口 ——
        # 不管上面哪条路算出来的 prompt，只要超限就在这里削到限内。
        #
        # 削法：保留开头（指令/协议往往在前）与最新内容，中间标注省略并
        # 指向存档。**不抛异常** —— 发得出去永远好过界面红字。
        _gatelim = int(getattr(self, "HARD_LIMIT_CHARS", 0) or 0)
        if _gatelim > 0 and len(prompt) > _gatelim:
            _over = len(prompt) - _gatelim
            _nh = m_keep_head = int(_gatelim * 0.45)
            _nt = _gatelim - _nh - 400          # 留 400 给省略说明
            if _nt > 0 and _nt < len(prompt) - _nh:
                _mark = (chr(10) + chr(10) +
                         f"〔中间省略约 {_over} 字 —— 不是没有，是被体积上限裁掉了。" +
                         chr(10) +
                         "  需要原文就自己读（不要凭猜测补）：" + chr(10) +
                         f"  {REPLY_FILE}" + chr(10) +
                         "  一次 turn 一行 JSON：t/ts/k/slug/sid/text" + chr(10) +
                         f"  本号 slug={getattr(self, 'slug', '')}" + chr(10) + chr(10) +
                         "  更早的完整对话在 _relay_replies.jsonl 的历史档里。〕" +
                         chr(10) + chr(10))
                prompt = prompt[:_nh] + _mark + prompt[-_nt:]
                self.note(f"  ✂ 兜底削尾：{_gatelim + _over} -> {len(prompt)} 字"
                          f"（闸门 {_gatelim}），不拒绝、已附存档检索指引")
            else:
                prompt = prompt[:_gatelim]
                self.note(f"  ✂ 兜底硬截：{_gatelim + _over} -> {len(prompt)} 字")
        # ===== 兜底结束 =====

        # 上游到底吐了哪几类片段。判断「这个会话是不是废了」不能只看正文：
        # 有的轮次只出 THINK 不出 RESPONSE，正文是空的，但会话本身好得很。
        seen = set()

        think_chars = [0]

        def watch(kind, t):
            if t:
                seen.add(kind)
                if kind != "RESPONSE":
                    think_chars[0] += len(t)
            if on_delta:
                on_delta(kind, t)

        with self.gate:
            self._inflight_at = time.time()      # 在途计时起点（/health 透出）
            self._pace()
            self.note(f"→ {model} {'续' if session else '新'}会话 "
                      f"{len(prompt)} 字 thinking={thinking} search={search} "
                      f"tools={len(tools or [])} 图片={len(images)}")
            try:
                file_ids = self._upload_images(images) if images else []
                _cp = getattr(self, "pending_checkpoint", "")
                # 2026-10-03（用户口径：「应该让他认为他就干了这么多活就到这里了」）：
                # **不再把快照上传成附件，改成拼进 prompt 的文本。**
                #
                # 依据：dsh 自己的压缩就是把摘要直接铺进消息流 + 一句
                # 「当作既成背景，直接继续」，实测正常；而桥做成附件后，
                # 模型进入「我有个文件要读」的状态，找不到就重复调用
                # （实测一次请求里重复 read 5 次）。
                #
                # 拼装放在下面（prompt 已成形之后），因为要先有 prompt 才能插。
                if _cp:
                    self.note("  快照：作为工作背景内联（%d 字），不再挂成附件"
                              % len(_cp))

                if _attach:
                    # 三份附件：上下文 / 工具表 / 台账。体检已过（见上面 _attach 的判定），
                    # 这里才是生成与上传 —— 顺序就是用户口径：先体检、再过门、最后生成上传。
                    # 2026-10-03（用户口径「删掉」）：**不再用占位。**
                    # 占位原来的作用是「对话很短、没被裁掉历史时，让文件集恒定」，
                    # 因为那时说明里固定列着 上下文.txt，而空串不落盘 -> 对不上。
                    # 现在说明由 attach_note_for(names) **按实际挂了哪几份**生成，
                    # 没挂就不提 —— 那个矛盾已经不存在，占位是悬空的补丁。
                    # 没被裁掉的历史 = 全都还在 inline 提示里，本来也没什么可挂。
                    _ctx = "".join(LAST_DROPPED)
                    # 2026-09-26 第93步：local 交接模式下 上下文.txt 不上传（只传 检查点 + 工具表 + 台账）。
                    # 2026-09-26 第95步（报告改动1）：续接同一个上游会话时也不上传
                    # 上下文.txt —— 那份历史上游会话自己已经有了，每轮重挂等于每轮再
                    # 喂 15 万字（实测 15~20 万 token，远超压缩阈值 104857，于是几乎
                    # 每轮触发压缩 -> 历史重写 -> 指纹全变 -> 认亲失败 -> 开新会话又
                    # 挂全量）。session 为空 = 新会话 / 换号接手，上游没有这份历史，
                    # 照旧挂全量。
                    _skip_ctx = bool(_local_attach or session)
                    # 2026-10-01 第425步（用户口径「你说的是每次只发一次工具表 除非
                    # 换号 那可不可以理解为 每次只发一次检查点换号之前不用一直发呢」）：
                    # **续接同一上游会话时，工具表也不挂。**
                    #
                    # 依据就在上面 12 行 —— 上下文.txt 早就按这个道理做过了
                    # （2026-09-26 第95步）：那份历史上游会话自己已经有了，每轮
                    # 重挂等于每轮再喂一份。**工具表是同一个道理**：上游会话里
                    # 已经有一份完整工具表（每轮 inline 还带着 tools_block），
                    # 再挂一次纯属重复投递。
                    #
                    # 为什么现在才改：先前只盯「省 token」，没意识到更狠的代价是
                    # **每轮重挂会把会话历史撑大 -> 触发上游压缩 -> 历史重写 ->
                    # 指纹全变 -> 认亲失败 -> 开新会话又挂全量**（正是上面第95步
                    # 注释描述的那条链）。工具表 8087 字/轮，挂 20 轮就是 16 万字。
                    #
                    # 只在**开新会话 / 换号接手**（session 为空）时才挂：
                    # 那时上游确实没有这份表，必须给。
                    _skip_tools = bool(session)
                    _docs = (("台账", "".join(LAST_LEDGER)),)
                    if not _skip_tools:
                        _docs = (("工具表", "".join(LAST_TOOLS)),) + _docs
                    else:
                        self.note("  附件：跳过 工具表.txt（续接同一上游会话，"
                                  "上游已有），省 " + str(len("".join(LAST_TOOLS)))
                                  + " 字")
                    if not _skip_ctx:
                        _docs = (("上下文", _ctx),) + _docs
                    else:
                        self.note("  附件：跳过 上下文.txt（"
                                  + ("本地交接模式" if _local_attach
                                     else "续接同一上游会话")
                                  + "），省 " + str(len(_ctx)) + " 字")
                    # 2026-10-02 第482步（用户口径「新窗口还是白搭」）：
                    # **附件说明必须按实际挂了哪几份生成。**
                    # 原来 prompt 里贴的是固定文案（检查点/上下文/工具表/台账 四份），
                    # 而这一发可能只挂了台账 —— 窗口于是每轮去找那四个找不到的文件，
                    # 连续几十轮空转。这就是「新窗口也白搭」的真因：跟窗口新旧无关。
                    # 用**真实变量** _cp（L9123 取的就是它）——
                    # 我第一版写成 _cp_on_this_round，那个名字根本不存在，
                    # 一跑就是 NameError。这类错今天已经犯过三次，记着。
                    # 2026-10-03 修：这里原来写 "检查点"，而 attach_note_for
                    # 判断的是 "快照"（实际上传用的也是 "快照"）—— 一字之差，
                    # 说明里那一行**永远生成不出来**。
                    # 实测：_relay_prompts.jsonl 整档含「本次请求的附件」103 次，
                    # 含「快照.txt」**0 次** —— 模型根本不知道有快照附件，
                    # 只能自己 glob/read 去找，找不到就重复调用。
                    # 2026-10-03：快照已改为内联文本，不再是附件，
                    # 所以这里**不再把「快照」算进附件名单** —— 否则说明里
                    # 会写「快照.txt」，而根本没有这个文件，模型又要去找。
                    _sent = [lbl for lbl, _t in _docs]
                    _an_now = attach_note_for(_sent)
                    if _an_now and self.attach_note_for_send():
                        # 把固定那份说明换成按实际生成的。
                        # 只在**确实含有那份固定文案**时才换，避免误伤。
                        _fixed = self.attach_note_for_send()
                        if _fixed in prompt:
                            prompt = prompt.replace(_fixed, _an_now)
                    self.note("  附件实挂：" + ("/".join(_sent) or "无")
                              + "；说明已按实际生成")
                    for _lab, _txt in _docs:
                        _aid = self._upload_attach(_lab, _txt)
                        if _aid:
                            file_ids = list(file_ids) + [_aid]
                # 2026-10-03：快照的内联已移到 plan() 的 _wh() ——
                # 那里才是 prompt 组装的地方，在这儿硬拼只能等上一发写完、
                # 这一发读（实测 102 次写只 10 次读到）。留个空壳不做事，
                # 免得 `_cp` 变量被下面的引用判成未定义。
                _ = _cp
                try:
                    text, sid, mid = self._ask(
                        prompt=prompt, session=session, parent=parent,
                        thinking=thinking, search=search, quiet=True,
                        file_ids=file_ids,
                        on_delta=watch, stop=stop)
                except Exception as exc:             # noqa: BLE001
                    if isinstance(exc, RequestTooLargeError):
                        # Phase A / 2026-09-30：体积超限是「这一发不能发」，
                        # 跟会话在不在毫无关系。换新会话重发只会用同样的
                        # prompt 再撞一次闸门。直接上抛，让客户端看到明确错误。
                        raise
                    if session is None or is_rate_limited(exc) \
                            or not session_gone(exc):
                        # 限流时再开新会话重发是雪上加霜；传输故障则会话还在，
                        # 留着记录，下一条消息接着发增量就行
                        raise
                    # 落盘的会话可能已经被删了（比如在 GUI 里点过清空），
                    # 那就把它踢出表、当新会话重来一次
                    self.note(f"  续会话 {session} 失败（{exc}），改开新会话重试")

                    self.cache.forget(session)
                    keys, _s, _p, prompt, images = self.plan(
                        model, messages, tools, attach=_attach)
                    # 这里 _pace 等的是「距上一轮结束满 min_interval」。
                    # last_call 不再在这里重记 —— 记在 gate 块末尾。
                    self._pace()
                    text, sid, mid = self._ask(
                        prompt=prompt, session=None, parent=None,
                        thinking=thinking, search=search, quiet=True,
                        file_ids=file_ids,
                        on_delta=watch, stop=stop)

            except Exception as exc:                 # noqa: BLE001
                self.fails += 1
                self.last_error = f"{type(exc).__name__}: {exc}"
                if is_rate_limited(exc):
                    # 图片上传/plan 这条路绕过了 _ask 的退避循环，不设冷却的话
                    # 用户连点「继续」就是连着撞限流。
                    self.cool_until = time.time() + self.COOLDOWN
                raise
            else:
                self.last_error = ""
                self.pending_checkpoint = ""   # 检查点这轮真发出去了才清空；失败留着下轮重发
                # 只思考不落笔 —— 深度思考档的典型失败：整轮预算烧在推理上，
                # 正文一个字不出（实测 113 连续 4 轮 outputTokens=0、chars=0）。
                # 同一会话关掉 thinking 立刻出正文 + 真工具调用（2434 字）。
                # 重试放在这里、而不是在 Handler 层递归调 run()，是为了让记账只走
                # 一遍：递归会让 turns_here 一轮涨两格、turn 事件和回复档案出双份。
                # 关掉：_empty_policy.json 里写 {"no_think_retry": false}。
                # 只思考不落笔有两个形态：正文全空，或正文只剩「空参数的工具调用」
                # （代码全写进思考里了）。后者正文不为空，原来漏掉没重发。
                _c0 = split_calls(text or "", tools)[0] if tools else None
                _only_empty_call = bool(_c0) and all(
                    (c.get("function") or {}).get("arguments") in ("{}", "")
                    for c in _c0)
                if (not no_think
                        and (not (text or "").strip() or _only_empty_call)
                        and "THINKING" in seen
                        and empty_policy(getattr(self, "slug", "")).get(
                            "no_think_retry", True)
                        and not (stop and stop())):
                    try:
                        self.note("  ⟳ 只思考不落笔，关掉思考重发一次")
                        t2, s2, m2 = self._ask(
                            prompt=prompt + pget("nudge_body", NUDGE_BODY),
                            session=sid, parent=mid,
                            thinking=False, search=search, quiet=True,
                            file_ids=file_ids, on_delta=watch, stop=stop)
                        # 2026-09-22 修（用户要求：把逻辑补完，而不是关掉兜底）。
                        # 关掉思考之后模型没有推理通道，**规划会从正文出来**（实测：
                        # "The user wants me to continue. Let me just do the work…"）。
                        # 原来只要非空就收 —— 于是那段草稿被当成助手的正式回复转给 dsh。
                        # 现在要求这一发**真的带工具调用**才收：关思考那一发是「重做」，
                        # 不是「收尾」；在 dsh 的 agent loop 里，给了工具表却没有工具调用
                        # 就等于这一轮没干活。不带调用的一律不收，让它落到空回复分支
                        # 去走冷却/换号，而不是把内心独白冒充成答复。
                        _t2c = split_calls(t2, tools)[0]
                        if (t2 or "").strip() and (_t2c or not tools):
                            self.note("  ⟳ 重发拿到 " + str(len(t2)) + " 字正文"
                                      + ("（带工具调用）" if _t2c else "（本轮无工具表）"))
                            text, sid, mid = t2, s2, m2
                        elif (t2 or "").strip():
                            self.note("  ⟳ 重发只有 " + str(len(t2))
                                      + " 字正文、没有工具调用 —— 判为规划的草稿，不收。"
                                      "这一轮落到空回复分支（冷却/换号）")
                            self.last_empty = self.last_empty or "keep"
                    except Exception as exc:             # noqa: BLE001
                        self.note("  ⟳ 关掉思考重发失败："
                                  + type(exc).__name__ + ": " + str(exc)[:200])

                # 2026-09-22 加（用户要求）：**返回非空但没有工具调用** —— 同样是「这一轮没干活」。
                # dsh 收到没有 tool-call 块的回复会直接判 completed（dsh-agent-loop/lib/index.js:1117），
                # 把那段文本当最终答复显示，任务当场停住 —— 这就是「窗口坏掉」的机制。
                # 实测这类（给了工具却写散文）占 326/342，是主类；其中一部分是模型把自己的规划
                # 写进了正文（"The user wants me to continue. Let me just do the work…"）。
                # 这里补一道闸：贴 NUDGE_BODY 重发一次；仍无调用才放行（可能模型是真答完了）。
                # 关掉：_empty_policy.json 里写 {"no_call_retry": false}。
                # 2026-10-01 第469步（多维度实测）：**极短答复不要重发。**
                #
                # 实测 10-01 全天 594 次触发、只 68 次拿到工具调用 —— **命中率 11.4%**，
                # 也就是说 526 次重发是**整发白烧**（每次都是一发的 token）。
                # 按正文字数分桶后信号很清楚：
                #     0-50 字    16 次   命中  0.0%   <- 纯浪费，且这是唯一 0% 的桶
                #    50-150 字   11 次   命中 54.5%   <- 很有效
                #   150-400 字  146 次   命中 13.7%
                #   400-1000 字 234 次   命中  7.3%   <- 主要浪费区
                #  1000-3000 字 115 次   命中  7.8%
                #   3000+ 字     28 次   命中 42.9%   <- 也有效
                # 两头有效、中间那块是浪费，但**中间那块量太大（349 次）不能一刀切**
                # —— 那会连带砍掉真能救回来的活。所以这里只切**唯一 0% 的那一桶**：
                # 正文 ≤ MIN_RETRY_CHARS 一律不重发，直接当答复放行。
                #
                # 为什么这一桶必然是浪费：模型回一句 4 字的短答复（实测「链路正常」）
                # 说明它**已经答完了**，不是「想干活但格式错了」。再贴 NUDGE 催它，
                # 它只会再回一句同样短的 —— 实测 22:12~22:13 三轮，每轮把 12698 字的
                # 请求发两遍，就为换回同一句「链路正常」，`n` 从 8 涨到 18。
                # 真实流量里这种「用户就是想要一句话」的询问占相当比例。
                #
                # 阈值选 50（实测桶边界）而不是拍脑袋：50 字以下命中率 0/16。
                # 想改：_empty_policy.json 里写 {"no_call_retry_min_chars": N}。
                _nc_min = 50
                try:
                    _nc_min = int(empty_policy(getattr(self, "slug", "")).get(
                        "no_call_retry_min_chars", 50) or 50)
                except (TypeError, ValueError):
                    _nc_min = 50
                if (not no_think and (text or "").strip() and tools
                        and not split_calls(text, tools)[0]
                        and empty_policy(getattr(self, "slug", "")).get(
                            "no_call_retry", True)
                        and len((text or "").strip()) > _nc_min
                        and not _looks_like_question(text)
                        and not (stop and stop())):
                    try:
                        self.note("  ⟳ 有正文但没有工具调用（" + str(len(text))
                                  + " 字），贴提醒重发一次")
                        t4, s4, m4 = self._ask(
                            prompt=prompt + pget("nudge_body", NUDGE_BODY),
                            session=sid, parent=mid,
                            thinking=thinking, search=search, quiet=True,
                            file_ids=file_ids, on_delta=watch, stop=stop)
                        if split_calls(t4 or "", tools)[0]:
                            self.note("  ⟳ 重发拿到工具调用（" + str(len(t4 or "")) + " 字）")
                            text, sid, mid = t4, s4, m4
                        elif (t4 or "").strip():
                            self.note("  ⟳ 重发仍无工具调用（" + str(len(t4))
                                      + " 字）—— 当答复放行")
                            text, sid, mid = t4, s4, m4
                    except Exception as exc:             # noqa: BLE001
                        self.note("  ⟳ 无调用重发失败：" + type(exc).__name__
                                  + ": " + str(exc)[:200])

            # 回完计时：这一轮（含重试）结束的这一刻记 last_call。
            # 下一轮 _pace 等的是「距这刻满 min_interval」= 回完再歇。
            # 但只有这轮正文真带了工具调用（有代码要执行）才等。
            self.last_call = time.time()
            self.last_had_tool = bool(split_calls(text, tools)[0])

        # 客户端发来的完整上下文大小：空回复定性要用它，而不是我们实际
        # 发上去的那段增量（大上下文 + 小增量会被当成小请求）。
        whole = self.input_chars(messages)
        # 2026-10-01 第449步：**留下"进这一轮之前"的空回复计数。**
        #
        # 多维分析（4559 个 turn）发现 empty_streak 字段**恒为 0**：
        # 下面成功分支第一件事就是 self.empty_streak = 0（L6937），
        # 而 emit 在它之后 —— 记进去的永远是清零后的值。
        # 结果这个字段分析不出一件事：从没观察到"带着空回复历史又成功"的轮次，
        # 而那恰恰是「这个号刚从限流/回空里缓过来」的唯一信号。
        # 在清零之前抓一份，事件里同时给 before / after 两个数。
        _streak_before = int(getattr(self, "empty_streak", 0) or 0)
        if keys == self.UTIL_KEYS:
            self.cache.used_util(sid, mid)
        elif (text or "").strip() or "THINKING" in seen:
            # 只出思考不出正文也算这个会话还活着 —— 丢掉它下一轮就要重开窗口，
            # 而且要把几十万字上下文重传一遍，代价比留着大得多
            self._empties.pop(sid, None)
            self.last_empty = ""
            self.empty_streak = 0          # 通了就清零，冷却档位退回最低
            self.empty_at = 0.0
            # 一次真实交互 = 1 条（重试/接续都在同一轮 run() 里，不额外涨）。
            # 2026-09-24 改：压缩不再特殊对待 —— 桥不再识别压缩调用，所有
            # 走到这里的轮次都算一条。原来压缩轮不占配额（「压缩调用不占轮询
            # 配额」），但那个判据本身是错的（把桥自己的检查点指令认成了压缩），
            # 结果是该计的没计、计数全乱。现在一律计。
            self.turns_here += 1
            self.cache.remember(keys, sid, mid)
            emit("turn", slug=getattr(self, "slug", ""),
                 model=model,
                 turns_here=self.turns_here, sid=sid,
                 chars=len(text or ""), fragments=sorted(seen),
                 empty_streak=self.empty_streak,
                 # 进这一轮之前连空了几次（0 = 上一轮是好的）。
                 # 见 _streak_before 的注释：empty_streak 本身在成功路径被清零，
                 # 只有这个 before 值能反映"刚从坑里爬出来"。
                 streak_before=_streak_before,
                 prompt=len(prompt), images=len(images),
                 think=think_chars[0],
                 # 工具命中数：这一轮正文里解析出几个工具调用。0 = 没产出动作。
                 # 观测台拿它算「工具命中率」（2026-09-22 加）。
                 tcalls=len(split_calls(text, tools)[0] or []),
                 head=rp_head(text), tail=rp_tail(text),
                 # 2026-10-02：**计「打转」的账**（用户口径「一直打转」）。
                 #
                 # 实测：窗口 40% 的轮次是纯侦察（只 read/glob/job_list，
                 # 不产出任何东西），而且它会**读懂台账、复述出下一步、
                 # 然后回头再读一遍台账**（实例 20:59:06 说对了下一步，
                 # 20:59:19 又「re-establish the actual state」）。
                 # 光写规则没用 —— 它已经证明会无视规则。所以要有个
                 # **桥自己数的计数**，供下一步注入「别读了，去干活」。
                 # 2026-10-02：**在正文还完整的这一刻**把「在干什么」算好。
                 # 交接的「最后一步」原来拿 head400+tail200 现拼，实测
                 # 95.3% 判成工具调用、669 轮只剩「调了 xxx」，还有 1253 轮
                 # head/tail 本就是同一个 blob 截了两次。根因是按字符截、
                 # 不认语义 —— 这里存一份算好的人话，交接直接取。
                 lastact=rp_act(text))
            emit_reply("turn", getattr(self, "slug", ""), sid, text,
                       think=think_chars[0],
                       extra={"turns_here": self.turns_here,
                              "prompt": len(prompt),
                              "whole": whole,
                              "images": len(images),
                              "fragments": sorted(seen),
                              "ask_head": (prompt or "")[:300]})
            # 2026-10-01 第451步：**在产生这一刻抽结论句。**
            #
            # 用户问「想想你自己怎么工作的 该用什么算法能模仿你」。
            # 我是每轮拿当前上下文重新推理 —— 上下文是工作台不是记忆。
            # 工作台上「扫一眼就生效」的句子最值钱（坑、禁令、堵死的路）。
            #
            # 为什么必须在这里抽：events 只留 head 400 + tail 200 字，
            # 是按字符截的，事后拿碎片找完整句子必然是碎片
            # （实测 7142 条候选里祈使句只有 1 条）。此刻 text 是完整正文。
            try:
                _n = conc_save(getattr(self, "slug", ""), sid, text)
                if _n:
                    self.note(f"  ▤ 结论句 {_n} 条入库")
            except BaseException:            # noqa: BLE001
                pass
        else:
            # 空回复分两种，处理方式完全不同：
            #
            # 「这一发太大了」：几十万字 + 一堆图，上游直接不回。丢掉会话的话，
            # 下一轮 match 不上 → 整段 transcript 重发 → 更大 → 又空 → 又丢，
            # DeepSeek 里于是每轮多一个新窗口。实测 020 账号一次 245K 字 + 6 图
            # 连炸三轮，窗口列表瞬间多出一堆。所以这种保留会话，下一轮只发增量。
            #
            # 「会话真废了」：小请求也照样空（实测 session 07242a33 被一个 13 万字
            # + 5 图打空之后，后面 477 字、79 字两轮全空）。这种才该丢掉重开。
            #
            # 同一个会话连着空两次就按「真废了」处理，免得保留策略自己陷进去。
            times = self._empties.get(sid, 0) + 1
            self._empties[sid] = times
            # ===== 2026-10-01 第445步：先核实「这个窗口还在不在」=====
            #
            # 病根（用户实测「每轮空 7 秒 × N 次」）：**上游删掉窗口后不报错，
            # 只返回静默空。** 而下面所有分支都假设「空 = 太大 / 太频繁 / 会话废了」，
            # 没有一条去问「它是不是压根不存在了」——
            #   · session_gone(exc) 只在**抛异常**时才有机会跑，静默空不抛；
            #   · 于是 cache.forget() 走不到，行一直在；
            #   · 下一轮 match() 又匹上这个死窗口，继续发、继续空。
            # 实测 19:16 [483] 对着已删除的 73e2ea30 连空 6 次、每次 7 秒。
            #
            # 判据刻意保守：**只在连着空第 2 次时才去查**（一次网络抖动不至于
            # 触发），查一次 list_sessions，确认这个 sid 不在表里就 forget +
            # 清计数，下一轮自然开新窗口。查不到（网络问题）就什么都不做。
            if times >= 2 and sid:
                try:
                    _alive = self.ds.session_alive(sid)
                    if _alive is False:
                        self.cache.forget(sid)
                        self._empties.pop(sid, None)
                        self.note(f"  ⊘ 会话 {sid[:8]} 在上游已不存在"
                                  f"（连空 {times} 次），丢弃记录，下一轮重开")
                        self.last_empty = "gone"
                        self.empty_streak += 1
                        self.empty_at = time.time()
                        emit_reply("empty_gone", getattr(self, "slug", ""), sid, "",
                                   think=think_chars[0],
                                   extra={"times": times, "prompt": len(prompt),
                                          "whole": whole, "reason": "session_deleted"})
                        return "", sid, mid, {"prompt": dsh_tokens(whole),
                                              "completion": 0, "cached": 0, "refs": []}
                except Exception:            # noqa: BLE001
                    pass
            # 2026-09-24 加（用户要求）：**不进轮询池的号**（界面上取消了「入池」），
            # 同一个会话连着空到 EMPTY_DROP_FIXED 次就判定会话已废 —— 直接丢掉，
            # 下一轮 match 不上就自动开新会话。
            #
            # 为什么只对不轮询的号这么做：轮询号重开会话代价大（要整段重传几十
            # 万字），而它下一轮本来就会换个号接着干；固定号是长期扛同一份活的，
            # 它陷在废会话里就是死循环，没人来救它。
            #
            # 实测案例（2026-09-24 16:00）：020 号（已停用）复用会话 eb03ff41，
            # 累计 2595 条指纹 / 上游 54 万 token，上游对这个历史一律回空；而下面
            # 两条分支**都**调 cache.remember() 保留会话，没有一条丢会话重开 ——
            # 于是 15s→30s→60s 冷却无限循环，用户看到的就是「桥把我拦住了」。
            # 更糟的是 dropped 分支会把 _empties 清空，times 永远回到 1，
            # 连 keep_max_times 那道兜底都翻不了身。
            if not self.enabled:
                _drop_at = self.EMPTY_DROP_FIXED
                if times >= _drop_at:
                    self.cache.forget(sid)
                    self._empties.pop(sid, None)
                    self.empty_streak += 1
                    self.last_empty = "dropped"
                    self.note(f"  ⊘ 会话 {sid[:8]} 连续空 {times} 次，"
                              f"本号不在轮询池 —— 判定已废，丢弃，下一轮开新会话")
                    emit_reply("empty_drop", getattr(self, "slug", ""), sid, "",
                               think=think_chars[0],
                               extra={"times": times, "prompt": len(prompt),
                                      "whole": whole, "reason": "fixed_no_rotate"})
                    # 不抛异常：run() 返回空文本，调用方（L4508/L4668）本来就把
                    # 空文本当「频繁限流」报 429 给 dsh；而会话已经 forget 了，
                    # dsh 重试进来时 match 不上 → 自动开新会话。
                    _u = {"prompt": dsh_tokens(whole), "completion": 0,
                          # 2026-09-26 第180步：与第173步同源。原注释说「会算两遍」不成立 ——
                          # pi-ai openai-completions.js:1193 是减法，pressureFrom 恒等于真实上下文。
                          "cached": max(0, dsh_tokens(whole) - dsh_tokens(len(prompt or ""))),
                          "refs": []}
                    return "", sid, mid, _u
            # 桥接自己拉长冷却：dsh 拿到 429 会很快重试进来，不在这儿垫一段
            # 的话它就是立刻又打上游，还是空，连着空到底。
            self.empty_streak += 1
            self.empty_at = time.time()
            cool = self.empty_cooldown()
            self.cool_until = max(self.cool_until, time.time() + cool)
            self.note(f"  连续空回复第 {self.empty_streak} 次，"
                      f"冷却 {cool:.0f}s（dsh 重试进来会先等这一段）")
            pol = empty_policy(getattr(self, "slug", ""))
            if not pol.get("enabled", True):
                pol = dict(POLICY_DEFAULTS)
            keep_chars = int(pol.get("keep_chars") or 0)
            keep_max = int(pol.get("keep_max_times") or 0)
            # 「这一发多大」按**客户端发来的完整上下文**算，不是按我们实际发上去
            # 的那段增量。用增量会把「大上下文 + 小增量」判成小请求 → 判成限流 →
            # 报 429 让 dsh 退避 —— 而退避治不了大请求，它原样重发、又空，闭环。
            # 实测两次假限流就是这么来的（客户端 183323 / 205325 字，实际只发上去
            # 81259 / 11150 字）。
            oversize = keep_chars > 0 and whole > keep_chars
            if pol.get("keep_images", True) and images:
                oversize = True
            das = int(pol.get("drop_after_streak") or 0)
            if das > 0 and self.empty_streak >= das:
                oversize = False
            # keep_max_times 只用来兜「压缩也救不回来」的死循环，所以留几次余地：
            # 只空一两次不算。否则大请求的首次空回复就被一刀切成「限流」。
            if oversize and keep_max > 0 and times > keep_max + 2:
                oversize = False
            if oversize:
                Bridge.EMPTY_KEEP += 1
                self.last_empty = "keep"       # 值得让 dsh 压一次再试
                self.cache.remember(keys, sid, mid)
                self.note(f"  空回复，但这一发很大（客户端 {whole} 字 / "
                          f"本次只发 {len(prompt)} 字 / {len(images)} 图），"
                          f"保留会话 {sid}，下一轮只发增量，别重开窗口")
                emit_reply("empty", getattr(self, "slug", ""), sid, "",
                      think=think_chars[0],
                      extra={"verdict": self.last_empty,
                             "prompt": len(prompt),
                             "whole": whole,
                             "images": len(images),
                             "streak": self.empty_streak,
                             "times": times,
                             "fragments": sorted(seen)})
            else:
                Bridge.EMPTY_DROPPED += 1
                self.last_empty = "dropped"      # 频繁限流：保留会话，下一轮只发增量
                self._empties.pop(sid, None)
                self.cache.remember(keys, sid, mid)
                self.note(f"  一个片段都没有（第 {times} 次），按频繁限流保留会话 {sid}，"
                          f"下一轮只发增量，不重开窗口")
                emit_reply("empty", getattr(self, "slug", ""), sid, "",
                      think=think_chars[0],
                      extra={"verdict": self.last_empty,
                             "prompt": len(prompt),
                             "whole": whole,
                             "images": len(images),
                             "streak": self.empty_streak,
                             "times": times,
                             "fragments": sorted(seen)})


        refs = getattr(self.ds, "last_refs", None) or []
        text = cite_links(text or "", refs) + sources_block(refs)


        # 客户端想看的是「这轮上下文多大」，所以按它发来的完整 messages 算，
        # 而不是我们实际只发上去的那一小段增量。两者的差额就是靠会话复用省下的
        # 重传量，正好塞进 cached_tokens —— 客户端的「缓存命中率」就有数了。
        #
        # token 一律按 dsh 的口径（4 字符/token）折算，不用我们自己那套
        # CJK=1 的估法：dsh 的用量条、上下文表、contextWindow（= 输入上限/4）
        # 全是这把尺子，报不同口径的话它那条「用量 xxK tok」跟上限对不上，
        # pi-ai 还会拿 usage 反推 overflow，数字偏大就会误判。
        whole_chars = whole
        self.last_chars = whole_chars
        self.peak_chars = max(self.peak_chars, whole_chars)
        whole = dsh_tokens(whole_chars)
        # 2026-10-03（用户口径「直接 id 就直通」「@309 → 直通,找「309」窗口,
        # 发过去,回过来。就这些」「不止 309 所有固定号都是这个规则」）：
        # **直通这一发,usage 按实发量报,不报客户端全量。**
        #
        # 为什么：直通的语义就是「桥不管」—— 原样发、原样回。
        # 而报 whole（客户端全量）是**管制动作**：它本来是给 dsh 的用量条/
        # 压缩判断用的仪表，属于分组那套（要算缺口、要压上下文才需要知道全量）。
        # 对直通来说，桥既不算缺口也不压上下文，没有任何理由去编一个
        # 「客户端一共多少字」的数报出去。
        #
        # 实证（_out_probe.log 抓到出口帧，探针已撤）：直通那一发报的是
        #     prompt_tokens = 4,285,316   cached = 4,280,628
        # 而 pi-ai 的判据（overflow.js:140-145 + openai-completions.js）
        #     input = max(0, P - C) → inputTokens = input + cacheRead = max(P, C)
        #     if (inputTokens > contextWindow) → CONTEXT_WINDOW_EXCEEDED
        # 428 万 >> 131072 → **每一轮都被判死、那一轮不落盘、界面空白**。
        # 而桥实际只发了 1.7 万字（续会话增量），上游侧毫无问题。
        #
        # 直通报实发量之后：prompt = 实发 tok，cached = 0，
        # 判据变成实发量（实测最大 2.5 万 << 13 万），不再误判。
        _direct = bool(getattr(self, "direct", False))
        if _direct:
            _sent_tok = dsh_tokens(len(prompt or ""))
            usage = {
                "prompt": _sent_tok,
                "completion": dsh_tokens(len(text or "")),
                "cached": 0,
                "refs": refs,
            }
        else:
            usage = {
                "prompt": whole,
                "completion": dsh_tokens(len(text or "")),
                # 2026-09-26 第173步：改回报真实缓存量（推翻 2026-09-24 的「不再虚报」）。
                # 复核 pi-ai 真实代码 openai-completions.js:1193：
                #   input = Math.max(0, prompt_tokens - cacheRead - cacheWrite)
                # 它是**减法** —— 报多少 cached 就从 input 里扣掉多少，于是
                #   input + cacheRead 恒等于 prompt_tokens，
                #   pressureFrom 恒等于真实上下文，报多少 cached 都不会算两遍。
                # 把 pi-ai 的真函数抽出来实跑：cached=49629 与 cached=0 两种写法，
                #   pressureFrom 都 = 49824（= whole），差 0。
                # 原推导「pressure ≈ 2 × whole/4」是误读了那组数字，不成立。
                # 现在报回 whole 与实发的差额，只影响面板命中率，压力/压缩行为不变。
                #
                # 2026-10-03：**这一段只对分组（非直通）有效。**
                # 直通走上面的 if 分支 —— 报 whole 会让 pi-ai 判 CONTEXT_WINDOW_EXCEEDED
                # （它做 max(P,C)，报全量必超 131072），直通因此每轮都被判死。
                "cached": max(0, whole - dsh_tokens(len(prompt or ""))),
                "refs": refs,
            }

        # 上游自己报的仪表（2026-09-22 加）：
        #   last_tokens = accumulated_token_usage，整轮 prompt+completion
        #                 的真实 token 数。原来它掉进 unhandled 被扔掉 ——
        #                 这是唯一能测出「上游到底在哪一刀断的」的仪表。
        #   last_finish = quasi_status，正常永远 FINISHED；一旦是别的值，
        #                 就是上游主动断了这一轮 —— 那是实证，不是猜的。
        up_tok = getattr(self.ds, "last_tokens", None)
        up_fin = getattr(self.ds, "last_finish", None)
        # 先留一份上一轮的累计值，好算这一轮的增量（2026-09-22 加）
        prev_up = getattr(self, "last_up_tokens", None)
        prev_sid = getattr(self, "last_up_sid", None)
        self.last_up_tokens = up_tok
        self.last_up_finish = up_fin
        self.last_up_prompt = len(prompt)
        self.last_up_sid = sid
        # 这一轮真实烧掉的 token = 本轮累计 - 上轮累计。
        # 上游一旦因为长度断流，最后一次 ⤴ 的增量就是「多长会断」的实证。
        # 新会话会让计数回零，那时增量为负 —— 记成 None，别当数看。
        #
        # 2026-10-01 第422步（用户口径「缓存命中率如何优化」「命中率有点低啊」）：
        # **必须同时比对 sid**。up_tok 是**上游会话**的累计量，换一个上游会话
        # 就从头计；而 prev_up 存的是桥实例上一次的值 —— 两者不在同一坐标系。
        # 只做减法就会把「新会话的总量」减去「旧会话的总量」，凭空造出天文数字。
        # 实证（.state/ds_bridge.log，10341 条 ⤴⤴ 行）：
        #   192 条 >100000 tok，最大 **839645 tok**；
        #   典型 2026-10-01 10:27:55 [309]：只发 10424 字，却报「本轮实际 599930 tok」。
        # 危害不止是日志难看：下面 L5755 直接拿它覆盖 usage["completion"]，
        # dsh 收到伪造的 completion_tokens -> 上下文压力条被灌爆 -> 提前触发
        # 压缩 -> 上游会话被重建 -> 会话复用失效 -> 命中率归零。
        self.last_up_delta = ((up_tok - prev_up)
                              if (up_tok and prev_up and up_tok >= prev_up
                                  and sid and prev_sid and sid == prev_sid)
                              else None)
        # A 口径（输出吞吐）：上游真实 token 增量 ≈ 本轮输出（prompt 增量相对很小），
        # 用它覆盖 4字符/token 的估算，让 dsh 的生成速度是准的。
        # 2026-10-01 第422步续：再加一道**物理上限**兜底。sid 相同也不代表
        # 上游语义没变（上游可能内部轮转、或 quasi_status 断流后重开而 sid 未变）。
        # 这一轮发上去的 prompt 是 len(prompt) 字，上游不可能凭它产出超过
        # 「prompt + 输出上限」的增量。超过这个量的 delta 一律判定为坏值，
        # 丢掉、退回 4 字符/token 的估算 —— 宁可不精确，也不能把 dsh 的压力条
        # 灌爆（宁可少报，不能虚报；虚报会触发压缩，压缩才是真正丢上下文的元凶）。
        _cap = dsh_tokens(len(prompt or "")) + 200000
        if (isinstance(self.last_up_delta, int) and self.last_up_delta > 0
                and self.last_up_delta <= _cap):
            usage["completion"] = self.last_up_delta
        elif isinstance(self.last_up_delta, int) and self.last_up_delta > _cap:
            self.note("  ⚠ 上游 token 增量 " + str(self.last_up_delta)
                      + " 超过本轮合理上限 " + str(_cap)
                      + "（prompt " + str(len(prompt or "")) + " 字）"
                      + "，判定为跨会话坏值，已丢弃，改用估算法")
            self.last_up_delta = None
        if up_fin and up_fin != "FINISHED":
            self.note("  ⚠⚠ 上游不是正常结束：quasi_status="
                      + str(up_fin)
                      + " | prompt=" + str(len(prompt)) + " 字"
                      + " | 上游计 " + str(up_tok) + " tok"
                      + " | 这是上游主动断的实证，不是 HANDOFF_MAX 砍的")
            emit("upfin", slug=getattr(self, "slug", ""),
                 fin=str(up_fin), tokens=up_tok, prompt=len(prompt),
                 chars=len(text or ""), model=model)



        if text.strip():
            # 留一份给 local_handoff()：老号被怼到交不出交接时，这段就是它
            # 最终的进度陈述（实测 2/15 换号会走到那条路）。
            #
            # 2026-09-30 修（用户口径「轮询不可能天天丢数据」「怕丢失导致 AI
            # 判断不准」）：这里原来存的是**原始未清洗文本**。实测全库存 9592 条
            # turn 里 89.3% 开头就是工具调用块，于是换号交接拿到的常是 run_code
            # 脚本源码 / present 的 JSON，而不是「做到哪、下一步是什么」。
            # 接手方**以为自己拿到了交接**，实际拿到噪音 —— 比空交接更危险。
            # 现在先剥壳：留下工具块**前面**那句真正的结论。
            _t = strip_tool_residue(text.strip())
            if _t and not looks_like_tool_residue(text):
                self.last_reply = _t
                self.last_reply_at = time.time()
            elif not self.last_reply:
                # 从来没存过任何一个字：宁可留个明确的占位，也不要空。
                _raw = text.strip()
                self.last_reply = (
                    f"【上一棒只执行了工具，没留下结论正文。"
                    f"原始输出 {len(_raw)} 字，开头："
                    f"{_raw[:200]}】")
                self.last_reply_at = time.time()
                self.note("  ⚠ 本轮正文是纯工具调用（剥壳后不足 "
                          f"{RESIDUE_MIN_CHARS} 字），未覆盖 last_reply")
            else:
                self.note("  ⚠ 本轮正文是纯工具调用（剥壳后不足 "
                          f"{RESIDUE_MIN_CHARS} 字），保留上一次真结论")
            # 压缩检查点单独留一份，交接时优先用它（比普通回复全得多）
            if _t.startswith(CHECKPOINT_HEAD) or text.strip().startswith(CHECKPOINT_HEAD):
                self.last_checkpoint = _t
                self.last_checkpoint_at = time.time()
                # PATched 2026-10-03: pending_checkpoint 从来没有写入点 —— 挂载点
                # run() 的 `if _cp:` 因此永不成立，快照.txt 一次都没上传过
                # （实测「附件实挂：台账」4437 次、「快照」0 次）。
                # last_checkpoint 只喂交接单，不喂附件。两份都写。
                self.pending_checkpoint = _t
                # 2026-09-24 改：压缩统计整块拆掉（桥不再识别压缩调用）。
                # 正文是 8 段快照就一律当检查点收 —— 具体是谁发的压缩指令
                # 桥不管是，dsh 自己管压缩。
                self.note(f"  ↳ 收到 8 段式快照（{len(_t)} 字），当检查点留着")
            self.note(f"← {len(text)} 字 ≈{usage['completion']} token"
                      f" | {text.strip()[:80]!r}")
            if up_tok or (up_fin and up_fin != "FINISHED"):
                self.note("  ⤴ 上游计 " + str(up_tok)
                          + " tok"
                          + ("，结束=" + str(up_fin)
                             if up_fin and up_fin != "FINISHED"
                             else ""))
                if self.last_up_delta is not None:
                    self.note("  ⤴⤴ 本轮实际 "
                              + str(self.last_up_delta) + " tok"
                              + "（累计 " + str(up_tok) + "）")
        else:
            # 空回复是 dsh 那边报 EMPTY_RESPONSE 的直接原因，多记点线索
            self.note(f"← 无正文！session={sid} prompt={len(prompt)} 字 "
                      f"search={search} thinking={thinking} 片段={sorted(seen)}")
        return text, sid, mid, usage




# ============================== 账号池 ==============================

def _slugify(name, taken, token=""):
    """账号名 → 模型名里能用的 ASCII 后缀（deepseek-chat@xxx 的 xxx）。

    纯中文名会被剃成空，那就退回 token 哈希 —— 不用序号，因为序号会随
    ds_auth.json 里的顺序变，一变 dsh 里钉住的模型名就全指错人了。
    """
    base = re.sub(r"[^a-z0-9]+", "-", (name or "").lower()).strip("-")
    if not base:
        base = "a" + hashlib.sha1(token.encode()).hexdigest()[:6] if token \
            else "acct"
    slug, n = base, 1
    while slug in taken:
        n += 1
        slug = f"{base}{n}"
    return slug



# 2026-10-03：把配置里的提示词灌进 PROMPTS 表。
# 「空串 = 回落到厂值」，所以 ini 里没写这些键时行为与改造前完全一致。
_PROMPT_FACTORY = {
    "tool_protocol": TOOL_PROTOCOL,
    "checkpoint_instruction": CHECKPOINT_INSTRUCTION,
    "salvage_note": SALVAGE_NOTE,
    "ctx_empty_note": CTX_EMPTY_NOTE,
    "nudge_body": NUDGE_BODY,
    "attach_note": ATTACH_NOTE,
    "attach_note_no_cp": ATTACH_NOTE_NO_CP,
    "attach_note_local": ATTACH_NOTE_LOCAL,
    "attach_ctx_lines": ATTACH_CTX_LINES,
    "standing_note": CHUNK_REMINDER,
    "pool_note": POOL_NOTE_DEFAULT,
    "format_note": "",
    "handoff_note": HANDOFF_PROMPT,
}


def _sync_prompts(conf):
    """conf（Pool.conf）-> PROMPTS。取不到的键保留厂值。"""
    try:
        for _k, _fac in _PROMPT_FACTORY.items():
            pset(_k, (conf or {}).get(_k), _fac)
    except BaseException:            # noqa: BLE001
        pass


class Pool:
    """一组账号，每个账号一个自己的 Bridge。

    为什么不是「一个 Bridge 管多个账号」：限流、冷却、会话表全是按账号算的，
    Bridge 本来就是「一个账号的状态机」。池子只负责挑人。

    挑人规则（按优先级）：
      1. 模型名带 `@账号` 后缀就钉住那个账号，不管它忙不忙；
      2. 否则看谁认得这段对话（DeepSeek 的 session id 属于具体账号，
         聊到一半换号等于换了个空白上下文）；
      3. 谁都不认就挑最闲的：没在干活的优先，然后是冷却结束最早、干得最少的。

    加账号不用改任何配置：GUI 存完 ds_auth.json 调一次 reload()，
    池化模型名（deepseek-chat 那四个）立刻就会用上新账号。
    """

    SEP = "@"

    # 两条线，各管一件事，互不牵连：
    #   windows —— 告诉 dsh 的上下文窗口，决定它**主动**压缩的时机。
    #              默认 0 = 不插手，用 dsh 自己那套（131072 → 阈值约 42 万字符，
    #              实际等于基本不压）。写小了它会压得很勤，历史被摘要替换掉，
    #              细节会丢，所以这条默认不动，要压再自己填。
    #   limits  —— 桥接硬拦的红线，默认 0（不拦）。它会误伤本来能跑的请求，
    #              所以只在明确想画红线时才填；平时靠「上游返回空 → 报 overflow」
    #              这条反馈来驱动压缩，上游自己说不行才动作，不用猜阈值。
    DEFAULT_WINDOW_CHARS = 0
    DEFAULT_LIMIT_CHARS = 0


    def __init__(self, accounts=None, min_interval=3.0, log=True, disabled=()):
        self.log = log
        self.lock = threading.RLock()
        self.bridges = {}
        # 每个账号单独的限流数值（slug → {min_interval/COOLDOWN/RATE_BACKOFF}）；
        # 没有条目就跟随 conf 里的全局默认。
        self.overrides = {}
        # 账号轮询游标：谁刚回了空就把通道让给下一个，走到头再从第一个开始。
        # 顺序就是 self.bridges 的插入顺序，也就是 ds_auth.json 里的次序。
        #
        # 2026-09-25 分组：游标/持号/时刻改成「一组一份」，存在 _rr 里，
        # 键是组 id。下面 rr_cursor / rr_active / rr_since 三个旧名字改成
        # property，读写「当前组」那一格 —— 这么做的原因是外部（_relay_get、
        # _relay_ctl、status、dsweb/state.py、GUI）全都按旧名字取，改成
        # 字典会把它们一起打断。property 让分组是加在下面的一层，不是掀桌子。
        # 没有 _pool_groups.json 时只有一个隐式组，行为和加分组之前一致。
        self._rr = {}
        # 当前轮到哪一组。空串 = 还没定，取组表第一个。
        self.grp_top = ""
        # 一个账号连着派满这么多条就让位给下一个；0 = 不限。
        # 跟「回空让位」共用同一条序列，换过去也接自己最近的会话。
        self.RR_TURN_LIMIT = 0.0
        # 刚换号时留下老号，do_POST 回来取一次做交接（取了就清）
        # 刚换号时留下老号，do_POST 回来取一次做交接（取了就清）。
        # 2026-09-21 改：按**接手账号**分槽（slug -> (老号, 换号时刻, 老号接手时刻)）。
        # 原来是全局单槽，两个窗口同时换号会互相把对方的交接顶掉。
        self.swap_pending = {}
        # 当前占着通道的那个号、以及它是什么时候拿到通道的 —— 现在是
        # 「当前组」那一格的 property（见类下面的 rr_active / rr_since），
        # 不再在 __init__ 里赋值。
        self._grp_sync()
        self.conf = {"min_interval": min_interval,
                     "COOLDOWN": Bridge.COOLDOWN,
                     "RATE_BACKOFF": Bridge.RATE_BACKOFF,
                     "standing_note": CHUNK_REMINDER,
        "attach_note": ATTACH_NOTE,
                     "pool_note": POOL_NOTE_DEFAULT,
                     "format_note": "",
                     "handoff_note": HANDOFF_PROMPT,
                     # 2026-10-03：原先写死在代码里、控制台改不到的那批提示词。
                     "tool_protocol": TOOL_PROTOCOL,
                     "checkpoint_instruction": CHECKPOINT_INSTRUCTION,
                     "salvage_note": SALVAGE_NOTE,
                     "ctx_empty_note": CTX_EMPTY_NOTE,
                     "nudge_body": NUDGE_BODY,
                     "attach_note_no_cp": ATTACH_NOTE_NO_CP,
                     "attach_note_local": ATTACH_NOTE_LOCAL,
                     "attach_ctx_lines": ATTACH_CTX_LINES,
                     "turn_limit": 0.0,
                     "limits": {m: self.DEFAULT_LIMIT_CHARS for m in MODELS},
                     "windows": {m: self.DEFAULT_WINDOW_CHARS for m in MODELS},
                     # 压缩比例：GUI 的 bridge/compact_* 覆盖，非法值退回默认
                     "compact_threshold_ratio": 0.8,
                     "compact_retain_ratio": 0.16,
                     # 重发全文时的字符预算：超了就只留尾部这么多字
                     "send_budget": 300000}
        # 2026-10-03：出厂那批提示词常量先灌一次，保证模块级函数在
        # 第一次 apply() 之前也有值可用。
        _sync_prompts(self.conf)
        self.reload(accounts, disabled)
        ds_api.set_pace_sec(self.conf.get("min_interval"))



    @classmethod
    def of(cls, ds, min_interval=3.0, log=True):
        """拿一个现成的 DeepSeek 实例开个单账号池（命令行 --account 走这条）。"""
        pool = cls([], min_interval, log)
        cfg = getattr(ds, "cfg", None) or {}
        name = cfg.get("name") or "账号"
        br = Bridge(ds, min_interval, log, name=name,
                    slug=_slugify(name, set(), cfg.get("token") or ""))
        # 2026-10-01 第429步：给 Bridge 一个回指池子的引用。
        # stress() 要用 Pool._limit_of() 才能拿到**真实**的轮次上限
        # （账号 > 组 > 池子三级回退）—— Bridge.turn_limit 只是第一层，
        # 实测恒为 0。没这个引用，那一项就是死代码。
        br.pool = pool
        pool._stamp(br)
        pool.bridges = {br.slug: br}
        return pool

    # ---------- 分组的读写口 ----------
    # 2026-09-25 加。外部代码（_relay_get / _relay_ctl / status / dsweb/state.py /
    # GUI）一律按 rr_active / rr_cursor / rr_since 三个旧名字取当前持号。这三个
    # 现在按组分开存，改成字典会把上面那一串全打断。所以旧名字保留成 property，
    # 读写都落在「当前组」那一格 —— 分组因此是加在下面的一层，不是掀桌子。

    def _groups(self):
        """此刻生效的组表（含隐式组）。成员取自池子里现有的账号。"""
        return pool_groups(list(self.bridges.keys()))

    def _gid(self, gid=None):
        """定出要操作哪一组：点名了就认，否则用当前组（grp_top）。

        点名的组不存在时退回当前组，不抛 —— 界面传了个过期的组 id 不该
        把整个 /relay 打成 500。
        """
        gs = self._groups()
        if not gs:
            return ""
        ids = [g["id"] for g in gs]
        if gid and gid in ids:
            return gid
        if self.grp_top and self.grp_top in ids:
            return self.grp_top
        # 还没定过组（或定过的组没了）：认配置里的 default，它要是也
        # 不在环上（被停用/没人）就用环首。这样「第一发从哪组开始」
        # 是可配的，而不是碰运气看谁先被建出来。
        want = (_groups_root().get("default") or "").strip()
        if want and want in ids:
            return want
        return ids[0]

    def _cell(self, gid=None):
        """某一组的状态格子：持号 / 组内游标 / 拿到通道的时刻 / 本组已派条数。

        取不到组（池子空）时返回一个临时格子，调用方照样能读写，只是不落地。
        """
        g = self._gid(gid)
        if not g:
            return {"active": None, "cursor": 0, "since": 0.0, "turns": 0}
        cell = self._rr.get(g)
        if cell is None:
            cell = {"active": None, "cursor": 0, "since": 0.0, "turns": 0}
            self._rr[g] = cell
        return cell

    def _grp_sync(self):
        """把「当前组」定下来并保证它那一格存在。__init__ 末尾调一次。

        包 try：这时候 conf 还没灌完，定组失败也不能让建池失败 ——
        大不了第一次挑人时再定。
        """
        try:
            g = self._gid()
            if g:
                self.grp_top = g
                self._cell(g)
        except Exception:
            pass

    @property
    def rr_active(self):
        """当前组占着通道的那个号。"""
        return self._cell().get("active")

    @rr_active.setter
    def rr_active(self, br):
        self._cell()["active"] = br

    @property
    def rr_cursor(self):
        """当前组的组内游标。"""
        return int(self._cell().get("cursor") or 0)

    @rr_cursor.setter
    def rr_cursor(self, i):
        self._cell()["cursor"] = int(i or 0)

    @property
    def rr_since(self):
        """当前组这个号是什么时候拿到通道的（交接里要写「你从几点到几点」）。"""
        return float(self._cell().get("since") or 0.0)

    @rr_since.setter
    def rr_since(self, t):
        self._cell()["since"] = float(t or 0.0)

    # ---------- 组装 ----------

    def _stamp(self, br):
        """把限流数值灌给一个账号：先全局默认，再盖它自己的单独设置。

        单独设置里 0（或缺省）表示「跟随默认」—— 间隔设成 0 本来也没意义，
        正好拿它当「没设」用，省一个开关。
        """
        ov = self.overrides.get(br.slug) or {}
        # 2026-09-25 第52步：这一号所在的组有没有自己的提醒。
        # 组里非空的键盖过全局那份；组里没写 = 沿用全局。组表本来就带
        # mtime 热重载，改完存盘就生效，不用重启桥。
        _gnotes = {}
        try:
            for _g in (_groups_root().get("groups") or []):
                _n = _notes_of(_g)
                if not _n:
                    continue
                for _s in (_g.get("slugs") or []):
                    _gnotes.setdefault(_s, _n)
        except Exception:
            _gnotes = {}
        _gn = _gnotes.get(br.slug) or {}

        def _note(key):
            return str(_gn.get(key) or self.conf.get(key) or "")
        br.min_interval = ov.get("min_interval") or self.conf["min_interval"]
        br.COOLDOWN = ov.get("COOLDOWN") or self.conf["COOLDOWN"]
        br.RATE_BACKOFF = ov.get("RATE_BACKOFF") or self.conf["RATE_BACKOFF"]
        # 2026-09-25 第55步（用户要求）：两种提醒作用域**互斥**，判据是「在不在轮询池」。
        #  - 在池号：slug 出现在任一组的 slugs 里 -> 只发「轮询池提示」pool_note。
        #  - 固定号：不在任何组里 -> 只发「常驻提示」standing_note。
        # 两者不再合并成一个〔提醒〕：池号每发都贴常驻提醒等于每发多付这份字数的钱；
        # 固定号根本不进池，池提醒与它们无关。_note(key) 已含「组非空值优先、回落全局」。
        # 注意判据不能用 br.enabled —— 那是「界面取消入池」的临时开关，不是「在不在组」。
        # 池子成员表：把每个组的 slugs 并起来。组表 mtime 热重载，改完存盘即生效。
        _pool_slugs = set()
        try:
            for _g in (_groups_root().get("groups") or []):
                for _s in (_g.get("slugs") or []):
                    _pool_slugs.add(_s)
        except Exception:
            _pool_slugs = set()
        if br.slug in _pool_slugs:
            br.standing_note = _note("pool_note")
        else:
            br.standing_note = _note("standing_note")
        # 格式提醒（2026-09-27 第346步，用户要求）：与池/固定号无关，谁跑都带。
        # 专门放「报错过的格式规范」，用户往设置台里加，每轮上游必然看到。
        # 空串 = 界面上清空了，这段就不发（reminder_block 会丢掉空段）。
        br.format_note = str(self.conf.get("format_note") or "")
        # 末尾提示词（附件说明）：只在走附件那一轮贴。空串 = 界面里清空了，不发。
        # 界面自己存的那份（含旧版硬编码文案）也过一遍同一个开关：关掉
        # 检查点之后，任何还列着 检查点.txt 的文案都会让模型去找空文件。
        _an = _gn.get("attach_note") or self.conf.get("attach_note")
        # 2026-10-03：附件说明**收成一份**（用户口径「光附件提示就3个 我醉了」）。
        # 真正常用的是 attach_note_for(names)，它按实际挂了哪几份动态生成 ——
        # 「没挂的就不提」这件事由它负责，不需要在这里预先剪文案。
        if _an is None:
            _an = attach_note_default()
        br.attach_note = _an
        # 2026-09-25 第55步（用户要求）：交接提示已删除 —— 附件提示（attach_note）取代了它。
        # 不再读配置/账号覆盖，固定用出厂那段，避免两套话术打架。
        # 第64步：交接提示词恢复可自定义 —— 组专属 > 全局 > 出厂。
        _hn = _note("handoff_note") or pget("handoff_note", HANDOFF_PROMPT)
        br.handoff_prompt = _hn
        # 账号自己的轮询条数；0 = 跟随全局
        br.turn_limit = float(ov.get("turn_limit") or 0.0)
        br.search = bool(ov.get("search"))   # 每号联网开关，默认关
        # 重发全文的裁剪预算（字符）。0 = 不裁；缺省给 30 万字。
        br.send_budget = _num(self.conf, "send_budget", 300000)
        # 账号级的输入上限一刀切盖掉按模型设的那套（这个号老/弱就压低它），
        # 没设就各模型用各自的全局值。
        one = int(ov.get("limit_chars") or 0)
        br.limits = ({m: one for m in MODELS} if one > 0
                     else dict(self.conf["limits"]))
        # 压缩检测点要算「离 dsh 的 80% 阈值还有多远」，得知道 dsh 那边看到的
        # 窗口有多大 —— 那就是本池发给 dsh 的同一个数，别在 Bridge 里再抄一份。
        br.pool_window = self.context_window

    def set_pace(self, slug, values=None):
        """某个账号单独的限流数值；传空就回到跟随默认。

        用在「这个号刚被封过，给它更保守的间隔」「那个号是主力，压快一点」
        这种场合 —— 界面上每行都能单独填。
        """
        with self.lock:
            if values:
                self.overrides[slug] = dict(values)
            else:
                self.overrides.pop(slug, None)
            br = self.bridges.get(slug)
            if br is not None:
                self._stamp(br)


    def reload(self, accounts=None, disabled=None):
        """按 ds_auth.json 重建池子。

        已经在池里的账号（按名字认）保留原来那个 Bridge —— 重建一个的话，
        它的会话表、限流冷却、轮数统计全丢，正在跑的那轮还会被甩掉。
        """
        if accounts is None:
            accounts = ds_api.load_accounts()["accounts"]
        with self.lock:
            off = set(disabled) if disabled is not None else {
                b.slug for b in self.bridges.values() if not b.enabled}
            old = {b.name: b for b in self.bridges.values()}
            fresh, taken, bad = {}, set(), []
            for i, acc in enumerate(accounts):
                name = acc.get("name") or f"账号{i + 1}"
                slug = _slugify(name, taken, acc.get("token") or "")
                taken.add(slug)
                br = old.get(name)
                if br is None:
                    try:
                        br = Bridge(ds_api.DeepSeek(account=acc),
                                    self.conf["min_interval"], self.log,
                                    name=name, slug=slug)
                    except Exception as exc:          # noqa: BLE001
                        # 缺 token 那种账号跳过就好，别拖垮整个池子
                        bad.append(f"{name}（{exc}）")
                        continue
                else:
                    br.slug = slug
                br.enabled = slug not in off
                br.pool = self      # 同上：重建时也要补回反向引用
                self._stamp(br)
                fresh[slug] = br
            self.bridges = fresh
            # 重建之后 rr_active 可能指向一个已经不存在的账号，游标也可能越界
            if self.rr_active is not None and self.rr_active not in fresh.values():
                self.rr_active = None
            if self.rr_cursor >= len(fresh):
                self.rr_cursor = 0
        if bad and self.log and sys.stdout is not None:
            print("跳过用不了的账号：" + "、".join(bad), flush=True)
        # 账号表换了，组表跟着变（slug 没了/多了）。重定当前组，并清掉
        # 已经不存在的组留下的格子 —— 留着它会让 _next_group 从环上
        # 找不到的位置开始数。
        self._grp_sync()
        ids = {g["id"] for g in self._groups()}
        for k in [k for k in self._rr if k not in ids]:
            self._rr.pop(k, None)
        return list(self.bridges)

    def apply(self, **kw):
        """把界面上的限流参数和常驻提醒灌给池里每个账号。"""
        with self.lock:
            for k, v in kw.items():
                if k in self.conf and v is not None:
                    self.conf[k] = v
            self.RR_TURN_LIMIT = float(self.conf.get("turn_limit") or 0)
            # 请求间隔是**全池一个**的闸（不分账号）—— 灌给 ds_api 的 pace_gate
            ds_api.set_pace_sec(self.conf.get("min_interval"))
            for br in self.bridges.values():
                self._stamp(br)
            # 2026-10-03：把提示词灌给模块级函数用的 PROMPTS 表。
            # 放这里（不是只放 __init__）是因为界面改完会再调一次 apply，
            # 灌在这里才能让「改完下一轮生效」成立。
            _sync_prompts(self.conf)

    def set_enabled(self, slug, on):
        with self.lock:
            br = self.bridges.get(slug)
            if br is not None:
                br.enabled = bool(on)
            # 被停用的号不能继续占着通道，不然下一轮还会派给它
            if self.rr_active is not None and not self.rr_active.enabled:
                self.rr_active = None

    # ---------- 对外形状 ----------

    def catalog(self):
        """/v1/models 的内容：四个池化条目 + 每账号四个钉住条目。"""
        out = [{"id": m, "owned_by": "deepseek-app（自动挑号）"} for m in MODELS]
        with self.lock:
            # 2026-09-25 分组：每组也是一个可点名的条目（deepseek-chat@main）。
            # 组 id 建的时候禁了与 slug 重名，所以两边不会撞。
            try:
                # 只报**显式配过**的组。隐式组（未分组 / 全部）不报 ——
                # 没配分组文件时不该凭空多出 @_rest 这四个模型名，
                # 「没有文件 = 和加分组之前一样」这句话得连目录一起算。
                for g in self._groups():
                    if g.get("implicit"):
                        continue
                    out += [{"id": f"{m}{self.SEP}{g['id']}",
                             "owned_by": "分组：" + g["name"]}
                            for m in MODELS]
            except Exception:
                pass
            for slug, br in self.bridges.items():
                out += [{"id": f"{m}{self.SEP}{slug}", "owned_by": br.name}
                        for m in MODELS]
        return out

    def status(self):
        with self.lock:
            out = []
            # 2026-09-25：轮询序列 = **分组里的号**，不再是全体启用号。
            # 未分组的号不进 order，因此 index/rr_pos 都是 0（界面据此
            # 标「不在轮询」），但它的 turns_here 照常返回 —— 次数要显示。
            _ing = set()
            try:
                for _g in self._groups():
                    for _s in (_g.get("slugs") or []):
                        _ing.add(_s)
            except Exception:
                _ing = set()
            order = [b for b in self.bridges.values()
                     if b.enabled and b.slug in _ing]
            # 通道归属才是「当前轮到谁」—— rr_cursor 只是个备查游标，
            # resolve() 读的是 rr_active。
            active = self.rr_active if self.rr_active in order else None
            for i, (slug, br) in enumerate(self.bridges.items()):
                st = br.status()
                st["custom_pace"] = slug in self.overrides
                # 这个号有没有单独写过交接提示词（界面上给个标记）
                _ov = self.overrides.get(slug) or {}
                st["handoff_prompt"] = bool((_ov.get("handoff_note") or "").strip())
                # 序号 = 在「参与轮询的账号」里的位次。不参与就是 0，
                # 这样界面一眼能看出谁被排除在外了。
                pos = order.index(br) + 1 if br in order else 0
                st["index"] = pos
                st["rr_pos"] = pos
                # 2026-09-25：这个号在不在轮询序列里。界面据此把它标成
                # 「不在轮询」；但 turns_here 照样带出去 —— 次数要显示。
                st["in_pool"] = br in order
                st["cursor"] = br is active
                st["active"] = br is active
                out.append(st)
            return out

    def active_slug(self):
        """当前占着通道的账号 slug；没有就返回空串。

        给界面存盘用 —— 自动轮到的和手动「置为当前」的都会反映在这里。
        """
        with self.lock:
            br = self.rr_active
            return br.slug if br is not None and br.enabled else ""

    def set_cursor(self, slug):
        """把轮询游标挪到某个账号：下一次自动挑号就从它开始。

        返回是否找到了这个账号。给界面上的「置为当前」用。
        """
        with self.lock:
            order = [b for b in self.bridges.values() if b.enabled]
            for i, br in enumerate(order):
                if br.slug == slug:
                    self.rr_cursor = i
                    # 通道归属也一并挪过去 —— resolve() 读的是 rr_active，
                    # 光挪 rr_cursor 是没用的。
                    self.rr_active = br
                    # 计数归零：不归零的话它要是刚用满条数，下一轮立刻
                    # 又判超限让位，手动指定等于没生效。
                    br.turns_here = 0
                    return True
            return False

    def clear_empty(self, slug):
        """把一个账号的回空状态清掉 —— 它立刻重新参与轮询。

        给界面上「回空」那一列的点击复位用：号只是暂时被压着，
        不想干等那个让位窗口就点一下。
        """
        with self.lock:
            br = self.bridges.get(slug)
            if br is None:
                return False
            br.last_empty = ""
            br.empty_streak = 0
            br.empty_at = 0.0
            br._empties = {}
            return True

    def any_bridge(self):
        """随便给一个，只用于「还没挑好人就出错了」的日志兜底。"""
        with self.lock:
            return next(iter(self.bridges.values()), None)

    # ---------- 同步给 dsh 的配置 ----------

    # 这一段管理 dsh 侧的账号可见性。dsh 的模型目录只认配置文件（它从不去问
    # 端点有哪些模型），所以「在 dsh 里按账号选」必须把账号写成配置。写法是
    # 一个账号一条 provider：dsh 的模型选择器按 provider 分组，分组名就是账号名，
    # 选模型 = 选账号 + 选思考模式，不需要它多长一个下拉。
    # 写到 $DSH_HOME/cordis.patch.yml，dsh 监听这个文件（watchUserPatches），
    # 写完下一次请求就生效，不用重启。文件由 GUI 自动重写，不用手写。

    PATCH_MARKER = "# generated-by: ds_bridge account pool"

    # 每个模型在 dsh 侧的显示后缀和思考档位
    MODEL_LABEL = {
        "deepseek-chat": ("不思考", False),
        "deepseek-reasoner": ("深度思考", True),
        "deepseek-chat-search": ("不思考+联网", False),
        "deepseek-reasoner-search": ("思考+联网", True),
    }
    CONTEXT_WINDOW = 131072

    def context_window(self, model):
        """告诉 dsh 这个模型的窗口有多大（token）。

        这个数单独控制 dsh 的主动压缩时机，跟硬拦（limits）无关。
        dsh 的 token 表按 4 字符/token 估（`dsh-token-meter:16`），压缩阈值是
        `contextWindow × 0.8`（`dsh-compaction-basic:14`）。
        """
        chars = int((self.conf.get("windows") or {}).get(model) or 0)
        if chars <= 0:
            return self.CONTEXT_WINDOW
        return max(8192, int(chars / 4))


    def dsh_patch(self, base_url, key_env="LOCAL_LLM_TOKEN"):
        """生成 $DSH_HOME/cordis.patch.yml 的全文。"""
        lines = [
            self.PATCH_MARKER,
            "# 这个文件由 ds/ds_bridge.py 的账号池自动重写，手改会被覆盖。",
            "#",
            "# 一个 DeepSeek 账号 = 一条 provider：dsh 的模型选择器里按账号名分组，",
            "# 新建对话选哪个分组，这个对话就一直用那个号（模型 id 的 @后缀让桥接",
            "# 钉住账号，绝不串号）。想让桥接自动挑号，就选 settings.yaml 里那条",
            "# ds-bridge（不带后缀的四个模型）。",
            "- id: llm-pi-ai",
            "  config:",
            "    providers:",
        ]
        with self.lock:
            items = list(self.bridges.items())
        for slug, br in items:
            lines += [
                f"      ds-{slug}:",
                f"        displayName: {json.dumps(br.name, ensure_ascii=False)}",
                "        api: openai-completions",
                f"        baseURL: {json.dumps(base_url)}",
                f"        apiKeyEnv: {key_env}",
                "        compat:",
                "          thinkingFormat: deepseek",
                "        models:",
            ]
            for model, (label, thinking) in self.MODEL_LABEL.items():
                name = f"{br.name} · {label}"
                lines += [
                    f"          - id: {model}{self.SEP}{slug}",
                    f"            name: {json.dumps(name, ensure_ascii=False)}",
                    f"            contextWindow: {self.context_window(model)}",
                    "            input:",
                    "              - text",
                    "              - image",
                ]
                if thinking:
                    lines += ["            reasoningEfforts:",
                              '              "off":',
                              "              high: high"]
                else:
                    lines.append("            reasoningEfforts: false")
        # 2026-09-27 第379步（用户口径）：「可以在切组动手脚，新建分组的时候
        # 把配置补上」。dsh 的模型目录**只认配置文件、从不问端点**（见本节
        # 开头注释），所以光把组写进 _pool_groups.json 是不够的 —— dsh 的
        # 选择器里没有 @组名 这一项，用户根本点不到。这里补上：每个组一条
        # provider，模型名就叫 deepseek-chat@<组名>，选中即走该组轮询。
        # 组名即 id（第377步），所以后缀直接用 name。
        try:
            _grps = self._groups()
        except Exception:                        # noqa: BLE001
            _grps = []
        for g in _grps:
            gname = str(g.get("name") or "").strip()
            if not gname or "@" in gname:
                continue
            lines += [
                f"      ds-grp-{gname}:",
                f"        displayName: {json.dumps('分组·' + gname, ensure_ascii=False)}",
                "        api: openai-completions",
                f"        baseURL: {json.dumps(base_url)}",
                f"        apiKeyEnv: {key_env}",
                "        compat:",
                "          thinkingFormat: deepseek",
                "        models:",
            ]
            for model, (label, thinking) in self.MODEL_LABEL.items():
                lines += [
                    f"          - id: {model}{self.SEP}{gname}",
                    f"            name: {json.dumps(gname + ' · ' + label, ensure_ascii=False)}",
                    f"            contextWindow: {self.context_window(model)}",
                    "            input:",
                    "              - text",
                    "              - image",
                ]
                if thinking:
                    lines += ["            reasoningEfforts:",
                              '              "off":',
                              "              high: high"]
                else:
                    lines.append("            reasoningEfforts: false")
        # 2026-09-24 加（用户要求）：把 ollama 的云端模型一起挂进来。
        # 为什么不写进 profiles/web/cordis.patch.yml：那份 patch 的语义是
        # 「整键替换 config」（dsh-app-boot/lib/index.js 的 applyEntryPatches：
        # `target[key] = value`），再打一条 `- id: llm-pi-ai` 会把上面生成的
        # 整个账号池覆盖掉。所以只能在这里跟着一起生成，桥每次 sync 都带上它。
        #
        # ollama 开的是 OpenAI 兼容口（/v1），不校验 key —— 但 pi-ai 的
        # contextWindow 262144 取自 /api/show 的 gemma4.context_length。
        lines += [
            "      ollama:",
            "        displayName: \"ollama\"",
            "        api: openai-completions",
            "        baseURL: \"http://127.0.0.1:11434/v1\"",
            "        apiKeyEnv: LOCAL_LLM_TOKEN",
            "        models:",
            "          - id: gemma4:31b-cloud",
            "            name: \"ollama · gemma4 31b (云)\"",
            "            contextWindow: 262144",
            "            input:",
            "              - text",
            "              - image",
            "            reasoningEfforts:",
            "              \"off\":",
            "              high: high",
        ]
        return "\n".join(lines) + "\n"

    # compaction-basic 挂在这个 ptc 预设里（宿主那行被 web bundle 显式
    # disabled 了），所以只改 top-level patch 不生效 —— 得写这个文件。
    # 它在 node_modules 里，升级 dsh 会被覆盖，所以每次 sync 都重贴一遍自愈。
    PRESET_YML = (PREV_ROOT / "node_modules" / "@deepseek-ai"
                  / "dsh-agent-presets"
                  / "presets" / "ptc" / "agent.cordis.yml")

    def compact_ratios(self):
        """从 GUI 配置里取压缩比例，非法就退回 (0.8, 0.16)。"""
        try:
            thr = float(self.conf.get("compact_threshold_ratio") or 0.8)
        except (TypeError, ValueError):
            thr = 0.8
        try:
            rtn = float(self.conf.get("compact_retain_ratio") or 0.16)
        except (TypeError, ValueError):
            rtn = 0.16
        if not (0.0 < rtn < thr < 1.0):
            thr, rtn = 0.8, 0.16
        return thr, rtn

    def apply_compact_ratios(self):
        """把压缩比例幂等地写进 ptc 预设。返回 (是否改了, 说明)。

        只动 compaction-basic 那一个条目下面的 config 块，别的地方不碰；
        找不到目标就原样返回，绝不让它把预设改坏（改坏了 dsh 起不来）。
        """
        thr, rtn = self.compact_ratios()
        p = self.PRESET_YML
        try:
            s = p.read_text(encoding="utf-8")
        except OSError as exc:
            return False, f"读不到预设：{exc}"
        CR, LF = chr(13), chr(10)
        nl = (CR + LF) if (CR + LF) in s else LF
        lines = s.split(nl)
        out, i, done, changed = [], 0, False, False
        while i < len(lines):
            ln = lines[i]
            if ln.strip() != "- id: compaction-basic":
                out.append(ln)
                i += 1
                continue
            out.append(ln)
            if i + 1 >= len(lines):
                i += 1
                continue
            name_ln = lines[i + 1]
            out.append(name_ln)
            ind = name_ln[:len(name_ln) - len(name_ln.lstrip())]
            j = i + 2
            if j < len(lines) and lines[j].strip() == "config:":
                j += 1
                while j < len(lines):
                    t = lines[j]
                    if not t.strip():
                        break
                    if len(t) - len(t.lstrip()) <= len(ind):
                        break
                    j += 1
            want = [ind + "config:",
                    ind + f"  thresholdRatio: {thr}",
                    ind + f"  retainRatio: {rtn}"]
            if lines[i + 2:j] != want:
                changed = True
            out += want
            i = j
            done = True
        if not done:
            return False, "预设里没有 compaction-basic 条目，没动"
        if not changed:
            return False, "压缩比例已经是最新的"
        try:
            p.write_text(nl.join(out), encoding="utf-8")
        except OSError as exc:
            return False, f"写不进预设：{exc}"
        return True, f"已把压缩比例写进 ptc 预设（{thr} / {rtn}）"

    # 2026-09-27 第379步：这份 patch 的真实位置。$DSH_HOME 就是 dsh-preview。
    DSH_PATCH_FILE = PREV_ROOT / "cordis.patch.yml"
    DSH_BASE_URL = "http://127.0.0.1:11999/v1"

    def sync_dsh_patch_now(self):
        """按当前账号+分组重写 cordis.patch.yml；失败只记一行，绝不抛。

        第379步：以前这份文件只有 ds_gui.py 会写，而 GUI 已经停用 ——
        于是文件变成没人维护的存量，dsh 选择器里既没有新账号也没有分组。
        现在由桥自己保证：启动时一次、组表每次变动一次。
        """
        try:
            ok, msg = self.sync_dsh_patch(self.DSH_PATCH_FILE,
                                          self.DSH_BASE_URL)
            if ok and self.log:
                print("  " + msg, flush=True)
            return ok, msg
        except Exception as exc:                 # noqa: BLE001
            try:
                self.note("  dsh 配置同步失败：" + str(exc)[:160])
            except Exception:                    # noqa: BLE001
                pass
            return False, str(exc)

    def sync_dsh_patch(self, path, base_url, key_env="LOCAL_LLM_TOKEN"):
        """把上面那段写进 dsh 的 home 覆盖层。

        返回 (是否写了, 说明)。别人写的同名文件不覆盖 —— 那里可能有他自己的
        配置，覆盖掉比不同步更糟。
        """
        path = pathlib.Path(path)
        want = self.dsh_patch(base_url, key_env)
        try:
            old = path.read_text(encoding="utf-8")
        except OSError:
            old = None
        if old is not None and self.PATCH_MARKER not in old:
        # 2026-09-22 拆隐患：这里原来调 self.apply_compact_ratios()，会往
        # node_modules/@deepseek-ai/dsh-agent-presets/presets/ptc/agent.cordis.yml
        # 重贴 compaction 的 thresholdRatio/retainRatio。那个文件是 dsh 出厂内容：
        # 升级/重装会被覆盖（原注释自己承认要「每次 sync 重贴一遍自愈」），写坏了
        # dsh 直接起不来。而 ds_bridge.ini 现在是 0.8/0.16 = 出厂默认值，这个写入在
        # 效果上是空操作，只把 node_modules 弄脏 —— 而且它比的是原始文本行
        # (lines[i+2:j] != want)，缩进格式差一点就判 changed，于是每次桥启动都重写。
        # 真要可配：includeShippedRoot:false + configuredRoots 指向 node_modules 外的
        # 自建预设目录（shipped 根在优先级最前，同名会盖掉用户那份）。见 PITFALLS.md。
            return False, "dsh 侧配置已经是最新的"
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(want, encoding="utf-8")
        except OSError as exc:
            return False, f"写不进 {path}：{exc}"
        return True, f"已同步 {len(self.bridges)} 个账号到 {path}（dsh 热生效）"

    # ---------- 挑人 ----------


    def split_model(self, model):
        base, sep, slug = (model or "").rpartition(self.SEP)
        return (base, slug) if sep else (model, "")

    # 让位窗口：一个号回过空之后，这么多秒内不再派活给它。
    # 过期就重新入队 —— 不然它永远等不到成功的机会，empty_streak
    # 永远清不掉，一圈之后就锁死在剩下的某一个号上了。
    RR_EMPTY_WINDOW = 90.0

    def _just_emptied(self, br):
        """这个号是不是刚回过空 —— 是的话本轮让位给别人。

        看的是时间窗而不是永久的标志位：回过空只躲 RR_EMPTY_WINDOW 秒，
        之后照常排进轮询。成功一回（Bridge.run 的 :1335）立刻清掉。
        """
        if not br.last_empty and br.empty_streak <= 0:
            return False
        if not br.empty_at:
            return True
        win = empty_policy(getattr(br, "slug", "")).get("yield_window")
        if win is None:
            win = self.RR_EMPTY_WINDOW
        try:
            win = float(win)
        except (TypeError, ValueError):
            win = self.RR_EMPTY_WINDOW
        # <= 0 = 关掉「回空让位」。注意不能用 `or` 兜默认值 —— 那样 0 会被
        # 吃掉回落 90 秒，写 0 等于没写（2026-09-24 踩过）。
        if win <= 0:
            return False
        return (time.time() - br.empty_at) < win

    def _limit_of(self, br):
        """这个号当前的轮次上限。三层，**窄的赢**：

            账号自己的 turn_limit  >  它所在组的 turn_limit  >  池子全局 RR_TURN_LIMIT

        0 = 这一层没设，继续往下看。没配分组时组表只有一个隐式组、
        turn_limit 恒 0，等价于加分组之前的两层。
        """
        lim = float(getattr(br, "turn_limit", 0) or 0)
        if lim <= 0:
            gs = self._groups()
            slug = getattr(br, "slug", "")
            for g in gs:
                if slug in g["slugs"]:
                    lim = float(g.get("turn_limit") or 0.0)
                    break
        if lim <= 0:
            lim = float(getattr(self, "RR_TURN_LIMIT", 0) or 0)
        return lim

    def _over_limit(self, br):
        """这个号连着派的条数已经用满 —— 该让位了。0 = 不限。"""
        lim = self._limit_of(br)
        return lim > 0 and br.turns_here >= lim

    def _yield_reason(self, br):
        """该不该让位，以及**为什么**。返回空串 = 不用让位。

        2026-09-21：原来只返回 bool，日志只能写死「条数用满或刚回空」——
        两条规则合流成一句话，报表里拆不出到底是哪条触发的。
        现在把原因拼成可 grep 的短串：
            条数用满(15/15) ｜ 刚回空(12s前) ｜ 两者都有时用 + 连起来
        """
        over = self._over_limit(br)
        empty = self._just_emptied(br)
        if not over and not empty:
            return ""
        parts = []
        if over:
            lim = self._limit_of(br)
            parts.append(f"条数用满({br.turns_here:.0f}/{lim:.0f})")
        if empty:
            age = (time.time() - br.empty_at) if br.empty_at else 0.0
            parts.append(f"刚回空({age:.0f}s前)")
        return "+".join(parts)

    def _yield(self, br):
        """兼容旧调用：真值 = 该让位。新代码用 _yield_reason 取原因。"""
        return bool(self._yield_reason(br))

    def _handoff(self, old, new, why=""):
        """换人时把老号的连续计数清零 —— 它下次再被轮到就重新数。

        这里必须落一行日志。账号轮换是「满 N 条让位」和「回空让位」两条规则
        合流的地方，出问题时唯一能复盘的就是这条记录。之前它是 staticmethod
        且一声不吭，日志里翻遍也找不到谁换了谁、因为什么换的。
        """
        if old is not None and old is not new:
            old.turns_here = 0
            # 记下这次换号，do_POST 拿到 messages 后会回来取，让老号吐一段交接。
            # 槽位按**接手方**的 slug 存：接手那一轮照自己的 slug 取，
            # 多窗口各取各的，不会串。
            if new is not None and getattr(new, "slug", ""):
                self.swap_pending[new.slug] = (old, time.time(),
                                               self.rr_since)
            self._note_swap(old, new, why)

    @staticmethod
    def _note_swap(old, new, why):
        """把一次换号写进日志；没日志通道就算了，不能因为记日志把轮询搞挂。"""
        emit("switch", frm=getattr(old, "slug", "?"),
             to=getattr(new, "slug", "?"), why=why or "",
             old_empty=getattr(old, "last_empty", ""),
             old_streak=getattr(old, "empty_streak", 0))
        try:
            who = new if new is not None else old
            if who is None:
                return
            fn = getattr(who, "note", None)
            if fn is None:
                return
            fn("换号 %s -> %s  %s" % (getattr(old, "slug", "?"),
                                      getattr(new, "slug", "?"),
                                      why or ""))
        except Exception:
            pass

    def _group_order(self, gid=None):
        """某一组里、参与轮询的账号（按 ds_auth.json 次序）。

        组表里的 slugs 已经按池子现有账号过滤过，这里再筛一次 enabled ——
        停用的号不进轮换，跟加分组之前的规矩一致。
        """
        gs = self._groups()
        want = None
        g = self._gid(gid)
        for x in gs:
            if x["id"] == g:
                want = set(x["slugs"])
                break
        if want is None:
            # 2026-09-25 新规矩：组不存在 / 一个组都没配 -> **没有号可轮**。
            # 原来这里兜底返回全体启用账号，等于把「没分组」偷偷变成
            # 「整池一组」，跟「只跑自定义分组」直接冲突。
            return []
        return [b for b in self.bridges.values()
                if b.enabled and b.slug in want]

    def _default_gid(self):
        """不带 @ 的请求归哪一组：先认配置里的 default，认不出用组表第一组。

        2026-09-25 加。跟 _gid() 的区别是**不看 grp_top** —— grp_top 现在
        只是「最近被服务过的一组」，拿它当默认会让不带 @ 的请求跟着别的
        组跑，两组就不独立了。没配 default 就固定用第一组，行为可预期。
        """
        gs = self._groups()
        if not gs:
            return ""
        ids = [g["id"] for g in gs]
        want = (_groups_root().get("default") or "").strip()
        if want and want in ids:
            return want
        # 2026-09-27 第422步（用户口径）：切到哪个组，不带 @ 就走哪个组。
        # 原来 default 空就固定第一组，导致「设置台切到剪辑，实际仍走 test」。
        top = str(getattr(self, "grp_top", "") or "").strip()
        if top and top in ids:
            return top
        return ids[0]

    def _set_anchor(self, br, name):
        """给这一发用的账号桥记下「窗口锚定名」。

        2026-10-03（用户口径「我固定号访问就是固定 id 访问，组就是组访问，
        他们不搭嘎」）：**锚定名取决于这一发是怎么进来的**，不是账号自带的属性。

            @组名 进来  -> 锚定名 = 组名   （组窗口，全组各号同名）
            @号名 进来  -> 锚定名 = 号 id  （这个号自己的窗口）

        为什么必须分开：分组发言时 resolve 会先在组里挑一个号，slug 变成
        那个号的名字 —— 如果拿 slug 当锚定名，分组语义就丢了，桥会去建一个
        以**号名**命名的新窗口（实测 04:02:33「新建「020」/020」），
        而「智普清言」那个真正的组窗口被晾在一边。

        存成属性（不是全局）是因为桥是 ThreadingHTTPServer：一个请求一个
        线程，两个组同时来请求时不能串。每发都由 resolve 重设一次，
        所以不会有残留值被下一发继承。
        """
        try:
            br.anchor = str(name or "").strip()
        except BaseException:            # noqa: BLE001
            pass
        return br.anchor

    def _enter_group(self, gid):
        """把某一组设为「当前组」并保证它那一格存在。**不清空任何状态**。

        2026-09-25 改（用户要求）：组与组并行、不排先后，组内才按顺序。
        原来这里会把 cursor/active/turns 一起清零 —— 那是「同一时刻只有
        一组在跑」的假设下写的。现在每组各跑各的，清零等于：只要有人
        点了一下 @另一组，本组正跑着的号就被踢下来重数，两组互相干扰。
        所以改成只落定 grp_top，状态格子原样留着。
        """
        if not gid:
            return ""
        self._cell(gid)
        self.grp_top = gid
        return gid

    def _group_ring(self):
        """参与组轮换的组 id，按文件里写的次序，跳过没人的组。"""
        return [g["id"] for g in self._groups() if self._group_order(g["id"])]

    def _next_group(self, after_gid=None):
        """**盲目**换到环上的下一个非空组，并切过去。只有一组时返回它自己。

        规矩跟账号轮换一模一样：严格按次序转圈，跳过空组，从表尾绕回表头
        就算一圈。**不挑「哪个组更闲」** —— 那样会在两组之间来回横跳，
        轮换就不成形了。
        """
        ring = self._group_ring()
        if not ring:
            return ""
        if len(ring) == 1:
            # 环上就一个非空组：换组等于原地不动。这时**不能**无脑
            # _enter_group —— 那会把游标和本组条数清零，等于每次点
            # 「立即换组」都把当前号的进度抹掉，白白重来一轮。
            # 只有当 grp_top 还没落在这个唯一组上（比如默认组是空的）
            # 才切一次，把状态对齐。
            if self.grp_top != ring[0]:
                return self._enter_group(ring[0])
            return ring[0]
        cur = after_gid if after_gid is not None else self.grp_top
        try:
            start = ring.index(cur) + 1
        except ValueError:
            start = 0
        return self._enter_group(ring[start % len(ring)])

    def _scan_group(self, after_gid=None):
        """找下一个「有号能干活」的组，返回 (gid, order)；找不到返回 ("", [])。

        只看不写 —— 定下来之前不动 grp_top 和游标。扫一圈下来要是把
        「当前组」留在某个最后被检查的组上，界面显示的持号组就和实际
        派活的那组对不上了。
        """
        ring = self._group_ring()
        if not ring:
            return "", []
        cur = after_gid if after_gid is not None else self.grp_top
        try:
            start = ring.index(cur) + 1
        except ValueError:
            start = 0
        for k in range(len(ring)):
            gid = ring[(start + k) % len(ring)]
            order = self._group_order(gid)
            for br in order:
                if not self._yield_reason(br):
                    return gid, order
        return "", []

    def _group_limit(self, gid=None):
        """某一组的组级轮次上限。0 = 这一层不管，**不是**「永不换组」。

        隐式组（未分组 / 全部）没地方写 turn_limit，就跟随池子全局值 ——
        否则它一被轮到就永远不放手，别的组全饿死。这跟账号层
        「账号没设就跟随组、组没设就跟随池子」是同一条规矩。
        """
        want = self._gid(gid)
        for g in self._groups():
            if g["id"] != want:
                continue
            lim = float(g.get("turn_limit") or 0.0)
            if lim <= 0:
                # 组没写 turn_limit = 跟随池子全局值。跟账号层一个字面
                # 相同的规矩（账号 0 = 跟随全局），别发明第二套语义。
                # 全局也是 0 才是真的不限 —— 那时候本来也只有一组轮。
                lim = float(getattr(self, "RR_TURN_LIMIT", 0) or 0)
            return lim
        return 0.0

    def _group_spent(self):
        """本组这一圈派的条数够了吗 —— 够了就该整组让位给下一组。

        只在**有别的组可去**时才算数：单组模式下这个判断永远是 False，
        免得把唯一一组的计数用满后就没人可派了。
        """
        if len(self._group_ring()) <= 1:
            return False
        lim = self._group_limit()
        if lim <= 0:
            return False
        return float(self._cell().get("turns") or 0) >= lim

    def _next_bridge(self, after=None, why=""):
        """严格按序号轮：after 后面第一个没刚回空的。

        不挑「认得这段对话」的 —— 轮询就是轮询，轮到谁是谁。到了那个号再让它
        接着自己最近那条会话发（SessionCache.latest 兜底），所以也不会新开窗口。
        全都回空就退回 after 的下一个，不做无谓等待。
        """
        # 2026-09-25 分组：顺序表 = **本组**里参与轮询的号，不再是全体。
        # 没有 _pool_groups.json 时只有一个隐式组（全体），跟以前一模一样。
        #
        # 2026-09-25 改：**组内轮序，不再自动换组**。原来这里是「本组条数
        # 用满 -> 整组让位给下一组」，实测不是要的效果。要的是一组 2 个号就
        # 1-2-1-2 一直转，各组互不干扰（上限 10 轮就是：1 跑 10 条换 2，
        # 2 跑 10 条换回 1）。所以跨组切换整个撤掉 —— 轮询只在**本组**里
        # 转圈，别的组参与不进来。换组只剩两条显式路子：模型名写 @组名，
        # 或者 /relay 的 group_swap。组自己的 turn_limit 不再管「什么时候
        # 换组」，它现在只管「本组每个号跑几轮」，喂给账号层那条级联。
        order = self._group_order()
        if not order:
            # 本组一个能用的号都没有（被删了 / 全停用 / 全退出轮询）。
            # 2026-09-25 改（用户要求：组外异步、不排先后）：**不跳别的组**。
            # 原来这里 _next_group() 整组切走，等于「A 组没人就把请求塞给
            # B 组」—— 两组就串台了，也不再有「各跑各的」。现在返回 None，
            # 调用方据此报「当前分组里没有可用账号」；换组只剩 @组名 和
            # /relay 的 group_swap 两条显式路子。
            return None
        n = len(order)
        # start 保留「没取模」的原始下标：只有它才看得出这一圈有没有绕回表头。
        # 取过模的话，从表尾绕回表头正好写成 0，跟「本来就从表头开始」混成一样。
        if after is not None:
            try:
                start = order.index(after) + 1
            except ValueError:
                start = self.rr_cursor
        else:
            start = self.rr_cursor
        # 让位的**原因**取自老号（after）—— 它才是要走的那个人。
        # 在进循环前算，因为 _handoff 会把 old.turns_here 清零。
        why_after = why or (self._yield_reason(after)
                            if after is not None else "")
        for k in range(n):
            pos = (start + k) % n
            br = order[pos]
            if not self._yield_reason(br):
                if start + k >= n:
                    # 扫过了表尾 = 绕回表头，一圈到此结束：所有启用账号的
                    # 连续计数一起归零，下一圈人人从 0 重新数。只靠 _handoff
                    # 清让位的那一个，遇上有号提前回空让位时各号余数会对不齐，
                    # 「满 30 条换人」的规矩就不齐了。
                    self._clear_cycle(order)
                self._handoff(after, br, why_after or "序列轮到")
                self.rr_cursor = (pos + 1) % n
                return br
        # 2026-09-25 改：本组全员都在让位窗口里时，**不再跳到别的组**。
        # 原来这里会 _scan_group() 找别的组整组切过去，跟「组内轮序、各组
        # 互不干扰」直接冲突 —— 本组号只是暂时回空，一会儿就能用，这时候
        # 换到别的组等于把流量白送给别的组。现在老老实实在本组里按序列
        # 走下一个：该是谁就是谁，哪怕它刚回空（回空让位本来就是轮询的
        # 一部分，不是「这组不能用了」）。
        # 全都在让位窗口里：照样按序列走下一个，
        # 保证严格转圈。不能挑 empty_at 最早的 —— 时间戳会相同，min 就
        # 固定选中同一个号，转不动。序列才是唯一的准绳。
        pos = start % n
        if start >= n:
            self._clear_cycle(order)
        nxt = order[pos]
        self._handoff(after, nxt, "全在让位窗口内")
        self.rr_cursor = (pos + 1) % n
        return nxt

    def _clear_cycle(self, order):
        """一圈轮完：所有启用账号的连续计数一起归零，下一圈重新数。

        光靠 _handoff 清「让位的那一个」在正常转圈时结果一样，但一旦有账号
        因为回空提前让位、或者中途被手动挪过游标，各号就会带着不同的余数进
        下一圈 —— 满 30 条换人的规矩就不齐了。这里显式对齐一圈的起点。

        这也是「一圈」的唯一定义点，所以顺带落一行日志：只靠换号日志看不出
        转了几圈，一圈的边界才是核对「满 N 条」规矩对不对的基准。
        """
        for b in order:
            b.turns_here = 0
        try:
            if order:
                fn = getattr(order[0], "note", None)
                if fn is not None:
                    fn("一圈轮完，%d 个启用账号的连续计数归零" % len(order))
        except Exception:
            pass

    def rotate_away(self, br, why="限流让位"):
        """报 429 的当场就把当前账号换掉，别等下一个请求。

        正常换号只在 resolve() 里做，而 resolve() 只在请求进来时跑。限流之后
        请求可能好几分钟才来（实测 dsh 5 分钟没回来），那时「刚回空」的
        yield_window(90s) 早过期了，判据不成立，同一个限流号又被派一遍 ——
        闭环。所以报 429 的这一刻先把 rr_active 移走，不管下一个请求什么时候
        到，都会落到别的号。

        2026-09-25 改（用户要求：组外异步、不排先后）：这里**必须先锚回 br
        自己那一组**。rr_active / rr_cursor 都是「当前组」的格子，而「当前组」
        是全局的 grp_top。429 是**上一个**请求报的，等它回来换号时，grp_top 早
        被另一组的并发请求改走了 —— 不锚回去就会去动别人那一组的状态：轻则
        白换一次（rr_active is not br 直接 return，限流号纹丝不动，闭环照旧），
        重则把另一组的游标推着跑。所以按 br.slug 反查它属于哪组，先切回去。
        查不到组（未分组号被 @ 点名钉着用）就原地不动 —— 不动任何组的状态，
        绝不会替别的组换号。
        """
        with self.lock:
            gid = group_of(list(self.bridges.keys())).get(
                getattr(br, "slug", ""), "")
            if gid:
                self._enter_group(gid)
            if self.rr_active is not br:
                return              # 已经换过（并发请求），别重复换
            nxt = self._next_bridge(br, why)
            if nxt is None or nxt is br:
                return
            self.rr_active = nxt
            self.rr_since = time.time()

    def resolve(self, model, messages):
        """→ (bridge, 去掉账号后缀的模型名)。挑不出人就抛异常。"""
        # 每发前扫一眼配置：设置台改完存盘，这一发就吃到新值
        _ini_auto_resync(self)
        base, slug = self.split_model(model)
        if base not in MODELS:
            raise KeyError(model)
        with self.lock:
            # 2026-09-25 分组：模型名带 @x 时，x 先当组名认，认不出再当账号名。
            # 组 id 与 slug 同名在写入端就被挡了（api.groups_set），所以不会歧义。
            # 两个都认不出才报错 —— deepseek-chat@不存在 本来就报错，这是纯增量。
            # 2026-09-25 改（用户要求）：组与组并行，组内按顺序。
            # 不带 @ 的请求固定归 default（认不出用第一组），**不看 grp_top**
            # —— 拿「最近被服务过的一组」当默认会让不带 @ 的请求跟着别的
            # 组跑，两组就串台了。
            if not slug:
                _d = self._default_gid()
                if _d:
                    self._enter_group(_d)
            pinned = self.bridges.get(slug) if slug else None
            # 2026-09-27 第375步（用户口径）：@后缀先认组名，再认组 id，
            # 最后才当账号名。组名优先于账号名 —— 组叫什么，@什么就进那组。
            _gref = group_by_ref(slug) if slug else ""
            if slug and pinned is None and not _gref:
                raise KeyError(model)

            # 2026-09-25 新规矩：**没有自动分组，只有自定义分组参与轮询**。
            # 后端点名谁，就顺着谁找到他那一组，切过去再在组内正常轮换 ——
            # 点名的账号只是「指认组」的凭据，不保证它本人干活。
            # 没被分进任何组的账号不进轮换，但 @它 仍然钉死专用（照列在
            # /v1/models 里，选中即离开轮询）。
            #
            # 2026-10-03（用户口径「**我固定号访问就是固定 id 访问，组就是组访问，
            # 他们不搭嘎**」）：**去掉了「点号名 -> 切它所在的组」这条转化。**
            #
            # 病：原来 @113 时若 113 在某个组里，会被改写成「切到那组」，于是
            # slug 清空、改成组内轮转 —— 实际干活的可能不是 113。而窗口锚定名
            # 取自 slug（`_gname = slug`，见 plan()），号一变锚定名就跟着变，
            # 窗口跑到别的号名下。**「固定号」和「进组」是两件事，不该互相改
            # 写**：组只由 @组名 触发，号只由 @号名 触发。
            if _gref:
                # (a) 写的是组名或组 id：切到那一组轮换。
                self._enter_group(_gref)
                slug = ""
                pinned = None
            elif pinned is not None:
                # (b) 写的是某个账号名：**一律钉死这个号**，不管它在不在组里。
                #     组轮换只能由 @组名 明确触发，不从这里推导。
                #
                # 2026-10-03（用户口径「我固定号访问就是固定 id 访问，组就是组访问，
                # 他们不搭嘎」）：钉号进来时，**窗口锚定名 = 这个号的 id**。
                # 这样 @113 就用「113」这个窗口，跟任何组都不相干。
                self._set_anchor(pinned, getattr(pinned, "slug", ""))
                # 2026-10-03（用户口径「@309 → 直通,找「309」窗口,发过去,回过来。
                # 就这些」「不止 309 所有固定号都是这个规则」）：
                # **这一发标记为「直通」。**
                #
                # 语义：客户端用号名访问 = 桥不管 —— 找「<号名>」窗口、续会话、
                # 原样发、原样回。**不套分组的缺口/标记法/快照/交接，也不改
                # usage（报实发量，不报客户端全量）。**
                #
                # 判据是「这一发怎么进来的」，不是「这个号归不归组」：
                # 309 即使被分进某个组，用 @309 发仍然直通；只有用 @组名 发
                # 才会走组那套（那时 309 被轮到也受组管制）。两条路互不相干。
                #
                # 为什么必须分开：那套机制是为**换号**设计的 —— 要换号才需要
                # 交接、才需要算缺口、才需要用 _marks 记时间锚。直通只有一个号
                # 一条线，不换号，所以一个都用不上；硬套上去的代价实测是
                # 每轮被判 CONTEXT_WINDOW_EXCEEDED（报全量 usage），界面空白。
                try:
                    pinned.direct = True
                except BaseException:        # noqa: BLE001
                    pass
                return pinned, base

            live = [b for b in self.bridges.values() if b.enabled]
            if not live:
                raise RuntimeError(
                    "账号池里没有可用账号：ds_auth.json 是空的，或者都被停用了")

            # 2026-09-25 新规矩：一个自定义组都没配 -> **没有号可轮**。
            # 这是用户明确要的：「没配组就没号可用」，不再退回单大组。
            if not self._groups():
                raise RuntimeError(
                    "轮询池没配任何分组：请在设置台建组并放入账号，"
                    "或者用 deepseek-chat@账号名 点名某个账号")

            cur = self.rr_active
            if cur is not None and not cur.enabled:
                cur = None
            if cur is not None and not self._yield_reason(cur):
                self.rr_active = cur
                # 组级条数在**派活这一刻**就 +1：组计数回答的是「这一组
                # 该分到的流量用完了吗」，那是派活时的问题。账号级的
                # turns_here 仍旧只数成功轮（它一直如此）。两条计数各管
                # 各的，故意不合并。
                self._cell()["turns"] = int(self._cell().get("turns") or 0) + 1
                # 2026-10-02：组轮次 +1。**只做这一件事**。
                #
                # mark_on 故意**不在这里**调：它记的是「我上次那一轮是几号」，
                # 必须等接手方那一发真去读缺口、把 marks_slice() 取完了才写，
                # 否则读到的是刚刷新的自己，缺口恒 0（实测踩到过）。
                # 写入点在 do_POST 的交接段，见那边的注释。
                _mg = group_of(list(self.bridges.keys())).get(
                    getattr(cur, "slug", ""), "")
                mark_turn(_mg)
                # 2026-10-03：分组进来 -> 窗口锚定名 = **组名**（不是号名）。
                self._set_anchor(cur, _mg)
                # 分组路一律清掉直通标记：同一个 Bridge 实例可能上一发是
                # @号名 直通、这一发是 @组名 轮到的，标记不能残留。
                try:
                    cur.direct = False
                except BaseException:        # noqa: BLE001
                    pass
                return cur, base
            # 让位：按序列换下一个，那个号会接自己最近那条会话（plan 的 latest 兜底）
            nxt = self._next_bridge(cur)
            if nxt is None:
                # 2026-09-25 新规矩：这里**不能**再兜 live[0] —— 那是全体
                # 启用账号里的第一个，可能是个没分组的号，等于把「只跑
                # 自定义分组」偷偷破掉。轮不出来就是真没号可轮，报错。
                raise RuntimeError(
                    "当前分组里没有可用账号：请检查分组配置，"
                    "或者用 deepseek-chat@组名 切到有人的组")
            self.rr_active = nxt
            self.rr_since = time.time()   # 这个号从此刻起干这条对话
            self._cell()["turns"] = int(self._cell().get("turns") or 0) + 1
            # 2026-10-02：组轮次 +1；顺手给老号记一笔 off（纯诊断，不参与
            # 算缺口）。mark_on 同上一路，留到 do_POST 读完缺口再写。
            _mg = group_of(list(self.bridges.keys())).get(
                getattr(nxt, "slug", ""), "")
            mark_turn(_mg)
            if cur is not None:
                mark_off(_mg, getattr(cur, "slug", ""))
            # 2026-10-03：分组轮转 -> 窗口锚定名 = **组名**（不是号名）。
            self._set_anchor(nxt, _mg)
            # 同上一处：分组路一律清掉直通标记（防同一实例残留）。
            try:
                nxt.direct = False
            except BaseException:            # noqa: BLE001
                pass
            return nxt, base




def chunk(model, delta=None, finish=None, usage=None):
    body = {
        "id": "chatcmpl-" + _short_id(),
        "object": "chat.completion.chunk",
        "created": int(time.time()),
        "model": model,
        "choices": [{"index": 0, "delta": delta or {},
                     "finish_reason": finish}],
    }
    if usage:
        body["usage"] = {
            "prompt_tokens": usage["prompt"],
            "completion_tokens": usage["completion"],
            "total_tokens": usage["prompt"] + usage["completion"],
            "prompt_tokens_details": {
                "cached_tokens": usage.get("cached", 0)},
        }

    return f"data: {json.dumps(body, ensure_ascii=False)}\n\n".encode()



class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    pool = None          # 账号池，make_server 现造子类时绑上去
    bridge = None        # 这一条请求挑中的账号（实例属性，每请求一份）
    server_version = "ds-bridge/1.0"

    def log_message(self, fmt, *args):       # 默认那行日志太吵
        pass

    # ---------- 基础回包 ----------

    def _json(self, code, obj):
        raw = json.dumps(obj, ensure_ascii=False).encode()
        try:
            self.send_response(code)
            self.send_header("content-type", "application/json; charset=utf-8")
            self.send_header("content-length", str(len(raw)))
            self.send_header("connection", "close")
            self.end_headers()
            self.wfile.write(raw)
        except OSError:
            # 客户端等不及先走了很正常。这里再抛的话，do_POST 的兜底又会来一次
            # _err，再炸一次，最后 socketserver 把三层 traceback 全喷出来。
            self._dead = True


    def _err(self, code, msg, kind="invalid_request_error"):
        self._json(code, {"error": {"message": msg, "type": kind}})

    def _rotate_away(self):
        """报 429 之后顺手把当前账号换掉（见 Pool.rotate_away）。

        换号本来要等下一个请求进来才做；限流之后请求可能很久才来，那时让位
        窗口已经过期，同一个号还会被派一遍。任何一步出错都不能影响回包。
        """
        try:
            if self.pool is not None and self.bridge is not None:
                self.pool.rotate_away(self.bridge)
        except Exception:                        # noqa: BLE001
            pass

    # 2026-09-22 删除 _err_overflow：不再有任何路径向 dsh 报 context_length_exceeded。
    # 桥不再指挥客户端压缩上下文 —— 那一动作会重写消息列表，把窗口号和认亲一起打断。

    def do_GET(self):
        path = self.path.split("?")[0].rstrip("/")
        if path in ("/v1/models", "/models"):
            now = int(time.time())
            self._json(200, {"object": "list", "data": [
                {"id": m["id"], "object": "model", "created": now,
                 "owned_by": m["owned_by"]} for m in self.pool.catalog()]})
        elif path in ("/health", "/v1/health"):
            # 2026-09-30：把 Safety Gate 的硬上限透出来 —— 运行态一眼能核实
            # 当前进程加载的是哪个值，不用去翻源码或重启日志。
            _acc = self.pool.status()
            # 2026-10-01（用户口径：「为何需要时间统计，还有别的方法」）：
            # **不统计「正常该多久」，只报「这一发已经跑了多久」。**
            # 挂死是一个可见的事实：inflight_secs 会一路涨。
            # 阈值 600s 不是量出来的分布，是「远超任何合理单发」的粗上限 ——
            # 实测正常单发 10~90 秒，600 秒是 7 倍余量，撞上就是真卡住。
            _stuck = [a for a in _acc
                      if float(a.get("inflight_secs") or 0) > STUCK_SEC]
            self._json(200, {"ok": not _stuck,
                             "hard_limit_chars": Bridge.HARD_LIMIT_CHARS,
                             "gate_trips": Bridge.GATE_TRIPS,
                             "gate_last_chars": Bridge.GATE_LAST_CHARS,
                             "empty_keep": Bridge.EMPTY_KEEP,
                             "empty_dropped": Bridge.EMPTY_DROPPED,
                             "handoff_local": Bridge.HANDOFF_LOCAL,
                             "stuck_secs": STUCK_SEC,
                             "stuck": [a.get("slug") for a in _stuck],
                             "accounts": _acc})
        elif path in ("/relay", "/v1/relay"):
            self._relay_get()
        elif path in ("/dsh", "/v1/dsh"):
            self._dsh_goto()
        else:
            self._err(404, f"没有这个路径：{self.path}")

    def _dsh_goto(self):
        """第424步：把浏览器送进 dsh 界面，带一个现签的浏览器 cookie。

        为什么这么做：dsh 的 ?token= 是进程内存里的随机值，托盘抓到的
        永远是别的进程的旧值，点开就 401。但 cookie 的签名密钥是落盘的，
        桥能现签一个真的。

        跨端口是**成立**的：cookie 作用域只看 host 和 path，不含端口。
        所以从 192.168.3.203:11999 设的 cookie，跳到 :3080 时照样带上。
        cookie 名和签名载荷都绑 authority（host:port），这里按浏览器
        实际要访问的那个 authority 签。
        """
        host = (self.headers.get("Host") or "").strip()
        if not host:
            self._err(400, "没有 Host 头，认不出该签哪个 authority")
            return
        name = host.split(":")[0]
        if name not in ("127.0.0.1", "localhost") and name not in _all_lan_ips():
            self._err(403, "这个 Host 不在本机的局域网地址里：%s" % host)
            return
        authority = "%s:%d" % (name, DSH_PORT)
        ck, val = _dsh_cookie(authority)
        target = "http://%s/" % authority
        if not ck:
            self._plain(200, "dsh 的签名密钥没读到（%s）。\n"
                              "文件在就说明还有救；不在就得重启一次 dsh 让它重新生成。\n"
                              "直连地址：%s" % (ROUTE_CRED, target))
            return
        body = ("<meta charset=utf-8>正在进入 dsh 界面…"
                "<script>location.replace(%s)</script>" % json.dumps(target))
        raw = body.encode("utf-8")
        self.send_response(303)
        self.send_header("Location", target)
        self.send_header("Cache-Control", "no-store")
        self.send_header("Set-Cookie", "%s=%s; Path=/; Max-Age=604800; HttpOnly; SameSite=Lax"
                         % (ck, val))
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    # ---------- 接管口：读状态 / 发指令 ----------
    # 2026-09-21 加。池子的唯一入口本来是 do_POST -> resolve，只能看不能管。
    # 这一段给出一条管理通道，不动原有路由：
    #   GET  /relay  读全量状态（谁占着、几条、空几次、待交接）
    #   POST /relay  {'action': ..., 'slug': ..., 'value': ...}
    # 指令只改内存里的池子状态，不写盘、不重启、不碰上游。
    def _relay_get(self):
        p = self.pool
        act = getattr(p, 'rr_active', None)
        rows = []
        for a in p.status():
            rows.append({k: a.get(k) for k in (
                'slug', 'name', 'enabled', 'state', 'turns_here',
                'turn_limit', 'empty_streak', 'last_empty',
                'cursor', 'wait',
                # 2026-09-25：这个号在不在轮询序列里。未分组的号
                # in_pool=False，界面把它标成「不在轮询」——但
                # turns_here 照样带出去，交互次数要显示。
                'in_pool', 'rr_pos')})
        # 2026-09-25 分组：多带一个 groups 数组和当前组。**原有键一个不动**，
        # 老界面（pool_dash / ds_gui / state.py）照旧读它们。
        grp = []
        try:
            gtop = p._gid()
            for g in p._groups():
                cell = p._cell(g['id'])
                grp.append({
                    'id': g['id'], 'name': g['name'],
                    'slugs': g['slugs'], 'count': len(g['slugs']),
                    'enabled': g['enabled'], 'implicit': g['implicit'],
                    'turn_limit': p._group_limit(g['id']),
                    # 2026-09-25 监控台：要分清这个上限是「本组自己设的」还是
                    # 「跟随池子继承来的」。turn_limit 是解析后的有效值，这里再
                    # 补原始配置值（0 = 本组没设，往下层要）。
                    'turn_limit_set': float(g.get('turn_limit') or 0.0),
                    # order 是组内真正参与轮换的号（已按 enabled 过滤过）。跟
                    # slugs 不一定一样：slugs 含已停用的号，拿它算「下一个是谁」
                    # 下标会对错人。面板直接读这个，不用自己猜。
                    'order': [b.slug for b in p._group_order(g['id'])],
                    'turns': int(cell.get('turns') or 0),
                    'cursor': int(cell.get('cursor') or 0),
                    'active': getattr(cell.get('active'), 'slug', None),
                    'current': g['id'] == gtop})
        except Exception:
            grp = []
        self._json(200, {
            'ok': True, 'relay': 'ds_bridge',
            'rr_active': getattr(act, 'slug', None),
            'rr_since': getattr(p, 'rr_since', 0.0),
            'rr_turn_limit': getattr(p, 'RR_TURN_LIMIT', 0.0),
            'rr_group': getattr(p, 'grp_top', ''),
            'groups': grp,
            'swap_pending': sorted(
                (getattr(p, 'swap_pending', None) or {}).keys()),
            'accounts': rows})
    def _relay_ctl(self, req):
        p = self.pool
        act = (req.get('action') or '').strip()
        slug = req.get('slug') or ''
        if act not in ('cursor', 'turn_limit', 'swap_now',
                       'reset_empty', 'turns_here',
                       'group_swap', 'group_pin', 'restamp'):
            self._err(400, 'action 只能是 cursor / turn_limit / swap_now / '
                           'reset_empty / turns_here / group_swap / group_pin')
            return
        # 2026-09-25 分组：整组让位 / 指定当前组。两个都只动内存，不写盘。
        if act == 'group_swap':
            was = getattr(p, 'grp_top', '')
            gid = p._next_group()
            self._json(200, {'ok': bool(gid), 'action': act,
                             'from': was, 'to': gid})
            return
        if act == 'group_pin':
            want = slug or (req.get('group') or '')
            ids = [g['id'] for g in p._groups()]
            if want not in ids:
                self._err(404, '没有这个组：%s（现有：%s）'
                          % (want, '、'.join(ids) or '无'))
                return
            p._enter_group(want)
            self._json(200, {'ok': True, 'action': act, 'group': want})
            return
        if act == 'restamp':
            with p.lock:
                for _b in p.bridges.values():
                    p._stamp(_b)
            self._json(200, {'ok': True, 'action': act, 'n': len(p.bridges)})
            return
        if act == 'cursor':
            self._json(200, {'ok': bool(p.set_cursor(slug)),
                             'action': act, 'slug': slug})
            return
        if act == 'turn_limit':
            p.RR_TURN_LIMIT = float(req.get('value') or 0)
            self._json(200, {'ok': True, 'action': act,
                             'value': p.RR_TURN_LIMIT})
            return
        if act == 'swap_now':
            cur = getattr(p, 'rr_active', None)
            nxt = p._next_bridge(cur)
            p.rr_active = nxt
            p.rr_since = time.time()
            self._json(200, {'ok': True, 'action': act,
                             'from': getattr(cur, 'slug', None),
                             'to': getattr(nxt, 'slug', None)})
            return
        br = p.bridges.get(slug)
        if br is None:
            self._err(404, '没有这个账号：%s' % slug)
            return
        if act == 'reset_empty':
            br.empty_streak = 0
            br.last_empty = ''
            br.empty_at = 0.0
            self._json(200, {'ok': True, 'action': act, 'slug': br.slug})
            return
        if act == 'turns_here':
            br.turns_here = float(req.get('value') or 0)
            self._json(200, {'ok': True, 'action': act, 'slug': br.slug,
                             'turns_here': br.turns_here})
            return
        self._err(400, 'action 只能是 cursor/turn_limit/swap_now/'
                       'reset_empty/turns_here')
    def do_POST(self):
        # [临时探针 2026-09-22] dsh 每一发到底带了哪些头 —— 判能不能拿稳定 session id
        # 当窗口身份。写独立文件、包 BaseException，绝不影响这一发。
        try:
            _sig = "|".join(sorted(self.headers.keys()))
            if _sig not in _HDR_SEEN:
                _HDR_SEEN.add(_sig)
                _ls = [time.strftime("%Y-%m-%d %H:%M:%S") + "  " + self.path + chr(10),
                       "    all = " + _sig + chr(10)]
                for _k in sorted(self.headers.keys()):
                    _lk = _k.lower()
                    if _lk.startswith("x-") or "session" in _lk or "user" in _lk \
                            or "purpose" in _lk or "harness" in _lk:
                        _ls.append("    %s = %s" % (_k, str(self.headers.get(_k))[:140]) + chr(10))
                with open(str(HDR_PROBE), "a", encoding="utf-8") as _f:
                    _f.writelines(_ls)
        except BaseException:
            pass
        path = self.path.split("?")[0].rstrip("/")
        chat = path in ("/v1/chat/completions", "/chat/completions")
        resp = path in ("/v1/responses", "/responses")
        if path in ("/relay", "/v1/relay"):
            try:
                n = int(self.headers.get("content-length") or 0)
                req = json.loads(self.rfile.read(n) or b"{}")
            except (ValueError, json.JSONDecodeError):
                self._err(400, "relay 请求体不是合法 JSON")
                return
            self._relay_ctl(req)
            return
        if not (chat or resp):
            self._err(404, f"没有这个路径：{self.path}")
            return
        try:
            n = int(self.headers.get("content-length") or 0)
            req = json.loads(self.rfile.read(n) or b"{}")
        except (ValueError, json.JSONDecodeError) as exc:
            self._err(400, f"请求体不是合法 JSON：{exc}")
            return

        # [临时探针] body 里有哪些字段，有没有 sessionId/purpose/user —— 判能不能拿它当窗口身份
        try:
            _bk = sorted(req.keys())
            _extra = {k: str(req[k])[:90] for k in req
                      if k in ("sessionId", "purpose", "user", "session_id",
                               "session", "requestId", "metadata", "instructions")}
            with open(str(BODY_PROBE), "a", encoding="utf-8") as _f:
                _f.write(time.strftime("%Y-%m-%d %H:%M:%S") + "  keys="
                         + "|".join(_bk) + chr(10))
                _f.write("    extra=" + json.dumps(_extra, ensure_ascii=False)
                         + chr(10))
        except BaseException:
            pass

        if resp:
            messages, tools = responses_to_messages(req)
        else:
            messages, tools = req.get("messages"), req.get("tools") or []
        if not isinstance(messages, list) or not messages:
            self._err(400, "没有可用的输入（messages / input 是空的）")
        # 2026-10-01 第442步（用户口径：「你先看看怎么存怎么去燥」）：
        # **把客户端原样发来的 messages 存一份。**
        #
        # 位置刻意放在「解析完、还没做任何加工」这一刻 ——
        # 下面所有逻辑（plan/build_prompt/tail_of）都会往 prompt 里拼东西，
        # 一旦拼过就再也分不出「哪些是用户说的、哪些是桥加的」。
        #
        # 整个包 BaseException：存档失败绝不能把这一发请求带塌。
        # 2026-10-01 第443步修：这里必须记**真实账号**，不能记请求里那段。
        # req["model"] 形如 "deepseek-reasoner@剪辑" —— @ 后面是组名；
        # 用户也可能写 @309（那才是账号）。原来直接切 @ 后半段，
        # 结果整份存档里 slug 全是「剪辑」「test」，按账号一查全空，
        # 差集永远建不起来。账号由下面的 resolve() 定，所以改到那之后写。
        try:
            _reqmodel = str(req.get("model") or "")
        except BaseException:            # noqa: BLE001
            _reqmodel = ""

        # 2026-10-01 第462步：把本轮工具表挂到 self，_switch_emit 要用它判断
        # 「这个工具 dsh 给没给」—— 决定是原样发还是包成 run_code（PTC）。
        self._tools_now = tools
        # 2026-10-03（诊断报告 5.1 / 建议第 3 条）：**每轮把模式判定记一行。**
        # 原来这个判定是四处各自猜，且走错分支时不打任何日志 ——
        # 出问题只能靠「全落空」那条可惜的提示，或者事后翻存档反推。
        # 这一行是排查「unknown tool」类报错的第一现场，代价只有一个 mode 词。
        try:
            _mode = detect_mode(tools)
            self.bridge.note("  ▷ 工具表 %d 个｜模式=%s%s"
                             % (len(tools or []),
                                "PTC" if _mode == "ptc" else "标准",
                                "（有 run_code）" if _mode == "ptc" else "（无 run_code）"))
        except BaseException:            # noqa: BLE001
            pass
        # 2026-10-03（查「tools=51 了为什么 has_tool 还是 False」）：
        # **把本轮工具表的名字落盘一次**（只写名字，不写 schema；按集合去重，
        # 同一批工具只记一行）。只读探针，写失败静默，不影响这一发。
        try:
            _names = []
            for _t in (tools or []):
                _fn = _t.get("function") if isinstance(_t, dict) else None
                _fn = _fn or (_t if isinstance(_t, dict) else {})
                _nm = str(_fn.get("name") or "")
                if _nm:
                    _names.append(_nm)
            _sig = "|".join(sorted(_names))
            if _sig not in _TOOLNAMES_SEEN:
                _TOOLNAMES_SEEN.add(_sig)
                with TOOLNAMES_PROBE.open("a", encoding="utf-8") as _f:
                    _f.write("%s  n=%d  %s\n" % (
                        time.strftime("%Y-%m-%d %H:%M:%S"), len(_names), _sig))
        except BaseException:            # noqa: BLE001
            pass

        want = req.get("model") or "deepseek-chat"

        # ---- ##切组## 隐式切组命令（2026-09-27 第357步；第369步前移）----
        # 用户口径：对话里发 ##切组##，桥自己认出这一发，不走上游，直接用
        # dsh 自己的 ask_user_question 弹选择框让用户点。
        # 必须在 pool.resolve 之前判：池子里一个分组都没有时 resolve 直接抛
        # 「没有可用账号」，请求在到达切组分支前就被 503 挡掉 —— 而「一个组
        # 都没有」恰恰是最需要靠 ##切组## 建组的状态（用户第369步删掉 g1）。
        # 这一支只用 want 当模型名，桥自己造响应，不需要真挑到号。
        if SWITCH_CMD in _last_user_text(messages):
            try:
                if self._switch_dialog(want, messages):
                    return
            except Exception as _sexc:            # noqa: BLE001
                try:
                    self.bridge.note("  切组对话出错：" + str(_sexc)[:200])
                except Exception:                 # noqa: BLE001
                    pass

        # ---- ##路由## 隐式查地址命令（2026-09-27 第373步）----
        # 跟 ##切组## 一个位置：在 pool.resolve 之前，池子空也能用。
        if ROUTE_CMD in _last_user_text(messages):
            try:
                if self._route_dialog(want, messages):
                    return
            except Exception as _rexc:            # noqa: BLE001
                try:
                    self.bridge.note("  查地址出错：" + str(_rexc)[:200])
                except Exception:                 # noqa: BLE001
                    pass

        # ---- 组路径未定 -> 问 dsh 要工作区（2026-10-01 第462步）----
        # 用户口径：「桥回的命令触发dsh命令返回工作区位置 再把当前发消息的组
        #   拼上 不就是真实目录了吗」
        #
        # 只在**两件事同时成立**时问一次：
        #   · 这个组的路径还没定（group_root_of 返回空）
        #   · 这一轮是真请求（不是 dsh 生成标题那种 util 小请求）
        # 问过就记住（_ws_asked），不反复问 —— 用户不理就下次再说，
        # 绝不用桥自己的目录顶替（那就是「落在桥身上」）。
        try:
            # 先看这一轮是不是把上回问的答案带回来了 —— 带了就记账，写进组路径
            _ws = ws_from_messages(messages)
            _wgrp0 = group_name_of(str(getattr(self.bridge, "slug", "") or ""))
            if _ws and _wgrp0:
                _okw, _msw = switch_set_root(_wgrp0, _ws)
                if _okw:
                    self.bridge.note("  ✓ dsh 报的工作区：%s -> 组「%s」路径已定"
                                     % (_ws, _wgrp0))
        except Exception:            # noqa: BLE001
            pass
        try:
            _wgrp = group_name_of(str(getattr(self.bridge, "slug", "") or ""))
            if _wgrp and not group_root_of(_wgrp):
                _wkey = str(getattr(self.bridge, "slug", ""))
                # 2026-10-01 第462步：**先确认 run_code 在本轮工具表里。**
                # PTC 下只有 run_code；标准模式下它是标配之一。
                # 不在就别问 —— 问了就是一个红叉（用户实测过），白绕一轮。
                if _wkey not in _WS_ASKED and has_tool(tools, "run_code"):
                    _WS_ASKED.add(_wkey)
                    if self._ask_workspace(model, _wgrp):
                        return
        except Exception as _wexc:            # noqa: BLE001
            try:
                self.bridge.note("  问工作区出错：" + str(_wexc)[:120])
            except Exception:
                pass

        # ---- ##模型## 隐式查模型命令（2026-09-27 第419步）----
        # 用户口径：「##模型## 获取配置模型，手动去配置也行，桥给配置也行」。
        # 跟 ##路由## 同一个位置、同一种单轮形态：桥自己回，不走上游、不弹框。
        if MODEL_CMD in _last_user_text(messages):
            try:
                if self._model_dialog(want, messages):
                    return
            except Exception as _mexc:            # noqa: BLE001
                try:
                    self.bridge.note("  查模型出错：" + str(_mexc)[:200])
                except Exception:                 # noqa: BLE001
                    pass

        # 挑账号必须在拿到 messages 之后：认亲要靠消息指纹，不然聊到一半的
        # 对话会被派给另一个账号，等于换了个空白上下文。
        try:
            self.bridge, model = self.pool.resolve(want, messages)
            # 稳定窗口身份（治本）：dsh 的 sessionId 在 dsh_session_log.session.id 里。
            # 有了它，窗口身份不再靠「第一条 user 消息 hash」猜，串窗口、压缩后认不出都根除。
            _sl = req.get("dsh_session_log")
            _sid = ""
            if isinstance(_sl, dict):
                _s = _sl.get("session")
                if isinstance(_s, dict):
                    _sid = str(_s.get("id") or "")
            self.bridge.sid = _sid
            # 2026-10-01 第462步（用户口径「你不会让桥回一个 dsh 的命令来获取当前
            #   分组真实路径吗？」）：**先看清 dsh 自报了哪些字段。**
            #
            # 桥一直只从 dsh_session_log 取 session.id，**从没看过它完整结构**。
            # 而「让桥去问 dsh 工作区在哪」的前提，是先知道 dsh 报了什么。
            # 这里加一个临时探针把整份落盘 —— 只为看结构，看完就删。
            # 探针失败绝不能带塌请求。
            try:
                _pj = json.dumps(_sl, ensure_ascii=False, default=str)
                with open(str(BODY_PROBE) + ".sesslog", "a", encoding="utf-8") as _f:
                    _f.write(time.strftime("%H:%M:%S") + "  " + _pj[:4000] + chr(10))
            except BaseException:        # noqa: BLE001
                pass
            # 存档放在 resolve 之后：此刻才拿到真实账号 slug。
            try:
                save_raw_messages(messages,
                                  slug=str(getattr(self.bridge, "slug", "") or ""),
                                  model=_reqmodel, tools=tools)
            except BaseException:        # noqa: BLE001
                pass
        except KeyError:
            self._err(404, f"没有这个模型：{want}，可选 "
                           f"{[m['id'] for m in self.pool.catalog()]}",
                      "model_not_found")
            return
        except RuntimeError as exc:                  # noqa: BLE001
            self._err(503, str(exc), "no_account")
            return

        # ---- ##切组## 隐式切组命令（2026-09-27 第357步）----
        # 用户口径：对话里发 ##切组##，桥自己认出这一发，不走上游，
        # 直接用 dsh 自己的 ask_user_question 弹选择框让用户点。
        # 判据只看 messages，桥不存任何状态：多窗口并发、桥重启都不会串。
        if SWITCH_CMD in _last_user_text(messages):
            try:
                if self._switch_dialog(model, messages):
                    return
            except Exception as _sexc:            # noqa: BLE001
                try:
                    self.bridge.note("  切组对话出错：" + str(_sexc)[:200])
                except Exception:                 # noqa: BLE001
                    pass

        # 超过这个模型的输入上限就不打上游，直接回一个 dsh 认得的 overflow 错误。
        # dsh 收到后会自己裁工具结果 + 压缩历史 + 重试这一轮（见
        # Bridge.OVERSIZE_TEMPLATE 的注释），比我们在这边瞎截靠谱得多。
        # 必须在开流之前判：一旦 _open_stream() 发了 200，就没法再回错误码了。
        # 2026-09-22 删控制通道：oversize() 现在恒返回空串（改由桥自己按 send_budget 裁
        # 尾部后上传），下面那段 400 + context_length_exceeded 的早退分支已无意义。
        # 仍然调用它，是为了记 last_chars / peak_chars 并写一条日志。
        self.bridge.oversize(model, messages, tools)


        # 刚换号：让老号吐一段交接，贴在接手的号这一轮前面。</
        # 老号是 _handoff() 记下的，取一次就清。失败静默跳过，不挡这一轮。
        #
        # 2026-09-21 修：这段必须在 oversize 检查**之后**。超限那一轮会被
        # dsh 压缩后重发，而重发时 swap_pending 若已被清掉，交接就白做了 ——
        # 而超限是必然发生的（实测发送峰值 316656 字）。放在后面，超限那轮
        # 直接 return，不消耗 swap_pending，压缩重发时照样取得到老号。
        #
        # 2026-09-21 改：槽位按**接手账号的 slug**取。原来是全局单槽，
        # 两个窗口同时换号会互相顶掉。取走即删，别的窗口不受影响。
        hand = ""
        pend = getattr(self.pool, "swap_pending", None) or {}
        _my = getattr(self.bridge, "slug", "")
        # 2026-09-22 修：只有「这一发真的会把交接拼进 prompt」才取走。
        # plan() 的 util 分支（dsh 生成标题/摘要）走 build_prompt，绕过了拼交接
        # 的 _wh()，也不清 handoff_note。原来这里无条件 pop：换号一旦正好落在
        # util 那一发上 —— 交接被取走 → handoff_dump 白花一次上游请求 →
        # 下一发真请求 hand="" 又把它覆盖掉，交接彻底丢。现在 util 那一发
        # 原样留着 swap_pending，下一发真请求照样取得到。
        _had = bool(_my and _my in pend)
        _util = bool(self.bridge.is_util(messages, tools))
        if (_my and _my in pend and not _util):
            old_br, _at, _since = pend.pop(_my)
            if old_br is not None and old_br is not self.bridge:
                # 2026-09-25：同样包成带 nc 的 Keys。换号交接走的是
                # old.cache.match(keys, fresh=False)；不包的话 match() 会退回
                # 拿全量指纹去比。结果碰巧一样（row 的 nc 本来就不含公共消息），
                # 但两条路径判据不一致，以后出问题不好解释。
                _hk, _hi = SessionCache.fingerprint(messages)
                _hk = SessionCache.with_nc(_hk, _hi)
                # 2026-09-26 第88步加：handoff_mode 开关
                #   "ai"    = 向老号要一段交接（默认，原行为）
                #   "local" = 不向老号发请求，直接用本地数据代写
                _hmode = str(empty_policy(_my).get("handoff_mode", "ai")
                             or "ai").lower()
                _local_h = (_hmode == "local")
                self.bridge.attach_note_local = _local_h
                # 2026-10-01 第450步：**交接抽取器优先。**
                #
                # 用户口径：「其实过程不重要 知道结果 及坑 就可以了 还有用的
                # 哪些工具」「多维度的去分析」「同一件事的重复发 去重后自然归零」。
                #
                # 它比「代写交接」强在三点：
                #   1. 素材是**九维分析**过的（干活率/时间/工具/产出/重复率/
                #      连续/streak/跨号重复），不是复述一段回复；
                #   2. 输出五层，按接手方真正需要的顺序排（硬约束/坑/已做过/
                #      工具/状态），体积恒定，不随历史长度涨；
                #   3. **跨号重复那一层能直接省掉重复的工具调用** ——
                #      实测全量 4559 个 turn 里有 116 种调用被多个号重复跑过，
                #      白跑 870 次（read 一个调用被 6 个号跑了 136 次）。
                #
                # 拿不到素材就返回空串，原地退回下面的路径 —— 不改任何已有行为。
                _hx = mark_handoff(_my, self.bridge)
                if _hx and len(_hx) > 200:
                    hand = _hx
                    self.bridge.note("  交接：抽取器 %d 字（硬约束/坑/已做过/工具/状态）"
                                     % len(_hx))
                else:
                    _hx = ""
                # 2026-10-01 第443步：增量上下文。
                # 用户口径：「同样的历史不在重发 只发他不知道的 而且去燥」
                # 代写交接让四个窗口各自不同（实测 3-19% 差异全来自它），
                # 既破坏缓存一致又是噪声。真实历史差集取代之。
                _dc = ""
                if not _hx and bool(empty_policy(_my).get("delta_context", False)):
                    try:
                        _dc = build_delta_context(group_name_of(_my), _my)
                    except BaseException:        # noqa: BLE001
                        _dc = ""
                if _dc and not _hx:
                    hand = _dc
                    self.bridge.note("  交接：真实历史差集 %d 字（本组最近 3 轮，已去燥）"
                                     % len(_dc))
                elif _hx:
                    pass                       # 抽取器已经给出交接，别再覆盖
                elif _local_h:
                    self.bridge.note("  交接：本地上传模式，跳过向老号要交接")
                    hand = old_br.local_handoff("用户选择本地上传模式")
                else:
                    hand = self.bridge.handoff_dump(old_br, _hk, model, _since)
                # 2026-10-02：**换号时不再生成「检查点」，改成「快照」。**
                #
                # 用户口径：「不要检查点了 有快照了要他干嘛」。
                #
                # 检查点那条路（checkpoint_dump 主动问老号要一份，门槛 15 万字）
                # 是多余的：
                #   · 它跟快照是同一个东西（都是 ## Primary Request and Intent
                #     开头的八段式），只是产生方式不同；
                #   · 它要**额外烧一次上游请求**去问老号，而快照是 dsh 现成能压的；
                #   · 门槛 15 万字意味着小对话永远不该生成它。
                #
                # 快照的取法见下面「换号那一发：按空缺时间取一段快照」那段：
                # 按接手方的 at 到当前时间压，由 dsh 的出厂压缩链路产出
                # （用户口径「要利用dsn算法咱们不专业」）。
                #
                # 保留的是**接住模型自己输出的八段式**那条路（last_checkpoint），
                # 那是免费的 —— 模型自己压了，桥只是接住存下来。
        # 2026-10-02 标记法：**没换号的那些轮也要看缺口。**
        #
        # 用户口径「每个id对应窗口起始做个标记 换号做个标记 下次轮到在从本地
        # 推算 中间经过了多少历史 在去燥推给他」。标记本身跟换不换号无关 ——
        # 换号只是打标记的**时机之一**，不是唯一。
        #
        # 实测剪辑组 2651 轮里只有 317 次换号（平均 8.4 轮才换一次），原来
        # 交接段整个长在 `if _my in swap_pending` 里，于是 68% 的轮次根本
        # 不算缺口 —— 号换回来时缺的那几十轮没人补。这一段就是补这个洞。
        #
        # 只在**没走换号交接**时兜底（hand 为空）：换号那条已经把缺口算过了，
        # 再算一遍会覆盖掉更全的那份。util 那一发不算 —— 它不拼 prompt，
        # 算了也白算，还会把 on 提前刷掉（下一发真请求就读不到缺口了）。
        if not hand and _my and not _util:
            _mt = ""
            try:
                _mt = mark_handoff(_my, self.bridge)
            except BaseException:        # noqa: BLE001
                _mt = ""
            if _mt and len(_mt) > 200:
                hand = _mt
        # 只设不清：清是 plan() 的活（谁用了谁清，见 L1578/L1586）。
        # 这里再无条件赋空，会把上一发没消费掉的交接抹掉。
        if hand:
            self.bridge.handoff_note = hand
        # 2026-09-22 加：交接被谁取走落成事件。util=1 = 这一发是 dsh 的
        # 标题/摘要小请求，交接被有意留给下一发真请求（改之前是直接丢）。
        if _had or hand:
            emit("take", slug=_my, chars=len(hand or ""),
                 util=1 if _util else 0)

        # ===== 换号那一发：按空缺时间取一段快照（2026-10-02）=====
        #
        # 用户口径（原话，逐字）：
        #   「要利用dsn算法咱们不专业」  —— 压缩用 dsh 出厂的链路，桥不自己压；
        #   「快照是按空缺时间 补 只要换号就按下一个号上次结束时间到当前时间的
        #     快照 并且附带当前最近没经过快照压缩的详细事件让ai知道该做什么了」；
        #   「如果连续空回复就不用一直快照 就快照一次就行」；
        #   「不要检查点了 有快照了要他干嘛」。
        #
        # ## 谁产出这个快照
        #
        # 桥自己压不了（手搓过两版：按骨架压 / 按动作签名压，实测只压掉 4%，
        # 还误杀方案演进链）。快照只能由 dsh 出厂链路产出 ——
        # 走 `get_range_context_compact` 工具（tool-range-compact 插件）。
        #
        # 而那个工具住在 dsh 里（要 exec.agent.session），桥的 ds_api.ask() 是
        # 绕过 dsh 直连上游的，调不到它。所以桥的做法是：**造一个 run_code 的
        # tool_call 回给 dsh**（`_rc_emit`），dsh 收到就执行 —— 切组 ##切组## 和
        # 问工作区用的就是这条，是既有能力。
        #
        # ## 只取一次
        #
        # 判据在 mark_snap_pending：snap_at == at 就跳过。at 是「接手方上次停手
        # 的时刻」，**at 不变 = 它没离开过**（连续空回复、连续几轮没轮到都属于
        # 这种）-> 不重复取。
        #
        # ## 为什么不 return
        #
        # `_rc_emit` 本身是「桥接管这一发」。但这里**不能接管** —— 那一发的
        # 交接单（handoff_note）刚存好，要发给接手方；接管等于把它吞了。
        # 而且快照是**下一发**才用得上（dsh 执行完才有结果），
        # 所以这里只把调用发出去，这一发照常走。
        # 2026-10-02 修（实测抓到的）：判据**不能是 `_had`**。
        #
        # `_had` = swap_pending 里有没有这个号，而那东西只在「上一发正常走完、
        # 轮到下一发时」才存在。实测限流场景下根本走不到这里：
        #   19:11:12 [309] <- 无正文！-> 按频繁限流处理（429 + 冷却 + 换号）
        #   19:11:15 [779] 换号 309 -> 779  限流让位
        # 换号是在**报 429 那一刻**由 rotate_away 内部做的，那一发已经结束了；
        # 而接下来几发又都因为挑不出号走了 `_err(503, no_account)` 提前 return。
        # 结果：四个号换了一整圈，快照一次都没发（实测 0 次）。
        #
        # 改成**按号判**：只要轮到某个号、而它还没为当前的 at 取过快照，就取。
        # 这跟换不换号无关 —— 标记法本来就是「每个 id 一条记号」的口径。
        if _my and not _util:
            try:
                _need, _sat, _swhy = mark_snap_pending(
                    group_name_of(_my), _my)
                # 2026-10-03（用户口径「每轮都检查 缺了才取」）：
                # **旧判据不再决定取不取。**
                #
                # 原来这里直接吃 mark_snap_pending 的 _need，而它是
                # 「snap_off == off 就 False」—— 同一个号取过一次就永远
                # 返回 False，**第一道闸就把路堵死了**，下面那层
                # 「缺不缺」的检查（_has_cp）根本没机会执行。
                # 所以「每轮检查缺不缺」从来没生效过。
                #
                # 现在判据只看一件事：**快照在不在手上。**
                #   pending_checkpoint 非空 -> 已在手，不取
                #   空                      -> 缺，取一次
                # 旧判据降级为日志参考，不再决定行为。
                # 2026-10-03（用户口径：「如果没变就继续干活」）：
                # **判据回到标记法的本意 —— 状态没变就不取。**
                #
                # mark_snap_pending 给的 _need 就是「off != snap_off」：
                #   相等 = 这个号没被交出去过 = 同一次接手 = 状态没变
                #   不等 = 它离开过又被轮回来 = 才该补缺口
                #
                # 我先前把它拆了，改成「pending_checkpoint 空就取」，结果：
                #   取快照失败 -> 仍为空 -> 下一轮又判「缺」-> 又取 -> 死循环
                # 而取快照那轮我还加了 return 不干活，于是：
                #   不干活 -> 没新内容 -> 压缩没东西可压 ->
                #   "summarization produced no text summary content" -> 更不干活
                # **自锁。**
                #
                # 现在：状态没变就直接干活 —— 干出来的活才是"变了"的东西，
                # 下一轮真要交接时，缺口里才有内容可压。
                _why_old = _swhy
                if not _need:
                    self.bridge.note("  快照：状态没变，继续干活（%s）" % _swhy)
                if _need:
                    # 2026-10-03：from 改用**真实工作断点**（台账最后一条
                    # 动作时刻），不再用 mark_on_time —— 后者每轮被刷成
                    # 「刚刚」，导致快照区间里只有「我刚调了本工具」，
                    # 压出 (none) 快照并自我引用（实测 32 次空转）。
                    _bp = work_breakpoint_time(group_name_of(_my), _my,
                                               fallback=_sat or 0.0)
                    _ok = self._rc_emit(
                        model, "get_range_context_compact",
                        {"from": int(_bp * 1000) if _bp else 0},
                        text="")
                    if _ok:
                        mark_snap_done(group_name_of(_my), _my)
                        # 2026-10-03 修：这里原来打 %s % _swhy，而 _swhy 里的
                        # "from=" 用的是 mark_snap_pending 内部的 at（= 旧口径
                        # 「刚刚」）—— 实际发出去的是上面的 _bp。日志与事实不符，
                        # 排查时会被带偏（我自己就被带偏过一次，误判「修复没生效」）。
                        # 现在**打印真正发出去的那个值**。
                        self.bridge.note(
                            "  快照：已让 dsh 压一段（实际 from=%d；旧判据：%s）"
                            % (int(_bp * 1000) if _bp else 0, _why_old))
                        # 2026-10-03：这里加过的 return 已撤回。
                        #
                        # 加它是想「取到快照再干活」。但它成了自锁：
                        #   取快照 -> dsh 执行 -> 这一轮 return 掉了
                        #   -> pending_checkpoint 仍空 -> 下一轮又判「缺」-> 又取
                        # 而且这一轮不干活，于是：不干活 -> 没新内容 ->
                        # 压缩没东西可压 -> "summarization produced no text
                        # summary content" -> 更不干活。
                        #
                        # 用户口径：「如果没变就继续干活」。取快照该是补充，
                        # 不是前置条件。所以这里照原样继续走，不 return。
            except BaseException as _sxe:        # noqa: BLE001
                try:
                    self.bridge.note("  快照指令失败（这一发照常走）："
                                     + str(_sxe)[:100])
                except BaseException:            # noqa: BLE001
                    pass



        self._opened = False
        self._proto = "responses" if resp else "chat"
        try:

            if resp:
                self._responses(model, messages, tools)
            elif tools or not req.get("stream", True):
                # 有工具时必须先把整段收完才知道是文本还是工具调用，所以不边收边吐
                self._buffered(model, messages, tools)
            else:
                self._streamed(model, messages)
        except Exception as exc:                     # noqa: BLE001
            # 桥接自己出 bug 也不能让客户端看到一个断掉的流：那边只会报
            # 「Stream ended without finish_reason」然后闷头重试 5 次。
            self.bridge.note(traceback.format_exc().strip())


            msg = f"[桥接内部错误] {type(exc).__name__}: {exc}"
            if self._opened and self._proto == "responses":
                self._write(revent("response.failed", response={
                    "id": "resp_" + _short_id(), "object": "response",
                    "status": "failed", "model": model, "output": [],
                    "error": {"code": "bridge_error", "message": msg}}))
            elif self._opened:
                self._write(chunk(model, {"content": msg}))
                self._write(chunk(model, {}, "stop"))
                self._write(b"data: [DONE]\n\n")
            else:
                self._err(500, msg, "bridge_error")

    # ---------- 两种出话方式 ----------


    def _open_stream(self):
        self._opened = True
        try:
            self.send_response(200)

            self.send_header("content-type", "text/event-stream; charset=utf-8")
            self.send_header("cache-control", "no-cache")
            self.send_header("connection", "close")
            self.end_headers()
        except OSError:
            self._dead = True


    # 写失败重试几次。http.client.ProtocolError（「Response ended prematurely」）
    # 不是 OSError 的子类，原来会直接穿过 _write 冒到 do_POST 外层，
    # 那一轮的流就此断掉，客户端只会看到「Stream ended without finish_reason」。
    WRITE_TRIES = 3

    def _write(self, raw):
        """写不出去就重试几次，再不行才放弃并让上游提前断流。"""
        for i in range(self.WRITE_TRIES):
            try:
                self.wfile.write(raw)
                self.wfile.flush()
                return True
            except (BrokenPipeError, ConnectionResetError,
                    ConnectionAbortedError):
                # 对面真关了，重试没意义
                self._dead = True
                return False
            except Exception as exc:             # noqa: BLE001
                # ProtocolError 这类：可能是分块编码/短写，重试一次常常就过
                try:
                    self.bridge.note(f"  写帧失败第 {i + 1} 次："
                                     f"{type(exc).__name__}: {exc}")
                except Exception:            # noqa: BLE001
                    pass                      # 记日志失败不能把写帧带下水
                if i + 1 >= self.WRITE_TRIES:
                    self._dead = True
                    return False
                time.sleep(0.15)
        return False

    # ---------- 隐式查地址 ##路由##（第373步）----------

    def _route_dialog(self, model, messages):
        """##路由## 的单轮对话。返回 True = 这一发桥自己回掉了。

        只问一档（内网/局域网/外网），答完就贴地址。轮次同样靠 messages
        推，桥不存状态。
        """
        real = _switch_real(_switch_answers(messages))
        if not real:
            self._switch_emit(model, [_mk(0, SWITCH_TOOL,
                                          {"questions": route_q1()})])
            return True
        ans = real[0] if real else ""
        if not ans.strip():
            self._plain(model, ROUTE_CANCEL)
            return True
        if not (ROUTE_IN in ans or ROUTE_LAN in ans or ROUTE_WAN in ans):
            # 认不出就重问一次，别丢一句「没认出来」把人挡回去。
            self._switch_emit(model, [_mk(1, SWITCH_TOOL,
                                          {"questions": route_q1()})])
            return True
        try:
            body = route_report(ans)
        except Exception as exc:                 # noqa: BLE001
            body = "查地址出错：%s" % exc
        self._plain(model, body)
        return True

    # ---------- 隐式查模型 ##模型##（第419步）----------

    def _model_dialog(self, model, messages):
        """##模型## 的单轮命令：不弹框，直接把清单与配置贴出来。

        用户口径：「##模型## 获取配置模型，手动去配置也行，桥给配置也行」。
        跟 ##路由## 同一种形态 —— 发一次就出结果，不需要多轮问答。
        """
        try:
            body = model_report(self.pool)
        except Exception as exc:                 # noqa: BLE001
            body = "查模型出错：%s" % exc
        self._plain(model, body)
        return True

    # ---------- 隐式切组（第357步）----------

    def _rc_emit(self, model, tool, args, text=""):
        """替桥造一条 tool_call 回给 dsh（切组 / 查地址 / 快照 都用这条）。

        2026-10-01 第462步（用户实测报错 + 选择方案 A）：
            Error: unknown tool "ask_user_question":
            only `run_code` is callable directly —
            call `ask_user_question` from inside a `run_code` program instead

        **PTC 模式下 dsh 只暴露 run_code**，桥造的其它 tool_call 一律被拒。
        但报错自己给了出路：**从 run_code 程序内部调它**。

        run_code 里的调用约定（历史 211 条例证，实测形态）：
            const r = await tools.pwsh({ command: "..." });
            const w = await tools.read({ file_path: "..." });
        即 `await tools.<工具名>(<参数对象>)`。

        ## 2026-10-03 修（用户口径「交接不利索，上棒跑的任务下一棒还要重复跑」）

        **原来的实现无条件包 run_code —— 标准模式下这是必错的。**

        实证（智普清言组存档 _relay_msgs.jsonl，5047 条消息）：
            桥造的调用（wsq*）:      140 个
            对应的 tool 结果  :      140 个   ← dsh 全都回了
            但结果内容**全部**是:      Error: unknown tool "run_code"   （30 字）
        即 **140 次快照指令，100% 被 dsh 拒收，一次都没成功。**

        后果链：快照取不到 -> pending_checkpoint 恒空 -> 附件里只有台账，
        没有「空缺期间发生了什么」-> 接手方只能自己 read/glob 重建状态 ->
        10 条额度被重建吃掉 -> 换号 -> 下一棒同样。**交接因此不利索。**

        判据跟 _switch_emit（13660 行）完全一致，那里早就做对了：
            工具表里有这个工具（标准模式）-> **原样发**，dsh 直接执行
            工具表里没有（PTC 模式）      -> 才包成 run_code
        """
        # 工具表里有没有 intended 这个工具？有就直发，别套壳。
        #
        # 2026-10-03 改（诊断报告 5.1 节）：这里原来只问 has_tool，不问模式。
        # 「工具不在表里」有两种可能，处置完全不同：
        #   PTC 模式   -> 该包 run_code（run_code 是唯一能直接调的）
        #   标准模式   -> 包 run_code 也是错的（表里没有 run_code），
        #                 属于桥自己要了一个不存在的工具，只能放弃。
        # 旧代码不分这两种，所以标准模式下一旦 has_tool 为假就发 run_code，
        # 而标准模式根本没有 run_code —— 就是那 140 次 100% 被拒的成因。
        _tools_now = getattr(self, "_tools_now", None)
        _ptc = is_ptc(_tools_now)
        _direct_ok = True
        try:
            _direct_ok = has_tool(_tools_now, tool)
        except BaseException:            # noqa: BLE001
            _direct_ok = True
        if not _direct_ok and not _ptc:
            # 标准模式下 dsh 没给这个工具，包 run_code 也救不回来（表里没有它）。
            # 直接不发，免得制造一个必然被拒的红叉。
            try:
                self.bridge.note("  ⊘ 放弃造调用：标准模式下工具表里没有「%s」"
                                 % str(tool))
            except BaseException:        # noqa: BLE001
                pass
            return False
        try:
            if _direct_ok:
                # 标准模式：原样造这个工具的调用。
                calls = [_mk(0, str(tool), dict(args or {}))]
                for _c in calls:
                    _c["id"] = "wsq%s_%s" % (_c.get("index", 0), _short_id())
                self._dead = False
                self._opened = False
                self._proto = "chat"
                self._open_stream()
                self._write(chunk(model, {"role": "assistant",
                                          "content": text or ""}))
                self._write(chunk(model, {"tool_calls": calls}))
                self._write(chunk(model, {}, "tool_calls"))
                self._write(b"data: [DONE]\n\n")
                return True
            _j = json.dumps(args or {}, ensure_ascii=False)
            code = ("const r = await tools." + str(tool)
                    + "(" + _j + ");" + chr(10)
                    + "return r;")
            # 2026-10-01 第462步修（用户实测报错：missing required property
            #   "description"）：**run_code 的 description 是必填。**
            # 这正是 TOOL_PROTOCOL 里写过的教训 ——「必填字段一个都不能少，
            # 实测最常漏的是 description」—— 我自己犯了一遍。
            # 历史例证：{"name": "run_code", "arguments": {"description": ...,
            # "code": ...}}
            calls = [_mk(0, "run_code", {
                "description": "桥内部：调 " + str(tool),
                "code": code})]
            for _c in calls:
                _c["id"] = "wsq%s_%s" % (_c.get("index", 0), _short_id())
            self._dead = False
            self._opened = False
            self._proto = "chat"
            self._open_stream()
            self._write(chunk(model, {"role": "assistant", "content": text or ""}))
            self._write(chunk(model, {"tool_calls": calls}))
            self._write(chunk(model, {}, "tool_calls"))
            self._write(b"data: [DONE]\n\n")
            return True
        except BaseException:            # noqa: BLE001
            return False

    def _ws_emit(self, model, calls):
        """把「问工作区」的 tool_call 当一条正常助手回复发出去。

        2026-10-01 第462步。跟 _switch_emit 同一套写法，**唯一区别是 id 前缀**：
        _switch_emit 用 swq（切组专用，_switch_count 靠它数轮次），
        这里用 wsq —— 各走各的计数，不会互相把轮次带偏。
        """
        for _c in (calls or []):
            _c["id"] = "wsq%s_%s" % (_c.get("index", 0), _short_id())
        self._dead = False
        self._opened = False
        self._proto = "chat"
        self._open_stream()
        self._write(chunk(model, {"role": "assistant", "content": ""}))
        self._write(chunk(model, {"tool_calls": calls}))
        self._write(chunk(model, {}, "tool_calls"))
        self._write(b"data: [DONE]\n\n")

    def _ask_workspace(self, model, gname):
        """2026-10-01 第462步：**桥向 dsh 要工作区路径。**

        用户口径：「你不会让桥回一个 dsh 的命令来获取当前分组真实路径吗？」
        「桥回的命令触发dsh命令返回工作区位置 再把当前发消息的组拼上
        不就是真实目录了吗」

        机制跟 ##切组## 完全一样：桥自己造一条 tool_call 发出去（不走上游），
        dsh 执行后把结果放进**下一轮请求**的工具结果里，桥从那儿读回来。
        区别只是工具从 ask_user_question 换成 pwsh。

        为什么要走这一趟：桥手上的请求里**没有任何工作区声明**
        （实测 system 段只有 6 个字"你是执行器"，dsh_session_log 是 null），
        拿桥本机 workdir 顶包就是「落在桥身上」—— 而 dsh 跟桥是两套，
        它的工作区桥不该替它决定。**问它本人最准。**
        """
        try:
            # 2026-10-01 第462步修（用户实测：「在ptc模式下发切组报错提问」）：
            #   Error: unknown tool "ask_user_question":
            #   only `run_code` is callable directly
            # **PTC 模式下 dsh 只暴露 run_code 一个工具。**
            # 所以桥造的任何非 run_code 调用都会被直接拒掉。
            #
            # 2026-10-03 改（诊断报告 5.1 节）：**原来这里硬写 run_code，压根没判模式。**
            # 注释写着「判据：ntools == 1 就是 PTC」，但代码里从来没有这一句 ——
            # 于是标准模式下也发 run_code，而标准模式的工具表里没有 run_code，
            # 必然 `Error: unknown tool "run_code"`。
            # 现在统一读 detect_mode()：PTC 用 run_code，标准模式用 pwsh。
            _ws_ptc = is_ptc(getattr(self, "_tools_now", None))
            self.bridge.note("  问 dsh 要工作区（组「%s」路径未定，模式=%s）"
                             % (gname, "PTC" if _ws_ptc else "标准"))
            if _ws_ptc:
                # PTC：只有 run_code 能直接调。它跑 TypeScript，用 process.cwd()。
                _code2 = ("const cwd = process.cwd(); "
                          "const r = '" + _WS_MARK + " ' + cwd; "
                          "return r;")
                calls = [_mk(0, "run_code", {
                    "description": "取当前工作区（桥问，用于定分组目录）",
                    "code": _code2})]
            else:
                # 标准模式：直接调 pwsh（工具表里一定有它）。
                calls = [_mk(0, "pwsh", {
                    "command": "pwd",
                    "description": _WS_MARK + " 取当前工作区（桥问）"})]
            # 2026-10-01 第462步：**必须用自己的 emit，不能复用 _switch_emit。**
            # 实测发现的冲突：_switch_emit 硬把 id 写成 "swq" 前缀，
            # 而 _switch_count 靠 swq/_switch_answers 数切组轮次；
            # 那个函数**认所有 tool/function 角色的消息**当切组答复 ——
            # 我这个 pwsh 结果会被它当成一次切组回答，
            # 用户同时在用 ##切组## 的话，轮次直接串掉（答一问跳两问）。
            # 所以这里用平行的 emit，前缀 wsq（workSpace Query），互不干扰。
            self._ws_emit(model, calls)
            return True
        except Exception as exc:            # noqa: BLE001
            try:
                self.bridge.note("  问工作区失败：" + str(exc)[:120])
            except Exception:
                pass
            return False

    def _switch_emit(self, model, calls, text=""):
        """把桥自己造的 tool_call 当一条正常助手回复发出去。

        id 必须带 swq 前缀 —— _switch_count 靠它数已经问过几轮，
        不带前缀轮次永远停在 0，切组会卡死在第一问上。
        text 是弹框前先贴给用户看的一段话（第366步：查看分组状态时用）。
        """
        # 2026-10-01 第462步（用户实测 + 选择方案 A）：**PTC 模式下要包一层 run_code。**
        #
        # PTC 只允许 run_code 直接调用，直接发 ask_user_question 会被拒：
        #   Error: unknown tool "ask_user_question":
        #   only `run_code` is callable directly
        # 报错自己给的出路是「从 run_code 程序内部调它」。
        # 所以这里看 dsh 给没给 ask_user_question：
        #   给了（标准模式）-> 原样发，dsh 直接弹框
        #   没给（PTC）     -> 包成 run_code，里面 await tools.ask_user_question(...)
        # _TOOLS_NOW 由 do_POST 每轮设成本轮的工具表。
        # 2026-10-03 改（诊断报告 5.1 节）：判据统一走 detect_mode。
        # 原来这里问的是 has_tool(SWITCH_TOOL)，与 _rc_emit、_proto_for 口径不同。
        # 现在：模式由 detect_mode 一个地方说了算，「这个工具能不能直接调」
        # 仍问 has_tool —— 两者合起来决定直发还是转 _rc_emit 包壳。
        _tools_now = getattr(self, "_tools_now", None)
        _ptc = is_ptc(_tools_now)
        _tool_ok = True
        try:
            _tool_ok = has_tool(_tools_now, SWITCH_TOOL)
        except BaseException:            # noqa: BLE001
            _tool_ok = True
        if not _tool_ok and calls:
            # 工具不直接可用：交给 _rc_emit 统一处置（它按模式决定包 run_code 还是放弃）。
            try:
                self.bridge.note("  切组弹窗：%s 不可直调（模式=%s），转 _rc_emit"
                                 % (SWITCH_TOOL, "PTC" if _ptc else "标准"))
            except BaseException:        # noqa: BLE001
                pass
            _c0 = calls[0]
            return self._rc_emit(model, SWITCH_TOOL,
                                 json.loads(_c0["function"]["arguments"] or "{}"),
                                 text=text)
        for _c in (calls or []):
            _c["id"] = "swq%s_%s" % (_c.get("index", 0), _short_id())
        self._dead = False
        self._opened = False
        self._proto = "chat"
        self._open_stream()
        self._write(chunk(model, {"role": "assistant", "content": text or ""}))
        self._write(chunk(model, {"tool_calls": calls}))
        self._write(chunk(model, {}, "tool_calls"))
        self._write(b"data: [DONE]\n\n")

    def _switch_dialog(self, model, messages):
        """##切组## 的多轮对话。返回 True = 这一发桥自己回掉了，不走上游。

        轮次靠 messages 里 id 以 swq 开头的 tool_call 个数推出来，
        桥不存任何状态：多窗口并发、桥重启都不会串。
        """
        n = _switch_count(messages)
        answers = _switch_answers(messages)
        # 2026-09-27 第366步：点【查看分组状态】只看一眼，不算一次回答 ——
        # 从 answers 里滤掉之后再算轮次，看完照旧回到第 1 问。
        real = _switch_real(answers)
        # 2026-10-01 第463步（用户口径「我点跳过肯定是结束会话了 怎么还一直蹦」）：
        # **取消 = 结束这次对话，立刻收手，不再问下一问。**
        # 见 _switch_answers 里的哨兵注释：取消原来被算成一次回答，
        # 轮次一直涨 -> 一直弹 -> 用户关不掉。
        if any(str(x).startswith("\x00CANCEL") for x in real):
            try:
                self._plain(model, SWITCH_CANCEL)
            except Exception:            # noqa: BLE001
                pass
            return True
        n = len(real)
        groups = switch_groups()
        alls = [b.slug for b in self.pool.bridges.values() if b.slug]
        cur = str(getattr(self.pool, "grp_top", "") or "")
        # 2026-09-27 第399步（用户口径）：在别台电脑上发 ##切组## 新建分组时，
        # 「本组干活目录」必须按**那台**电脑的工作目录算，不能永远拿桥本机的
        # ini workdir 顶包 —— 否则 B 机新建的组会挂在 A 机的路径下。
        # 先从本轮请求里抽（跟 dir_block_now 同一条证据链），抽不到才退回 ini。
        _root, _src = _evid_root_from_messages(messages)
        if not _root:
            try:
                _root = _norm_root((_ini_read().get("codex") or {}).get("workdir") or "")
            except Exception:            # noqa: BLE001
                _root = ""
        report = switch_status(alls, cur, _root)
        # 2026-09-27 第363步：把轮次与已收到的回答数落进日志。用户实测
        # 「##切组## 认不出来」时，光看回复分不清是轮次数错了还是回答没传回来，
        # 这一行让下一次实测有据可查（note 前缀不匹配事件表，只写日志）。
        try:
            self.bridge.note("  切组：轮次=%d 回答=%d 组=%d 号=%d"
                             % (n, len(answers), len(groups), len(alls)))
        except Exception:            # noqa: BLE001
            pass

        # 第366步：点【查看分组状态】只是看一眼，不算回答 —— 把状态贴在
        # 问题前面弹回同一问，绝不让它把流程往前推。
        if _switch_wants_info(answers):
            # 看完状态要回到**下一该问的那一问**，跟正常路线一致：
            # n=0 回第1问；n=1（已答「新建」）回勾号问；n>=2 回起名问。
            taken = set()
            for g in groups:
                taken.update(g["slugs"])
            qs = switch_q1(groups)
            if n == 1 and (SWITCH_NEW in (real[0] if real else "")
                           or not groups):
                qs = switch_q2(alls, taken)
            elif n >= 2:
                _pk = _switch_pick(real[1] if len(real) > 1 else "", alls)
                qs = switch_q3(_pk) if _pk else switch_q2(alls, taken)
            self._switch_emit(model, [_mk(n, SWITCH_TOOL, {"questions": qs})],
                              text=report)
            return True

        # 第1问：切到哪一组（没有可选组时就只剩「新建」）
        if n == 0:
            self._switch_emit(model, [_mk(0, SWITCH_TOOL,
                                          {"questions": switch_q1(groups)})])
            return True

        # 第2问：新建时勾号；否则落进已有组
        if n == 1:
            # 2026-09-27 第367步：一律用过滤后的 real —— 用户可以先点几次
            # 【查看分组状态】再看回答，用 answers[0] 会把状态文本当成第一答。
            ans = real[0] if real else ""
            # 2026-09-27 第372步：选了【删除分组】就走删除线，不碰新建。
            if SWITCH_ROOT in ans:
                # 2026-10-01 第455步：更新组路径。跟删除同一位置、同一形态。
                self._switch_emit(model, [_mk(
                    1, SWITCH_TOOL, {"questions": switch_qroot(groups)})])
                return True
            if SWITCH_DEL in ans:
                self._switch_emit(model, [_mk(
                    1, SWITCH_TOOL, {"questions": switch_qdel(groups)})])
                return True
            if SWITCH_NEW in ans or not groups:
                taken = set()
                for g in groups:
                    taken.update(g["slugs"])
                self._switch_emit(model, [_mk(
                    1, SWITCH_TOOL, {"questions": switch_q2(alls, taken)})])
                return True
            if not ans.strip():
                # 2026-09-27 第365步：用户把第 1 问的框关了（或 dsh 回了个
                # 空 selected）。原来这里原样重问，连点两次取消就无限弹框。
                self._plain(model, SWITCH_CANCEL + chr(10) + chr(10) + report)
                return True
            hit = None
            for g in groups:
                if g["id"] in ans or g["name"] in ans:
                    hit = g
                    break
            if hit is None:
                # 2026-09-27 第364步：认不出（多半是对话框被回车关掉、
                # dsh 回了个 selected 空的答复）就原样重问第 1 问，
                # 不再丢一句「没认出来」把用户挡回去。
                self._switch_emit(model, [_mk(0, SWITCH_TOOL,
                                              {"questions": switch_q1(groups)})])
                return True
            try:
                self.pool._enter_group(hit["id"])
                # 2026-10-01 第460步：**桥不搬数据，只报告工作目录。**
                # 用户口径「搬也是手动切组以后把这台电脑的剪辑目录复制到
                # 另一个 dsh 工作目录下在更新路径」。
                # 复制文件夹是人的动作，桥插手只会出现「两边都有 / 搬一半」，
                # 比不搬更难查。这里只把路径报出来。
                _mv = ""
                try:
                    _gd = group_dir_of(hit["name"])
                    if _gd:
                        _mv = chr(10) + "本组工作目录：" + _gd
                except BaseException:            # noqa: BLE001
                    pass
                self._plain(model, "切到分组「%s」（%s）了。"
                            % (hit["name"], "、".join(hit["slugs"]) or "空号")
                            + _mv + chr(10) + chr(10) + report)
            except Exception as exc:             # noqa: BLE001
                self._plain(model, "切组失败：%s" % exc)
            return True

        # 第3问：新分组叫什么名字
        if n == 2:
            ans = real[1] if len(real) > 1 else ""
            # 2026-09-27 第372步：删除线走到这一步 —— 用户已经点了要删哪一组，
            # 再确认一次才真删（删了要重建，值得多点一下）。
            if SWITCH_ROOT in (real[0] if real else ""):
                # 2026-10-01 第455步：选了"更新组路径" -> 用户答的是"改哪一组"
                _rg = _switch_pick_group(ans, groups)
                if _rg is None:
                    self._plain(model, SWITCH_CANCEL + chr(10) + chr(10) + report)
                    return True
                # 把这一组和"这台电脑的工作目录"一起带去下一问 ——
                # _root 是本轮请求里抽出来的，换电脑时它就是正确答案。
                self._switch_emit(model, [_mk(
                    2, SWITCH_TOOL,
                    {"questions": switch_qroot_new(_rg, _root or "")})])
                return True
            if SWITCH_DEL in (real[0] if real else ""):
                _dg = _switch_pick_group(ans, groups)
                if _dg is None:
                    self._plain(model, SWITCH_CANCEL + chr(10) + chr(10) + report)
                    return True
                self._switch_emit(model, [_mk(
                    2, SWITCH_TOOL, {"questions": switch_qdel_ok(_dg)})])
                return True
            picked = _switch_pick(ans, alls)
            if not picked:
                # 2026-09-27 第365步：一个号都没勾到 = 用户收手了，回一句
                # 收场话；重问会变成无限弹框（用户连点两次取消就卡在这）。
                self._plain(model, SWITCH_CANCEL + chr(10) + chr(10) + report)
                return True
            self._switch_emit(model, [_mk(2, SWITCH_TOOL,
                                          {"questions": switch_q3(picked)})])
            return True

        # 2026-10-01 第463步（用户口径：**「跳过或者选择完成应该截断而不是在发
        #   上游窗口返回模型消息啊」**）：**轮次超出就问完了，必须截断。**
        #
        # 历史实证（.state/ds_bridge.log）——这是真发生过的：
        #   13:27:20 切组：轮次=1 回答=1
        #   13:27:20 切组：轮次=2 回答=2
        #   13:27:20 切组：轮次=3 回答=3      <- 只处理 n=0/1/2，3 没有分支
        #   13:27:20 续接会话 4aa41c35         <- **掉出去发给上游了**
        #   13:27:20 → deepseek-reasoner 续会话 67872 字
        # 用户把对话框跳过/答完，dsh 会把那一次答复再发一遍，
        # 轮次就成了 3。而这里只有 n=0/1/2 三条路，3 掉出 if 链条 ->
        # 一路走到打上游 —— 模型被迫对着「切组回答」回话，白烧一次。
        #
        # 所以：**轮次 > 3 一律截断**（桥自己回一句收场话，return True）。
        # 对话框已经走完了，这一发不该再有上游请求。
        #
        # 2026-10-02 修（用户实测「用切组怎么在桥中新建分组建不起来」）：
        # 原来写的是 n > 2，把第3问（起名）的**回答**（n=3）也一起截断了 ——
        # 三轮对话框走完、正该落盘建组时，直接收到「切组对话已经结束了」，
        # 于是 _pool_groups.json 永远不存在，一个组也建不起来。
        # n 与问的对应：n=0 发第1问 / n=1 发第2问 / n=2 发第3问 /
        #   n=3 收第3问的回答、建组 / n>3 才是真走完。
        if n > 3:
            try:
                self._plain(model, "切组对话已经结束了。" + chr(10) + chr(10) + report)
            except Exception:            # noqa: BLE001
                pass
            return True

        # 第3问（起名）的回答：落盘，然后切过去
        ans = real[2] if len(real) > 2 else ""
        # 2026-09-27 第372步：删除线的最后一步 —— 确认删就真删。
        if SWITCH_ROOT in (real[0] if real else ""):
            # 2026-10-01 第455步：更新组路径的最后一步。
            # real[1] 是"改哪一组"，real[2] 是"新路径"，ans 是确认。
            _rg = _switch_pick_group(real[1] if len(real) > 1 else "", groups)
            if _rg is None:
                self._plain(model, SWITCH_CANCEL + chr(10) + chr(10) + report)
                return True
            _newp = str(real[2] if len(real) > 2 else "").strip()
            if SWITCH_ROOT_OK not in (ans or ""):
                self._plain(model, "没改，分组路径保持不动。" + chr(10) + chr(10)
                            + report)
                return True
            _ok, _res = switch_set_root(_rg["id"], _newp)
            if not _ok:
                self._plain(model, "改路径失败：%s" % _res
                            + chr(10) + chr(10) + report)
                return True
            # 2026-10-01 第460步：**桥不搬数据。** 换电脑的完整流程是：
            #   1. 手动切组  2. 手动复制 <组名> 目录到新机器  3. 这里改路径
            # 桥只认路径 —— 复制文件夹是人的动作，看得见、可核对。
            _mv2 = ""
            try:
                _gd2 = group_dir_of(_rg["name"], root=_res)
                if _gd2:
                    _mv2 = chr(10) + "本组工作目录：" + _gd2
            except BaseException:                # noqa: BLE001
                pass
            # 改完重算状态 —— 报告里的目录列要显示新路径
            try:
                report = switch_status(alls,
                                       str(getattr(self.pool, "grp_top", "") or ""),
                                       _res)
            except Exception:                # noqa: BLE001
                pass
            self._plain(model, "分组「%s」的工作目录已改成 %s。" % (_rg["name"], _res)
                        + _mv2 + chr(10)
                        + "号、窗口、台账都没动；下次这个组干活就写在新目录下。"
                        + chr(10) + chr(10) + report)
            return True
        if SWITCH_DEL in (real[0] if real else ""):
            _dg = _switch_pick_group(real[1] if len(real) > 1 else "", groups)
            if _dg is None:
                self._plain(model, SWITCH_CANCEL + chr(10) + chr(10) + report)
                return True
            if "确认删除" not in (ans or ""):
                self._plain(model, "没删，分组都还在。" + chr(10) + chr(10) + report)
                return True
            _ok, _err = switch_del(_dg["id"])
            if not _ok:
                self._plain(model, "删分组失败：%s" % _err
                            + chr(10) + chr(10) + report)
                return True
            # 删掉的可能正是「当前组」。当前组没了就得重新落定一个，
            # 不然 grp_top 指着不存在的组，界面和派活会对不上。
            try:
                _ids = [g["id"] for g in switch_groups()]
            except Exception:            # noqa: BLE001
                _ids = []
            if self.pool.grp_top == _dg["id"] or self.pool.grp_top not in _ids:
                if _ids:
                    self.pool._enter_group(_ids[0])
                else:
                    self.pool.grp_top = ""
            try:
                report = switch_status(alls,
                                       str(getattr(self.pool, "grp_top", "") or ""),
                                       _root)
            except Exception:            # noqa: BLE001
                pass
            self._plain(model, "删掉分组「%s」了，号（%s）回到未分组。"
                        % (_dg["name"], "、".join(_dg["slugs"]) or "空")
                        + chr(10) + chr(10) + report)
            return True
        name = _switch_name(ans)
        picked = _switch_pick(real[1] if len(real) > 1 else "", alls)
        if not picked or not name:
            # 2026-09-27 第365步：名字或号没给全（多半是用户把最后一问关了），
            # 回一句收场话，别重问 —— 重问会一直弹框。
            self._plain(model, SWITCH_CANCEL + chr(10) + chr(10) + report)
            return True
        try:
            gid = switch_save(name, picked)
        except Exception as exc:                 # noqa: BLE001
            self._plain(model, "写 _pool_groups.json 失败：%s" % exc)
            return True
        # 2026-10-01 第461步（用户口径「切组 更新路径和新建的时候路径就切到桥
        #   那里去了根本不用这么麻烦」）：**新建时就把路径定下来，写进配置。**
        #
        # 这里原来写的是 group_root_learn(gid, _root) —— 存进"学习记录"。
        # 两处错：
        #   1. 学习那套已经砍了（见 group_root_of 注释：用统计猜语义值不该自动化）
        #   2. 更根本的：**记进哪一份是次要的，新建时本来就该定死。**
        #      路径在新建那一刻就确定了（= 这台 dsh 的工作目录），
        #      没有理由留到以后再猜。
        #
        # _root 是本轮请求里抽出来的"发起这台电脑的工作目录"——
        # 正是 dsh 那边的工作区，不是桥自己的。
        try:
            if _root:
                switch_set_root(gid, _root)
        except BaseException:                    # noqa: BLE001
            pass
        try:
            self.pool._enter_group(gid)
        except Exception:                        # noqa: BLE001
            pass
        # 2026-09-27 第371步（用户口径）：这份状态原来在开头就算好了，新建的
        # 那一组还没落盘，收场里当然看不到它。落盘、切组都做完之后重算一遍。
        # 收场话里也不再写内部编号（用户嫌 g1/g2 难看），只写组名和号。
        try:
            report = switch_status(alls, gid, _root)
        except Exception:                        # noqa: BLE001
            pass
        self._plain(model, "建好分组「%s」（号：%s），已经切过去了。"
                    % (name, "、".join(picked))
                    + chr(10) + chr(10) + report)
        return True

    def _plain(self, model, msg):
        """当成一条正常的助手回复发出去（200 + 完整流）。

        用在「不希望客户端重试」的场合：客户端看到 5xx 会自己重试好几次，
        限流时那就是雪上加霜。
        """
        self._open_stream()
        self._write(chunk(model, {"role": "assistant", "content": ""}))
        self._write(chunk(model, {"content": msg}))
        self._write(chunk(model, {}, "stop"))
        self._write(b"data: [DONE]\n\n")


    def _streamed(self, model, messages):
        self._dead = False
        self._open_stream()
        self._write(chunk(model, {"role": "assistant", "content": ""}))
        sent = []

        def on_delta(kind, text):
            # 分类跟 GUI 保持一致（ds_gui.on_delta）：TIP/SEARCH 是「正在浏览网页…」
            # 那类状态提示，归到思考栏；其余片段类型一律当正文。以前只认 RESPONSE，
            # 上游换个新 type 就整段静默丢掉，还查不出来。
            if kind in ("THINKING", "SEARCH", "TIP"):
                self._write(chunk(model, {"reasoning_content": text}))
            else:
                sent.append(text)
                self._write(chunk(model, {"content": text}))

        used = None
        try:
            text, _sid, _mid, used = self.bridge.run(
                model, messages, None, on_delta, lambda: False)
            # 2026-09-24：无工具这条路以前不判截断 —— 上游主动断流
            # （quasi_status != FINISHED）时，半截正文直接交回客户端，静默丢内容。
            # 注意 looks_truncated(text, tools) 内含 bool(tools)，这里 tools=None
            # 所以它恒为 False —— 只能靠上游自己报的准状态。
            _fin0 = getattr(self.bridge, "last_up_finish", None)
            if (_fin0 and _fin0 != "FINISHED" and (text or "").strip()):
                self.bridge.note("  ✂ 上游报 quasi_status=" + str(_fin0)
                                 + "，无工具路径接续补完")
                try:
                    _more, _sid, _mid = self.bridge.continue_until_parsable(
                        model, text, _sid, _mid, tries=1)
                except Exception as _cexc:            # noqa: BLE001
                    self.bridge.note("  无工具接续失败：" + str(_cexc)[:200])
                    _more = None
                if _more and _more != text:
                    _add = (_more[len(text):]
                            if _more.startswith(text) else _more)
                    if _add.strip():
                        self._write(chunk(model, {"content": _add}))
                        sent.append(_add)
            if not "".join(sent).strip():
                # 一个字都没流出去，补一句，别让上游判成空回复
                self._write(chunk(model, {"content": (text or "").strip() or
                                          "[上游返回了空回复，见 ds_bridge.log]"}))
            else:
                # 正文是边收边吐的，来源清单只能在末尾补上（行内的
                # [citation:N] 已经原样流出去了，没法回头改）
                blk = sources_block(used.get("refs"))
                if blk:
                    self._write(chunk(model, {"content": blk}))

        except Exception as exc:                     # noqa: BLE001
            self.bridge.note(traceback.format_exc().strip())

            self._write(chunk(model, {"content": f"\n\n[上游出错] "
                                                 f"{type(exc).__name__}: {exc}"}))
        self._write(chunk(model, {}, "stop"))
        if used:
            # token 数是估的（网页接口不给），单独一帧发，客户端才有东西显示
            self._write(chunk(model, usage=used))
        self._write(b"data: [DONE]\n\n")



    def _buffered(self, model, messages, tools):
        """先收完再判断：是工具调用就发 tool_calls 帧，否则一次性发正文。

        2026-09-22 改流式（用户要求）：**思考边到边发**。以前思考是攒到末尾一次性
        灌给 dsh，一轮十几二十秒里客户端什么都看不到、像卡死。
        正文与工具调用**仍然必须缓冲** —— 要等收完才能解析工具调用，也才谈得上
        截断判定与接续。无工具那条路（_streamed）本来就是边到边吐的。
        """
        # 2026-09-22 解耦（用户口径：两个方向都不许断流）：
        #   _dead = 「客户端走了」（往 dsh 写不出去），**只影响往下游写**；
        #   传给 run() 的 stop 一律恒 False —— 上游那条流**永远读完整**。
        # 以前 _dead 同时当 stop，于是「往 dsh 写失败」会连带掐断上游读取，
        # 表现为：上游还在流、桥这边先把消息截断了；而且 last_finish / token 这些
        # 仪表也拿不准（流没读完）。代价是客户端走了还继续读几秒、多花一点配额。
        self._dead = False
        reasoning = []

        stream_ok = [True]
        pend = []            # 还没发出去的思考碎片
        pend_last = [time.time()]

        def _flush_pend(force=False):
            """攒够了才发一帧。force=True 时不管多少都发（收尾用）。"""
            if not pend or not stream_ok[0] or self._dead:
                return
            body = "".join(pend)
            if not force and STREAM_CHUNK_SEC > 0 \
                    and (time.time() - pend_last[0]) < STREAM_CHUNK_SEC \
                    and (STREAM_CHUNK_CHARS <= 0 or len(body) < STREAM_CHUNK_CHARS):
                return
            # STREAM_CHUNK_SEC = 0 -> 条件恒假 -> 每个碎片立刻发出（逐片发送）
            # 2026-09-22 修（用户实测：上游还在流，桥这边先把消息截断了）：
            # 这里**绝对不能走 self._write()** —— 它会写失败就置 self._dead，
            # 而 self._dead 同时是 run() 的 stop 回调 -> _ask 立刻断开上游读取，
            # 回复被截成半截。所以直接写 socket，失败只关掉流式，绝不碰 _dead。
            try:
                if not self._opened:
                    self._open_stream()
                    self.wfile.write(chunk(model, {"role": "assistant",
                                                 "content": ""}))
                _fr = chunk(model, {"reasoning_content": body})
                self.wfile.write(_fr)
                self.wfile.flush()
                del pend[:]
                pend_last[0] = time.time()
            except Exception as exc:             # noqa: BLE001
                stream_ok[0] = False
                del pend[:]
                # 2026-10-03：**把异常类型和未送达字数记上。**
                # 原来只有一句「思考流写失败，停止边到边发」，查「窗口不显示
                # 他说话」时无从判断是客户端断开(BrokenPipe)还是写超时 ——
                # 前者要治发送时机，后者要调超时，修法完全不同。
                try:
                    self.bridge.note(
                        "  ⚠ 思考流写失败，停止边到边发（不影响上游读取）"
                        "｜%s: %s｜本段 %d 字未送达"
                        % (type(exc).__name__, str(exc)[:80], len(body)))
                except Exception:            # noqa: BLE001
                    pass

        # 正文外吐的缓冲与「已吐了多少」——用来判断安全边界
        live = []           # 已确定可以外吐的正文
        sent_text = []      # 真正吐出去的正文

        def _emit_text(s):
            """把一段安全的正文吐给客户端（按同一套节流）。"""
            if not s or not stream_ok[0] or self._dead:
                return
            # 2026-10-03（用户报「窗口不显示他说话」，查了很久才发现）：
            # **原来这里是先 append 再 write** —— 写失败时这段正文已经被记进
            # sent_text，收尾的 _unsent_tail 就认为「这段已经发过了」，不再补发。
            # 于是「写失败」和「送达」在账上无法区分，日志也看不出来。
            # 改成：**写成功了才记账**。失败就让它留在 unsent 里，收尾还有一次机会。
            try:
                if not self._opened:
                    self._open_stream()
                    self.wfile.write(chunk(model, {"role": "assistant",
                                                 "content": ""}))
                _fc = chunk(model, {"content": s})
                self.wfile.write(_fc)
                self.wfile.flush()
                sent_text.append(s)          # ← 只有真写出去才记
            except Exception as exc:         # noqa: BLE001
                stream_ok[0] = False
                # 记异常类型：原来只写「(不影响上游读取)」，查的时候分不清
                # 是客户端真断了（BrokenPipe）还是写超时 —— 这两种修法完全不同。
                try:
                    self.bridge.note(
                        "  ⚠ 正文流写失败，停止边到边发（不影响上游读取）"
                        "｜%s: %s｜本段 %d 字未送达"
                        % (type(exc).__name__, str(exc)[:80], len(s)))
                except Exception:            # noqa: BLE001
                    pass

        def _flush_live(force=False):
            """把 live 里「确定安全」的前缀吐出去。

            安全 = 不含任何标记开头。一旦发现标记开头，从那里截断，
            后面的留到收完由解析器判定。末尾还要留 STREAM_KEEP 个字，
            免得半个标记刚好卡在边界上。
            """
            buf = "".join(live)
            if not buf:
                return
            cut = len(buf)
            for m in STREAM_HOLD:
                i = buf.find(m)
                if 0 <= i < cut:
                    cut = i
            if not force:
                keep = max(0, cut - STREAM_KEEP)
            else:
                keep = cut
            if keep <= 0:
                return
            _emit_text(buf[:keep])
            del live[:]
            if buf[keep:]:
                live.append(buf[keep:])

        def on_delta(kind, t):
            if not t or not stream_ok[0] or self._dead:
                return
            if kind in ("THINKING", "SEARCH", "TIP"):
                # 思考 / 联网提示：老代码（_streamed）就是这么分流的，保持一致
                reasoning.append(t)
                pend.append(t)
                _flush_pend()
                return
            # 其余一律当正文，边收边吐（带安全边界）
            live.append(t)
            _flush_live()

        def _unsent_tail(body, sent):
            """正文里还没吐出去的那一段。

            2026-09-27 改（用户实测：正文只看到开头 / 同一段重复两遍）：原来
            有工具那条路用「prose 在不在已吐串里」判断 —— 已吐的只要是 prose
            的前缀，就判成不在，于是整段重发一遍；没有工具那条路只在一个字都
            没吐时才补发，凡吐了一半（正文里出现代码围栏，_flush_live 从围栏
            处截断）后面的正文就永远丢了。这里改成按最长公共前缀取后缀。
            """
            if not body:
                return ""
            if body.startswith(sent):
                return body[len(sent):]
            if sent.startswith(body):
                return ""
            k = 0
            while k < len(body) and k < len(sent) and body[k] == sent[k]:
                k += 1
            return body[k:]
        def _peek_safe():
            """收完了：把还没吐的正文里安全的部分冲出去。"""
            _flush_pend(force=True)
            # 收尾时若整段正文里没有任何标记，就全吐；有标记则只吐标记之前那段。
            _flush_live(force=True)

        try:
            text, sid, mid, used = self.bridge.run(
                model, messages, tools, on_delta, lambda: False)
        except Exception as exc:                     # noqa: BLE001
            self.bridge.note(traceback.format_exc().strip())
            _flush_pend(force=True)

            # 返 5xx 的话 dsh 会闷头重试最多 5 次，而每次重试在桥这边都是
            # 「新会话 + 整段 transcript 重发」—— 日志里就出现过连着 4 次
            # 38 万字的新会话。重试在这里从来没救回过什么，只会把限流坐实。
            # 所以一律当成一条正常回复发出去，让客户端停下来。
            if is_rate_limited(exc):
                # 限流必须报成 429。报成正文的话 dsh 以为这轮正常结束，
                # 既不退避也不冷却，用户连点「继续」就是连着撞上游。
                self.bridge.note("  ⚠ 上游限流，按 429 报给 dsh")
                self._err(429, self.bridge.rate_limited_msg(), "rate_limit")
                self.bridge.cool_until = time.time() + self.bridge.COOLDOWN
                self._rotate_away()
                return

            self._plain(model, f"[桥接] 上游失败：{type(exc).__name__}: {exc}\n\n"
                               f"这一轮没有任何工具被执行。要继续就重发一次。")
            return


        calls, prose = split_calls(text, tools) if tools else (None, text)
        # 2026-09-22 加（用户实测）：原来只看文本形状（looks_truncated）猜是不是被截断，
        # 但桥手里有上游自己的实证 —— quasi_status（ds_api 的 last_finish），正常永远
        # FINISHED，一旦是别的值就是上游主动断了这一轮（:2442 的注释：「这是上游主动断的
        # 实证，不是猜的」）。只看形状会漏掉**在正文里被截断**的那种（没有工具调用片段，
        # TRUNC_HINT 匹配不上）—— 那种回复原样交给 dsh，dsh 见没有 tool-call 块就判
        # completed（dsh-agent-loop:1117），任务停在半句话上。
        _fin = getattr(self.bridge, "last_up_finish", None)
        _cut = looks_truncated(text, tools) or bool(_fin and _fin != "FINISHED")
        if _cut:
            # 被输出长度上限砍断了，自动接着往下要，拼完再解析
            if _fin and _fin != "FINISHED":
                self.bridge.note("  ✂ 上游报 quasi_status=" + str(_fin)
                                 + "，按截断处理并接续")
            text, sid, mid = self.bridge.continue_until_parsable(
                model, text, sid, mid)
            calls, prose = split_calls(text, tools)
            used["completion"] = est_tokens(text)
        # 2026-10-01 第462步（用户实测报错 missing required property
        #   "description"）：**交回客户端之前，把必填的说明性字段补齐。**
        # 那一条是模型自己漏的（日志：{"name":"run_code","arguments":
        #   {"code":...}}，没带 description），dsh 直接拒。
        # TOOL_PROTOCOL 里早写着这个教训，提醒了模型照漏 —— 所以在出口纠一次。
        # 只补 FILLABLE 里那些（description 之类）；code/command 缺了不补。
        if calls:
            try:
                _nf = fill_required(calls, tools)
                if _nf:
                    self.bridge.note("  ✚ 补齐必填的说明性字段 %d 处（模型漏了）" % _nf)
            except BaseException:        # noqa: BLE001
                pass
        if tools and not calls:
            # 留个证据：模型可能又换了一种工具调用写法，不记下来没法补解析
            self.bridge.note("  ⚠ 给了工具但没解析出调用，原文："
                             + repr((text or "")[:600]))
            text = salvage(text, tools)


        else:
            # 2026-10-01 第475步（用户口径「看看test组正常不」，从日志抓出来的）：
            # **「参数是空」要按该工具自己的参数表判，不能一律报。**
            #
            # 实测误报（test 组 020，23:11:10）：
            #   <- 428 字 | '{"tool_calls":[{"name":"job_list","arguments":{}},
            #                        {"name":"pwsh","arguments":{"command":...}}]}'
            #   [警告] 工具调用参数解析成空
            # 但 job_list 本来就是无参工具 —— 证据：dsh 侧定义
            #   node_modules/@deepseek-ai/dsh-tool-jobs/lib/index.js
            #     name: "job_list", description: "List your background jobs...",
            #     parameters: {}          <-- 空的
            # 所以 arguments:{} 是**完全正确的调用**，而旧写法只要**任一**调用空就报，
            # 于是这一发里两个调用（一个无参、一个有参）被判成「解析成空」。
            #
            # 代价不只是日志噪声：它会**掩盖真正该报的那一种**
            # （工具明明有必填项却给了空参数）。改成逐条按参数表判。
            _bad = []
            try:
                _tmap = {}
                for _t in (tools or []):
                    _fn2 = _t.get("function") if isinstance(_t, dict) else _t
                    if not isinstance(_fn2, dict):
                        continue
                    _sc = _fn2.get("parameters")
                    _req = _sc.get("required") if isinstance(_sc, dict) else None
                    _tmap[str(_fn2.get("name") or "")] = set(
                        _req) if isinstance(_req, (list, tuple)) else set()
                for _c in calls:
                    _nm = str((_c.get("function") or {}).get("name") or "")
                    _ar = (_c.get("function") or {}).get("arguments")
                    _need = _tmap.get(_nm)
                    if not _need:
                        continue          # 无参工具（或不在表里）：空是对的
                    if _ar in ("{}", "", None):
                        _bad.append(_nm + " 需要 " + "/".join(sorted(_need)))
            except BaseException:         # noqa: BLE001
                _bad = []
            if _bad:
                self.bridge.note("  [警告] 工具调用参数是空但工具要必填项："
                                 + "；".join(_bad)
                                 + " ｜原文：" + repr((text or "")[:400]))


        if not calls and not (text or "").strip():
            # 2026-09-22 改（用户要求）：思考**不再**当正文发。
            # 上游池看到的是「思考模型」，思考只走 reasoning_content 通道；
            # 正文空就是真的空 —— run() 里的关思考重发先兜一次，兜不住
            # 就按空回复交给 dsh 自己退避重试。
            # 绝不把草稿纸冒充成要执行的动作。实证：113 窗口里 reasoning
            # 与 text 长度完全一致（8728/8728），用户读到的全是内心独白。
            if reasoning:
                self.bridge.note("  ⊘ 只有思考（"
                                 + str(len("".join(reasoning)))
                                 + " 字），按新规矩不塞进正文")
            # 2026-09-22 简化（用户口径）：**上游返回空，一律当频繁限流**。
            # 以前按「这一发大不大 / 会话是不是失效」分三种处理，现在不分：
            # 大发送回空同样是限流的表现，退避 + 换号才对，压上下文治不了它。
            # session_cache 里的原分类只留作日志线索。
            self._err(429, self.bridge.rate_limited_msg(), "rate_limit")
            if reasoning:
                self.bridge.note("  ⊘ 只思考无正文：不冷却、不换号，让 dsh 自己退避")
            elif _fin and _fin != "FINISHED":
                # 2026-09-23 加：上游自己报了不是正常结束 —— 这是断流，不是限流。
                # 正文空是上游把流断了（SSE 一发一条流），罚账号没用：冷却/换号
                # 只会把一个健康的号踢走。只回 429 让 dsh 退避重试。
                self.bridge.note("  ⊘ 空回复但 quasi_status=" + str(_fin)
                                 + " -> 上游断流，不冷却、不换号")
            else:
                _yld = bool(empty_policy(getattr(self.bridge, "slug", "")).get(
                    "empty_yield", True))
                self.bridge.note("  ⚠ 空回复 -> 按频繁限流处理（429"
                                 + (" + 冷却 + 换号" if _yld else "；让位已关")
                                 + "）"
                                 + ("｜原分类=" + str(self.bridge.last_empty)
                                    if self.bridge.last_empty else ""))
                if _yld:
                    self.bridge.cool_until = time.time() + self.bridge.COOLDOWN
                    self._rotate_away()
            return




        # 思考内容有没有拿到，日志里要能一眼看出来 —— 不然「dsh 不显示思考」这件事
        # 分不清是上游没给、桥没发，还是客户端没渲染
        cot = "".join(reasoning)
        if cot:
            self.bridge.note(f"  思考 {len(cot)} 字，已发 reasoning_content"
                             f" | {cot.strip()[:60]!r}")
        elif MODELS.get(model, (False, False))[0]:
            ds = self.bridge.ds
            self.bridge.note(
                "  ⚠ 思考档模型，但上游一个 THINKING 片段都没给"
                f" | 帧类型={getattr(ds, 'last_frags', None)}"
                f" | 未认领 path={getattr(ds, 'last_unhandled', None)}",
                model=model)


        if not self._opened:
            # 思考一段都没流出去（上游没给 THINKING，或根本开不了流）——
            # 按老规矩补一次：开流 + role 帧 + 整段思考。
            self._open_stream()
            self._write(chunk(model, {"role": "assistant", "content": ""}))
            if cot:
                self._write(chunk(model, {"reasoning_content": cot}))
        # 已经边到边发过就不重发 —— 否则客户端会看到两份思考。
        # 2026-09-22：正文已经边收边吐过了，这里只补「还没吐出去的那部分」。
        # 已经吐出去的不能再发一遍 —— 否则客户端看到两份正文。
        sent_so_far = "".join(sent_text)
        _flush_live(force=True)          # 把边界之外的残留冲出去
        sent_so_far = "".join(sent_text)
        # 2026-10-03（用户报「窗口不显示他说话」）：
        # **收尾这一跳到底成没成，日志里必须有一句话。**
        # 原来「上游回了正文(← N 字)」和「正文真到了窗口」在日志上长得一模一样，
        # 所以查「不显示」时无从下手 —— 桥全程以为自己正常。
        # 这里把「本段有没有送达 + 谁挡的」合成一条，判据是写 socket 的返回值。
        _ok_w = True
        if calls:
            # 调用之外的那段说明文字也要发出去，否则客户端只看得到思考和命令。
            # 已经吐过的部分不重发。
            _rest = _unsent_tail(prose, sent_so_far)
            if _rest:
                _ok_w = self._write(chunk(model, {"content": _rest})) and _ok_w
            _ok_w = self._write(chunk(model, {"tool_calls": calls})) and _ok_w
            _ok_w = self._write(chunk(model, {}, "tool_calls")) and _ok_w
        else:
            _rest = _unsent_tail(text or "", sent_so_far)
            if _rest:
                _ok_w = self._write(chunk(model, {"content": _rest})) and _ok_w
            _ok_w = self._write(chunk(model, {}, "stop")) and _ok_w
        self._write(chunk(model, usage=used))
        self._write(b"data: [DONE]\n\n")
        _body_n = len((prose if calls else (text or "")) or "")
        _snt = len(sent_so_far)
        if not _ok_w or self._dead or not stream_ok[0]:
            try:
                self.bridge.note(
                    "  ⚠⚠ 正文可能未送达窗口：写回=%s dead=%s stream_ok=%s"
                    "｜正文 %d 字 / 边发已送出 %d 字"
                    % (_ok_w, self._dead, stream_ok[0], _body_n, _snt))
            except Exception:                # noqa: BLE001
                pass
        elif _body_n and _snt < _body_n:
            # 送达了，但有缺 —— 也记上，省得下次又猜
            try:
                self.bridge.note("  ▤ 正文送达客户端：%d/%d 字（边发 + 收尾补发）"
                                 % (_snt, _body_n))
            except Exception:                # noqa: BLE001
                pass


    # ---------- Responses 协议（Codex 走这条）----------

    def _responses(self, model, messages, tools):
        """Codex 要的那套事件流。和 _buffered 一样先收完再决定形状。"""
        self._dead = False
        reasoning = []
        rid = "resp_" + _short_id()

        def envelope(status, output, used=None):
            return {"id": rid, "object": "response",
                    "created_at": int(time.time()), "status": status,
                    "model": model, "output": output,
                    "usage": used and {
                        "input_tokens": used["prompt"],
                        "output_tokens": used["completion"],
                        "total_tokens": used["prompt"] + used["completion"],
                        "input_tokens_details": {
                            "cached_tokens": used.get("cached", 0)}},

                    "error": None}

        try:
            text, sid, mid, used = self.bridge.run(
                model, messages, tools,
                lambda kind, t: reasoning.append(t) if kind == "THINKING" else None,
                lambda: False)      # 永不主动停止读上游（见 _dead 的说明）

        except Exception as exc:                     # noqa: BLE001
            self.bridge.note(traceback.format_exc().strip())

            if is_rate_limited(exc):
                self.bridge.note("  ⚠ 上游限流，按 429 报给 dsh")
                self._err(429, self.bridge.rate_limited_msg(), "rate_limit")
                self.bridge.cool_until = time.time() + self.bridge.COOLDOWN
                self._rotate_away()
                return

            self._err(502, f"上游失败：{type(exc).__name__}: {exc}",
                      "upstream_error")
            return

        calls, prose = split_calls(text, tools) if tools else (None, text)
        _fin = getattr(self.bridge, "last_up_finish", None)
        if looks_truncated(text, tools) or (_fin and _fin != "FINISHED"):
            text, sid, mid = self.bridge.continue_until_parsable(
                model, text, sid, mid)
            calls, prose = split_calls(text, tools)
            used["completion"] = est_tokens(text)
        if calls:
            try:
                _nf2 = fill_required(calls, tools)
                if _nf2:
                    self.bridge.note("  ✚ 补齐必填的说明性字段 %d 处（模型漏了）" % _nf2)
            except BaseException:        # noqa: BLE001
                pass
        if tools and not calls:
            self.bridge.note("  ⚠ 给了工具但没解析出调用，原文："
                             + repr((text or "")[:600]))
            text = salvage(text, tools)

        elif calls and any(c["function"]["arguments"] in ("{}", "")
                           for c in calls):
            self.bridge.note("  ⚠ 工具调用参数解析成空，原文："
                             + repr((text or "")[:600]))

        cot = "".join(reasoning).strip()
        if not calls and not (text or "").strip():
            # 2026-09-22 改：思考不再当正文（同 _buffered）。
            # 原来这里把思考塞进 text 发给客户端，用户读到的全是内心独白。
            # 现在正文空就走下面的空回复分支，让 dsh 自己退避重试。
            # 2026-09-22 简化（同 _buffered）：空回复一律当频繁限流。
            self._err(429, self.bridge.rate_limited_msg(), "rate_limit")
            if reasoning:
                self.bridge.note("  ⊘ 只思考无正文：不冷却、不换号，让 dsh 自己退避")
            elif _fin and _fin != "FINISHED":
                # 2026-09-23 加：上游自己报了不是正常结束 —— 这是断流，不是限流。
                # 正文空是上游把流断了（SSE 一发一条流），罚账号没用：冷却/换号
                # 只会把一个健康的号踢走。只回 429 让 dsh 退避重试。
                self.bridge.note("  ⊘ 空回复但 quasi_status=" + str(_fin)
                                 + " -> 上游断流，不冷却、不换号")
            else:
                _yld = bool(empty_policy(getattr(self.bridge, "slug", "")).get(
                    "empty_yield", True))
                self.bridge.note("  ⚠ 空回复 -> 按频繁限流处理（429"
                                 + (" + 冷却 + 换号" if _yld else "；让位已关")
                                 + "）"
                                 + ("｜原分类=" + str(self.bridge.last_empty)
                                    if self.bridge.last_empty else ""))
                if _yld:
                    self.bridge.cool_until = time.time() + self.bridge.COOLDOWN
                    self._rotate_away()
            return
        if cot:
            self.bridge.note(f"  思考 {len(cot)} 字，已发 reasoning 项"
                             f" | {cot[:60]!r}")

        self._open_stream()
        self._write(revent("response.created",

                           response=envelope("in_progress", [])))

        output = []

        def emit_reasoning(idx, body):
            """思考走 reasoning 输出项那套事件（Codex 只认这个，不认
            chat 协议的 reasoning_content —— 以前这条路径压根没发过思考）。"""
            iid = "rs_" + _short_id()
            self._write(revent("response.output_item.added", output_index=idx,
                               item={"id": iid, "type": "reasoning",
                                     "status": "in_progress", "summary": []}))
            self._write(revent("response.reasoning_summary_part.added",
                               item_id=iid, output_index=idx, summary_index=0,
                               part={"type": "summary_text", "text": ""}))
            self._write(revent("response.reasoning_summary_text.delta",
                               item_id=iid, output_index=idx, summary_index=0,
                               delta=body))
            self._write(revent("response.reasoning_summary_text.done",
                               item_id=iid, output_index=idx, summary_index=0,
                               text=body))
            part = {"type": "summary_text", "text": body}
            self._write(revent("response.reasoning_summary_part.done",
                               item_id=iid, output_index=idx, summary_index=0,
                               part=part))
            item = {"id": iid, "type": "reasoning", "status": "completed",
                    "summary": [part]}
            output.append(item)
            self._write(revent("response.output_item.done",
                               output_index=idx, item=item))

        def emit_text(idx, body):
            iid = "msg_" + _short_id()
            self._write(revent("response.output_item.added", output_index=idx, item={
                "id": iid, "type": "message", "status": "in_progress",
                "role": "assistant", "content": []}))
            self._write(revent("response.output_text.delta", item_id=iid,
                               output_index=idx, content_index=0, delta=body))
            self._write(revent("response.output_text.done", item_id=iid,
                               output_index=idx, content_index=0, text=body))
            item = {"id": iid, "type": "message", "status": "completed",
                    "role": "assistant",
                    "content": [{"type": "output_text", "text": body,
                                 "annotations": []}]}
            output.append(item)
            self._write(revent("response.output_item.done",
                               output_index=idx, item=item))

        if cot:
            emit_reasoning(len(output), cot)
        if calls:
            # 调用之外的那段说明文字也要发出去，排在 function_call 前面
            if prose:
                emit_text(len(output), prose)
            for i, c in enumerate(calls, len(output)):
                item = {"id": "fc_" + _short_id(), "type": "function_call",
                        "status": "completed",
                        "name": c["function"]["name"],
                        "arguments": c["function"]["arguments"],
                        "call_id": c["id"]}
                output.append(item)
                self._write(revent("response.output_item.added",
                                   output_index=i, item=item))
                self._write(revent("response.output_item.done",
                                   output_index=i, item=item))
        else:
            emit_text(len(output), text)

        self._write(revent("response.completed",
                           response=envelope("completed", output, used)))



def port_busy(host, port, timeout=0.4):
    """端口上有没有人在听。

    Windows 上 socket 允许 SO_REUSEADDR 绑到已经 LISTEN 的端口而不报错，
    结果是新服务默默收不到请求、连接全被旧进程接走。所以先主动连一下探。
    """
    with socket.socket() as s:
        s.settimeout(timeout)
        return s.connect_ex((host, port)) == 0


def _reap_half_dead(port):
    """把占着 port 的**桥进程**杀掉。成功返回 True。

    2026-10-02 第483步。只杀**命令行里带 ds_bridge.py 的 python 进程** ——
    绝不误杀别的程序（端口撞车时宁可报错也不乱杀）。
    """
    try:
        import subprocess as _sp
        out = _sp.run(["netstat", "-ano", "-p", "TCP"],
                      capture_output=True, text=True, timeout=4).stdout
        pids = set()
        want = ":%d " % port
        for ln in out.splitlines():
            if "LISTENING" in ln and want in ln:
                parts = ln.split()
                if parts and parts[-1].isdigit():
                    pids.add(parts[-1])
        for pid in pids:
            q = _sp.run(
                ["powershell", "-NoProfile", "-Command",
                 "(Get-CimInstance Win32_Process -Filter \"ProcessId=%s\")"
                 ".CommandLine" % pid],
                capture_output=True, text=True, timeout=8).stdout
            if "ds_bridge.py" not in q:
                continue                 # 不是桥，不动它
            _sp.run(["taskkill", "/F", "/PID", pid],
                    capture_output=True, timeout=8)
            print("  已清掉半死实例 PID " + pid)
        import time as _t
        _t.sleep(2)
        return True
    except BaseException:        # noqa: BLE001
        return False


def _port_listening_but_dead(host, port):
    """端口在 LISTEN、但连不上 —— 半死实例。

    2026-10-02 第483步。判据来自实测：
      · 正常桥：  connect_ex == 0（连得上，port_busy 会拦）
      · 空闲端口：connect_ex != 0，且**没有 LISTEN**
      · 半死实例：connect_ex != 0，但**有 LISTEN**   <- 只这一种要报
    用 Get-NetTCPConnection / netstat 判断 LISTEN，比 socket 层更准。
    """
    # 顺序很重要：**先看能不能连**。连得上就是活的服务，不是半死。
    # （我第一版只查了 LISTEN，结果把**正在正常服务的桥**也判成半死 ——
    #   实测 port_busy=True 和它同时为 True，判据整个反了。）
    try:
        with socket.socket() as _s:
            _s.settimeout(1.5)
            if _s.connect_ex((host, port)) == 0:
                return False          # 连得上 = 活的，不是半死
    except BaseException:        # noqa: BLE001
        return False
    # 连不上，那才看是不是还有 LISTEN 挂着
    try:
        import subprocess as _sp
        out = _sp.run(["netstat", "-ano", "-p", "TCP"],
                      capture_output=True, text=True, timeout=4).stdout
        want = ":%d " % port
        for ln in out.splitlines():
            if "LISTENING" in ln and want in ln:
                return True           # 连不上 + 有 LISTEN = 半死
    except BaseException:        # noqa: BLE001
        pass
    return False


class _Server(ThreadingHTTPServer):
    # 端口是否真被别人占着，靠上面 port_busy() 主动连一下来判断，所以这里保留
    # 地址复用：关掉重开时，上一批连接还在 TIME_WAIT，不复用就会绑不上端口，
    # 表现就是「重启桥接后客户端连不上」。
    allow_reuse_address = True
    daemon_threads = True



def make_server(target=None, host="0.0.0.0", port=11999, min_interval=3.0,
                log=True, disabled=()):
    """建一个服务但不启动，返回 (server, pool)。GUI 里也用这个。

    target 可以是：
      * None      —— 按 ds_auth.json 里的全部账号建池（GUI 和默认命令行都走这条）
      * Pool      —— 现成的池子
      * DeepSeek  —— 单个账号，包成一个只有它的池子（命令行 --account）

    Handler 上的 pool 是类属性，所以每次都现造一个子类绑上去，
    免得多个 server 互相踩。
    """
    # 0.0.0.0 是绑定用的通配地址，connect 不到它，探端口得换成回环地址
    probe = "127.0.0.1" if host in ("0.0.0.0", "::", "") else host
    if port_busy(probe, port):
        raise OSError(f"{host}:{port} 已经有服务在听了 —— "
                      f"大概是另一个 ds_bridge（命令行跑的那个也算）")
    # 2026-10-02 第483步（用户口径「为什么会死 要彻底根治」）：
    # **启动前要排掉「半死实例」—— 端口被一个不响应的旧进程占着。**
    #
    # 实测病因（今晚复现多次）：
    #   1) Stop-Process -Force 杀掉桥 -> Windows 强杀，socket 进半关闭，
    #      但**端口没立刻释放**；
    #   2) 新桥启动，port_busy() 去 connect_ex -> 那个死 socket 回 10061
    #      -> 判成「端口不忙」-> 继续启；
    #   3) _Server.allow_reuse_address = True -> bind **成功**；
    #   4) 但内核里那批旧连接仍指向死 socket -> 新来的请求 10061。
    #   表现就是「端口在听、进程活着、就是不响应」。
    #
    # 判据：connect_ex 失败（不是忙）**但端口在 LISTEN**，就是半死。
    # 这时不能硬起 —— 报清楚，让调用方去清干净。
    # 2026-10-02 第483步 **回滚**：这里原本想自动清「半死实例」，
    # 但实现里调了 netstat / powershell —— **实测把桥的启动卡死了**
    # （30 秒都打不出「桥接已启动」，而正常 3 秒内就出）。
    # 病根：在 make_server() 这个**启动必经路径**上做外部进程调用，
    # 任何一个慢/挂都会让桥起不来 —— 修 bug 反而造了更大的 bug。
    #
    # 教训：启动路径只做**纯内存 / 单次 socket** 的检查，绝不 fork 外部进程。
    # 半死实例的处置放到**外部**（谁启动谁负责），不塞进桥自己。
    if isinstance(target, Pool):
        pool = target
    elif target is None:
        pool = Pool(min_interval=min_interval, log=log, disabled=disabled)
    else:
        pool = Pool.of(target, min_interval, log)
    bound = type("BoundHandler", (Handler,), {"pool": pool})
    return _Server((host, port), bound), pool


# ===== 监控台（dsweb）随桥一起起 =====
# 2026-09-26 第105步（用户：给我的桥把监控台一起启动）：
# 监控台就是 dsweb/api.py —— 一个进程同时提供设置台和监控台两个页面，听 8791。
# 以前它要单独双击 启动.vbs，桥起来了它多半没起，打开就是空的。
DASHBOARD_DIR = PREV_ROOT / "dsweb"
DASHBOARD_PY = DASHBOARD_DIR / "api.py"
DASHBOARD_PORT = 8791


def start_dashboard():
    """确保监控台在跑，已经在跑就什么都不做。返回 (ok, 说明)。

    桥和监控台互不依赖：这里失败只打印一行，绝不影响桥转发。
    """
    if not DASHBOARD_PY.is_file():
        return False, "监控台脚本不在：%s" % DASHBOARD_PY
    if port_busy("127.0.0.1", DASHBOARD_PORT):
        return True, "监控台已在运行：http://127.0.0.1:%d/" % DASHBOARD_PORT
    exe = sys.executable or ""
    _pw = pathlib.Path(exe).with_name("pythonw.exe") if exe else None
    if _pw is not None and _pw.is_file():
        exe = str(_pw)                      # 不带控制台窗口
    try:
        subprocess.Popen(
            [exe, str(DASHBOARD_PY)],
            cwd=str(DASHBOARD_DIR),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=0x00000008 | 0x08000000,   # 脱钩 + 不弹窗
            close_fds=True)
    except Exception as exc:                      # noqa: BLE001
        return False, "监控台起不来：%s" % exc
    for _ in range(30):                           # 最多等 6 秒
        time.sleep(0.2)
        if port_busy("127.0.0.1", DASHBOARD_PORT):
            return True, "监控台已启动：http://127.0.0.1:%d/" % DASHBOARD_PORT
    return False, "监控台进程起了但 %d 还没听起来" % DASHBOARD_PORT


# ===== 关掉 dsh 的自动压缩（2026-10-02）=====
#
# 用户口径（原话，逐字）：
#   「以后桥开启先检查所有窗口的点有没有自动关掉不就得了 还有新建分组
#     切换分组的时候 别的就不用管了」
#   「不压是不可能」（上下文一定会涨到上限，压是必须的，能调的只是压多狠）
#
# ## 为什么是 auto: false，不是 disabled: true
#
# 这两个看着像「关得更彻底 / 关得轻一点」，其实**关的不是一回事**：
#     auto: false    -> 插件照常加载，只是不注册自动压缩监听器
#                       （dsh-compaction-basic/lib/index.js:786
#                        if (this.config.auto) this._registerAutomaticCompaction()）
#     disabled: true -> 插件根本不加载，ctx.compaction 这个服务就没了
#
# 我中途改成过 disabled: true（以为更简单更彻底），**那是错的**：
# 压缩引擎还得用 —— 桥要「按时间区间取那一段的摘要」补给下一个号，
# 那一步靠的就是这个引擎（engine.summarize()）。关成 disabled 之后，
# 那个只读工具 get_range_context_compact 会直接报
# 「found no compaction engine for this agent」（README 出错信息对照表）。
#
# 所以：**只关自动触发，留住手动/按需的能力。**
# `/compact` 手动压缩是另一个插件（command-compact），不受影响。
#
# ## 为什么必须关自动
#
# 自动压缩到阈值会把最旧那段**替换成一条摘要消息**，原文当场没了
# （dsh-compaction-basic README「压缩运行时会发生什么」）。
# 而桥的交接要「按时间点切」出全量，切的是原文 —— 被压过的那段只剩摘要，
# 切出来就不是全量（用户口径「备份的那些根本用不了」）。
#
# ## 先查过 API：没有运行时开关
#
# compaction-basic 只暴露 compactIfNeeded / compactRegion / compactNow
# —— 全是「压」的动作，没有「关」的。而 auto 是构造时读一次的
# （this.config 是 readonly）-> 运行中改不了。所以只能改文件。
#
# ## 为什么每次启动都重贴
#
# 这些 preset 在 node_modules 里，dsh 升级/重装会覆盖回出厂值。
# 所以桥每次启动、每次建组/切组都检查一遍 —— 这正是「自动关」的落点，
# 也是「别的就不用管了」的意思：不轮询、不定时，只在关键时刻看一眼。
#
# 失败一律不抛（改坏了 dsh 起不来），只记一行。返回 (改了几个, 说明)。
def ensure_auto_compact_off():
    try:
        changed, checked, notes = 0, 0, []
        for name, path in preset_files_to_check():
            try:
                if not path.is_file():
                    continue
                checked += 1
                s = path.read_text(encoding="utf-8")
            except OSError as exc:
                notes.append("%s 读不到: %s" % (name, str(exc)[:60]))
                continue
            CR, LF = chr(13), chr(10)
            nl = (CR + LF) if (CR + LF) in s else LF
            lines = s.split(nl)
            out, i, found, dirty = [], 0, False, False
            while i < len(lines):
                ln = lines[i]
                if ln.strip() != "- id: compaction-basic":
                    out.append(ln)
                    i += 1
                    continue
                found = True
                out.append(ln)
                if i + 1 >= len(lines):
                    i += 1
                    continue
                name_ln = lines[i + 1]
                out.append(name_ln)
                ind = name_ln[:len(name_ln) - len(name_ln.lstrip())]
                # 这一条的边界：到下一条同级的 "- " 之前（或空行为止）。
                j = i + 2
                while j < len(lines):
                    t2 = lines[j]
                    if not t2.strip():
                        break
                    if t2.lstrip().startswith("- ") and \
                            len(t2) - len(t2.lstrip()) <= len(ind):
                        break
                    j += 1
                body = lines[i + 2:j]
                # 第一步：清掉任何行级 disabled:（它跟 auto 表达同一件事，
                # 留着的话引擎根本不加载，写 auto:false 也没用）。
                clean = []
                for x in body:
                    sx = x.strip()
                    if sx.startswith("disabled:") and \
                            len(x) - len(x.lstrip()) <= len(ind) + 2:
                        dirty = True
                        continue
                    clean.append(x)
                body = clean
                # 第二步：找 config: 块
                cfg_at = -1
                for k2, x in enumerate(body):
                    if x.strip() == "config:":
                        cfg_at = k2
                        break
                if cfg_at < 0:
                    # 没有 config 块（standard/cordis 就是）-> 补一个。
                    # config 与 name 同级（ind），里面的字段再深两格。
                    body.append(ind + "config:")
                    body.append(ind + "  auto: false")
                    dirty = True
                    out += body
                    i = j
                    continue
                cind = body[cfg_at][:len(body[cfg_at]) - len(body[cfg_at].lstrip())]
                cend = cfg_at + 1
                while cend < len(body):
                    x = body[cend]
                    if not x.strip():
                        break
                    if len(x) - len(x.lstrip()) <= len(cind):
                        break
                    cend += 1
                inner = body[cfg_at + 1:cend]
                # 第三步：在 config 里写 auto: false（幂等）
                has_off = any(x.strip() == "auto: false" for x in inner)
                if has_off:
                    out += body
                    i = j
                    continue
                new_inner, put = [], False
                for x in inner:
                    if x.strip().startswith("auto:"):
                        new_inner.append(cind + "  auto: false")
                        put = True
                    else:
                        new_inner.append(x)
                if not put:
                    new_inner.append(cind + "  auto: false")
                out += body[:cfg_at + 1] + new_inner + body[cend:]
                dirty = True
                i = j
            if not found or not dirty:
                continue
            try:
                path.write_text(nl.join(out), encoding="utf-8")
                changed += 1
                notes.append(name)
            except OSError as exc:
                notes.append("%s 写不进: %s" % (name, str(exc)[:60]))
        if changed:
            return changed, ("已关掉 %d 个预设的自动压缩（%s，auto=false），"
                             "dsh 热生效；压缩引擎保留，按需取摘要仍可用"
                             % (changed, ", ".join(notes)))
        return 0, ("自动压缩本来就是关的（查了 %d 个预设）" % checked)
    except BaseException as exc:                # noqa: BLE001
        return 0, "检查自动压缩时出错：" + str(exc)[:120]


def preset_files_to_check():
    """所有**在用**的 preset 文件 → [(名字, path)]。

    查全部四个出厂 preset，不只是默认那个 —— 因为「切换分组」时可能换到
    另一个 preset，那时它里面的自动压缩也得是关的（用户口径「还有新建分组
    切换分组的时候」）。minimal 没挂 compaction，跳过即可（找不到条目就 continue）。
    """
    out = []
    try:
        root = (PREV_ROOT / "node_modules" / "@deepseek-ai"
                / "dsh-agent-presets" / "presets")
        for name in ("ptc", "standard", "cordis", "minimal"):
            out.append((name, root / name / "agent.cordis.yml"))
    except BaseException:            # noqa: BLE001
        pass
    return out

def main():
    # 2026-09-26 第56步：命令行直起时 stdout 可能是 GBK 控制台（pythonw
    # 下则是 None，print 会静默跳过）。main 里有非 GBK 字符（如 U+26A0），
    # 一编不出来就 UnicodeEncodeError 把整个进程带走 —— 桥已经 bind 好、
    # 账号也打出来了，却死在最后一句提示上。改成无法编码时降级成 ? 而不是抛。
    try:
        sys.stdout.reconfigure(errors="replace")
    except Exception:
        pass


    ap = argparse.ArgumentParser(
        description="把 DeepSeek 手机账号包装成 OpenAI 兼容的本地模型服务")
    ap.add_argument("--host", default="0.0.0.0",
                    help="监听地址，默认 0.0.0.0（局域网可访问）。"
                         "桥接没有鉴权，任何能连到这个端口的人都能用你的账号，"
                         "只想给本机用就传 127.0.0.1")
    ap.add_argument("--port", type=int, default=11999)
    ap.add_argument("--account", help="只用 ds_auth.json 里的这一个账号"
                                      "（默认全部账号组成一个池）")
    ap.add_argument("--index", type=int, help="账号下标（和 --account 二选一）")
    ap.add_argument("--min-interval", type=float, default=3.0,
                    help="同一个账号两次上游请求之间至少隔多久，单位秒")
    ap.add_argument("--quiet", action="store_true")
    args = ap.parse_args()

    target = None
    if args.account or args.index is not None:
        cfg = ds_api.load_accounts()
        account = None
        if args.account:
            account = next((a for a in cfg["accounts"]
                            if a.get("name") == args.account), None)
            if account is None:
                sys.exit(f"ds_auth.json 里没有账号「{args.account}」")
        target = ds_api.DeepSeek(account=account, index=args.index)

    srv, pool = make_server(target, args.host, args.port, args.min_interval,
                            log=not args.quiet)
    # 2026-09-27 第326步：命令行直起也要吃 ds_bridge.ini —— 这些值原先
    # 靠 GUI 启动桥时灌进来，GUI 摘掉后不读就等于静默退回出厂值。
    _ini_apply(pool)
    if not pool.bridges:
        sys.exit("ds_auth.json 里没有可用账号，先用 ds_token.py 或 GUI 加一个")
    # 2026-09-27 第379步：启动时把 dsh 那份 patch 重贴一遍（账号 + 分组）。
    # 这份文件原先只有 ds_gui.py 会写，GUI 停用后就成了没人维护的存量 ——
    # dsh 选择器里既看不到新账号，也看不到任何分组。现在桥自己保证它是最新
    # 的：启动一次，之后组表每次变动再由 _ini_auto_resync 补一次。
    # 放在账号检查之后：bridges 为空时上面已经 sys.exit，不会写出空 patch。
    _pd_ok, _pd_msg = pool.sync_dsh_patch_now()
    print("  " + _pd_msg)
    # 2026-10-02（用户口径「以后桥开启先检查所有窗口的点有没有自动关掉」）：
    # 启动时把 dsh 的**自动压缩**关掉（auto: false）。
    # 这些 preset 在 node_modules 里，dsh 升级会被覆盖回出厂值 → 每次启动重贴。
    # 见 ensure_auto_compact_off 的注释（先查过 API：只有 compact*，没有「关」）。
    try:
        _ac_n, _ac_msg = ensure_auto_compact_off()
        print("  " + _ac_msg)
    except BaseException as _ac_exc:            # noqa: BLE001
        print("  自动压缩检查失败（不影响启动）：" + str(_ac_exc)[:120])
    # 0.0.0.0 是绑定地址，不是能填给客户端的地址 —— 客户端要填具体 IP
    client = "127.0.0.1" if args.host in ("0.0.0.0", "::", "") else args.host
    print(f"DeepSeek 桥接已启动：监听 {args.host}:{args.port}")
    for st in pool.status():
        print(f"  账号 {st['name']}  →  钉住后缀 @{st['slug']}")
    print(f"  自动挑号的模型名：{' / '.join(MODELS)}")
    print(f"  dsh / Codex 里填 baseURL = http://{client}:{args.port}/v1"
          "（局域网的机器换成本机 IP）")
    if args.host not in ("127.0.0.1", "localhost", "::1"):
        print("  [注意] 没有鉴权且监听非本机地址：任何能连到这个端口的人"
              "都能用你的 DeepSeek 账号，自己拿防火墙拦一下")

    # 2026-09-26 第105步：桥一起来就把监控台也带起来（用户要求）。
    _dash_ok, _dash_msg = start_dashboard()
    print("  " + _dash_msg)
    # 2026-10-02 第483步（用户口径「为什么会死 要彻底根治」）：
    # **起来之后自检一次，确认是真的在服务。**
    #
    # 今晚反复出现「进程在、端口在听、但连不上」—— 而 pythonw 没控制台，
    # 启动失败的信息全丢了，用户只能看到 GUI 一直在转。所以这里主动自检：
    # 起一个线程 5 秒后连自己一次，连不上就把原因写进**日志文件**
    # （不是 stdout，pythonw 下 stdout 是 None）。
    def _selfcheck():
        import time as _t
        _t.sleep(5)
        try:
            with socket.socket() as _s:
                _s.settimeout(3)
                _rc = _s.connect_ex((probe, args.port))
            if _rc != 0:
                _msg = ("桥自检失败：监听端口连不上（rc=%s）。"
                        "多半是**半死实例**占着端口 —— 杀干净再启。"
                        % _rc)
                try:
                    with (STATE / "ds_bridge.log").open("a", encoding="utf-8") as _fh:
                        _fh.write(_t.strftime("%Y-%m-%d %H:%M:%S")
                                  + " [SELFCHECK] " + _msg + chr(10))
                except OSError:
                    pass
                print("  " + _msg)
            else:
                print("  自检通过：端口可连")
        except BaseException:        # noqa: BLE001
            pass

    threading.Thread(target=_selfcheck, daemon=True).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\n收到 Ctrl+C，收工")
    finally:
        srv.server_close()



if __name__ == "__main__":
    main()
