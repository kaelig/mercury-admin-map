---
title: "feat: Link issues to their agent tickets, add Z-click zoom"
date: 2026-09-29
type: feat
artifact_contract: ce-unified-plan/v1
product_contract_source: ce-plan-bootstrap
execution: code
depth: lightweight
---

# feat: Link issues to their agent tickets, add Z-click zoom

## Goal Capsule

- **Objective:** A person reading an issue in the Mercury Admin Map can open that issue's agent instructions (the ready-to-paste Markdown ticket) in one click. They can also zoom the map by holding Z and clicking, adding Option to zoom out, the way design tools work.
- **Means:** Link each rendered issue to the existing `agent/tickets/<issue id>.md` export (KTD1). Add a held-key zoom tool shared by the map canvas and the screenshot lightbox (KTD3, KTD4).
- **Authority:** User request > this plan > existing code conventions in `src/`.
- **Stop conditions:** Stop if a ticket file is missing for any issue id at build time. Stop if the Z tool cannot be kept from firing while the user types in the search box.
- **Execution profile:** Small viewer change in `src/` plus README. No test suite exists. Proof is a successful `python3 build.py` and scripted checks in a real browser.
- **Finishes and ships:** `ce-work` implements. The calling LFG pipeline reviews and commits. No deploy: deploying to Netlify reaches an audience and needs the user's go-ahead.

## Product Contract

### Summary

Add an "Agent instructions" link to every issue the viewer shows, pointing at its Markdown ticket. Add a Z-held click-zoom tool (Z+click in, Z+Option+click out) to the map and the lightbox.

### Problem Frame

The viewer shows 236 audit issues, and the agent export already writes one Markdown ticket per issue to `dist/agent/tickets/<id>.md`. Nothing in the viewer links the two, so someone triaging an issue on the map has to find its id and build the ticket URL by hand. Zooming works by scroll, pinch, Shift+scroll and the +/− buttons, but no zoom gesture targets a point the user clicks. Designers who use the map expect the Z tool from Figma and Photoshop.

### Requirements

**Ticket links**
- R1. Every issue card in the screen panel ("Design issues on this screen") has a link labeled "Agent instructions" that opens that issue's Markdown ticket.
- R2. When an issue is the active highlight (banner reads "Issue: …"), the banner also links to that issue's ticket.
- R3. Links work on the Netlify site and from `file://` (`dist/index.html` opened directly), and open in a new tab so the map keeps its state.

**Z-click zoom**
- R4. While Z is held, a click on the map canvas zooms in, centred on the click point. With Z and Option (Alt) held, the click zooms out.
- R5. A Z-click only zooms. It does not open the screen under the pointer.
- R6. While Z is held over the canvas, the cursor shows zoom-in, or zoom-out when Option is also held.
- R7. Z does nothing while focus is in a text field, and Cmd/Ctrl+Z is not captured.
- R8. The same Z-click and Z+Option-click gestures work in the full-screen screenshot lightbox.
- R9. The on-canvas hint, the lightbox hint and `README.md` describe the gesture and the ticket link.

### Key Decisions

- Z+click zooms in, Z+Option+click zooms out. (session-settled: user-directed — chosen over other bindings such as double-click or keyboard-only +/−: the user specified the exact gesture.) Governs R4, R8.
- The link targets Markdown agent instructions for the issue. (session-settled: user-directed — chosen over linking the JSON record in `issues.json`: the user prefers Markdown.) Governs R1, R2.

### Scope Boundaries

