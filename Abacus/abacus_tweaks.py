"""GUI tweaks: Abacus changes how websites and apps look or behave, and the change sticks.

A tweak is a small piece of CSS (and, rarely, JavaScript) tied to a website or a desktop app,
kept in tweaks.json. "Abacus, hide Shorts on YouTube" or "make Spotify light mode" ends with the
agent saving a tweak here; these three parts then keep it applied:

- Websites: the "Abacus Tweaks" Chrome extension (the extension/ folder) asks the little local
  server in this file (127.0.0.1 only) for the rules of the page's site and injects them on
  every load. It caches them, so tweaks keep working while Abacus is closed.
- Desktop apps built on Chromium (Spotify, Discord, VS Code, Slack, Notion, Obsidian...): the app
  is started with a local debugging port, and the watcher thread in this file injects the CSS
  through it and re-injects whenever the app reloads. The app's shortcuts are patched to pass
  the port flag so the tweak survives a normal restart.
- Classic Windows apps have no stylesheet to change; for those the agent uses the app's own
  settings or Windows' theme settings instead.

Security notes: the local server only answers the extension (custom header + origin check, bound
to 127.0.0.1). A debugging port lets any program running on this PC drive that app, the same
access the app's own DevTools would give; it is never exposed to the network. CSS tweaks are
stripped of remote url()/@import so a stylesheet can't phone home.
"""
import base64
import contextlib
import http.server
import io
import json
import logging
import os
import re
import subprocess
import threading
import time
import zlib
from datetime import datetime
from pathlib import Path

import config

HERE = Path(__file__).resolve().parent
TWEAKS_FILE = HERE / "tweaks.json"
STATUS_FILE = HERE / "extension_status.json"
log = logging.getLogger("abacus.tweaks")

PORT = getattr(config, "TWEAKS_PORT", 47616)
APP_PORT_BASE = 47700
MAX_CSS = 60_000
MAX_SHOT_WIDTH = 1568
# Apps that may hold unsaved work: never restarted without Venu saying yes first.
ASK_BEFORE_RESTART = {"code.exe", "cursor.exe", "devenv.exe", "winword.exe", "excel.exe",
                      "powerpnt.exe", "obsidian.exe", "notion.exe", "figma.exe", "slack.exe",
                      "teams.exe", "ms-teams.exe", "discord.exe"}
_file_lock = threading.Lock()


# ---------------------------------------------------------------- the store

def _load() -> dict:
    try:
        data = json.loads(TWEAKS_FILE.read_text("utf-8"))
    except FileNotFoundError:
        data = {}
    except Exception as e:
        log.warning("tweaks.json unreadable (%s)", e)
        data = {}
    data.setdefault("tweaks", [])
    data.setdefault("apps", {})
    return data


def _save(data: dict) -> None:
    tmp = TWEAKS_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), "utf-8")
    tmp.replace(TWEAKS_FILE)


def normalize_target(target: str) -> tuple[str, str]:
    """'youtube.com', 'https://www.youtube.com/x', 'site:youtube.com' -> ('site', 'youtube.com');
    'Spotify', 'app:Spotify.exe' -> ('app', 'spotify.exe')."""
    t = target.strip().strip("\"'").lower()
    kind = None
    if t.startswith("site:"):
        kind, t = "site", t[5:]
    elif t.startswith("app:"):
        kind, t = "app", t[4:]
    t = t.strip()
    if kind is None:
        kind = "app" if (t.endswith(".exe") or "." not in t) and "/" not in t else "site"
    if kind == "site":
        t = re.sub(r"^[a-z]+://", "", t).split("/")[0].split(":")[0]
        t = re.sub(r"^(www|m)\.", "", t)
        if not re.fullmatch(r"[a-z0-9.-]+\.[a-z]{2,}|localhost", t):
            raise ValueError(f"'{target}' is not a website address like youtube.com")
    else:
        t = os.path.basename(t)
        if not t.endswith(".exe"):
            t += ".exe"
        if not re.fullmatch(r"[\w .+()-]+\.exe", t):
            raise ValueError(f"'{target}' is not an app name like Spotify.exe")
    return kind, t


