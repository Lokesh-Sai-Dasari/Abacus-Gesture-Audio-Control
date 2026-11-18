# GUI tweaks: how Abacus changes an interface and keeps it changed

A tweak is CSS (rarely a script) saved for one website or one desktop app. Saved tweaks are
re-applied automatically every time the site or app opens. Saving again with the same target
and name replaces the old version, so iterate freely.

## Websites (Chrome)

1. Open the site with the Chrome tools (tabs_context_mcp, navigate) and look at the real DOM to
   find stable selectors: `find` / `read_page` to locate the thing, then
   `javascript_tool` to read tag names, ids, attributes and classes
   (for example `document.querySelector('...').outerHTML.slice(0, 400)`). Prefer element names,
   ids, `aria-label`, `role`, `data-*` and `href` selectors over random-looking class names.
2. Save it: `ui_tweak_save_css(target="youtube.com", name="Hide Shorts", css="...")`.
   The Abacus Tweaks extension applies it to open tabs within about five seconds and on every
   later visit. The tool result says whether the extension is connected.
3. Verify: wait a few seconds, take a screenshot of the tab (Chrome `computer` screenshot), and
   check the change is visible and nothing else broke. Fix and save again if needed.
4. If the extension has never connected, the tweak is saved but invisible. Tell Venu it will
   show once he loads the extension (chrome://extensions, Developer mode, Load unpacked, the
   `extension` folder inside the Abacus folder). You may show him the effect now by injecting
   a `<style>` with `javascript_tool`, but say that this preview lasts only until reload.

A target covers the domain and its subdomains (`youtube.com` covers `www.` and `m.`).
Use `music.youtube.com` to target only that subdomain.

## Desktop apps built on Chromium (Spotify, Discord, VS Code, Slack, Notion, Obsidian, Teams...)

1. `app_ui_open(app="Spotify.exe")`. If it says the app is running without the debugging port,
   call again with `restart=true` (a music player may simply be restarted; for apps with
   possible unsaved work ask Venu first, then pass `confirmed=true`).
2. `app_ui_inspect(app)` shows the theme's CSS variables, the big regions with their colours and
   the scroll containers. `app_ui_inspect(app, selector="...")` shows what a selector matches.
3. `app_ui_try_css(app, css)` applies CSS without saving; `app_ui_screenshot(app)` shows the
   result. Repeat until it looks right: readable text everywhere, no leftover patches.
4. `ui_tweak_save_css(target="app:Spotify.exe", name="Light mode", css=...)` saves it, applies
   it, and updates the app's shortcuts so it survives restarts. Then clear the trial with
   `app_ui_try_css(app, "")` and take one last screenshot.

If `app_ui_open` reports that the app has no debugging port after starting, it is not a
Chromium app: use its own settings (read_window + act, or the mouse) or Windows settings.

## CSS that works

- Tweak CSS is added after the page's own styles. Use `!important` on every declaration.
- Hide: `selector { display: none !important; }`. Use `visibility: hidden` to keep the space.
- Class names that look random (`.aB3x_9Q`) change with updates; avoid them when anything
  stable exists. Attribute selectors help: `[data-testid="now-playing-bar"]`,
  `a[href^="/shorts"]`, `[aria-label="Shorts"]`.
- `:has()` is available: `ytd-rich-item-renderer:has(a[href^="/shorts"]) { display:none !important; }`.
- Remote `url()` and `@import` are stripped from tweaks; use colours, gradients or `data:` URIs.

### Light mode for a dark interface (or dark for a light one)

Best: override the theme's colour variables that `app_ui_inspect` lists, on the same selector
that defines them, then fix hard-coded colours you still see in the screenshot. Spotify example
(its variables live on `.encore-dark-theme`):

