"""
Rubric engine — source of truth for live Conan v2 scoring logic.

Preservation covenant (PRD §6, spec.md §2):
  - WEIGHTS dict is byte-for-byte identical to the preserved v1 rubric weights.
  - Live v2 band thresholds remain 35 / 25 / 15. The separate "Scoring engine"
    folder contains a later D-034 experiment that shifted a legacy file-bus copy
    to 30 / 20 / 10, but that change is intentionally NOT authoritative for the
    Modal + Supabase runtime unless a new rubric version explicitly adopts it.
  - apply_auto_caps rule IDs unchanged — each cap returns a stable rule_id string that
    is persisted to signals.auto_caps_triggered (text[]) per spec.md §3.4.
  - score_signal flow identical: profile lookup, clamp dims to [1,5], weighted_total,
    classify_band, apply_auto_caps.

The reactor edge function calls apply_auto_caps via a thin Modal web endpoint exposed
in modal_workers/app.py::rubric_apply_caps; Modal scanners call score_signal
directly as a Python import.

Any change to WEIGHTS or auto-caps MUST:
  1. Introduce a new rubric_version in the rubrics table (do NOT mutate version 1).
  2. Add a new rule_id for the cap (do NOT rename existing rule_ids).
  3. Be reflected in spec.md §12 under "Additional surfaced conflicts".
  4. Update RUBRIC_VERSION in this module so signals.rubric_version_id is pinned
     to the exact DB row whose weights/caps this code implements.
"""

from __future__ import annotations

from typing import Dict, List, Tuple, Any, Optional


# --------------------------------------------------------------------
# Profile weight tables
# --------------------------------------------------------------------

RUBRIC_VERSION = 1

WEIGHTS: Dict[str, Dict[str, float]] = {
    "merger_arb": {
        "spread_size": 3.0,
        "deal_certainty": 2.5,
        "annualized_return": 2.0,
        "break_risk": 1.5,
        "liquidity": 1.0,
    },
    "activist_governance": {
        "signal_strength": 2.0,
        "information_asymmetry": 2.0,
        "activist_track_record": 1.5,
        "risk_reward": 1.5,
        "catalyst_clarity": 1.0,
        "edge_decay": 1.0,
        "liquidity": 1.0,
    },
    "binary_catalyst": {
        "approval_probability": 2.5,
        "market_mispricing": 2.5,
        "magnitude": 1.5,
        "competitive_landscape": 1.5,
        "catalyst_timeline": 1.0,
        "liquidity": 1.0,
    },
    "short_positioning": {
        "crowding_intensity": 2.5,
        "trend_direction": 2.0,
        "catalyst_proximity": 2.0,
        "size_vs_float": 1.5,
        "historical_analog": 1.0,
        "liquidity": 1.0,
    },
    "litigation": {
        "financial_materiality": 3.0,
        "legal_outcome_probability": 2.0,
        "market_pricing": 2.0,
        "resolution_timeline": 1.5,
        "liquidity": 1.0,
        "party_resolution_confidence": 0.5,
    },
    "takeover_candidate": {
        "setup_strength": 3.0,
        "edge_freshness": 2.0,
        "valuation_cushion": 2.0,
        "strategic_buyer_clarity": 2.0,
        "liquidity": 1.0,
    },
}


def weighted_total(dims: Dict[str, int], profile: str) -> float:
    weights = WEIGHTS[profile]
    total = 0.0
    for dim, weight in weights.items():
        raw = dims.get(dim, 0)
        total += raw * weight
    return round(total, 2)


def classify_band(score: float) -> str:
    if score >= 35:
        return "immediate"
    if score >= 25:
        return "watchlist"
    if score >= 15:
        return "archive"
    return "discard"


# Damper applied in score_signal when provenance=='heuristic'. Heuristic dim
# estimates come directly from scanner raw_payload without AI/analyst vetting;
# a borderline-immediate score should not skip the resolver queue. Applied after
# weighted_total, before classify_band.
#
# 0.9 calibrated against the 2026-04-23 ESMA same-day distribution where 14 of
# 17 immediates landed at crowd=5,trend=3,catalyst=3 (score ≈36.5, just over
# the 35 threshold). 0.9 × 36.5 = 32.85 → watchlist. Genuinely-high scores
# (≥39, e.g. trend=5,crowd=5,catalyst=5) remain immediate after damping.
#
# Not applied to provenance in {'scanner','ai_resolved','analyst'} — those
# paths carry explicit per-dim reasoning and shouldn't be penalised.
HEURISTIC_SCORE_MULTIPLIER: float = 0.9


