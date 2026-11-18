"""Abacus's on-screen presence: a small pill at the bottom of the screen that appears when you
say "Abacus", shows your words live as you speak, then what Abacus is doing and its reply.

It never takes focus and clicks pass straight through it, so the box you're dictating into
keeps the cursor. All Tk work happens on the overlay's own thread; other threads only put
messages on a queue.
"""
import ctypes
import ctypes.wintypes as wt
import logging
import math
import queue
import threading
import time

log = logging.getLogger("abacus")
user32 = ctypes.WinDLL("user32", use_last_error=True)
user32.GetParent.argtypes = [wt.HWND]
user32.GetParent.restype = wt.HWND
user32.GetWindowLongW.argtypes = [wt.HWND, ctypes.c_int]
user32.SetWindowLongW.argtypes = [wt.HWND, ctypes.c_int, ctypes.c_long]
user32.ShowWindow.argtypes = [wt.HWND, ctypes.c_int]
user32.SetWindowPos.argtypes = [wt.HWND, wt.HWND, ctypes.c_int, ctypes.c_int, ctypes.c_int,
                                ctypes.c_int, wt.UINT]
user32.SystemParametersInfoW.argtypes = [wt.UINT, wt.UINT, ctypes.c_void_p, wt.UINT]

GWL_EXSTYLE = -20
WS_EX = 0x80000 | 0x20 | 0x80 | 0x8000000 | 0x8   # layered, click-through, tool, no-activate, topmost
HWND_TOPMOST = wt.HWND(-1)
SWP_NOACTIVATE, SWP_SHOWWINDOW = 0x10, 0x40
SW_HIDE = 0
KEY = "#010203"   # transparent colour key (outside the rounded pill)

BG, FG, DIM = "#14171d", "#f2f4f8", "#8a93a6"
STATES = {  # label, accent colour
    "listening": ("Listening", "#4cc9f0"),
    "dictating": ("Dictation · typing into the box", "#b388ff"),
    "thinking": ("Working on it", "#f4a261"),
    "polishing": ("Tidying your words", "#b388ff"),
    "reply": ("Abacus", "#7bd88f"),
    "typed": ("Typed", "#7bd88f"),
    "error": ("Didn't catch that", "#ef6f6c"),
}


