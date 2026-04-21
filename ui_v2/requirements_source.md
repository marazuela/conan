**Unified Investment Research System**

Architecture, Components, Logic, and Data Flow

*Current state as of 2026-04-20*

*Prepared for Pedro --- meeting briefing*

########## 1. Executive Overview

The Unified Investment Research System is a primary-source, event-driven
discovery pipeline that scans 13 global markets plus United States
federal and state litigation for pre-edge situations --- setups
observable in regulatory filings, court dockets, and trial registries
before the broader market prices them. The system consolidates what were
previously six parallel toolchains (Investment Tool, Beta, Delta, Gamma,
Reporting Hub, and the Independent Review set-up) into a single codebase
under unified\_system/, with one signal log, one convergence engine, and
one reporting layer.

The mandate is explicit and narrow: surface 2 to 5 high-conviction,
actionable opportunities per week, each tradable at a position size of
\$3 million or greater, each carrying a clear catalyst with a dated
timeline and explicit kill conditions. Everything else is anti-goal ---
this is not a general market-scanner, not a news aggregator, and not a
back-testing framework.

The architectural inflection that shapes the current codebase is the
AVNS miss on 2026-04-14, where American Industrial Partners announced a
\$25-per-share take-private of Avanos at a 72% premium. The system had
enough primary-source signal to surface Avanos pre-edge but did not.
That miss drove Decision D-013 (the pre-edge mandate) and the two newest
scanners --- takeover\_candidate\_scanner and
pre\_phase3\_readout\_scanner --- both of which went operational on
2026-04-20. A sixth scoring profile, takeover\_candidate, was added to
score these pre-edge hypotheses against a separate rubric that rewards
setup strength and edge freshness rather than announced-deal spread.

#################### 1.1 System at a glance

  **Primary folder**        C:\\Users\\javie\\OneDrive\\Desktop\\Claude Cowork\\Conan\\unified\_system\\
  ------------------------- ----------------------------------------------------------------------------------------------------------------
  **Scanners registered**   17 (all operational as of 2026-04-20)
  **Scoring profiles**      6 --- merger\_arb, activist\_governance, binary\_catalyst, short\_positioning, litigation, takeover\_candidate
  **Signal log size**       733 signals (after 2026-04-20 ingestion adding 239)
  **Market-cap floor**      \$215M USD universal (≈ €200M)
  **Convergence window**    14 days standard, 30 days when any signal is litigation
  **Rolling retention**     14 days standard, 90 days for litigation
  **Band thresholds**       Immediate ≥35, Watchlist 25--34, Archive 15--24, Discard \<15
  **Active candidates**     RPAY, AXSM, VERA, VRDN, RGR (all pre-edge)
  **Archived post-edge**    TVTX (WIN), AVNS (MISS), GSAT (WIN), SEM (NEUTRAL)
  **Scheduled tasks**       unified-operational (3h), unified-maintenance (3h @ :50), unified-reporting (4h @ :30)

########## 2. Mandate and Philosophy

#################### 2.1 What the system is asked to do

The explicit deliverable defined in docs/OBJECTIVES.md is a weekly
shortlist of 2 to 5 opportunities that a human can act on. Each
opportunity must carry: (a) a thesis in plain language naming the
mispricing, (b) a catalyst on a dated timeline inside the next 12
months, (c) a position sizing floor of \$3 million in tradable
liquidity, and (d) explicit kill conditions that define when the trade
is wrong and should be exited.

Anti-goals are documented with equal clarity. The system does not
generate market commentary, does not score macro trends, does not
attempt to predict index direction, does not aggregate news, and does
not run back-tests. It is a discovery-and-scoring pipeline for discrete,
dated, company-specific events.

#################### 2.2 The pre-edge mandate (D-013)

Decision D-013 is the organizing principle behind the current
architecture. After the AVNS miss, the team concluded that the system\'s
value is strictly pre-announcement: once a deal is public, the edge is
gone and the opportunity is available to anyone running a merger-arb
screen. The codebase now treats any post-edge form --- a definitive
merger agreement (DEFM14A), a tender-offer filing (SC TO-T), a
going-private filing (SC 13E3), or a prospectus for an already-announced
deal (Form 425) --- as a disqualifier for new candidate entry under the
takeover\_candidate profile. Merger\_arb remains available for announced
deals, but the emphasis of net-new discovery is pre-edge.

#################### 2.3 Primary sources only

Every scanner reads from a regulator, exchange, or primary registry. The
system does not consume news feeds, press aggregators, or third-party
screening tools as signal sources. This is both a data-quality choice
(primary sources are the canonical version of what was said) and a
defensibility choice (every candidate dossier must cite the underlying
filing and URL).

########## 3. High-Level Architecture

The system is a four-layer pipeline. Each layer has a single
responsibility and communicates with the next through files on disk, not
in-process calls. This isolation is deliberate: it lets any layer be
re-run independently, it survives partial failures, and it makes every
run auditable.

#################### 3.1 The four layers

  **Layer**                                         Responsibility
  ------------------------------------------------- ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------
  **Layer 1 --- Discovery (scanners)**              Read primary sources, emit normalized signal JSON. Each scanner is a subprocess launched by pipeline\_runner.py with a hard timeout. 17 scanners today.
  **Layer 2 --- Scoring & entity resolution**       run\_post\_scan.py ingests all scanner outputs, resolves entities via OpenFIGI, applies the matching scoring rubric, enforces auto-caps, and appends to the rolling signal log.
  **Layer 3 --- Convergence**                       convergence\_engine.py groups signals across scanners by issuer FIGI (with fallbacks) within the 14/30-day window, classifies relationships (same-direction, orthogonal, contradiction), and awards bonus points (+5 for two signals, +10 for three or more).
  **Layer 4 --- Candidate promotion & reporting**   candidate\_gate.py enforces the thesis quality gate (D-008). reporting/ produces PDF and markdown outputs for the human reviewer. A separate REPORTING\_LOCK keeps reporting independent of the scan session.

#################### 3.2 Data flow end-to-end

