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


_SUPPORTED_VERSION = {
    "version": "9.0.0",
    "major": 9,
    "minor": 0,
    "patch": 0,
    "status": "supported",
    "detail": "ok",
}


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
            with mock.patch.object(
                bootstrap_observability, "check_es_version", side_effect=AssertionError("should not be called")
            ):
                warnings = bootstrap_observability._preflight(_args(), ws, None)
        self.assertIsInstance(warnings, list)

    def test_apply_es_assets_blocks_when_es_unreachable(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ws = Path(tmp)
            (ws / "src").mkdir()
            with mock.patch.object(bootstrap_observability, "check_es_version", side_effect=SkillError("boom")):
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
            with mock.patch.object(bootstrap_observability, "check_es_version", return_value=_SUPPORTED_VERSION):
                with mock.patch("apply_elasticsearch_assets.kibana_request", side_effect=SkillError("kibana down")):
                    with self.assertRaises(SkillError) as ctx:
                        bootstrap_observability._preflight(
                            _args(apply_es_assets=True, apply_kibana_assets=True, kibana_url="http://k"),
                            ws,
                            ("u", "p"),
                        )
            self.assertIn("Kibana", str(ctx.exception))

    def test_unsupported_es_version_hard_fails(self) -> None:
        """The whole point of the version check: refuse 7.x cleanly, not halfway."""
        with tempfile.TemporaryDirectory() as tmp:
            ws = Path(tmp)
            (ws / "src").mkdir()
            unsupported = {
                "version": "7.17.0",
                "major": 7,
                "minor": 17,
                "patch": 0,
                "status": "unsupported",
                "detail": "ES 7.17.0 is below the minimum supported major (8.x).",
            }
            with mock.patch.object(bootstrap_observability, "check_es_version", return_value=unsupported):
                with self.assertRaises(SkillError) as ctx:
                    bootstrap_observability._preflight(
                        _args(apply_es_assets=True),
                        ws,
                        ("u", "p"),
                    )
            self.assertIn("7.17", str(ctx.exception))

    def test_future_es_version_warns_but_proceeds(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ws = Path(tmp)
            (ws / "src").mkdir()
            future = {
                "version": "10.0.0",
                "major": 10,
                "minor": 0,
                "patch": 0,
                "status": "warn",
                "detail": "ES 10.0.0 is newer than the latest tested major.",
            }
            with mock.patch.object(bootstrap_observability, "check_es_version", return_value=future):
                warnings = bootstrap_observability._preflight(
                    _args(apply_es_assets=True),
                    ws,
                    ("u", "p"),
                )
            self.assertTrue(any("10.0.0" in w for w in warnings))

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
            with mock.patch.object(
                bootstrap_observability, "check_es_version", side_effect=AssertionError("should not be called")
            ):
                warnings = bootstrap_observability._preflight(
                    _args(apply_es_assets=True, dry_run=True),
                    ws,
                    ("u", "p"),
                )
            self.assertIsInstance(warnings, list)


if __name__ == "__main__":
    unittest.main()
