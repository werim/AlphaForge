(() => {
  "use strict";

  async function refreshElement(element) {
    const url = element.dataset.refreshUrl;
    if (!url) return;
    try {
      const response = await fetch(url, { headers: { "X-AlphaForge-Partial": "1" } });
      if (!response.ok) return;
      const html = await response.text();
      const wrapper = document.createElement("div");
      wrapper.innerHTML = html.trim();
      const replacement = wrapper.firstElementChild;
      if (replacement) element.replaceWith(replacement);
    } catch (_error) {
      // A temporarily unavailable dashboard must not imply a runtime status change.
    }
  }

  function scheduleRefresh() {
    const element = document.querySelector("[data-refresh-url]");
    if (!element) return;
    const interval = Number(element.dataset.refreshMs || "10000");
    window.setTimeout(async () => {
      await refreshElement(element);
      scheduleRefresh();
    }, Number.isFinite(interval) && interval >= 1000 ? interval : 10000);
  }

  window.addEventListener("DOMContentLoaded", scheduleRefresh);
})();