def sanitize_css(css: str) -> tuple[str, list[str]]:
    """A tweak's CSS only restyles what is already on the page: remote resources are removed."""
    notes = []
    if len(css) > MAX_CSS:
        raise ValueError(f"CSS is too long ({len(css)} characters; limit {MAX_CSS}).")
    cleaned = re.sub(r"@import\b[^;]*;?", "", css, flags=re.I)
    if cleaned != css:
        notes.append("removed @import")

    def url(m: re.Match) -> str:
        inner = m.group(1).strip().strip("\"'")
        if inner.lower().startswith("data:") or inner.startswith("#"):
            return m.group(0)
        notes.append("removed a remote url()")
        return "none"
    cleaned = re.sub(r"url\(([^)]*)\)", url, cleaned, flags=re.I)
    cleaned = cleaned.replace("</style", "")
    return cleaned.strip(), sorted(set(notes))


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:48] or "tweak"


def save_tweak(target: str, name: str, css: str = "", js: str = "") -> tuple[dict, list[str]]:
    """Create or update (same target + name) a tweak. Returns (tweak, notes)."""
    kind, value = normalize_target(target)
    css, notes = sanitize_css(css or "")
    js = (js or "").strip()
    if not css and not js:
        raise ValueError("A tweak needs some CSS or a script.")
    name = " ".join(name.split())[:60] or "Tweak"
    tid = _slug(f"{value.rsplit('.', 1)[0]}-{name}")
    now = datetime.now().isoformat(timespec="seconds")
    with _file_lock:
        data = _load()
        tweak = next((t for t in data["tweaks"] if t["id"] == tid), None)
        if tweak is None:
            tweak = {"id": tid, "created": now}
            data["tweaks"].append(tweak)
        tweak.update(kind=kind, target=value, name=name, css=css, js=js, enabled=True, updated=now)
        _save(data)
    log.info("saved tweak %s (%s %s, %d css chars, %d js chars)", tid, kind, value, len(css), len(js))
    return tweak, notes


def remove_tweak(tweak_id: str) -> dict | None:
    with _file_lock:
        data = _load()
        tweak = next((t for t in data["tweaks"] if t["id"] == tweak_id), None)
        if tweak:
            data["tweaks"].remove(tweak)
            _save(data)
            log.info("removed tweak %s", tweak_id)
    return tweak


def set_enabled(tweak_id: str, enabled: bool) -> dict | None:
    with _file_lock:
        data = _load()
        tweak = next((t for t in data["tweaks"] if t["id"] == tweak_id), None)
        if tweak:
            tweak["enabled"] = bool(enabled)
            _save(data)
    return tweak


def all_tweaks() -> list[dict]:
    return _load()["tweaks"]


def find_tweaks(text: str) -> list[dict]:
    """Tweaks whose id, name or target mention every word of `text` (for 'undo the Spotify one')."""
    words = [w for w in re.split(r"\W+", text.lower()) if w]
    return [t for t in all_tweaks()
            if all(w in f"{t['id']} {t['name']} {t['target']}".lower() for w in words)]


def listing() -> str:
    tweaks = all_tweaks()
    if not tweaks:
        return "No GUI tweaks saved yet."
    lines = []
    for t in tweaks:
        what = "+".join(x for x, has in (("css", t.get("css")), ("script", t.get("js"))) if has)
        lines.append(f"[{t['id']}] {t['kind']} {t['target']}: \"{t['name']}\" ({what}, "
                     f"{'on' if t.get('enabled', True) else 'OFF'}, updated {t.get('updated', '?')})")
    return "\n".join(lines)


def site_rules() -> list[dict]:
    return [{"id": t["id"], "target": t["target"], "name": t["name"], "css": t.get("css", ""),
             "js": t.get("js", "")}
            for t in all_tweaks() if t["kind"] == "site" and t.get("enabled", True)]


def extension_status() -> str:
    """Whether the Chrome extension has been talking to us, in words for the agent."""
    try:
        st = json.loads(STATUS_FILE.read_text("utf-8"))
    except Exception:
        return ("The Abacus Tweaks Chrome extension has never connected. Website tweaks are saved "
                "but won't show until Venu loads it once: chrome://extensions -> Developer mode -> "
                f"Load unpacked -> {HERE / 'extension'}.")
    age = time.time() - st.get("seen", 0)
    scripts = ("script tweaks enabled" if st.get("userScripts")
               else "CSS tweaks only (script tweaks need 'Allow User Scripts' switched on in the "
                    "extension's Details page)")
    when = "connected" if age < 120 else f"last seen {int(age // 60)} min ago"
    return f"Abacus Tweaks extension {when}; {scripts}."


# ---------------------------------------------------------------- local server for the extension

