import sys
import tempfile
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = REPO_ROOT / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import bootstrap_observability  # noqa: E402
import common  # noqa: E402
import generate_report  # noqa: E402
import render_collector_config  # noqa: E402
import render_elastic_agent_assets  # noqa: E402
import render_es_assets  # noqa: E402


DISCOVERY_SAMPLE = {
    "files_scanned": 400,
    "detected_modules": [
        {"module_kind": "tool_registry"},
        {"module_kind": "model_adapter"},
    ],
    "recommended_ingest_modes": [
        {"mode": "collector", "score": 0.94},
        {"mode": "apm-otlp-hybrid", "score": 0.88},
    ],
}


class ContractsAndSecurityTests(unittest.TestCase):
    def test_render_config_uses_env_placeholders_by_default(self) -> None:
        rendered = render_collector_config.render_config(
            DISCOVERY_SAMPLE,
            es_url="http://localhost:9200",
            index_prefix="agent-obsv",
            environment="dev",
            service_name="agent-runtime",
            es_user="elastic",
            es_password="super-secret",
        )
        self.assertIn("${env:ELASTICSEARCH_USERNAME}", rendered)
        self.assertIn("${env:ELASTICSEARCH_PASSWORD}", rendered)
        self.assertNotIn("super-secret", rendered)

    def test_render_config_embeds_credentials_only_when_explicit(self) -> None:
        rendered = render_collector_config.render_config(
            DISCOVERY_SAMPLE,
            es_url="http://localhost:9200",
            index_prefix="agent-obsv",
            environment="dev",
            service_name="agent-runtime",
            es_user="elastic",
            es_password="super-secret",
            embed_credentials=True,
        )
        self.assertIn("super-secret", rendered)
        self.assertIn('"agent-obsv-events"', rendered)

    def test_render_config_includes_metrics_pipeline(self) -> None:
        rendered = render_collector_config.render_config(
            DISCOVERY_SAMPLE,
            es_url="http://localhost:9200",
            index_prefix="agent-obsv",
            environment="dev",
            service_name="agent-runtime",
        )
        self.assertIn("metrics:", rendered)
        self.assertIn("spanmetrics", rendered)
        self.assertIn("elasticsearch/metrics", rendered)

    def test_render_config_uses_conservative_spanmetrics_shape(self) -> None:
        rendered = render_collector_config.render_config(
            DISCOVERY_SAMPLE,
            es_url="http://localhost:9200",
            index_prefix="agent-obsv",
            environment="dev",
            service_name="agent-runtime",
        )
        self.assertIn("connectors:\n  spanmetrics:\n    dimensions:", rendered)
        self.assertNotIn("histogram:", rendered)

    def test_spanmetrics_dimensions_are_normalized(self) -> None:
        normalized = render_collector_config._normalize_spanmetrics_dimensions(
            ["service.name", " event.outcome ", "service.name", "", "gen_ai.agent.tool_name"]
        )
        self.assertEqual(normalized, ["service.name", "event.outcome", "gen_ai.agent.tool_name"])

    def test_render_config_supports_filelog_receiver(self) -> None:
        rendered = render_collector_config.render_config(
            DISCOVERY_SAMPLE,
            es_url="http://localhost:9200",
            index_prefix="agent-obsv",
            environment="dev",
            service_name="agent-runtime",
            enable_filelog=True,
            filelog_path="/tmp/agent.log",
        )
        self.assertIn("filelog", rendered)
        self.assertIn("/tmp/agent.log", rendered)

    def test_render_config_supports_probabilistic_sampling(self) -> None:
        rendered = render_collector_config.render_config(
            DISCOVERY_SAMPLE,
            es_url="http://localhost:9200",
            index_prefix="agent-obsv",
            environment="dev",
            service_name="agent-runtime",
            sampling_ratio=0.5,
        )
        self.assertIn("probabilistic_sampler", rendered)
        self.assertIn("50.0", rendered)

    def test_render_config_sets_explicit_telemetry_metrics_port(self) -> None:
        rendered = render_collector_config.render_config(
            DISCOVERY_SAMPLE,
            es_url="http://localhost:9200",
            index_prefix="agent-obsv",
            environment="dev",
            service_name="agent-runtime",
            telemetry_metrics_port=18888,
        )
        self.assertIn('address: "127.0.0.1:18888"', rendered)

    def test_render_config_forces_ecs_mapping_mode(self) -> None:
        rendered = render_collector_config.render_config(
            DISCOVERY_SAMPLE,
            es_url="http://localhost:9200",
            index_prefix="agent-obsv",
            environment="dev",
            service_name="agent-runtime",
        )
        self.assertIn('set(attributes["elastic.mapping.mode"], "ecs")', rendered)
        self.assertIn("transform/elastic_mapping", rendered)

    def test_index_template_uses_data_streams(self) -> None:
        template = render_es_assets.build_index_template("agent-obsv", ["tool_registry"])
        self.assertIn("data_stream", template)
        self.assertEqual(template["index_patterns"], ["agent-obsv-events*"])
        self.assertEqual(len(template["composed_of"]), 2)

    def test_component_template_ecs_base_has_ecs_fields(self) -> None:
        component = render_es_assets.build_component_template_ecs_base("agent-obsv")
        props = component["template"]["mappings"]["properties"]
        self.assertIn("@timestamp", props)
        self.assertIn("event.outcome", props)
        self.assertIn("service.name", props)
        self.assertIn("trace.id", props)
        self.assertIn("gen_ai.usage.input_tokens", props)
        self.assertIn("gen_ai.agent.tool_name", props)
        self.assertNotIn("captured_at", props)
        self.assertIn("gen_ai.agent.component_type", props)
        self.assertIn("gen_ai.guardrail.action", props)
        self.assertIn("gen_ai.guardrail.category", props)
        self.assertIn("gen_ai.evaluation.score", props)
        self.assertIn("gen_ai.evaluation.outcome", props)
        self.assertIn("gen_ai.agent.retrieval_latency_ms", props)
        self.assertIn("gen_ai.agent.cache_hit", props)

    def test_ilm_policy_has_tiered_phases(self) -> None:
        ilm = render_es_assets.build_ilm_policy(30)
        phases = ilm["policy"]["phases"]
        self.assertIn("hot", phases)
        self.assertIn("warm", phases)
        self.assertIn("cold", phases)
        self.assertNotIn("frozen", phases)
        self.assertIn("delete", phases)

    def test_ingest_pipeline_is_ecs_native_and_structured_parsing(self) -> None:
        pipeline = render_es_assets.build_ingest_pipeline(["tool_registry"])
        processor_types = []
        for item in pipeline["processors"]:
            processor_types.extend(item.keys())
        self.assertNotIn("rename", processor_types)
        self.assertIn("json", processor_types)
        self.assertIn("script", processor_types)
        set_processors = [item["set"] for item in pipeline["processors"] if "set" in item]
        self.assertTrue(any(proc.get("field") == "@timestamp" for proc in set_processors))

    def test_kibana_objects_include_lens_no_paid_features(self) -> None:
        bundle = render_es_assets.build_kibana_saved_objects("agent-obsv")
        types = {obj["type"] for obj in bundle["objects"]}
        self.assertIn("lens", types)
        self.assertIn("dashboard", types)
        self.assertNotIn("alert", types)
        self.assertIn("search", types)
        self.assertIn("index-pattern", types)
        self.assertGreaterEqual(bundle["summary"]["object_count"], 7)
        self.assertNotIn("alert_ids", bundle["summary"])

    def test_kibana_objects_keep_meta_only_for_search_and_dashboard(self) -> None:
        bundle = render_es_assets.build_kibana_saved_objects("agent-obsv")
        for obj in bundle["objects"]:
            attributes = obj.get("attributes", {})
            if obj["type"] in {"search", "dashboard"}:
                self.assertIn("kibanaSavedObjectMeta", attributes)
            if obj["type"] == "lens":
                self.assertNotIn("kibanaSavedObjectMeta", attributes)

    def test_lens_objects_use_indexpattern_state_contract(self) -> None:
        bundle = render_es_assets.build_kibana_saved_objects("agent-obsv")
        lens_objects = [obj for obj in bundle["objects"] if obj["type"] == "lens"]
        self.assertGreaterEqual(len(lens_objects), 4)
        for obj in lens_objects:
            state = obj["attributes"]["state"]
            self.assertIn("datasourceStates", state)
            self.assertIn("indexpattern", state["datasourceStates"])
            self.assertIn("visualization", state)
            self.assertIn("filters", state)
            self.assertIn("query", state)
            self.assertNotIn("formBased", state["datasourceStates"])
            references = {ref["name"] for ref in obj["references"]}
            self.assertIn("indexpattern-datasource-current-indexpattern", references)
            self.assertIn("indexpattern-datasource-layer-layer1", references)

    def test_report_config_uses_data_stream_and_timestamp(self) -> None:
        report_config = render_es_assets.build_report_config("agent-obsv", DISCOVERY_SAMPLE)
        self.assertEqual(report_config["time_field"], "@timestamp")
        self.assertIn("data_stream", report_config)
        self.assertEqual(report_config["data_stream"], "agent-obsv-events")
        self.assertIn("dashboard_id", report_config["kibana"])
        self.assertIn("p50_latency_ms", report_config["metrics"])
        self.assertIn("p95_latency_ms", report_config["metrics"])
        self.assertNotIn("p50_latency_ns", report_config["metrics"])

    def test_native_preflight_manifest_does_not_store_sensitive_tokens(self) -> None:
        surface_manifest = render_elastic_agent_assets.build_surface_manifest(
            service_name="agent-runtime",
            environment="dev",
            apm_server_url="https://apm.example.com:8200",
            kibana_url="https://kibana.example.com",
            ingest_mode="elastic-agent-fleet",
        )
        manifest = render_elastic_agent_assets.build_preflight_manifest(
            DISCOVERY_SAMPLE,
            ingest_mode="elastic-agent-fleet",
            service_name="agent-runtime",
            environment="dev",
            fleet_server_url="https://fleet.example.com:8220",
            fleet_enrollment_token="super-secret-token",
            apm_server_url="",
            kibana_url="https://kibana.example.com",
            otlp_endpoint="http://127.0.0.1:4317",
            surface_manifest=surface_manifest,
        )
        self.assertEqual(manifest["overall_status"], "ready")
        self.assertNotIn("super-secret-token", str(manifest))
        checks = {item["key"]: item for item in manifest["checks"]}
        self.assertEqual(checks["fleet_enrollment_token"]["status"], "ready")
        self.assertEqual(checks["apm_server_url"]["status"], "skipped")

    def test_collect_summary_notes_reports_truncation_and_auth_mode(self) -> None:
        notes = bootstrap_observability.collect_summary_notes(
            {"files_scanned": 400, "detected_modules": [], "recommended_ingest_modes": [{"mode": "collector", "score": 0.94}]},
            max_files=400,
            auth_mode="env",
            index_prefix="agent-obsv",
            ingest_mode="collector",
            bridge_bind_host="127.0.0.1",
            bridge_http_port=14319,
            dry_run=True,
        )
        self.assertTrue(any("--max-files" in note for note in notes))
        self.assertTrue(any("credentials were not written to disk" in note for note in notes))
        self.assertTrue(any("restart the Collector process" in note for note in notes))
        self.assertTrue(any("agent-obsv-events" in note for note in notes))
        self.assertTrue(any("Selected ingest mode" in note for note in notes))
        self.assertTrue(any("otelcol-contrib" in note for note in notes))
        self.assertTrue(any("--telemetry-metrics-port" in note for note in notes))
        self.assertTrue(any("logs_index" in note and "traces_index" in note and "metrics_index" in note for note in notes))
        self.assertTrue(any("agent-obsv-events" in note and "agent-obsv-metrics" in note for note in notes))
        self.assertTrue(any("mapping.allowed_modes" in note for note in notes))
        self.assertTrue(any("Collector → Elasticsearch exporter" in note for note in notes))
        self.assertTrue(any("http://127.0.0.1:14319" in note and "logs` and `traces" in note for note in notes))
        self.assertTrue(any("metrics on the Collector path" in note for note in notes))
        self.assertTrue(any("Dry-run mode" in note for note in notes))

    def test_collect_summary_notes_clarify_sanity_check_scope(self) -> None:
        notes = bootstrap_observability.collect_summary_notes(
            {"files_scanned": 10, "detected_modules": [{"module_kind": "runtime_entrypoint"}], "recommended_ingest_modes": []},
            max_files=400,
            auth_mode="none",
            index_prefix="agent-obsv",
            ingest_mode="collector",
            bridge_bind_host="127.0.0.1",
            bridge_http_port=14319,
        )
        self.assertFalse(any("sanity check" in note for note in notes))

    def test_search_payload_uses_ecs_fields_by_default(self) -> None:
        payload = generate_report.search_payload("now-24h")
        self.assertIn("@timestamp", payload["query"]["range"])
        self.assertIn("gen_ai.agent.tool_name", str(payload["aggs"]))

    def test_search_payload_uses_custom_time_field(self) -> None:
        payload = generate_report.search_payload("now-24h", time_field="event.ingested")
        self.assertIn("event.ingested", payload["query"]["range"])
        self.assertNotIn("@timestamp", payload["query"]["range"])

    def test_iter_text_files_ignores_generated_reference_and_test_dirs(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            (root / "app").mkdir()
            (root / "generated").mkdir()
            (root / "references").mkdir()
            (root / "tests").mkdir()
            (root / "app" / "agent.py").write_text("print('ok')\n", encoding="utf-8")
            (root / "generated" / "noise.py").write_text("print('noise')\n", encoding="utf-8")
            (root / "references" / "design.md").write_text("workflow orchestrator\n", encoding="utf-8")
            (root / "tests" / "test_app.py").write_text("def test_it(): pass\n", encoding="utf-8")
            files = common.iter_text_files(root, max_files=20)
        relative = {path.relative_to(root).as_posix() for path in files}
        self.assertEqual(relative, {"app/agent.py"})

    def test_report_latency_converts_ns_to_ms(self) -> None:
        """Verify that build_report properly converts event.duration (ns) to ms."""
        mock_result = {
            "hits": {"total": {"value": 100}},
            "aggregations": {
                "with_errors": {"doc_count": 5},
                "tool_calls": {"doc_count": 50},
                "tool_errors": {"doc_count": 2},
                "latency_percentiles": {"values": {"50.0": 500_000_000, "95.0": 2_000_000_000}},
                "retry_sum": {"value": 3},
                "token_input_sum": {"value": 1000},
                "token_output_sum": {"value": 500},
                "cost_sum": {"value": 0.5},
                "top_sessions": {"buckets": [{"key": "session-1", "doc_count": 12}]},
                "failed_sessions": {"sessions": {"buckets": [{"key": "session-1", "doc_count": 4}]}},
                "slow_turns": {
                    "buckets": [
                        {
                            "key": "turn-1",
                            "doc_count": 3,
                            "avg_latency": {"value": 3210.0},
                            "sessions": {"buckets": [{"key": "session-1", "doc_count": 3}]},
                            "failure_count": {"doc_count": 1},
                        }
                    ]
                },
                "top_components": {"buckets": [{"key": "tool", "doc_count": 40}]},
                "failed_components": {"components": {"buckets": [{"key": "tool", "doc_count": 2}]}},
                "top_tools": {"buckets": []},
                "top_models": {"buckets": []},
                "mcp_methods": {"buckets": []},
                "error_types": {"buckets": []},
            },
        }
        report = generate_report.build_report(mock_result)
        self.assertEqual(report["p50_latency_ms"], 500.0)
        self.assertEqual(report["p95_latency_ms"], 2000.0)
        self.assertEqual(report["top_sessions"][0]["key"], "session-1")
        self.assertEqual(report["slow_turns"][0]["avg_latency_ms"], 3210.0)
        self.assertEqual(report["failed_components"][0]["key"], "tool")

    def test_esconfig_has_verify_tls_and_kibana_api_key(self) -> None:
        config = common.ESConfig(es_url="http://localhost:9200")
        self.assertTrue(config.verify_tls)
        self.assertIsNone(config.kibana_api_key)
        config2 = common.ESConfig(es_url="http://localhost:9200", verify_tls=False, kibana_api_key="test-key")
        self.assertFalse(config2.verify_tls)
        self.assertEqual(config2.kibana_api_key, "test-key")


if __name__ == "__main__":
    unittest.main()
