"""
ASX-specific rubric seeder. Maps (signal_type, strength) → 7-dimension rubric.

ASX is English-language and well-covered by local sell-side, so info_asymmetry
is generally LOW (2-3) unless the signal is small-cap or non-standard. That
makes this rubric skew lower than TDnet's (which baselines info_asymmetry=4).
"""

def rubric_scores_asx(strength: int, signal_type: str, is_price_sensitive: bool,
                      market_cap_usd_mm: float | None) -> dict:
    # Baseline
    base = {
        "signal_strength": max(1, min(5, strength)),
        "catalyst_clarity": 3,
        "info_asymmetry": 2,   # ASX = English, well-covered
        "risk_reward": 3,
        "edge_decay": 3,
        "liquidity": 4,        # ASX $300M+ names are generally liquid
        "catalyst_timeline": 3,
    }

    # Signal-type overrides
    if signal_type in ("takeover_bid", "scheme_of_arrangement", "merger_agreement", "acquisition_proposal"):
        base["catalyst_clarity"] = 5
        base["catalyst_timeline"] = 5
        base["edge_decay"] = 4
    elif signal_type == "guidance_downgrade":
        base["catalyst_clarity"] = 5
        base["catalyst_timeline"] = 5
        base["edge_decay"] = 5
    elif signal_type == "guidance_upgrade":
        base["catalyst_clarity"] = 5
        base["catalyst_timeline"] = 5
        base["edge_decay"] = 5
    elif signal_type == "guidance_revision":
        base["catalyst_clarity"] = 4
        base["catalyst_timeline"] = 4
        base["edge_decay"] = 4
    elif signal_type == "impairment_loss":
        base["catalyst_clarity"] = 4
        base["catalyst_timeline"] = 4
    elif signal_type in ("going_concern_warning", "covenant_breach"):
        base["catalyst_clarity"] = 4
        base["catalyst_timeline"] = 4
        base["risk_reward"] = 4
    elif signal_type in ("equity_placement", "rights_issue", "share_purchase_plan"):
        base["catalyst_clarity"] = 4
        base["catalyst_timeline"] = 3
    elif signal_type == "substantial_holder_change":
        base["catalyst_clarity"] = 2
        base["catalyst_timeline"] = 2
        base["info_asymmetry"] = 2
    elif signal_type in ("substantial_holder_initial", "substantial_holder_ceasing"):
        base["catalyst_clarity"] = 3
        base["catalyst_timeline"] = 3
        base["info_asymmetry"] = 3
    elif signal_type in ("trading_halt", "trading_suspension"):
        base["catalyst_clarity"] = 2
        base["catalyst_timeline"] = 2
        base["info_asymmetry"] = 2
    elif signal_type in ("jorc_drilling_results", "jorc_resource_update"):
        base["catalyst_clarity"] = 3
        base["info_asymmetry"] = 4   # technical data, often under-covered
    elif signal_type == "appendix_4c_cashflow":
        base["catalyst_clarity"] = 3
        base["info_asymmetry"] = 3
    elif signal_type in ("special_dividend", "dividend_cut"):
        base["catalyst_clarity"] = 4
        base["catalyst_timeline"] = 4

    # Price-sensitive boost to edge_decay (market has already started pricing in)
    if is_price_sensitive:
        base["edge_decay"] = min(5, base["edge_decay"] + 1)

    # Small-cap bump to info_asymmetry (less sell-side coverage)
    if market_cap_usd_mm is not None and market_cap_usd_mm < 1000:
        base["info_asymmetry"] = min(5, base["info_asymmetry"] + 1)

    return base

# --- END OF FILE ---
