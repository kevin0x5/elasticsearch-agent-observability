import sys
import unittest
from pathlib import Path
from unittest import mock


REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = REPO_ROOT / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import bootstrap_observability  # noqa: E402
import render_collector_config  # noqa: E402


DISCOVERY_SAMPLE = {
    "files_scanned": 12,
    "detected_modules": [{"module_kind": "tool_registry"}],
    "recommended_ingest_modes": [{"mode": "collector", "score": 0.94}],
}


class CollectorGovernanceTests(unittest.TestCase):
    def test_bootstrap_parses_governance_flags(self) -> None:
        with mock.patch.object(
            sys,
            "argv",
            [
                "bootstrap_observability.py",
                "--workspace",
                "/tmp/ws",
                "--output-dir",
                "/tmp/out",
                "--es-url",
                "http://localhost:9200",
                "--sampling-ratio",
                "0.2",
                "--send-queue-size",
                "4096",
                "--retry-initial-interval",
                "1s",
                "--retry-max-interval",
                "30s",
            ],
        ):
            args = bootstrap_observability.parse_args()
        self.assertEqual(args.sampling_ratio, 0.2)
        self.assertEqual(args.send_queue_size, 4096)
        self.assertEqual(args.retry_initial_interval, "1s")
        self.assertEqual(args.retry_max_interval, "30s")

    def test_render_config_includes_queue_and_retry_blocks(self) -> None:
        rendered = render_collector_config.render_config(
            DISCOVERY_SAMPLE,
            es_url="http://localhost:9200",
            index_prefix="agent-obsv",
            environment="dev",
            service_name="agent-runtime",
            sampling_ratio=0.2,
            send_queue_size=4096,
            retry_initial_interval="1s",
            retry_max_interval="30s",
        )
        self.assertIn("probabilistic_sampler", rendered)
        self.assertIn("sampling_percentage: 20.0", rendered)
        self.assertIn("sending_queue:", rendered)
        self.assertIn("queue_size: 4096", rendered)
        self.assertIn("retry_on_failure:", rendered)
        self.assertIn("initial_interval: 1s", rendered)
        self.assertIn("max_interval: 30s", rendered)

    def test_layered_architecture_base_and_governance_are_independent(self) -> None:
        """The base topology and governance overrides must be independently
        constructible dicts that _assemble_yaml merges into the final output."""
        from common import validate_credential_pair, validate_index_prefix

        base = render_collector_config._build_base_topology(
            discovery=DISCOVERY_SAMPLE,
            es_url="http://localhost:9200",
            validated_prefix=validate_index_prefix("agent-obsv"),
            environment="dev",
            service_name="agent-runtime",
            credentials=validate_credential_pair("", ""),
            embed_credentials=False,
            grpc_port=4317,
            http_port=4318,
            enable_filelog=False,
            filelog_path="/var/log/agent/*.log",
        )
        gov = render_collector_config._build_governance_overrides(
            sampling_ratio=0.5,
            send_queue_size=1024,
            retry_initial_interval="2s",
            retry_max_interval="60s",
            telemetry_metrics_port=8888,
            log_min_severity="",
        )
        # Both are plain dicts — no side effects, no YAML yet.
        self.assertIsInstance(base, dict)
        self.assertIsInstance(gov, dict)
        # No key collision between the two layers.
        self.assertFalse(set(base) & set(gov), "base and governance must not share keys")
        # Assembling them produces the same output as calling render_config directly.
        assembled = render_collector_config._assemble_yaml(base, gov)
        via_public = render_collector_config.render_config(
            DISCOVERY_SAMPLE,
            es_url="http://localhost:9200",
            index_prefix="agent-obsv",
            environment="dev",
            service_name="agent-runtime",
            sampling_ratio=0.5,
            send_queue_size=1024,
            retry_initial_interval="2s",
            retry_max_interval="60s",
            telemetry_metrics_port=8888,
        )
        self.assertEqual(assembled, via_public)


if __name__ == "__main__":
    unittest.main()
