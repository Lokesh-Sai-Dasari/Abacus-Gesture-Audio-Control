"""Abacus settings. Edit these, then restart Abacus."""

# Microphone: a substring of the device name (run `python abacus.py --list-devices`).
# None = Windows default input device.
MIC_DEVICE = "Microphone Array (Realtek"

# Wake phrases (lowercase). Vosk only listens for these words, which keeps false triggers low.
# Just the name, Jarvis-style: "Abacus, type Blinding Lights" in one breath.
WAKE_PHRASES = ["abacus"]

# Speech-to-text model for commands. Must be a MULTILINGUAL model (no ".en") to understand
# Telugu/Tamil. large-v3-turbo runs on the GPU in about a second; if the GPU can't be used,
# Abacus falls back to WHISPER_CPU_MODEL on the processor (slower, less accurate).
WHISPER_MODEL = "large-v3-turbo"
WHISPER_CPU_MODEL = "small"
WHISPER_DEVICE = "auto"       # "auto" = GPU if it works, else CPU; or "cuda" / "cpu"

# Languages you speak to Abacus (Whisper codes). Each command is tried in each of these and the
# most confident transcript wins. Fewer languages = faster and fewer mix-ups; one = no guessing.
LANGUAGES = ["en", "te", "ta"]

# Reply voice per language, picked by the reply's script. Empty = always the English VOICE below
# (Abacus understands Telugu/Tamil but answers in English). To let it speak them again, use e.g.
# {"te": "te-IN-MohanNeural", "ta": "ta-IN-ValluvarNeural"} and change the rule in persona.md.
VOICES_BY_LANGUAGE = {}

# Beam search width for Telugu/Tamil (English uses 1 for speed). 5 = a little more accurate.
WHISPER_BEAM = 5

# Words that prime the recognizer, written the way you say them in each script. This is the
# biggest accuracy lever for Telugu/Tamil (it cut the error rate by about 40% in testing):
# app names come out spelled right and in the right script. Add friends' names here too.
# Abacus also learns words on its own into vocab.json when it works out something misheard.
VOCABULARY = {
    "te": "యూట్యూబ్, ఇన్‌స్టాగ్రామ్, వాట్సాప్, స్పాటిఫై, క్రోమ్. ఓపెన్ చెయ్యి, ప్లే చెయ్యి, "
          "మెసేజ్ పంపు, రిప్లై పంపు.",
    "ta": "யூடியூப், இன்ஸ்டாகிராம், வாட்ஸ்அப், ஸ்பாட்டிஃபை, குரோம். திற, போடு, "
          "மெசேஜ் அனுப்பு, பதில் அனுப்பு.",
    "en": "",
}

# Let Abacus use your Chrome (where you're signed in to Instagram, WhatsApp Web, YouTube...)
# through the Claude in Chrome extension. Messages to other people are only typed, never sent,
# until you say "Abacus, send it".
BROWSER = True

# Wake check: every wake-word hit from Vosk is double-checked by running Whisper on the last few
# seconds of audio, so chatter that merely sounds like "abacus" is ignored. Rejections are logged.
WAKE_VERIFY = True

# Voice: a Microsoft neural voice (needs internet; run `python abacus.py --voices` to hear options).
# Good picks: en-GB-RyanNeural (British, calm), en-GB-ThomasNeural, en-US-ChristopherNeural (deep),
# en-US-AndrewNeural (warm). Rate is a percent, pitch is in Hz (negative = deeper).
VOICE = "en-GB-RyanNeural"
VOICE_RATE = "+5%"
VOICE_PITCH = "-4Hz"
VOICE_EFFECT = True     # faint metallic echo, like a voice inside the suit; False = clean
# Offline fallback: substring of an installed Windows SAPI voice ("David", "Zira"); None = default.
SAPI_VOICE_NAME = "David"

# Command recording (seconds).
COMMAND_START_TIMEOUT = 6     # how long to wait for you to start talking after the chime
FOLLOWUP_WINDOW = 0           # seconds to accept a follow-up without the wake word after a reply; 0 = always need it
# How long you can pause before Abacus decides you've finished. Raise END_SILENCE if it still
# cuts you off; lower it if it feels slow to react. (The transcript is prepared while this
# pause runs, so a longer pause adds little to the response time.)
END_SILENCE = 2.0             # this much silence ends your command
END_SILENCE_UNFINISHED = 3.5  # ...unless the words so far sound unfinished ("open Chrome and"): then this much
CONTINUE_FACTOR = 0.7         # once you're talking, sound this fraction of the start level still counts as speech
EARLY_TRANSCRIPT_AFTER = 0.9  # start transcribing after this much silence, in case that was the end
MAX_COMMAND = 45              # hard cap on one command

# Search-box dictation: when a Gesture Control click lands in a text box, the next thing you say
# (no wake word) is typed into it. You hear the low chime when it starts listening.
DICTATE_ON_CLICK = True
DICTATE_START_TIMEOUT = 5     # seconds to start talking after the click, else it gives up quietly
DICTATE_ENTER = False         # True = press Enter after typing (search immediately)

