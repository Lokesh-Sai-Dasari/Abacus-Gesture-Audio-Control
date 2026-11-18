# Abacus - Gesture/Audio Control

Control a Windows PC with your hands and your voice. A webcam turns hand gestures into mouse,
scroll and window actions, and a voice assistant called **Abacus** takes spoken commands,
cleans up dictation and can operate apps and websites on your behalf.

The two halves run as one program: starting Gesture Control starts Abacus, and closing it stops
both. They also hand work to each other. Clicking into a search box with a gesture starts
dictation, and when Abacus needs the real mouse it first parks the gesture pointer.

## Gesture Control (`GestureControl/`)

| Hand | Gesture | Action |
|---|---|---|
| Right | Point the index finger | Move the pointer (tilt the finger and/or move the hand) |
| Right | Lift the middle finger | Click (twice quickly = double click) |
| Right | Open the hand | Let go of the pointer to re-centre |
| Left | Point up / down | Scroll, continuously while held |
| Left | Point left / right | Previous / next tab, or previous / next app over the desktop |
| Left | Pinch / spread | Minimize / maximize the window |
| Both | Open palms, fingers outward | Alt+Tab view; move a hand to pick, tap to open |
| Either | Snap | Play / next track in Spotify |

How it works, and the decisions that mattered:

- **Hand tracking** is MediaPipe's HandLandmarker on the webcam feed (OpenCV, MJPG for 30 fps).
- **Hybrid pointer.** Aiming with the finger's 3-D angle is precise but tiring, and moving the
  whole hand covers distance but is shaky. The pointer adds both, with movement measured from
  the wrist so a tilt that pivots at the wrist is not counted twice. Each half has its own
  One Euro filter, and a 250 Hz glide thread keeps the cursor smooth between camera frames.
- **Clicking without moving.** An early forward-tap click dragged the pointer off target, so the
  click became a middle-finger lift, and the pointer freezes while the lift happens.
- **Calibration and learning.** `--calibrate` fits the smoothing and click thresholds to your
  hand. Settings are kept per desk setup (laptop alone vs. docked with the camera off to the
  side), and a learned camera angle switches on a "side view" mode for the docked case. Click
  thresholds keep adapting while you use it.

## Abacus voice assistant (`Abacus/`)

Say **"Abacus, ..."** followed by a command in one breath. See [`Abacus/README.md`](Abacus/README.md)
for the full command list.

- **Listening:** a small Vosk model spots the wake word cheaply, and faster-whisper
  (large-v3-turbo on the GPU) confirms it and transcribes the command. It understands English,
  Telugu and Tamil and replies in English.
- **Thinking:** commands go to Claude through the Claude Code CLI, kept warm as two
  long-running stream-json processes: a faster model for dictation clean-up and a stronger one
  with tools for real tasks.
- **Doing:** an MCP server gives the agent Windows tools built on UI Automation, so it can read
  and press controls in a window without taking over your mouse. Clicks, typing, Chrome
  automation and a browser extension that keeps interface changes ("hide Shorts on YouTube")
  are also available.
- **Smart dictation:** "Abacus, type ..." or holding Ctrl+Win removes filler words and
  self-corrections, then types the text in a style that suits the app it is going into.
- **Safety:** routine actions run directly. Anything risky is blocked or shown as a Yes/No
  dialog, and messages to other people are only drafted until you say "Abacus, send it".
- **Feedback:** a small click-through overlay shows when it is listening, the live transcript,
  and the reply, which is also spoken with a neural voice (edge-tts).

## Setup (Windows 10/11, Python 3.12)

```powershell
git clone https://github.com/Lokesh-Sai-Dasari/Abacus-Gesture-Audio-Control
cd Abacus-Gesture-Audio-Control
py -3.12 -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
```

Download the two models (they are not stored in the repo):

- `GestureControl/models/hand_landmarker.task` from
  [MediaPipe Hand Landmarker](https://ai.google.dev/edge/mediapipe/solutions/vision/hand_landmarker#models)
- `Abacus/models/vosk-model-small-en-us-0.15/` (unzipped) from
  [alphacephei.com/vosk/models](https://alphacephei.com/vosk/models)

Abacus also needs the [Claude Code](https://claude.com/claude-code) CLI on your PATH, signed in.
For GPU transcription, also install `nvidia-cublas-cu12` and `nvidia-cudnn-cu12==9.*`;
without a GPU it falls back to a smaller model on the CPU.

Run:

```powershell
python GestureControl\gesture_control.py --calibrate   # first time: fit it to your hand
python GestureControl\gesture_control.py               # gestures + Abacus
python Abacus\abacus.py                                # voice only
```

Useful flags: `gesture_control.py --dry` (no input is sent), `--selftest` (camera and model check),
`abacus.py --check` (silent health check), `abacus.py --text "..."` (type a command instead of speaking).
Tuning constants are at the top of `gesture_control.py` and in `Abacus/config.py`.

## Notes

- Windows only: it uses Win32, UI Automation and Windows media controls directly.
- Abacus runs on your own Claude Code login, and edge-tts is an unofficial client for
  Microsoft's voices.
- Built with Claude Code as a coding assistant.
