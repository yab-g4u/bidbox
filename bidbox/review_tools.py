"""Deterministic review tools over downloaded OCDS records. They never invent values."""

from __future__ import annotations

import json
import re
from datetime import datetime
from statistics import median

from bidbox.data import get_process
from bidbox.ingest import connect_store, db_path

MIN_COMPARABLES = 3
STOPWORDS = {
    "supply", "supplies", "delivery", "deliver", "provision", "services", "service", "and", "the", "for", "of", "to",
    "with", "assorted", "various", "general", "items", "item", "tender", "quotation", "request", "framework", "contract",
    "county", "office", "year", "financial", "lot", "works", "goods",
}
BUYER_TYPES = (
    ("county government", ("county government", "county assembly", "county executive")),
    ("water and sanitation", ("water", "sewerage", "sanitation")),
    ("health facility", ("hospital", "health", "medical")),
    ("education institution", ("university", "college", "school", "polytechnic", "institute", "vocational", "technical")),
    ("energy and utilities", ("electricity", "power", "energy", "transmission", "geothermal", "petroleum")),
    ("judiciary", ("judiciary", "court", "tribunal")),
)
KEY_FIELDS = (
    "buyer.name", "tender.title", "tender.value.amount", "tender.procurementMethod", "tender.numberOfTenderers",
    "tender.tenderPeriod.startDate", "tender.tenderPeriod.endDate", "awards[0].value.amount", "awards[0].date",
    "awards[0].suppliers",
)


def resolve(record: dict, path: str):
    """Resolve a dotted OCDS path such as ``awards[0].value.amount``; None when not published."""
    value = record
    for part in re.findall(r"[^.\[\]]+|\[\d+\]", path):
        if part.startswith("["):
            index = int(part[1:-1])
            if not isinstance(value, list) or index >= len(value):
                return None
            value = value[index]
        else:
            if not isinstance(value, dict) or part not in value:
                return None
            value = value[part]
    return value


def present(record: dict, path: str) -> bool:
    return resolve(record, path) not in (None, "", [], {})


def cite(process: dict, path: str) -> dict:
    return {"ocid": process["ocid"], "field_path": path, "value": resolve(process["record"], path),
            "source_url": process.get("source_url"), "origin": process.get("origin", "live")}


def _award_index(record: dict, field: str) -> int | None:
    """First award publishing `field` ("value.amount" or "suppliers")."""
    for index in range(len(record.get("awards") or [])):
        if present(record, f"awards[{index}].{field}"):
            return index
    return None


def _process(ocid: str, origin: str = "live") -> dict | None:
    return get_process(ocid, origin) if db_path().exists() else None


def _cannot(tool: str, reason: str, next_check: str | None) -> dict:
    return {"tool": tool, "status": "cannot_assess", "reason": reason, "next_check": next_check, "observations": [], "citations": []}


def buyer_type(name: str | None) -> str | None:
    lowered = (name or "").lower()
    return next((label for label, words in BUYER_TYPES if any(word in lowered for word in words)), None)


def title_keywords(title: str | None) -> list[str]:
    words = [word for word in re.findall(r"[a-z]{4,}", (title or "").lower()) if word not in STOPWORDS]
    return list(dict.fromkeys(words))[:4]


def _rows(sql: str, params: list) -> list[tuple]:
    with connect_store(read_only=True) as connection:
        return connection.execute(sql, params).fetchall()