# Smart dictation ("Abacus, type ...", "Abacus, dictate", the hold-to-talk key, search-box
# dictation): your words are cleaned up before they are typed. Fillers go, "at five, actually
# six" becomes "at six", punctuation and lists appear, and the style fits the app you are in
# (email, chat, search box, code). Select text and say "Abacus, make this more formal" to
# rewrite it. False = type exactly what the recognizer heard.
SMART_DICTATION = True
POLISH_MIN_WORDS = 6          # shorter dictation (a song title, a search) is typed at once, no model
POLISH_TIMEOUT = 6            # seconds; if the clean-up takes longer, the raw words are typed
# Hold this key combination and talk; release to type what you said (no wake word, no cut-offs).
# Examples: "ctrl+win", "f9", "ralt", "capslock". None = off.
DICTATE_HOTKEY = "ctrl+win"
DICTATION_END_SILENCE = 2.5   # "Abacus, dictate": pause that ends the passage
MAX_DICTATION = 120           # longest single dictation, seconds

# May Abacus drive your real mouse pointer?
#   True  (default) = yes: it clicks, scrolls and drags with your mouse, like a person at the PC.
#   False           = no: it works in the background only (accessibility actions, shell, Chrome),
#                     and the mouse tools refuse.
# Switch by voice at any time: "Abacus, hands off my mouse" / "Abacus, use my mouse".
MOUSE_CONTROL = True

# GUI tweaks: "Abacus, hide Shorts on YouTube", "make Spotify light mode". Website tweaks are
# applied by the Abacus Tweaks Chrome extension (extension/ folder, load it once); app tweaks
# are injected into Chromium-based apps through a local debugging port. See README.
UI_TWEAKS = True
TWEAKS_PORT = 47616            # local-only port the extension reads the rules from
TWEAKS_PATCH_SHORTCUTS = True  # tweaked apps' shortcuts start them tweak-ready (undone when the last tweak goes)

# Voice-activity detection: speech must be this many times louder than background noise,
# and at least MIN_SPEECH_RMS. Raise these if background noise triggers recording.
SPEECH_FACTOR = 2.5
MIN_SPEECH_RMS = 350

# The model backend (abacus_llm.py). "claude_code" = the `claude` CLI on your Claude subscription,
# kept running between commands so there is no start-up wait. A product build would add another
# backend there and name it here; nothing else changes.
BACKEND = "claude_code"
WORK_DIR = str(__import__("pathlib").Path.home())   # folder Claude starts in
# Speed matters more than depth for a voice assistant. "sonnet" answers a simple command in
# 1-3 s; None = your Claude Code default model (smarter on hard jobs, several seconds slower).
CLAUDE_MODEL = "sonnet"
CLAUDE_EFFORT = "low"         # low / medium / high; None = the model's default
QUICK_MODEL = "haiku"         # dictation clean-up and selection rewrites: about a second
ACK_AFTER = 1.5               # say "On it." only if the answer isn't ready within this many seconds
CLAUDE_TIMEOUT = 15 * 60      # seconds before a single command is abandoned
SESSION_IDLE_RESET = 10 * 60  # start a fresh conversation after this much idle time

# "auto"   = Claude Code's safety classifier approves routine actions on its own and blocks risky ones.
# "manual" = every non-allowlisted action pops the Yes/No dialog.
PERMISSION_MODE = "auto"

# Tools that always run without a check. Everything else (shell commands, file edits, page
# scripts...) goes through PERMISSION_MODE.
AUTO_ALLOWED_TOOLS = [
    # looking
    "Read", "Glob", "Grep", "WebSearch", "WebFetch",
    "mcp__abacus__screenshot", "mcp__abacus__move_mouse", "mcp__abacus__remember_word",
    "mcp__abacus__remember_snippet",
    "mcp__abacus__list_windows", "mcp__abacus__read_window", "mcp__abacus__window_screenshot",
    # hands: typing, keys, clicks, filling boxes, so "enter X" never stalls on a safety check
    "mcp__abacus__act", "mcp__abacus__background_click", "mcp__abacus__click",
    "mcp__abacus__type_text", "mcp__abacus__press_keys", "mcp__abacus__scroll",
    # Claude in Chrome: read pages, open/close tabs, click and fill forms
    "mcp__claude-in-chrome__tabs_context_mcp", "mcp__claude-in-chrome__read_page",
    "mcp__claude-in-chrome__get_page_text", "mcp__claude-in-chrome__find",
    "mcp__claude-in-chrome__navigate", "mcp__claude-in-chrome__tabs_create_mcp",
    "mcp__claude-in-chrome__tabs_close_mcp", "mcp__claude-in-chrome__computer",
    "mcp__claude-in-chrome__form_input",
    # GUI tweaks: looking at an app's interface, trying and saving CSS, undoing a tweak.
    # (Running scripts inside apps and pages, and saving script tweaks, still get checked.)
    "mcp__abacus__ui_tweaks_list", "mcp__abacus__ui_tweak_save_css", "mcp__abacus__ui_tweak_change",
    "mcp__abacus__app_ui_open", "mcp__abacus__app_ui_inspect", "mcp__abacus__app_ui_try_css",
    "mcp__abacus__app_ui_screenshot",
]

# On-screen pop-up: appears when you say "Abacus", shows your words live while you talk, then
# what Abacus is doing and its reply. Click-through and never takes focus.
OVERLAY = True
LIVE_TRANSCRIPT = True   # False = the pop-up shows only the final transcript (less GPU work)
