"""Write-contract tests for bc_universe_pdufa using a fake Supabase client.

Asserts (per spike spec §5.4 — offline, no network):
  - upsert bodies for all three bc_* tables have the right shape + on_conflict targets
  - NOT-NULL placeholders are populated (sponsor_name, cycle_type, as_of/snapshot/built_at)
  - designations + borrow_available are NULL-not-False when unknown
  - surrogate appno -> feature_quality='low'; real appno -> 'standard'
  - bc_pipeline_runs opens 'running' and closes 'succeeded' (and 'failed' on crash)
  - tradeable is deduped by CIK (one snapshot per sponsor per day)
  - CHECK-safe enum values (status / appl_type / feature_quality)
"""

from __future__ import annotations

from datetime import date

import modal_workers.fetchers.universe.bc_universe_pdufa as bcmod
from modal_workers.fetchers.universe.bc_universe_pdufa import (
    UniverseCandidate,
    _apply_writes,
    _build_write_bodies,
    _close_run,
    _hit_to_candidate,
    _open_run,
    _ON_CONFLICT,
    _recover_appnos,
    _recover_one,
    _surrogate_appno,
)
from modal_workers.shared.bc_appno_recover import RecoveredAppno

SNAP = "2026-06-04"

# CHECK-allowed enum domains (mirror the live constraints verified 2026-06-04).
_OK_STATUS = {"running", "succeeded", "failed", "partial"}
_OK_APPL_TYPE = {"NDA", "BLA", "sNDA", "sBLA"}
_OK_FEATURE_QUALITY = {"standard", "low", "built_at_install"}


# ---------------------------------------------------------------------------
# Fake client — records every POST/PATCH for assertion.
# ---------------------------------------------------------------------------


class FakeClient:
    def __init__(self):
        self.calls = []  # list of dicts {method, path, json_body, prefer}
        self._next_id = 0

    def _rest_with_retry(self, method, path, *, json_body=None, prefer=None,
                         params=None, attempts=3, backoff_s=0.25):
        self.calls.append({
            "method": method, "path": path, "json_body": json_body, "prefer": prefer,
        })
        # Emulate bc_pipeline_runs INSERT ... RETURNING id
        if method == "POST" and path == "bc_pipeline_runs":
            self._next_id += 1
            return [{"id": f"run-{self._next_id}"}]
        return []

    def posts_to(self, table_prefix):
        return [c for c in self.calls
                if c["method"] == "POST" and c["path"].startswith(table_prefix)]


def _cand(**over):
    base = dict(
        cik="320193", ticker="EXEL", sponsor_name="Exelixis, Inc.",
        drug_name="zanzalintinib", accession="0001-25-000001", file_date="2026-05-20",
        forms_hit="8-K", pdufa_date="2026-09-15", appl_type="NDA",
        application_number="EDGAR8K:320193:zanzalintinib", is_surrogate_appno=True,
        has_bt=True, has_ft=None, has_aa=None,
        days_to_pdufa=103, in_window=True,
        market_cap_usd=12_000_000_000.0, avg_daily_volume_usd=134_000_000.0,
        options_chain_exists=True, borrow_available=None,
        passes_g2=True, passes_mcap_adv_only=True,
    )
    base.update(over)
    return UniverseCandidate(**base)


# ---------------------------------------------------------------------------
# _build_write_bodies — shape + NOT-NULL + NULL-not-False
# ---------------------------------------------------------------------------

def test_write_bodies_have_three_tables():
    bodies = _build_write_bodies(_cand(), SNAP)
    assert set(bodies) == {"applications", "features", "tradeable"}


def test_features_not_null_placeholders_present():
    f = _build_write_bodies(_cand(), SNAP)["features"]
    # NOT-NULL columns verified live must all be present + non-null.
    for col in ("sponsor_cik", "sponsor_name", "application_number", "appl_type",
                "cycle_type", "is_biosimilar_bla", "as_of_date", "snapshot_date", "built_at"):
        assert f.get(col) is not None, f"{col} must be populated (NOT NULL)"
    assert f["cycle_type"] == "unknown"
    assert f["is_biosimilar_bla"] is False
    assert f["snapshot_date"] == SNAP
    assert f["as_of_date"] == SNAP


def test_designations_null_not_false_when_unknown():
    f = _build_write_bodies(_cand(has_bt=True, has_ft=None, has_aa=None), SNAP)["features"]
    assert f["has_bt"] is True
    assert f["has_ft"] is None   # unknown -> NULL, never False
    assert f["has_aa"] is None


