# Prompt: map and audit a product

Paste everything below the line into Claude Code (or a similar agent with subagents and browser tools). Fill in the three placeholders first. Everything else is settled in the scoping conversation.

- `{{PRODUCT_URL}}`: where the product runs (demo, staging or production)
- `{{PRODUCT_NAME}}`: what to call it
- `{{WORKDIR}}`: an empty directory for the project

---

You will build a map and audit of {{PRODUCT_NAME}} at {{PRODUCT_URL}}. The map is an interactive, zoomable canvas. Its cards are the product's screens: pages, modals, drawers, menus, form states, validation errors, empty states, toasts and confirmations. Arrows labeled with the user action connect each screen to the screens it leads to. On top of the map sits an audit: every UX, content, accessibility and design-system issue you find, with verbatim evidence, grouped into patterns that recur across the product. It also includes a data export that another agent can use to verify issues and file tickets without ever opening the map.

Work in {{WORKDIR}}. Run the work in six phases. Phases 1, 3 and 5 fan out to subagents. Phase 2 is a conversation with me. It is a hard gate: capture nothing until I sign off on the scope.

## Ground rules

- **Done means done.** Deliver every item in the signed-off scope. If something is truly blocked, finish the rest and name the specific blocker in one sentence.
- **Black-box honesty.** Unless I give you internal docs, you are auditing from outside. Every finding is a hypothesis. Say so at the top of every deliverable. Do not dress up an inference as a confirmed defect.
- **Evidence at capture time.** Record rendered text, computed styles and accessibility-tree attributes while the screen is live. Do not reconstruct evidence from screenshots later.
- **Do no harm to real data.** Submit forms, send money or messages, delete or invite only when the scope allows it, and only in a sandbox.
- **Ask before anything reaches an audience.** Deploying, sharing links and filing tickets all need my go-ahead. Research, drafts, rebuilds and re-runs don't: do them, then tell me.
- **Parallelism without conflicts.** Each subagent owns a set of files that no other subagent touches. The main thread merges.
- **Model choice.** Use a fast model (e.g. Sonnet) for capture, bulk edits, sweeps and verification. Use the strongest model (e.g. Opus) for scoping, synthesis, the renderer and performance work.
- **Write plainly.** Short sentences, active voice, no hype, numbers over adjectives. This applies to issue text, READMEs and messages to me.

## Phase 1: Gather context (parallel, read-only)

Spawn these subagents at the same time. None of them writes outside `{{WORKDIR}}/context/`.

1. **Surface recon.** Open the product, walk the global navigation, and read any sitemap, route manifest or public docs. Output `context/areas.md`: each top-level area, its entry URL, sub-sections, and an estimated count of screens and states (the order of magnitude matters more than the exact number). Flag areas that leave the product (external links, other domains), areas behind feature flags, and areas that need a different role.
2. **Environment and access.** Find out what login this needs, which roles exist (admin, member, read-only…), whether the product is a resettable sandbox with seeded data, and what a mutation would affect. Check for bot detection or rate limits that break headless browsing. Output `context/environment.md`.
3. **Tooling inventory.** Check which browser automation works here: a headless CLI such as agent-browser or Playwright, Chrome DevTools MCP (for computed styles, the accessibility tree and performance traces), and a driver for my real, logged-in browser (e.g. Claude in Chrome) as a fallback. Take a test screenshot at 1440×900 @2x with each one. Check for `cwebp`, Pillow, Node and esbuild. Output `context/tooling.md`, with the exact launch flags that worked.
4. **Standards to audit against.** If I have given you a design system, token file, component library, content style guide or accessibility commitment, summarize the rules you can check mechanically. If not, list the public standards you will fall back on (WCAG 2.2 AA, platform conventions) and say plainly that you have no house rules. Output `context/standards.md`.

Then write `context/brief.md`: one page, with what the product is, its size, what you can and cannot reach, and the risks. Give it to me with the Phase 2 questions.

