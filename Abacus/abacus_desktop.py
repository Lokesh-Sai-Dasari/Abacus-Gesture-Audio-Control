"""Abacus's own hands: work in windows WITHOUT taking over Venu's mouse or keyboard.

Windows has one mouse pointer, and Windows 11 Home can't host a second session (no RDP host,
Sandbox or Hyper-V), so Abacus can't literally have a second pointer. Instead it uses:

- UI Automation (the accessibility API screen readers use): list a window's buttons, boxes and
  menus and press / fill / select them directly, even when the window is behind others.
- PrintWindow: capture a window's image even when it's covered.
- Posted mouse messages as a last background resort for old-style apps.
- A "ghost" ring labelled Abacus that shows where it is acting.

When an app ignores all of that, `borrowed_mouse()` lends Abacus the real pointer: it waits for
Venu to stop using it, pauses Gesture Control's pointer, and puts the cursor back afterwards.
"""
import concurrent.futures
import contextlib
import ctypes
import ctypes.wintypes as wt
import io
import logging
import queue
import threading
import time

log = logging.getLogger("abacus.desktop")
user32 = ctypes.WinDLL("user32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32")
user32.GetForegroundWindow.restype = wt.HWND
user32.WindowFromPoint.argtypes = [wt.POINT]
user32.WindowFromPoint.restype = wt.HWND
for _fn in ("IsWindowVisible", "IsIconic", "IsWindow", "GetWindowTextLengthW"):
    getattr(user32, _fn).argtypes = [wt.HWND]
user32.GetWindowTextW.argtypes = [wt.HWND, wt.LPWSTR, ctypes.c_int]
user32.GetClassNameW.argtypes = [wt.HWND, wt.LPWSTR, ctypes.c_int]
user32.GetWindowRect.argtypes = [wt.HWND, ctypes.POINTER(wt.RECT)]
user32.GetWindowLongW.argtypes = [wt.HWND, ctypes.c_int]
user32.GetWindowThreadProcessId.argtypes = [wt.HWND, ctypes.POINTER(wt.DWORD)]
user32.PrintWindow.argtypes = [wt.HWND, wt.HDC, wt.UINT]
user32.PostMessageW.argtypes = [wt.HWND, wt.UINT, wt.WPARAM, wt.LPARAM]
user32.ScreenToClient.argtypes = [wt.HWND, ctypes.POINTER(wt.POINT)]
user32.SetWindowPos.argtypes = [wt.HWND, wt.HWND, ctypes.c_int, ctypes.c_int, ctypes.c_int,
                                ctypes.c_int, wt.UINT]
user32.ShowWindow.argtypes = [wt.HWND, ctypes.c_int]
user32.SetWindowLongW.argtypes = [wt.HWND, ctypes.c_int, ctypes.c_long]
user32.GetParent.argtypes = [wt.HWND]
user32.GetParent.restype = wt.HWND
kernel32.CreateEventW.restype = wt.HANDLE

GWL_EXSTYLE, WS_EX_TOOLWINDOW, WS_EX_TRANSPARENT = -20, 0x80, 0x20
WS_EX_LAYERED, WS_EX_NOACTIVATE, WS_EX_TOPMOST = 0x80000, 0x8000000, 0x8
HWND_TOPMOST = wt.HWND(-1)
SWP_NOSIZE, SWP_NOACTIVATE, SWP_SHOWWINDOW = 0x1, 0x10, 0x40
SW_HIDE = 0
WM_MOUSEMOVE, WM_LBUTTONDOWN, WM_LBUTTONUP, WM_LBUTTONDBLCLK = 0x200, 0x201, 0x202, 0x203
WM_RBUTTONDOWN, WM_RBUTTONUP, MK_LBUTTON, MK_RBUTTON = 0x204, 0x205, 0x1, 0x2

BORROW_EVENT = "Local\\AbacusBorrowMouse"  # Gesture Control parks its pointer while this is set
IDLE_NEEDED = 0.6     # s Venu must leave mouse/keyboard alone before Abacus borrows them
IDLE_WAIT_MAX = 4.0   # s to wait for that before going ahead anyway
MAX_ELEMENTS = 250    # elements listed per read_window call (keeps replies readable)
MAX_DEPTH = 25
READ_TIMEOUT = 10.0   # s; huge windows are cut short rather than hanging the call
MAX_SHOT_WIDTH = 1568


