import asyncio
import subprocess
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

import app as updater


class UpdateEndpointTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        self.projects = {}
        for name in ("a", "b", "c"):
            path = root / name
            path.mkdir()
            (path / "docker-compose.yml").write_text("services: {}\n")
            self.projects[name] = str(path)
        self.projects["empty"] = " "
        self.projects["missing"] = str(root / "missing")
        patcher = patch.object(updater, "folders", self.projects)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_validation_does_not_enqueue(self):
        with patch.object(updater, "perform_update") as perform:
            with TestClient(updater.app) as client:
                self.assertEqual(client.post("/update?api_key=bad").status_code, 401)
                self.assertEqual(client.post("/update?api_key=empty").status_code, 500)
                self.assertEqual(client.post("/update?api_key=missing").status_code, 500)
                compose = Path(self.projects["a"]) / "docker-compose.yml"
                compose.unlink()
                compose.mkdir()
                self.assertEqual(client.post("/update?api_key=a").status_code, 500)
                client.portal.call(updater.app.state.update_queue.join)
            perform.assert_not_called()

    def test_immediate_response_fifo_and_no_overlap(self):
        started = threading.Event()
        release = threading.Event()
        events = []

        def perform(job):
            name = job.project_path.name
            events.append(("start", name))
            if name == "a":
                started.set()
                if not release.wait(5):
                    raise RuntimeError("test release timed out")
            events.append(("finish", name))

        with patch.object(updater, "perform_update", side_effect=perform):
            with TestClient(updater.app) as client:
                try:
                    first = client.post("/update?api_key=a")
                    self.assertEqual(first.status_code, 202)
                    self.assertTrue(started.wait(2))
                    self.assertEqual(first.json()["status"], "queued")
                    self.assertEqual(first.json()["project_dir"], self.projects["a"])
                    self.assertNotIn("api_key", first.json())

                    second = client.post("/update?api_key=b")
                    third = client.post("/update?api_key=c")
                    self.assertEqual(second.status_code, 202)
                    self.assertEqual(third.status_code, 202)
                    ids = {r.json()["job_id"] for r in (first, second, third)}
                    self.assertEqual(len(ids), 3)
                    self.assertEqual(events, [("start", "a")])
                finally:
                    release.set()

                client.portal.call(updater.app.state.update_queue.join)

        self.assertEqual(events, [
            ("start", "a"), ("finish", "a"),
            ("start", "b"), ("finish", "b"),
            ("start", "c"), ("finish", "c"),
        ])

    def test_queue_full_and_duplicate_requests(self):
        started = threading.Event()
        release = threading.Event()
        processed = []

        def perform(job):
            processed.append(job)
            started.set()
            if not release.wait(5):
                raise RuntimeError("test release timed out")

        with patch.object(updater, "QUEUE_CAPACITY", 1):
            with patch.object(updater, "perform_update", side_effect=perform):
                with TestClient(updater.app) as client:
                    try:
                        first = client.post("/update?api_key=a")
                        self.assertEqual(first.status_code, 202)
                        self.assertTrue(started.wait(2))
                        second = client.post("/update?api_key=a")
                        self.assertEqual(second.status_code, 202)
                        self.assertNotEqual(first.json()["job_id"], second.json()["job_id"])
                        self.assertEqual(client.post("/update?api_key=b").status_code, 503)
                    finally:
                        release.set()
                    client.portal.call(updater.app.state.update_queue.join)
        self.assertEqual(len(processed), 2)
        self.assertEqual(processed[0].project_path, processed[1].project_path)

    def test_failed_job_does_not_stop_worker(self):
        with patch.object(updater, "perform_update", side_effect=[RuntimeError("boom"), None]) as perform:
            with self.assertLogs(updater.logger, level="ERROR") as logs:
                with TestClient(updater.app) as client:
                    self.assertEqual(client.post("/update?api_key=a").status_code, 202)
                    self.assertEqual(client.post("/update?api_key=b").status_code, 202)
                    client.portal.call(updater.app.state.update_queue.join)
            self.assertIn("boom", "\n".join(logs.output))
        self.assertEqual(perform.call_count, 2)


class ComposeExecutionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        path = Path(self.temp.name)
        (path / "docker-compose.yml").write_text("services: {}\n")
        self.job = updater.UpdateJob("test-job", path)

    def test_pull_then_up_with_existing_arguments(self):
        ok = subprocess.CompletedProcess([], 0, stdout="", stderr="")
        with patch.object(updater.subprocess, "run", return_value=ok) as run:
            updater.perform_update(self.job)
        self.assertEqual(
            [call.args[0] for call in run.call_args_list],
            [["docker", "compose", "pull"], ["docker", "compose", "up", "-d"]],
        )
        for call in run.call_args_list:
            self.assertEqual(call.kwargs, {
                "capture_output": True, "text": True,
                "cwd": self.job.project_path, "timeout": 600,
            })

    def test_pull_failure_skips_up(self):
        failed = subprocess.CompletedProcess([], 1, stdout="", stderr="pull failed")
        with patch.object(updater.subprocess, "run", return_value=failed) as run:
            with self.assertRaisesRegex(RuntimeError, "pull failed"):
                updater.perform_update(self.job)
            run.assert_called_once()

    def test_up_failure_is_reported(self):
        ok = subprocess.CompletedProcess([], 0, stdout="", stderr="")
        failed = subprocess.CompletedProcess([], 1, stdout="", stderr="up failed")
        with patch.object(updater.subprocess, "run", side_effect=[ok, failed]) as run:
            with self.assertRaisesRegex(RuntimeError, "up failed"):
                updater.perform_update(self.job)
            self.assertEqual(run.call_count, 2)

    def test_timeout_skips_up(self):
        timeout = subprocess.TimeoutExpired(["docker", "compose", "pull"], 600)
        with patch.object(updater.subprocess, "run", side_effect=timeout) as run:
            with self.assertRaisesRegex(RuntimeError, "timed out"):
                updater.perform_update(self.job)
            run.assert_called_once()

    def test_rechecks_compose_file(self):
        (self.job.project_path / "docker-compose.yml").unlink()
        with patch.object(updater.subprocess, "run") as run:
            with self.assertRaises(FileNotFoundError):
                updater.perform_update(self.job)
            run.assert_not_called()


class ShutdownTests(unittest.IsolatedAsyncioTestCase):
    async def test_shutdown_discards_pending_and_waits_for_active(self):
        started = threading.Event()
        release = threading.Event()
        processed = []

        def perform(job):
            processed.append(job.job_id)
            started.set()
            if not release.wait(5):
                raise RuntimeError("test release timed out")

        with patch.object(updater, "perform_update", side_effect=perform):
            context = updater.lifespan(updater.app)
            await context.__aenter__()
            shutdown = None
            try:
                queue = updater.app.state.update_queue
                queue.put_nowait(updater.UpdateJob("active", Path("/active")))
                self.assertTrue(await asyncio.to_thread(started.wait, 2))
                queue.put_nowait(updater.UpdateJob("pending", Path("/pending")))
                shutdown = asyncio.create_task(context.__aexit__(None, None, None))
                await asyncio.sleep(0)
                self.assertFalse(updater.app.state.accepting_updates)
                self.assertFalse(shutdown.done())
            finally:
                release.set()
                if shutdown is None:
                    await context.__aexit__(None, None, None)
                else:
                    await asyncio.wait_for(shutdown, 5)
            await asyncio.wait_for(queue.join(), 2)
        self.assertEqual(processed, ["active"])


if __name__ == "__main__":
    unittest.main()