```css
.encore-dark-theme, .encore-dark-theme .encore-base-set, .encore-dark-theme > * {
  --background-base: #ffffff !important;      --background-highlight: #f0f0f0 !important;
  --background-press: #e2e2e2 !important;     --background-elevated-base: #ffffff !important;
  --background-elevated-highlight: #f0f0f0 !important; --background-elevated-press: #e2e2e2 !important;
  --background-tinted-base: #0000000f !important; --background-tinted-highlight: #0000001a !important;
  --background-tinted-press: #00000029 !important;
  --text-base: #121212 !important;            --text-subdued: #5c5c5c !important;
  --essential-base: #121212 !important;       --essential-subdued: #8a8a8a !important;
  --decorative-base: #121212 !important;      --decorative-subdued: #dcdcdc !important;
  --parents-essential-base: #121212 !important;
  color-scheme: light !important;
}
html, body, #main, #global-nav-bar, [data-testid="now-playing-bar"] { background-color: #f3f3f3 !important; }
```

After that, look for what is still dark or unreadable (search box, cards with white titles,
gaps between panels) with `app_ui_inspect(app, selector)` and add rules for them.

Quick and universal fallback when a theme has no variables: invert everything, then invert
pictures back.

```css
html { filter: invert(1) hue-rotate(180deg) !important; background: #fff !important; }
img, video, picture, canvas, [style*="background-image"] { filter: invert(1) hue-rotate(180deg) !important; }
```

### Stopping endless scrolling

Infinite feeds load more when a sentinel element at the bottom scrolls into view. Hiding the
sentinel stops the loading, with CSS alone. Find it by scrolling to the bottom and inspecting
the last element (a spinner, "continuation", "sentinel", "load more").

To stop a page from scrolling at all: `html, body { overflow: hidden !important; }`, or set
`overflow: hidden !important` on the scroll container that `app_ui_inspect` lists.

### YouTube starting points (check them against the live page; YouTube changes its markup)

```css
/* No endless feed: stop home, search and related lists loading more */
ytd-continuation-item-renderer, #continuations, tp-yt-paper-spinner { display: none !important; }

/* No Shorts: shelves, grid items, search results, sidebar entries */
ytd-rich-shelf-renderer[is-shorts], ytd-reel-shelf-renderer, grid-shelf-view-model,
ytd-rich-section-renderer:has(ytd-rich-shelf-renderer[is-shorts]),
ytd-rich-item-renderer:has(a[href^="/shorts"]), ytd-video-renderer:has(a[href^="/shorts"]),
ytd-guide-entry-renderer:has(a[title="Shorts"]), ytd-mini-guide-entry-renderer[aria-label="Shorts"],
a[href^="/shorts"] { display: none !important; }

/* Other common requests */
#comments { display: none !important; }                 /* comments */
#secondary #related { display: none !important; }       /* suggested videos beside the player */
.ytp-endscreen-content, .ytp-ce-element { display: none !important; }   /* end-screen cards */
```

"Remove the scrolling feature on YouTube" most likely means the endless feed and/or Shorts'
swipe-to-next. Apply the endless-feed rule; if he meant Shorts, hide Shorts too. Say which you
did.

## Scripts (only when CSS cannot do it)

`ui_tweak_save_script(target, name, js, css="")`. The script runs on every load, so make it
idempotent, small, and free of network requests; scope it with a check such as
`if (location.pathname.startsWith('/shorts'))`. On websites, script tweaks need "Allow User
Scripts" switched on for the extension; the tool result tells you if it is off. Example,
redirect Shorts to the normal player:

```js
const fix = () => { const m = location.pathname.match(/^\/shorts\/([\w-]+)/); if (m) location.replace('/watch?v=' + m[1]); };
fix(); document.addEventListener('yt-navigate-start', fix);
```

## Undo and housekeeping

- `ui_tweaks_list()` shows every tweak and its id; `ui_tweak_change(id, "remove")` deletes one,
  `"off"` / `"on"` suspends or resumes it.
- After removing a website tweak, open tabs lose the styling within seconds. After removing an
  app tweak the app is restored at once, and when its last tweak goes its shortcuts are put back.
- Tell Venu what you changed in one sentence, and that "Abacus, undo that" reverses it.
