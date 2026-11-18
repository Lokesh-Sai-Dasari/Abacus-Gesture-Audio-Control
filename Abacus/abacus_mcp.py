"""MCP server that Claude Code launches for Abacus.

- `approve` is wired to --permission-prompt-tool: every tool call not on the allowlist lands here,
  and only a human click on the Yes/No dialog lets it run.
- Background tools (abacus_desktop.py) work in windows without touching Venu's mouse:
  list_windows, read_window, act, window_screenshot, background_click.
- Foreground tools (screenshot, click, type_text, press_keys, scroll, move_mouse) borrow the real
  mouse/keyboard politely: they park Gesture Control's pointer, wait for Venu to pause, and put
  the cursor back afterwards. The mouse ones refuse while mouse control is switched off
  (config.MOUSE_CONTROL, or "Abacus, hands off my mouse").
- GUI tweak tools (abacus_tweaks.py) restyle websites and Chromium-based desktop apps and keep
  the change: ui_tweak_*, app_ui_*.

stdout is the MCP channel, so this file must never print; it logs to abacus.log instead.
"""
import ctypes
import io
import json
import logging
from pathlib import Path

HERE = Path(__file__).resolve().parent
SCALE_FILE = HERE / ".screen_scale"
MAX_SHOT_WIDTH = 1568

logging.basicConfig(
    filename=HERE / "abacus.log", level=logging.INFO,
    format="%(asctime)s [gate] %(message)s", encoding="utf-8",
)
log = logging.getLogger("abacus.gate")

try:  # real pixel coordinates on scaled (e.g. 125%) displays
    ctypes.windll.shcore.SetProcessDpiAwareness(2)
except Exception:
    pass

import pyautogui  # noqa: E402  (must come after DPI awareness)
from mcp.server.mcpserver import Image, MCPServer  # noqa: E402

import abacus_desktop as desk  # noqa: E402
import abacus_flow  # noqa: E402
import abacus_keys  # noqa: E402
import abacus_state  # noqa: E402
import abacus_tweaks as tweaks  # noqa: E402

pyautogui.FAILSAFE = True  # slam the mouse into a screen corner to abort automation
pyautogui.PAUSE = 0.05

server = MCPServer("abacus")

MB_YESNO, MB_ICONWARNING, MB_DEFBUTTON2 = 0x4, 0x30, 0x100
MB_SETFOREGROUND, MB_TOPMOST = 0x10000, 0x40000
IDYES = 6


def _say_async(text: str) -> None:
    try:
        import pythoncom
        import win32com.client
        pythoncom.CoInitialize()
        win32com.client.Dispatch("SAPI.SpVoice").Speak(text, 1)  # 1 = SVSFlagsAsync
    except Exception:
        log.exception("speech failed")


def _describe(tool_name: str, tool_input: dict) -> str:
    """Human-readable summary of exactly what is about to run."""
    short = tool_name.removeprefix("mcp__abacus__")
    if "command" in tool_input:
        body = f"Run command:\n\n{tool_input['command']}"
        if tool_input.get("description"):
            body += f"\n\n({tool_input['description']})"
    elif short in ("Write", "Edit", "MultiEdit", "NotebookEdit"):
        body = f"{short} file:\n\n{tool_input.get('file_path') or tool_input.get('notebook_path')}"
    elif short == "click":
        body = (f"{'Double-' if tool_input.get('double') else ''}{tool_input.get('button', 'left')}-click "
                f"at ({tool_input.get('x')}, {tool_input.get('y')}) on the last screenshot")
    elif short == "type_text":
        body = f"Type this text:\n\n{tool_input.get('text')}"
    elif short == "press_keys":
        body = f"Press keys: {tool_input.get('keys')}"
    elif short == "act":
        body = (f"{tool_input.get('action')} on element {tool_input.get('element')}"
                + (f"\n\nText: {tool_input.get('text')}" if tool_input.get("text") else ""))
    elif short == "background_click":
        body = (f"Background {tool_input.get('button', 'left')}-click in window "
                f"{tool_input.get('window_id')} at ({tool_input.get('x')}, {tool_input.get('y')})")
    else:
        body = f"{short}:\n\n{json.dumps(tool_input, indent=1)}"
    return body if len(body) <= 1500 else body[:1500] + "\n…(truncated)"


# structured_output=False: Claude Code requires the gate to answer with exactly one text block.
@server.tool(description="Permission gate for Abacus. Shows the user a Yes/No dialog.",
             structured_output=False)