def dimensions_with_provenance(
    dimensions: Dict[str, int],
    provenance: str,
) -> Dict[str, Any]:
    """Persisted JSON shape for `signals.dimensions`.

    Convergence and rubric math consume the pure-int `dimensions` dict. Storage and
    UI surfaces also need to know whether those ints came from a scanner, a
    heuristic estimator, or AI resolution, so we attach `_provenance` only in the
    persisted JSONB copy.
    """
    payload: Dict[str, Any] = dict(dimensions or {})
    payload["_provenance"] = provenance
    return payload


def flatten_persisted_dimensions(dims: Dict[str, Any]) -> Dict[str, int]:
    """Inverse of dimensions_with_provenance.

    signal_resolver persists the nested envelope `{dim:{value,provenance},
    _provenance}` on `signals.dimensions`; `apply_auto_caps` and `score_signal`
    want flat ints. Without this step, a dict slips into `dims.get(dim) < 3`
    and raises TypeError (litigation), or silently returns False against an
    int (merger_arb) — both seen live.

    Bools drop out because `isinstance(True, int)` would otherwise mask them.
    """
    out: Dict[str, int] = {}
    for k, v in (dims or {}).items():
        if k.startswith("_"):
            continue
        if isinstance(v, bool):
            continue
        if isinstance(v, int):
            out[k] = v
        elif isinstance(v, float):
            out[k] = int(v)
        elif isinstance(v, dict):
            inner = v.get("value")
            if isinstance(inner, bool):
                continue
            if isinstance(inner, int):
                out[k] = inner
            elif isinstance(inner, float):
                out[k] = int(inner)
    return out


def build_scoring_meta(
    *,
    provenance: str,
    supported_dims: List[str],
    defaulted_dims: List[str],
    requires_resolution: bool,
    missing_dimensions: List[str] | None = None,
    data_freshness: Dict[str, Any] | None = None,
) -> Dict[str, Any]:
    """Canonical JSON shape for `extensions.scoring_meta`.

    `data_freshness` is an optional per-source staleness block (e.g. market
    snapshot age/liveness) attached by the scanner so reactor + UI can
    distinguish live-data scores from rows scored off stale external data.
    Shape: `{"market_snapshot": {"status": "live"|"stale_served"|"missing",
    "age_seconds": int|None, "source": str}}`.
    """
    meta: Dict[str, Any] = {
        "provenance": provenance,
        "supported_dims": list(supported_dims),
        "defaulted_dims": list(defaulted_dims),
        "requires_resolution": requires_resolution,
    }
    if missing_dimensions:
        meta["missing_dimensions"] = list(missing_dimensions)
    if data_freshness:
        meta["data_freshness"] = dict(data_freshness)
    return meta


VALID_PROVENANCES = frozenset(
    {"heuristic", "scanner", "unscored", "ai_resolved", "analyst"}
)


def validate_scoring_meta(meta: Any) -> List[str]:
    """Return a list of shape errors on the scoring_meta dict (empty when valid).

    Enforces the contract reactor/UI depend on: without this, a future change to
    `build_scoring_meta` could drop a key and reactor `isProvisionalHeuristic`
    would silently misclassify rows. Tests call this with expectation `==[]`;
    `_signal_to_row` logs a warning when this returns non-empty, so dev
    accidents surface in Modal logs without a per-signal DB write.
    """
    errors: List[str] = []
    if not isinstance(meta, dict):
        return ["scoring_meta must be a dict"]

    for required_key in ("provenance", "supported_dims", "defaulted_dims", "requires_resolution"):
        if required_key not in meta:
            errors.append(f"missing required key: {required_key}")

    provenance = meta.get("provenance")
    if provenance is not None and provenance not in VALID_PROVENANCES:
        errors.append(
            f"invalid provenance: {provenance!r} (expected one of {sorted(VALID_PROVENANCES)})"
        )

    supported = meta.get("supported_dims")
    if "supported_dims" in meta and not isinstance(supported, list):
        errors.append("supported_dims must be a list")
    defaulted = meta.get("defaulted_dims")
    if "defaulted_dims" in meta and not isinstance(defaulted, list):
        errors.append("defaulted_dims must be a list")

    requires_resolution = meta.get("requires_resolution")
    if "requires_resolution" in meta and not isinstance(requires_resolution, bool):
        errors.append("requires_resolution must be bool")

    if isinstance(supported, list) and isinstance(defaulted, list):
        overlap = sorted(set(supported) & set(defaulted))
        if overlap:
            errors.append(f"supported_dims and defaulted_dims overlap: {overlap}")

    if provenance in {"heuristic", "scanner"}:
        missing = meta.get("missing_dimensions")
        if isinstance(missing, list) and isinstance(defaulted, list):
            stray = sorted(set(missing) - set(defaulted))
            if stray:
                errors.append(
                    f"missing_dimensions contains dims not in defaulted_dims: {stray}"
                )

    return errors


