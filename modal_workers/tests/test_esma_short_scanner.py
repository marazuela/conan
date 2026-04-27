from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import List

from modal_workers.scanners.esma_short_scanner import (
    CROWDED_SHORT_MIN_HOLDERS,
    CROWDING_MAX_HOLDER_STALENESS_DAYS,
    CROWDING_MIN_TOTAL_PCT,
    DEDUP_WINDOW_DAYS,
    _PendingEmission,
    _apply_top_signal_limit,
    _build_crowding_signal,
    _classify,
    _dedup_positions,
    _detect_crowded,
    _is_position_fresh,
    _load_dedup,
    _normalize_holder,
    _project_short_score,
    _prune_dedup,
    _signal_id,
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


def test_detect_crowded_ignores_duplicate_holders():
    # Same holder repeated (e.g. dual-regulator filings or multi-dated rows that
    # slipped past upstream dedup) must not count as N distinct holders.
    per_row = (CROWDING_MIN_TOTAL_PCT / CROWDED_SHORT_MIN_HOLDERS) + 0.1
    duplicates = [
        _holder("GB0000DDDD04", "Ilex Capital Partners (UK) LLP", per_row)
        for _ in range(CROWDED_SHORT_MIN_HOLDERS + 4)
    ]
    assert _detect_crowded(duplicates) == {}


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


def test_dedup_positions_keeps_most_recent_per_holder_isin():
    # Same (holder, isin) twice in a feed — e.g. amended FCA disclosure left alongside
    # the original row. Must collapse to one entry (the one with the later date), or
    # else both emit the same signal_id and crash the bulk insert.
    positions = [
        {"regulator": "FCA", "holder_name": "X", "isin": "GB0000AAAA01",
         "position_pct": 0.7, "position_date": "2026-04-15"},
        {"regulator": "FCA", "holder_name": "X", "isin": "GB0000AAAA01",
         "position_pct": 0.9, "position_date": "2026-04-20"},
        {"regulator": "FCA", "holder_name": "Y", "isin": "GB0000AAAA01",
         "position_pct": 0.5, "position_date": "2026-04-18"},
    ]
    result = _dedup_positions(positions)
    assert len(result) == 2
    by_holder = {p["holder_name"]: p for p in result}
    assert by_holder["X"]["position_pct"] == 0.9  # latest date wins
    assert by_holder["X"]["position_date"] == "2026-04-20"
    assert by_holder["Y"]["position_pct"] == 0.5


def test_signal_id_deterministic_on_regulator_isin_holder_type():
    # Contract the bug exploited: same tuple -> same PK. Regression test so a
    # future refactor doesn't silently add entropy (breaking idempotent re-runs)
    # or drop an input field (re-introducing cross-tuple collisions).
    a = _signal_id("FCA", "GB0000AAAA01", "Holder X", "short_disclosure")
    b = _signal_id("FCA", "GB0000AAAA01", "Holder X", "short_disclosure")
    assert a == b
    different_type = _signal_id("FCA", "GB0000AAAA01", "Holder X", "short_buildup")
    different_holder = _signal_id("FCA", "GB0000AAAA01", "Holder Y", "short_disclosure")
    assert a != different_type
    assert a != different_holder


# ---------------------------------------------------------------------------
# F-111 — dedup prune horizon must equal novelty window, not 3× it
# ---------------------------------------------------------------------------

def test_prune_dedup_horizon_equals_novelty_window():
    today = datetime.now(timezone.utc).date()
    # Anchor on (DEDUP_WINDOW_DAYS - 1) inside, (DEDUP_WINDOW_DAYS + 1) outside.
    inside = (today - timedelta(days=DEDUP_WINDOW_DAYS - 1)).strftime("%Y-%m-%d")
    boundary_old = (today - timedelta(days=DEDUP_WINDOW_DAYS + 2)).strftime("%Y-%m-%d")
    legacy_zombie = (today - timedelta(days=DEDUP_WINDOW_DAYS * 2)).strftime("%Y-%m-%d")
    log = {
        "h-inside": inside,
        "h-boundary": boundary_old,
        "h-zombie": legacy_zombie,
    }
    pruned = _prune_dedup(log)
    # Inside the novelty window — kept.
    assert "h-inside" in pruned
    # Outside it — dropped (was kept under the old 3× horizon).
    assert "h-boundary" not in pruned
    assert "h-zombie" not in pruned


# ---------------------------------------------------------------------------
# F-113 — corrupt dedup cache must surface a warning, not silently reset
# ---------------------------------------------------------------------------

class _FakeCacheClient:
    def __init__(self, payload):
        self._payload = payload
        self.writes: List[tuple] = []

    def read_cache(self, _scope, _key):
        return self._payload

    def write_cache(self, scope, key, body, content_type=None):
        self.writes.append((scope, key, body, content_type))


def test_load_dedup_corrupt_payload_appends_warning_and_preserves_blob():
    warnings: List[str] = []
    fake = _FakeCacheClient(b"{not-json")
    result = _load_dedup(fake, warnings)
    assert result == {}
    assert warnings, "expected a warning to surface the parse failure"
    assert any("dedup cache parse failure" in w for w in warnings)
    # Forensic copy must be written — without it operators cannot diagnose root cause.
    assert fake.writes, "expected corrupt blob to be persisted under dedup.corrupt-*.json"
    scope, key, body, _ct = fake.writes[0]
    assert scope == "esma"
    assert key.startswith("dedup.corrupt-") and key.endswith(".json")
    assert body == b"{not-json"


def test_load_dedup_missing_cache_returns_empty_without_warning():
    warnings: List[str] = []
    fake = _FakeCacheClient(None)
    assert _load_dedup(fake, warnings) == {}
    assert warnings == []


# ---------------------------------------------------------------------------
# `_project_short_score` must still return a non-zero ranking score even
# though the public estimator is now `_estimate_none`. Otherwise the
# top_signal_limit ranking degenerates to "every signal ties at 0".
# ---------------------------------------------------------------------------

def test_project_short_score_uses_preserved_heuristic():
    sig = _signal(
        "rank-1",
        "multi_regulator_crowding",
        {
            "isin": "GB0000RANK01",
            "holder_count": 6,
            "total_disclosed_pct": 12.0,
            "regulators": ["FCA", "AMF", "BAFIN"],
            "holders": [
                {"position_pct": 2.5, "position_date": datetime.now(timezone.utc).strftime("%Y-%m-%d")},
            ] * 6,
        },
        strength=4,
    )
    score = _project_short_score(sig)
    assert score > 0.0, "ranking score must use the preserved heuristic, not zero"


def test_project_short_score_handles_unknown_payload():
    sig = _signal("rank-2", "short_disclosure", {"isin": "GB0000RANK02"}, strength=2)
    # No position evidence at all → heuristic returns None → score 0.0.
    assert _project_short_score(sig) == 0.0


# ---------------------------------------------------------------------------
# Holder name normalization — collapse affiliate/suffix variants to one fund
# (audit/findings_2026-04-27.md DLQ: Elliott dual-filed FCA+AFM, Citadel
# under three affiliates, Atalan counted six times).
# ---------------------------------------------------------------------------

class TestNormalizeHolder:
    def test_entity_suffix_variants_collapse(self):
        # The exact failure mode that flooded the dashboard.
        a = _normalize_holder("Elliott Investment Management LP")
        b = _normalize_holder("Elliott Investment Management L.P.")
        c = _normalize_holder("Elliott Capital Advisors LLC")
        assert a == b == c == "elliott"

    def test_citadel_affiliates_collapse(self):
        names = [
            "Citadel Advisors LLC",
            "Citadel Capital Holdings LP",
            "Citadel Americas LLC",
        ]
        norms = {_normalize_holder(n) for n in names}
        assert norms == {"citadel"}

    def test_person_name_preserved_full(self):
        # No org suffix tokens, no comma → full lowercased name (don't accidentally
        # collapse two real shorts by an individual to their first name).
        assert _normalize_holder("John Smith") == "john smith"

    def test_empty_returns_empty_string(self):
        assert _normalize_holder(None) == ""
        assert _normalize_holder("") == ""
        assert _normalize_holder("   ") == ""

    def test_jpmorgan_asset_management_collapses(self):
        # Real DLQ case: "JPMorgan Asset Management UK Ltd" — geographic +
        # entity suffix tokens both strip; first token survives.
        assert _normalize_holder("JPMorgan Asset Management UK Ltd") == "jpmorgan"


# ---------------------------------------------------------------------------
# Crowding gate — must respect normalization + staleness
# ---------------------------------------------------------------------------

def _holder_dated(isin: str, holder: str, pct: float, days_ago: int) -> dict:
    pos_date = (datetime.now(timezone.utc).date() - timedelta(days=days_ago)).isoformat()
    return {"regulator": "FCA", "holder_name": holder, "isin": isin,
            "position_pct": pct, "position_date": pos_date}


class TestDetectCrowdedNormalization:
    def test_six_affiliates_of_one_fund_do_not_count_as_crowded(self):
        """Pre-fix: Atalan filed 6 times → marked multi_regulator_crowding.
        Post-fix: all 6 normalize to one holder → not crowded."""
        positions = [
            _holder_dated("GB0000FAKE01", f"Atalan Capital Partners {suffix}", 1.5, 5)
            for suffix in ["LP", "L.P.", "LLC", "Advisors LP", "Holdings Ltd", "Management Inc"]
        ]
        assert _detect_crowded(positions) == {}

    def test_six_distinct_funds_still_emit(self):
        # Sanity: real crowd must still pass.
        positions = [
            _holder_dated("GB0000REAL02", name, 1.0, 5)
            for name in [
                "Marshall Wace LLP",
                "Citadel Advisors LLC",
                "Bridgewater Associates LP",
                "Millennium Management LLC",
                "Two Sigma Investments LP",
                "Renaissance Technologies LLC",
            ]
        ]
        result = _detect_crowded(positions)
        assert list(result.keys()) == ["GB0000REAL02"]

    def test_stale_holders_dropped_from_crowding(self):
        """BNP-2017 / Atalan-2025-08 case: stale disclosures should not count
        toward a current crowd. 6 distinct funds but all 91+ days old → not crowded."""
        positions = [
            _holder_dated("GB0000STALE03", f"Fund {n} Capital Management LP", 1.0, 120)
            for n in range(1, 7)
        ]
        assert _detect_crowded(positions) == {}

    def test_mixed_fresh_and_stale_only_fresh_count(self):
        # 3 fresh + 4 stale; fresh-distinct < 6 → not crowded.
        fresh = [
            _holder_dated("GB0000MIX04", f"Fresh {n} Capital LP", 1.0, 10)
            for n in range(1, 4)
        ]
        stale = [
            _holder_dated("GB0000MIX04", f"Stale {n} Capital LP", 1.0, 200)
            for n in range(1, 5)
        ]
        assert _detect_crowded(fresh + stale) == {}


class TestIsPositionFresh:
    def test_today_is_fresh(self):
        today = datetime.now(timezone.utc)
        assert _is_position_fresh(today.strftime("%Y-%m-%d"), today) is True

    def test_within_window_is_fresh(self):
        now = datetime.now(timezone.utc)
        d = (now - timedelta(days=CROWDING_MAX_HOLDER_STALENESS_DAYS - 1)).strftime("%Y-%m-%d")
        assert _is_position_fresh(d, now) is True

    def test_past_window_is_stale(self):
        now = datetime.now(timezone.utc)
        d = (now - timedelta(days=CROWDING_MAX_HOLDER_STALENESS_DAYS + 1)).strftime("%Y-%m-%d")
        assert _is_position_fresh(d, now) is False

    def test_missing_date_fails_open(self):
        # Operator-visible row > silent drop — keep this contract.
        assert _is_position_fresh(None, datetime.now(timezone.utc)) is True
        assert _is_position_fresh("", datetime.now(timezone.utc)) is True

    def test_unparseable_date_fails_open(self):
        assert _is_position_fresh("not-a-date", datetime.now(timezone.utc)) is True


# ---------------------------------------------------------------------------
# Crowding payload — holder_count must be DISTINCT NORMALIZED holders, not
# `len(positions)`. This was the second smoking gun in the 2026-04-27 DLQ
# (esma_short_scanner.py:796 pre-fix).
# ---------------------------------------------------------------------------

class TestCrowdingPayloadHolderCount:
    def test_holder_count_uses_normalized_distinct(self):
        # Same fund, three suffix variants — payload should report 1 not 3.
        positions = [
            _holder_dated("GB0000PAY01", "Citadel Advisors LLC", 1.5, 5),
            _holder_dated("GB0000PAY01", "Citadel Capital Holdings LP", 1.5, 5),
            _holder_dated("GB0000PAY01", "Citadel Americas LLC", 1.5, 5),
        ]
        sig = _build_crowding_signal("GB0000PAY01", positions,
                                     scan_date=datetime.now(timezone.utc),
                                     issuer_figi=None)
        assert sig.raw_payload["holder_count"] == 1
        assert sig.raw_payload["filing_row_count"] == 3

    def test_holder_count_distinct_funds(self):
        positions = [
            _holder_dated("GB0000PAY02", "Marshall Wace LLP", 1.0, 5),
            _holder_dated("GB0000PAY02", "Citadel Advisors LLC", 1.0, 5),
            _holder_dated("GB0000PAY02", "Bridgewater Associates LP", 1.0, 5),
        ]
        sig = _build_crowding_signal("GB0000PAY02", positions,
                                     scan_date=datetime.now(timezone.utc),
                                     issuer_figi=None)
        assert sig.raw_payload["holder_count"] == 3
        assert sig.raw_payload["filing_row_count"] == 3
