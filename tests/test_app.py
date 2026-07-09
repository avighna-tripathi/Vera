from __future__ import annotations

import unittest

from fastapi.testclient import TestClient

from vera_bot.app import app, context_store
from vera_bot.services.composer import Composer
from vera_bot.services.context_resolver import ResolvedContexts


class FakeRefiner:
    enabled = True

    def refine(self, resolved, draft_body, draft_cta, draft_rationale):
        class Result:
            body = draft_body + " Refined."
            cta = draft_cta
            rationale = draft_rationale + " Refined."

        return Result()


class AppTests(unittest.TestCase):
    def setUp(self) -> None:
        self.client = TestClient(app)

    def test_healthz(self) -> None:
        response = self.client.get("/v1/healthz")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["status"], "ok")

    def test_context_and_tick_flow(self) -> None:
        category = {
            "slug": "dentists",
            "display_name": "Dentists",
            "voice": {"tone": "peer_clinical"},
            "digest": [
                {
                    "id": "d_2026W17_jida_fluoride",
                    "title": "3-month fluoride recall outperforms 6-month",
                    "source": "JIDA Oct 2026, p.14",
                    "trial_n": 2100,
                    "patient_segment": "high_risk_adults",
                    "summary": "38% lower caries recurrence in high-risk adults.",
                }
            ],
        }
        merchant = {
            "merchant_id": "m_001_drmeera_dentist_delhi",
            "category_slug": "dentists",
            "identity": {"name": "Dr. Meera's Dental Clinic", "owner_first_name": "Meera"},
            "offers": [{"title": "Dental Cleaning @ ₹299", "status": "active"}],
            "signals": ["engaged_in_last_48h"],
            "conversation_history": [],
        }
        trigger = {
            "id": "trg_001_research_digest_dentists",
            "scope": "merchant",
            "kind": "research_digest",
            "merchant_id": "m_001_drmeera_dentist_delhi",
            "payload": {"top_item_id": "d_2026W17_jida_fluoride"},
            "urgency": 2,
            "suppression_key": "research:dentists:2026-W17",
            "expires_at": "2099-01-01T00:00:00Z",
        }
        for scope, context_id, payload in [
            ("category", "dentists", category),
            ("merchant", merchant["merchant_id"], merchant),
            ("trigger", trigger["id"], trigger),
        ]:
            response = self.client.post(
                "/v1/context",
                json={"scope": scope, "context_id": context_id, "version": 1, "payload": payload, "delivered_at": "2026-01-01T00:00:00Z"},
            )
            self.assertEqual(response.status_code, 200)
            self.assertTrue(response.json()["accepted"])

        response = self.client.post("/v1/tick", json={"now": "2026-04-26T10:35:00Z", "available_triggers": [trigger["id"]]})
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(len(data["actions"]), 1)
        self.assertIn("JIDA", data["actions"][0]["body"])

    def test_reply_opt_out(self) -> None:
        response = self.client.post(
            "/v1/reply",
            json={
                "conversation_id": "conv_test",
                "merchant_id": "m_001_drmeera_dentist_delhi",
                "customer_id": None,
                "from_role": "merchant",
                "message": "Stop messaging me.",
                "received_at": "2026-04-26T10:42:00Z",
                "turn_number": 2,
            },
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["action"], "end")

    def test_composer_can_use_refiner(self) -> None:
        resolved = ResolvedContexts(
            category={"slug": "dentists", "voice": {}},
            merchant={"merchant_id": "m1", "identity": {"name": "Clinic", "owner_first_name": "Meera"}, "offers": []},
            trigger={"id": "t1", "scope": "merchant", "kind": "curious_ask_due", "suppression_key": "x", "payload": {}},
            customer=None,
        )
        composed = Composer(refiner=FakeRefiner()).compose(resolved)
        self.assertIn("Refined.", composed.body)


if __name__ == "__main__":
    unittest.main()
