"""Gesture Control: drive Windows with hand gestures seen by the webcam.

Gestures (as seen in the mirrored preview):
  RIGHT hand - the mouse
  * Point the index finger (others curled) .. move the pointer by tilting the finger AND/OR by
                                              moving the whole hand; slow = fine, fast = far
  * Lift the middle finger, curl it back .... left click (twice quickly = double click)
                                              Clicking into a search box / text field starts
                                              dictation: say the words and Abacus types them
                                              (CLICK_MODE = "tap": push the finger forward instead)
  * Open the hand / curl the index .......... let go of the pointer (to re-centre your hand)
  LEFT hand - navigation
  * Point index finger up / down .......... scroll up / down, continuously while held
  * Point index finger left / right ....... previous / next tab (Ctrl+Tab); repeats while held.
                                            Over the desktop/taskbar: previous / next app instead
  * Pinch thumb+index (other fingers up) .. minimize the current window
  * Fingertips bunched, then spread open .. maximize (restores the last window minimized by gesture)
  BOTH / EITHER hand
  * Both hands open, palms to the screen,
    fingers pointing outward, hold ~0.4 s .. open and hold the Alt+Tab view
      - then move one hand left/right ..... move the selection
      - tap forward with a pointed finger . open the selected application
      - remove both hands for 3 s ......... cancel the view
  * Snap fingers .......................... Spotify: resume last song if paused, else next track
  * Clap .................................. open PowerShell, run `claude`, choose Yes on the trust prompt

  VOICE
  * "Abacus, <command>" ............................ the Abacus voice assistant (../Abacus),
                                                    which starts and stops with this program

Runs until the preview window is closed.  Keys in the preview: + / - pointer speed.
Flags: --dry (no keystrokes), --selftest (camera/model check),
       --calibrate (learn your hand's jitter and tap; saved to calibration.json, then runs normally).
"""
import asyncio
import collections
import ctypes
import ctypes.wintypes as wt
import json
import logging
import math
import os
import queue
import subprocess
import sys
import threading
import time
from pathlib import Path

import cv2
import numpy as np
from mediapipe import Image, ImageFormat
from mediapipe.tasks.python import BaseOptions, vision

HERE = Path(__file__).resolve().parent
MODEL = HERE / "models" / "hand_landmarker.task"
WIN = "Gesture Control"

# ---- tuning -------------------------------------------------------------
DEVICE_EXCLUDE = ("iriun", "virtual", "voice changer", "headset", "hands-free", "stereo mix")
CAMERA_PREFER = ("hd user facing", "integrated", "webcam")  # first name match wins
MIC_PREFER = ("microphone array", "realtek", "microphone")
PREVIEW_W = 400            # preview window width (px)
SWAP_HANDEDNESS = True     # MediaPipe labels left/right backwards on this camera; flip them
EXT_RATIO = 1.15           # finger counts as extended if wrist->tip > wrist->pip * this

SPREAD_HOLD = 0.4          # s the two-hand pose must be held
SPREAD_OUT = 0.4           # outward component of finger direction (0..1)
PALM_FACING = 0.3          # |cross| needed to call the palm "facing the screen"
PALM_SIGN = 1              # flip to -1 if palms-forward is detected backwards on your camera

POINT_RATIO = 1.45         # index tip must be this much farther from the wrist than the middle tip
POINT_AXIS = 1.3           # dominant axis must beat the other by this (~37 deg cone per direction)
POINT_HOLD = 0.2           # s a direction must be held before it acts
POINT_GRACE = 0.15         # s of lost tracking tolerated before the point counts as released
SCROLL_SPEED = 8.0         # wheel notches per second when scrolling starts
SCROLL_RAMP = 1.5          # s of holding to add another 1x speed
SCROLL_MAX = 3.0           # speed cap, as a multiple of SCROLL_SPEED
TAB_REPEAT_DELAY = 0.7     # s a left/right point must be held before tab switching repeats
TAB_REPEAT = 0.35          # s between repeats after that
CAROUSEL_RESET = 5.0       # s without stepping before the desktop app order is re-read

# Right-hand pointer.  Distances are in palm lengths (wrist -> middle knuckle), so the
# feel doesn't change with how far the hand is from the camera.
GAIN_SLOW = 220            # screen px per palm length when the finger moves slowly (precision)
GAIN_FAST = 1500           # ... when it moves fast (travel)
SPEED_SLOW = 0.3           # palm lengths/s at or below which GAIN_SLOW applies
SPEED_FAST = 3.0           # ... at or above which GAIN_FAST applies (smooth blend in between)
GAIN_REF_W = 1920          # gains are for a screen this wide; scaled for others
# "angle": the pointer follows the finger's aim (turn it like a laser pointer; hand position
#          doesn't matter).  "position": it follows the fingertip's position (trackpad-like).
# "hybrid": both at once - tilting the finger moves it (angle gains) and moving the whole
#          hand moves it too (position gains, from the wrist so a tilt isn't counted twice).
POINTER_MODE = "hybrid"
HYBRID_MOVE = 1.0          # weight of hand movement in "hybrid" (0 = angle only)
ANGLE_GAIN_SLOW = 6.0      # screen px per degree of aim change when turning slowly (precision)
ANGLE_GAIN_FAST = 45.0     # ... when turning fast (travel)
ANGLE_SPEED_SLOW = 8.0     # deg/s at or below which ANGLE_GAIN_SLOW applies
ANGLE_SPEED_FAST = 120.0   # deg/s at or above which ANGLE_GAIN_FAST applies
INDEX_GRACE = 0.4          # s the index may look bent (mid-tap) before the pointer is let go
HAND_GRACE = 0.15          # s of lost tracking before the pointer is let go
# "middle": click by lifting the middle finger while the index points.
# "tap": click by pushing the pointing finger toward the screen.
CLICK_MODE = "middle"
MID_FREEZE = 0.15          # middle-finger lift progress (0 curled .. 1 click) that freezes the pointer
MID_STUCK = 1.0            # s a lift may hold the pointer still; longer means the thresholds are off
TAP_WINDOW = 0.3           # s over which a forward tap must happen
TAP_FREEZE = 0.45          # tap progress (1 = click) at which the pointer stops following
TAP_REARM = 0.3            # progress must fall back below this before the next tap counts
TAP_HOLD = 0.15            # s the pointer stays frozen after a click
TAP_REFRACT = 0.12         # s minimum between clicks (double clicks still fit)
CURSOR_HZ = 250            # pointer redraw rate; it glides between camera frames
CAL_FILE = HERE / "calibration.json"
DEFAULT_CAL = {
    "mincutoff": 1.0,      # One Euro filter: Hz of smoothing when still (lower = steadier, laggier)
    "beta": 1.5,           # ... how fast smoothing opens up with speed (higher = less lag when moving)
    "mincutoff_angle": 1.0,  # the same two, for POINTER_MODE "angle" (beta per rad/s)
    "beta_angle": 0.5,
    "speed": 1.0,          # overall pointer speed multiplier (+ / - keys)
    # middle-finger click: wrist->tip over wrist->middle joint; curled ~0.8, straight ~1.4
    "mid_up": 1.2,         # above this = lifted (click)
    "mid_down": 1.0,       # below this = curled again (ready for the next click)
    # forward-tap features and the rise (within TAP_WINDOW) that counts as a tap:
    #   push = log hand size (whole hand moves toward the camera)
    #   depth = fingertip toward the camera relative to its knuckle, in palm lengths
    "tap": {"push": 0.11, "depth": 0.35},
}

# Setup awareness.  Each monitor arrangement (laptop alone, docked to a monitor, ...) gets its
# own learned profile in profiles.json; calibration.json is the starting point for a new one.
PROFILES_FILE = HERE / "profiles.json"
SETUP_POLL = 2.0           # s between checks of the monitor arrangement
PROFILE_SAVE_EVERY = 30.0  # s: learned profiles are written at most this often (and on exit)
CAMERA_FOCAL = 0.66        # webcam focal length as a fraction of frame width (~75 deg field of view)
# Where you point relative to the camera.  Pointing at the laptop screen = toward the camera
# = 0 deg; with the laptop turned to the side of a monitor you point past it, at an angle.
YAW_MIN_HORIZ = 0.5        # aim must be at least this horizontal to say anything about yaw
YAW_MAX = 75.0             # deg, clamp
YAW_TAU_NEW = 6.0          # s of pointing: learning time constant while a profile is new...
YAW_TAU = 60.0             # ... and once it has settled
YAW_NEW_SECS = 30.0        # s of pointing after which a profile counts as learned
YAW_SIDE_ON = 10.0         # deg: camera this far to the side switches on the side-view maths...
YAW_SIDE_OFF = 6.0         # ... and it switches off again below this (hysteresis)
# Hand distance is smoothed with the same One Euro settings as hand position (so the two lag
# alike and a diagonal move keeps its shape); its extra jitter is taken out by a dead-band.
# The band is learned: 2.5x the distance jitter measured while the hand is still, so a camera
# (or light) with noisier tracking gets a wider band and a clean one keeps moves crisp.
DEPTH_BAND_START = 0.12    # palm lengths of distance jitter ignored (backlash), before it is measured
DEPTH_BAND_RANGE = (0.06, 0.3)
DEPTH_BAND_K = 2.5
# Middle-finger click thresholds keep adapting to how your lifts actually look.
ADAPT_CURLED_N = 600       # curled-finger readings kept (~20 s of pointing)
ADAPT_PEAKS_N = 20         # completed lifts kept
ADAPT_MIN_PEAKS = 5        # lifts needed before thresholds move
ADAPT_MIN_CURLED = 300     # curled readings needed before thresholds move
ADAPT_MIN_GAP = 0.15       # lifted minus curled must be at least this to trust the data
ADAPT_RATE = 0.2           # fraction of the way to the new thresholds per completed lift
ADAPT_UP_RANGE = (0.9, 2.5)
ADAPT_MIN_SPLIT = 0.08     # mid_up - mid_down is never less than this
STUCK_FIX_FRAMES = 60      # stuck-lift readings (~2 s) across >= 2 separate lifts before mid_down is raised

PINCH_CLOSE = 0.28         # thumb-index distance / palm size counted as touching
PINCH_OPEN = 0.6           # ... counted as apart
SPREAD_OPEN = 0.7          # ... counted as fully spread (for maximize)
PINCH_SPEED = 0.6          # s: pinch must close this soon after being open
BUNCH_HOLD = 0.15          # s fingertips must stay bunched before the spread

STEP = 0.08                # fraction of frame width per selection step in the Alt+Tab view
TAP_GROWTH = 1.15          # hand-size growth that counts as a forward tap in the Alt+Tab view
SW_CANCEL_NOHANDS = 3.0    # s without hands that cancels the Alt+Tab view
SW_TIMEOUT = 45.0          # s safety limit for holding Alt

SNAP_LEVEL = 0.01          # min high-pass RMS of a 10 ms mic block (quiet room is ~0.0005)
SNAP_RATIO = 10            # ... and this many times the running background level
SNAP_LOOKBACK = 0.6        # s: thumb-on-middle-finger must have been seen this soon before the click
SNAP_CONFIRM = 0.4         # s after the click for the thumb/middle contact to break
SNAP_COOLDOWN = 0.8

CLAP_LOOKBACK = 0.5        # s of two-hand history checked when a mic click arrives
CLAP_CLOSE = 2.2           # hands' centres within this many palm-lengths just before the click
CLAP_APPROACH = 0.8        # ... after closing in by at least this many palm-lengths
CLAP_COOLDOWN = 3.0
TERMINAL_CLASSES = {"CASCADIA_HOSTING_WINDOW_CLASS", "ConsoleWindowClass"}

ABACUS_DIR = HERE.parent / "Abacus"   # voice assistant; runs alongside, starts and stops with us
ABACUS_PYTHON = Path(os.environ.get("ABACUS_PYTHON", sys.executable))   # interpreter for Abacus
DICTATE_EVENT = "Local\\AbacusDictate"  # set after a click lands in a text box; Abacus waits on it
DICTATE_DELAY = 0.25       # s after a click before checking what got focus (apps update it late)