- No in-page Markdown rendering of the ticket. The link opens the raw `.md`, which is also the URL an agent would fetch.
- No links inside the sidebar Audit list rows. Those rows are `<button>`s, and nesting a link inside a button is invalid HTML. Clicking a row already opens the screen panel with that issue highlighted, and R1 and R2 cover that state.
- No per-pattern Markdown link. Patterns have no per-pattern ticket file, only `patterns.json`.
- No Z-drag marquee zoom (Figma's drag-a-box variant). Z-drag keeps panning as today.
- No deploy to Netlify in this change.

### Sources

- `src/main.js`: `issueHTML`, `applyFocus` (banner), the canvas pointer handlers, `zoomAt`, and the global `keydown` handler.
- `src/lightbox.js`: stage pointer handlers (click toggles zoom, backdrop click closes) and `set(scale, cx, cy)`.
- `tools/export_agent_data.py`: writes `tickets/<issue id>.md` for every issue (`ticketUrl` field).
- `build.py`: `write_host_files` serves `/*.md` as `text/markdown; charset=utf-8`.
- `src/index.html`: the existing relative link `agent/README.md` ("Data for agents") is the precedent for R3.

## Planning Contract

### Key Technical Decisions

- KTD1. **Relative href `agent/tickets/<issue id>.md`.** It matches the existing "Data for agents" link, so the same URL works on Netlify and from `file://` (R3). Issue ids are already URL-safe (`[a-z0-9.-]`), and all 236 ids have a ticket file today. Still pass the id through `encodeURIComponent` inside `esc()` so a future id cannot break the attribute. Open with `target="_blank" rel="noopener"` and the existing `EXT_ICON` plus a visually hidden "(opens in a new tab)", mirroring the demo-path link in the panel header.
- KTD2. **Keep the `text/markdown` content type.** Chromium displays `text/*` types inline except for a short denylist (csv, vcard, calendar and similar), so `text/markdown` should render as plain text, not download. Execution confirms this under a local server that sends `text/markdown` (Python's `http.server` does) and from `file://`. If Chrome downloads instead, the fallback is to scope a `text/plain; charset=utf-8` rule to `/agent/tickets/*` in `write_host_files`. Do not change the site-wide `/*.md` rule.
- KTD3. **One held-key tracker that follows the key labelled Z.** Activate on `e.key.toLowerCase() === "z"` and remember that key's `e.code`. Do not match on `e.code === "KeyZ"` alone: `code` names the physical QWERTY position, so on AZERTY the Z key sends `KeyW` and on QWERTZ it sends `KeyY`. While Alt is down, match on the remembered code, defaulting to `KeyZ` until a plain Z press has been seen. On macOS, Option+Z changes `e.key` (to "Ω" on US layouts), so `e.key` cannot identify the key while Alt is held. Track the Z state and the Alt state on `window` keydown and keyup. This runs before the existing handler's `altKey` early return, which would otherwise swallow Z+Option. The guards apply to keydown only: ignore Z when the target is an input or textarea (R7), when Cmd or Ctrl is down, and on key repeat. A keyup of the remembered key always clears the Z state, whatever its target. Pressing or releasing Meta also clears it, because macOS browsers drop the keyup for a key released while Cmd is held. Reset on window `blur` and `visibilitychange` as well, because a keyup lost to a tab switch would otherwise leave the tool stuck on. Put the tracker in a small module (e.g. `src/zoomtool.js`) that `main.js` and `lightbox.js` both import, since both need it (R8). Directional; exact naming is up to the implementer.
- KTD4. **Zoom step 2× per click on the map, 1.5× in the lightbox.** 2× matches the Figma and Photoshop zoom tools, and the map's range (0.02–2.5) is wide enough that 1.4× (the +/− buttons) feels slow for a point tool. The lightbox keeps its existing 1.5× step so its buttons, keys and Z-click agree. Both reuse the existing clamped zoom functions (`zoomAt` in `main.js`, `set` in `lightbox.js`), so limits and redraw scheduling stay as they are.
- KTD5. **Cursor via a class on the stage, not inline style.** Hover sets `canvas.style.cursor = "pointer"` inline. While the tool is active, a stage class such as `zoom-in` or `zoom-out` must win: either the hover code skips the inline cursor while the tool is active, or the CSS rule uses `!important`. Update the cursor on key changes and on `pointermove` (which carries `e.altKey`) without scheduling a canvas redraw, so the one-draw-per-frame rule holds.

### Assumptions

- R8 (lightbox) extends the user's "zoom in/out feature" to the second zoomable surface for consistency. It is an agent bet, not something the user stated.
- The link label "Agent instructions" reuses the user's own words. "Markdown" may appear in the tooltip.
- A Z-click on the lightbox backdrop zooms and does not close the lightbox. Without Z, the current click-to-close and click-to-toggle behaviour stays unchanged.
- 2× is an acceptable map step (KTD4). The factor lives in one constant.

### Risks

- Chrome may download `text/markdown` instead of showing it (KTD2 has the verification and the fallback).
- A keyup lost while the window is unfocused would leave the Z tool stuck on (KTD3 resets on blur).

## Implementation Units

### U1. Agent-instructions link on issues

- **Goal:** Link every rendered issue to its Markdown ticket (R1, R2, R3).
- **Requirements:** R1, R2, R3; KTD1, KTD2.
- **Dependencies:** none.
- **Files:** `src/main.js` (`issueHTML`, `applyFocus` banner), `src/index.html` (banner markup gains a link slot), `src/app.css` (link styling in `.issue .meta` and in the dark-on-light `.banner`).
- **Approach:**
  1. Add a helper that returns the ticket href for an issue id (KTD1).
  2. In `issueHTML`, add the "Agent instructions" link to the `.meta` row, after the severity, category and "Show all N screens" chip.
  3. In `applyFocus`, show a banner link when `focus.type === "issue"` and hide it for journey, pattern and search focus. Keep the banner text in `textContent`: never build the banner from issue text as HTML.
  4. Style the banner link for the inverted banner colours, and the panel link to match `.u a.path`.
- **Patterns to follow:** the header demo-path link in `openNode` (EXT_ICON plus the `.sr` span), and the `.brand a` styling.
- **Test scenarios:**
  - Open a screen that has issues. Each issue card shows "Agent instructions", and its href resolves to `agent/tickets/<that issue id>.md`.
  - From the Audit tab, click an issue. The banner reads "Issue: …" with a ticket link for that issue, and the highlighted card in the panel links to the same file.
  - Switch focus to a journey, a pattern, then a search. The banner ticket link is hidden in each case.
  - Serve `dist/` over HTTP with a `text/markdown` content type and click a link. The ticket opens in a new tab and displays as text (KTD2). Repeat from `file://`.
  - Every id in `MAP.issues` has a matching file in `dist/agent/tickets/`, checked by a one-off script against the build output.
- **Verification:** All scenarios pass in Chrome. The map keeps its state after a link opens.

### U2. Z-click zoom on the map

- **Goal:** Z+click zooms in and Z+Option+click zooms out at the click point, with cursor feedback (R4–R7).
- **Requirements:** R4, R5, R6, R7; KTD3, KTD4, KTD5.
- **Dependencies:** none (U1 and U2 touch different functions in `src/main.js`, so run them one after the other to avoid edit conflicts).
- **Files:** `src/zoomtool.js` (new, shared tracker), `src/main.js` (pointer `end` handler, hover cursor, stage class), `src/app.css` (`zoom-in` and `zoom-out` cursors on `canvas.world`).
- **Approach:**
  1. Create the tracker (KTD3) with a subscribe hook so callers can react when the Z or Alt state changes.
  2. In the canvas pointer-up `end` path, when the pointer did not move and the tool is active, call `zoomAt(2 or 1/2, x, y)` in stage coordinates (subtract `SR.left` and `SR.top`) instead of `openNode` (R5).
  3. Toggle the stage cursor class from the tracker and from `pointermove`'s `e.altKey` (KTD5).
- **Patterns to follow:** `zoomAt` and the `SR`-relative coordinates used by the wheel handler. Keep input handlers free of layout reads (the `SR` cache comment).
- **Test scenarios:**
  - Hold Z and click an empty spot. Zoom doubles and the world point under the cursor stays under the cursor (check with `window.__map.T` before and after).
  - Hold Z and Option and click. Zoom halves around the click point.
  - Hold Z and click a card. The map zooms and the panel does not open (`screen=` does not appear in the URL).
  - Hold Z and drag. The map pans as before.
  - Focus the search box and type "z". No zoom, and the cursor does not change.
  - Press Cmd+Z. The tool does not activate.
  - Hold Z, switch tabs, release Z, and come back. The tool is off and a click opens a screen.
  - Hold Z, press and release Cmd, then release Z. A plain click on a card opens the screen.
  - Hold Z over the canvas, click into the search box, then release Z. A plain click on the canvas opens a screen.
  - Switch the macOS input source to French AZERTY. The key labelled Z activates the tool, with and without Option, and the key labelled W does not.
  - Hold Z at 250%. A click stays clamped at 250%.
  - Hold Z over a card. The cursor shows zoom-in, not pointer, and adding Option switches it to zoom-out.
- **Verification:** All scenarios pass in Chrome through Claude in Chrome. Real key events are required: Option+Z on macOS is the case `e.code` handles.

### U3. Z-click zoom in the lightbox

- **Goal:** The same gesture works in the full-screen screenshot view (R8).
- **Requirements:** R8; KTD3, KTD4.
- **Dependencies:** U2 (shared tracker).
- **Files:** `src/lightbox.js` (stage pointer-up handling, cursor class), `src/app.css` (lightbox cursors while the tool is active).
- **Approach:** On a click that did not move, if the tool is active, call `set(s × 1.5 or ÷ 1.5, clientX, clientY, true)` in place of both the image click-to-toggle and the backdrop click-to-close. Show zoom-in or zoom-out cursors on the stage while the tool is active.
- **Patterns to follow:** the existing `pts`/`g` gesture tracking and `set` in `src/lightbox.js`.
- **Test scenarios:**
  - Open the lightbox, hold Z and click the image. It zooms 1.5× at the click point.
  - Hold Z and Option and click the image. It zooms out, clamped at fit.
  - Hold Z and click the backdrop. The lightbox stays open and zooms.
  - Release Z and click the backdrop. The lightbox closes as before.
- **Verification:** All scenarios pass in Chrome. Existing lightbox keys (+, −, 0, arrows, Esc) still work.

### U4. Hints and README

- **Goal:** Document the gesture and the link (R9).
- **Requirements:** R9.
- **Dependencies:** U1, U2, U3.
- **Files:** `src/index.html` (`.hint`), `src/lightbox.js` (`.lb-hint`), `README.md` ("Open it" section).
- **Approach:** Add the gesture to the canvas hint by folding it into the existing zoom clause, for example "Drag to pan · scroll, pinch, Shift+scroll or Z+click to zoom (Option+Z+click out) · click a screen for details · C to center it · F to fit". Appending a whole new sentence would push the hint under the minimap at 1440px. Use "Z+click to zoom in, Z+Option+click to zoom out" in the lightbox hint and the README's zoom bullet. Add one README line saying each issue links to its Markdown ticket under `agent/tickets/`. The ticket link belongs only in the README: the lightbox has no issues. Keep the house style: short sentences, no hype.
- **Test expectation:** none — copy only; checked by reading the rendered hint.
- **Verification:** At 1440px the canvas hint does not overlap the minimap (it hides below the mobile breakpoint). The lightbox hint shows the Z-click text without an ellipsis at 1440px; shorten its existing wording if it does not fit.

## Verification Contract

| Gate | How | Applies to |
|---|---|---|
| Build | `python3 build.py` exits 0 with no new `WARN` lines, and `dist/index.html` is regenerated | all units |
| Ticket coverage | One-off script: every issue id in `data/*.json` has `dist/agent/tickets/<id>.md` | U1 |
| Browser, HTTP | Serve `dist/` with `python3 -m http.server` (sends `.md` as `text/markdown`), then run the U1–U3 scenarios in Chrome | U1–U3 |
| Browser, file:// | Open `dist/index.html` directly and click a ticket link | U1 |
| Console | No errors in the console during the scenarios | all |
| Perf | A Z-click goes through `zoomAt`/`moved` and costs the same frames as a +/− button click: one moving frame plus the existing 180 ms settle pass. Cursor-only changes (Z or Option pressed or released, `pointermove` while the tool is active) add no frames to `window.__map.stats.frames` | U2 |

No automated test suite exists in this repo, so none is added.

## Definition of Done

- R1–R9 hold in Chrome over HTTP, and R3 also holds from `file://`.
- `python3 build.py` succeeds.
- Both hints describe the new gesture, and the README describes the gesture and the ticket link.
- No leftover experimental code, debug logging or unused helpers in the diff.
- Not deployed. Deploying is the user's call.
