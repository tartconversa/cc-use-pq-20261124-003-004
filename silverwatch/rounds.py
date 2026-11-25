from __future__ import annotations

from datetime import datetime
from sqlite3 import Connection

from silverwatch.contracts import ReviewAppeal
from silverwatch.digest import digest
from silverwatch.errors import conflict, invalid, missing
from silverwatch.ledger import append, current_entities, history, latest, row_value

ROUND_ROLES = ("investigator", "compliance")


def _case_row(db: Connection, case_id: str, *, cut: int | None = None,
              as_of: datetime | None = None) -> dict:
    row = next((row for row in current_entities(db, "case", cut=cut, as_of=as_of)
                if row["entity_id"] == case_id), None)
    if row is None:
        raise missing("case", case_id)
    return row


def appeals(db: Connection, case_id: str, *, cut: int | None = None,
            as_of: datetime | None = None) -> list[dict]:
    _case_row(db, case_id, cut=cut, as_of=as_of)
    return [row["payload"] | {"event_seq": row["seq"], "digest": row["digest"]}
            for row in latest(db, "review_round", cut=cut, as_of=as_of)
            if row["entity_id"] == case_id]


def _find_appeal(db: Connection, appeal_id: str) -> dict | None:
    rows = db.execute(
        "SELECT * FROM event WHERE event_type='review_round' AND source_ref=? ORDER BY seq",
        (f"appeal:{appeal_id}",)).fetchall()
    if not rows:
        return None
    return row_value(rows[-1])


def open_appeal(db: Connection, body: ReviewAppeal) -> dict:
    case = _case_row(db, body.case_id)
    if body.submitted_at < datetime.fromisoformat(case["payload"]["reported_at"]):
        raise invalid("appeal_time", "申诉时间早于线索上报时间")
    existing = _find_appeal(db, body.appeal_id)
    payload = body.model_dump(mode="json")
    fingerprint = digest(payload)
    if existing:
        if existing["entity_id"] != body.case_id or digest(existing["payload"]) != fingerprint:
            raise conflict("appeal_changed", "申诉编号已对应其他内容")
        return {**payload, "round": existing["revision"] + 1,
                "digest": existing["digest"], "replayed": True}
    history = _rounds(db, body.case_id)
    previous = history[-1]
    if previous["status"] != "complete":
        raise conflict("review_round_open", "上一轮复核尚未完成，不能开启新一轮")
    if previous["appeal"] is not None and \
            previous["appeal"]["submitted_at"] >= body.submitted_at.isoformat():
        raise invalid("appeal_time", "新一轮申诉必须晚于上一轮申诉")
    round_no = previous["round"] + 1
    result = append(db, "review_round", body.case_id, round_no - 1,
                    f"appeal:{body.appeal_id}", payload, body.submitted_at)
    return {**payload, "round": round_no, **result}


def decisions_by_round(db: Connection, case_id: str, *, cut: int | None = None,
                       as_of: datetime | None = None) -> dict[int, list[dict]]:
    _case_row(db, case_id, cut=cut, as_of=as_of)
    grouped: dict[int, list[dict]] = {}
    for row in latest(db, "decision", cut=cut, as_of=as_of):
        if row["entity_id"] != case_id:
            continue
        payload = row["payload"]
        round_no = payload.get("round", 1)
        grouped.setdefault(round_no, []).append(
            payload | {"event_seq": row["seq"], "digest": row["digest"]})
    for rows in grouped.values():
        rows.sort(key=lambda item: (item["role"], item["decided_at"]))
    return grouped


def _combine(rows: list[dict]) -> str:
    by_role = {row["role"]: row for row in rows}
    if set(by_role) != set(ROUND_ROLES):
        return "pending"
    if by_role["investigator"]["finding"] != by_role["compliance"]["finding"]:
        return "inconclusive"
    return by_role["investigator"]["finding"]


def _rounds(db: Connection, case_id: str, *, cut: int | None = None,
            as_of: datetime | None = None) -> list[dict]:
    appeal_rows = appeals(db, case_id, cut=cut, as_of=as_of)
    grouped = decisions_by_round(db, case_id, cut=cut, as_of=as_of)
    result = []
    total = max([1, *grouped.keys()], default=1)
    total = max(total, 1 + len(appeal_rows))
    appeal_by_round = {2 + index: row for index, row in enumerate(appeal_rows)}
    for round_no in range(1, total + 1):
        rows = grouped.get(round_no, [])
        appeal = appeal_by_round.get(round_no)
        result.append({
            "round": round_no,
            "status": "complete" if len({row["role"] for row in rows}) == len(ROUND_ROLES) else "open",
            "appeal": None if appeal is None else {key: value for key, value in appeal.items()
                                                   if key not in {"event_seq", "digest"}},
            "decisions": rows,
            "finding": _combine(rows),
        })
    return result


def rounds(db: Connection, case_id: str, *, cut: int | None = None,
           as_of: datetime | None = None) -> list[dict]:
    return _rounds(db, case_id, cut=cut, as_of=as_of)


def effective_round(db: Connection, case_id: str, *, cut: int | None = None,
                    as_of: datetime | None = None) -> dict | None:
    completed = [row for row in _rounds(db, case_id, cut=cut, as_of=as_of)
                 if row["status"] == "complete"]
    return completed[-1] if completed else None


def current_round_no(db: Connection, case_id: str, *, cut: int | None = None,
                     as_of: datetime | None = None) -> int:
    return _rounds(db, case_id, cut=cut, as_of=as_of)[-1]["round"]


def reopen_case(db: Connection, case_id: str, previous_finding: str,
                new_finding: str, observed_at: datetime) -> dict | None:
    case = _case_row(db, case_id)
    if case["payload"]["status"] != "closed":
        return None
    if previous_finding == new_finding:
        return None
    body = {**case["payload"], "status": "investigating"}
    revision = 1 + max(
        (row["revision"] for row in history(db, "case", case_id)
         if row["source_ref"] == "case-management"),
        default=0)
    return append(db, "case", case_id, revision, "case-management", body, observed_at)
