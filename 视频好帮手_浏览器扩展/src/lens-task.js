// Google Lens 图片反向搜索 —— 扩展侧任务处理器（新增文件）
//
// 完整链路（全自动，无需人工点击）：
//   Bridge GET /v1/lens/next            领取任务 + 拿到图片 base64
//     → POST https://lens.google.com/v3/upload   multipart 字段 encoded_image
//     → 303 跳转到 www.google.com/search?...&udm=26（Lens 结果页）
//     → chrome.tabs.create 打开结果页（该页 100% 由 JS 渲染，必须真实浏览器）
//     → chrome.scripting.executeScript 注入：等待结果 / 滚动 / 点更多 / 解析
//     → Bridge POST /v1/lens/result     回传结构化 JSON
//
// 设计约束：
//   - 只新增，不改动任何下载相关逻辑
//   - 解析全部在扩展里完成，Bridge 不解析 HTML
//   - 拿不到的字段一律 null，绝不用缩略图冒充原图，绝不伪造成功

import { loadBridgeConfig, trimEndpoint, autoPair } from "./bridge-client.js";

const LOG_PREFIX = "[视频好帮手/Lens]";

export const LENS_POLL_ALARM = "视频好帮手-lens-poll";

// Bridge 轮询间隔：空闲 2s，Bridge 不可达时退避到 10s
const POLL_IDLE_MS = 2000;
const POLL_BACKOFF_MS = 10000;

const UPLOAD_TIMEOUT_MS = 60000;
const BRIDGE_TIMEOUT_MS = 15000;

// 结果页等待与滚动上限
const RESULT_WAIT_TIMEOUT_MS = 45000;
const MAX_SCROLL_ROUNDS = 25;
const MAX_RESULTS_DEFAULT = 100;

let polling = false;
let busy = false;
let lastPollState = "";

function notePollState(state) {
  // 状态变化时才打一条日志，避免 2 秒一条刷屏
  if (state !== lastPollState) {
    lastPollState = state;
    log("轮询状态：" + state);
  }
}


function log(...args) {
  console.info(LOG_PREFIX, ...args);
}

function warn(...args) {
  console.warn(LOG_PREFIX, ...args);
}

function sleep(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

// ---------------------------------------------------------------------------
// Bridge 通信（沿用 bridge-client.js 的 endpoint/token 存储契约）
// ---------------------------------------------------------------------------

async function bridgeRequest(path, { method = "GET", body = null, timeoutMs = BRIDGE_TIMEOUT_MS } = {}) {
  const config = await loadBridgeConfig();
  const endpoint = trimEndpoint(config.endpoint);
  const token = typeof config.token === "string" ? config.token.trim() : "";

  if (!endpoint) return { ok: false, reason: "missing-endpoint" };
  if (!token) return { ok: false, reason: "missing-token" };

  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), timeoutMs);

  try {
    const init = {
      method,
      headers: { Authorization: `Bearer ${token}` },
      signal: controller.signal,
    };
    if (body !== null) {
      init.headers["Content-Type"] = "application/json";
      init.body = JSON.stringify(body);
    }
    const response = await fetch(`${endpoint}${path}`, init);
    const parsed = await response.json().catch(() => null);
    if (!response.ok) {
      return { ok: false, reason: "http-error", status: response.status, body: parsed };
    }
    return { ok: true, body: parsed };
  } catch (error) {
    return { ok: false, reason: "fetch-failed", message: error?.message ?? String(error) };
  } finally {
    clearTimeout(timer);
  }
}

async function reportProgress(taskId, stage, message = "", lensUrl = null) {
  const payload = { task_id: taskId, stage, message };
  if (lensUrl) payload.lens_url = lensUrl;
  const result = await bridgeRequest("/v1/lens/progress", { method: "POST", body: payload });
  // Bridge 顺带回传 cancelled 标记，用来在用户点「停止搜索」后及时中断
  return Boolean(result.ok && result.body?.cancelled);
}

async function reportResult(payload) {
  return bridgeRequest("/v1/lens/result", { method: "POST", body: payload });
}

async function reportDebug(name, content, { base64 = false, append = false } = {}) {
  return bridgeRequest("/v1/lens/debug", {
    method: "POST",
    body: { name, content, base64, append },
  });
}

async function reportExtensionError(taskId, message) {
  const line = `[${new Date().toISOString()}] task=${taskId || "-"} ${message}`;
  await reportDebug("extension_error.log", line, { append: true }).catch(() => {});
}

// ---------------------------------------------------------------------------
// 图片上传
// ---------------------------------------------------------------------------

function base64ToBlob(base64, mime) {
  const binary = atob(base64);
  const bytes = new Uint8Array(binary.length);
  for (let i = 0; i < binary.length; i += 1) {
    bytes[i] = binary.charCodeAt(i);
  }
  return new Blob([bytes], { type: mime || "image/png" });
}

function buildUploadUrl() {
  const params = new URLSearchParams({
    ep: "ccm", // EntryPoint = ChromeChipMenu
    re: "dcsp", // RenderingEnvironment = DesktopChromeSurfaceProto
    s: "4", // Surface = CHROMIUM
    st: String(Date.now()),
    sideimagesearch: "1",
    vpw: "1280",
    vph: "900",
  });
  return `https://lens.google.com/v3/upload?${params.toString()}`;
}

// 方案 A：直接 POST 到 Lens 上传端点，跟随 303 拿到结果页 URL。
// 这是 chrome-lens-ocr / google-lens-python / Google-Reverse-Image-Search
// 共用的公开端点，无需任何 API Key。
async function uploadViaLensEndpoint(task) {
  const blob = base64ToBlob(task.image_base64, task.image_mime);
  const form = new FormData();
  form.append("encoded_image", blob, task.image || "image.png");
  form.append("original_width", "0");
  form.append("original_height", "0");

  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), UPLOAD_TIMEOUT_MS);

  try {
    const response = await fetch(buildUploadUrl(), {
      method: "POST",
      body: form,
      credentials: "include",
      redirect: "follow",
      signal: controller.signal,
      headers: {
        Accept: "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
      },
    });

    const finalUrl = response.url || "";
    log("上传响应", response.status, finalUrl.slice(0, 160));

    if (!finalUrl || !/[?&]vsrid=/.test(finalUrl)) {
      return { ok: false, reason: "no-vsrid", status: response.status, url: finalUrl };
    }
    return { ok: true, url: finalUrl };
  } catch (error) {
    return { ok: false, reason: "fetch-failed", message: error?.message ?? String(error) };
  } finally {
    clearTimeout(timer);
  }
}

// 打开标签页。
// service worker 刚被唤醒时浏览器还没有"当前窗口"，chrome.tabs.create /
// chrome.windows.create 都会直接报 No current window，所以这里逐级退化并重试：
// 常规 create → 显式指定 windowId → 复用已有标签页 → 新建窗口。
async function openTab(url) {
  const tryOnce = async () => {
    try {
      return await chrome.tabs.create({ url, active: true });
    } catch (e) {
      warn("tabs.create 失败，改用显式窗口", e);
    }

    let windows = [];
    try {
      windows = await chrome.windows.getAll({ populate: true });
    } catch (e) {
      warn("windows.getAll 失败", e);
    }

    const normal = windows.filter((w) => w.type === "normal");
    const target = normal[0] || windows[0] || null;

    if (target) {
      try {
        return await chrome.tabs.create({ url, active: true, windowId: target.id });
      } catch (e) {
        warn("指定 windowId 建标签页失败，尝试复用已有标签页", e);
      }

      const tabs = target.tabs || [];
      const idle = tabs.find((t) => {
        const u = t.url || "";
        return u === "about:blank" || u.startsWith("edge://newtab") || u.startsWith("chrome://newtab");
      });
      const reusable = idle || tabs[0];
      if (reusable) {
        try {
          return await chrome.tabs.update(reusable.id, { url, active: true });
        } catch (e) {
          warn("复用标签页失败", e);
        }
      }
    }

    try {
      const win = await chrome.windows.create({ url, focused: true });
      return win?.tabs?.[0] || null;
    } catch (e) {
      warn("windows.create 失败", e);
      return null;
    }
  };

  for (let attempt = 0; attempt < 4; attempt += 1) {
    const tab = await tryOnce();
    if (tab && tab.id) return tab;
    // 唤醒竞态是暂时的，等一下再试
    await sleep(800);
  }
  return null;
}

