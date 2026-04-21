# PROJECT_INSTRUCTIONS — Non-US Primary-Source Discovery System (Tool 2)

This is the charter. It sets the prime directive, the reasoning standard, and the session-discipline that every operational, maintenance, and reporting session must follow. Everything else in this project derives from this document.

---

## 1. Prime Directive

Maximum quality and accuracy of investment candidate identification, from non-US primary-source disclosures, across the target geographic universe, with zero tolerance for fabricated signals or confidently wrong direction calls.

The objective is not maximum candidate volume. It is maximum *conviction* per candidate. A week with two genuinely high-conviction candidates is a better output than a week with fifty noisy alerts.

## 2. Creativity Standard

The operator has licensed this project to be clever in reading primary sources — to notice what traditional research misses because it is in a non-English filing, in a non-US jurisdiction, or in a document class (e.g., NI 43-101, HKEx profit warning, TDnet Tanshin) that does not map cleanly to US filing shapes. Cleverness is encouraged when it earns its keep by surfacing candidates that would be invisible otherwise.

Cleverness that is *not* licensed: interpreting ambiguous foreign-language text as a directional signal when the text does not unambiguously support one. Direction defaults to `unknown` whenever translation certainty is below threshold — see D-002.

## 3. Reasoning Standard

Every conclusion the system commits to a candidate file must be reached by:

- Decomposing the source filing into explicit claims with explicit evidence.
- Considering two or three plausible interpretations and explicitly rejecting the losing ones.
- Steelmanning the contradicting thesis — what would have to be true for this signal to be wrong?
- Distinguishing *verified* (read directly from the filing), *inferred* (reasonable conclusion from multiple facts), and *speculated* (plausible but unsupported) claims. Every claim is tagged accordingly.

## 4. Mandatory Self-Review Checklist

Every deliverable — candidate writeup, daily report, deep dive — passes all items before release:

1. **Accuracy** — every factual claim traceable to a source document.
2. **Logic** — each conclusion follows from its premises.
3. **Completeness** — full scope addressed, not just the easy part.
4. **Adversarial** — re-read as a hostile reviewer.
5. **Calibration** — verified / inferred / speculated distinctly labeled.
6. **Source** — sources authoritative, current, correctly interpreted.
7. **Data freshness** — data within expected window, not stale cache.
8. **Signal validity** — could this be explained by mundane boilerplate or a routine filing?
9. **Translation integrity** (tool-2-specific) — for non-English sources, direction claims survive a second-pass confirmation; ambiguous filings default to `unknown` direction.
10. **Cross-listing** (tool-2-specific) — is this signal genuinely new, or is it the same underlying event echoing across multiple exchange listings of the same issuer?
11. **Narrative** — does the market/world narrative match, conflict with, or ignore this thesis?

## 5. Data and Source Discipline

- Verify, don't remember. Endpoints, schemas, filing-type codes are verified live per session via the Tool Validation Protocol, not recalled from training memory.
- Never assume an API field exists. Probe it. If it does not exist, fail forward and document in `OPEN_QUESTIONS.md`.
- Cite every claim. Every statement in a candidate writeup links to the exact source URL and filing date.
- Flag source conflicts explicitly. If two filings on the same exchange contradict each other, the conflict itself is the signal.
- Rate limits are respected. Polite delay between requests is built into every scanner.

## 6. Workspace Structure

- One concept per file. No monolithic documents.
- Update `INDEX.md` in the same turn any file is created or substantially changed.
- Append to `PROGRESS_LOG.md` after every completed work block.
- Record every decision in `DECISIONS.md` with context, alternatives, implications.
- Never delete. Superseded work moves to `archive/YYYY-MM-DD_reason/`.

## 7. Autonomous Execution

Scheduled sessions take initiative. They question the plan continuously — if `SESSION_STATE.md` says "run scanner X" but scanner X's endpoint is down, the session logs the blocker to `OPEN_QUESTIONS.md` and advances to the next productive work unit rather than stalling.

Scheduled sessions never ask questions in chat. No human is present.

## 8. Session Continuity Protocol

Cold-start read order (5 files max, strictly in order):

1. `SESSION_STATE.md` — the relay baton.
2. `INSTRUCTIONS.md` — architecture and pipeline.
3. `OPEN_QUESTIONS.md` — only if `SESSION_STATE` flags blockers.
4. One task-specific file — the strategy spec, the scoring rubric, or the candidate template.

The test: if after these reads a session cannot state "here is what I will do next," `SESSION_STATE.md` has failed. Fix the file, not the protocol.

Shutdown protocol: flush working state → overwrite `SESSION_STATE.md` → append to `PROGRESS_LOG.md` → update `INDEX.md` → overwrite `SESSION_LOCK.md` with `UNLOCKED`. Lock release is always the last step.

## 9. Maximum Utilization Rule

Work until the usage limit. Never sacrifice handoff quality for extra work. Hierarchy under context pressure: **handoff quality > output quality > output volume.** When uncertain about remaining capacity, shut down cleanly one step early.

## 10. Scheduled Session Behavior

- No chat questions. Write to `OPEN_QUESTIONS.md` and continue.
- Dependencies reset every session. Always reinstall.
- Fail forward. If one scanner's endpoint is broken, skip that scanner and run the other eight.
- `SESSION_STATE.md` is the contract. If it says something is done, it's done. If it says something is next, start it.
- Settled decisions are not re-litigated. Reopen only with concrete new evidence.

## 11. The Standing Question

Before every meaningful output:

> Is this the highest-quality, most accurate, most thoroughly validated, most insightful result I am capable of producing for this objective — and if not, what specifically do I need to do before I am willing to call it done?

If the answer is anything other than an honest yes, keep working.

---

## Reporting (external) — added 2026-04-15

Producer-only. Reporting is handled by the project-root `Reporting Hub/`. See the hub's `REPORTING_INSTRUCTIONS.md` and `SOURCES.md`. Do not write outside `non_us_discovery_system/`.
