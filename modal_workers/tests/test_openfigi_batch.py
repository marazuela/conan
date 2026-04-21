from __future__ import annotations

from modal_workers.shared import openfigi_resolver


def test_resolve_batch_preserves_alignment_on_short_response(monkeypatch):
    openfigi_resolver._inmem_cache.clear()

    monkeypatch.setattr(openfigi_resolver, "_load_cache", lambda key: None)
    monkeypatch.setattr(openfigi_resolver, "_save_cache", lambda key, data: None)
    monkeypatch.setattr(
        openfigi_resolver,
        "_post_batch",
        lambda queries: [{
            "data": [{
                "figi": "BBG000FIRST",
                "compositeFIGI": "BBG000FIRSTCOMP",
                "ticker": "AAA",
                "name": "First Corp",
                "securityType": "Common Stock",
                "exchCode": "US",
            }],
        }],
    )

    results = openfigi_resolver.resolve_batch([
        {"idType": "ID_ISIN", "idValue": "US0000000001"},
        {"idType": "ID_ISIN", "idValue": "US0000000002"},
    ])

    assert len(results) == 2
    assert results[0].resolved is True
    assert results[0].issuer_figi == "BBG000FIRSTCOMP"
    assert results[1].resolved is False
    assert results[1].error == "missing batch response"
