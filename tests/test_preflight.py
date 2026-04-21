"""Bootstrap preflight tests — fail-fast checks before any file lands on disk."""

import argparse
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = REPO_ROOT / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import bootstrap_observability  # noqa: E402
from common import SkillError  # noqa: E402


def _args(**overrides) -> argparse.Namespace:
    defaults = dict(
        es_url="http://localhost:9200",
        kibana_url="",
        no_verify_tls=False,
        apply_es_assets=False,
        apply_kibana_assets=False,
        dry_run=False,
        ingest_mode="collector",
        fleet_server_url="",
        fleet_enrollment_token="",
    )
    defaults.update(overrides)
    return argparse.Namespace(**defaults)


class PreflightTests(unittest.TestCase):
    def test_no_apply_means_no_cluster_check(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ws = Path(tmp)
            (ws / "src").mkdir()
            with mock.patch.object(bootstrap_observability, "es_request", side_effect=AssertionError("should not be called")):
                warnings = bootstrap_observability._preflight(_args(), ws, None)
        self.assertIsInstance(warnings, list)

    def test_apply_es_assets_blocks_when_es_unreachable(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ws = Path(tmp)
            (ws / "src").mkdir()
            with mock.patch.object(bootstrap_observability, "es_request", side_effect=SkillError("boom")):
                with self.assertRaises(SkillError) as ctx:
                    bootstrap_observability._preflight(
                        _args(apply_es_assets=True),
                        ws,
                        ("u", "p"),
                    )
            self.assertIn("not reachable", str(ctx.exception))

    def test_apply_kibana_blocks_when_kibana_unreachable(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ws = Path(tmp)
            (ws / "src").mkdir()
            with mock.patch.object(bootstrap_observability, "es_request", return_value={"ok": True}):
                from apply_elasticsearch_assets import kibana_request as _real
                with mock.patch("apply_elasticsearch_assets.kibana_request", side_effect=SkillError("kibana down")):
                    with self.assertRaises(SkillError) as ctx:
                        bootstrap_observability._preflight(
                            _args(apply_es_assets=True, apply_kibana_assets=True, kibana_url="http://k"),
                            ws,
                            ("u", "p"),
                        )
            self.assertIn("Kibana", str(ctx.exception))

    def test_fleet_mode_warns_when_inputs_missing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ws = Path(tmp)
            (ws / "src").mkdir()
            warnings = bootstrap_observability._preflight(
                _args(ingest_mode="elastic-agent-fleet"),
                ws,
                None,
            )
            self.assertTrue(any("fleet-server-url" in w for w in warnings))

    def test_empty_workspace_emits_warning(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ws = Path(tmp)
            warnings = bootstrap_observability._preflight(_args(), ws, None)
            self.assertTrue(any("empty" in w for w in warnings))

    def test_dry_run_skips_cluster_check(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ws = Path(tmp)
            (ws / "src").mkdir()
            with mock.patch.object(bootstrap_observability, "es_request", side_effect=AssertionError("should not be called")):
                warnings = bootstrap_observability._preflight(
                    _args(apply_es_assets=True, dry_run=True),
                    ws,
                    ("u", "p"),
                )
            self.assertIsInstance(warnings, list)


if __name__ == "__main__":
    unittest.main()