# ---------------------------------------------------------------- windows
def _text(hwnd) -> str:
    n = user32.GetWindowTextLengthW(hwnd)
    buf = ctypes.create_unicode_buffer(n + 1)
    user32.GetWindowTextW(hwnd, buf, n + 1)
    return buf.value


def _class(hwnd) -> str:
    buf = ctypes.create_unicode_buffer(256)
    user32.GetClassNameW(hwnd, buf, 256)
    return buf.value


def _rect(hwnd) -> tuple[int, int, int, int]:
    r = wt.RECT()
    user32.GetWindowRect(hwnd, ctypes.byref(r))
    return r.left, r.top, r.right, r.bottom


def _process_name(hwnd) -> str:
    pid = wt.DWORD()
    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    try:
        import psutil
        return psutil.Process(pid.value).name()
    except Exception:
        return f"pid {pid.value}"


def _is_app_window(hwnd) -> bool:
    if not user32.IsWindowVisible(hwnd) or not _text(hwnd):
        return False
    if user32.GetWindowLongW(hwnd, GWL_EXSTYLE) & WS_EX_TOOLWINDOW:
        return False
    cloaked = ctypes.c_int(0)  # hidden UWP frames and windows on other virtual desktops
    ctypes.windll.dwmapi.DwmGetWindowAttribute(wt.HWND(hwnd), 14, ctypes.byref(cloaked), 4)
    return not cloaked.value and _class(hwnd) not in ("Progman", "WorkerW", "Shell_TrayWnd")


def list_windows() -> str:
    found = []
    cb_type = ctypes.WINFUNCTYPE(wt.BOOL, wt.HWND, wt.LPARAM)

    def cb(hwnd, _):
        if _is_app_window(hwnd):
            found.append(hwnd)
        return True
    user32.EnumWindows(cb_type(cb), 0)
    fg = user32.GetForegroundWindow()
    lines = []
    for hwnd in found:  # EnumWindows goes front to back
        l, t, r, b = _rect(hwnd)
        state = "minimized" if user32.IsIconic(hwnd) else f"at ({l},{t}) {r - l}x{b - t}"
        mark = "  <- Venu's active window" if hwnd == fg else ""
        lines.append(f"window {int(hwnd)}: \"{_text(hwnd)[:80]}\" [{_process_name(hwnd)}] {state}{mark}")
    return "\n".join(lines) or "No app windows found."


def _window(window_id: int):
    hwnd = int(window_id)
    if not user32.IsWindow(hwnd):
        raise ValueError(f"window {window_id} no longer exists; call list_windows again")
    return hwnd


def window_screenshot(window_id: int):
    """PNG/JPEG bytes of one window as it draws itself, even if other windows cover it."""
    import win32gui
    import win32ui
    from PIL import Image
    hwnd = _window(window_id)
    if user32.IsIconic(hwnd):
        raise ValueError("that window is minimized, so it has nothing to draw; "
                         "read_window still works on it")
    l, t, r, b = _rect(hwnd)
    w, h = r - l, b - t
    hdc = win32gui.GetWindowDC(int(hwnd))
    src = win32ui.CreateDCFromHandle(hdc)
    mem = src.CreateCompatibleDC()
    bmp = win32ui.CreateBitmap()
    bmp.CreateCompatibleBitmap(src, w, h)
    mem.SelectObject(bmp)
    try:
        user32.PrintWindow(hwnd, mem.GetSafeHdc(), 2)  # 2 = PW_RENDERFULLCONTENT (Chrome, UWP)
        img = Image.frombuffer("RGB", (w, h), bmp.GetBitmapBits(True), "raw", "BGRX", 0, 1)
    finally:
        win32gui.DeleteObject(bmp.GetHandle())
        mem.DeleteDC()
        src.DeleteDC()
        win32gui.ReleaseDC(int(hwnd), hdc)
    scale = min(1.0, MAX_SHOT_WIDTH / max(w, 1))
    if scale < 1:
        img = img.resize((round(w * scale), round(h * scale)))
    _shot_scale[int(window_id)] = scale
    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=80)
    return buf.getvalue(), img.size, (w, h)


_shot_scale: dict[int, float] = {}


