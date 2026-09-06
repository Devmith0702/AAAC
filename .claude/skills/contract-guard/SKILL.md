---
name: contract-guard
description: Stop-and-check gate for any change touching the AAAC shared contract — src/aaac/common/, the pydantic schemas, the admit-token format, the event vocabulary, or any HTTP endpoint path or payload shape. Invoke BEFORE making such an edit, not after.
---

# Contract guard

The contract in CLAUDE.md §3 is identical in all three work-package briefs.
Changing it breaks M1's or M3's package **silently** — their code keeps importing,
keeps running, and starts being wrong. This skill exists to make that impossible
to do by accident.

## Trip conditions

Run this skill before an edit that touches any of:

- any file under `src/aaac/common/`
- a field name, type, or default in `LinkSample`, `LinkEstimate`, `TicketStatus`
- `AccessClass` members or their integer ordering (§3.3)
- the admit-token payload keys `tid`, `cls`, `att`, `exp`, `var`, the HMAC scheme,
  or the `b64url(payload).b64url(sig)` encoding (§3.7)
- any event name in the closed vocabulary (§3.8), or adding a new one
- any path, query parameter, request body, or response shape in the §3.5 API tables
- the run modes `none` / `baseline` / `aaac` (§3.4)
- keys under `estimator:` or `delivery:` in `configs/run.yaml` (§3.9)

If unsure whether an edit trips this, assume it does.

## Procedure

**Step 1 — Stop.** Do not make the edit. Do not stage anything.

**Step 2 — Classify the change.** Two cases, handled differently:

- **Case A: the edit is inside `src/aaac/common/` or `src/aaac/admission/`.**
  This is M1's package. Per §1.4 I do not write it, even if it is missing and
  something of mine fails to import. Report the missing symbol, then propose a
  clearly-marked local stub in `src/aaac/estimator/` with a `# STUB — delete when
  M1 ships` comment, and record it in the removal list. One of M2's success
  criteria is literally "M2 never had to patch `common/`". Do not offer editing
  `common/` as an option.

- **Case B: the edit is in my own package but changes a shape M1 or M3 consumes.**
  This is a real contract change. Continue to step 3.

**Step 3 — Quote the clause verbatim.** Read CLAUDE.md and quote the exact text of
the §3 clause at issue, with its line number. Do not paraphrase it from memory —
paraphrase is how drift starts.

**Step 4 — State the blast radius, concretely.** Name the specific consumer and
the specific failure. Not "this might affect M1" but, for example: "M1's
`/queue/estimate` handler validates the posted body against `LinkEstimate`.
Renaming `rtt_jitter_ms` makes every POST fail pydantic validation with a 422, and
because §3.10 rule 2 requires an event per decision, M3's event log silently loses
every `ESTIMATE` row — so Δ is computed from partial data and looks better than it
is." Cover both M1 and M3 separately; if one is unaffected, say so explicitly.

**Step 5 — Ask, and wait.** Present exactly this choice:

  1. Proceed as a `CONTRACT CHANGE` — Thisaru (M1) and Devmith (M3) must both be
     notified before it lands, and the change goes in the PR description under a
     `CONTRACT CHANGE` heading
  2. Find a way to do it inside my package without touching the contract
     (state whether one exists, and what it costs)
  3. Drop it

Wait for an answer. Never pick one and proceed.

**Step 6 — If approved.** Make the change, and draft the `CONTRACT CHANGE` note:
what changed, old shape → new shape, which package must update, and what happens
to a package that does not. Hand the note over; do not commit it and do not touch
the remote (§1.1).

## Never

- Never edit CLAUDE.md §3 itself as part of a routine task. If §3 must change,
  say so out loud and flag that it affects two other people's work.
- Never widen a schema "just to be compatible". A field M1 does not send is a
  field M2 must not require.
- Never resolve a contract mismatch by making my side tolerant of both shapes.
  That hides the drift instead of surfacing it.