A single cycle of the operational pipeline proceeds as follows. The
scheduler fires unified-operational every three hours.
pipeline\_runner.py reads config/scanner\_registry.json and identifies
every scanner whose cadence has elapsed. Each due scanner is launched as
a subprocess (per Decision D-014 --- process isolation so a crash or
hang cannot take down the pipeline) with a hard wall-clock budget
(typically 35 to 120 seconds). On success, each scanner writes
signals/\<scanner\_name\>\_scanner\_output.json containing a list of raw
signals with source URLs, issuer identifiers, and the scoring\_profile
the scanner wants applied.

After all subprocesses return or are killed, run\_post\_scan.py ingests
every \_scanner\_output.json. For each signal, it applies the matching
rubric from WEIGHTS, clamps raw dimensions to 1--5, computes a weighted
total, classifies it into a band, and runs profile-specific auto-cap
rules. The scored signal is appended to signals/signal\_log.json
atomically (write .tmp, replace .bak, rename in place --- Decision D-052
guarantees no half-written log even if the process is interrupted).

convergence\_engine.py then groups the rolling log by issuer\_figi
(falling back through ticker+mic, codigo\_cvm, id\_empresa\_biva,
stock\_code, and normalized company name if FIGI is missing) inside the
active window. Where two or more profiles converge on the same issuer,
the group earns a bonus (+5 for two, +10 for three or more) and is
flagged as same-direction, orthogonal, or contradictory.

Finally, reporting runs on its own four-hour cadence. It reads the
current signal log, applies REPORTING\_LOCK so it never collides with an
in-flight scan, and produces the executive PDF via reportlab (D-005 ---
no weasyprint or wkhtmltopdf), plus markdown dossiers for each active
candidate drawn from candidates/.

#################### 3.3 Key isolation decisions

-   D-014 --- scanner subprocess isolation: every scanner runs in its
    own process so a crash, OOM, or hang cannot break the pipeline.
    pipeline\_runner.py hard-kills at the budget.

-   D-018 --- EDGAR 35-second budget: all EDGAR-derived scanners share a
    35-second wall-clock budget to keep the pipeline bounded regardless
    of SEC latency.

-   D-052 --- atomic writes: signal\_log.json, candidate files, and
    reports use write-tmp / backup-existing / rename-in-place. If the
    log is corrupt on load, .bak is consulted before starting fresh.

-   D-005 --- reportlab-only for PDF: reportlab has no external binary
    dependencies and deploys cleanly into the scheduled-task
    environment. weasyprint and wkhtmltopdf are explicitly forbidden.

-   REPORTING\_LOCK is independent of SESSION\_LOCK: reporting cannot
    block scans, and scans cannot block reporting. Either can safely
    fail without the other.

########## 4. The 17 Scanners

Each scanner is a self-contained Python program in tools/ that emits
normalized signal JSON. The scanner\_registry.json file is the
authoritative registration --- it lists cadence, last\_run, timeout,
geography, default scoring profile, and a signal\_type-to-profile map
that lets one scanner emit signals for multiple profiles (for example,
edgar emits activist\_governance for 13D, merger\_arb for DEFM14A, and
litigation for 10-K litigation disclosures).

The scanners group by region and source. Below, each scanner is
described under the same five headings --- rationale (why it exists),
edge (why it is valuable), data source, mechanics, and the specific
filter it applies to select raw signals for scoring.

#################### 4.1 US primary-regulatory scanners

############################## edgar\_filing\_monitor (v2.4)

**Rationale:** SEC EDGAR is the canonical disclosure system for every
US-listed issuer. Most of the event types the system cares about ---
activist stakes, proxy contests, definitive mergers, going-private
transactions, distress filings, and 10-K litigation disclosures ---
first appear as EDGAR forms. This is the system\'s single
highest-information scanner.

**Edge:** The edge is velocity and depth combined. EDGAR\'s EFTS
endpoint supports full-text search across the body of filings, not just
form type and filer. Most retail and even many institutional tools only
screen by form type. By running keyword searches inside the document
text, the system catches narrative disclosures (e.g., \'exploring
strategic alternatives\', \'engaged a financial advisor\') that would
not be captured by form-type filters alone.

**Data source:** https://efts.sec.gov/LATEST/search-index (EFTS
full-text), with fallback to the per-company submissions JSON at
data.sec.gov. All calls use the mandatory SEC User-Agent.

**Mechanics:** A curated keyword set runs across forms 10-K, 10-Q, 8-K,
DEF 14A, SC 13D, SC 13G, PREM14A, DEFM14A, 25-NSE, NT 10-K. Each hit
produces a normalized signal with issuer CIK, form type, accession
number, filing timestamp, the matched keyword and a text excerpt, plus a
content hash for dedup. A 35-second wall-clock budget (D-018) caps the
call regardless of SEC latency.

**Candidate picker:** A hit advances if (a) the issuer clears the \$215M
market-cap floor, (b) the form/keyword combination matches one of the
signal\_type rules in scanner\_registry.json, and (c) the content hash
has not already been logged within the rolling window.

**Scoring profile mapping:** Routed by signal\_type: SC 13D →
activist\_governance, DEFM14A → merger\_arb,
strategic-alternatives/banker-mandate language → takeover\_candidate,
litigation disclosures in 10-K/10-Q → litigation.

**Notes / current state:** Handles CIK-to-FIGI via
openfigi\_resolver.py. If OpenFIGI misses, ticker+MIC is used downstream
in convergence.

############################## sec\_enforcement

**Rationale:** SEC Division of Enforcement press releases signal
distress scenarios, going-concern risks, and executive misconduct ---
all meaningful short-positioning catalysts. They are also a leading
indicator for restatements and delistings.

**Edge:** Enforcement outcomes are deterministic information --- the SEC
has already decided to act. That timing edge (release before broader
market digestion) gives one or two days of actionable window on illiquid
names.

**Data source:** https://www.sec.gov/cgi-bin/browse-edgar with
action=getcompany for filer-linked enforcement actions, plus the
press-release feed.

**Mechanics:** Parses press releases and enforcement filings, extracts
named respondents, resolves to ticker via EDGAR CIK mapping.

**Candidate picker:** Issuer present in enforcement action and clears
the \$215M floor. Named individuals without a linked filer are filtered
out unless they map to a current insider of a listed issuer.

**Scoring profile mapping:** Default short\_positioning with a
conditional promotion to litigation when a parallel civil action exists.

############################## courtlistener

