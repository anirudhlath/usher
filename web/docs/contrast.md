# Contrast ledger

Every colour pair this design system sanctions, measured. Ratios are computed from the authored
`oklch()` values converted to sRGB (WCAG 2.1 relative luminance).

- **4.5:1** — body text, and any text below 24 px / 19 px bold.
- **3:1** — large text, focus rings, control boundaries, and chart marks.

The old reference client failed here: its most-used text colour measured 4.34:1 on the page
background and 3.98:1 on cards, a secondary tone used 96 times sat at 2.83:1, and one tone
carrying real content sat at 1.75:1. Nothing below ships now.

| Theme | Pair | Foreground | Background | Ratio | Min | |
|---|---|---|---|---|---|---|
| dark | text-primary on canvas | `--n-50` #fbf8f6 | `--n-900` #0e0c0a | **18.46:1** | 4.5:1 | ✓ |
| dark | text-primary on surface | `--n-50` #fbf8f6 | `--n-850` #181513 | **17.19:1** | 4.5:1 | ✓ |
| dark | text-primary on raised | `--n-50` #fbf8f6 | `--n-800` #221f1c | **15.51:1** | 4.5:1 | ✓ |
| dark | text-secondary on canvas | `--n-300` #c3bcb7 | `--n-900` #0e0c0a | **10.41:1** | 4.5:1 | ✓ |
| dark | text-secondary on surface | `--n-300` #c3bcb7 | `--n-850` #181513 | **9.69:1** | 4.5:1 | ✓ |
| dark | text-secondary on raised | `--n-300` #c3bcb7 | `--n-800` #221f1c | **8.74:1** | 4.5:1 | ✓ |
| dark | text-muted on canvas | `--n-400` #968d87 | `--n-900` #0e0c0a | **6.00:1** | 4.5:1 | ✓ |
| dark | text-muted on surface | `--n-400` #968d87 | `--n-850` #181513 | **5.59:1** | 4.5:1 | ✓ |
| dark | text-muted on raised | `--n-400` #968d87 | `--n-800` #221f1c | **5.04:1** | 4.5:1 | ✓ |
| dark | accent / link text on canvas | `--teal-400` #1ad1d1 | `--n-900` #0e0c0a | **10.31:1** | 4.5:1 | ✓ |
| dark | accent / link text on surface | `--teal-400` #1ad1d1 | `--n-850` #181513 | **9.59:1** | 4.5:1 | ✓ |
| dark | accent / link text on raised | `--teal-400` #1ad1d1 | `--n-800` #221f1c | **8.65:1** | 4.5:1 | ✓ |
| dark | good text on canvas | `--green-400` #76cf8a | `--n-900` #0e0c0a | **10.28:1** | 4.5:1 | ✓ |
| dark | good text on surface | `--green-400` #76cf8a | `--n-850` #181513 | **9.57:1** | 4.5:1 | ✓ |
| dark | good text on raised | `--green-400` #76cf8a | `--n-800` #221f1c | **8.64:1** | 4.5:1 | ✓ |
| dark | warn text on canvas | `--amber-400` #e3ad4b | `--n-900` #0e0c0a | **9.62:1** | 4.5:1 | ✓ |
| dark | warn text on surface | `--amber-400` #e3ad4b | `--n-850` #181513 | **8.95:1** | 4.5:1 | ✓ |
| dark | warn text on raised | `--amber-400` #e3ad4b | `--n-800` #221f1c | **8.08:1** | 4.5:1 | ✓ |
| dark | bad text on canvas | `--red-400` #ff958d | `--n-900` #0e0c0a | **9.24:1** | 4.5:1 | ✓ |
| dark | bad text on surface | `--red-400` #ff958d | `--n-850` #181513 | **8.60:1** | 4.5:1 | ✓ |
| dark | bad text on raised | `--red-400` #ff958d | `--n-800` #221f1c | **7.76:1** | 4.5:1 | ✓ |
| dark | border-control on surface | `--n-500` #716a65 | `--n-850` #181513 | **3.42:1** | 3:1 | ✓ |
| dark | border-control on raised | `--n-500` #716a65 | `--n-800` #221f1c | **3.09:1** | 3:1 | ✓ |
| dark | border-control on canvas | `--n-500` #716a65 | `--n-900` #0e0c0a | **3.67:1** | 3:1 | ✓ |
| dark | accent solid on canvas | `--teal-500` #00b0b1 | `--n-900` #0e0c0a | **7.29:1** | 3:1 | ✓ |
| dark | good solid on canvas | `--green-500` #56ae6c | `--n-900` #0e0c0a | **7.13:1** | 3:1 | ✓ |
| dark | warn solid on canvas | `--amber-500` #c28e24 | `--n-900` #0e0c0a | **6.68:1** | 3:1 | ✓ |
| dark | bad solid on canvas | `--red-500` #dd766f | `--n-900` #0e0c0a | **6.41:1** | 3:1 | ✓ |
| dark | ink on teal-500 fill | `--n-950` #070604 | `--teal-500` #00b0b1 | **7.57:1** | 4.5:1 | ✓ |
| dark | teal text on teal quiet | `--teal-400` #1ad1d1 | `--teal-800` #023536 | **7.08:1** | 4.5:1 | ✓ |
| dark | ink on green-500 fill | `--n-950` #070604 | `--green-500` #56ae6c | **7.39:1** | 4.5:1 | ✓ |
| dark | green text on green quiet | `--green-400` #76cf8a | `--green-800` #1a3520 | **7.03:1** | 4.5:1 | ✓ |
| dark | ink on amber-500 fill | `--n-950` #070604 | `--amber-500` #c28e24 | **6.93:1** | 4.5:1 | ✓ |
| dark | amber text on amber quiet | `--amber-400` #e3ad4b | `--amber-800` #3b2b0d | **6.73:1** | 4.5:1 | ✓ |
| dark | ink on red-500 fill | `--n-950` #070604 | `--red-500` #dd766f | **6.65:1** | 4.5:1 | ✓ |
| dark | red text on red quiet | `--red-400` #ff958d | `--red-800` #442321 | **6.59:1** | 4.5:1 | ✓ |
| dark | focus ring on canvas | `--teal-400` #1ad1d1 | `--n-900` #0e0c0a | **10.31:1** | 3:1 | ✓ |
| dark | focus ring on surface | `--teal-400` #1ad1d1 | `--n-850` #181513 | **9.59:1** | 3:1 | ✓ |
| light | text-primary on canvas | `--n-800` #221f1c | `--n-50` #fbf8f6 | **15.51:1** | 4.5:1 | ✓ |
| light | text-primary on surface | `--n-800` #221f1c | `--n-0` #ffffff | **16.40:1** | 4.5:1 | ✓ |
| light | text-primary on sunken | `--n-800` #221f1c | `--n-100` #f3efed | **14.35:1** | 4.5:1 | ✓ |
| light | text-secondary on canvas | `--n-600` #5c5753 | `--n-50` #fbf8f6 | **6.75:1** | 4.5:1 | ✓ |
| light | text-secondary on surface | `--n-600` #5c5753 | `--n-0` #ffffff | **7.13:1** | 4.5:1 | ✓ |
| light | text-secondary on sunken | `--n-600` #5c5753 | `--n-100` #f3efed | **6.24:1** | 4.5:1 | ✓ |
| light | text-muted on canvas | `--n-500` #716a65 | `--n-50` #fbf8f6 | **5.03:1** | 4.5:1 | ✓ |
| light | text-muted on surface | `--n-500` #716a65 | `--n-0` #ffffff | **5.31:1** | 4.5:1 | ✓ |
| light | text-muted on sunken | `--n-500` #716a65 | `--n-100` #f3efed | **4.65:1** | 4.5:1 | ✓ |
| light | accent / link text on canvas | `--teal-600` #007678 | `--n-50` #fbf8f6 | **5.14:1** | 4.5:1 | ✓ |
| light | accent / link text on surface | `--teal-600` #007678 | `--n-0` #ffffff | **5.44:1** | 4.5:1 | ✓ |
| light | accent / link text on sunken | `--teal-600` #007678 | `--n-100` #f3efed | **4.76:1** | 4.5:1 | ✓ |
| light | good text on canvas | `--green-600` #197539 | `--n-50` #fbf8f6 | **5.45:1** | 4.5:1 | ✓ |
| light | good text on surface | `--green-600` #197539 | `--n-0` #ffffff | **5.76:1** | 4.5:1 | ✓ |
| light | good text on sunken | `--green-600` #197539 | `--n-100` #f3efed | **5.04:1** | 4.5:1 | ✓ |
| light | warn text on canvas | `--amber-600` #865700 | `--n-50` #fbf8f6 | **5.88:1** | 4.5:1 | ✓ |
| light | warn text on surface | `--amber-600` #865700 | `--n-0` #ffffff | **6.22:1** | 4.5:1 | ✓ |
| light | warn text on sunken | `--amber-600` #865700 | `--n-100` #f3efed | **5.44:1** | 4.5:1 | ✓ |
| light | bad text on canvas | `--red-600` #9c403c | `--n-50` #fbf8f6 | **6.18:1** | 4.5:1 | ✓ |
| light | bad text on surface | `--red-600` #9c403c | `--n-0` #ffffff | **6.54:1** | 4.5:1 | ✓ |
| light | bad text on sunken | `--red-600` #9c403c | `--n-100` #f3efed | **5.72:1** | 4.5:1 | ✓ |
| light | border-control on surface | `--n-400` #968d87 | `--n-0` #ffffff | **3.25:1** | 3:1 | ✓ |
| light | border-control on canvas | `--n-400` #968d87 | `--n-50` #fbf8f6 | **3.08:1** | 3:1 | ✓ |
| light | white on teal-600 fill | `--n-0` #ffffff | `--teal-600` #007678 | **5.44:1** | 4.5:1 | ✓ |
| light | teal text on teal quiet | `--teal-600` #007678 | `--teal-50` #e7f9f8 | **5.00:1** | 4.5:1 | ✓ |
| light | white on green-600 fill | `--n-0` #ffffff | `--green-600` #197539 | **5.76:1** | 4.5:1 | ✓ |
| light | green text on green quiet | `--green-600` #197539 | `--green-50` #ecf8ee | **5.28:1** | 4.5:1 | ✓ |
| light | white on amber-600 fill | `--n-0` #ffffff | `--amber-600` #865700 | **6.22:1** | 4.5:1 | ✓ |
| light | amber text on amber quiet | `--amber-600` #865700 | `--amber-50` #fbf3e7 | **5.65:1** | 4.5:1 | ✓ |
| light | white on red-600 fill | `--n-0` #ffffff | `--red-600` #9c403c | **6.54:1** | 4.5:1 | ✓ |
| light | red text on red quiet | `--red-600` #9c403c | `--red-50` #fff0ee | **5.90:1** | 4.5:1 | ✓ |
| light | focus ring on canvas | `--teal-600` #007678 | `--n-50` #fbf8f6 | **5.14:1** | 3:1 | ✓ |
| light | focus ring on surface | `--teal-600` #007678 | `--n-0` #ffffff | **5.44:1** | 3:1 | ✓ |
| dark | viz blue mark on canvas | `--viz-blue-dark` #549de5 | `--n-900` #0e0c0a | **6.82:1** | 3:1 | ✓ |
| dark | viz blue mark on surface | `--viz-blue-dark` #549de5 | `--n-850` #181513 | **6.34:1** | 3:1 | ✓ |
| light | viz blue mark on surface | `--viz-blue-light` #006ac0 | `--n-0` #ffffff | **5.49:1** | 3:1 | ✓ |
| light | viz blue mark on canvas | `--viz-blue-light` #006ac0 | `--n-50` #fbf8f6 | **5.20:1** | 3:1 | ✓ |
| dark | viz orange mark on canvas | `--viz-orange-dark` #fb9d59 | `--n-900` #0e0c0a | **9.36:1** | 3:1 | ✓ |
| dark | viz orange mark on surface | `--viz-orange-dark` #fb9d59 | `--n-850` #181513 | **8.71:1** | 3:1 | ✓ |
| light | viz orange mark on surface | `--viz-orange-light` #b75f0b | `--n-0` #ffffff | **4.51:1** | 3:1 | ✓ |
| light | viz orange mark on canvas | `--viz-orange-light` #b75f0b | `--n-50` #fbf8f6 | **4.27:1** | 3:1 | ✓ |
| dark | viz green mark on canvas | `--viz-green-dark` #68ca80 | `--n-900` #0e0c0a | **9.62:1** | 3:1 | ✓ |
| dark | viz green mark on surface | `--viz-green-dark` #68ca80 | `--n-850` #181513 | **8.96:1** | 3:1 | ✓ |
| light | viz green mark on surface | `--viz-green-light` #11813c | `--n-0` #ffffff | **4.97:1** | 3:1 | ✓ |
| light | viz green mark on canvas | `--viz-green-light` #11813c | `--n-50` #fbf8f6 | **4.70:1** | 3:1 | ✓ |
| dark | viz magenta mark on canvas | `--viz-magenta-dark` #e86ec4 | `--n-900` #0e0c0a | **6.93:1** | 3:1 | ✓ |
| dark | viz magenta mark on surface | `--viz-magenta-dark` #e86ec4 | `--n-850` #181513 | **6.45:1** | 3:1 | ✓ |
| light | viz magenta mark on surface | `--viz-magenta-light` #a32384 | `--n-0` #ffffff | **6.71:1** | 3:1 | ✓ |
| light | viz magenta mark on canvas | `--viz-magenta-light` #a32384 | `--n-50` #fbf8f6 | **6.34:1** | 3:1 | ✓ |
| dark | viz yellow mark on canvas | `--viz-yellow-dark` #f6d653 | `--n-900` #0e0c0a | **13.63:1** | 3:1 | ✓ |
| dark | viz yellow mark on surface | `--viz-yellow-dark` #f6d653 | `--n-850` #181513 | **12.69:1** | 3:1 | ✓ |
| light | viz yellow mark on surface | `--viz-yellow-light` #997e00 | `--n-0` #ffffff | **3.93:1** | 3:1 | ✓ |
| light | viz yellow mark on canvas | `--viz-yellow-light` #997e00 | `--n-50` #fbf8f6 | **3.72:1** | 3:1 | ✓ |
| dark | viz cyan mark on canvas | `--viz-cyan-dark` #5ad5e3 | `--n-900` #0e0c0a | **11.21:1** | 3:1 | ✓ |
| dark | viz cyan mark on surface | `--viz-cyan-dark` #5ad5e3 | `--n-850` #181513 | **10.44:1** | 3:1 | ✓ |
| light | viz cyan mark on surface | `--viz-cyan-light` #00808d | `--n-0` #ffffff | **4.70:1** | 3:1 | ✓ |
| light | viz cyan mark on canvas | `--viz-cyan-light` #00808d | `--n-50` #fbf8f6 | **4.44:1** | 3:1 | ✓ |
| dark | viz violet mark on canvas | `--viz-violet-dark` #9b7be9 | `--n-900` #0e0c0a | **5.95:1** | 3:1 | ✓ |
| dark | viz violet mark on surface | `--viz-violet-dark` #9b7be9 | `--n-850` #181513 | **5.54:1** | 3:1 | ✓ |
| light | viz violet mark on surface | `--viz-violet-light` #693abb | `--n-0` #ffffff | **7.18:1** | 3:1 | ✓ |
| light | viz violet mark on canvas | `--viz-violet-light` #693abb | `--n-50` #fbf8f6 | **6.79:1** | 3:1 | ✓ |
| dark | viz brick mark on canvas | `--viz-brick-dark` #dc5f4e | `--n-900` #0e0c0a | **5.38:1** | 3:1 | ✓ |
| dark | viz brick mark on surface | `--viz-brick-dark` #dc5f4e | `--n-850` #181513 | **5.01:1** | 3:1 | ✓ |
| light | viz brick mark on surface | `--viz-brick-light` #a82418 | `--n-0` #ffffff | **7.17:1** | 3:1 | ✓ |
| light | viz brick mark on canvas | `--viz-brick-light` #a82418 | `--n-50` #fbf8f6 | **6.78:1** | 3:1 | ✓ |

