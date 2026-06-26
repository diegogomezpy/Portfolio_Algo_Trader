# SFI design-language handoff (visual reference)

Two self-contained HTML files — open either in a browser (no server needed; fonts are inlined):

- **`SFI Design Language - Handoff.html`** — the language itself: principles, motion tokens,
  transitions, interaction states, colour/type foundations, iconography, data-viz rules, number
  formatting, responsive, resilience, accessibility, and the full CSS-variable token block.
- **`SFI Dashboard - Handoff.html`** — the language applied: a full dashboard mock (Overview /
  Portfolio / Performance) built from the same tokens.

These are the **visual** source of truth. The **written** spec is
[`../docs/DESIGN_LANGUAGE.md`](../docs/DESIGN_LANGUAGE.md); the **implementation** is
[`../dashboard/static/theme.css`](../dashboard/static/theme.css). Keep all three in sync — when
they disagree, `theme.css` wins.