// 方案 B（兜底）：打开 lens.google.com，用 DataTransfer 把文件塞进真实
// <input type=file name=encoded_image>，触发页面自身的上传流程。
async function uploadViaLensPage(task) {
  let tabId = null;

  try {
    const tab = await openTab("https://lens.google.com/?hl=en");
    if (!tab || !tab.id) {
      return { ok: false, reason: "no-tab", tabId };
    }
    tabId = tab.id;

    await waitForTabComplete(tabId, 30000);

    const [injected] = await chrome.scripting.executeScript({
      target: { tabId },
      args: [task.image_base64, task.image_mime || "image/png", task.image || "image.png"],
      func: async (b64, mime, name) => {
        const findInput = () =>
          document.querySelector('input[type="file"][name="encoded_image"]') ||
          document.querySelector('input[type="file"]');

        let input = null;
        for (let i = 0; i < 60; i += 1) {
          input = findInput();
          if (input) break;
          await new Promise((r) => setTimeout(r, 250));
        }
        if (!input) return { ok: false, reason: "no-file-input" };

        const binary = atob(b64);
        const bytes = new Uint8Array(binary.length);
        for (let i = 0; i < binary.length; i += 1) bytes[i] = binary.charCodeAt(i);
        const file = new File([bytes], name, { type: mime });

        const dt = new DataTransfer();
        dt.items.add(file);
        input.files = dt.files;
        input.dispatchEvent(new Event("input", { bubbles: true }));
        input.dispatchEvent(new Event("change", { bubbles: true }));
        return { ok: true, inputName: input.name || null };
      },
    });

    if (!injected?.result?.ok) {
      return { ok: false, reason: injected?.result?.reason || "inject-failed", tabId };
    }

    const url = await waitForTabUrl(tabId, /[?&]vsrid=/, 45000);
    if (!url) return { ok: false, reason: "no-navigation", tabId };
    return { ok: true, url, tabId };
  } catch (error) {
    return { ok: false, reason: "page-upload-error", message: error?.message ?? String(error), tabId };
  }
}

// ---------------------------------------------------------------------------
// 标签页辅助
// ---------------------------------------------------------------------------

async function waitForTabComplete(tabId, timeoutMs) {
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    const tab = await chrome.tabs.get(tabId).catch(() => null);
    if (!tab) return false;
    if (tab.status === "complete") return true;
    await sleep(300);
  }
  return false;
}

async function waitForTabUrl(tabId, pattern, timeoutMs) {
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    const tab = await chrome.tabs.get(tabId).catch(() => null);
    if (!tab) return null;
    if (tab.url && pattern.test(tab.url)) return tab.url;
    await sleep(400);
  }
  return null;
}

async function closeTab(tabId) {
  if (!tabId) return;
  // 不能关掉窗口里最后一个标签页，否则整个浏览器会退出
  try {
    const tab = await chrome.tabs.get(tabId);
    const siblings = await chrome.tabs.query({ windowId: tab.windowId });
    if (siblings.length <= 1) {
      await chrome.tabs.update(tabId, { url: "about:blank" }).catch(() => {});
      return;
    }
  } catch (e) {
    // 查询失败就按普通关闭处理
  }
  await chrome.tabs.remove(tabId).catch(() => {});
}

// ---------------------------------------------------------------------------
// /goto?url=<token> → 真实来源页面 URL
//
// 2026 年的 Lens 结果页把站外链接全部换成了 www.google.com/goto?url=<不透明 token>，
// token 无法本地解码，只能真的发一次请求看它 302 到哪里。
// 跨域那一跳会被 CORS 拦掉（扩展没有全站 host 权限），所以同时用 webRequest
// 的 onBeforeRedirect 把重定向目标记下来——这一步不需要目标站点的权限。
// ---------------------------------------------------------------------------

const gotoRedirects = new Map();
let gotoListenerInstalled = false;

function installGotoRedirectListener() {
  if (gotoListenerInstalled) return;
  try {
    chrome.webRequest.onBeforeRedirect.addListener(
      (details) => {
        if (details.url && details.url.includes("/goto?url=")) {
          gotoRedirects.set(details.url, details.redirectUrl);
        }
      },
      { urls: ["*://www.google.com/goto*", "*://*.google.com/goto*"] }
    );
    gotoListenerInstalled = true;
  } catch (e) {
    warn("安装 /goto 重定向监听失败", e);
  }
}

function isGoogleHost(url) {
  try {
    return /(^|\.)(google\.[a-z.]+|gstatic\.com|googleusercontent\.com)$/i.test(
      new URL(url).hostname
    );
  } catch {
    return true;
  }
}

async function resolveGotoUrl(gotoPath) {
  installGotoRedirectListener();

  let abs;
  try {
    abs = new URL(gotoPath, "https://www.google.com").href;
  } catch {
    return null;
  }

  gotoRedirects.delete(abs);

  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), 12000);
  try {
    const response = await fetch(abs, {
      credentials: "include",
      redirect: "follow",
      signal: controller.signal,
    });
    if (response.url && !isGoogleHost(response.url)) {
      return response.url;
    }
  } catch {
    // 跨域被拦是预期行为，重定向目标已经被 webRequest 记下
  } finally {
    clearTimeout(timer);
  }

  const captured = gotoRedirects.get(abs);
  if (captured && !isGoogleHost(captured)) return captured;
  return null;
}

/** 批量还原来源 URL；还原不出来的条目直接丢弃，绝不用站点首页凑数 */
async function resolveSourceUrls(items, onProgress) {
  const CONCURRENCY = 4;
  let done = 0;

  for (let i = 0; i < items.length; i += CONCURRENCY) {
    const slice = items.slice(i, i + CONCURRENCY);
    await Promise.all(
      slice.map(async (item) => {
        if (item.source_url || !item.goto) return;
        item.source_url = await resolveGotoUrl(item.goto);
      })
    );
    done += slice.length;
    if (onProgress) await onProgress(done, items.length);
  }

  const out = [];
  const seen = new Set();
  for (const item of items) {
    if (!item.source_url) continue;
    if (seen.has(item.source_url)) continue;
    seen.add(item.source_url);

    let source = item.source;
    if (!source) {
      try {
        source = new URL(item.source_url).hostname.replace(/^www\./, "");
      } catch {
        source = null;
      }
    }

    out.push({
      title: item.title ?? null,
      thumbnail_url: item.thumbnail_url ?? null,
      original_image_url: item.original_image_url ?? null,
      source_url: item.source_url,
      source: source ?? null,
      match_type: item.match_type ?? null,
    });
  }
  return out;
}


// ---------------------------------------------------------------------------
// 结果页注入脚本
//
// 全部写成"通用启发式 + 已知选择器"两层，避免 Google 换 class 名就整体失效。
// ---------------------------------------------------------------------------