class _Handler(http.server.BaseHTTPRequestHandler):
    server_version = "AbacusTweaks/1"

    def log_message(self, *a) -> None:   # stay quiet: this runs inside the voice loop's console
        pass

    def _origin_ok(self) -> bool:
        origin = self.headers.get("Origin", "")
        return not origin or origin.startswith("chrome-extension://")

    def _send(self, code: int, body: dict | None = None) -> None:
        data = json.dumps(body or {}, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        origin = self.headers.get("Origin", "")
        if origin.startswith("chrome-extension://"):
            self.send_header("Access-Control-Allow-Origin", origin)
            self.send_header("Access-Control-Allow-Headers", "X-Abacus-Tweaks, Content-Type")
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def _allowed(self) -> bool:
        # Web pages can't send this header cross-origin without a preflight, which we refuse.
        if self.headers.get("X-Abacus-Tweaks") != "1" or not self._origin_ok():
            self._send(403, {"error": "forbidden"})
            return False
        return True

    def do_OPTIONS(self) -> None:
        self._send(204 if self._origin_ok() and self.headers.get("Origin") else 403)

    def do_GET(self) -> None:
        if not self._allowed():
            return
        if self.path.startswith("/tweaks"):
            try:
                rev = str(TWEAKS_FILE.stat().st_mtime_ns)
            except OSError:
                rev = "0"
            self._send(200, {"rev": rev, "tweaks": site_rules()})
        elif self.path.startswith("/ping"):
            self._send(200, {"ok": True})
        else:
            self._send(404, {"error": "not found"})

    def do_POST(self) -> None:
        if not self._allowed():
            return
        if not self.path.startswith("/status"):
            self._send(404, {"error": "not found"})
            return
        try:
            body = json.loads(self.rfile.read(min(int(self.headers.get("Content-Length", 0)), 4096)))
            status = {"seen": time.time(), "userScripts": bool(body.get("userScripts")),
                      "version": str(body.get("version", ""))[:20]}
            STATUS_FILE.write_text(json.dumps(status), "utf-8")
            self._send(200, {"ok": True})
        except Exception:
            self._send(400, {"error": "bad request"})


def serve() -> http.server.ThreadingHTTPServer | None:
    try:
        httpd = http.server.ThreadingHTTPServer(("127.0.0.1", PORT), _Handler)
    except OSError as e:
        log.info("tweak server not started (port %d busy: %s)", PORT, e)
        return None
    httpd.daemon_threads = True
    threading.Thread(target=httpd.serve_forever, daemon=True, name="tweak-server").start()
    return httpd


# ---------------------------------------------------------------- Chromium desktop apps (CDP)

class Cdp:
    """A DevTools-protocol connection to one page of an app."""

    def __init__(self, ws_url: str, timeout: float = 10.0) -> None:
        import websocket
        # no Origin header: Chromium refuses debugger connections that carry one
        self.ws = websocket.create_connection(ws_url, timeout=timeout, suppress_origin=True)
        self.n = 0

    def call(self, method: str, **params) -> dict:
        self.n += 1
        self.ws.send(json.dumps({"id": self.n, "method": method, "params": params}))
        while True:
            msg = json.loads(self.ws.recv())
            if msg.get("id") == self.n:
                if "error" in msg:
                    raise RuntimeError(f"{method}: {msg['error'].get('message')}")
                return msg.get("result", {})

    def evaluate(self, expression: str):
        r = self.call("Runtime.evaluate", expression=expression, returnByValue=True,
                      awaitPromise=True)
        if "exceptionDetails" in r:
            d = r["exceptionDetails"]
            raise RuntimeError("page script error: " + str(
                d.get("exception", {}).get("description") or d.get("text"))[:400])
        return r.get("result", {}).get("value")

    def close(self) -> None:
        with contextlib.suppress(Exception):
            self.ws.close()


def _app_name(app: str) -> str:
    return normalize_target(app if app.lower().startswith("app:") else "app:" + app)[1]


def _processes(exe: str) -> list:
    import psutil
    found = []
    for p in psutil.process_iter(["name", "exe", "cmdline", "create_time"]):
        if (p.info["name"] or "").lower() == exe:
            found.append(p)
    return found


def _port_owner(port: int) -> str | None:
    """Name of the process listening on a local port (so we never inject into the wrong app)."""
    import psutil
    try:
        for c in psutil.net_connections(kind="tcp"):
            if c.status == psutil.CONN_LISTEN and c.laddr and c.laddr.port == port and c.pid:
                return psutil.Process(c.pid).name().lower()
    except Exception:
        pass
    return None


def _app_record(exe: str, create: bool = False) -> dict:
    with _file_lock:
        data = _load()
        rec = data["apps"].get(exe)
        if rec is None and create:
            used = {a.get("port") for a in data["apps"].values()}
            port = APP_PORT_BASE + zlib.crc32(exe.encode()) % 250
            while port in used:
                port += 1
            rec = data["apps"][exe] = {"port": port, "shortcuts": []}
            _save(data)
        return dict(rec or {})


def _update_record(exe: str, **fields) -> None:
    with _file_lock:
        data = _load()
        data["apps"].setdefault(exe, {}).update(fields)
        _save(data)


def app_pages(exe: str) -> list[dict] | None:
    """The app's debuggable pages, or None if it isn't running with our debugging port."""
    import requests
    rec = _app_record(exe)
    port = rec.get("port")
    if not port:
        return None
    try:
        targets = requests.get(f"http://127.0.0.1:{port}/json", timeout=0.8).json()
    except Exception:
        return None
    owner = _port_owner(port)
    if owner and owner != exe:
        log.warning("port %d belongs to %s, not %s; ignoring it", port, owner, exe)
        return None
    return [t for t in targets if t.get("type") == "page" and t.get("webSocketDebuggerUrl")
            and not t.get("url", "").startswith("devtools://")]


def _find_exe(exe: str) -> str | None:
    procs = _processes(exe)
    for p in procs:
        if p.info.get("exe"):
            return p.info["exe"]
    stored = _app_record(exe).get("path")
    if stored and os.path.exists(stored):
        return stored
    stem = exe[:-4]
    for base in (os.environ.get("APPDATA", ""), os.environ.get("LOCALAPPDATA", ""),
                 os.path.join(os.environ.get("LOCALAPPDATA", ""), "Programs"),
                 os.environ.get("ProgramFiles", ""), os.environ.get("ProgramFiles(x86)", "")):
        for sub in (stem, stem.capitalize(), stem.title()):
            cand = os.path.join(base, sub, exe)
            if base and os.path.exists(cand):
                return cand
    for lnk, target, _ in _shortcuts():
        if os.path.basename(target).lower() == exe or lnk.stem.lower() == stem:
            if target.lower().endswith(".exe") and os.path.exists(target):
                return target
    return None


def _shortcuts():
    """(path, target, arguments) for Start Menu, Desktop and taskbar shortcuts."""
    try:
        import pythoncom
        import win32com.client
        pythoncom.CoInitialize()
        shell = win32com.client.Dispatch("WScript.Shell")
    except Exception:
        return
    roots = [Path(os.environ.get("APPDATA", "")) / "Microsoft/Windows/Start Menu/Programs",
             Path(os.environ.get("ProgramData", "")) / "Microsoft/Windows/Start Menu/Programs",
             Path.home() / "Desktop",
             Path(os.environ.get("APPDATA", "")) / "Microsoft/Internet Explorer/Quick Launch/User Pinned/TaskBar"]
    for root in roots:
        if not root.is_dir():
            continue
        for lnk in root.rglob("*.lnk"):
            try:
                sc = shell.CreateShortcut(str(lnk))
                yield lnk, sc.TargetPath or "", sc.Arguments or ""
            except Exception:
                continue


def _patch_shortcuts(exe: str, port: int, add: bool = True) -> list[str]:
    """Make the app's shortcuts start it with (or without) the debugging port, so a tweak
    survives a normal restart. Returns the shortcuts changed."""
    import win32com.client
    flag = f"--remote-debugging-port={port}"
    changed = []
    shell = None
    for lnk, target, arguments in _shortcuts():
        if os.path.basename(target).lower() != exe:
            continue
        has = "--remote-debugging-port=" in arguments
        if add == has:
            continue
        try:
            shell = shell or win32com.client.Dispatch("WScript.Shell")
            sc = shell.CreateShortcut(str(lnk))
            sc.Arguments = (f"{arguments} {flag}".strip() if add else
                            re.sub(r"\s*--remote-debugging-port=\d+", "", arguments).strip())
            sc.Save()
            changed.append(str(lnk))
        except Exception as e:   # e.g. the all-users Start Menu needs admin rights
            log.info("couldn't update shortcut %s: %s", lnk, e)
    return changed


def _launch(path: str, args: list[str]) -> None:
    """Start an app so that it outlives Abacus (which runs inside a kill-on-close job)."""
    DETACHED, NEW_GROUP, BREAKAWAY = 0x8, 0x200, 0x01000000
    try:
        subprocess.Popen([path, *args], creationflags=DETACHED | NEW_GROUP | BREAKAWAY,
                         close_fds=True, cwd=os.path.dirname(path))
        return
    except OSError:
        pass
    try:   # the job forbids breakaway: let WMI start it, outside the job
        import pythoncom
        import win32com.client
        pythoncom.CoInitialize()
        proc = win32com.client.GetObject("winmgmts:").Get("Win32_Process")
        result = proc.Create(subprocess.list2cmdline([path, *args]), os.path.dirname(path), None)
        if result[0] == 0:
            return
    except Exception as e:
        log.info("WMI launch failed (%s); starting it as a child", e)
    subprocess.Popen([path, *args], creationflags=DETACHED | NEW_GROUP, close_fds=True,
                     cwd=os.path.dirname(path))


def app_open(app: str, restart: bool = False, confirmed: bool = False) -> str:
    """Make an app's interface reachable: start it (or restart it) with a debugging port."""
    exe = _app_name(app)
    pages = app_pages(exe)
    if pages:
        return f"{exe} is ready: {len(pages)} page(s): " + "; ".join(
            f"\"{p.get('title', '')[:50]}\"" for p in pages[:4])
    path = _find_exe(exe)
    if not path:
        return (f"Couldn't find {exe} on this PC. Use the process name shown by list_windows "
                "(for example Spotify.exe).")
    rec = _app_record(exe, create=True)
    port = rec["port"]
    if rec.get("path") != path:
        _update_record(exe, path=path)
    pages = app_pages(exe)   # first time we've had a port to look at: it may already be open
    if pages:
        return f"{exe} is ready: {len(pages)} page(s)."
    running = _processes(exe)
    if running:
        if not restart:
            return (f"{exe} is running, but without the debugging port, so its interface can't be "
                    "changed yet. Call app_ui_open again with restart=true to restart it "
                    "(tell Venu it will restart; playback or unsent drafts in it are interrupted).")
        if exe in ASK_BEFORE_RESTART and not confirmed:
            return (f"{exe} may hold unsaved work or drafts. Ask Venu if restarting it is all right, "
                    "and only then call again with restart=true and confirmed=true.")
        subprocess.run(["taskkill", "/IM", exe], capture_output=True, creationflags=0x08000000)
        deadline = time.time() + 5
        while time.time() < deadline and _processes(exe):
            time.sleep(0.3)
        for p in _processes(exe):
            with contextlib.suppress(Exception):
                p.kill()
        time.sleep(0.8)
    _launch(path, [f"--remote-debugging-port={port}"])
    deadline = time.time() + 25
    while time.time() < deadline:
        time.sleep(0.7)
        pages = app_pages(exe)
        if pages:
            time.sleep(1.5)   # let the first page finish loading
            return (f"{'Restarted' if running else 'Started'} {exe} with its interface open to "
                    f"Abacus: {len(pages)} page(s).")
    if _processes(exe):
        return (f"{exe} started but didn't open a debugging port, so it is probably not built on "
                "Chromium (or it blocks the flag). Its interface can't be restyled this way; use "
                "the app's own settings instead.")
    return f"{exe} didn't start."


def _connect(exe: str, page: str = "") -> Cdp:
    pages = app_pages(exe)
    if not pages:
        raise RuntimeError(f"{exe} isn't open to Abacus yet: call app_ui_open first.")
    hint = page.strip().lower()
    chosen = next((p for p in pages if hint and hint in f"{p.get('title')} {p.get('url')}".lower()),
                  None) or pages[0]
    return Cdp(chosen["webSocketDebuggerUrl"])


def app_eval(app: str, js: str, page: str = "") -> str:
    cdp = _connect(_app_name(app), page)
    try:
        value = cdp.evaluate(js)
    finally:
        cdp.close()
    out = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
    return out if out is not None and len(out) <= 6000 else (out or "undefined")[:6000] + " …(cut)"


TRY_CSS = """(() => { let el = document.getElementById('abacus-tweak-try');
  if (!el) { el = document.createElement('style'); el.id = 'abacus-tweak-try'; document.documentElement.appendChild(el); }
  el.textContent = %s; return 'ok'; })()"""


def app_try_css(app: str, css: str) -> str:
    """Apply CSS to the app temporarily (replaces the previous try); nothing is saved."""
    css, notes = sanitize_css(css)
    exe = _app_name(app)
    applied = 0
    for p in app_pages(exe) or []:
        cdp = Cdp(p["webSocketDebuggerUrl"])
        try:
            cdp.evaluate(TRY_CSS % json.dumps(css))
            applied += 1
        finally:
            cdp.close()
    if not applied:
        raise RuntimeError(f"{exe} isn't open to Abacus yet: call app_ui_open first.")
    return ("Trial CSS applied (not saved)" + (f"; {', '.join(notes)}" if notes else "")
            + ". Look with app_ui_screenshot, then ui_tweak_save_css to keep it; an empty css clears it.")


INSPECT = r"""((selector) => {
  const cs = e => getComputedStyle(e);
  const sel = e => e.tagName.toLowerCase() + (e.id ? '#' + e.id : '') +
      [...e.classList].slice(0, 3).map(c => '.' + c).join('') +
      (e.dataset && e.dataset.testid ? `[data-testid="${e.dataset.testid}"]` : '');
  const box = e => { const r = e.getBoundingClientRect(); return `${Math.round(r.width)}x${Math.round(r.height)}@${Math.round(r.left)},${Math.round(r.top)}`; };
  if (selector) {
    let found; try { found = [...document.querySelectorAll(selector)]; } catch (e) { return JSON.stringify({error: 'bad selector: ' + e.message}); }
    return JSON.stringify({matches: found.length, elements: found.slice(0, 12).map(e => ({
      sel: sel(e), box: box(e), text: (e.innerText || '').trim().slice(0, 60),
      role: e.getAttribute('role'), label: e.getAttribute('aria-label'),
      color: cs(e).color, background: cs(e).backgroundColor, display: cs(e).display,
      overflowY: cs(e).overflowY, html: e.outerHTML.slice(0, 260)}))});
  }
  const out = {title: document.title, url: location.href,
    htmlClass: document.documentElement.className, bodyClass: document.body ? document.body.className : '',
    colorScheme: cs(document.documentElement).colorScheme,
    bodyBackground: document.body ? cs(document.body).backgroundColor : '', bodyColor: document.body ? cs(document.body).color : '',
    viewport: innerWidth + 'x' + innerHeight};
  const vars = {};
  const walk = rules => { for (const r of rules) {
      if (r.cssRules && r.cssRules.length && !r.style) { walk(r.cssRules); continue; }
      if (!r.style) continue;
      for (const p of r.style) if (p.startsWith('--')) {
        const v = r.style.getPropertyValue(p).trim();
        if (v.length < 50 && /^(#|rgb|hsl|var\(|transparent|white|black)/i.test(v))
          (vars[p] = vars[p] || new Set()).add((r.selectorText || '').slice(0, 50) + ' = ' + v);
      } } };
  for (const s of document.styleSheets) { try { walk(s.cssRules); } catch (e) {} }
  out.themeVariables = Object.entries(vars)
    .filter(([k]) => /color|bg|background|text|foreground|surface|border|accent|base|essential|decorative|primary|secondary|highlight|elevated/i.test(k))
    .slice(0, 60).map(([k, v]) => k + ' { ' + [...v].slice(0, 2).join(' | ') + ' }');
  const regions = [], scrollers = [];
  for (const e of document.querySelectorAll('body *')) {
    const r = e.getBoundingClientRect(); const c = cs(e);
    if (/(auto|scroll)/.test(c.overflowY) && e.scrollHeight > e.clientHeight + 40 && scrollers.length < 8)
      scrollers.push(sel(e) + ' ' + box(e));
    if (regions.length >= 30 || r.width * r.height < innerWidth * innerHeight * 0.04) continue;
    if (c.backgroundColor === 'rgba(0, 0, 0, 0)' && !e.getAttribute('role') && !e.getAttribute('aria-label')) continue;
    regions.push(sel(e) + ' ' + box(e) + ' bg=' + c.backgroundColor + ' color=' + c.color +
      (e.getAttribute('aria-label') ? ' label="' + e.getAttribute('aria-label').slice(0, 30) + '"' : ''));
  }
  out.largeRegions = regions; out.scrollContainers = scrollers;
  out.abacusTweaks = [...document.querySelectorAll('style[id^="abacus-tweak-"]')].map(e => e.id);
  return JSON.stringify(out);
})(%s)"""


def app_inspect(app: str, selector: str = "", page: str = "") -> str:
    """Theme variables, large regions and scroll containers of the app's page; or, with a CSS
    selector, the elements it matches and their computed colours."""
    cdp = _connect(_app_name(app), page)
    try:
        data = json.loads(cdp.evaluate(INSPECT % json.dumps(selector.strip())))
    finally:
        cdp.close()
    if "error" in data:
        return data["error"]
    lines = []
    if selector.strip():
        lines.append(f"{data['matches']} element(s) match {selector!r}"
                     + (" (first 12 shown)" if data["matches"] > 12 else "") + ":")
        for e in data["elements"]:
            lines.append(f"- {e['sel']} {e['box']} color={e['color']} bg={e['background']} "
                         f"display={e['display']} overflow-y={e['overflowY']}"
                         + (f" role={e['role']}" if e.get("role") else "")
                         + (f" label=\"{e['label'][:40]}\"" if e.get("label") else "")
                         + (f" text=\"{e['text']}\"" if e.get("text") else ""))
            lines.append(f"    {e['html']}")
    else:
        lines.append(f"\"{data['title'][:70]}\" {data['url'][:90]} ({data['viewport']})")
        lines.append(f"html class: {data['htmlClass'][:200]}")
        lines.append(f"body class: {data['bodyClass'][:200]}")
        lines.append(f"color-scheme: {data['colorScheme']}; body background {data['bodyBackground']}, "
                     f"text {data['bodyColor']}")
        lines.append("Theme variables (name { where = value }):")
        # the variables holding actual colours are the theme's roots: list those first
        roots = sorted(data["themeVariables"], key=lambda v: "= #" not in v and "= rgb" not in v)
        lines += ["  " + v[:150] for v in roots[:45]]
        lines.append("Large regions (selector size@position colours):")
        lines += ["  " + r[:170] for r in data["largeRegions"][:24]]
        lines.append("Scroll containers:")
        lines += ["  " + r[:120] for r in data["scrollContainers"]] or ["  (none)"]
        if data.get("abacusTweaks"):
            lines.append("Abacus styles present: " + ", ".join(data["abacusTweaks"]))
    out = "\n".join(lines)
    return out if len(out) <= 9000 else out[:9000] + "\n…(cut: pass a selector to narrow it)"


def app_screenshot(app: str, page: str = "") -> tuple[bytes, tuple[int, int]]:
    from PIL import Image
    cdp = _connect(_app_name(app), page)
    try:
        shot = cdp.call("Page.captureScreenshot", format="jpeg", quality=75)
    finally:
        cdp.close()
    img = Image.open(io.BytesIO(base64.b64decode(shot["data"]))).convert("RGB")
    if img.width > MAX_SHOT_WIDTH:
        img = img.resize((MAX_SHOT_WIDTH, round(img.height * MAX_SHOT_WIDTH / img.width)))
    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=78)
    return buf.getvalue(), img.size


