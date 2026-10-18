import unittest

from scnet_widget.eta import estimate_progress, eta_labels
from scnet_widget.monitor import Snapshot


def progress(current=100, total=1000, **kwargs):
    return {"job_id": "101", "log_path": "calc/logs/epw_101.out",
            "q_current": current, "q_total": total, **kwargs}


class ETATests(unittest.TestCase):
    def observe(self, current, now, previous=None, **kwargs):
        return estimate_progress(progress(current), now=now, previous=previous, **kwargs)

    def test_first_observation_waits_for_speed(self):
        first = self.observe(100, 1000)
        self.assertEqual(first["eta_state"], "collecting")
        self.assertNotIn("eta_seconds", first)

    def test_speed_and_remaining_use_observed_work_not_total_job_runtime(self):
        first = self.observe(100, 1000, elapsed_s=3600)
        second = self.observe(200, 1060, first, elapsed_s=3660)
        self.assertAlmostEqual(second["eta_rate"], 100/60)
        self.assertAlmostEqual(second["eta_seconds"], 480)
        self.assertEqual(eta_labels(second, recent=True, running=True)[0], "本阶段剩余约 8分")

    def test_short_manual_refresh_cannot_create_false_precise_speed(self):
        second = self.observe(200, 1002, self.observe(100, 1000))
        self.assertEqual(second["eta_state"], "collecting")

    def test_flat_progress_is_included_in_speed_then_stalls(self):
        first = self.observe(100, 1000)
        second = self.observe(200, 1060, first)
        third = self.observe(200, 1120, second)
        self.assertAlmostEqual(third["eta_seconds"], 960)
        for now in (1180, 1240): third = self.observe(200, now, third)
        self.assertEqual(third["eta_state"], "stalled")
        self.assertNotIn("eta_seconds", third)
        resumed = self.observe(300, 1300, third)
        self.assertEqual(resumed["eta_state"], "estimated")

    def test_buffered_output_without_any_advance_does_not_divide_by_zero(self):
        result = self.observe(100, 1060, self.observe(100, 1000))
        self.assertEqual(result["eta_state"], "collecting")

    def test_stage_or_log_or_job_change_discards_old_speed(self):
        previous = self.observe(200, 1060, self.observe(100, 1000))
        for changes in ({"job_id": "102"}, {"log_path": "calc/phase2.out"}, {"q_total": 2000}):
            with self.subTest(changes=changes):
                raw = progress(300)
                raw.update(changes)
                result = estimate_progress(raw, now=1120, previous=previous)
                self.assertEqual(result["eta_state"], "collecting")
                self.assertNotIn("eta_seconds", result)

    def test_counter_rollback_or_requeued_runtime_resets_speed(self):
        previous = self.observe(200, 1060, self.observe(100, 1000))
        self.assertEqual(self.observe(50, 1120, previous)["eta_state"], "collecting")
        result = self.observe(300, 1120, previous, elapsed_s=10, previous_elapsed_s=100)
        self.assertEqual(result["eta_state"], "collecting")

    def test_iteration_error_and_terminal_jobs_never_reuse_q_eta(self):
        for extra, state in (({"iteration": 2}, "iteration"), ({"native_error": True}, "error")):
            result = estimate_progress(progress(900, **extra), now=1120)
            self.assertEqual(result["eta_state"], state)
            self.assertNotIn("eta_seconds", result)
        self.assertEqual(estimate_progress(progress(900), now=1120, active=False)["eta_state"], "inactive")

    def test_final_q_point_does_not_claim_entire_job_is_complete(self):
        result = self.observe(1000, 1120)
        self.assertEqual(result["eta_state"], "phase_complete")
        self.assertNotIn("eta_seconds", result)
        self.assertIn("后续阶段", eta_labels(result, recent=True, running=True)[1])

    def test_clock_rollback_and_long_disconnect_reset_samples(self):
        previous = self.observe(200, 1060, self.observe(100, 1000))
        for now in (1000, 1060, 2000):
            result = self.observe(300, now, previous)
            self.assertEqual(result["eta_state"], "collecting")

    def test_recent_cached_snapshot_can_seed_first_measurement_after_restart(self):
        previous = Snapshot([], "2026-10-18T12:00:00+0800", progress=self.observe(100, 1000))
        restored = Snapshot.from_dict(previous.to_dict())
        result = self.observe(200, 1060, restored.progress)
        self.assertEqual(result["eta_state"], "estimated")
        legacy = estimate_progress(progress(200), now=1060, previous=progress(100), previous_at=1000)
        self.assertEqual(legacy["eta_state"], "estimated")

    def test_history_is_bounded_and_old_speed_expires(self):
        result = None
        for index in range(1, 70):
            result = self.observe(index*10, 1000+index*30, result)
        self.assertLessEqual(len(result["eta_samples"]), 32)
        self.assertGreaterEqual(result["eta_samples"][0][0], 1000+69*30-600)

    def test_offline_display_hides_previous_eta_and_speed(self):
        result = self.observe(200, 1060, self.observe(100, 1000))
        title, detail = eta_labels(result, recent=False, running=True)
        self.assertIn("等待同步", title)
        self.assertNotIn("点/分", detail)
        self.assertEqual(eta_labels(result, recent=True, running=False), ("预计剩余：—", ""))

    def test_invalid_or_missing_progress_has_no_eta(self):
        for raw in ({}, progress(-1), progress(1001), progress(total=0)):
            self.assertEqual(estimate_progress(raw, now=1000)["eta_state"], "unavailable")


if __name__ == "__main__":
    unittest.main()
