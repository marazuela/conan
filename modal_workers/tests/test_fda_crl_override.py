"""Tests for the CRL override switch: bridge resolver + record-and-activate admin."""

from __future__ import annotations

import pytest

from modal_workers.scanners import fda_signal_bridge as bridge
from modal_workers.scripts import fda_crl_override_admin as admin


class FakeClient:
    def __init__(self, version_rows=None):
        self.version_rows = list(version_rows or [])
        self.calls = []

    def _rest(self, method, table, params=None):
        self.calls.append((method, table, params, None))
        if table == "fda_model_versions":
            return list(self.version_rows)
        return []

    def _rest_with_retry(self, method, table, params=None, json_body=None, prefer=None):
        self.calls.append((method, table, params, json_body))
        if isinstance(json_body, list):
            return [{"id": "new", **json_body[0]}]
        return []


# --------------------------------------------------------------------------- #
# bridge resolver: env wins explicitly, else an active model version drives it
# --------------------------------------------------------------------------- #
def test_resolve_env_force_on(monkeypatch):
    monkeypatch.setenv("FDA_CRL_OVERRIDE_ENABLED", "true")
    assert bridge._resolve_crl_override_enabled(FakeClient([])) is True  # no version, env forces on


def test_resolve_env_kill_switch(monkeypatch):
    monkeypatch.setenv("FDA_CRL_OVERRIDE_ENABLED", "false")
    # active version present, but env explicitly off -> kill switch wins
    assert bridge._resolve_crl_override_enabled(FakeClient([{"version": "v1"}])) is False


def test_resolve_defers_to_active_version(monkeypatch):
    monkeypatch.delenv("FDA_CRL_OVERRIDE_ENABLED", raising=False)
    assert bridge._resolve_crl_override_enabled(FakeClient([{"version": "v1"}])) is True
    assert bridge._resolve_crl_override_enabled(FakeClient([])) is False


def test_resolve_swallows_lookup_errors():
    class Boom:
        def _rest(self, *a, **k):
            raise RuntimeError("db down")
    # env unset + lookup fails -> safe default OFF
    assert bridge._crl_override_version_active(Boom()) is False


# --------------------------------------------------------------------------- #
# admin: enable supersedes then inserts; disable supersedes; status reports
# --------------------------------------------------------------------------- #
def test_enable_supersedes_then_inserts():
    c = FakeClient([{"version": "old"}])
    out = admin.enable(c, "v1", notes="go", now_iso="2026-06-03T00:00:00+00:00")
    assert out["enabled"] is True and out["version"] == "v1"
    patches = [x for x in c.calls if x[0] == "PATCH"]
    posts = [x for x in c.calls if x[0] == "POST"]
    assert patches and posts  # superseded prior, then inserted
    assert patches[0][3] == {"superseded_at": "2026-06-03T00:00:00+00:00"}
    body = posts[0][3][0]
    assert body["scope"] == "fda_crl_override"
    assert body["version"] == "v1"
    assert body["effective_at"] == "2026-06-03T00:00:00+00:00"


def test_disable_supersedes_active():
    c = FakeClient([{"version": "v1"}])
    out = admin.disable(c, now_iso="2026-06-03T00:00:00+00:00")
    assert out["enabled"] is False
    patches = [x for x in c.calls if x[0] == "PATCH"]
    assert patches and patches[0][3]["superseded_at"] == "2026-06-03T00:00:00+00:00"
    assert not [x for x in c.calls if x[0] == "POST"]  # disable never inserts


def test_status_reports_active_state():
    assert admin.status(FakeClient([{"version": "v1", "effective_at": "t"}]))["enabled"] is True
    assert admin.status(FakeClient([]))["enabled"] is False


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
