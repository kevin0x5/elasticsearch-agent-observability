import sys
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = REPO_ROOT / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import bootstrap_observability  # noqa: E402
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

    def test_collect_summary_notes_reports_truncation_and_auth_mode(self) -> None:
        notes = bootstrap_observability.collect_summary_notes(
            {"files_scanned": 400, "detected_modules": []},
            max_files=400,
            auth_mode="env",
            index_prefix="agent-obsv",
        )
        self.assertTrue(any("--max-files" in note for note in notes))
        self.assertTrue(any("credentials were not written to disk" in note for note in notes))
        self.assertTrue(any("agent-obsv-events" in note for note in notes))


if __name__ == "__main__":
    unittest.main()