# The page-side installer: keeps one <style> per CSS tweak, refreshed when its text changes, and
# removes styles of tweaks that are gone. Safe to run again and again, and at document start.
INSTALLER = """(() => { const rules = %s;
  const apply = () => { const root = document.documentElement; if (!root) return false;
    const keep = new Set(['abacus-tweak-try']);
    for (const r of rules) { const id = 'abacus-tweak-' + r.id; keep.add(id);
      let el = document.getElementById(id);
      if (!el) { el = document.createElement('style'); el.id = id; root.appendChild(el); }
      if (el.getAttribute('data-h') !== r.h) { el.textContent = r.css; el.setAttribute('data-h', r.h); } }
    for (const el of document.querySelectorAll('style[id^="abacus-tweak-"]')) if (!keep.has(el.id)) el.remove();
    return true; };
  if (!apply()) new MutationObserver((m, o) => { if (apply()) o.disconnect(); }).observe(document, {childList: true, subtree: true});
  return 'ok'; })()"""
RUN_ONCE = """(() => { const w = window; w.__abacusTweaksRan = w.__abacusTweaksRan || {};
  if (w.__abacusTweaksRan[%s] === %s) return 'already'; w.__abacusTweaksRan[%s] = %s;
  %s
  ; return 'ran'; })()"""