logging.basicConfig(filename=HERE / "gesture.log", level=logging.INFO,
                    format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("gesture")

# ---- Win32 --------------------------------------------------------------
user32 = ctypes.WinDLL("user32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32")
kernel32.OpenEventW.argtypes = [wt.DWORD, wt.BOOL, wt.LPCWSTR]
kernel32.OpenEventW.restype = wt.HANDLE
kernel32.WaitForSingleObject.argtypes = [wt.HANDLE, wt.DWORD]
dwmapi = ctypes.WinDLL("dwmapi")
user32.GetForegroundWindow.restype = wt.HWND
user32.GetTopWindow.argtypes = [wt.HWND]
user32.GetTopWindow.restype = wt.HWND
user32.GetWindow.argtypes = [wt.HWND, wt.UINT]
user32.GetWindow.restype = wt.HWND
user32.FindWindowW.argtypes = [wt.LPCWSTR, wt.LPCWSTR]
user32.FindWindowW.restype = wt.HWND
for _fn in ("IsWindowVisible", "IsIconic", "IsWindow", "IsZoomed", "GetWindowTextLengthW",
            "SetForegroundWindow", "BringWindowToTop"):
    getattr(user32, _fn).argtypes = [wt.HWND]
user32.ShowWindow.argtypes = [wt.HWND, ctypes.c_int]
user32.GetWindowLongW.argtypes = [wt.HWND, ctypes.c_int]
user32.GetClassNameW.argtypes = [wt.HWND, wt.LPWSTR, ctypes.c_int]
user32.GetWindowTextW.argtypes = [wt.HWND, wt.LPWSTR, ctypes.c_int]
user32.GetWindowThreadProcessId.argtypes = [wt.HWND, ctypes.POINTER(wt.DWORD)]
user32.GetWindowThreadProcessId.restype = wt.DWORD
user32.AttachThreadInput.argtypes = [wt.DWORD, wt.DWORD, wt.BOOL]
user32.MessageBoxW.argtypes = [wt.HWND, wt.LPCWSTR, wt.LPCWSTR, wt.UINT]
dwmapi.DwmGetWindowAttribute.argtypes = [wt.HWND, wt.DWORD, ctypes.c_void_p, wt.DWORD]

VK_MENU, VK_TAB, VK_SHIFT, VK_ESCAPE, VK_RETURN, VK_LWIN = 0x12, 0x09, 0x10, 0x1B, 0x0D, 0x5B
VK_DOWN, VK_CONTROL = 0x28, 0x11
MOUSEEVENTF_LEFTDOWN, MOUSEEVENTF_LEFTUP = 0x2, 0x4
SW_MAXIMIZE, SW_MINIMIZE, SW_RESTORE = 3, 6, 9
GW_HWNDNEXT, GW_OWNER = 2, 4
GWL_EXSTYLE, WS_EX_TOOLWINDOW = -20, 0x80
SHELL_CLASSES = {"Progman", "WorkerW", "Shell_TrayWnd", "Shell_SecondaryTrayWnd"}


class KEYBDINPUT(ctypes.Structure):
    _fields_ = [("wVk", wt.WORD), ("wScan", wt.WORD), ("dwFlags", wt.DWORD),
                ("time", wt.DWORD), ("dwExtraInfo", ctypes.c_size_t)]


class MOUSEINPUT(ctypes.Structure):
    _fields_ = [("dx", wt.LONG), ("dy", wt.LONG), ("mouseData", wt.DWORD), ("dwFlags", wt.DWORD),
                ("time", wt.DWORD), ("dwExtraInfo", ctypes.c_size_t)]


class INPUT(ctypes.Structure):
    class _U(ctypes.Union):
        _fields_ = [("ki", KEYBDINPUT), ("mi", MOUSEINPUT)]
    _anonymous_ = ("u",)
    _fields_ = [("type", wt.DWORD), ("u", _U)]


user32.SendInput.argtypes = [wt.UINT, ctypes.POINTER(INPUT), ctypes.c_int]
user32.SetCursorPos.argtypes = [ctypes.c_int, ctypes.c_int]
user32.WindowFromPoint.argtypes = [wt.POINT]
user32.WindowFromPoint.restype = wt.HWND
user32.GetAncestor.argtypes = [wt.HWND, wt.UINT]
user32.GetAncestor.restype = wt.HWND
user32.GetCursorPos.argtypes = [ctypes.POINTER(wt.POINT)]

own_hwnd = None
alt_held = False


def key(vk, up=False):
    user32.keybd_event(vk, user32.MapVirtualKeyW(vk, 0), 2 if up else 0, 0)


def tap(vk):
    key(vk)
    time.sleep(0.02)
    key(vk, up=True)


def alt(down):
    global alt_held
    if down != alt_held:
        key(VK_MENU, up=not down)
        alt_held = down


def type_text(text):
    """Type text as Unicode keystrokes (independent of keyboard layout)."""
    for ch in text:
        for flags in (0x4, 0x4 | 0x2):  # KEYEVENTF_UNICODE, + KEYUP
            inp = INPUT(type=1, ki=KEYBDINPUT(0, ord(ch), flags, 0, 0))
            user32.SendInput(1, ctypes.byref(inp), ctypes.sizeof(INPUT))
        time.sleep(0.01)


def scroll(delta):
    """Mouse wheel (120 = one notch); positive scrolls up. Goes to the window under the pointer."""
    inp = INPUT(type=0, mi=MOUSEINPUT(0, 0, delta & 0xFFFFFFFF, 0x0800, 0, 0))
    user32.SendInput(1, ctypes.byref(inp), ctypes.sizeof(INPUT))


def mouse_button(flags):
    inp = INPUT(type=0, mi=MOUSEINPUT(0, 0, 0, flags, 0, 0))
    user32.SendInput(1, ctypes.byref(inp), ctypes.sizeof(INPUT))


ABACUS_BORROW_EVENT = "Local\\AbacusBorrowMouse"  # set by Abacus while it borrows the real mouse
_borrow = {"handle": None, "next_try": 0.0}


def abacus_borrowing():
    """True while Abacus has borrowed the mouse (its MCP server sets a named event)."""
    h = _borrow["handle"]
    if not h:
        now = time.perf_counter()
        if now < _borrow["next_try"]:
            return False
        _borrow["next_try"] = now + 2.0  # Abacus may start after us: look again every 2 s
        h = _borrow["handle"] = kernel32.OpenEventW(0x00100000, False, ABACUS_BORROW_EVENT)  # SYNCHRONIZE
        if not h:
            return False
    return kernel32.WaitForSingleObject(h, 0) == 0  # WAIT_OBJECT_0 = signalled


def cursor_pos():
    pt = wt.POINT()
    user32.GetCursorPos(ctypes.byref(pt))
    return pt.x, pt.y


def virtual_screen():
    """(left, top, width, height) of the desktop spanning all monitors, in physical pixels."""
    return tuple(user32.GetSystemMetrics(i) for i in (76, 77, 78, 79))


def class_name(hwnd):
    buf = ctypes.create_unicode_buffer(256)
    user32.GetClassNameW(hwnd, buf, 256)
    return buf.value


def window_title(hwnd):
    buf = ctypes.create_unicode_buffer(512)
    user32.GetWindowTextW(hwnd, buf, 512)
    return buf.value


def is_app_window(hwnd):
    """A real top-level application window (what Alt+Tab would list), excluding our preview."""
    if not hwnd or hwnd == own_hwnd or not user32.IsWindowVisible(hwnd):
        return False
    if user32.GetWindowTextLengthW(hwnd) == 0 or user32.GetWindow(hwnd, GW_OWNER):
        return False
    if user32.GetWindowLongW(hwnd, GWL_EXSTYLE) & WS_EX_TOOLWINDOW:
        return False
    if class_name(hwnd) in SHELL_CLASSES:
        return False
    cloaked = ctypes.c_int(0)
    dwmapi.DwmGetWindowAttribute(hwnd, 14, ctypes.byref(cloaked), 4)  # DWMWA_CLOAKED
    return not cloaked.value


def app_windows():
    """App windows in z-order, most recently used first (minimized ones included)."""
    out, h = [], user32.GetTopWindow(None)
    while h:
        if is_app_window(h):
            out.append(h)
        h = user32.GetWindow(h, GW_HWNDNEXT)
    return out


def active_app_window():
    """Foreground window, or the top-most app window if our preview has focus."""
    fg = user32.GetForegroundWindow()
    if is_app_window(fg) and not user32.IsIconic(fg):
        return fg
    return next((h for h in app_windows() if not user32.IsIconic(h)), None)


def focus(hwnd):
    if user32.IsIconic(hwnd):
        user32.ShowWindow(hwnd, SW_RESTORE)
    if user32.SetForegroundWindow(hwnd) and user32.GetForegroundWindow() == hwnd:
        return
    # Windows blocks background processes from stealing focus. Sharing the foreground
    # thread's input state lifts that; failing that, a synthetic Alt press does.
    fg = user32.GetForegroundWindow()
    fg_thread = user32.GetWindowThreadProcessId(fg, None)
    me = kernel32.GetCurrentThreadId()
    if fg_thread and fg_thread != me and user32.AttachThreadInput(me, fg_thread, True):
        user32.BringWindowToTop(hwnd)
        user32.SetForegroundWindow(hwnd)
        user32.AttachThreadInput(me, fg_thread, False)
    if user32.GetForegroundWindow() != hwnd:
        tap(VK_MENU)
        user32.SetForegroundWindow(hwnd)


def window_under_pointer():
    x, y = cursor_pos()
    hwnd = user32.GetAncestor(user32.WindowFromPoint(wt.POINT(x, y)), 2)  # GA_ROOT
    return hwnd if is_app_window(hwnd) else None


class AppCarousel:
    """App list for stepping between windows: a snapshot that stays fixed while you keep
    stepping, so repeated steps walk through every app instead of flipping between two."""

    def __init__(self):
        self.order, self.idx, self.last_t = [], 0, float("-inf")

    def step(self, direction, t):
        self.order = [w for w in self.order if user32.IsWindow(w) and is_app_window(w)]
        current = active_app_window()
        if t - self.last_t > CAROUSEL_RESET or current not in self.order:
            self.order = app_windows()  # most recently used first, minimized ones included
            if current in self.order:
                self.order.remove(current)
                self.order.insert(0, current)
                self.idx = 0
            else:  # on the desktop: right = most recent app, left = the oldest
                self.idx = -1 if direction > 0 else 0
        self.last_t = t
        if not self.order or (current in self.order and len(self.order) < 2):
            return
        self.idx = (self.idx + direction) % len(self.order)
        focus(self.order[self.idx])


CAROUSEL = AppCarousel()


def switch_tab(step):
    # Ctrl+Tab goes to the focused window, which is often our own preview (it takes focus
    # when it opens). Send it to the window under the pointer - the one you're looking at.
    target = window_under_pointer()
    now = time.perf_counter()
    if target is None or now - CAROUSEL.last_t < 1.5:
        # desktop or taskbar under the pointer: there are no tabs, so step between apps
        # (restoring minimized ones) instead of sending Ctrl+Tab to nothing. Keep stepping
        # apps while the steps keep coming, even once an app is under the pointer.
        CAROUSEL.step(step, now)
        return
    if user32.GetForegroundWindow() != target:
        focus(target)
        time.sleep(0.05)
    key(VK_CONTROL)
    if step < 0:
        key(VK_SHIFT)
    tap(VK_TAB)
    if step < 0:
        key(VK_SHIFT, up=True)
    key(VK_CONTROL, up=True)


# ---- pointer ------------------------------------------------------------
def load_cal():
    cal = json.loads(json.dumps(DEFAULT_CAL))
    try:
        saved = json.loads(CAL_FILE.read_text())
        cal.update({k: v for k, v in saved.items() if k in cal})
    except FileNotFoundError:
        pass
    except Exception:
        log.exception("bad %s; using defaults", CAL_FILE.name)
    return cal


def save_cal(cal, report=None):
    data = dict(cal)
    if report:
        data["report"] = report
    CAL_FILE.write_text(json.dumps(data, indent=2))


def current_setup():
    """(key, friendly name) of the monitor arrangement.  Docking or undocking changes it."""
    n = user32.GetSystemMetrics(80)  # SM_CMONITORS
    vw, vh = user32.GetSystemMetrics(78), user32.GetSystemMetrics(79)
    pw, ph = user32.GetSystemMetrics(0), user32.GetSystemMetrics(1)
    return f"{n}mon {vw}x{vh} p{pw}x{ph}", ("Laptop screen" if n <= 1 else f"{n} monitors")


class Profiles:
    """Settings that depend on the desk setup, learned while you use it.

    Turning the laptop to the side of a monitor changes what the camera sees: a sideways
    hand move becomes partly a move toward or away from the camera, a left/right point
    looks foreshortened (and pointing at the monitor looks like pointing sideways), and the
    middle finger's 2-D length ratio reads differently.  Each monitor arrangement gets its
    own profile: pointer speed, click thresholds, and two learned angles -
      aim  = where the pointing finger usually aims, in camera coordinates (sets "your right")
      yaw  = that aim relative to the ray from the hand to the camera; ~0 when you point at the
             laptop screen (the camera is in it), large when you point past the laptop at a
             monitor.  It decides whether the side-view maths is needed at all, so a hand held
             off to one side of the laptop doesn't count as a turned camera.
    A profile overlays speed/mid_up/mid_down onto the shared calibration dict while active."""

    FIELDS = ("speed", "mid_up", "mid_down")

    def __init__(self, cal, path=PROFILES_FILE):
        self.cal, self.path = cal, Path(path)
        self.base = {k: cal[k] for k in self.FIELDS}  # calibration.json: start for new setups
        try:
            self.data = json.loads(self.path.read_text())
        except FileNotFoundError:
            self.data = {}
        except Exception:
            log.exception("bad %s; starting fresh", self.path.name)
            self.data = {}
        self.key = self.p = None
        self.dirty, self.saved_t = False, time.perf_counter()
        self._reset_learning()

    def _reset_learning(self):
        self.learn_t = None
        self.curled = collections.deque(maxlen=ADAPT_CURLED_N)
        self.peaks = collections.deque(maxlen=ADAPT_PEAKS_N)
        self.stuck, self.stuck_eps = [], set()
        self.logged = {}

    # -- switching
    def activate(self, key, name):
        """Make `key` the active setup; True if it changed."""
        if key == self.key:
            return False
        new = key not in self.data
        p = self.data.setdefault(key, {})
        p["name"] = name
        for k, v in (("yaw_deg", 0.0), ("aim_deg", 0.0), ("yaw_secs", 0.0), ("side", False),
                     ("peaks_n", 0)):
            p.setdefault(k, v)
        for k in self.FIELDS:
            p.setdefault(k, self.base[k])
        self.key, self.p = key, p
        self.cal.update({k: p[k] for k in self.FIELDS})
        self._reset_learning()
        self.dirty = True
        log.info("setup %s: %r (%s)", "new" if new else "known", key, self.describe())
        return True

    def set(self, k, v):
        self.p[k] = self.cal[k] = v
        self.dirty = True

    @property
    def learned(self):
        return self.p is not None and self.p["yaw_secs"] >= YAW_NEW_SECS

    def describe(self):
        if self.p is None:
            return ""
        state = "learned" if self.learned else "learning..."
        return f"{self.p['name']} | {state} | camera {self.p['yaw_deg']:+.0f} deg"

    def side_angle(self):
        """Radians of the user-right axis from camera-right when the camera is off to the
        side, else None (laptop-style: the camera's own axes are used, as before)."""
        if self.p is None or not self.p["side"]:
            return None
        return math.radians(self.p["aim_deg"])

    # -- learning the angles (called each frame the pointer is engaged)
    def learn_yaw(self, u, hand_x, hand_z, t):
        """u: unit aim, camera coords (x right in the mirrored view, y down, z away).
        hand_x / hand_z: hand position relative to the camera (any common unit)."""
        dt = 0.0 if self.learn_t is None else min(max(t - self.learn_t, 0.0), 0.1)
        self.learn_t = t
        if self.p is None or dt == 0.0 or math.hypot(u[0], u[2]) < YAW_MIN_HORIZ:
            return
        aim = math.degrees(math.atan2(u[0], -u[2]))
        # aim minus the direction from the hand to the camera (atan2(-x, z) in the same sense)
        yaw = aim + math.degrees(math.atan2(hand_x, hand_z))
        yaw = (yaw + 180) % 360 - 180
        aim, yaw = (min(max(a, -YAW_MAX), YAW_MAX) for a in (aim, yaw))
        p = self.p
        k = min(dt / (YAW_TAU_NEW if p["yaw_secs"] < YAW_NEW_SECS else YAW_TAU), 1.0)
        p["aim_deg"] += (aim - p["aim_deg"]) * k
        p["yaw_deg"] += (yaw - p["yaw_deg"]) * k
        p["yaw_secs"] += dt
        side = abs(p["yaw_deg"]) >= (YAW_SIDE_OFF if p["side"] else YAW_SIDE_ON)
        if side != p["side"]:
            p["side"] = side
            log.info("camera %s (yaw %+.0f deg, aim %+.0f deg)",
                     "to the side: side-view mode on" if side else "in front: side-view mode off",
                     p["yaw_deg"], p["aim_deg"])
        self.dirty = True

    # -- learning the middle-finger click
    def curled_sample(self, r):
        self.curled.append(r)

    def stuck_sample(self, r, episode):
        """A lift held longer than MID_STUCK: almost always a curled finger that reads above
        mid_down (from a new camera angle, or a bad calibration), so the lift never ends and
        clicks stop.  Seen across two separate lifts, raise mid_down above it."""
        self.curled.append(r)
        self.stuck.append(r)
        self.stuck_eps.add(episode)
        if len(self.stuck) >= STUCK_FIX_FRAMES and len(self.stuck_eps) >= 2:
            down = float(np.percentile(self.stuck, 90)) + 0.02
            up = max(self.cal["mid_up"], down + ADAPT_MIN_GAP)
            log.info("click unstuck: curled finger reads ~%.2f, above mid_down %.3f",
                     float(np.median(self.stuck)), self.cal["mid_down"])
            self._apply(down, up, force_log=True)
            self.stuck, self.stuck_eps = [], set()

    def lift_done(self, peak):
        self.peaks.append(peak)
        self.p["peaks_n"] += 1
        if len(self.peaks) < ADAPT_MIN_PEAKS or len(self.curled) < ADAPT_MIN_CURLED:
            return
        lo = float(np.percentile(self.curled, 90))  # curled at its most relaxed, like Calibrator
        hi = float(np.median(self.peaks))
        self.p["mid_curled"], self.p["mid_peak"] = round(lo, 3), round(hi, 3)
        gap = hi - lo
        if gap < ADAPT_MIN_GAP:
            return
        down, up = self.cal["mid_down"], self.cal["mid_up"]
        self._apply(down + ADAPT_RATE * (lo + 0.25 * gap - down),
                    up + ADAPT_RATE * (lo + 0.6 * gap - up))

    def _apply(self, down, up, force_log=False):
        up = min(max(up, ADAPT_UP_RANGE[0]), ADAPT_UP_RANGE[1])
        down = min(down, up - ADAPT_MIN_SPLIT)
        self.set("mid_down", round(down, 4))
        self.set("mid_up", round(up, 4))
        last = self.logged.get(self.key, (None, None))
        if force_log or last[0] is None or abs(down - last[0]) > 0.02 or abs(up - last[1]) > 0.02:
            self.logged[self.key] = (down, up)
            log.info("click thresholds for %s: mid_down %.3f, mid_up %.3f",
                     self.p["name"], down, up)

    def reset_click(self):
        """--calibrate just finished: its fresh thresholds win over anything learned."""
        for k in ("mid_up", "mid_down"):
            self.base[k] = self.cal[k]
            if self.p is not None:
                self.p[k] = self.cal[k]
        if self.p is not None:
            self.p.pop("mid_curled", None)
            self.p.pop("mid_peak", None)
            self.p["peaks_n"] = 0
        self.curled.clear()
        self.peaks.clear()
        self.stuck, self.stuck_eps = [], set()
        try:  # Calibrator saved the profile's speed into calibration.json; keep the base speed
            data = json.loads(CAL_FILE.read_text())
            data["speed"] = self.base["speed"]
            CAL_FILE.write_text(json.dumps(data, indent=2))
        except Exception:
            log.exception("could not restore base speed in %s", CAL_FILE.name)
        self.dirty = True

    # -- persistence
    def maybe_save(self, t):
        if self.dirty and t - self.saved_t >= PROFILE_SAVE_EVERY:
            self.save(t)

    def save(self, t=None):
        if not self.dirty:
            return
        tmp = self.path.with_suffix(".tmp")
        out = {k: {kk: (round(vv, 4) if isinstance(vv, float) else vv) for kk, vv in v.items()}
               for k, v in self.data.items()}
        tmp.write_text(json.dumps(out, indent=2))
        os.replace(tmp, self.path)
        self.dirty, self.saved_t = False, time.perf_counter() if t is None else t


class OneEuro:
    """One Euro filter (Casiez et al. 2012): heavy smoothing when slow, little lag when fast."""

    def __init__(self, mincutoff, beta, dcutoff=1.0):
        self.mincutoff, self.beta, self.dcutoff = mincutoff, beta, dcutoff
        self.reset()

    def reset(self):
        self.x = self.dx = self.t = None

    @staticmethod
    def _alpha(cutoff, dt):
        r = 2 * math.pi * cutoff * dt
        return r / (r + 1)

    def __call__(self, x, t):
        x = np.asarray(x, dtype=np.float64)
        if self.x is None:
            self.x, self.dx, self.t = x, np.zeros_like(x), t
            return self.x
        dt = max(t - self.t, 1e-3)
        self.t = t
        self.dx = self.dx + self._alpha(self.dcutoff, dt) * ((x - self.x) / dt - self.dx)
        cutoff = self.mincutoff + self.beta * float(np.linalg.norm(self.dx))
        self.x = self.x + self._alpha(cutoff, dt) * (x - self.x)
        return self.x


class Cursor:
    """Owns the real mouse pointer.  The camera gives a new target ~25x a second; a
    background thread redraws at CURSOR_HZ, gliding from where the pointer is to the new
    target over one camera frame, so motion looks continuous instead of stepping."""

    def __init__(self):
        self.lock = threading.Lock()
        self.left, self.top, self.width, self.height = virtual_screen()
        self.history = collections.deque(maxlen=128)  # (t, x, y) of targets, for click rewind
        self.span = 1 / 25
        self.resync()
        threading.Thread(target=self._run, daemon=True).start()

    def resync(self):
        """Start from wherever the pointer really is (the physical mouse may have moved it)."""
        with self.lock:
            self.shown = cursor_pos()
            self.target = self.start = np.array(self.shown, dtype=np.float64)
            self.t0 = time.perf_counter()

    def refresh_bounds(self):
        """The desktop changed (a monitor was plugged in or removed): re-read its extent, or
        the pointer would stay clipped to the old one."""
        with self.lock:
            self.left, self.top, self.width, self.height = virtual_screen()
            lo, hi = (self.left, self.top), (self.left + self.width - 1, self.top + self.height - 1)
            self.target = np.clip(self.target, lo, hi)
            self.start = np.clip(self.start, lo, hi)

    def _pos(self, now):
        k = min(1.0, (now - self.t0) / self.span)
        return self.start + (self.target - self.start) * k

    def move_by(self, dx, dy):
        with self.lock:
            now = time.perf_counter()
            self.span = 0.8 * self.span + 0.2 * min(max(now - self.t0, 0.01), 0.1)
            self.start = self._pos(now)
            self.target = np.clip(self.target + (dx, dy), (self.left, self.top),
                                  (self.left + self.width - 1, self.top + self.height - 1))
            self.t0 = now
            self.history.append((now, *self.target))

    def click(self, at_t):
        """Left click where the pointer was at at_t (just before the tap began to move it)."""
        with self.lock:
            pos = self.target
            for t, x, y in reversed(self.history):
                if t <= at_t:
                    pos = np.array((x, y))
                    break
            self.target = self.start = pos
            self.shown = (int(round(pos[0])), int(round(pos[1])))
            user32.SetCursorPos(*self.shown)
        mouse_button(MOUSEEVENTF_LEFTDOWN)
        time.sleep(0.01)
        mouse_button(MOUSEEVENTF_LEFTUP)

    def _run(self):
        period = 1 / CURSOR_HZ
        try:  # Windows sleeps in ~15 ms ticks by default, which would cap the redraw at ~65 Hz
            ctypes.WinDLL("winmm").timeBeginPeriod(1)
        except OSError:
            pass
        while True:
            time.sleep(period)
            with self.lock:
                real = cursor_pos()
                if abs(real[0] - self.shown[0]) > 2 or abs(real[1] - self.shown[1]) > 2:
                    # the physical mouse moved the pointer: follow it instead of fighting it
                    self.shown = real
                    self.target = self.start = np.array(real, dtype=np.float64)
                    continue
                p = self._pos(time.perf_counter())
                xy = (int(round(p[0])), int(round(p[1])))
                if xy != self.shown:
                    user32.SetCursorPos(*xy)
                    self.shown = xy


def aim_axes(u):
    """Unit directions that read as screen-right and screen-down for a finger aimed along u.
    Works for any aim: at the screen, straight up, or anything between."""
    right = np.array([1.0, 0.0, 0.0]) - u[0] * u
    n = np.linalg.norm(right)
    right = right / n if n > 1e-3 else np.array([0.0, 0.0, 1.0])
    return right, np.cross(right, u)


def smoothstep(x):
    x = min(max(x, 0.0), 1.0)
    return x * x * (3 - 2 * x)


class Pointer:
    """Right index finger -> mouse pointer, forward tap -> click.

    Movement is relative, like a trackpad, with speed-dependent gain: slow, careful finger
    motion moves the pointer a little (precision), quick motion moves it far (travel).
    In "hybrid" mode the finger's tilt and the hand's movement each add their own share.
    Opening the hand releases the pointer so you can re-centre without moving it."""

    def __init__(self, cal, act, dry=False, screen_w=GAIN_REF_W):
        self.cal, self.act = cal, act
        self.mode = POINTER_MODE
        self.click_mode = CLICK_MODE
        self.mids = collections.deque()  # (t, middle-finger ratio)
        self.cursor = None if dry else Cursor()
        # by the primary monitor's width, so a second monitor doesn't double the speed
        self.screen_scale = (user32.GetSystemMetrics(0) if self.cursor else screen_w) / GAIN_REF_W
        self.filter = OneEuro(cal["mincutoff_angle"], cal["beta_angle"])  # finger aim
        self.move_filter = OneEuro(cal["mincutoff"], cal["beta"])  # hand / fingertip position
        self.z_filter = OneEuro(cal["mincutoff"], cal["beta"])  # hand distance (side-view mode)
        self.adapt = None  # Profiles: learns the setup's angles and click thresholds
        self.z_noise = DEPTH_BAND_START / DEPTH_BAND_K  # learned distance jitter (palm lengths)
        self.z_slow = self.z_t = None
        self.on_click = None  # called after each real click (search-box dictation)
        self.engaged = False
        self.feats = collections.deque()
        self.total = np.zeros(2)  # screen px moved (for tests / the preview)
        self.tap_level = 0.0
        self.last_click = float("-inf")
        self.clicks = 0
        self._release()

    def _release(self):
        self.engaged = False
        self.last = self.last_move = None
        self.feats.clear()
        self.mids.clear()
        self.mid_up = False
        self.mid_start = self.mid_since = 0.0
        self.lift_peak = 0.0
        self.last_z = self.z_hold = None
        self.tap_level = 0.0
        self.armed = True
        self.frozen_until = self.settle_until = self.last_t = 0.0

    def release(self):
        self._release()

    def refresh_screen(self):
        """Monitors changed: new desktop bounds, and gains re-scaled to the new primary."""
        if self.cursor:
            self.cursor.refresh_bounds()
            self.screen_scale = user32.GetSystemMetrics(0) / GAIN_REF_W

    def update(self, h, t):
        if abacus_borrowing():
            # Abacus is using the real mouse for a moment: don't fight it for the pointer
            if self.engaged:
                self._release()
            return
        if h is None:
            if self.engaged and t - self.seen_t > HAND_GRACE:
                self._release()
            return
        if self.click_mode == "middle":
            pose = h.ext["ring"] + h.ext["pinky"] <= 1  # the middle finger is free: it clicks
        else:
            pose = h.mrp_ext <= 1  # middle/ring/pinky curled (one may be sloppy)
        if h.ext["index"] and pose:
            self.index_t = t
        elif not pose or not self.engaged or t - self.index_t > INDEX_GRACE:
            self._release()
            return
        if not self.engaged:
            self.engaged = True
            self.palm_ref = h.palm
            self.filter.reset()
            self.move_filter.reset()
            self.z_filter.reset()
            self.last_z = self.z_hold = self.z_slow = self.z_t = None
            self.lift_peak = 0.0
            self.index_t = t
            # a middle finger already up when the pointer engages isn't a click: wait for
            # it to curl first
            self.mid_up = h.mid_ratio > self.cal["mid_down"]
            self.mid_since = t
            if self.cursor:
                self.cursor.resync()
        self.seen_t = t
        self.palm_ref += 0.1 * (h.palm - self.palm_ref)
        aim = move = None  # filtered finger aim (unit 3-D) / hand position (image px)
        if self.mode in ("angle", "hybrid"):
            self.filter.mincutoff = self.cal["mincutoff_angle"]
            self.filter.beta = self.cal["beta_angle"]
            u = self.filter(h.finger_dir(), t)
            aim = u / (np.linalg.norm(u) + 1e-9)
        if self.mode in ("position", "hybrid"):
            self.move_filter.mincutoff = self.cal["mincutoff"]
            self.move_filter.beta = self.cal["beta"] / self.palm_ref  # beta is per palm length/s
            # the wrist barely moves when the finger tilts, so in hybrid the two don't overlap
            move = self.move_filter(h.p[0] if self.mode == "hybrid" else h.p[8], t)
        w = getattr(h, "w", None)
        side = self.adapt.side_angle() if self.adapt else None
        z = None
        if move is not None and side is not None and w:
            # Camera off to the side: part of a sideways hand move is toward/away from it.
            # Distance from the camera in palm lengths (a smaller palm = farther away).
            fpx = CAMERA_FOCAL * w
            if getattr(h, "img_scale", None) and getattr(h, "world_palm", None):
                z_raw = fpx / h.img_scale / h.world_palm
            else:
                z_raw = fpx / h.palm
            self.z_filter.mincutoff = self.cal["mincutoff"]
            self.z_filter.beta = self.cal["beta"]  # z is already in palm lengths
            z = float(self.z_filter(z_raw, t))
            # Learn the jitter: how far the raw distance strays from a 1 s average while the
            # hand is (nearly) still sideways.  (Raw, because noise also opens the One Euro
            # filter's cutoff, so the smoothed value is no measure of what gets through.)
            dtz = 0.0 if self.z_t is None else min(max(t - self.z_t, 0.0), 0.1)
            self.z_t = t
            self.z_slow = z_raw if self.z_slow is None else self.z_slow + (z_raw - self.z_slow) * min(dtz / 1.0, 1.0)
            if float(np.linalg.norm(self.move_filter.dx)) / self.palm_ref < SPEED_SLOW:
                self.z_noise += (abs(z_raw - self.z_slow) - self.z_noise) * min(dtz / 3.0, 1.0)
            band = min(max(DEPTH_BAND_K * self.z_noise, DEPTH_BAND_RANGE[0]), DEPTH_BAND_RANGE[1])
            # backlash: distance has to change by more than its jitter before it counts
            if self.z_hold is None:
                self.z_hold = z
            elif abs(z - self.z_hold) > band:
                self.z_hold = z - math.copysign(band, z - self.z_hold)
            z = self.z_hold
        if aim is not None and self.adapt and w:
            fpx = CAMERA_FOCAL * w
            self.adapt.learn_yaw(aim, (h.p[0][0] - w / 2) / fpx, 1.0, t)

        if self.click_mode == "middle":
            click, start_t, freeze = self._middle(h, t)
        else:
            level, start_t, motion = self._tap(h, t)
            click, freeze = level >= 1 and self.armed, motion >= TAP_FREEZE
            if click:
                self.armed = False
            elif level < TAP_REARM:
                self.armed = True
        settling = False
        if t < self.settle_until:
            # After a click the smoothing filter is still catching up with the finger's
            # return motion; letting that through would drift the pointer off the target.
            # Stay still until the filtered aim (and hand) have (nearly) stopped.
            dt = max(t - self.last_t, 1e-3)
            if aim is not None and self.last is not None:
                step = float(np.linalg.norm(aim - self.last)) / dt
                settling |= math.degrees(step) > 0.25 * ANGLE_SPEED_SLOW
            if move is not None and self.last_move is not None:
                step = float(np.linalg.norm(move - self.last_move)) / dt
                settling |= step / self.palm_ref > 0.25 * SPEED_SLOW
        frozen = freeze or settling or t < self.frozen_until or not h.ext["index"]
        self.last_t = t
        if not frozen:
            delta = np.zeros(2)
            if aim is not None and self.last is not None:
                # turn of the aim, split into screen-right / screen-down components (radians)
                right, down = aim_axes(aim)
                d = np.array([(aim - self.last) @ right, (aim - self.last) @ down])
                speed = math.degrees(float(np.linalg.norm(self.filter.dx)))
                k = smoothstep((speed - ANGLE_SPEED_SLOW) / (ANGLE_SPEED_FAST - ANGLE_SPEED_SLOW))
                delta += d * math.degrees(1) * (ANGLE_GAIN_SLOW + (ANGLE_GAIN_FAST - ANGLE_GAIN_SLOW) * k)
            if move is not None and self.last_move is not None:
                d = (move - self.last_move) / self.palm_ref
                speed = float(np.linalg.norm(self.move_filter.dx)) / self.palm_ref
                if z is not None and self.last_z is not None:
                    # Full 3-D hand position in palm lengths (X = image offset * distance / focal),
                    # so moving away from the camera doesn't also slide the hand toward the
                    # image centre.  Your right = (cos a, 0, sin a) in camera coordinates.
                    fpx = CAMERA_FOCAL * w
                    ctr = np.array([w / 2, getattr(h, "img_h", 0.75 * w) / 2])
                    cur = np.append((move - ctr) * z / fpx, z)
                    prev = np.append((self.last_move - ctr) * self.last_z / fpx, self.last_z)
                    c, sn = math.cos(side), math.sin(side)
                    dd = cur - prev
                    d = np.array([c * dd[0] + sn * dd[2], dd[1]])
                    dz = float(self.z_filter.dx) if dd[2] else 0.0  # inside the dead-band: still
                    v = self.move_filter.dx * z / fpx + (move - ctr) * dz / fpx
                    speed = math.hypot(c * float(v[0]) + sn * dz, float(v[1]))
                k = smoothstep((speed - SPEED_SLOW) / (SPEED_FAST - SPEED_SLOW))
                weight = HYBRID_MOVE if self.mode == "hybrid" else 1.0
                delta += d * (GAIN_SLOW + (GAIN_FAST - GAIN_SLOW) * k) * weight
            delta *= self.cal["speed"] * self.screen_scale
            self.total += delta
            if self.cursor and delta.any():
                self.cursor.move_by(*delta)
        self.last, self.last_move, self.last_z = aim, move, z

        if click and t - self.last_click > TAP_REFRACT:
            self.last_click, self.frozen_until = t, t + TAP_HOLD
            self.settle_until = self.frozen_until + 0.5
            self.clicks += 1
            label = "Middle lift: click" if self.click_mode == "middle" else "Tap: click"
            self.act(label, t, (lambda: self._click(start_t)) if self.cursor else None)

    def _click(self, start_t):
        self.cursor.click(start_t)
        if self.on_click:
            self.on_click()

    def _middle(self, h, t):
        """Middle-finger lift.  Clicks once as the finger straightens past mid_up; the next
        click needs it curled below mid_down first.  The pointer holds still from the
        moment the finger starts rising until it is curled again, and the click lands
        where the pointer was when the lift began - lifting a finger tugs the index a bit."""
        r = h.mid_ratio
        self.mids.append((t, r))
        while t - self.mids[0][0] > TAP_WINDOW:
            self.mids.popleft()
        up, down = self.cal["mid_up"], self.cal["mid_down"]
        lo, lo_t = min((v, tt) for tt, v in self.mids)
        self.tap_level = min(max((r - down) / (up - down), 0.0), 1.0)
        click = False
        if not self.mid_up and r > up:
            self.mid_up, click = True, True
            self.mid_start, self.mid_since = lo_t, t
            self.lift_peak = r
        elif self.mid_up and r < down:
            self.mid_up = False
            self.frozen_until = t + TAP_HOLD  # lowering the finger tugs the index too
            self.settle_until = self.frozen_until + 0.5
            if self.adapt and self.lift_peak:  # a real (clicking) lift just finished
                self.adapt.lift_done(self.lift_peak)
            self.lift_peak = 0.0
        if self.mid_up and self.lift_peak:
            self.lift_peak = max(self.lift_peak, r)
        rising = (r - lo) / (up - down) >= MID_FREEZE and r > down
        # A "lift" that never ends means your curled finger sits above mid_down (bad
        # calibration): stop holding the pointer rather than freezing it for good.
        held = self.mid_up and t - self.mid_since < MID_STUCK
        if self.adapt:
            if self.mid_up and not held:
                self.adapt.stuck_sample(r, self.mid_since)
            elif not self.mid_up and not rising:
                self.adapt.curled_sample(r)
        return click, self.mid_start if click else lo_t, held or (rising and not self.mid_up)

    def _tap(self, h, t):
        """Tap progress: the largest feature rise within TAP_WINDOW over its threshold
        (1.0 = click), when that rise started, and the same measure counting the finger's
        return stroke too (the pointer stays still for both halves of a tap)."""
        f = h.tap_features()
        self.feats.append((t, f))
        while t - self.feats[0][0] > TAP_WINDOW:
            self.feats.popleft()
        level, start, motion = 0.0, t, 0.0
        for name, thr in self.cal["tap"].items():
            lo, lo_t = min((ff[name], tt) for tt, ff in self.feats)
            hi = max(ff[name] for _, ff in self.feats)
            r = (f[name] - lo) / thr
            motion = max(motion, r, (hi - f[name]) / thr)
            if r > level:
                level, start = r, lo_t
        self.tap_level = level
        return level, start, motion


# ---- devices ------------------------------------------------------------
def pick(names, prefer):
    """Index of the best device name: skip excluded ones, then take the first preferred match."""
    ok = [(i, n) for i, n in enumerate(names) if not any(x in n.lower() for x in DEVICE_EXCLUDE)]
    for word in prefer:
        for i, n in ok:
            if word in n.lower():
                return i
    return ok[0][0] if ok else None


def open_camera():
    try:
        from pygrabber.dshow_graph import FilterGraph
        names = FilterGraph().get_input_devices()  # same order as OpenCV's DirectShow indices
    except Exception:
        log.exception("camera enumeration failed")
        names = []
    idx = pick(names, CAMERA_PREFER) if names else 0
    if idx is None:
        fail("No usable webcam found (only excluded devices: %s)." % ", ".join(names))
    log.info("camera %d: %s (of %s)", idx, names[idx] if names else "?", names)
    cap = cv2.VideoCapture(idx, cv2.CAP_DSHOW)
    cap.set(cv2.CAP_PROP_FOURCC, cv2.VideoWriter_fourcc(*"MJPG"))  # raw YUY2 often caps at 15-20 fps
    cap.set(cv2.CAP_PROP_FRAME_WIDTH, 640)
    cap.set(cv2.CAP_PROP_FRAME_HEIGHT, 480)
    cap.set(cv2.CAP_PROP_FPS, 30)
    cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)  # always the newest frame: less pointer lag
    return cap, names[idx] if names else f"camera {idx}"


