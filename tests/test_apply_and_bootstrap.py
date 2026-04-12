import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = REPO_ROOT / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import apply_elasticsearch_assets  # noqa: E402
import bootstrap_observability  # noqa: E402
from common import ESConfig  # noqa: E402


class ApplyAndBootstrapTests(unittest.TestCase):
    def test_apply_assets_calls_expected_es_paths(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            assets_dir = Path(tmp_dir)
            (assets_dir / "component-template-ecs-base.json").write_text('{"template": {"mappings": {}}}', encoding="utf-8")
            (assets_dir / "component-template-settings.json").write_text('{"template": {"settings": {}}}', encoding="utf-8")
            (assets_dir / "index-template.json").write_text('{"index_patterns": ["agent-obsv-events*"], "data_stream": {}}', encoding="utf-8")
            (assets_dir / "ingest-pipeline.json").write_text('{"processors": []}', encoding="utf-8")
            (assets_dir / "ilm-policy.json").write_text('{"policy": {"phases": {}}}', encoding="utf-8")
            (assets_dir / "report-config.json").write_text('{"events_alias": "agent-obsv-events", "data_stream": "agent-obsv-events"}', encoding="utf-8")
            (assets_dir / "kibana-saved-objects.json").write_text('{"objects": []}', encoding="utf-8")
            calls = []

            def fake_es_request(config, method, path, payload=None):
                calls.append((method, path, payload))
                return {"acknowledged": True}

            with mock.patch.object(apply_elasticsearch_assets, "es_request", side_effect=fake_es_request):
                summary = apply_elasticsearch_assets.apply_assets(
                    ESConfig(es_url="http://localhost:9200"),
                    assets_dir=assets_dir,
                    index_prefix="agent-obsv",
                    bootstrap_index=True,
                )

        self.assertEqual(summary["template_name"], "agent-obsv-events-template")
        self.assertTrue(any(path == "/_ilm/policy/agent-obsv-lifecycle" for _, path, _ in calls))
        self.assertTrue(any(path == "/_ingest/pipeline/agent-obsv-normalize" for _, path, _ in calls))
        self.assertTrue(any("_component_template" in path for _, path, _ in calls))
        self.assertTrue(any(path == "/_index_template/agent-obsv-events-template" for _, path, _ in calls))
        self.assertTrue(any("_data_stream" in path for _, path, _ in calls))

    def test_apply_assets_can_push_kibana_saved_objects(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            assets_dir = Path(tmp_dir)
            (assets_dir / "index-template.json").write_text('{"index_patterns": ["agent-obsv-events*"], "data_stream": {}}', encoding="utf-8")
            (assets_dir / "ingest-pipeline.json").write_text('{"processors": []}', encoding="utf-8")
            (assets_dir / "ilm-policy.json").write_text('{"policy": {"phases": {}}}', encoding="utf-8")
            (assets_dir / "report-config.json").write_text('{"events_alias": "agent-obsv-events"}', encoding="utf-8")
            (assets_dir / "kibana-saved-objects.json").write_text(
                '{"objects": ['
                '{"type": "index-pattern", "id": "agent-obsv-events-view", "attributes": {"title": "agent-obsv-events*", "timeFieldName": "@timestamp"}},'
                '{"type": "search", "id": "agent-obsv-event-stream", "attributes": {"title": "Agent observability event stream", "kibanaSavedObjectMeta": {"searchSourceJSON": "{}"}}, "references": []}'
                '] }',
                encoding="utf-8",
            )
            kibana_calls = []

            def fake_es_request(config, method, path, payload=None):
                return {"acknowledged": True}

            def fake_kibana_request(config, kibana_url, method, path, payload=None, *, body_bytes=None):
                kibana_calls.append((method, path, payload))
                return {"id": path.rsplit("/", 1)[-1]}

            with mock.patch.object(apply_elasticsearch_assets, "es_request", side_effect=fake_es_request):
                with mock.patch.object(apply_elasticsearch_assets, "kibana_request", side_effect=fake_kibana_request):
                    summary = apply_elasticsearch_assets.apply_assets(
                        ESConfig(es_url="http://localhost:9200"),
                        assets_dir=assets_dir,
                        index_prefix="agent-obsv",
                        bootstrap_index=False,
                        kibana_url="http://localhost:5601",
                        kibana_space="default",
                        apply_kibana=True,
                    )

        self.assertEqual(summary["kibana"]["status"], "applied")
        self.assertEqual(summary["kibana"]["count"], 2)
        self.assertTrue(any(path.startswith("/api/saved_objects/index-pattern/agent-obsv-events-view") for _, path, _ in kibana_calls))
        self.assertTrue(any(path.startswith("/api/saved_objects/search/agent-obsv-event-stream") for _, path, _ in kibana_calls))

    def test_build_runtime_env_and_launcher_include_otlp_details(self) -> None:
        env_text = bootstrap_observability.build_runtime_env(
            service_name="agent-runtime",
            environment="dev",
            otlp_endpoint="http://127.0.0.1:4317",
        )
        launcher = bootstrap_observability.build_collector_run_script(
            collector_bin="otelcol",
            collector_path=Path("/tmp/otel-collector.generated.yaml"),
            env_path=Path("/tmp/agent-otel.env"),
        )
        self.assertIn("OTEL_EXPORTER_OTLP_ENDPOINT=http://127.0.0.1:4317", env_text)
        self.assertIn("OTEL_SERVICE_NAME=agent-runtime", env_text)
        self.assertIn('source "$SCRIPT_DIR/agent-otel.env"', launcher)
        self.assertIn('--config "$SCRIPT_DIR/otel-collector.generated.yaml"', launcher)


if __name__ == "__main__":
    unittest.main()
