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
        self.assertIn("captured_at", props)
        self.assertEqual(props["captured_at"]["type"], "alias")

    def test_ilm_policy_has_tiered_phases(self) -> None:
        ilm = render_es_assets.build_ilm_policy(30)
        phases = ilm["policy"]["phases"]
        self.assertIn("hot", phases)
        self.assertIn("warm", phases)
        self.assertIn("cold", phases)
        self.assertIn("frozen", phases)
        self.assertIn("delete", phases)

    def test_ingest_pipeline_does_ecs_rename_and_structured_parsing(self) -> None:
        pipeline = render_es_assets.build_ingest_pipeline(["tool_registry"])
        processor_types = []
        for item in pipeline["processors"]:
            processor_types.extend(item.keys())
        self.assertIn("rename", processor_types)
        self.assertIn("json", processor_types)
        self.assertIn("script", processor_types)
        set_processors = [item["set"] for item in pipeline["processors"] if "set" in item]
        self.assertTrue(any(proc.get("field") == "@timestamp" for proc in set_processors))

    def test_kibana_objects_include_lens_and_alert(self) -> None:
        bundle = render_es_assets.build_kibana_saved_objects("agent-obsv")
        types = {obj["type"] for obj in bundle["objects"]}
        self.assertIn("lens", types)
        self.assertIn("dashboard", types)
        self.assertIn("alert", types)
        self.assertIn("search", types)
        self.assertIn("index-pattern", types)
        self.assertGreaterEqual(bundle["summary"]["object_count"], 8)

    def test_report_config_uses_data_stream_and_timestamp(self) -> None:
        report_config = render_es_assets.build_report_config("agent-obsv", DISCOVERY_SAMPLE)
        self.assertEqual(report_config["time_field"], "@timestamp")
        self.assertIn("data_stream", report_config)
        self.assertEqual(report_config["data_stream"], "agent-obsv-events")
        self.assertIn("dashboard_id", report_config["kibana"])

    def test_collect_summary_notes_reports_truncation_and_auth_mode(self) -> None:
        notes = bootstrap_observability.collect_summary_notes(
            {"files_scanned": 400, "detected_modules": [], "recommended_ingest_modes": [{"mode": "collector", "score": 0.94}]},
            max_files=400,
            auth_mode="env",
            index_prefix="agent-obsv",
            ingest_mode="collector",
        )
        self.assertTrue(any("--max-files" in note for note in notes))
        self.assertTrue(any("credentials were not written to disk" in note for note in notes))
        self.assertTrue(any("agent-obsv-events" in note for note in notes))
        self.assertTrue(any("Selected ingest mode" in note for note in notes))

    def test_search_payload_uses_custom_time_field(self) -> None:
        payload = generate_report.search_payload("now-24h", time_field="event.ingested")
        self.assertIn("event.ingested", payload["query"]["range"])
        self.assertNotIn("captured_at", payload["query"]["range"])

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


if __name__ == "__main__":
    unittest.main()
