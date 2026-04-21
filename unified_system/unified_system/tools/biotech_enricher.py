"""
biotech_enricher.py — deterministic enrichment for biotech binary catalysts.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

REPO = Path(__file__).parent.parent
SIGNAL_LOG = REPO / "signals" / "signal_log.json"
WORKING = REPO / "working"
WORKING.mkdir(exist_ok=True)
CACHE_PATH = WORKING / "biotech_cache.json"
CACHE_TTL_DAYS = 7

BIOTECH_SCANNERS = {
    "pre_phase3_readout_scanner",
    "fda_pdufa_pipeline",
    "fda_pdufa_scanner",
    "fda_advisory_committee_scanner",
    "fda_advcomm_scanner",
    "biotech_catalyst_scanner",
}
USER_AGENT = "Conan engine biotech_enricher/1.0"
HTTP_TIMEOUT_S = 8.0

ENDPOINT_LABELS = {1: "Thin", 2: "Weak", 3: "Moderate", 4: "Strong", 5: "Gold"}
SPONSOR_LABELS = {
    1: "D — unknown / individual",
    2: "C — academic / NIH / FED",
    3: "B — industry (smaller)",
    4: "A — industry (meaningful enrollment)",
    5: "S — large-cap big pharma",
}

BIG_PHARMA_RE = re.compile(
    r"\b("
    r"pfizer|merck(?:\s*&?\s*co)?|novartis|roche|genentech|astrazeneca|"
    r"johnson\s*&?\s*johnson|janssen|bristol[-\s]?myers[-\s]?squibb|bms|"
    r"eli\s*lilly|lilly|glaxosmithkline|gsk|sanofi|takeda|amgen|gilead|"
    r"biogen|regeneron|vertex|moderna|biontech|abbvie|abbott|bayer|"
    r"boehringer[-\s]ingelheim|daiichi[-\s]?sankyo|astellas|otsuka|"
    r"chugai|eisai|celgene|allergan|teva|mylan|viatris|novo\s*nordisk|"
    r"kyowa\s*kirin"
    r")\b",
    re.I,
)
MID_CAP_BIOTECH_RE = re.compile(
    r"\b("
    r"incyte|alnylam|seagen|jazz\s*pharma|horizon\s*therapeutics|"
    r"neurocrine|united\s*therapeutics|exelixis|nektar|bluebird\s*bio|"
    r"arrowhead|ionis|sarepta|ultragenyx|agios|cymabay|madrigal|"
    r"crispr\s*therapeutics|editas|beam\s*therapeutics|intellia|"
    r"argenx|ascendis|mirati|beigene|zymeworks|bioxcel"
    r")\b",
    re.I,
)

HARD_ENDPOINT_RE = re.compile(
    r"\b(overall\s*survival|\bos\b|mortality|death|cardiovascular\s*death|all[-\s]cause\s*mortality|time\s*to\s*death|cv\s*death|mace\b|major\s*adverse\s*cardiovascular)\b",
    re.I,
)
SURROGATE_ENDPOINT_RE = re.compile(
    r"\b(progression[-\s]free\s*survival|\bpfs\b|overall\s*response\s*rate|\borr\b|complete\s*response|\bcr\b|hba1c|ldl[-\s]?c|blood\s*pressure|\bmmrm\b|change\s*from\s*baseline|act20|acr20|acr50|pasi\s*75|pasi\s*90|ejection\s*fraction|fev1|6mwd|six[-\s]minute\s*walk|egfr|viral\s*load)\b",
    re.I,
)
SOFT_ENDPOINT_RE = re.compile(
    r"\b(adverse\s*events?|safety|tolerability|pharmacokinetic|pk\b|acceptability|feasibility|patient[-\s]reported\s*outcome|\bpro\b|quality\s*of\s*life|\bqol\b)\b",
    re.I,
)

INDICATION_RULES: List[Tuple[re.Pattern, str]] = [
    (re.compile(r"\b(alzheimer)", re.I), "neurology_alzheimers"),
    (re.compile(r"\b(amyotrophic\s*lateral|\bals\b|lou\s*gehrig)", re.I), "neurology_als"),
    (re.compile(r"\bparkinson", re.I), "neurology_parkinsons"),
    (re.compile(r"\b(migraine|cluster\s*headache)", re.I), "neurology_migraine"),
    (re.compile(r"\b(epilepsy|seizure|refractory\s*seizure)", re.I), "neurology_epilepsy"),
    (re.compile(r"\b(major\s*depressive|\bmdd\b|treatment[-\s]resistant\s*depression)", re.I), "psychiatry_depression"),
    (re.compile(r"\b(schizophrenia)", re.I), "psychiatry_schizophrenia"),
    (re.compile(r"\b(agitation\b.*(dementia|alzheimer)|alzheimer.*agitation)", re.I), "psychiatry_agitation"),
    (re.compile(r"\b(asthma)", re.I), "respiratory_asthma"),
    (re.compile(r"\b(copd|chronic\s*obstructive)", re.I), "respiratory_copd"),
    (re.compile(r"\b(idiopathic\s*pulmonary\s*fibrosis|\bipf\b)", re.I), "respiratory_ipf"),
    (re.compile(r"\b(inflammatory\s*bowel|crohn|ulcerative\s*colitis|\bibd\b)", re.I), "gastro_ibd"),
    (re.compile(r"\b(nash|non[-\s]?alcoholic\s*steatohepatitis|mash\b|metabolic\s*dysfunction[-\s]associated)", re.I), "gastro_nash"),
    (re.compile(r"\b(chronic\s*kidney\s*disease|\bckd\b)", re.I), "nephrology_ckd"),
    (re.compile(r"\b(fabry|polycystic\s*kidney|alport|iga\s*nephropathy|focal\s*segmental\s*glomerulosclerosis|\bfsgs\b)", re.I), "nephrology_rare"),
    (re.compile(r"\b(hepatitis\s*b|hbv\b|chronic\s*hbv)", re.I), "hepatology_hepb"),
    (re.compile(r"\b(psoriasis(?!\s*arthritis))", re.I), "dermatology_psoriasis"),
    (re.compile(r"\b(atopic\s*dermatitis|eczema)", re.I), "dermatology_atopic_dermatitis"),
    (re.compile(r"\b(rheumatoid\s*arthritis|\bra\b(?!\w))", re.I), "rheumatology_ra"),
    (re.compile(r"\b(wet\s*amd|neovascular\s*age[-\s]related|wet\s*age[-\s]related)", re.I), "ophthalmology_wet_amd"),
    (re.compile(r"\b(retinitis\s*pigmentosa|stargardt|usher|leber\s*congenital|geographic\s*atrophy)", re.I), "ophthalmology_rare"),
    (re.compile(r"\b(hypothyroid|hyperthyroid|thyroid\s*eye|graves)", re.I), "endocrinology_thyroid"),
    (re.compile(r"\b(type\s*2\s*diabetes|type\s*ii\s*diabetes|\bt2d\b|diabetes\s*mellitus)", re.I), "metabolic_diabetes"),
    (re.compile(r"\b(obesity|overweight|weight\s*management)", re.I), "metabolic_obesity"),
    (re.compile(r"\b(atrial\s*fibrillation|heart\s*failure|coronary|hypertension|dyslipidem|cardiovascular)", re.I), "cardiovascular"),
    (re.compile(r"\b(sickle\s*cell)", re.I), "hematology_sickle_cell"),
    (re.compile(r"\b(hemophilia|thrombotic\s*thrombocytopenic|aplastic\s*anemia|paroxysmal\s*nocturnal|beta\s*thalassemia)", re.I), "hematology_rare"),
    (re.compile(r"\b(acute\s*pain|post[-\s]operative\s*pain|dental\s*pain)", re.I), "pain_acute"),
    (re.compile(r"\b(chronic\s*pain|neuropathic\s*pain|fibromyalgia)", re.I), "pain_chronic"),
    (re.compile(r"\b(lupus|systemic\s*lupus|vasculitis|myasthenia|autoimmune)", re.I), "autoimmune"),
    (re.compile(r"\b(covid|influenza|rsv|hiv|hepatitis\s*c)", re.I), "infectious_antiviral"),
    (re.compile(r"\b(mrsa|staphylococc|pseudomonas|antibiotic|bacterial\s*infection|sepsis|urinary\s*tract\s*infection|uti\b)", re.I), "infectious_antibacterial"),
    (re.compile(r"\b(vaccine|immunization)", re.I), "infectious_vaccine"),
    (re.compile(r"\b(multiple\s*myeloma|lymphoma|leukemia|\baml\b|\ball\b(?!\w)|mantle\s*cell|cll\b|mds\b|myelodysplastic)", re.I), "oncology_hematologic"),
    (re.compile(r"\b(glioblastoma|glioma|neuroblastoma|sarcoma\b(?!\w)|ewing\s*sarcoma|rhabdomyosarcoma|chordoma)", re.I), "oncology_rare"),
    (re.compile(r"\b(lung\s*cancer|breast\s*cancer|prostate\s*cancer|colorectal\s*cancer|melanoma|pancreatic\s*cancer|gastric\s*cancer|hepatocellular|renal\s*cell|ovarian|bladder\s*cancer|\bnsclc\b|sclc\b|tnbc\b|hcc\b)", re.I), "oncology_solid_tumor"),
    (re.compile(r"\b(gaucher|pompe|mucopolysaccharidosis|\bmps\b|phenylketonuria|pku\b|homocystinuria|batten|niemann)", re.I), "rare_disease_metabolic"),
    (re.compile(r"\b(duchenne|becker\s*muscular|spinal\s*muscular|sma\b|cystic\s*fibrosis|huntington|angelman|rett)", re.I), "rare_disease_genetic"),
]

MECHANISM_RULES: List[Tuple[re.Pattern, str]] = [
    (re.compile(r"\b(monoclonal|mab\b|antibody[-\s]drug\s*conjugate|adc\b)", re.I), "mAb / ADC"),
    (re.compile(r"\b(crispr|gene\s*therapy|aav\b|cas9|base[-\s]editing|prime[-\s]editing)", re.I), "gene therapy / editing"),
    (re.compile(r"\b(antisense|sirna\b|rna\s*interference|oligo)", re.I), "RNA therapeutic"),
    (re.compile(r"\b(mrna|messenger\s*rna)", re.I), "mRNA"),
    (re.compile(r"\b(car[-\s]?t|car\s*nk|bispecific|\btce\b|t[-\s]cell\s*engager)", re.I), "cell therapy / bispecific"),
    (re.compile(r"\b(kinase\s*inhibitor|tyrosine\s*kinase|\btki\b|btk\s*inhibitor)", re.I), "kinase inhibitor"),
    (re.compile(r"\b(checkpoint\s*inhibitor|pd[-\s]?1|pd[-\s]?l1|ctla[-\s]?4)", re.I), "immune checkpoint"),
    (re.compile(r"\b(glp[-\s]?1|gipr?\b|incretin)", re.I), "GLP-1 / incretin"),
    (re.compile(r"\b(vaccine|immunization)", re.I), "vaccine"),
    (re.compile(r"\b(small\s*molecule|oral\s*small[-\s]?molecule)", re.I), "small molecule"),
]


def _resolve_indication(conditions: List[str], trial_title: str, scanner_key: Optional[str]) -> Tuple[str, bool]:
    text = " ".join([*(conditions or []), trial_title or ""])
    for pattern, key in INDICATION_RULES:
        if pattern.search(text):
            if scanner_key and scanner_key != "default" and scanner_key == key:
                return scanner_key, False
            if scanner_key == "default":
                return key, True
            return scanner_key or key, False
    return scanner_key or "default", False


def _endpoint_strength(signal: dict) -> Tuple[int, str]:
    raw = signal.get("raw_data") or {}
    primaries = raw.get("primary_outcomes") or []
    patterns = set(raw.get("pattern_names") or [])
    enrollment = int(raw.get("enrollment") or 0)
    text = " ".join(str(item) for item in primaries)
    n_primary = len(primaries)
    has_hard = bool(HARD_ENDPOINT_RE.search(text))
    has_surrogate = bool(SURROGATE_ENDPOINT_RE.search(text))
    only_soft = bool(SOFT_ENDPOINT_RE.search(text)) and not (has_hard or has_surrogate)
    has_single = "single_primary_endpoint" in patterns

    tier = 3
    if only_soft or not primaries:
        tier = 2
    if n_primary >= 4:
        tier = max(1, tier - 1)
    if has_hard and has_single:
        tier = 5
    elif has_hard:
        tier = max(tier, 4)
    elif has_single and enrollment >= 300:
        tier = max(tier, 4)
    elif has_single:
        tier = max(tier, 3)
    if enrollment and enrollment < 50:
        tier = min(tier, 2)
    tier = max(1, min(5, tier))

    reasons = []
    if has_hard:
        reasons.append("hard endpoint")
    if has_single:
        reasons.append("single primary")
    if only_soft:
        reasons.append("safety-only outcomes")
    if n_primary >= 4:
        reasons.append(f"{n_primary} co-primaries")
    if enrollment:
        reasons.append(f"n={enrollment}")
    return tier, ", ".join(reasons) or "baseline"


def _sponsor_tier(signal: dict) -> Tuple[int, str, str]:
    raw = signal.get("raw_data") or {}
    sponsor_class = (raw.get("sponsor_class") or "").upper()
    sponsor_text = raw.get("sponsor_name") or raw.get("sponsor") or signal.get("company_name_en") or ""
    text_blob = " ".join(filter(None, [sponsor_text, signal.get("headline") or "", signal.get("summary") or ""]))
    enrollment = int(raw.get("enrollment") or 0)

    tier = 3 if sponsor_class == "INDUSTRY" else 2
    if BIG_PHARMA_RE.search(text_blob):
        tier = 5
    elif MID_CAP_BIOTECH_RE.search(text_blob):
        tier = 4
    elif sponsor_class == "INDUSTRY" and enrollment >= 300:
        tier = 4
    elif sponsor_class in ("NIH", "FED"):
        tier = 2
    elif sponsor_class in ("", "OTHER", "INDIVIDUAL"):
        tier = min(tier, 2) if sponsor_class else 1
    tier = max(1, min(5, tier))
    return tier, SPONSOR_LABELS[tier], sponsor_text or "(unknown)"


def _mechanism_class(signal: dict) -> str:
    raw = signal.get("raw_data") or {}
    text = " ".join(
        filter(
            None,
            [
                raw.get("trial_title") or "",
                signal.get("headline") or "",
                signal.get("summary") or "",
                " ".join(raw.get("conditions") or []),
            ],
        )
    )
    for pattern, label in MECHANISM_RULES:
        if pattern.search(text):
            return label
    return "unclassified"


def _composite_score(endpoint_tier: int, sponsor_tier: int, indication_resolved: bool, pubmed_count: Optional[int], preprint_count: Optional[int]) -> int:
    ep = (endpoint_tier - 1) / 4.0
    sp = (sponsor_tier - 1) / 4.0
    ind = 1.0 if indication_resolved else 0.0

    def _bucket(count: Optional[int], low: int, high: int) -> float:
        if count is None:
            return 0.5
        if count <= low:
            return 0.0
        if count >= high:
            return 1.0
        return (count - low) / float(high - low)

    literature = _bucket(pubmed_count, 5, 100)
    preprints = _bucket(preprint_count, 1, 20)
    weighted = ep * 0.40 + sp * 0.25 + ind * 0.10 + literature * 0.15 + preprints * 0.10
    return int(round(weighted * 100))


def _color_for_enrichment(score: int) -> str:
    if score >= 75:
        return "GREEN"
    if score >= 55:
        return "YELLOW"
    if score >= 35:
        return "ORANGE"
    return "RED"


def _load_cache() -> dict:
    try:
        return json.loads(CACHE_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _save_cache(cache: dict) -> None:
    try:
        _atomic_write(CACHE_PATH, json.dumps(cache, indent=2, ensure_ascii=False))
    except Exception:
        pass


def _cache_get(cache: dict, key: str) -> Optional[Any]:
    entry = cache.get(key)
    if not entry:
        return None
    try:
        ts = datetime.fromisoformat(entry["ts"])
        if datetime.now(timezone.utc) - ts > timedelta(days=CACHE_TTL_DAYS):
            return None
        return entry["value"]
    except Exception:
        return None


def _cache_put(cache: dict, key: str, value: Any) -> None:
    cache[key] = {"ts": datetime.now(timezone.utc).isoformat(), "value": value}


def _http_get_json(url: str) -> Optional[dict]:
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=HTTP_TIMEOUT_S) as response:
            return json.loads(response.read().decode("utf-8", errors="replace"))
    except Exception:
        return None


def pubmed_count(query: str, cache: Optional[dict] = None) -> Optional[int]:
    if not query.strip():
        return None
    key = f"pubmed::{query}"
    if cache is not None:
        cached = _cache_get(cache, key)
        if cached is not None:
            return int(cached)
    url = f"https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esearch.fcgi?db=pubmed&term={urllib.parse.quote(query)}&retmode=json&rettype=count"
    payload = _http_get_json(url)
    if not payload:
        return None
    try:
        count = int(payload.get("esearchresult", {}).get("count", 0))
    except Exception:
        return None
    if cache is not None:
        _cache_put(cache, key, count)
    return count


def biorxiv_count(condition: str, cache: Optional[dict] = None) -> Optional[int]:
    if not condition.strip():
        return None
    key = f"biorxiv::{condition}"
    if cache is not None:
        cached = _cache_get(cache, key)
        if cached is not None:
            return int(cached)
    since = (datetime.now(timezone.utc) - timedelta(days=90)).date().isoformat()
    until = datetime.now(timezone.utc).date().isoformat()
    payload = _http_get_json(f"https://api.biorxiv.org/details/biorxiv/{since}/{until}/0")
    if not payload:
        return None
    needle = condition.lower().split()[0]
    count = sum(1 for item in payload.get("collection", []) if needle in (item.get("title") or "").lower())
    if cache is not None:
        _cache_put(cache, key, count)
    return count


def sponsor_trial_count(sponsor: str, cache: Optional[dict] = None) -> Optional[int]:
    if not sponsor.strip():
        return None
    key = f"ctgov::{sponsor}"
    if cache is not None:
        cached = _cache_get(cache, key)
        if cached is not None:
            return int(cached)
    url = f"https://clinicaltrials.gov/api/v2/studies?query.lead={urllib.parse.quote(sponsor)}&countTotal=true&pageSize=1"
    payload = _http_get_json(url)
    if not payload:
        return None
    try:
        count = int(payload.get("totalCount") or 0)
    except Exception:
        return None
    if cache is not None:
        _cache_put(cache, key, count)
    return count


def _is_biotech_signal(signal: dict) -> bool:
    return signal.get("scoring_profile") == "binary_catalyst" and (signal.get("upstream_scanner") or signal.get("scanner_source") or "") in BIOTECH_SCANNERS


def enrich_biotech_signal(signal: dict, *, online: bool = False, cache: Optional[dict] = None) -> dict:
    raw = signal.get("raw_data") or {}
    conditions = raw.get("conditions") or []
    trial_title = raw.get("trial_title") or ""
    scanner_base_rate = raw.get("base_rate_key")

    endpoint_tier, endpoint_reason = _endpoint_strength(signal)
    sponsor_tier, sponsor_label, sponsor_text = _sponsor_tier(signal)
    mechanism = _mechanism_class(signal)
    resolved_indication, resolved_by_enricher = _resolve_indication(conditions, trial_title, scanner_base_rate)

    pubmed_hits: Optional[int] = None
    preprint_hits: Optional[int] = None
    sponsor_trials: Optional[int] = None
    online_status = "offline"
    if online:
        online_status = "online"
        condition_query = conditions[0] if conditions else trial_title
        if condition_query:
            pubmed_hits = pubmed_count(f"{condition_query} AND phase III[Publication Type]", cache=cache)
            preprint_hits = biorxiv_count(condition_query, cache=cache)
        if sponsor_text and sponsor_text != "(unknown)":
            sponsor_trials = sponsor_trial_count(sponsor_text, cache=cache)

    score = _composite_score(endpoint_tier, sponsor_tier, resolved_by_enricher or (resolved_indication != "default"), pubmed_hits, preprint_hits)
    color = _color_for_enrichment(score)
    explanation = (
        f"Endpoint {endpoint_tier} ({ENDPOINT_LABELS[endpoint_tier]}; {endpoint_reason}); "
        f"Sponsor {sponsor_tier} ({sponsor_label}); "
        f"Mechanism: {mechanism}; "
        f"Indication: {resolved_indication}"
        + (" (enricher)" if resolved_by_enricher else "")
        + (f"; PubMed n={pubmed_hits}" if pubmed_hits is not None else "")
        + (f"; bioRxiv n={preprint_hits}" if preprint_hits is not None else "")
        + (f"; sponsor trials n={sponsor_trials}" if sponsor_trials is not None else "")
        + f" → score {score} → {color}."
    )

    patch = {
        "endpoint_strength_tier": endpoint_tier,
        "endpoint_strength_label": ENDPOINT_LABELS[endpoint_tier],
        "endpoint_reason": endpoint_reason,
        "sponsor_track_record_tier": sponsor_tier,
        "sponsor_track_record_label": sponsor_label,
        "sponsor_text": sponsor_text,
        "mechanism_class": mechanism,
        "indication_resolved": resolved_indication,
        "indication_resolved_by_enricher": resolved_by_enricher,
        "pubmed_count": pubmed_hits,
        "biorxiv_count": preprint_hits,
        "sponsor_trial_count": sponsor_trials,
        "online_mode": online_status,
        "enrichment_score": score,
        "enrichment_color": color,
        "explanation": explanation,
        "enriched_at": datetime.now(timezone.utc).isoformat(),
        "framework": "biotech_enricher v1 (endpoint×sponsor×indication[+literature])",
    }
    signal["biotech_enrichment"] = patch
    return patch


def _atomic_write(path: Path, text: str) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    try:
        with open(tmp, "rb") as handle:
            os.fsync(handle.fileno())
    except Exception:
        pass
    os.replace(str(tmp), str(path))


def enrich_signal_log(path: Optional[Path] = None, *, online: bool = False) -> Dict[str, Any]:
    path = path or SIGNAL_LOG
    if not path.exists():
        return {"status": "no_log", "enriched": 0}
    data = json.loads(path.read_text(encoding="utf-8"))
    biotech_signals = [signal for signal in data if _is_biotech_signal(signal)]
    cache = _load_cache() if online else None
    enriched = 0
    color_counts = {"GREEN": 0, "YELLOW": 0, "ORANGE": 0, "RED": 0}
    tier_histogram = {"endpoint": {i: 0 for i in range(1, 6)}, "sponsor": {i: 0 for i in range(1, 6)}}
    for signal in biotech_signals:
        patch = enrich_biotech_signal(signal, online=online, cache=cache)
        enriched += 1
        color_counts[patch["enrichment_color"]] += 1
        tier_histogram["endpoint"][patch["endpoint_strength_tier"]] += 1
        tier_histogram["sponsor"][patch["sponsor_track_record_tier"]] += 1
    if cache is not None:
        _save_cache(cache)
    _atomic_write(path, json.dumps(data, indent=2, default=str, ensure_ascii=False))
    today = datetime.now(timezone.utc).date().isoformat()
    report = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "framework": "biotech_enricher v1",
        "online": online,
        "total_biotech_signals": len(biotech_signals),
        "enriched": enriched,
        "by_color": color_counts,
        "tier_histogram": tier_histogram,
        "top_green_signals": [
            {
                "signal_id": signal.get("signal_id"),
                "ticker": signal.get("ticker"),
                "nct_id": (signal.get("raw_data") or {}).get("nct_id"),
                "sponsor": signal["biotech_enrichment"]["sponsor_text"],
                "indication": signal["biotech_enrichment"]["indication_resolved"],
                "score": signal["biotech_enrichment"]["enrichment_score"],
                "color": signal["biotech_enrichment"]["enrichment_color"],
            }
            for signal in sorted(biotech_signals, key=lambda item: item["biotech_enrichment"]["enrichment_score"], reverse=True)[:10]
        ],
    }
    out = WORKING / f"biotech_enrichment_report_{today}.json"
    _atomic_write(out, json.dumps(report, indent=2, ensure_ascii=False))
    report["report_path"] = str(out)
    report["status"] = "ok"
    return report


def summarize_biotech_desk(window_days: int = 7, max_items: int = 12) -> List[Dict[str, Any]]:
    if not SIGNAL_LOG.exists():
        return []
    data = json.loads(SIGNAL_LOG.read_text(encoding="utf-8"))
    cutoff = datetime.now(timezone.utc) - timedelta(days=window_days)
    items = []
    for signal in data:
        if not _is_biotech_signal(signal):
            continue
        if "biotech_enrichment" not in signal:
            enrich_biotech_signal(signal, online=False)
        source_date = signal.get("source_date") or signal.get("scan_date")
        try:
            ts = datetime.fromisoformat(str(source_date).replace("Z", "+00:00"))
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=timezone.utc)
            if ts < cutoff:
                continue
        except Exception:
            continue
        enrichment = signal["biotech_enrichment"]
        raw = signal.get("raw_data") or {}
        items.append(
            {
                "signal_id": signal.get("signal_id"),
                "ticker": signal.get("ticker"),
                "company": signal.get("company_name_en"),
                "headline": (signal.get("headline") or "")[:200],
                "nct_id": raw.get("nct_id"),
                "sponsor": enrichment["sponsor_text"],
                "indication": enrichment["indication_resolved"],
                "mechanism": enrichment["mechanism_class"],
                "endpoint_strength": enrichment["endpoint_strength_label"],
                "sponsor_tier": enrichment["sponsor_track_record_label"],
                "days_until_readout": raw.get("days_until_readout"),
                "primary_completion_date": raw.get("primary_completion_date"),
                "approval_probability": raw.get("approval_probability"),
                "upside_pct": raw.get("upside_pct"),
                "downside_pct": raw.get("downside_pct"),
                "enrichment_score": enrichment["enrichment_score"],
                "enrichment_color": enrichment["enrichment_color"],
                "pubmed_count": enrichment.get("pubmed_count"),
                "source_url": raw.get("source_url"),
                "explanation": enrichment["explanation"],
            }
        )
    items.sort(key=lambda item: item["enrichment_score"], reverse=True)
    return items[:max_items]


def refresh_base_rates(online: bool = False) -> Dict[str, Any]:
    return {"status": "not_implemented", "note": "Follow-up; see D-030."}


def _cli() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--desk", action="store_true", help="Print biotech-desk JSON for the given window.")
    parser.add_argument("--window-days", type=int, default=7, help="Window for --desk (default 7).")
    parser.add_argument("--signal-id", default=None, help="Enrich only the matching signal_id and print result.")
    parser.add_argument("--online", action="store_true", help="Enable PubMed/bioRxiv/ClinicalTrials.gov lookups.")
    parser.add_argument("--refresh-base-rates", action="store_true", help="Refresh phase3_approval_base_rates.json (stub for now).")
    args = parser.parse_args()
    if args.refresh_base_rates:
        print(json.dumps(refresh_base_rates(online=args.online), indent=2, ensure_ascii=False))
        return
    if args.signal_id:
        data = json.loads(SIGNAL_LOG.read_text(encoding="utf-8"))
        signal = next((item for item in data if item.get("signal_id") == args.signal_id), None)
        if not signal:
            print(json.dumps({"error": "signal_id not found"}))
            return
        cache = _load_cache() if args.online else None
        patch = enrich_biotech_signal(signal, online=args.online, cache=cache)
        if cache is not None:
            _save_cache(cache)
        print(json.dumps(patch, indent=2, ensure_ascii=False))
        return
    if args.desk:
        print(json.dumps(summarize_biotech_desk(window_days=args.window_days), indent=2, ensure_ascii=False))
        return
    report = enrich_signal_log(online=args.online)
    print(json.dumps({key: value for key, value in report.items() if key != "top_green_signals"}, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    _cli()