/** 页面就绪 / 拦截检测 */
function pageProbe() {
  const url = location.href;
  const text = (document.body?.innerText || "").slice(0, 4000);

  if (/\/sorry\/|unusual traffic|异常流量/i.test(url + text)) {
    return { state: "blocked", reason: "google-sorry-captcha", url };
  }
  if (/consent\.google\.com/.test(url)) {
    return { state: "consent", url };
  }

  // Lens 结果页的两种形态：
  //  1) 卡片数据藏在 <!--TgQPHd|||[...]--> 注释里，链接是 /goto?url=<不透明 token>
  //  2) 传统结果列表，锚点 href 直接是站外真实 URL
  const GOOGLE_HOSTS = /(^|\.)(google\.[a-z.]+|gstatic\.com|googleapis\.com|googleusercontent\.com|withgoogle\.com|googleadservices\.com)$/i;

  let cards = 0;
  // Lens 会在 hydration 后移除这些注释 payload，所以每次探测都先把它们缓存到页面上
  const store = (window.__ogLensPayloads = window.__ogLensPayloads || []);
  const walker = document.createTreeWalker(
    document.documentElement,
    NodeFilter.SHOW_COMMENT
  );
  while (walker.nextNode()) {
    const v = walker.currentNode.nodeValue || "";
    // 注释里 "=" 被转义成 \u003d，所以只匹配到 "/goto?url"；一条注释可能含多张卡片
    const hits = v.split("/goto?url").length - 1;
    if (hits > 0) {
      cards += hits;
      if (store.indexOf(v) === -1) store.push(v);
    }
  }



  let anchors = 0;
  for (const a of document.querySelectorAll("a[href]")) {
    const raw = a.getAttribute("href") || "";
    if (raw.startsWith("/goto?url")) {
      anchors += 1;
      continue;
    }
    if (/[?&](q|imgrefurl|url)=https?(%3A|:)/i.test(raw)) {
      anchors += 1;
      continue;
    }
    try {
      const u = new URL(a.href, location.href);
      if (/^https?:$/.test(u.protocol) && !GOOGLE_HOSTS.test(u.hostname)) anchors += 1;
    } catch {}
  }

  const candidates = cards + anchors;
  return {
    state: candidates > 0 ? "ready" : "loading",
    candidates,
    cards,
    anchors,
    url,
    title: document.title,
  };
}

/** 滚动一轮 + 尝试点击「更多结果」，返回当前候选数 */
async function scrollRound() {
  const clicked = [];

  // 1) 找到真正在滚动的容器（Lens 结果在内部面板里）
  const scrollers = [document.scrollingElement, document.body].filter(Boolean);
  for (const el of document.querySelectorAll("div,main,section")) {
    if (el.scrollHeight > el.clientHeight + 120 && el.clientHeight > 200) {
      scrollers.push(el);
      if (scrollers.length > 8) break;
    }
  }
  for (const el of scrollers) {
    try {
      el.scrollTop = el.scrollHeight;
    } catch {}
  }
  window.scrollTo(0, document.body.scrollHeight);

  // 2) 点击「更多视觉匹配 / More visual matches / 显示更多」类按钮
  const MORE_RE = /(more (visual )?(matches|results)|see more|show more|更多(视觉)?(匹配|结果)?|显示更多|查看更多)/i;
  const clickable = document.querySelectorAll(
    "button,[role='button'],a[jsaction],div[jsaction][tabindex]"
  );
  for (const el of clickable) {
    const label = (el.getAttribute("aria-label") || el.innerText || "").trim();
    if (!label || label.length > 40) continue;
    if (!MORE_RE.test(label)) continue;
    if (el.dataset.__ogLensClicked === "1") continue;
    const href = el.getAttribute("href") || "";
    if (/^https?:/i.test(href)) continue; // 站外链接不是"更多"按钮
    el.dataset.__ogLensClicked = "1";
    try {
      el.click();
      clicked.push(label);
    } catch {}
    if (clicked.length >= 2) break;
  }

  await new Promise((r) => setTimeout(r, 1200));

  const GOOGLE_HOSTS = /(^|\.)(google\.[a-z.]+|gstatic\.com|googleapis\.com|googleusercontent\.com|withgoogle\.com|googleadservices\.com)$/i;
  let count = 0;
  const store = (window.__ogLensPayloads = window.__ogLensPayloads || []);
  const walker = document.createTreeWalker(
    document.documentElement,
    NodeFilter.SHOW_COMMENT
  );
  while (walker.nextNode()) {
    const v = walker.currentNode.nodeValue || "";
    if (v.includes("/goto?url") && store.indexOf(v) === -1) store.push(v);
  }
  // 累计缓存里的卡片数（注释会被 Lens 移除，所以只统计缓存，保证单调递增）
  let payloadCards = 0;
  for (const v of store) payloadCards += v.split("/goto?url").length - 1;

  let gotoAnchors = 0;
  let extAnchors = 0;
  for (const a of document.querySelectorAll("a[href]")) {
    const raw = a.getAttribute("href") || "";
    if (raw.startsWith("/goto?url")) {
      gotoAnchors += 1;
      continue;
    }
    if (/[?&](q|imgrefurl|url)=https?(%3A|:)/i.test(raw)) {
      extAnchors += 1;
      continue;
    }
    try {
      const u = new URL(a.href, location.href);
      if (/^https?:$/.test(u.protocol) && !GOOGLE_HOSTS.test(u.hostname)) extAnchors += 1;
    } catch {}
  }

  count = Math.max(payloadCards, gotoAnchors) + extAnchors;
  return { count, payloadCards, gotoAnchors, extAnchors, clicked, scrollerCount: scrollers.length };
}


