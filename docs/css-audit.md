# CSS feature audit

Audit of Net Surfer's CSS support against the MDN CSS reference, done on
2026-10-06 by reading the current `browser/css_parser.py`, `style.py`, `layout.py`,
`paint.py` and `fonts.py` (not just the README). The grid/flexbox work landed the
same day is included. Nothing was changed in the code.

**Update (2026-10-07):** every gap below has been implemented except `@font-face`, which was left out on purpose. Earlier update (2026-10-06): the gaps marked ✅ below in positioning, multi-column,
borders/shadows/outline, `rowspan`, word breaking/ellipsis and scrolling boxes have since
been implemented; their rows were updated in place.

**Legend:** ✅ supported · 🟡 partial · ❌ missing.
**Impact** says how much a gap shows on real pages (Wikipedia's Vector 2022 skin is
used as the main example): **High** = visibly breaks common layouts, **Med** = looks
off on many sites, **Low** = cosmetic or rare.

Where an impact note says how Wikipedia uses a feature, that is from general knowledge
of the skin, not from loading the page in this audit.

---

## Biggest gaps at a glance

| Gap | Category | Impact | Status |
|---|---|---|
| `position: sticky` is ignored and `position: fixed` scrolls away with the page | Positioning | High |
| Multi-column layout (`columns`, `column-width`, `column-count`) | Multi-column | High (Wikipedia references) |
| `border-radius`, `box-shadow`, `outline` are not painted | Backgrounds & borders / UI | Med–High (every modern button, card, input) |
| `rowspan` is ignored in tables | Tables | Med (infoboxes, data tables) |
| No word breaking: `overflow-wrap`, `word-break`, `hyphens`, `text-overflow: ellipsis` | Text | Med (long URLs overflow) |
| `opacity` only handles 0; no translucency; gradients are flattened to one colour | Color / Compositing | Med |
| `text-align: justify`, `letter-spacing`, `word-spacing` ignored | Text | Low–Med |
| Web fonts (`@font-face`) and icon fonts | Fonts | Med |
| `:has()`, `:target`, `:lang()`, `::first-letter`, `::marker`, `::placeholder` | Selectors | Low–Med |
| CSS counters (`counter-reset/increment`, `counter()`) | Lists & counters | Low–Med |
| Transforms other than `translate()`; no transitions/animations | Transforms / Animation | Low (static rendering) |

---

## 1. Syntax, cascade and at-rules

| Feature | Status | Notes / impact |
|---|---|---|
| Rules, declarations, comments, `!important` | ✅ | |
| Cascade: origin, specificity, source order, inline `style` | ✅ | UA < author < author `!important` < UA `!important`. |
| Inheritance, `inherit` / `initial` / `unset` | ✅ | `revert` / `revert-layer` treated as `initial` (Low). |
| Custom properties and `var()` with fallback | ✅ | Shorthands re-expanded after substitution. |
| `@import` (with media list) | ✅ | |
| `@media` | ✅ | Width, height, aspect-ratio, orientation, full range syntax (`400px < width <= 900px`) and `calc()` values; print/screen; `prefers-*`, `hover`, `pointer`. Resolution queries evaluate false (one screen density). |
| `@supports` | ✅ | Evaluated honestly against the engine (`not/and/or`, `selector()`), so fallbacks apply. |
| `@layer` | ✅ | Layer order by first appearance (statement or block, nested names); layered < unlayered for normal declarations, reversed for `!important`. |
| `@container` | ✅ | Size queries against the nearest `container-type` ancestor (optionally by `container-name`), using its size from the previous layout; the page is styled and laid out again when a container's size changes. `style()` queries evaluate false. |
| `@font-face` | ❌ | Skipped on purpose (web fonts left out of scope). |
| `@keyframes` | ✅ | Collected per page and run by the animator (see §17). |
| `@page`, `@namespace`, `@property`, `@counter-style`, `@scope` | ✅ | `@property` (initial values, `inherits: false`), `@counter-style` (cyclic, fixed, symbolic, alphabetic, numeric, additive, extends, prefix/suffix, pad, negative, fallback), `@scope (start) to (end)`, `@namespace` prefixes in selectors. `@page` is parsed and ignored: the browser has no printing. |
| Nesting (`&`, nested rules) | ✅ | Nested rules (with or without `&`, leading combinators) and nested `@media`/`@supports`/`@layer` inside rules. |

