"""Mode-driven reviews: deterministic tool plans, cited findings only, honest cannot-assess."""

import asyncio
import json

import duckdb
import pytest

from agent import review_agent
from audit.logger import get_audit_logs
from bidbox import review_tools
from bidbox.ingest import ingest_page, initialize

SOURCE = "https://tenders.go.ke/api/ocds/tenders?fy=2026-2027"


def release(ocid, buyer, title, supplier, amount, method="open", tenderers=None, period=True):
    tender = {"title": title, "procurementMethod": method}
    if tenderers is not None:
        tender["numberOfTenderers"] = tenderers
    if period:
        tender["tenderPeriod"] = {"startDate": "2026-08-01T00:00:00+03:00", "endDate": "2026-08-15T00:00:00+03:00"}
    return {"ocid": ocid, "id": f"{ocid}-1", "date": "2026-08-20T00:00:00Z", "tag": ["award"], "buyer": {"id": buyer[:4], "name": buyer},
            "tender": tender, "awards": [{"id": f"A-{ocid}", "title": title, "value": {"amount": amount, "currency": "KES"},
                                          "suppliers": [{"id": supplier[:3], "name": supplier}]}]}


@pytest.fixture
def store(tmp_path, monkeypatch):
    db = tmp_path / "test.duckdb"
    monkeypatch.setenv("BIDBOX_DB_PATH", str(db))
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("LLM_API_KEY", raising=False)
    monkeypatch.setenv("LLM_BASE_URL", "https://api.groq.com/openai/v1")
    releases = [release("ocds-t-1", "Alpha Water and Sanitation Company", "Supply of tyres and spare parts", "ACME", 900000, tenderers=1),
                release("ocds-t-2", "Beta Water Company", "Tyres and spare parts supply", "ZED", 300000),
                release("ocds-t-3", "Gamma Water Services", "Spare parts and tyres", "ACME", 310000),
                release("ocds-t-4", "Delta Sewerage Company", "Tyres spare parts", "QQQ", 290000),
                release("ocds-t-5", "Alpha Water and Sanitation Company", "Office chairs", "ACME", 50000, period=False),
                release("ocds-t-6", "Some Agency", "Unique gadget", "NOBODY", 10000, method="direct")]
    page = tmp_path / "page.json"
    page.write_text(json.dumps({"uri": SOURCE, "publishedDate": "2026-10-31T23:59:00Z", "releases": releases}))
    with duckdb.connect(str(db)) as connection:
        initialize(connection)
        ingest_page(connection, page, SOURCE, "2026-2027")
    return db


def tools_in(run_id):
    return [e["tool"] for e in get_audit_logs(run_id) if e["tool"] in review_tools.TOOLS]


def test_modes_produce_different_tool_sequences(store):
    for mode, expected in [("price", ["price_check", "process_checks"]), ("supplier", ["supplier_history", "process_checks"]),
                           ("both", ["price_check", "supplier_history", "process_checks"])]:
        run_id = f"run-{mode}"
        asyncio.run(review_agent.review(run_id, "ocds-t-1", mode))
        assert tools_in(run_id) == expected
        plan = next(e for e in get_audit_logs(run_id) if e["tool"] == "plan")
        assert plan["outputs"]["mode"] == mode


def test_findings_cite_paths_and_values(store):
    result = asyncio.run(review_agent.review("run-cite", "ocds-t-1", "both"))
    assert result["findings"]
    for finding in result["findings"]:
        assert finding["citations"] and all(c["field_path"] and "value" in c for c in finding["citations"])
    codes = {f["code"] for f in result["findings"]}
    assert {"PRICE_VS_COMPARABLES", "SUPPLIER_HISTORY", "TENDERER_COUNT"} <= codes
    price = review_tools.price_check("ocds-t-1")
    assert price["confidence"] == "approximate, low confidence" and price["comparable_count"] == 3


def test_uncited_findings_are_dropped(store, monkeypatch):
    async def fake_brief(run_id, process, outputs):
        return "Brief.", [{"code": "LLM_1", "message": "No source", "citations": []},
                          {"code": "LLM_2", "message": "Wrong value", "citations": [{"ocid": "ocds-t-1", "field_path": "awards[0].value.amount", "value": 1}]},
                          {"code": "LLM_3", "message": "Good", "citations": [{"ocid": "ocds-t-1", "field_path": "tender.procurementMethod", "value": "open"}]}], []
    monkeypatch.setattr(review_agent, "write_brief", fake_brief)
    result = asyncio.run(review_agent.review("run-drop", "ocds-t-1", "price"))
    dropped = {d["finding"]: d["reason"] for d in result["dropped_findings"]}
    assert dropped == {"LLM_1": "dropped: no source", "LLM_2": "dropped: no source"}
    assert "LLM_3" in {f["code"] for f in result["findings"]}


def test_cannot_benchmark_reports_reason_and_next_check(store):
    price = review_tools.price_check("ocds-t-6")
    assert price["status"] == "cannot_assess" and price["next_check"] and "Cannot benchmark" in price["reason"]
    result = asyncio.run(review_agent.review("run-none", "ocds-t-6", "price"))
    assert any("Next available check" in item for item in result["limitations"])


def test_button_endpoint_starts_mode_run_without_free_text(store, monkeypatch):
    from fastapi.testclient import TestClient
    from bidbox import cases, web
    started = []
    monkeypatch.setattr(cases, "submit_case", lambda run_id, filing=False: started.append(run_id))
    monkeypatch.setattr(cases, "candidates", lambda question: pytest.fail("free-text intake must be skipped"))
    client = TestClient(web.app)
    response = client.post("/api/cases", json={"ocid": "ocds-t-1", "mode": "supplier"})
    assert response.status_code == 202 and started == [response.json()["run_id"]]
    assert cases.load_case(started[0])["mode"] == "supplier"
    assert client.post("/api/cases", json={"ocid": "ocds-missing", "mode": "price"}).status_code == 404
    assert client.post("/api/cases", json={"ocid": "ocds-t-1", "mode": "other"}).status_code == 422
