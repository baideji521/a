import { extractCookiesForPlatform } from "./cookies.js";
import { detectSupportedMediaUrl } from "./detect.js";
import { createActionFeedbackController } from "./action-feedback.js";
import { registerSnifferListeners, getMediaCount, getMediaCountForPage, getDetectedMedia, getDetectedMediaForUrl, getPageKeyForTab, restoreMedia } from "./media-sniffer.js";
import { summarizeCookies } from "./cookie-summary.js";
import { loadSnifferState, isSnifferEnabled, setSnifferEnabled } from "./sniffer-toggle.js";
import { registerContextMenu, getContextMenuId, getTikTokSearchMenuId, getGoogleSearchMenuId, getGoogleImageSearchMenuId, getPairMenuId, getCopyUrlMenuId, getCopyTitleMenuId } from "./context-menu.js";
import { openVideoHelperScheme } from "./send-via-scheme.js";
import { loadOpenAppState } from "./open-app-toggle.js";
import {
  sendViaBridge,
  sendCookiesViaBridge,
  autoPair,
  loadBridgeConfig,
  AUTOPAIR_ALARM_NAME,
  STORAGE_KEY_TOKEN,
} from "./bridge-client.js";

const AUTOPAIR_ALARM = AUTOPAIR_ALARM_NAME;

async function isPaired() {
  try {
    const cfg = await loadBridgeConfig();
    return Boolean(cfg.token);
  } catch {
    return false;
  }
}

// Keep a low-frequency poll alive while unpaired so that, the moment the user
// clicks "Pair extension" in the desktop app (which opens a ~120s single-use
// window), the extension grabs the token on its own — no copy-paste, no
// returning to the extension. The alarm clears itself once paired.
async function runAutoPairTick() {
  if (await isPaired()) {
    try {
      await chrome.alarms.clear(AUTOPAIR_ALARM);
    } catch {}
    return true;
  }
  const result = await autoPair().catch(() => ({ ok: false }));
  if (result?.ok) {
    try {
      await chrome.alarms.clear(AUTOPAIR_ALARM);
    } catch {}
    refreshActiveTab().catch(() => {});
    // 配对成功后立即发送所有 Cookie
    console.info("[视频好帮手] 配对成功，立即发送所有 Cookie");
    void scanAllPlatformsForCookies();
    return true;
  }
  return false;
}

async function ensureAutoPairAlarm() {
  if (await isPaired()) return;
  try {
    if (!(await chrome.alarms.get(AUTOPAIR_ALARM))) {
      chrome.alarms.create(AUTOPAIR_ALARM, { periodInMinutes: 1 });
    }
  } catch {}
}

if (chrome.alarms?.onAlarm) {
  chrome.alarms.onAlarm.addListener((alarm) => {
    if (alarm?.name === AUTOPAIR_ALARM) void runAutoPairTick();
  });
}

// If the stored token is ever cleared (401 recovery in bridge-client.js, or
// the user wiping it from the options page), go back to polling for a fresh
// pairing window so the browser can re-pair without a reinstall.
if (chrome.storage?.onChanged) {
  chrome.storage.onChanged.addListener((changes, areaName) => {
    if (areaName !== "local") return;
    const change = changes?.[STORAGE_KEY_TOKEN];
    if (!change) return;
    const newToken = typeof change.newValue === "string" ? change.newValue.trim() : "";
    if (!newToken) void ensureAutoPairAlarm();
  });
}

const INSTALL_URL = "https://github.com/tonhowtf/视频好帮手/releases/latest";
const PROTOCOL_VERSION = 1;

function getIconPath(iconSet) {
  return {
    16: chrome.runtime.getURL(iconSet[16]),
    24: chrome.runtime.getURL(iconSet[24]),
    32: chrome.runtime.getURL(iconSet[32]),
    48: chrome.runtime.getURL(iconSet[48]),
  };
}

const ACTIVE_ICON_PATHS = Object.freeze({
  16: "icons/active-16.png",
  24: "icons/active-24.png",
  32: "icons/active-32.png",
  48: "icons/active-48.png",
});

const INACTIVE_ICON_PATHS = Object.freeze({
  16: "icons/inactive-16.png",
  24: "icons/inactive-24.png",
  32: "icons/inactive-32.png",
  48: "icons/inactive-48.png",
});

const actionFeedback = createActionFeedbackController({
  setBadgeText: (details) => chrome.action.setBadgeText(details),
  setBadgeBackgroundColor: (details) => chrome.action.setBadgeBackgroundColor(details),
});

let snifferRegistered = false;

loadSnifferState().then(async (enabled) => {
  await restoreMedia();
  if (enabled) {
    registerSnifferListeners(onMediaDetected);
    snifferRegistered = true;
  }
});

chrome.runtime.onInstalled.addListener(async (details) => {
  registerContextMenu();
  refreshActiveTab().catch(() => {});
  // Surface the pairing page on any install/update *if the user hasn't
  // already paired this browser*. Reloading an unpacked extension fires
  // `update`, not `install`, so gating only on `install` would silently
  // skip the onboarding flow for dev builds and users coming from a
  // pre-bridge 视频好帮手 version.
  if (typeof chrome.runtime.openOptionsPage !== "function") return;
  try {
    const stored = await chrome.storage.local.get("bridge_token");
    const token = typeof stored?.bridge_token === "string" ? stored.bridge_token.trim() : "";
    if (!token) {
      const paired = await runAutoPairTick();
      if (!paired) {
        await ensureAutoPairAlarm();
        chrome.runtime.openOptionsPage().catch(() => {});
      }
    }
  } catch {
    // storage unavailable — fall back to the previous behaviour and only
    // open on a real install.
    if (details?.reason === "install") {
      chrome.runtime.openOptionsPage().catch(() => {});
    }
  }
});

chrome.contextMenus.onClicked.addListener(async (info, tab) => {
  // ---- TikTok搜索 ----
  if (info.menuItemId === getTikTokSearchMenuId()) {
    await handleTikTokSearch(tab);
    return;
  }

  // ---- Google 搜索 ----
  if (info.menuItemId === getGoogleSearchMenuId()) {
    await handleGoogleSearch(tab);
    return;
  }

  // ---- Google 图片搜索 ----
  if (info.menuItemId === getGoogleImageSearchMenuId()) {
    await handleGoogleImageSearch(tab);
    return;
  }

  // ---- 配对：打开配对/设置页面 ----
  if (info.menuItemId === getPairMenuId()) {
    try {
      await chrome.runtime.openOptionsPage();
    } catch (e) {
      // openOptionsPage 偶发失败时直接开页面地址
      console.log("[视频好帮手] openOptionsPage 失败，改为直接打开:", e?.message || e);
      chrome.tabs.create({ url: chrome.runtime.getURL("pages/options.html") });
    }
    return;
  }



  // ---- 复制当前视频地址 ----
  if (info.menuItemId === getCopyUrlMenuId()) {
    await handleCopyVideoUrl(info, tab);
    return;
  }

  // ---- 查看/复制视频标题 ----
  if (info.menuItemId === getCopyTitleMenuId()) {
    await handleCopyVideoTitle(tab);
    return;
  }

  // ---- 下载视频 ----
  if (info.menuItemId !== getContextMenuId()) return;

  const url = info.linkUrl || info.srcUrl;
  if (!url) return;

  const result = await handleSendToApp({
    type: "sendTo视频好帮手",
    url,
    platform: "generic",
    referer: tab?.url || "",
  });

  if (result.ok) {
    actionFeedback.showSuccessBadge(tab?.id);
  }
});