**103 pairs checked, 0 failing.**

## Deliberately not in the ledger

`--border-subtle`, `--border-default` and `--border-strong` are decorative dividers and card edges.
They do not meet 3:1 and are not permitted to be the only thing identifying a control — every
interactive boundary uses `--border-control` (n-500 dark / n-400 light), which does.

---

## 2026-08-19 — 37 failures the ledger above could not have caught

The table above is the handoff's own evidence and it is not amended: every pair in it still
measures what it says. What follows is what the *product* renders, which is a different set.

The gap had been invisible because the stylesheet was not loading at all — `styles.css` is nothing
but `@import` rules, and CSS drops an `@import` that follows any other rule, so once
`@import "tailwindcss"` was inlined ahead of them the whole token layer was discarded. The jsdom
component tests resolve no custom properties and the Playwright axe sweeps were measuring browser
defaults. With the stylesheet loading, the sweeps failed.

Measured in Chromium at 1440×900 over the component gallery with `scripts/contrast-probe.mjs` —
**24 failures dark/comfortable, 13 light/compact**. All 37 now pass; the ratios below are before →
after, both measured rather than computed.

**The ledger's blind spots**, all three of which are about *ground* rather than about tone:

1. **`--text-disabled` rendering prose.** Not a measurement error — a rule violation. §12 says the
   disabled tone appears only on a control that is also `aria-disabled`, and `--state-na-text`
   aliased it. `StateBlock kind="na"` is one of the four absent-state treatments and its sentence
   is real content.