class SnapListener:
    """Listens on the microphone for sharp clicks (snaps, claps); onset times land in self.onsets."""

    def __init__(self):
        import sounddevice as sd
        devices = [(i, d) for i, d in enumerate(sd.query_devices())
                   if d["max_input_channels"] > 0
                   and "WASAPI" in sd.query_hostapis(d["hostapi"])["name"]]
        choice = pick([d["name"] for _, d in devices], MIC_PREFER)
        if choice is None:
            raise RuntimeError("no usable microphone")
        dev, info = devices[choice]
        self.name = info["name"]
        rate = int(info["default_samplerate"])
        self.onsets = collections.deque(maxlen=32)
        self.level = 0.0
        self._base, self._prev, self._last, self._last_onset = None, 0.0, 0.0, 0.0
        self.stream = sd.InputStream(device=dev, channels=1, samplerate=rate, blocksize=rate // 100,
                                     dtype="float32", callback=self._callback)
        self.stream.start()
        log.info("microphone: %s @ %d Hz", self.name, rate)

    def _callback(self, indata, frames, time_info, status):
        x = indata[:, 0]
        hp = np.diff(x, prepend=self._last)  # first difference ~ high-pass: keeps the click, drops hum
        self._last = float(x[-1])
        e = float(np.sqrt(np.mean(hp * hp)))
        now = time.perf_counter()
        if self._base is None:
            self._base = e
        if (e > SNAP_LEVEL and e > SNAP_RATIO * self._base and e > 3 * self._prev
                and now - self._last_onset > 0.15):
            self.onsets.append(now)
            self._last_onset = now
        elif e < SNAP_RATIO * self._base:
            self._base = 0.98 * self._base + 0.02 * e
        self._prev = e
        self.level = e

    def drain(self):
        out = []
        while self.onsets:
            out.append(self.onsets.popleft())
        return out

    def close(self):
        self.stream.stop()
        self.stream.close()


