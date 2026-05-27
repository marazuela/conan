"""Regression tests for the deterministic citation validator's prefix tolerance.

Background — the Stage 1 prompt instructs the model with a 6-char short
example (`[F:abc123]`) while the in-context fact table shows 8-char shorts
(`F:abc12345`). The regex accepts 6-12 char shorts. Pre-fix the lookup set
was built with `[:8]` exact strings, so a model that anchored on the prompt
example emitted 6-char shorts that the lookup could never resolve — Stage 7
blocked every run that took the prompt at its word.

Fix: prefix-tolerant matching. A 6-char cited short resolves if it is a
prefix of any full fact_id / document_id shown to the model.
"""

import uuid

from orchestrator_runtime.runtime import _validate_citations


def _fact(fact_id: str) -> dict:
    return {
        "id": fact_id,
        "fact_type": "trial_endpoint",
        "fact_text": "p < 0.001",
        "evidence_quote": "primary endpoint met with p < 0.001",
        "document_id": str(uuid.uuid4()),
    }


def test_six_char_short_resolves_against_full_uuid_fact():
    """Model emits a 6-char short matching the prompt example — must resolve."""
    fact_uuid = "abc12345-0000-4000-8000-000000000001"
    prose = "Endpoint was met [F:abc123]."
    result = _validate_citations(
        cited_prose=prose,
        facts=[_fact(fact_uuid)],
        document_ids=[],
    )
    assert result.pass_, f"6-char prefix should resolve, got findings: {result.findings}"
    assert result.n_citations_checked == 1
    assert result.n_citations_resolved == 1


def test_eight_char_short_resolves_against_full_uuid_fact():
    """Standard 8-char short emission — must continue to resolve."""
    fact_uuid = "abc12345-0000-4000-8000-000000000002"
    prose = "Endpoint was met [F:abc12345]."
    result = _validate_citations(
        cited_prose=prose,
        facts=[_fact(fact_uuid)],
        document_ids=[],
    )
    assert result.pass_
    assert result.n_citations_resolved == 1


def test_unresolved_short_still_fails():
    """Hallucinated short that prefixes no fact must still raise."""
    fact_uuid = "abc12345-0000-4000-8000-000000000003"
    prose = "Endpoint was met [F:deadbe]."  # 'deadbe' does not prefix 'abc12345...'
    result = _validate_citations(
        cited_prose=prose,
        facts=[_fact(fact_uuid)],
        document_ids=[],
    )
    assert not result.pass_
    assert len(result.findings) == 1
    assert result.findings[0].check == "unresolved_fact_id"
    assert result.findings[0].affected_id == "deadbe"


def test_doc_short_prefix_resolution():
    """Same prefix tolerance for [D:] document shorts."""
    doc_uuid = "fedcba98-7654-3210-fedc-ba9876543210"
    prose = "Per the 8-K [D:fedcba]."
    result = _validate_citations(
        cited_prose=prose,
        facts=[],
        document_ids=[doc_uuid],
    )
    assert result.pass_
    assert result.n_citations_resolved == 1


def test_mixed_resolvable_and_hallucinated_shorts():
    """One legit cite + one hallucinated cite: pass_=False, one finding."""
    fact_uuid = "abc12345-0000-4000-8000-000000000004"
    prose = "Real cite [F:abc12345], fake cite [F:deadbe]."
    result = _validate_citations(
        cited_prose=prose,
        facts=[_fact(fact_uuid)],
        document_ids=[],
    )
    assert not result.pass_
    assert result.n_citations_checked == 2
    assert result.n_citations_resolved == 1
    assert len(result.findings) == 1
    assert result.findings[0].affected_id == "deadbe"


def test_case_insensitive_matching():
    """Regex is IGNORECASE; both upper and lowercase shorts must resolve."""
    fact_uuid = "ABC12345-0000-4000-8000-000000000005"
    prose = "Endpoint [F:ABC123] met."
    result = _validate_citations(
        cited_prose=prose,
        facts=[_fact(fact_uuid)],
        document_ids=[],
    )
    assert result.pass_
    assert result.n_citations_resolved == 1


def test_empty_prose_no_citations():
    """No citations + no facts = no findings, vacuously passes."""
    result = _validate_citations(
        cited_prose="No citations here.",
        facts=[],
        document_ids=[],
    )
    assert result.pass_
    assert result.n_citations_checked == 0
    assert result.n_citations_resolved == 0


def test_seven_char_short_resolves():
    """Length between the prompt example (6) and the in-context format (8)."""
    fact_uuid = "abc12345-0000-4000-8000-000000000006"
    prose = "Endpoint [F:abc1234] met."
    result = _validate_citations(
        cited_prose=prose,
        facts=[_fact(fact_uuid)],
        document_ids=[],
    )
    assert result.pass_
    assert result.n_citations_resolved == 1
