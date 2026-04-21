"""Alert correlation, confidence scoring, and skill self-audit."""

import sys
import unittest
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = REPO_ROOT / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import alert_and_diagnose  # noqa: E402
import common  # noqa: E402
from common import ESConfig, SkillError  # noqa: E402


def _alert(alert_type: str, severity: str = "warning", **evidence):
    return {
        "alert_type": alert_type,
        "severity": severity,
        "phenomenon": f"{alert_type} happened",
        "root_cause": "test",
        "recommendation": "test",
        "evidence": evidence,
    }


class ConfidenceTests(unittest.TestCase):
    def test_bare_threshold_hit_gets_baseline_score(self) -> None:
        alert = _alert("error_rate_spike", error_rate=0.2, baseline_rate=0.18)
        score = alert_and_diagnose._confidence(alert)
        # Small deviation + no breadth => close to the 0.3 floor.
        self.assertGreaterEqual(score, 0.3)
        self.assertLess(score, 0.6)

    def test_large_deviation_plus_breadth_scores_higher(self) -> None:
        alert = _alert(
            "error_rate_spike",
            error_rate=0.5,
            baseline_rate=0.05,  # 9x deviation
            top_error_tools=[{"key": "search"}],
            top_error_models=[{"key": "gpt"}],
            top_failure_sessions=[{"key": "s1"}],
        )
        score = alert_and_diagnose._confidence(alert)
        self.assertGreater(score, 0.7)

    def test_score_capped_at_one(self) -> None:
        alert = _alert(
            "latency_degradation",
            p95_ms=50000,
            threshold_ms=1000,  # 49x over threshold
            ratio=100,
            concentration=0.9,
            top_latency_tools=[{"key": "t"}],
            top_turns=[{"key": "turn-1"}],
        )
        self.assertLessEqual(alert_and_diagnose._confidence(alert), 1.0)


class CorrelationTests(unittest.TestCase):
    def test_single_alert_yields_no_chain(self) -> None:
        alerts = [_alert("error_rate_spike", top_error_tools=[{"key": "search"}])]
        chains = alert_and_diagnose._correlate_alerts(alerts)
        self.assertEqual(chains, [])

    def test_disjoint_alerts_yield_no_chain(self) -> None:
        alerts = [
            _alert("error_rate_spike", top_error_tools=[{"key": "search"}]),
            _alert("latency_degradation", top_latency_tools=[{"key": "upload"}]),
        ]
        chains = alert_and_diagnose._correlate_alerts(alerts)
        self.assertEqual(chains, [])

    def test_shared_tool_chains_alerts(self) -> None:
        alerts = [
            _alert("error_rate_spike", top_error_tools=[{"key": "search"}]),
            _alert("latency_degradation", top_latency_tools=[{"key": "search"}]),
        ]
        for a in alerts:
            a["confidence"] = 0.6
        chains = alert_and_diagnose._correlate_alerts(alerts)
        self.assertEqual(len(chains), 1)
        self.assertIn("search", chains[0]["shared_entities"].get("tool", []))
        self.assertIn("error_rate_spike", chains[0]["members"])
        self.assertIn("latency_degradation", chains[0]["members"])

    def test_transitive_union_via_shared_session(self) -> None:
        """retry_storm(session=s1,tool=A) <-> token_anomaly(session=s1,tool=B) <-> latency(tool=B)."""
        alerts = [
            _alert("retry_storm", top_retry_sessions=[{"key": "s1"}], top_retry_tools=[{"key": "A"}]),
            _alert(
                "token_consumption_anomaly",
                top_retry_sessions=[{"key": "s1"}],
                top_tools=[{"key": "B"}],
            ),
            _alert("latency_degradation", top_latency_tools=[{"key": "B"}]),
        ]
        for a in alerts:
            a["confidence"] = 0.5
        chains = alert_and_diagnose._correlate_alerts(alerts)
        # All three share transitive connection => one chain containing all.
        self.assertEqual(len(chains), 1)
        self.assertEqual(len(chains[0]["members"]), 3)

    def test_severity_ordered_in_chain(self) -> None:
        alerts = [
            _alert("latency_degradation", severity="warning", top_latency_tools=[{"key": "x"}]),
            _alert("error_rate_spike", severity="critical", top_error_tools=[{"key": "x"}]),
        ]
        for a in alerts:
            a["confidence"] = 0.5
        chains = alert_and_diagnose._correlate_alerts(alerts)
        # Critical comes first regardless of input order.
        self.assertEqual(chains[0]["members"][0], "error_rate_spike")

    def test_chain_confidence_is_max_member_confidence(self) -> None:
        alerts = [
            _alert("error_rate_spike", top_error_tools=[{"key": "x"}]),
            _alert("latency_degradation", top_latency_tools=[{"key": "x"}]),
        ]
        alerts[0]["confidence"] = 0.4
        alerts[1]["confidence"] = 0.85
        chains = alert_and_diagnose._correlate_alerts(alerts)
        self.assertEqual(chains[0]["confidence"], 0.85)


