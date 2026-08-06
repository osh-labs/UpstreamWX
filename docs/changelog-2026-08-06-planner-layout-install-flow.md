# Narrow-phone planner layout + PWA install flow (2026-08-06, PR #163)

Frontend only. No engine, API, or contract change — engine output is unchanged (NFR-4).

Two independent changes on one branch: a mission-planner layout bug that made the planner
unusable on a 393 pt iPhone, and the Add-to-Home-Screen flow (banner + iOS walkthrough).

## 1. Mission planner on a narrow phone

The planner had been tuned against a 428 pt iPhone 13 Pro Max. On a 393 pt device (15/16 Pro)
it broke two ways at once.

**The overflow.** `.mp-fields` used `grid-template-columns: 1fr 1fr`. `1fr` is
`minmax(auto, 1fr)`, so a track cannot shrink below its item's min-content width — and a
`datetime-local` reports a very wide one (282 px in Chromium; iOS renders the full
`Aug 6, 2026 at 22:00`). Both tracks blew past the sheet: the **End** field ran off the screen
edge, and because column 2 started at x≈311 in a 393 pt sheet it took the "Slot canyon"
checkbox with it, which is why that control rendered mangled rather than merely shifted.

**The crushed map.** Every other row was fixed-height and `.mp-map` was the only `flex: 1`, so
the map absorbed the entire shortfall — about **120 pt** of map in Safari, most of it covered by
the layer switcher and attribution.

Fixes:

- Field labels moved **beside** their control instead of above, roughly halving the block's
  height. Each time field takes a full-width row (re-paired at ≥560 px, where a column fits the
  whole locale-formatted value). Two `datetime-local`s side by side genuinely do not fit at
  393 pt: iOS needs ~186 pt each and only ~176 pt is available, so side-by-side would trade a
  visible overflow for text clipped inside the field.
- Grid tracks are `minmax(0, …)` and controls carry `min-width: 0`, so an intrinsic minimum can
  never push a field past the sheet again.
- Chrome trimmed: 8 px section padding, 7 px control padding, 10 px action buttons (still 44 pt,
  the iOS touch minimum), a search-status row that collapses when empty, and **explicit
  `line-height: var(--lh-caption)` on the 11 px labels and ticks** — they were inheriting the
  22 px body leading, costing ~6 px of dead space per row.
- The middle of the sheet is a scroll container (`.mp-body`) with the map floored at 180 px, so a
  viewport too short for even the minimums scrolls instead of crushing the map. The bar and the
  action buttons stay pinned. `.settings-body` got the same treatment — its four slider blocks
  overflow a short phone.
- `.mp-sheet` honours all four safe-area insets (was bottom only).

Map height in Safari on a 393 pt phone: **120 → 236 px**. At 402×874: 275 → 365. At 428×926:
327 → 417. No element extends past the sheet at 375/393/402/428 pt.

## 2. Add-to-Home-Screen flow

### Display-mode helper

`displayMode()` / `isInstalledDisplay()` / `initDisplayMode()` in `frontend/js/app.js` replace a
one-off standalone check that lived inside `initInstallPrompt`.

- Two signals, ORed, because they cover different browsers: `(display-mode: …)` is the standard
  media query but Safari only shipped it in **16.4**, and `navigator.standalone` is the only
  signal on iOS below that.
- Widened past `standalone` to the other installed display modes, so changing the manifest's
  `display` cannot silently make the check false.
- Stamps `<html data-display-mode>` so CSS can branch on it too, and keeps it live — a desktop
  PWA can be popped back into a tab, and `appinstalled` fires mid-session.

**It reports how *this window* is running, not whether the app is installed.** Someone with the
app on their Home Screen who opens the site in a Safari tab reads as `browser` — which is exactly
what the install banner needs to know. The only API for true install state is Chromium's
`getInstalledRelatedApps()`, which does not exist on iOS; we deliberately do not use it.

### Banner

In normal flow as the first child of `.app`, **not** `position: fixed` like the update/busy
banners — those are transient, this one persists until dismissed, so it pushes the header down
rather than covering it. Brand-dim rather than a severity token: an install reminder must not
read as a hazard on the FR-35 ladder.

Dismissal is a **30-day snooze** (`install_nag_dismissed_at` in `uwx.prefs.v1`), not a permanent
mute — offline access matters for an audience that loses signal underground, but a user who said
"not now" gets a month of quiet. Suppressed in `DEMO_MODE`: the demo is a static GitHub Pages
mirror, so installing it would pin the user to sample data.

The status-bar pill and the banner share one `renderInstallUi()` decision, so they can never both
offer install at once; the pill fills in only once the banner is snoozed.

### iOS walkthrough

**"Add to Home Screen" lives in Safari's own share menu and no API opens it.** This is a
deliberate, long-standing iOS restriction. `navigator.share()` is **not** a workaround: it opens
the Web Share sheet (AirDrop, Messages, Copy), which does not contain that action. The install
can therefore only be *shown*, which is what the carousel does.

The carousel is a **scroll-snap track**, so the horizontal swipe is native scrolling — momentum,
rubber-banding and trackpad gestures come free and no touch handlers fight the browser for the
gesture. Dots and the nudge arrow set `scrollLeft`; a rAF-coalesced scroll listener reads the
index back, so pointer, keyboard and programmatic movement converge on one sync path and the dots
cannot drift out of step. `scroll-snap-stop: always` keeps one swipe to one step;
`overscroll-behavior-x: contain` stops a swipe past the last slide triggering back-navigation.
The arrow pulses until the first move and hides on the last slide; the animation is inside a
`prefers-reduced-motion: no-preference` block.

Screenshots (`frontend/img/install/`, spec in its README) are square, 1170 px — exactly 3× the
388 CSS px the slide can reach. They are **JPEG, not PNG**: these are mostly iOS's translucent
blurred menu surfaces, and PNG measured 3–4× larger for identical output. They load from
`data-src` on first open, not on page load, so no visitor who never opens the card — and no
non-iOS visitor — pays for them; they are deliberately not in the `sw.js` precache either, since
the card is only useful while online. A missing file is hidden and the caption stands alone.

Every slide reserves two caption lines: step 3's caption wraps where the others do not, and
because slides are centred that sat its screenshot ~11 px higher than its neighbours' — visible
as a jiggle mid-swipe.

**The captions are pinned to the iOS version captured** (••• → Share → Add to Home Screen, not a
Share button directly in the toolbar). If Apple moves the row again, images and captions must be
updated together.

## Verification

Headless Chromium at 375/393/402/428 pt plus 834×1112 and both landscape orientations. Planner:
no element past the sheet, map floor respected, body scrolls when short. Carousel: open, arrow,
swipe, keyboard, dots, Escape, reopen-from-pill, lazy-load request count (0 before open, 3
after), missing-image fallback, and identical image size and vertical offset across all three
slides. Install matrix: no-prompt / with-prompt / dismiss / reload-after-dismiss / expired-snooze
/ iOS UA / standalone / demo. Backend suite 536 passed, ruff clean.