// ★ 绝招：background 直接 fetch 页面 HTML，提取 ytInitialData 中的视频标题
async function fetchTitleFromServer(url, videoId) {
  try {
    console.log("[视频好帮手] fetchTitleFromServer:", url, "videoId:", videoId);

    const resp = await fetch(url, {
      credentials: "include",
      headers: {
        "Accept-Language": "en-US,en;q=0.9",
      },
    });
    console.log("[视频好帮手] fetch status:", resp.status, "ok:", resp.ok);
    if (!resp.ok) return "";

    const html = await resp.text();
    console.log("[视频好帮手] HTML length:", html.length);

    // === YouTube: ytInitialData ===
    // 用括号计数法精确提取完整 JSON
    let ytJson = "";
    const ytStart = html.indexOf("var ytInitialData = ");
    if (ytStart >= 0) {
      const jsonStart = ytStart + "var ytInitialData = ".length;
      let depth = 0;
      let jsonEnd = jsonStart;
      for (let i = jsonStart; i < html.length && i < jsonStart + 5000000; i++) {
        if (html[i] === "{") depth++;
        else if (html[i] === "}") { depth--; if (depth === 0) { jsonEnd = i + 1; break; } }
      }
      if (depth === 0 && jsonEnd > jsonStart) {
        ytJson = html.substring(jsonStart, jsonEnd);
        console.log("[视频好帮手] ytInitialData JSON length:", ytJson.length);
      }
    }

    if (ytJson) {
      try {
        const data = JSON.parse(ytJson);
        const text = JSON.stringify(data);
        console.log("[视频好帮手] ytInitialData text length:", text.length);
        const results = [];
        const re = /"videoId"\s*:\s*"([\w-]{11})"/g;
        let m;
        while ((m = re.exec(text)) !== null) {
          const vid = m[1];
          const ctx = text.substring(Math.max(0, m.index - 3000), Math.min(text.length, m.index + 3000));
          const accMatch = ctx.match(/"accessibilityText"\s*:\s*"([^"]{5,500})"/);
          if (accMatch) {
            let t = accMatch[1].replace(/\\u0026/g, "&").replace(/\\u003c/g, "<");
            t = t.replace(/,\s*[\d,.]+\s*(?:thousand|million|billion|K|M|B)?\s*views?.*/i, "").trim();
            results.push({ videoId: vid, title: t });
            continue;
          }
          const runMatch = ctx.match(/"title"\s*:\s*\{[^}]*"runs"\s*:\s*\[\s*\{[^}]*"text"\s*:\s*"([^"]+)"/);
          if (runMatch) {
            results.push({ videoId: vid, title: runMatch[1].replace(/\\u0026/g, "&") });
            continue;
          }
          const stMatch = ctx.match(/"title"\s*:\s*\{[^}]*"simpleText"\s*:\s*"([^"]+)"/);
          if (stMatch) {
            results.push({ videoId: vid, title: stMatch[1].replace(/\\u0026/g, "&") });
          }
        }
        console.log("[视频好帮手] YouTube titles found:", results.length);
        if (results.length > 0) {
          if (videoId) {
            const exact = results.find(r => r.videoId === videoId);
            if (exact) { console.log("[视频好帮手] exact match:", exact.title); return exact.title; }
          }
          console.log("[视频好帮手] first title:", results[0].title);
          return results[0].title;
        }
      } catch (e) {
        console.warn("[视频好帮手] ytInitialData parse error:", e.message);
      }
    } else {
      console.log("[视频好帮手] ytInitialData NOT found in HTML");
    }

    // === TikTok ===
    const ttMatch = html.match(/<script\s+id="__UNIVERSAL_DATA_FOR_REHYDRATION__"[^>]*>(.*?)<\/script>/s);
    if (ttMatch) {
      try {
        const data = JSON.parse(ttMatch[1]);
        const text = JSON.stringify(data);
        const re = /"desc"\s*:\s*"([^"]{3,500})"/g;
        let m;
        while ((m = re.exec(text)) !== null) {
          const t = m[1].replace(/\\u0026/g, "&").replace(/\\n/g, " ");
          console.log("[视频好帮手] TikTok title:", t);
          return t;
        }
      } catch {}
    }
    const sigiMatch = html.match(/<script\s+id="SIGI_STATE"[^>]*>(.*?)<\/script>/s);
    if (sigiMatch) {
      try {
        const data = JSON.parse(sigiMatch[1]);
        const text = JSON.stringify(data);
        const re = /"desc"\s*:\s*"([^"]{3,500})"/g;
        let m;
        while ((m = re.exec(text)) !== null) {
          return m[1].replace(/\\u0026/g, "&").replace(/\\n/g, " ");
        }
      } catch {}
    }
    console.log("[视频好帮手] No title found in server HTML");
    return "";
  } catch (e) {
    console.error("[视频好帮手] fetchTitleFromServer ERROR:", e?.message || e);
    return "";
  }
}

// 检查扩展是否对该 URL 拥有主机权限。
// 右键菜单是 contexts:["all"]，会出现在任何站点上；对不在 host_permissions
// 里的站点执行 executeScript / fetch 必然失败（Cannot access contents / CORS），
// 所以调用前先预检，无权限时静默走 tab.title 兜底。
async function hasHostAccess(url) {
  try {
    if (!url) return false;
    const parsed = new URL(url);
    if (parsed.protocol !== "http:" && parsed.protocol !== "https:") return false;
    if (!chrome.permissions?.contains) return true;
    return await chrome.permissions.contains({ origins: [`${parsed.origin}/*`] });
  } catch {
    return false;
  }
}

// 从页面上下文读取（world: MAIN）— 直接读取 window.ytInitialData
async function extractTitleFromPageContext(tabId, videoId) {
  try {
    console.log("[视频好帮手] extractTitleFromPageContext tabId:", tabId, "videoId:", videoId || "(空)");
    const results = await chrome.scripting.executeScript({
      target: { tabId },
      world: "MAIN",
      func: (targetVid) => {
        console.log("[视频好帮手] MAIN script 执行, ytInitialData:", typeof window.ytInitialData);
        if (window.ytInitialData) {
          try {
            const text = JSON.stringify(window.ytInitialData);
            console.log("[视频好帮手] ytInitialData text length:", text.length);
            const results = [];
            const re = /"videoId"\s*:\s*"([\w-]{11})"/g;
            let m;
            while ((m = re.exec(text)) !== null) {
              const vid = m[1];
              const ctx = text.substring(Math.max(0, m.index - 2000), Math.min(text.length, m.index + 2000));
              const a = ctx.match(/"accessibilityText"\s*:\s*"([^"]{5,500})"/);
              if (a) { let t = a[1].replace(/\\u0026/g,"&"); t = t.replace(/,\s*[\d,.]+\s*(?:thousand|million|billion|K|M|B)?\s*views?.*/i,"").trim(); results.push({videoId:vid,title:t}); continue; }
              const r = ctx.match(/"title"\s*:\s*\{[^}]*"runs"\s*:\s*\[\s*\{[^}]*"text"\s*:\s*"([^"]+)"/);
              if (r) { results.push({videoId:vid,title:r[1].replace(/\\u0026/g,"&")}); continue; }
              const st = ctx.match(/"title"\s*:\s*\{[^}]*"simpleText"\s*:\s*"([^"]+)"/);
              if (st) { results.push({videoId:vid,title:st[1].replace(/\\u0026/g,"&")}); }
            }
            console.log("[视频好帮手] MAIN titles found:", results.length);
            if (results.length > 0) {
              if (targetVid) { const ex = results.find(r => r.videoId === targetVid); if (ex) return ex.title; }
              return results[0]?.title || "";
            }
          } catch (e) { console.log("[视频好帮手] MAIN parse error:", e.message); }
        }
        // TikTok
        try {
          const el = document.getElementById("__UNIVERSAL_DATA_FOR_REHYDRATION__");
          if (el) {
            const data = JSON.parse(el.textContent);
            const text = JSON.stringify(data);
            const re = /"desc"\s*:\s*"([^"]{3,500})"/g;
            let m;
            while ((m = re.exec(text)) !== null) {
              return m[1].replace(/\\u0026/g, "&").replace(/\\n/g, " ");
            }
          }
        } catch {}
        return "";
      },
      args: [videoId || null],
    });
    const result = results?.[0]?.result || "";
    console.log("[视频好帮手] extractTitleFromPageContext result:", (result || "(空)").substring(0, 80));
    return result;
  } catch (e) {
    console.error("[视频好帮手] extractTitleFromPageContext ERROR:", e?.message || e);
    return "";
  }
}