# --------------------------------------------------------------------
# Auto-cap rules
#
# Each branch appends a stable rule_id string to caps and may downgrade band.
# The rule_ids are the contract with the `signals.auto_caps_triggered` column and
# with downstream dashboards / replay tests — do not rename.
#
# Why some profiles have NO auto-caps (activist_governance, short_positioning):
#   Caps exist here only for scoring paths that bypass downstream AI review —
#   scanner-side mechanical fields like takeover_candidate.patterns_hit or
#   binary_catalyst.approval_probability. Profiles without caps rely on the
#   architecture for safety:
#     - activist_governance / merger_arb / litigation emit with
#       `score=NULL, band=NULL` (dim_estimator returns None). The reactor
#       enqueues them onto signal_resolver, which fills dims WITH full
#       raw_data context. If a distress_keyword filing carries a going_concern
#       warning, the AI sees it and prices it into information_asymmetry /
#       risk_reward. A hard cap would double-count the same signal.
#     - short_positioning is heuristically scored by dim_estimator; any
#       Immediate-band signal still passes through thesis_writer before a
#       candidate is promoted, which enforces kill_conditions + steelman.
#   Adding a new cap to a capless profile is a behavior change governed by
#   the preservation covenant in this module's header: new rubric_version,
#   new rule_id, spec.md §12 update.
# --------------------------------------------------------------------

RISK_FREE_RATE = 0.043  # 10Y UST as of 2026-04-16 (carried from v1)
EV_FLOOR = 5.0          # percent (binary_catalyst)

# 2026-04-27: AbbVie / Sanofi / AstraZeneca PDUFA signals were landing in the
# immediate band at 31–36 with `magnitude=1` correctly set by the AI. The
# rubric formula still let approval_probability=5 (×2.5) + catalyst_timeline=5
# (×1.0) dominate, and thesis_writer was killing them downstream as "no
# asymmetry on $130B parent stock." The cap moves them to watchlist before they
# burn AI cycles. A truly binary readout on a megacap (e.g. JNJ Stelara LOE)
# also gets capped — acceptable: the watchlist signal still flows for manual
# promotion.
MEGACAP_ABSORPTION_THRESHOLD_USD = 50_000_000_000


def _coerce_patterns_hit(v: Any) -> int:
    """Normalise takeover_candidate raw_data.patterns_hit to an int.

    Missing, None, booleans, or non-numeric values all collapse to 0, which
    triggers below_triage_gate. This closes two sharp edges:
      - `raw.get("patterns_hit", 0)` default fires on missing keys but `.get()`
        returns None (not 0) when the key is present with value None → cap
        skipped silently, signal survives.
      - `isinstance(True, int)` is True in Python, so a stray `True` would fall
        into the `< 2` branch with a rule_id like "below_triage_gate (patterns=True)".
    """
    if v is None or isinstance(v, bool):
        return 0
    if isinstance(v, int):
        return v
    if isinstance(v, float):
        return int(v)
    return 0


# --------------------------------------------------------------------
# Cap → narrative mapping for signals.demotion_reason
#
# The rule_id strings in auto_caps_triggered are the machine contract
# (replay tests, dashboards). compute_demotion_reason() turns them into a
# short curator-readable phrase persisted to signals.demotion_reason. New
# caps must register here AND in apply_auto_caps below; an unmapped cap
# falls back to the rule_id verbatim, which still surfaces *something*
# but loses the human gloss.
# --------------------------------------------------------------------

