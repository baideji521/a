export const 视频好帮手_SCHEME = "视频好帮手://";
const SCHEME_TAB_LIFETIME_MS = 1500;

export function buildVideoHelperSchemeUrl(url) {
  if (typeof url !== "string") {
    return null;
  }
  const trimmed = url.trim();
  if (!trimmed) {
    return null;
  }
  if (trimmed.startsWith(视频好帮手_SCHEME)) {
    return trimmed;
  }
  if (trimmed.startsWith("magnet:") || trimmed.startsWith("p2p:")) {
    return `视频好帮手:${trimmed}`;
  }
  const withoutProtocol = trimmed.replace(/^https?:\/\//i, "");
  if (!withoutProtocol) {
    return null;
  }
  return `${视频好帮手_SCHEME}${withoutProtocol}`;
}

// Opens the 视频好帮手:// scheme in a hidden background tab so the OS can route
// it to the desktop app, without disturbing the user's currently focused tab.
// We close the throwaway tab shortly after creation since the page itself has
// no meaningful content (the protocol handler runs out-of-process).
export async function openVideoHelperScheme(
  url,
  {
    tabs = chrome?.tabs,
    schedule = (fn, ms) => setTimeout(fn, ms),
    tabLifetimeMs = SCHEME_TAB_LIFETIME_MS,
  } = {}
) {
  const schemeUrl = buildVideoHelperSchemeUrl(url);
  if (!schemeUrl) {
    return { ok: false, reason: "invalid-url" };
  }
  if (!tabs?.create) {
    return { ok: false, reason: "tabs-api-unavailable" };
  }
  try {
    const newTab = await tabs.create({ url: schemeUrl, active: false });
    const tabId = newTab?.id;
    if (typeof tabId === "number" && typeof tabs.remove === "function") {
      schedule(() => {
        try {
          const removeResult = tabs.remove(tabId);
          if (removeResult && typeof removeResult.catch === "function") {
            removeResult.catch(() => {});
          }
        } catch {
          // Tab may already be gone; ignore.
        }
      }, tabLifetimeMs);
    }
    return { ok: true, schemeUrl };
  } catch (error) {
    return {
      ok: false,
      reason: "tabs-create-failed",
      message: error instanceof Error ? error.message : String(error),
    };
  }
}
