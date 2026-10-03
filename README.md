# Mercury Admin Map

A map of demo.mercury.com in the Admin view: 915 screens (pages, modals, drawers, menus, form states, validation errors, confirmations), 1,125 links between them, 66 user journeys and 236 design-system issues, shown on a pan-and-zoom canvas.

## Open it

Live at <https://mercury-admin-map.netlify.app> (public, `noindex`). Locally, open `dist/index.html`: it runs from `file://`, no server needed.

- **Journeys** view: each journey is a row of numbered screenshots joined by the action that leads to the next screen. States outside any journey sit under "Other screens" in each area.
- **Flow graph** view: every screen and link in an area, laid out as a graph.
- Click a screen to open its full screenshot, notes, issues, and where it leads to and comes from. Arrow keys step through a journey.
- **Audit** tab: 15 patterns that recur across areas come first, then every issue by category and severity. Clicking one highlights the screens where it shows up.
- Drag to pan; scroll, pinch or Shift+scroll to zoom; F to fit. Hold **Z** and click to zoom in on that point, or **Z**+**Option** and click to zoom out. Zoom in and sharper screenshots load in place of thumbnails.
- **Links**: the URL always describes what is on screen, so any view can be shared or bookmarked, and Back/Forward step through selections. Parameters: `view=graph`, `q=<search>`, `journey=`, `issue=` or `pattern=<id>` (highlight), `screen=<nodeId>` (open screen), `lane=<journeyId>` (which journey row that screen sits in), `at=<x>,<y>,<zoom>` (viewport centre in map units; added once you pan or zoom). Example: `?view=graph&journey=home.j-clear-approvals&screen=home.task-invite-review`. Old `#<nodeId>` links still work.
- **C** centres the selected screen; **Esc** closes the panel, then clears the highlight.
- Click the screenshot in the panel to view it full screen: click, scroll, pinch or **+**/**−** to zoom, drag or arrow keys to pan, **0** to fit, **Esc** to close. Z+click and Z+Option+click zoom in and out there too. Cmd/Ctrl-click or middle-click opens the image in a new tab. The path under the title opens that page on demo.mercury.com.
- **Data for agents**: `agent/README.md` (also `llms.txt` at the root) explains the ticket-ready export. Each issue in the screen panel, and the banner while an issue is highlighted, links to its ticket under `agent/tickets/<issue id>.md` ("Agent instructions"). It is a black-box audit; findings must be verified against Mercury's internal conventions before anyone files them.

## Files

| Path | What it holds |
|---|---|
| `data/<area>.json` | One manifest per area: nodes, edges, journeys, issues |
| `screens/<area>/*.png` | Source screenshots, 2880×1800 (1440×900 at 2x) |
| `patterns.json` | Patterns that recur across areas, pointing at issue ids |
| `src/` | Viewer source: `main.js` (state, input, sidebar, drawer), `render.js` (canvas renderer), `images.js` (level-of-detail loader), `layout.js` (layouts, shared with the build), `index.html` and `app.css` (template and Mercury tokens) |
| `tools/prerender.mjs` | Computes both layouts at build time (dagre runs here, not in the page) |
| `tools/export_agent_data.py` | Writes `dist/agent/` and `dist/llms.txt`: ticket-ready issues, patterns as epics, screens, journeys |
| `tools/link-punctuation/` | Re-runnable check for punctuation inside link text: `sweep.sh` scans every captured route in 8 headless agent-browser sessions, `judge.py` scores each candidate with Jev (needs `JEV_API_KEY` in `.env`). `judged-2026-09-25.json` holds the last run |
| `assets/` | Mercury's Arcadia webfonts and the `--ds-*` token dump they came with (`mercury-theme.json`) |
| `build.py` | The whole pipeline, see below |
| `audit/` | Chrome DevTools MCP scans used to check cross-page findings |
| `a11y/` | Committed accessibility-audit data: the mutation policy, replay recipes, each run's ledger (`runs/<id>/`, without its raw files and screenshots) and the published snapshot (`published/`) that `build.py` merges |
| `tools/a11y/` | The accessibility-audit tooling: `audit.py` runs every stage, see [`tools/a11y/README.md`](tools/a11y/README.md) |
| `screens-1x/`, `data-1x-backup/` | First pass at 1x, kept for reference; safe to delete |

## Build and deploy

```sh
npm install                      # esbuild + dagre, first time only
python3 build.py                 # writes dist/
MAP_BASE_URL=https://mercury-admin-map.netlify.app python3 build.py   # absolute links in agent tickets
netlify deploy --prod --dir dist
```

`build.py` merges the manifests, encodes each screenshot three ways (400px and 800px lossy, 2880px lossless WebP; only changed PNGs are re-encoded), packs every screen into one 128×80 sprite sheet, runs the layout prerender and esbuild, inlines data, layouts, app and fonts into `dist/index.html`, writes the agent export, renders the accessibility report when `a11y/published/` exists, and adds `robots.txt` and Netlify `_headers` (`X-Robots-Tag: noindex`). A `data/*.json` file that does not parse is left out of the map and the agent export, and the build prints `WARN <file>: invalid JSON (<error>)` as it finishes. `tools/export_agent_data.py` run on its own stops on that file instead.

## Accessibility audit

A repeatable WCAG 2.2 AA audit of every screen in the map. A scripted Playwright sweep replays each screen on demo.mercury.com and measures it, Sonnet agents try to disprove every finding on the live demo, and the verified findings are published to `a11y/published/`. `build.py` merges that snapshot into the map, as `a11y` issues on their screens, and into the agent export.

- **Report:** [`dist/a11y/index.html`](dist/a11y/index.html), built only when `a11y/published/` exists. It covers what was tested and what was not, findings by WCAG principle, and what changed since the last published run. Each row opens the map with that issue's screens highlighted.
- **Run, resume or refresh it** with the `a11y-audit` skill in Claude Code: [`.agents/skills/a11y-audit/SKILL.md`](.agents/skills/a11y-audit/SKILL.md). It stops before anything reaches an audience and never deploys.
- **Run stages by hand,** read the file formats, or follow a fixture run from plan to publish: [`tools/a11y/README.md`](tools/a11y/README.md). `npm run a11y:test` runs the tooling's tests.

## How the viewer renders

The map is one viewport-sized `<canvas>`, redrawn only when something changes and at most once per frame. The first version put 915 DOM cards and 1,125 SVG paths on a single scaled `will-change: transform` layer about 40,000px wide; zooming exceeded Chrome's tile memory, so cards, regions and even the sidebar stopped painting. With the canvas, GPU memory is bounded by the viewport.

- **Level of detail.** Each card draws the smallest image that is sharp at its on-screen size: an atlas tile, then 400px, 800px, and the lossless original once zooming stops. Decoded images live in a per-level LRU cache so memory stays bounded.
- **Atlas.** It ships as one file, then is cut into 915 small `ImageBitmap`s. In Chrome's canvas, drawing a sub-rect of a big bitmap costs about 70 times more than drawing a small bitmap. The tiles are cropped from a decoded `ImageBitmap`: cropping from the `<img>` re-decodes the whole sheet for each tile, which froze Chrome for over a minute.
- **Cached overview.** Zoomed far out, the map is painted once per scale into a bitmap and blitted. It is rebuilt only when focus, theme or layout change.
- **Edges.** Batched per area when zoomed out and culled one by one when zoomed in. Cross-area links are solid because Skia dashes the whole path, including the off-screen part.
- **Region titles.** Drawn in screen space at sizes rounded to 0.5px, so zooming reuses cached glyphs.

Measured with a scripted pan, zoom, fit and click run in Chrome 153 (1440×900 at 2x), as total main, GPU, compositor and raster busy time: 28.5s for the DOM version, 4.3s for the canvas version. Main thread went from 18.2s to 2.0s, and time in style recalc, layout, paint and layerize from 17.0s to 0.25s.

## Capture notes

- Captured with agent-browser at `set viewport 1440 900 2`. Every page loaded headlessly, so the Claude-in-Chrome fallback was never needed. Perks is the only page left out, because it links to mercury.com.
- After the laptop sleeps, freshly launched headless Chrome hangs on screenshots. Launch sessions with `--args $'--disable-gpu\n--disable-gpu-compositing\n--disable-gpu-vsync\n--disable-frame-rate-limit'`.