def test_borrow_always_null():
    t = _build_write_bodies(_cand(), SNAP)["tradeable"]
    assert t["borrow_available"] is None
    assert t["borrow_cost_bps"] is None
    assert t["data_source"] == "polygon"


def test_options_chain_exists_can_be_null():
    t = _build_write_bodies(_cand(options_chain_exists=None), SNAP)["tradeable"]
    assert t["options_chain_exists"] is None  # unknown propagates as NULL


def test_surrogate_appno_feature_quality_low():
    f = _build_write_bodies(_cand(application_number="EDGAR8K:320193:foo"), SNAP)["features"]
    assert f["feature_quality"] == "low"
    assert f["feature_quality"] in _OK_FEATURE_QUALITY


def test_real_appno_feature_quality_standard():
    f = _build_write_bodies(
        _cand(application_number="NDA215000", is_surrogate_appno=False), SNAP
    )["features"]
    assert f["feature_quality"] == "standard"


def test_appl_type_check_safe():
    for at in ("NDA", "BLA"):
        bodies = _build_write_bodies(_cand(appl_type=at), SNAP)
        assert bodies["applications"]["appl_type"] in _OK_APPL_TYPE
        assert bodies["features"]["appl_type"] in _OK_APPL_TYPE


def test_sponsor_name_falls_back_when_missing():
    # sponsor_name is NOT NULL; when the filer name is absent we fall back to ticker.
    f = _build_write_bodies(_cand(sponsor_name=None, ticker="EXEL"), SNAP)["features"]
    assert f["sponsor_name"] == "EXEL"
    assert _build_write_bodies(_cand(sponsor_name=None), SNAP)["applications"]["sponsor_name"]


def test_surrogate_appno_keys_on_date_not_drug():
    """The surrogate is keyed on the PDUFA DATE (stable), not the drug name (which
    parses non-deterministically across runs). Same CIK + same date -> SAME surrogate,
    regardless of whether/what drug parsed. This is the idempotency fix (gate crit 4):
    a 2nd same-day --apply must be a no-op even when the drug parse drifts."""
    # drug present
    a = _surrogate_appno("320193", "Zanzalintinib", "0001-25-000001", "2026-09-15")
    # SAME application, different run: drug failed to parse (None)
    b = _surrogate_appno("320193", None, "0009-25-999999", "2026-09-15")
    # SAME application, different run: drug parsed differently
    c = _surrogate_appno("320193", "zanza-001", "0002-25-000002", "2026-09-15")
    assert a == b == c == "EDGAR8K:320193:d20260915"  # all collapse on (CIK, date)


def test_surrogate_appno_distinct_dates_stay_distinct():
    """Genuinely-distinct same-sponsor applications carry DIFFERENT PDUFA dates, so
    date-keying still separates them (IONS olezarsen 06-30 vs zilganersen 09-22)."""
    olez = _surrogate_appno("874015", "olezarsen", "x", "2026-06-30")
    zilg = _surrogate_appno("874015", "zilganersen", "y", "2026-09-22")
    assert olez == "EDGAR8K:874015:d20260630"
    assert zilg == "EDGAR8K:874015:d20260922"
    assert olez != zilg


def test_surrogate_appno_falls_back_to_drug_then_accession_without_date():
    """With NO date, fall back to the drug slug; with neither, the accession tail."""
    assert _surrogate_appno("320193", "Zanzalintinib", "0001-25-000001", None) \
        == "EDGAR8K:320193:zanzalintinib"
    no_drug_no_date = _surrogate_appno("320193", None, "0001-25-000777", None)
    assert no_drug_no_date.startswith("EDGAR8K:320193:")
    assert "000777" in no_drug_no_date or no_drug_no_date.endswith("unknown")


# ---------------------------------------------------------------------------
# _apply_writes — on_conflict targets, dedup, prefer header
# ---------------------------------------------------------------------------

def test_apply_writes_targets_and_on_conflict():
    client = FakeClient()
    _apply_writes(client, [_cand()], SNAP)
    apps = client.posts_to("bc_applications")
    feats = client.posts_to("bc_application_features")
    trade = client.posts_to("bc_company_tradeable")
    assert len(apps) == 1 and _ON_CONFLICT["applications"] in apps[0]["path"]
    assert len(feats) == 1 and _ON_CONFLICT["features"] in feats[0]["path"]
    assert len(trade) == 1 and _ON_CONFLICT["tradeable"] in trade[0]["path"]
    # idempotent merge-duplicates prefer header on every upsert
    for c in apps + feats + trade:
        assert "merge-duplicates" in (c["prefer"] or "")