_CAP_NARRATIVES: Dict[str, str] = {
    "merger_arb.rule_A_sub_scale_return":
        "Annualized return below risk-free + 3% threshold",
    "merger_arb.rule_B_break_risk_dominance":
        "Break-risk dominant with low deal certainty",
    "binary_catalyst.ev_floor":
        "Expected value below 5% floor",
    "binary_catalyst.megacap_absorption_cap":
        "Megacap parent absorbs binary catalyst (low-magnitude readout)",
    "litigation.party_confidence_cap":
        "Party-resolution confidence too low (caption parse weak)",
    "litigation.universe_miss_cap":
        "Defendant outside public-issuer universe (NOS not high-priority)",
    "takeover_candidate.post_edge_disqualified":
        "Definitive merger agreement filed — signal is post-edge",
    "takeover_candidate.prior_rejection_cap":
        "Issuer rejected prior offer within last 6 months",
    "takeover_candidate.going_concern_cap":
        "Going-concern warning present (distress overshadows takeover thesis)",
    "takeover_candidate.below_triage_gate":
        "Below triage gate (insufficient pattern hits)",
}


def compute_demotion_reason(caps: List[str]) -> Optional[str]:
    """Return a short narrative for the first triggered cap, else None.

    `caps` is the auto_caps_triggered list from apply_auto_caps. Each entry
    starts with a stable rule_id and may include a parameterised suffix
    (e.g. `binary_catalyst.ev_floor (ev=2.34)`). We split on first space
    to recover the rule_id stem, look it up in `_CAP_NARRATIVES`, and
    append the full cap entry in parens so the parameter survives.
    """
    if not caps:
        return None
    primary = caps[0]
    stem = primary.split(" ", 1)[0]
    narrative = _CAP_NARRATIVES.get(stem)
    if narrative is None:
        return primary
    return f"{narrative} ({primary})"


def apply_auto_caps(
    signal: Dict[str, Any],
    dims: Dict[str, int],
    profile: str,
    band: str,
) -> Tuple[str, List[str]]:
    """Return (possibly_capped_band, list_of_triggered_rule_ids).

    Signal shape: {"raw_data": {...}, ...} — we read from raw_data just like v1.
    """
    caps: List[str] = []

    if profile == "merger_arb":
        annualized = signal.get("raw_data", {}).get("annualized_return_pct")
        if annualized is not None:
            if annualized < (RISK_FREE_RATE * 100) + 3:
                if band == "immediate":
                    band = "watchlist"
                    caps.append("merger_arb.rule_A_sub_scale_return")
        if dims.get("break_risk", 5) == 1 and dims.get("deal_certainty", 5) <= 2:
            if band == "immediate":
                band = "watchlist"
                caps.append("merger_arb.rule_B_break_risk_dominance")

    elif profile == "binary_catalyst":
        raw = signal.get("raw_data", {}) or {}
        p_approval = raw.get("approval_probability")
        upside = raw.get("upside_pct")
        downside = raw.get("downside_pct")
        if p_approval is not None and upside is not None and downside is not None:
            ev = p_approval * upside - (1 - p_approval) * abs(downside)
            if ev < EV_FLOOR and band == "immediate":
                band = "watchlist"
                caps.append(f"binary_catalyst.ev_floor (ev={ev:.2f})")

        mcap = raw.get("market_cap_usd")
        magnitude = dims.get("magnitude")
        if (
            isinstance(mcap, (int, float))
            and mcap > MEGACAP_ABSORPTION_THRESHOLD_USD
            and isinstance(magnitude, int)
            and magnitude < 3
            and band == "immediate"
        ):
            band = "watchlist"
            caps.append("binary_catalyst.megacap_absorption_cap")

    elif profile == "litigation":
        # 2026-04-24 selectivity tightening (courtlistener flood review):
        #   1. Raise party_confidence_cap threshold from <3 to <4. With the
        #      scanner now populating caption-extracted confidence + optional
        #      SEC issuer resolution, the distribution is bimodal — either
        #      clean (≥4) or junk (≤2). Threshold 4 captures more noise
        #      without hurting clean extractions.
        #   2. Add universe_miss_cap: litigation signals where no public-
        #      issuer ticker/FIGI resolved AND the NOS isn't high-priority
        #      (securities 850 / antitrust 410 / chancery matters) get
        #      archived. Patent and contract noise dominates when we don't
        #      recognize the defendant as a listed issuer.
        prc = dims.get("party_resolution_confidence", 5)
        if prc < 4:
            if band in ("immediate", "watchlist"):
                band = "archive"
                caps.append("litigation.party_confidence_cap")

        raw = signal.get("raw_data", {}) or {}
        universe_resolved = bool(raw.get("universe_resolved"))
        nos = str(raw.get("nos") or raw.get("nature_of_suit") or "")
        signal_category = str(raw.get("signal_category") or "")
        high_priority = (
            nos in ("850", "410")
            or signal_category == "delaware_chancery"
        )
        if (not universe_resolved) and (not high_priority):
            if band in ("immediate", "watchlist"):
                band = "archive"
                caps.append("litigation.universe_miss_cap")

    elif profile == "takeover_candidate":
        raw = signal.get("raw_data", {})
        if raw.get("definitive_merger_agreement") is True:
            caps.append("takeover_candidate.post_edge_disqualified")
            return "discard", caps
        if raw.get("rejected_prior_offer_6mo") is True:
            if band in ("immediate", "watchlist"):
                band = "archive"
                caps.append("takeover_candidate.prior_rejection_cap")
        if raw.get("going_concern_warning") is True:
            if band == "immediate":
                band = "watchlist"
                caps.append("takeover_candidate.going_concern_cap")
        # The legacy `Scoring engine/` folder also documented a sector-consolidation
        # watchlist cap here. Live Conan defers that rule until the scanner emits a
        # stable, auditable payload signal for it; no such field exists today.
        patterns_hit = _coerce_patterns_hit(raw.get("patterns_hit"))
        if patterns_hit < 2:
            caps.append(f"takeover_candidate.below_triage_gate (patterns={patterns_hit})")
            return "discard", caps

    return band, caps


