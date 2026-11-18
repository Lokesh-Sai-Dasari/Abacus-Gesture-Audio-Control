chrome.runtime.sendMessage({ type: "status" }, (res) => {
  const state = document.getElementById("state");
  const list = document.getElementById("list");
  if (!res) { state.textContent = "Extension is starting..."; return; }
  const dot = document.createElement("span");
  dot.className = "dot" + (res.connected ? " on" : "");
  state.append(dot, res.connected ? "Connected to Abacus" : "Abacus is closed (using saved tweaks)");
  if (!res.userScripts) {
    const note = document.createElement("div");
    note.textContent = 'Script tweaks are off: enable "Allow User Scripts" in this extension\'s Details page.';
    state.append(note);
  }
  if (!res.rules.length) {
    const li = document.createElement("li");
    const small = document.createElement("small");
    small.textContent = "No website tweaks yet.";
    li.append(small);
    list.append(li);
    return;
  }
  for (const r of res.rules) {
    const li = document.createElement("li");
    li.textContent = r.name;
    const small = document.createElement("small");
    small.textContent = r.target + (r.js ? " · script" : "");
    li.append(small);
    list.append(li);
  }
});