// ============================================================
// 标题提取 + 搜索关键词（TikTok搜索 与 Google 搜索共用）
//
// 这两个函数是从原 handleTikTokSearch 里原样拆出来的，逻辑一字未改，
// 目的只是让 Google 搜索复用同一份标题来源和同一套关键词规则。
// ============================================================

// 标题提取链：1 内容脚本 → 2 world:MAIN 页面上下文 → 3 tab.title 兜底
async function extractSearchTitle(tab) {
  if (!tab?.id) return "";

  let title = "";
  try {
    const response = await chrome.tabs.sendMessage(tab.id, { type: "getVideoTitle" });
    title = response?.title || "";
  } catch {}

  if (!title) {
    if (await hasHostAccess(tab.url)) {
      title = await extractTitleFromPageContext(tab.id, "");
    }
  }

  if (!title) {
    title = tab.title || "";
  }

  return title;
}

// 关键词规则：提取第一个 # 后的内容，将 # 替换为空格
// 没有 # 或结果为空时返回 ""，由调用方决定怎么处理
function buildSearchQuery(title) {
  const hashIndex = title.indexOf("#");
  if (hashIndex === -1) return "";
  return title.substring(hashIndex + 1).replace(/#/g, " ").trim();
}

// 处理"TikTok搜索"右键菜单点击
async function handleTikTokSearch(tab) {
  if (!tab?.id) return;

  const title = await extractSearchTitle(tab);
  const query = buildSearchQuery(title);
  if (!query) return; // 没有 # 或关键词为空则不触发任何动作

  const searchUrl = `https://www.tiktok.com/search/video?q=${encodeURIComponent(query)}`;
  chrome.tabs.create({ url: searchUrl });
}

// ============================================================
// Google 搜索（文字）
// ============================================================

// 不加任何 site: 白名单，也不加任何排除项，
// 直接用标题原文搜，结果范围完全交给 Google。
function buildGoogleSearchUrl(query) {
  return `https://www.google.com/search?q=${encodeURIComponent(query)}`;
}

// 处理"🔎 Google 搜索"右键菜单点击。
// 搜索词完全来自 TikTok搜索 那一套：extractSearchTitle → buildSearchQuery。
// 打开结果页后立即结束，不解析、不抓取、不调用 bridge、不建任务。
async function handleGoogleSearch(tab) {
  if (!tab?.id) return;

  const title = await extractSearchTitle(tab);
  const query = buildSearchQuery(title);

  if (!query) {
    await showSimpleToast(tab, "未获取到有效标题。");
    return;
  }

  chrome.tabs.create({ url: buildGoogleSearchUrl(query) });
}

// ============================================================
// Google 图片搜索（当前剪贴板里的完整原图）
// ============================================================

const LENS_PAGE_URL = "https://lens.google.com/?hl=zh-CN";
const LENS_UPLOAD_TIMEOUT_MS = 45000;

// Lens 上传端点。参数照 Chrome 自己「用 Google 搜索图片」时的取值。
function buildLensUploadUrl() {
  const params = new URLSearchParams({
    ep: "ccm",
    re: "dcsp",
    s: "4",
    st: String(Date.now()),
    sideimagesearch: "1",
    vpw: "1280",
    vph: "900",
  });
  return `https://lens.google.com/v3/upload?${params.toString()}`;
}

function base64ToBytes(base64) {
  const binary = atob(base64);
  const bytes = new Uint8Array(binary.length);
  for (let i = 0; i < binary.length; i += 1) bytes[i] = binary.charCodeAt(i);
  return bytes;
}

// Lens 返回的结果页 URL 里带区域参数，会让页面上出现一个自动框选。
// 我们要的是整张图，所以把区域相关参数去掉再打开。
// vsint = 区域信息（中心点/宽高比例），vsdim = 显示尺寸。
// vsrid 是本次上传的会话 id，必须保留。
const LENS_REGION_PARAMS = ["vsint", "vsdim"];

function stripLensRegion(rawUrl) {
  try {
    const u = new URL(rawUrl);
    for (const key of LENS_REGION_PARAMS) u.searchParams.delete(key);
    return u.toString();
  } catch {
    return rawUrl;
  }
}

// 主路径：直接 POST 原图到 Lens 上传端点，跟随 303 拿到结果页 URL，
// 然后新建标签页打开它。不需要模拟页面上传，也就不受 Lens 前端改版影响。
async function uploadToLensAndGetUrl(base64, mime) {
  const bytes = base64ToBytes(base64);
  const form = new FormData();
  form.append("encoded_image", new Blob([bytes], { type: mime || "image/png" }), "clipboard.png");
  form.append("original_width", "0");
  form.append("original_height", "0");

  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), LENS_UPLOAD_TIMEOUT_MS);

  try {
    const response = await fetch(buildLensUploadUrl(), {
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
    if (!finalUrl || !/[?&]vsrid=/.test(finalUrl)) {
      return { ok: false, reason: "no-vsrid", status: response.status };
    }
    return { ok: true, url: stripLensRegion(finalUrl) };
  } catch (e) {
    return { ok: false, reason: "upload-failed", message: e?.message || String(e) };
  } finally {
    clearTimeout(timer);
  }
}

// 在当前标签页里读剪贴板。必须在有焦点的文档里执行，
// service worker 自己没有 document，读不到。
// 全程走 clipboard.read() → Blob → ArrayBuffer，不经过 canvas，
// 所以不存在缩放/裁剪/重编码，拿到的就是剪贴板里的原始字节。
async function readClipboardImage(tabId) {
  try {
    const [injected] = await chrome.scripting.executeScript({
      target: { tabId },
      func: async () => {
        if (!navigator.clipboard?.read) return { ok: false, reason: "no-api" };

        let items;
        try {
          items = await navigator.clipboard.read();
        } catch (e) {
          return { ok: false, reason: "read-denied", message: e?.message || String(e) };
        }

        for (const item of items) {
          const mime = (item.types || []).find((t) => t.startsWith("image/"));
          if (!mime) continue;

          const blob = await item.getType(mime);
          const buffer = await blob.arrayBuffer();
          const bytes = new Uint8Array(buffer);

          // 分块转 base64，避免大图触发 apply 的参数上限
          let binary = "";
          const CHUNK = 0x8000;
          for (let i = 0; i < bytes.length; i += CHUNK) {
            binary += String.fromCharCode.apply(null, bytes.subarray(i, i + CHUNK));
          }

          return { ok: true, mime, size: bytes.length, base64: btoa(binary) };
        }

        return { ok: false, reason: "not-image" };
      },
    });

    return injected?.result || { ok: false, reason: "inject-failed" };
  } catch (e) {
    return { ok: false, reason: "inject-failed", message: e?.message || String(e) };
  }
}

// 等标签页加载完成
function waitForTabComplete(tabId, timeoutMs = 30000) {
  return new Promise((resolve) => {
    const timer = setTimeout(() => {
      chrome.tabs.onUpdated.removeListener(listener);
      resolve(false);
    }, timeoutMs);
    function listener(updatedTabId, changeInfo) {
      if (updatedTabId === tabId && changeInfo.status === "complete") {
        clearTimeout(timer);
        chrome.tabs.onUpdated.removeListener(listener);
        resolve(true);
      }
    }
    chrome.tabs.onUpdated.addListener(listener);
  });
}

// Lens 结果页会自己跑目标检测，在图上画一个小选框，只搜那一块。
// URL 参数控制不了它（vsint 里的区域本来就是整图，前端仍然自己重新框）。
// 所以打开结果页后模拟一次拖拽，把选框从左上角拉到右下角，覆盖整张图。
//
// 注意：这是唯一一处会去碰 Google 页面 DOM 的代码，只做"拉满选框"这一件事，
// 不读结果、不点其他按钮、不关标签页。Lens 前端改版后可能失效，
// 失效时只是选框保持原样，不影响页面正常使用。
async function expandLensSelection(tabId) {
  try {
    const [injected] = await chrome.scripting.executeScript({
      target: { tabId },
      func: async () => {
        const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

        // 上传的那张图是预览区里面积最大的 <img>
        function findImage() {
          let best = null;
          let bestArea = 0;
          for (const img of document.querySelectorAll("img")) {
            const r = img.getBoundingClientRect();
            if (r.width < 80 || r.height < 80) continue;
            const area = r.width * r.height;
            if (area > bestArea) {
              best = img;
              bestArea = area;
            }
          }
          return best;
        }

        let img = null;
        for (let i = 0; i < 80; i += 1) {
          img = findImage();
          if (img) break;
          await sleep(250);
        }
        if (!img) return { ok: false, reason: "no-image" };

        // 先等 Lens 把它自己的自动选框画完，否则我们拉完会被它覆盖回去
        await sleep(1500);

        const rect = img.getBoundingClientRect();
        if (rect.width < 80 || rect.height < 80) return { ok: false, reason: "image-too-small" };

        const inset = 3;
        const x1 = rect.left + inset;
        const y1 = rect.top + inset;
        const x2 = rect.right - inset;
        const y2 = rect.bottom - inset;

        // 拖拽要一直发给同一个元素（指针捕获语义），取起点处最上层的那个
        const target = document.elementFromPoint(x1 + 1, y1 + 1) || img;

        function fire(type, x, y) {
          const down = type !== "pointerup" && type !== "mouseup";
          const init = {
            bubbles: true,
            cancelable: true,
            composed: true,
            clientX: x,
            clientY: y,
            screenX: x,
            screenY: y,
            button: 0,
            buttons: down ? 1 : 0,
          };
          // 指针事件和鼠标事件都发一遍，兼容不同实现
          if (type.startsWith("pointer")) {
            let ev;
            try {
              ev = new PointerEvent(type, { ...init, pointerId: 1, pointerType: "mouse", isPrimary: true });
            } catch {
              ev = new MouseEvent(type.replace("pointer", "mouse"), init);
            }
            target.dispatchEvent(ev);
          } else {
            target.dispatchEvent(new MouseEvent(type, init));
          }
        }

        fire("pointerdown", x1, y1);
        fire("mousedown", x1, y1);

        const STEPS = 14;
        for (let i = 1; i <= STEPS; i += 1) {
          const x = x1 + ((x2 - x1) * i) / STEPS;
          const y = y1 + ((y2 - y1) * i) / STEPS;
          fire("pointermove", x, y);
          fire("mousemove", x, y);
          await sleep(25);
        }

        fire("pointerup", x2, y2);
        fire("mouseup", x2, y2);

        return {
          ok: true,
          w: Math.round(rect.width),
          h: Math.round(rect.height),
          tag: target.tagName,
        };
      },
    });

    return injected?.result || { ok: false, reason: "inject-failed" };
  } catch (e) {
    return { ok: false, reason: "inject-failed", message: e?.message || String(e) };
  }
}

// 兜底路径：打开 lens.google.com，把图片塞进页面自己的
// <input type=file name=encoded_image>，触发它原本的上传流程。
// 只在主路径 POST 失败时才走这里。
async function openLensPageAndInject(base64, mime) {
  const tab = await chrome.tabs.create({ url: LENS_PAGE_URL, active: true });
  if (!tab?.id) return { ok: false, reason: "no-tab" };

  const loaded = await waitForTabComplete(tab.id);
  if (!loaded) return { ok: false, reason: "load-timeout" };

  try {
    const ext = (mime || "image/png").split("/")[1] || "png";
    const [injected] = await chrome.scripting.executeScript({
      target: { tabId: tab.id },
      args: [base64, mime || "image/png", `clipboard.${ext}`],
      func: async (b64, type, name) => {
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
        const file = new File([bytes], name, { type });

        const dt = new DataTransfer();
        dt.items.add(file);
        input.files = dt.files;
        input.dispatchEvent(new Event("input", { bubbles: true }));
        input.dispatchEvent(new Event("change", { bubbles: true }));
        return { ok: true };
      },
    });

    if (!injected?.result?.ok) {
      return { ok: false, reason: injected?.result?.reason || "inject-failed" };
    }
    return { ok: true };
  } catch (e) {
    return { ok: false, reason: "inject-failed", message: e?.message || String(e) };
  }
}

// 处理"🔍 Google 图片搜索"右键菜单点击
async function handleGoogleImageSearch(tab) {
  if (!tab?.id) return;

  const clip = await readClipboardImage(tab.id);

  if (!clip.ok) {
    // 剪贴板里不是图片 / 读不到：只提示，不打开 Google
    const message =
      clip.reason === "not-image" || clip.reason === "no-api"
        ? "剪贴板中没有图片，请先复制一张图片。"
        : "读取剪贴板失败，请点击页面后重试。";
    console.log("[视频好帮手] Google 图片搜索中止:", clip.reason, clip.message || "");
    await showSimpleToast(tab, message);
    return;
  }

  console.log("[视频好帮手] 剪贴板图片:", clip.mime, clip.size, "字节");

  // 主路径：POST 原图 → 直接打开结果页
  const uploaded = await uploadToLensAndGetUrl(clip.base64, clip.mime);
  if (uploaded.ok) {
    const resultTab = await chrome.tabs.create({ url: uploaded.url, active: true });

    // 结果页打开后把 Lens 的自动选框拉满整图。
    // 失败也不影响页面本身，标签页始终保持打开。
    if (resultTab?.id) {
      const loaded = await waitForTabComplete(resultTab.id);
      if (loaded) {
        const expanded = await expandLensSelection(resultTab.id);
        console.log(
          "[视频好帮手] 选框拉满:",
          expanded.ok ? `成功 ${expanded.w}x${expanded.h} (${expanded.tag})` : `失败 ${expanded.reason}`
        );
      } else {
        console.log("[视频好帮手] 结果页加载超时，跳过拉选框");
      }
    }
    return;
  }

  console.log("[视频好帮手] Lens 上传失败，改走页面注入:", uploaded.reason, uploaded.message || "");

  // 兜底：打开 Lens 页面塞文件
  const injected = await openLensPageAndInject(clip.base64, clip.mime);
  if (!injected.ok) {
    console.log("[视频好帮手] Lens 页面注入也失败:", injected.reason, injected.message || "");
    // 把两条失败原因直接显示出来，省得去翻 Service Worker 控制台
    const detail = [
      `上传：${uploaded.reason}${uploaded.status ? "/" + uploaded.status : ""}`,
      `注入：${injected.reason}`,
    ].join("，");
    await showSimpleToast(tab, `Google 图片搜索失败（${detail}）`);
  }
}


// 轻量提示条。只注入一个自动消失的浮层，不影响 showTitleDialog。
async function showSimpleToast(tab, message) {
  if (!tab?.id) return;
  try {
    await chrome.scripting.executeScript({
      target: { tabId: tab.id },
      args: [message],
      func: (text) => {
        const old = document.getElementById("__og-toast");
        if (old) old.remove();

        const box = document.createElement("div");
        box.id = "__og-toast";
        box.textContent = text;
        box.style.cssText = [
          "position:fixed", "top:24px", "left:50%", "transform:translateX(-50%)",
          "z-index:2147483647", "background:#2f2b26", "color:#f6f4f0",
          "padding:12px 20px", "border-radius:10px", "font-size:14px",
          "font-family:system-ui,-apple-system,'Microsoft YaHei',sans-serif",
          "box-shadow:0 8px 28px rgba(0,0,0,.35)", "max-width:80vw",
          "line-height:1.5", "pointer-events:none",
        ].join(";");
        document.documentElement.appendChild(box);
        setTimeout(() => box.remove(), 3200);
      },
    });
  } catch (e) {
    // 无法注入（受限页面等）时退化成系统通知
    try {
      chrome.notifications.create({
        type: "basic",
        iconUrl: chrome.runtime.getURL("icons/active-48.png"),
        title: "视频好帮手",
        message,
      });
    } catch {}
  }
}


// 处理"复制当前视频地址"右键菜单点击
async function handleCopyVideoUrl(info, tab) {
  const videoUrl = info.linkUrl || info.srcUrl || info.pageUrl;
  if (!videoUrl) return;

  try {
    await navigator.clipboard.writeText(videoUrl);
  } catch {
    // 回退：通过 content script 复制
    if (tab?.id) {
      await chrome.scripting.executeScript({
        target: { tabId: tab.id },
        func: (text) => { navigator.clipboard.writeText(text); },
        args: [videoUrl],
      });
    }
  }
}

// 处理"查看视频标题"右键菜单点击
async function handleCopyVideoTitle(tab) {
  if (!tab?.id) return;
  let title = "";
  let videoId = "";

  // 该站点是否在 host_permissions 内 —— 决定方案2/3/注入是否可用
  const allowed = await hasHostAccess(tab.url);
  if (!allowed) {
    console.debug("[视频好帮手] 无该站点主机权限，跳过页面注入与 fetch:", tab.url);
  }

  // 方案1: 向内容脚本获取 videoId（右键时通过 composedPath 捕获）
  try {
    const response = await chrome.tabs.sendMessage(tab.id, { type: "getVideoTitle" });
    title = response?.title || "";
    videoId = response?.videoId || "";
    console.log("[视频好帮手] 方案1 内容脚本返回 title:", (title || "(空)").substring(0, 50), "videoId:", videoId || "(空)");
  } catch (e) {
    console.log("[视频好帮手] 方案1 内容脚本通信失败:", e?.message || e);
  }

  // ★ 方案2（核心）: chrome.scripting.executeScript + world:MAIN
  // 直接读取 window.ytInitialData / TikTok 数据，完全绕过 CSP
  if (!title && allowed) {
    title = await extractTitleFromPageContext(tab.id, videoId);
    console.log("[视频好帮手] 方案2 world:MAIN 返回:", (title || "(空)").substring(0, 80));
  }

  // 方案3: background fetch 页面 HTML
  if (!title && allowed && tab.url) {
    title = await fetchTitleFromServer(tab.url, videoId);
    console.log("[视频好帮手] 方案3 fetch 返回:", (title || "(空)").substring(0, 80));
  }

  // 方案4: 最终兜底用 tab.title
  if (!title) {
    title = tab.title || "未找到视频标题";
    console.log("[视频好帮手] 方案4 兜底 tab.title:", title.substring(0, 80));
  }

  showTitleDialog(tab, title);
}

// 显示标题对话框（带复制功能）— 注入页面内自定义对话框
function showTitleDialog(tab, title) {
  chrome.scripting.executeScript({
    target: { tabId: tab.id },
    func: (t) => {
      // 清除已有对话框
      const old = document.getElementById("__og-title-dlg");
      if (old) old.remove();

      const overlay = document.createElement("div");
      overlay.id = "__og-title-dlg";
      overlay.innerHTML = `
        <style>
          #__og-title-dlg{position:fixed;top:0;left:0;width:100%;height:100%;
            background:rgba(0,0,0,.55);z-index:2147483647;display:flex;
            align-items:center;justify-content:center;font-family:system-ui,-apple-system,sans-serif}
          #__og-title-dlg .og-dlg{background:#fff;border-radius:14px;padding:24px 28px;
            max-width:520px;min-width:320px;width:90%;box-shadow:0 12px 40px rgba(0,0,0,.35);
            position:relative;animation:og-dlg-in .2s ease-out}
          @keyframes og-dlg-in{from{opacity:0;transform:scale(.95)}to{opacity:1;transform:scale(1)}}
          #__og-title-dlg .og-dlg-hd{font-size:15px;font-weight:600;color:#333;
            margin-bottom:14px;display:flex;align-items:center;gap:8px}
          #__og-title-dlg .og-dlg-hd::before{content:"🎬";font-size:18px}
          #__og-title-dlg .og-dlg-tt{background:#f5f5f5;border:1px solid #e0e0e0;border-radius:8px;
            padding:12px 14px;font-size:14px;line-height:1.6;color:#222;width:100%;
            box-sizing:border-box;resize:vertical;min-height:48px;max-height:200px;
            font-family:inherit;outline:none;transition:border-color .15s}
          #__og-title-dlg .og-dlg-tt:focus{border-color:#4285f4;background:#fafcff}
          #__og-title-dlg .og-dlg-bt{display:flex;gap:10px;margin-top:16px;justify-content:flex-end}
          #__og-title-dlg .og-cp{background:#4285f4;color:#fff;border:none;border-radius:8px;
            padding:9px 22px;font-size:14px;cursor:pointer;font-weight:500;
            transition:background .15s}
          #__og-title-dlg .og-cp:hover{background:#3367d6}
          #__og-title-dlg .og-cl{background:#f1f3f4;color:#555;border:none;border-radius:8px;
            padding:9px 22px;font-size:14px;cursor:pointer;transition:background .15s}
          #__og-title-dlg .og-cl:hover{background:#e2e3e5}
        </style>
        <div class="og-dlg">
          <div class="og-dlg-hd">视频标题 - 视频好帮手</div>
          <textarea class="og-dlg-tt" readonly></textarea>
          <div class="og-dlg-bt">
            <button class="og-cl" id="__og-dlg-close">关闭</button>
            <button class="og-cp" id="__og-dlg-copy">复制标题</button>
          </div>
        </div>
      `;

      // 设置标题文本（安全方式，避免 HTML 注入）
      const textarea = overlay.querySelector(".og-dlg-tt");
      textarea.value = t;

      // 关闭逻辑
      const close = () => overlay.remove();
      overlay.querySelector("#__og-dlg-close").addEventListener("click", close);
      overlay.addEventListener("click", (e) => { if (e.target === overlay) close(); });
      document.addEventListener("keydown", function esc(e) {
        if (e.key === "Escape") { close(); document.removeEventListener("keydown", esc); }
      });

      // 复制逻辑
      overlay.querySelector("#__og-dlg-copy").addEventListener("click", async () => {
        const btn = overlay.querySelector("#__og-dlg-copy");
        textarea.readOnly = false;
        textarea.focus();
        textarea.select();
        try {
          await navigator.clipboard.writeText(t);
          btn.textContent = "已复制 ✓";
          btn.style.background = "#34a853";
        } catch {
          try {
            document.execCommand("copy");
            btn.textContent = "已复制 ✓";
            btn.style.background = "#34a853";
          } catch {
            btn.textContent = "请手动 Ctrl+C";
          }
        }
        textarea.readOnly = true;
        setTimeout(() => {
          btn.textContent = "复制标题";
          btn.style.background = "#4285f4";
        }, 2000);
      });

      document.body.appendChild(overlay);
      textarea.focus();
      textarea.select();
    },
    args: [title],
  }).catch((e) => {
    // 无主机权限 / 特殊页面（chrome://、扩展页）无法注入属预期情况，转用通知展示
    console.debug("[视频好帮手] showTitleDialog 注入失败，改用通知:", e?.message || e);
    const notifId = `title-${Date.now()}`;
    chrome.storage?.local?.set({ ["__og_title_clipboard"]: title }).catch(() => {});
    chrome.notifications?.create(notifId, {
      type: "basic",
      iconUrl: chrome.runtime.getURL("icons/active-128.png"),
      title: "视频标题 - 视频好帮手",
      message: title,
      buttons: [{ title: "复制标题" }],
      requireInteraction: false,
    }).catch(() => {});
  });
}

// 处理通知按钮点击（复制标题）
if (chrome.notifications?.onButtonClicked) {
  chrome.notifications.onButtonClicked.addListener(async (notifId, btnIdx) => {
    if (!notifId?.startsWith("title-")) return;
    if (btnIdx === 0) {
      // 复制按钮
      try {
        const data = await chrome.storage?.local?.get("__og_title_clipboard");
        const title = data?.__og_title_clipboard || "";
        if (!title) return;
        // 优先用 service worker 的 clipboard API
        try {
          await navigator.clipboard.writeText(title);
        } catch {
          // 回退：通过 content script 复制
          const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
          if (tab?.id) {
            await chrome.scripting.executeScript({
              target: { tabId: tab.id },
              func: (text) => { navigator.clipboard.writeText(text); },
              args: [title],
            });
          }
        }
      } catch {}
    }
  });
}

chrome.runtime.onStartup.addListener(() => {
  refreshActiveTab().catch(() => {});
  void runAutoPairTick();
  void ensureAutoPairAlarm();
});

if (chrome.commands && chrome.commands.onCommand) {
  chrome.commands.onCommand.addListener(async (command) => {
    if (command !== "send-to-视频好帮手") return;
    try {
      const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
      if (!tab?.url) return;
      const detected = detectSupportedMediaUrl(tab.url);
      if (detected?.supported) {
        const result = await handleSendToApp({
          type: "sendTo视频好帮手",
          url: tab.url,
          platform: detected.platform,
          referer: tab.url,
        });
        if (result?.ok && tab.id !== undefined) {
          actionFeedback.showSuccessBadge(tab.id);
        }
        return;
      }
      if (chrome.action && typeof chrome.action.openPopup === "function") {
        try { await chrome.action.openPopup(); } catch {}
      }
    } catch (error) {
      console.error("[视频好帮手] command handler failed:", error);
    }
  });
}

chrome.tabs.onActivated.addListener(() => {
  refreshActiveTab().catch(() => {});
});

chrome.tabs.onUpdated.addListener((tabId, changeInfo, tab) => {
  if (!changeInfo.url && !changeInfo.status) {
    return;
  }
  if (!tab?.url) {
    return;
  }
  refreshTabAction(tabId, tab).catch((error) => {
    console.error("[视频好帮手] Failed to refresh tab action:", error);
  });
});

chrome.windows.onFocusChanged.addListener((windowId) => {
  if (windowId !== chrome.windows.WINDOW_ID_NONE) {
    refreshActiveTab().catch(() => {});
  }
});

chrome.runtime.onMessage.addListener((msg, sender, sendResponse) => {
  if (msg.type === "getDetectedMedia") {
    chrome.tabs.query({ active: true, currentWindow: true }, (tabs) => {
      const tabId = tabs[0]?.id;
      const pageUrl = tabs[0]?.url;
      if (!tabId) { sendResponse({ media: [], snifferEnabled: isSnifferEnabled() }); return; }

      const media = pageUrl
        ? getDetectedMediaForUrl(pageUrl)
        : getDetectedMedia(tabId);
      const list = Array.from(media.values()).sort((a, b) => b.detectedAt - a.detectedAt);

      const pageDetected = detectSupportedMediaUrl(pageUrl);

      sendResponse({
        media: list,
        pageDetected,
        snifferEnabled: isSnifferEnabled(),
        tabUrl: pageUrl,
      });
    });
    return true;
  }

  if (msg.type === "toggleSniffer") {
    setSnifferEnabled(msg.enabled).then((result) => {
      const effective = isSnifferEnabled();
      if (effective && !snifferRegistered) {
        registerSnifferListeners(onMediaDetected);
        snifferRegistered = true;
      }
      sendResponse({
        ok: result?.ok !== false,
        enabled: effective,
        reason: result?.reason,
      });
    });
    return true;
  }

  if (msg.type === "sendTo视频好帮手") {
    handleSendToApp(msg).then(sendResponse);
    return true;
  }

  // Content script 请求在 MAIN world 注入网络拦截代码
  if (msg.type === "injectNetworkInterceptor") {
    const tabId = sender.tab?.id;
    if (!tabId) { sendResponse({ ok: false }); return true; }
    chrome.scripting.executeScript({
      target: { tabId },
      world: "MAIN",
      func: () => {
        if (window.__ogNetIntercepted) return;
        window.__ogNetIntercepted = true;
        const origFetch = window.fetch;
        window.fetch = function() {
          const url = arguments[0]?.url || arguments[0] || '';
          return origFetch.apply(this, arguments).then(resp => {
            try {
              const clone = resp.clone();
              clone.text().then(text => {
                if (text.length > 500 && (text.includes('"videoId"') || text.includes('"playAddr"'))) {
                  window.dispatchEvent(new CustomEvent('__og-net', { detail: text.substring(0, 50000) }));
                }
              }).catch(()=>{});
            } catch(e){}
            return resp;
          });
        };
        const origOpen = XMLHttpRequest.prototype.open;
        const origSend = XMLHttpRequest.prototype.send;
        XMLHttpRequest.prototype.open = function() { this.__og_url = arguments[1] || ''; return origOpen.apply(this, arguments); };
        XMLHttpRequest.prototype.send = function() {
          this.addEventListener('load', function() {
            try {
              const t = this.responseText;
              if (t && t.length > 500 && (t.includes('"videoId"') || t.includes('"playAddr"'))) {
                window.dispatchEvent(new CustomEvent('__og-net', { detail: t.substring(0, 50000) }));
              }
            } catch(e){}
          });
          return origSend.apply(this, arguments);
        };
      },
    }).then(() => sendResponse({ ok: true })).catch((e) => sendResponse({ ok: false, error: String(e) }));
    return true;
  }

  // Content script 请求读取 ytInitialData 中的 videoId 列表
  if (msg.type === "scanYtInternalState") {
    const tabId = sender.tab?.id;
    if (!tabId) { sendResponse({ ids: [] }); return true; }
    chrome.scripting.executeScript({
      target: { tabId },
      world: "MAIN",
      func: () => {
        try {
          if (!window.ytInitialData) return [];
          const json = JSON.stringify(window.ytInitialData);
          const m = json.match(/"videoId":"([\w-]{11})"/g);
          if (!m) return [];
          return [...new Set(m.map(x => x.match(/"([\w-]{11})"/)[1]))];
        } catch { return []; }
      },
    }).then((results) => {
      const ids = results?.[0]?.result || [];
      sendResponse({ ids });
    }).catch(() => sendResponse({ ids: [] }));
    return true;
  }

  // Content script 请求从页面上下文读取视频标题
  if (msg.type === "readPageTitle") {
    const tabId = sender.tab?.id;
    const videoId = msg.videoId || "";
    const eventName = msg.eventName || "";
    if (!tabId || !eventName) { sendResponse({ ok: false }); return true; }
    chrome.scripting.executeScript({
      target: { tabId },
      world: "MAIN",
      func: (vid, evtName) => {
        try {
          var t = "";
          if (window.ytInitialData) {
            var s = JSON.stringify(window.ytInitialData);
            var re = /"videoId"\s*:\s*"([\w-]{11})"/g;
            var m, results = [];
            while ((m = re.exec(s)) !== null) {
              var v = m[1], c = s.substring(Math.max(0, m.index - 2000), Math.min(s.length, m.index + 2000));
              var a = c.match(/"accessibilityText"\s*:\s*"([^"]{5,500})"/);
              if (a) { var x = a[1].replace(/\\u0026/g, "&").replace(/\\u003c/g, "<"); x = x.replace(/,\s*[\d,.]+\s*(?:thousand|million|billion|K|M|B)?\s*views?.*/i, "").trim(); results.push({ v: v, t: x }); continue; }
              var r = c.match(/"title"\s*:\s*\{[^}]*"runs"\s*:\s*\[\s*\{[^}]*"text"\s*:\s*"([^"]+)"/);
              if (r) { results.push({ v: v, t: r[1].replace(/\\u0026/g, "&") }); continue; }
              var st = c.match(/"title"\s*:\s*\{[^}]*"simpleText"\s*:\s*"([^"]+)"/);
              if (st) { results.push({ v: v, t: st[1].replace(/\\u0026/g, "&") }); }
            }
            if (results.length > 0) {
              if (vid) { var ex = results.find(function(r) { return r.v === vid; }); if (ex) t = ex.t; }
              if (!t) t = results[0].t;
            }
          }
          if (!t) {
            var el = document.getElementById("__UNIVERSAL_DATA_FOR_REHYDRATION__");
            if (el) { var d = JSON.parse(el.textContent), s2 = JSON.stringify(d); var re2 = /"desc"\s*:\s*"([^"]{3,500})"/g, m2; while ((m2 = re2.exec(s2)) !== null) { t = m2[1].replace(/\\u0026/g, "&").replace(/\\n/g, " "); break; } }
          }
          window.dispatchEvent(new CustomEvent(evtName, { detail: t }));
        } catch (e) { window.dispatchEvent(new CustomEvent(evtName, { detail: "" })); }
      },
      args: [videoId, eventName],
    }).then(() => sendResponse({ ok: true })).catch(() => sendResponse({ ok: false }));
    return true;
  }
});