**Rationale:** Federal civil dockets, Delaware Chancery cases, and the
Public Access to Court Electronic Records (PACER) RSS feed carry
early-stage litigation signals --- antitrust, securities class actions,
IP disputes --- before the financial press catches them.

**Edge:** Docket-level granularity lets the system see procedural events
(summary-judgment rulings, class certifications, settlement conferences)
that predict financial materiality. Press coverage tends to arrive days
to weeks later.

**Data source:** https://www.courtlistener.com/api/rest/v3/ (Free Law
Project). Requires CourtListener API token --- currently unset (Q-017
blocker).

**Mechanics:** Per-case polling of docket entries, filtered by court
(S.D.N.Y., D. Del., C.D. Cal., D. Del. Chancery) and party-type regex
that flags publicly-traded defendants.

**Candidate picker:** Defendant resolves to a listed issuer, case type
matches the litigation-profile registry (federal civil, Delaware
chancery, DOJ/FTC antitrust, ITC 337, PTAB IPR, SEC civil).

**Scoring profile mapping:** litigation.

**Notes / current state:** Scanner returns status=auth\_required
gracefully until CourtListener API token is configured. Q-017 tracks the
blocker.

############################## congressional

**Rationale:** Congressional calendars (hearings, markups, committee
referrals) and STOCK-Act trade disclosures from members of Congress
carry information edge on regulated sectors --- healthcare, defence,
semis, financials.

**Edge:** Committee action is a leading indicator for regulation that
will move sector multiples. STOCK-Act trade disclosures (within the
45-day reporting window) occasionally reveal unusual concentration by
members with committee-level visibility.

**Data source:** house.gov and senate.gov calendar feeds;
efdsearch.senate.gov and clerk.house.gov trade-disclosure APIs.

**Mechanics:** Polls scheduled-hearings feeds and trade-disclosure
endpoints; emits signals tagged with chamber, committee, and relevant
ticker where the issuer is a publicly-traded name.

**Candidate picker:** Committee topic intersects a target sector and a
listed issuer is named, OR a single disclosure reports concentrated
activity in one issuer above a threshold.

**Scoring profile mapping:** Default activist\_governance for
committee-level corporate-governance actions; short\_positioning when a
distressed-sector hearing is scheduled.

############################## fda\_pdufa\_pipeline (v2.0)

**Rationale:** FDA Prescription Drug User Fee Act (PDUFA) dates are
dated binary catalysts --- the clearest form of event-driven setup on
the healthcare side. The system treats PDUFA as a first-class data type.

**Edge:** PDUFA dates are sometimes publicly reported by sponsors but
not always indexed consistently. The scanner cross-references
ClinicalTrials.gov (trial status and primary completion), openFDA (prior
approvals and labels), and EDGAR 8-K (company disclosure of action-date
expectations) to construct a higher-confidence watchlist than any single
source gives.

**Data source:** clinicaltrials.gov/api/v2/studies, api.fda.gov/drug,
and data.sec.gov 8-K filings for the sponsor CIK.

**Mechanics:** Maintains a persistent auto-populated PDUFA watchlist.
Each candidate drug is scored on approval probability (seeded from
phase3\_approval\_base\_rates.json keyed by indication), competitive
landscape, and a disqualified-ticker list (ZLAB, CORT, ORCA excluded as
explicit noise per prior iteration).

**Candidate picker:** Primary completion within 90 days, issuer clears
\$215M floor, indication maps to a base-rate entry, and ticker is not on
the disqualified list.

**Scoring profile mapping:** binary\_catalyst.

#################### 4.2 European and United Kingdom scanners

############################## esma\_short\_scanner (v2.0)

**Rationale:** ESMA short-disclosure regimes (FCA for UK, AMF for
France, AFM for Netherlands, BaFin for Germany) publish net short
positions above 0.5% daily. Crowded-short setups and reversal-prone
setups are visible from the regulatory tape alone.

**Edge:** Four-regulator consolidation is the edge. Most market data
tools cover one regulator. By unioning FCA/AMF/AFM/BaFin and tracking
position changes across all four, the scanner catches multi-regulator
crowding that a single-source screen misses. A multi-regulator hit earns
a +1 scoring boost in short\_positioning.

**Data source:** fca.org.uk regulatory-disclosure CSV, amf-france.org
decisions endpoint, afm.nl net-short-positions feed, bafin.de
Leerverkaufs-Meldung feed.

**Mechanics:** Daily poll of each regulator\'s publication feed.
Thresholds: new position at 0.5%, position change at 0.2%, crowded flag
at 3+ named holders on the same issuer, large flag at any individual
holder above 2%.

**Candidate picker:** Issuer clears \$215M floor, aggregate short \>=
0.5% of issued capital, and at least one of the four thresholds (new /
change / crowded / large) is hit.

**Scoring profile mapping:** short\_positioning.

**Notes / current state:** CNMV (Spain) and CONSOB (Italy) feeds are
currently blocked by format/auth changes and are intentionally excluded
from the regulator set.

############################## lse\_rns\_scanner

**Rationale:** London Stock Exchange Regulatory News Service is the
primary disclosure channel for LSE-listed issuers and AIM. Activist
positions (Holdings in Company / TR-1), offer periods, and
scheme-of-arrangement announcements first appear here.

**Edge:** Investegate.co.uk aggregates RNS with stable URLs and headline
normalization that the LSE\'s own pages do not. Combining Investegate
enumeration with the LSE\'s alldata API for structured metadata gives a
richer signal than either source alone.

**Data source:** investegate.co.uk (enumeration), lse.com alldata API
(metadata), openfigi.com (FIGI resolution).

**Mechanics:** Polls Investegate for new RNS items, resolves each via
LSE alldata to get SEDOL/ISIN, runs a headline regex classifier to
assign a signal\_type (TR-1 Holdings, Offer Period, Scheme of
Arrangement, Trading Update, Going-Concern), then resolves to FIGI.

**Candidate picker:** Issuer clears \$215M floor (converted to GBP at
rolling rate), and headline matches one of the monitored signal types.

**Scoring profile mapping:** Routed by signal\_type: TR-1 →
activist\_governance, Offer/Scheme → merger\_arb, Going-Concern →
short\_positioning.

#################### 4.3 Asia-Pacific scanners

############################## tdnet (Japan)

