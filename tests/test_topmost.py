import unittest

from scnet_widget.topmost import TopmostController, covers_monitor


class FullscreenTests(unittest.TestCase):
    def test_fullscreen_and_borderless_cover_the_monitor(self):
        self.assertTrue(covers_monitor((0, 0, 1920, 1080), (0, 0, 1920, 1080)))
        self.assertTrue(covers_monitor((-1, -1, 1921, 1081), (0, 0, 1920, 1080)))

    def test_maximized_client_area_and_taskbar_do_not_count_as_fullscreen(self):
        self.assertFalse(covers_monitor((0, 30, 1920, 1040), (0, 0, 1920, 1080)))
        self.assertFalse(covers_monitor((0, 0, 1920, 1040), (0, 0, 1920, 1080)))

    def test_secondary_monitor_with_negative_coordinates(self):
        self.assertTrue(covers_monitor((-2560, -400, 0, 1040), (-2560, -400, 0, 1040)))
        self.assertFalse(covers_monitor((0, 0, 1920, 1080), (-2560, -400, 0, 1040)))

    def test_scaled_border_rounding_is_tolerated_but_small_windows_are_not(self):
        self.assertTrue(covers_monitor((1, 1, 1919, 1079), (0, 0, 1920, 1080)))
        self.assertFalse(covers_monitor((10, 10, 1910, 1070), (0, 0, 1920, 1080)))
        self.assertFalse(covers_monitor((0, 0, 0, 0), (0, 0, 1920, 1080)))

    def test_enter_and_exit_fullscreen_restore_original_preference(self):
        state = [False]
        changes = []
        controller = TopmostController(changes.append, lambda: state[0])
        controller.update(True)
        state[0] = True
        controller.update(True)
        self.assertTrue(controller.suspended)
        state[0] = False
        controller.update(True)
        self.assertEqual(changes, [True, False, True])

    def test_disabled_preference_never_restores_topmost(self):
        changes = []
        controller = TopmostController(changes.append, lambda: False)
        controller.update(False)
        controller.detector = lambda: True
        controller.update(False)
        controller.detector = lambda: False
        controller.update(False)
        self.assertEqual(changes, [False])

    def test_preference_changed_during_fullscreen_is_respected_on_exit(self):
        changes = []
        controller = TopmostController(changes.append, lambda: True)
        controller.update(True)
        controller.update(False)
        controller.detector = lambda: False
        controller.update(False)
        self.assertEqual(changes, [False])

    def test_transient_activation_failure_keeps_topmost_suspended(self):
        changes = []
        controller = TopmostController(changes.append, lambda: True)
        controller.update(True)
        controller.detector = lambda: None
        controller.update(True)
        def failure(): raise OSError("Window closed during query")
        controller.detector = failure
        controller.update(True)
        self.assertEqual(changes, [False])
        self.assertTrue(controller.suspended)

    def test_repeated_checks_do_not_reorder_windows(self):
        changes = []
        controller = TopmostController(changes.append, lambda: False)
        for _ in range(5): controller.update(True)
        self.assertEqual(changes, [True])


if __name__ == "__main__":
    unittest.main()