def test_apply_writes_dedups_tradeable_by_cik():
    client = FakeClient()
    # two applications, same sponsor CIK -> ONE tradeable snapshot, TWO features rows
    c1 = _cand(application_number="EDGAR8K:320193:a")
    c2 = _cand(application_number="EDGAR8K:320193:b")
    _apply_writes(client, [c1, c2], SNAP)
    assert len(client.posts_to("bc_application_features")) == 2
    assert len(client.posts_to("bc_company_tradeable")) == 1  # deduped by CIK


def test_apply_writes_skips_tradeable_when_no_ticker():
    client = FakeClient()
    _apply_writes(client, [_cand(ticker=None)], SNAP)
    assert len(client.posts_to("bc_company_tradeable")) == 0
    # but the application + features rows still land
    assert len(client.posts_to("bc_application_features")) == 1


def test_apply_writes_returns_write_stats_dict():
    client = FakeClient()
    stats = _apply_writes(client, [_cand(), _cand(application_number="EDGAR8K:320193:b")], SNAP)
    assert stats["written"] == 2
    assert stats["skipped_no_cik"] == 0
    assert stats["tradeable_written"] == 1  # same CIK -> one tradeable snapshot


def test_apply_writes_skips_candidate_with_no_real_cik():
    """A CIK-less candidate must be SKIPPED entirely (no 'sponsor_cik=0' placeholder
    write to ANY of the three tables) so two distinct CIK-less sponsors cannot collide
    on the tradeable composite UNIQUE and cross-link feature rows."""
    client = FakeClient()
    stats = _apply_writes(client, [_cand(cik=None)], SNAP)
    assert stats["written"] == 0
    assert stats["skipped_no_cik"] == 1
    assert stats["tradeable_written"] == 0
    # NOTHING written for the CIK-less candidate (not even a '0'-keyed app/feature row)
    assert client.posts_to("bc_applications") == []
    assert client.posts_to("bc_application_features") == []
    assert client.posts_to("bc_company_tradeable") == []


def test_apply_writes_two_no_cik_sponsors_do_not_collide():
    """Two distinct CIK-less sponsors: both skipped, so neither can clobber the other
    on the tradeable UNIQUE (the latent 'cik or 0' collision the fix removes)."""
    client = FakeClient()
    a = _cand(cik=None, ticker="AAAA", drug_name="alpha",
              application_number="EDGAR8K:0:alpha", surrogate_appno="EDGAR8K:0:alpha")
    b = _cand(cik=None, ticker="BBBB", drug_name="beta",
              application_number="EDGAR8K:0:beta", surrogate_appno="EDGAR8K:0:beta")
    stats = _apply_writes(client, [a, b], SNAP)
    assert stats["skipped_no_cik"] == 2
    assert stats["written"] == 0
    assert client.posts_to("bc_company_tradeable") == []


def test_apply_writes_mixed_cik_and_no_cik():
    """A real-CIK candidate writes normally even when a CIK-less one is present + skipped."""
    client = FakeClient()
    good = _cand(cik="320193", application_number="EDGAR8K:320193:good")
    bad = _cand(cik=None, ticker="ZZZZ", application_number="EDGAR8K:0:bad",
                surrogate_appno="EDGAR8K:0:bad")
    stats = _apply_writes(client, [good, bad], SNAP)
    assert stats["written"] == 1
    assert stats["skipped_no_cik"] == 1
    assert len(client.posts_to("bc_application_features")) == 1
    # the one features row written is for the real CIK, not "0"
    assert client.posts_to("bc_application_features")[0]["json_body"][0]["sponsor_cik"] == "320193"


# ---------------------------------------------------------------------------
# bc_pipeline_runs open/close — fail-loud
# ---------------------------------------------------------------------------

def test_open_run_status_running():
    client = FakeClient()
    run_id = _open_run(client, SNAP)
    assert run_id == "run-1"
    open_call = client.calls[0]
    assert open_call["path"] == "bc_pipeline_runs"
    body = open_call["json_body"][0]
    assert body["status"] == "running"
    assert body["status"] in _OK_STATUS
    assert body["pipeline_name"] == "bc_universe_pdufa"