## 2. Selectors

| Feature | Status | Notes / impact |
|---|---|---|
| Type, class, id, universal | ✅ | |
| Attribute `[a] = ~= \|= ^= $= *=` | ✅ | `i`/`s` flags parsed but case-insensitive matching not done (Low). |
| Combinators: descendant, `>`, `+`, `~` | ✅ | Ancestor bloom-style filter for speed. |
| Selector lists, `:is()`, `:where()`, `:not()` (complex args) | ✅ | Specificity handled per spec. |
| Structural: `:root`, `:first/last/only-child`, `:first/last-of-type`, `:nth-child`, `:nth-last-child`, `:nth-of-type`, `:empty` | ✅ | `:nth-last-of-type`, `:only-of-type` missing; `:nth-child(… of S)` missing (Low). |
| `:link`, `:any-link` | ✅ | `:visited` never matches (correct for privacy). |
| `:hover`, `:active`, `:focus`, `:focus-visible`, `:focus-within`, `:checked` | ✅ | Restyles only the affected subtree. |
| `:disabled`, `:enabled` | ✅ | |
| `:has()` | ✅ | Relative selectors with descendant, `>`, `+` and `~`; `@supports selector(:has())` is true. |
| `:target`, `:lang()`, `:dir()`, `:placeholder-shown`, `:required`, `:invalid`, etc. | ✅ | `:target` (set when a #fragment is followed), `:lang()`, `:dir()`, `:placeholder-shown`, `:required`/`:optional`, `:valid`/`:invalid`/`:user-valid`/`:user-invalid` (required, email, url, number, min/max, pattern, minlength/maxlength; forms and fieldsets too), `:in-range`/`:out-of-range`, `:indeterminate`, `:read-only`/`:read-write`, `:default`, `:only-of-type`, `:nth-last-of-type`. Form states re-evaluate as you type. |
| `::before`, `::after` | ✅ | `content` with strings, `attr()`, `open-/close-quote`. |
| `::first-letter`, `::first-line`, `::marker`, `::placeholder`, `::selection` | ✅ | `::first-letter` (drop caps, floats), `::first-line` (only the words on the first line), `::marker` (colour, font, `content`), `::placeholder`, `::selection` (colours of the text selection). |

## 3. Values and units

| Feature | Status | Notes / impact |
|---|---|---|
| px, em, rem, %, pt, pc, in, cm, mm, Q | ✅ | |
| vw, vh, vmin, vmax | ✅ | |
| ex, ch | ✅ | From Arial/Helvetica metrics (x-height 0.519em, '0' 0.556em); also `lh`, `rlh`, `cap`, `ic`. |
| dvh/svh/lvh, container units (cqw…) | ✅ | `dvh/svh/lvh` (and `vw`/`vmin`/`vmax` variants) equal the window size; `cqw/cqh/cqi/cqb/cqmin/cqmax` from the nearest query container. |
| `calc()`, `min()`, `max()`, `clamp()` | ✅ | Nested calls supported. |
| `round()`, `mod()`, trig functions | ✅ | A real calc parser: `round()` (all strategies), `mod()`, `rem()`, `abs()`, `sign()`, `sin/cos/tan/asin/acos/atan/atan2`, `pow`, `sqrt`, `hypot`, `log`, `exp`, `pi`, `e`. |
| `min-content` / `max-content` / `fit-content` | ✅ | Widths and grid tracks. |
| `attr()` outside `content` | ✅ | `attr(name)`, `attr(name px)`, `attr(name, fallback)` in any property. |

## 4. Color

| Feature | Status | Notes / impact |
|---|---|---|
| Named colours (full list), system colours, `currentcolor`, `transparent` | ✅ | |
| Hex (3/4/6/8 digit), `rgb()/rgba()`, `hsl()/hsla()` incl. space syntax | ✅ | |
| Alpha | ✅ | Translucent fills are drawn with Tk stipple patterns, so what is behind really shows through; translucent text is blended with the real background behind it, not white. |
| `hwb()`, `lab()`, `lch()`, `oklab()`, `oklch()`, `color()`, `color-mix()`, `light-dark()` | ✅ | Proper colour-space conversions; `color-mix()` in srgb, oklab and oklch. |
| `accent-color`, `caret-color` | ✅ | Checked checkboxes and radios use `accent-color`; the text cursor uses `caret-color`. |

## 5. Box model and sizing

| Feature | Status | Notes / impact |
|---|---|---|
| margin / padding / border widths, shorthands | ✅ | |
| `width`, `height`, `min-*`, `max-*` | ✅ | %-based `min/max-height` ignored (Low). |
| `box-sizing` | ✅ | |
| Auto margins, vertical margin collapsing | ✅ | |
| `aspect-ratio` | ✅ | Blocks and images; `@supports` is true. |
| Logical properties: `margin/padding-inline/block(-start/-end)`, `border-block/inline` | ✅ | Also `inline-size`, `block-size` (and min/max), `inset-inline/-block`, `border-inline-start/-end`, logical corner radii. Mapped for horizontal writing. |

## 6. Backgrounds and borders

| Feature | Status | Notes / impact |
|---|---|---|
| `background-color` | ✅ | |
| `background-image: url()` with `-repeat`, `-position`, `-size` | ✅ | Only the first layer of multiple backgrounds. |
| Gradients | ✅ | `linear-`, `radial-`, `conic-` and `repeating-` gradients drawn as images, with angles, `to` sides/corners, stop positions, transparent stops and rounded corners. |
| `background-clip`, `-origin`, `-attachment` | ✅ | Every background layer (comma lists) with its own size, position (1–4 values), repeat (incl. `round`/`space`), origin, clip (incl. `text`) and attachment (`fixed` stays put while scrolling). |
| Border styles | ✅ | solid, dotted, dashed, double, inset/outset/groove/ridge (shaded). |
| `border-radius` | ✅ | Backgrounds, uniform borders, shadows and outlines are drawn with rounded corners (one radius per box: the largest corner). |
| `box-shadow` | ✅ | Outer shadows with offset, spread and blur (blur approximated by fading rings); `inset` skipped. |
| `border-image` | ✅ | Nine-slice from images or gradients: slice (with `fill`), width, outset, `stretch`/`repeat`/`round`. |

## 7. Fonts

| Feature | Status | Notes / impact |
|---|---|---|
| `font-family` with generic families and fallback list | ✅ | Picks the first installed family. |
| `font-size` (keywords, relative, units) | ✅ | |
| `font-weight` | ✅ | Light, semilight, semibold, extra-bold and black use the installed weight faces (e.g. Segoe UI Light/Semibold/Black); `bolder`/`lighter` follow the CSS table. |
| `font-style` | ✅ | italic/oblique. |
| `font` shorthand | ✅ | |
| `@font-face` / web fonts | ❌ | Text uses installed fonts; icon fonts (Font Awesome etc.) vanish because private-use glyphs are dropped. Med. Wikipedia mostly uses system fonts and SVG icons, so Low there. |
| `font-variant`, `font-feature-settings`, `font-stretch` | ✅ | `small-caps` (also via `"smcp"`), `font-stretch` through installed condensed/expanded faces. Other OpenType features can't be switched through Tk. |

## 8. Text

| Feature | Status | Notes / impact |
|---|---|---|
| `color`, `line-height`, `text-indent` | ✅ | |
| `text-align` left/right/center/end | ✅ | |
| `text-align: justify` | ✅ | With `text-align-last`. |
| `text-transform` | ✅ | |
| `white-space` (normal, nowrap, pre, pre-wrap, pre-line, break-spaces) | ✅ | |
| `letter-spacing`, `word-spacing` | ✅ |  |
| `overflow-wrap` / `word-wrap`, `word-break` | ✅ | `break-word`/`anywhere`/`break-all` cut words that are wider than the line. Line breaks are also allowed before `nowrap` runs at normal spaces (Wikipedia navboxes). |
| `hyphens` | ✅ | Soft hyphens (`&shy;`) break with a visible hyphen; `hyphens: auto` breaks long words between syllables. |
| `text-overflow: ellipsis` | ✅ | Lines are cut at the box edge and end with "…". |
| `-webkit-line-clamp` | ✅ | And `line-clamp`: the box keeps N lines and ends the last with "…". |
| `text-shadow` | ✅ | Multiple shadows; blur lightens the shadow colour. |
| `tab-size`, `text-align-last`, `text-wrap: balance/pretty` | ✅ |  |

## 9. Text decoration

| Feature | Status | Notes / impact |
|---|---|---|
| `text-decoration` underline / line-through / overline, propagation | ✅ | |
| `text-decoration-color` | ✅ | |
| `text-decoration-style` (wavy, dotted…), `-thickness`, `text-underline-offset` | ✅ | Also `text-underline-position: under` and colour/style inside the `text-decoration` shorthand. |

## 10. Display and flow layout

| Feature | Status | Notes / impact |
|---|---|---|
| `display`: block, inline, inline-block, none, list-item, flow-root, contents | ✅ | |
| flex, inline-flex, grid, inline-grid, table family | ✅ | See below. |
| Legacy `-webkit-box` | ✅ | `-webkit-box-orient`, `-flex`, `-pack`, `-align`, `-direction`, `-ordinal-group` mapped to flexbox. |
| Inline layout, line wrapping, inline backgrounds/borders | ✅ | |
| `vertical-align` | ✅ | `text-top`/`text-bottom` now align with the parent font. |
| Floats and `clear`, text wrapping around floats | ✅ | Floats inside tables not handled (README). Wikipedia thumbnails and infoboxes float correctly. |
| Block formatting contexts (overflow, flow-root, inline-block) | ✅ | |

## 11. Positioning

| Feature | Status | Notes / impact |
|---|---|---|
| `position: relative` with offsets | ✅ | |
| `position: absolute` with containing block, `top/right/bottom/left`, `inset` | ✅ | |
| `position: fixed` | ✅ | Painted as its own layer that the window moves with the scroll, so it stays on screen; hover and clicks follow it. |
| `position: sticky` | ✅ | With `top`: sticks while its parent is on screen (Wikipedia's table of contents). `bottom`/`left`/`right` stickiness not done (Low). |
| `z-index` | ✅ | Real stacking contexts: negative layers above the context's background, in-flow content, then z-index 0/auto and positive layers in order; contexts from z-index, opacity, transforms, filters, clip-path, isolation, blend modes, fixed/sticky, flex items with z-index. |

## 12. Flexbox

| Feature | Status | Notes / impact |
|---|---|---|
| `flex-direction` (incl. reverse), `flex-wrap` (incl. wrap-reverse), `flex-flow` | ✅ | |
| `flex-grow/shrink/basis`, `flex` shorthand, min/max clamping, auto min size | ✅ | Full resolution loop. |
| `justify-content`, `align-items`, `align-self` (incl. baseline), `align-content` | ✅ | |
| `gap`, `row-gap`, `column-gap` | ✅ | |
| `order`, auto margins | ✅ | |

Flexbox is in good shape; the remaining risk is edge cases, not missing features.

## 13. Grid

| Feature | Status | Notes / impact |
|---|---|---|
| `grid-template-columns/rows`: px, %, fr, auto, min/max-content, `minmax()`, `fit-content()`, `repeat()` incl. auto-fill/auto-fit | ✅ | |
| `grid-template-areas`, `grid-area`, line numbers/names, `span` | ✅ | |
| `grid-template`, `grid` shorthands, `grid-auto-flow` (row/column/dense), `grid-auto-rows/columns` | ✅ | |
| `justify-/align-items/self/content`, `place-*` | ✅ | |
| `subgrid` | ✅ | `grid-template-columns: subgrid` takes the parent's tracks and gaps; subgridded rows fall back to auto. |
| `masonry` | ✅ | `grid-template-rows: masonry` stacks items in the shortest column. |

## 14. Tables

| Feature | Status | Notes / impact |
|---|---|---|
| Auto table layout, `colspan`, captions | ✅ | |
| `border-collapse`, `border-spacing`, legacy `border/cellpadding/cellspacing` | ✅ | |
| Cell `vertical-align` | ✅ | |
| `rowspan` | ✅ | Spanned slots are skipped in later rows; a tall spanning cell grows the last row it covers. |
| `table-layout: fixed` | ✅ |  |
| `<col>` / `<colgroup>` widths, `caption-side` | ✅ |  |

## 15. Lists and counters

| Feature | Status | Notes / impact |
|---|---|---|
| `list-style-type` (disc, circle, square, decimal, decimal-leading-zero, alpha, roman, string), `list-style-position` | ✅ | |
| `list-style-image` | ✅ |  |
| `counter-reset`, `counter-increment`, `counter()` / `counters()` | ✅ | Also `counter-set` and `@counter-style` names. |
| `ol start`, `li value`, `reversed` | ✅ |  |

## 16. Overflow and scrolling

| Feature | Status | Notes / impact |
|---|---|---|
| `overflow: hidden / clip` | ✅ | Clips painting and hit-testing. |
| `overflow: auto / scroll` | ✅ | The box scrolls with the mouse wheel (Shift+wheel sideways) and shows a thin scroll indicator; the page scrolls once the box reaches its end. |
| `overflow-x` / `overflow-y` separately | ✅ | Each axis clips and scrolls on its own. |
| `scroll-behavior`, `scroll-snap-*`, `scroll-margin` | ✅ | Smooth fragment scrolling, `scroll-margin-top`/`scroll-padding-top` for #fragment jumps, page `scroll-snap-type` with `scroll-snap-align` start/center/end (mandatory and proximity). |

## 17. Transforms, transitions, animation

| Feature | Status | Notes / impact |
|---|---|---|
| `transform: translate()/translateX/Y/translate3d()` | ✅ | Enough for off-screen drawers and centring tricks (`translate(-50%, -50%)`). |
| `rotate`, `scale`, `skew`, `matrix`, individual `translate/rotate/scale` properties | ✅ | Full 2D transforms at paint time (`transform-origin`; text turns with Tk's text angle, images through Pillow). 3D functions are flattened. |
| `transition-*` | ✅ | Colours, lengths, numbers and transforms animate after :hover/:focus/script changes, with the timing functions (`cubic-bezier`, `steps`). |
| `animation`, `@keyframes` | ✅ | Durations, delays, iteration counts, directions, fill modes, play state, per-keyframe timing. |

## 18. Masking, compositing and effects

| Feature | Status | Notes / impact |
|---|---|---|
| `mask-image` / `-webkit-mask-image` with `mask-size/position/repeat` | ✅ | Single-colour icons through SVG/PNG masks, which is how Wikipedia draws its UI icons. |
| `opacity` | ✅ | Partial opacity fades the box's drawing (onto white: Tk has no alpha). |
| `clip-path` | ✅ | `inset()`, `circle()`, `ellipse()`, `polygon()`, `rect()`, `xywh()`; `url()` references are not supported. |
| `clip: rect()` | ✅ |  |
| `filter`, `backdrop-filter`, `mix-blend-mode`, `isolation` | ✅ | Colour filters on everything, `blur()` on images, `drop-shadow()`; `backdrop-filter` filters what is behind the box; blend modes against the colour behind the box; `isolation: isolate` makes a stacking context. |

## 19. Generated content

| Feature | Status | Notes / impact |
|---|---|---|
| `content` with strings, escapes, `attr()`, `open-quote/close-quote` | ✅ | |
| `content: url()`, `counter()` | ✅ |  |
| `quotes` property | ✅ | Pairs by nesting depth; `<q>` uses them. |

## 20. Multi-column layout

| Feature | Status | Notes / impact |
|---|---|---|
| `columns`, `column-count`, `column-width`, `column-gap`, `column-rule` | ✅ | Content is laid out at the column width and cut into balanced columns between blocks (or lines). Wikipedia's reference lists split into columns on wide windows. |
| `column-span`, `break-inside` | ✅ | Also `break-before`/`break-after: column`; paragraphs can continue in the next column line by line. |

## 21. Writing modes and direction

| Feature | Status | Notes / impact |
|---|---|---|
| `direction: rtl`, `unicode-bidi` | ✅ | Right-to-left paragraphs (word order with left-to-right runs kept, start/end alignment, list markers, flex rows, grid columns), `dir` attributes, `<bdo>`. |
| `writing-mode`, `text-orientation` | ✅ | `vertical-rl/-lr`, `sideways-rl/-lr` (laid out then turned a quarter), `text-orientation: upright` (stacked letters). |

## 22. Containment and container queries

| Feature | Status | Notes / impact |
|---|---|---|
| `contain`, `content-visibility` | ✅ | `contain: paint/strict/content` clips; `content-visibility: hidden` skips the contents (with `contain-intrinsic-size`). |
| `container-type`, `@container` | ✅ | See §1. |

## 23. User interface

| Feature | Status | Notes / impact |
|---|---|---|
| `cursor` | ✅ | Pointer over links/buttons. |
| `outline`, `outline-offset` | ✅ | Painted outside the border box (solid, dotted, dashed), rounded with `border-radius`. |
| `pointer-events`, `user-select`, `appearance`, `resize` | ✅ | `pointer-events: none` lets clicks and hover through; mouse text selection with Ctrl+C/Ctrl+A that honours `user-select: none` and `::selection`; `appearance: none` drops the native check marks/arrows; `resize` shows a grip you can drag (textareas by default). |

---

## What would help most for real pages (in order)

1. **`position: fixed` staying on screen and `position: sticky`.** These change the page structure people see the most.
2. **Multi-column (`column-width` / `column-count`).** Directly visible on every long Wikipedia article.
3. **`border-radius` + `box-shadow` + `outline` painting.** Tk canvases can draw rounded rectangles with polygons; shadows can be approximated with an offset darker rectangle. Cheap, highly visible.
4. **`rowspan`.** Fixes misaligned infobox and data table rows.
5. **Word breaking (`overflow-wrap: break-word` / `anywhere`) and `text-overflow: ellipsis`.**
6. **Scrollable `overflow: auto` boxes,** or at least letting wide tables overflow visibly instead of being clipped.

Everything else listed is Low impact for an English-language, mostly static site like Wikipedia.

---

## Closed after the audit (2026-10-06, evening)

Besides the rows updated above, these smaller gaps were also closed:

- `aspect-ratio` (blocks and images); `@supports (aspect-ratio: …)` is now true.
- Partial `opacity` (faded toward white, since Tk has no alpha) and real `linear-`, `radial-`, `conic-` and `repeating-` gradients.
- `text-align: justify`, `letter-spacing`, `word-spacing`.
- Selectors: `:has()`, `:target` (set when you follow a `#fragment` link), `:lang()`, `:only-of-type`, `:nth-last-of-type`, `:placeholder-shown`, `:required`, `:optional`, `:read-only`, `:read-write`, `:default`.
- Colours: `oklch()`, `oklab()`, `lab()`, `lch()`, `hwb()`, `color()`, `color-mix()`, `light-dark()`; `dvh`/`svh`/`lvh` units.
- CSS counters (`counter-reset`, `counter-increment`, `counter-set`, `counter()`, `counters()`); `<ol reversed>`, and `<li value>` now continues the numbering.
- Logical sizes (`inline-size`, `block-size`, min/max versions), `inset-inline/-block`, `border-inline/-block-start/-end`.
- `@media` height, aspect-ratio, orientation and full range syntax; `calc()` in media queries (Wikipedia uses `max-width: calc(1120px - 1px)`); `vh` now uses the real window height.

Still open: only `@font-face` / icon fonts, left out on purpose.