# --------------------------------------------------------------------
# Signal scoring
# --------------------------------------------------------------------

class UnknownScoringProfile(ValueError):
    """Raised when a caller asks for a profile that is not in WEIGHTS.

    The scanner registry should prevent this, but the scorer is the last
    defensive boundary before bad rows can be persisted. Unknown profile drift is
    safer as a loud per-signal error than a quiet activist_governance mis-score.
    """


def score_signal(signal: Dict[str, Any], *, provenance: str = "scanner") -> Dict[str, Any]:
    """Apply the matching profile rubric to a raw signal.

    Input contract:
      signal["scoring_profile"] — one of WEIGHTS keys. Missing profile falls back
        to 'activist_governance' for v1 parity; unknown non-empty profiles raise
        UnknownScoringProfile rather than silently mis-scoring.
      signal["raw_data"]["dimensions"] — dict of dim_name → int[1..5]. If ANY required
        dim for the profile is missing, the signal is returned unscored (score=None,
        band=None) rather than silently filled with defaults. Values are clamped to
        [1, 5] before scoring.
      provenance — kwarg; when 'heuristic', HEURISTIC_SCORE_MULTIPLIER is applied
        to the weighted total before classify_band. Other values ('scanner',
        'ai_resolved', 'analyst') leave the score unchanged.

    Returns:
      {
        "scoring_profile": str,
        "dimensions": dict[str, int],      # clamped; {} when unscored
        "score": float | None,              # None when unscored
        "band": str | None,                 # None when unscored; otherwise post-auto-caps
        "auto_caps_triggered": list[str],
        "missing_dimensions": list[str],    # present only when unscored
      }

    This matches the "scoring" sub-object shape stored in v1's signal_log.json and the
    columns in v2's `signals` table (dimensions, score, band, auto_caps_triggered).

    Unscored semantics were introduced as a bug-fix patch to v1 behaviour, not a
    rubric_version bump: weights/thresholds/caps are unchanged, and previously every
    scanner that omitted dimensions produced a fake 30 (every profile's weights sum
    to exactly 10 × default-3). No historical re-scoring is possible because the
    dimensions were never recorded.
    """
    profile = signal.get("scoring_profile") or "activist_governance"
    if profile not in WEIGHTS:
        raise UnknownScoringProfile(
            f"score_signal: {profile!r} is not in WEIGHTS "
            f"(known: {sorted(WEIGHTS.keys())})"
        )

    raw_dims = signal.get("raw_data", {}).get("dimensions") or {}
    required = list(WEIGHTS[profile].keys())
    missing = [d for d in required if d not in raw_dims]
    if missing:
        return {
            "scoring_profile": profile,
            "dimensions": {},
            "score": None,
            "band": None,
            "auto_caps_triggered": [],
            "demotion_reason": None,
            "missing_dimensions": missing,
        }

    dims: Dict[str, int] = {}
    for dim in required:
        v = int(raw_dims[dim])
        dims[dim] = max(1, min(5, v))

    score = weighted_total(dims, profile)
    if provenance == "heuristic":
        score = round(score * HEURISTIC_SCORE_MULTIPLIER, 2)
    band = classify_band(score)
    band, caps = apply_auto_caps(signal, dims, profile, band)

    return {
        "scoring_profile": profile,
        "dimensions": dims,
        "score": score,
        "band": band,
        "auto_caps_triggered": caps,
        "demotion_reason": compute_demotion_reason(caps),
    }