class Spotify:
    """Snap handler: resume Spotify if it isn't playing, otherwise skip to the next track."""

    EXE = Path(os.environ.get("APPDATA", "")) / "Spotify" / "Spotify.exe"
    VK_MEDIA_PLAY_PAUSE = 0xB3

    def __init__(self):
        self.q = queue.Queue()
        self.status = ""
        threading.Thread(target=self._run, daemon=True).start()

    def snap(self):
        self.q.put("snap")

    def _run(self):
        while True:
            self.q.get()
            try:
                asyncio.run(self._snap())
            except Exception as exc:
                log.exception("spotify control failed")
                self.status = f"Spotify error: {exc}"

    @staticmethod
    async def _session():
        from winrt.windows.media.control import GlobalSystemMediaTransportControlsSessionManager as Mgr
        mgr = await Mgr.request_async()
        for s in mgr.get_sessions():
            if "spotify" in (s.source_app_user_model_id or "").lower():
                return s
        return None

    async def _snap(self):
        from winrt.windows.media.control import (
            GlobalSystemMediaTransportControlsSessionPlaybackStatus as Status)
        session = await self._session()
        if session is None:
            self.status = "Launching Spotify..."
            if self.EXE.exists():
                subprocess.Popen([str(self.EXE)])
            else:
                os.startfile("spotify:")
            for _ in range(40):
                await asyncio.sleep(0.5)
                session = await self._session()
                if session:
                    break
            if session is None:  # never registered with Windows media controls: use the media key
                tap(self.VK_MEDIA_PLAY_PAUSE)
                self.status = "Spotify: play (media key)"
                return
            await asyncio.sleep(1.5)  # give it a moment to load the last track
        if session.get_playback_info().playback_status == Status.PLAYING:
            await session.try_skip_next_async()
            self.status = "Spotify: next track"
        else:
            await session.try_play_async()
            self.status = "Spotify: play"
        log.info(self.status)