def supplier_history(ocid: str, origin: str = "live") -> dict:
    process = _process(ocid, origin)
    if not process:
        return _cannot("supplier_history", "Process not found in the downloaded records.", None)
    record = process["record"]
    index = _award_index(record, "suppliers")
    path = f"awards[{index}].suppliers" if index is not None else "awards[0].suppliers"
    suppliers = resolve(record, path) or []
    if not suppliers:
        return _cannot("supplier_history", "No supplier is published on this award (awards[].suppliers not published).", "process_checks")
    supplier = suppliers[0]
    supplier_id, supplier_name = supplier.get("id"), supplier.get("name")
    needle = supplier_name or supplier_id
    rows = _rows("SELECT ocid, buyer_name, raw_json FROM procurement_records WHERE origin = ? AND award_count > 0 AND contains(raw_json, ?)",
                 [origin, json.dumps(needle)[1:-1]])
    awards, buyers, dates, totals = [], {}, [], {}
    for other_ocid, other_buyer, raw in rows:
        for award in json.loads(raw).get("awards") or []:
            match = any((supplier_id and item.get("id") == supplier_id) or (supplier_name and item.get("name") == supplier_name)
                        for item in award.get("suppliers") or [])
            if not match:
                continue
            awards.append(other_ocid)
            buyers[other_buyer or "Buyer not published"] = buyers.get(other_buyer or "Buyer not published", 0) + 1
            if award.get("date"):
                dates.append(award["date"][:10])
            value = award.get("value") or {}
            if value.get("amount") is not None:
                currency = value.get("currency") or "currency not published"
                totals[currency] = totals.get(currency, 0) + float(value["amount"])
    others = [item for item in awards if item != ocid]
    buyer_name = process.get("buyer_name")
    buyer_total = _rows("SELECT coalesce(sum(award_count), 0) FROM procurement_records WHERE origin = ? AND buyer_name = ?",
                        [origin, buyer_name])[0][0] if buyer_name else 0
    with_buyer = buyers.get(buyer_name, 0) if buyer_name else 0
    share = round(with_buyer / buyer_total, 3) if buyer_total else None
    citations = [cite(process, path)] + ([cite(process, "buyer.name")] if present(record, "buyer.name") else [])
    observations = [{
        "code": "SUPPLIER_HISTORY", "category": "supplier_history", "severity": "info",
        "message": f"{supplier_name or supplier_id} has {len(others)} other award(s) in the downloaded records across {len(buyers)} buyer(s).",
        "citations": [citations[0]],
    }]
    if share is not None and buyer_total >= MIN_COMPARABLES:
        observations.append({
            "code": "BUYER_SHARE", "category": "supplier_history", "severity": "medium" if share >= 0.5 else "info",
            "message": f"This supplier holds {with_buyer} of {buyer_total} downloaded awards by {buyer_name} ({share:.0%}).",
            "citations": citations,
        })
    return {"tool": "supplier_history", "status": "ok", "supplier": {"id": supplier_id, "name": supplier_name},
            "award_count": len(awards), "other_award_count": len(others), "total_value_by_currency": totals,
            "buyers": buyers, "first_award_date": min(dates) if dates else None, "last_award_date": max(dates) if dates else None,
            "buyer_award_total": buyer_total, "buyer_share": share, "observations": observations, "citations": citations,
            "coverage_note": "Counts cover downloaded records only; full history is not established."}


def _classification(record: dict) -> tuple[str, str] | None:
    for path in ("awards[0].items[0].classification", "tender.items[0].classification"):
        value = resolve(record, path)
        if isinstance(value, dict) and value.get("id"):
            return path, str(value["id"])
    return None