def background_click(window_id: int, x: int, y: int, button: str = "left",
                     double: bool = False) -> str:
    """Post a click to a window at (x, y) of its window_screenshot, without moving the pointer.
    Works for classic Win32 apps; Chromium, Electron and many modern apps ignore posted clicks."""
    hwnd = _window(window_id)
    s = _shot_scale.get(int(window_id), 1.0)
    l, t, _, _ = _rect(hwnd)
    pt = wt.POINT(l + round(x / s), t + round(y / s))
    target = user32.WindowFromPoint(pt) or hwnd   # the child control actually under that point
    screen = (pt.x, pt.y)
    user32.ScreenToClient(target, ctypes.byref(pt))
    lp = (pt.y & 0xFFFF) << 16 | (pt.x & 0xFFFF)
    down, up, mk = ((WM_RBUTTONDOWN, WM_RBUTTONUP, MK_RBUTTON) if button == "right"
                    else (WM_LBUTTONDOWN, WM_LBUTTONUP, MK_LBUTTON))
    ghost.show(*screen)
    user32.PostMessageW(target, WM_MOUSEMOVE, 0, lp)
    for i in range(2 if double else 1):
        user32.PostMessageW(target, WM_LBUTTONDBLCLK if (double and i) else down, mk, lp)
        user32.PostMessageW(target, up, 0, lp)
    return (f"posted a {button} click to {_class(target)} at screen {screen}. Posted clicks are "
            "ignored by some apps: check the result with window_screenshot or read_window.")


# ---------------------------------------------------------------- UI Automation
# All UIA calls run on one dedicated thread: COM objects belong to the thread that made them,
# and element ids handed to Claude must stay valid between calls.
def _uia_init():
    import uiautomation as auto
    auto.SetGlobalSearchTimeout(2)
    _uia_state["ctx"] = auto.UIAutomationInitializerInThread()
    _uia_state["ctx"].__enter__()


_uia_state: dict = {}
_uia = concurrent.futures.ThreadPoolExecutor(1, thread_name_prefix="uia", initializer=_uia_init)
_elements: dict[str, object] = {}   # "w123.45" -> uiautomation Control
_counter = [0]

# what each control type can usually do, so a listing doesn't query every pattern of every element
ACTIONS = {
    "ButtonControl": "click", "SplitButtonControl": "click/expand", "HyperlinkControl": "click",
    "MenuItemControl": "click/expand", "CheckBoxControl": "toggle", "RadioButtonControl": "select",
    "EditControl": "set_text", "DocumentControl": "set_text", "ComboBoxControl": "expand/set_text",
    "ListItemControl": "select/click", "TabItemControl": "select", "TreeItemControl": "select/expand",
    "DataItemControl": "select/click", "MenuBarItemControl": "expand",
}
SKIP_UNNAMED = {"PaneControl", "GroupControl", "CustomControl", "TextControl", "ImageControl",
                "ToolBarControl", "SeparatorControl", "TitleBarControl", "ScrollBarControl",
                "ThumbControl", "StatusBarControl"}


def _run(fn, *args):
    return _uia.submit(fn, *args).result(timeout=READ_TIMEOUT + 5)


def read_window(window_id: int, query: str = "") -> str:
    return _run(_read_window, int(window_id), query.strip().lower())