## Phase 2: Scope it with me (conversation, hard gate)

Ask in rounds of up to four questions, each with options and a recommended default. Don't ask about anything you can decide from context. Cover:

1. **Purpose and audience.** Who uses the map and audit, and what do they do with it? For example: design-system team triage, a pitch, onboarding, a pre-redesign inventory. This decides depth and tone.
2. **Coverage.** Which areas and which roles. Which state kinds count as separate screens (pages only, or also modals, drawers, menus, popovers, form states, validation errors, empty states, toasts, confirmations). Whether error and edge states are in (empty lists, long names, zero balances, permission denials).
3. **Mutation policy.** May you submit forms, create and delete records, or trigger emails? Is there a sandbox that resets?
4. **Capture spec.** Viewport and device pixel ratio (default 1440×900 @2x), whether to add a mobile pass, light and/or dark theme, and how to mask personal data. Lock this now: recapturing hundreds of screens is the most expensive redo in the whole project.
5. **Audit dimensions.** Default categories: copy, data-format, component, interaction, layout, a11y, color, typography, iconography. Default impact classes: data-integrity, broken-task, accessibility, consistency, cosmetic. Which to add, drop or weight up (e.g. money movement, permissions, compliance copy)?
6. **Mechanical sweeps.** Which checks should run as re-runnable scripts over every captured route instead of by eye? Examples: date and number formats, casing, punctuation inside link text, missing accessible names, contrast.
7. **Deliverable and hosting.** Local `file://` only, a private static host, or a public static host with `noindex`. May brand fonts be embedded publicly? (Check the license, or have me ask legal.) Any confidentiality limits on screenshots?
8. **Agent export target.** Which tracker the tickets will go to (Linear, Jira, GitHub Issues…), the label and priority conventions, and whether you may file tickets or should only prepare them.
9. **Budget.** Time and tokens, and what to cut first if we run long.

Write `SCOPE.md`: the answers, a numbered list of deliverables, the acceptance criteria for each, and what is out of scope. Show it to me. Start Phase 3 only after I say go.

## Phase 3: Capture and audit (parallel, one subagent per area)

Give each area its own subagent, with a brief that contains `SCOPE.md`, `context/tooling.md` and the schema below. Each one writes only `data/<area>.json` and `screens/<area>/`.

**File naming.** `screens/<area>/NN-slug.png`, numbered in capture order.

**Manifest schema** (`data/<area>.json`):

```json
{
  "area": "payments",
  "title": "Payments: send, transfer, recipients",
  "nodes": [{ "id": "payments.scheduled", "title": "Scheduled tab", "url": "/payments/scheduled",
              "image": "screens/payments/86-payments-scheduled.png", "kind": "page",
              "note": "What is on screen, in one or two factual sentences." }],
  "edges": [{ "from": "payments.index", "to": "payments.scheduled", "action": "Click Scheduled tab" }],
  "journeys": [{ "id": "payments.j-edit-recurring", "name": "Change a recurring payment",
                 "description": "…", "steps": ["payments.index", "payments.scheduled", "…"] }],
  "issues": [{ "id": "payments.i-edit-wipes-due-dates", "title": "…", "detail": "…",
               "evidence": "Verbatim rendered text, computed styles or a11y attributes",
               "severity": "high|medium|low", "category": "…", "impact": "…",
               "confidence": "high|medium|low", "confidenceReason": "…",
               "demoArtifactRisk": false, "nodeIds": ["payments.scheduled-updated"] }],
  "blocked": [{ "what": "…", "why": "…" }]
}
```

`kind` is one of: page, modal, drawer, menu, popover, form-state, validation-error, toast, confirmation, empty-state. Node ids are `<area>.<slug>`, issue ids `<area>.i-<slug>`, journey ids `<area>.j-<slug>`. Once published, an id never changes.

**Capture rules.**

