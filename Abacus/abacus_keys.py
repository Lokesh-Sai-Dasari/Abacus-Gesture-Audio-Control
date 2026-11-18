"""Reliable typing for Abacus, shared by abacus.py (the "type ..." fast path) and abacus_mcp.py.

pyautogui.write presses physical keys one by one, so it depends on the keyboard layout, can't do
non-ASCII, and some apps drop keys when they arrive too fast. Instead every character is sent as
a Unicode keystroke with SendInput (KEYEVENTF_UNICODE): the app receives the exact character, in
any script, with no clipboard involved. After typing, the focused field is read back through UI
Automation; if the text didn't land, it is pasted instead (and the clipboard is restored).
"""
import ctypes
import ctypes.wintypes as wt
import logging
import time

log = logging.getLogger("abacus.keys")
user32 = ctypes.WinDLL("user32", use_last_error=True)

INPUT_KEYBOARD = 1
KEYEVENTF_KEYUP, KEYEVENTF_UNICODE = 0x2, 0x4
VK = {"\n": 0x0D, "\r": 0x0D, "\t": 0x09}
ULONG_PTR = ctypes.c_size_t


class KEYBDINPUT(ctypes.Structure):
    _fields_ = [("wVk", wt.WORD), ("wScan", wt.WORD), ("dwFlags", wt.DWORD),
                ("time", wt.DWORD), ("dwExtraInfo", ULONG_PTR)]


class MOUSEINPUT(ctypes.Structure):  # only here so the union has its real (larger) size
    _fields_ = [("dx", wt.LONG), ("dy", wt.LONG), ("mouseData", wt.DWORD), ("dwFlags", wt.DWORD),
                ("time", wt.DWORD), ("dwExtraInfo", ULONG_PTR)]


class _U(ctypes.Union):
    _fields_ = [("ki", KEYBDINPUT), ("mi", MOUSEINPUT)]


class INPUT(ctypes.Structure):
    _fields_ = [("type", wt.DWORD), ("u", _U)]


user32.SendInput.argtypes = [wt.UINT, ctypes.POINTER(INPUT), ctypes.c_int]
user32.SendInput.restype = wt.UINT
user32.VkKeyScanW.argtypes = [ctypes.c_wchar]
user32.VkKeyScanW.restype = ctypes.c_short


def _key(vk: int = 0, scan: int = 0, flags: int = 0) -> INPUT:
    return INPUT(INPUT_KEYBOARD, _U(ki=KEYBDINPUT(vk, scan, flags, 0, 0)))


def _events(text: str) -> list[INPUT]:
    out = []
    for ch in text:
        if ch in VK:
            out += [_key(VK[ch]), _key(VK[ch], flags=KEYEVENTF_KEYUP)]
            continue
        data = ch.encode("utf-16-le")  # characters beyond U+FFFF become two surrogate units
        for i in range(0, len(data), 2):
            unit = int.from_bytes(data[i:i + 2], "little")
            out += [_key(scan=unit, flags=KEYEVENTF_UNICODE),
                    _key(scan=unit, flags=KEYEVENTF_UNICODE | KEYEVENTF_KEYUP)]
    return out


def send_text(text: str, chunk: int = 40, pause: float = 0.01) -> None:
    """Type text into whatever has keyboard focus, as exact Unicode characters."""
    events = _events(text)
    for i in range(0, len(events), chunk * 2):
        batch = events[i:i + chunk * 2]
        arr = (INPUT * len(batch))(*batch)
        if user32.SendInput(len(batch), arr, ctypes.sizeof(INPUT)) != len(batch):
            raise OSError(f"SendInput blocked (error {ctypes.get_last_error()}); "
                          "is an admin window focused?")
        time.sleep(pause)


def _focused_value() -> str | None:
    """Text of the focused field via UI Automation, or None if it can't be read (passwords,
    canvases, games)."""
    try:
        import uiautomation as auto
        ctrl = auto.GetFocusedControl()
        if ctrl is None:
            return None
        for getter in ("GetValuePattern", "GetTextPattern"):
            try:
                pat = getattr(ctrl, getter)()
                if pat is None:
                    continue
                value = pat.Value if getter == "GetValuePattern" else pat.DocumentRange.GetText(-1)
                if value is not None:
                    return value
            except Exception:
                continue
    except Exception:
        pass
    return None