function onMediaDetected(tabId, _entry) {
  if (!isSnifferEnabled()) return;
  updateBadge(tabId);
  const pageKey = getPageKeyForTab(tabId);
  if (!pageKey) return;
  const count = getMediaCountForPage(pageKey);
  chrome.runtime.sendMessage({
    type: "media-detected",
    pageKey,
    count,
  }).catch(() => {});
}

function updateBadge(tabId) {
  const count = getMediaCount(tabId);
  chrome.action.setBadgeText({
    tabId,
    text: count > 0 ? String(count) : "",
  }).catch(() => {});
  chrome.action.setBadgeBackgroundColor({
    tabId,
    color: "#F04E23",
  }).catch(() => {});
}

async function handleSendToApp(msg) {
  const url = msg.url;
  const platform = msg.platform || "generic";

  let pageTitle = "";
  let pageThumbnail = "";
  try {
    const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
    pageTitle = tab?.title || "";
    pageThumbnail = tab?.favIconUrl || "";
  } catch {}

  let cookies = null;
  try {
    // 获取浏览器全部 Cookie（不分类，统一发送）
    const allCookies = await chrome.cookies.getAll({});
    if (allCookies && allCookies.length > 0) {
      cookies = allCookies.map(c => ({
        domain: c.domain,
        httpOnly: c.httpOnly,
        path: c.path,
        secure: c.secure,
        expires: c.expirationDate ? Math.floor(c.expirationDate) : 0,
        name: c.name,
        value: c.value,
        hostOnly: c.hostOnly,
        sameSite: c.sameSite,
      }));
    }
  } catch {}

  const message = { type: "enqueue", url, protocolVersion: PROTOCOL_VERSION };
  if (cookies) message.cookies = cookies;
  if (msg.referer) message.referer = msg.referer;
  if (msg.title) message.title = msg.title;
  else if (pageTitle) message.title = pageTitle;
  if (msg.thumbnail) message.thumbnail = msg.thumbnail;
  else if (pageThumbnail) message.thumbnail = pageThumbnail;
  if (msg.mediaType) message.mediaType = msg.mediaType;
  if (msg.contentType) message.contentType = msg.contentType;
  if (msg.headers) message.headers = msg.headers;
  if (typeof msg.openApp === "boolean") message.openApp = msg.openApp;
  else {
    // 右键菜单 / 快捷键路径不带 openApp，这里直接读开关状态，保证行为一致
    try {
      message.openApp = await loadOpenAppState();
    } catch {}
  }
  message.pageUrl = msg.referer || "";
  message.userAgent = navigator.userAgent;

  try {
    await chrome.storage.local.set({
      last_download_metadata: {
        url,
        referer: msg.referer || "",
        headers: msg.headers || {},
        cookies: cookies || [],
        userAgent: navigator.userAgent,
        timestamp: Date.now(),
      },
    }).catch(() => {});
  } catch {}

  const cookieSummary = summarizeCookies(cookies);

  // Primary path: localhost HTTP bridge (no extension-ID dependency, full
  // cookie + metadata payload).
  const bridgeResult = await sendViaBridge(message);
  if (bridgeResult?.ok) {
    return { ok: true, viaBridge: true, cookieSummary };
  }

  // Fallback: 视频好帮手:// scheme handler. The desktop app is launched (or
  // brought to focus) and the URL is queued, but cookies aren't forwarded
  // — the user can pair the bridge from the extension's options page to
  // get the full experience.
  const schemeResult = await openVideoHelperScheme(url);
  if (schemeResult?.ok) {
    return { ok: true, viaScheme: true, cookieSummary, bridgeReason: bridgeResult?.reason };
  }
  return {
    ok: false,
    error: bridgeResult?.message || schemeResult?.message || "视频好帮手 is not reachable",
    bridgeReason: bridgeResult?.reason,
    schemeReason: schemeResult?.reason,
  };
}

