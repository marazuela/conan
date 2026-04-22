from __future__ import annotations

from datetime import datetime, timezone

from modal_workers.scanners.esma_short_scanner import (
    CROWDED_SHORT_MIN_HOLDERS,
    CROWDING_MIN_TOTAL_PCT,
    _PendingEmission,
    _apply_top_signal_limit,
    _classify,
    _detect_crowded,
)
from modal_workers.shared.scanner_base import Signal


def _signal(signal_id: str, signal_type: str, raw_payload: dict, strength: int) -> Signal:
    now = datetime.now(timezone.utc)
    return Signal(
        signal_id=signal_id,
        source_content_hash=f"sha256:{signal_id}",
        source_date=now,
        scan_date=now,
        signal_type=signal_type,
        raw_payload=raw_payload,
        strength_estimate=strength,
    )


def test_top_signal_limit_prefers_crowding_and_higher_conviction_rows():
    emissions = [
        _PendingEmission(
            signal=_signal(
                "crowding",
                "multi_regulator_crowding",
                {
                    "isin": "GB00TEST0001",
                    "holder_count": 4,
                    "regulators": ["FCA", "AMF", "BAFIN"],
                    "total_disclosed_pct": 8.4,
                },
                strength=3,
            ),
            dedup_hash="h-crowding",
        ),
        _PendingEmission(
            signal=_signal(
                "buildup",
                "short_buildup",
                {
                    "isin": "GB00TEST0002",
                    "position_pct": 1.6,
                    "previous_position_pct": 0.8,
                    "change_pct": 0.8,
                },
                strength=4,
            ),
            dedup_hash="h-buildup",
        ),
        _PendingEmission(
            signal=_signal(
                "disclosure",
                "short_disclosure",
                {
                    "isin": "GB00TEST0003",
                    "position_pct": 2.2,
                    "previous_position_pct": None,
                },
                strength=4,
            ),
            dedup_hash="h-disclosure",
        ),
    ]

    kept, dropped = _apply_top_signal_limit(emissions, 2)

    assert [item.signal.signal_id for item in kept] == ["crowding", "buildup"]
    assert [item.signal.signal_id for item in dropped] == ["disclosure"]


def test_top_signal_limit_zero_disables_ranking_cap():
    emissions = [
        _PendingEmission(
            signal=_signal(
                "one",
                "short_disclosure",
                {"isin": "GB00TEST0004", "position_pct": 0.9},
                strength=3,
            ),
            dedup_hash="h-one",
        ),
        _PendingEmission(
            signal=_signal(
                "two",
                "short_unwind",
                {
                    "isin": "GB00TEST0005",
                    "position_pct": 1.2,
                    "previous_position_pct": 1.9,
                    "change_pct": -0.7,
                },
                strength=3,
            ),
            dedup_hash="h-two",
        ),
    ]

    kept, dropped = _apply_top_signal_limit(emissions, 0)

    assert [item.signal.signal_id for item in kept] == ["one", "two"]
    assert dropped == []


def _holder(isin: str, holder: str, pct: float) -> dict:
    return {"regulator": "FCA", "holder_name": holder, "isin": isin, "position_pct": pct}


def test_detect_crowded_requires_min_holders():
    below_holders = [_holder("GB0000AAAA01", f"H{i}", 1.0) for i in range(CROWDED_SHORT_MIN_HOLDERS - 1)]
    assert _detect_crowded(below_holders) == {}


def test_detect_crowded_requires_min_total_pct():
    # Enough holders, but each holder tiny — total disclosed below threshold.
    per_holder = (CROWDING_MIN_TOTAL_PCT / CROWDED_SHORT_MIN_HOLDERS) - 0.1
    thin = [_holder("GB0000BBBB02", f"H{i}", per_holder) for i in range(CROWDED_SHORT_MIN_HOLDERS)]
    assert _detect_crowded(thin) == {}


def test_detect_crowded_emits_when_both_gates_pass():
    per_holder = (CROWDING_MIN_TOTAL_PCT / CROWDED_SHORT_MIN_HOLDERS) + 0.1
    thick = [_holder("GB0000CCCC03", f"H{i}", per_holder) for i in range(CROWDED_SHORT_MIN_HOLDERS)]
    result = _detect_crowded(thick)
    assert list(result.keys()) == ["GB0000CCCC03"]


def test_classify_sub_threshold_change_is_none():
    # 0.3pp change no longer crosses the 0.5pp bar.
    assert _classify({"position_pct": 1.1, "previous_position_pct": 0.8, "change_pct": 0.3}) is None


def test_classify_sub_threshold_disclosure_is_none():
    # Fresh 0.7% disclosure no longer crosses the 1.0% bar.
    assert _classify({"position_pct": 0.7, "previous_position_pct": None, "change_pct": None}) is None


def test_classify_buildup_strength_tiers_at_one_pct():
    mid = _classify({"position_pct": 1.2, "previous_position_pct": 0.5, "change_pct": 0.7})
    assert mid == ("short_buildup", "short", 3)
    big = _classify({"position_pct": 2.3, "previous_position_pct": 0.8, "change_pct": 1.5})
    assert big == ("short_buildup", "short", 4)