- Wait for network idle and web fonts before each screenshot. Disable animations and hide the caret. Close stray popovers you did not mean to capture.
- Several states share one URL, because modals, menus and form states don't change it. The edge's `action` is how a reader, or an agent, reaches that state. Write it as an instruction: "Click Edit", "Clear the amount, click Save".
- Journeys are the task flows a real user performs, step by step. Screens that belong to no journey stay in the map under "Other screens".
- Audit while you capture. For each issue, record the evidence on the live page. DevTools evidence (computed style, class, accessibility-tree attribute, geometry) beats quoted rendered text, and quoted rendered text beats a description.
- Watch for state bugs, not only visual ones: lists that don't refresh, a value that changes on rows you didn't touch, Back or Escape losing work, times shown in UTC. They are rarer than visual issues and matter more.
- Set `demoArtifactRisk: true` when seeded data, a frozen clock or the sandbox could explain a finding.
- If headless capture fails (bot checks, auth, GPU hangs after the machine sleeps), retry once with the fallback flags from `context/tooling.md`. Then use the real-browser driver. If that fails too, log it under `blocked` and move on.
- Stay inside the product. External destinations get a single edge to a stub node, or are left out and listed under `blocked`.

While the area agents run, the main thread writes the mechanical sweep scripts from Phase 2 (`tools/sweeps/`). Run them over every captured URL once capture finishes. Where a check needs judgment (e.g. whether punctuation belongs inside a link), score each candidate with a model call and save the judged results with a date. The sweep can then be re-run and diffed later.

## Phase 4: Synthesize and verify

1. **Patterns** (strong model, main thread or one subagent). Read every issue across areas and group those with a single shared fix (one date formatter, one amount component, one validation pattern) into `patterns.json`: `id`, `title`, `summary`, `issueIds`, `areas`. A pattern needs issues from at least two areas.
2. **Adversarial verification** (fast model, parallel, one subagent per area, read-only on manifests; each returns verdicts, not edits). For every high and medium issue, try to disprove it on the live product: reproduce it, measure it in DevTools, and check whether the standards allow it. Return `confirmed`, `downgrade` or `drop`, with a reason. The main thread applies the verdicts.
3. **Triage.** Suggested priority = impact × severity:

   | Impact | high | medium | low |
   |---|---|---|---|
   | data-integrity | P1 | P2 | P2 |
   | broken-task | P1 | P2 | P3 |
   | accessibility | P1 | P2 | P3 |
   | consistency | P2 | P3 | P4 |
   | cosmetic | P3 | P4 | P4 |

   Low confidence lowers the priority by one step. A P1 or P2 with `demoArtifactRisk` says so in its rationale.

## Phase 5: Build the viewer and the agent export

### Build pipeline (`build.py` or equivalent, one command)

- Merge the manifests. Validate them: missing images, edges that point at unknown nodes, unknown journey steps and self-loops each produce a warning line. Drop the broken record and continue the build.
- Encode each screenshot at three levels of detail: ~400px lossy, ~800px lossy (top crop at card aspect ratio) and the full-size lossless original. Re-encode only changed sources.
- Pack every screen into one sprite atlas of small tiles (e.g. 128×80) for zoomed-out views. Hash the file name so it busts the cache.
- **Compute layouts at build time** (e.g. dagre in Node), not in the page.
- Bundle the app and inline the data, layouts, JS, CSS and fonts into one `index.html` that runs from `file://` with no server. Web fonts must be inlined as data: URIs, because `file://` pages cannot fetch fonts (font requests are CORS-mode and the origin is null).
- If the site will be hosted: write `robots.txt`, `noindex` meta and `X-Robots-Tag` headers, cache headers for images, and correct content types for `.md`, `.jsonl` and `llms.txt`. Take the base URL from an environment variable so exported links are absolute.
- Print the stats and the total size of `dist/`.

### Renderer: rules learned the hard way