class ClaudeLauncher:
    """Clap handler: Start search -> PowerShell -> `claude` -> accept the folder-trust prompt.

    Every keystroke after the search box goes only to a window confirmed to be PowerShell;
    if that window doesn't show up or loses focus, the sequence stops.
    """

    def __init__(self):
        self.busy = False
        self.status = ""

    def launch(self):
        if not self.busy:
            self.busy = True
            threading.Thread(target=self._run, daemon=True).start()

    def _say(self, msg):
        self.status = msg
        log.info(msg)

    def _run(self):
        try:
            self._say("Clap: opening PowerShell...")
            key(VK_LWIN)
            tap(ord("S"))
            key(VK_LWIN, up=True)
            time.sleep(0.9)
            type_text("powershell")
            time.sleep(1.2)
            tap(VK_RETURN)

            ps = None
            deadline = time.perf_counter() + 12
            while time.perf_counter() < deadline:
                fg = user32.GetForegroundWindow()
                if class_name(fg) in TERMINAL_CLASSES and "powershell" in window_title(fg).lower():
                    ps = fg
                    break
                time.sleep(0.2)
            if ps is None:
                return self._say("Clap: PowerShell didn't open - stopped")
            time.sleep(1.5)  # let the prompt load

            if user32.GetForegroundWindow() != ps:
                return self._say("Clap: PowerShell lost focus - stopped")
            type_text("claude")
            tap(VK_RETURN)
            self._say("Clap: starting Claude...")
            time.sleep(6.0)  # Claude Code start-up, then the "trust this folder?" prompt

            if user32.GetForegroundWindow() != ps:
                return self._say("Clap: PowerShell lost focus - skipped 'Yes'")
            tap(VK_DOWN)  # the trust prompt's first option is "No": move to "Yes", then confirm
            time.sleep(0.3)
            tap(VK_RETURN)
            self._say("Clap: Claude is open")
        except Exception as exc:
            log.exception("clap sequence failed")
            self.status = f"Clap error: {exc}"
        finally:
            self.busy = False


class _JobLimits(ctypes.Structure):  # JOBOBJECT_EXTENDED_LIMIT_INFORMATION
    _fields_ = [("PerProcessUserTimeLimit", ctypes.c_int64), ("PerJobUserTimeLimit", ctypes.c_int64),
                ("LimitFlags", wt.DWORD), ("MinimumWorkingSetSize", ctypes.c_size_t),
                ("MaximumWorkingSetSize", ctypes.c_size_t), ("ActiveProcessLimit", wt.DWORD),
                ("Affinity", ctypes.c_size_t), ("PriorityClass", wt.DWORD),
                ("SchedulingClass", wt.DWORD), ("IoCounters", ctypes.c_uint64 * 6),
                ("ProcessMemoryLimit", ctypes.c_size_t), ("JobMemoryLimit", ctypes.c_size_t),
                ("PeakProcessMemoryUsed", ctypes.c_size_t), ("PeakJobMemoryUsed", ctypes.c_size_t)]


class Abacus:
    """The Abacus voice assistant ("Hey Abacus"), run as a hidden child process.

    It goes in a Windows job object that kills everything inside when our handle closes, so
    Abacus (and any Claude command it is running) stops with us even if we crash.
    Abacus itself refuses to start twice, so its own Desktop shortcut can't double it up.
    """

    JOB_KILL_ON_CLOSE = 0x2000
    JOB_EXTENDED_LIMITS = 9

    def __init__(self):
        self.proc = None
        self.job = None
        script = ABACUS_DIR / "abacus.py"
        if not (script.exists() and ABACUS_PYTHON.exists()):
            log.warning("Abacus not found (%s / %s); voice disabled", script, ABACUS_PYTHON)
            return
        self.proc = subprocess.Popen(
            [str(ABACUS_PYTHON), str(script)], cwd=ABACUS_DIR,
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            creationflags=subprocess.CREATE_NO_WINDOW)
        try:
            k32 = ctypes.WinDLL("kernel32", use_last_error=True)
            k32.CreateJobObjectW.restype = wt.HANDLE
            k32.SetInformationJobObject.argtypes = [wt.HANDLE, ctypes.c_int, ctypes.c_void_p, wt.DWORD]
            k32.AssignProcessToJobObject.argtypes = [wt.HANDLE, wt.HANDLE]
            self.job = k32.CreateJobObjectW(None, None)
            limits = _JobLimits(LimitFlags=self.JOB_KILL_ON_CLOSE)
            if not (self.job
                    and k32.SetInformationJobObject(self.job, self.JOB_EXTENDED_LIMITS,
                                                    ctypes.byref(limits), ctypes.sizeof(limits))
                    and k32.AssignProcessToJobObject(self.job, int(self.proc._handle))):
                log.warning("Abacus job object failed (error %d); it may outlive a crash",
                            ctypes.get_last_error())
        except Exception:
            log.exception("Abacus job object failed; it may outlive a crash")
        log.info("Abacus started (pid %d)", self.proc.pid)

    @property
    def status(self):
        code = self.proc.poll() if self.proc else None
        # 0 = clean exit, e.g. another copy was already running
        return f"Abacus stopped (code {code}) - see abacus.log" if code not in (None, 0) else ""

    def stop(self):
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()
            log.info("Abacus stopped")
        if self.job:
            kernel32.CloseHandle(wt.HANDLE(self.job))  # kills anything Abacus started, too


class SearchDictation:
    """After a gesture click, if keyboard focus landed in a text box (a search bar, the
    browser's address bar...), signal Abacus to take dictation: it records what you say next
    and types it in.  Focus is read with UI Automation, on a thread so frames never wait on
    a slow app.  Password boxes, read-only fields and documents (Word, Notepad) never count."""

    EDIT, COMBO_BOX = 50004, 50003  # UIA control types
    VALUE_PATTERN = 10002

    def __init__(self):
        kernel32.CreateEventW.argtypes = [ctypes.c_void_p, wt.BOOL, wt.BOOL, wt.LPCWSTR]
        kernel32.CreateEventW.restype = wt.HANDLE
        kernel32.SetEvent.argtypes = [wt.HANDLE]
        self.event = kernel32.CreateEventW(None, False, False, DICTATE_EVENT)  # auto-reset
        self.clicks = queue.Queue()
        threading.Thread(target=self._run, daemon=True).start()

    def clicked(self):
        self.clicks.put(None)

    def _run(self):
        import comtypes
        import comtypes.client
        comtypes.CoInitialize()
        uia_lib = comtypes.client.GetModule("UIAutomationCore.dll")
        uia = comtypes.client.CreateObject(uia_lib.CUIAutomation, interface=uia_lib.IUIAutomation)
        while True:
            self.clicks.get()
            time.sleep(DICTATE_DELAY)
            while not self.clicks.empty():  # a double click is checked once
                self.clicks.get_nowait()
            try:
                # Chromium builds its accessibility tree only once a UIA client asks, so the
                # very first look can see just the page; look once more before giving up.
                field = self._text_field(uia, uia_lib)
                if field is None:
                    time.sleep(0.2)
                    field = self._text_field(uia, uia_lib)
            except Exception:
                log.exception("focus check failed")
                continue
            if field is not None:
                log.info("Clicked into text box %r: dictation", field)
                kernel32.SetEvent(self.event)

    def _text_field(self, uia, lib):
        return self.describe(uia.GetFocusedElement(), lib)

    @classmethod
    def describe(cls, el, lib):
        """Name of the element if it is an editable single-line text box, else None."""
        if not el or el.CurrentIsPassword:
            return None
        kind = el.CurrentControlType
        if kind not in (cls.EDIT, cls.COMBO_BOX):
            return None
        pattern = el.GetCurrentPattern(cls.VALUE_PATTERN)
        if pattern:
            if pattern.QueryInterface(lib.IUIAutomationValuePattern).CurrentIsReadOnly:
                return None
        elif kind == cls.COMBO_BOX:  # a plain drop-down list, nothing to type into
            return None
        return el.CurrentName or el.CurrentClassName or ""


