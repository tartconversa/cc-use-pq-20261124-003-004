from __future__ import annotations

import unittest

from silverwatch import cases, evidence, queries, review, rounds, workqueue
from silverwatch.contracts import CaseEvidence, Remediation, ReviewAppeal, ReviewDecision
from silverwatch.errors import Problem
from silverwatch.ledger import current_cut
from tests.support import (DatabaseCase, decision_data, evidence_data, instant,
                          resolve_case, seed_case)


def appeal_data(**updates):
    value = {"appeal_id": "appeal-one", "case_id": "case-one",
             "requested_by": "resident-one",
             "reason": "A new payment voucher contradicts the earlier not-substantiated decision.",
             "submitted_at": "2026-11-05T09:00:00Z",
             "new_evidence_refs": ["voucher-two"], "change_summary": "New signed contract and voucher."}
    return value | updates


def round_decision(round_no, role, reviewer, finding, *, decided_at=None, refs=None,
                   decision_id=None):
    return decision_data(role, reviewer, finding,
        decision_id=decision_id or f"decision-r{round_no}-{role}",
        decided_at=decided_at or f"2026-11-{8 + round_no:02d}T10:00:00Z",
        evidence_refs=refs or ["evidence-one"], round=round_no)


def seed_rejected_and_closed(db):
    seed_case(db)
    review.decide(db, ReviewDecision.model_validate(decision_data(
        "investigator", "reviewer-one", "not_substantiated")))
    review.decide(db, ReviewDecision.model_validate(decision_data(
        "compliance", "reviewer-two", "not_substantiated")))
    review.close(db, "case-one", "not_substantiated", 1)


class AppealLifecycleTests(DatabaseCase, unittest.TestCase):
    def test_appeal_opens_a_second_round_but_does_not_change_effective_finding(self):
        with self.transaction() as db:
            seed_rejected_and_closed(db)
            result = review.open_appeal(db, ReviewAppeal.model_validate(appeal_data()))
            self.assertEqual(result["round"], 2)
            finding = cases.finding(db, "case-one")
            self.assertEqual(finding["finding"], "not_substantiated")
            self.assertEqual(finding["effective_round"], 1)
            self.assertEqual(finding["open_round"], 2)
            self.assertEqual(finding["round_count"], 2)

    def test_appeal_requires_a_completed_first_round(self):
        with self.transaction() as db:
            seed_case(db)
            review.decide(db, ReviewDecision.model_validate(decision_data()))
            with self.assertRaises(Problem) as captured:
                review.open_appeal(db, ReviewAppeal.model_validate(appeal_data()))
            self.assertEqual(captured.exception.code, "review_round_open")

    def test_appeal_before_report_time_is_rejected(self):
        with self.transaction() as db:
            seed_rejected_and_closed(db)
            body = ReviewAppeal.model_validate(appeal_data(
                submitted_at="2026-10-01T00:00:00Z"))
            with self.assertRaises(Problem) as captured:
                review.open_appeal(db, body)
            self.assertEqual(captured.exception.code, "appeal_time")

    def test_appeal_replay_is_idempotent_and_changed_content_is_rejected(self):
        with self.transaction() as db:
            seed_rejected_and_closed(db)
            body = ReviewAppeal.model_validate(appeal_data())
            first = review.open_appeal(db, body)
            second = review.open_appeal(db, body)
            self.assertTrue(second["replayed"])
            self.assertEqual(first["digest"], second["digest"])
            self.assertEqual(len(rounds.appeals(db, "case-one")), 1)
            changed = ReviewAppeal.model_validate(appeal_data(reason="Different claim."))
            with self.assertRaises(Problem) as captured:
                review.open_appeal(db, changed)
            self.assertEqual(captured.exception.code, "appeal_changed")

    def test_round_two_decisions_require_an_open_appeal_round(self):
        with self.transaction() as db:
            seed_rejected_and_closed(db)
            body = ReviewDecision.model_validate(round_decision(2, "investigator",
                "reviewer-three", "substantiated", decided_at="2026-11-10T10:00:00Z"))
            with self.assertRaises(Problem) as captured:
                cases.submit_decision(db, body)
            self.assertEqual(captured.exception.code, "review_round_missing")

    def test_decision_must_come_after_the_appeal_it_belongs_to(self):
        with self.transaction() as db:
            seed_rejected_and_closed(db)
            review.open_appeal(db, ReviewAppeal.model_validate(appeal_data()))
            body = ReviewDecision.model_validate(round_decision(2, "investigator",
                "reviewer-three", "substantiated", decided_at="2026-11-04T10:00:00Z"))
            with self.assertRaises(Problem) as captured:
                cases.submit_decision(db, body)
            self.assertEqual(captured.exception.code, "decision_time")

    def test_round_two_keeps_two_role_slots_sealed_and_requires_independent_reviewers(self):
        with self.transaction() as db:
            seed_rejected_and_closed(db)
            review.open_appeal(db, ReviewAppeal.model_validate(appeal_data()))
            first = ReviewDecision.model_validate(round_decision(2, "investigator",
                "reviewer-three", "substantiated"))
            cases.submit_decision(db, first)
            sealed = ReviewDecision.model_validate(round_decision(2, "investigator",
                "reviewer-three", "not_substantiated",
                decision_id="decision-r2-investigator-other"))
            with self.assertRaises(Problem) as captured:
                cases.submit_decision(db, sealed)
            self.assertEqual(captured.exception.code, "decision_sealed")
            same_person = ReviewDecision.model_validate(round_decision(2, "compliance",
                "reviewer-three", "substantiated"))
            with self.assertRaises(Problem) as other:
                cases.submit_decision(db, same_person)
            self.assertEqual(other.exception.code, "reviewer_not_independent")

    def test_round_one_reviewer_may_serve_again_in_a_later_round(self):
        with self.transaction() as db:
            seed_rejected_and_closed(db)
            review.open_appeal(db, ReviewAppeal.model_validate(appeal_data()))
            cases.submit_decision(db, ReviewDecision.model_validate(round_decision(
                2, "investigator", "reviewer-one", "substantiated")))
            cases.submit_decision(db, ReviewDecision.model_validate(round_decision(
                2, "compliance", "reviewer-four", "substantiated",
                decision_id="decision-r2-compliance")))
            finding = cases.finding(db, "case-one")
            self.assertEqual(finding["finding"], "substantiated")
            self.assertEqual(finding["effective_round"], 2)

    def test_identical_round_two_decision_replays_without_duplicate_event(self):
        with self.transaction() as db:
            seed_rejected_and_closed(db)
            review.open_appeal(db, ReviewAppeal.model_validate(appeal_data()))
            body = ReviewDecision.model_validate(round_decision(2, "investigator",
                "reviewer-three", "substantiated"))
            first = cases.submit_decision(db, body)
            second = cases.submit_decision(db, body)
            self.assertFalse(first["replayed"])
            self.assertTrue(second["replayed"])
            self.assertEqual(len(cases.decisions(db, "case-one")), 3)


