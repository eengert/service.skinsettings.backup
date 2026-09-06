import sys
import unittest


ROOT = __import__("pathlib").Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from resources.lib.policy import due, fingerprint, reset_reason, statistics  # noqa: E402


class PolicyTests(unittest.TestCase):
    def settings(self, values):
        body = "".join('<setting id="s{}">{}</setting>'.format(index, value)
                       for index, value in enumerate(values))
        return ("<settings>" + body + "</settings>").encode()

    def files(self, values=("true", "false", "none", "configured")):
        return {
            "addon_data/skin.example/settings.xml": self.settings(values),
            "addon_data/script.skinvariables/nodes/skin.example/menu.json": b"{}",
            "addon_data/script.skinvariables/skin.example-viewtypes.json": b"{}",
        }

    def test_statistics_counts_settings_meaningful_helpers_and_bytes(self):
        files = self.files()
        result = statistics(files, "skin.example")

        self.assertEqual({"settings": 4, "meaningful": 2, "helpers": 2,
                          "bytes": sum(map(len, files.values()))}, result)

    def test_reset_drop_values_and_fingerprint_stable(self):
        previous = {"settings": 20, "meaningful": 12, "helpers": 8}
        current = {"settings": 9, "meaningful": 5, "helpers": 3}

        self.assertEqual("The number of skin settings fell from 20 to 9.",
                         reset_reason(previous, current))
        self.assertEqual(fingerprint(self.files()), fingerprint(dict(reversed(list(self.files().items())))))

    def test_reset_reason_uses_first_triggered_drop(self):
        previous = {"settings": 10, "meaningful": 12, "helpers": 8}
        current = {"settings": 10, "meaningful": 5, "helpers": 3}

        self.assertEqual("The number of configured values fell from 12 to 5.",
                         reset_reason(previous, current))

    def test_malformed_marker_and_missing_previous_are_safe(self):
        self.assertEqual("", reset_reason({}, {"settings": 0}))
        self.assertEqual("", reset_reason(None, {"settings": 0}))
        self.assertEqual("", reset_reason({"settings": 10}, {"settings": 5}))

    def test_scheduling_catches_up_and_handles_clock_backwards(self):
        self.assertTrue(due(None, 100, 24))
        self.assertFalse(due(100, 101, 24))
        self.assertTrue(due(100, 99, 24))
        self.assertTrue(due(100, 100 + 24 * 3600, 24))
        self.assertFalse(due(100, 100 + 23 * 3600, 24))


if __name__ == "__main__":
    unittest.main()
