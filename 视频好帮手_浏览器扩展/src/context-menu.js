const MENU_ID = "视频好帮手-download";
const MENU_TIKTOK_SEARCH_ID = "视频好帮手-tiktok-search";
const MENU_COPY_URL_ID = "视频好帮手-copy-url";
const MENU_TITLE_ID = "视频好帮手-copy-title";

export function registerContextMenu() {
  chrome.contextMenus.removeAll(() => {
    chrome.contextMenus.create({
      id: MENU_TIKTOK_SEARCH_ID,
      title: "TikTok搜索",
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
  });
}

export function getContextMenuId() {
  return MENU_ID;
}

export function getTikTokSearchMenuId() {
  return MENU_TIKTOK_SEARCH_ID;
}

export function getCopyUrlMenuId() {
  return MENU_COPY_URL_ID;
}

export function getCopyTitleMenuId() {
  return MENU_TITLE_ID;
}