async function refreshActiveTab() {
  const [tab] = await chrome.tabs.query({
    active: true,
    lastFocusedWindow: true,
  });

  if (tab?.id !== undefined) {
    await refreshTabAction(tab.id, tab);
  }
}

async function refreshTabAction(tabId, tab) {
  if (!tab?.url) {
    return;
  }

  const detected = detectSupportedMediaUrl(tab.url);
  const supported = Boolean(detected?.supported);
  const mediaCount = getMediaCount(tabId);

  try {
    const iconSet = supported ? ACTIVE_ICON_PATHS : INACTIVE_ICON_PATHS;
    await chrome.action.setIcon({ tabId, path: getIconPath(iconSet) });
  } catch (error) {
    if (isTabGoneError(error)) return;
  }

  if (mediaCount > 0) {
    updateBadge(tabId);
  } else {
    try { await actionFeedback.clearBadge(tabId); } catch {}
  }
}

function isTabGoneError(error) {
  const msg = error instanceof Error ? error.message : String(error);
  return msg.includes("No tab with id");
}

const COOKIE_AUTO_CAPTURE_DEBOUNCE_MS = 1500;
const COOKIE_AUTO_CAPTURE_MIN_INTERVAL_MS = 60_000;
const cookieDebounceTimers = new Map();
const cookieLastSentAt = new Map();

