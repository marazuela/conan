"""Tests for modal_workers.providers.pubmed.eutils.

Run: python -m pytest modal_workers/tests/test_pubmed_eutils.py -v
"""

from __future__ import annotations

import os
from unittest.mock import MagicMock, patch

import pytest

os.environ.setdefault("SUPABASE_URL", "https://x.supabase.co")
os.environ.setdefault("SUPABASE_SERVICE_KEY", "x")

from modal_workers.providers.pubmed.eutils import (
    PubMedClient,
    PubMedError,
)


# ---------- search ----------


def test_search_parses_pmid_list():
    client = PubMedClient()
    fake_resp = MagicMock()
    fake_resp.status_code = 200
    fake_resp.text = '{"esearchresult":{"idlist":["12345","67890"]}}'
    with patch.object(client._session, "get", return_value=fake_resp) as mock_get:
        pmids = client.search("BRAF V600E melanoma", limit=10)
    assert pmids == ["12345", "67890"]
    args, kwargs = mock_get.call_args
    assert "esearch.fcgi" in args[0]
    assert kwargs["params"]["term"] == "BRAF V600E melanoma"
    assert kwargs["params"]["retmax"] == 10


def test_search_handles_empty_idlist():
    client = PubMedClient()
    fake_resp = MagicMock()
    fake_resp.status_code = 200
    fake_resp.text = '{"esearchresult":{"idlist":[]}}'
    with patch.object(client._session, "get", return_value=fake_resp):
        pmids = client.search("nonsense xyz query")
    assert pmids == []


def test_search_404_returns_empty():
    client = PubMedClient()
    fake_resp = MagicMock()
    fake_resp.status_code = 404
    with patch.object(client._session, "get", return_value=fake_resp):
        pmids = client.search("anything")
    assert pmids == []


def test_search_4xx_raises():
    client = PubMedClient()
    fake_resp = MagicMock()
    fake_resp.status_code = 400
    fake_resp.text = "bad request"
    with patch.object(client._session, "get", return_value=fake_resp):
        with pytest.raises(PubMedError):
            client.search("anything")


def test_search_caps_limit_at_50():
    client = PubMedClient()
    fake_resp = MagicMock()
    fake_resp.status_code = 200
    fake_resp.text = '{"esearchresult":{"idlist":[]}}'
    with patch.object(client._session, "get", return_value=fake_resp) as mock_get:
        client.search("q", limit=999)
    assert mock_get.call_args.kwargs["params"]["retmax"] == 50


# ---------- fetch_abstracts ----------


_GOLDEN_EFETCH_XML = """<?xml version="1.0"?>
<PubmedArticleSet>
  <PubmedArticle>
    <MedlineCitation>
      <PMID Version="1">37894123</PMID>
      <Article PubModel="Print">
        <Journal>
          <Title>The New England Journal of Medicine</Title>
          <JournalIssue>
            <PubDate>
              <Year>2023</Year>
            </PubDate>
          </JournalIssue>
        </Journal>
        <ArticleTitle>Test trial of widget X in indication Y.</ArticleTitle>
        <Abstract>
          <AbstractText Label="BACKGROUND">Indication Y is rare.</AbstractText>
          <AbstractText Label="RESULTS">Widget X showed 30% response rate.</AbstractText>
        </Abstract>
        <AuthorList>
          <Author>
            <LastName>Smith</LastName>
            <Initials>JA</Initials>
          </Author>
          <Author>
            <LastName>Doe</LastName>
            <Initials>BC</Initials>
          </Author>
        </AuthorList>
      </Article>
    </MedlineCitation>
    <PubmedData>
      <ArticleIdList>
        <ArticleId IdType="pubmed">37894123</ArticleId>
        <ArticleId IdType="doi">10.1056/NEJMoa1234567</ArticleId>
      </ArticleIdList>
    </PubmedData>
  </PubmedArticle>
</PubmedArticleSet>
"""


def test_fetch_abstracts_parses_xml_into_papers():
    client = PubMedClient()
    fake_resp = MagicMock()
    fake_resp.status_code = 200
    fake_resp.text = _GOLDEN_EFETCH_XML
    with patch.object(client._session, "get", return_value=fake_resp):
        papers = client.fetch_abstracts(["37894123"])
    assert len(papers) == 1
    p = papers[0]
    assert p.pmid == "37894123"
    assert p.title.startswith("Test trial")
    assert "Widget X showed 30% response rate" in p.abstract
    assert "BACKGROUND" in p.abstract
    assert p.authors == ["Smith JA", "Doe BC"]
    assert p.journal == "The New England Journal of Medicine"
    assert p.year == 2023
    assert p.doi == "10.1056/NEJMoa1234567"
    assert p.primary_source_url == "https://pubmed.ncbi.nlm.nih.gov/37894123/"


def test_fetch_abstracts_empty_pmids_short_circuits():
    client = PubMedClient()
    with patch.object(client._session, "get") as mock_get:
        papers = client.fetch_abstracts([])
    assert papers == []
    mock_get.assert_not_called()


def test_fetch_abstracts_handles_malformed_xml():
    client = PubMedClient()
    fake_resp = MagicMock()
    fake_resp.status_code = 200
    fake_resp.text = "<not-valid-xml"
    with patch.object(client._session, "get", return_value=fake_resp):
        papers = client.fetch_abstracts(["123"])
    assert papers == []


# ---------- citation_graph_expand ----------


def test_citation_graph_expand_parses_neighbors():
    client = PubMedClient()
    fake_resp = MagicMock()
    fake_resp.status_code = 200
    fake_resp.text = (
        '{"linksets":[{"linksetdbs":[{"linkname":"pubmed_pubmed_citedin",'
        '"links":["111","222","333"]}]}]}'
    )
    with patch.object(client._session, "get", return_value=fake_resp):
        out = client.citation_graph_expand("123", direction="cited_by", limit=5)
    assert out == ["111", "222", "333"]


def test_citation_graph_expand_caps_at_limit():
    client = PubMedClient()
    fake_resp = MagicMock()
    fake_resp.status_code = 200
    fake_resp.text = (
        '{"linksets":[{"linksetdbs":[{"linkname":"pubmed_pubmed_citedin",'
        '"links":["1","2","3","4","5","6","7","8"]}]}]}'
    )
    with patch.object(client._session, "get", return_value=fake_resp):
        out = client.citation_graph_expand("123", direction="cited_by", limit=3)
    assert out == ["1", "2", "3"]