# --------------------------------------------------------------------
# Re-score with externally supplied dims
# --------------------------------------------------------------------

def rescore_with_dims(
    scoring_profile: str,
    raw_payload: Dict[str, Any],
    dims: Dict[str, int],
    *,
    provenance: str = "ai_resolved",
) -> Dict[str, Any]:
    """Rescore a signal after external dim estimation.

    Pure-Python wrapper around `score_signal` — accepts `dims` as a separate
    arg so the caller doesn't have to mutate the existing raw_payload. Used
    by the `signal_resolver` Cowork skill to turn AI-estimated dims into a
    (score, band, auto_caps) tuple that the skill then writes to `signals`
    in a single UPDATE.

    The returned `dimensions_with_provenance` field is the dims dict plus a
    `_provenance` key — callers persist this as the JSONB value of
    `signals.dimensions`. Current provenance values: "scanner", "heuristic",
    "ai_resolved", "analyst".

    Raises `UnknownScoringProfile` if the caller passes a profile not in
    WEIGHTS, matching `score_signal`'s non-empty unknown-profile behavior.
    """
    if scoring_profile not in WEIGHTS:
        raise UnknownScoringProfile(
            f"rescore_with_dims: {scoring_profile!r} is not in WEIGHTS "
            f"(known: {sorted(WEIGHTS.keys())})"
        )
    merged_payload: Dict[str, Any] = dict(raw_payload or {})
    merged_payload["dimensions"] = dims
    result = score_signal(
        {"scoring_profile": scoring_profile, "raw_data": merged_payload},
        provenance=provenance,
    )

    dims_with_provenance = dimensions_with_provenance(
        result.get("dimensions") or {},
        provenance,
    )

    return {
        "scoring_profile": result["scoring_profile"],
        "dimensions": result["dimensions"],
        "dimensions_with_provenance": dims_with_provenance,
        "score": result["score"],
        "band": result["band"],
        "auto_caps_triggered": result["auto_caps_triggered"],
        "demotion_reason": result.get("demotion_reason"),
    }


# --------------------------------------------------------------------
# Convergence audit reference (spec.md §7.6.3)
#
# The reactor edge function (supabase/functions/reactor/index.ts) does the
# convergence classification in TypeScript against SQL-resolved group rows.
# This function is a pure-Python re-implementation with the SAME inputs and
# outputs; the `convergence_qa` Modal function (§7.6.3) samples live
# reactor decisions and re-computes them here, flagging mismatches into
# operator_flags(kind='convergence_disagreement').
#
# Must stay byte-equivalent to supabase/functions/_shared/convergence.ts::
# classifyGroup + windowDays + classifyBand + signalFingerprint. Any diff
# between this and the reactor is by definition a bug in one or the other.
# --------------------------------------------------------------------

import hashlib  # noqa: E402

CONVERGENCE_WINDOW_LITIGATION_DAYS = 30
CONVERGENCE_WINDOW_STANDARD_DAYS = 14


def window_days(profiles: List[str]) -> int:
    """Window rule: 30d if any signal in the group is litigation-profiled, else 14d."""
    return CONVERGENCE_WINDOW_LITIGATION_DAYS if "litigation" in profiles else CONVERGENCE_WINDOW_STANDARD_DAYS