def test_close_run_succeeded():
    client = FakeClient()
    rid = _open_run(client, SNAP)
    _close_run(client, rid, status="succeeded", n_processed=18, n_failed=0,
               log={"N_in_window_pending_nda_bla": 18})
    patch = client.calls[-1]
    assert patch["method"] == "PATCH"
    assert f"id=eq.{rid}" in patch["path"]
    assert patch["json_body"]["status"] == "succeeded"
    assert patch["json_body"]["status"] in _OK_STATUS
    assert patch["json_body"]["n_processed"] == 18
    assert patch["json_body"]["finished_at"] is not None


def test_close_run_failed_path_check_safe():
    client = FakeClient()
    rid = _open_run(client, SNAP)
    _close_run(client, rid, status="failed", n_processed=0, n_failed=0,
               log={"error": "boom"}, reason="RuntimeError: boom")
    patch = client.calls[-1]
    assert patch["json_body"]["status"] == "failed"
    assert patch["json_body"]["status"] in _OK_STATUS
    assert patch["json_body"]["reason"].startswith("RuntimeError")


def test_close_run_noop_when_no_run_id():
    client = FakeClient()
    _close_run(client, None, status="succeeded", n_processed=0, n_failed=0, log={})
    assert client.calls == []  # nothing written without a run id


# ---------------------------------------------------------------------------
# _hit_to_candidate — parse-bridge (guards the PdufaExtract field-name contract;
# this is the path the live run exercises end-to-end).
# ---------------------------------------------------------------------------

_FILING_TEXT = (
    "Exelixis, Inc. (EXEL) announced that the FDA accepted its New Drug Application "
    "for zanzalintinib and assigned a PDUFA goal date of September 15, 2026. The "
    "program holds Breakthrough Therapy designation."
)


def test_hit_to_candidate_parses_date_and_window(monkeypatch):
    monkeypatch.setattr(bcmod, "fetch_filing_text", lambda *a, **k: _FILING_TEXT, raising=False)
    # patch the lazily-imported name inside the function module
    import modal_workers.shared.edgar_efts as efts
    monkeypatch.setattr(efts, "fetch_filing_text", lambda *a, **k: _FILING_TEXT)

    info = {
        "accession": "0001-26-000123",
        "file_id": "0001-26-000123:doc.htm",
        "display_name": "Exelixis, Inc. (EXEL) (CIK 0001016504)",
        "file_date": "2026-06-01",
        "forms": "8-K",
    }
    # today chosen so the Sept 15 2026 date is ~106 days out (in 120d window)
    cand = _hit_to_candidate(info, user_agent="x", window_days=120, today=date(2026, 6, 1))
    assert cand is not None
    assert cand.pdufa_date == "2026-09-15"
    assert cand.appl_type == "NDA"
    assert cand.in_window is True
    assert cand.days_to_pdufa == 106
    assert cand.ticker == "EXEL"
    assert cand.cik == "1016504"
    assert cand.has_bt is True
    assert cand.application_number.startswith("EDGAR8K:1016504:")


def test_hit_to_candidate_none_when_body_unfetchable(monkeypatch):
    import modal_workers.shared.edgar_efts as efts
    monkeypatch.setattr(efts, "fetch_filing_text", lambda *a, **k: None)
    info = {"accession": "x", "file_id": "x:y", "display_name": "Foo (FOO) (CIK 0000000001)"}
    assert _hit_to_candidate(info, user_agent="x", window_days=120, today=date(2026, 6, 1)) is None


def test_hit_to_candidate_out_of_window_date(monkeypatch):
    import modal_workers.shared.edgar_efts as efts
    far = ("Acme (ACME) (CIK 0000000002) PDUFA goal date of January 5, 2030 for its NDA.")
    monkeypatch.setattr(efts, "fetch_filing_text", lambda *a, **k: far)
    info = {"accession": "z", "file_id": "z:y", "display_name": "Acme (ACME) (CIK 0000000002)"}
    cand = _hit_to_candidate(info, user_agent="x", window_days=120, today=date(2026, 6, 1))
    assert cand is not None
    assert cand.pdufa_date == "2030-01-05"
    assert cand.in_window is False  # far past the 120d window
    # surrogate is stashed so a later recovery has a restore anchor
    assert cand.surrogate_appno == cand.application_number
    assert cand.is_surrogate_appno is True


