# Abacus

Voice assistant powered by your Claude Code login. It starts and stops with **Gesture Control**
(Desktop shortcut). For voice only, double-click **Abacus** on the Desktop. Only one copy ever runs.

1. Wait for "Abacus online."
2. Say **"Abacus, ..."** and your command in one breath (or say "Abacus", hear the rising chime, then speak).
3. After the reply, Abacus goes back to waiting for its name. Nothing is recorded until you say it again.
4. Close the window to quit.

## What you can say

| You say | What happens |
|---|---|
| "Abacus, open Spotify and play something calm" | The agent does it, using the PC, Chrome and (if allowed) your mouse. |
| "Abacus, type I'll be there at five, actually six" | Types **I'll be there at six.** where your cursor is. |
| "Abacus, dictate" | Low chime, then speak a whole passage; it is cleaned up and typed. |
| Hold **Ctrl+Win**, talk, release | The same, with no wake word and no risk of being cut off. |
| Select text, then "Abacus, make this more formal" | Rewrites the selection in place (shorten, fix grammar, translate...). |
| "Abacus, hide Shorts on YouTube" / "make Spotify light mode" | Changes the interface and keeps it changed. See GUI tweaks. |
| "Abacus, undo that" / "put YouTube back" | Removes the tweak. |
| "Abacus, hands off my mouse" / "Abacus, use my mouse" | Switches mouse control off / on. |
| "Abacus, never mind" | Cancels. |

## It waits for you to finish

Abacus ends a command after **2 seconds** of silence (`END_SILENCE`), and waits up to **3.5 seconds**
when what you've said so far sounds unfinished ("open Chrome and ...", `END_SILENCE_UNFINISHED`).
Quiet trailing words still count as speech (`CONTINUE_FACTOR`). The transcript is prepared while
the pause runs out, so the longer pause barely slows the reply. Still cut off? Raise `END_SILENCE`.
For long thoughts, hold Ctrl+Win instead: it records until you let go.

## Smart dictation

Dictated words are cleaned up for where they are going before they are typed:

- "um", "you know" and false starts are removed; "at five, actually six" becomes "at six"
- punctuation, capitals, paragraphs and numbered lists are added; "new line" and "new paragraph" work
- an email reads like an email, a chat message like a chat message, a search box gets plain
  keywords, a code editor gets literal text ("def load underscore dataset" -> `def load_dataset`)
- it continues from the text already in the box (spacing, lower case mid-sentence)
- names in `vocab.json` are spelled your way; "Abacus, when I dictate my email, type ..." saves a
  shortcut in `snippets.json`
- short dictation (six words or fewer, e.g. a song title) is typed at once without the model
- if the clean-up is slow or fails, your raw words are typed, so nothing is lost

Turn it off with `SMART_DICTATION = False`. Change or disable the key with `DICTATE_HOTKEY`.

## Mouse control

`MOUSE_CONTROL = True` (default): Abacus works with your real mouse and keyboard, like a person
at the PC. It waits for your hand to be still, and puts the pointer back afterwards.
`MOUSE_CONTROL = False`: it never moves your pointer and works in the background only
(accessibility actions, shell, Chrome). Say "Abacus, hands off my mouse" or "Abacus, use my mouse"
to switch at any time; the choice is remembered. Emergency stop: slam the mouse into a screen corner.

## GUI tweaks

Ask Abacus to change how a site or app looks or behaves. It writes a small stylesheet, checks
the result, and saves it in `tweaks.json` so the change is still there next time.

**Websites (one-time setup, about 30 seconds):** open `chrome://extensions`, switch on
**Developer mode**, click **Load unpacked**, and choose the `extension` folder inside this folder.
That installs "Abacus Tweaks", which applies your website tweaks on every visit, and keeps
applying them while Abacus is closed. For the rare tweak that needs a script, also switch on
**Allow User Scripts** on the extension's Details page.

**Desktop apps built on Chromium** (Spotify, Discord, VS Code, Slack, Notion, Obsidian...) need no
setup. Abacus restarts the app once with a local debugging port, restyles it through that, and
updates the app's shortcuts so it keeps starting that way. Abacus must be running for an app
tweak to show; when the last tweak for an app is removed, its shortcuts are put back.

