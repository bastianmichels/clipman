"""Where the popup opens and how big it is (clipman/placement.py)."""

import unittest

from clipman.placement import DEFAULT_WIDTH, MIN_HEIGHT, MIN_WIDTH, Placement


class _Settings:
    """The two ClipboardDB methods Placement uses, on a dict."""

    def __init__(self, **values):
        self.values = dict(values)

    def get_setting(self, key, default=None):
        return self.values.get(key, default)

    def set_setting(self, key, value):
        self.values[key] = value


class TestPosition(unittest.TestCase):
    def test_first_open_is_at_the_pointer(self):
        self.assertEqual(Placement(_Settings()).request(), ("pointer", 0, 0))

    def test_a_move_becomes_the_offset_and_the_fixed_spot(self):
        db = _Settings()
        p = Placement(db)
        # Opened with the pointer at (500, 300), placed there, dragged to
        # (450, 200).
        self.assertTrue(p.record((500, 300), (500, 300), (450, 200)))
        self.assertEqual(p.request(), ("pointer", -50, -100))
        p.set_mode("fixed")
        self.assertEqual(p.request(), ("fixed", 450, 200))

    def test_the_edge_correction_is_not_remembered(self):
        p = Placement(_Settings(popup_offset_x="10", popup_offset_y="20"))
        # Opened near the right edge: the extension pushed the window
        # left, the user did not move it.
        self.assertFalse(p.record((1900, 1000), (1480, 440), (1480, 440)))
        self.assertEqual(p.offset(), (10, 20))

    def test_fixed_mode_without_a_spot_opens_at_the_pointer(self):
        p = Placement(_Settings(popup_position_mode="fixed",
                                popup_offset_x="5", popup_offset_y="6"))
        self.assertEqual(p.request(), ("pointer", 5, 6))

    def test_reset(self):
        p = Placement(_Settings())
        p.record((0, 0), (0, 0), (100, 100))
        p.set_mode("fixed")
        p.reset()
        self.assertEqual(p.request(), ("pointer", 0, 0))

    def test_junk_is_ignored(self):
        p = Placement(_Settings(popup_position_mode="sideways",
                                popup_offset_x="abc", popup_offset_y="9e99",
                                popup_fixed_x="", popup_fixed_y="3"))
        self.assertEqual(p.mode(), "pointer")
        self.assertEqual(p.offset(), (0, 0))
        self.assertIsNone(p.fixed())
        self.assertFalse(p.record((0, 0), (0, 0), (10 ** 9, 0)))
        self.assertFalse(p.record((0, 0), (0, 0), ("x", 1)))
        p.set_mode("bogus")
        self.assertEqual(p.mode(), "pointer")


class TestSize(unittest.TestCase):
    def test_default_and_saved_size(self):
        p = Placement(_Settings())
        self.assertEqual(p.size(600), (DEFAULT_WIDTH, 600))
        self.assertTrue(p.save_size(500, 700))
        self.assertEqual(p.size(600), (500, 700))
        # Unchanged: nothing written.
        self.assertFalse(p.save_size(500, 700))

    def test_size_fits_the_monitor_and_has_a_minimum(self):
        p = Placement(_Settings(window_width="3000", window_height="50"))
        self.assertEqual(p.size(600, max_size=(1280, 800)), (1280, MIN_HEIGHT))
        p = Placement(_Settings(window_width="10", window_height="5000"))
        self.assertEqual(p.size(600, max_size=(1280, 800)), (MIN_WIDTH, 800))

    def test_unrealized_sizes_are_not_saved(self):
        p = Placement(_Settings())
        self.assertFalse(p.save_size(0, 0))
        self.assertFalse(p.save_size(None, 400))
        self.assertEqual(p.size(600), (DEFAULT_WIDTH, 600))


if __name__ == "__main__":
    unittest.main()
