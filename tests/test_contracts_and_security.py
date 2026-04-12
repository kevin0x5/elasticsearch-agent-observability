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
        self.assertIn('logs_index: "agent-obsv-events"', rendered)
        self.assertIn('traces_index: "agent-obsv-events"', rendered)

    def test_render_assets_and_report_share_same_events_alias(self) -> None:
        template = render_es_assets.build_index_template("agent-obsv", ["tool_registry", "model_adapter"])
        report_config = render_es_assets.build_report_config("agent-obsv", DISCOVERY_SAMPLE)
        self.assertEqual(template["template"]["settings"]["index.lifecycle.rollover_alias"], report_config["events_alias"])
        self.assertEqual(template["index_patterns"], ["agent-obsv-events-*"])
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

    def test_build_ingest_pipeline_stamps_captured_at(self) -> None:
        pipeline = render_es_assets.build_ingest_pipeline(["tool_registry"])
        set_processors = [item["set"] for item in pipeline["processors"] if "set" in item]
        self.assertTrue(any(proc.get("field") == "captured_at" for proc in set_processors))

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