**Rationale:** TDnet is the Tokyo Stock Exchange\'s timely-disclosure
channel --- mandatory within 30 minutes of material events for
TSE-listed issuers. Management buyouts, tender offers, and earnings
revisions hit TDnet first.

**Edge:** Japanese ticker conventions differ between JPX and OpenFIGI
--- the latter expects 4-character tickers, while newer TSE listings
(including some REITs and mid-caps) carry 5-character tickers ending in
\'0\'. Without the normalization fix, FIGI resolution fails silently on
the whole cohort. The system carries a targeted normalizer: when
len(ticker)==5 and ticker\[3\].isalpha() and ticker\[4\]==\'0\' and MIC
is JP, it strips the trailing \'0\' before querying OpenFIGI (example:
469A0 → 469A). This is the critical TDnet fix and it lives in
tools/openfigi\_resolver.py::normalize\_ticker.

**Data source:** tdnet.info disclosure list (HTML), parsed to a
structured feed.

**Mechanics:** Polls the TDnet list page, extracts each disclosure\'s
title, timestamp, ticker, and linked PDF/HTML. Headline regex classifies
MBO, tender offer, earnings revision, going-concern.

**Candidate picker:** Issuer clears \$215M USD floor (JPY converted),
headline matches monitored signal types.

**Scoring profile mapping:** Routed: MBO/tender → merger\_arb (or
takeover\_candidate when pre-edge), earnings revision with downward
guidance → short\_positioning.

############################## asx (rewritten 2026-04-20)

**Rationale:** Australian Securities Exchange Announcements feed carries
substantial holdings, takeover bids, and trading halts. Mid-cap
Australian resources and healthcare frequently show M&A setups before
they reach the Wall Street tape.

**Edge:** Concurrent polling of ticker announcements --- previously the
scanner serialized its requests and hit the 120-second hard kill. The
2026-04-20 rewrite adopts ThreadPoolExecutor with a rotation checkpoint,
cutting 200 tickers to roughly 11 seconds. That makes it feasible to
poll the entire target universe every operational cycle rather than a
sample.

**Data source:** asx.com.au/asx/statistics/announcements.do endpoints,
one per ticker.

**Mechanics:** ThreadPoolExecutor with concurrency capped to respect ASX
rate limits. Rotation checkpoint persists last-seen announcement IDs so
on restart the scanner resumes without re-emitting duplicates.
Wall-clock budget enforced per cycle.

**Candidate picker:** Issuer clears \$215M floor (AUD converted),
announcement type in the monitored set (Substantial Shareholder Notice,
Takeover Bid, Scheme Booklet, Trading Halt).

**Scoring profile mapping:** Routed: Substantial Shareholder →
activist\_governance, Takeover/Scheme → merger\_arb or
takeover\_candidate, Trading Halt → short\_positioning.

**Notes / current state:** Prior 120s-timeout failure resolved.
Known-good state since 2026-04-20.

############################## hkex (Hong Kong)

**Rationale:** Hong Kong Exchanges and Clearing (HKEx) HKEXnews is the
disclosure channel for both Hong Kong primary-listed names and
dual-listed China ADRs. Substantial shareholder filings (SFC Disclosure
of Interests) and privatization schemes surface here.

**Edge:** Cross-references HKEx filings with dual-listed US ADR
counterparts --- an HKEx disclosure often precedes the corresponding
20-F or 6-K amendment by hours, which becomes a latency edge on the ADR
side.

**Data source:** hkexnews.hk e-submission feed, sfc.hk SDI (Disclosure
of Interests) extracts.

**Mechanics:** Polls HKExnews search, classifies by announcement
category, resolves dual-listed ADR where applicable via CUSIP crosswalk.

**Candidate picker:** Issuer clears \$215M USD floor (HKD converted),
category in the monitored set.

**Scoring profile mapping:** SDI → activist\_governance;
Scheme/Privatization → merger\_arb or takeover\_candidate.

############################## kind (OpenDART, Korea)

**Rationale:** KIND and OpenDART are the Korean disclosure systems.
Korean chaebol activism, foreign-investor stake filings, and
going-private setups are visible here.

**Edge:** OpenDART publishes structured XBRL for material events ---
significantly richer than the unstructured RNS-style feeds in some other
jurisdictions.

**Data source:** opendart.fss.or.kr API --- requires OPENDART\_KEY.
Currently unset (Q-019 blocker).

**Mechanics:** Daily structured query by report type (major
equity-holding changes, governance resolutions, corporate-restructuring
disclosures).

**Candidate picker:** Issuer clears \$215M USD floor (KRW converted),
report type in the monitored set.

**Scoring profile mapping:** Equity-holding changes →
activist\_governance; restructuring → takeover\_candidate or
merger\_arb.

**Notes / current state:** Scanner returns status=auth\_required
gracefully until OPENDART\_KEY is configured.

#################### 4.4 India and Latin America

############################## bse\_nse (India --- NSE path)

**Rationale:** National Stock Exchange and Bombay Stock Exchange
disclosures cover India\'s mid-cap universe. Promoter-pledge increases,
SAST (substantial acquisition) filings, and open-offer announcements
surface here.

**Edge:** Covers a universe that most Western data vendors under-weight.
SAST and promoter-pledge data are the single most reliable Indian
pre-edge indicators.

**Data source:** nseindia.com corporate-announcements API, bseindia.com
announcement feed.

**Mechanics:** NSE-primary polling (BSE as backup given more restrictive
anti-scraping), headline classifier for SAST Regulation 29 disclosures,
open-offer 8(1), pledge Regulation 31.

**Candidate picker:** Issuer clears \$215M USD floor (INR converted),
headline matches monitored set.

**Scoring profile mapping:** SAST → activist\_governance; Open Offer →
merger\_arb; Pledge → short\_positioning.

############################## cvm (Brazil)

**Rationale:** Comissão de Valores Mobiliários is the Brazilian
regulator. IPE (Informações Periódicas e Eventuais) is the disclosure
dataset for material events on B3-listed issuers.

**Edge:** The IPE dataset is structured CSV with consistent event codes
--- cleaner than scraping B3\'s HTML directly. Tender offers (OPA),
cross-border mergers, and activist-stake disclosures all have discrete
codes.

