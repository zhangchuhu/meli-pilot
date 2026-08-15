import io
import json
import multiprocessing
import os
import select
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch


sys.path.insert(0, str(Path(__file__).parent))
import run_lock


def release_after_forced_read(
        lock_file: str, run_id: str,
        owner_read: object, allow_unlink: object, finished: object,
) -> None:
    original = run_lock._read_lock
    reads = 0

    def pause_after_first_read(path: Path) -> dict[str, object]:
        nonlocal reads
        lock = original(path)
        reads += 1
        if reads == 1:
            owner_read.set()
            if not allow_unlink.wait(5):
                raise RuntimeError("test did not allow release to continue")
        return lock

    with patch.object(run_lock, "_read_lock", side_effect=pause_after_first_read):
        run_lock.release(lock_file, run_id)
    finished.set()


def acquire_during_release(
        lock_root: str, started: object, finished: object,
) -> None:
    started.set()
    run_lock.acquire(
        lock_root, "app_x", "tbl_x", "run_new", pid=os.getpid(),
        hostname="host-a", started_at="2026-08-15T02:01:00Z",
        alive=lambda _pid: False,
    )
    finished.set()


class RunLockTest(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)

    def tearDown(self) -> None:
        self.directory.cleanup()

    def _start_cli_holder(
            self, home: Path, run_id: str,
    ) -> tuple[subprocess.Popen[str], dict[str, object]]:
        environment = os.environ.copy()
        environment["HOME"] = str(home)
        environment["PYTHONDONTWRITEBYTECODE"] = "1"
        process = subprocess.Popen(
            [
                sys.executable, str(Path(run_lock.__file__)), "hold",
                "--base-token", "app_x", "--table-id", "tbl_x",
                "--run-id", run_id,
            ],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            env=environment,
        )
        assert process.stdout is not None
        assert process.stderr is not None
        ready, _, _ = select.select([process.stdout], [], [], 3)
        if not ready:
            process.terminate()
            process.wait(timeout=3)
            self.fail(f"lock holder did not publish readiness: {process.stderr.read()}")
        line = process.stdout.readline()
        if not line:
            process.wait(timeout=3)
            self.fail(f"lock holder exited before readiness: {process.stderr.read()}")
        return process, json.loads(line)

    def test_cli_holder_blocks_same_table_until_released(self) -> None:
        home = self.root / "home"
        home.mkdir()
        environment = os.environ.copy()
        environment["HOME"] = str(home)
        environment["PYTHONDONTWRITEBYTECODE"] = "1"
        first: subprocess.Popen[str] | None = None
        successor: subprocess.Popen[str] | None = None
        try:
            first, owned = self._start_cli_holder(home, "run_1")
            lock_file = Path(str(owned["lock_file"]))
            self.assertEqual(
                lock_file.parent,
                (home / ".codex" / "state" / "outfit-swap" / "locks").resolve(),
            )
            self.assertIsNone(first.poll(), "holder must remain alive while the run owns the lock")

            contender = subprocess.run(
                [
                    sys.executable, str(Path(run_lock.__file__)), "hold",
                    "--base-token", "app_x", "--table-id", "tbl_x",
                    "--run-id", "run_2",
                ],
                capture_output=True, text=True, check=False, timeout=3,
                env=environment,
            )
            self.assertEqual(contender.returncode, 1)
            self.assertEqual(contender.stdout, "")
            self.assertIn("lock is held", contender.stderr)
            self.assertIsNone(first.poll())

            released = subprocess.run(
                [
                    sys.executable, str(Path(run_lock.__file__)), "release",
                    "--lock-file", str(lock_file), "--run-id", "run_1",
                ],
                capture_output=True, text=True, check=False, timeout=3,
                env=environment,
            )
            self.assertEqual(released.returncode, 0, released.stderr)
            self.assertEqual(json.loads(released.stdout), {"released": True})
            self.assertEqual(first.wait(timeout=3), 0)
            first.communicate(timeout=3)
            self.assertFalse(lock_file.exists())

            successor, successor_owned = self._start_cli_holder(home, "run_2")
            successor_lock = Path(str(successor_owned["lock_file"]))
            self.assertEqual(successor_lock, lock_file)
            released = subprocess.run(
                [
                    sys.executable, str(Path(run_lock.__file__)), "release",
                    "--lock-file", str(successor_lock), "--run-id", "run_2",
                ],
                capture_output=True, text=True, check=False, timeout=3,
                env=environment,
            )
            self.assertEqual(released.returncode, 0, released.stderr)
            self.assertEqual(successor.wait(timeout=3), 0)
            successor.communicate(timeout=3)
        finally:
            for process in (successor, first):
                if process is not None and process.poll() is None:
                    process.terminate()
                if process is not None:
                    process.communicate(timeout=3)

    def test_cli_release_uses_holder_channel_without_sending_sigterm(self) -> None:
        home = self.root / "home"
        home.mkdir()
        holder: subprocess.Popen[str] | None = None
        original_kill = run_lock.os.kill

        def refuse_sigterm(pid: int, signal_number: int) -> None:
            if signal_number == run_lock.signal.SIGTERM:
                raise AssertionError("release must not signal a potentially reused PID")
            original_kill(pid, signal_number)

        try:
            holder, owned = self._start_cli_holder(home, "run_channel")
            with patch.object(run_lock.os, "kill", side_effect=refuse_sigterm):
                self.assertTrue(run_lock.request_release(
                    str(owned["lock_file"]), "run_channel", timeout=2,
                ))
            self.assertEqual(holder.wait(timeout=3), 0)
        finally:
            if holder is not None and holder.poll() is None:
                holder.terminate()
            if holder is not None:
                holder.communicate(timeout=3)

    def test_protocol_lock_with_reused_live_pid_is_reclaimed_without_signaling(self) -> None:
        stale_control = self.root / ".stale-holder.control"
        run_lock.acquire(
            self.root, "app_x", "tbl_x", "run_old", pid=os.getpid(),
            hostname=run_lock.socket.gethostname(),
            started_at="2026-08-15T02:00:00Z", alive=lambda _pid: True,
            control_path=str(stale_control), control_token="stale-token",
            process_identity="old-process-start",
        )
        with (
            patch.object(run_lock.os, "kill") as kill,
            patch.object(run_lock, "process_start_identity", return_value="new-process-start"),
        ):
            replacement = run_lock.acquire(
                self.root, "app_x", "tbl_x", "run_new", pid=456,
                hostname=run_lock.socket.gethostname(),
                started_at="2026-08-15T02:01:00Z", alive=lambda _pid: True,
            )
        self.assertEqual(json.loads(replacement.read_text())["run_id"], "run_new")
        kill.assert_not_called()

    def test_control_timeout_for_same_live_process_fails_closed(self) -> None:
        control = self.root / ".holder.control"
        run_lock.acquire(
            self.root, "app_x", "tbl_x", "run_old", pid=123,
            hostname="host-a", started_at="2026-08-15T02:00:00Z",
            alive=lambda _pid: True, control_path=str(control),
            control_token="holder-token", process_identity="same-process-start",
        )
        with (
            patch.object(run_lock, "_control_request", return_value=False),
            patch.object(
                run_lock, "process_start_identity", return_value="same-process-start",
            ),
        ):
            with self.assertRaises(run_lock.LockHeldError):
                run_lock.acquire(
                    self.root, "app_x", "tbl_x", "run_new", pid=456,
                    hostname="host-a", started_at="2026-08-15T02:01:00Z",
                    alive=lambda _pid: True,
                )

    def test_acquire_writes_owned_lock(self) -> None:
        path = run_lock.acquire(
            self.root, "app_x", "tbl_x", "run_1", pid=123,
            hostname="host-a", started_at="2026-08-15T02:00:00Z",
            alive=lambda pid: False,
        )
        self.assertEqual(json.loads(path.read_text())["run_id"], "run_1")

    def test_invalid_owner_identity_is_rejected_before_lock_publication(self) -> None:
        cases = (
            {"run_id": ""}, {"pid": 0}, {"pid": True},
            {"hostname": ""}, {"started_at": ""},
        )
        defaults = {
            "run_id": "run_1", "pid": 123, "hostname": "host-a",
            "started_at": "2026-08-15T02:00:00Z",
        }
        for index, override in enumerate(cases):
            with self.subTest(override=override):
                root = self.root / str(index)
                with self.assertRaises(run_lock.LockHeldError):
                    run_lock.acquire(
                        root, "app_x", "tbl_x", alive=lambda _pid: False,
                        **(defaults | override),
                    )
                self.assertFalse(
                    run_lock.lock_path(root, "app_x", "tbl_x").exists(),
                )

    def test_initial_publication_crash_leaves_no_partial_lock(self) -> None:
        path = run_lock.lock_path(self.root, "app_x", "tbl_x")

        def fail_after_partial_write(
                _contents: object, handle: object, **_kwargs: object,
        ) -> None:
            handle.write("{")
            handle.flush()
            raise OSError("injected serialization crash")

        with patch.object(run_lock.json, "dump", side_effect=fail_after_partial_write):
            with self.assertRaisesRegex(OSError, "injected serialization crash"):
                run_lock.acquire(
                    self.root, "app_x", "tbl_x", "run_1", pid=123,
                    hostname="host-a", started_at="2026-08-15T02:00:00Z",
                    alive=lambda _pid: False,
                )
        self.assertFalse(path.exists(), "partial JSON must never reach the final lock path")

    def test_lock_filename_and_contents_do_not_expose_base_coordinates(self) -> None:
        path = run_lock.acquire(
            self.root, "app_x", "tbl_x", "run_1", pid=123,
            hostname="host-a", started_at="2026-08-15T02:00:00Z", alive=lambda pid: False,
        )
        lock = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(path.name, "05f8eb68fc15784d8454.lock")
        self.assertEqual(lock["base_token_sha256"],
                         "82aa68d2a7bb83532150788749901d2afe924ab5a3ba8e0019b5d6f3f8ffc1f8")
        self.assertEqual(lock["table_id_sha256"],
                         "e736a5e906c4c9f33923d8538eb34ca08457888fb81ee3c820ff2803121c67ff")
        self.assertNotIn("app_x", path.read_text(encoding="utf-8"))
        self.assertNotIn("tbl_x", path.read_text(encoding="utf-8"))

    def test_live_pid_blocks_second_run(self) -> None:
        run_lock.acquire(
            self.root, "app_x", "tbl_x", "run_1", pid=123,
            hostname="host-a", started_at="2026-08-15T02:00:00Z",
            alive=lambda pid: pid == 123,
        )
        with self.assertRaises(run_lock.LockHeldError):
            run_lock.acquire(
                self.root, "app_x", "tbl_x", "run_2", pid=456,
                hostname="host-a", started_at="2026-08-15T02:01:00Z",
                alive=lambda pid: pid == 123,
            )

    def test_different_tables_have_independent_locks(self) -> None:
        first = run_lock.acquire(
            self.root, "app_x", "tbl_x", "run_1", pid=123,
            hostname="host-a", started_at="2026-08-15T02:00:00Z", alive=lambda pid: True,
        )
        second = run_lock.acquire(
            self.root, "app_x", "tbl_y", "run_2", pid=456,
            hostname="host-a", started_at="2026-08-15T02:01:00Z", alive=lambda pid: True,
        )
        self.assertNotEqual(first, second)

    def test_dead_same_host_lock_is_archived_before_reacquisition(self) -> None:
        path = run_lock.acquire(
            self.root, "app_x", "tbl_x", "run_old", pid=123,
            hostname="host-a", started_at="2026-08-15T02:00:00Z", alive=lambda pid: False,
        )
        replacement = run_lock.acquire(
            self.root, "app_x", "tbl_x", "run_new", pid=456,
            hostname="host-a", started_at="2026-08-15T02:01:00Z", alive=lambda pid: False,
        )
        self.assertEqual(replacement, path)
        self.assertEqual(json.loads(path.read_text())["run_id"], "run_new")
        archived = path.with_name(f"{path.name}.stale-run_new")
        self.assertEqual(json.loads(archived.read_text())["run_id"], "run_old")

    def test_reclaim_never_removes_a_live_lock_replaced_after_stale_inspection(self) -> None:
        path = run_lock.acquire(
            self.root, "app_x", "tbl_x", "run_old", pid=123,
            hostname="host-a", started_at="2026-08-15T02:00:00Z", alive=lambda pid: False,
        )
        original_inspect = run_lock._inspect_lock
        calls = 0

        def replace_before_reclaim(current: Path):
            nonlocal calls
            calls += 1
            if calls == 2:
                path.unlink()
                path.write_text(json.dumps({
                    "run_id": "run_live", "pid": 999, "hostname": "host-a",
                    "started_at": "2026-08-15T02:01:00Z",
                }), encoding="utf-8")
            return original_inspect(current)

        with patch.object(run_lock, "_inspect_lock", side_effect=replace_before_reclaim):
            with self.assertRaises(run_lock.LockHeldError):
                run_lock.acquire(
                    self.root, "app_x", "tbl_x", "run_new", pid=456,
                    hostname="host-a", started_at="2026-08-15T02:02:00Z",
                    alive=lambda pid: pid == 999,
                )
        self.assertEqual(json.loads(path.read_text())["run_id"], "run_live")

    def test_reclaim_never_renames_live_lock_substituted_in_final_mutation_window(self) -> None:
        path = run_lock.acquire(
            self.root, "app_x", "tbl_x", "run_old", pid=123,
            hostname="host-a", started_at="2026-08-15T02:00:00Z", alive=lambda pid: False,
        )
        original_lseek = run_lock.os.lseek
        injected = False

        def replace_before_stale_inode_rewrite(descriptor: int, position: int, how: int) -> int:
            nonlocal injected
            if not injected:
                injected = True
                path.unlink()
                path.write_text(json.dumps({
                    "run_id": "run_live", "pid": 999, "hostname": "host-a",
                    "started_at": "2026-08-15T02:01:00Z",
                }), encoding="utf-8")
            return original_lseek(descriptor, position, how)

        with patch.object(run_lock.os, "lseek", side_effect=replace_before_stale_inode_rewrite):
            with self.assertRaises(run_lock.LockHeldError):
                run_lock.acquire(
                    self.root, "app_x", "tbl_x", "run_new", pid=456,
                    hostname="host-a", started_at="2026-08-15T02:02:00Z",
                    alive=lambda pid: pid == 999,
                )
        self.assertTrue(injected)
        self.assertEqual(json.loads(path.read_text())["run_id"], "run_live")

    def test_dead_same_host_reclaim_marker_is_recovered_after_crash(self) -> None:
        path = run_lock.acquire(
            self.root, "app_x", "tbl_x", "run_old", pid=111,
            hostname="host-a", started_at="2026-08-15T01:59:00Z", alive=lambda pid: False,
        )
        status = path.stat()
        marker = run_lock._reclaim_marker(path)
        marker.write_text(json.dumps({
            "run_id": "run_crashed", "pid": 123, "hostname": "host-a",
            "started_at": "2026-08-15T02:00:00Z",
            "target_device": status.st_dev, "target_inode": status.st_ino,
        }), encoding="utf-8")
        replacement = run_lock.acquire(
            self.root, "app_x", "tbl_x", "run_new", pid=456,
            hostname="host-a", started_at="2026-08-15T02:01:00Z", alive=lambda pid: False,
        )
        self.assertEqual(json.loads(replacement.read_text())["run_id"], "run_new")
        self.assertFalse(marker.exists())

    def test_unlocked_orphan_marker_is_recovered_after_owner_pid_reuse(self) -> None:
        path = run_lock.acquire(
            self.root, "app_x", "tbl_x", "run_old", pid=111,
            hostname="host-a", started_at="2026-08-15T01:59:00Z",
            alive=lambda _pid: False,
        )
        status = path.stat()
        marker = run_lock._reclaim_marker(path)
        marker.write_text(json.dumps({
            "run_id": "run_crashed", "pid": 123, "hostname": "host-a",
            "started_at": "2026-08-15T02:00:00Z",
            "target_device": status.st_dev, "target_inode": status.st_ino,
        }), encoding="utf-8")

        replacement = run_lock.acquire(
            self.root, "app_x", "tbl_x", "run_new", pid=456,
            hostname="host-a", started_at="2026-08-15T02:01:00Z",
            alive=lambda pid: pid == 123,
        )

        self.assertEqual(json.loads(replacement.read_text())["run_id"], "run_new")
        self.assertFalse(marker.exists())

    def test_delayed_orphan_recoverer_preserves_new_active_marker_and_lock(self) -> None:
        path = run_lock.acquire(
            self.root, "app_x", "tbl_x", "run_old", pid=111,
            hostname="host-a", started_at="2026-08-15T01:59:00Z", alive=lambda pid: False,
        )
        status = path.stat()
        identity = (status.st_dev, status.st_ino)
        old_payload = path.read_bytes()
        path.with_name(f"{path.name}.stale-run_crashed").write_bytes(old_payload)
        marker = run_lock._reclaim_marker(path)
        marker.write_text(json.dumps({
            "run_id": "run_crashed", "pid": 123, "hostname": "host-a",
            "started_at": "2026-08-15T02:00:00Z",
            "target_device": identity[0], "target_inode": identity[1],
        }), encoding="utf-8")
        live_lock = json.loads(old_payload)
        live_lock.update({
            "run_id": "run_live", "pid": 999,
            "started_at": "2026-08-15T02:01:00Z",
        })
        live_marker = {
            "run_id": "run_live", "pid": 999, "hostname": "host-a",
            "started_at": "2026-08-15T02:01:00Z",
        }
        original_flock = run_lock.fcntl.flock
        active_descriptor = None
        injected = False

        def replace_marker_before_delayed_flock(descriptor: int, operation: int) -> None:
            nonlocal active_descriptor, injected
            if not injected:
                injected = True
                marker.unlink()
                path.write_text(json.dumps(live_lock), encoding="utf-8")
                active_descriptor = run_lock._claim_reclaim_marker(
                    marker, run_lock._marker_payload(live_marker, identity),
                )
                self.assertIsNotNone(active_descriptor)
            original_flock(descriptor, operation)

        try:
            with patch.object(
                    run_lock.fcntl, "flock", side_effect=replace_marker_before_delayed_flock):
                run_lock._recover_orphan_marker(path, marker, "host-a", lambda pid: False)
            self.assertTrue(injected)
            self.assertEqual(json.loads(path.read_text())["run_id"], "run_live")
            self.assertEqual(json.loads(marker.read_text())["run_id"], "run_live")
        finally:
            if active_descriptor is not None:
                run_lock._unlink_owned_marker(marker, active_descriptor)
                run_lock.os.close(active_descriptor)

    def test_acquire_retries_when_holder_disappears_before_inspection(self) -> None:
        path = run_lock.acquire(
            self.root, "app_x", "tbl_x", "run_old", pid=123,
            hostname="host-a", started_at="2026-08-15T02:00:00Z", alive=lambda pid: False,
        )
        original_inspect = run_lock._inspect_lock
        calls = 0

        def disappear_once(current: Path):
            nonlocal calls
            calls += 1
            if calls == 1:
                path.unlink()
                raise FileNotFoundError(path)
            return original_inspect(current)

        with patch.object(run_lock, "_inspect_lock", side_effect=disappear_once):
            replacement = run_lock.acquire(
                self.root, "app_x", "tbl_x", "run_new", pid=456,
                hostname="host-a", started_at="2026-08-15T02:01:00Z", alive=lambda pid: False,
            )
        self.assertEqual(json.loads(replacement.read_text())["run_id"], "run_new")

    def test_different_host_lock_is_never_reclaimed(self) -> None:
        run_lock.acquire(
            self.root, "app_x", "tbl_x", "run_1", pid=123,
            hostname="host-a", started_at="2026-08-15T02:00:00Z", alive=lambda pid: False,
        )
        with self.assertRaises(run_lock.LockHeldError):
            run_lock.acquire(
                self.root, "app_x", "tbl_x", "run_2", pid=456,
                hostname="host-b", started_at="2026-08-15T02:01:00Z", alive=lambda pid: False,
            )

    def test_release_refuses_a_different_run(self) -> None:
        path = run_lock.acquire(
            self.root, "app_x", "tbl_x", "run_1", pid=123,
            hostname="host-a", started_at="2026-08-15T02:00:00Z", alive=lambda pid: False,
        )
        with self.assertRaises(run_lock.LockHeldError):
            run_lock.release(path, "run_2")
        self.assertTrue(path.exists())

    def test_release_removes_matching_run_lock(self) -> None:
        path = run_lock.acquire(
            self.root, "app_x", "tbl_x", "run_1", pid=123,
            hostname="host-a", started_at="2026-08-15T02:00:00Z", alive=lambda pid: False,
        )
        self.assertTrue(run_lock.release(path, "run_1"))
        self.assertFalse(path.exists())

    @unittest.skipUnless(hasattr(os, "fork"), "requires POSIX multi-process flock semantics")
    def test_release_guard_blocks_successor_during_read_unlink_window(self) -> None:
        path = run_lock.acquire(
            self.root, "app_x", "tbl_x", "run_old", pid=111,
            hostname="host-a", started_at="2026-08-15T02:00:00Z",
            alive=lambda _pid: False,
        )
        context = multiprocessing.get_context("fork")
        owner_read = context.Event()
        allow_unlink = context.Event()
        release_finished = context.Event()
        successor_started = context.Event()
        successor_finished = context.Event()
        releaser = context.Process(
            target=release_after_forced_read,
            args=(str(path), "run_old", owner_read, allow_unlink, release_finished),
        )
        successor = context.Process(
            target=acquire_during_release,
            args=(str(self.root), successor_started, successor_finished),
        )
        releaser.start()
        try:
            self.assertTrue(owner_read.wait(3), "releaser never reached ownership read")
            successor.start()
            self.assertTrue(successor_started.wait(3), "successor process never started")
            self.assertFalse(
                successor_finished.wait(0.25),
                "successor must block while release owns the mutation guard",
            )
            allow_unlink.set()
            releaser.join(3)
            successor.join(3)
            self.assertEqual(releaser.exitcode, 0)
            self.assertEqual(successor.exitcode, 0)
            self.assertTrue(release_finished.is_set())
            self.assertTrue(successor_finished.is_set())
            self.assertEqual(json.loads(path.read_text())["run_id"], "run_new")
        finally:
            allow_unlink.set()
            for process in (successor, releaser):
                if process.pid is not None:
                    process.join(3)
                    if process.is_alive():
                        process.terminate()
                        process.join(3)

    def test_invalid_lock_json_is_refused_without_removal(self) -> None:
        path = run_lock.lock_path(self.root, "app_x", "tbl_x")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{", encoding="utf-8")
        with self.assertRaises(run_lock.LockHeldError):
            run_lock.acquire(
                self.root, "app_x", "tbl_x", "run_2", pid=456,
                hostname="host-a", started_at="2026-08-15T02:01:00Z", alive=lambda pid: False,
            )
        self.assertEqual(path.read_text(encoding="utf-8"), "{")

    def test_cli_release_refuses_a_non_holder_process(self) -> None:
        path = run_lock.acquire(
            self.root, "app_x", "tbl_x", "run_1", pid=123,
            hostname="other-host", started_at="2026-08-15T02:00:00Z",
            alive=lambda pid: False,
        )
        stdout = io.StringIO()
        stderr = io.StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            code = run_lock.main([
                "release", "--lock-file", str(path), "--run-id", "run_1",
            ])
        self.assertEqual(code, 1)
        self.assertEqual(stdout.getvalue(), "")
        self.assertIn("another host", stderr.getvalue())

    def test_cli_errors_write_only_to_stderr(self) -> None:
        path = run_lock.acquire(
            self.root, "app_x", "tbl_x", "run_1", pid=123,
            hostname="other-host", started_at="2026-08-15T02:00:00Z", alive=lambda pid: False,
        )
        stdout = io.StringIO()
        stderr = io.StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            code = run_lock.main([
                "release", "--lock-file", str(path), "--run-id", "run_2",
            ])
        self.assertEqual(code, 1)
        self.assertEqual(stdout.getvalue(), "")
        self.assertTrue(stderr.getvalue().startswith("run-lock error: "))


if __name__ == "__main__":
    unittest.main()
