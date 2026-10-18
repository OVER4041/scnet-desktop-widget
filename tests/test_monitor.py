import asyncio
from dataclasses import replace
import json
from pathlib import Path
import tempfile
import unittest

from scnet_widget.config import BridgeError, Cluster, Settings
from scnet_widget.monitor import (Job, MARKERS, SlurmMonitor, Snapshot, duration_seconds,
                               parse_progress, parse_snapshot, pending_seconds, project_totals)
from scnet_widget.transport import Result


def response(queue="", accounting="", watched="", log=""):
    return "\n".join((MARKERS[0], "2026-10-18T12:30:00+0800", queue,
        MARKERS[1], accounting, MARKERS[2], watched, MARKERS[3], log, MARKERS[4]))


class MonitorTests(unittest.TestCase):
    def test_slurm_durations_keep_unknown_distinct_from_zero(self):
        self.assertEqual(duration_seconds("03:45"), 225)
        self.assertEqual(duration_seconds("12:03:45"), 43425)
        self.assertEqual(duration_seconds("2-12:03:45"), 216225)
        self.assertEqual(duration_seconds("00:00"), 0)
        self.assertIsNone(duration_seconds("INVALID"))

    def test_live_queue_overrides_history_and_steps_are_not_double_counted(self):
        snapshot = parse_snapshot(response(
            queue="101|metal|RUNNING|02:03|128|1|700G|node1|2026-10-18T12:20:00|\n102|wait|PENDING|0:00|32|1|32G|(Priority)|2026-10-18T12:00:00|",
            accounting="101|metal|RUNNING|120|128|1|700Gn|2026-10-18T12:20:00|Unknown|0:0\n"
                "100|finished|COMPLETED|600|32|1|32Gn|2026-10-18T10:00:00|2026-10-18T10:10:00|0:0\n"
                "100.batch|batch|COMPLETED|600|32|1|32Gn|2026-10-18T10:00:00|2026-10-18T10:10:00|0:0"))
        self.assertEqual(len(snapshot.jobs), 3)
        self.assertEqual(snapshot.counts(), {"active": 2, "running": 1, "pending": 1, "completed": 1, "failed": 0})
        self.assertEqual(next(j for j in snapshot.jobs if j.job_id == "101").elapsed_s, 123)
        wait = next(j for j in snapshot.jobs if j.job_id == "102")
        self.assertEqual(pending_seconds(wait, snapshot), 1800)

    def test_disappeared_job_is_unknown_until_accounting_confirms_it(self):
        previous = Snapshot([Job("101", "work", "RUNNING", 60, in_queue=True)], "2026-10-18T12:30:00+0800")
        unknown = parse_snapshot(response(accounting="__ACCOUNTING_UNAVAILABLE__"), previous)
        self.assertEqual(unknown.jobs[0].state, "UNKNOWN")
        self.assertIsNone(unknown.counts()["completed"])
        terminal = parse_snapshot(response(watched="101|work|COMPLETED|70|1|1|1Gn|2026-10-18T12:20:00|2026-10-18T12:21:10|0:0"), unknown)
        self.assertEqual(terminal.jobs[0].state, "COMPLETED")
        self.assertFalse(terminal.jobs[0].in_queue)

    def test_today_counts_use_end_date_and_do_not_treat_cancellation_as_failure(self):
        snapshot = parse_snapshot(response(accounting=
            "100|yesterday|COMPLETED|100|32|1|1Gn|2026-10-17T10:00:00|2026-10-17T11:00:00|0:0\n"
            "101|cancel|CANCELLED by 1|10|32|1|1Gn|2026-10-18T10:00:00|2026-10-18T11:00:00|0:15\n"
            "102|failure|OUT_OF_MEMORY|200|32|1|1Gn|2026-10-18T10:00:00|2026-10-18T11:00:00|1:0"))
        self.assertEqual(snapshot.counts()["completed"], 0)
        self.assertEqual(snapshot.counts()["failed"], 1)

    def test_incomplete_or_truncated_data_does_not_become_empty_queue(self):
        with self.assertRaises(BridgeError):
            parse_snapshot(response().replace(MARKERS[4], ""))

    def test_q_progress_and_iteration_are_distinct(self):
        progress = parse_progress("Progression iq (fine) = 100/1000\nProgression iq (fine) = 200/1000")
        self.assertEqual(progress, {"q_current": 200, "q_total": 1000})
        iterative = parse_progress("Iteration number: 19\n  5.74073E-07 Max error\nTotal program execution")
        self.assertEqual(iterative["iteration"], 19)
        self.assertAlmostEqual(iterative["max_error"], 5.74073e-7)
        self.assertNotIn("q_current", iterative)

    def test_ledger_totals_include_failed_time_and_use_live_job_once(self):
        snapshot = Snapshot([Job("102", "current", "RUNNING", 200, in_queue=True)], "2026-10-18T12:30:00+0800")
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder)/"ledger.json"
            path.write_text(json.dumps({"jobs": [
                {"id": "100", "actual_elapsed_s": 100, "mpi_tasks": 32, "status": "FAILED"},
                {"id": "101", "actual_elapsed_s": 300, "mpi_tasks": 64, "status": "COMPLETED"},
                {"id": "102", "actual_elapsed_s": None, "mpi_tasks": 128},
                {"id": "102", "actual_elapsed_s": None, "mpi_tasks": 128}]}), encoding="utf8")
            seconds, hours = project_totals(path, snapshot, advance=10)
            self.assertEqual(seconds, 610)
            self.assertAlmostEqual(hours, (100*32+300*64+210*128)/3600)

    def test_fetch_preserves_snapshot_on_network_or_output_failure(self):
        class FakeBridge:
            def cluster(self, name): return None
            async def run(self, name, body): return Result(response(), "", 0, truncated=True)
        with tempfile.TemporaryDirectory() as directory:
            settings = Settings(Path(directory), Path(directory), {"scnet": Cluster("scnet", "test")})
            monitor = SlurmMonitor(settings, "scnet", bridge=FakeBridge())
            monitor.previous = Snapshot([Job("1", "old", "RUNNING", 60)], "2026-10-18T12:30:00+0800")
            with self.assertRaises(BridgeError):
                asyncio.run(monitor.fetch())
            self.assertEqual(monitor.previous.jobs[0].job_id, "1")


if __name__ == "__main__":
    unittest.main()
