"""
SEDAR+ (Canada) rubric seeder. Maps (signal_type, strength) → 7-dim rubric.

Canada is mostly English with some French filings. Sell-side coverage is dense
on TSX large-caps (Shopify, RBC, TD, Suncor, etc.) but thin on TSXV juniors
and on mid-cap mining/oil-&-gas where the deep expertise sits with local
specialists (Northern Miner, BNN). So:
  - info_asymmetry baseline = 2 for TSX >$5B, 3 otherwise
  - TSXV bump: +1 info_asymmetry (specialist coverage)
  - NI 43-101 / NI 51-101 technical reports bump info_asymmetry to 4
  - French-language filings get info_asymmetry +1 (translation barrier)
"""

FRENCH_LANG_CODES = {"fr", "fra", "french"}


def rubric_scores_sedar(strength: int,
                        signal_type: str,
                        market_cap_usd_mm: float | None,
                        board: str = "tsx",
                        filing_language: str = "en",
                        translation_confidence: float | None = None) -> dict:
    # Baseline
    base = {
        "signal_strength": max(1, min(5, strength)),
        "catalyst_clarity": 3,
        "info_asymmetry": 2,
        "risk_reward": 3,
        "edge_decay": 3,
        "liquidity": 4,
        "catalyst_timeline": 3,
    }

    # Signal-type overrides
    if signal_type in ("takeover_bid_circular", "plan_of_arrangement",
                       "acquisition_proposal", "merger_agreement"):
        base["catalyst_clarity"] = 5
        base["catalyst_timeline"] = 5
        base["edge_decay"] = 4
    elif signal_type == "directors_circular":
        base["catalyst_clarity"] = 4
        base["catalyst_timeline"] = 4
        base["edge_decay"] = 4
    elif signal_type == "material_change_report":
        base["catalyst_clarity"] = 4
        base["catalyst_timeline"] = 3
    elif signal_type == "guidance_downgrade":
        base["catalyst_clarity"] = 5
        base["catalyst_timeline"] = 5
        base["edge_decay"] = 5
    elif signal_type == "guidance_upgrade":
        base["catalyst_clarity"] = 5
        base["catalyst_timeline"] = 5
        base["edge_decay"] = 5
    elif signal_type == "early_warning_10pct":
        base["catalyst_clarity"] = 3
        base["catalyst_timeline"] = 2
        base["info_asymmetry"] = 3
    elif signal_type in ("ni43101_technical_report", "ni51101_reserves"):
        base["catalyst_clarity"] = 3
        base["info_asymmetry"] = 4   # technical, specialist coverage
        base["catalyst_timeline"] = 2
    elif signal_type in ("cease_trade_order", "mcto_management_cease_trade"):
        base["catalyst_clarity"] = 5
        base["catalyst_timeline"] = 5
        base["risk_reward"] = 4
        base["edge_decay"] = 5
    elif signal_type == "impairment_loss":
        base["catalyst_clarity"] = 4
        base["catalyst_timeline"] = 4
    elif signal_type in ("going_concern_warning", "covenant_breach"):
        base["catalyst_clarity"] = 4
        base["catalyst_timeline"] = 4
        base["risk_reward"] = 4
    elif signal_type in ("equity_financing", "bought_deal", "private_placement"):
        base["catalyst_clarity"] = 4
        base["catalyst_timeline"] = 3
    elif signal_type == "share_buyback":
        base["catalyst_clarity"] = 3
        base["catalyst_timeline"] = 3
    elif signal_type in ("special_dividend", "dividend_cut"):
        base["catalyst_clarity"] = 4
        base["catalyst_timeline"] = 4
    elif signal_type in ("proxy_circular", "annual_mda", "interim_mda"):
        base["catalyst_clarity"] = 2
        base["catalyst_timeline"] = 2
        base["edge_decay"] = 2

    # Board modifier: TSXV is venture-stage, specialist-covered
    if board == "tsxv":
        base["info_asymmetry"] = min(5, base["info_asymmetry"] + 1)
        base["liquidity"] = max(1, base["liquidity"] - 1)

    # Small-cap bump to info_asymmetry
    if market_cap_usd_mm is not None and market_cap_usd_mm < 1000:
        base["info_asymmetry"] = min(5, base["info_asymmetry"] + 1)

    # French-language filings — slight edge because of translation/coverage barrier
    is_french = (filing_language or "en").lower() in FRENCH_LANG_CODES
    if is_french:
        base["info_asymmetry"] = min(5, base["info_asymmetry"] + 1)
        # D-002 translation honesty: low confidence caps strength/RR
        if translation_confidence is not None and translation_confidence < 0.85:
            base["signal_strength"] = min(base["signal_strength"], 2)
            base["risk_reward"] = min(base["risk_reward"], 3)

    return base
