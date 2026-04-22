"""Tests for the new enhancement features:

1. Contract test events — verify pipeline logic against reference samples
2. Maturity score — verify scoring dimensions
3. Dashboard extensions — verify custom panels are added to Kibana bundle
4. Validate state — verify drift detection logic
5. Store-to-insight — verify alert → insight-store bridge
"""

import json
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = REPO_ROOT / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import alert_and_diagnose  # noqa: E402
import discover_agent_architecture  # noqa: E402
import render_es_assets  # noqa: E402
import render_instrument_snippet  # noqa: E402
import validate_state  # noqa: E402
from common import ESConfig, read_json  # noqa: E402


CONTRACT_EVENTS_PATH = REPO_ROOT / "references" / "contract_test_events.json"


class ContractTestEvents(unittest.TestCase):
    """Verify that the ingest pipeline logic is consistent with the reference event samples."""

    def setUp(self) -> None:
        self.events = read_json(CONTRACT_EVENTS_PATH)
        self.pipeline = render_es_assets.build_ingest_pipeline(["tool_registry", "model_adapter"])

    def test_contract_events_file_exists_and_is_non_empty(self) -> None:
        self.assertTrue(CONTRACT_EVENTS_PATH.exists())
        self.assertGreater(len(self.events), 0)

    def test_contract_events_are_ecs_native_only(self) -> None:
        """Reference samples should already use ECS / GenAI field names for the 9.x contract."""
        legacy_fields = set()
        for event in self.events:
            for field in event.get("input", {}):
                if field in ("message", "@timestamp", "gen_ai.prompt", "gen_ai.completion",
                             "gen_ai.tool.call.arguments", "gen_ai.tool.call.result"):
                    continue
                if "." not in field and field not in ("message",):
                    legacy_fields.add(field)

        processor_types = {next(iter(proc.keys())) for proc in self.pipeline["processors"]}
        self.assertEqual(legacy_fields, set(), f"Contract samples still contain legacy flat fields: {sorted(legacy_fields)}")
        self.assertNotIn("rename", processor_types)

    def test_pipeline_redacts_sensitive_fields(self) -> None:
        """Ensure all sensitive GenAI fields from sample events have remove processors."""
        sensitive_event = next((e for e in self.events if e.get("_comment", "").startswith("Sensitive")), None)
        self.assertIsNotNone(sensitive_event)
        removed_fields = set()
        for proc in self.pipeline["processors"]:
            if "remove" in proc:
                removed_fields.add(proc["remove"]["field"])
        for field in sensitive_event.get("expected_absent", []):
            self.assertIn(field, removed_fields, f"Sensitive field '{field}' is not removed by pipeline")

    def test_pipeline_sets_observer_product(self) -> None:
        """Every event after pipeline should have observer.product set."""
        set_processors = [p["set"] for p in self.pipeline["processors"] if "set" in p]
        observer_set = [p for p in set_processors if p.get("field") == "observer.product"]
        self.assertTrue(len(observer_set) > 0)
        self.assertEqual(observer_set[0]["value"], "elasticsearch-agent-observability")

    def test_all_expected_fields_have_matching_pipeline_logic(self) -> None:
        """For each event, verify that expected_after_pipeline fields exist in input or are set by pipeline."""
        set_fields = set()
        has_json_parser = False
        for proc in self.pipeline["processors"]:
            if "set" in proc:
                set_fields.add(proc["set"]["field"])
            if "json" in proc:
                has_json_parser = True

        for event in self.events:
            expected = event.get("expected_after_pipeline", {})
            input_fields = set(event.get("input", {}).keys())
            # If pipeline has a JSON parser and input has a "message" field with JSON body,
            # fields inside that JSON body are also reachable.
            json_body_fields: set[str] = set()
            if has_json_parser:
                import json as _json
                msg = event.get("input", {}).get("message", "")
                if isinstance(msg, str) and msg.strip().startswith("{"):
                    try:
                        def _flatten(obj: Any, prefix: str = "") -> set[str]:
                            out: set[str] = set()
                            if isinstance(obj, dict):
                                for k, v in obj.items():
                                    path = f"{prefix}.{k}" if prefix else k
                                    out.add(path)
                                    out |= _flatten(v, path)
                            return out
                        json_body_fields = _flatten(_json.loads(msg))
                    except _json.JSONDecodeError:
                        pass

            for key in expected:
                self.assertTrue(
                    key in input_fields or key in set_fields or key in json_body_fields,
                    f"Expected field '{key}' not found in input, pipeline set, or JSON body fields",
                )


