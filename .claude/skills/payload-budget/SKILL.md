---
name: payload-budget
description: Verify the AAAC C3 payload variants after any edit to delivery/templates/ or delivery/variants.py — renders each variant, measures real byte sizes against the configs/run.yaml budgets, checks the full/essential ratio is at least 10x, and parses the essential HTML to prove it makes zero sub-resource requests.
---

# Payload budget

The C3 claim is that a slow client gets an order of magnitude fewer bytes for the
same information. That claim is only as good as the byte counts behind it, and a
template grows by accident. Run this after **every** edit to
`src/aaac/delivery/templates/` or `src/aaac/delivery/variants.py`.

## Budgets

Read from `configs/run.yaml` → `delivery.budgets_bytes`. Do not hardcode. If the
config is unreadable, stop and say so rather than falling back to remembered
numbers — a silent fallback would validate against the wrong target.

Documented values, for reference only: `full: 460800`, `reduced: 61440`,
`essential: 6144`.

## Checks — run all four, report all four

**1. Render each variant.** Use the same Jinja2 render path the `/result` handler
uses, with representative content (a realistic name, index number, and a full
subject/grade table — not a stub, and not an empty result). Measure the encoded
byte length of the response body, the same number that goes in `Content-Length`.

**2. Each variant is inside its budget.** Report actual bytes and headroom for
each, as a table, every time — pass or fail. These counts are the evidence for the
C3 claim in the write-up, so they get reported even when everything is green.

**3. Ratio: `size(full) / size(essential) >= 10`.** Report the computed ratio to
two decimals. This is a hard build gate: the proposal claims an order of
magnitude, so it must be true and measurable.

**4. `essential` makes exactly one HTTP request.** Parse the rendered HTML with an
HTML parser — never a regex, per §4.5. Assert zero `<script>`, `<link>`, `<img>`,
`<iframe>`, `<video>`, `<audio>`, `<object>`, `<embed>`, `<source>`; no `srcset`;
no favicon; no `@import` or `url(...)` inside any inline `<style>`; no external
`href` that the browser would fetch. Any sub-resource defeats the entire point on
a 250 ms link, where a second round trip costs more than the bytes saved.

## Reporting

Always print a table like:

| Variant | Bytes | Budget | Headroom | Status |
|---|---|---|---|---|

followed by the ratio and the sub-resource verdict. Numbers first, prose after.

## On failure

State plainly which check failed and by how much. Then stop and report — do not
fix it unilaterally, because the two available fixes are not equivalent:

- Trimming the template is usually right
- **Raising a budget in `configs/run.yaml` is a contract change** (§3.9) and needs
  `contract-guard`. Never widen a budget to make a failing test pass — that is
  §1.5, adjusting a threshold to flatter a result, and it silently weakens the
  headline C3 claim

## Environment

Windows PowerShell (§6). Activate with `.\.venv\Scripts\Activate.ps1`, set
`$env:PYTHONPATH="src"`. No `source`, no `export`, no `mkdir -p`.

## Never

- Never measure a template rendered with empty or placeholder content — an empty
  results table makes `full` look far smaller than it is in production
- Never report the ratio without both underlying byte counts
- Never let `essential` gain a sub-resource "temporarily"
