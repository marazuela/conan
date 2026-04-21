# Candidate Writeup Template

Every candidate file uses this template. Filename convention: `candidates/TICKER_MIC_short-description.md` (e.g., `candidates/7203_XTKS_toyota-guidance-revision.md`). This avoids cross-exchange ticker collisions.

---

```markdown
---
ticker_local: <4-digit or alphanumeric local ticker>
mic: <MIC code>
ticker_plus_mic: <ticker.MIC>
isin: <ISIN if available>
figi: <FIGI>
issuer_figi: <composite FIGI of ultimate issuer>
company_name_local: <original-script name>
company_name_en: <English name>
market_cap_usd_mm: <number>
exchange: <LSE | TDnet | ASX | SEDAR+ | HKEx | KIND | BSE | NSE | CVM | BMV>
country: <ISO 3166 alpha-2>
score: <number, e.g. 32>
convergence_bonus: <0 | 4 | 8>
score_total: <score + convergence_bonus>
status: active | watch | killed | delivered
thesis_direction: long | short | neutral | unknown
translation_confidence: <0.0 – 1.0, or "n/a" for English sources>
first_signal_date: YYYY-MM-DD
last_updated: YYYY-MM-DD
primary_catalyst_date: YYYY-MM-DD | YYYY-MM | "indefinite"
cross_listed_on: [list of other MIC codes]
related_signal_ids: [list]
---

# <Company Name> (<ticker>.<MIC>) — <Short Thesis Title>

## TL;DR (3 sentences max)

<One sentence describing what the system saw.>
<One sentence describing what the thesis is.>
<One sentence describing what would invalidate it.>

## Source signal(s)

List every signal that contributed to this candidate, with direct source URL, filing date, filing type, and a verified/inferred/speculated tag for each claim extracted.

- **Signal 1** — `source_url` — filing date — filing type — extracted claims with tags.
- **Signal 2** — (if convergence) — same structure.

## Translation notes (non-English sources only)

For each translated passage that drives thesis direction, include:
- Original text.
- Translated text.
- Confidence score (0.0 – 1.0).
- What would flip the direction if the translation is wrong.

If `thesis_direction = unknown`, state the specific translation ambiguity that caused it.

## Company context

- Market cap (USD): <number>, local currency market cap: <number>
- Sector: <GICS or local equivalent>
- Recent price action: <30-day, 90-day>
- Analyst coverage: <approx. count, sources if known>
- Cross-listings: <list of exchanges with this issuer's listings>
- Institutional ownership: <if discoverable>

## Thesis statement

A focused paragraph. Answer: what does the market not yet know / not yet price in? What specific claim is the system making? Tag each load-bearing claim as [verified], [inferred], or [speculated].

## Steelman of the opposite view

A focused paragraph. Answer: what would have to be true for this thesis to be wrong? Who would disagree, and with what argument? What data would they point to?

## Web research layer

Mandatory. Search for:
- Recent news on the company (non-primary-source, general press).
- Analyst activity (upgrades, downgrades, price targets).
- Litigation / regulatory actions.
- Social sentiment.
- Sell-side research mentions (if free-accessible).

State whether findings strengthen, weaken, or leave the thesis neutral. Flag kill conditions discovered during web research.

## Kill conditions (explicit, measurable)

Each kill condition:
- Specific event that would invalidate the thesis.
- How it would be observable.
- Where it would appear (filing type, data source).

Example format:
- **Kill 1:** Rule 2.7 offer is withdrawn. Observable in LSE RNS as Rule 2.8 announcement. Check daily during Phase 5 operational.
- **Kill 2:** Q2 results confirm guidance revision was *positive*, reversing the ambiguity. Observable in next Tanshin filing.

## Catalyst map

| Event | Date/Window | Entry trigger | Exit trigger |
|-------|-------------|---------------|--------------|
| ... | ... | ... | ... |

## Position sizing note

Satellite (2–5% of portfolio). Specific caveats for this candidate if any (e.g., tight liquidity requires scaling over multiple sessions; short-lockup requires patience).

## Source traceability

Every source URL, filing date, and retrieval timestamp recorded. Translation sources and confidence scores recorded. OpenFIGI resolution record recorded.

- `<url 1>` — retrieved <timestamp>
- `<url 2>` — retrieved <timestamp>
```

---

## Template usage rules

1. **All frontmatter fields are mandatory.** If a field doesn't apply (e.g., `translation_confidence` for an English source), use `"n/a"` explicitly. Empty fields break the parser.
2. **Thesis statement tags are mandatory.** Every load-bearing claim tagged [verified] / [inferred] / [speculated]. Un-tagged claims are treated as [speculated] by reviewers.
3. **Steelman section is not optional.** If you cannot write a credible steelman, the thesis is probably not clear enough to be a candidate.
4. **Kill conditions must be observable.** "Management reverses course" is not a kill condition; "Q2 10-K contains line item X changed by more than 15%" is.
5. **Web research layer is mandatory even for strong primary-source signals.** Many kill conditions surface only from general press, not from filings.
