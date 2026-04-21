"""
profile_map — central signal_type → scoring_profile mapping.

Keeps three things consistent across scanners and the signal-log validator:
  1. Every scanner tags its output with a concrete `scoring_profile` so
     `validate_signal_log` doesn't flag missing-profile records.
  2. A scanner-level default profile is available for legacy / minimal
     entries that were persisted before per-signal classification.
  3. The mapping is authoritative — scanners SHOULD import `profile_for()`
     rather than hardcoding a profile string next to each title-pattern.
"""

from __future__ import annotations

from typing import Optional

_TAKEOVER = {
    "takeover_bid_circular",
    "plan_of_arrangement",
    "acquisition_proposal",
    "merger_agreement",
    "mna_keyword",
    "directors_circular",
    "tender_offer",
    "mbo_announcement",
    "scheme_of_arrangement",
    "going_private",
    "offer_document",
    "possible_offer",
    "firm_offer",
}

_BINARY_CATALYST = {
    "trial_readout",
    "phase3_readout",
    "phase2_readout",
    "phase1_readout",
    "pdufa",
    "PDUFA",
    "bla_submission",
    "bla_approval",
    "marketing_authorization",
    "ema_chmp_positive_opinion",
    "ema_chmp_negative_opinion",
    "clinical_hold",
    "cmc_rtf",
    "advisory_committee",
    "pre_phase3_readout",
}

_LITIGATION = {
    "securities_class_action",
    "securities_litigation",
    "litigation_regulatory",
    "regulatory_action",
    "sec_enforcement",
    "doj_enforcement",
    "ftc_action",
    "cease_trade_order",
    "mcto_management_cease_trade",
    "investigation_announcement",
    "wells_notice",
    "subpoena",
    "antitrust_complaint",
    "consumer_class_action",
}

_SHORT_POSITIONING = {
    "short_crowded",
    "heavy_short",
    "short_report_published",
    "borrow_tight",
    "ftd_spike",
    "profit_warning",
    "guidance_downgrade",
    "earnings_miss",
    "impairment_loss",
    "write_down",
    "financial_restatement",
    "restatement",
    "internal_control_weakness",
    "going_concern_warning",
    "covenant_breach",
    "administration_or_receivership",
    "ccaa_filing",
}

_ACTIVIST_GOVERNANCE = {
    "activist_proxy",
    "activist_keyword",
    "activist_ownership",
    "13d_filing",
    "early_warning_10pct",
    "proxy_circular",
    "distress_keyword",
    "shareholder_meeting",
    "equity_fundraise",
    "bought_deal",
    "private_placement",
    "equity_financing",
    "buyback_initiation",
    "share_buyback",
    "dividend_increase",
    "dividend_cut",
    "special_dividend",
    "guidance_revision",
    "guidance_upgrade",
    "forecast_variance",
    "profit_upgrade",
    "tanshin_results",
    "material_change_report",
    "capital_reorganization",
    "spin_off",
    "rights_issue",
    "ni43101_technical_report",
    "ni51101_reserves",
    "governance_keyword",
    "late_filings",
    "distress_keyword",
}

_TAKEOVER_CANDIDATE = {"takeover_candidate"}

_SIGNAL_TYPE_TO_PROFILE = {
    **{st: "merger_arb" for st in _TAKEOVER},
    **{st: "binary_catalyst" for st in _BINARY_CATALYST},
    **{st: "litigation" for st in _LITIGATION},
    **{st: "short_positioning" for st in _SHORT_POSITIONING},
    **{st: "activist_governance" for st in _ACTIVIST_GOVERNANCE},
    **{st: "takeover_candidate" for st in _TAKEOVER_CANDIDATE},
}

_SCANNER_DEFAULT_PROFILE = {
    "asx": "activist_governance",
    "asx_scanner": "activist_governance",
    "tdnet": "activist_governance",
    "tdnet_scanner": "activist_governance",
    "lse_rns": "activist_governance",
    "lse_rns_scanner": "activist_governance",
    "sedar": "activist_governance",
    "sedar_plus_scanner": "activist_governance",
    "kind_scanner": "activist_governance",
    "hkex_scanner": "activist_governance",
    "bse_nse_scanner": "activist_governance",
    "cvm_scanner": "activist_governance",
    "bmv_scanner": "activist_governance",
    "courtlistener_scanner": "litigation",
    "sec_enforcement_scanner": "litigation",
    "pre_phase3_readout_scanner": "binary_catalyst",
    "fda_pdufa_pipeline": "binary_catalyst",
    "takeover_candidate_scanner": "takeover_candidate",
    "edgar_filing_monitor": "activist_governance",
    "esma_short_scanner": "short_positioning",
    "congressional_trading": "activist_governance",
}


def profile_for(signal_type: Optional[str] = None, scanner: Optional[str] = None) -> Optional[str]:
    """Resolve a scoring_profile for a given (signal_type, scanner)."""
    if signal_type:
        profile = _SIGNAL_TYPE_TO_PROFILE.get(signal_type)
        if profile:
            return profile
    if scanner:
        profile = _SCANNER_DEFAULT_PROFILE.get(scanner)
        if profile:
            return profile
    return None


TICKERLESS_BY_DESIGN_SCANNERS = frozenset(
    {
        "pre_phase3_readout_scanner",
        "cvm_scanner",
        "courtlistener_scanner",
        "sec_enforcement_scanner",
    }
)