class ReopenAndRemediationTests(DatabaseCase, unittest.TestCase):
    def reopen_with_substantiated_round(self, db):
        seed_rejected_and_closed(db)
        review.open_appeal(db, ReviewAppeal.model_validate(appeal_data()))
        evidence.add(db, CaseEvidence.model_validate(evidence_data(
            evidence_id="voucher-two", evidence_type="receipt",
            source_ref="archive://voucher-2", content_digest="b" * 64,
            observed_at="2026-11-03T10:00:00Z")))
        cases.submit_decision(db, ReviewDecision.model_validate(round_decision(
            2, "investigator", "reviewer-three", "substantiated",
            refs=["voucher-two"], decision_id="decision-r2-investigator")))
        cases.submit_decision(db, ReviewDecision.model_validate(round_decision(
            2, "compliance", "reviewer-four", "substantiated",
            refs=["voucher-two"], decision_id="decision-r2-compliance")))

    def test_changed_conclusion_reopens_closed_case_with_a_new_management_event(self):
        with self.transaction() as db:
            self.reopen_with_substantiated_round(db)
            status = review.case_status(db, "case-one")
            self.assertEqual(status["case"]["status"], "investigating")
            self.assertEqual(status["finding"]["finding"], "substantiated")
            timeline = queries.case_timeline(db, "case-one")
            management = [row for row in timeline["revisions"]
                          if row["source_ref"] == "case-management"]
            self.assertEqual([row["payload"]["status"] for row in management],
                             ["closed", "investigating"])

    def test_reopened_substantiated_case_gains_remediation_eligibility(self):
        with self.transaction() as db:
            self.reopen_with_substantiated_round(db)
            action = Remediation.model_validate({
                "action_id": "fix-two", "case_id": "case-one", "provider_id": "provider-one",
                "owner": "operator-one", "due_at": "2026-12-20T00:00:00Z",
                "action": "Refund the disputed payment and correct the contract.",
                "evidence_ref": None, "verified_by": None, "verified_at": None,
                "status": "planned"})
            saved = cases.add_remediation(db, action)
            self.assertIn("seq", saved)
            stored = next(item for item in cases.current_remediations(db)
                          if item["action_id"] == "fix-two")
            self.assertEqual(stored["status"], "planned")

    def test_reopened_case_cannot_close_until_new_remediation_is_verified(self):
        with self.transaction() as db:
            self.reopen_with_substantiated_round(db)
            with self.assertRaises(Problem) as captured:
                review.close(db, "case-one", "substantiated", 3)
            self.assertEqual(captured.exception.code, "remediation_open")
            cases.add_remediation(db, Remediation.model_validate({
                "action_id": "fix-two", "case_id": "case-one", "provider_id": "provider-one",
                "owner": "operator-one", "due_at": "2026-12-20T00:00:00Z",
                "action": "Refund the disputed payment.", "evidence_ref": "archive://refund",
                "verified_by": "auditor-one", "verified_at": "2026-12-10T00:00:00Z",
                "status": "verified"}))
            closed = review.close(db, "case-one", "substantiated", 3)
            self.assertIn("seq", closed)
            self.assertEqual(review.case_status(db, "case-one")["case"]["status"], "closed")

    def test_close_is_blocked_while_a_round_is_still_open(self):
        with self.transaction() as db:
            seed_case(db)
            resolve_case(db, first="substantiated", second="substantiated")
            review.open_appeal(db, ReviewAppeal.model_validate(appeal_data(
                submitted_at="2026-11-05T09:00:00Z")))
            cases.add_remediation(db, Remediation.model_validate({
                "action_id": "fix-one", "case_id": "case-one", "provider_id": "provider-one",
                "owner": "operator-one", "due_at": "2026-10-20T00:00:00Z",
                "action": "Correct the notice.", "evidence_ref": "archive://proof",
                "verified_by": "auditor-one", "verified_at": "2026-10-19T00:00:00Z",
                "status": "verified"}))
            with self.assertRaises(Problem) as captured:
                review.close(db, "case-one", "substantiated", 1)
            self.assertEqual(captured.exception.code, "review_round_open")

    def test_unchanged_conclusion_keeps_the_closed_case_closed(self):
        with self.transaction() as db:
            seed_rejected_and_closed(db)
            review.open_appeal(db, ReviewAppeal.model_validate(appeal_data()))
            cases.submit_decision(db, ReviewDecision.model_validate(round_decision(
                2, "investigator", "reviewer-three", "not_substantiated",
                decision_id="decision-r2-investigator")))
            cases.submit_decision(db, ReviewDecision.model_validate(round_decision(
                2, "compliance", "reviewer-four", "not_substantiated",
                decision_id="decision-r2-compliance")))
            status = review.case_status(db, "case-one")
            self.assertEqual(status["case"]["status"], "closed")
            self.assertEqual(status["finding"]["finding"], "not_substantiated")


