"""Abacus: say "Abacus, <command>" in one breath, hear the answer.

"Abacus, type <words>" or "Abacus, dictate" types what you say into whatever field has focus,
cleaned up for where it is going (abacus_flow.py). Holding the hold-to-talk key does the same
without the wake word. Everything else goes to the agent (abacus_llm.py), which can use the PC,
Chrome, and restyle sites and apps (abacus_tweaks.py).

    python abacus.py                  run the voice assistant (until the window is closed)
    python abacus.py --list-devices   show microphones
    python abacus.py --text "..."     send one typed command (no microphone) and speak the reply
    python abacus.py --text "..." --quiet    the same, but print the reply instead of speaking
    python abacus.py --check          load everything, time each part, and exit
    python abacus.py --voices         hear a sample of each suggested voice
"""
import argparse
import asyncio
import atexit
import collections
import hashlib
import io
import json
import logging
import queue
import random
import re
import sys
import threading
import time
import wave
import winsound
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import sounddevice as sd

import abacus_flow
import abacus_state
import abacus_tweaks
import config
from abacus_llm import make_backend

HERE = Path(__file__).resolve().parent
SAMPLE_RATE = 16000
BLOCK = 1600                       # 0.1 s of audio per block
BLOCK_SEC = BLOCK / SAMPLE_RATE
VOSK_MODEL = HERE / "models" / "vosk-model-small-en-us-0.15"
VOCAB_FILE = HERE / "vocab.json"   # words Abacus learned (mcp__abacus__remember_word writes it)

log = logging.getLogger("abacus")

LANG_NAMES = {"en": "English", "te": "Telugu", "ta": "Tamil", "hi": "Hindi", "kn": "Kannada",
              "ml": "Malayalam"}
# Unicode block of each language's own script (English is checked as ASCII instead).
SCRIPTS = {"te": (0x0C00, 0x0C7F), "ta": (0x0B80, 0x0BFF), "hi": (0x0900, 0x097F),
           "kn": (0x0C80, 0x0CFF), "ml": (0x0D00, 0x0D7F)}
# Spoken while Claude works / when nothing was understood. Always English: Abacus understands
# Telugu and Tamil but answers in English. Short, so the voice cache makes them instant.
ACKS = ["On it.", "Right away, sir.", "Consider it done.", "Very good, sir.", "One moment.",
        "Leave it with me.", "As you wish."]
NOT_CAUGHT = ["Sorry, I didn't catch that.", "Once more, sir? I missed that.",
              "I'm afraid that went past me. Again?"]


def script_of(text: str) -> str:
    """Which language's script most of the letters are in ("en" if mostly Latin)."""
    counts = collections.Counter()
    for ch in text:
        cp = ord(ch)
        for lang, (lo, hi) in SCRIPTS.items():
            if lo <= cp <= hi:
                counts[lang] += 1
                break
        else:
            if ch.isascii() and ch.isalpha():
                counts["en"] += 1
    return counts.most_common(1)[0][0] if counts else "en"


def wrong_script_share(text: str, lang: str) -> float:
    """Share of letters in a script that doesn't belong to `lang`. Latin is always allowed,
    since Telugu/Tamil speech is full of English words (YouTube, play, open)."""
    letters = [c for c in text if c.isalpha()]
    if not letters:
        return 0.0
    lo, hi = SCRIPTS.get(lang, (0, -1))
    bad = sum(1 for c in letters if not c.isascii() and not lo <= ord(c) <= hi)
    return bad / len(letters)


def add_cuda_dlls() -> None:
    """faster-whisper's GPU path needs cuBLAS/cuDNN; the pip nvidia-* wheels ship them here."""
    import os
    import site
    for sp in site.getsitepackages():
        for d in (Path(sp) / "nvidia").glob("*/bin"):
            os.add_dll_directory(str(d))
            os.environ["PATH"] = str(d) + os.pathsep + os.environ.get("PATH", "")


def setup_logging() -> None:
    fmt = logging.Formatter("%(asctime)s [main] %(message)s")
    fh = logging.FileHandler(HERE / "abacus.log", encoding="utf-8")
    fh.setFormatter(fmt)
    sh = logging.StreamHandler(sys.stdout)
    sh.setFormatter(logging.Formatter("%(message)s"))
    log.addHandler(fh)
    log.addHandler(sh)
    log.setLevel(logging.INFO)


# ---------------------------------------------------------------- speech out

def clean_for_speech(text: str) -> str:
    text = re.sub(r"```.*?```", " ", text, flags=re.S)
    text = re.sub(r"https?://\S+", "a link", text)
    text = re.sub(r"[*_`#>|]", "", text)
    return re.sub(r"\s+", " ", text).strip()


def to_wav(pcm: np.ndarray, sr: int) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sr)
        w.writeframes(np.clip(pcm * 32767, -32768, 32767).astype(np.int16).tobytes())
    return buf.getvalue()


