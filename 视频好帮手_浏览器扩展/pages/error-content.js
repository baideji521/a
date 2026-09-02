const ERROR_PAGE_MESSAGES = Object.freeze({
  error_page_document_title: "视频好帮手 扩展",
  error_page_eyebrow: "视频好帮手 扩展",
  error_page_install_cta: "安装视频批量下载工具",
  error_page_open_extensions: "打开扩展设置",
  error_host_missing_title: "请先启动一次视频批量下载工具以完成设置",
  error_host_missing_body: "浏览器还没有在这台电脑上找到视频批量下载工具的本地连接。",
  error_host_missing_detail:
    "如果尚未安装，请先安装视频批量下载工具；已安装的话启动一次桌面程序，然后重新点击扩展。",
  error_invalid_url_title: "当前页面地址无法发送给视频批量下载工具",
  error_invalid_url_body:
    "当前页面不是视频好帮手扩展支持的媒体页面。",
  error_invalid_url_detail:
    "请在具体的视频、Reel、帖子、播放列表或课程页面上重试。",
  error_launch_failed_title: "无法从浏览器启动视频批量下载工具",
  error_launch_failed_body:
    "扩展已经和本地程序通上了，但桌面端没有正常启动。",
  error_launch_failed_detail:
    "请确认视频批量下载工具已安装且没有被系统拦截，然后重试。",
});

const ERROR_CODES = Object.freeze({
  HOST_MISSING: Object.freeze({
    title: "error_host_missing_title",
    body: "error_host_missing_body",
    detail: "error_host_missing_detail",
  }),
  INVALID_URL: Object.freeze({
    title: "error_invalid_url_title",
    body: "error_invalid_url_body",
    detail: "error_invalid_url_detail",
  }),
  LAUNCH_FAILED: Object.freeze({
    title: "error_launch_failed_title",
    body: "error_launch_failed_body",
    detail: "error_launch_failed_detail",
  }),
});

function defaultGetMessage(name) {
  if (typeof chrome === "undefined" || !chrome.i18n?.getMessage) {
    return "";
  }

  return chrome.i18n.getMessage(name);
}

function resolveMessage(name, getMessage) {
  return getMessage(name) || ERROR_PAGE_MESSAGES[name];
}

export function resolveErrorPageContent({
  code = "LAUNCH_FAILED",
  message = "",
  getMessage = defaultGetMessage,
} = {}) {
  const resolvedCode = Object.prototype.hasOwnProperty.call(ERROR_CODES, code)
    ? code
    : "LAUNCH_FAILED";
  const content = ERROR_CODES[resolvedCode];

  return {
    documentTitle: resolveMessage("error_page_document_title", getMessage),
    eyebrow: resolveMessage("error_page_eyebrow", getMessage),
    installLabel: resolveMessage("error_page_install_cta", getMessage),
    openExtensionsLabel: resolveMessage("error_page_open_extensions", getMessage),
    title: resolveMessage(content.title, getMessage),
    body: resolveMessage(content.body, getMessage),
    detail: message || resolveMessage(content.detail, getMessage),
    code: resolvedCode,
  };
}
