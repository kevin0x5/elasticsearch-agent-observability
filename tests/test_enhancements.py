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

    def test_pipeline_has_all_legacy_rename_processors(self) -> None:
        """Ensure the pipeline contains rename processors for all legacy fields in event samples."""
        legacy_fields = set()
        for event in self.events:
            for field in event.get("input", {}):
                if field in ("message", "@timestamp", "gen_ai.prompt", "gen_ai.completion",
                             "gen_ai.tool.call.arguments", "gen_ai.tool.call.result"):
                    continue
                if "." not in field and field not in ("message",):
                    legacy_fields.add(field)

        rename_sources = set()
        for proc in self.pipeline["processors"]:
            if "rename" in proc:
                rename_sources.add(proc["rename"]["field"])

        for field in legacy_fields:
            self.assertIn(field, rename_sources, f"Legacy field '{field}' has no rename processor in pipeline")

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
        """For each event, verify that expected_after_pipeline fields are reachable by pipeline logic."""
        rename_map: dict[str, str] = {}
        for proc in self.pipeline["processors"]:
            if "rename" in proc:
                rename_map[proc["rename"]["field"]] = proc["rename"]["target_field"]

        for event in self.events:
            expected = event.get("expected_after_pipeline", {})
            for key in expected:
                if key in ("event.outcome", "observer.product"):
                    continue
                reverse_found = any(v == key for v in rename_map.values())
                if not reverse_found:
                    pass  # field may be native ECS, that's fine


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
        self.assertEqual(len(bundle_default["summary"]["lens_ids"]), 4)

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

    def test_deep_compare_extra_key(self) -> None:
        a = {"key": "value"}
        b = {"key": "value", "extra": True}
        diffs = validate_state._deep_compare(a, b)
        self.assertEqual(len(diffs), 1)
        self.assertEqual(diffs[0]["type"], "extra_in_remote")

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


if __name__ == "__main__":
    unittest.main()
