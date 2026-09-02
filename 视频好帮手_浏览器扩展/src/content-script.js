/**
 * 视频好帮手 Content Script - TikTok & YouTube
 *
 * 多渠道视频检测：
 *   渠道1: DOM 扫描（<a> 链接 + 平台特定选择器）
 *   渠道2: 网络拦截（拦截 fetch/XHR 获取 API 返回的视频数据）
 *   渠道3: <video> 元素追踪
 *   渠道4: 平台内部状态读取（SIGI/ytInitialData）
 *
 * 发现视频后在卡片上叠加悬停下载按钮。
 */

(function () {
  "use strict";
  if (document.__视频好帮手Injected) return;
  document.__视频好帮手Injected = true;

  const BTN = "og-dl";
  const DONE = "data-og";
  const CARD = "data-og-c";
  const host = location.hostname.replace(/^www\./, "");
  const IS_TIKTOK = host.endsWith("tiktok.com");
  const IS_YOUTUBE = host.endsWith("youtube.com") || host.endsWith("youtube-nocookie.com");
  const IS_INSTAGRAM = host.endsWith("instagram.com");
  const IS_TWITTER = host.endsWith("x.com") || host.endsWith("twitter.com");
  const IS_BILIBILI = host.endsWith("bilibili.com");
  const IS_VIMEO = host.endsWith("vimeo.com");
  const IS_TWITCH = host.endsWith("twitch.tv");
  const IS_REDDIT = host.endsWith("reddit.com");
  const IS_PINTEREST = host.endsWith("pinterest.com");
  const IS_SOUNDCLOUD = host.endsWith("soundcloud.com");
  const PLATFORM = IS_TIKTOK ? "tiktok" : IS_YOUTUBE ? "youtube" : IS_INSTAGRAM ? "instagram" : IS_TWITTER ? "twitter" : IS_BILIBILI ? "bilibili" : IS_VIMEO ? "vimeo" : IS_TWITCH ? "twitch" : IS_REDDIT ? "reddit" : IS_PINTEREST ? "pinterest" : IS_SOUNDCLOUD ? "soundcloud" : "generic";

  // ============ 扩展上下文守卫 ============
  // 扩展被重载/更新/停用后，遗留在页面里的本脚本仍会继续运行，此时任何
  // chrome.* 调用都会抛 "Extension context invalidated"。统一在这里拦掉，
  // 并停止后续扫描，避免把未捕获异常刷进页面控制台。
  let contextAlive = true;

  function isContextAlive() {
    if (!contextAlive) return false;
    try {
      if (!chrome.runtime?.id) {
        contextAlive = false;
      }
    } catch {
      contextAlive = false;
    }
    return contextAlive;
  }

  // 安全版 sendMessage：上下文失效时直接回调 undefined，不抛异常
  function safeSendMessage(msg, cb) {
    if (!isContextAlive()) {
      if (cb) cb(undefined);
      return false;
    }
    try {
      chrome.runtime.sendMessage(msg, (resp) => {
        // 读取 lastError 以消除 "Unchecked runtime.lastError" 噪声
        void chrome.runtime.lastError;
        if (cb) cb(resp);
      });
      return true;
    } catch {
      contextAlive = false;
      if (cb) cb(undefined);
      return false;
    }
  }

  // ============ 样式 ============
  function injectCSS() {
    if (document.getElementById("og-s")) return;
    const s = document.createElement("style");
    s.id = "og-s";
    s.textContent = `
      .${BTN}{position:absolute;bottom:8px;right:8px;width:36px;height:36px;border-radius:50%;
        background:rgba(255,255,255,.93);border:2px solid rgba(0,0,0,.08);cursor:pointer;
        display:none;align-items:center;justify-content:center;z-index:10001;
        box-shadow:0 2px 10px rgba(0,0,0,.3);transition:transform .15s,background .15s;padding:0}
      [${CARD}]:hover>.${BTN},[${CARD}]:focus-within>.${BTN}{display:flex}
      .${BTN}:hover{background:#fe2c55;transform:scale(1.15)}
      .${BTN}:hover svg{fill:#fff;stroke:#fff}
      .${BTN} svg{width:18px;height:18px;fill:#333;stroke:none;pointer-events:none}
      .${BTN}.og-s{background:#fe2c55;pointer-events:none}
      .${BTN}.og-s svg{fill:#fff;animation:og-r .8s linear infinite}
      .${BTN}.og-k{background:#25d366}
      .${BTN}.og-k svg{fill:#fff}
      .${BTN}.og-e{background:#f44}
      .${BTN}.og-e svg{fill:#fff}
      @keyframes og-r{to{transform:rotate(360deg)}}
    `;
    document.head.appendChild(s);
  }

  const ICO = {
    dl: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M21 15v4a2 2 0 01-2 2H5a2 2 0 01-2-2v-4"/><polyline points="7 10 12 15 17 10"/><line x1="12" y1="15" x2="12" y2="3"/></svg>',
    sp: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5"><circle cx="12" cy="12" r="10" stroke-dasharray="31.4 31.4" stroke-linecap="round"/></svg>',
    ok: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><polyline points="6 12 10 16 18 8"/></svg>',
    er: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round"><line x1="6" y1="6" x2="18" y2="18"/><line x1="18" y1="6" x2="6" y2="18"/></svg>',
  };

  // ============ 发送 ============
  function send(url) {
    return new Promise(r => {
      safeSendMessage({ type: "sendTo视频好帮手", url, platform: PLATFORM, referer: location.href, title: document.title }, resp => r(resp));
    });
  }

  function mkBtn(videoUrl) {
    const b = document.createElement("button");
    b.className = BTN;
    b.innerHTML = ICO.dl;
    b.title = "视频好帮手 下载";
    b.addEventListener("click", async e => {
      e.preventDefault(); e.stopPropagation(); e.stopImmediatePropagation();
      if (b.classList.contains("og-s")) return;
      b.className = BTN + " og-s"; b.innerHTML = ICO.sp;
      try {
        const r = await send(videoUrl);
        b.className = BTN + (r?.ok ? " og-k" : " og-e");
        b.innerHTML = r?.ok ? ICO.ok : ICO.er;
      } catch { b.className = BTN + " og-e"; b.innerHTML = ICO.er; }
      setTimeout(() => { b.className = BTN; b.innerHTML = ICO.dl; }, 2500);
    });
    return b;
  }

  function markCard(card, url) {
    if (card.getAttribute(DONE)) return;
    card.setAttribute(DONE, "1");
    card.setAttribute(CARD, "1");
    const p = getComputedStyle(card).position;
    if (p === "static") card.style.position = "relative";
    card.appendChild(mkBtn(url));
  }

  // 从元素向上查找卡片容器
  function findCard(el, maxUp = 7) {
    let c = el;
    for (let i = 0; i < maxUp; i++) {
      if (!c.parentElement) break;
      c = c.parentElement;
      if (c.getAttribute(DONE)) return null; // 已处理
      // 检测是否为卡片级容器
      if (c.tagName === "YTD-RICH-ITEM-RENDERER" || c.tagName === "YTD-VIDEO-RENDERER" ||
          c.tagName === "YTD-GRID-VIDEO-RENDERER" || c.tagName === "YTD-COMPACT-VIDEO-RENDERER" ||
          c.tagName === "YTD-RICH-SHELF-RENDERER") return c;
      // TikTok: 查找有固定尺寸或 overflow hidden 的容器
      if (IS_TIKTOK) {
        const s = getComputedStyle(c);
        if ((s.overflow === "hidden" || s.position === "relative") && c.querySelector('a[href*="/video/"]')) return c;
      }
    }
    // 最后兜底：如果 c 比原始元素大了好几层就用它
    return c !== el ? c : null;
  }

  // ============ 渠道1: DOM 链接扫描 ============
  function scanLinks() {
    let n = 0;
    if (IS_TIKTOK) {
      document.querySelectorAll('a[href*="/video/"]').forEach(a => {
        if (!/\/@[^/]+\/video\/\d+/.test(a.href)) return;
        const card = findCard(a);
        if (card && !card.getAttribute(DONE)) { markCard(card, a.href); n++; }
      });
    }
    if (IS_YOUTUBE) {
      // YouTube 视频链接: /watch?v=ID 或 /shorts/ID
      document.querySelectorAll('a[href*="/watch?v="], a[href*="/shorts/"]').forEach(a => {
        const href = a.href;
        if (!/v=[\w-]+/.test(href) && !/\/shorts\/[\w-]+/.test(href)) return;
        // 跳过已处理的
        if (a.getAttribute(DONE)) return;
        const card = findCard(a);
        if (card && !card.getAttribute(DONE)) { markCard(card, href); n++; }
      });
      // YouTube data-e2e 类似属性
      document.querySelectorAll('ytd-rich-item-renderer, ytd-video-renderer, ytd-grid-video-renderer').forEach(el => {
        if (el.getAttribute(DONE)) return;
        const a = el.querySelector('a#video-title, a#video-title-link, a[href*="/watch"]');
        if (a && a.href) { markCard(el, a.href); n++; }
      });
    }
    return n;
  }

  // ============ 渠道2: 网络拦截（通过 chrome.scripting 注入 MAIN world，避免 CSP） ============
  const interceptedUrls = new Map(); // videoId -> pageUrl

  function interceptNetwork() {
    // 监听 MAIN world 发来的拦截数据
    window.addEventListener("__og-net", e => {
      try {
        const text = e.detail;
        const vidMatches = text.match(/"videoId"\s*:\s*"([\w-]{11})"/g);
        if (vidMatches) {
          vidMatches.forEach(m => {
            const id = m.match(/"([\w-]{11})"/)[1];
            if (!interceptedUrls.has(id)) {
              interceptedUrls.set(id, `https://www.youtube.com/watch?v=${id}`);
            }
          });
        }
        const addrMatches = text.match(/"(?:playAddr|downloadAddr)"\s*:\s*"(https?:[^"]+)"/g);
        if (addrMatches) {
          addrMatches.forEach(m => {
            const url = m.match(/"(https?:[^"]+)"/)[1].replace(/\\u002F/g, "/");
            const idMatch = text.match(/"id"\s*:\s*"(\d{15,})"/);
            if (idMatch && !interceptedUrls.has(idMatch[1])) {
              interceptedUrls.set(idMatch[1], url);
            }
          });
        }
        // TikTok: 同一份响应里通常还带 author / stats，顺手解析给悬浮面板用（不影响上面的媒体 URL 提取）
        if (IS_TIKTOK) {
          try {
            parseTikTokNetworkMetadata(text);
            ttRefreshCurrentPanel();
          } catch (error) {
            ttLog("网络数据解析失败", error);
          }
        }
        scanIntercepted();
      } catch {}
    });

    // 通过 background 在 MAIN world 注入 fetch/XHR 拦截（background 有 chrome.tabs + chrome.scripting 权限）
    safeSendMessage({ type: "injectNetworkInterceptor" }, () => {});
  }

  function scanIntercepted() {
    let n = 0;
    interceptedUrls.forEach((pageUrl, id) => {
      // 查找页面上对应的元素
      let el = null;
      if (IS_YOUTUBE) {
        el = document.querySelector(`a[href*="v=${id}"]`) || document.querySelector(`[data-videoid="${id}"]`);
      } else if (IS_TIKTOK) {
        el = document.querySelector(`a[href*="/video/${id}"]`);
      }
      if (el) {
        const card = findCard(el);
        if (card && !card.getAttribute(DONE)) { markCard(card, pageUrl); n++; }
      }
    });
    if (n > 0) console.log(`[视频好帮手] 网络拦截匹配 ${n} 个视频`);
  }

  // ============ 渠道3: <video> 元素 ============
  function scanVideoElements() {
    let n = 0;
    document.querySelectorAll("video[src], video source[src]").forEach(v => {
      const src = v.src || v.getAttribute("src");
      if (!src || !src.startsWith("http")) return;
      // 向上找容器
      let container = v.closest("ytd-player") || v.closest("[class*='player']") || v.closest("div");
      if (!container) return;
      // 在容器中找视频链接
      const link = container.querySelector('a[href*="/watch"], a[href*="/video/"], a[href*="/shorts/"]');
      const url = link?.href || src;
      const card = findCard(container);
      if (card && !card.getAttribute(DONE)) { markCard(card, url); n++; }
    });
    return n;
  }

  // ============ 渠道4: 平台内部状态（通过 chrome.scripting 读取，避免 CSP） ============
  function scanInternalState() {
    if (!IS_YOUTUBE) return 0;
    // 通过 background 在 MAIN world 读取 ytInitialData（background 有 chrome.tabs + chrome.scripting 权限）
    safeSendMessage({ type: "scanYtInternalState" }, (resp) => {
      try {
        const ids = resp?.ids || [];
        let n = 0;
        ids.forEach(id => {
          const url = `https://www.youtube.com/watch?v=${id}`;
          const el = document.querySelector(`a[href*="v=${id}"]`);
          if (el) {
            const card = findCard(el);
            if (card && !card.getAttribute(DONE)) { markCard(card, url); n++; }
          }
        });
        if (n > 0) console.log(`[视频好帮手] 内部状态匹配 ${n} 个视频`);
      } catch {}
    });
    return 0;
  }

  // ============ 主扫描 ============
  let scanTimer = null;
  function scan() {
    if (scanTimer) return;
    scanTimer = setTimeout(() => {
      scanTimer = null;
      let total = 0;
      total += scanLinks();
      total += scanVideoElements();
      total += scanIntercepted();
      if (IS_YOUTUBE) { scanInternalState(); }
      if (total > 0) console.log(`[视频好帮手] 扫描发现 ${total} 个新视频`);
    }, 200);
  }

  // ============ 标题提取 ============
  let lastHoveredTitle = "";
  let lastHoveredCard = null;
  let lastContextMenuTitle = ""; // 右键菜单打开时的标题
  let lastContextMenuVideoId = ""; // 右键时捕获的 videoId

  // 穿透 shadow DOM 查找元素
  function deepQuerySelector(root, selector) {
    if (!root) return null;
    const found = root.querySelector(selector);
    if (found) return found;
    const all = root.querySelectorAll("*");
    for (const el of all) {
      if (el.shadowRoot) {
        const inShadow = el.shadowRoot.querySelector(selector);
        if (inShadow) return inShadow;
        const deeper = deepQuerySelector(el.shadowRoot, selector);
        if (deeper) return deeper;
      }
    }
    return null;
  }

  // 从事件 composedPath 中查找视频卡片（穿透 shadow DOM）
  function findCardFromEvent(e) {
    // composedPath() 返回事件的完整路径，包括穿透 shadow DOM
    const path = e.composedPath ? e.composedPath() : [];
    for (const el of path) {
      if (!el || el === document || el === window) continue;
      if (IS_YOUTUBE) {
        if (el.tagName === "YTD-RICH-ITEM-RENDERER" || el.tagName === "YTD-VIDEO-RENDERER" ||
            el.tagName === "YTD-GRID-VIDEO-RENDERER" || el.tagName === "YTD-COMPACT-VIDEO-RENDERER") {
          return el;
        }
      } else if (IS_TIKTOK) {
        if (el.getAttribute && el.getAttribute(CARD)) return el;
        if (el.tagName === "A" && el.href && /\/video\/\d+/.test(el.href)) {
          return findCard(el) || el;
        }
      }
    }
    // 兆底：用 e.target 的 closest
    const target = e.target;
    if (target && target.closest) {
      if (IS_YOUTUBE) {
        return target.closest("ytd-rich-item-renderer, ytd-video-renderer, ytd-grid-video-renderer");
      } else if (IS_TIKTOK) {
        return target.closest(`[${CARD}]`) || target.closest('a[href*="/video/"]');
      }
    }
    return null;
  }

  // 从视频卡片中提取标题
  function extractTitle(card) {
    if (!card) return "";
    let title = "";

    if (IS_YOUTUBE) {
      // YouTube: 优先使用精确选择器（穿透 shadow DOM），避免误取菜单按钮 aria-label
      title = extractTitleYouTube(card);
      if (title) return title;
      // 最后才尝试卡片级 aria-label（可能匹配到 "其他操作" 等 UI 文本）
      const cardAria = card.getAttribute && card.getAttribute("aria-label");
      if (cardAria && cardAria.length > 5) {
        title = cardAria.replace(/,\s*[\d,.]+\s*(?:thousand|million|billion|K|M|B)?\s*views?.*/i, "").trim();
        if (title && !isUIText(title)) return title;
      }
    } else if (IS_TIKTOK) {
      const descEl = card.querySelector('[data-e2e="video-desc"]') ||
                     card.querySelector('[class*="DivVideoCardMetadata"] span') ||
                     card.querySelector('[class*="SpanDescInfo"]');
      if (descEl) title = descEl.textContent || "";
      if (!title) {
        const a = card.querySelector('a[href*="/video/"]');
        if (a) title = a.getAttribute("title") || a.textContent || "";
      }
      if (!title) {
        const spans = card.querySelectorAll("span");
        for (const s of spans) {
          const t = s.textContent?.trim();
          if (t && t.length > 5 && t.length < 300) { title = t; break; }
        }
      }
    } else {
      // 其他平台：先尝试 title 属性，再用 aria-label
      const titleEl = card.querySelector('[title]');
      if (titleEl) title = titleEl.getAttribute("title") || "";
      if (!title) {
        const ariaEl = card.querySelector('[aria-label]');
        if (ariaEl) title = ariaEl.getAttribute("aria-label") || "";
      }
      if (!title) {
        const hEl = card.querySelector('h1, h2, h3, .title');
        if (hEl) title = hEl.textContent || "";
      }
    }
    if (!title) title = document.title || "";
    return title.trim();
  }

  // 判断是否为 UI 按钮文本（非视频标题）
  function isUIText(text) {
    if (!text) return false;
    const t = text.trim();
    // 极短文本（<=6字符）很可能是按钮/菜单标签
    if (t.length <= 6) return true;
    // 短英文文本（<=10字符，<=3词）
    if (t.length <= 10 && /^[a-zA-Z\s]+$/.test(t) && t.split(/\s+/).length <= 3) return true;
    return false;
  }

  // YouTube 专用标题提取（多策略优先级排序）
  function extractTitleYouTube(card) {
    if (!card) return "";
    let title = "";

    // 策略1: 精确 ID 选择器（穿透 shadow DOM）
    const titleEl = deepQuerySelector(card, "#video-title, #video-title-link");
    if (titleEl) {
      title = titleEl.getAttribute("title") || "";
      if (title && title.length > 1 && !isUIText(title)) return title;
      title = titleEl.textContent || "";
      if (title && title.length > 1 && !isUIText(title)) return title;
    }

    // 策略2: 视频链接的 title/aria-label（最可靠来源之一）
    const videoLink = deepQuerySelector(card, 'a[href*="/watch"], a[href*="/shorts/"]');
    if (videoLink) {
      const linkTitle = videoLink.getAttribute("title");
      if (linkTitle && linkTitle.length > 1 && !isUIText(linkTitle)) return linkTitle;
      const linkAria = videoLink.getAttribute("aria-label");
      if (linkAria && linkAria.length > 1 && !isUIText(linkAria)) return linkAria;
      // 链接文本通常是标题（但可能截断）
      const linkText = (videoLink.textContent || "").trim();
      if (linkText.length > 5 && linkText.length < 300 && !isUIText(linkText)) return linkText;
    }

    // 策略3: shadow DOM 内精确 ID 查找（比遍历所有 aria-label 更精确）
    const allEls = card.querySelectorAll ? card.querySelectorAll("*") : [];
    for (const el of allEls) {
      if (el.shadowRoot) {
        const st = el.shadowRoot.querySelector("#video-title, #video-title-link");
        if (st) {
          title = st.getAttribute("title") || st.textContent || "";
          if (title && title.length > 1 && !isUIText(title)) return title;
        }
      }
    }

    // 策略4: shadow DOM 内视频链接查找
    for (const el of allEls) {
      if (el.shadowRoot) {
        const sa = el.shadowRoot.querySelector('a[href*="/watch"], a[href*="/shorts/"]');
        if (sa) {
          title = sa.getAttribute("title") || sa.getAttribute("aria-label") || "";
          if (title && title.length > 1 && !isUIText(title)) return title;
          const st = (sa.textContent || "").trim();
          if (st.length > 5 && st.length < 300 && !isUIText(st)) return st;
        }
      }
    }

    // 策略5: 带过滤的 aria-label 遍历（排除 UI 按钮文本）
    for (const el of allEls) {
      const label = el.getAttribute && el.getAttribute("aria-label");
      if (label && label.length > 5 && !isUIText(label)) {
        title = label;
        break;
      }
      if (el.shadowRoot) {
        const inner = el.shadowRoot.querySelector("[aria-label]");
        if (inner) {
          const innerLabel = inner.getAttribute("aria-label") || "";
          if (innerLabel.length > 5 && !isUIText(innerLabel)) {
            title = innerLabel;
            break;
          }
        }
      }
    }
    if (title) return title;

    // 策略6: yt-formatted-string 文本内容
    const fmtEl = deepQuerySelector(card, "yt-formatted-string");
    if (fmtEl) {
      title = (fmtEl.textContent || "").trim();
      if (title.length > 3 && title.length < 300 && !isUIText(title)) return title;
    }

    // 策略7: 通过 videoId 在 ytInitialData 中查找（仅限 YouTube）
    try {
      const vid = extractVideoId(card);
      if (vid) {
        const renderer = card.querySelector("ytd-rich-item-renderer") || card;
        const dataAttr = renderer.getAttribute("data") || "";
        // 尝试从 DOM 数据属性中获取
        const allLinks = card.querySelectorAll('a[href*="v="], a[href*="/shorts/"]');
        for (const a of allLinks) {
          const t = a.getAttribute("title") || a.getAttribute("aria-label") || "";
          if (t.length > 5 && !isUIText(t)) return t;
        }
      }
    } catch {}

    return "";
  }

  // 从事件 composedPath 中提取 videoId（穿透 shadow DOM）
  function extractVideoId(card) {
    if (!card) return "";
    // ★ 优先用最近右键事件的 composedPath（穿透 shadow DOM）
    if (window.__ogLastEvent) {
      const path = window.__ogLastEvent.composedPath ? window.__ogLastEvent.composedPath() : [];
      for (const el of path) {
        if (!el || el === document || el === document.documentElement) continue;
        // YouTube: data-videoid 属性
        if (el.getAttribute) {
          const vid = el.getAttribute("data-videoid");
          if (vid && /^[\w-]{11}$/.test(vid)) return vid;
        }
        // YouTube/TikTok: href 中的视频链接
        if (el.href) {
          const vMatch = el.href.match(/[?&]v=([\w-]{11})/);
          if (vMatch) return vMatch[1];
          const sMatch = el.href.match(/\/shorts\/([\w-]{11})/);
          if (sMatch) return sMatch[1];
          const ttMatch = el.href.match(/\/video\/(\d{15,})/);
          if (ttMatch) return ttMatch[1];
        }
      }
    }
    // 回退：直接在卡片 light DOM 中查找
    const a = card.querySelector('a[href*="/watch"], a[href*="/shorts/"], a[href*="/video/"]');
    if (a) {
      const href = a.href || "";
      const vMatch = href.match(/[?&]v=([\w-]{11})/);
      if (vMatch) return vMatch[1];
      const sMatch = href.match(/\/shorts\/([\w-]{11})/);
      if (sMatch) return sMatch[1];
      const ttMatch = href.match(/\/video\/(\d{15,})/);
      if (ttMatch) return ttMatch[1];
    }
    return "";
  }

  // 方案A: 监听右键菜单事件，捕获精确的卡片位置
  function trackContextMenu() {
    document.addEventListener("contextmenu", (e) => {
      window.__ogLastEvent = e; // ★ 保存事件引用，供 extractVideoId 用 composedPath
      const card = findCardFromEvent(e);
      if (card) {
        lastContextMenuTitle = extractTitle(card);
        lastContextMenuVideoId = extractVideoId(card);
        lastHoveredCard = card;
        lastHoveredTitle = lastContextMenuTitle;
        console.log("[视频好帮手] 右键捕获 card:", card.tagName, "videoId:", lastContextMenuVideoId, "title:", lastContextMenuTitle.substring(0, 50));
      } else {
        lastContextMenuTitle = "";
        lastContextMenuVideoId = "";
        console.log("[视频好帮手] 右键未命中视频卡片");
      }
    }, { passive: true });
  }

  // 方案B: 鼠标移动追踪（辅助）
  function trackHover() {
    document.addEventListener("mousemove", (e) => {
      // 使用 composedPath 获取真实目标（穿透 shadow DOM）
      const path = e.composedPath ? e.composedPath() : [];
      let card = null;
      for (const el of path) {
        if (!el || el === document || el === window) continue;
        if (IS_YOUTUBE) {
          if (el.tagName === "YTD-RICH-ITEM-RENDERER" || el.tagName === "YTD-VIDEO-RENDERER" ||
              el.tagName === "YTD-GRID-VIDEO-RENDERER" || el.tagName === "YTD-COMPACT-VIDEO-RENDERER") {
            card = el; break;
          }
        } else if (IS_TIKTOK) {
          if (el.getAttribute && el.getAttribute(CARD)) { card = el; break; }
        }
      }
      if (!card && e.target && e.target.closest) {
        if (IS_YOUTUBE) {
          card = e.target.closest("ytd-rich-item-renderer, ytd-video-renderer, ytd-grid-video-renderer");
        } else if (IS_TIKTOK) {
          card = e.target.closest(`[${CARD}]`);
        }
      }
      if (card && (card !== lastHoveredCard || !lastHoveredTitle)) {
        lastHoveredCard = card;
        lastHoveredTitle = extractTitle(card);
      }
    }, { passive: true });
  }

  // 从页面上下文读取 ytInitialData / TikTok 数据（通过 chrome.scripting MAIN world，避免 CSP）
  function readTitleFromPageContext(videoId) {
    return new Promise((resolve) => {
      const eventName = "__og-title-" + Date.now();
      const handler = (e) => {
        window.removeEventListener(eventName, handler);
        resolve(e.detail || "");
      };
      window.addEventListener(eventName, handler);

      safeSendMessage({ type: "readPageTitle", videoId: videoId || "", eventName }, (resp) => {
        // background 注入的代码会 dispatch custom event，handler 会捕获
      });

      setTimeout(() => {
        window.removeEventListener(eventName, handler);
        resolve("");
      }, 1500);
    });
  }

  // 响应 background 的标题查询请求
  function listenForTitleRequest() {
    chrome.runtime.onMessage.addListener((msg, sender, sendResponse) => {
      if (msg.type === "getVideoTitle") {
        const vid = lastContextMenuVideoId;
        // 仅返回从 DOM 中实际提取到的标题（不是 document.title 兜底）
        let title = lastContextMenuTitle || lastHoveredTitle || "";
        // 清空右键状态
        lastContextMenuTitle = "";
        lastContextMenuVideoId = "";
        console.log("[视频好帮手] content-script 返回标题:", (title || "(空)").substring(0, 80), "videoId:", vid || "(空)");

        // 只返回真正从卡片中提取到的标题，不返回 document.title 兜底
        // document.title 在频道页是频道名，不是视频标题，会误导
        if (title && title !== (document.title || "")) {
          sendResponse({ title, videoId: vid });
        } else {
          // 异步兜底：从页面上下文读取 ytInitialData（绕过 content script 限制）
          readTitleFromPageContext(vid).then(pageTitle => {
            if (pageTitle) {
              sendResponse({ title: pageTitle, videoId: vid });
            } else {
              // 返回空，让 background 用 world:MAIN 或 fetch 方案
              sendResponse({ title: "", videoId: vid });
            }
          }).catch(() => {
            sendResponse({ title: "", videoId: vid });
          });
        }
        return true; // 保持消息通道开启（异步响应）
      }
    });
  }

  // ============ 模拟用户活跃行为 ============
  function simulateUserActivity() {
    if (!IS_TIKTOK) return; // 只在 TikTok 上生效
    
    // 每 2 秒发送一个真实的网络请求，保持 TikTok 会话活跃
    setInterval(() => {
      try {
        // 方法: 使用 fetch 请求当前页面的一个小 API 端点
        // 这会携带 Cookie，并且是真实的网络请求
        fetch('https://www.tiktok.com/api/recommend/item_list/?aid=1988&app_name=tiktok_web&device_platform=web_pc&region=US&language=en', {
          method: 'GET',
          headers: {
            'accept': 'application/json',
          },
          mode: 'cors',
          credentials: 'include' // 携带 Cookie
        }).then(() => {
          console.debug("[视频好帮手] 发送保活请求 (真实网络请求)");
        }).catch(() => {
          // 静默失败，不影响其他功能
        });
      } catch (e) {
        // 静默失败
      }
    }, 2000); // 2 秒一次
  }

  // ==============================================================
  // TikTok 当前视频信息悬浮面板（作者 / 点赞 / 评论，仅 TikTok 生效）
  //
  // 数据来源优先级：缓存 → 网络拦截(__og-net) → __UNIVERSAL_DATA_FOR_REHYDRATION__
  //                → SIGI_STATE → 当前视频卡片 DOM
  // 网络与页面状态解析出的视频统一按 videoId 写进 tikTokMetadataCache，
  // 所以"查缓存"本身就覆盖了前两级来源；DOM 只在前面都拿不到时兜底。
  // ==============================================================

  const TT_PANEL_ID = "video-helper-tiktok-meta";
  const TT_STYLE_ID = "og-tt-meta-s";
  const TT_CACHE_LIMIT = 400;
  const TT_SRC_RANK = { dom: 1, state: 2, network: 3 };
  const TT_LIKE_KEYS = ["diggCount", "digg_count", "likeCount", "like_count"];
  const TT_COMMENT_KEYS = ["commentCount", "comment_count"];
  const TT_VIEW_KEYS = ["playCount", "play_count", "viewCount", "view_count"];
  // data-e2e 在不同页面有 like-count / browse-like-count / video-like-count 等变体，用包含匹配一次覆盖
  const TT_LIKE_SEL = '[data-e2e*="like-count"], [data-e2e*="digg-count"]';
  const TT_COMMENT_SEL = '[data-e2e*="comment-count"]';
  const TT_VIEW_SEL = '[data-e2e*="video-views"], [data-e2e*="play-count"]';
  const TT_COUNT_SEL = TT_LIKE_SEL + ", " + TT_COMMENT_SEL;

  const tikTokMetadataCache = new Map(); // videoId -> metadata
  let currentTikTokVideoId = null;       // 当前鼠标所在视频的 key（videoId，或无 id 时的元素 key）
  let ttPanel = null;
  let ttAuthorEl = null;
  let ttDurationEl = null;
  let ttViewEl = null;
  let ttLikeEl = null;
  let ttCommentEl = null;
  let ttStateSignature = "";             // 页面状态脚本的指纹，避免重复解析
  let ttElementKeySeed = 0;
  let ttLastLoggedKey = "";
  let ttLastMouse = null;

  function ttLog(...args) {
    try { console.debug("[视频好帮手][TikTok Metadata]", ...args); } catch {}
  }

  // ---------- 数字格式化 ----------
  function ttTrimDecimal(value) {
    const s = value.toFixed(1);
    return s.endsWith(".0") ? s.slice(0, -2) : s;
  }

  function formatTikTokCount(value) {
    if (value === null || value === undefined || value === "") return "";
    const n = typeof value === "number" ? value : Number(value);
    if (!Number.isFinite(n) || n < 0) return "";
    if (n < 1000) return String(Math.round(n));
    if (n < 1e6) { const k = n / 1e3; return (k < 100 ? ttTrimDecimal(k) : String(Math.round(k))) + "K"; }
    if (n < 1e9) { const m = n / 1e6; return (m < 100 ? ttTrimDecimal(m) : String(Math.round(m))) + "M"; }
    const b = n / 1e9;
    return (b < 100 ? ttTrimDecimal(b) : String(Math.round(b))) + "B";
  }

  // 秒 → MM:SS（超过 1 小时用 HH:MM:SS）。拿不到时长返回空串，由调用方显示 …
  function formatTikTokDuration(seconds) {
    if (seconds === null || seconds === undefined || seconds === "") return "";
    const n = typeof seconds === "number" ? seconds : Number(seconds);
    if (!Number.isFinite(n) || n < 0) return "";
    const total = Math.round(n);
    const s = total % 60;
    const m = Math.floor(total / 60) % 60;
    const h = Math.floor(total / 3600);
    const pad = (v) => (v < 10 ? "0" + v : String(v));
    return h > 0 ? h + ":" + pad(m) + ":" + pad(s) : pad(m) + ":" + pad(s);
  }

  // DOM 上的数字已经是 "12.4K" / "1.2万" 这种展示形态，反解成数值再统一格式化
  function ttParseCountText(text) {
    if (text === null || text === undefined) return NaN;
    const t = String(text).trim().replace(/,/g, "").replace(/\s/g, "");
    if (!t) return NaN;
    const m = t.match(/^(\d+(?:\.\d+)?)([KkMmBbWw万亿])?$/);
    if (!m) return NaN;
    const base = parseFloat(m[1]);
    if (!Number.isFinite(base)) return NaN;
    switch ((m[2] || "").toLowerCase()) {
      case "k": return Math.round(base * 1e3);
      case "m": return Math.round(base * 1e6);
      case "b": return Math.round(base * 1e9);
      case "w": case "万": return Math.round(base * 1e4);
      case "亿": return Math.round(base * 1e8);
      default: return Math.round(base);
    }
  }

  // ---------- 面板 ----------
  function createTikTokMetadataPanel() {
    if (ttPanel && ttPanel.isConnected) return ttPanel;
    const root = document.body || document.documentElement;
    if (!root) return null;
    if (!document.getElementById(TT_STYLE_ID)) {
      const s = document.createElement("style");
      s.id = TT_STYLE_ID;
      s.textContent = `
        #${TT_PANEL_ID}{position:fixed;top:16px;right:16px;left:auto;z-index:2147483647;pointer-events:none;
          display:none;min-width:118px;max-width:260px;padding:7px 11px;border-radius:10px;
          background:rgba(20,20,22,.82);color:#fff;text-align:left;
          font:13px/1.45 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,"PingFang SC","Microsoft YaHei",sans-serif;
          box-shadow:0 4px 16px rgba(0,0,0,.35)}
        #${TT_PANEL_ID}.og-tt-on{display:block}
        #${TT_PANEL_ID} .author{font-weight:600;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
        #${TT_PANEL_ID} .stats{margin-top:3px;opacity:.92;white-space:nowrap}
        #${TT_PANEL_ID} .stats span+span{margin-left:12px}
      `;
      (document.head || root).appendChild(s);
    }
    const el = document.createElement("div");
    el.id = TT_PANEL_ID;
    el.setAttribute(DONE, "1"); // 让下载按钮的扫描逻辑跳过它
    const author = document.createElement("div");
    author.className = "author";
    const media = document.createElement("div");
    media.className = "stats";
    const dur = document.createElement("span");
    const view = document.createElement("span");
    media.appendChild(dur);
    media.appendChild(view);
    const stats = document.createElement("div");
    stats.className = "stats";
    const like = document.createElement("span");
    const comment = document.createElement("span");
    stats.appendChild(like);
    stats.appendChild(comment);
    el.appendChild(author);
    el.appendChild(media);
    el.appendChild(stats);
    root.appendChild(el);
    ttPanel = el;
    ttAuthorEl = author;
    ttDurationEl = dur;
    ttViewEl = view;
    ttLikeEl = like;
    ttCommentEl = comment;
    return ttPanel;
  }

  function showTikTokMetadataPanel(meta) {
    const panel = createTikTokMetadataPanel();
    if (!panel) return;
    const name = meta && (meta.author || meta.nickname) ? String(meta.author || meta.nickname).replace(/^@+/, "") : "";
    const author = "@" + (name || "unknown");
    const like = "❤️ " + (meta && Number.isFinite(meta.likeCount) ? formatTikTokCount(meta.likeCount) : "…");
    const comment = "💬 " + (meta && Number.isFinite(meta.commentCount) ? formatTikTokCount(meta.commentCount) : "…");
    const dur = "⏱ " + (meta ? (formatTikTokDuration(meta.duration) || "…") : "…");
    const view = "▶ " + (meta && Number.isFinite(meta.viewCount) ? formatTikTokCount(meta.viewCount) : "…");
    if (ttAuthorEl.textContent !== author) ttAuthorEl.textContent = author;
    if (ttDurationEl.textContent !== dur) ttDurationEl.textContent = dur;
    if (ttViewEl.textContent !== view) ttViewEl.textContent = view;
    if (ttLikeEl.textContent !== like) ttLikeEl.textContent = like;
    if (ttCommentEl.textContent !== comment) ttCommentEl.textContent = comment;
    panel.classList.add("og-tt-on");
  }

  function hideTikTokMetadataPanel() {
    if (ttPanel) ttPanel.classList.remove("og-tt-on");
  }

  // ---------- 缓存 ----------
  function ttRank(meta) {
    return (meta && TT_SRC_RANK[meta.source]) || 0;
  }

  // 高优先级来源的字段优先，缺失字段用另一份补齐
  function ttMergeMeta(a, b) {
    if (!a) return b || null;
    if (!b) return a;
    const [hi, lo] = ttRank(b) >= ttRank(a) ? [b, a] : [a, b];
    return {
      videoId: hi.videoId || lo.videoId || "",
      author: hi.author || lo.author || "",
      nickname: hi.nickname || lo.nickname || "",
      likeCount: Number.isFinite(hi.likeCount) ? hi.likeCount : lo.likeCount,
      commentCount: Number.isFinite(hi.commentCount) ? hi.commentCount : lo.commentCount,
      viewCount: Number.isFinite(hi.viewCount) ? hi.viewCount : lo.viewCount,
      duration: Number.isFinite(hi.duration) ? hi.duration : lo.duration,
      source: hi.source || lo.source || "",
      timestamp: Math.max(hi.timestamp || 0, lo.timestamp || 0),
    };
  }

  // 五个字段都到位才算完整。少了播放量或时长就必须继续往页面状态 / DOM 补，
  // 否则网络通道先把作者+点赞+评论写进缓存后会直接提前返回，viewCount / duration 永远补不上。
  function ttMetaComplete(meta) {
    return !!meta && !!(meta.author || meta.nickname) &&
      Number.isFinite(meta.likeCount) && Number.isFinite(meta.commentCount) &&
      Number.isFinite(meta.viewCount) && Number.isFinite(meta.duration);
  }

  function ttCacheSet(meta) {
    if (!meta || !meta.videoId) return null;
    const merged = ttMergeMeta(tikTokMetadataCache.get(meta.videoId), meta);
    tikTokMetadataCache.delete(meta.videoId);
    tikTokMetadataCache.set(meta.videoId, merged);
    while (tikTokMetadataCache.size > TT_CACHE_LIMIT) {
      const oldest = tikTokMetadataCache.keys().next().value;
      if (oldest === undefined) break;
      tikTokMetadataCache.delete(oldest);
    }
    return merged;
  }

  // ---------- 通用解析器 ----------
  function ttPickVideoId(obj) {
    const raw = obj.id !== undefined ? obj.id
      : obj.aweme_id !== undefined ? obj.aweme_id
      : obj.awemeId !== undefined ? obj.awemeId
      : obj.itemId !== undefined ? obj.itemId
      : obj.video_id;
    const s = typeof raw === "number" ? String(raw) : raw;
    return typeof s === "string" && /^\d{15,}$/.test(s) ? s : "";
  }

  function ttPickCount(stats, keys) {
    if (!stats || typeof stats !== "object") return NaN;
    for (const k of keys) {
      const v = stats[k];
      if (v === undefined || v === null || v === "") continue;
      const n = typeof v === "number" ? v : Number(v);
      if (Number.isFinite(n) && n >= 0) return n;
    }
    return NaN;
  }

  // 时长：只读真实字段（video.duration / duration / videoMeta.duration），不靠播放进度推算。
  // TikTok web 的 video.duration 是秒，app 风格接口是毫秒，超过 10 小时的值按毫秒处理。
  function ttPickDuration(obj) {
    if (!obj || typeof obj !== "object") return NaN;
    const raw = [obj.video, obj.videoMeta, obj]
      .filter((o) => o && typeof o === "object")
      .map((o) => (o.duration !== undefined ? o.duration : o.durationSec !== undefined ? o.durationSec : o.duration_sec))
      .find((v) => v !== undefined && v !== null && v !== "");
    if (raw === undefined) return NaN;
    let n = typeof raw === "number" ? raw : Number(raw);
    if (!Number.isFinite(n) || n <= 0) return NaN;
    if (n > 36000) n = n / 1000;
    return n > 0 && n < 86400 ? n : NaN;
  }

  // 只有"看起来是视频条目"的对象才允许作为 videoId 归属点，
  // 否则 author.id / music.id（同样是 19 位数字）会把归属带偏
  function ttLooksLikeItem(obj) {
    return ("desc" in obj) || ("video" in obj) || ("stats" in obj) || ("statistics" in obj) ||
      ("author" in obj) || ("createTime" in obj) || ("create_time" in obj) || ("statsV2" in obj);
  }

  // authorStats / 含 followerCount 的对象里的 diggCount 是"作者总获赞"，不是本视频的点赞
  function ttIsAuthorStats(obj, parentKey) {
    if (parentKey && /author/i.test(parentKey)) return true;
    return ("followerCount" in obj) || ("follower_count" in obj) || ("followingCount" in obj) ||
      ("heartCount" in obj) || ("heart" in obj);
  }

  // 抽出 videoId / 作者 / 点赞 / 评论。
  // 作者与统计**不要求在同一个对象里**：只要能确定所属 videoId（自身 id 或最近的条目祖先 id），
  // 就单独产出一份局部 metadata，由 ttCacheSet 按 videoId 合并。
  function ttItemMetadata(obj, source, ownerId, parentKey) {
    if (!obj || typeof obj !== "object") return null;
    const own = ttPickVideoId(obj);
    const videoId = (own && ttLooksLikeItem(obj)) ? own : (ownerId || "");
    if (!videoId) return null;
    const author = obj.author !== undefined ? obj.author : obj.authorInfo;
    const stats = obj.stats || obj.statistics || obj.statsV2 || obj.stats_v2 || null;
    let uniqueId = "";
    let nickname = "";
    if (typeof author === "string") {
      uniqueId = author;
    } else if (author && typeof author === "object") {
      uniqueId = author.uniqueId || author.unique_id || author.uniqueID || "";
      nickname = author.nickname || author.nickName || "";
    }
    // 对象自身就是 author（作者对象在条目之外的层级）
    if (!uniqueId && typeof obj.uniqueId === "string") uniqueId = obj.uniqueId;
    if (!uniqueId && typeof obj.unique_id === "string") uniqueId = obj.unique_id;
    if (!nickname && typeof obj.nickname === "string") nickname = obj.nickname;
    const likeKeys = TT_LIKE_KEYS;
    const commentKeys = TT_COMMENT_KEYS;
    let likeCount = ttPickCount(stats, likeKeys);
    let commentCount = ttPickCount(stats, commentKeys);
    // 对象自身就是 stats（statistics 在条目之外的层级）
    if (!ttIsAuthorStats(obj, parentKey)) {
      if (!Number.isFinite(likeCount)) likeCount = ttPickCount(obj, likeKeys);
      if (!Number.isFinite(commentCount)) commentCount = ttPickCount(obj, commentKeys);
    }
    // 播放量：优先条目自己的 stats/statistics；对象自身就是 stats 时也认，
    // 但必须同时带 digg/comment 计数，否则 challenge/music 的 viewCount 会被误当成视频播放量
    let viewCount = ttPickCount(stats, TT_VIEW_KEYS);
    if (!Number.isFinite(viewCount) && !ttIsAuthorStats(obj, parentKey) &&
        (Number.isFinite(ttPickCount(obj, likeKeys)) || Number.isFinite(ttPickCount(obj, commentKeys)))) {
      viewCount = ttPickCount(obj, TT_VIEW_KEYS);
    }
    // 时长只认"条目本身"或它的 video / videoMeta 子对象，
    // 否则 music.duration（配乐长度）会被当成视频时长
    const duration = ((own && ttLooksLikeItem(obj)) || /^video/i.test(parentKey || ""))
      ? ttPickDuration(obj) : NaN;
    if (!uniqueId && !nickname && !Number.isFinite(likeCount) && !Number.isFinite(commentCount) &&
        !Number.isFinite(viewCount) && !Number.isFinite(duration)) return null;
    return {
      videoId,
      author: typeof uniqueId === "string" ? uniqueId : "",
      nickname: typeof nickname === "string" ? nickname : "",
      likeCount,
      commentCount,
      viewCount,
      duration,
      source: source || "state",
      timestamp: Date.now(),
    };
  }

  // 递归遍历任意结构：沿途记住"最近的视频条目 id"作为归属，
  // 这样 author 和 statistics 分处不同层级时也能按同一个 videoId 归拢。
  function parseTikTokVideoMetadata(data, targetVideoId, source) {
    let hit = null;
    const budget = { nodes: 0 };
    const itemIds = new Set();
    const orphanStats = [];
    (function walk(node, ownerId, parentKey, depth) {
      if (!node || typeof node !== "object" || depth > 16 || budget.nodes > 200000) return;
      budget.nodes++;
      if (Array.isArray(node)) {
        for (const v of node) walk(v, ownerId, parentKey, depth + 1);
        return;
      }
      const own = ttPickVideoId(node);
      const isItem = !!own && ttLooksLikeItem(node);
      const id = isItem ? own : ownerId;
      if (isItem) itemIds.add(own);
      const meta = ttItemMetadata(node, source, id, parentKey);
      if (meta) {
        const stored = ttCacheSet(meta);
        if (targetVideoId && meta.videoId === targetVideoId) hit = stored || meta;
      } else if (!id && !ttIsAuthorStats(node, parentKey)) {
        // 计数所在的对象没有任何 id 归属：先记下，等确认整份数据只对应一个视频再认领
        const like = ttPickCount(node, TT_LIKE_KEYS);
        const comment = ttPickCount(node, TT_COMMENT_KEYS);
        if (Number.isFinite(like) || Number.isFinite(comment)) orphanStats.push({ like, comment });
      }
      for (const k in node) {
        const v = node[k];
        if (v && typeof v === "object") walk(v, id, k, depth + 1);
      }
    })(data, "", "", 0);
    // 整份数据里只有一个视频时，游离的 statistics 只可能属于它——这是唯一解，不是猜测。
    // 有多个视频就一律丢弃，宁可显示 … 也不认错人。
    if (itemIds.size === 1 && orphanStats.length) {
      const only = itemIds.values().next().value;
      for (const s of orphanStats) {
        const stored = ttCacheSet({
          videoId: only, author: "", nickname: "",
          likeCount: s.like, commentCount: s.comment,
          source: source || "state", timestamp: Date.now(),
        });
        if (targetVideoId && only === targetVideoId) hit = stored || hit;
      }
    }
    return hit || (targetVideoId ? tikTokMetadataCache.get(targetVideoId) || null : null);
  }

  // 从字符串位置 start（必须指向 '{'）起做字符串感知的花括号配对，取出完整对象片段
  function ttSliceObject(text, start, limit) {
    if (text[start] !== "{") return "";
    const max = Math.min(text.length, start + (limit || 60000));
    let depth = 0, inStr = false, esc = false;
    for (let i = start; i < max; i++) {
      const c = text[i];
      if (inStr) {
        if (esc) esc = false;
        else if (c === "\\") esc = true;
        else if (c === '"') inStr = false;
        continue;
      }
      if (c === '"') { inStr = true; continue; }
      if (c === "{") depth++;
      else if (c === "}") { depth--; if (depth === 0) return text.slice(start, i + 1); }
    }
    return "";
  }

  // 网络拦截数据：__og-net 只带前 50000 字符，整体常常不是合法 JSON。
  // 能整体解析就整体遍历；否则抓出完整的条目对象逐个精确解析，绝不跨对象取字段。
  // 条目起点同时支持 {"id":"<19位>" 与 "itemStruct"/"item"/"aweme_detail"/"aweme" 包装层。
  function parseTikTokNetworkMetadata(text) {
    if (!IS_TIKTOK || !text) return;
    try {
      parseTikTokVideoMetadata(JSON.parse(text), null, "network");
      return;
    } catch {}
    const starts = [];
    const idRe = /\{"(?:id|aweme_id|awemeId|itemId)":"?\d{15,}/g;
    let m;
    while ((m = idRe.exec(text)) !== null && starts.length < 60) starts.push(m.index);
    const wrapRe = /"(?:itemStruct|itemInfo|item|aweme_detail|aweme)"\s*:\s*\{/g;
    while ((m = wrapRe.exec(text)) !== null && starts.length < 120) {
      starts.push(m.index + m[0].length - 1);
    }
    let parsed = 0;
    for (const start of starts) {
      if (parsed >= 40) break;
      const slice = ttSliceObject(text, start);
      if (!slice) continue;
      try {
        // 包装层内部可能还有嵌套（itemInfo → itemStruct），交给递归解析器按 videoId 归属
        parseTikTokVideoMetadata(JSON.parse(slice), null, "network");
        parsed++;
      } catch {}
    }
  }

  // 统计字段常常落在 __og-net 的 50000 字符截断之外，所以 background 注入的 MAIN world
  // 拦截器会就着完整响应额外派发一个极小的 __og-tt-meta 事件
  // （每项 {id, author?, nickname?, likeCount?, commentCount?}）。
  // 这里只按 videoId 入缓存，由 ttCacheSet 合并，不做任何跨条目猜测。
  function handleTikTokMetaEvent(detail) {
    let list = null;
    try { list = JSON.parse(detail); } catch { return; }
    if (!Array.isArray(list)) return;
    let n = 0;
    for (const it of list) {
      if (!it || !/^\d{15,}$/.test(String(it.id || ""))) continue;
      const like = it.likeCount === undefined || it.likeCount === null ? NaN : Number(it.likeCount);
      const comment = it.commentCount === undefined || it.commentCount === null ? NaN : Number(it.commentCount);
      const view = it.viewCount === undefined || it.viewCount === null ? NaN : Number(it.viewCount);
      const dur = it.duration === undefined || it.duration === null ? NaN : Number(it.duration);
      ttCacheSet({
        videoId: String(it.id),
        author: typeof it.author === "string" ? it.author : "",
        nickname: typeof it.nickname === "string" ? it.nickname : "",
        likeCount: Number.isFinite(like) ? like : NaN,
        commentCount: Number.isFinite(comment) ? comment : NaN,
        viewCount: Number.isFinite(view) ? view : NaN,
        duration: Number.isFinite(dur) && dur > 0 ? (dur > 36000 ? dur / 1000 : dur) : NaN,
        source: "network",
        timestamp: Date.now(),
      });
      n++;
    }
    if (n) ttRefreshCurrentPanel();
  }

  // 页面内嵌状态：__UNIVERSAL_DATA_FOR_REHYDRATION__ 优先，其次 SIGI_STATE。
  // 两者都是 <script> 文本节点，content script 可直接读，无需注入 MAIN world。
  function getTikTokPageStateMetadata(videoId) {
    let hit = null;
    let signature = "";
    const sources = [];
    for (const id of ["__UNIVERSAL_DATA_FOR_REHYDRATION__", "SIGI_STATE"]) {
      const el = document.getElementById(id);
      const text = el && el.textContent ? el.textContent : "";
      if (!text) continue;
      sources.push(text);
      signature += id + ":" + text.length + ";";
    }
    if (!sources.length) return null;
    // 内容没变就不重复解析整棵 JSON，直接吃缓存
    if (signature === ttStateSignature) {
      return videoId ? tikTokMetadataCache.get(videoId) || null : null;
    }
    ttStateSignature = signature;
    for (const text of sources) {
      try {
        const found = parseTikTokVideoMetadata(JSON.parse(text), videoId, "state");
        if (found && !hit) hit = found;
      } catch (error) {
        ttLog("页面状态解析失败", error);
      }
    }
    return hit || (videoId ? tikTokMetadataCache.get(videoId) || null : null);
  }

  // ---------- DOM 兜底（必须限定在当前视频容器内） ----------
  function getTikTokDomMetadata(container, videoId) {
    if (!container || typeof container.querySelector !== "function") return null;
    let author = "";
    let nickname = "";
    const uniqueEl = container.querySelector(
      '[data-e2e="video-author-uniqueid"], [data-e2e="browse-username"], [data-e2e="author-uniqueid"], [data-e2e="search-card-user-unique-id"]'
    );
    if (uniqueEl) author = (uniqueEl.textContent || "").trim();
    if (!author) {
      // 容器万一偏大，优先取与当前 videoId 对应的那条链接，避免拿成别人的作者
      const link = (videoId && container.querySelector(`a[href*="/video/${videoId}"]`)) ||
        container.querySelector('a[href*="/@"]');
      const href = link ? link.getAttribute("href") || "" : "";
      const m = href.match(/\/@([\w.\-]+)/);
      if (m) author = m[1];
    }
    if (!author) {
      const m = location.pathname.match(/^\/@([\w.\-]+)/);
      if (m) author = m[1];
    }
    const nickEl = container.querySelector('[data-e2e="browse-author-name"], [data-e2e="video-author-nickname"]');
    if (nickEl) nickname = (nickEl.textContent || "").trim();
    // data-e2e 在不同页面有 like-count / browse-like-count / video-like-count 等变体，用包含匹配一次覆盖
    let likeEl = container.querySelector(TT_LIKE_SEL);
    let commentEl = container.querySelector(TT_COMMENT_SEL);
    // 单视频页（/@user/video/<id>）：页面主视频就是它，此时才允许放宽到整页去找计数，
    // 其它页面绝不整页查询，避免拿到别的视频的数字
    const urlId = (location.pathname.match(/\/video\/(\d{15,})/) || [])[1] || "";
    let viewEl = container.querySelector(TT_VIEW_SEL);
    if (videoId && urlId && videoId === urlId) {
      if (!likeEl) likeEl = document.querySelector(TT_LIKE_SEL);
      if (!commentEl) commentEl = document.querySelector(TT_COMMENT_SEL);
      if (!viewEl) viewEl = document.querySelector(TT_VIEW_SEL);
    }
    const likeCount = likeEl ? ttParseCountText(likeEl.textContent) : NaN;
    const commentCount = commentEl ? ttParseCountText(commentEl.textContent) : NaN;
    const viewCount = viewEl ? ttParseCountText(viewEl.textContent) : NaN;
    // 时长兜底：读当前容器里 <video> 元素自带的真实媒体时长（不是播放进度）
    let duration = NaN;
    const videoEl = container.tagName === "VIDEO" ? container : container.querySelector("video");
    if (videoEl && Number.isFinite(videoEl.duration) && videoEl.duration > 0) duration = videoEl.duration;
    if (!author && !nickname && !Number.isFinite(likeCount) && !Number.isFinite(commentCount) &&
        !Number.isFinite(viewCount) && !Number.isFinite(duration)) return null;
    return {
      videoId: videoId || "",
      author,
      nickname,
      likeCount,
      commentCount,
      viewCount,
      duration,
      source: "dom",
      timestamp: Date.now(),
    };
  }

  // ---------- videoId 提取 ----------
  function ttIdFromHref(href) {
    const m = (href || "").match(/\/video\/(\d{15,})/);
    return m ? m[1] : "";
  }

  function extractTikTokVideoId(element) {
    try {
      if (element) {
        // 自身 / 祖先的视频链接（最贴近鼠标，优先级最高）
        if (element.tagName === "A") {
          const own = ttIdFromHref(element.href || element.getAttribute("href"));
          if (own) return own;
        }
        if (typeof element.closest === "function") {
          const up = element.closest('a[href*="/video/"]');
          if (up) {
            const id = ttIdFromHref(up.href || up.getAttribute("href"));
            if (id) return id;
          }
        }
        // data-* 属性（自身 + 祖先）
        let node = element;
        for (let i = 0; i < 8 && node; i++) {
          if (node.getAttribute) {
            for (const attr of ["data-video-id", "data-videoid", "data-item-id", "data-e2e-id"]) {
              const v = node.getAttribute(attr);
              if (v && /^\d{15,}$/.test(v)) return v;
            }
            // 播放器容器 id 形如 xgwrapper-0-7123456789012345678
            const domId = node.getAttribute("id") || "";
            const idMatch = domId.match(/(\d{15,})/);
            if (idMatch) return idMatch[1];
          }
          node = node.parentElement;
        }
        // 容器内的视频链接
        if (typeof element.querySelector === "function") {
          const inner = element.querySelector('a[href*="/video/"]');
          if (inner) {
            const id = ttIdFromHref(inner.href || inner.getAttribute("href"));
            if (id) return id;
          }
        }
      }
      // 详情页 / 单视频页直接看地址栏
      const fromUrl = ttIdFromHref(location.pathname);
      if (fromUrl) return fromUrl;
    } catch (error) {
      ttLog("videoId 提取失败", error);
    }
    return "";
  }

  // ---------- 命中判定 ----------
  // 命中条件只有三种：<video> 元素、/video/ 链接、已标记的下载卡片 [data-og-c]。
  // 导航栏 / 搜索框 / 评论区 / 作者区都不满足，所以不会误弹。
  function ttHitFromNodes(nodes) {
    for (const el of nodes) {
      if (!el || !el.tagName || el === document || el === window) continue;
      if (el.id === TT_PANEL_ID) continue;
      if (el.tagName === "VIDEO") return el;
      if (el.tagName === "A" && /\/video\/\d{15,}/.test(el.href || el.getAttribute("href") || "")) return el;
      if (el.getAttribute && el.getAttribute(CARD)) return el;
    }
    return null;
  }

  function findTikTokVideoAtPoint(x, y, event) {
    let hit = null;
    try {
      if (typeof document.elementsFromPoint === "function") {
        hit = ttHitFromNodes(document.elementsFromPoint(x, y) || []);
      }
    } catch {}
    // 第二层：composedPath（穿透 shadow DOM）
    if (!hit && event && typeof event.composedPath === "function") {
      try { hit = ttHitFromNodes(event.composedPath() || []); } catch {}
    }
    // 第三层：closest 兜底
    if (!hit && event && event.target && typeof event.target.closest === "function") {
      hit = event.target.closest(`a[href*="/video/"], [${CARD}]`);
    }
    if (!hit) return null;
    return { hit, container: ttVideoContainer(hit) };
  }

  // 找到"这一个视频"的容器：DOM 兜底必须 scoped 到它。
  // 上溯时有三道边界，防止越过卡片拿到别的视频的数字：
  //   1) 不允许上到 body / html
  //   2) 命中已标记卡片 [data-og-c] 就停在卡片
  //   3) 候选容器里出现两个以上不同 videoId 的链接，说明已经跨到多视频容器，弃用
  function ttVideoContainer(el) {
    let marked = null;
    try { marked = el.closest ? el.closest(`[${CARD}]`) : null; } catch {}
    let node = el;
    for (let i = 0; i < 14 && node; i++) {
      if (node === document.body || node === document.documentElement) break;
      if (!marked && node.getAttribute && node.getAttribute(CARD)) marked = node;
      if (node.querySelector && node.querySelector(TT_COUNT_SEL)) {
        if (ttDistinctVideoIds(node) > 1) break;
        return node;
      }
      if (marked && node === marked) break;
      node = node.parentElement;
    }
    return marked || el;
  }

  function ttDistinctVideoIds(node) {
    try {
      if (!node.querySelectorAll) return 0;
      const ids = new Set();
      node.querySelectorAll('a[href*="/video/"]').forEach((a) => {
        const m = (a.getAttribute("href") || "").match(/\/video\/(\d{15,})/);
        if (m) ids.add(m[1]);
      });
      return ids.size;
    } catch {
      return 0;
    }
  }

  function ttElementKey(el) {
    if (!el) return null;
    try {
      if (!el.__ogTtKey) el.__ogTtKey = "el#" + (++ttElementKeySeed);
      return el.__ogTtKey;
    } catch {
      return null;
    }
  }

  // ---------- 取数（缓存 → 网络/页面状态 → DOM） ----------
  function getTikTokMetadata(videoId, container) {
    let meta = videoId ? tikTokMetadataCache.get(videoId) || null : null;
    if (ttMetaComplete(meta)) return meta;
    if (videoId) {
      const fromState = getTikTokPageStateMetadata(videoId);
      if (fromState) meta = ttMergeMeta(meta, fromState);
      if (ttMetaComplete(meta)) return meta;
    }
    const fromDom = getTikTokDomMetadata(container, videoId);
    if (fromDom) {
      meta = ttMergeMeta(meta, fromDom);
      if (videoId) meta = ttCacheSet(meta) || meta;
    }
    return meta;
  }

  // ---------- hover ----------
  function handleTikTokMetadataHover(point) {
    try {
      const found = point ? findTikTokVideoAtPoint(point.clientX, point.clientY, point) : null;
      if (!found) {
        currentTikTokVideoId = null;
        hideTikTokMetadataPanel();
        return;
      }
      const videoId = extractTikTokVideoId(found.hit) || extractTikTokVideoId(found.container);
      const key = videoId || ttElementKey(found.container);
      if (!key) { hideTikTokMetadataPanel(); return; }

      if (key === currentTikTokVideoId) {
        // 同一个视频：数据后到（网络到达、DOM 渲染出计数）时补齐，已完整就不再重算
        const cached = videoId ? tikTokMetadataCache.get(videoId) : null;
        if (ttMetaComplete(cached)) { showTikTokMetadataPanel(cached); return; }
        const refreshed = getTikTokMetadata(videoId, found.container);
        if (key !== currentTikTokVideoId) return;
        if (refreshed) showTikTokMetadataPanel(refreshed);
        return;
      }

      // 切换视频：先把 key 换掉并清空旧数据，任何晚到的旧视频结果都会被 key 判定挡掉
      currentTikTokVideoId = key;
      showTikTokMetadataPanel({ videoId });

      const meta = getTikTokMetadata(videoId, found.container);
      if (key !== currentTikTokVideoId) return;
      if (meta) showTikTokMetadataPanel(meta);
      if (key !== ttLastLoggedKey) {
        ttLastLoggedKey = key;
        ttLog("videoId=" + (videoId || "(未识别)"),
          "author=" + ((meta && (meta.author || meta.nickname)) || "(空)"),
          "likes=" + ((meta && meta.likeCount) ?? "(空)"),
          "comments=" + ((meta && meta.commentCount) ?? "(空)"),
          "source=" + ((meta && meta.source) || "(无)"));
      }
    } catch (error) {
      ttLog("hover 处理失败", error);
    }
  }

  // 网络数据晚于 hover 到达时，刷新当前正在显示的视频
  function ttRefreshCurrentPanel() {
    if (!currentTikTokVideoId) return;
    const meta = tikTokMetadataCache.get(currentTikTokVideoId);
    if (meta) showTikTokMetadataPanel(meta);
  }

  function initTikTokMetadataPanel() {
    if (!IS_TIKTOK) return;

    // MAIN world 拦截器就着完整响应派发的紧凑统计数据（点赞 / 评论）
    window.addEventListener("__og-tt-meta", (e) => {
      try { handleTikTokMetaEvent(e.detail); } catch (error) { ttLog("统计事件处理失败", error); }
    });

    let pendingPoint = null;
    let frame = null;
    const schedule = (point) => {
      pendingPoint = point;
      if (frame) return;
      frame = requestAnimationFrame(() => {
        frame = null;
        const p = pendingPoint;
        pendingPoint = null;
        if (p) handleTikTokMetadataHover(p);
      });
    };

    document.addEventListener("mousemove", (e) => {
      ttLastMouse = { clientX: e.clientX, clientY: e.clientY };
      schedule(e);
    }, { passive: true });

    // 滚动后鼠标下的视频会变，但不会触发 mousemove，这里用最后的坐标重算一次
    document.addEventListener("scroll", () => {
      if (ttLastMouse) schedule(ttLastMouse);
    }, { passive: true, capture: true });

    const leave = () => {
      currentTikTokVideoId = null;
      hideTikTokMetadataPanel();
    };
    document.addEventListener("mouseleave", leave, { passive: true });
    window.addEventListener("blur", leave, { passive: true });

    try { getTikTokPageStateMetadata(""); } catch (error) { ttLog("首次页面状态解析失败", error); }
    ttLog("已启用");
  }

  // ============ 初始化 ============
  let scanObserver = null;
  let scanInterval = null;

  // 上下文失效后彻底停掉扫描，避免旧脚本在页面里空转
  function teardown() {
    try { scanObserver?.disconnect(); } catch {}
    if (scanInterval) clearInterval(scanInterval);
    scanObserver = null;
    scanInterval = null;
  }

  function init() {
    injectCSS();
    interceptNetwork();
    trackContextMenu();
    trackHover();
    listenForTitleRequest();
    simulateUserActivity(); // 模拟用户活跃行为
    initTikTokMetadataPanel(); // TikTok 当前视频信息悬浮面板
    scan();

    scanObserver = new MutationObserver(() => {
      if (!isContextAlive()) { teardown(); return; }
      scan();
    });
    scanObserver.observe(document.body || document.documentElement, { childList: true, subtree: true });
    scanInterval = setInterval(() => {
      if (!isContextAlive()) { teardown(); return; }
      scan();
    }, 3000);

    console.log(`[视频好帮手] 内容脚本已加载 (${PLATFORM})`);
  }

  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", init);
  else init();
})();