def approve(tool_name: str, input: dict, tool_use_id: str | None = None) -> str:
    summary = _describe(tool_name, input)
    _say_async("I need your approval.")
    choice = ctypes.windll.user32.MessageBoxW(
        None, summary + "\n\nAllow this?", "Abacus wants to act",
        MB_YESNO | MB_ICONWARNING | MB_DEFBUTTON2 | MB_SETFOREGROUND | MB_TOPMOST,
    )
    allowed = choice == IDYES
    log.info("%s %s: %s", "ALLOWED" if allowed else "DENIED", tool_name,
             json.dumps(input, ensure_ascii=False)[:500])
    if allowed:
        return json.dumps({"behavior": "allow", "updatedInput": input})
    return json.dumps({"behavior": "deny", "message": "The user clicked No on the approval dialog."})


def _scale() -> tuple[float, int, int]:
    try:
        s, ox, oy = SCALE_FILE.read_text().split()
        return float(s), int(ox), int(oy)
    except Exception:
        return 1.0, 0, 0


def _real(x: int, y: int) -> tuple[int, int]:
    s, ox, oy = _scale()
    return ox + round(x / s), oy + round(y / s)


@server.tool(description="Screenshot of what is visible on screen (primary monitor, or every monitor "
                         "with all_monitors=true). Coordinates for click/move_mouse/scroll are in this "
                         "image's pixel space. To look at one window, even a covered one, prefer "
                         "window_screenshot.")
def screenshot(all_monitors: bool = False) -> list:
    from PIL import ImageGrab
    if all_monitors:
        img = ImageGrab.grab(all_screens=True)
        g = [ctypes.windll.user32.GetSystemMetrics(i) for i in (76, 77)]  # virtual screen origin
    else:
        img, g = pyautogui.screenshot(), [0, 0]
    w, h = img.size
    scale = min(1.0, MAX_SHOT_WIDTH / w)
    if scale < 1.0:
        img = img.resize((round(w * scale), round(h * scale)))
    SCALE_FILE.write_text(f"{scale} {g[0]} {g[1]}")
    buf = io.BytesIO()
    img.convert("RGB").save(buf, "JPEG", quality=80)
    return [f"Screenshot {img.size[0]}x{img.size[1]} (screen is {w}x{h}).",
            Image(data=buf.getvalue(), format="jpeg")]


# ---- background tools: Venu keeps the mouse and keyboard
@server.tool(description="List open app windows (front to back) with their window ids, and which one "
                         "Venu is using. Start here for any desktop-app task.")
def list_windows() -> str:
    return desk.list_windows()


@server.tool(description="List a window's buttons, text boxes, menus, list items etc. with element ids, "
                         "via UI Automation. Works on windows that are covered or minimized. query = "
                         "optional text filter (e.g. 'save') for big windows.")
def read_window(window_id: int, query: str = "") -> str:
    return desk.read_window(window_id, query)


@server.tool(description="Operate an element from read_window WITHOUT moving Venu's mouse or stealing "
                         "focus. action: click, toggle, select, expand, collapse, set_text (with text), "
                         "scroll_into_view, scroll_up, scroll_down, read.")
def act(element: str, action: str, text: str = "") -> str:
    return desk.act(element, action, text)


@server.tool(description="Image of one window as it draws itself, even if other windows cover it "
                         "(not minimized ones). Coordinates in it are for background_click.")
def window_screenshot(window_id: int) -> list:
    data, (w, h), (rw, rh) = desk.window_screenshot(window_id)
    return [f"Window {window_id}: image {w}x{h} (window is {rw}x{rh}).",
            Image(data=data, format="jpeg")]


@server.tool(description="Click at (x, y) of window_screenshot by posting the click straight to the "
                         "window: Venu's mouse doesn't move. Works for classic Windows apps; browsers "
                         "and many modern apps ignore it, so check the result.")
def background_click(window_id: int, x: int, y: int, button: str = "left", double: bool = False) -> str:
    return desk.background_click(window_id, x, y, button, double)


# ---- foreground tools: borrow the one real pointer, then give it back
MOUSE_OFF = ("Mouse control is switched off, so Abacus must not move Venu's pointer. Use the "
             "background tools (read_window + act, background_click, shell commands, Chrome tools). "
             "If the job truly needs the real mouse, tell Venu he can say 'Abacus, use my mouse'.")


@server.tool(description="Move the real mouse to (x, y) in screenshot coordinates (foreground; it stays "
                         "there, so use only for hover menus).")
def move_mouse(x: int, y: int) -> str:
    if not abacus_state.mouse_allowed():
        return MOUSE_OFF
    with desk.borrowed_mouse(restore=False):
        rx, ry = _real(x, y)
        desk.ghost.show(rx, ry)
        pyautogui.moveTo(rx, ry, duration=0.15)
    return "ok"


