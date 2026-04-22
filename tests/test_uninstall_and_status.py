"""Uninstall and status script tests (ES mocked)."""

import sys
import unittest
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = REPO_ROOT / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import status  # noqa: E402
import uninstall  # noqa: E402
from common import ESConfig, OBSERVER_PRODUCT_TAG, SkillError  # noqa: E402


def _cfg() -> ESConfig:
    return ESConfig(es_url="http://localhost:9200")


def _ours_response(asset: str) -> dict:
    """Build a GET response shaped like the ES API that carries our _meta tag."""
    meta = {"product": OBSERVER_PRODUCT_TAG, "managed": True}
    if asset == "ilm_policy":
        return {"agent-obsv-lifecycle": {"policy": {"_meta": meta}}}
    if asset == "ingest_pipeline":
        return {"agent-obsv-normalize": {"_meta": meta, "processors": []}}
    if asset == "index_template":
        return {"index_templates": [{"name": "agent-obsv-events-template", "index_template": {"_meta": meta}}]}
    if asset.startswith("component_template"):
        return {"component_templates": [{"name": "agent-obsv-ecs-base", "component_template": {"_meta": meta}}]}
    if asset == "data_stream":
        return {"data_streams": [{"name": "agent-obsv-events"}]}
    return {}


def _owned_es(*, deletes_fail: set[str] | None = None, deletes_404: set[str] | None = None):
    """Fake es_request that pretends every asset exists and is owned by us."""
    deletes_fail = deletes_fail or set()
    deletes_404 = deletes_404 or set()

    def fake_es(config, method, path, payload=None):
        if method == "GET":
            # Detect asset by path
            if "/_ilm/policy/" in path:
                return _ours_response("ilm_policy")
            if "/_ingest/pipeline/" in path:
                return _ours_response("ingest_pipeline")
            if "/_index_template/" in path:
                return _ours_response("index_template")
            if "/_component_template/" in path:
                return _ours_response("component_template_ecs_base")
            if "/_data_stream/" in path:
                return _ours_response("data_stream")
            return {"ok": True}
        if method == "DELETE":
            for marker in deletes_fail:
                if marker in path:
                    raise SkillError("Elasticsearch HTTP 500: boom")
            for marker in deletes_404:
                if marker in path:
                    raise SkillError("Elasticsearch HTTP 404: not_found")
            return {"acknowledged": True}
        return {"acknowledged": True}

    return fake_es