const TRACKED_COOKIE_NAMES = new Set([
  "__Secure-3PAPISID",
  "__Secure-1PAPISID",
  "__Secure-3PSID",
  "__Secure-1PSID",
  "SAPISID",
  "SID",
  "HSID",
  "SSID",
  "APISID",
  "LOGIN_INFO",
  "VISITOR_INFO1_LIVE",
  "PREF",
  "sessionid",
  "ds_user_id",
  "ig_did",
  "auth_token",
  "ct0",
  "kp",
  "tt_webid",
  "twid",
  "loid",
  "edgebucket",
  "oauth_token",
  "sc_anonymous_id",
  "moe_uuid",
  "datadome",
]);

const TRACKED_DOMAIN_SUFFIXES = [
  ".youtube.com",
  ".google.com",
  ".instagram.com",
  ".tiktok.com",
  ".x.com",
  ".twitter.com",
  ".reddit.com",
  ".twitch.tv",
  ".vimeo.com",
  ".bilibili.com",
  ".pinterest.com",
  ".hotmart.com",
  ".udemy.com",
  ".bsky.app",
  ".bsky.social",
  ".telegram.org",
  ".soundcloud.com",
];

function platformForDomain(domain) {
  const d = (domain || "").toLowerCase();
  if (d.endsWith(".youtube.com") || d.endsWith(".google.com") || d === "youtube.com")
    return "youtube";
  if (d.endsWith(".instagram.com") || d.endsWith(".cdninstagram.com")) return "instagram";
  if (d.endsWith(".tiktok.com")) return "tiktok";
  if (d.endsWith(".x.com") || d.endsWith(".twitter.com")) return "twitter";
  if (d.endsWith(".reddit.com")) return "reddit";
  if (d.endsWith(".twitch.tv")) return "twitch";
  if (d.endsWith(".vimeo.com")) return "vimeo";
  if (d.endsWith(".bilibili.com")) return "bilibili";
  if (d.endsWith(".pinterest.com")) return "pinterest";
  if (d.endsWith(".hotmart.com")) return "hotmart";
  if (d.endsWith(".udemy.com")) return "udemy";
  if (d.endsWith(".bsky.app") || d.endsWith(".bsky.social")) return "bluesky";
  if (d.endsWith(".telegram.org")) return "telegram";
  if (d.endsWith(".soundcloud.com") || d === "soundcloud.com") return "soundcloud";
  return null;
}

