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
      chrome.runtime.sendMessage({ type: "sendTo视频好帮手", url, platform: PLATFORM, referer: location.href, title: document.title }, resp => r(resp));
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
        scanIntercepted();
      } catch {}
    });

    // 通过 background 在 MAIN world 注入 fetch/XHR 拦截（background 有 chrome.tabs + chrome.scripting 权限）
    chrome.runtime.sendMessage({ type: "injectNetworkInterceptor" }, () => {});
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
    chrome.runtime.sendMessage({ type: "scanYtInternalState" }, (resp) => {
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

      chrome.runtime.sendMessage({ type: "readPageTitle", videoId: videoId || "", eventName }, (resp) => {
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

  // ============ 初始化 ============
  function init() {
    injectCSS();
    interceptNetwork();
    trackContextMenu();
    trackHover();
    listenForTitleRequest();
    simulateUserActivity(); // 模拟用户活跃行为
    scan();

    new MutationObserver(scan).observe(document.body || document.documentElement, { childList: true, subtree: true });
    setInterval(scan, 3000);

    console.log(`[视频好帮手] 内容脚本已加载 (${PLATFORM})`);
  }

  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", init);
  else init();
})();
