"""Mode-driven evidence review: deterministic tools first, then a cited LLM brief."""

from __future__ import annotations

import json
import re
import os
import time
from typing import Any

from audit.events import request_approval
from audit.logger import append_audit_event
from bidbox import review_tools
from bidbox.data import get_process

REPORT_ACTION = "flag_finding_and_create_investigation_file"
BRIEF_SYSTEM = (
    "You write a short procurement review brief for an auditor from tool outputs only. Return JSON "
    '{"brief": string, "findings": [{"message": string, "field_path": string, "value": any}]}. '
    "Every finding must cite one OCDS field_path from the citations given and its exact value. "
    "Never accuse, never recommend an award, never add facts not in the input. Keep the brief under 80 words."
)


def llm_configured() -> bool:
    from urllib.parse import urlsplit
    host = urlsplit(os.getenv("LLM_BASE_URL", "https://api.groq.com/openai/v1")).hostname
    return bool(os.getenv("LLM_API_KEY")) or host in ("localhost", "127.0.0.1", "::1")


def contract_summary(process: dict) -> dict:
    record = process["record"]
    award = (record.get("awards") or [{}])[0]
    return {"ocid": process["ocid"], "title": process.get("title"), "buyer_name": process.get("buyer_name"),
            "amount": (award.get("value") or {}).get("amount"), "currency": (award.get("value") or {}).get("currency"),
            "supplier_name": next((s.get("name") for s in award.get("suppliers") or []), None), "origin": process.get("origin", "live"),
            "sources": [{"ocid": process["ocid"], "field_path": "awards[0]", "source_url": process.get("source_url")}]}


def plan(run_id: str, mode: str, process: dict) -> list[dict]:
    planned, skipped = review_tools.plan_tools(mode, process["record"])
    append_audit_event(run_id, "agent", "plan", {"source": "review mode and published OCDS fields", "mode": mode},
                       {"mode": mode, "tools": planned, "skipped": skipped,
                        "summary": f"Run {', '.join(item['tool'] for item in planned) or 'no tools'} on published fields"},
                       origin=process.get("origin", "live"))
    return planned


def run_tools(run_id: str, ocid: str, planned: list[dict], origin: str) -> list[dict]:
    outputs = []
    for item in planned:
        started = time.perf_counter()
        result = review_tools.TOOLS[item["tool"]](ocid, origin)
        append_audit_event(run_id, "agent", item["tool"], {"ocid": ocid}, {"status": result["status"], "reason": result.get("reason"),
                           "next_check": result.get("next_check"), "summary": "; ".join(o["message"] for o in result["observations"]) or result.get("reason")},
                           duration_ms=(time.perf_counter() - started) * 1000, status="ok", origin=origin)
        outputs.append(result)
    return outputs


async def write_brief(run_id: str, process: dict, outputs: list[dict]) -> tuple[str, list[dict], list[str]]:
    """Ask the existing LLM client for a brief and cited findings; never required for the deterministic result."""
    if not llm_configured():
        return "", [], ["Review brief not written: model not configured on the host. Deterministic findings are shown as-is."]
    from agent.llm import ask_json
    payload = {"contract": contract_summary(process), "tool_outputs": [
        {key: value for key, value in output.items() if key in ("tool", "status", "reason", "observations", "basis", "confidence", "missing_fields")}
        for output in outputs]}
    try:
        response = await ask_json(run_id, BRIEF_SYSTEM, json.dumps(payload, default=str), {"type": "object"})
    except Exception as exc:  # the deterministic result stands without a brief
        return "", [], [f"Review brief not written: {type(exc).__name__}. Deterministic findings are shown as-is."]
    findings = []
    for index, item in enumerate(response.get("findings") or []):
        if not isinstance(item, dict):
            continue
        path = item.get("field_path")
        citation = {"ocid": process["ocid"], "field_path": path, "value": item.get("value"), "source_url": process.get("source_url")} if path else None
        findings.append({"code": f"LLM_{index + 1}", "category": "brief", "severity": "info", "message": str(item.get("message", "")),
                         "citations": [citation] if citation else []})
    return str(response.get("brief") or ""), findings, []


def award_id_for(path: str, record: dict) -> str:
    match = re.match(r"awards\[(\d+)\]", path or "")
    awards = record.get("awards") or []
    index = int(match.group(1)) if match else 0
    return str((awards[index] if index < len(awards) else {}).get("id") or "not published")


def to_filed(finding: dict, process: dict) -> dict:
    return {**finding, "rationale": "; ".join(f"{c['field_path']} = {json.dumps(c['value'], default=str)[:120]}" for c in finding["citations"]),
            "uncertainty": "Based on downloaded records only; coverage is not independently established.",
            "follow_up": "Committee should check the cited fields against the source portal.",
            "sources": [{"ocid": c["ocid"], "award_id": award_id_for(c["field_path"], process["record"]), "field_path": c["field_path"], "source_url": c.get("source_url") or ""} for c in finding["citations"]]}


async def review(run_id: str, ocid: str, mode: str, origin: str = "live") -> dict[str, Any]:
    process = get_process(ocid, origin)
    if not process:
        raise ValueError("Selected process is no longer in the downloaded records")
    planned = plan(run_id, mode, process)
    outputs = run_tools(run_id, ocid, planned, origin)
    limitations = [f"{o['tool']}: {o['reason']}" + (f" Next available check: {o['next_check']}." if o.get("next_check") else "")
                   for o in outputs if o["status"] != "ok"]
    _, skipped = review_tools.plan_tools(mode, process["record"])
    limitations += [f"{item['tool']} not run: {item['reason']}." for item in skipped]
    missing = next((o.get("missing_fields") for o in outputs if o["tool"] == "process_checks"), None)
    if missing:
        limitations.append("Not published: " + ", ".join(missing) + ".")
    drafts = [observation for output in outputs for observation in output["observations"]]
    brief, llm_findings, brief_limits = await write_brief(run_id, process, outputs)
    limitations += brief_limits
    seen = {(d["message"], tuple(sorted(c.get("field_path") or "" for c in d["citations"]))) for d in drafts}
    seen_paths = {key[1] for key in seen}
    llm_findings = [f for f in llm_findings if tuple(sorted(c.get("field_path") or "" for c in f["citations"])) not in seen_paths
                    and f["message"] not in {key[0] for key in seen}]
    kept, dropped = review_tools.validate_findings(drafts + llm_findings, process["record"])
    filed = [to_filed(finding, process) for finding in kept]
    reason = brief or (f"{len(kept)} cited observation(s) retained." if kept else
                       "Cannot assess: " + ("; ".join(limitations) or "no tool produced a cited observation."))
    summary = {"candidate_count": len(drafts) + len(llm_findings), "verified_count": len(kept), "retained_count": len(kept),
               "status": "ok", "reason": reason, "brief": brief, "drops": dropped, "mode": mode}
    append_audit_event(run_id, "agent", "evidence_review", {"mode": mode},
                       {**summary, "findings": filed, "contract": contract_summary(process), "limitations": limitations}, status="ok", origin=origin)
    request_approval(run_id, REPORT_ACTION, "Commit cited findings and prepare the committee investigation file.")
    return {"findings": filed, "dropped_findings": dropped, "limitations": limitations, "review_summary": summary,
            "tender": contract_summary(process), "ocid": ocid}