class Overlay:
    W, MIN_H, PAD, ORB = 560, 84, 18, 48

    def __init__(self) -> None:
        self.q: queue.Queue = queue.Queue()
        threading.Thread(target=self._run, daemon=True, name="overlay").start()

    # ---- thread-safe API
    def show(self, state: str, text: str = "") -> None:
        self.q.put(("show", state, text))

    def text(self, text: str) -> None:
        self.q.put(("text", text))

    def level(self, x: float) -> None:
        """Microphone loudness 0..1; the orb's ring breathes with your voice."""
        self.q.put(("level", max(0.0, min(1.0, x))))

    def hide(self, after: float = 0.0) -> None:
        self.q.put(("hide", after))

    # ---- Tk thread
    def _run(self) -> None:
        try:
            import tkinter as tk
            import tkinter.font as tkfont
            try:
                ctypes.windll.shcore.SetProcessDpiAwareness(2)
            except Exception:
                pass
            root = tk.Tk()
            s = root.winfo_fpixels("1i") / 96.0          # DPI scale (1.25 at 125%)
            W, PAD, ORB = int(self.W * s), int(self.PAD * s), int(self.ORB * s)
            root.overrideredirect(True)
            root.attributes("-topmost", True)
            root.attributes("-alpha", 0.96)
            root.configure(bg=KEY)
            root.attributes("-transparentcolor", KEY)
            c = tk.Canvas(root, width=W, height=int(self.MIN_H * s), bg=KEY, highlightthickness=0)
            c.pack()
            f_label = tkfont.Font(family="Segoe UI Semibold", size=9)
            f_text = tkfont.Font(family="Segoe UI", size=13)
            text_x = PAD + ORB + PAD
            text_w = W - text_x - PAD
            root.geometry(f"{W}x{int(self.MIN_H * s)}+-3000+-3000")
            root.update()
            hwnd = user32.GetParent(wt.HWND(root.winfo_id())) or wt.HWND(root.winfo_id())
            user32.SetWindowLongW(hwnd, GWL_EXSTYLE, user32.GetWindowLongW(hwnd, GWL_EXSTYLE) | WS_EX)
            user32.ShowWindow(hwnd, SW_HIDE)

            st = {"state": None, "text": "", "level": 0.0, "shown": False, "hide_at": 0.0,
                  "t0": time.time(), "h": int(self.MIN_H * s)}

            def place(h: int) -> None:
                rect = wt.RECT()
                user32.SystemParametersInfoW(0x30, 0, ctypes.byref(rect), 0)  # SPI_GETWORKAREA
                x = rect.left + (rect.right - rect.left - W) // 2
                y = rect.bottom - h - int(28 * s)
                user32.SetWindowPos(hwnd, HWND_TOPMOST, x, y, W, h, SWP_NOACTIVATE | SWP_SHOWWINDOW)

            def rounded(x0, y0, x1, y1, r, **kw):
                pts = [x0 + r, y0, x1 - r, y0, x1, y0, x1, y0 + r, x1, y1 - r, x1, y1,
                       x1 - r, y1, x0 + r, y1, x0, y1, x0, y1 - r, x0, y0 + r, x0, y0]
                return c.create_polygon(pts, smooth=True, **kw)

            line = f_text.metrics("linespace")
            measured = {}  # text -> wrapped height (text only changes a few times a second)

            def height_of(text: str) -> int:
                if text not in measured:
                    probe = c.create_text(0, 0, text=text, width=text_w, anchor="nw", font=f_text)
                    bb = c.bbox(probe)
                    c.delete(probe)
                    measured.clear() if len(measured) > 200 else None
                    measured[text] = (bb[3] - bb[1]) if bb else line
                return measured[text]

            def fit(text: str, lines: int) -> str:
                """At most `lines` wrapped lines; live speech keeps the newest words."""
                if height_of(text) <= lines * line:
                    return text
                words = text.split()
                while len(words) > 1 and height_of("… " + " ".join(words)) > lines * line:
                    words = words[1:]
                return "… " + " ".join(words)

            def draw() -> None:
                label, accent = STATES.get(st["state"], STATES["reply"])
                body = st["text"] or ("…" if st["state"] in ("listening", "dictating") else "")
                body = fit(body, 4 if st["state"] in ("listening", "dictating") else 7)
                th = height_of(body)
                c.delete("all")
                h = max(int(self.MIN_H * s), PAD + int(18 * s) + th + PAD)
                h = min(h, int(300 * s))
                if h != st["h"]:
                    st["h"] = h
                    c.configure(height=h)
                    if st["shown"]:
                        place(h)
                rounded(1, 1, W - 1, h - 1, int(22 * s), fill=BG, outline="#2a2f3a")
                # orb: accent disc + a tiny abacus (three rods with beads), ring pulses
                cx, cy = PAD + ORB // 2, h // 2
                t = time.time() - st["t0"]
                if st["state"] in ("listening", "dictating"):
                    pulse = 0.25 + 0.75 * st["level"]
                elif st["state"] in ("thinking", "polishing"):
                    pulse = 0.5 + 0.5 * math.sin(t * 4)
                else:
                    pulse = 0.15
                ring = ORB // 2 + int(4 * s + 9 * s * pulse)
                c.create_oval(cx - ring, cy - ring, cx + ring, cy + ring, outline=accent,
                              width=max(1, int(2 * s)))
                r = ORB // 2
                c.create_oval(cx - r, cy - r, cx + r, cy + r, fill=accent, outline="")
                for i, dy in enumerate((-0.42, 0, 0.42)):
                    yy = cy + int(dy * r)
                    c.create_line(cx - int(r * 0.62), yy, cx + int(r * 0.62), yy, fill=BG,
                                  width=max(1, int(1.6 * s)))
                    shift = math.sin(t * 3 + i * 2) * 0.18 if st["state"] == "thinking" else 0
                    for bx in ((-0.32, -0.12) if i != 1 else (0.12, 0.32)):
                        x = cx + int((bx + shift) * r)
                        b = max(2, int(r * 0.13))
                        c.create_oval(x - b, yy - b, x + b, yy + b, fill=BG, outline="")
                if st["state"] == "thinking":  # three dots after the label
                    label += "." * (1 + int(t * 2) % 3)
                c.create_text(text_x, PAD, text=label.upper(), anchor="nw", font=f_label, fill=accent)
                c.create_text(text_x, PAD + int(18 * s), text=body, width=text_w,
                              anchor="nw", font=f_text, fill=FG if st["text"] else DIM)

            def tick():
                dirty = False
                try:
                    while True:
                        msg = self.q.get_nowait()
                        if msg[0] == "show":
                            if msg[1] != st["state"]:
                                st["t0"] = time.time()
                            st["state"], st["text"], st["hide_at"] = msg[1], msg[2], 0.0
                            dirty = True
                            if not st["shown"]:
                                st["shown"] = True
                                draw()
                                place(st["h"])
                        elif msg[0] == "text":
                            st["text"] = msg[1]
                            dirty = True
                        elif msg[0] == "level":
                            st["level"] = 0.6 * st["level"] + 0.4 * msg[1]
                        elif msg[0] == "hide":
                            st["hide_at"] = time.time() + msg[1]
                except queue.Empty:
                    pass
                if st["hide_at"] and time.time() >= st["hide_at"]:
                    user32.ShowWindow(hwnd, SW_HIDE)
                    st.update(shown=False, hide_at=0.0, state=None, text="")
                elif st["shown"]:
                    draw()  # animated orb: redraw every frame while visible
                root.after(33, tick)

            root.after(33, tick)
            root.mainloop()
        except Exception:
            log.exception("overlay failed")


class NullOverlay:
    """Stand-in when the overlay is turned off."""
    def show(self, *a, **k): pass
    def text(self, *a, **k): pass
    def level(self, *a, **k): pass
    def hide(self, *a, **k): pass


if __name__ == "__main__":  # quick visual check: python abacus_overlay.py
    o = Overlay()
    o.show("listening")
    demo = "open Spotify and play something calm for studying please"
    for i in range(1, len(demo.split()) + 1):
        o.text(" ".join(demo.split()[:i]))
        for _ in range(6):
            o.level(abs(math.sin(time.time() * 7)))
            time.sleep(0.05)
    o.show("thinking", demo)
    time.sleep(2.5)
    o.show("reply", "Spotify is playing a calm study mix, sir. Do try to study rather than "
                    "merely listen.")
    time.sleep(3)
    o.hide()
    time.sleep(0.3)
