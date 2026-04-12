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
    def test_render_elastic_native_assets_writes_policy_and_launcher(self) -> None:
        discovery = {
            "detected_modules": [
                {"module_kind": "runtime_entrypoint"},
                {"module_kind": "tool_registry"},
                {"module_kind": "model_adapter"},
            ],
            "recommended_signals": ["tool_calls", "model_calls", "latency"],
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
            self.assertIn("policy", paths)

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


if __name__ == "__main__":
    unittest.main()