def _read_window(window_id: int, query: str) -> str:
    import uiautomation as auto
    hwnd = _window(window_id)
    root = auto.ControlFromHandle(int(hwnd))
    if root is None:
        return "UI Automation couldn't open that window."
    while len(_elements) > 3000:  # keep ids from recent reads valid; drop the oldest
        del _elements[next(iter(_elements))]
    lines, deadline, truncated = [], time.time() + READ_TIMEOUT, False
    stack = [(root, 0)]
    while stack:
        ctl, depth = stack.pop()
        if len(lines) >= MAX_ELEMENTS or time.time() > deadline:
            truncated = True
            break
        try:
            kind, name = ctl.ControlTypeName, (ctl.Name or "").strip()
            offscreen = ctl.IsOffscreen
        except Exception:
            continue
        action = ACTIONS.get(kind, "")
        value = ""
        if kind in ("EditControl", "ComboBoxControl"):
            try:
                value = ctl.GetValuePattern().Value[:80]
            except Exception:
                pass
        listed = depth > 0 and not offscreen and (name or action or value) and not (
            kind in SKIP_UNNAMED and not name)
        if listed and query and query not in f"{name} {value} {kind}".lower():
            listed = False
        if listed:
            _counter[0] += 1
            eid = f"w{window_id}.{_counter[0]}"
            _elements[eid] = ctl
            r = ctl.BoundingRectangle
            line = f"[{eid}] {kind.removesuffix('Control')} \"{name[:70]}\""
            if value:
                line += f" value=\"{value}\""
            if action:
                line += f" -> {action}"
            if not ctl.IsEnabled:
                line += " (disabled)"
            lines.append("  " * min(depth - 1, 8) + line + f" @({r.left},{r.top},{r.width()}x{r.height()})")
        if depth < MAX_DEPTH:
            try:
                kids = ctl.GetChildren()
            except Exception:
                kids = []
            stack.extend((k, depth + 1) for k in reversed(kids))
    head = f"Window {window_id} \"{_text(hwnd)[:80]}\": {len(lines)} elements"
    if truncated:
        head += " (cut short: pass a query to narrow it)"
    return head + "\n" + "\n".join(lines)


def act(element: str, action: str, text: str = "") -> str:
    return _run(_act, element, action.strip().lower(), text)


