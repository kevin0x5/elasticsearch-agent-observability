"""ES version compatibility probe tests."""

import sys
import unittest
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = REPO_ROOT / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import common  # noqa: E402
from common import ESConfig, check_es_version, parse_es_version  # noqa: E402


class ParseVersionTests(unittest.TestCase):
    def test_parses_standard_triple(self) -> None:
        self.assertEqual(parse_es_version("9.0.0"), (9, 0, 0))
        self.assertEqual(parse_es_version("8.13.2"), (8, 13, 2))

    def test_parses_build_suffix(self) -> None:
        self.assertEqual(parse_es_version("8.13.2-SNAPSHOT"), (8, 13, 2))
        self.assertEqual(parse_es_version("9.0.0+build.123"), (9, 0, 0))

    def test_garbage_returns_zeros(self) -> None:
        self.assertEqual(parse_es_version(""), (0, 0, 0))
        self.assertEqual(parse_es_version("not-a-version"), (0, 0, 0))


def _es(config):
    return ESConfig(es_url="http://x")


class CheckVersionTests(unittest.TestCase):
    def _probe(self, number: str):
        with mock.patch.object(common, "es_request", return_value={"version": {"number": number}}):
            return check_es_version(_es(None))

    def test_supported_major_9(self) -> None:
        result = self._probe("9.0.0")
        self.assertEqual(result["status"], "supported")
        self.assertEqual(result["major"], 9)

    def test_supported_major_8(self) -> None:
        result = self._probe("8.13.4")
        self.assertEqual(result["status"], "supported")

    def test_unsupported_7x_is_hard_fail(self) -> None:
        """The whole point: 7.x must not be silently accepted."""
        result = self._probe("7.17.0")
        self.assertEqual(result["status"], "unsupported")
        self.assertIn("7.17.0", result["detail"])
        self.assertIn("minimum supported major", result["detail"])

    def test_future_major_warns_but_does_not_fail(self) -> None:
        result = self._probe("10.0.0")
        self.assertEqual(result["status"], "warn")
        self.assertIn("newer than", result["detail"])

    def test_unparseable_version_warns(self) -> None:
        result = self._probe("something-weird")
        self.assertEqual(result["status"], "warn")


if __name__ == "__main__":
    unittest.main()
