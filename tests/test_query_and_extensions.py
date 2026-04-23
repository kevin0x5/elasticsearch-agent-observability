"""Tests for query.py templates, multi-agent correlation schema, trace
timeline Kibana object, and log severity filter governance."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = REPO_ROOT / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import query  # noqa: E402
import render_collector_config  # noqa: E402
import render_es_assets  # noqa: E402

DISCOVERY_SAMPLE = {
    "files_scanned": 12,
    "detected_modules": [{"module_kind": "tool_registry"}],
    "recommended_ingest_modes": [{"mode": "collector", "score": 0.94}],
}


class QueryTemplateTests(unittest.TestCase):
    """Verify that query builders produce valid ES payloads."""

    def test_trace_query_filters_by_trace_id(self) -> None:
        path, payload = query.query_trace("agent-obsv-events", "trace-abc")
        self.assertIn("agent-obsv-events", path)
        term = payload["query"]["bool"]["must"][0]["term"]["trace.id"]
        self.assertEqual(term, "trace-abc")
        self.assertEqual(payload["sort"][0]["@timestamp"], "asc")

    def test_tools_query_aggregates_by_tool_name(self) -> None:
        _, payload = query.query_tools("agent-obsv-events", "now-1h")
        self.assertIn("tools", payload["aggs"])
        self.assertEqual(payload["size"], 0)

    def test_errors_query_filters_failures(self) -> None:
        _, payload = query.query_errors("agent-obsv-events", "now-6h")
        must = payload["query"]["bool"]["must"]
        self.assertTrue(any(c.get("term", {}).get("event.outcome") == "failure" for c in must))

    def test_sessions_query_groups_by_conversation_id(self) -> None:
        _, payload = query.query_sessions("agent-obsv-events", "now-24h")
        self.assertIn("sessions", payload["aggs"])

    def test_timeline_query_searches_agent_run_id(self) -> None:
        _, payload = query.query_timeline("agent-obsv-events", "run-xyz")
        should = payload["query"]["bool"]["should"]
        ids = [c.get("term", {}).get("gen_ai.agent.id") or c.get("term", {}).get("trace.id") for c in should]
        self.assertIn("run-xyz", ids)

    def test_all_queries_exclude_internal_datasets(self) -> None:
        queries = [
            query.query_trace("idx", "t"),
            query.query_tools("idx", "now-1h"),
            query.query_errors("idx", "now-1h"),
            query.query_sessions("idx", "now-1h"),
            query.query_timeline("idx", "r"),
        ]
        for _, payload in queries:
            must_not = payload["query"]["bool"].get("must_not", [])
            self.assertTrue(
                any(c.get("term", {}).get("event.dataset") == "internal.sanity_check" for c in must_not),
                f"query must exclude internal datasets: {payload}",
            )


class MultiAgentCorrelationSchemaTests(unittest.TestCase):
    """Verify multi-agent fields exist in the component template."""

    def test_parent_agent_id_in_schema(self) -> None:
        component = render_es_assets.build_component_template_ecs_base("agent-obsv")
        props = component["template"]["mappings"]["properties"]
        self.assertIn("gen_ai.agent_ext.parent_agent.id", props)
        self.assertEqual(props["gen_ai.agent_ext.parent_agent.id"]["type"], "keyword")

    def test_causality_trigger_span_id_in_schema(self) -> None:
        component = render_es_assets.build_component_template_ecs_base("agent-obsv")
        props = component["template"]["mappings"]["properties"]
        self.assertIn("gen_ai.agent_ext.causality.trigger_span_id", props)

    def test_delegation_target_in_schema(self) -> None:
        component = render_es_assets.build_component_template_ecs_base("agent-obsv")
        props = component["template"]["mappings"]["properties"]
        self.assertIn("gen_ai.agent_ext.delegation_target", props)


class TraceTimelineKibanaTests(unittest.TestCase):
    """Verify the trace timeline saved search is included in Kibana objects."""

    def test_trace_timeline_search_exists(self) -> None:
        bundle = render_es_assets.build_kibana_saved_objects("agent-obsv")
        ids = {obj["id"] for obj in bundle["objects"]}
        self.assertIn("agent-obsv-trace-timeline", ids)

    def test_trace_timeline_has_trace_columns(self) -> None:
        bundle = render_es_assets.build_kibana_saved_objects("agent-obsv")
        timeline = next(o for o in bundle["objects"] if o["id"] == "agent-obsv-trace-timeline")
        columns = timeline["attributes"]["columns"]
        self.assertIn("event.action", columns)
        self.assertIn("gen_ai.agent_ext.component_type", columns)
        self.assertIn("span.id", columns)

    def test_trace_timeline_in_dashboard_panels(self) -> None:
        bundle = render_es_assets.build_kibana_saved_objects("agent-obsv")
        dashboard = next(o for o in bundle["objects"] if o["type"] == "dashboard")
        ref_ids = {r["id"] for r in dashboard.get("references", [])}
        self.assertIn("agent-obsv-trace-timeline", ref_ids)


class LogSeverityFilterTests(unittest.TestCase):
    """Verify log severity filter processor in Collector config."""

    def test_no_severity_filter_by_default(self) -> None:
        rendered = render_collector_config.render_config(
            DISCOVERY_SAMPLE,
            es_url="http://localhost:9200",
            index_prefix="agent-obsv",
            environment="dev",
            service_name="agent-runtime",
        )
        self.assertNotIn("filter/log_severity", rendered)

    def test_severity_filter_added_when_set(self) -> None:
        rendered = render_collector_config.render_config(
            DISCOVERY_SAMPLE,
            es_url="http://localhost:9200",
            index_prefix="agent-obsv",
            environment="dev",
            service_name="agent-runtime",
            log_min_severity="WARN",
        )
        self.assertIn("filter/log_severity", rendered)
        self.assertIn("SEVERITY_NUMBER_WARN", rendered)

    def test_severity_filter_in_logs_pipeline_only(self) -> None:
        rendered = render_collector_config.render_config(
            DISCOVERY_SAMPLE,
            es_url="http://localhost:9200",
            index_prefix="agent-obsv",
            environment="dev",
            service_name="agent-runtime",
            log_min_severity="ERROR",
        )
        # filter/log_severity must be in logs pipeline processors, not in traces or metrics
        self.assertIn("filter/log_severity, batch]\n      exporters: [elasticsearch/events]", rendered)
        # Must NOT be in traces pipeline
        traces_line = [l for l in rendered.split("\n") if "traces:" in l and "receivers" not in l]
        for line in rendered.split("\n"):
            if "exporters: [elasticsearch/events, spanmetrics]" in line:
                # This is the traces pipeline exporter line — check the processor line above
                idx = rendered.split("\n").index(line)
                processors_line = rendered.split("\n")[idx - 1]
                self.assertNotIn("filter/log_severity", processors_line)

    def test_trace_level_does_not_add_filter(self) -> None:
        rendered = render_collector_config.render_config(
            DISCOVERY_SAMPLE,
            es_url="http://localhost:9200",
            index_prefix="agent-obsv",
            environment="dev",
            service_name="agent-runtime",
            log_min_severity="TRACE",
        )
        self.assertNotIn("filter/log_severity", rendered)


if __name__ == "__main__":
    unittest.main()