# ---- hand geometry ------------------------------------------------------
class Hand:
    def __init__(self, landmarks, w, h, handed=None, conf=1.0, world=None):
        p = np.array([[l.x * w, l.y * h] for l in landmarks], dtype=np.float32)
        p3 = np.array([[l.x * w, l.y * h, (getattr(l, "z", 0.0) or 0.0) * w] for l in landmarks],
                      dtype=np.float32)
        self.p, self.p3 = p, p3
        self.w, self.img_h = w, h
        # metric 3-D landmarks (MediaPipe's world landmarks) give the truest finger aim
        self.world = (np.array([[l.x, l.y, l.z] for l in world], dtype=np.float64)
                      if world else None)
        self.handed, self.hand_conf = handed, conf
        # Image scale (px per metre) fitted over all 21 points: MediaPipe's metric landmarks,
        # flattened the way the camera sees them, against the image.  Far steadier than one
        # palm length and not fooled by turning the hand, so it gives a usable distance from
        # the camera (side-view mode).  x and y are fitted apart in case one axis is mirrored.
        self.img_scale = self.world_palm = None
        if self.world is not None:
            wc = self.world[:, :2] - self.world[:, :2].mean(axis=0)
            pc = p.astype(np.float64) - p.mean(axis=0)
            den = float((wc * wc).sum())
            if den > 1e-12:
                self.img_scale = (abs(float(pc[:, 0] @ wc[:, 0])) + abs(float(pc[:, 1] @ wc[:, 1]))) / den
                self.world_palm = float(np.linalg.norm(self.world[0] - self.world[9])) or None

        def d(a, b):
            return float(np.linalg.norm(p[a] - p[b]))

        self.palm = max(d(0, 9), 1.25 * d(5, 17), 1e-3)
        self.ext = {name: d(0, tip) > d(0, pip) * EXT_RATIO
                    for name, tip, pip in (("index", 8, 6), ("middle", 12, 10),
                                           ("ring", 16, 14), ("pinky", 20, 18))}
        self.open = all(self.ext.values())
        self.mrp_ext = self.ext["middle"] + self.ext["ring"] + self.ext["pinky"]
        self.mid_ratio = d(0, 12) / max(d(0, 10), 1e-3)
        # pointer pose: index out, ring and pinky (mostly) curled; the middle is free (it clicks)
        self.pointer_pose = self.ext["index"] and self.ext["ring"] + self.ext["pinky"] <= 1
        self.pinch = d(4, 8) / self.palm
        # Pointing: middle/ring/pinky curled, index clearly longer than them.
        self.pointing = self.mrp_ext == 0 and d(0, 8) > d(0, 12) * 1.1
        # Loaded snap: thumb pressed to the middle fingertip (closer than to the index),
        # ring and pinky folded.
        self.snap_ready = (d(4, 12) / self.palm < 0.35 and d(4, 12) < d(4, 8)
                           and not self.ext["ring"] and not self.ext["pinky"])
        self.thumb_nearer_index = d(4, 8) <= d(4, 12)

        # Index-finger direction (knuckle -> tip), only for a clear one-finger point.
        self.point_curled = self.ext["index"] and self.mrp_ext == 0
        self.point_dir = self.point_direction()

        f = p[9] - p[0]
        f = f / (np.linalg.norm(f) + 1e-6)
        a = p[5] - p[17]
        a = a / (np.linalg.norm(a) + 1e-6)
        self.dir = f  # wrist -> knuckles, image coords (y down)
        # In the mirrored image a palm facing the camera gives cross < 0 for the
        # right hand and > 0 for the left hand, whatever the hand's rotation.
        self.cross = PALM_SIGN * float(f[0] * a[1] - f[1] * a[0])

        c = p[[0, 5, 9, 17]].mean(axis=0)
        self.center = c
        self.cx, self.cy = float(c[0] / w), float(c[1] / h)
        self.scale = d(0, 5) + d(5, 17) + d(17, 0)

        # Palm-facing score from the 3-D palm normal: +1 palm square to the camera,
        # 0 edge-on, -1 back of the hand.
        self.is_right = handed == "Right" if handed else self.cx > 0.5
        n = np.cross(p3[9] - p3[0], p3[5] - p3[17])
        side = -1.0 if self.is_right else 1.0
        self.facing = PALM_SIGN * side * float(n[2] / (np.linalg.norm(n) + 1e-6))
        self.role = "right" if self.is_right else "left"  # steadied across frames by HandRoles

    def point_direction(self, side=None):
        """"up"/"down"/"left"/"right" for a clear one-finger point, else None.

        side: angle (radians) of your right from camera-right when the camera is off to the
        side.  Then the image is misleading - a point at the monitor looks like a point
        sideways, and a real sideways point is foreshortened - so the metric 3-D landmarks
        are turned into your frame instead.  None = the image, as always."""
        if not self.point_curled:
            return None
        if side is not None and self.world is not None:
            q = self.world

            def wd(a, b):
                return float(np.linalg.norm(q[a] - q[b]))

            palm = max(wd(0, 9), 1.25 * wd(5, 17), 1e-6)
            v = q[8] - q[5]
            if not (wd(0, 8) > wd(0, 12) * POINT_RATIO and np.linalg.norm(v) > 0.5 * palm):
                return None
            vx = math.cos(side) * float(v[0]) + math.sin(side) * float(v[2])
            vy = float(v[1])
        else:
            p = self.p
            v = p[8] - p[5]
            if not (float(np.linalg.norm(p[0] - p[8])) > float(np.linalg.norm(p[0] - p[12])) * POINT_RATIO
                    and np.linalg.norm(v) > 0.5 * self.palm):
                return None
            vx, vy = float(v[0]), float(v[1])
        if abs(vy) > POINT_AXIS * abs(vx):
            return "up" if vy < 0 else "down"
        if abs(vx) > POINT_AXIS * abs(vy):
            return "right" if vx > 0 else "left"
        return None

    def finger_dir(self):
        """Unit 3-D aim of the index finger (knuckle -> tip); x right, y down, z away from
        the camera, in the mirrored view."""
        q = self.world if self.world is not None else self.p3
        v = np.asarray(q[8] - q[5], dtype=np.float64)
        return v / (np.linalg.norm(v) + 1e-9)

    def tap_features(self):
        """Signals that rise during a forward tap (see DEFAULT_CAL["tap"])."""
        return {"push": math.log(max(self.scale, 1e-3)),
                "depth": -float(self.p3[8, 2] - self.p3[5, 2]) / self.palm,
                "bend": -float(np.linalg.norm(self.p[8] - self.p[5])) / self.palm}

    def palm_forward(self, is_right):
        return self.cross < -PALM_FACING if is_right else self.cross > PALM_FACING


class HandRoles:
    """Gives each hand a steady left/right role.  MediaPipe's per-frame handedness label
    flickers now and then; a flicker would turn a pointer move into a scroll, so each
    hand is followed by position and its label is averaged over frames."""

    def __init__(self):
        self.tracks = []  # (centre (normalized), smoothed score: + right, - left, t)

    def assign(self, hands, t):
        new, used = [], set()
        for h in hands:
            c = np.array((h.cx, h.cy))
            best, best_d = None, 0.25
            for i, (tc, _, tt) in enumerate(self.tracks):
                dist = float(np.linalg.norm(c - tc))
                if i not in used and t - tt < 0.5 and dist < best_d:
                    best, best_d = i, dist
            if h.handed:
                vote = h.hand_conf if h.handed == "Right" else -h.hand_conf
            else:
                vote = 1.0 if h.cx > 0.5 else -1.0
            if best is None:
                h.role_score = vote
            else:
                used.add(best)
                h.role_score = 0.7 * self.tracks[best][1] + 0.3 * vote
        if len(hands) == 2 and (hands[0].role_score > 0) == (hands[1].role_score > 0):
            # both claim the same side: in the mirrored view the right hand is the one on the right
            lh, rh = sorted(hands, key=lambda h: h.cx)
            lh.role_score, rh.role_score = -max(abs(lh.role_score), 0.5), max(abs(rh.role_score), 0.5)
        for h in hands:
            h.role = "right" if h.role_score > 0 else "left"
            new.append((np.array((h.cx, h.cy)), h.role_score, t))
        self.tracks = new


def spread_pose(hands):
    left, right = sorted(hands, key=lambda h: h.cx)
    return (left.open and right.open and right.cx - left.cx > 0.2
            and right.dir[0] > SPREAD_OUT and left.dir[0] < -SPREAD_OUT
            and right.palm_forward(True) and left.palm_forward(False))


# ---- gesture engine -----------------------------------------------------
class Engine:
    def __init__(self, dry=False, spotify=None, launcher=None, cal=None):
        self.dry = dry
        self.spotify = spotify
        self.launcher = launcher
        self.roles = HandRoles()
        self.pointer = Pointer(cal or load_cal(), self._act, dry=dry)
        self.snap_seen_t = float("-inf")
        self.snap_pending = None
        self.snap_ready_t = 0.0
        self.pairs = collections.deque()  # (t, hand distance in palm lengths)
        self.clap_ready_t = 0.0
        self.mode = "NORMAL"
        self.cooldown_until = 0.0
        self.last_action, self.last_action_t = "", 0.0
        self.spread_since = None
        self.minimized = []
        self.point_block = False
        self._reset_pinch()
        self._reset_point()
        self.sw_anchor = None
        self.sw_ready_t = self.sw_started = self.sw_last_hand_t = 0.0
        self.scales = collections.deque()

    def _act(self, label, t, fn=None):
        log.info(label)
        self.last_action, self.last_action_t = label, t
        if fn and not self.dry:
            fn()

    def _reset_pinch(self):
        self.closed, self.closed_kind = False, None
        self.closed_since = self.last_closed_t = self.last_open_t = 0.0

    def _reset_point(self):
        self.point_dir = None
        self.point_since = self.point_seen_t = self.last_scroll_t = 0.0
        self.point_fired = False
        self.next_tab_t = float("inf")
        self.scroll_acc = 0.0

    def update(self, hands, t, sounds=()):
        self.roles.assign(hands, t)
        if self.launcher and self.launcher.busy:
            self.pointer.release()
            return  # the clap sequence is typing; don't interfere
        if self.mode == "SWITCHER":
            self._switcher(hands, t)
        else:
            if not self._clap(hands, t, sounds):
                self._snap(hands, t, sounds)
            self._normal(hands, t)

    # -- clap: a mic click right after two hands closed in on each other
    def _clap(self, hands, t, sounds):
        if len(hands) == 2:
            a, b = hands
            dist = float(np.linalg.norm(a.center - b.center)) / ((a.palm + b.palm) / 2)
            self.pairs.append((t, dist))
        while self.pairs and t - self.pairs[0][0] > 1.0:
            self.pairs.popleft()
        for s in sounds:
            recent = [dd for tt, dd in self.pairs if -0.15 < s - tt < CLAP_LOOKBACK]
            if (s >= self.clap_ready_t and recent and recent[-1] < CLAP_CLOSE
                    and max(recent) - recent[-1] > CLAP_APPROACH):
                self.clap_ready_t = t + CLAP_COOLDOWN
                self.pairs.clear()
                self.snap_pending = None
                self._act("Clap: PowerShell + Claude", t, self.launcher.launch if self.launcher else None)
                return True
        return False

    # -- snap: a mic click is only a snap if the camera saw a loaded snap just before it
    #    and sees the thumb/middle contact released just after it.
    def _snap(self, hands, t, sounds):
        loaded = any(h.snap_ready for h in hands)
        if loaded:
            self.snap_seen_t = t
        for s in sounds:
            if abs(s - self.snap_seen_t) < SNAP_LOOKBACK and s >= self.snap_ready_t:
                self.snap_pending = s
        if self.snap_pending is None:
            return
        if not loaded:
            self.snap_pending = None
            self.snap_seen_t = float("-inf")
            self.snap_ready_t = t + SNAP_COOLDOWN
            self._reset_pinch()  # the snapping hand shouldn't also count as a pinch/spread/point
            self._reset_point()
            self.point_block = True
            self._act("Snap: Spotify", t, self.spotify.snap if self.spotify else None)
        elif t - self.snap_pending > SNAP_CONFIRM:
            self.snap_pending = None

    # -- normal mode: right hand = pointer; left hand = point, pinch; both = switcher
    def _normal(self, hands, t):
        if len(hands) == 2 and spread_pose(hands):
            self.pointer.release()
            self._reset_pinch()
            self._reset_point()
            if self.spread_since is None:
                self.spread_since = t
            elif t - self.spread_since >= SPREAD_HOLD and t >= self.cooldown_until:
                self._open_switcher(t)
            return
        self.spread_since = None
        self.pointer.update(next((h for h in hands if h.role == "right"), None), t)
        h = next((h for h in hands if h.role == "left"), None)
        if h is None:
            self._reset_pinch()
            if t - self.point_seen_t > POINT_GRACE:
                self._reset_point()
            return
        ready = t >= self.cooldown_until

        if self._point(h, t, ready):
            self._reset_pinch()
            return

        # Pinch / reverse pinch (only with the palm roughly toward the camera; a tipped
        # hand foreshortens the fingers and can look pinched)
        if abs(h.facing) < 0.4:
            self._reset_pinch()
            return
        if h.pinch < PINCH_CLOSE:
            if not self.closed:
                self.closed, self.closed_since = True, t
                if h.mrp_ext == 3:
                    self.closed_kind = "pinch"
                elif h.mrp_ext <= 1 and h.thumb_nearer_index and not h.snap_ready:
                    self.closed_kind = "bunch"
                else:
                    self.closed_kind = None
                if self.closed_kind == "pinch" and t - self.last_open_t < PINCH_SPEED and ready:
                    self._act("Pinch: minimize", t, self._minimize)
                    self.cooldown_until = t + 0.6
            self.last_closed_t = t
        elif h.pinch > PINCH_OPEN:
            if (self.closed and self.closed_kind == "bunch" and ready
                    and h.pinch > SPREAD_OPEN and h.ext["index"] and h.mrp_ext >= 2
                    and self.last_closed_t - self.closed_since >= BUNCH_HOLD
                    and t - self.last_closed_t < PINCH_SPEED):
                self._act("Reverse pinch: maximize", t, self._maximize)
                self.cooldown_until = t + 0.6
            self.closed = False
            self.last_open_t = t

    def _point(self, h, t, ready):
        """Left index up/down: scroll continuously while held; left/right: switch tab,
        repeating if held.

        Returns True while a point is in progress so pinch detection stays out of it."""
        side = self.pointer.adapt.side_angle() if self.pointer.adapt else None
        if side is not None:
            h.point_dir = h.point_direction(side)
        d = h.point_dir
        if self.point_block:  # right after a snap the index is often still up: not a point
            self.point_block = d is not None
            return d is not None
        if d is None:
            if self.point_dir and t - self.point_seen_t > POINT_GRACE:
                self._reset_point()  # finger put away
            return self.point_dir is not None
        self.point_seen_t = t
        if d != self.point_dir:
            self._reset_point()
            self.point_dir, self.point_since, self.point_seen_t = d, t, t
            return True
        held = t - self.point_since
        if held < POINT_HOLD or not ready:
            self.last_scroll_t = t
            return True

        if d in ("left", "right"):
            if not self.point_fired or t >= self.next_tab_t:
                self.next_tab_t = t + (TAB_REPEAT if self.point_fired else TAB_REPEAT_DELAY)
                self.point_fired = True
                step = 1 if d == "right" else -1
                self._act(f"Point {d}: {'next' if step > 0 else 'previous'} tab", t,
                          lambda: switch_tab(step))
            return True

        if not self.point_fired:
            self.point_fired = True
            self._act(f"Point {d}: scrolling {d}", t)
        speed = SCROLL_SPEED * min(1 + (held - POINT_HOLD) / SCROLL_RAMP, SCROLL_MAX)
        self.scroll_acc += speed * 120 * (t - self.last_scroll_t)
        self.last_scroll_t = t
        n = int(self.scroll_acc)
        if n:
            self.scroll_acc -= n
            if not self.dry:
                scroll(n if d == "up" else -n)
        return True

    def _minimize(self):
        hwnd = active_app_window()
        if hwnd:
            user32.ShowWindow(hwnd, SW_MINIMIZE)
            self.minimized = [w for w in self.minimized if w != hwnd] + [hwnd]

    def _maximize(self):
        while self.minimized:
            hwnd = self.minimized.pop()
            if user32.IsWindow(hwnd) and user32.IsIconic(hwnd):
                break
        else:
            hwnd = active_app_window()
        if hwnd:
            user32.ShowWindow(hwnd, SW_MAXIMIZE)
            focus(hwnd)

    # -- Alt+Tab view
    def _open_switcher(self, t):
        self.pointer.release()
        self._act("Alt+Tab view opened", t, lambda: (alt(True), tap(VK_TAB)))
        self.mode = "SWITCHER"
        self.sw_started = self.sw_last_hand_t = t
        self.sw_anchor = None
        self.scales.clear()

    def _close_switcher(self, t, label, cancel):
        def fn():
            if cancel:
                tap(VK_ESCAPE)
            alt(False)
        self._act(label, t, fn)
        self.mode = "NORMAL"
        self.cooldown_until = t + 1.0
        self.spread_since = None
        self._reset_pinch()
        self._reset_point()

    def _switcher(self, hands, t):
        if hands:
            self.sw_last_hand_t = t
        elif t - self.sw_last_hand_t > SW_CANCEL_NOHANDS:
            return self._close_switcher(t, "Alt+Tab cancelled (no hands)", cancel=True)
        if t - self.sw_started > SW_TIMEOUT:
            return self._close_switcher(t, "Alt+Tab cancelled (timeout)", cancel=True)
        if len(hands) != 1:
            # Still holding the two-hand pose, or no hand: wait for a single hand.
            self.sw_anchor = None
            self.scales.clear()
            return
        h = hands[0]
        self.scales.append((t, h.scale))
        while t - self.scales[0][0] > TAP_WINDOW:
            self.scales.popleft()
        if self.sw_anchor is None or t < self.sw_ready_t:
            if self.sw_anchor is None:
                self.sw_ready_t = t + 0.25  # let the hand settle before tracking
            self.sw_anchor = h.cx
            return

        if h.pointing and h.scale / min(s for _, s in self.scales) > TAP_GROWTH:
            return self._close_switcher(t, "Tap: open selected app", cancel=False)

        recent = [s for tt, s in self.scales if t - tt <= 0.2]
        if max(recent) / min(recent) > 1.06:
            self.sw_anchor = h.cx  # hand moving toward/away from camera: don't step
            return
        while h.cx - self.sw_anchor > STEP:
            self.sw_anchor += STEP
            self._act("Select right", t, lambda: tap(VK_TAB))
        while self.sw_anchor - h.cx > STEP:
            self.sw_anchor -= STEP
            self._act("Select left", t, lambda: (key(VK_SHIFT), tap(VK_TAB), key(VK_SHIFT, up=True)))


