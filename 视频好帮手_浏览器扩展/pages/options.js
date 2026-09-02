import {
  loadBridgeConfig,
  saveBridgeConfig,
  checkBridgeHealth,
  discoverBridgeEndpoint,
  trimEndpoint,
  autoPair,
} from "../src/bridge-client.js";

const endpointInput = document.getElementById("endpoint");
const tokenInput = document.getElementById("token");
const revealBtn = document.getElementById("reveal");
const form = document.getElementById("pair-form");
const testBtn = document.getElementById("test");
const pairNowBtn = document.getElementById("pair-now");
const statusEl = document.getElementById("status");
const welcomeHeader = document.getElementById("welcome-header");
const settingsHeader = document.getElementById("settings-header");
const discoveryEl = document.getElementById("discovery-status");
const discoveryMessage = document.getElementById("discovery-message");
const endpointHint = document.getElementById("endpoint-hint");
const advancedDetails = document.getElementById("advanced");

// 与 man.py 的 BRIDGE_DEFAULT_PORT 保持一致
const FALLBACK_ENDPOINT = "http://127.0.0.1:5999";

function resolvedEndpoint() {
  return trimEndpoint(endpointInput.value) || FALLBACK_ENDPOINT;
}

function setStatus(message, kind) {
  statusEl.textContent = message ?? "";
  statusEl.classList.remove("ok", "error");
  if (kind === "ok") statusEl.classList.add("ok");
  if (kind === "error") statusEl.classList.add("error");
}

function setDiscovery(state, message) {
  discoveryEl.classList.remove("found", "missing");
  if (state === "found") discoveryEl.classList.add("found");
  if (state === "missing") discoveryEl.classList.add("missing");
  discoveryMessage.textContent = message;
}

// --- 自动配对 ---------------------------------------------------------------
// 桌面端点「配对」后会开启一个约 120 秒、一次性的配对窗口。
// 本页可见时每 5 秒轮询一次 `GET /v1/pair`，用户点完按钮几秒内即可完成配对
// （后台 Service Worker 只有每分钟一次的兜底重试）。
const AUTOPAIR_POLL_MS = 5000;
let pairPollTimer = null;

async function onPairedSuccess() {
  const { endpoint, token } = await loadBridgeConfig();
  endpointInput.value = endpoint || "";
  tokenInput.value = token || "";
  setDiscovery("found", `已连接到 ${endpoint}`);
  setStatus("配对成功，可以开始使用了。", "ok");
  stopPairPolling();
}

async function tryAutoPair() {
  const result = await autoPair().catch(() => ({ ok: false }));
  if (!result?.ok) return false;
  if (result.reason === "already-paired") {
    stopPairPolling();
    return true;
  }
  await onPairedSuccess();
  return true;
}

function startPairPolling() {
  if (pairPollTimer !== null) return;
  pairPollTimer = setInterval(() => {
    void tryAutoPair();
  }, AUTOPAIR_POLL_MS);
}

function stopPairPolling() {
  if (pairPollTimer !== null) {
    clearInterval(pairPollTimer);
    pairPollTimer = null;
  }
}

document.addEventListener("visibilitychange", () => {
  if (document.hidden) {
    stopPairPolling();
    return;
  }
  loadBridgeConfig().then(({ token }) => {
    if (!token) {
      void tryAutoPair();
      startPairPolling();
    }
  });
});

async function init() {
  const { endpoint, token } = await loadBridgeConfig();
  const alreadyPaired = Boolean(token);

  // 未配对时持续尝试自动取令牌：先立刻试一次（用户可能已经在桌面端
  // 开好了配对窗口），之后只要本页保持可见就每隔几秒重试。
  if (!alreadyPaired) {
    void tryAutoPair();
    if (!document.hidden) startPairPolling();
  }

  // 首次使用显示欢迎标题；已配对则显示设置标题（这时用户是来查看或改配置的）。
  if (alreadyPaired) {
    settingsHeader.hidden = false;
  } else {
    welcomeHeader.hidden = false;
  }

  endpointInput.value = endpoint || "";
  tokenInput.value = token || "";

  // 已配对且保存的端点能连通时跳过自动发现——用户多半只是来换令牌的，
  // 不该覆盖他自己填的地址。
  if (alreadyPaired) {
    const result = await checkBridgeHealth(endpoint);
    if (result.ok) {
      const versionSuffix = result.version ? `（协议 v${result.version}）` : "";
      setDiscovery("found", `已连接到视频批量下载工具${versionSuffix}：${endpoint}`);
      return;
    }
    setDiscovery(
      "missing",
      `连接不上已保存的地址 ${endpoint}，正在扫描默认端口…`
    );
  }

  const found = await discoverBridgeEndpoint();
  if (found) {
    endpointInput.value = found.endpoint;
    const versionSuffix = found.version ? `（协议 v${found.version}）` : "";
    setDiscovery(
      "found",
      `已找到视频批量下载工具${versionSuffix}：${found.endpoint}。请在桌面端点「配对」按钮，或把令牌粘贴到上方输入框。`
    );
    endpointHint.textContent = `已自动连接到 ${found.endpoint}`;
    return;
  }

  // 自动发现失败：展开「高级」，让用户手动填地址。
  if (advancedDetails) advancedDetails.open = true;
  setDiscovery(
    "missing",
    "没有检测到视频批量下载工具。请先启动桌面程序，然后刷新本页；也可以在下方手动填写地址。"
  );
}

revealBtn.addEventListener("click", () => {
  const next = tokenInput.type === "password" ? "text" : "password";
  tokenInput.type = next;
  revealBtn.textContent = next === "password" ? "显示" : "隐藏";
  revealBtn.setAttribute("aria-pressed", String(next !== "password"));
});

form.addEventListener("submit", async (event) => {
  event.preventDefault();
  const endpoint = resolvedEndpoint();
  const token = tokenInput.value.trim();
  if (!token) {
    setStatus("请先粘贴配对令牌。", "error");
    return;
  }
  await saveBridgeConfig({ endpoint, token });
  setStatus("已保存，扩展之后会使用这个令牌。", "ok");
});

if (pairNowBtn) {
  pairNowBtn.addEventListener("click", async () => {
    setStatus("正在尝试自动配对…");
    const paired = await tryAutoPair();
    if (paired) return;
    setStatus(
      "没有找到已开启的配对窗口。请在视频批量下载工具主界面点「配对」按钮，然后再试一次（也可以直接等待，本页会持续重试）。",
      "error"
    );
    if (!document.hidden) startPairPolling();
  });
}

testBtn.addEventListener("click", async () => {
  const endpoint = resolvedEndpoint();
  setStatus("正在测试连接…");
  const result = await checkBridgeHealth(endpoint);
  if (result.ok) {
    const versionSuffix = result.version ? `（协议 v${result.version}）` : "";
    setStatus(`连接成功${versionSuffix}：${endpoint}`, "ok");
  } else {
    setStatus(
      `连接不上 ${endpoint}，请确认视频批量下载工具正在运行。`,
      "error"
    );
  }
});

init();
