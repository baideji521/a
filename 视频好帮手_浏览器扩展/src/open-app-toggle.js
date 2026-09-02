const STORAGE_KEY = "视频好帮手_open_app_on_download";

// 默认关闭：只有用户在弹窗里显式勾选过，才会在下载时把桌面端拉到前台
let openApp = false;

export async function loadOpenAppState() {
  const result = await chrome.storage.local.get(STORAGE_KEY);
  openApp = result[STORAGE_KEY] === true;
  return openApp;
}

export function isOpenAppEnabled() {
  return openApp;
}

export async function setOpenAppEnabled(value) {
  openApp = value;
  await chrome.storage.local.set({ [STORAGE_KEY]: value });
}