# ---- calibration --------------------------------------------------------
class Calibrator:
    """--calibrate: records your right hand and fits the pointer to it.

    still -> the steadiest smoothing that still keeps the pointer within half a pixel
    move  -> how high each tap signal gets during ordinary movement (the noise floor)
    tap   -> how high it gets on your real taps; threshold = geometric mean of the two,
             using only the signals that cleanly separate taps from movement"""

    PHASES = (("still", 5.0, "Point RIGHT index at the screen. Hold it still."),
              ("move", 10.0, "Aim the finger around the screen, slow and fast. No taps."),
              ("tap", 12.0, "Tap forward 6 times, pausing ~1 s between taps."))
    if CLICK_MODE == "middle":
        PHASES = PHASES[:2] + (("tap", 12.0, "Lift the MIDDLE finger 6 times (index stays out), ~1 s apart."),)
    LEAD = 1.0  # s at the start of each phase that isn't recorded (time to get ready)
    TAIL = 0.5  # ... and at the end (people start the next step early)
    MIN_FRAMES = {"still": 40, "move": 80, "tap": 100}  # about half of each step's frames

    def __init__(self, cal):
        self.cal = cal
        self.roles = HandRoles()
        self.phase, self.phase_t = 0, None
        self.data = {name: [] for name, _, _ in self.PHASES}
        self.done, self.summary, self.retry = False, [], ""
        self.recent = collections.deque(maxlen=15)  # (palm, tip) of accepted frames
        self.glitches = 0

    def update(self, hands, t):
        if self.done:
            return
        if self.phase_t is None:
            self.phase_t = t
        name, dur, _ = self.PHASES[self.phase]
        self.roles.assign(hands, t)
        h = next((h for h in hands if h.role == "right"), None)
        posed = h is not None and (h.pointer_pose if CLICK_MODE == "middle"
                                   else h.ext["index"] and h.mrp_ext <= 1)
        if posed and self.LEAD < t - self.phase_t < dur - self.TAIL and not self._glitch(h):
            self.data[name].append((t, h.p[8].astype(np.float64), h.palm, h.tap_features(),
                                    h.finger_dir(), h.mid_ratio))
        if t - self.phase_t >= dur:
            self.phase_t = t
            if len(self.data[name]) < self.MIN_FRAMES[name]:
                # the hand wasn't seen pointing for long enough: redo this step, don't fit on nothing
                self.data[name].clear()
                self.retry = f"Not enough frames - redo with the RIGHT hand, index out, others curled"
                return
            self.retry = ""
            self.phase += 1
            if self.phase == len(self.PHASES):
                self._fit()
                self.done = True

    def _glitch(self, h):
        """Tracking glitches (hand size or fingertip jumping in one frame) would swamp the
        statistics - your last calibration measured 20% jitter from a handful of them."""
        recent = self.recent
        bad = False
        if len(recent) >= 5:
            palm = float(np.median([p for p, _ in recent]))
            last_tip = recent[-1][1]
            bad = (not 0.67 < h.palm / palm < 1.5  # a tap grows the hand ~10-20%; keep those
                   or float(np.linalg.norm(h.p[8] - last_tip)) > 0.6 * palm)
        if not bad:
            recent.append((h.palm, h.p[8].copy()))
        else:
            self.glitches += 1
        return bad

    def prompt(self, t):
        if self.done:
            return "Calibrated", self.summary
        name, dur, text = self.PHASES[self.phase]
        left = dur - (t - (self.phase_t or t))
        n = len(self.data[name])
        lines = [f"{left:4.1f} s   frames: {n} (need {self.MIN_FRAMES[name]})"]
        return f"{self.phase + 1}/{len(self.PHASES)} {text}", ([self.retry] if self.retry else []) + lines

    @staticmethod
    def rises(samples, name):
        """Per sample: rise of feature `name` above its minimum over the last TAP_WINDOW."""
        out, win = [], collections.deque()
        for t, _, _, f, *_ in samples:
            win.append((t, f[name]))
            while t - win[0][0] > TAP_WINDOW:
                win.popleft()
            out.append((t, f[name] - min(v for _, v in win)))
        return out

    def _fit(self):
        report = {}
        still = self.data["still"]
        if len(still) >= 20:
            # Replay the still finger through the filter at each candidate setting; keep the
            # least-smoothed (least laggy) one whose pointer wobble stays under 0.4
            # pixel per frame.  Wobble = frame-to-frame steps, so a slow drift of the
            # "still" hand isn't mistaken for jitter.
            palm = float(np.median([s[2] for s in still]))
            ts = [s[0] for s in still]

            def wobble_pos(mc):
                f = OneEuro(mc, self.cal["beta"] / palm)
                out = np.array([f(s[1], s[0]) for s in still])[10:]
                return float(np.sqrt(np.mean(np.sum(np.diff(out, axis=0) ** 2, axis=1)))) / palm * GAIN_SLOW

            def wobble_angle(mc):
                f = OneEuro(mc, self.cal["beta_angle"])
                out = [f(s[4], s[0]) for s in still][10:]
                out = [u / np.linalg.norm(u) for u in out]
                steps = []
                for a, b in zip(out, out[1:]):
                    r, d = aim_axes(b)
                    steps.append(((b - a) @ r) ** 2 + ((b - a) @ d) ** 2)
                return math.degrees(math.sqrt(float(np.mean(steps)))) * ANGLE_GAIN_SLOW

            # A run that lost many frames to glitches, or that no setting can steady, says
            # more about the tracking that moment than about your hand: keep the old values.
            glitchy = self.glitches > 0.25 * (len(still) + self.glitches)
            for key_, wobble in (("mincutoff", wobble_pos), ("mincutoff_angle", wobble_angle)):
                pick = None
                for mc in (0.3, 0.5, 0.8, 1.2, 1.8, 2.5):
                    if wobble(mc) <= 0.4:
                        pick = mc
                if pick is not None and not glitchy:
                    self.cal[key_] = pick
            steps = np.linalg.norm(np.diff([s[1] for s in still], axis=0), axis=1)
            raw = float(np.median(steps)) / palm
            dirs = [s[4] for s in still]
            raw_deg = float(np.median([math.degrees(math.acos(min(1.0, float(a @ b))))
                                       for a, b in zip(dirs, dirs[1:])]))
            report.update(jitter_palm=round(raw, 5), jitter_deg=round(raw_deg, 3),
                          glitch_frames=self.glitches, fps=round(len(ts) / (ts[-1] - ts[0] + 1e-9), 1))
            self.summary.append(f"smoothing: {self.cal['mincutoff_angle']} Hz aim / {self.cal['mincutoff']} Hz "
                                f"position (jitter {raw_deg:.2f} deg, {self.glitches} glitch frames dropped)"
                                + ("; tracking too glitchy, kept previous" if glitchy else ""))
        else:
            self.summary.append("smoothing: too few still frames, kept")

        noise = self.data["still"] + self.data["move"]
        taps = self.data["tap"]
        fitted = {}
        if CLICK_MODE == "middle":
            self._fit_middle(noise, taps, report)
        elif len(noise) >= 30 and len(taps) >= 30:
            for name in ("push", "depth", "bend"):
                floor = float(np.percentile([r for _, r in self.rises(noise, name)], 97)) + 1e-6
                series = self.rises(taps, name)
                peaks = [r for i, (t, r) in enumerate(series) if r > 1.5 * floor
                         and r == max(rr for tt, rr in series if abs(tt - t) < 0.35)]
                peaks = sorted(peaks, reverse=True)[:6]
                med = float(np.median(peaks)) if len(peaks) >= 3 else 0.0
                ratio = med / floor
                report[name] = {"floor": round(floor, 4), "tap": round(med, 4),
                                "peaks": len(peaks), "ratio": round(ratio, 2)}
                if ratio >= 2.0:
                    fitted[name] = round(math.sqrt(floor * med), 4)
        if fitted:
            self.cal["tap"] = fitted
            self.summary.append("tap: " + ", ".join(
                f"{k} (taps {report[k]['ratio']}x above movement)" for k in fitted))
        elif CLICK_MODE != "middle":
            self.summary.append("tap: no signal separated taps from movement; kept defaults")
        save_cal(self.cal, report)

    def _fit_middle(self, curled_s, lift_s, report):
        """Middle-finger thresholds: just above how far your curled middle finger strays
        while you point, and well short of a full lift."""
        if len(curled_s) < 30 or len(lift_s) < 30:
            self.summary.append("click: too few frames, kept defaults")
            return
        lo = float(np.percentile([s[5] for s in curled_s], 90))  # curled, at its most relaxed
        hi = float(np.percentile([s[5] for s in lift_s], 90))    # a full lift
        gap = hi - lo
        lifts, up, last = 0, False, float("-inf")
        for t, *_, r in lift_s:  # count distinct lifts past the halfway point
            if not up and r > lo + 0.5 * gap and t - last > 0.3:
                up, last, lifts = True, t, lifts + 1
            elif up and r < lo + 0.25 * gap:
                up = False
        report["middle"] = {"curled_p90": round(lo, 3), "lifted_p90": round(hi, 3), "lifts": lifts}
        if gap >= 0.15 and lifts >= 3:
            self.cal["mid_down"] = round(lo + 0.25 * gap, 3)
            self.cal["mid_up"] = round(lo + 0.6 * gap, 3)
            self.summary.append(f"click: middle lift at {self.cal['mid_up']} (curled ~{lo:.2f}, "
                                f"lifted ~{hi:.2f}, {lifts} lifts seen)")
        else:
            self.summary.append(f"click: lifts unclear ({lifts} seen, gap {gap:.2f}); kept defaults")
        log.info("calibrated: %s | %s", self.cal, report)