class MaturityScoreTests(unittest.TestCase):
    def test_minimal_workspace_scores_low(self) -> None:
        modules: list[dict[str, Any]] = []
        score = discover_agent_architecture.compute_maturity_score(modules, [], [])
        self.assertLessEqual(score["score"], 25)
        self.assertEqual(score["level"], "minimal")

    def test_basic_workspace_scores_basic(self) -> None:
        modules = [
            {"module_kind": "runtime_entrypoint"},
            {"module_kind": "agent_manifest"},
        ]
        signals = ["runs", "errors", "turns"]
        score = discover_agent_architecture.compute_maturity_score(modules, [], signals)
        self.assertGreaterEqual(score["score"], 15)
        self.assertIn(score["level"], ("basic", "minimal"))

    def test_intermediate_workspace_scores_intermediate(self) -> None:
        modules = [
            {"module_kind": "runtime_entrypoint"},
            {"module_kind": "tool_registry"},
            {"module_kind": "model_adapter"},
            {"module_kind": "otel_sdk_surface"},
        ]
        signals = ["runs", "errors", "tool_calls", "tool_latency", "token_usage", "cost", "otlp_ingest"]
        score = discover_agent_architecture.compute_maturity_score(modules, ["cmd_search"], signals)
        self.assertGreaterEqual(score["score"], 50)
        self.assertIn(score["level"], ("intermediate", "advanced"))

    def test_advanced_workspace_scores_high(self) -> None:
        modules = [
            {"module_kind": "runtime_entrypoint"},
            {"module_kind": "agent_manifest"},
            {"module_kind": "tool_registry"},
            {"module_kind": "model_adapter"},
            {"module_kind": "otel_sdk_surface"},
            {"module_kind": "existing_observability"},
            {"module_kind": "mcp_surface"},
            {"module_kind": "command_surface"},
            {"module_kind": "evaluation_harness"},
            {"module_kind": "memory_store"},
        ]
        signals = ["runs", "errors", "tool_calls", "tool_latency", "tool_errors",
                    "token_usage", "cost", "otlp_ingest", "otel_semantics",
                    "mcp_calls", "command_calls", "evaluation_runs", "cache_hits"]
        handlers = ["cmd_search", "cmd_store", "cmd_get", "cmd_browse", "cmd_list"]
        score = discover_agent_architecture.compute_maturity_score(modules, handlers, signals)
        self.assertGreaterEqual(score["score"], 80)
        self.assertEqual(score["level"], "advanced")

    def test_maturity_score_has_all_dimensions(self) -> None:
        score = discover_agent_architecture.compute_maturity_score([], [], [])
        dims = score["dimensions"]
        self.assertIn("basic_logging", dims)
        self.assertIn("structured_telemetry", dims)
        self.assertIn("genai_instrumentation", dims)
        self.assertIn("operational_readiness", dims)
        self.assertIn("depth_bonus", dims)

    def test_discover_workspace_includes_maturity_score(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            (root / "agent.py").write_text("if __name__ == '__main__':\n    print('ok')\n", encoding="utf-8")
            payload = discover_agent_architecture.discover_workspace(root, max_files=20)
        self.assertIn("maturity_score", payload)
        self.assertIn("score", payload["maturity_score"])
        self.assertIn("level", payload["maturity_score"])


class DashboardExtensionsTests(unittest.TestCase):
    def test_extensions_add_extra_lens_to_kibana_bundle(self) -> None:
        extensions = [
            {"id": "mcp-methods", "field": "gen_ai.agent.mcp_method_name", "aggregation": "terms", "title": "Top MCP Methods"},
        ]
        bundle = render_es_assets.build_kibana_saved_objects("agent-obsv", extensions=extensions)
        lens_ids = bundle["summary"]["lens_ids"]
        self.assertIn("agent-obsv-lens-mcp-methods", lens_ids)
        self.assertGreater(bundle["summary"]["object_count"], 8)

    def test_no_extensions_produces_default_bundle(self) -> None:
        bundle_default = render_es_assets.build_kibana_saved_objects("agent-obsv")
        bundle_none = render_es_assets.build_kibana_saved_objects("agent-obsv", extensions=None)
        self.assertEqual(bundle_default["summary"]["object_count"], bundle_none["summary"]["object_count"])
        self.assertEqual(len(bundle_default["summary"]["lens_ids"]), 8)
        self.assertIn("session_search_id", bundle_default["summary"])

    def test_lens_objects_omit_kibana_saved_object_meta(self) -> None:
        extensions = [
            {"id": "mcp-methods", "field": "gen_ai.agent.mcp_method_name", "aggregation": "terms", "title": "Top MCP Methods"},
        ]
        bundle = render_es_assets.build_kibana_saved_objects("agent-obsv", extensions=extensions)
        lens_objects = [obj for obj in bundle["objects"] if obj.get("type") == "lens"]
        self.assertGreaterEqual(len(lens_objects), 5)
        self.assertTrue(all("kibanaSavedObjectMeta" not in obj["attributes"] for obj in lens_objects))

    def test_sum_extension_creates_xy_chart(self) -> None:
        extensions = [
            {"id": "cost-trend", "field": "gen_ai.agent.cost", "aggregation": "sum", "title": "Cost Over Time"},
        ]
        bundle = render_es_assets.build_kibana_saved_objects("agent-obsv", extensions=extensions)
        custom_lens = [obj for obj in bundle["objects"] if obj.get("id") == "agent-obsv-lens-cost-trend"]
        self.assertEqual(len(custom_lens), 1)
        self.assertEqual(custom_lens[0]["attributes"]["visualizationType"], "lnsXY")

    def test_percentile_extension_creates_metric(self) -> None:
        extensions = [
            {"id": "p99-latency", "field": "event.duration", "aggregation": "percentile", "percentile": 99, "title": "P99 Latency"},
        ]
        bundle = render_es_assets.build_kibana_saved_objects("agent-obsv", extensions=extensions)
        custom_lens = [obj for obj in bundle["objects"] if obj.get("id") == "agent-obsv-lens-p99-latency"]
        self.assertEqual(len(custom_lens), 1)
        self.assertEqual(custom_lens[0]["attributes"]["visualizationType"], "lnsMetric")


class ValidateStateTests(unittest.TestCase):
    def test_deep_compare_identical(self) -> None:
        a = {"key": "value", "nested": {"a": 1}}
        b = {"key": "value", "nested": {"a": 1}}
        diffs = validate_state._deep_compare(a, b)
        self.assertEqual(len(diffs), 0)

    def test_deep_compare_value_mismatch(self) -> None:
        a = {"key": "value1"}
        b = {"key": "value2"}
        diffs = validate_state._deep_compare(a, b)
        self.assertEqual(len(diffs), 1)
        self.assertEqual(diffs[0]["type"], "value_mismatch")

    def test_deep_compare_missing_key(self) -> None:
        a = {"key": "value", "extra": True}
        b = {"key": "value"}
        diffs = validate_state._deep_compare(a, b)
        self.assertEqual(len(diffs), 1)
        self.assertEqual(diffs[0]["type"], "missing_in_remote")

    def test_deep_compare_extra_key_in_remote_is_ignored(self) -> None:
        a = {"key": "value"}
        b = {"key": "value", "extra": True}
        diffs = validate_state._deep_compare(a, b)
        self.assertEqual(len(diffs), 0)

    def test_validate_state_reports_not_found(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            assets_dir = Path(tmp_dir)
            (assets_dir / "ilm-policy.json").write_text('{"policy": {"phases": {}}}', encoding="utf-8")

            def fake_es_request(config, method, path, payload=None):
                from common import SkillError
                raise SkillError("not found")

            with mock.patch.object(validate_state, "es_request", side_effect=fake_es_request):
                report = validate_state.validate_state(
                    ESConfig(es_url="http://localhost:9200"),
                    assets_dir=assets_dir,
                    index_prefix="agent-obsv",
                )
        self.assertEqual(report["not_found"], 1)
        self.assertIn(report["overall_status"], ("incomplete", "not_found"))


class StoreToInsightTests(unittest.TestCase):
    def test_store_to_insight_skips_when_script_not_found(self) -> None:
        """Should not raise, just print warning."""
        alert_and_diagnose._store_to_insight(
            store_script="/nonexistent/store.py",
            result={"alerts": [{"severity": "warning", "alert_type": "test", "phenomenon": "x", "root_cause": "y", "recommendation": "z", "evidence": {}}], "checked_at": "now"},
            es_url="http://localhost:9200",
            es_user="",
            es_password="",
        )

    def test_store_to_insight_calls_subprocess_for_each_alert(self) -> None:
        with tempfile.NamedTemporaryFile(suffix=".py", delete=False, mode="w") as f:
            f.write("# fake store.py\n")
            fake_script = f.name

        result = {
            "checked_at": "2026-04-13T00:00:00Z",
            "alerts": [
                {"severity": "critical", "alert_type": "error_rate_spike", "phenomenon": "High errors", "root_cause": "Tool failure", "recommendation": "Check tool", "evidence": {"rate": 0.5}},
                {"severity": "warning", "alert_type": "latency_degradation", "phenomenon": "Slow", "root_cause": "Timeout", "recommendation": "Profile", "evidence": {"p95": 6000}},
            ],
        }
        calls = []

        def fake_run(cmd, **kwargs):
            calls.append(cmd)
            return mock.MagicMock(returncode=0)

        try:
            with mock.patch("subprocess.run", side_effect=fake_run):
                alert_and_diagnose._store_to_insight(
                    store_script=fake_script,
                    result=result,
                    es_url="http://localhost:9200",
                    es_user="elastic",
                    es_password="pass",
                )
        finally:
            Path(fake_script).unlink(missing_ok=True)

        self.assertEqual(len(calls), 2)
        self.assertIn("store", calls[0])
        self.assertIn("--title", calls[0])


class InstrumentSnippetTests(unittest.TestCase):
    def test_render_snippet_is_valid_python(self) -> None:
        discovery = {
            "detected_modules": [
                {"module_kind": "runtime_entrypoint"},
                {"module_kind": "model_adapter"},
                {"module_kind": "tool_registry"},
            ],
        }
        snippet = render_instrument_snippet.render_instrument_snippet(
            discovery,
            service_name="test-agent",
            environment="test",
            otlp_endpoint="http://127.0.0.1:4317",
            index_prefix="agent-obsv",
        )
        compile(snippet, "instrument-snippet", "exec")

    def test_render_snippet_includes_auto_patch_when_model_adapter(self) -> None:
        discovery = {"detected_modules": [{"module_kind": "model_adapter"}]}
        snippet = render_instrument_snippet.render_instrument_snippet(
            discovery,
            service_name="test",
            environment="dev",
            otlp_endpoint="http://127.0.0.1:4317",
            index_prefix="agent-obsv",
        )
        self.assertIn("_auto_patch", snippet)
        self.assertIn("AGENT_OTEL_AUTO_SETUP", snippet)

    def test_render_snippet_includes_tool_wrapper_when_tool_registry(self) -> None:
        discovery = {"detected_modules": [{"module_kind": "tool_registry"}]}
        snippet = render_instrument_snippet.render_instrument_snippet(
            discovery,
            service_name="test",
            environment="dev",
            otlp_endpoint="http://127.0.0.1:4317",
            index_prefix="agent-obsv",
        )
        self.assertIn("traced_tool_call", snippet)
        self.assertIn("traced_model_call", snippet)

    def test_render_snippet_minimal_without_modules(self) -> None:
        discovery = {"detected_modules": []}
        snippet = render_instrument_snippet.render_instrument_snippet(
            discovery,
            service_name="test",
            environment="dev",
            otlp_endpoint="http://127.0.0.1:4317",
            index_prefix="agent-obsv",
        )
        self.assertNotIn("_auto_patch", snippet)
        self.assertNotIn("traced_tool_call", snippet)
        self.assertIn("setup(", snippet)


class AlertDiagnoseLogicTests(unittest.TestCase):
    def _make_current(self, error_count: int = 0, total: int = 100, p95_ns: float = 0, tokens: float = 0, retries: float = 0, turn_latency_ms: float = 0, top_token_session_key: str = "session-token") -> dict:
        return {
            "aggregations": {
                "error_count": {"doc_count": error_count},
                "total_events": {"value": total},
                "p95_latency": {"values": {"95.0": p95_ns}},
                "token_sum": {"value": tokens * 0.6},
                "token_output_sum": {"value": tokens * 0.4},
                "retry_sum": {"value": retries},
                "top_error_types": {"buckets": [{"key": "TimeoutError", "doc_count": error_count}]},
                "top_error_tools": {"tools": {"buckets": [{"key": "web_search", "doc_count": error_count}]}},
                "top_error_models": {"models": {"buckets": [{"key": "gpt-5", "doc_count": error_count}]}},
                "top_failure_sessions": {"sessions": {"buckets": [{"key": "session-1", "doc_count": max(error_count - 1, 0)}]}},
                "top_failure_components": {"components": {"buckets": [{"key": "tool", "doc_count": max(error_count - 1, 0)}]}},
                "top_token_tools": {"buckets": [{"key": "web_search", "doc_count": 10, "token_sum": {"value": tokens}}]},
                "top_token_models": {"buckets": [{"key": "gpt-5", "doc_count": 10, "token_sum": {"value": tokens}}]},
                # Token-burning session, ranked by token_sum. _analyze_token_anomaly
                # must prefer this over top_retry_sessions; regressing to the
                # retry bucket is what caused the "retry-heavy but cheap session
                # blamed for token spend" bug.
                "top_token_sessions": {"buckets": [{"key": top_token_session_key, "doc_count": 5, "token_sum": {"value": tokens}}]},
                "top_latency_tools": {"buckets": [{"key": "web_search", "doc_count": 10, "p95": {"value": p95_ns}}]},
                "top_retry_sessions": {"buckets": [{"key": "session-retry", "doc_count": 8, "retry_sum": {"value": retries}}]},
                "top_retry_tools": {"buckets": [{"key": "web_search", "doc_count": 8, "retry_sum": {"value": retries}}]},
                "top_turns_by_latency": {
                    "buckets": [
                        {
                            "key": "turn-1",
                            "doc_count": 4,
                            "avg_latency": {"value": turn_latency_ms},
                            "sessions": {"buckets": [{"key": "session-1", "doc_count": 4}]},
                            "components": {"buckets": [{"key": "tool", "doc_count": 4}]},
                            "failure_count": {"doc_count": error_count},
                        }
                    ]
                },
            }
        }

    def _make_baseline(self, error_count: int = 2, total: int = 100, p95_ns: float = 1_000_000_000, tokens: float = 1000, retries: float = 1) -> dict:
        return {
            "aggregations": {
                "error_count": {"doc_count": error_count},
                "total_events": {"value": total},
                "p95_latency": {"values": {"95.0": p95_ns}},
                "token_sum": {"value": tokens * 0.6},
                "token_output_sum": {"value": tokens * 0.4},
                "retry_sum": {"value": retries},
            }
        }

    def test_error_spike_triggers(self) -> None:
        result = alert_and_diagnose._analyze_error_spike(
            self._make_current(error_count=20, total=100),
            self._make_baseline(),
            threshold=10,
        )
        self.assertIsNotNone(result)
        self.assertEqual(result["alert_type"], "error_rate_spike")

    def test_error_spike_not_triggered_below_threshold(self) -> None:
        result = alert_and_diagnose._analyze_error_spike(
            self._make_current(error_count=5, total=100),
            self._make_baseline(),
            threshold=10,
        )
        self.assertIsNone(result)

    def test_token_anomaly_triggers(self) -> None:
        result = alert_and_diagnose._analyze_token_anomaly(
            self._make_current(tokens=10000),
            self._make_baseline(tokens=1000),
            multiplier=3.0,
        )
        self.assertIsNotNone(result)
        self.assertEqual(result["alert_type"], "token_consumption_anomaly")

    def test_token_anomaly_primary_session_comes_from_token_bucket(self) -> None:
        """Regression: `primary_session` must be the top token-consuming session,
        not the top retry session. Retry-heavy but token-cheap sessions being
        blamed for token spend is what motivated the dedicated top_token_sessions
        aggregation.
        """
        current = self._make_current(tokens=10000, top_token_session_key="session-big-spender")
        result = alert_and_diagnose._analyze_token_anomaly(
            current,
            self._make_baseline(tokens=1000),
            multiplier=3.0,
        )
        self.assertIsNotNone(result)
        self.assertIn("session-big-spender", result["root_cause"])
        self.assertEqual(result["evidence"]["primary_session_source"], "token")

    def test_token_anomaly_falls_back_to_retry_session_when_token_bucket_empty(self) -> None:
        """When the token-session aggregation is empty (legacy docs without
        session_id), fall back to the retry-session hot key but flag the
        fallback in evidence so the RCA is honest about the source.
        """
        current = self._make_current(tokens=10000)
        current["aggregations"]["top_token_sessions"] = {"buckets": []}
        result = alert_and_diagnose._analyze_token_anomaly(
            current,
            self._make_baseline(tokens=1000),
            multiplier=3.0,
        )
        self.assertIsNotNone(result)
        self.assertEqual(result["evidence"]["primary_session_source"], "retry_fallback")
        self.assertIn("session-retry", result["root_cause"])

    def test_internal_dataset_filter_excludes_all_internal_streams(self) -> None:
        """Every query against the events stream must filter out internal.*.

        If this regresses to excluding only `internal.sanity_check`, healthy-
        looking clusters whose traffic is dominated by pipeline_verify /
        alert_check / skill_audit heartbeats will mask real breakage.
        """
        must_not = alert_and_diagnose._internal_dataset_filter()
        # Must be a single-prefix filter on event.dataset: internal.
        self.assertEqual(len(must_not), 1)
        clause = must_not[0]
        self.assertEqual(clause.get("prefix", {}).get("event.dataset"), "internal.")

    def test_latency_degradation_triggers(self) -> None:
        result = alert_and_diagnose._analyze_latency_degradation(
            self._make_current(p95_ns=10_000_000_000, turn_latency_ms=7200),  # 10s
            self._make_baseline(p95_ns=1_000_000_000),
            threshold_ms=5000,
        )
        self.assertIsNotNone(result)
        self.assertEqual(result["alert_type"], "latency_degradation")

    def test_session_failure_hotspot_triggers(self) -> None:
        result = alert_and_diagnose._analyze_session_failure_hotspot(
            self._make_current(error_count=12, total=100),
            threshold=10,
        )
        self.assertIsNotNone(result)
        self.assertEqual(result["alert_type"], "session_failure_hotspot")

    def test_retry_storm_triggers(self) -> None:
        result = alert_and_diagnose._analyze_retry_storm(
            self._make_current(retries=12),
            self._make_baseline(retries=2),
            threshold=10,
        )
        self.assertIsNotNone(result)
        self.assertEqual(result["alert_type"], "retry_storm")

    def test_long_turn_hotspot_triggers(self) -> None:
        result = alert_and_diagnose._analyze_long_turn_hotspot(
            self._make_current(turn_latency_ms=4200),
            threshold_ms=5000,
        )
        self.assertIsNotNone(result)
        self.assertEqual(result["alert_type"], "long_turn_hotspot")


class ValidateStateExtendedTests(unittest.TestCase):
    def test_deep_compare_partial_drift(self) -> None:
        a = {"key1": "match", "key2": "local_val"}
        b = {"key1": "match", "key2": "remote_val"}
        diffs = validate_state._deep_compare(a, b)
        self.assertEqual(len(diffs), 1)
        self.assertEqual(diffs[0]["path"], "key2")
        self.assertEqual(diffs[0]["type"], "value_mismatch")

    def test_deep_compare_nested_partial_drift(self) -> None:
        a = {"outer": {"inner": "local"}}
        b = {"outer": {"inner": "remote", "extra": True}}
        diffs = validate_state._deep_compare(a, b)
        self.assertEqual(len(diffs), 1)
        self.assertEqual(diffs[0]["path"], "outer.inner")

    def test_validate_state_all_in_sync(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            assets_dir = Path(tmp_dir)
            (assets_dir / "ilm-policy.json").write_text('{"policy": {"phases": {"hot": {}}}}', encoding="utf-8")

            def fake_es_request(config, method, path, payload=None):
                return {"agent-obsv-lifecycle": {"policy": {"phases": {"hot": {}, "_meta": {"managed": True}}}}}

            with mock.patch.object(validate_state, "es_request", side_effect=fake_es_request):
                report = validate_state.validate_state(
                    ESConfig(es_url="http://localhost:9200"),
                    assets_dir=assets_dir,
                    index_prefix="agent-obsv",
                )
        self.assertEqual(report["overall_status"], "in_sync")


if __name__ == "__main__":
    unittest.main()
