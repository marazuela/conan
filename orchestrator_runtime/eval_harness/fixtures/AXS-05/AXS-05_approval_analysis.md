# FDA Approval Prospects -- AXS-05 for Alzheimer Disease Agitation

**Ticker:** AXSM (Axsome Therapeutics)
**Catalyst:** 2026-04-30 (evaluative)
**As of:** 2026-05-05T05:45:30.882192Z

**P(approval) = 0.62 (range 0.55 to 0.68)** -- spread 0.13, overall confidence 0.71

## Trial set

| NCT | Title | Phase | Status | Sponsor | Enrollment |
|---|---|---|---|---|---|
| NCT05557409 | ADVANCE-2: Addressing Dementia Via Agitation-Centered Evaluation 2: A Randomized | PHASE3 | COMPLETED | Axsome Therapeutics, Inc. | 408 |
| NCT04797715 | A Double-blind, Placebo-controlled, Randomized Withdrawal Trial to Assess the Ef | PHASE3 | COMPLETED | Axsome Therapeutics, Inc. | 178 |
| NCT03226522 | A Randomized, Double-blind, Placebo-controlled Trial to Assess the Efficacy and  | PHASE2, PHASE3 | COMPLETED | Axsome Therapeutics, Inc. | 366 |

## Trial forensics

| Dimension | Signal | Dpp | Finding | Confidence |
|---|---|---|---|---|
| primary_endpoint_achievement | negative | -8 | 0/3 Phase 3 trials hit primary endpoint | 0.85 |
| sap_integrity | neutral | +0 | no trial in set has posted results — SAP integrity not verifiable from CT.gov alone | 0.30 |
| population | neutral | +0 | aggregate enrollment 952 — moderate power | 0.65 |
| trial_design | positive | +3 | 3 pivotal trials — independent replication mitigates single-trial variance | 0.80 |

## AdCom risk

- Status: **low**
- Rationale: no AdCom notices in Federal Register matching term
- Source: https://www.federalregister.gov/api/v1/documents.json?per_page=20&order=newest&conditions[term]=AXS-05%20Alzheimer%20Disease%20Agitation&conditions[type][]=NOTICE

## Label risk

- Boxed warning probability: **0.2**
- REMS likelihood: low
- Indication restriction risk: unknown
- PMR likelihood: moderate
- P2 class precedent available: False
- Confidence: 0.40
- Rationale: Conservative NME class default: boxed-warning base rate ~20% for NMEs per FDA CDER data (2013-2023). REMS + indication-restriction assessment requires P2 class-precedent lookup; P2 not yet built.
- Source: FDA CDER NME approval statistics (2013-2023 aggregate); P2 integration pending

## CMC risk

- Status: **low**
- Rationale: no recent FDA-483 observations identified in primary search (best-effort scan; for full assurance, query the FDA inspection database directly)

## Class precedent

- Class approval rate: 0.667
- Source: openFDA + Federal Register + EDGAR EFTS (offline)

## Assumption ledger

| Adjustment | Sign | Dpp | Rationale | Source | Confidence |
|---|---|---|---|---|---|
| class base rate | anchor | +67 | starting probability | openFDA + Federal Register + EDGAR EFTS (offline) | 0.30 |
| trial: primary_endpoint_achievement | negative | -8 | 0/3 Phase 3 trials hit primary endpoint | ClinicalTrials.gov | 0.85 |
| trial: sap_integrity | neutral | +0 | no trial in set has posted results — SAP integrity not verifiable from CT.gov al | ClinicalTrials.gov | 0.30 |
| trial: population | neutral | +0 | aggregate enrollment 952 — moderate power | ClinicalTrials.gov | 0.65 |
| trial: trial_design | positive | +3 | 3 pivotal trials — independent replication mitigates single-trial variance | ClinicalTrials.gov | 0.80 |
| AdCom risk | neutral | +0 | no AdCom notices in Federal Register matching term | https://www.federalregister.gov/api/v1/documents.j | 0.85 |
| CMC / manufacturing risk | neutral | +0 | no recent FDA-483 observations identified in primary search (best-effort scan; f | https://www.fda.gov/inspections-compliance-enforce | 0.55 |

## Data-quality notes

- label_risk_class_default_confidence_0.40_p2_pending

---

*Skill: analyze-fda-approval-prospects.*
