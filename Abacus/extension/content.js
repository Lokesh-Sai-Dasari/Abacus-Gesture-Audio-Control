// Abacus Tweaks: content script. Runs at document start on every page, applies this site's CSS
// tweaks, and keeps them in step with what Abacus has saved (new tweaks appear within seconds,
// removed ones disappear) while the tab is visible.
(() => {
  if (window.top !== window) return;
  const host = location.hostname.replace(/^(www|m)\./, "");
  const PREFIX = "abacus-tweak-";
  const CACHE = "abacus-tweaks:" + host;

  function apply(rules) {
    const root = document.documentElement;
    if (!root) return;
    const keep = new Set();
    for (const r of rules) {
      if (!r.css) continue;
      const id = PREFIX + r.id;
      keep.add(id);
      let el = document.getElementById(id);
      if (!el) {
        el = document.createElement("style");
        el.id = id;
        root.appendChild(el);
      }
      if (el.textContent !== r.css) el.textContent = r.css;
    }
    for (const el of document.querySelectorAll('style[id^="' + PREFIX + '"]')) {
      if (!keep.has(el.id) && el.id !== PREFIX + "try") el.remove();
    }
  }

  // 1. Instantly, from this site's own cache, so the page never flashes its original look.
  try {
    const cached = JSON.parse(localStorage.getItem(CACHE) || "null");
    if (cached) apply(cached);
  } catch (e) {}

  function use(rules) {
    apply(rules);
    try {
      if (rules.length) localStorage.setItem(CACHE, JSON.stringify(rules));
      else localStorage.removeItem(CACHE);
    } catch (e) {}
  }

  // 2. Then from the extension (which asks Abacus): once on load, every few seconds while the
  //    tab is visible, and occasionally while it is in the background.
  let fast = 0, slow = 0;
  function refresh(evenIfHidden) {
    if (!evenIfHidden && document.visibilityState !== "visible") return;
    try {
      chrome.runtime.sendMessage({ type: "rules", host }, (res) => {
        if (chrome.runtime.lastError || !res) return;
        use(res.rules);
      });
    } catch (e) {
      clearInterval(fast); clearInterval(slow);   // the extension was reloaded: this copy is orphaned
    }
  }
  refresh(true);
  fast = setInterval(() => refresh(false), 4000);
  slow = setInterval(() => refresh(true), 30000);
  document.addEventListener("visibilitychange", () => refresh(false));

  // 3. And pushed: when any tab's check reveals that the rules changed, the extension sends
  //    the new rules to every open tab of the affected sites, visible or not.
  try {
    chrome.runtime.onMessage.addListener((msg) => {
      if (msg && msg.type === "apply" && Array.isArray(msg.rules)) use(msg.rules);
    });
  } catch (e) {}
})();
