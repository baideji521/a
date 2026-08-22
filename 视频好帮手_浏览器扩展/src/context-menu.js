const MENU_ID = "视频好帮手-download";
const MENU_TITLE_ID = "视频好帮手-copy-title";

export function registerContextMenu() {
  chrome.contextMenus.removeAll(() => {
    chrome.contextMenus.create({
      id: MENU_ID,
      title: "视频好帮手_下载",
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

export function getCopyTitleMenuId() {
  return MENU_TITLE_ID;
}