def _app_rules(exe: str) -> list[dict]:
    return [{"id": t["id"], "css": t.get("css", ""), "js": t.get("js", ""),
             "h": format(zlib.crc32((t.get("css", "") + "\0" + t.get("js", "")).encode()), "x")}
            for t in all_tweaks() if t["kind"] == "app" and t["target"] == exe
            and t.get("enabled", True)]


def _install(cdp: Cdp, rules: list[dict]) -> None:
    cdp.evaluate(INSTALLER % json.dumps([{"id": r["id"], "css": r["css"], "h": r["h"]}
                                         for r in rules if r["css"]]))
    for r in rules:
        if r["js"]:
            try:
                cdp.evaluate(RUN_ONCE % (json.dumps(r["id"]), json.dumps(r["h"]),
                                         json.dumps(r["id"]), json.dumps(r["h"]), r["js"]))
            except Exception as e:
                log.info("script tweak %s failed: %s", r["id"], e)


def apply_app_tweaks(app: str) -> str:
    """Inject (or refresh, or clear) the saved tweaks of one app right now."""
    exe = _app_name(app)
    pages = app_pages(exe)
    if not pages:
        return f"{exe} isn't open to Abacus (no debugging port), so nothing was applied."
    rules = _app_rules(exe)
    for p in pages:
        cdp = Cdp(p["webSocketDebuggerUrl"])
        try:
            _install(cdp, rules)
        finally:
            cdp.close()
    return f"{len(rules)} tweak(s) applied to {len(pages)} page(s) of {exe}."