def signal_fingerprint(source_content_hash: str, scoring_profile: str) -> str:
    """Deterministic fingerprint for alerts dedup — sha256(content_hash|profile).

    Matches the reactor's `signalFingerprint()` in `_shared/convergence.ts` and the
    `alerts.UNIQUE(entity_id, signal_fingerprint, day_utc)` constraint in §3.4.
    """
    return hashlib.sha256(f"{source_content_hash}|{scoring_profile}".encode("utf-8")).hexdigest()


def convergence_reference(group: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Pure-Python reference classification of a convergence group.

    Input: list of signal dicts, each with at minimum:
      - signal_id (str)
      - scoring_profile (str)
      - thesis_direction ('long' | 'short' | 'neutral' | None)
      - score (float)
      - source_content_hash (str)

    Output:
      {
        "bonus": 0 | 5 | 10,
        "type": "contradiction" | "same_direction" | "orthogonal" | "single",
        "winner_signal_id": str | None,
        "unique_signal_ids": [str, ...],   # post-dedup by source_content_hash
      }

    Dedup rule (matches reactor + v1 convergence_engine.py):
      - Collapse entries sharing source_content_hash; keep the highest-scoring per hash.
      - Signals with null/empty hash are treated as unique (keyed by signal_id).
      - This handles cross-listing echoes (same filing republished on a second exchange).

    Classification:
      - directions include both 'long' AND 'short'  → type='contradiction', bonus=0
      - only 1 unique signal after dedup            → type='single', bonus=0
      - no directional (long|short) signal in group → type='single', bonus=0
      - all same direction, 2 unique                → type='same_direction', bonus=5
      - all same direction, 3+ unique               → type='same_direction', bonus=10
      - same as above but profiles differ           → type='orthogonal', same bonus scale
    """
    if not group:
        return {"bonus": 0, "type": "single", "winner_signal_id": None, "unique_signal_ids": []}

    # Dedup on source_content_hash (keep highest-scoring per hash).
    by_hash: Dict[str, Dict[str, Any]] = {}
    for s in group:
        h = s.get("source_content_hash")
        if h is None or h == "":
            # Signals without a content hash can't be deduped; treat each as unique.
            by_hash[f"__no_hash__{s.get('signal_id')}"] = s
            continue
        existing = by_hash.get(h)
        if existing is None or _score_of(s) > _score_of(existing):
            by_hash[h] = s
    unique = list(by_hash.values())

    if not unique:
        return {"bonus": 0, "type": "single", "winner_signal_id": None, "unique_signal_ids": []}

    dirs = {s.get("thesis_direction") for s in unique if s.get("thesis_direction")}
    winner = _pick_winner(unique)
    unique_ids = [s["signal_id"] for s in unique]

    if "long" in dirs and "short" in dirs:
        return {"bonus": 0, "type": "contradiction", "winner_signal_id": winner["signal_id"], "unique_signal_ids": unique_ids}

    if len(unique) == 1:
        return {"bonus": 0, "type": "single", "winner_signal_id": winner["signal_id"], "unique_signal_ids": unique_ids}

    # Require at least one directional (long|short) signal to award a bonus.
    # Groups of pure neutral/null directions converge on "something is happening"
    # with no actionable thesis — no bonus. Neutral signals can still ride a
    # directional sibling's bonus (they're part of `unique` and contribute to
    # the 2/3+ threshold), but pure-neutral groups stay at bonus=0.
    if "long" not in dirs and "short" not in dirs:
        return {"bonus": 0, "type": "single", "winner_signal_id": winner["signal_id"], "unique_signal_ids": unique_ids}

    profiles = {s.get("scoring_profile") for s in unique}
    group_type = "orthogonal" if len(profiles) > 1 else "same_direction"
    bonus = 10 if len(unique) >= 3 else 5
    return {"bonus": bonus, "type": group_type, "winner_signal_id": winner["signal_id"], "unique_signal_ids": unique_ids}


def _pick_winner(signals: List[Dict[str, Any]]) -> Dict[str, Any]:
    return max(signals, key=_score_of)


def _score_of(s: Dict[str, Any]) -> float:
    try:
        return float(s.get("score") or 0.0)
    except (TypeError, ValueError):
        return 0.0