def _act(element: str, action: str, text: str) -> str:
    import uiautomation as auto
    ctl = _elements.get(element)
    if ctl is None:
        return f"Unknown element {element}: call read_window again to get fresh ids."
    try:
        r = ctl.BoundingRectangle
        ghost.show((r.left + r.right) // 2, (r.top + r.bottom) // 2)
    except Exception:
        pass
    name = f"{ctl.ControlTypeName.removesuffix('Control')} \"{(ctl.Name or '')[:50]}\""
    P = auto.PatternId

    def pat(pid):
        try:
            return ctl.GetPattern(pid)
        except Exception:
            return None

    if action in ("click", "invoke", "press"):
        tries = [(P.InvokePattern, lambda p: p.Invoke()),
                 (P.TogglePattern, lambda p: p.Toggle()),
                 (P.SelectionItemPattern, lambda p: p.Select()),
                 (P.ExpandCollapsePattern, lambda p: p.Expand()),
                 (P.LegacyIAccessiblePattern, lambda p: p.DoDefaultAction())]
    elif action == "toggle":
        tries = [(P.TogglePattern, lambda p: p.Toggle()), (P.InvokePattern, lambda p: p.Invoke())]
    elif action == "select":
        tries = [(P.SelectionItemPattern, lambda p: p.Select()),
                 (P.InvokePattern, lambda p: p.Invoke())]
    elif action in ("expand", "collapse"):
        tries = [(P.ExpandCollapsePattern,
                  (lambda p: p.Expand()) if action == "expand" else (lambda p: p.Collapse()))]
    elif action == "set_text":
        tries = [(P.ValuePattern, lambda p: p.SetValue(text)),
                 (P.LegacyIAccessiblePattern, lambda p: p.SetValue(text))]
    elif action == "scroll_into_view":
        tries = [(P.ScrollItemPattern, lambda p: p.ScrollIntoView())]
    elif action in ("scroll_up", "scroll_down"):
        amount = auto.ScrollAmount.LargeDecrement if action == "scroll_up" else auto.ScrollAmount.LargeIncrement
        tries = [(P.ScrollPattern, lambda p: p.Scroll(auto.ScrollAmount.NoAmount, amount))]
    elif action == "read":
        val = ""
        p = pat(P.ValuePattern)
        if p:
            val = p.Value
        if not val:
            p = pat(P.TextPattern)
            if p:
                val = p.DocumentRange.GetText(4000)
        return f"{name}: {val!r}" if val else f"{name} has no readable text value."
    else:
        return ("Unknown action. Use click, toggle, select, expand, collapse, set_text, "
                "scroll_into_view, scroll_up, scroll_down or read.")
    for pid, fn in tries:
        p = pat(pid)
        if p is not None:
            try:
                fn(p)
                return f"{action} done on {name} (Venu's mouse untouched)."
            except Exception as e:
                log.info("%s via pattern %s failed: %s", action, pid, e)
    return (f"{name} doesn't support '{action}' in the background. Options: background_click on "
            "its position in window_screenshot, or the foreground click tool as a last resort.")


# ---------------------------------------------------------------- borrowing the real mouse
class _LASTINPUTINFO(ctypes.Structure):
    _fields_ = [("cbSize", wt.UINT), ("dwTime", wt.DWORD)]


def _idle_seconds() -> float:
    info = _LASTINPUTINFO(ctypes.sizeof(_LASTINPUTINFO), 0)
    user32.GetLastInputInfo(ctypes.byref(info))
    return (kernel32.GetTickCount() - info.dwTime) / 1000.0


_borrow_event = kernel32.CreateEventW(None, True, False, BORROW_EVENT)  # manual-reset


@contextlib.contextmanager
def borrowed_mouse(restore: bool = True):
    """Lend Abacus the one real pointer politely: park the gesture pointer, wait until Venu
    pauses, and put the cursor back where Venu left it."""
    # Park Gesture Control first: its pointer movements count as "input", so waiting for idle
    # before parking it could never succeed while a hand is in view.
    kernel32.SetEvent(_borrow_event)
    time.sleep(0.05)  # one camera frame for Gesture Control to let go
    waited = 0.0
    while _idle_seconds() < IDLE_NEEDED and waited < IDLE_WAIT_MAX:
        time.sleep(0.1)
        waited += 0.1
    pos = wt.POINT()
    user32.GetCursorPos(ctypes.byref(pos))
    try:
        yield
    finally:
        if restore:
            time.sleep(0.15)  # let the target app handle the click before the pointer leaves
            user32.SetCursorPos(pos.x, pos.y)
        kernel32.ResetEvent(_borrow_event)


# ---------------------------------------------------------------- ghost pointer
class Ghost:
    """A click-through, never-focused ring labelled "Abacus" that appears where Abacus acts."""
    SIZE, SHOW_FOR = 120, 1.6

    def __init__(self):
        self.q: queue.Queue = queue.Queue()
        self.started = False

    def show(self, x: int, y: int) -> None:
        if not self.started:
            self.started = True
            threading.Thread(target=self._run, daemon=True, name="ghost").start()
        self.q.put((int(x), int(y)))

    def _run(self):
        try:
            import tkinter as tk
            S = self.SIZE
            root = tk.Tk()
            root.overrideredirect(True)
            root.attributes("-topmost", True)
            root.configure(bg="#ff00ff")
            root.attributes("-transparentcolor", "#ff00ff")
            root.geometry(f"{S}x{S}+-2000+-2000")
            c = tk.Canvas(root, width=S, height=S, bg="#ff00ff", highlightthickness=0)
            c.pack()
            m = S // 2
            c.create_oval(m - 22, m - 22, m + 22, m + 22, outline="#ffb300", width=4)
            c.create_oval(m - 4, m - 4, m + 4, m + 4, fill="#ffb300", outline="")
            c.create_text(m, m + 38, text="Abacus", fill="#ffb300", font=("Segoe UI", 11, "bold"))
            root.update()
            hwnd = user32.GetParent(wt.HWND(root.winfo_id())) or wt.HWND(root.winfo_id())
            ex = user32.GetWindowLongW(hwnd, GWL_EXSTYLE)
            user32.SetWindowLongW(hwnd, GWL_EXSTYLE, ex | WS_EX_LAYERED | WS_EX_TRANSPARENT
                                  | WS_EX_TOOLWINDOW | WS_EX_NOACTIVATE | WS_EX_TOPMOST)
            user32.ShowWindow(hwnd, SW_HIDE)
            state = {"hide_at": 0.0}

            def tick():
                try:
                    while True:
                        x, y = self.q.get_nowait()
                        user32.SetWindowPos(hwnd, HWND_TOPMOST, x - m, y - m, 0, 0,
                                            SWP_NOSIZE | SWP_NOACTIVATE | SWP_SHOWWINDOW)
                        state["hide_at"] = time.time() + self.SHOW_FOR
                except queue.Empty:
                    pass
                if state["hide_at"] and time.time() > state["hide_at"]:
                    user32.ShowWindow(hwnd, SW_HIDE)
                    state["hide_at"] = 0.0
                root.after(30, tick)
            root.after(30, tick)
            root.mainloop()
        except Exception:
            log.exception("ghost pointer failed")


ghost = Ghost()