def _paste(text: str) -> None:
    import win32clipboard
    win32clipboard.OpenClipboard()
    try:
        try:
            old = win32clipboard.GetClipboardData(win32clipboard.CF_UNICODETEXT)
        except Exception:
            old = None
        win32clipboard.EmptyClipboard()
        win32clipboard.SetClipboardText(text, win32clipboard.CF_UNICODETEXT)
    finally:
        win32clipboard.CloseClipboard()
    press("ctrl+v")
    time.sleep(0.25)  # let the app read the clipboard before it is restored
    if old is not None:
        win32clipboard.OpenClipboard()
        try:
            win32clipboard.EmptyClipboard()
            win32clipboard.SetClipboardText(old, win32clipboard.CF_UNICODETEXT)
        finally:
            win32clipboard.CloseClipboard()


def paste_text(text: str) -> None:
    """Insert text through the clipboard in one go (the clipboard is put back afterwards).
    Used for multi-line text, where typed line breaks would be Enter presses."""
    _paste(text)


def type_text(text: str, verify: bool = True, reader=None) -> str:
    """Type text and make sure it landed. Returns 'verified', 'pasted' or 'sent (unverifiable)'.
    reader: how to read the focused field (abacus_mcp passes one that runs on its COM thread)."""
    def norm(s: str) -> str:
        return " ".join(s.split()).casefold()

    reader = reader or _focused_value
    before = reader() if verify else None
    send_text(text)
    if not verify or before is None:
        return "sent (unverifiable)"
    time.sleep(0.15)
    after = reader()
    if after is not None and norm(text) in norm(after):
        return "verified"
    if after is not None and norm(after) == norm(before):  # nothing arrived at all
        log.warning("typed text didn't land; pasting instead")
        _paste(text)
        return "pasted"
    return "sent (unverifiable)"  # field reformats its text (masks, autocomplete); leave it


NAMES = {"ctrl": 0x11, "control": 0x11, "shift": 0x10, "alt": 0x12, "win": 0x5B, "windows": 0x5B,
         "enter": 0x0D, "return": 0x0D, "tab": 0x09, "esc": 0x1B, "escape": 0x1B,
         "backspace": 0x08, "delete": 0x2E, "del": 0x2E, "space": 0x20, "up": 0x26,
         "down": 0x28, "left": 0x25, "right": 0x27, "home": 0x24, "end": 0x23,
         "pageup": 0x21, "pagedown": 0x22, "insert": 0x2D, "printscreen": 0x2C,
         "volumeup": 0xAF, "volumedown": 0xAE, "volumemute": 0xAD, "playpause": 0xB3,
         "nexttrack": 0xB0, "prevtrack": 0xB1, "capslock": 0x14}
NAMES.update({f"f{i}": 0x6F + i for i in range(1, 25)})
EXTENDED = {0x26, 0x28, 0x25, 0x27, 0x24, 0x23, 0x21, 0x22, 0x2D, 0x2E, 0x5B}


def press(combo: str) -> None:
    """Press a key or combo like 'enter', 'ctrl+shift+t', 'win+d'."""
    vks = []
    for part in [p.strip().lower() for p in combo.replace(" ", "").split("+") if p.strip()]:
        if part in NAMES:
            vks.append(NAMES[part])
        elif len(part) == 1:
            vk = user32.VkKeyScanW(part) & 0xFF
            if vk == 0xFF:
                raise ValueError(f"no key for {part!r}")
            vks.append(vk)
        else:
            raise ValueError(f"unknown key {part!r}")
    ext = lambda vk: 0x1 if vk in EXTENDED else 0  # KEYEVENTF_EXTENDEDKEY
    events = [_key(vk, flags=ext(vk)) for vk in vks]
    events += [_key(vk, flags=ext(vk) | KEYEVENTF_KEYUP) for vk in reversed(vks)]
    arr = (INPUT * len(events))(*events)
    user32.SendInput(len(events), arr, ctypes.sizeof(INPUT))
