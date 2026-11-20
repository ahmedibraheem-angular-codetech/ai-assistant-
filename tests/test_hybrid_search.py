"""Hybrid knowledge search: vector similarity + keyword overlap (phase 3, slice 2)."""

from app.knowledge.lexical import combine, keyword_score, normalize, terms


def test_arabic_normalization_matches_common_spelling_variants():
    assert normalize("أحمد") == normalize("احمد")
    assert normalize("مدرسة") == normalize("مدرسه")
    assert normalize("مستشفى") == normalize("مستشفي")
    assert normalize("مُحَمَّد") == normalize("محمد")
    assert normalize("٢٠٢٦") == "2026"


def test_terms_drop_stopwords_and_the_arabic_article():
    assert terms("ما هو سعر المنتج في المشروع؟") == {"سعر", "منتج", "مشروع"}
    assert terms("What is the price of WIDGET-42") == {"price", "widget", "42"}


def test_keyword_score_is_the_share_of_query_terms_found():
    query = terms("فاتورة رقم 1043")
    assert keyword_score(query, "تفاصيل الفاتورة رقم ١٠٤٣ للعميل") == 1.0
    assert keyword_score(query, "فاتورة أخرى") == 1 / 3
    assert keyword_score(set(), "anything") == 0.0


def test_exact_code_beats_a_slightly_closer_vector():
    # A chunk with the exact invoice number should outrank a vaguely similar one.
    query = terms("invoice 1043")
    exact = combine(0.55, keyword_score(query, "Invoice 1043 total is 900"))
    vague = combine(0.65, keyword_score(query, "Invoices are sent monthly"))
    assert exact > vague


import sys
from pathlib import Path

import pytest

from app.knowledge.ocr import available as ocr_available
from tests.test_knowledge import _approve, _prepare

_FIXTURE = Path(__file__).parent / "fixtures" / "invoice_ar.png"


@pytest.mark.skipif(sys.platform != "darwin" or not ocr_available(), reason="macOS Vision OCR")
def test_image_is_read_with_ocr_and_found_by_its_number(client, tmp_path, monkeypatch):
    folder, workspace, agent, _child = _prepare(client, tmp_path, monkeypatch)
    (folder / "invoice.png").write_bytes(_FIXTURE.read_bytes())
    body = _approve(
        client,
        agent["id"],
        "ingest_document",
        {"workspace_id": workspace["id"], "relative_path": "invoice.png", "scope": "project"},
    )
    assert body["status"] == "COMPLETED", body
    assert body["output"]["status"] == "ready"
    found = client.post(
        "/api/v1/tools/search_knowledge/test",
        json={"arguments": {"query": "فاتوره رقم ١٠٤٣"}, "agent_id": agent["id"]},
    ).json()
    citations = found["output"]["citations"]
    assert citations and citations[0]["filename"] == "invoice.png"
    top = found["output"]["results"][0]
    assert top["keyword_score"] == 1.0
    assert "1043" in top["excerpt"]


def test_ocr_reports_non_macos_clearly(monkeypatch):
    from app.files.exceptions import FileAccessError
    from app.knowledge import ocr

    monkeypatch.setattr(ocr.sys, "platform", "linux")
    with pytest.raises(FileAccessError, match="needs macOS"):
        ocr.image_text(b"x", ".png")
