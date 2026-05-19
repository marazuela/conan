"""
Shared phase-3 → approval base-rate table loader and indication mapper.

Lifted from `pre_phase3_readout_scanner.py` so both biotech scanners
(`pre_phase3_readout_scanner` and `fda_pdufa_pipeline`) share one source
of truth for indication regex patterns and the Supabase `phase3_base_rates`
table reader.

The table is seeded by `migrations/seed_registry.py` from
`data/legacy/phase3_approval_base_rates.json` and includes a `default` row at
0.58 (used as fallback when no indication regex matches).
"""

from __future__ import annotations

import re
from typing import Dict, List, Optional, Tuple

from modal_workers.shared.supabase_client import SupabaseClient

# Default phase3->approval rate if no indication matches and the table has
# no 'default' row.
DEFAULT_APPROVAL_PROB = 0.58

INDICATION_MAP: List[Tuple[str, str]] = [
    # (regex pattern, base-rate key)
    (r"alzheimer", "neurology_alzheimers"),
    (r"amyotrophic lateral|ALS\b", "neurology_als"),
    (r"parkinson", "neurology_parkinsons"),
    (r"migraine", "neurology_migraine"),
    (r"epilepsy|seizure", "neurology_epilepsy"),
    (r"depress|MDD", "psychiatry_depression"),
    (r"schizophrenia", "psychiatry_schizophrenia"),
    (r"agitation", "psychiatry_agitation"),
    (r"hepatitis", "hepatology_hepb"),
    (r"NASH|steatohepatit", "gastro_nash"),
    (r"crohn|ulcerative colitis|IBD|inflammatory bowel", "gastro_ibd"),
    (r"psoriasis", "dermatology_psoriasis"),
    (r"atopic dermatitis|eczema", "dermatology_atopic_dermatitis"),
    (r"rheumatoid arthritis|RA\b", "rheumatology_ra"),
    (r"thyroid eye|graves ophthalmopath", "ophthalmology_rare"),
    (r"wet AMD|macular degeneration", "ophthalmology_wet_amd"),
    (r"IgA nephropathy|IgAN", "nephrology_rare"),
    (r"FSGS|focal segmental", "nephrology_rare"),
    (r"chronic kidney disease|CKD\b", "nephrology_ckd"),
    (r"sickle cell", "hematology_sickle_cell"),
    (r"obesity|weight management", "metabolic_obesity"),
    (r"diabetes|type 2 DM|T2DM", "metabolic_diabetes"),
    (r"hypertension|heart failure|atrial fib|MACE|cardiovascular", "cardiovascular"),
    (r"COPD|chronic obstructive", "respiratory_copd"),
    (r"asthma", "respiratory_asthma"),
    (r"pulmonary fibrosis|IPF", "respiratory_ipf"),
    (r"pain", "pain_chronic"),
    (r"lymphoma|leukemia|myeloma|myelofibrosis", "oncology_hematologic"),
    (r"carcinoma|tumor|tumour|melanoma|cancer", "oncology_solid_tumor"),
    (r"rare disease|orphan", "rare_disease_genetic"),
    (r"autoimmune|lupus|SLE", "autoimmune"),
    (r"influenza|coronavirus|covid|RSV|HIV|hepatitis C", "infectious_antiviral"),
    (r"bacterial infection|sepsis|pneumonia", "infectious_antibacterial"),
    (r"vaccine", "infectious_vaccine"),
]


_BASE_RATES_CACHE: Optional[Dict[str, float]] = None


def load_base_rates(client: SupabaseClient) -> Dict[str, float]:
    """Fetch {indication: phase3_to_approval} from Supabase. Cached per-process."""
    global _BASE_RATES_CACHE
    if _BASE_RATES_CACHE is not None:
        return _BASE_RATES_CACHE
    try:
        rows = client._rest("GET", "phase3_base_rates", params={"select": "*"})
    except Exception:
        _BASE_RATES_CACHE = {}
        return _BASE_RATES_CACHE
    base_rates: Dict[str, float] = {}
    for r in rows or []:
        key = r.get("indication")
        val = r.get("phase3_to_approval")
        if key and val is not None:
            try:
                base_rates[key] = float(val)
            except (TypeError, ValueError):
                continue
    _BASE_RATES_CACHE = base_rates
    return base_rates


def reset_cache() -> None:
    """Test hook: clear the per-process base-rates cache."""
    global _BASE_RATES_CACHE
    _BASE_RATES_CACHE = None


def map_conditions_to_base_key(conditions: List[str]) -> Tuple[str, List[str]]:
    """Return (base_rate_key, matched_patterns) for the best condition match."""
    joined = " ; ".join(conditions).lower()
    matched: List[str] = []
    key = "default"
    for pattern, k in INDICATION_MAP:
        if re.search(pattern, joined, re.IGNORECASE):
            if key == "default":
                key = k
            matched.append(pattern)
    return key, matched
