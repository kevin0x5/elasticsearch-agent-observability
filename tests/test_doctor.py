"""Doctor script tests — the 'healthz lying' scenarios are the whole point."""

import argparse
import sys
import unittest
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = REPO_ROOT / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import bootstrap_observability  # noqa: E402
import doctor  # noqa: E402
from common import SkillError  # noqa: E402


def _args(**overrides) -> argparse.Namespace:
    defaults = dict(
        es_url="http://localhost:9200",
        es_user="",
        es_password="",
        index_prefix="agent-obsv",
        healthz_url="http://127.0.0.1:14319/healthz",
        otlp_http_endpoint="http://127.0.0.1:14319",
        freshness_minutes=10,
        skip_canary=True,  # most unit tests run without the live canary
        no_verify_tls=False,
        collector_log="",
        output_format="text",
    )
    defaults.update(overrides)
    return argparse.Namespace(**defaults)


class DoctorTests(unittest.TestCase):
    # ----- the "bridge ok, collector dead" case from the field ------------

    def test_bridge_up_collector_partial_with_data_is_still_degraded_collector_path(self) -> None:
        """Collector ``partial`` (one of 4317/4318 listening) must resolve the
        same as ``down`` for the degraded_collector_path verdict: bridge is
        still the path keeping us alive, and the operator must be told that
        the Collector path is broken — not merely ``degraded``."""
        fake_proc = {
            "status": "warn",
            "detail": "Bridge up; Collector partial (only 4317).",
            "paths": {
                "bridge": {"status": "up", "listening_ports": {"14319": True}},
                "collector": {
                    "status": "partial",
                    "listening_ports": {"4317": True, "4318": False},
                },
            },
        }
        fake_recent = {"status": "pass", "detail": "data flowing", "doc_count": 17}
        with mock.patch.object(doctor, "_probe_healthz", return_value={"status": "pass", "detail": "ok"}):
            with mock.patch.object(doctor, "_probe_processes_and_ports", return_value=fake_proc):
                with mock.patch.object(doctor, "_probe_recent_data", return_value=fake_recent):
                    with mock.patch.object(doctor, "_probe_canary", return_value={"status": "pass", "detail": "ok"}):
                        result = doctor.run_doctor(_args(skip_canary=False))
        self.assertEqual(result["verdict"], "degraded_collector_path")

    def test_bridge_up_collector_down_with_real_data_is_degraded_collector_path(self) -> None:
        """Real-world case: Collector is <defunct>, but bridge is handling traffic.

        The important bit is that this verdict is SPECIFIC, not a generic
        ``degraded``. Operators need a one-liner that says "the fallback is
        saving you, fix the standard path" — not "something is warn".
        """
        fake_proc = {
            "status": "warn",
            "detail": "Bridge path listening, Collector path down.",
            "paths": {
                "bridge": {"status": "up", "listening_ports": {"14319": True}},
                "collector": {"status": "down", "listening_ports": {"4317": False, "4318": False}},
            },
        }
        fake_recent = {
            "status": "pass",
            "detail": "real agent data flowing",
            "doc_count": 42,
        }
        fake_canary = {"status": "pass", "detail": "canary landed"}
        with mock.patch.object(doctor, "_probe_healthz", return_value={"status": "pass", "detail": "ok"}):
            with mock.patch.object(doctor, "_probe_processes_and_ports", return_value=fake_proc):
                with mock.patch.object(doctor, "_probe_recent_data", return_value=fake_recent):
                    with mock.patch.object(doctor, "_probe_canary", return_value=fake_canary):
                        result = doctor.run_doctor(_args(skip_canary=False))

        self.assertEqual(result["verdict"], "degraded_collector_path")
        # Summary must name the specific fault, not just "partial".
        summary = result["summary"].lower()
        self.assertIn("bridge", summary)
        self.assertIn("collector", summary)
        self.assertIn("fallback", summary)

    def test_collector_up_bridge_down_is_generic_degraded(self) -> None:
        """Opposite half-state: standard path works but no fallback. Still warn,
        but NOT the specific 'bridge saved you' verdict — since bridge isn't."""
        fake_proc = {
            "status": "warn",
            "detail": "Collector up, bridge down.",
            "paths": {
                "bridge": {"status": "down", "listening_ports": {"14319": False}},
                "collector": {"status": "up", "listening_ports": {"4317": True, "4318": True}},
            },
        }
        fake_recent = {"status": "pass", "detail": "data flowing", "doc_count": 10}
        with mock.patch.object(doctor, "_probe_healthz", return_value={"status": "pass", "detail": "ok"}):
            with mock.patch.object(doctor, "_probe_processes_and_ports", return_value=fake_proc):
                with mock.patch.object(doctor, "_probe_recent_data", return_value=fake_recent):
                    with mock.patch.object(doctor, "_probe_canary", return_value={"status": "pass", "detail": "ok"}):
                        result = doctor.run_doctor(_args(skip_canary=False))
        self.assertEqual(result["verdict"], "degraded")

    def test_bridge_up_collector_down_but_no_data_is_still_degraded_generic(self) -> None:
        """If data isn't actually flowing, we don't claim bridge is saving us."""
        fake_proc = {
            "status": "warn",
            "detail": "Bridge up, collector down",
            "paths": {
                "bridge": {"status": "up", "listening_ports": {"14319": True}},
                "collector": {"status": "down", "listening_ports": {"4317": False, "4318": False}},
            },
        }
        fake_recent = {"status": "fail", "detail": "no docs", "doc_count": 0}
        with mock.patch.object(doctor, "_probe_healthz", return_value={"status": "pass", "detail": "ok"}):
            with mock.patch.object(doctor, "_probe_processes_and_ports", return_value=fake_proc):
                with mock.patch.object(doctor, "_probe_recent_data", return_value=fake_recent):
                    with mock.patch.object(doctor, "_probe_canary", return_value={"status": "skipped", "detail": "skip"}):
                        result = doctor.run_doctor(_args(skip_canary=True))
        # recent_data fail => broken, not degraded_collector_path.
        self.assertEqual(result["verdict"], "broken")

    # ----- path classifier unit tests -------------------------------------

    def test_classify_paths_bridge_only(self) -> None:
        paths = doctor._classify_paths({"4317": False, "4318": False, "14319": True})
        self.assertEqual(paths["bridge"]["status"], "up")
        self.assertEqual(paths["collector"]["status"], "down")

    def test_classify_paths_collector_partial(self) -> None:
        paths = doctor._classify_paths({"4317": True, "4318": False, "14319": True})
        self.assertEqual(paths["collector"]["status"], "partial")

    def test_classify_paths_both_up(self) -> None:
        paths = doctor._classify_paths({"4317": True, "4318": True, "14319": True})
        self.assertEqual(paths["bridge"]["status"], "up")
        self.assertEqual(paths["collector"]["status"], "up")

    def test_classify_paths_all_down(self) -> None:
        paths = doctor._classify_paths({})
        self.assertEqual(paths["bridge"]["status"], "down")
        self.assertEqual(paths["collector"]["status"], "down")

    # ----- runtime-config port resolution --------------------------------
    # Regression: the doctor used to hard-code 14319 for the bridge port and
    # (4317, 4318) for the collector. Operators who passed
    # ``--bridge-http-port`` to bootstrap then saw doctor report the bridge
    # path as `down` even when it was healthy on the custom port. These
    # tests pin the fix: `_classify_paths` must honour explicit port tuples,
    # and `resolve_otlp_ports` must thread bootstrap's runtime-config.json
    # into that path.

    def test_classify_paths_honours_custom_bridge_port(self) -> None:
        paths = doctor._classify_paths(
            {"4317": True, "4318": True, "24319": True},
            bridge_ports=("24319",),
            collector_ports=("4317", "4318"),
        )
        self.assertEqual(paths["bridge"]["status"], "up")
        self.assertEqual(paths["collector"]["status"], "up")

    def test_classify_paths_custom_bridge_down_when_only_default_listens(self) -> None:
        """If runtime-config says bridge moved to 24319, an older process still
        listening on the default 14319 must NOT be reported as the bridge."""
        paths = doctor._classify_paths(
            {"14319": True},
            bridge_ports=("24319",),
            collector_ports=("4317", "4318"),
        )
        self.assertEqual(paths["bridge"]["status"], "down")

    def test_resolve_otlp_ports_reads_runtime_config_overrides(self) -> None:
        from common import resolve_otlp_ports
        collector, bridge = resolve_otlp_ports(
            {"collector_otlp_ports": [5317, 5318], "bridge_http_port": 24319}
        )
        self.assertEqual(collector, ("5317", "5318"))
        self.assertEqual(bridge, ("24319",))

    def test_resolve_otlp_ports_falls_back_to_defaults(self) -> None:
        from common import BRIDGE_OTLP_PORTS, COLLECTOR_OTLP_PORTS, resolve_otlp_ports
        collector, bridge = resolve_otlp_ports({})
        self.assertEqual(collector, COLLECTOR_OTLP_PORTS)
        self.assertEqual(bridge, BRIDGE_OTLP_PORTS)

    def test_load_runtime_config_from_env_var_is_respected(self) -> None:
        """End-to-end of the bootstrap → doctor hand-off: a config file whose
        path is set in $AGENT_OBSV_RUNTIME_CONFIG must be picked up by
        ``_resolved_ports``. This is what stops doctor from claiming the
        Collector path is down when an operator moved the bridge port.
        """
        import json
        import os
        import tempfile
        from common import load_runtime_config

        with tempfile.TemporaryDirectory() as tmp:
            cfg_path = Path(tmp) / "runtime-config.json"
            cfg_path.write_text(json.dumps({"bridge_http_port": 24319, "collector_otlp_ports": ["4317", "4318"]}))
            old = os.environ.get("AGENT_OBSV_RUNTIME_CONFIG")
            os.environ["AGENT_OBSV_RUNTIME_CONFIG"] = str(cfg_path)
            try:
                loaded = load_runtime_config()
                self.assertEqual(loaded.get("bridge_http_port"), 24319)
                collector, bridge = doctor._resolved_ports()
                self.assertEqual(bridge, ("24319",))
                self.assertEqual(collector, ("4317", "4318"))
            finally:
                if old is None:
                    os.environ.pop("AGENT_OBSV_RUNTIME_CONFIG", None)
                else:
                    os.environ["AGENT_OBSV_RUNTIME_CONFIG"] = old

    # ----- the central lie-detection case ---------------------------------

    def test_healthz_ok_but_data_plane_dead_is_broken(self) -> None:
        """The exact scenario from the user report: healthz=200, zombie collector, ES empty."""
        fake_healthz = {"status": "pass", "detail": "healthz 200"}
        fake_proc = {
            "status": "fail",
            "detail": "Detected 2 zombie/defunct Collector process(es).",
            "zombies": ["Z 1234 1 otelcol-contrib <defunct>"],
            "listening_ports": {"4317": False, "4318": False, "14319": True},
            "fix": "pkill + relaunch",
        }
        fake_recent = {
            "status": "fail",
            "detail": "No real agent documents in the last 10 minutes.",
            "doc_count": 0,
        }

        with mock.patch.object(doctor, "_probe_healthz", return_value=fake_healthz):
            with mock.patch.object(doctor, "_probe_processes_and_ports", return_value=fake_proc):
                with mock.patch.object(doctor, "_probe_recent_data", return_value=fake_recent):
                    result = doctor.run_doctor(_args())

        self.assertEqual(result["verdict"], "broken")
        # The summary must explicitly flag the lie.
        self.assertIn("healthz", result["summary"].lower())
        self.assertIn("do not trust", result["summary"].lower())

    # ----- healthy path ---------------------------------------------------

    def test_all_pass_is_healthy(self) -> None:
        with mock.patch.object(doctor, "_probe_healthz", return_value={"status": "pass", "detail": "ok"}):
            with mock.patch.object(
                doctor, "_probe_processes_and_ports", return_value={"status": "pass", "detail": "ok"}
            ):
                with mock.patch.object(
                    doctor,
                    "_probe_recent_data",
                    return_value={"status": "pass", "detail": "5 docs", "doc_count": 5},
                ):
                    result = doctor.run_doctor(_args())
        self.assertEqual(result["verdict"], "healthy")
        self.assertIn("live", result["summary"].lower())

    # ----- degraded path --------------------------------------------------

    def test_warn_anywhere_is_degraded(self) -> None:
        with mock.patch.object(doctor, "_probe_healthz", return_value={"status": "pass", "detail": "ok"}):
            with mock.patch.object(
                doctor,
                "_probe_processes_and_ports",
                return_value={"status": "warn", "detail": "only 14319 listening"},
            ):
                with mock.patch.object(
                    doctor,
                    "_probe_recent_data",
                    return_value={"status": "pass", "detail": "3 docs", "doc_count": 3},
                ):
                    result = doctor.run_doctor(_args())
        self.assertEqual(result["verdict"], "degraded")

    # ----- unreachable path ----------------------------------------------

    def test_es_unreachable_is_unreachable_verdict(self) -> None:
        with mock.patch.object(doctor, "_probe_healthz", return_value={"status": "fail", "detail": "no healthz"}):
            with mock.patch.object(
                doctor,
                "_probe_processes_and_ports",
                return_value={"status": "fail", "detail": "no listener"},
            ):
                with mock.patch.object(
                    doctor,
                    "_probe_recent_data",
                    # _probe_recent_data sets es_unreachable=True when the
                    # cluster itself cannot be queried; _aggregate keys off
                    # that structured flag (not string matching on `detail`).
                    return_value={
                        "status": "fail",
                        "detail": "cannot query ES: connection refused",
                        "es_unreachable": True,
                    },
                ):
                    result = doctor.run_doctor(_args())
        self.assertEqual(result["verdict"], "unreachable")

    # ----- probe: recent data filters internal datasets --------------------

    def test_recent_data_filters_internal_datasets(self) -> None:
        from common import ESConfig

        captured: dict = {}

        def fake_es(config, method, path, payload=None):
            captured["payload"] = payload
            return {"hits": {"total": {"value": 7}}, "aggregations": {"by_service": {"buckets": []}}}

        with mock.patch.object(doctor, "es_request", side_effect=fake_es):
            result = doctor._probe_recent_data(
                ESConfig(es_url="http://x"), index_prefix="agent-obsv", freshness_minutes=10
            )
        self.assertEqual(result["status"], "pass")
        # Critical: must exclude internal.* (sanity, canary, alert) or healthy-looking
        # clusters that only contain heartbeats would mask real breakage.
        must_not = result_must_not_clauses(captured["payload"])
        self.assertTrue(
            any(clause.get("prefix", {}).get("event.dataset") == "internal." for clause in must_not),
            f"recent_data must exclude internal.* datasets; got must_not={must_not}",
        )

    # ----- render --------------------------------------------------------

    def test_render_text_flags_healthz_lie(self) -> None:
        fake = {
            "verdict": "broken",
            "summary": "Pipeline is BROKEN on the data plane (processes_and_ports, recent_data)."
            " /healthz is returning 200 but the data plane is dead — do NOT trust healthz as a pipeline indicator."
            " See per-check detail for fix.",
            "index_prefix": "agent-obsv",
            "healthz_url": "h",
            "otlp_http_endpoint": "e",
            "freshness_minutes": 10,
            "checks": {
                "healthz": {"status": "pass", "detail": "200", "warning": "healthz only proves HTTP listener alive"},
                "processes_and_ports": {"status": "fail", "detail": "zombies", "fix": "pkill"},
                "recent_data": {"status": "fail", "detail": "no docs"},
                "canary": {"status": "skipped", "detail": "skipped"},
            },
        }
        text = doctor.render_text(fake)
        self.assertIn("BROKEN", text)
        self.assertIn("do NOT trust", text)
        self.assertIn("→ fix: pkill", text)


