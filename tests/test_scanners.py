"""Scanner health tracking: pure state, no network. Run: python3 -m unittest discover -s tests"""

import unittest

from ha_ble_vhci.ha import NO_SCANNER_MESSAGE, ScannerTracker, scanner_label

KITCHEN = {
    "source": "30:C9:22:D1:A5:72",
    "connectable": True,
    "name": "btproxy-kitchen (30:C9:22:D1:A5:72)",
    "adapter": "btproxy-kitchen",
    "scanner_type": "remote",
}
HALL = {"source": "AA:BB:CC:DD:EE:FF", "name": "btproxy-hall (AA:BB:CC:DD:EE:FF)", "adapter": "btproxy-hall"}


class ScannerTrackerTest(unittest.TestCase):
    def test_add_tracks_scanner_and_logs_online(self):
        tracker = ScannerTracker()
        with self.assertLogs("ha_ble_vhci.ha", "INFO") as logs:
            tracker.apply({"add": [KITCHEN]})
        self.assertEqual(list(tracker.online), [KITCHEN["source"]])
        self.assertIn(f"Bluetooth scanner online: {KITCHEN['name']}", logs.output[0])

    def test_repeated_add_logs_once(self):
        tracker = ScannerTracker()
        tracker.apply({"add": [KITCHEN]})
        with self.assertNoLogs("ha_ble_vhci.ha", "INFO"):
            tracker.apply({"add": [KITCHEN]})

    def test_removing_last_scanner_warns(self):
        tracker = ScannerTracker()
        tracker.apply({"add": [KITCHEN]})
        with self.assertLogs("ha_ble_vhci.ha", "WARNING") as logs:
            tracker.apply({"remove": [KITCHEN]})
        self.assertEqual(tracker.online, {})
        self.assertTrue(any(NO_SCANNER_MESSAGE in line for line in logs.output))

    def test_removing_one_of_two_does_not_warn_no_scanner(self):
        tracker = ScannerTracker()
        tracker.apply({"add": [KITCHEN, HALL]})
        with self.assertLogs("ha_ble_vhci.ha", "WARNING") as logs:
            tracker.apply({"remove": [HALL]})
        self.assertEqual(list(tracker.online), [KITCHEN["source"]])
        self.assertFalse(any(NO_SCANNER_MESSAGE in line for line in logs.output))

    def test_remove_of_unknown_scanner_is_ignored(self):
        tracker = ScannerTracker()
        with self.assertNoLogs("ha_ble_vhci.ha", "INFO"):
            tracker.apply({"remove": [KITCHEN]})

    def test_malformed_events_are_ignored(self):
        tracker = ScannerTracker()
        tracker.apply({})
        tracker.apply({"add": None, "remove": None})
        tracker.apply({"add": [{"name": "no source"}]})
        self.assertEqual(tracker.online, {})

    def test_reset_forgets_scanners_silently(self):
        tracker = ScannerTracker()
        tracker.apply({"add": [KITCHEN]})
        with self.assertNoLogs("ha_ble_vhci.ha", "INFO"):
            tracker.reset()
        self.assertEqual(tracker.online, {})

    def test_warn_if_empty(self):
        tracker = ScannerTracker()
        with self.assertLogs("ha_ble_vhci.ha", "WARNING") as logs:
            tracker.warn_if_empty()
        self.assertIn(NO_SCANNER_MESSAGE, logs.output[0])
        tracker.apply({"add": [KITCHEN]})
        with self.assertNoLogs("ha_ble_vhci.ha", "WARNING"):
            tracker.warn_if_empty()

    def test_remove_by_bare_source_string(self):
        tracker = ScannerTracker()
        tracker.apply({"add": [KITCHEN]})
        with self.assertLogs("ha_ble_vhci.ha", "WARNING") as logs:
            tracker.apply({"remove": [KITCHEN["source"]]})
        self.assertEqual(tracker.online, {})
        self.assertTrue(any(NO_SCANNER_MESSAGE in line for line in logs.output))

    def test_non_dict_add_items_are_ignored(self):
        tracker = ScannerTracker()
        tracker.apply({"add": ["30:C9:22:D1:A5:72", None]})
        self.assertEqual(tracker.online, {})

    def test_label_falls_back(self):
        self.assertEqual(scanner_label({"source": "X"}), "X")
        self.assertEqual(scanner_label({"adapter": "a", "source": "X"}), "a")


if __name__ == "__main__":
    unittest.main()