class HistoricalCutTests(DatabaseCase, unittest.TestCase):
    def test_historical_cut_preserves_the_first_round_conclusion(self):
        with self.transaction() as db:
            seed_rejected_and_closed(db)
            cut_after_round_one = current_cut(db)
            review.open_appeal(db, ReviewAppeal.model_validate(appeal_data()))
            evidence.add(db, CaseEvidence.model_validate(evidence_data(
                evidence_id="voucher-two", evidence_type="receipt",
                source_ref="archive://voucher-2", content_digest="b" * 64,
                observed_at="2026-11-03T10:00:00Z")))
            cases.submit_decision(db, ReviewDecision.model_validate(round_decision(
                2, "investigator", "reviewer-three", "substantiated",
                refs=["voucher-two"], decision_id="decision-r2-investigator")))
            cases.submit_decision(db, ReviewDecision.model_validate(round_decision(
                2, "compliance", "reviewer-four", "substantiated",
                refs=["voucher-two"], decision_id="decision-r2-compliance")))
            historical = cases.finding(db, "case-one", cut=cut_after_round_one)
            self.assertEqual(historical["finding"], "not_substantiated")
            self.assertEqual(historical["effective_round"], 1)
            current = cases.finding(db, "case-one")
            self.assertEqual(current["finding"], "substantiated")
            self.assertEqual(current["effective_round"], 2)

    def test_time_cut_shows_pending_while_round_two_is_decided_later(self):
        with self.transaction() as db:
            seed_rejected_and_closed(db)
            review.open_appeal(db, ReviewAppeal.model_validate(appeal_data()))
            cases.submit_decision(db, ReviewDecision.model_validate(round_decision(
                2, "investigator", "reviewer-three", "substantiated",
                decided_at="2026-11-10T10:00:00Z",
                decision_id="decision-r2-investigator")))
            midpoint = cases.finding(db, "case-one", as_of=instant("2026-11-09T00:00:00Z"))
            self.assertEqual(midpoint["finding"], "not_substantiated")
            self.assertEqual(midpoint["effective_round"], 1)
            self.assertEqual(midpoint["open_round"], 2)

    def test_review_history_explains_each_round_and_the_material_that_changed_it(self):
        with self.transaction() as db:
            seed_rejected_and_closed(db)
            review.open_appeal(db, ReviewAppeal.model_validate(appeal_data()))
            evidence.add(db, CaseEvidence.model_validate(evidence_data(
                evidence_id="voucher-two", evidence_type="receipt",
                source_ref="archive://voucher-2", content_digest="b" * 64,
                observed_at="2026-11-03T10:00:00Z")))
            cases.submit_decision(db, ReviewDecision.model_validate(round_decision(
                2, "investigator", "reviewer-three", "substantiated",
                refs=["voucher-two"], decision_id="decision-r2-investigator")))
            cases.submit_decision(db, ReviewDecision.model_validate(round_decision(
                2, "compliance", "reviewer-four", "substantiated",
                refs=["voucher-two"], decision_id="decision-r2-compliance")))
            view = review.review_history(db, "case-one")
            self.assertEqual([row["round"] for row in view["rounds"]], [1, 2])
            self.assertEqual(view["rounds"][0]["finding"], "not_substantiated")
            self.assertEqual(view["rounds"][1]["finding"], "substantiated")
            change = view["rounds"][1]["finding_change"]
            self.assertEqual(change, {"from": "not_substantiated",
                                      "to": "substantiated", "changed": True})
            self.assertEqual(view["rounds"][1]["cited_evidence"], ["voucher-two"])
            self.assertEqual(view["rounds"][1]["appeal"]["appeal_id"], "appeal-one")
            self.assertEqual(view["effective_finding"], "substantiated")