**Data source:** CVM IPE dataset via dados.gov.br distribution.

**Mechanics:** Daily dataset ingest keyed by codigo\_cvm (the Brazilian
issuer identifier), which also becomes a convergence-fallback key when
FIGI is unavailable.

**Candidate picker:** Issuer clears \$215M USD floor (BRL converted),
IPE event code in the monitored set.

**Scoring profile mapping:** Stake disclosure → activist\_governance;
OPA → merger\_arb.

############################## bmv (Mexico, via BIVA JSON API)

**Rationale:** Bolsa Mexicana de Valores and the competing BIVA exchange
both list Mexican equities. Cross-border M&A between US strategic buyers
and Mexican targets, and domestic grupo-activism, surface in BIVA\'s
structured feed.

**Edge:** BIVA\'s JSON API is more reliable than BMV\'s legacy HTML feed
and returns a cleaner typed event stream. The scanner uses
id\_empresa\_biva as a convergence-fallback identifier when FIGI
resolution is unavailable.

**Data source:** biva.mx JSON endpoints.

**Mechanics:** Polls BIVA\'s event feed; emits signals by event\_type
with id\_empresa\_biva retained for convergence fallback.

**Candidate picker:** Issuer clears \$215M USD floor (MXN converted),
event\_type in the monitored set.

**Scoring profile mapping:** Activist-stake → activist\_governance;
tender-offer → merger\_arb.

#################### 4.5 Canada

############################## sedar\_plus (Canada)

**Rationale:** SEDAR+ is the consolidated Canadian securities filings
system --- the Canadian analog of EDGAR. Early-warning reports,
take-over bids, and material change reports surface here.

**Edge:** Canadian mid-caps (natural resources, cannabis, healthcare)
are chronically underscreened by US-centric tools. Early-warning reports
(Regulation 62-103) trigger below the US 5% threshold.

**Data source:** sedarplus.ca API with ca\_universe.json as a probe list
for the scan perimeter.

**Mechanics:** Works through the ca\_universe.json list on a rotating
cadence to keep within the SEDAR+ rate ceiling.

**Candidate picker:** Issuer clears \$215M USD floor (CAD converted),
filing type in the monitored set (Early Warning, Take-Over Bid Circular,
Material Change Report, NI 62-103 Alternative Monthly Report).

**Scoring profile mapping:** Routed by filing type: Early Warning / AMR
→ activist\_governance; Take-Over Bid → merger\_arb; Material Change
with strategic-alternatives language → takeover\_candidate.

**Notes / current state:** Known defect (Q-018): the CLI entry point
does not write signals/sedar\_plus\_scanner\_output.json. Fix deferred.
Scanner is otherwise operational and feeds the library when called
programmatically.

#################### 4.6 Pre-edge additions (new 2026-04-20)

Both of these scanners were built in direct response to the AVNS miss.
They are the operational embodiment of the D-013 pre-edge mandate.

############################## takeover\_candidate\_scanner

**Rationale:** The AVNS miss established that the system needed explicit
pre-announcement coverage of take-private setups, not just
post-announcement merger-arb coverage. This scanner scores the setup,
not the spread.

