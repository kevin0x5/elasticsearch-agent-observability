#!/usr/bin/env python3
"""Live integration tests against a real Elasticsearch cluster.

Run with:
  python3 -m pytest tests/test_integration_live.py -v

Requires a live ES cluster + OTLP bridge. Skips gracefully if unavailable.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import time
import unittest
import urllib.request
import base64
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = REPO_ROOT / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

ES_URL = os.environ.get("ES_URL", "http://21.214.66.165:9201")
ES_USER = os.environ.get("ES_USER", "elastic")
ES_PASSWORD = os.environ.get("ES_PASSWORD", "wFfJMfUGNmPAl-HUzeeG")
OTLP_ENDPOINT = os.environ.get("OTLP_ENDPOINT", "http://127.0.0.1:14319")
INDEX_PREFIX = os.environ.get("INDEX_PREFIX", "agent-obsv-inttest")
BRIDGE_PREFIX = os.environ.get("BRIDGE_PREFIX", "agent-obsv")  # bridge writes here
SKIP = os.environ.get("SKIP_LIVE_TESTS", "").lower() in ("1", "true", "yes")


# ---------------------------------------------------------------------------
# Skip helpers
# ---------------------------------------------------------------------------
def _es_reachable() -> bool:
    try:
        req = urllib.request.Request(ES_URL + "/")
        token = base64.b64encode(f"{ES_USER}:{ES_PASSWORD}".encode()).decode()
        req.add_header("Authorization", f"Basic {token}")
        urllib.request.urlopen(req, timeout=5)
        return True
    except Exception:
        return False


def _bridge_reachable() -> bool:
    try:
        urllib.request.urlopen(OTLP_ENDPOINT + "/healthz", timeout=3)
        return True
    except Exception:
        return False


def skip_if_no_cluster(fn):
    import functools
    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        if SKIP or not _es_reachable():
            raise unittest.SkipTest("ES cluster not available")
        return fn(*args, **kwargs)
    return wrapper


def skip_if_no_bridge(fn):
    import functools
    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        if SKIP or not _bridge_reachable():
            raise unittest.SkipTest("OTLP bridge not running")
        return fn(*args, **kwargs)
    return wrapper


# ---------------------------------------------------------------------------
# Imports (after path setup)
# ---------------------------------------------------------------------------
import alert_and_diagnose
import apply_elasticsearch_assets
import generate_report
import query as query_module
import render_es_assets
import status as status_module
import uninstall
import verify_pipeline
from common import (
    ESConfig, SkillError,
    build_data_stream_name, es_request, validate_credential_pair,
)


def _config() -> ESConfig:
    creds = validate_credential_pair(ES_USER, ES_PASSWORD)
    return ESConfig(
        es_url=ES_URL,
        es_user=creds[0] if creds else None,
        es_password=creds[1] if creds else None,
        verify_tls=False,
    )


def _send_otlp(body_json: str, ts_ns: int = 1745400000000000000) -> dict:
    payload = json.dumps({
        "resourceLogs": [{
            "resource": {"attributes": [
                {"key": "service.name", "value": {"stringValue": "inttest"}}
            ]},
            "scopeLogs": [{"logRecords": [{
                "timeUnixNano": str(ts_ns),
                "body": {"stringValue": body_json},
                "attributes": [],
            }]}],
        }]
    }).encode()
    req = urllib.request.Request(
        OTLP_ENDPOINT + "/v1/logs", data=payload, method="POST",
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=10) as r:
        return json.loads(r.read())


def _poll(config: ESConfig, query: dict, max_attempts: int = 12, sleep: float = 0.5) -> dict | None:
    # Bridge writes to BRIDGE_PREFIX, not INDEX_PREFIX
    ds = build_data_stream_name(BRIDGE_PREFIX)
    for _ in range(max_attempts):
        try:
            r = es_request(config, "POST", f"/{ds}*/_search", query)
            hits = r.get("hits", {}).get("hits", [])
            if hits:
                return hits[0]["_source"]
        except SkillError:
            pass
        time.sleep(sleep)
    return None


# ===========================================================================
# Cluster connectivity
# ===========================================================================

class ClusterConnectivityTests(unittest.TestCase):

    @skip_if_no_cluster
    def test_es_version_9x(self) -> None:
        config = _config()
        result = es_request(config, "GET", "/")
        version = result.get("version", {}).get("number", "")
        self.assertTrue(version.startswith("9."), f"Expected ES 9.x, got {version}")

    @skip_if_no_cluster
    def test_cluster_health_not_red(self) -> None:
        config = _config()
        result = es_request(config, "GET", "/_cluster/health")
        self.assertNotEqual(result.get("status"), "red")

    @skip_if_no_bridge
    def test_bridge_healthz(self) -> None:
        req = urllib.request.Request(OTLP_ENDPOINT + "/healthz")
        with urllib.request.urlopen(req, timeout=5) as r:
            data = json.loads(r.read())
        self.assertEqual(data.get("status"), "ok")


# ===========================================================================
# Asset lifecycle: render → apply → status → uninstall
# ===========================================================================

class AssetLifecycleTests(unittest.TestCase):

    @classmethod
    def setUpClass(cls) -> None:
        if SKIP or not _es_reachable():
            return
        # Pre-clean test prefix
        config = _config()
        from common import asset_names
        names = asset_names(INDEX_PREFIX)
        for path in [
            f"/_data_stream/{names['data_stream']}",
            f"/_index_template/{names['index_template']}",
            f"/_ingest/pipeline/{names['ingest_pipeline']}",
            f"/_ilm/policy/{names['ilm_policy']}",
            f"/_component_template/{INDEX_PREFIX}-ecs-base",
            f"/_component_template/{INDEX_PREFIX}-settings",
        ]:
            try:
                es_request(config, "DELETE", path)
            except SkillError:
                pass
        # Apply fresh assets so all tests in this class start from a known state.
        with tempfile.TemporaryDirectory() as tmpdir:
            out = Path(tmpdir)
            discovery = {
                "detected_modules": [{"module_kind": "tool_registry"}],
                "files_scanned": 10,
                "recommended_ingest_modes": [{"mode": "collector", "score": 0.94}],
            }
            try:
                render_es_assets.render_assets(discovery, out, index_prefix=INDEX_PREFIX, retention_days=30)
                apply_elasticsearch_assets.apply_assets(
                    config, assets_dir=out, index_prefix=INDEX_PREFIX,
                    bootstrap_index=True, apply_kibana=False, dry_run=False,
                )
            except Exception:
                pass

    @skip_if_no_cluster
    def test_render_and_apply_creates_all_assets(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            out = Path(tmpdir)
            discovery = {
                "detected_modules": [{"module_kind": "tool_registry"}],
                "files_scanned": 10,
                "recommended_ingest_modes": [{"mode": "collector", "score": 0.94}],
            }
            render_es_assets.render_assets(
                discovery, out,
                index_prefix=INDEX_PREFIX, retention_days=30,
            )
            config = _config()
            result = apply_elasticsearch_assets.apply_assets(
                config,
                assets_dir=out,
                index_prefix=INDEX_PREFIX,
                bootstrap_index=False,
                apply_kibana=False,
                dry_run=False,
            )
            responses = result.get("responses", {})
            for key in ("ilm_policy", "ingest_pipeline",
                        "component_template_ecs_base", "component_template_settings",
                        "index_template"):
                resp = responses.get(key, {})
                self.assertTrue(
                    resp.get("acknowledged") or "acknowledged" in str(resp),
                    f"{key}: ES did not acknowledge — got {resp!r}",
                )

    @skip_if_no_cluster
    def test_sanity_check_passes_after_apply(self) -> None:
        config = _config()
        result = apply_elasticsearch_assets.sanity_check(config, index_prefix=INDEX_PREFIX)
        self.assertEqual(result.get("status"), "passed",
                         f"sanity_check failed: {result}")
        self.assertTrue(result.get("pipeline_applied"),
                        "observer.product not set — ingest pipeline not applied correctly")

    @skip_if_no_cluster
    def test_run_status_reports_ready(self) -> None:
        config = _config()
        result = status_module.run_status(config, index_prefix=INDEX_PREFIX)
        overall = result.get("overall", "")
        self.assertEqual(overall, "ready",
                         f"status.run_status returned {overall!r}: {result}")

    @skip_if_no_cluster
    def test_run_uninstall_dry_run_produces_plan(self) -> None:
        config = _config()
        result = uninstall.run_uninstall(
            config,
            index_prefix=INDEX_PREFIX,
            confirm=False,
            keep_data_stream=False,
            kibana_url="",
            kibana_space="default",
            kibana_assets_file="",
            force=False,
        )
        self.assertTrue(result.get("dry_run"), "Expected dry_run=True when confirm=False")
        self.assertIn("plan", result)
        self.assertGreater(len(result["plan"]), 0)


# ===========================================================================
# Ingest pipeline field behavior
# ===========================================================================

class IngestPipelineFieldTests(unittest.TestCase):
    """Verify the ingest pipeline flattens JSON body into dotted keys."""

    @classmethod
    def setUpClass(cls) -> None:
        if SKIP or not _es_reachable() or not _bridge_reachable():
            return
        # Ensure assets are applied
        config = _config()
        with tempfile.TemporaryDirectory() as tmpdir:
            out = Path(tmpdir)
            discovery = {
                "detected_modules": [{"module_kind": "tool_registry"}],
                "files_scanned": 10,
                "recommended_ingest_modes": [{"mode": "collector", "score": 0.94}],
            }
            try:
                render_es_assets.render_assets(discovery, out, index_prefix=INDEX_PREFIX, retention_days=30)
                apply_elasticsearch_assets.apply_assets(
                    config, assets_dir=out, index_prefix=INDEX_PREFIX,
                    bootstrap_index=False, apply_kibana=False, dry_run=False,
                )
            except Exception:
                pass

    @skip_if_no_bridge
    def test_nested_json_flattened_to_dotted_keys(self) -> None:
        body = json.dumps({
            "event": {"action": "tool_call", "outcome": "failure"},
            "error": {"type": "TimeoutError"},
            "gen_ai": {
                "tool": {"name": "web_search"},
                "request": {"model": "gpt-5"},
                "usage": {"input_tokens": 500, "output_tokens": 0},
                "agent_ext": {"latency_ms": 5000.0, "component_type": "tool", "retry_count": 3},
                "conversation": {"id": "sess-flat-001"},
                "agent": {"id": "run-flat-001"},
            },
            "trace": {"id": "inttest-trace-flatten"},
            "span": {"id": "span-001"},
        })
        _send_otlp(body, ts_ns=1745404000000000000)
        config = _config()
        doc = _poll(config, {"size": 1, "query": {"term": {"trace.id": "inttest-trace-flatten"}}})
        self.assertIsNotNone(doc, "Document not found in ES")

        expected = {
            "event.outcome": "failure",
            "error.type": "TimeoutError",
            "gen_ai.tool.name": "web_search",
            "gen_ai.request.model": "gpt-5",
            "gen_ai.usage.input_tokens": 500,
            "gen_ai.agent_ext.latency_ms": 5000.0,
            "gen_ai.agent_ext.component_type": "tool",
            "gen_ai.agent_ext.retry_count": 3,
            "gen_ai.conversation.id": "sess-flat-001",
            "trace.id": "inttest-trace-flatten",
        }
        for field, val in expected.items():
            self.assertIn(field, doc, f"flat key '{field}' missing from ES doc")
            self.assertEqual(doc[field], val, f"field '{field}': expected {val!r}, got {doc[field]!r}")

    @skip_if_no_bridge
    def test_4_level_nesting_flattened(self) -> None:
        body = json.dumps({
            "event": {"action": "delegation", "outcome": "success"},
            "gen_ai": {
                "agent_ext": {
                    "parent_agent": {"id": "root-agent-001"},
                    "causality": {"trigger_span_id": "span-parent-001"},
                },
                "agent": {"id": "child-agent-001"},
            },
            "trace": {"id": "inttest-trace-4level"},
        })
        _send_otlp(body, ts_ns=1745404001000000000)
        config = _config()
        doc = _poll(config, {"size": 1, "query": {"term": {"trace.id": "inttest-trace-4level"}}})
        self.assertIsNotNone(doc)
        self.assertIn("gen_ai.agent_ext.parent_agent.id", doc,
                      "4-level nested field not flattened")
        self.assertEqual(doc["gen_ai.agent_ext.parent_agent.id"], "root-agent-001")

    @skip_if_no_bridge
    def test_unknown_root_key_goes_to_labels_unmapped(self) -> None:
        body = json.dumps({
            "event": {"action": "tool_call", "outcome": "success"},
            "my_custom_field": "should_be_unmapped",
            "another_unknown": 42,
            "gen_ai": {"agent": {"id": "run-unmapped"}},
            "trace": {"id": "inttest-trace-unmapped"},
        })
        _send_otlp(body, ts_ns=1745404002000000000)
        config = _config()
        doc = _poll(config, {"size": 1, "query": {"term": {"trace.id": "inttest-trace-unmapped"}}})
        self.assertIsNotNone(doc)
        unmapped = doc.get("labels", {}).get("unmapped", {})
        self.assertIn("my_custom_field", unmapped, "custom field not in labels.unmapped")
        self.assertEqual(unmapped["my_custom_field"], "should_be_unmapped")
        self.assertNotIn("my_custom_field", doc)

    @skip_if_no_bridge
    def test_sensitive_fields_redacted(self) -> None:
        import uuid
        unique_trace = f"inttest-redact-{uuid.uuid4().hex[:8]}"
        ts_now = int(time.time() * 1_000_000_000)
        body = json.dumps({
            "event": {"action": "tool_call", "outcome": "success"},
            "gen_ai": {
                "prompt": "secret_prompt",
                "completion": "secret_completion",
                "agent": {"id": "run-redact"},
            },
            "trace": {"id": unique_trace},
        })
        _send_otlp(body, ts_ns=ts_now)
        config = _config()
        doc = _poll(config, {"size": 1, "query": {"term": {"trace.id": unique_trace}}})
        self.assertIsNotNone(doc, "Redact test document not found in ES")
        for f in ("gen_ai.prompt", "gen_ai.completion"):
            self.assertNotIn(f, doc, f"Sensitive field {f!r} not redacted!")

    @skip_if_no_bridge
    def test_observer_product_stamped(self) -> None:
        body = json.dumps({
            "event": {"action": "ping"},
            "trace": {"id": "inttest-trace-observer"},
        })
        _send_otlp(body, ts_ns=1745404004000000000)
        config = _config()
        doc = _poll(config, {"size": 1, "query": {"term": {"trace.id": "inttest-trace-observer"}}})
        self.assertIsNotNone(doc)
        # observer.product may be stored as flat key or nested object depending on path
        obs_product = (
            doc.get("observer.product")
            or doc.get("observer", {}).get("product")
        )
        self.assertEqual(obs_product, "elasticsearch-agent-observability",
                         f"observer.product not set: doc keys={list(doc.keys())}")


# ===========================================================================
# Verify pipeline
# ===========================================================================

class VerifyPipelineTests(unittest.TestCase):

    @skip_if_no_bridge
    def test_canary_ok(self) -> None:
        result = verify_pipeline.run_verify(
            es_url=ES_URL,
            es_user=ES_USER,
            es_password=ES_PASSWORD,
            index_prefix=BRIDGE_PREFIX,
            otlp_http_endpoint=OTLP_ENDPOINT,
            no_verify_tls=True,
        )
        self.assertEqual(result["verdict"], "ok",
                         f"verify not ok: {result['verdict']}\n{result.get('next_step', '')}")

    @skip_if_no_cluster
    def test_bad_port_gives_transport_unreachable(self) -> None:
        result = verify_pipeline.run_verify(
            es_url=ES_URL,
            es_user=ES_USER,
            es_password=ES_PASSWORD,
            index_prefix=BRIDGE_PREFIX,
            otlp_http_endpoint="http://127.0.0.1:19999",
            no_verify_tls=True,
            poll_attempts=1,
            poll_backoff=0.1,
        )
        self.assertEqual(result["verdict"], "transport_unreachable")

    @skip_if_no_bridge
    def test_bad_es_credentials_gives_sent_but_lost(self) -> None:
        """Canary is sent but ES write fails — should be sent_but_lost or contract_broken."""
        result = verify_pipeline.run_verify(
            es_url=ES_URL,
            es_user="wrong_user",
            es_password="wrong_pass",
            index_prefix=BRIDGE_PREFIX,
            otlp_http_endpoint=OTLP_ENDPOINT,
            no_verify_tls=True,
            poll_attempts=3,
            poll_backoff=0.5,
        )
        # With wrong credentials, either we can't connect to ES or canary lands nowhere
        self.assertIn(result["verdict"],
                      ("transport_unreachable", "sent_but_lost", "unreachable"),
                      f"Unexpected verdict with bad creds: {result['verdict']}")


# ===========================================================================
# Query templates
# ===========================================================================

class QueryTemplateTests(unittest.TestCase):

    @skip_if_no_cluster
    def test_trace_query_hits_known_trace(self) -> None:
        config = _config()
        # Bridge data lands in BRIDGE_PREFIX
        ds = build_data_stream_name(BRIDGE_PREFIX)
        path, payload = query_module.query_trace(ds, "inttest-trace-flatten")
        result = es_request(config, "POST", path, payload)
        total = result["hits"]["total"]["value"]
        self.assertGreaterEqual(total, 1)

    @skip_if_no_cluster
    def test_errors_query_only_returns_failure_docs(self) -> None:
        config = _config()
        ds = build_data_stream_name(BRIDGE_PREFIX)
        path, payload = query_module.query_errors(ds, "now-3y", size=20)
        result = es_request(config, "POST", path, payload)
        for hit in result["hits"]["hits"]:
            src = hit["_source"]
            # outcome may be flat key or nested depending on ingestion path
            outcome = src.get("event.outcome") or src.get("event", {}).get("outcome")
            if outcome is None:
                continue  # old-format doc, skip
            self.assertEqual(outcome, "failure",
                             f"errors query returned non-failure doc: outcome={outcome!r}")

    @skip_if_no_cluster
    def test_tools_agg_runs_without_error(self) -> None:
        config = _config()
        ds = build_data_stream_name(BRIDGE_PREFIX)
        path, payload = query_module.query_tools(ds, "now-3y")
        result = es_request(config, "POST", path, payload)
        self.assertIn("aggregations", result)
        self.assertIn("tools", result["aggregations"])

    @skip_if_no_cluster
    def test_sessions_agg_runs_without_error(self) -> None:
        config = _config()
        ds = build_data_stream_name(BRIDGE_PREFIX)
        path, payload = query_module.query_sessions(ds, "now-3y")
        result = es_request(config, "POST", path, payload)
        self.assertIn("aggregations", result)

    @skip_if_no_cluster
    def test_timeline_returns_chronological_order(self) -> None:
        config = _config()
        ds = build_data_stream_name(BRIDGE_PREFIX)
        path, payload = query_module.query_timeline(ds, "run-flat-001")
        result = es_request(config, "POST", path, payload)
        hits = result["hits"]["hits"]
        if len(hits) >= 2:
            ts = [h["_source"].get("@timestamp", "") for h in hits]
            self.assertEqual(ts, sorted(ts), "timeline not chronological")


# ===========================================================================
# Alert and diagnose
# ===========================================================================

class AlertTests(unittest.TestCase):

    @skip_if_no_cluster
    def test_run_alert_check_completes(self) -> None:
        config = _config()
        result = alert_and_diagnose.run_alert_check(
            config,
            index_prefix=BRIDGE_PREFIX,
            time_range="now-3y",
            baseline_range="now-6y",
            error_threshold=5,
            p95_latency_threshold_ms=10000,
            token_threshold_multiplier=5.0,
        )
        self.assertIn("alerts", result)
        self.assertIn("checked_at", result)
        self.assertIsInstance(result["alerts"], list)

    @skip_if_no_cluster
    def test_percentiles_order_key_p95_95_is_valid(self) -> None:
        """Regression: ES 9.x requires 'p95.95' not 'p95' for percentiles order."""
        config = _config()
        try:
            alert_and_diagnose.run_alert_check(
                config,
                index_prefix=BRIDGE_PREFIX,
                time_range="now-3y",
                baseline_range="now-6y",
                error_threshold=5,
                p95_latency_threshold_ms=10000,
                token_threshold_multiplier=5.0,
            )
        except SkillError as exc:
            self.fail(f"alert_and_diagnose raised SkillError (aggregation bug?): {exc}")


# ===========================================================================
# Generate report
# ===========================================================================

class GenerateReportTests(unittest.TestCase):

    @skip_if_no_cluster
    def test_build_report_returns_expected_keys(self) -> None:
        config = _config()
        ds = build_data_stream_name(BRIDGE_PREFIX)
        payload = generate_report.search_payload("now-3y")
        raw = es_request(config, "POST", f"/{ds}*/_search", payload)
        report = generate_report.build_report(raw)
        for key in ("documents", "success_rate", "top_tools", "top_models"):
            self.assertIn(key, report, f"'{key}' missing from report")
        self.assertIsInstance(report["documents"], int)

    @skip_if_no_cluster
    def test_render_markdown_produces_non_empty_string(self) -> None:
        config = _config()
        ds = build_data_stream_name(BRIDGE_PREFIX)
        payload = generate_report.search_payload("now-3y")
        raw = es_request(config, "POST", f"/{ds}*/_search", payload)
        report = generate_report.build_report(raw)
        cfg = {"events_alias": ds, "time_field": "@timestamp"}
        md = generate_report.render_markdown(report, cfg)
        self.assertIsInstance(md, str)
        self.assertGreater(len(md), 50)
        self.assertIn("Agent Observability Report", md)


# ===========================================================================
# Data integrity / mapping
# ===========================================================================

class MappingIntegrityTests(unittest.TestCase):

    def _props(self) -> dict:
        config = _config()
        # Use INDEX_PREFIX (inttest) — assets were applied there
        ds = build_data_stream_name(INDEX_PREFIX)
        result = es_request(config, "GET", f"/{ds}/_mapping")
        for meta in result.values():
            return meta.get("mappings", {}).get("properties", {})
        return {}

    @skip_if_no_cluster
    def test_root_dynamic_is_false(self) -> None:
        config = _config()
        ds = build_data_stream_name(INDEX_PREFIX)
        result = es_request(config, "GET", f"/{ds}/_mapping")
        for meta in result.values():
            dynamic = meta.get("mappings", {}).get("dynamic")
            self.assertIn(str(dynamic).lower(), ("false", "strict"),
                          f"Root dynamic not false: {dynamic!r}")
            break

    @skip_if_no_cluster
    def test_event_outcome_is_keyword(self) -> None:
        """ES stores mapping as nested objects: event.properties.outcome."""
        props = self._props()
        event_props = props.get("event", {}).get("properties", {})
        self.assertIn("outcome", event_props, "event.outcome missing from mapping")
        self.assertEqual(event_props["outcome"]["type"], "keyword")

    @skip_if_no_cluster
    def test_gen_ai_tool_name_is_keyword(self) -> None:
        props = self._props()
        tool = (props.get("gen_ai", {}).get("properties", {})
                .get("tool", {}).get("properties", {}))
        self.assertIn("name", tool, "gen_ai.tool.name missing from mapping")
        self.assertEqual(tool["name"]["type"], "keyword")

    @skip_if_no_cluster
    def test_labels_unmapped_is_flattened(self) -> None:
        props = self._props()
        labels = props.get("labels", {}).get("properties", {})
        self.assertIn("unmapped", labels, "labels.unmapped missing from mapping")
        self.assertEqual(labels["unmapped"]["type"], "flattened")

    @skip_if_no_cluster
    def test_multi_agent_fields_in_mapping(self) -> None:
        props = self._props()
        agent_ext = (props.get("gen_ai", {}).get("properties", {})
                     .get("agent_ext", {}).get("properties", {}))
        self.assertIn("parent_agent", agent_ext)
        self.assertIn("id", agent_ext["parent_agent"].get("properties", {}))
        self.assertIn("causality", agent_ext)
        self.assertIn("delegation_target", agent_ext)


if __name__ == "__main__":
    unittest.main(verbosity=2)