@server.tool(description="Click at (x, y) in screenshot coordinates with the REAL mouse. It waits for "
                         "Venu to pause and puts the pointer back afterwards. Refused while mouse "
                         "control is off. button: left/right/middle.")
def click(x: int, y: int, button: str = "left", double: bool = False) -> str:
    if not abacus_state.mouse_allowed():
        return MOUSE_OFF
    rx, ry = _real(x, y)
    with desk.borrowed_mouse():
        desk.ghost.show(rx, ry)
        pyautogui.click(rx, ry, clicks=2 if double else 1, button=button, interval=0.08)
    return f"clicked at screen ({rx}, {ry}); pointer returned to Venu"


@server.tool(description="Type text into the focused window with the real keyboard (foreground; prefer "
                         "act with set_text, which doesn't need focus). Sends exact Unicode characters "
                         "(any language, symbols, emoji), then reads the field back: the result says "
                         "'verified', 'pasted' (keys didn't land, so it pasted) or 'sent (unverifiable)'.")
def type_text(text: str) -> str:
    with desk.borrowed_mouse(restore=False):
        result = abacus_keys.type_text(text, reader=lambda: desk._run(abacus_keys._focused_value))
    return f"typed: {result}"


@server.tool(description="Press a key or combo in the focused window, e.g. 'enter', 'ctrl+c', 'win+d', "
                         "'alt+tab', 'f5', 'playpause' (foreground, real keyboard).")
def press_keys(keys: str) -> str:
    with desk.borrowed_mouse(restore=False):
        abacus_keys.press(keys)
    return "pressed"


@server.tool(description="Scroll: positive = up, negative = down. Optional (x, y) in screenshot coordinates.")
def scroll(amount: int, x: int | None = None, y: int | None = None) -> str:
    if not abacus_state.mouse_allowed():
        return MOUSE_OFF
    with desk.borrowed_mouse():
        if x is not None and y is not None:
            desk.ghost.show(*_real(x, y))
            pyautogui.scroll(amount * 100, *_real(x, y))
        else:
            pyautogui.scroll(amount * 100)
    return "scrolled"


VOCAB_FILE = HERE / "vocab.json"
VOCAB_LANGS = {"te", "ta", "en"}
VOCAB_MAX = 200


@server.tool(description="Teach Abacus's speech recognizer a word it got wrong (a friend's name, an app, "
                         "a place), written the way Venu says it in that language's script. "
                         "language: 'te', 'ta' or 'en'. It primes recognition from the next command on.")
def remember_word(word: str, language: str) -> str:
    word, language = word.strip(), language.strip().lower()
    if language not in VOCAB_LANGS or not word or len(word) > 40:
        return "Not saved: language must be te, ta or en, and the word 1-40 characters."
    try:
        vocab = json.loads(VOCAB_FILE.read_text("utf-8"))
    except FileNotFoundError:
        vocab = {}
    words = [w for w in vocab.get(language, []) if w != word] + [word]
    vocab[language] = words[-VOCAB_MAX:]
    tmp = VOCAB_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(vocab, ensure_ascii=False, indent=1), "utf-8")
    tmp.replace(VOCAB_FILE)
    log.info("learned word %r (%s)", word, language)
    return f"Saved '{word}' for {language}."


@server.tool(description="Save a dictation shortcut: when Venu dictates the trigger (e.g. 'my email', "
                         "'my address', 'my zoom link'), Abacus types the text instead.")
def remember_snippet(trigger: str, text: str) -> str:
    trigger = " ".join(trigger.split())
    if not trigger or len(trigger) > 40 or not text.strip() or len(text) > 2000:
        return "Not saved: the trigger must be 1-40 characters and the text 1-2000."
    abacus_flow.save_snippet(trigger, text)
    log.info("saved snippet %r", trigger)
    return f"Saved. Dictating '{trigger}' now types that text."


# ---- GUI tweaks: restyle websites and Chromium-based apps, and keep the change
def _after_save(tweak: dict, notes: list[str]) -> str:
    out = [f"Saved tweak [{tweak['id']}] for {tweak['kind']} {tweak['target']}."]
    if notes:
        out.append("Note: " + ", ".join(notes) + " (tweaks may not load remote resources).")
    if tweak["kind"] == "app":
        out.append(tweaks.apply_app_tweaks(tweak["target"]))
        out.append(tweaks.persist_app(tweak["target"]))
    else:
        out.append(tweaks.extension_status())
        out.append("Open tabs on that site pick it up within about five seconds; check with a "
                   "screenshot of the tab.")
    return " ".join(x for x in out if x)


