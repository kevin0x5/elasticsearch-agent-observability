"""Simulate the ingest pipeline in pure Python.

This test replicates the Painless processor chain from ``render_es_assets.build_ingest_pipeline``
at the Python level, then feeds every event in ``contract_test_events.json`` through it.

The goal is *integration-level confidence* in the field-routing and redaction contract
without requiring a running Elasticsearch cluster. If the Painless script or processor
list changes in a way that breaks the contract, these tests catch it before the change
reaches a real cluster.
"""

from __future__ import annotations

import copy
import json
import sys
import unittest
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = REPO_ROOT / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import render_es_assets  # noqa: E402


CONTRACT_EVENTS_PATH = REPO_ROOT / "references" / "contract_test_events.json"

# The set of root-level keys the Painless script considers "known".
KNOWN_ROOTS = frozenset([
    "@timestamp", "message", "event", "service", "agent", "trace", "span",
    "parent", "transaction", "observer", "host", "labels", "gen_ai", "error", "log",
])

SENSITIVE_FIELDS = frozenset([
    "gen_ai.prompt",
    "gen_ai.completion",
    "gen_ai.tool.call.arguments",
    "gen_ai.tool.call.result",
])


# ---------------------------------------------------------------------------
# Python reimplementation of the Painless processor chain
# ---------------------------------------------------------------------------

def _route_unknown(ctx: dict, key: str, value: Any) -> None:
    """Route an unknown top-level key into labels.unmapped."""
    ctx.setdefault("labels", {})
    ctx["labels"].setdefault("unmapped", {})
    ctx["labels"]["unmapped"][key] = value


def _flatten_into(target: dict, incoming: dict, prefix: str = "") -> None:
    """Flatten nested dicts into dotted keys in target, matching the Painless
    script behavior. Unknown top-level keys → labels.unmapped."""
    for key, value in incoming.items():
        if not isinstance(key, str):
            continue
        full_key = f"{prefix}{key}" if prefix else key

        # Already dotted key from upstream
        if not prefix and "." in key:
            if full_key not in target or target[full_key] is None:
                target[full_key] = value
            continue

        # Top-level unknown root → labels.unmapped
        if not prefix and key not in KNOWN_ROOTS:
            _route_unknown(target, key, value)
            continue

        # Non-Map value → set as dotted key if absent
        if not isinstance(value, dict):
            if full_key not in target or target[full_key] is None:
                target[full_key] = value
            continue

        # Map value → recurse to flatten deeper
        _flatten_into(target, value, full_key + ".")


def simulate_pipeline(doc: dict[str, Any]) -> dict[str, Any]:
    """Run the full processor chain on a document clone."""
    ctx = copy.deepcopy(doc)

    # 1. observer.product + observer.type
    ctx["observer.product"] = "elasticsearch-agent-observability"
    ctx["observer.type"] = "agent-observability"

    # 2. @timestamp default
    ctx.setdefault("@timestamp", "simulated-timestamp")

    # 3. event defaults
    ctx.setdefault("event.kind", "event")
    ctx.setdefault("event.category", "process")

    # 4. event.duration from latency_ms (simplified — skip nested nav)
    latency = ctx.get("gen_ai.agent_ext.latency_ms")
    if latency is not None and ctx.get("event.duration") is None:
        ctx["event.duration"] = int(latency * 1_000_000)

    # 5. event.outcome default
    if ctx.get("event.outcome") is None:
        ctx["event.outcome"] = "failure" if ctx.get("error.type") else "success"

    # 6. JSON body parsing
    msg = ctx.get("message", "")
    parsed = None
    if isinstance(msg, str):
        try:
            parsed = json.loads(msg)
        except (json.JSONDecodeError, TypeError):
            pass

    # 7. Merge parsed message (flatten nested maps into dotted keys)
    if isinstance(parsed, dict):
        ctx["_parsed_message"] = parsed
        _flatten_into(ctx, parsed)
        ctx.pop("_parsed_message", None)

    # 8. Redact sensitive fields
    for field in SENSITIVE_FIELDS:
        ctx.pop(field, None)

    return ctx


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

def _resolve_field(doc: dict[str, Any], dotted_key: str) -> tuple[bool, Any]:
    """Look up a dotted key in a doc that may contain both flat dotted keys
    and nested dicts (as produced by the Painless merge).

    Returns (found, value). Checks flat key first, then nested path."""
    # 1. Flat dotted key
    if dotted_key in doc:
        return True, doc[dotted_key]
    # 2. Nested path
    parts = dotted_key.split(".")
    current: Any = doc
    for part in parts:
        if isinstance(current, dict) and part in current:
            current = current[part]
        else:
            return False, None
    return True, current


def _field_absent(doc: dict[str, Any], dotted_key: str) -> bool:
    """Return True if a dotted key is absent from both flat and nested form."""
    found, _ = _resolve_field(doc, dotted_key)
    return not found


