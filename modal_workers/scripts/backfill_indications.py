"""Backfill fda_assets.indication + indication_normalized via openFDA /drug/label.

Phase 0 close-out — D3. The openFDA-driven curation script left every fda_assets
row's indication NULL because /drug/drugsfda doesn't return indication text.
This pass calls /drug/label per asset using the application_number, extracts the
indications_and_usage section, and normalizes to a small therapeutic-area
taxonomy (oncology / autoimmune / CNS / cardio / metabolic / rare-disease /
infectious / dermatology / ophthalmology / other).

Run:
  python3 -m modal_workers.scripts.backfill_indications [--limit N] [--dry-run]
"""

from __future__ import annotations

import argparse
import logging
import re
import sys
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

import requests

from modal_workers.shared.supabase_client import SupabaseClient

logger = logging.getLogger(__name__)

OPENFDA_BASE = "https://api.fda.gov"
LABEL_TIMEOUT_S = 20.0


# Therapeutic-area dispatch table. Order matters — first hit wins.
# Patterns are case-insensitive substring matches in the indication text.
_NORMALIZATION_RULES: List[Tuple[str, List[str]]] = [
    ("oncology", [
        "cancer", "carcinoma", "melanoma", "leukemia", "leukaemia", "lymphoma",
        "myeloma", "sarcoma", "tumor", "tumour", "neoplasm", "metastatic",
        "metastasis", "oncology", "malignant", "glioma", "blastoma",
        "myelodysplastic", "myeloproliferative",
    ]),
    ("rare_disease", [
        "rare disease", "ultra-rare", "orphan", "huntington", "duchenne",
        "spinal muscular atrophy", "cystic fibrosis", "phenylketonuria",
        "gaucher", "fabry", "pompe", "mucopolysaccharidosis", "amyloidosis",
        "wilson", "porphyria", "thalassemia", "sickle cell",
    ]),
    ("autoimmune", [
        "rheumatoid", "psoriasis", "psoriatic", "lupus", "crohn",
        "ulcerative colitis", "inflammatory bowel", "multiple sclerosis",
        "myasthenia", "atopic dermatitis", "vitiligo", "alopecia areata",
        "ankylosing spondylitis", "autoimmune", "type 1 diabetes",
    ]),
    ("cns", [
        "alzheimer", "parkinson", "epilepsy", "schizophrenia", "depression",
        "bipolar", "anxiety", "insomnia", "migraine", "narcolepsy",
        "amyotrophic lateral", "als ", "dementia", "neuropathic pain",
        "post-traumatic", "ptsd", "adhd", "attention deficit", "autism",
        "tourette", "essential tremor",
    ]),
    ("cardiovascular", [
        "heart failure", "hypertension", "cardiac", "cardiovascular",
        "myocardial", "coronary", "atrial fibrillation", "stroke",
        "thromboembolism", "thrombosis", "deep vein", "pulmonary embolism",
        "hyperlipidemia", "hypercholesterolemia", "atherosclerosis",
    ]),
    ("metabolic", [
        "type 2 diabetes", "diabetes mellitus", "obesity", "weight",
        "hyperglycemia", "metabolic syndrome", "nash ",
        "non-alcoholic steatohepatitis", "fatty liver",
        "hyperphenylalaninemia", "hyperuricemia", "gout",
        "primary biliary cholangitis", "pbc",
    ]),
    ("infectious", [
        "hiv", "hepatitis", "covid", "sars-cov", "influenza", "tuberculosis",
        "malaria", "bacterial infection", "viral infection", "fungal infection",
        "antibiotic", "antiviral", "antifungal", "antimicrobial",
        "clostridioides", "clostridium difficile", "rsv ",
        "respiratory syncytial",
    ]),
    ("dermatology", [
        "acne", "rosacea", "eczema", "actinic keratosis",
        "hidradenitis suppurativa", "skin", "dermatitis",
    ]),
    ("ophthalmology", [
        "macular degeneration", "diabetic retinopathy", "glaucoma",
        "uveitis", "dry eye", "myopia", "amblyopia", "blepharitis",
        "ocular", "ophthalmic", "retinal", "retina", "iritis",
        "conjunctivitis", "keratitis",
    ]),
    ("respiratory", [
        "asthma", "copd", "chronic obstructive pulmonary",
        "pulmonary arterial hypertension", "pulmonary fibrosis",
        "idiopathic pulmonary",
    ]),
    ("hematology", [
        "anemia", "hemophilia", "thrombocytopenia", "neutropenia",
        "von willebrand", "coagulation disorder",
    ]),
    ("renal", [
        "chronic kidney", "ckd", "end-stage renal", "esrd",
        "renal failure", "hyperkalemia", "iga nephropathy",
    ]),
    ("women_health", [
        "menopause", "endometriosis", "uterine", "postpartum",
        "fibroid", "preterm",
    ]),
]


