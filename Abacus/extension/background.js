// Abacus Tweaks: background service worker.
// Keeps a copy of the tweak rules Abacus has saved (fetched from Abacus's local server on this
// PC, cached in chrome.storage so they keep working while Abacus is closed), answers content
// scripts asking for their site's rules, and registers script tweaks as user scripts.

const SERVER = "http://127.0.0.1:47616";
const HEADERS = { "X-Abacus-Tweaks": "1" };
const FRESH_MS = 3000;        // don't ask the server more often than this
const BACKOFF_MS = 20000;     // after a failed fetch (Abacus closed), wait this long

let lastTry = 0, lastOk = 0, failedAt = 0, inFlight = null;

function userScriptsAvailable() {
  try { chrome.userScripts.getScripts; return true; } catch (e) { return false; }
}

function matches(host, target) {
  return host === target || host.endsWith("." + target);
}

function patterns(target) {
  return ["*://" + target + "/*", "*://*." + target + "/*"];
}

async function getState() {
  const { rules = [], rev = "" } = await chrome.storage.local.get(["rules", "rev"]);
  return { rules, rev };
}

async function registerScripts(rules, changedIds) {
  if (!userScriptsAvailable()) return;
  try {
    const wanted = rules.filter(r => r.js);
    const existing = await chrome.userScripts.getScripts();
    const stale = existing.map(s => s.id).filter(id => !wanted.some(r => "abacus-" + r.id === id));
    if (stale.length) await chrome.userScripts.unregister({ ids: stale });
    for (const r of wanted) {
      const script = { id: "abacus-" + r.id, matches: patterns(r.target), js: [{ code: r.js }],
                       runAt: "document_idle", world: "MAIN" };
      if (existing.some(s => s.id === script.id)) await chrome.userScripts.update([script]);
      else await chrome.userScripts.register([script]);
      // run it now in tabs that are already open on that site
      if (changedIds.has(r.id) && chrome.userScripts.execute) {
        const tabs = await chrome.tabs.query({ url: patterns(r.target) });
        for (const tab of tabs) {
          chrome.userScripts.execute({ target: { tabId: tab.id }, js: [{ code: r.js }], world: "MAIN" })
            .catch(() => {});
        }
      }
    }
  } catch (e) {
    console.warn("Abacus Tweaks: script tweaks not registered:", e);
  }
}

// The rules changed: tell every open tab of an affected site right away, visible or not, so a
// new tweak shows up (and a removed one disappears) without waiting for that tab's own check.
async function broadcast(oldRules, newRules) {
  const targets = new Set([...oldRules, ...newRules].map(r => r.target));
  for (const target of targets) {
    let tabs = [];
    try { tabs = await chrome.tabs.query({ url: patterns(target) }); } catch (e) { continue; }
    for (const tab of tabs) {
      let host = "";
      try { host = new URL(tab.url).hostname.replace(/^(www|m)\./, ""); } catch (e) { continue; }
      const mine = newRules.filter(r => matches(host, r.target))
                           .map(r => ({ id: r.id, name: r.name, css: r.css }));
      chrome.tabs.sendMessage(tab.id, { type: "apply", rules: mine }).catch(() => {});
    }
  }
}

async function sync() {
  const now = Date.now();
  if (inFlight) return inFlight;
  if (now - lastTry < FRESH_MS || (failedAt && now - failedAt < BACKOFF_MS)) return;
  lastTry = now;
  inFlight = (async () => {
    try {
      const res = await fetch(SERVER + "/tweaks", { headers: HEADERS, cache: "no-store",
                                                    signal: AbortSignal.timeout(1500) });
      if (!res.ok) throw new Error("HTTP " + res.status);
      const data = await res.json();
      const old = await getState();
      failedAt = 0; lastOk = Date.now();
      if (data.rev !== old.rev) {
        const before = new Map(old.rules.map(r => [r.id, r.js || ""]));
        const changed = new Set(data.tweaks.filter(r => r.js && before.get(r.id) !== r.js).map(r => r.id));
        await chrome.storage.local.set({ rules: data.tweaks, rev: data.rev });
        await registerScripts(data.tweaks, changed);
        await broadcast(old.rules, data.tweaks);
      }
      fetch(SERVER + "/status", { method: "POST", headers: { ...HEADERS, "Content-Type": "application/json" },
        body: JSON.stringify({ userScripts: userScriptsAvailable(), version: chrome.runtime.getManifest().version }) })
        .catch(() => {});
    } catch (e) {
      failedAt = Date.now();   // Abacus isn't running: keep using the cached rules
    } finally {
      inFlight = null;
    }
  })();
  return inFlight;
}

chrome.runtime.onMessage.addListener((msg, sender, reply) => {
  if (msg && msg.type === "rules") {
    (async () => {
      await sync();
      const { rules, rev } = await getState();
      const mine = rules.filter(r => matches(msg.host, r.target));
      if (sender.tab) {
        chrome.action.setBadgeText({ tabId: sender.tab.id, text: mine.length ? String(mine.length) : "" });
        chrome.action.setBadgeBackgroundColor({ tabId: sender.tab.id, color: "#f4a261" });
      }
      reply({ rev, rules: mine.map(r => ({ id: r.id, name: r.name, css: r.css })) });
    })();
    return true;   // reply asynchronously
  }
  if (msg && msg.type === "status") {
    (async () => {
      await sync();
      const { rules } = await getState();
      reply({ connected: !failedAt && lastOk > 0, userScripts: userScriptsAvailable(), rules });
    })();
    return true;
  }
});

chrome.runtime.onInstalled.addListener(() => { sync(); });
chrome.runtime.onStartup.addListener(async () => {
  const { rules } = await getState();
  await registerScripts(rules, new Set());
  sync();
});
