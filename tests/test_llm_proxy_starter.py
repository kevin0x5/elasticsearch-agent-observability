"""LLM proxy starter bundle tests."""

import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = REPO_ROOT / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import render_llm_proxy_starter  # noqa: E402


class LLMProxyStarterTests(unittest.TestCase):
    def test_render_llm_proxy_bundle_writes_all_artifacts(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            target = Path(tmp_dir) / "llm-proxy"
            paths = render_llm_proxy_starter.render_llm_proxy_bundle(
                target,
                service_name="openclaw-agent",
                environment="dev",
                proxy_port=4000,
            )
            # Every produced path must exist on disk
            for key in ("compose", "config", "env_example", "readme"):
                self.assertTrue(paths[key].exists(), f"missing {key}: {paths[key]}")

            compose_text = paths["compose"].read_text(encoding="utf-8")
            self.assertIn("litellm", compose_text)
            self.assertIn("4000:4000", compose_text)
            self.assertIn("OTEL_EXPORTER_OTLP_ENDPOINT", compose_text)
            self.assertIn("openclaw-agent-proxy", compose_text)

            config_text = paths["config"].read_text(encoding="utf-8")
            self.assertIn("callbacks:", config_text)
            self.assertIn("otel", config_text)

            readme_text = paths["readme"].read_text(encoding="utf-8")
            self.assertIn("OPENAI_API_BASE", readme_text)
            self.assertIn("4000", readme_text)

    def test_render_llm_proxy_bundle_respects_custom_port(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            target = Path(tmp_dir) / "llm-proxy"
            paths = render_llm_proxy_starter.render_llm_proxy_bundle(
                target,
                service_name="svc",
                environment="prod",
                proxy_port=8765,
            )
            compose_text = paths["compose"].read_text(encoding="utf-8")
            readme_text = paths["readme"].read_text(encoding="utf-8")
            self.assertIn("8765:4000", compose_text)
            self.assertIn("8765", readme_text)


if __name__ == "__main__":
    unittest.main()
