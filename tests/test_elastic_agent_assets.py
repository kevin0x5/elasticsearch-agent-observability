import sys
import tempfile
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = REPO_ROOT / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import discover_agent_architecture  # noqa: E402
import render_elastic_agent_assets  # noqa: E402


class ElasticAgentAssetTests(unittest.TestCase):
    def test_render_elastic_native_assets_writes_extended_native_bundle(self) -> None:
        discovery = {
            "detected_modules": [
                {"module_kind": "runtime_entrypoint"},
                {"module_kind": "tool_registry"},
                {"module_kind": "model_adapter"},
                {"module_kind": "browser_frontend"},
                {"module_kind": "web_service"},
            ],
            "recommended_signals": [
                "tool_calls",
                "model_calls",
                "latency",
                "frontend_trace_correlation",
                "distributed_tracing",
            ],
        }
        with tempfile.TemporaryDirectory() as tmp_dir:
            output_dir = Path(tmp_dir)
            paths = render_elastic_agent_assets.render_assets(
                discovery,
                output_dir,
                ingest_mode="apm-otlp-hybrid",
                index_prefix="agent-obsv",
                service_name="agent-runtime",
                environment="dev",
                fleet_server_url="https://fleet.example.com:8220",
                fleet_enrollment_token="token-value",
                apm_server_url="https://apm.example.com:8200",
                kibana_url="https://kibana.example.com",
                otlp_endpoint="http://127.0.0.1:4317",
            )

            self.assertTrue((output_dir / "elastic-agent-policy.json").exists())
            self.assertTrue((output_dir / "run-elastic-agent.sh").exists())
            self.assertTrue((output_dir / "elastic-agent.env").exists())
            self.assertTrue((output_dir / "surface-manifest.json").exists())
            self.assertTrue((output_dir / "preflight-checklist.json").exists())
            self.assertTrue((output_dir / "apm-agent.env").exists())
            self.assertTrue((output_dir / "apm-entrypoints.md").exists())
            self.assertTrue((output_dir / "trace-analysis-playbook.md").exists())
            self.assertTrue((output_dir / "rum-config.json").exists())
            self.assertTrue((output_dir / "rum-agent-snippet.js").exists())
            self.assertTrue((output_dir / "ux-observability-playbook.md").exists())
            self.assertTrue((output_dir / "profiling-starter.md").exists())

            self.assertIn("policy", paths)
            self.assertIn("surface_manifest", paths)
            self.assertIn("preflight", paths)
            self.assertIn("trace_playbook", paths)
            self.assertIn("rum_config", paths)
            self.assertIn("rum_snippet", paths)
            self.assertIn("ux_playbook", paths)

            policy = render_elastic_agent_assets.read_json(
                output_dir / "elastic-agent-policy.json"
            )
            integration_names = {item["name"] for item in policy["integrations"]}
            self.assertIn("elastic_apm", integration_names)
            self.assertIn("universal_profiling", integration_names)
            self.assertIn("otlp_bridge", integration_names)
            self.assertTrue(policy["browser_monitoring"]["enabled"])
            self.assertEqual(
                policy["browser_monitoring"]["service_name"],
                "agent-runtime-web",
            )

            apm_env = (output_dir / "apm-agent.env").read_text(encoding="utf-8")
            self.assertIn("ELASTIC_APM_SERVICE_NAME=agent-runtime", apm_env)
            self.assertIn("ELASTIC_APM_TRANSACTION_SAMPLE_RATE=1.0", apm_env)
            self.assertIn("ELASTIC_APM_BREAKDOWN_METRICS=true", apm_env)

            surface_manifest = render_elastic_agent_assets.read_json(
                output_dir / "surface-manifest.json"
            )
            self.assertIn(
                "/app/apm/traces",
                surface_manifest["kibana_apps"]["traces"],
            )
            self.assertEqual(
                surface_manifest["services"]["frontend"],
                "agent-runtime-web",
            )

            preflight = render_elastic_agent_assets.read_json(
                output_dir / "preflight-checklist.json"
            )
            self.assertEqual(preflight["overall_status"], "action_required")
            self.assertEqual(preflight["action_required_count"], 1)
            self.assertEqual(preflight["native_apps"]["traces"], surface_manifest["kibana_apps"]["traces"])
            self.assertFalse(any("token-value" in str(value) for value in preflight.values()))
            check_map = {item["key"]: item for item in preflight["checks"]}
            self.assertEqual(check_map["kibana_url"]["status"], "ready")
            self.assertEqual(check_map["apm_server_url"]["status"], "ready")
            self.assertEqual(check_map["otlp_endpoint"]["status"], "ready")
            self.assertEqual(check_map["rum_distributed_tracing_origins"]["status"], "action_required")

            rum_config = render_elastic_agent_assets.read_json(
                output_dir / "rum-config.json"
            )
            self.assertEqual(rum_config["serviceName"], "agent-runtime-web")
            self.assertTrue(rum_config["captureInteractions"])
            self.assertTrue(rum_config["propagateTracestate"])
            self.assertIn("https://", rum_config["distributedTracingOrigins"][0])

            rum_snippet = (output_dir / "rum-agent-snippet.js").read_text(
                encoding="utf-8"
            )
            self.assertIn("@elastic/apm-rum", rum_snippet)
            self.assertIn("captureInteractions: true", rum_snippet)
            self.assertIn("propagateTracestate: true", rum_snippet)
            self.assertIn("window.location.origin", rum_snippet)

            trace_playbook = (output_dir / "trace-analysis-playbook.md").read_text(
                encoding="utf-8"
            )
            self.assertIn("Service Map", trace_playbook)
            self.assertIn("custom dashboard", trace_playbook)

            ux_playbook = (output_dir / "ux-observability-playbook.md").read_text(
                encoding="utf-8"
            )
            self.assertIn("frontend/backend trace correlation", ux_playbook)

    def test_discovery_recommends_hybrid_when_otel_signals_exist(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            (root / "agent.py").write_text(
                "from opentelemetry import trace\nimport elasticapm\n\nif __name__ == '__main__':\n    print('ok')\n",
                encoding="utf-8",
            )
            payload = discover_agent_architecture.discover_workspace(root, max_files=20)
        recommended_modes = [item["mode"] for item in payload["recommended_ingest_modes"]]
        self.assertIn("collector", recommended_modes)
        self.assertIn("apm-otlp-hybrid", recommended_modes)

    def test_discovery_detects_browser_frontend_and_web_service(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            (root / "package.json").write_text(
                '{"name": "agent-ui", "dependencies": {"@elastic/apm-rum": "latest", "react": "latest"}}',
                encoding="utf-8",
            )
            src_dir = root / "src"
            src_dir.mkdir()
            (src_dir / "main.tsx").write_text(
                "import { init as initApm } from '@elastic/apm-rum';\n"
                "import ReactDOM from 'react-dom/client';\n"
                "initApm({ serviceName: 'agent-ui' });\n"
                "ReactDOM.createRoot(document.getElementById('root')!).render(null);\n"
                "console.log(window.location.pathname);\n",
                encoding="utf-8",
            )
            (root / "server.py").write_text(
                "from fastapi import FastAPI\napp = FastAPI()\n\n@app.get('/health')\ndef health():\n    return {'ok': True}\n",
                encoding="utf-8",
            )
            payload = discover_agent_architecture.discover_workspace(root, max_files=20)

        module_kinds = {item["module_kind"] for item in payload["detected_modules"]}
        recommended_modes = [item["mode"] for item in payload["recommended_ingest_modes"]]
        recommended_signals = set(payload["recommended_signals"])

        self.assertIn("browser_frontend", module_kinds)
        self.assertIn("web_service", module_kinds)
        self.assertIn("apm-otlp-hybrid", recommended_modes)
        self.assertIn("frontend_trace_correlation", recommended_signals)
        self.assertIn("distributed_tracing", recommended_signals)

    def test_discovery_detects_guardrail_and_knowledge_base(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            (root / "guardrail.py").write_text(
                "def safety_check(text):\n    if prompt_injection(text):\n        return block_response()\n    return pass\n",
                encoding="utf-8",
            )
            (root / "rag.py").write_text(
                "from qdrant import QdrantClient\ndef similarity_search(query, knowledge_base='default'):\n    return vector_search(query)\n",
                encoding="utf-8",
            )
            payload = discover_agent_architecture.discover_workspace(root, max_files=20)

        module_kinds = {item["module_kind"] for item in payload["detected_modules"]}
        recommended_signals = set(payload["recommended_signals"])

        self.assertIn("guardrail", module_kinds)
        self.assertIn("knowledge_base", module_kinds)
        self.assertIn("guardrail_checks", recommended_signals)
        self.assertIn("retrieval_calls", recommended_signals)


if __name__ == "__main__":
    unittest.main()