def price_check(ocid: str, origin: str = "live") -> dict:
    process = _process(ocid, origin)
    if not process:
        return _cannot("price_check", "Process not found in the downloaded records.", None)
    record = process["record"]
    index = _award_index(record, "value.amount")
    amount_path = f"awards[{index if index is not None else 0}].value.amount"
    amount = resolve(record, amount_path)
    if amount is None:
        return _cannot("price_check", "Cannot benchmark: award value (awards[].value.amount) is not published.", "process_checks")
    currency = resolve(record, amount_path.replace("amount", "currency"))
    if not currency:
        return _cannot("price_check", f"Cannot benchmark: award currency ({amount_path.replace('amount', 'currency')}) is not published, so amounts cannot be compared.", "process_checks")
    classification = _classification(record)
    rows = []
    if classification:
        basis, confidence = f"same classification {classification[1]}", "standard"
        candidates = _rows("SELECT ocid, raw_json FROM procurement_records WHERE origin = ? AND award_count > 0 AND ocid <> ? AND contains(raw_json, ?)",
                           [origin, ocid, classification[1]])
        rows = [(o, raw) for o, raw in candidates if (_classification(json.loads(raw)) or ("", ""))[1] == classification[1]]
    else:
        kind, keywords = buyer_type(process.get("buyer_name")), title_keywords(process.get("title"))
        confidence = "approximate, low confidence"
        if not kind or not keywords:
            missing = "buyer type could not be derived from buyer.name" if not kind else "title has no distinctive keywords"
            return _cannot("price_check", f"Cannot benchmark: no item classification is published and {missing}.", "supplier_history")
        basis = f"no classification published; same buyer type ({kind}) and title keywords {', '.join(keywords)}"
        needed = min(2, len(keywords))
        candidates = _rows("SELECT ocid, title, buyer_name, raw_json FROM procurement_records WHERE origin = ? AND award_count > 0 AND ocid <> ?"
                           " AND (" + " OR ".join(["contains(lower(coalesce(title, '')), ?)"] * len(keywords)) + ")",
                           [origin, ocid, *keywords])
        rows = [(o, raw) for o, title, buyer, raw in candidates
                if buyer_type(buyer) == kind and sum(word in (title or "").lower() for word in keywords) >= needed]
    peers = []
    for peer_ocid, raw in rows:
        valued = [award for award in json.loads(raw).get("awards") or []
                  if (award.get("value") or {}).get("amount") is not None and award["value"].get("currency") == currency]
        if classification:
            matching = [award for award in valued
                        if any(str((item.get("classification") or {}).get("id")) == classification[1] for item in award.get("items") or [])]
            valued = matching or (valued if len(valued) == 1 else [])
        if valued:
            peers.append({"ocid": peer_ocid, "amount": float(valued[0]["value"]["amount"])})
    citations = [cite(process, amount_path)] + ([cite(process, classification[0])] if classification else
                [cite(process, path) for path in ("buyer.name", "tender.title") if present(record, path)])
    if len(peers) < MIN_COMPARABLES:
        result = _cannot("price_check", f"Cannot benchmark: only {len(peers)} comparable award(s) in {currency or 'the same currency'} ({basis}); at least {MIN_COMPARABLES} are required.", "supplier_history")
        return {**result, "basis": basis, "confidence": confidence, "comparables": peers, "citations": citations}
    mid = median(peer["amount"] for peer in peers)
    ratio = round(float(amount) / mid, 2) if mid else None
    observations = [{
        "code": "PRICE_VS_COMPARABLES", "category": "pricing", "severity": "medium" if ratio and ratio >= 2 else "info",
        "message": f"Award value {currency} {float(amount):,.0f} is {ratio}x the median of {len(peers)} comparable awards ({currency} {mid:,.0f}); {confidence}.",
        "citations": citations,
    }]
    return {"tool": "price_check", "status": "ok", "basis": basis, "confidence": confidence, "amount": amount, "currency": currency,
            "comparable_count": len(peers), "median": mid, "ratio_to_median": ratio, "comparables": peers[:20],
            "observations": observations, "citations": citations}


def _days(start: str, end: str) -> int | None:
    try:
        return (datetime.fromisoformat(end.replace("Z", "+00:00")) - datetime.fromisoformat(start.replace("Z", "+00:00"))).days
    except (ValueError, AttributeError):
        return None