**Classic Windows apps** (File Explorer, Notepad, games) can't be restyled this way; Abacus uses
their own settings or Windows' personalisation settings and says so when something isn't possible.

Good to know:
- A debugging port lets any program running on this PC control that app, the same access the
  app's own developer tools give. It listens on this PC only, never the network.
- Tweak stylesheets can't load anything from the internet (remote `url()` and `@import` are removed).
- An app that Windows starts by itself at sign-in (not through its shortcut) comes up without
  the port, so its tweak won't show until you ask Abacus to fix it or reopen the app normally.
- `python abacus.py --text "list my tweaks" --quiet` shows what is saved.

## Speed

Abacus keeps two Claude sessions running in the background, so nothing has to start when you
speak: a small fast model for dictation clean-up (about a second) and the agent for commands
(a simple command answers in 1 to 3 seconds; multi-step jobs take as long as the steps take).
"On it" is only spoken when the answer isn't ready within `ACK_AFTER` seconds, and the pop-up
shows what the agent is doing ("Opening the page", "Trying the new look").

`CLAUDE_MODEL = "sonnet"` and `CLAUDE_EFFORT = "low"` favour speed. Set `CLAUDE_MODEL = None` to use
your Claude Code default model for harder jobs at the cost of a few seconds per command.

Everything model-related sits behind one interface in `abacus_llm.py` (`BACKEND = "claude_code"`).
To ship Abacus as a product, add a backend class there that talks to your own server.

## Pop-up
When Abacus hears its name, a small pill appears at the bottom of the screen and shows your words
live as you speak, then what it is doing, then the reply. It never takes focus and clicks pass
through it. Turn it off with `OVERLAY = False`, or keep it but skip live words with
`LIVE_TRANSCRIPT = False` (this also disables the "sounds unfinished" longer pause).

## Wake word
The offline Vosk model listens for "Abacus". Each time it thinks it heard it, Whisper re-checks
the last 2.5 seconds of audio and must hear it too. Lookalikes are dropped and logged as
`(ignored false wake: ...)` in `abacus.log`.
To allow follow-ups without the wake word again, set `FOLLOWUP_WINDOW = 5`.

## Approvals
`PERMISSION_MODE = "auto"` (default): Claude Code's safety classifier approves routine actions
on its own. Anything it blocks or can't judge pops an **"Abacus wants to act"** dialog showing the
exact command, with **No** as the default button. Typing, clicking, Chrome form filling, and
looking at / restyling an app with CSS are pre-approved in `AUTO_ALLOWED_TOOLS`; shell commands,
file edits, and scripts run inside pages or apps still go through the check.
Set `PERMISSION_MODE = "manual"` to get a dialog for every action that isn't pre-approved.

## Tuning (`config.py`)
- Wrong mic: `python abacus.py --list-devices`, set `MIC_DEVICE`.
- Cut off mid-sentence: raise `END_SILENCE`. Slow to react: lower it.
- Background noise starts or prolongs recordings: raise `MIN_SPEECH_RMS` / `SPEECH_FACTOR` / `CONTINUE_FACTOR`.
- Different voice: `VOICE`, `VOICE_RATE` (`python abacus.py --voices` plays samples).
- Health check without speaking: `python abacus.py --check`.
- Test a command without the microphone: `python abacus.py --text "what's on my calendar" --quiet`.

## Files
`abacus.py` main loop and routing · `abacus_llm.py` model backend (warm Claude sessions) ·
`abacus_flow.py` smart dictation · `abacus_tweaks.py` GUI tweaks engine · `extension/` Chrome
extension · `tweaks_guide.md` restyling recipes the agent reads · `abacus_mcp.py` approval gate
and tools · `abacus_desktop.py` window and mouse control · `abacus_keys.py` typing ·
`abacus_overlay.py` pop-up · `persona.md` how Abacus talks and works · `config.py` settings ·
`models/` offline wake-word model. Files ending in `.bak-1117` are the versions from before
this upgrade.
