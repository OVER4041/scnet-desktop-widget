import asyncio
from pathlib import Path
import tempfile
import unittest

from scnet_widget.config import BridgeError, Cluster, Settings
from scnet_widget.log_discovery import (SCRIPT_MARKER, discovery_paths,
                                     log_read_script, read_log_records, relative_path)
from scnet_widget.monitor import SlurmMonitor
from scnet_widget.transport import Result
from test_monitor import response


def metadata(script="", work="/home/user/calc", stdout="/home/user/calc/slurm-101.out"):
    return (f"/home/user\nJobId=101 UserId=user(1) WorkDir={work} "
            f"StdOut={stdout} StdErr=/dev/null\n{SCRIPT_MARKER}\n{script}")


class LogDiscoveryTests(unittest.TestCase):
    def test_scheduler_paths_and_redirects_with_job_variables(self):
        folder, paths = discovery_paths(metadata(
            'cd /home/user/calc\nLOG="logs/epw_${SLURM_JOB_ID}.out"\n'
            'mpirun epw.x > "$LOG" 2>&1'), "101")
        self.assertEqual(folder, "calc")
        self.assertIn("calc/logs/epw_101.out", paths)
        self.assertIn("calc/slurm-101.out", paths)
        self.assertNotIn("dev/null", paths)

    def test_array_placeholders_resolve_to_current_task(self):
        _, paths = discovery_paths(metadata(stdout="/home/user/calc/slurm-%A_%a.out"), "101_7")
        self.assertEqual(paths, ["calc/slurm-101_7.out"])

    def test_paths_outside_root_and_command_substitution_are_not_used(self):
        folder, paths = discovery_paths(metadata(
            'epw.x > "$(touch /tmp/should-not-run).out"\n'
            'epw.x > /etc/private.out\nepw.x > ../../outside.out',
            stdout="/elsewhere/slurm-101.out"), "101")
        self.assertEqual(folder, "calc")
        self.assertEqual(paths, [])
        self.assertIsNone(relative_path("/home/user2/private", "/home/user"))

    def test_workdir_and_paths_may_contain_spaces_and_shell_metacharacters(self):
        folder, paths = discovery_paths(metadata(
            'mpirun epw.x > "logs/phase 101.out"', work="/home/user/calc space"), "101")
        self.assertEqual(folder, "calc space")
        self.assertIn("calc space/logs/phase 101.out", paths)
        body = log_read_script("101", folder, ["calc space/$(bad).out"])
        self.assertIn("'calc space/$(bad).out'", body)

    def test_unsupported_cd_does_not_guess_relative_redirects(self):
        _, paths = discovery_paths(metadata('cd "$(pwd)"\nepw.x > guessed.out'), "101")
        self.assertNotIn("calc/guessed.out", paths)

    def test_home_is_not_recursively_scanned(self):
        folder, paths = discovery_paths(metadata(work="/home/user"), "101")
        self.assertEqual(folder, ".")
        self.assertNotIn("find ", log_read_script("101", folder, paths))

    def test_log_protocol_keeps_each_file_separate(self):
        self.assertEqual(read_log_records("a.out\0"+"123\0one\ntwo\0b.out\0"+"124\0three\0"),
                         [("a.out", 123, "one\ntwo"), ("b.out", 124, "three")])

    def make_monitor(self, replies):
        class FakeBridge:
            def __init__(self): self.bodies = []
            def cluster(self, name): return None
            async def run(self, name, body):
                self.bodies.append(body)
                result = replies.pop(0)
                if isinstance(result, Exception): raise result
                return Result(result, "", 0)
        settings = Settings(Path(tempfile.gettempdir()), Path(tempfile.gettempdir()),
                            {"scnet": Cluster("scnet", "test")})
        return SlurmMonitor(settings, "scnet", bridge=FakeBridge())

    def test_newest_scientific_log_wins_over_newer_driver_and_old_phase(self):
        records = ("calc/logs/old_101.out\0"+"10\0Iteration number: 100\0"
                   "calc/logs/new_101.out\0"+"20\0Progression iq (fine) = 5/100\0"
                   "calc/logs/driver_101.out\0"+"30\0driver started\0")
        monitor = self.make_monitor([metadata(), records])
        result = asyncio.run(monitor.fetch_progress("101", None))
        self.assertEqual(result["q_current"], 5)
        self.assertNotIn("iteration", result)
        self.assertEqual(result["log_source"], "automatic")

    def test_configured_path_overrides_discovery(self):
        monitor = self.make_monitor(["custom.out\0"+"10\0Iteration number: 4\0"])
        result = asyncio.run(monitor.fetch_progress("101", "custom.out"))
        self.assertEqual(result["iteration"], 4)
        self.assertEqual(len(monitor.bridge.bodies), 1)
        self.assertNotIn("find ", monitor.bridge.bodies[0])

    def test_discovery_metadata_is_cached_but_directory_is_checked_again(self):
        monitor = self.make_monitor([metadata(), "", "calc/logs/epw_101.out\0"+"11\0Iteration number: 2\0"])
        first = asyncio.run(monitor.fetch_progress("101", None))
        second = asyncio.run(monitor.fetch_progress("101", None))
        self.assertNotIn("log_path", first)
        self.assertEqual(second["iteration"], 2)
        self.assertEqual(sum("scontrol show" in body for body in monitor.bridge.bodies), 1)

    def test_log_failure_keeps_live_queue_online_and_retries(self):
        queue = "101|work|RUNNING|00:10|32|1|32G|node|2026-10-18T12:00:00|"
        monitor = self.make_monitor([response(queue=queue), BridgeError("temporary log failure")])
        snapshot = asyncio.run(monitor.fetch(log_job_id="101"))
        self.assertEqual(snapshot.counts()["running"], 1)
        self.assertEqual(len(snapshot.warnings), 1)
        self.assertIs(monitor.previous, snapshot)

    def test_follow_active_moves_from_completed_job_but_manual_focus_stays(self):
        queue = "102|new|RUNNING|00:10|32|1|32G|node|2026-10-18T12:00:00|"
        old = "101|old|COMPLETED|10|32|1|32G|2026-10-18T10:00:00|2026-10-18T10:00:10|0:0"
        for follow, expected in ((True, "102"), (False, "101")):
            with self.subTest(follow=follow):
                monitor = self.make_monitor([response(queue=queue, accounting=old)])
                async def capture(identifier, path): return {"job_id": identifier}
                monitor.fetch_progress = capture
                result = asyncio.run(monitor.fetch(log_job_id="101", follow_active=follow))
                self.assertEqual(result.progress["job_id"], expected)

    def test_first_refresh_without_watched_jobs_tracks_running_job(self):
        monitor = self.make_monitor([response(queue="101|new|RUNNING|00:10|32|1|32G|node|2026-10-18T12:00:00|")])
        async def capture(identifier, path): return {"job_id": identifier}
        monitor.fetch_progress = capture
        self.assertEqual(asyncio.run(monitor.fetch()).progress["job_id"], "101")


if __name__ == "__main__":
    unittest.main()