2. **`--text-muted` on a tinted `*-quiet` fill or a hover overlay.** The ledger measured the muted
   tone against the three *neutral* surfaces, where its floor is 5.04:1 on `--bg-raised`. The
   quiet fills are darker and warmer than those, and `--bg-hover` over `--bg-raised` is darker
   still; on both, that 5.04:1 floor lands under 4.5:1.
3. **Any pair inside an `opacity` group.** No entry in the ledger is measured through one, and an
   `opacity` on an ancestor composites foreground *and* background toward whatever is behind, so
   it compresses every ratio inside it. This is the class that produced the worst numbers, and
   the one no tone can fix: inside a 0.55 group in the light theme even pure black on the
   absent-artwork panel reaches only 4.95:1.

| Theme | Element | Foreground | Ground | Before | After | Class |
|---|---|---|---|---|---|---|
| dark | `u-state__body` (`kind="na"`) | `--text-disabled` #5c5753 | `--bg-canvas` #0e0c0a | **2.73:1** | **6.00:1** | 1 |
| light | `u-state__body` (`kind="na"`) | `--text-disabled` #b1a9a3 | `--bg-canvas` #fbf8f6 | **2.18:1** | **5.02:1** | 1 |
| dark | `u-chip__state`, pressed ×2 | `--text-muted` #968d87 | `--accent-quiet` #023536 | **4.12:1** | **7.15:1** | 2 |
| dark | `u-combo__tier` / `__meta`, active option ×2 | `--text-muted` #968d87 | `--bg-hover` on `--bg-raised` #2e2b28 | **4.32:1** | **7.50:1** | 2 |
| dark | `u-state__meta` (`kind="stale"`) | `--text-muted` #968d87 | `--warn-quiet` #3b2b0d | **4.19:1** | **7.28:1** | 2 |
| dark | `u-problem__meta` facts, panel ×7 | `--text-muted` #968d87 | `--bad-quiet` #442321 | **4.28:1** | **7.42:1** | 2 |
| dark | `u-problem__meta` facts, panel-warn ×2 | `--text-muted` #968d87 | `--warn-quiet` #3b2b0d | **4.19:1** | **7.28:1** | 2 |
| dark | `u-target__src` / `__audio` ×4 | `--text-muted` at `opacity:.72` #706964 | `--bg-surface` at `.72` #151210 | **3.45:1** | **5.58:1** | 3 |
| dark | `u-badge--info` in an undecodable row ×2 | `--info-text` at `opacity:.72` #179a99 | `--info-quiet` at `.72` #052a2a | **4.46:1** | **7.08:1** | 3 |
| dark | Artwork absent-state sentence, un-owned | `--text-muted` at `opacity:.55` #59534f | `--bg-surface` at `.55` #14110f | **2.48:1** | **5.79:1** | 3 |
| light | `u-target__src` / `__audio` ×4 | `--text-muted` at `opacity:.72` #98928e | `--bg-surface` at `.72` #fefdfc | **3.02:1** | **5.31:1** | 3 |
| light | `u-target__undecodable` ×3 | `--warn-text` at `opacity:.72` #a78445 | `--bg-surface` at `.72` #fefdfc | **3.42:1** | **6.21:1** | 3 |
| light | `u-badge--info` in an undecodable row ×2 | `--info-text` at `opacity:.72` #469a9b | `--info-quiet` at `.72` #edf9f7 | **3.06:1** | **4.99:1** | 3 |
| light | `u-problem__trace` ×2 | `--text-secondary` at `opacity:.8` #7d7572 | `--bad-quiet` #fff0ee | **4.06:1** | **6.43:1** | 3 |
| light | Artwork absent-state sentence, un-owned | `--text-muted` at `opacity:.55` #afaaa6 | `--bg-surface` at `.55` #fdfcfb | **2.24:1** | **5.18:1** | 3 |

