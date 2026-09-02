const MENU_ID = "视频好帮手-download";
const MENU_TIKTOK_SEARCH_ID = "视频好帮手-tiktok-search";
const MENU_GOOGLE_SEARCH_ID = "视频好帮手-google-search";
const MENU_GOOGLE_IMAGE_SEARCH_ID = "视频好帮手-google-image-search";
const MENU_COPY_URL_ID = "视频好帮手-copy-url";
const MENU_TITLE_ID = "视频好帮手-copy-title";
const MENU_PAIR_ID = "视频好帮手-pair";

export function registerContextMenu() {
  chrome.contextMenus.removeAll(() => {
    chrome.contextMenus.create({
      id: MENU_TIKTOK_SEARCH_ID,
      title: "TikTok搜索",
      contexts: ["all"],
    });
    // Google 文字搜索：复用 TikTok搜索 的标题提取与关键词规则
    chrome.contextMenus.create({
      id: MENU_GOOGLE_SEARCH_ID,
      title: "🔎 Google 搜索",
      contexts: ["all"],
    });
    // Google 图片搜索：只用当前剪贴板里的完整原图
    chrome.contextMenus.create({
      id: MENU_GOOGLE_IMAGE_SEARCH_ID,
      title: "🔍 Google 图片搜索",
      contexts: ["all"],
    });
    chrome.contextMenus.create({
      id: MENU_ID,
      title: "视频好帮手_下载",
      contexts: ["all"],
    });
    chrome.contextMenus.create({
      id: MENU_COPY_URL_ID,
      title: "好帮手_复制当前地址",
      contexts: ["all"],
    });
    // 查看/复制视频标题
    chrome.contextMenus.create({
      id: MENU_TITLE_ID,
      title: "视频好帮手_查看视频标题",
      contexts: ["all"],
    });
    // 打开配对页面
    chrome.contextMenus.create({
      id: MENU_PAIR_ID,
      title: "配对",
      contexts: ["all"],
    });
  });
}

export function getContextMenuId() {
  return MENU_ID;
}

export function getTikTokSearchMenuId() {
  return MENU_TIKTOK_SEARCH_ID;
}

export function getGoogleSearchMenuId() {
  return MENU_GOOGLE_SEARCH_ID;
}

export function getGoogleImageSearchMenuId() {
  return MENU_GOOGLE_IMAGE_SEARCH_ID;
}

export function getPairMenuId() {
  return MENU_PAIR_ID;
}

export function getCopyUrlMenuId() {
  return MENU_COPY_URL_ID;
}

export function getCopyTitleMenuId() {
  return MENU_TITLE_ID;
}