class QueueVisibilityTests(DatabaseCase, unittest.TestCase):
    def test_closed_case_returns_to_the_queue_while_appeal_round_is_open(self):
        with self.transaction() as db:
            seed_rejected_and_closed(db)
            review.open_appeal(db, ReviewAppeal.model_validate(appeal_data()))
            queue = workqueue.investigation_queue(db, as_of=instant("2026-11-06T00:00:00Z"))
            self.assertEqual(queue["count"], 1)
            row = queue["items"][0]
            self.assertEqual(row["case_id"], "case-one")
            self.assertEqual(row["open_round"], 2)
            self.assertTrue(row["under_review"])
            self.assertIn("appeal_round_open", row["reasons"])

    def test_completed_round_two_reopened_case_remains_queued_until_reclosed(self):
        with self.transaction() as db:
            seed_rejected_and_closed(db)
            review.open_appeal(db, ReviewAppeal.model_validate(appeal_data()))
            evidence.add(db, CaseEvidence.model_validate(evidence_data(
                evidence_id="voucher-two", evidence_type="receipt",
                source_ref="archive://voucher-2", content_digest="b" * 64,
                observed_at="2026-11-03T10:00:00Z")))
            for role, reviewer in (("investigator", "reviewer-three"),
                                   ("compliance", "reviewer-four")):
                cases.submit_decision(db, ReviewDecision.model_validate(round_decision(
                    2, role, reviewer, "substantiated", refs=["voucher-two"],
                    decision_id=f"decision-r2-{role}")))
            queue = workqueue.investigation_queue(db, as_of=instant("2026-11-20T00:00:00Z"))
            self.assertEqual(queue["count"], 1)
            self.assertIsNone(queue["items"][0]["open_round"])
            self.assertFalse(queue["items"][0]["under_review"])

    def test_closed_case_without_appeal_stays_outside_the_queue(self):
        with self.transaction() as db:
            seed_rejected_and_closed(db)
            queue = workqueue.investigation_queue(db, as_of=instant("2026-11-06T00:00:00Z"))
            self.assertEqual(queue["count"], 0)


