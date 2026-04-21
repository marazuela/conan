# ZLAB Triage — Session 22

**Scanner hit**: FDA PDUFA pipeline auto-discovery flagged ZLAB with PDUFA 2026-05-10, drug "Augtyro (repotrectinib) sNDA (probable)".

**Finding: FALSE POSITIVE.** The event is an **NMPA (China)** approval/filing, not a US FDA PDUFA action.

Confirmed timeline:
- **May 2024**: NMPA approved AUGTYRO for ROS1-positive NSCLC in China
- **Recent**: NMPA approved sNDA for AUGTYRO for NTRK solid tumors in China
- **May 2026**: No verifiable US FDA PDUFA date for ZLAB on Augtyro

ZLAB (Zai Lab) is a China pharma with US ADR listing on NASDAQ. It files 8-Ks with SEC when it gets major NMPA approvals (because they're material to ADR holders). The FDA PDUFA auto-discovery keyword-matched the 8-K mention of "Augtyro" and "approval" and speculatively labeled it a US PDUFA event. The "probable" tag on the drug name indicates the scanner flagged low confidence — working as designed.

**Action**: No candidate work, no scoring. Note for FDA PDUFA scanner improvement queue: auto-discovery should down-weight 8-Ks from known China ADR filers (ZLAB, BGNE, LEGN, BBIO-style ADRs) where the regulatory event is likely NMPA not FDA. Low priority — the "probable" tag + manual triage is already catching these.

**Price context** (informational only):
- ZLAB $20.74, +11.4% in 15 days, $2.34B mcap
- Movement likely reflects China biotech sector flows and/or the NMPA NTRK approval, not any US event

**Score**: Not scored (event is not a US FDA catalyst). Archived.

---

*Logged: 2026-04-10 06:22 UTC, Session 22*