class UninstallTests(unittest.TestCase):
    def test_dry_run_builds_plan_without_es_calls(self) -> None:
        with mock.patch.object(uninstall, "es_request", side_effect=AssertionError("no es calls in dry-run")):
            summary = uninstall.run_uninstall(
                _cfg(),
                index_prefix="agent-obsv",
                confirm=False,
                keep_data_stream=False,
                kibana_url="",
                kibana_space="default",
                kibana_assets_file="",
            )
        self.assertTrue(summary["dry_run"])
        assets = [step["asset"] for step in summary["plan"]]
        self.assertEqual(assets[0], "data_stream")
        self.assertEqual(assets[-1], "ilm_policy")
        self.assertIn("index_template", assets)
        self.assertIn("ingest_pipeline", assets)

    def test_keep_data_stream_drops_that_step(self) -> None:
        with mock.patch.object(uninstall, "es_request", side_effect=AssertionError("no es calls in dry-run")):
            summary = uninstall.run_uninstall(
                _cfg(),
                index_prefix="agent-obsv",
                confirm=False,
                keep_data_stream=True,
                kibana_url="",
                kibana_space="default",
                kibana_assets_file="",
            )
        assets = [step["asset"] for step in summary["plan"]]
        self.assertNotIn("data_stream", assets)

    def test_owned_resources_are_deleted(self) -> None:
        with mock.patch.object(uninstall, "es_request", side_effect=_owned_es()):
            summary = uninstall.run_uninstall(
                _cfg(),
                index_prefix="agent-obsv",
                confirm=True,
                keep_data_stream=False,
                kibana_url="",
                kibana_space="default",
                kibana_assets_file="",
            )
        self.assertTrue(all(item["status"] == "deleted" for item in summary["results"]))

    def test_foreign_resource_is_refused(self) -> None:
        """If someone else's ILM policy shares our name, we must NOT delete it."""
        def fake_es(config, method, path, payload=None):
            if method == "GET" and "/_ilm/policy/" in path:
                return {"agent-obsv-lifecycle": {"policy": {"_meta": {"product": "someone-else"}}}}
            if method == "GET":
                return _owned_es()(config, method, path, payload)
            if method == "DELETE":
                if "ilm/policy" in path:
                    raise AssertionError("must not delete foreign ilm policy")
                return {"acknowledged": True}
            return {"acknowledged": True}

        with mock.patch.object(uninstall, "es_request", side_effect=fake_es):
            summary = uninstall.run_uninstall(
                _cfg(),
                index_prefix="agent-obsv",
                confirm=True,
                keep_data_stream=False,
                kibana_url="",
                kibana_space="default",
                kibana_assets_file="",
            )
        ilm = next(i for i in summary["results"] if i["asset"] == "ilm_policy")
        self.assertEqual(ilm["status"], "refused_foreign")
        self.assertEqual(ilm["owner"], "someone-else")

    def test_untagged_resource_is_refused_without_force(self) -> None:
        """Legacy resource (no _meta tag). Without --force we must refuse."""
        def fake_es(config, method, path, payload=None):
            if method == "GET" and "/_ilm/policy/" in path:
                return {"agent-obsv-lifecycle": {"policy": {"phases": {}}}}  # no _meta
            if method == "GET":
                return _owned_es()(config, method, path, payload)
            if method == "DELETE":
                if "ilm/policy" in path:
                    raise AssertionError("must not delete untagged without --force")
                return {"acknowledged": True}
            return {"acknowledged": True}

        with mock.patch.object(uninstall, "es_request", side_effect=fake_es):
            summary = uninstall.run_uninstall(
                _cfg(),
                index_prefix="agent-obsv",
                confirm=True,
                keep_data_stream=False,
                kibana_url="",
                kibana_space="default",
                kibana_assets_file="",
                force=False,
            )
        ilm = next(i for i in summary["results"] if i["asset"] == "ilm_policy")
        self.assertEqual(ilm["status"], "refused_untagged")

    def test_force_bypasses_ownership_check(self) -> None:
        """--force should delete regardless of _meta (even foreign)."""
        deleted = []

        def fake_es(config, method, path, payload=None):
            if method == "GET":
                raise AssertionError("force mode must not call GET")
            if method == "DELETE":
                deleted.append(path)
                return {"acknowledged": True}
            return {"acknowledged": True}

        with mock.patch.object(uninstall, "es_request", side_effect=fake_es):
            summary = uninstall.run_uninstall(
                _cfg(),
                index_prefix="agent-obsv",
                confirm=True,
                keep_data_stream=False,
                kibana_url="",
                kibana_space="default",
                kibana_assets_file="",
                force=True,
            )
        self.assertTrue(all(item["status"] == "deleted" for item in summary["results"]))
        self.assertEqual(len(deleted), len(summary["results"]))

    def test_absent_resource_is_already_absent(self) -> None:
        def fake_es(config, method, path, payload=None):
            if method == "GET":
                raise SkillError("Elasticsearch HTTP 404: not_found")
            if method == "DELETE":
                raise AssertionError("absent resource should not be DELETEd")
            return {"acknowledged": True}

        with mock.patch.object(uninstall, "es_request", side_effect=fake_es):
            summary = uninstall.run_uninstall(
                _cfg(),
                index_prefix="agent-obsv",
                confirm=True,
                keep_data_stream=False,
                kibana_url="",
                kibana_space="default",
                kibana_assets_file="",
            )
        self.assertTrue(all(item["status"] == "already_absent" for item in summary["results"]))

    def test_confirm_surfaces_real_failure(self) -> None:
        with mock.patch.object(
            uninstall,
            "es_request",
            side_effect=_owned_es(deletes_fail={"index_template"}),
        ):
            summary = uninstall.run_uninstall(
                _cfg(),
                index_prefix="agent-obsv",
                confirm=True,
                keep_data_stream=False,
                kibana_url="",
                kibana_space="default",
                kibana_assets_file="",
            )
        item = next(i for i in summary["results"] if i["asset"] == "index_template")
        self.assertEqual(item["status"], "failed")
        self.assertIn("500", item["detail"])


