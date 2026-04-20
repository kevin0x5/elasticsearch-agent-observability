"""Verify pipeline script tests (transport mocked, ES mocked)."""

import argparse
import sys
import unittest
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = REPO_ROOT / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import verify_pipeline  # noqa: E402


def _make_args(**overrides) -> argparse.Namespace:
    defaults = dict(
        es_url="http://localhost:9200",
        es_user="",
        es_password="",
        index_prefix="agent-obsv",
        otlp_http_endpoint="http://127.0.0.1:14319",
        service_name="pipeline-verify",
        poll_attempts=2,
        poll_backoff=0.01,
        no_verify_tls=False,
        collector_log="",
        output=None,
    )
    defaults.update(overrides)
    return argparse.Namespace(**defaults)


class VerifyPipelineTests(unittest.TestCase):
    def test_verdict_ok_when_send_and_poll_succeed(self) -> None:
        args = _make_args()

        def fake_send(endpoint, payload, timeout=10):
            return {"ok": True, "status_code": 200, "url": endpoint + "/v1/logs"}

        captured = {}

        def fake_es_request(config, method, path, payload=None):
            # The verify script queries `/<glob>/_search` — capture canary id from query.
            term = payload["query"]["term"]
            canary_id = term["gen_ai.agent.verify_id"]
            captured["canary_id"] = canary_id
            return {
                "hits": {
                    "hits": [
                        {
                            "_index": "agent-obsv-events-000001",
                            "_id": "doc-1",
                            "_source": {
                                "event.dataset": verify_pipeline.CANARY_DATASET,
                                "service.name": "pipeline-verify",
                                "gen_ai.agent.verify_id": canary_id,
                            },
                        }
                    ]
                }
            }

        with mock.patch.object(verify_pipeline, "_send_canary", side_effect=fake_send):
            with mock.patch.object(verify_pipeline, "es_request", side_effect=fake_es_request):
                result = verify_pipeline.run_verify(args)

        self.assertEqual(result["verdict"], "ok")
        self.assertEqual(result["poll"]["found"], True)
        self.assertEqual(result["canary_id"], captured["canary_id"])

    def test_verdict_sent_but_lost_when_poll_returns_no_hits(self) -> None:
        args = _make_args()

        def fake_send(endpoint, payload, timeout=10):
            return {"ok": True, "status_code": 200, "url": endpoint + "/v1/logs"}

        def fake_es_request(config, method, path, payload=None):
            return {"hits": {"hits": []}}

        with mock.patch.object(verify_pipeline, "_send_canary", side_effect=fake_send):
            with mock.patch.object(verify_pipeline, "es_request", side_effect=fake_es_request):
                result = verify_pipeline.run_verify(args)

        self.assertEqual(result["verdict"], "sent_but_lost")
        self.assertIn("OTLP HTTP bridge", result["next_step"])
        self.assertIn("14319", result["next_step"])

    def test_verdict_transport_unreachable_when_url_error(self) -> None:
        args = _make_args()

        def fake_send(endpoint, payload, timeout=10):
            return {"ok": False, "status_code": None, "url": endpoint + "/v1/logs", "detail": "Connection refused"}

        with mock.patch.object(verify_pipeline, "_send_canary", side_effect=fake_send):
            # ES should not be called at all when transport never succeeded.
            with mock.patch.object(verify_pipeline, "es_request", side_effect=AssertionError("should not be called")):
                result = verify_pipeline.run_verify(args)

        self.assertEqual(result["verdict"], "transport_unreachable")
        self.assertIn("listening", result["next_step"])

    def test_verdict_transport_rejected_when_http_error(self) -> None:
        args = _make_args()

        def fake_send(endpoint, payload, timeout=10):
            return {"ok": False, "status_code": 404, "url": endpoint + "/v1/logs", "detail": "not found"}

        with mock.patch.object(verify_pipeline, "_send_canary", side_effect=fake_send):
            with mock.patch.object(verify_pipeline, "es_request", side_effect=AssertionError("should not be called")):
                result = verify_pipeline.run_verify(args)

        self.assertEqual(result["verdict"], "transport_rejected")
        self.assertIn("404", result["next_step"])

    def test_verdict_contract_broken_when_fields_missing(self) -> None:
        args = _make_args()

        def fake_send(endpoint, payload, timeout=10):
            return {"ok": True, "status_code": 200, "url": endpoint + "/v1/logs"}

        def fake_es_request(config, method, path, payload=None):
            canary_id = payload["query"]["term"]["gen_ai.agent.verify_id"]
            return {
                "hits": {
                    "hits": [
                        {
                            "_index": "agent-obsv-events-000001",
                            "_id": "doc-x",
                            "_source": {
                                # Intentionally missing event.dataset and service.name
                                "gen_ai.agent.verify_id": canary_id,
                            },
                        }
                    ]
                }
            }

        with mock.patch.object(verify_pipeline, "_send_canary", side_effect=fake_send):
            with mock.patch.object(verify_pipeline, "es_request", side_effect=fake_es_request):
                result = verify_pipeline.run_verify(args)

        self.assertEqual(result["verdict"], "contract_broken")
        self.assertIn("event.dataset", result["next_step"])


if __name__ == "__main__":
    unittest.main()