@server.tool(description="Save a CSS tweak that permanently restyles a website or a Chromium-based desktop "
                         "app (hide elements, recolour, light/dark theme, stop infinite scroll...). target: "
                         "a site like 'youtube.com' or an app like 'app:Spotify.exe'. name: short label, "
                         "e.g. 'Hide Shorts'. Saving again with the same target and name replaces it. "
                         "Read tweaks_guide.md in the Abacus folder first.")
def ui_tweak_save_css(target: str, name: str, css: str) -> str:
    try:
        return _after_save(*tweaks.save_tweak(target, name, css=css))
    except Exception as e:
        return f"Not saved: {e}"


@server.tool(description="Save a tweak that needs JavaScript (CSS alone can't do it), optionally with CSS. "
                         "The script runs on every load of that site/app, so keep it small, idempotent, "
                         "and free of network calls. Same target/name rules as ui_tweak_save_css.")
def ui_tweak_save_script(target: str, name: str, js: str, css: str = "") -> str:
    try:
        return _after_save(*tweaks.save_tweak(target, name, css=css, js=js))
    except Exception as e:
        return f"Not saved: {e}"


@server.tool(description="List saved GUI tweaks with their ids, and whether the Chrome extension is connected.")
def ui_tweaks_list() -> str:
    return tweaks.listing() + "\n" + tweaks.extension_status()


@server.tool(description="Remove a GUI tweak by id (undo a change), or switch it off/on without deleting it "
                         "(action: 'remove', 'off' or 'on').")
def ui_tweak_change(tweak_id: str, action: str = "remove") -> str:
    action = action.strip().lower()
    if action not in ("remove", "off", "on"):
        return "action must be remove, off or on."
    t = tweaks.remove_tweak(tweak_id) if action == "remove" else tweaks.set_enabled(tweak_id, action == "on")
    if not t:
        return f"No tweak with id {tweak_id}. Ids: " + ", ".join(x["id"] for x in tweaks.all_tweaks())
    if t["kind"] == "app":
        extra = " " + tweaks.apply_app_tweaks(t["target"]) + " " + tweaks.persist_app(t["target"])
    else:
        extra = (" Open tabs drop the styling within about five seconds; a script tweak stops "
                 "after the tab reloads.")
    return f"Tweak [{tweak_id}] {'removed' if action == 'remove' else 'switched ' + action}.{extra}"


@server.tool(description="Make a Chromium-based desktop app (Spotify, Discord, VS Code, Slack, Notion...) "
                         "restylable: starts it, or with restart=true restarts it, with a local debugging "
                         "port. app: process name from list_windows, e.g. 'Spotify.exe'. Apps that may hold "
                         "unsaved work need confirmed=true, after Venu has agreed.")
def app_ui_open(app: str, restart: bool = False, confirmed: bool = False) -> str:
    try:
        return tweaks.app_open(app, restart, confirmed)
    except Exception as e:
        return f"Failed: {e}"


@server.tool(description="Look inside an opened app's interface. Without selector: theme colour variables, "
                         "large regions with their colours, scroll containers. With a CSS selector: the "
                         "matching elements, their computed colours and HTML. Use it to write the CSS.")
def app_ui_inspect(app: str, selector: str = "") -> str:
    try:
        return tweaks.app_inspect(app, selector)
    except Exception as e:
        return f"Failed: {e}"


@server.tool(description="Try CSS on an opened app without saving it (each call replaces the previous "
                         "trial; empty css clears it). Then check with app_ui_screenshot.")
def app_ui_try_css(app: str, css: str) -> str:
    try:
        return tweaks.app_try_css(app, css)
    except Exception as e:
        return f"Failed: {e}"


@server.tool(description="Picture of an opened app's interface as it currently renders (works when the "
                         "window is covered).")
def app_ui_screenshot(app: str) -> list:
    try:
        data, (w, h) = tweaks.app_screenshot(app)
    except Exception as e:
        return [f"Failed: {e}"]
    return [f"{app}: {w}x{h}", Image(data=data, format="jpeg")]


@server.tool(description="Run JavaScript inside an opened app's page and get the result (for inspection "
                         "beyond app_ui_inspect, or behaviour CSS can't change). Returns the value as JSON.")
def app_ui_eval(app: str, js: str) -> str:
    try:
        return tweaks.app_eval(app, js)
    except Exception as e:
        return f"Failed: {e}"


if __name__ == "__main__":
    server.run("stdio")