def process_checks(ocid: str, origin: str = "live") -> dict:
    process = _process(ocid, origin)
    if not process:
        return _cannot("process_checks", "Process not found in the downloaded records.", None)
    record = process["record"]
    observations = []
    if present(record, "tender.procurementMethod"):
        method = resolve(record, "tender.procurementMethod")
        observations.append({"code": "PROCUREMENT_METHOD", "category": "process", "severity": "medium" if method in ("direct", "limited") else "info",
                             "message": f"Procurement method is published as '{method}'.", "citations": [cite(process, "tender.procurementMethod")]})
    start, end = resolve(record, "tender.tenderPeriod.startDate"), resolve(record, "tender.tenderPeriod.endDate")
    if start and end and (days := _days(start, end)) is not None:
        observations.append({"code": "TENDER_PERIOD", "category": "process", "severity": "info",
                             "message": f"Tender period is {days} day(s) long.",
                             "citations": [cite(process, "tender.tenderPeriod.startDate"), cite(process, "tender.tenderPeriod.endDate")]})
    if present(record, "tender.numberOfTenderers"):
        count = resolve(record, "tender.numberOfTenderers")
        observations.append({"code": "TENDERER_COUNT", "category": "process", "severity": "medium" if count == 1 else "info",
                             "message": f"{count} tenderer(s) are published for this process.", "citations": [cite(process, "tender.numberOfTenderers")]})
    index = _award_index(record, "value.amount")
    award_path = f"awards[{index}].value.amount" if index is not None else None
    award_currency = resolve(record, award_path.replace("amount", "currency")) if award_path else None
    if (award_path and present(record, award_path) and present(record, "tender.value.amount")
            and award_currency and award_currency == resolve(record, "tender.value.currency")):
        award, tender = float(resolve(record, award_path)), float(resolve(record, "tender.value.amount"))
        if tender:
            ratio = round(award / tender, 2)
            observations.append({"code": "AWARD_VS_TENDER", "category": "process", "severity": "medium" if ratio > 1 else "info",
                                 "message": f"Award value is {ratio}x the published tender value.",
                                 "citations": [cite(process, award_path), cite(process, "tender.value.amount")]})
    missing = [path for path in KEY_FIELDS if not present(record, path)]
    status = "ok" if observations else "cannot_assess"
    return {"tool": "process_checks", "status": status, "missing_fields": missing, "observations": observations,
            "reason": None if observations else "None of the process fields are published.",
            "next_check": None if observations else "supplier_history",
            "citations": [citation for item in observations for citation in item["citations"]]}


TOOLS = {"supplier_history": supplier_history, "price_check": price_check, "process_checks": process_checks}


def plan_tools(mode: str, record: dict) -> tuple[list[dict], list[dict]]:
    """Choose deterministic tools from the review mode and the fields that exist."""
    wanted = {"price": ["price_check"], "supplier": ["supplier_history"], "both": ["price_check", "supplier_history"]}[mode]
    requirements = {name: f"awards[{index if index is not None else 0}].{field}"
                    for name, field in (("price_check", "value.amount"), ("supplier_history", "suppliers"))
                    for index in [_award_index(record, field)]}
    planned, skipped = [], []
    for name in wanted:
        if present(record, requirements[name]):
            planned.append({"tool": name, "reason": f"Mode '{mode}' requests it; {requirements[name]} is published"})
        else:
            skipped.append({"tool": name, "reason": f"{requirements[name]} is not published"})
    if any(present(record, path) for path in ("tender.procurementMethod", "tender.tenderPeriod.startDate", "tender.numberOfTenderers", "tender.value.amount")):
        planned.append({"tool": "process_checks", "reason": "Process fields are published"})
    else:
        skipped.append({"tool": "process_checks", "reason": "No process fields are published"})
    return planned, skipped


def validate_findings(findings: list[dict], record: dict) -> tuple[list[dict], list[dict]]:
    """Keep only findings whose every citation names an OCDS path whose value matches the record."""
    kept, dropped = [], []
    for finding in findings:
        citations = finding.get("citations") or []
        ok = bool(citations) and all(
            isinstance(item, dict) and item.get("field_path") and present(record, item["field_path"])
            and resolve(record, item["field_path"]) == item.get("value") for item in citations)
        (kept if ok else dropped).append(finding if ok else {"finding": finding.get("code") or finding.get("message"), "reason": "dropped: no source"})
    return kept, dropped