- **One viewport-sized `<canvas>`**, opaque (`alpha: false`). Redraw only when something changes, at most once per animation frame. Do not render hundreds of DOM cards on a scaled `will-change: transform` layer. Past a certain zoom it exceeds Chrome's tile memory, and cards, regions, even the sidebar stop painting. With one canvas, GPU memory is bounded by the viewport, not the world.
- **Level of detail.** Each card draws the smallest image that is sharp at its on-screen size: atlas tile, then 400px, then 800px, then the lossless original once zooming stops. Queue loads nearest the viewport centre first, cap in-flight loads per level, decode off-thread (`img.decode()` then `createImageBitmap`), and evict per level with an LRU budget, calling `bitmap.close()`.
- **Slice the atlas at load, and crop from a decoded `ImageBitmap`.** `createImageBitmap(img, sx, sy, sw, sh)` on an `HTMLImageElement` re-decodes the whole sheet on every call. With ~900 tiles, that froze Chrome for over a minute while Safari was fine. Drawing a sub-rect of a large bitmap is also ~70× slower in Chrome than drawing a small standalone bitmap.
- **Cached overview.** When zoomed far out, paint the whole map once per scale step into an offscreen bitmap (≤4096px a side, only ever scaled down) and blit it. Rebuild only when focus, theme or layout changes.
- **Edges.** Batch them into one `Path2D` per area and style (normal, highlighted, dimmed) when zoomed out. Cull them one by one when zoomed in. Avoid dashed strokes on long paths: Skia dashes the entire path, including the off-screen part.
- **Text.** Draw labels in screen space at font sizes rounded to 0.5px so zooming reuses cached glyphs. Cache text measurement and wrapping.
- **Theme.** Read colors and fonts from CSS custom properties into the renderer, and re-read them on theme change so dark mode works on the canvas too.

### Interactions

- Pan by dragging. Zoom with the wheel, pinch, Shift+scroll and the trackpad, anchored at the cursor. **F** fits, **C** centres the selection, **+/−** zoom. **Esc** closes the innermost thing first (lightbox, then panel, then highlight).
- Two views: **Journeys** (each journey is a row of numbered screenshots joined by the action label, with loose states under "Other screens" per area) and **Flow graph** (every screen and edge per area).
- A sidebar with Journeys, Screens and Audit tabs, plus search across titles, URLs and issues. The Audit tab lists patterns first, then issues by category and severity. Selecting one highlights its screens and dims the rest.
- A detail panel for the selected screen: large screenshot, note, issues, and where it leads to and comes from (clickable). Arrow keys step through the current journey.
- A full-screen lightbox for the lossless screenshot, with zoom and pan. Cmd/Ctrl-click and middle-click open the image in a new tab. The product URL opens the live page.
- A minimap with the viewport rectangle; clicking it jumps.
- **The URL always describes what is on screen**: `view`, `q`, `journey`/`issue`/`pattern` (highlight), `screen` (selection), `lane` (which journey row, since one screen can appear in several), `at=x,y,zoom`. Selections push history so Back and Forward work. Pan and zoom replace the current entry. Old `#<nodeId>` links keep working.

### Accessibility of the viewer itself

A canvas is invisible to assistive tech, so the DOM carries the full equivalent. Every screen, journey, pattern and issue is reachable and operable from the keyboard through the sidebar, with real buttons, tabs with the right roles and states, labeled icon buttons, visible focus, focus moved into the panel on open and restored on close, and `prefers-reduced-motion` respected for animated camera moves. The canvas has an `aria-label` explaining how to operate it.

### Agent export (`dist/agent/`, plus `llms.txt` at the root)

Write this for an agent with no context and a limited window:

