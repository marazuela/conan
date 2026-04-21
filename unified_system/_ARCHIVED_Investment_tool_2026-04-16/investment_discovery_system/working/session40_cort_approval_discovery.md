# CRITICAL FINDING: CORT Relacorilant Already Approved (March 25, 2026)

## Discovery
Session 40 discovered that CORT (Corcept Therapeutics) received FDA approval for relacorilant (branded as **Lifyorli**) on **March 25, 2026** — well ahead of the July 11, 2026 PDUFA date.

## Source
- FDA official: https://www.fda.gov/drugs/resources-information-approved-drugs/fda-approves-relacorilant-nab-paclitaxel-platinum-resistant-epithelial-ovarian-fallopian-tube-or
- Corcept IR: https://ir.corcept.com/news-releases/news-release-details/fda-approves-corcepts-selective-glucocorticoid-receptor
- BusinessWire: https://www.businesswire.com/news/home/20260325948774/

## What Happened
- FDA approved Lifyorli (relacorilant) + nab-paclitaxel for platinum-resistant ovarian cancer on March 25
- Approval was ~3.5 months ahead of the July 11 PDUFA target action date
- CORT surged ~19.66% on the day of approval

## Impact on Our System
1. **CORT is no longer a PDUFA candidate** — the binary event has already resolved
2. **Remove from PDUFA watchlist** — the Jul 11 date is obsolete
3. **Remove from watchlist scoring** — score of 27.25 was for a PDUFA event that already happened
4. **Update pdufa_watchlist.json** if CORT is in it
5. **S39's preliminary scoring was unknowingly scoring a resolved event**

## Lessons Learned
- Early FDA approvals (ahead of PDUFA) can happen and our system should check for this
- The FDA PDUFA pipeline scanner should cross-reference FDA approvals database to filter out already-approved drugs
- Web research layer is critical for catching these — the structured data (PDUFA date) was stale while the real-world narrative had moved on

## What CORT Is Now
- Approved oncology drug, first-in-class selective glucocorticoid receptor antagonist
- Separate hypercortisolism CRL still pending (different indication)
- Stock at $41.91 — post-approval stabilization
- No longer a binary catalyst play; now an execution/commercial launch story