@dataclass
class Stats:
    assets_seen: int = 0
    label_fetched: int = 0
    label_missing: int = 0
    indication_extracted: int = 0
    indication_updated: int = 0
    errors: int = 0


def fetch_label_for_application(application_number: str) -> Optional[Dict[str, Any]]:
    """Pull the most recent /drug/label record for an NDA/BLA application."""
    if not application_number or not application_number.isdigit():
        # Synthetic application numbers from the 8-K-derived path won't resolve.
        return None
    # openFDA stores application_number as e.g. "NDA213947" or "BLA761234".
    # Try both prefixes.
    for prefix in ("NDA", "BLA", "ANDA"):
        search = f'openfda.application_number:"{prefix}{application_number}"'
        try:
            r = requests.get(
                f"{OPENFDA_BASE}/drug/label.json",
                params={"search": search, "limit": 1},
                timeout=LABEL_TIMEOUT_S,
            )
        except requests.exceptions.RequestException as exc:
            logger.warning("openFDA label fetch failed for %s%s: %s",
                           prefix, application_number, exc)
            continue
        if r.status_code == 404:
            continue
        if r.status_code != 200:
            logger.warning("openFDA label %s%s non-200: %d",
                           prefix, application_number, r.status_code)
            continue
        body = r.json() or {}
        results = body.get("results") or []
        if results:
            return results[0]
    return None


def extract_indication_text(label: Dict[str, Any]) -> Optional[str]:
    """Pull the first paragraph of indications_and_usage from a label record."""
    raw = label.get("indications_and_usage")
    if not raw:
        return None
    if isinstance(raw, list):
        # openFDA returns this as ["...full text..."] usually with one element.
        text = "\n\n".join(str(x) for x in raw if x)
    else:
        text = str(raw)
    text = re.sub(r"\s+", " ", text).strip()
    if not text:
        return None
    # Trim to first ~600 chars or first complete sentence boundary.
    if len(text) > 600:
        cut = text[:600]
        last_period = cut.rfind(". ")
        if last_period > 200:
            text = cut[: last_period + 1]
        else:
            text = cut + "…"
    return text


def normalize_indication(text: str) -> str:
    """Map free-text indication to the small therapeutic-area taxonomy."""
    if not text:
        return "other"
    lower = text.lower()
    for area, patterns in _NORMALIZATION_RULES:
        for p in patterns:
            if p in lower:
                return area
    return "other"


def update_asset_indication(
    asset_id: str,
    indication: str,
    indication_normalized: str,
    client: SupabaseClient,
) -> bool:
    try:
        client._rest(
            "PATCH", "fda_assets",
            params={"id": f"eq.{asset_id}"},
            json_body={
                "indication": indication,
                "indication_normalized": indication_normalized,
            },
            prefer="return=minimal",
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("PATCH fda_assets failed for %s: %s", asset_id, exc)
        return False
    return True


def main(argv: Optional[List[str]] = None) -> int:
    p = argparse.ArgumentParser(prog="backfill_indications")
    p.add_argument("--limit", type=int, default=200,
                   help="Max assets to backfill in one run")
    p.add_argument("--dry-run", action="store_true",
                   help="Fetch labels and print proposed updates without writing")
    args = p.parse_args(argv)

    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")
    client = SupabaseClient()
    stats = Stats()

    # Pull fda_assets rows linked to eval_harness with NULL indication.
    rows = client._rest(
        "GET", "fda_assets",
        params={
            "select": "id,ticker,drug_name,application_number,indication,indication_normalized",
            "or": "(indication.is.null,indication_normalized.is.null)",
            "limit": str(args.limit),
        },
    ) or []
    stats.assets_seen = len(rows)
    logger.info("Found %d fda_assets rows missing indication", len(rows))

    for row in rows:
        asset_id = row["id"]
        appl = (row.get("application_number") or "").strip()
        # Synthetic 8K_DERIVED_* application numbers won't resolve; leave NULL.
        if not appl or not appl.isdigit():
            continue

        label = fetch_label_for_application(appl)
        if not label:
            stats.label_missing += 1
            continue
        stats.label_fetched += 1

        indication_text = extract_indication_text(label)
        if not indication_text:
            stats.label_missing += 1
            continue
        normalized = normalize_indication(indication_text)
        stats.indication_extracted += 1

        if args.dry_run:
            logger.info(
                "[dry-run] %s/%s appl=%s -> '%s' (%s)",
                row.get("ticker"), row.get("drug_name"), appl,
                indication_text[:80], normalized,
            )
            continue

        if update_asset_indication(asset_id, indication_text, normalized, client):
            stats.indication_updated += 1
        else:
            stats.errors += 1

    logger.info(
        "Indication backfill summary: assets=%d label_fetched=%d label_missing=%d "
        "extracted=%d updated=%d errors=%d",
        stats.assets_seen, stats.label_fetched, stats.label_missing,
        stats.indication_extracted, stats.indication_updated, stats.errors,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