**Edge:** Combines three orthogonal primary-source signals: (1) 13G or
13D filings from a curated allowlist of private-equity filers
(config/pe\_filer\_allowlist.json, 39 CIKs --- Silver Lake, KKR, Apollo,
Blackstone, Thoma Bravo, etc.), (2) strategic-review language in 8-K,
10-K and 10-Q body text (\'exploring strategic alternatives\',
\'financial advisor engaged\', \'Board has initiated a review\'), and
(3) streamlined-for-sale patterns (consecutive divestitures followed by
a CFO change or a cost reprint). Any one signal is weak; two or more in
the 14-day window becomes a high-confidence takeover setup.

**Data source:** EDGAR full-text search (same EFTS endpoint as
edgar\_filing\_monitor but scoped to the PE-filer and keyword sets).

**Mechanics:** Weekly cadence, 90-second budget. Filters out post-edge
forms (DEFM14A, SC TO-T, SC 13E3, 425) at the source --- a company with
a definitive agreement is disqualified from this profile immediately.
Emits 115 pre\_phase3 + 87 takeover signals were added in the 2026-04-20
ingestion bringing the signal log from 351 to 733. Wait --- correction:
the new takeover signals came from this scanner (87 on initial run).

**Candidate picker:** Passes the five-pattern triage gate with at least
two patterns hit. Market cap above \$215M. No definitive merger
agreement currently in effect. No rejected prior bid in the trailing six
months (would cap to archive).

**Scoring profile mapping:** takeover\_candidate.

**Notes / current state:** Uses config/pe\_filer\_allowlist.json.
Strategies spec: strategies/pre\_edge\_takeover\_candidate.md.

############################## pre\_phase3\_readout\_scanner

**Rationale:** The healthcare analog of the takeover scanner. Phase 3
readouts are the clearest dated binary catalysts in biotech and the
system needs explicit pre-readout discovery --- not just post-readout
news.

**Edge:** Queries ClinicalTrials.gov v2 directly for trials with
PrimaryCompletionDate in the next 90 days and ACTIVE\_NOT\_RECRUITING or
COMPLETED status. Then maps each indication to a base-rate
approval-probability key using a library of 30+ regex patterns
(config/phase3\_approval\_base\_rates.json, 36 entries covering oncology
sublanes, CNS, rare disease, and infectious disease). This seeds the
binary\_catalyst scoring dimension 1 (approval\_probability) with
indication-specific priors rather than a flat default.

**Data source:** clinicaltrials.gov/api/v2/studies with StudyFilters for
Phase 3, PrimaryCompletionDate window, and status.

**Mechanics:** Daily cadence. For each qualifying trial, emit one signal
containing: NCT ID, sponsor (resolved to listed issuer via openFDA and
EDGAR CIK crosswalk), indication, PrimaryCompletionDate, and the
base-rate approval probability for the matched indication.

**Candidate picker:** Sponsor is publicly traded and above \$215M,
PrimaryCompletionDate within 90 days, indication maps to a base-rate
entry.

**Scoring profile mapping:** binary\_catalyst with a seeded
approval\_probability dimension.

**Notes / current state:** Seeded 115 new signals in the 2026-04-20
ingestion. Strategies spec: strategies/pre\_edge\_phase3\_readout.md.

########## 5. Scoring --- the six profiles

Scoring is a pure function of the signal\'s attributes and the profile
rubric. The profile is assigned by the scanner (via the
signal\_type\_profile\_map in scanner\_registry.json). If the profile is
missing or unknown, run\_post\_scan.py falls back to
activist\_governance as a safe default. Each rubric scores a fixed set
of dimensions on a 1--5 scale (clamped to the range on ingest),
multiplies by the dimension weight, and sums to a weighted total. The
band is then classified: Immediate ≥35, Watchlist 25--34, Archive
15--24, Discard \<15. Finally, profile-specific auto-caps may downgrade
the band.

The weights live in tools/run\_post\_scan.py::WEIGHTS. The framework
markdown files (framework/profile\_\*.md) are the human-readable
rubrics. The two must match.

#################### 5.1 merger\_arb --- announced deal spread

Scores announced M&A deals. Max score = 5 × (3 + 2.5 + 2 + 1.5 + 1) =
50.

  **\#**   **Dimension**       **Weight**   **What it measures**
  -------- ------------------- ------------ --------------------------------------------------------------------------------------------------------------
  1        Spread size         ×3           Annualized and absolute spread to deal consideration. Wider spreads with clear deal certainty score highest.
  2        Deal certainty      ×2.5         Regulatory path, financing commitments, shareholder approval dynamics, reverse-termination protection.
  3        Annualized return   ×2           Return adjusted for expected close date. Sub-scale returns hit auto-cap A.
  4        Break risk          ×1.5         Downside to pre-announcement price on a break. Break-risk 1 with deal-certainty ≤2 triggers auto-cap B.
  5        Liquidity           ×1           30-day ADV and spread.

Auto-caps --- Rule A: annualized return below risk-free + 3% caps
Immediate → Watchlist. Rule B: break\_risk=1 with deal\_certainty≤2 caps
Immediate → Watchlist.

#################### 5.2 activist\_governance --- 13D, proxy contests, TR-1, SDI

Scores activist stakes and governance events. Max score = 5 × (2 + 2 +
1.5 + 1.5 + 1 + 1 + 1) = 50.

  **\#**   **Dimension**           **Weight**   **What it measures**
  -------- ----------------------- ------------ -----------------------------------------------------------------------------------------------
  1        Signal strength         ×2           Size of stake, specificity of demands (13D with explicit demands \> 13G \> passive Rule 13f).
  2        Information asymmetry   ×2           How much edge the filing reveals that is not already public.
  3        Activist track record   ×1.5         Named activist\'s historical win rate and average duration-to-settlement.
  4        Risk/reward             ×1.5         Target company\'s valuation gap to activist-implied fair value.
  5        Catalyst clarity        ×1           Is there a dated annual meeting, proxy deadline, or stated deadline?
  6        Edge decay              ×1           How stale is the filing. Filings older than 30 days lose scoring weight.
  7        Liquidity               ×1           30-day ADV and borrow availability.

#################### 5.3 binary\_catalyst --- FDA, regulatory, dated events

Scores dated binary catalysts (PDUFA, Phase 3 readouts, FDA panels,
antitrust decisions). Max score = 5 × (2.5 + 2.5 + 1.5 + 1.5 + 1 + 1) =
50.

  **\#**   **Dimension**           **Weight**   **What it measures**
  -------- ----------------------- ------------ --------------------------------------------------------------------------------------------------------------
  1        Approval probability    ×2.5         Seeded from phase3\_approval\_base\_rates.json by indication, adjusted by trial design and endpoint quality.
  2        Market mispricing       ×2.5         Implied probability from option skew and analyst priors vs. the base rate.
  3        Magnitude               ×1.5         Expected +/- move on approval vs. rejection.
  4        Competitive landscape   ×1.5         Number of alternatives in-indication and whether this is first-in-class / best-in-class.
  5        Catalyst timeline       ×1           Days to dated readout.
  6        Liquidity               ×1           30-day ADV, option open interest, borrow availability.

Auto-cap --- EV floor: expected value = p × upside − (1−p) ×
\|downside\|. If EV \< 5%, cap Immediate → Watchlist.

#################### 5.4 short\_positioning --- crowding and distress

Scores short-side setups. Max score = 5 × (2.5 + 2 + 2 + 1.5 + 1 + 1) =
50.

  **\#**   **Dimension**        **Weight**   **What it measures**
  -------- -------------------- ------------ ---------------------------------------------------------------------------------------------------
  1        Crowding intensity   ×2.5         Aggregate short as % of issued capital and number of named holders. Multi-regulator hits earn +1.
  2        Trend direction      ×2           Increasing vs. decreasing position trajectory across the rolling window.
  3        Catalyst proximity   ×2           Time to next scheduled disclosure (earnings, going-concern check, regulatory action).
  4        Size vs. float       ×1.5         Short interest normalized by free float (better metric than issued capital for squeeze risk).
  5        Historical analog    ×1           Similar setups in the same sector with known outcomes.
  6        Liquidity            ×1           Borrow availability and cost-to-borrow.

#################### 5.5 litigation --- case-driven materiality

Scores litigation-driven situations. Max score = 5 × (3 + 2 + 2 + 1.5 +
1 + 0.5) = 50.

  **\#**   **Dimension**                 **Weight**   **What it measures**
  -------- ----------------------------- ------------ ---------------------------------------------------------------------------------------------------------------------------------------
  1        Financial materiality         ×3           Claimed damages / EV ratio. A claim above 20% of EV is a 5.
  2        Legal outcome probability     ×2           Assessed probability of plaintiff (or defendant) win based on court, docket posture, and precedent.
  3        Market pricing                ×2           Does the stock price reflect the claim? Under-priced cases score highest.
  4        Resolution timeline           ×1.5         Days to next major procedural event (summary judgment, trial, appeal decision).
  5        Liquidity                     ×1           30-day ADV.
  6        Party resolution confidence   ×0.5         Confidence that the defendant in the filing is the listed issuer (not a subsidiary, not a namesake). Below 0.92 auto-caps at Archive.

Retention: 90-day rolling window (vs. 14-day standard) since litigation
cases evolve slowly. Convergence window: 30 days when any signal in a
group is litigation.

#################### 5.6 takeover\_candidate --- pre-edge (new)

The sixth profile, added 2026-04-20. Scores un-announced take-private
candidates. Max score = 5 × (3 + 2 + 2 + 2 + 1) = 50.

  **\#**   **Dimension**             **Weight**   **What it measures**
  -------- ------------------------- ------------ -----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------
  1        Setup strength            ×3           How many of the 5 setup patterns hit (PE take-private setup, streamlined-for-sale, strategic-review disclosure, insider+institutional accumulation, strategic buyer fit). 4--5 patterns + explicit strategic-review language = 5.
  2        Edge freshness            ×2           Recency of key triggering signal. Within 30 days = 5. Over 12 months = 1 (stale).
  3        Valuation cushion         ×2           Discount to 5-year median EV/EBITDA or EV/Revenue. \>35% = 5.
  4        Strategic buyer clarity   ×2           Named strategic with sector M&A history = 5. Unknown PE path = low.
  5        Liquidity                 ×1           30-day ADV.

Triage gate (signal must pass all before scoring): market cap ≥ \$215M
USD; major-exchange listed; no definitive merger agreement currently in
effect; no rejected prior bid in trailing 6 months; at least 2 of 5
setup patterns.

Auto-caps --- definitive merger agreement → disqualify (discard); prior
rejection → cap at Archive; going-concern warning → cap at Watchlist;
fewer than 2 patterns hit → discard.

########## 6. Convergence engine

Convergence is the mechanism by which the system rewards multi-source
corroboration. A signal that appears once in one scanner is interesting.
The same issuer appearing in two or three scanners within the active
window is materially higher-confidence, because it means two independent
primary sources are pointing at the same setup.

#################### 6.1 Grouping

convergence\_engine.py groups signals by issuer\_figi (the Bloomberg
FIGI as resolved by OpenFIGI). When FIGI is unavailable, the engine
falls back through an ordered key set: ticker+MIC, then codigo\_cvm
(Brazil), id\_empresa\_biva (Mexico), stock\_code, and finally a
normalized company-name key. This fallback chain matters because not
every jurisdiction has clean FIGI coverage, and losing a convergence
group because one scanner missed FIGI would silently degrade the whole
pipeline.

#################### 6.2 Windowing

The standard convergence window is 14 days. When any signal in a
candidate group is a litigation signal, the window expands to 30 days to
accommodate the slower pace of case-driven events. The same 14/30
asymmetry governs signal-log retention (see save\_signal\_log in
run\_post\_scan.py).

#################### 6.3 Relationship classification and bonuses

Each converged group is classified as one of three types. Same-direction
means both signals imply the same trade (two activist 13Gs in the same
month). Orthogonal means the signals cover different aspects (an
activist 13D plus a PDUFA date --- the catalyst is independent but both
matter). Contradiction means the signals point in opposite directions (a
13G plus a management-initiated share buyback), which typically
downgrades conviction.

Scoring bonuses: +5 for two-signal convergence, +10 for three or more.
These bonuses are additive to the individual signal scores and can push
a Watchlist-band signal into the Immediate band on their own, which is
the whole point --- convergence is the mechanism that surfaces the
highest-conviction setups.

########## 7. Candidate promotion and the thesis gate

Scoring a signal in the Immediate band does not automatically produce a
candidate. Candidates live in candidates/ as dossiers following
framework/candidate\_template.md --- an 11-section template with every
evidentiary claim tagged VERIFIED, INFERRED, or SPECULATED. Nothing
enters the active candidate set without passing the thesis gate.

#################### 7.1 The thesis gate (D-008)

tools/candidate\_gate.py is the mandatory entry point for promotion. It
enforces minimum-viable-thesis length requirements because the team
learned that terse or boilerplate theses are correlated with bad
promotions.

-   situation: at least 80 characters, plain-language description of
    what is happening.

-   why\_underpriced: at least 100 characters, naming the specific
    mispricing and why the market is missing it.

-   next\_catalyst: at least 40 characters, a dated event inside the
    12-month horizon.

-   next\_catalyst\_date: an ISO date. No TBDs, no \'soon\', no \'Q2
    2026\' without a specific day.

-   kill\_conditions: at least 60 characters, stating what would
    invalidate the trade.

The gate also runs a boilerplate detector (regex) to reject phrases like
\'strong fundamentals\', \'compelling valuation\', or \'attractive entry
point\' unless they appear alongside specific evidence.
promote\_candidate() is the single mandatory entry point --- nothing
else can add to candidates/ without routing through the gate.

#################### 7.2 Current active and archived candidates

  **Status**                   Candidate summary (from candidates/\_curated\_rationales.json)
  ---------------------------- ------------------------------------------------------------------------------------------------------
  **RPAY (active)**            \$4.80 Forager offer on Repay Holdings --- 51% spread, pre-edge stake build visible in 13G sequence.
  **AXSM (active)**            April 30 PDUFA for Alzheimer\'s agitation indication (AXS-05).
  **VRDN (active)**            June 30 PDUFA for thyroid eye disease (TED) drug.
  **VERA (active)**            July 7 PDUFA for atacicept in IgA nephropathy.
  **RGR (active)**             May 27 AGM with Beretta proxy fight.
  **TVTX (archived WIN)**      FSGS approved --- thesis delivered.
  **AVNS (archived MISS)**     AIP take-private at \$25, +67% premium. The miss that drove D-013 and the two new scanners.
  **GSAT (archived WIN)**      Amazon \$90 per share agreement --- thesis delivered.
  **SEM (archived NEUTRAL)**   WCAS at \$16.50 --- neutral outcome.

########## 8. Reporting layer

Reporting is intentionally decoupled from discovery. It runs on its own
four-hour cadence (unified-reporting, 30 \*/4 \* \* \*) and holds its
own REPORTING\_LOCK independent of SESSION\_LOCK. This means reporting
can never block a scan, and a scan can never block reporting --- either
can fail without breaking the other.

#################### 8.1 Outputs

-   Executive PDF --- generated via reportlab (D-005 mandates reportlab
    only; weasyprint, wkhtmltopdf, and pypdf are explicitly forbidden).
    Contains the active candidate set, band distribution, and
    convergence summary.

-   Markdown candidate dossiers --- one file per active candidate,
    sourced from candidates/ and enriched with hand-curated rationale
    cards from candidates/\_curated\_rationales.json (schema v2.2).

-   Post-scan diagnostic reports ---
    working/post\_scan\_report\_YYYY-MM-DD.json written by
    run\_post\_scan.py each cycle, listing per-scanner signal counts and
    the ingestion delta.

########## 9. Scheduling

Three scheduled tasks drive the system. They were consolidated on
2026-04-16 from the legacy per-tool tasks (investment-tool-\*,
non-us-\*, reporting-hub-\*), which are now disabled and must not be
re-enabled.

  **Task**                  Cadence and responsibility
  ------------------------- ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------
  **unified-operational**   0 \*/3 \* \* \* --- every 3 hours on the hour. Runs pipeline\_runner.py → run\_post\_scan.py → convergence\_engine.py in sequence.
  **unified-maintenance**   50 \*/3 \* \* \* --- every 3 hours at :50. Housekeeping: retire stale candidates, refresh config/pe\_filer\_allowlist.json from EDGAR 13F aggregates, update RISK\_FREE\_RATE, roll backups.
  **unified-reporting**     30 \*/4 \* \* \* --- every 4 hours at :30. Generates executive PDF and markdown dossiers. Acquires REPORTING\_LOCK only; does not block scans.

########## 10. Entity resolution and the OpenFIGI layer

Entity resolution is the glue that makes convergence work. Without a
stable cross-scanner identifier, signals from different jurisdictions
cannot be grouped. The system uses OpenFIGI as the primary identifier
because FIGI covers every major exchange, is free to query, and does not
require a CUSIP license.

#################### 10.1 The normalizer

tools/openfigi\_resolver.py is a thin wrapper around OpenFIGI\'s v3
endpoint with per-jurisdiction ticker normalization. The critical fix:
for Japanese tickers, when len(ticker)==5 and ticker\[3\].isalpha() and
ticker\[4\]==\'0\' and the MIC is JP, the trailing \'0\' is stripped
before querying OpenFIGI (example: 469A0 → 469A). Without this, every
new 5-character TSE ticker fails FIGI resolution silently and drops out
of convergence. The normalizer is the single source of truth for this
fix; every scanner resolves through this module.

#################### 10.2 Fallback chain

When FIGI resolution fails, convergence\_engine.py falls back through
ticker+MIC, then codigo\_cvm (Brazil IPE dataset identifier),
id\_empresa\_biva (Mexico BIVA identifier), stock\_code, and finally a
normalized company name. This fallback is ordered from most-specific
(FIGI) to least-specific (name) so that convergence prefers
higher-confidence matches when they exist.

########## 11. Known issues and open items

-   Q-017 --- COURTLISTENER\_TOKEN unset. courtlistener scanner returns
    status=auth\_required gracefully; no signals emitted from US civil
    docket stream until resolved.

-   Q-018 --- sedar\_plus CLI entry point does not write
    signals/sedar\_plus\_scanner\_output.json. Scanner is otherwise
    operational when called programmatically. Fix deferred.

-   Q-019 --- OPENDART\_KEY unset. kind/OpenDART scanner returns
    status=auth\_required gracefully; Korean signals suspended until
    resolved.

-   Q-016 --- validation guard: all Python files in tools/ must end with
    \'\# \-\-- END OF FILE \-\--\'. This is a pre-commit check; any
    scanner that loses the marker fails validation.

-   CNMV (Spain) and CONSOB (Italy) ESMA feeds are blocked by
    format/auth changes and intentionally excluded from
    esma\_short\_scanner.

-   TDnet 469A0-style ticker normalization is implemented
    (openfigi\_resolver.normalize\_ticker) --- must not be reverted.

########## 12. Decisions register (selected)

The full register lives in docs/DECISIONS.md. The decisions that most
shape the current state of the codebase are summarized below.

  **Decision**   Content
  -------------- ------------------------------------------------------------------------------------------------------------------------------------------------------
  **D-003**      Universal market-cap floor \$215M USD (≈ €200M). Matches Tool 1\'s legacy operational floor to preserve candidate continuity across the unification.
  **D-005**      PDF generation uses reportlab only. weasyprint, wkhtmltopdf, and pypdf are forbidden.
  **D-008**      Thesis-required promotion gate. candidate\_gate.py is the sole entry point into candidates/.
  **D-013**      Pre-edge mandate. Post-edge forms (DEFM14A, SC TO-T, SC 13E3, 425) disqualify new takeover\_candidate entries. Driven by the AVNS miss.
  **D-014**      Scanner subprocess isolation. Every scanner runs in its own process with a hard kill at the wall-clock budget.
  **D-018**      EDGAR 35-second budget. All EDGAR-derived scanners share a bounded wall-clock envelope.
  **D-047**      Convergence key fallback chain --- FIGI → ticker+MIC → codigo\_cvm → id\_empresa\_biva → stock\_code → normalized name.
  **D-052**      Atomic writes. Write .tmp, replace .bak, rename in place. If log is corrupt on load, try .bak before starting fresh.

########## 13. Summary and the shape of the edge

The system\'s edge is the combination of four things that are
individually available but rarely combined. First, primary-source-only
ingestion, with every claim traceable to a filing URL, so no signal is
ever \'just something from the news\'. Second, 13 geographies treated
with equal seriousness --- the Indian, Brazilian, Mexican, and Korean
feeds are operated under the same cadence and quality bar as EDGAR,
which is unusual for a tool this size. Third, convergence across
orthogonal sources --- an activist 13D plus an ESMA short disclosure
plus a TDnet MBO hint on a dual-listed name is a higher-confidence
signal than any one of those in isolation, and the convergence engine
formalizes that. Fourth, the pre-edge mandate --- the two newest
scanners (takeover\_candidate and pre\_phase3\_readout) encode the
discipline that the value is in surfacing setups before the market
prices them.

The system is fully operational as of 2026-04-20. 17 scanners, 6
profiles, 733 signals in the rolling log, five active candidates, three
scheduled tasks, and a disciplined separation between discovery,
scoring, convergence, promotion, and reporting. The AVNS miss was the
forcing function for the current architecture --- every piece added
since is a response to \'what primary-source signal existed that we
failed to surface pre-announcement.\'