/** 解析所有可见结果，返回标准 JSON 数组 */
function extractResults(maxResults) {
  const GOOGLE_HOSTS = /(^|\.)(google\.[a-z.]+|gstatic\.com|googleapis\.com|googleusercontent\.com|withgoogle\.com|googleadservices\.com|schema\.org|w3\.org)$/i;

  const brandOf = (host) => {
    const parts = String(host || "").replace(/^www\./, "").split(".");
    if (!parts.length) return "";
    const known = {
      "tiktok": "TikTok",
      "youtube": "YouTube",
      "youtu": "YouTube",
      "instagram": "Instagram",
      "facebook": "Facebook",
      "fb": "Facebook",
      "reddit": "Reddit",
      "pinterest": "Pinterest",
      "twitter": "X",
      "x": "X",
      "vimeo": "Vimeo",
      "bilibili": "Bilibili",
      "twitch": "Twitch",
      "threads": "Threads",
      "linkedin": "LinkedIn",
      "weibo": "Weibo",
      "douyin": "Douyin",
      "snapchat": "Snapchat",
      "tumblr": "Tumblr",
    };
    const base = parts[0].toLowerCase();
    if (known[base]) return known[base];
    return base.charAt(0).toUpperCase() + base.slice(1);
  };

  // 段落标题 → match_type
  const headingType = (el) => {
    let node = el;
    for (let depth = 0; node && depth < 12; depth += 1) {
      let sib = node.previousElementSibling;
      while (sib) {
        const t = (sib.innerText || "").trim().slice(0, 60);
        if (t) {
          if (/exact match|完全匹配|精确匹配|pages (that )?include|包含此图片/i.test(t)) return "exact_match";
          if (/visual match|视觉匹配|相似图片|similar/i.test(t)) return "visual_match";
          if (/related|相关/i.test(t)) return "related";
        }
        sib = sib.previousElementSibling;
      }
      node = node.parentElement;
    }
    return null;
  };

  const pickThumb = (scope) => {
    const imgs = Array.from(scope.querySelectorAll("img"));
    for (const img of imgs) {
      const src = img.currentSrc || img.src || img.getAttribute("data-src") || "";
      if (!src) continue;
      if (/^data:image\/gif/i.test(src)) continue; // 占位图
      const w = img.naturalWidth || img.width || 0;
      const h = img.naturalHeight || img.height || 0;
      if (w && w < 24 && h && h < 24) continue; // favicon
      return src;
    }
    return null;
  };

  // 原图 URL：只有页面真的带 imgurl / imgrefurl 参数时才算拿到
  const pickOriginal = (scope, anchorHref) => {
    for (const a of scope.querySelectorAll("a[href]")) {
      const href = a.getAttribute("href") || "";
      const m = /[?&]imgurl=([^&]+)/.exec(href);
      if (m) {
        try {
          return decodeURIComponent(m[1]);
        } catch {
          return m[1];
        }
      }
    }
    for (const el of scope.querySelectorAll("[data-lpage],[data-original-url],[data-src-original]")) {
      const v =
        el.getAttribute("data-original-url") || el.getAttribute("data-src-original") || "";
      if (/^https?:\/\//.test(v)) return v;
    }
    return null;
  };

  const results = [];
  const seen = new Set();
  const diag = {
    comments: 0,
    comments_with_goto: 0,
    json_fail: 0,
    cards_found: 0,
    skip_no_goto: 0,
    skip_no_thumb: 0,
    skip_seen: 0,
    anchor_results: 0,
  };


  const pageUdm = new URLSearchParams(location.search).get("udm") || "";
  const pageMatchType =
    pageUdm === "48" ? "exact_match" : pageUdm === "26" ? "visual_match" : null;

  // ------------------------------------------------------------------
  // 路径 A：解析 <!--TgQPHd|||[...]--> 注释里的卡片数据
  //
  // 2026 年的 Lens「外观匹配」页面不再在 DOM 里放站外真实链接，
  // 每张卡片只有 /goto?url=<不透明 token>，真实 URL 必须由扩展跟随重定向拿到。
  // 卡片数据本身（标题 / 来源站点 / 缩略图）就在这些注释里，索引可能变动，
  // 所以这里不写死下标，而是把 payload 里所有字符串扁平化后按特征匹配。
  // ------------------------------------------------------------------
  // 注释 payload 会被 Lens 在 hydration 后移除，探测/滚动阶段已把它们缓存在页面上
  const payloads = [];
  const store = window.__ogLensPayloads;
  if (Array.isArray(store)) {
    for (const v of store) if (typeof v === "string") payloads.push(v);
  }
  const commentWalker = document.createTreeWalker(
    document.documentElement,
    NodeFilter.SHOW_COMMENT
  );
  while (commentWalker.nextNode()) {
    const v = commentWalker.currentNode.nodeValue || "";
    if (v && payloads.indexOf(v) === -1) payloads.push(v);
  }


  const UUID_RE = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;

  // 注释里的引号被转成 HTML 实体，"=" 被 JSON 转义成 \u003d（JSON.parse 会还原）
  const unentity = (s) =>
    s
      .replace(/&quot;/g, '"')
      .replace(/&#39;/g, "'")
      .replace(/&lt;/g, "<")
      .replace(/&gt;/g, ">")
      .replace(/&amp;/g, "&");

  // 一条注释 payload 里可能装着多张卡片，卡片本体是「直接含 /goto 字符串的数组」
  const collectCards = (node, out) => {
    if (Array.isArray(node)) {
      if (node.some((v) => typeof v === "string" && v.startsWith("/goto?url"))) {
        out.push(node);
        return;
      }
      for (const v of node) collectCards(v, out);
    } else if (node && typeof node === "object") {
      for (const v of Object.values(node)) collectCards(v, out);
    }
  };

  const flatten = (node, out) => {
    if (typeof node === "string") out.push(node);
    else if (Array.isArray(node)) for (const v of node) flatten(v, out);
    else if (node && typeof node === "object") for (const v of Object.values(node)) flatten(v, out);
  };

  for (let pi = 0; pi < payloads.length; pi += 1) {
    if (results.length >= maxResults) break;

    let raw = payloads[pi];
    diag.comments += 1;

    if (!raw.includes("/goto?url")) continue;
    diag.comments_with_goto += 1;

    const sep = raw.indexOf("|||");
    if (sep >= 0) raw = raw.slice(sep + 3);
    raw = unentity(raw);

    let parsed = null;
    try {
      parsed = JSON.parse(raw);
    } catch {
      parsed = null;
    }
    if (!parsed) {
      diag.json_fail += 1;
      continue;
    }

    const cards = [];
    collectCards(parsed, cards);
    diag.cards_found += cards.length;

    for (const card of cards) {
      if (results.length >= maxResults) break;

      const strings = [];
      flatten(card, strings);

      const goto = strings.find((s) => s.startsWith("/goto?url"));
      if (!goto) {
        diag.skip_no_goto += 1;
        continue;
      }
      if (seen.has(goto)) {
        diag.skip_seen += 1;
        continue;
      }

      const thumbnail =
        strings.find((s) => /^https:\/\/encrypted-tbn\d*\.gstatic\.com\/images/.test(s)) ||
        strings.find((s) => /^https?:\/\/.+\.(jpg|jpeg|png|webp)(\?|$)/i.test(s)) ||
        null;
      if (!thumbnail) {
        diag.skip_no_thumb += 1;
        continue; // 没有缩略图的是站点聚合卡，不是结果
      }


      let siteRoot = null;
      for (const s of strings) {
        if (!/^https?:\/\/[^/]+\/?$/.test(s)) continue;
        try {
          if (!GOOGLE_HOSTS.test(new URL(s).hostname)) {
            siteRoot = s;
            break;
          }
        } catch {}
      }

      const title =
        strings.find(
          (s) =>
            s &&
            s.length > 3 &&
            s.length < 300 &&
            !s.startsWith("/") &&
            !/^https?:/.test(s) &&
            !UUID_RE.test(s) &&
            !/^[A-Za-z0-9_-]{12,}$/.test(s)
        ) || null;

      let source = null;
      if (siteRoot) {
        try {
          source = brandOf(new URL(siteRoot).hostname);
        } catch {
          source = null;
        }
      }

      seen.add(goto);
      results.push({
        title: title,
        thumbnail_url: thumbnail,
        original_image_url: null, // Lens 只给 gstatic 缩略图，拿不到原图就必须是 null
        source_url: null, // 由扩展跟随 /goto 重定向后填入真实 URL
        goto: goto,
        site_root: siteRoot || null,
        source: source,
        match_type: pageMatchType,
      });
    }
  }

  // ------------------------------------------------------------------
  // 路径 A2：直接遍历渲染出来的卡片锚点 a[href^="/goto?url"]
  // 注释 payload 属于流式数据，可能已被消费掉；锚点是页面上稳定存在的卡片。
  // ------------------------------------------------------------------
  for (const anchor of document.querySelectorAll('a[href^="/goto?url"]')) {
    if (results.length >= maxResults) break;

    const goto = anchor.getAttribute("href") || "";
    if (!goto || seen.has(goto)) continue;

    // 卡片容器：从锚点往上找到第一个含缩略图的容器
    let card = anchor;
    for (let i = 0; i < 6; i += 1) {
      if (card.querySelector && card.querySelector("img")) break;
      if (!card.parentElement) break;
      card = card.parentElement;
    }

    let thumbnail = null;
    let favicon = null;
    for (const img of card.querySelectorAll("img")) {
      const src = img.currentSrc || img.src || img.getAttribute("data-src") || "";
      if (!src) continue;
      if (/faviconV2/i.test(src)) {
        if (!favicon) favicon = src;
        continue;
      }
      if (/^data:image\/gif/i.test(src)) continue;
      if (!thumbnail) thumbnail = src;
    }
    if (!thumbnail) continue;

    const lines = (card.innerText || "")
      .split("\n")
      .map((s) => s.trim())
      .filter(Boolean);

    let title =
      (anchor.getAttribute("aria-label") || "").trim() ||
      lines.slice().sort((a, b) => b.length - a.length)[0] ||
      (card.querySelector("img")?.alt || "").trim();
    if (title && title.length > 300) title = title.slice(0, 300);

    // 来源站点：favicon 的 url 参数就是来源域名，最可靠
    let siteRoot = null;
    let source = null;
    if (favicon) {
      try {
        const fav = new URL(favicon, location.href).searchParams.get("url") || "";
        if (/^https?:\/\//i.test(fav)) {
          const host = new URL(fav).hostname;
          if (!GOOGLE_HOSTS.test(host)) {
            siteRoot = new URL(fav).origin;
            source = brandOf(host);
          }
        }
      } catch {}
    }

    seen.add(goto);
    diag.anchor_results += 1;
    results.push({
      title: title || null,
      thumbnail_url: thumbnail,
      original_image_url: null,
      source_url: null,
      goto: goto,
      site_root: siteRoot,
      source: source,
      match_type: headingType(card) || pageMatchType,
    });
  }



  // Google 中转链接解包：/url?q=<target> 、/imgres?imgurl=<原图>&imgrefurl=<来源页>
  const unwrap = (anchor) => {
    const raw = anchor.getAttribute("href") || "";
    let abs;
    try {
      abs = new URL(anchor.href, location.href);
    } catch {
      return null;
    }

    if (GOOGLE_HOSTS.test(abs.hostname)) {
      const q =
        abs.searchParams.get("imgrefurl") ||
        abs.searchParams.get("q") ||
        abs.searchParams.get("url") ||
        "";
      const img = abs.searchParams.get("imgurl") || "";
      if (/^https?:\/\//i.test(q)) {
        return { href: q, original: /^https?:\/\//i.test(img) ? img : null };
      }
      return null;
    }

    if (!/^https?:/i.test(abs.protocol + ":") && !/^https?:/i.test(raw)) return null;
    return { href: abs.href, original: null };
  };

  for (const anchor of document.querySelectorAll("a[href]")) {
    if (results.length >= maxResults) break;

    const unwrapped = unwrap(anchor);
    if (!unwrapped) continue;

    const href = unwrapped.href;
    let host = "";
    try {
      host = new URL(href).hostname;
    } catch {
      continue;
    }
    if (GOOGLE_HOSTS.test(host)) continue;
    if (seen.has(href)) continue;

    // 找到承载这条结果的卡片：向上找到同时含图片和文字的最小容器
    let card = anchor;
    for (let i = 0; i < 5; i += 1) {
      if (card.querySelector("img") && (card.innerText || "").trim()) break;
      if (!card.parentElement) break;
      card = card.parentElement;
    }

    const thumbnail = pickThumb(card) || pickThumb(anchor);
    // 没有缩略图的多半是页脚 / 帮助链接，不是搜索结果
    if (!thumbnail) continue;

    // 标题：aria-label → 卡片内最长文本行 → img.alt → title 属性
    const lines = (card.innerText || "")
      .split("\n")
      .map((s) => s.trim())
      .filter(Boolean);

    const longest = lines.slice().sort((a, b) => b.length - a.length)[0] || "";

    let title =
      (anchor.getAttribute("aria-label") || "").trim() ||
      longest ||
      (card.querySelector("img")?.alt || "").trim() ||
      (anchor.getAttribute("title") || "").trim();

    if (title.length > 300) title = title.slice(0, 300);

    // 来源站点名：卡片里与域名品牌相符的短文本，否则用域名推导
    const brand = brandOf(host);
    let source = null;
    for (const line of lines) {
      if (line.length <= 40 && brand && line.toLowerCase().includes(brand.toLowerCase())) {
        source = line;
        break;
      }
    }
    if (!source) source = brand || host;

    seen.add(href);
    results.push({
      title: title || null,
      thumbnail_url: thumbnail,
      original_image_url: unwrapped.original || pickOriginal(card, href),
      source_url: href,
      goto: null,
      site_root: null,
      source: source,
      match_type: headingType(card) || pageMatchType,
    });
  }

  return {
    results,
    url: location.href,
    title: document.title,
    udm: pageUdm,
    diag,
  };
}


// ---------------------------------------------------------------------------
// 关键字搜索（新增）：google.com/search 结果页探测与解析
// ---------------------------------------------------------------------------

/** 在 google.com 首页真实搜索框里输入关键词并提交（比直接拼 search?q= 更不容易触发风控） */
function submitSearchOnPage(query) {
  const box = document.querySelector('textarea[name="q"], input[name="q"]');
  if (!box) return { ok: false, reason: "no-search-box", url: location.href };

  box.focus();
  box.value = query;
  box.dispatchEvent(new Event("input", { bubbles: true }));

  const form =
    box.form ||
    document.querySelector('form[action="/search"], form[role="search"], form');
  if (form) {
    try {
      if (form.requestSubmit) form.requestSubmit();
      else form.submit();
      return { ok: true, via: "form" };
    } catch (e) {
      // 继续走回车
    }
  }

  box.dispatchEvent(
    new KeyboardEvent("keydown", { key: "Enter", keyCode: 13, which: 13, bubbles: true })
  );
  return { ok: true, via: "enter" };
}

/** 结果页顶部的「图片 / 视频」标签，点击比直接改 udm 参数更接近真实操作 */
function clickSearchTab(kind) {
  const RE =
    kind === "videos"
      ? /^(视频|videos)$/i
      : kind === "images"
      ? /^(图片|images)$/i
      : /^(全部|all)$/i;

  for (const a of document.querySelectorAll('a[href*="udm="], a[href*="tbm="], [role="link"]')) {
    const text = (a.innerText || a.getAttribute("aria-label") || "").trim();
    if (RE.test(text)) {
      a.click();
      return { clicked: text };
    }
  }
  return { clicked: null };
}

/** 关键字结果页是否已渲染出可用结果 */

function keywordProbe() {
  const url = location.href;
  const text = (document.body?.innerText || "").slice(0, 3000);

  if (/\/sorry\/|unusual traffic|异常流量/i.test(url + text)) {
    return { state: "blocked", reason: "google-sorry-captcha", url };
  }
  if (/consent\.google\.com/.test(url)) {
    return { state: "consent", url };
  }

  const GOOGLE_HOSTS = /(^|\.)(google\.[a-z.]+|gstatic\.com|googleapis\.com|googleusercontent\.com|withgoogle\.com|googleadservices\.com)$/i;

  let imgres = document.querySelectorAll('a[href*="/imgres?"]').length;
  let external = 0;
  for (const a of document.querySelectorAll("a[href^='http']")) {
    try {
      if (!GOOGLE_HOSTS.test(new URL(a.href).hostname)) external += 1;
    } catch {}
  }

  const candidates = imgres + external;
  return {
    state: candidates > 0 ? "ready" : "loading",
    candidates,
    imgres,
    external,
    url,
    title: document.title,
  };
}

/** 关键字结果页滚动一轮 */
async function keywordScrollRound() {
  window.scrollTo(0, document.body.scrollHeight);
  for (const el of document.querySelectorAll("div,main,section")) {
    if (el.scrollHeight > el.clientHeight + 200 && el.clientHeight > 200) {
      try {
        el.scrollTop = el.scrollHeight;
      } catch {}
    }
  }

  // 「更多结果 / More results」按钮
  const MORE_RE = /(more results|show more|更多结果|显示更多|加载更多)/i;
  const clicked = [];
  for (const el of document.querySelectorAll("button,[role='button'],a[jsaction]")) {
    const label = (el.getAttribute("aria-label") || el.innerText || "").trim();
    if (!label || label.length > 30 || !MORE_RE.test(label)) continue;
    if (el.dataset.__ogKwClicked === "1") continue;
    el.dataset.__ogKwClicked = "1";
    try {
      el.click();
      clicked.push(label);
    } catch {}
    if (clicked.length >= 2) break;
  }

  await new Promise((r) => setTimeout(r, 1200));

  const GOOGLE_HOSTS = /(^|\.)(google\.[a-z.]+|gstatic\.com|googleapis\.com|googleusercontent\.com|withgoogle\.com|googleadservices\.com)$/i;
  let count = document.querySelectorAll('a[href*="/imgres?"]').length;
  for (const a of document.querySelectorAll("a[href^='http']")) {
    try {
      if (!GOOGLE_HOSTS.test(new URL(a.href).hostname)) count += 1;
    } catch {}
  }
  return { count, clicked };
}

/** 解析关键字结果页（图片 udm=2 / 视频 udm=7 / 网页） */
function extractKeywordResults(maxResults) {
  const GOOGLE_HOSTS = /(^|\.)(google\.[a-z.]+|gstatic\.com|googleapis\.com|googleusercontent\.com|withgoogle\.com|googleadservices\.com|schema\.org|w3\.org|youtube\.com\/redirect)$/i;

  const brandOf = (host) => {
    const parts = String(host || "").replace(/^www\./, "").split(".");
    if (!parts.length) return "";
    const known = {
      tiktok: "TikTok", youtube: "YouTube", youtu: "YouTube", instagram: "Instagram",
      facebook: "Facebook", reddit: "Reddit", pinterest: "Pinterest", twitter: "X",
      x: "X", vimeo: "Vimeo", bilibili: "Bilibili", twitch: "Twitch",
      threads: "Threads", linkedin: "LinkedIn", weibo: "Weibo", douyin: "Douyin",
      snapchat: "Snapchat", tumblr: "Tumblr", imgur: "Imgur", flickr: "Flickr",
    };
    const base = parts[0].toLowerCase();
    if (known[base]) return known[base];
    return base.charAt(0).toUpperCase() + base.slice(1);
  };

  const results = [];
  const seen = new Set();
  const diag = { imgres: 0, generic: 0, skipped: 0 };

  const cardOf = (anchor) => {
    let card = anchor;
    for (let i = 0; i < 6; i += 1) {
      if (card.querySelector && card.querySelector("img")) break;
      if (!card.parentElement) break;
      card = card.parentElement;
    }
    return card;
  };

  const thumbOf = (scope) => {
    for (const img of scope.querySelectorAll("img")) {
      const src = img.currentSrc || img.src || img.getAttribute("data-src") || "";
      if (!src) continue;
      if (/faviconV2|^data:image\/gif/i.test(src)) continue;
      return src;
    }
    return null;
  };

  // ---- 路径一：图片搜索页的 /imgres 链接，同时带原图与来源页 ----
  for (const anchor of document.querySelectorAll('a[href*="/imgres?"]')) {
    if (results.length >= maxResults) break;

    let params;
    try {
      params = new URL(anchor.href, location.href).searchParams;
    } catch {
      continue;
    }

    const ref = params.get("imgrefurl") || "";
    const img = params.get("imgurl") || "";
    if (!/^https?:\/\//i.test(ref)) continue;
    if (seen.has(ref)) continue;

    let host = "";
    try {
      host = new URL(ref).hostname;
    } catch {
      continue;
    }
    if (GOOGLE_HOSTS.test(host)) continue;

    const card = cardOf(anchor);
    const thumb = thumbOf(card);

    let title =
      (anchor.getAttribute("aria-label") || "").trim() ||
      (card.querySelector("h3")?.innerText || "").trim() ||
      (card.querySelector("img")?.alt || "").trim() ||
      (card.innerText || "").split("\n").map((s) => s.trim()).filter(Boolean)[0] ||
      null;
    if (title && title.length > 300) title = title.slice(0, 300);

    seen.add(ref);
    diag.imgres += 1;
    results.push({
      title: title,
      thumbnail_url: thumb,
      original_image_url: /^https?:\/\//i.test(img) && img !== thumb ? img : null,
      source_url: ref,
      goto: null,
      site_root: null,
      source: brandOf(host),
      match_type: "keyword_match",
    });
  }

  // ---- 路径二：2026 版图片/网页结果：站外直链（或 data-lpage）+ img[alt] 标题 ----
  const candidates = [];
  for (const el of document.querySelectorAll("a[href^='http'], [data-lpage]")) {
    const raw =
      el.getAttribute("href") || el.getAttribute("data-lpage") || "";
    if (/^https?:\/\//i.test(raw)) candidates.push([el, raw]);
  }

  for (const [el, raw] of candidates) {
    if (results.length >= maxResults) break;

    let host = "";
    let href = "";
    try {
      const u = new URL(raw, location.href);
      host = u.hostname;
      href = u.href;
    } catch {
      continue;
    }
    if (GOOGLE_HOSTS.test(host)) continue;
    if (seen.has(href)) continue;

    const card = cardOf(el);
    const thumb = thumbOf(card);

    const heading =
      (el.querySelector("h3")?.innerText || "").trim() ||
      (card.querySelector("h3")?.innerText || "").trim() ||
      (el.getAttribute("aria-label") || "").trim() ||
      (card.querySelector("img[alt]")?.alt || "").trim() ||
      (card.innerText || "").split("\n").map((s) => s.trim()).filter(Boolean)[0] ||
      "";

    // 既没标题也没缩略图的多是页脚 / 图标链接
    if (!heading && !thumb) {
      diag.skipped += 1;
      continue;
    }

    seen.add(href);
    diag.generic += 1;
    results.push({
      title: heading ? heading.slice(0, 300) : null,
      thumbnail_url: thumb,
      original_image_url: null,
      source_url: href,
      goto: null,
      site_root: null,
      source: brandOf(host),
      match_type: "keyword_match",
    });
  }


  return {
    results,
    url: location.href,
    title: document.title,
    udm: new URLSearchParams(location.search).get("udm") || "",
    diag,
  };
}

/** 同意页自动点击 */

function acceptConsent() {
  const RE = /(accept all|i agree|同意|接受全部|全部接受|reject all)/i;
  for (const el of document.querySelectorAll("button,[role='button'],input[type='submit']")) {
    const label = (el.getAttribute("aria-label") || el.innerText || el.value || "").trim();
    if (label && RE.test(label)) {
      el.click();
      return { clicked: label };
    }
  }
  return { clicked: null };
}

async function runInTab(tabId, func, args = []) {
  const injected = await chrome.scripting.executeScript({ target: { tabId }, func, args });
  return injected?.[0]?.result ?? null;
}

// ---------------------------------------------------------------------------
// 单个任务的完整执行
// ---------------------------------------------------------------------------

async function handleLensTask(task) {
  const taskId = task.task_id;
  const maxResults = Number(task.max_results) || MAX_RESULTS_DEFAULT;
  let tabId = null;

  const finish = async (payload) => {
    await closeTab(tabId);
    await reportResult({
      task_id: taskId,
      image: task.image,
      ...payload,
    });
  };

  try {
    // ---- 1. 上传图片 ----
    if (await reportProgress(taskId, "uploading", "打开 lens.google.com 并自动上传图片")) {
      return finish({ status: "failed", result_count: 0, results: [], error: "任务已被用户停止" });
    }

    let lensUrl = null;

    // 首选：在真实 lens.google.com 页面上把文件塞进 <input type=file>，
    // 走页面自身的上传流程（Origin/Referer 天然正确）。
    // 实测扩展直接 POST /v3/upload 会被 Google 以 403 拒绝——扩展无法设置
    // Origin/Referer 这类禁止修改的请求头，所以只能作为兜底。
    const viaPage = await uploadViaLensPage(task);

    if (viaPage.ok) {
      lensUrl = viaPage.url;
      tabId = viaPage.tabId;
      await reportProgress(taskId, "uploaded", "页面上传成功", lensUrl);
    } else {
      warn("页面上传失败，回退到 v3/upload 端点", viaPage);
      await reportExtensionError(taskId, `页面上传失败：${JSON.stringify(viaPage)}`);
      tabId = viaPage.tabId || null;
      await reportProgress(taskId, "uploading", "回退：POST lens.google.com/v3/upload");

      const direct = await uploadViaLensEndpoint(task);
      if (!direct.ok) {
        await reportExtensionError(taskId, `v3/upload 失败：${JSON.stringify(direct)}`);
        await captureFailureArtifacts(tabId);
        return finish({
          status: "failed",
          result_count: 0,
          results: [],
          error: `图片上传失败：页面上传 ${viaPage.reason} / 端点 ${direct.reason}`,
        });
      }
      lensUrl = direct.url;
      await reportProgress(taskId, "uploaded", "上传端点返回结果页", lensUrl);
    }

    // 文字辅助（新增）：在视觉搜索结果页上追加文字查询，等价于 Lens 的「添加到搜索」
    if (task.query && lensUrl) {
      try {
        const withText = new URL(lensUrl);
        withText.searchParams.set("q", task.query);
        lensUrl = withText.href;
        await reportProgress(taskId, "querying", `追加文字查询「${task.query}」`, lensUrl);
      } catch (e) {
        warn("追加文字查询失败，按纯图片搜索继续", e);
      }
    }

    // ---- 2. 打开结果页 ----

    await reportProgress(taskId, "opening", "打开 Google Lens 结果页", lensUrl);

    if (tabId) {
      await chrome.tabs.update(tabId, { url: lensUrl }).catch(() => {});
    } else {
      const tab = await openTab(lensUrl);
      if (!tab || !tab.id) {
        return finish({
          status: "failed",
          result_count: 0,
          results: [],
          error: "无法打开结果页：浏览器没有可用窗口",
        });
      }
      tabId = tab.id;
    }
    await waitForTabComplete(tabId, 40000);

    // ---- 3. 等待结果渲染 ----
    await reportProgress(taskId, "waiting_results", "等待 Lens 渲染视觉匹配");

    const deadline = Date.now() + RESULT_WAIT_TIMEOUT_MS;
    let probe = null;
    while (Date.now() < deadline) {
      probe = await runInTab(tabId, pageProbe).catch(() => null);

      if (probe?.state === "consent") {
        await runInTab(tabId, acceptConsent).catch(() => {});
        await sleep(2000);
        await waitForTabComplete(tabId, 20000);
        continue;
      }
      if (probe?.state === "blocked") {
        await captureFailureArtifacts(tabId);
        return finish({
          status: "failed",
          result_count: 0,
          results: [],
          lens_url: probe.url || lensUrl,
          error: "Google 外部限制：命中 /sorry/ 验证码页（人机验证 / 频率限制）",
        });
      }
      if (probe?.state === "ready") break;

      if (await reportProgress(taskId, "waiting_results")) {
        return finish({ status: "failed", result_count: 0, results: [], error: "任务已被用户停止" });
      }
      await sleep(1500);
    }

    if (probe?.state !== "ready") {
      await captureFailureArtifacts(tabId);
      return finish({
        status: "failed",
        result_count: 0,
        results: [],
        lens_url: lensUrl,
        error: `结果页未渲染出任何站外结果（state=${probe?.state || "unknown"}）`,
      });
    }

    // ---- 4. 滚动 / 加载更多 ----
    await reportProgress(taskId, "scrolling", `已发现 ${probe.candidates} 个候选`);

    let lastCount = probe.candidates || 0;
    let stagnant = 0;
    for (let round = 0; round < MAX_SCROLL_ROUNDS; round += 1) {
      const info = await runInTab(tabId, scrollRound).catch(() => null);
      const count = info?.count ?? lastCount;

      if (count >= maxResults) break;
      if (count <= lastCount) {
        stagnant += 1;
        if (stagnant >= 3) break; // 连续三轮没有新结果 → 到底了
      } else {
        stagnant = 0;
      }
      lastCount = count;

      if (round % 3 === 0) {
        if (await reportProgress(taskId, "scrolling", `已加载 ${count} 个候选`)) {
          return finish({ status: "failed", result_count: 0, results: [], error: "任务已被用户停止" });
        }
      }
    }

    // ---- 5. 解析 ----
    await reportProgress(taskId, "parsing", `解析中（候选 ${lastCount}）`);

    const parsed = await runInTab(tabId, extractResults, [maxResults]);
    const raw = Array.isArray(parsed?.results) ? parsed.results : [];

    log(`卡片解析完成：${raw.length} 条（udm=${parsed?.udm || "?"}）`);
    log(`解析诊断：${JSON.stringify(parsed?.diag || {})}`);


    if (!raw.length) {
      await captureFailureArtifacts(tabId);
      return finish({
        status: "failed",
        result_count: 0,
        results: [],
        lens_url: parsed?.url || lensUrl,
        error: "结果页已加载但解析到 0 条结果（选择器可能已失效，已保存页面快照）",
      });
    }

    // Lens 卡片只给 /goto?url=<token>，这里逐条跟随重定向还原真实来源页面
    const needResolve = raw.filter((x) => !x.source_url && x.goto).length;
    await reportProgress(
      taskId,
      "parsing",
      `解析 ${raw.length} 条，还原来源 URL（${needResolve} 条需跳转还原）`
    );

    const results = await resolveSourceUrls(raw, async (done, total) => {
      if (done % 12 === 0) {
        await reportProgress(taskId, "parsing", `还原来源 URL ${done}/${total}`);
      }
    });

    log(`来源 URL 还原完成：${results.length}/${raw.length}`);

    if (!results.length) {
      await captureFailureArtifacts(tabId);
      return finish({
        status: "failed",
        result_count: 0,
        results: [],
        lens_url: parsed?.url || lensUrl,
        error: `解析到 ${raw.length} 条卡片，但没有任何一条能还原出真实来源 URL`,
      });
    }

    await reportProgress(taskId, "reporting", `回传 ${results.length} 条`);

    return finish({
      status: "completed",
      result_count: results.length,
      results,
      lens_url: parsed?.url || lensUrl,
    });
  } catch (error) {
    const message = error?.message ?? String(error);
    warn("任务执行异常", error);
    await reportExtensionError(
      taskId,
      `handleLensTask 异常：${message}\n${error?.stack || "(无堆栈)"}`
    );
    await captureFailureArtifacts(tabId);
    return finish({
      status: "failed",
      result_count: 0,
      results: [],
      error: `扩展执行异常：${message}`,
    });
  }
}

// ---------------------------------------------------------------------------
// 关键字搜索任务（新增，与图片搜索共用回传协议）
// ---------------------------------------------------------------------------

function buildKeywordQuery(task) {
  let q = String(task.query || "").trim();
  const only = (task.only_hosts || []).filter(Boolean);
  const excludes = (task.exclude_hosts || []).filter(Boolean);

  if (only.length) {
    q += " (" + only.map((h) => `site:${h}`).join(" OR ") + ")";
  }
  for (const host of excludes) {
    q += ` -site:${host}`;
  }
  return q.trim();
}

async function handleKeywordTask(task) {
  const taskId = task.task_id;
  const maxResults = Number(task.max_results) || MAX_RESULTS_DEFAULT;
  let tabId = null;

  const finish = async (payload) => {
    await closeTab(tabId);
    await reportResult({
      task_id: taskId,
      image: task.image || null,
      ...payload,
    });
  };

  try {
    const query = buildKeywordQuery(task);
    if (!query) {
      return finish({ status: "failed", result_count: 0, results: [], error: "关键词为空" });
    }

    const udm = String(task.udm || "");
    const kind = String(task.search_kind || "images");
    let searchUrl =
      "https://www.google.com/search?q=" + encodeURIComponent(query) + "&hl=zh-CN";
    if (udm) searchUrl += "&udm=" + encodeURIComponent(udm);

    if (await reportProgress(taskId, "querying", `提交查询「${query}」`, searchUrl)) {
      return finish({ status: "failed", result_count: 0, results: [], error: "任务已被用户停止" });
    }

    // 直接打开 search?q= 在全新 Profile 上容易被 Google 判为自动流量，
    // 所以先进首页（拿 consent / NID cookie），再用页面自己的搜索框提交。
    const tab = await openTab("https://www.google.com/");
    if (!tab || !tab.id) {
      return finish({
        status: "failed",
        result_count: 0,
        results: [],
        error: "无法打开搜索页：浏览器没有可用窗口",
      });
    }
    tabId = tab.id;

    await waitForTabComplete(tabId, 30000);
    await runInTab(tabId, acceptConsent).catch(() => null);

    const submitted = await runInTab(tabId, submitSearchOnPage, [query]).catch(() => null);
    if (submitted?.ok) {
      await waitForTabUrl(tabId, /[?&]q=/, 30000).catch(() => null);
      await waitForTabComplete(tabId, 30000);

      // 切到「图片 / 视频」标签；点不到就退回改 udm 参数
      if (kind !== "web") {
        const tabClick = await runInTab(tabId, clickSearchTab, [kind]).catch(() => null);
        if (!tabClick?.clicked && udm) {
          const current = await chrome.tabs.get(tabId).catch(() => null);
          if (current?.url) {
            try {
              const u = new URL(current.url);
              u.searchParams.set("udm", udm);
              await chrome.tabs.update(tabId, { url: u.href }).catch(() => {});
            } catch {}
          }
        }
        await waitForTabComplete(tabId, 30000);
      }
    } else {
      warn("首页搜索框不可用，回退到直接打开 search URL", submitted);
      await chrome.tabs.update(tabId, { url: searchUrl }).catch(() => {});
      await waitForTabComplete(tabId, 40000);
    }


    await reportProgress(taskId, "waiting_results", "等待 Google 渲染结果");

    const deadline = Date.now() + RESULT_WAIT_TIMEOUT_MS;
    let probe = null;
    while (Date.now() < deadline) {
      probe = await runInTab(tabId, keywordProbe).catch(() => null);

      if (probe?.state === "consent") {
        await runInTab(tabId, acceptConsent).catch(() => null);
        await sleep(1500);
        continue;
      }
      if (probe?.state === "blocked") {
        await captureFailureArtifacts(tabId);
        return finish({
          status: "failed",
          result_count: 0,
          results: [],
          lens_url: probe.url || searchUrl,
          error: "Google 触发了验证码/风控页面（/sorry），非本地问题",
        });
      }
      if (probe?.state === "ready") break;

      if (await reportProgress(taskId, "waiting_results", "结果尚未渲染，继续等待")) {
        return finish({ status: "failed", result_count: 0, results: [], error: "任务已被用户停止" });
      }
      await sleep(1500);
    }

    if (probe?.state !== "ready") {
      await captureFailureArtifacts(tabId);
      return finish({
        status: "failed",
        result_count: 0,
        results: [],
        lens_url: searchUrl,
        error: `结果页未渲染出任何站外结果（state=${probe?.state || "unknown"}）`,
      });
    }

    await reportProgress(taskId, "scrolling", `已发现 ${probe.candidates} 个候选`);

    let lastCount = probe.candidates || 0;
    let stagnant = 0;
    for (let round = 0; round < MAX_SCROLL_ROUNDS; round += 1) {
      const info = await runInTab(tabId, keywordScrollRound).catch(() => null);
      const count = info?.count ?? lastCount;

      if (count >= maxResults) break;
      if (count <= lastCount) {
        stagnant += 1;
        if (stagnant >= 3) break;
      } else {
        stagnant = 0;
      }
      lastCount = count;

      if (round % 3 === 0) {
        if (await reportProgress(taskId, "scrolling", `已加载 ${count} 个候选`)) {
          return finish({ status: "failed", result_count: 0, results: [], error: "任务已被用户停止" });
        }
      }
    }

    await reportProgress(taskId, "parsing", `解析中（候选 ${lastCount}）`);

    const parsed = await runInTab(tabId, extractKeywordResults, [maxResults]);
    const results = Array.isArray(parsed?.results) ? parsed.results : [];

    log(`关键字结果解析完成：${results.length} 条（udm=${parsed?.udm || "?"}）`);
    log(`解析诊断：${JSON.stringify(parsed?.diag || {})}`);

    if (!results.length) {
      await captureFailureArtifacts(tabId);
    }

    await reportProgress(taskId, "reporting", `回传 ${results.length} 条`);

    return finish({
      status: "completed",
      result_count: results.length,
      results: results.map((item) => ({
        title: item.title ?? null,
        thumbnail_url: item.thumbnail_url ?? null,
        original_image_url: item.original_image_url ?? null,
        source_url: item.source_url,
        source: item.source ?? null,
        match_type: "keyword_match",
      })),
      lens_url: parsed?.url || searchUrl,
    });
  } catch (error) {
    const message = error?.message ?? String(error);
    warn("关键字任务异常", error);
    await reportExtensionError(taskId, `handleKeywordTask 异常：${message}`);
    await captureFailureArtifacts(tabId);
    return finish({
      status: "failed",
      result_count: 0,
      results: [],
      error: `扩展执行异常：${message}`,
    });
  }
}

// 失败现场：页面 HTML + 截图 + DOM 采样，全部交给 Bridge 落盘，绝不删除

async function captureFailureArtifacts(tabId) {
  if (!tabId) return;

  try {
    const html = await runInTab(tabId, () => document.documentElement.outerHTML);
    if (html) await reportDebug("lens_page.html", html);
  } catch (e) {
    warn("保存 lens_page.html 失败", e);
  }

  try {
    const sample = await runInTab(tabId, () => {
      const out = [];
      out.push(`URL: ${location.href}`);
      out.push(`TITLE: ${document.title}`);
      out.push(`BODY TEXT (2000): ${(document.body?.innerText || "").slice(0, 2000)}`);
      out.push("--- 站外链接样本 ---");
      let n = 0;
      for (const a of document.querySelectorAll("a[href^='http']")) {
        let host = "";
        try {
          host = new URL(a.href).hostname;
        } catch {
          continue;
        }
        if (/google|gstatic/i.test(host)) continue;
        const img = a.querySelector("img") || a.parentElement?.querySelector?.("img");
        out.push(
          `[${n}] href=${a.href}\n     cls=${a.className}\n     img=${img ? img.src.slice(0, 120) : "none"}\n     text=${(a.innerText || "").replace(/\n/g, " | ").slice(0, 160)}`
        );
        n += 1;
        if (n >= 25) break;
      }
      out.push(`--- 站外链接总数: ${n} ---`);
      return out.join("\n");
    });
    if (sample) await reportDebug("lens_dom_sample.txt", sample);
  } catch (e) {
    warn("保存 DOM 采样失败", e);
  }

  try {
    const tab = await chrome.tabs.get(tabId).catch(() => null);
    if (tab) {
      const dataUrl = await chrome.tabs
        .captureVisibleTab(tab.windowId, { format: "png" })
        .catch(() => null);
      if (dataUrl && dataUrl.includes(",")) {
        await reportDebug("lens_page.png", dataUrl.split(",")[1], { base64: true });
      }
    }
  } catch (e) {
    warn("截图失败", e);
  }
}

// ---------------------------------------------------------------------------
// 轮询循环
// ---------------------------------------------------------------------------

async function pollOnce() {
  if (busy) return POLL_IDLE_MS;

  const config = await loadBridgeConfig();
  if (!config.token) {
    // 未配对：桌面端打开配对窗口时自动领取 token，无需用户手动粘贴
    notePollState("未配对，尝试自动配对");
    await autoPair().catch(() => null);
    return POLL_IDLE_MS;
  }

  const response = await bridgeRequest(
    "/v1/lens/next?types=google_lens,google_keyword"
  );

  if (!response.ok) {
    notePollState(`Bridge 不可达/出错：${response.reason || ""} ${response.status || ""}`);
    return POLL_BACKOFF_MS;
  }

  const task = response.body?.task;
  if (!task) {
    notePollState("已连接 Bridge，暂无任务");
    return POLL_IDLE_MS;
  }
  if (task.type !== "google_lens" && task.type !== "google_keyword") {
    notePollState(`收到未知任务类型：${task.type}`);
    return POLL_IDLE_MS;
  }

  busy = true;
  notePollState("执行任务中");
  log("领取任务", task.task_id, task.type, task.image || task.query || "");
  try {
    if (task.type === "google_keyword") {
      await handleKeywordTask(task);
    } else {
      await handleLensTask(task);
    }
  } finally {
    busy = false;
    lastPollState = "";
  }
  return POLL_IDLE_MS;
}


async function pollLoop() {
  if (polling) return;
  polling = true;
  try {
    for (;;) {
      let delay = POLL_IDLE_MS;
      try {
        delay = await pollOnce();
      } catch (error) {
        warn("轮询异常", error);
        delay = POLL_BACKOFF_MS;
      }
      await sleep(delay);
    }
  } finally {
    polling = false;
  }
}

/** 由 background.js 调用：启动 Lens 任务轮询 */
export function startLensPolling() {
  log("Lens 任务轮询已启动（间隔 " + POLL_IDLE_MS + "ms）");
  // alarm 只作为 service worker 被回收后的唤醒兜底（最小周期 1 分钟）；
  // 真正的秒级轮询靠下面的 fetch 链，fetch 本身会不断刷新 SW 存活计时。
  try {
    chrome.alarms.create(LENS_POLL_ALARM, { periodInMinutes: 1 });
  } catch (e) {
    warn("创建 Lens 轮询闹钟失败", e);
  }
  pollLoop();
}

/** alarm 唤醒后重新拉起轮询循环 */
export function resumeLensPolling() {
  pollLoop();
}