- `README.md` (identical copy as `AGENTS.md`). The black-box caveat comes first, then what this is, the counts, a file table, a recommended workflow (read patterns, then the index, pick by priority, verify, dedupe, file, link, leave a trail), the full schemas, a ticket template, the triage rubric, one complete example record, and a mapping to the tracker from Phase 2.
- `llms.txt`: a short index of these files.
- `issues-index.json`: one short row per issue, small enough to read whole. Triage starts here.
- `issues.json` (with `meta`: caveat, counts, schema version) and `issues.jsonl` (one record per line, for grep and jq).
- `tickets/<id>.md`: ready-to-paste ticket bodies.
- `patterns.json`: each pattern framed as an epic (`suggestedEpic` with goal, approach, done-when and child tickets), plus the issues outside any pattern grouped by area.
- `screens.json` and `journeys.json`, with absolute screenshot, product and viewer URLs, and `leadsTo[]` actions.

Every issue record carries: a stable `id`; a `dedupeKey` (`<audit-slug>:<id>`) to put in every ticket; an imperative `title`; `summary`; `problem`; verbatim `evidence` with an `evidenceType` (`devtools`, `rendered-text` or `observed`); `severity`, `impact`, `category` and `priority` {`level`, `rank`, `rationale`}; `confidence` with a reason; `demoArtifactRisk`; `affectedScreens[]`; `reproduce` {`method`: `journey`, `edge-path` or `url`, `steps[]`}, where steps are derived from the recorded journey or the shortest recorded edge path; `fixHypothesis`; observable `acceptanceCriteria`; `labels` including `needs-verification`; `searchTerms` for duplicate search; and a `verifyBeforeFiling` checklist.

## Phase 6: Test and verify

Run these in parallel where they don't share state. Report results with numbers.

1. **Build checks.** Zero warnings, or each remaining warning explained. Every node has three image levels and an atlas tile.
2. **Performance: interaction.** A scripted run of pan, zoom sweep, fit, select and journey stepping, traced with Chrome DevTools. Report main-thread, GPU, compositor and raster busy time, and the longest task. Budget: no task over 100 ms during interaction, and a smooth frame rate while panning at full zoom-out.
3. **Performance: load.** Profile from navigation start, not after a fixed delay. A benchmark that starts late hides load-time freezes, which is exactly where the atlas bug lived. Report time to first map paint and the longest task during load.
4. **Real browsers.** Repeat the above in my real Chrome (through the real-browser driver), and smoke-test Safari and Firefox. Headless Chrome with GPU flags off is not representative.
5. **Memory.** Take a heap snapshot after a full zoom-in/zoom-out sweep across the whole map, and check that decoded images stay within the LRU budgets.
6. **Functional.** Scripted tests for URL round-trips (load every parameter combination and assert the state), Back and Forward, keyboard shortcuts, search, the Esc order, lightbox zoom and `file://` loading with fonts.
7. **Accessibility.** Lighthouse or axe on the DOM shell, plus a keyboard-only walk-through: reach one issue's screens without a mouse.
8. **Agent-friendliness.** A fresh subagent with no prior context gets only the site URL (or `dist/`) and the task "find the P1 issues, verify the top one against the live product, and draft the ticket for {{TRACKER}}". It passes if it finds `llms.txt`, stays within a modest context budget and produces a correct ticket without reading `issues.json` whole. Fix whatever confused it.
9. **Data QA.** Re-verify a random 10% of issues against the live product. If more than a few fail, re-run Phase 4 verification on that area.
10. **Visual check.** Screenshots at four zoom levels, in light and dark, and at a narrow window width. Look at them yourself before showing me.

## Deliver

- `dist/`: the viewer and the agent export, at full image quality. Never downsample to fit an upload limit unless I ask.
- `README.md`: how to open, use, rebuild and deploy it; how the renderer works and why; capture notes and gotchas.
- A short report to me: counts (screens, edges, journeys, issues by priority, patterns), the performance numbers from Phase 6, what was blocked and why, and the three findings you would act on first.
- Hosting: prepare the deploy command, then ask me before running it.