class SkillAuditTests(unittest.TestCase):
    def test_audit_writes_to_events_data_stream_with_create(self) -> None:
        captured: dict = {}

        def fake_es(config, method, path, payload=None):
            captured["method"] = method
            captured["path"] = path
            captured["payload"] = payload
            return {"result": "created"}

        with mock.patch.object(common, "es_request", side_effect=fake_es):
            ok = common.emit_skill_audit(
                ESConfig(es_url="http://x"),
                index_prefix="agent-obsv",
                tool_name="doctor",
                verdict="healthy",
                duration_ms=42,
                inputs={"otlp_http_endpoint": "http://127.0.0.1:14319"},
                evidence={"healthz": "pass", "canary": "pass"},
            )
        self.assertTrue(ok)
        # Data-stream contract: must use _create, not _doc.
        self.assertEqual(captured["method"], "POST")
        self.assertTrue(captured["path"].endswith("/agent-obsv-events/_create"))
        doc = captured["payload"]
        # Must carry internal.* dataset so aggregations filter it out.
        self.assertEqual(doc["event.dataset"], common.SKILL_AUDIT_DATASET)
        self.assertEqual(doc["gen_ai.agent.tool_name"], "doctor")
        self.assertEqual(doc["skill.verdict"], "healthy")
        self.assertEqual(doc["skill.duration_ms"], 42)
        self.assertEqual(doc["skill.evidence"], {"healthz": "pass", "canary": "pass"})

    def test_audit_failure_is_swallowed(self) -> None:
        """ES down must not raise back into the caller — audit is best-effort."""
        with mock.patch.object(common, "es_request", side_effect=SkillError("boom")):
            ok = common.emit_skill_audit(
                ESConfig(es_url="http://x"),
                index_prefix="agent-obsv",
                tool_name="doctor",
                verdict="broken",
            )
        self.assertFalse(ok)

    def test_verdict_maps_to_event_outcome(self) -> None:
        outcomes: dict = {}

        def fake_es(config, method, path, payload=None):
            outcomes[payload["skill.verdict"]] = payload["event.outcome"]
            return {"result": "created"}

        with mock.patch.object(common, "es_request", side_effect=fake_es):
            for verdict in ["healthy", "ok", "broken", "degraded", "alert"]:
                common.emit_skill_audit(
                    ESConfig(es_url="http://x"),
                    index_prefix="agent-obsv",
                    tool_name="t",
                    verdict=verdict,
                )
        self.assertEqual(outcomes["healthy"], "success")
        self.assertEqual(outcomes["ok"], "success")
        self.assertEqual(outcomes["broken"], "failure")
        self.assertEqual(outcomes["degraded"], "failure")
        self.assertEqual(outcomes["alert"], "failure")


if __name__ == "__main__":
    unittest.main()
