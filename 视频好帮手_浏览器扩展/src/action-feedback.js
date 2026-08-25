export const SUCCESS_BADGE_TEXT = "✓";
export const SUCCESS_BADGE_DURATION_MS = 1500;
export const SUCCESS_BADGE_BACKGROUND_COLOR = "#57b96a";

export function createActionFeedbackController({
  setBadgeText,
  setBadgeBackgroundColor = async () => {},
  setTimeoutFn = setTimeout,
  clearTimeoutFn = clearTimeout,
  durationMs = SUCCESS_BADGE_DURATION_MS,
  successText = SUCCESS_BADGE_TEXT,
  successColor = SUCCESS_BADGE_BACKGROUND_COLOR,
}) {
  const badgeTimeouts = new Map();

  // 标签页可能在徽章设置/清除之前就被关闭（chrome.action 会抛
  // "No tab with id: xxx"）。这属于预期情况，静默吞掉即可。
  async function safeCall(fn, args) {
    try {
      await fn(args);
    } catch {
      // tab 已关闭，忽略
    }
  }

  function clearBadgeTimer(tabId) {
    const timeoutId = badgeTimeouts.get(tabId);
    if (timeoutId === undefined) {
      return;
    }

    clearTimeoutFn(timeoutId);
    badgeTimeouts.delete(tabId);
  }

  async function clearBadge(tabId) {
    if (tabId === undefined || tabId === null) {
      return;
    }

    clearBadgeTimer(tabId);
    await safeCall(setBadgeText, {
      tabId,
      text: "",
    });
  }

  async function showSuccessBadge(tabId) {
    if (tabId === undefined || tabId === null) {
      return;
    }

    clearBadgeTimer(tabId);

    await Promise.all([
      safeCall(setBadgeBackgroundColor, {
        tabId,
        color: successColor,
      }),
      safeCall(setBadgeText, {
        tabId,
        text: successText,
      }),
    ]);

    const timeoutId = setTimeoutFn(() => {
      badgeTimeouts.delete(tabId);
      void safeCall(setBadgeText, {
        tabId,
        text: "",
      });
    }, durationMs);

    badgeTimeouts.set(tabId, timeoutId);
  }

  return {
    clearBadge,
    showSuccessBadge,
  };
}