class AppealHttpTests(DatabaseCase, unittest.TestCase):
    def setUp(self):
        super().setUp()
        from fastapi.testclient import TestClient

        from silverwatch.api import create_app

        self.client = TestClient(create_app(self.store.location))
        with self.transaction() as db:
            seed_rejected_and_closed(db)

    def test_appeal_and_review_history_routes_preserve_each_round(self):
        response = self.client.post("/cases/case-one/appeals", json=appeal_data())
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["round"], 2)
        history = self.client.get("/cases/case-one/reviews")
        self.assertEqual(history.status_code, 200)
        body = history.json()
        self.assertEqual([row["round"] for row in body["rounds"]], [1, 2])
        self.assertEqual(body["effective_finding"], "not_substantiated")

    def test_appeal_route_rejects_path_body_scope_mismatch(self):
        payload = appeal_data(case_id="other-case")
        response = self.client.post("/cases/case-one/appeals", json=payload)
        self.assertEqual(response.status_code, 422)
        self.assertEqual(response.json()["code"], "case_scope")

    def test_unknown_case_appeal_returns_not_found(self):
        response = self.client.post("/cases/missing-case/appeals",
                                    json=appeal_data(case_id="missing-case"))
        self.assertEqual(response.status_code, 404)


class MultiRoundTests(DatabaseCase, unittest.TestCase):
    def _decide_round(self, db, round_no, finding, reviewers):
        for role, reviewer in reviewers:
            cases.submit_decision(db, ReviewDecision.model_validate(round_decision(
                round_no, role, reviewer, finding,
                decided_at=f"2026-11-{15 + round_no:02d}T10:00:00Z",
                decision_id=f"d{round_no}-{role}")))

    def test_three_rounds_track_each_conclusion_and_only_the_last_is_effective(self):
        with self.transaction() as db:
            seed_rejected_and_closed(db)
            review.open_appeal(db, ReviewAppeal.model_validate(appeal_data()))
            self._decide_round(db, 2, "not_substantiated",
                               [("investigator", "r3"), ("compliance", "r4")])
            review.open_appeal(db, ReviewAppeal.model_validate(appeal_data(
                appeal_id="appeal-two", submitted_at="2026-11-15T09:00:00Z",
                reason="A further recording surfaces.")))
            self._decide_round(db, 3, "substantiated",
                               [("investigator", "r5"), ("compliance", "r6")])
            finding = cases.finding(db, "case-one")
            self.assertEqual(finding["finding"], "substantiated")
            self.assertEqual(finding["effective_round"], 3)
            self.assertEqual(finding["round_count"], 3)
            view = review.review_history(db, "case-one")
            self.assertEqual([row["finding"] for row in view["rounds"]],
                             ["not_substantiated", "not_substantiated", "substantiated"])
            self.assertEqual(view["rounds"][2]["finding_change"]["changed"], True)
            self.assertEqual(review.case_status(db, "case-one")["case"]["status"],
                             "investigating")

    def test_appeals_must_advance_in_time_across_rounds(self):
        with self.transaction() as db:
            seed_rejected_and_closed(db)
            review.open_appeal(db, ReviewAppeal.model_validate(appeal_data()))
            self._decide_round(db, 2, "not_substantiated",
                               [("investigator", "r3"), ("compliance", "r4")])
            early = ReviewAppeal.model_validate(appeal_data(
                appeal_id="appeal-two", submitted_at="2026-11-01T00:00:00Z"))
            with self.assertRaises(Problem) as captured:
                review.open_appeal(db, early)
            self.assertEqual(captured.exception.code, "appeal_time")


class AppealBundleTests(DatabaseCase, unittest.TestCase):
    def test_appeal_can_enter_through_the_batch_importer(self):
        from silverwatch import intake

        with self.transaction() as db:
            seed_rejected_and_closed(db)
            bundle = {"batch_id": "bundle-appeal", "source_ref": "appeal-desk", "revision": 1,
                      "records": [{"kind": "appeals", "data": appeal_data()}]}
            result = intake.import_bundle(db, bundle)
            self.assertEqual(result["accepted_count"], 1)
            self.assertEqual(rounds.current_round_no(db, "case-one"), 2)
            replay = intake.import_bundle(db, bundle)
            self.assertEqual(replay["replayed_count"], 1)


if __name__ == "__main__":
    unittest.main()