def mp3_to_wav(mp3: bytes, effect: bool) -> bytes:
    """Decode edge-tts mp3, trim its padding silence, optionally add a faint 'suit' echo."""
    import av
    sr = 24000
    resampler = av.AudioResampler(format="s16", layout="mono", rate=sr)
    chunks = []
    with av.open(io.BytesIO(mp3)) as container:
        for frame in container.decode(audio=0):
            chunks += [f.to_ndarray().reshape(-1) for f in resampler.resample(frame)]
    pcm = np.concatenate(chunks).astype(np.float32) / 32768.0
    loud = np.flatnonzero(np.abs(pcm) > 0.01)
    if loud.size:
        pcm = pcm[max(0, loud[0] - sr // 50): loud[-1] + sr // 10]
    if effect:
        out = np.concatenate([pcm, np.zeros(int(sr * 0.12), np.float32)])
        for delay, gain in ((0.018, 0.18), (0.047, 0.10), (0.090, 0.05)):
            d = int(sr * delay)
            out[d:d + pcm.size] += gain * pcm
        pcm = out / np.max(np.abs(out)) * 0.9
    return to_wav(pcm, sr)


class Voice:
    """Neural voice via edge-tts; falls back to Windows SAPI when offline."""

    CACHE_MAX_CHARS = 80   # short stock phrases ("On it.") are cached so they play instantly

    def __init__(self, mute: bool = False) -> None:
        self.cache = HERE / "voice_cache"
        self.cache.mkdir(exist_ok=True)
        self.sapi = None
        self.mute = mute   # --quiet / --check: log what would be said, play nothing

    def _neural(self, text: str) -> bytes:
        import edge_tts
        voice = getattr(config, "VOICES_BY_LANGUAGE", {}).get(script_of(text), config.VOICE)
        key = hashlib.sha1(f"{voice}|{config.VOICE_RATE}|{config.VOICE_PITCH}|"
                           f"{config.VOICE_EFFECT}|{text}".encode()).hexdigest()[:16]
        path = self.cache / f"{key}.wav"
        if path.exists():
            return path.read_bytes()

        async def fetch() -> bytes:
            tts = edge_tts.Communicate(text, voice, rate=config.VOICE_RATE,
                                       pitch=config.VOICE_PITCH)
            return b"".join([c["data"] async for c in tts.stream() if c["type"] == "audio"])

        wav = mp3_to_wav(asyncio.run(fetch()), config.VOICE_EFFECT)
        if len(text) <= self.CACHE_MAX_CHARS:
            path.write_bytes(wav)
        return wav

    def _sapi_say(self, text: str) -> None:
        if self.sapi is None:
            import win32com.client
            self.sapi = win32com.client.Dispatch("SAPI.SpVoice")
            if config.SAPI_VOICE_NAME:
                for v in self.sapi.GetVoices():
                    if config.SAPI_VOICE_NAME.lower() in v.GetDescription().lower():
                        self.sapi.Voice = v
                        break
        self.sapi.Speak(text)

    def say(self, text: str) -> None:
        text = clean_for_speech(text)
        if not text:
            return
        log.info("Abacus: %s", text)
        if self.mute:
            return
        try:
            winsound.PlaySound(self._neural(text), winsound.SND_MEMORY)
        except Exception as e:
            log.warning("Neural voice failed (%s); using the Windows voice.", e)
            self._sapi_say(text)


def _make_chime(freqs: list[float], dur: float = 0.09) -> bytes:
    """Soft two-note sine chime with fades, as an in-memory WAV."""
    sr = 22050
    parts = []
    for f in freqs:
        t = np.arange(int(sr * dur)) / sr
        tone = np.sin(2 * np.pi * f * t) + 0.3 * np.sin(4 * np.pi * f * t)
        env = np.minimum(1, t / 0.008) * np.exp(-t * 18)
        parts.append(tone * env)
    return to_wav(np.concatenate(parts) / 1.3 * 0.5, sr)


CHIME_UP = _make_chime([988, 1319])     # wake word accepted
CHIME_DOWN = _make_chime([1319, 988])   # follow-up window open


def chime(high: bool = True) -> None:
    winsound.PlaySound(CHIME_UP if high else CHIME_DOWN, winsound.SND_MEMORY)


# ---------------------------------------------------------------- microphone

def find_device(name: str | None) -> int | None:
    if not name:
        return None
    for i, d in enumerate(sd.query_devices()):
        if d["max_input_channels"] > 0 and name.lower() in d["name"].lower():
            return i
    log.warning("Mic '%s' not found; using the Windows default input.", name)
    return None


class Mic:
    def __init__(self) -> None:
        self.q: queue.Queue[bytes] = queue.Queue()
        self.pending: collections.deque[bytes] = collections.deque()
        self.noise = 200.0
        device = find_device(config.MIC_DEVICE)
        name = sd.query_devices(device, "input")["name"]
        log.info("Microphone: %s", name)
        self.stream = sd.RawInputStream(
            samplerate=SAMPLE_RATE, blocksize=BLOCK, dtype="int16", channels=1,
            device=device, callback=lambda data, *_: self.q.put(bytes(data)),
        )
        self.stream.start()

    def read(self) -> bytes:
        return self.pending.popleft() if self.pending else self.q.get()

    def unread(self, blocks: list[bytes]) -> None:
        """Hand already-read audio back so the next read() calls see it again."""
        self.pending.extendleft(reversed(blocks))

    def flush(self) -> None:
        """Drop audio captured while Abacus was talking or thinking."""
        self.pending.clear()
        while not self.q.empty():
            self.q.get_nowait()

    def rms(self, data: bytes) -> float:
        a = np.frombuffer(data, np.int16).astype(np.float32)
        return float(np.sqrt(np.mean(a * a))) if a.size else 0.0

    def track_noise(self, level: float) -> None:
        # falls quickly toward quiet, rises slowly, so speech doesn't inflate the floor
        if level < self.noise:
            self.noise = 0.7 * self.noise + 0.3 * level
        else:
            self.noise = 0.995 * self.noise + 0.005 * level


WAKE_BUFFER_SEC = 2.5
WAKE_SETTLE_SEC = 0.6


DICTATE_PREROLL_SEC = 0.5  # audio from just before the request, in case you started talking first


class DictationRequests:
    """Gesture Control sets a named event when one of its clicks lands in a text box (a search
    bar); Abacus then types whatever is said next, no wake word needed."""
    NAME = "Local\\AbacusDictate"

    def __init__(self) -> None:
        import ctypes
        self.k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        self.k32.CreateEventW.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_int, ctypes.c_wchar_p]
        self.k32.CreateEventW.restype = ctypes.c_void_p
        self.k32.WaitForSingleObject.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
        self.k32.ResetEvent.argtypes = [ctypes.c_void_p]
        self.handle = self.k32.CreateEventW(None, False, False, self.NAME)  # auto-reset

    def pending(self) -> bool:
        """True once per request (reading it clears it)."""
        return bool(self.handle) and self.k32.WaitForSingleObject(self.handle, 0) == 0

    def clear(self) -> None:
        if self.handle:
            self.k32.ResetEvent(self.handle)


def wait_for_wake(mic: Mic, rec, transcribe: "Transcriber",
                  requests: DictationRequests | None = None, hold=None) -> str:
    """Block until something starts a turn, and say what it was:
    "wake"  the wake phrase. Vosk (cheap, always on) proposes a hit; because its grammar only
            knows the wake words, any speech can get squeezed into "abacus", so Whisper
            re-listens to the last few seconds and must also hear it.
    "click" Gesture Control clicked a text box and wants dictation.
    "hold"  the hold-to-talk key is being held."""
    rec.Reset()
    if requests:
        requests.clear()  # a click made while Abacus was busy talking is stale by now
    recent: collections.deque[bytes] = collections.deque(maxlen=int(WAKE_BUFFER_SEC / BLOCK_SEC))
    while True:
        if requests and requests.pending():
            mic.unread(list(recent)[-int(DICTATE_PREROLL_SEC / BLOCK_SEC):])
            return "click"
        if hold is not None and hold.armed():
            mic.unread(list(recent)[-3:])  # people start talking as they press the key
            return "hold"
        data = mic.read()
        recent.append(data)
        mic.track_noise(mic.rms(data))
        if rec.AcceptWaveform(data):
            heard = json.loads(rec.Result()).get("text", "")
        else:
            heard = json.loads(rec.PartialResult()).get("partial", "")
        if not any(p in heard for p in config.WAKE_PHRASES):
            continue
        rec.Reset()
        if not config.WAKE_VERIFY:
            return "wake"
        # Vosk's partial result often "completes" the word early (on "aba..."), so listen a moment
        # longer before checking, then hand that audio back so the command said in the same
        # breath ("Abacus, type ...") reaches the command recorder.
        extra = [mic.read() for _ in range(int(WAKE_SETTLE_SEC / BLOCK_SEC))]
        audio = np.frombuffer(b"".join([*recent, *extra]), np.int16).astype(np.float32) / 32768.0
        ok, said = transcribe.heard_wake(audio)
        if ok:
            mic.unread(extra)
            return "wake"
        log.info("(ignored false wake: %r)", said)
        recent.extend(extra)


class LivePreview:
    """While you talk, re-transcribe what has been said so far every fraction of a second and show
    it in the overlay. The final transcript is made separately; this draft is for display, and
    for judging whether a pause is the end of the command or just a breath (see UNFINISHED)."""

    def __init__(self, transcribe: "Transcriber", overlay) -> None:
        self.transcribe, self.overlay = transcribe, overlay
        self.thread = None
        self.last = ""        # newest draft transcript of the current recording
        self.paused = False   # True while the end-of-speech transcript is using the model

    def start(self, frames: list[bytes]) -> None:
        self.frames, self.stopping = frames, threading.Event()
        self.last, self.paused = "", False
        self.transcribe.preview_lang = None
        self.thread = threading.Thread(target=self._loop, daemon=True, name="preview")
        self.thread.start()

    def _loop(self) -> None:
        shown = 0
        while not self.stopping.wait(0.3):
            n = len(self.frames)
            if self.paused or n == shown or n * BLOCK_SEC < 0.6:
                continue
            shown = n
            audio = np.frombuffer(b"".join(self.frames[:n]), np.int16).astype(np.float32) / 32768.0
            try:
                text = self.transcribe.preview(audio)
            except Exception as e:
                log.warning("live preview failed: %s", e)
                return
            if text and not self.stopping.is_set():
                self.last = text
                if self.overlay:
                    self.overlay.text(text)

    def stop(self) -> None:
        if self.thread:
            self.stopping.set()
            self.thread.join(timeout=3)  # the final transcript shouldn't share the GPU with it
            self.thread = None


@dataclass
class Recording:
    audio: np.ndarray
    # (text, language, alternatives) when the transcript was already made while waiting out the
    # end-of-speech pause; None means "transcribe the audio now"
    result: tuple | None = None


# A draft transcript ending like this is a thought in mid-air ("open Chrome and", "send it to"),
# so the pause after it is a breath, not the end: wait longer before closing the recording.
UNFINISHED = re.compile(
    r"(?:,|\b(?:and|but|or|so|then|because|to|the|a|an|of|in|on|at|for|with|from|that|which|if|"
    r"when|while|my|your|is|are|was|um+|uh+|like|into|about|as|by|than|also|plus|make|open|go|"
    r"please|can|could|should|would|will|i|we|it|its|their|his|her|this|these|those|not|just))"
    r"\W*$", re.I)


class _Speculation:
    """Transcribe what has been said so far while the end-of-speech pause runs out. If no more
    speech arrives, the transcript is ready the moment the pause ends, so a longer pause costs
    almost nothing in response time."""

    def __init__(self, transcribe: "Transcriber", frames: list[bytes]) -> None:
        self.result = None
        audio = np.frombuffer(b"".join(frames), np.int16).astype(np.float32) / 32768.0
        self.thread = threading.Thread(target=self._run, args=(transcribe, audio), daemon=True,
                                       name="speculate")
        self.thread.start()

    def _run(self, transcribe, audio) -> None:
        try:
            self.result = transcribe.full(audio)
        except Exception as e:
            log.warning("early transcript failed: %s", e)

    def wait(self) -> tuple | None:
        self.thread.join(timeout=30)
        return self.result


def record_command(mic: Mic, start_timeout: float, overlay=None, preview: LivePreview | None = None,
                   *, transcribe: "Transcriber | None" = None, end_silence: float | None = None,
                   max_len: float | None = None, hold=None) -> Recording | None:
    """Record one utterance. It ends after `end_silence` seconds of quiet, or longer
    (END_SILENCE_UNFINISHED) when the words so far sound unfinished, so a pause to think doesn't
    cut the command off. With `hold` (the hold-to-talk key) it simply records until the key is
    released. Returns None if nothing was said."""
    threshold = max(mic.noise * config.SPEECH_FACTOR, config.MIN_SPEECH_RMS)
    # Once you're talking, quieter sound still counts as speech: trailing words fade.
    keep_going = threshold * getattr(config, "CONTINUE_FACTOR", 0.7)
    end_silence = end_silence or config.END_SILENCE
    long_silence = max(end_silence, getattr(config, "END_SILENCE_UNFINISHED", end_silence))
    max_len = max_len or config.MAX_COMMAND
    early_after = min(getattr(config, "EARLY_TRANSCRIPT_AFTER", 0.9), end_silence)
    preroll: list[bytes] = []
    frames: list[bytes] = []
    waited = silence = 0.0
    early: _Speculation | None = None
    try:
        while True:
            data = mic.read()
            level = mic.rms(data)
            if overlay:
                overlay.level(level / (threshold * 4))
            if hold is not None:
                frames.append(data)
                if preview and len(frames) == 1:
                    preview.start(frames)
                if not hold.held() or len(frames) * BLOCK_SEC >= max_len:
                    break
                continue
            if not frames:
                if level > threshold:
                    frames.extend(preroll + [data])
                    if preview:
                        preview.start(frames)
                else:
                    preroll = (preroll + [data])[-3:]
                    waited += BLOCK_SEC
                    if waited >= start_timeout:
                        return None
                continue
            frames.append(data)
            if level > keep_going:
                silence = 0.0
                if early is not None:      # you carried on: that early transcript is stale
                    early = None
                    if preview:
                        preview.paused = False
            else:
                silence += BLOCK_SEC
            if transcribe is not None and early is None and silence >= early_after:
                if preview:
                    preview.paused = True
                early = _Speculation(transcribe, list(frames))
            unfinished = bool(preview and preview.last and UNFINISHED.search(preview.last))
            if (silence >= (long_silence if unfinished else end_silence)
                    or len(frames) * BLOCK_SEC >= max_len):
                break
    finally:
        if preview:
            preview.stop()
    if hold is not None and len(frames) * BLOCK_SEC < 0.4:
        return None  # a tap, not a hold
    audio = np.frombuffer(b"".join(frames), np.int16).astype(np.float32) / 32768.0
    return Recording(audio, early.wait() if early is not None else None)


# ---------------------------------------------------------------- speech to text

# Whisper's spellings of "Abacus": abacus, abbacus, abakus, abacas, "a bacus", "abba cus"...
ABACUS = r"a?\W?bb?a\W?(c|k)+\W?(u|a|o|i)s"
WAKE_PREFIX = re.compile(rf"^\W*(hello|hey|hi)?[\s,]*{ABACUS}\w*\W*", re.I)
# What Whisper must hear for a wake to count: Abacus (or a near-miss spelling) as its own word.
WAKE_CONFIRM = re.compile(rf"\b{ABACUS}\b", re.I)
# "type Blinding Lights." -> "Blinding Lights" (Whisper adds the capital and the full stop).
# One short leading word is allowed: the command recording can start with the tail of the wake
# word ("cus, type ...") when Vosk fired early.
TYPE_COMMAND = re.compile(rf"^\W*(?:\w*(?:{ABACUS}|[ck][ua]s)\W+)?(?:please\s+)?(?:type|dictate)\b[\s:,]*(?P<words>.+?)\s*$",
                          re.I | re.S)
# "Abacus, dictate" on its own: listen for a longer passage and type it.
DICTATE_SESSION = re.compile(r"^(?:please )?(?:start |begin )?(?:dictation|dictating|dictate|"
                             r"take (?:a )?(?:dictation|note)|take this down|write this down)"
                             r"(?: mode)?$", re.I)
NOISE_TRANSCRIPTS = {"", "you", "thank you", "thanks for watching", "bye"}
_POLITE = r"(?:(?:okay|ok|please|now|hey|so|and)[, ]+)*"
_TAIL = r"(?:[, ]+(?:again|now|please|from now on|for now|alone|then|anymore|any more))*"
_PTR = r"(?:my|the|that)\s+(?:mouse|pointer|cursor)"
MOUSE_ON = re.compile(
    rf"^{_POLITE}(?:(?:you (?:can|may)|go ahead and|feel free to|start)\s+)?"
    rf"(?:use|using|take|control|have|borrow|take over|take control of)\s+{_PTR}{_TAIL}$"
    rf"|^{_POLITE}(?:turn on|enable|switch on)\s+(?:the\s+)?mouse(?: mode| control)?{_TAIL}$"
    rf"|^{_POLITE}(?:turn |switch )?mouse(?: mode| control)? on{_TAIL}$", re.I)
MOUSE_OFF = re.compile(
    rf"^{_POLITE}(?:hands off|don'?t (?:use|touch|control|move)|do not (?:use|touch|control|move)|"
    rf"stop (?:using|controlling|moving|touching)|leave|stay off|keep off|let go of|"
    rf"give (?:me )?back)\s+{_PTR}{_TAIL}$"
    rf"|^{_POLITE}(?:turn off|disable|switch off)\s+(?:the\s+)?mouse(?: mode| control)?{_TAIL}$"
    rf"|^{_POLITE}(?:turn |switch )?mouse(?: mode| control)? off{_TAIL}$"
    rf"|^{_POLITE}(?:(?:switch to|go to|use|work in)\s+)?(?:the\s+)?background(?: mode| only)?{_TAIL}$",
    re.I)


def mouse_toggle(plain: str) -> bool | None:
    """True / False if the command just switches mouse control on / off, else None."""
    plain = plain.replace("’", "'")
    if MOUSE_OFF.match(plain):
        return False
    if MOUSE_ON.match(plain):
        return True
    return None


class Transcriber:
    def __init__(self) -> None:
        import os
        import warnings
        os.environ.setdefault("HF_HUB_DISABLE_SYMLINKS_WARNING", "1")
        warnings.filterwarnings("ignore", message=".*symlinks.*")
        add_cuda_dlls()
        for noisy in ("faster_whisper", "httpx"):  # per-clip INFO lines would flood the log
            logging.getLogger(noisy).setLevel(logging.WARNING)
        from faster_whisper import WhisperModel
        self.langs = list(getattr(config, "LANGUAGES", ["en"])) or ["en"]
        self.last_lang = self.langs[0]
        self.last_alts: list[tuple[str, str]] = []  # other readings, for Claude to weigh
        self.preview_lang: str | None = None         # live overlay's language for this command
        self.vocab_mtime, self.learned = None, {}
        self.lock = threading.Lock()   # the live preview and the final transcript share one model
        device = getattr(config, "WHISPER_DEVICE", "auto")
        self.model = None
        if device in ("auto", "cuda"):
            try:
                log.info("Loading speech model '%s' on the GPU (first run downloads it)...",
                         config.WHISPER_MODEL)
                self.model = WhisperModel(config.WHISPER_MODEL, device="cuda",
                                          compute_type="int8_float16")
                self._run(np.zeros(SAMPLE_RATE // 2, np.float32), "en")  # load CUDA libs now
            except Exception as e:
                if device == "cuda":
                    raise
                log.warning("GPU speech model failed (%s); using the CPU.", e)
                self.model = None
        if self.model is None:
            name = getattr(config, "WHISPER_CPU_MODEL", config.WHISPER_MODEL)
            log.info("Loading speech model '%s' on the CPU...", name)
            self.model = WhisperModel(name, device="cpu", compute_type="int8")

    def _prompt(self, lang: str) -> str | None:
        """Vocabulary that primes the decoder: config.VOCABULARY plus words learned in vocab.json
        (re-read whenever the file changes, so a newly learned name helps the very next command)."""
        try:
            mtime = VOCAB_FILE.stat().st_mtime
            if mtime != self.vocab_mtime:
                self.vocab_mtime, self.learned = mtime, json.loads(VOCAB_FILE.read_text("utf-8"))
        except FileNotFoundError:
            self.learned = {}
        except Exception as e:
            log.warning("vocab.json unreadable (%s)", e)
        words = list(self.learned.get(lang, []))[-40:]  # Whisper reads only ~220 prompt tokens
        base = getattr(config, "VOCABULARY", {}).get(lang) or ""
        prompt = " ".join(x for x in (base, ", ".join(words)) if x).strip()
        return prompt or None

    def _detect(self, audio: np.ndarray) -> dict:
        with self.lock:
            _, _, probs = self.model.detect_language(audio)
        return dict(probs)

    def preview(self, audio: np.ndarray) -> str:
        """Quick draft transcript for the live overlay: one language, greedy decoding. The
        language is chosen once per command (Whisper's detector, English if it's fairly sure)."""
        if self.preview_lang is None:
            if len(self.langs) == 1 or audio.size < SAMPLE_RATE:
                lang = self.langs[0] if len(self.langs) == 1 else "en"
            else:
                p = self._detect(audio)
                indic = [L for L in self.langs if L != "en"]
                lang = ("en" if "en" in self.langs and p.get("en", 0) >= 0.5
                        else max(indic, key=lambda L: p.get(L, 0)) if indic else self.langs[0])
                self.preview_lang = lang
        else:
            lang = self.preview_lang
        text, _ = self._run(audio, lang, beam=1)
        return WAKE_PREFIX.sub("", text).strip()

    def _run(self, audio: np.ndarray, lang: str, beam: int | None = None) -> tuple[str, float]:
        if beam is None:
            beam = 1 if lang == "en" else getattr(config, "WHISPER_BEAM", 1)
        with self.lock:
            segs = list(self.model.transcribe(audio, language=lang, beam_size=beam,
                                              without_timestamps=True,
                                              condition_on_previous_text=False,
                                              initial_prompt=self._prompt(lang))[0])
        text = " ".join(s.text for s in segs).strip()
        score = sum(s.avg_logprob for s in segs) / len(segs) if segs else -9.0
        return text, score

    def _pick_language(self, audio: np.ndarray) -> tuple[str, str, list[tuple[str, str]]]:
        if len(self.langs) == 1:
            return self.langs[0], self._run(audio, self.langs[0])[0], []
        # Whisper's own detector mistakes Telugu for Hindi, so it is trusted only for clear
        # English and as a tie-breaker; the real test is which language transcribes most confidently.
        prob = self._detect(audio)
        if "en" in self.langs and prob.get("en", 0) >= 0.7:
            return "en", self._run(audio, "en")[0], []
        results = {}
        for lang in self.langs:
            text, score = self._run(audio, lang)
            score -= 1.5 * wrong_script_share(text, lang)   # e.g. Telugu written in Devanagari
            score += 0.1 * prob.get(lang, 0)                # small nudge from the detector
            results[lang] = (score, text)
        best = max(results, key=lambda L: results[L][0])
        log.info("Language scores: %s -> %s",
                 {L: round(r[0], 2) for L, r in results.items()}, best)
        # Telugu and Tamil score almost alike on short commands, so Claude (which reads both
        # well) gets the other readings too and decides what was actually said.
        alts = [(L, results[L][1]) for L in results
                if L != best and results[L][1]
                and (L != "en" or results[best][0] - results[L][0] < 0.3)
                and (best != "en" or results[best][0] - results[L][0] < 0.3)]
        return best, results[best][1], alts

    def full(self, audio: np.ndarray) -> tuple[str, str, list[tuple[str, str]]]:
        """(text, language, alternative readings). Safe to call from any thread."""
        lang, text, alts = self._pick_language(audio)
        text = WAKE_PREFIX.sub("", text).strip()
        if text.lower().strip(" .!?") in NOISE_TRANSCRIPTS:
            text = ""
        return text, lang, alts

    def __call__(self, audio: np.ndarray) -> str:
        text, self.last_lang, self.last_alts = self.full(audio)
        return text

    def heard_wake(self, audio: np.ndarray) -> tuple[bool, str]:
        with self.lock:
            segments, _ = self.model.transcribe(
                audio, language="en", beam_size=1, without_timestamps=True,
                condition_on_previous_text=False)
            text = " ".join(s.text for s in segments).strip()
        return bool(WAKE_CONFIRM.search(text)), text


SAMPLE_VOICES = ["en-GB-RyanNeural", "en-GB-ThomasNeural", "en-US-ChristopherNeural",
                 "en-US-AndrewNeural", "en-US-GuyNeural"]


# ---------------------------------------------------------------- the assistant

CANCEL_WORDS = {"never mind", "nevermind", "cancel", "nothing", "stop"}
# "make this more formal" style requests about selected text (see abacus_flow.edit_selection)
EDIT_TARGET = re.compile(r"\b(this|that|it|selection|selected|highlighted|text|paragraph|sentence|"
                         r"message|email|grammar|spelling|typos?)\b", re.I)
# What the overlay says while the agent works, by tool name (first match wins).
PROGRESS = [
    ("ui_tweak_save", "Saving the change so it sticks"),
    ("ui_tweak", "Checking your saved tweaks"),
    ("app_ui_open", "Opening the app's interface"),
    ("app_ui_screenshot", "Checking how it looks"),
    ("app_ui_try_css", "Trying the new look"),
    ("app_ui", "Studying the app's interface"),
    ("javascript_tool", "Working inside the page"),
    ("navigate", "Opening the page"),
    ("tabs_", "Looking at your tabs"),
    ("read_page", "Reading the page"), ("get_page_text", "Reading the page"),
    ("find", "Finding it on the page"),
    ("form_input", "Filling it in"),
    ("claude-in-chrome__computer", "Working in Chrome"),
    ("list_windows", "Looking at your windows"), ("read_window", "Reading the window"),
    ("window_screenshot", "Looking at the window"), ("screenshot", "Looking at the screen"),
    ("background_click", "Clicking"), ("click", "Clicking"), ("act", "Pressing it"),
    ("type_text", "Typing"), ("press_keys", "Pressing keys"), ("scroll", "Scrolling"),
    ("move_mouse", "Moving the pointer"),
    ("WebSearch", "Searching the web"), ("WebFetch", "Reading a web page"),
    ("Bash", "Running a command"), ("PowerShell", "Running a command"),
    ("Read", "Reading a file"), ("Write", "Writing a file"), ("Edit", "Editing a file"),
    ("Grep", "Searching files"), ("Glob", "Searching files"),
]


class Assistant:
    """Everything that happens after Abacus has heard something: route it to the right place
    (a local switch, dictation, a selection edit, or the agent) and report back."""

    def __init__(self, backend, voice: Voice, overlay=None, mic: Mic | None = None,
                 transcribe: "Transcriber | None" = None, preview: LivePreview | None = None,
                 hold=None) -> None:
        self.backend, self.voice, self.overlay = backend, voice, overlay
        self.mic, self.transcribe, self.preview, self.hold = mic, transcribe, preview, hold

    # ---- overlay shorthands
    def show(self, state: str, text: str = "") -> None:
        if self.overlay:
            self.overlay.show(state, text)

    def hide(self, after: float = 0.0) -> None:
        if self.overlay:
            self.overlay.hide(after)

    # ---- routing
    def command(self, text: str, lang: str = "en", alts: list[tuple[str, str]] | None = None) -> None:
        plain = text.lower().strip(" .!?,")
        if plain in CANCEL_WORDS:
            self.hide()
            self.voice.say("Okay.")
            return
        toggle = mouse_toggle(plain)
        if toggle is not None:
            self.set_mouse(toggle, text)
            return
        if DICTATE_SESSION.match(plain) and self.mic is not None:
            log.info("You: %s  (dictation)", text)
            self.dictate(start_timeout=config.DICTATE_START_TIMEOUT, passage=True)
            return
        typed = TYPE_COMMAND.match(text)
        if typed:
            # The user's own words, typed where the cursor is: no agent round trip.
            words = typed.group("words").strip("\"'")
            log.info("You: %s  (typing)", text)
            self.smart_type(words, lang)
            chime(high=False)
            return
        if abacus_flow.looks_like_edit(text) and (EDIT_TARGET.search(text) or len(text.split()) <= 3):
            ctx = abacus_flow.capture_context()
            if len(ctx.selection.strip()) >= 2 and self.edit_selection(text, ctx, lang):
                return
        self.ask(text, lang, alts or [])

    def set_mouse(self, allowed: bool, text: str = "") -> None:
        abacus_state.set_mouse(allowed)
        log.info("You: %s  (mouse control %s)", text, "on" if allowed else "off")
        reply = ("Very good, sir. I'll use your mouse when a job calls for it." if allowed else
                 "Hands off the mouse, sir. I'll work in the background.")
        self.show("reply", reply)
        self.voice.say(reply)
        self.hide(after=1.5)

    # ---- dictation
    def smart_type(self, raw: str, lang: str = "en", ctx=None) -> None:
        """Clean up dictated words for where they are going, then type them there."""
        ctx = ctx or abacus_flow.capture_context()
        if abacus_flow.needs_model(raw):
            self.show("polishing", raw)
        started = time.time()
        text, how = abacus_flow.polish(self.backend, raw, ctx, lang)
        result = abacus_flow.insert(text, ctx)
        log.info("(dictation into %s [%s]: %r -> %r; %s, %s, %.1fs)", ctx.app or "?",
                 ctx.field or "?", raw, text, how, result, time.time() - started)
        self.show("typed", text)
        self.hide(after=1.5)

    def edit_selection(self, instruction: str, ctx, lang: str = "en") -> bool:
        """Rewrite the selected text as asked. False if the words weren't an edit request."""
        self.show("polishing", instruction)
        out = abacus_flow.edit_selection(self.backend, instruction, ctx, lang)
        if out is None:
            return False
        log.info("You: %s  (edited %d selected chars -> %d)", instruction, len(ctx.selection), len(out))
        ctx.before = ""   # replacing the selection: no spacing fix-up
        abacus_flow.insert(out, ctx)
        self.show("typed", out)
        self.hide(after=2.0)
        chime(high=False)
        return True

    def dictate(self, start_timeout: float, hold=None, passage: bool = False) -> None:
        """Record what is said next and type it. Used by a Gesture Control click in a text box,
        by "Abacus, dictate" (passage=True: longer pauses allowed, up to MAX_DICTATION) and by
        the hold-to-talk key (hold given: records until the key is released)."""
        ctx = abacus_flow.capture_context()   # before recording: the cursor is where it matters
        if hold is None:
            chime(high=False)
        self.show("dictating")
        long_form = passage or hold is not None
        rec = record_command(
            self.mic, start_timeout, self.overlay, self.preview, hold=hold,
            transcribe=None if hold is not None else self.transcribe,
            end_silence=(getattr(config, "DICTATION_END_SILENCE", config.END_SILENCE)
                         if passage else config.END_SILENCE),
            max_len=getattr(config, "MAX_DICTATION", 120) if long_form else config.MAX_COMMAND)
        if rec is None:
            log.info("(dictation: nothing said)")
            self.hide()
            return
        if hold is not None:
            hold.wait_released()   # never type while Ctrl or Win is still physically down
        text, lang, _ = rec.result or self.transcribe.full(rec.audio)
        if not text or text.lower().strip(" .!?") in CANCEL_WORDS:
            log.info("(dictation: cancelled or not caught)")
            self.hide()
            return
        if len(ctx.selection.strip()) >= 2 and abacus_flow.looks_like_edit(text):
            if self.edit_selection(text, ctx, lang):
                return
        self.smart_type(text, lang, ctx)
        if hold is None and config.DICTATE_ENTER:
            import abacus_keys
            abacus_keys.press("enter")

    # ---- the agent
    def _progress(self, kind: str, name: str, data: dict) -> None:
        if kind != "tool":
            return
        label = next((words for key, words in PROGRESS if key in name), None)
        if label is None:
            label = name.rsplit("__", 1)[-1].replace("_", " ").capitalize()
        detail = ""
        if "navigate" in name and data.get("url"):
            detail = " " + re.sub(r"^https?://(www\.)?", "", str(data["url"])).split("/")[0]
        elif name.startswith("mcp__abacus__app_ui") and data.get("app"):
            detail = f" ({data['app']})"
        log.info("  ...%s%s", label[0].lower() + label[1:], detail)
        self.show("thinking", label + detail)

    def ask(self, text: str, lang: str, alts: list[tuple[str, str]]) -> None:
        self.show("thinking", text)
        notes = [f"Mouse control: {'on' if abacus_state.mouse_allowed() else 'off'}."]
        if lang != "en" or alts:
            name = LANG_NAMES.get(lang, lang)
            log.info("(spoken in %s; alternatives %s)", name, alts)
            other = "; ".join(f'as {LANG_NAMES.get(L, L)}: "{t}"' for L, t in alts)
            notes.append(f"Speech recognition: probably {name}."
                         + (f" It also heard it {other}." if other else "")
                         + " Work out what Venu actually said. Reply in English.")
        prompt = f"[{' '.join(notes)}] {text}"
        box: dict = {}

        def run() -> None:
            try:
                box["reply"] = self.backend.ask(prompt, on_event=self._progress)
            except TimeoutError:
                box["reply"] = "That took too long, so I stopped."
            except Exception:
                log.exception("agent turn failed")
                box["reply"] = "Sorry, something went wrong on my side."

        worker = threading.Thread(target=run, daemon=True, name="ask")
        worker.start()
        # The agent is already working. Only fill the silence if it isn't done almost at once.
        worker.join(timeout=getattr(config, "ACK_AFTER", 1.5))
        if worker.is_alive():
            self.voice.say(random.choice(ACKS))
        worker.join()
        reply = box.get("reply") or "Done."
        self.show("reply", clean_for_speech(reply))
        self.voice.say(reply)
        self.hide(after=2.5)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--list-devices", action="store_true")
    parser.add_argument("--text", help="send one typed command instead of listening")
    parser.add_argument("--voices", action="store_true")
    parser.add_argument("--quiet", action="store_true", help="print replies instead of speaking")
    parser.add_argument("--check", action="store_true",
                        help="load everything, report how long each part took, and exit")
    args = parser.parse_args()
    setup_logging()

    if args.voices:
        voice = Voice()
        for name in SAMPLE_VOICES:
            config.VOICE = name
            print(name)
            voice.say(f"Good evening. This is {name.split('-')[2].replace('Neural', '')}. All systems are online.")
        print('Put your favourite in config.py as VOICE = "..."')
        return

    if args.list_devices:
        for i, d in enumerate(sd.query_devices()):
            if d["max_input_channels"] > 0:
                print(f"{i:3}  {d['name']}")
        return

    voice = Voice(mute=args.quiet or args.check)
    backend = make_backend()
    atexit.register(backend.close)
    if args.text:
        abacus_tweaks.start_services()
        Assistant(backend, voice).command(args.text)
        return

    # One listener at a time: Gesture Control starts Abacus too, and two copies would both answer.
    import ctypes
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateMutexW.restype = ctypes.c_void_p
    _instance = kernel32.CreateMutexW(None, False, "Local\\AbacusVoiceAssistant")  # noqa: F841 (held until exit)
    if ctypes.get_last_error() == 183 and not args.check:  # ERROR_ALREADY_EXISTS
        log.info("Abacus is already running; not starting a second copy.")
        return

    started = time.time()
    backend.warm(abacus_flow.SYSTEM)   # both model lanes start now, while the speech models load
    abacus_tweaks.start_services()
    from vosk import KaldiRecognizer, Model, SetLogLevel
    SetLogLevel(-1)
    rec = KaldiRecognizer(Model(str(VOSK_MODEL)), SAMPLE_RATE,
                          json.dumps(config.WAKE_PHRASES + ["[unk]"]))
    transcribe = Transcriber()
    requests = DictationRequests() if config.DICTATE_ON_CLICK else None
    hold = abacus_flow.HoldKey(getattr(config, "DICTATE_HOTKEY", None))
    hold = hold if hold.parts else None
    overlay = None
    if getattr(config, "OVERLAY", True):
        from abacus_overlay import Overlay
        overlay = Overlay()
    preview = (LivePreview(transcribe, overlay)
               if getattr(config, "LIVE_TRANSCRIPT", True) else None)
    if args.check:
        log.info("Speech models ready in %.1fs.", time.time() - started)
        t = time.time()
        out = backend.quick(abacus_flow.SYSTEM, "<mode>dictate</mode>\n<app>check</app>\n"
                            "<field>Edit, multi-line</field>\n<language>English</language>\n"
                            "<speech>um this is a a test, actually no, a check</speech>")
        log.info("Dictation clean-up lane: %.1fs -> %r", time.time() - t, out)
        t = time.time()
        log.info("Agent lane: %r (%.1fs)",
                 backend.ask("[Mouse control: on.] Systems check: reply with the single word ready."),
                 time.time() - t)
        log.info("Mouse control: %s. Hold-to-talk key: %s. GUI tweaks saved: %d.",
                 "on" if abacus_state.mouse_allowed() else "off",
                 getattr(config, "DICTATE_HOTKEY", None) if hold else "off",
                 len(abacus_tweaks.all_tweaks()))
        return
    mic = Mic()
    assistant = Assistant(backend, voice, overlay, mic, transcribe, preview, hold)
    voice.say(random.choice(["Abacus online. At your service, sir.", "Abacus online.",
                             "Good to be back, sir. Abacus online."]))
    mic.flush()
    log.info("Listening for: %s  (close this window to quit)", ", ".join(config.WAKE_PHRASES))

    while True:
        try:
            why = wait_for_wake(mic, rec, transcribe, requests, hold)
            if why == "click":
                assistant.dictate(start_timeout=config.DICTATE_START_TIMEOUT)
                mic.flush()
                continue
            if why == "hold":
                assistant.dictate(start_timeout=0, hold=hold)
                mic.flush()
                continue
            backend.prepare()   # a stale conversation is swapped out while you speak
            if overlay:
                overlay.show("listening")
            chime()
            timeout = config.COMMAND_START_TIMEOUT
            while True:
                recording = record_command(mic, timeout, overlay, preview, transcribe=transcribe)
                if recording is None:
                    if overlay:
                        overlay.hide()
                    break
                text, lang, alts = recording.result or transcribe.full(recording.audio)
                if not text:
                    if overlay:
                        overlay.show("error")
                        overlay.hide(after=2.0)
                    voice.say(random.choice(NOT_CAUGHT))
                else:
                    if overlay:
                        overlay.text(text)  # the final, more careful transcript
                    assistant.command(text, lang, alts)
                mic.flush()
                if config.FOLLOWUP_WINDOW <= 0:
                    break  # back to waiting for the wake word
                chime(high=False)  # follow-up window open
                timeout = config.FOLLOWUP_WINDOW
        except KeyboardInterrupt:
            raise
        except Exception:
            log.exception("loop error")
            if overlay:
                overlay.show("error", "Something went wrong; details are in abacus.log.")
                overlay.hide(after=3.0)
            voice.say("Sorry, something went wrong.")
            mic.flush()


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        pass