def persist_app(app: str) -> str:
    """After a tweak is saved or removed: keep the app's shortcuts in step with whether it has
    any tweaks (port flag on while it has some, off when the last one goes)."""
    exe = _app_name(app)
    if not getattr(config, "TWEAKS_PATCH_SHORTCUTS", True):
        return ""
    has = any(t["kind"] == "app" and t["target"] == exe for t in all_tweaks())
    rec = _app_record(exe, create=has)
    if not rec.get("port"):
        return ""
    changed = _patch_shortcuts(exe, rec["port"], add=has)
    if not changed:
        return ""
    known = set(rec.get("shortcuts", []))
    _update_record(exe, shortcuts=sorted(known | set(changed)) if has else [])
    return (f"Updated {len(changed)} shortcut(s) so {exe} starts ready for its tweaks."
            if has else f"Restored {len(changed)} shortcut(s) of {exe} to normal.")


class Watcher(threading.Thread):
    """Keeps app tweaks applied: every few seconds it visits each tweaked app that has its
    debugging port open and (re)installs the styles. Connections are kept open, and the
    installer is also registered to run at the start of every new document, so a reload inside
    the app comes up already restyled."""
    INTERVAL = 4.0

    def __init__(self) -> None:
        super().__init__(daemon=True, name="tweak-watcher")
        self.conns: dict[str, tuple[Cdp, str, str | None]] = {}   # ws url -> (cdp, rules hash, script id)

    def run(self) -> None:
        while True:
            try:
                self.tick()
            except Exception:
                log.exception("tweak watcher error")
            time.sleep(self.INTERVAL)

    def tick(self) -> None:
        data = _load()
        apps = {t["target"] for t in data["tweaks"] if t["kind"] == "app"} | set(data["apps"])
        live = set()
        for exe in apps:
            rules = _app_rules(exe)
            pages = app_pages(exe) if (rules or exe in data["apps"]) else None
            for p in pages or []:
                url = p["webSocketDebuggerUrl"]
                live.add(url)
                h = json.dumps(rules, sort_keys=True)
                conn = self.conns.get(url)
                try:
                    if conn is None:
                        if not rules:
                            continue
                        cdp, old_h, script = Cdp(url, timeout=5), None, None
                        cdp.call("Page.enable")
                    else:
                        cdp, old_h, script = conn
                    if old_h != h:
                        if script:
                            with contextlib.suppress(Exception):
                                cdp.call("Page.removeScriptToEvaluateOnNewDocument", identifier=script)
                        script = cdp.call(
                            "Page.addScriptToEvaluateOnNewDocument",
                            source=INSTALLER % json.dumps([{"id": r["id"], "css": r["css"], "h": r["h"]}
                                                           for r in rules if r["css"]])).get("identifier")
                    _install(cdp, rules)
                    self.conns[url] = (cdp, h, script)
                except Exception as e:
                    log.info("tweak watcher: %s page dropped (%s)", exe, e)
                    if conn:
                        conn[0].close()
                    self.conns.pop(url, None)
        for url in [u for u in self.conns if u not in live]:
            self.conns.pop(url)[0].close()


_services: dict = {}


def start_services() -> None:
    """Called once by the main Abacus process: the extension's local server + the app watcher."""
    if _services or not getattr(config, "UI_TWEAKS", True):
        return
    _services["http"] = serve()
    _services["watcher"] = Watcher()
    _services["watcher"].start()
    log.info("GUI tweaks: %d saved; extension server %s", len(all_tweaks()),
             f"on 127.0.0.1:{PORT}" if _services["http"] else "not started")