function shouldTrackCookieDomain(domain) {
  if (!domain) return false;
  const d = domain.toLowerCase();
  for (const suffix of TRACKED_DOMAIN_SUFFIXES) {
    if (d === suffix.slice(1) || d.endsWith(suffix)) return true;
  }
  return false;
}

function debounceCookieCapture(platform) {
  if (cookieDebounceTimers.has(platform)) {
    clearTimeout(cookieDebounceTimers.get(platform));
  }
  const timer = setTimeout(() => {
    cookieDebounceTimers.delete(platform);
    void capturePlatformCookies(platform);
  }, COOKIE_AUTO_CAPTURE_DEBOUNCE_MS);
  cookieDebounceTimers.set(platform, timer);
}

async function capturePlatformCookies(platform, force = false) {
  const lastSent = cookieLastSentAt.get("__all__") || 0;
  if (!force && Date.now() - lastSent < COOKIE_AUTO_CAPTURE_MIN_INTERVAL_MS) {
    console.debug("[视频好帮手] cookie capture throttled");
    return { ok: false, reason: "throttled" };
  }
  cookieLastSentAt.set("__all__", Date.now());

  let allCookies = [];
  try {
    allCookies = await chrome.cookies.getAll({});
  } catch (e) {
    console.warn("[视频好帮手] cookie extract failed:", e);
    return { ok: false, reason: "extract_failed" };
  }
  if (!allCookies || allCookies.length === 0) {
    console.debug("[视频好帮手] no cookies found");
    return { ok: false, reason: "no_cookies" };
  }

  const cookies = allCookies.map(c => ({
    domain: c.domain,
    httpOnly: c.httpOnly,
    path: c.path,
    secure: c.secure,
    expires: c.expirationDate ? Math.floor(c.expirationDate) : 0,
    name: c.name,
    value: c.value,
    hostOnly: c.hostOnly,
    sameSite: c.sameSite,
  }));

  const response = await sendCookiesViaBridge(cookies, { userAgent: navigator.userAgent });
  if (response.ok) {
    console.info("[视频好帮手] all cookies exported:", cookies.length);
    return { ok: true, count: cookies.length };
  }
  const reason = response.reason ?? "bridge_failed";
  // 客户端未启动 / 未配对属于常态（定时任务每 2 分钟触发一次），
  // 这类原因降级为 debug，避免持续刷红控制台。
  const clientNotReady = reason === "fetch-failed" || reason === "missing-endpoint"
    || reason === "no-endpoint" || reason === "missing-token";
  if (clientNotReady) {
    console.debug("[视频好帮手] 客户端未就绪，跳过 cookie 上传:", reason);
  } else {
    console.warn("[视频好帮手] cookie export failed:", response.message ?? reason);
  }
  return { ok: false, reason };
}