class PipelineSimulateTests(unittest.TestCase):
    """Run every contract event through the simulated pipeline and verify
    the expected-after-pipeline fields are present with correct values."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.events = json.loads(CONTRACT_EVENTS_PATH.read_text(encoding="utf-8"))
        cls.pipeline = render_es_assets.build_ingest_pipeline(["tool_registry", "model_adapter"])

    def test_contract_events_file_exists(self) -> None:
        self.assertGreater(len(self.events), 0)

    def test_all_contract_events_match_expected_output(self) -> None:
        for i, event in enumerate(self.events):
            comment = event.get("_comment", f"event #{i}")
            input_doc = event["input"]
            expected = event.get("expected_after_pipeline", {})
            expected_absent = event.get("expected_absent", [])

            result = simulate_pipeline(input_doc)

            for field, expected_value in expected.items():
                found, actual = _resolve_field(result, field)
                self.assertTrue(found, f"[{comment}] missing field: {field}")
                self.assertEqual(
                    actual, expected_value,
                    f"[{comment}] field {field}: expected {expected_value!r}, got {actual!r}",
                )

            for field in expected_absent:
                self.assertTrue(
                    _field_absent(result, field),
                    f"[{comment}] sensitive field not redacted: {field}",
                )

    def test_observer_product_always_set(self) -> None:
        for event in self.events:
            result = simulate_pipeline(event["input"])
            self.assertEqual(result["observer.product"], "elasticsearch-agent-observability")

    def test_event_outcome_defaults_to_success(self) -> None:
        result = simulate_pipeline({"message": "bare event"})
        self.assertEqual(result["event.outcome"], "success")

    def test_event_outcome_defaults_to_failure_on_error(self) -> None:
        result = simulate_pipeline({"message": "error event", "error.type": "RuntimeError"})
        self.assertEqual(result["event.outcome"], "failure")


class UnknownFieldRoutingTests(unittest.TestCase):
    """Verify that top-level keys not in KNOWN_ROOTS get routed to
    labels.unmapped instead of polluting the root mapping."""

    def test_unknown_toplevel_key_routed_to_labels_unmapped(self) -> None:
        doc = {
            "message": '{"custom_app_field": "surprise", "event": {"action": "test"}}',
            "service.name": "test-agent",
        }
        result = simulate_pipeline(doc)
        unmapped = result.get("labels", {}).get("unmapped", {})
        self.assertIn("custom_app_field", unmapped)
        self.assertEqual(unmapped["custom_app_field"], "surprise")
        # Must NOT appear at root level
        self.assertNotIn("custom_app_field", result)

    def test_known_root_key_not_routed_to_unmapped(self) -> None:
        doc = {
            "message": '{"event": {"action": "known"}, "service": {"name": "merged"}}',
        }
        result = simulate_pipeline(doc)
        unmapped = result.get("labels", {}).get("unmapped", {})
        self.assertNotIn("event", unmapped)
        self.assertNotIn("service", unmapped)

    def test_dotted_key_in_parsed_message_set_only_if_absent(self) -> None:
        doc = {
            "gen_ai.tool.name": "existing",
            "message": '{"gen_ai.tool.name": "incoming"}',
        }
        result = simulate_pipeline(doc)
        # Existing dotted key must not be overwritten
        self.assertEqual(result["gen_ai.tool.name"], "existing")

    def test_multiple_unknown_keys_all_routed(self) -> None:
        doc = {
            "message": '{"foo": 1, "bar": "two", "event": {"action": "test"}}',
        }
        result = simulate_pipeline(doc)
        unmapped = result.get("labels", {}).get("unmapped", {})
        self.assertIn("foo", unmapped)
        self.assertIn("bar", unmapped)
        self.assertEqual(unmapped["foo"], 1)
        self.assertEqual(unmapped["bar"], "two")


class PipelineStructureTests(unittest.TestCase):
    """Verify the pipeline structure itself — processor ordering, required
    processors, and metadata — matches the integration contract."""

    def test_pipeline_has_json_before_merge_script(self) -> None:
        """The json processor must appear before the merge script so that
        _parsed_message exists when mergeMaps runs."""
        processors = self.pipeline["processors"]
        json_idx = next(i for i, p in enumerate(processors) if "json" in p)
        merge_idx = next(
            i for i, p in enumerate(processors)
            if "script" in p and "_parsed_message" in p["script"].get("source", "")
        )
        self.assertLess(json_idx, merge_idx)

    def test_redact_processors_after_merge(self) -> None:
        """Sensitive field removal must happen after the merge script so that
        fields injected via JSON body are also caught."""
        processors = self.pipeline["processors"]
        merge_idx = next(
            i for i, p in enumerate(processors)
            if "script" in p and "_parsed_message" in p["script"].get("source", "")
        )
        remove_indices = [
            i for i, p in enumerate(processors)
            if "remove" in p and p["remove"]["field"] in SENSITIVE_FIELDS
        ]
        self.assertTrue(remove_indices, "no redaction processors found")
        for idx in remove_indices:
            self.assertGreater(idx, merge_idx)

    def test_pipeline_known_roots_match_painless_script(self) -> None:
        """The KNOWN_ROOTS set in this test must match the Painless script
        so that our simulation is faithful."""
        processors = self.pipeline["processors"]
        merge_script = next(
            p["script"]["source"]
            for p in processors
            if "script" in p and "known_roots" in p["script"].get("source", "")
        )
        # Extract the HashSet literal from the Painless source
        import re
        match = re.search(r"new HashSet\(\[([^\]]+)\]\)", merge_script)
        self.assertIsNotNone(match, "could not find known_roots HashSet in Painless source")
        painless_roots = frozenset(
            s.strip().strip("'\"") for s in match.group(1).split(",")
        )
        self.assertEqual(KNOWN_ROOTS, painless_roots,
                         f"KNOWN_ROOTS drift: test={KNOWN_ROOTS - painless_roots}, painless={painless_roots - KNOWN_ROOTS}")

    @classmethod
    def setUpClass(cls) -> None:
        cls.pipeline = render_es_assets.build_ingest_pipeline(["tool_registry", "model_adapter"])


if __name__ == "__main__":
    unittest.main()
