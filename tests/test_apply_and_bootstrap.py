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
import render_otlp_http_bridge  # noqa: E402
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

    def test_apply_assets_reports_native_preflight_status(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            assets_dir = root / "elasticsearch"
            native_dir = root / "elastic-native"
            assets_dir.mkdir()
            native_dir.mkdir()
            (assets_dir / "index-template.json").write_text('{"index_patterns": ["agent-obsv-events*"], "data_stream": {}}', encoding="utf-8")
            (assets_dir / "ingest-pipeline.json").write_text('{"processors": []}', encoding="utf-8")
            (assets_dir / "ilm-policy.json").write_text('{"policy": {"phases": {}}}', encoding="utf-8")
            (assets_dir / "report-config.json").write_text('{"events_alias": "agent-obsv-events"}', encoding="utf-8")
            (assets_dir / "kibana-saved-objects.json").write_text('{"objects": []}', encoding="utf-8")
            (native_dir / "preflight-checklist.json").write_text(
                '{"ingest_mode": "elastic-agent-fleet", "service_name": "agent-runtime", "environment": "dev", "checks": [{"key": "kibana_url", "required": true, "status": "ready", "detail": "ok"}], "next_steps": ["review fleet"]}',
                encoding="utf-8",
            )
            (native_dir / "surface-manifest.json").write_text(
                '{"services": {"backend": "agent-runtime", "frontend": "agent-runtime-web", "environment": "dev"}, "kibana_apps": {"services": "https://kibana.acme.internal/app/apm/services", "traces": "https://kibana.acme.internal/app/apm/traces", "service_map": "https://kibana.acme.internal/app/apm/service-map", "user_experience": "https://kibana.acme.internal/app/ux"}}',
                encoding="utf-8",
            )

            def fake_es_request(config, method, path, payload=None):
                return {"acknowledged": True}

            def fake_kibana_request(config, kibana_url, method, path, payload=None, *, body_bytes=None):
                if path == "/api/status":
                    return {"status": {"overall": {"level": "available", "summary": "ok"}}}
                if path.startswith("/api/fleet/agent_policies"):
                    return {"total": 3, "items": []}
                raise AssertionError(path)

            with mock.patch.object(apply_elasticsearch_assets, "es_request", side_effect=fake_es_request):
                with mock.patch.object(apply_elasticsearch_assets, "kibana_request", side_effect=fake_kibana_request):
                    summary = apply_elasticsearch_assets.apply_assets(
                        ESConfig(es_url="http://localhost:9200"),
                        assets_dir=assets_dir,
                        native_assets_dir=native_dir,
                        index_prefix="agent-obsv",
                        bootstrap_index=False,
                        kibana_url="https://kibana.acme.internal",
                        apply_kibana=False,
                    )

        self.assertIsNotNone(summary["native_bundle"])
        self.assertEqual(summary["native_bundle"]["overall_status"], "ready")
        self.assertEqual(len(summary["native_bundle"]["runtime_checks"]), 2)
        self.assertEqual(len(summary["native_bundle"]["contract_checks"]), 3)
        self.assertEqual(summary["native_bundle"]["ready_count"], 5)
        self.assertEqual(summary["native_bundle"]["native_apps"]["services"], "https://kibana.acme.internal/app/apm/services")

    def test_apply_assets_surfaces_native_contract_gaps(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            assets_dir = root / "elasticsearch"
            native_dir = root / "elastic-native"
            assets_dir.mkdir()
            native_dir.mkdir()
            (assets_dir / "index-template.json").write_text('{"index_patterns": ["agent-obsv-events*"], "data_stream": {}}', encoding="utf-8")
            (assets_dir / "ingest-pipeline.json").write_text('{"processors": []}', encoding="utf-8")
            (assets_dir / "ilm-policy.json").write_text('{"policy": {"phases": {}}}', encoding="utf-8")
            (assets_dir / "report-config.json").write_text('{"events_alias": "agent-obsv-events"}', encoding="utf-8")
            (assets_dir / "kibana-saved-objects.json").write_text('{"objects": []}', encoding="utf-8")
            (native_dir / "preflight-checklist.json").write_text(
                '{"ingest_mode": "apm-otlp-hybrid", "service_name": "agent-runtime", "environment": "dev", "checks": [{"key": "kibana_url", "required": true, "status": "ready", "detail": "ok"}, {"key": "rum_distributed_tracing_origins", "required": true, "status": "ready", "detail": "configured"}], "next_steps": []}',
                encoding="utf-8",
            )
            (native_dir / "surface-manifest.json").write_text(
                '{"services": {"backend": "agent-runtime", "frontend": "agent-runtime-web", "environment": "prod"}, "kibana_apps": {"services": "https://kibana.example.com/app/apm/services", "traces": "https://kibana.example.com/app/apm/traces", "service_map": "https://kibana.example.com/app/apm/service-map", "user_experience": "https://kibana.example.com/app/ux"}}',
                encoding="utf-8",
            )
            (native_dir / "rum-config.json").write_text(
                '{"serviceName": "agent-runtime-web", "distributedTracingOrigins": ["https://your-app-origin.example.com"]}',
                encoding="utf-8",
            )

            summary = apply_elasticsearch_assets.apply_assets(
                ESConfig(es_url="http://localhost:9200"),
                assets_dir=assets_dir,
                native_assets_dir=native_dir,
                index_prefix="agent-obsv",
                bootstrap_index=False,
                kibana_url="https://kibana.example.com",
                apply_kibana=False,
                dry_run=True,
            )

        self.assertEqual(summary["native_bundle"]["overall_status"], "action_required")
        contract_map = {item["key"]: item for item in summary["native_bundle"]["contract_checks"]}
        self.assertEqual(contract_map["native_kibana_entrypoints"]["status"], "action_required")
        self.assertEqual(contract_map["native_service_contract"]["status"], "action_required")
        self.assertEqual(contract_map["rum_trace_correlation_contract"]["status"], "action_required")
        self.assertGreaterEqual(len(summary["native_bundle"]["blocking_checks"]), 3)

    def test_apply_assets_does_not_fall_back_to_legacy_write_index(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            assets_dir = Path(tmp_dir)
            (assets_dir / "index-template.json").write_text('{"index_patterns": ["agent-obsv-events*"], "data_stream": {}}', encoding="utf-8")
            (assets_dir / "ingest-pipeline.json").write_text('{"processors": []}', encoding="utf-8")
            (assets_dir / "ilm-policy.json").write_text('{"policy": {"phases": {}}}', encoding="utf-8")
            (assets_dir / "report-config.json").write_text('{"events_alias": "agent-obsv-events", "data_stream": "agent-obsv-events"}', encoding="utf-8")
            (assets_dir / "kibana-saved-objects.json").write_text('{"objects": []}', encoding="utf-8")

            def fake_es_request(config, method, path, payload=None):
                if path == "/_data_stream/agent-obsv-events":
                    raise apply_elasticsearch_assets.SkillError("data stream bootstrap failed")
                return {"acknowledged": True}

            with mock.patch.object(apply_elasticsearch_assets, "es_request", side_effect=fake_es_request):
                with self.assertRaises(apply_elasticsearch_assets.SkillError):
                    apply_elasticsearch_assets.apply_assets(
                        ESConfig(es_url="http://localhost:9200"),
                        assets_dir=assets_dir,
                        index_prefix="agent-obsv",
                        bootstrap_index=True,
                        apply_kibana=False,
                    )

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
        self.assertNotIn("ELASTICSEARCH_USERNAME=", env_text)
        self.assertIn('source "$SCRIPT_DIR/agent-otel.env"', launcher)
        self.assertIn('--config "$SCRIPT_DIR/otel-collector.generated.yaml"', launcher)

    def test_build_runtime_env_can_include_es_placeholders(self) -> None:
        env_text = bootstrap_observability.build_runtime_env(
            service_name="agent-runtime",
            environment="dev",
            otlp_endpoint="http://127.0.0.1:4317",
            include_es_placeholders=True,
        )
        self.assertIn("ELASTICSEARCH_USERNAME=", env_text)
        self.assertIn("ELASTICSEARCH_PASSWORD=", env_text)
        self.assertIn("avoid storing real secrets in this file", env_text)

    def test_build_bridge_runtime_env_and_launcher_include_http_details(self) -> None:
        env_text = bootstrap_observability.build_bridge_runtime_env(
            service_name="agent-runtime",
            environment="dev",
            bridge_endpoint="http://127.0.0.1:14319",
        )
        launcher = bootstrap_observability.build_bridge_run_script(
            bridge_path=Path("/tmp/otlphttpbridge.py"),
            env_path=Path("/tmp/agent-otel-bridge.env"),
        )
        self.assertIn("OTEL_EXPORTER_OTLP_PROTOCOL=http/protobuf", env_text)
        self.assertIn("OTEL_EXPORTER_OTLP_TRACES_ENDPOINT=http://127.0.0.1:14319/v1/traces", env_text)
        self.assertIn("OTEL_EXPORTER_OTLP_LOGS_ENDPOINT=http://127.0.0.1:14319/v1/logs", env_text)
        self.assertIn("Metrics are not bridged", env_text)
        self.assertIn('source "$SCRIPT_DIR/agent-otel-bridge.env"', launcher)
        self.assertIn('exec python3 "$SCRIPT_DIR/otlphttpbridge.py"', launcher)

    def test_render_bridge_script_binds_expected_endpoint(self) -> None:
        script = render_otlp_http_bridge.render_bridge_script(
            es_url="http://localhost:9200",
            index_prefix="agent-obsv",
            bind_host="127.0.0.1",
            bind_port=14319,
            verify_tls=True,
        )
        self.assertIn("EVENTS_DATA_STREAM = 'agent-obsv-events'", script)
        self.assertIn('BRIDGE_PORT = 14319', script)
        self.assertIn('OTLP protobuf payloads require', script)
        compile(script, "rendered-bridge", "exec")

    def test_summary_includes_sanity_check_scope_note_after_apply(self) -> None:
        summary = bootstrap_observability.build_summary(
            discovery_path=Path("/tmp/discovery.json"),
            assets_paths={
                "index_template": "/tmp/index-template.json",
                "ingest_pipeline": "/tmp/ingest-pipeline.json",
                "ilm_policy": "/tmp/ilm-policy.json",
                "report_config": "/tmp/report-config.json",
                "kibana_saved_objects_json": "/tmp/kibana-saved-objects.json",
                "kibana_saved_objects_ndjson": "/tmp/kibana-saved-objects.ndjson",
            },
            notes=["The built-in sanity check writes directly to Elasticsearch; treat it as Elastic-side validation, not as Collector end-to-end proof."],
            ingest_mode="collector",
            collector_path=None,
            env_path=None,
            collector_run_path=None,
            bridge_path=Path("/tmp/otlphttpbridge.py"),
            bridge_env_path=Path("/tmp/agent-otel-bridge.env"),
            bridge_run_path=Path("/tmp/run-otlphttpbridge.sh"),
            instrument_snippet_path=None,
            native_assets_paths=None,
            apply_summary_path=Path("/tmp/apply-summary.json"),
            sanity_check_path=Path("/tmp/sanity-check.json"),
            report_output=None,
        )
        self.assertIn("Collector end-to-end proof", summary)
        self.assertIn("sanity check", summary)
        self.assertIn("OTLP HTTP bridge", summary)
        self.assertIn("run-otlphttpbridge.sh", summary)

    def test_apply_assets_dry_run_returns_plan(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            assets_dir = Path(tmp_dir)
            (assets_dir / "component-template-ecs-base.json").write_text('{"template": {"mappings": {}}}', encoding="utf-8")
            (assets_dir / "component-template-settings.json").write_text('{"template": {"settings": {}}}', encoding="utf-8")
            (assets_dir / "index-template.json").write_text('{"index_patterns": ["agent-obsv-events*"], "data_stream": {}}', encoding="utf-8")
            (assets_dir / "ingest-pipeline.json").write_text('{"processors": []}', encoding="utf-8")
            (assets_dir / "ilm-policy.json").write_text('{"policy": {"phases": {}}}', encoding="utf-8")
            (assets_dir / "report-config.json").write_text('{"events_alias": "agent-obsv-events"}', encoding="utf-8")
            (assets_dir / "kibana-saved-objects.json").write_text('{"objects": [{"type": "index-pattern", "id": "test-view"}]}', encoding="utf-8")
            summary = apply_elasticsearch_assets.apply_assets(
                ESConfig(es_url="http://localhost:9200"),
                assets_dir=assets_dir,
                index_prefix="agent-obsv",
                bootstrap_index=True,
                kibana_url="http://localhost:5601",
                apply_kibana=True,
                dry_run=True,
            )
        self.assertTrue(summary["dry_run"])
        self.assertGreater(summary["plan_count"], 0)
        actions = [step["action"] for step in summary["plan"]]
        self.assertIn("PUT", actions)
        paths = [step["path"] for step in summary["plan"]]
        self.assertTrue(any("_ilm/policy" in p for p in paths))
        self.assertTrue(any("_data_stream" in p for p in paths))
        self.assertTrue(any("saved_objects" in p for p in paths))

    def test_apply_assets_dry_run_can_preview_kibana_without_url(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            assets_dir = Path(tmp_dir)
            (assets_dir / "component-template-ecs-base.json").write_text('{"template": {"mappings": {}}}', encoding="utf-8")
            (assets_dir / "component-template-settings.json").write_text('{"template": {"settings": {}}}', encoding="utf-8")
            (assets_dir / "index-template.json").write_text('{"index_patterns": ["agent-obsv-events*"], "data_stream": {}}', encoding="utf-8")
            (assets_dir / "ingest-pipeline.json").write_text('{"processors": []}', encoding="utf-8")
            (assets_dir / "ilm-policy.json").write_text('{"policy": {"phases": {}}}', encoding="utf-8")
            (assets_dir / "report-config.json").write_text('{"events_alias": "agent-obsv-events"}', encoding="utf-8")
            (assets_dir / "kibana-saved-objects.json").write_text('{"objects": [{"type": "search", "id": "test-search"}]}', encoding="utf-8")
            summary = apply_elasticsearch_assets.apply_assets(
                ESConfig(es_url="http://localhost:9200"),
                assets_dir=assets_dir,
                index_prefix="agent-obsv",
                bootstrap_index=False,
                apply_kibana=True,
                dry_run=True,
            )
        paths = [step["path"] for step in summary["plan"]]
        self.assertTrue(any(path.endswith("/api/saved_objects/search/test-search") for path in paths))

    def test_apply_assets_dry_run_can_preview_native_checks(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            assets_dir = root / "elasticsearch"
            native_dir = root / "elastic-native"
            assets_dir.mkdir()
            native_dir.mkdir()
            (assets_dir / "component-template-ecs-base.json").write_text('{"template": {"mappings": {}}}', encoding="utf-8")
            (assets_dir / "component-template-settings.json").write_text('{"template": {"settings": {}}}', encoding="utf-8")
            (assets_dir / "index-template.json").write_text('{"index_patterns": ["agent-obsv-events*"], "data_stream": {}}', encoding="utf-8")
            (assets_dir / "ingest-pipeline.json").write_text('{"processors": []}', encoding="utf-8")
            (assets_dir / "ilm-policy.json").write_text('{"policy": {"phases": {}}}', encoding="utf-8")
            (assets_dir / "report-config.json").write_text('{"events_alias": "agent-obsv-events"}', encoding="utf-8")
            (assets_dir / "kibana-saved-objects.json").write_text('{"objects": []}', encoding="utf-8")
            (native_dir / "preflight-checklist.json").write_text(
                '{"ingest_mode": "elastic-agent-fleet", "service_name": "agent-runtime", "environment": "dev", "checks": [{"key": "kibana_url", "required": true, "status": "ready", "detail": "ok"}], "next_steps": []}',
                encoding="utf-8",
            )
            (native_dir / "surface-manifest.json").write_text(
                '{"services": {"backend": "agent-runtime", "frontend": "agent-runtime-web", "environment": "dev"}, "kibana_apps": {"services": "https://kibana.acme.internal/app/apm/services", "traces": "https://kibana.acme.internal/app/apm/traces", "service_map": "https://kibana.acme.internal/app/apm/service-map", "user_experience": "https://kibana.acme.internal/app/ux"}}',
                encoding="utf-8",
            )
            summary = apply_elasticsearch_assets.apply_assets(
                ESConfig(es_url="http://localhost:9200"),
                assets_dir=assets_dir,
                native_assets_dir=native_dir,
                index_prefix="agent-obsv",
                bootstrap_index=False,
                kibana_url="https://kibana.acme.internal",
                apply_kibana=False,
                dry_run=True,
            )
        self.assertEqual(summary["native_bundle"]["overall_status"], "ready")
        paths = [step["path"] for step in summary["plan"]]
        self.assertIn("/api/status", paths)
        self.assertIn("/api/fleet/agent_policies?page=1&perPage=1", paths)


if __name__ == "__main__":
    unittest.main()