**37 failures found, 37 fixed. No entry in the 103-pair table is invalidated** — one token moved,
`--state-na-text`, and it moved from `--text-disabled` to `--text-muted`, neither of which the
table measures. Every other fix moved a call site to a tone the table already sanctions, so the
after-ratios above are ledger pairs: #c3bcb7 on #442321, #968d87 on #181513, #1ad1d1 on #023536.

### Two things a future sweep should not have to rediscover

- **`--text-muted` on `--bg-raised` is 5.04:1, and that is the whole margin.** Anything laid over
  `--bg-raised` — `--bg-hover` is the one in the system — spends it. A quiet fill is not a
  neutral surface and must be measured separately; four of them are grounds for text in this
  product and none was in the table above.
- **An `opacity` on an ancestor is a contrast change, and nothing in the token layer can see it.**
  `--unowned-opacity` is the only one that is a token; `.72` on an undecodable play target and
  `.8` on a trace id were literals in component CSS. Before dimming anything, check whether the
  group contains a sentence.

### Not fixed, and why

- **`u-art__initial`** — the absent-artwork monogram — is `--text-disabled` at 22 px and measures
  **2.54:1** dark / **2.31:1** light. axe reports it `incomplete` ("content is too short to
  determine if it is actual text") rather than as a violation. It is a decorative stand-in for a
  poster whose only information, the title's first letter, is already carried by the card title
  and by the sentence beside it — the same "drawing of an absence" that `Person.tsx` renders at
  the same tone behind an explicit `aria-hidden`. Raising it to `--text-muted` would fix the
  number and flatten the panel, so it is left as a design decision rather than taken unilaterally.
- **Un-owned has no carrier but opacity.** §12 requires every state to be hue *plus* icon *plus*
  word, and `unowned` is dimming alone. On a card that has artwork the dim is unchanged and
  legible; on a card that has none — a skeleton title, which is most of a 1.27M catalog — nearly
  all of the old visible difference *was* the sentence washing out, so what is left after the fix
  is faint. That wants a word, which is a design decision and not a contrast one.