def result_must_not_clauses(payload: dict) -> list:
    return payload["query"]["bool"]["must_not"]


class RunCollectorScriptTests(unittest.TestCase):
    """The user-reported root cause was defunct Collectors. Make sure the
    generated launcher supports `--daemon` / `--stop` / `--status`, and that
    `--daemon` uses setsid+nohup so shell exit does not orphan the process."""

    def _render(self):
        return bootstrap_observability.build_collector_run_script(
            collector_bin="otelcol-contrib",
            collector_path=Path("/tmp/otel-collector.generated.yaml"),
            env_path=Path("/tmp/agent-otel.env"),
        )

    def test_supports_daemon_stop_status_foreground(self) -> None:
        script = self._render()
        for token in ["--daemon", "--stop", "--status", "foreground"]:
            self.assertIn(token, script, f"missing {token}")

    def test_daemon_mode_uses_setsid_and_nohup(self) -> None:
        script = self._render()
        self.assertIn("setsid nohup", script)
        # PID file must be written so --stop works.
        self.assertIn("PIDFILE=", script)
        # Log redirection so the user can find out why it died.
        self.assertIn("LOGFILE=", script)

    def test_bridge_launcher_has_same_daemon_contract(self) -> None:
        bridge_script = bootstrap_observability.build_bridge_run_script(
            bridge_path=Path("/tmp/otlphttpbridge.py"),
            env_path=Path("/tmp/agent-otel-bridge.env"),
        )
        for token in ["--daemon", "--stop", "--status", "setsid nohup"]:
            self.assertIn(token, bridge_script)

    def test_stop_escalates_to_sigkill_after_timeout(self) -> None:
        """--stop must SIGTERM first, then escalate to SIGKILL if the process
        does not exit within the grace window. Prevents zombie accumulation."""
        script = self._render()
        self.assertIn("kill -9", script)
        self.assertIn("SIGKILL", script)
        # The grace loop must exist (sleep inside a for)
        self.assertIn("for i in", script)

    def test_bridge_stop_also_escalates(self) -> None:
        bridge_script = bootstrap_observability.build_bridge_run_script(
            bridge_path=Path("/tmp/otlphttpbridge.py"),
            env_path=Path("/tmp/agent-otel-bridge.env"),
        )
        self.assertIn("kill -9", bridge_script)
        self.assertIn("SIGKILL", bridge_script)

    def test_status_cleans_stale_pidfile(self) -> None:
        """--status must remove a stale PID file pointing at a dead process
        instead of leaving it around to confuse subsequent --daemon calls."""
        script = self._render()
        # The 'not running' branch in --status must rm -f the pidfile
        self.assertIn('rm -f "$PIDFILE"', script)


if __name__ == "__main__":
    unittest.main()
