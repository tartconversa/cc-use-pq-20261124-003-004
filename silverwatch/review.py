from __future__ import annotations

from datetime import datetime, timezone
from sqlite3 import Connection

from silverwatch import cases, rounds
from silverwatch.contracts import Case, CaseEvidence, ReviewAppeal, ReviewDecision
from silverwatch.errors import conflict, missing
from silverwatch.ledger import append, current_entities


def open_case(db: Connection, body: Case, revision: int, source_ref: str) -> dict:
    return cases.create(db, body, source_ref, revision)


def add_evidence(db: Connection, body: CaseEvidence) -> dict:
    from silverwatch.evidence import add

    return add(db, body)


def open_appeal(db: Connection, body: ReviewAppeal) -> dict:
    return rounds.open_appeal(db, body)


def decide(db: Connection, body: ReviewDecision) -> dict:
    return cases.submit_decision(db, body)


def review_history(db: Connection, case_id: str, *, cut: int | None = None) -> dict:
    case = next((row for row in current_entities(db, "case", cut=cut)
                 if row["entity_id"] == case_id), None)
    if case is None:
        raise missing("case", case_id)
    history = rounds.rounds(db, case_id, cut=cut)
    effective = rounds.effective_round(db, case_id, cut=cut)
    from silverwatch.evidence import for_case

    material = for_case(db, case_id, cut=cut)
    views = []
    for row in history:
        cited = sorted({ref for item in row["decisions"] for ref in item["evidence_refs"]})
        previous_finding = views[-1]["finding"] if views else None
        change = None
        if row["status"] == "complete" and previous_finding is not None:
            change = {"from": previous_finding, "to": row["finding"],
                      "changed": previous_finding != row["finding"]}
        views.append({
            "round": row["round"], "status": row["status"], "appeal": row["appeal"],
            "decisions": row["decisions"], "finding": row["finding"],
            "cited_evidence": cited, "finding_change": change,
        })
    return {"case_id": case_id, "case_status": case["payload"]["status"],
            "rounds": views,
            "effective_round": None if effective is None else effective["round"],
            "effective_finding": None if effective is None else effective["finding"],
            "evidence": material,
            "case_digest": case["digest"], "cut": cut}


def case_status(db: Connection, case_id: str, *, cut: int | None = None) -> dict:
    row = next((value for value in current_entities(db, "case", cut=cut)
                if value["entity_id"] == case_id), None)
    if row is None:
        raise missing("case", case_id)
    finding = cases.finding(db, case_id, cut=cut)
    remediations = [value for value in cases.current_remediations(db, cut=cut)
                    if value["case_id"] == case_id]
    from silverwatch.evidence import for_case

    material = for_case(db, case_id, cut=cut)
    return {
        "case": row["payload"], "finding": finding,
        "evidence": material, "remediations": remediations,
        "case_digest": row["digest"],
        "review_rounds": rounds.rounds(db, case_id, cut=cut),
        "may_close": (row["payload"]["status"] == "closed"
                      and finding["finding"] in {"substantiated", "not_substantiated"}
                      and all(value["status"] == "verified" for value in remediations)),
    }


def close(db: Connection, case_id: str, expected_finding: str, revision: int) -> dict:
    state = case_status(db, case_id)
    if state["finding"]["finding"] != expected_finding:
        raise conflict("finding_changed", "案件核实结果已变化",
                       {"current": state["finding"]["finding"]})
    if state["finding"]["open_round"] is not None:
        raise conflict("review_round_open", "仍有复核轮次未完成，不能关闭案件")
    if state["finding"]["finding"] == "substantiated":
        if not state["remediations"] or any(item["status"] != "verified" for item in state["remediations"]):
            raise conflict("remediation_open", "已确认案件仍有未核验整改")
    body = {**state["case"], "status": "closed"}
    observed = datetime.now(timezone.utc)
    from silverwatch.ledger import history

    next_revision = 1 + max(
        (row["revision"] for row in history(db, "case", case_id)
         if row["source_ref"] == "case-management"),
        default=0)
    if revision != next_revision:
        raise conflict("revision_out_of_order", "案件管理修订号不连续",
                       {"expected": next_revision, "submitted": revision})
    return append(db, "case", case_id, next_revision, "case-management", body, observed)


def cases_for_provider(db: Connection, provider_id: str, *, cut: int | None = None) -> list[dict]:
    return [case_status(db, row["entity_id"], cut=cut)
            for row in current_entities(db, "case", cut=cut)
            if row["payload"]["provider_id"] == provider_id]


def remediation_queue(db: Connection, *, at: datetime | None = None) -> dict:
    today = at or datetime.now(timezone.utc)
    rows = cases.current_remediations(db)
    return {
        "overdue": [row for row in rows if row["status"] not in {"verified", "rejected"}
                    and datetime.fromisoformat(row["due_at"]) < today],
        "awaiting_verification": [row for row in rows if row["status"] == "submitted"],
        "verified": [row for row in rows if row["status"] == "verified"],
    }