# ---- drawing ------------------------------------------------------------
try:
    CONNECTIONS = [(c.start, c.end) for c in vision.HandLandmarksConnections.HAND_CONNECTIONS]
except Exception:
    CONNECTIONS = []


def draw(frame, hands, engine, t, fps, mic=None, statuses=()):
    if mic:  # mic level meter (log scale), with the click threshold marked
        lvl = min(1.0, max(0.0, (np.log10(max(mic.level, 1e-5)) + 5) / 4))
        thr = (np.log10(SNAP_LEVEL) + 5) / 4
        x0, y0 = frame.shape[1] - 130, frame.shape[0] - 14
        cv2.rectangle(frame, (x0, y0 - 8), (x0 + 120, y0), (60, 60, 60), -1)
        cv2.rectangle(frame, (x0, y0 - 8), (x0 + int(120 * lvl), y0), (0, 200, 0), -1)
        cv2.line(frame, (x0 + int(120 * thr), y0 - 11), (x0 + int(120 * thr), y0 + 3), (0, 0, 255), 2)
        cv2.putText(frame, "mic", (x0 - 34, y0), cv2.FONT_HERSHEY_SIMPLEX, 0.45, (220, 220, 220), 1)
    y = frame.shape[0] - 50
    for s in statuses:
        if s:
            cv2.putText(frame, s, (10, y), cv2.FONT_HERSHEY_SIMPLEX, 0.6, (100, 255, 100), 2, cv2.LINE_AA)
            y -= 26

    for h in hands:
        pts = h.p.astype(int)
        for a, b in CONNECTIONS:
            cv2.line(frame, tuple(pts[a]), tuple(pts[b]), (200, 200, 200), 2)
        for x, yy in pts:
            cv2.circle(frame, (int(x), int(yy)), 4, (0, 200, 255), -1)
        tags = [h.role[0].upper(), "open" if h.open else "",
                f"point-{h.point_dir}" if h.point_dir and h.role == "left" else "",
                "snap" if h.snap_ready else "", f"p={h.pinch:.2f}"]
        org = tuple(pts[0] + np.array([-40, 30]))
        cv2.putText(frame, " ".join(x for x in tags if x), org, cv2.FONT_HERSHEY_SIMPLEX,
                    0.5, (255, 255, 255), 2, cv2.LINE_AA)

    color = (0, 220, 0) if engine.mode == "NORMAL" else (0, 140, 255)
    cv2.rectangle(frame, (0, 0), (frame.shape[1], 34), (0, 0, 0), -1)
    status = f"{engine.mode}{'  (DRY RUN)' if engine.dry else ''}   hands:{len(hands)}   {fps:4.1f} fps"
    cv2.putText(frame, status, (10, 24), cv2.FONT_HERSHEY_SIMPLEX, 0.65, color, 2, cv2.LINE_AA)
    prof = engine.pointer.adapt
    if prof is not None and prof.p is not None:
        cv2.putText(frame, prof.describe(), (10, 56), cv2.FONT_HERSHEY_SIMPLEX, 0.45,
                    (200, 200, 200), 1, cv2.LINE_AA)
    if engine.last_action and t - engine.last_action_t < 2.0:
        cv2.putText(frame, engine.last_action, (10, frame.shape[0] - 16), cv2.FONT_HERSHEY_SIMPLEX,
                    0.8, (0, 255, 255), 2, cv2.LINE_AA)
    if engine.spread_since is not None:
        frac = min(1.0, (t - engine.spread_since) / SPREAD_HOLD)
        cv2.rectangle(frame, (0, 36), (int(frame.shape[1] * frac), 42), (0, 140, 255), -1)
    ptr = engine.pointer
    if ptr.engaged:  # tap meter: fills as you tap forward; the click happens at the right end
        x0, w = frame.shape[1] - 150, 140
        cv2.putText(frame, f"pointer x{ptr.cal['speed']:.1f}", (x0, 58), cv2.FONT_HERSHEY_SIMPLEX,
                    0.5, (255, 255, 255), 1, cv2.LINE_AA)
        cv2.rectangle(frame, (x0, 64), (x0 + w, 72), (60, 60, 60), -1)
        col = (0, 255, 255) if ptr.tap_level >= TAP_FREEZE else (255, 200, 0)
        cv2.rectangle(frame, (x0, 64), (x0 + int(w * min(ptr.tap_level, 1.0)), 72), col, -1)


def draw_calibration(frame, title, lines):
    cv2.rectangle(frame, (0, 40), (frame.shape[1], 70 + 26 * len(lines)), (0, 0, 0), -1)
    cv2.putText(frame, title, (10, 62), cv2.FONT_HERSHEY_SIMPLEX, 0.55, (0, 255, 255), 2, cv2.LINE_AA)
    for i, s in enumerate(lines):
        cv2.putText(frame, s, (10, 88 + 26 * i), cv2.FONT_HERSHEY_SIMPLEX, 0.5, (255, 255, 255), 1,
                    cv2.LINE_AA)


# ---- main ---------------------------------------------------------------
def fail(msg):
    log.error(msg)
    user32.MessageBoxW(None, msg, WIN, 0x10)
    sys.exit(1)


def main():
    global own_hwnd
    dry = "--dry" in sys.argv
    selftest = "--selftest" in sys.argv
    try:  # physical pixels, so the pointer maps 1:1 on scaled (125%, 150%) displays
        user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))
    except Exception:
        pass
    if not MODEL.exists():
        fail(f"Hand model not found:\n{MODEL}")

    cap, cam_name = open_camera()
    if not cap.isOpened():
        fail(f"Could not open the webcam '{cam_name}'. Is another app using it?")
    try:
        mic = SnapListener()
    except Exception:
        log.exception("microphone unavailable; snap and clap disabled")
        mic = None

    options = vision.HandLandmarkerOptions(
        base_options=BaseOptions(model_asset_path=str(MODEL)),
        running_mode=vision.RunningMode.VIDEO,
        num_hands=2,
        min_hand_detection_confidence=0.6,
        min_hand_presence_confidence=0.5,
        min_tracking_confidence=0.5,
    )
    live = not (dry or selftest)
    spotify = Spotify() if live else None
    launcher = ClaudeLauncher() if live else None
    abacus = Abacus() if live else None
    cal = load_cal()
    profiles = None if selftest else Profiles(cal)
    if profiles:
        profiles.activate(*current_setup())
    calibrator = Calibrator(cal) if "--calibrate" in sys.argv and not selftest else None
    engine = Engine(dry=not live, spotify=spotify, launcher=launcher, cal=cal)
    engine.pointer.adapt = profiles
    setup_t = time.perf_counter()
    if abacus and abacus.proc:
        try:
            engine.pointer.on_click = SearchDictation().clicked
        except Exception:
            log.exception("search-box dictation unavailable")
    t_start = time.perf_counter()
    last_ts = -1
    fps, frames, seen = 0.0, 0, collections.Counter()
    prev_t = None
    calib_end = 0.0
    log.info("started (dry=%s, selftest=%s)", dry, selftest)

    try:
        with vision.HandLandmarker.create_from_options(options) as landmarker:
            while True:
                ok, frame = cap.read()
                if not ok:
                    time.sleep(0.05)
                    continue
                frame = cv2.flip(frame, 1)  # mirror so movement matches the user's view
                t = time.perf_counter()
                ts = max(int((t - t_start) * 1000), last_ts + 1)
                last_ts = ts
                rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
                result = landmarker.detect_for_video(Image(image_format=ImageFormat.SRGB, data=rgb), ts)
                h_px, w_px = frame.shape[:2]
                hands = []
                for i, lms in enumerate(result.hand_landmarks):
                    cat = result.handedness[i][0] if result.handedness else None
                    label = cat.category_name if cat else None
                    if label and SWAP_HANDEDNESS:
                        label = "Left" if label == "Right" else "Right"
                    world = (result.hand_world_landmarks[i]
                             if i < len(getattr(result, "hand_world_landmarks", None) or []) else None)
                    hands.append(Hand(lms, w_px, h_px, label, cat.score if cat else 1.0, world))
                sounds = mic.drain() if mic else ()
                if calibrator and not calibrator.done:
                    calibrator.update(hands, t)  # the engine sits out until calibration ends
                    if calibrator.done:
                        calib_end = t
                        if profiles:
                            profiles.reset_click()
                else:
                    engine.update(hands, t, sounds)
                if profiles and t - setup_t >= SETUP_POLL:
                    setup_t = t
                    if profiles.activate(*current_setup()):
                        engine.pointer.release()
                        engine.pointer.refresh_screen()
                        engine.last_action, engine.last_action_t = f"Setup: {profiles.describe()}", t
                    profiles.maybe_save(t)

                frames += 1
                if prev_t is not None:
                    fps = 0.9 * fps + 0.1 / max(t - prev_t, 1e-3)
                prev_t = t
                seen[len(hands)] += 1

                if selftest:
                    if frames >= 90:
                        print(f"selftest ok: camera '{cam_name}', mic '{mic.name if mic else None}', "
                              f"{frames} frames, {fps:.1f} fps, hand counts {dict(seen)}")
                        return
                    continue

                statuses = [launcher.status if launcher else "", spotify.status if spotify else "",
                            abacus.status if abacus else ""]
                draw(frame, hands, engine, t, fps, mic, statuses)
                if calibrator and (not calibrator.done or t - calib_end < 8):
                    draw_calibration(frame, *calibrator.prompt(t))
                preview = cv2.resize(frame, (PREVIEW_W, int(PREVIEW_W * h_px / w_px)))
                cv2.imshow(WIN, preview)
                k = cv2.waitKey(1) & 0xFF
                if k in (ord("+"), ord("="), ord("-")):
                    cal["speed"] = round(min(max(cal["speed"] * (1.15 if k != ord("-") else 1 / 1.15),
                                                 0.3), 4.0), 2)
                    if profiles:  # speed is per setup: a big monitor wants more
                        profiles.set("speed", cal["speed"])
                    else:
                        save_cal(cal)
                    engine.last_action, engine.last_action_t = f"Pointer speed x{cal['speed']}", t
                if own_hwnd is None:
                    own_hwnd = user32.FindWindowW(None, WIN)
                    try:
                        cv2.setWindowProperty(WIN, cv2.WND_PROP_TOPMOST, 1)
                    except cv2.error:
                        pass
                    cv2.moveWindow(WIN, user32.GetSystemMetrics(0) - PREVIEW_W - 30, 30)
                elif cv2.getWindowProperty(WIN, cv2.WND_PROP_VISIBLE) < 1:
                    break  # the user closed the window
    finally:
        if live:
            alt(False)  # never leave Alt stuck down
        if profiles:
            try:
                profiles.save()
            except Exception:
                log.exception("could not save %s", PROFILES_FILE.name)
        if abacus:
            abacus.stop()
        cap.release()
        if mic:
            mic.close()
        cv2.destroyAllWindows()
        log.info("stopped")


if __name__ == "__main__":
    try:
        main()
    except SystemExit:
        raise
    except Exception as exc:
        log.exception("crashed")
        if alt_held:
            key(VK_MENU, up=True)
        user32.MessageBoxW(None, f"Gesture Control crashed:\n{exc}\n\nSee gesture.log", WIN, 0x10)