class StatusTests(unittest.TestCase):
    def _make_es(
        self,
        *,
        present: set[str],
        ds_present: bool = True,
        ds_count: int = 42,
        foreign: set[str] | None = None,
        untagged: set[str] | None = None,
    ):
        """Build a fake es_request that answers by path.

        ``present`` = fully ours (carries our _meta.product tag).
        ``foreign`` = exists but tagged by someone else.
        ``untagged`` = exists but predates _meta tagging.
        """
        foreign = foreign or set()
        untagged = untagged or set()

        def _asset_for(path: str) -> str | None:
            if "/_ilm/policy/" in path:
                return "ilm_policy"
            if "/_ingest/pipeline/" in path:
                return "ingest_pipeline"
            if "/_index_template/" in path:
                return "index_template"
            if "/_component_template/" in path:
                return "component_template_ecs_base"
            return None

        def _untagged_response(asset: str) -> dict:
            # Same shape as _ours_response but with no _meta at all.
            if asset == "ilm_policy":
                return {"agent-obsv-lifecycle": {"policy": {}}}
            if asset == "ingest_pipeline":
                return {"agent-obsv-normalize": {"processors": []}}
            if asset == "index_template":
                return {"index_templates": [{"name": "agent-obsv-events-template", "index_template": {}}]}
            return {"component_templates": [{"name": "agent-obsv-ecs-base", "component_template": {}}]}

        def _foreign_response(asset: str) -> dict:
            meta = {"product": "some-other-skill"}
            if asset == "ilm_policy":
                return {"agent-obsv-lifecycle": {"policy": {"_meta": meta}}}
            if asset == "ingest_pipeline":
                return {"agent-obsv-normalize": {"_meta": meta, "processors": []}}
            if asset == "index_template":
                return {"index_templates": [{"name": "agent-obsv-events-template", "index_template": {"_meta": meta}}]}
            return {"component_templates": [{"name": "agent-obsv-ecs-base", "component_template": {"_meta": meta}}]}

        def fake_es(config, method, path, payload=None):
            if path == "/":
                return {"version": {"number": "9.0.0"}}
            if path.endswith("/_count"):
                return {"count": ds_count}
            if path.startswith("/_data_stream/"):
                if not ds_present:
                    raise SkillError("Elasticsearch HTTP 404: index_not_found_exception")
                return {
                    "data_streams": [
                        {
                            "name": path.rsplit("/", 1)[1],
                            "generation": 3,
                            "template": "agent-obsv-events-template",
                            "indices": [{"index_name": ".ds-agent-obsv-events-000001"}],
                        }
                    ]
                }
            asset = _asset_for(path)
            # Match by coarse label-in-path (legacy) for presence membership.
            def _matches(labels: set[str]) -> bool:
                return any(label in path for label in labels)
            if _matches(foreign):
                return _foreign_response(asset or "index_template")
            if _matches(untagged):
                return _untagged_response(asset or "index_template")
            if _matches(present):
                return _ours_response(asset or "index_template")
            raise SkillError("Elasticsearch HTTP 404: not_found")
        return fake_es

    def test_all_present_is_ready(self) -> None:
        fake = self._make_es(
            present={"ilm/policy", "ingest/pipeline", "component_template", "index_template"},
            ds_present=True,
        )
        with mock.patch.object(status, "es_request", side_effect=fake):
            result = status.run_status(_cfg(), index_prefix="agent-obsv")
        self.assertEqual(result["overall"], "ready")
        self.assertEqual(result["missing"], [])
        self.assertEqual(result["data_stream"]["status"], "present")
        self.assertEqual(result["data_stream"]["doc_count"], 42)

    def test_missing_template_is_degraded(self) -> None:
        fake = self._make_es(
            present={"ilm/policy", "ingest/pipeline", "component_template"},  # no index_template
            ds_present=True,
        )
        with mock.patch.object(status, "es_request", side_effect=fake):
            result = status.run_status(_cfg(), index_prefix="agent-obsv")
        self.assertEqual(result["overall"], "degraded")
        self.assertIn("index_template", result["missing"])

    def test_missing_data_stream_degrades(self) -> None:
        fake = self._make_es(
            present={"ilm/policy", "ingest/pipeline", "component_template", "index_template"},
            ds_present=False,
        )
        with mock.patch.object(status, "es_request", side_effect=fake):
            result = status.run_status(_cfg(), index_prefix="agent-obsv")
        self.assertEqual(result["overall"], "degraded")
        self.assertIn("data_stream", result["missing"])

    def test_render_text_contains_key_signals(self) -> None:
        fake = self._make_es(
            present={"ilm/policy", "ingest/pipeline", "component_template", "index_template"},
            ds_present=True,
            ds_count=7,
        )
        with mock.patch.object(status, "es_request", side_effect=fake):
            result = status.run_status(_cfg(), index_prefix="agent-obsv")
        text = status.render_text(result)
        self.assertIn("READY", text)
        self.assertIn("doc_count=7", text)

    def test_foreign_owner_is_surfaced(self) -> None:
        """Foreign-tagged resource must flip overall to `foreign`, not `ready`.

        Silently reporting ready would mislead operators into thinking the skill
        is installed correctly when another product is squatting on the name.
        """
        fake = self._make_es(
            present={"ilm/policy", "ingest/pipeline", "component_template"},
            foreign={"index_template"},
            ds_present=True,
        )
        with mock.patch.object(status, "es_request", side_effect=fake):
            result = status.run_status(_cfg(), index_prefix="agent-obsv")
        self.assertEqual(result["overall"], "foreign")
        self.assertIn("index_template", result["foreign"])
        # Owner must be surfaced so operators know who to ask.
        tmpl = next(c for c in result["checks"] if c["asset"] == "index_template")
        self.assertEqual(tmpl["owner"], "some-other-skill")
        text = status.render_text(result)
        self.assertIn("FOREIGN", text)
        self.assertIn("some-other-skill", text)

    def test_untagged_owner_degrades(self) -> None:
        """Legacy (no _meta.product) install should degrade, not report ready."""
        fake = self._make_es(
            present={"ilm/policy", "ingest/pipeline", "component_template"},
            untagged={"index_template"},
            ds_present=True,
        )
        with mock.patch.object(status, "es_request", side_effect=fake):
            result = status.run_status(_cfg(), index_prefix="agent-obsv")
        self.assertEqual(result["overall"], "degraded")
        self.assertIn("index_template", result["untagged"])


if __name__ == "__main__":
    unittest.main()