# ---------------------------------------------------------------------------
# Real-appno recovery wiring (_recover_one / _recover_appnos). The drugsfda
# join itself is unit-tested in test_bc_appno_recover.py; here we test that the
# enumerator ADOPTS a recovered number, flips provenance, and that a miss keeps
# the surrogate (-> feature_quality stays 'low').
# ---------------------------------------------------------------------------

def _recovered(appno="NDA215000", appl_type="NDA", priority="PRIORITY", basis="brand"):
    return RecoveredAppno(application_number=appno, appl_type=appl_type,
                          review_priority=priority, match_basis=basis)


def test_recover_one_adopts_real_number_and_flips_provenance():
    c = _cand(application_number="EDGAR8K:1016504:zanzalintinib",
              surrogate_appno="EDGAR8K:1016504:zanzalintinib",
              is_surrogate_appno=True, appl_type="NDA", review_priority=None)
    _recover_one(c, recover_fn=lambda d, s: _recovered(), cache={})
    assert c.application_number == "NDA215000"
    assert c.appl_type == "NDA"
    assert c.is_surrogate_appno is False
    assert c.appno_recovered is True
    assert c.appno_match_basis == "brand"
    assert c.review_priority == "PRIORITY"
    # -> the write body now scores feature_quality='standard' (real appno)
    f = _build_write_bodies(c, SNAP)["features"]
    assert f["feature_quality"] == "standard"
    assert f["review_priority"] == "PRIORITY"
    assert f["application_number"] == "NDA215000"


def test_recover_one_miss_keeps_surrogate_low_quality():
    c = _cand(application_number="EDGAR8K:999:foo",
              surrogate_appno="EDGAR8K:999:foo", is_surrogate_appno=True)
    _recover_one(c, recover_fn=lambda d, s: None, cache={})  # miss
    assert c.is_surrogate_appno is True
    assert c.appno_recovered is False
    assert c.application_number == "EDGAR8K:999:foo"
    f = _build_write_bodies(c, SNAP)["features"]
    assert f["feature_quality"] == "low"  # surrogate stays low


def test_recover_appnos_caches_per_cik_drug():
    calls = {"n": 0}

    def fake(drug, sponsor):
        calls["n"] += 1
        return _recovered(appno=f"NDA{calls['n']:06d}")

    # two candidates, SAME cik + drug -> ONE lookup shared via cache
    c1 = _cand(cik="100", drug_name="dupdrug", application_number="EDGAR8K:100:dupdrug",
               surrogate_appno="EDGAR8K:100:dupdrug", is_surrogate_appno=True)
    c2 = _cand(cik="100", drug_name="dupdrug", application_number="EDGAR8K:100:dupdrug",
               surrogate_appno="EDGAR8K:100:dupdrug", is_surrogate_appno=True)
    stats = _recover_appnos([c1, c2], recover_fn=fake, pace_s=0.0)
    assert stats["appno_lookups"] == 1            # cache collapsed the duplicate
    assert calls["n"] == 1
    assert c1.application_number == c2.application_number  # both adopted the same recovered number


def test_recover_appnos_counts_recovered_and_remaining():
    hit = _cand(cik="1", drug_name="a", application_number="EDGAR8K:1:a",
                surrogate_appno="EDGAR8K:1:a", is_surrogate_appno=True)
    miss = _cand(cik="2", drug_name="b", application_number="EDGAR8K:2:b",
                 surrogate_appno="EDGAR8K:2:b", is_surrogate_appno=True)

    def fake(drug, sponsor):
        return _recovered() if drug == "a" else None

    stats = _recover_appnos([hit, miss], recover_fn=fake, pace_s=0.0)
    assert stats["appno_recovered"] == 1
    assert stats["appno_surrogate_remaining"] == 1
    assert hit.appno_recovered is True
    assert miss.is_surrogate_appno is True


def test_recover_appnos_respects_max_lookups():
    def fake(drug, sponsor):
        return _recovered()

    cands = [
        _cand(cik=str(i), drug_name=f"d{i}", application_number=f"EDGAR8K:{i}:d{i}",
              surrogate_appno=f"EDGAR8K:{i}:d{i}", is_surrogate_appno=True)
        for i in range(5)
    ]
    stats = _recover_appnos(cands, recover_fn=fake, pace_s=0.0, max_lookups=2)
    assert stats["appno_lookups"] == 2
    # only the first 2 distinct (cik,drug) were recovered
    assert sum(1 for c in cands if c.appno_recovered) == 2