async function scanOpenTabsForCookies() {
  if (!chrome.tabs?.query) return;
  try {
    const tabs = await chrome.tabs.query({});
    const seen = new Set();
    for (const tab of tabs) {
      if (!tab.url) continue;
      let host;
      try {
        host = new URL(tab.url).hostname;
      } catch {
        continue;
      }
      const platform = platformForDomain(host);
      if (!platform || seen.has(platform)) continue;
      seen.add(platform);
    }
    if (seen.size === 0) {
      console.info("[视频好帮手] no tracked tabs open at extension load");
      return;
    }
    // 仅作诊断记录：实际上传由 scanAllPlatformsForCookies 统一触发一次，
    // 避免同一份全量 cookie 被重复上传。
    console.debug("[视频好帮手] tracked platforms open:", [...seen].join(", "));
  } catch (e) {
    console.warn("[视频好帮手] scan tabs failed", e);
  }
}

// Scan every tracked platform — useful when the user logged into a service
// before installing the extension (no cookies.onChanged event was ever fired
// for that login, and they may not have a tab open right now).
async function scanAllPlatformsForCookies() {
  if (!chrome.cookies?.getAll) return;
  // 同上：一次全量导出即覆盖所有平台。历史实现按 14 个平台各调一次，
  // 会把同一份 cookie 重复上传 14 遍，客户端未运行时还会刷 14 条报错。
  try {
    await capturePlatformCookies("__all__", true);
  } catch (e) {
    console.warn("[视频好帮手] proactive capture failed", e);
  }
}

scanOpenTabsForCookies();
// Also do a proactive sweep for users who logged in BEFORE installing the
// extension (cookies.onChanged never fired, no tab open) — covers SoundCloud,
// YouTube etc. when the user already had a session in their browser.
scanAllPlatformsForCookies();

if (chrome.runtime?.onStartup) {
  chrome.runtime.onStartup.addListener(() => {
    void scanOpenTabsForCookies();
    void scanAllPlatformsForCookies();
    void ensureCookieRefreshAlarm();
  });
}
if (chrome.runtime?.onInstalled) {
  chrome.runtime.onInstalled.addListener(() => {
    void scanAllPlatformsForCookies();
    void ensureCookieRefreshAlarm();
  });
}

// ---- 定时 Cookie 自动刷新（每 2 分钟） ----
const COOKIE_REFRESH_ALARM = "视频好帮手-cookie-refresh";
const COOKIE_REFRESH_INTERVAL_MIN = 2;

async function ensureCookieRefreshAlarm() {
  try {
    // 先清除旧的
    await chrome.alarms.clear(COOKIE_REFRESH_ALARM).catch(() => {});
    chrome.alarms.create(COOKIE_REFRESH_ALARM, {
      periodInMinutes: COOKIE_REFRESH_INTERVAL_MIN,
    });
    console.info(`[视频好帮手] Cookie 自动刷新已启用（每 ${COOKIE_REFRESH_INTERVAL_MIN} 分钟）`);
  } catch (e) {
    console.warn("[视频好帮手] 创建 Cookie 刷新闹钟失败", e);
  }
}

if (chrome.alarms?.onAlarm) {
  chrome.alarms.onAlarm.addListener((alarm) => {
    if (alarm?.name === COOKIE_REFRESH_ALARM) {
      console.info("[视频好帮手] 定时 Cookie 刷新触发");
      void scanAllPlatformsForCookies();
    }
  });
}

if (chrome.cookies?.onChanged) {
  chrome.cookies.onChanged.addListener((change) => {
    const cookie = change.cookie;
    if (!cookie) return;
    if (!TRACKED_COOKIE_NAMES.has(cookie.name)) return;
    if (!shouldTrackCookieDomain(cookie.domain)) return;
    const platform = platformForDomain(cookie.domain);
    if (!platform) return;
    debounceCookieCapture(platform);
  });
}

if (chrome.tabs?.onUpdated) {
  chrome.tabs.onUpdated.addListener((_tabId, changeInfo, tab) => {
    if (changeInfo.status !== "complete") return;
    if (!tab?.url) return;
    let host;
    try {
      host = new URL(tab.url).hostname;
    } catch {
      return;
    }
    if (!shouldTrackCookieDomain(host)) return;
    const platform = platformForDomain(host);
    if (!platform) return;
    debounceCookieCapture(platform);
  });
}
