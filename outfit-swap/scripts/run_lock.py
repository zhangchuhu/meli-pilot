"""Same-machine ownership locks for outfit-swap runs."""

from __future__ import annotations

import argparse
import contextlib
import ctypes
import datetime
import fcntl
import hashlib
import json
import os
import secrets
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path
from typing import Any, Callable, Iterator


class LockHeldError(RuntimeError):
    """Raised when an existing lock cannot safely be replaced."""


def canonical_lock_root() -> Path:
    """Return the one per-user lock root shared by every outfit-swap workspace."""
    return Path.home() / ".codex" / "state" / "outfit-swap" / "locks"


def lock_path(lock_root: str | Path, base_token: str, table_id: str) -> Path:
    """Return the stable, non-secret lock path for a Base table."""
    digest = hashlib.sha256(f"{base_token}\0{table_id}".encode()).hexdigest()[:20]
    return Path(lock_root).resolve() / f"{digest}.lock"


def _guard_path(path: Path) -> Path:
    """Return the permanent inode used to serialize mutations of one lock path."""
    return path.with_name(f".{path.name}.guard")


@contextlib.contextmanager
def _mutation_guard(path: Path) -> Iterator[None]:
    """Serialize every acquire/reclaim/release without ever unlinking the guard inode."""
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(_guard_path(path), os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)


def pid_is_alive(pid: int) -> bool:
    """Report whether a local process ID still exists."""
    if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def process_start_identity(pid: int) -> str | None:
    """Return a stable OS start identity for a live PID, or None if unknowable."""
    if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
        return None
    proc_stat = Path(f"/proc/{pid}/stat")
    try:
        value = proc_stat.read_text(encoding="utf-8")
    except OSError:
        pass
    else:
        closing_parenthesis = value.rfind(")")
        fields_after_name = value[closing_parenthesis + 2:].split()
        if closing_parenthesis > 0 and len(fields_after_name) > 19:
            return f"proc-start-ticks:{fields_after_name[19]}"
    if sys.platform == "darwin":
        unsigned = ctypes.c_uint32

        class ProcBSDInfo(ctypes.Structure):
            _fields_ = [
                ("flags", unsigned), ("status", unsigned),
                ("xstatus", unsigned), ("pid", unsigned),
                ("ppid", unsigned), ("uid", unsigned), ("gid", unsigned),
                ("ruid", unsigned), ("rgid", unsigned),
                ("svuid", unsigned), ("svgid", unsigned), ("rfu", unsigned),
                ("comm", ctypes.c_char * 16), ("name", ctypes.c_char * 32),
                ("nfiles", unsigned), ("pgid", unsigned),
                ("pjobc", unsigned), ("e_tdev", unsigned),
                ("e_tpgid", unsigned), ("nice", ctypes.c_int32),
                ("start_sec", ctypes.c_uint64),
                ("start_usec", ctypes.c_uint64),
            ]

        try:
            libproc = ctypes.CDLL("/usr/lib/libproc.dylib")
            info = ProcBSDInfo()
            size = libproc.proc_pidinfo(
                pid, 3, 0, ctypes.byref(info), ctypes.sizeof(info),
            )
        except (OSError, AttributeError):
            pass
        else:
            if size == ctypes.sizeof(info) and info.pid == pid and info.start_sec:
                return f"darwin-start:{info.start_sec}:{info.start_usec}"
    try:
        result = subprocess.run(
            ["ps", "-o", "lstart=", "-p", str(pid)],
            capture_output=True, text=True, check=False, timeout=2,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    started = " ".join(result.stdout.split())
    if result.returncode != 0 or not started:
        return None
    return f"ps-lstart:{started}"


def _inspect_lock(path: Path) -> tuple[dict[str, Any], tuple[int, int]]:
    """Return validated lock contents and the identity of the opened file."""
    try:
        with path.open(encoding="utf-8") as handle:
            lock = json.load(handle)
            status = os.fstat(handle.fileno())
    except FileNotFoundError:
        raise
    except (OSError, json.JSONDecodeError, UnicodeError) as error:
        raise LockHeldError(f"cannot inspect existing lock: {path}") from error
    if (not isinstance(lock, dict)
            or not isinstance(lock.get("run_id"), str) or not lock["run_id"]
            or not isinstance(lock.get("pid"), int) or isinstance(lock["pid"], bool)
            or lock["pid"] <= 0
            or not isinstance(lock.get("hostname"), str) or not lock["hostname"]):
        raise LockHeldError(f"existing lock is invalid: {path}")
    control_path = lock.get("control_path")
    control_token = lock.get("control_token")
    stored_process_identity = lock.get("process_identity")
    if (stored_process_identity is not None
            and (not isinstance(stored_process_identity, str)
                 or not stored_process_identity)):
        raise LockHeldError(f"existing lock has invalid process identity: {path}")
    if control_path is not None or control_token is not None:
        if (not isinstance(control_path, str) or not control_path
                or not isinstance(control_token, str) or not control_token
                or not isinstance(stored_process_identity, str)
                or not stored_process_identity):
            raise LockHeldError(f"existing lock has invalid holder control: {path}")
        endpoint = Path(control_path)
        if (not endpoint.is_absolute()
                or endpoint.parent.resolve() != path.parent.resolve()):
            raise LockHeldError(f"existing lock has unsafe holder control: {path}")
    return lock, (status.st_dev, status.st_ino)


def _read_lock(path: Path) -> dict[str, Any]:
    return _inspect_lock(path)[0]


def _reclaim_marker(path: Path) -> Path:
    return path.with_name(f".{path.name}.reclaim")


def _validate_marker(owner: Any, marker: Path) -> dict[str, Any]:
    if (not isinstance(owner, dict)
            or not isinstance(owner.get("run_id"), str) or not owner["run_id"]
            or not isinstance(owner.get("pid"), int) or isinstance(owner["pid"], bool)
            or owner["pid"] <= 0
            or not isinstance(owner.get("hostname"), str) or not owner["hostname"]
            or not isinstance(owner.get("started_at"), str) or not owner["started_at"]
            or not isinstance(owner.get("target_device"), int)
            or isinstance(owner["target_device"], bool)
            or owner["target_device"] < 0
            or not isinstance(owner.get("target_inode"), int)
            or isinstance(owner["target_inode"], bool)
            or owner["target_inode"] <= 0):
        raise LockHeldError(f"recovery marker is invalid: {marker}")
    return owner


def _write_all(descriptor: int, payload: bytes) -> None:
    offset = 0
    while offset < len(payload):
        written = os.write(descriptor, payload[offset:])
        if written <= 0:
            raise OSError("could not finish writing lock state")
        offset += written


def _publish_exclusive(path: Path, payload: bytes) -> bool:
    """Publish complete bytes at path without exposing a partial file."""
    descriptor, temporary_name = tempfile.mkstemp(
        dir=path.parent, prefix=f".{path.name}.", suffix=".tmp",
    )
    temporary = Path(temporary_name)
    try:
        os.fchmod(descriptor, 0o600)
        _write_all(descriptor, payload)
        os.fsync(descriptor)
        os.close(descriptor)
        descriptor = -1
        try:
            os.link(temporary, path)
        except FileExistsError:
            return False
        return True
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def _marker_payload(contents: dict[str, Any], identity: tuple[int, int]) -> bytes:
    owner = {key: contents[key] for key in ("run_id", "pid", "hostname", "started_at")}
    owner.update({"target_device": identity[0], "target_inode": identity[1]})
    return (json.dumps(owner, sort_keys=True, separators=(",", ":")) + "\n").encode()


def _claim_reclaim_marker(marker: Path, payload: bytes) -> int | None:
    """Atomically publish and retain an exclusively locked recovery marker."""
    descriptor, temporary_name = tempfile.mkstemp(
        dir=marker.parent, prefix=f".{marker.name}.", suffix=".tmp",
    )
    temporary = Path(temporary_name)
    keep_open = False
    try:
        os.fchmod(descriptor, 0o600)
        _write_all(descriptor, payload)
        os.fsync(descriptor)
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        try:
            os.link(temporary, marker)
        except FileExistsError:
            return None
        keep_open = True
        return descriptor
    finally:
        if not keep_open:
            os.close(descriptor)
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def _unlink_owned_marker(marker: Path, descriptor: int) -> None:
    """Unlink marker only while holding and still naming its exact inode."""
    owned = os.fstat(descriptor)
    try:
        current = os.stat(marker)
    except FileNotFoundError:
        return
    if (current.st_dev, current.st_ino) == (owned.st_dev, owned.st_ino):
        marker.unlink()


def _restore_archived_lock(path: Path, archive: Path, identity: tuple[int, int]) -> None:
    """Restore an interrupted in-place reclaim without touching a replacement path."""
    try:
        descriptor = os.open(path, os.O_RDWR)
    except FileNotFoundError:
        return
    try:
        status = os.fstat(descriptor)
        if (status.st_dev, status.st_ino) != identity:
            return
        try:
            payload = archive.read_bytes()
        except FileNotFoundError:
            return
        os.lseek(descriptor, 0, os.SEEK_SET)
        _write_all(descriptor, payload)
        os.ftruncate(descriptor, len(payload))
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _recover_orphan_marker(path: Path, marker: Path, hostname: str,
                           alive: Callable[[int], bool]) -> None:
    """Recover a marker after proving no reclaimer still owns its flock."""
    try:
        descriptor = os.open(marker, os.O_RDONLY)
    except FileNotFoundError:
        return
    try:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise LockHeldError(f"lock recovery is in progress: {marker}") from error
        opened = os.fstat(descriptor)
        try:
            current = os.stat(marker)
        except FileNotFoundError:
            return
        if (current.st_dev, current.st_ino) != (opened.st_dev, opened.st_ino):
            return
        try:
            payload = os.read(descriptor, opened.st_size + 1)
            owner = _validate_marker(json.loads(payload.decode("utf-8")), marker)
        except (json.JSONDecodeError, UnicodeError) as error:
            raise LockHeldError(f"cannot inspect recovery marker: {marker}") from error
        # Successful nonblocking exclusive flock acquisition is the authoritative
        # ownership proof. PIDs can be reused; a live reclaimer would still own
        # this exact inode's flock and could not reach this point.
        identity = (owner["target_device"], owner["target_inode"])
        archive = path.with_name(f"{path.name}.stale-{owner['run_id']}")
        _restore_archived_lock(path, archive, identity)
        _unlink_owned_marker(marker, descriptor)
    finally:
        os.close(descriptor)


def _write_new_lock(path: Path, contents: dict[str, Any], *, respect_marker: bool = True) -> bool:
    marker = _reclaim_marker(path)
    if respect_marker and marker.exists():
        return False
    descriptor, temporary_name = tempfile.mkstemp(
        dir=path.parent, prefix=f".{path.name}.", suffix=".tmp",
    )
    temporary = Path(temporary_name)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            descriptor = -1
            json.dump(contents, handle, sort_keys=True, separators=(",", ":"))
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        try:
            os.link(temporary, path)
        except FileExistsError:
            return False
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass
    if respect_marker and marker.exists():
        path.unlink()
        return False
    return True


def _try_reclaim(path: Path, expected_lock: dict[str, Any],
                 expected_identity: tuple[int, int], contents: dict[str, Any],
                 stale_path: Path) -> Path | None:
    """Reclaim exactly the stale file inspected by this contender, if still present."""
    marker = _reclaim_marker(path)
    marker_descriptor = _claim_reclaim_marker(
        marker, _marker_payload(contents, expected_identity),
    )
    if marker_descriptor is None:
        return None
    try:
        try:
            _, current_identity = _inspect_lock(path)
        except FileNotFoundError:
            return None
        if current_identity != expected_identity:
            return None
        try:
            descriptor = os.open(path, os.O_RDWR)
        except FileNotFoundError:
            return None
        try:
            status = os.fstat(descriptor)
            current_identity = (status.st_dev, status.st_ino)
            if current_identity != expected_identity:
                return None
            try:
                old_payload = os.read(descriptor, status.st_size + 1)
                current_lock = json.loads(old_payload.decode("utf-8"))
            except (json.JSONDecodeError, UnicodeError) as error:
                raise LockHeldError(f"cannot inspect existing lock: {path}") from error
            if current_lock != expected_lock:
                return None
            if not _publish_exclusive(stale_path, old_payload):
                try:
                    if stale_path.read_bytes() != old_payload:
                        raise LockHeldError(f"stale lock archive already exists: {stale_path}")
                except OSError as error:
                    raise LockHeldError(f"cannot inspect stale lock archive: {stale_path}") from error
            new_payload = (
                json.dumps(contents, sort_keys=True, separators=(",", ":")) + "\n"
            ).encode()
            os.lseek(descriptor, 0, os.SEEK_SET)
            _write_all(descriptor, new_payload)
            os.ftruncate(descriptor, len(new_payload))
            os.fsync(descriptor)
            try:
                current = os.stat(path)
            except FileNotFoundError:
                return None
            if (current.st_dev, current.st_ino) != expected_identity:
                return None
            return path
        finally:
            os.close(descriptor)
    finally:
        try:
            _unlink_owned_marker(marker, marker_descriptor)
        finally:
            os.close(marker_descriptor)


def _control_request(
        lock: dict[str, Any], action: str, *, timeout: float = 1.0,
) -> bool:
    """Exchange one authenticated request through holder-polled sidecar files."""
    endpoint = lock.get("control_path")
    token = lock.get("control_token")
    if not isinstance(endpoint, str) or not isinstance(token, str):
        return False
    request_id = secrets.token_hex(16)
    control = Path(endpoint)
    request_path = control.with_name(f"{control.name}.request-{request_id}")
    response_path = control.with_name(f"{control.name}.response-{request_id}")
    request = json.dumps(
        {"action": action, "request_id": request_id,
         "run_id": lock["run_id"], "token": token},
        sort_keys=True, separators=(",", ":"),
    ).encode() + b"\n"
    try:
        if not _publish_exclusive(request_path, request):
            return False
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                response = response_path.read_bytes()
            except FileNotFoundError:
                time.sleep(0.01)
                continue
            if len(response) > 4096:
                return False
            try:
                payload = json.loads(response.decode("utf-8"))
            except (UnicodeError, json.JSONDecodeError):
                return False
            return payload == {
                "action": action, "ok": True,
                "request_id": request_id, "run_id": lock["run_id"],
            }
        return False
    finally:
        for sidecar in (request_path, response_path):
            try:
                sidecar.unlink()
            except FileNotFoundError:
                pass


def _serve_control_once(control: Path, control_token: str, run_id: str) -> bool:
    """Serve pending authenticated sidecars; report a valid release request."""
    should_release = False
    for request_path in control.parent.glob(f"{control.name}.request-*"):
        request_id = request_path.name.removeprefix(f"{control.name}.request-")
        if (len(request_id) != 32
                or any(character not in "0123456789abcdef" for character in request_id)):
            continue
        try:
            with request_path.open("rb") as handle:
                opened = os.fstat(handle.fileno())
                request_bytes = handle.read(4097)
        except FileNotFoundError:
            continue
        action: Any = None
        authorized = False
        try:
            if len(request_bytes) > 4096:
                raise ValueError("control request is too large")
            request = json.loads(request_bytes.decode("utf-8"))
            action = request.get("action")
            authorized = (
                request.get("request_id") == request_id
                and request.get("run_id") == run_id
                and request.get("token") == control_token
                and action in {"ping", "release"}
            )
        except (UnicodeError, json.JSONDecodeError, ValueError, AttributeError):
            pass
        response_path = control.with_name(f"{control.name}.response-{request_id}")
        response = json.dumps(
            {"action": action, "ok": authorized,
             "request_id": request_id, "run_id": run_id},
            sort_keys=True, separators=(",", ":"),
        ).encode() + b"\n"
        _publish_exclusive(response_path, response)
        try:
            current = request_path.stat()
        except FileNotFoundError:
            pass
        else:
            if (current.st_dev, current.st_ino) == (opened.st_dev, opened.st_ino):
                request_path.unlink()
        should_release = should_release or authorized and action == "release"
    return should_release


def _holder_is_live(
        lock: dict[str, Any], hostname: str, alive: Callable[[int], bool],
) -> bool:
    """Verify a same-host protocol holder without trusting a reusable PID alone."""
    if lock["hostname"] != hostname:
        return True
    if "control_path" in lock:
        if _control_request(lock, "ping"):
            return True
        if not alive(lock["pid"]):
            return False
        current_identity = process_start_identity(lock["pid"])
        return (
            current_identity is None
            or current_identity == lock["process_identity"]
        )
    return alive(lock["pid"])


def acquire(lock_root: str | Path, base_token: str, table_id: str, run_id: str, *,
            pid: int, hostname: str, started_at: str,
            alive: Callable[[int], bool], control_path: str | None = None,
            control_token: str | None = None,
            process_identity: str | None = None) -> Path:
    """Atomically create a lock or reject a lock held by a live run."""
    for name, value in (
        ("base_token", base_token), ("table_id", table_id),
        ("run_id", run_id), ("hostname", hostname), ("started_at", started_at),
    ):
        if not isinstance(value, str) or not value:
            raise LockHeldError(f"{name} must be a non-empty string")
    if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
        raise LockHeldError("pid must be a positive integer")
    path = lock_path(lock_root, base_token, table_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    contents = {
        "base_token_sha256": hashlib.sha256(base_token.encode()).hexdigest(),
        "table_id_sha256": hashlib.sha256(table_id.encode()).hexdigest(),
        "run_id": run_id,
        "pid": pid,
        "hostname": hostname,
        "started_at": started_at,
    }
    if process_identity is not None:
        if not isinstance(process_identity, str) or not process_identity:
            raise LockHeldError("process identity must be a non-empty string")
        contents["process_identity"] = process_identity
    if control_path is not None or control_token is not None:
        if (not isinstance(control_path, str) or not control_path
                or not isinstance(control_token, str) or not control_token
                or not isinstance(process_identity, str) or not process_identity):
            raise LockHeldError(
                "holder control path, token, and process identity must be non-empty",
            )
        endpoint = Path(control_path)
        if (not endpoint.is_absolute()
                or endpoint.parent.resolve() != path.parent.resolve()):
            raise LockHeldError("holder control path must be inside the lock root")
        contents.update({
            "control_path": control_path,
            "control_token": control_token,
        })
    while True:
        with _mutation_guard(path):
            marker = _reclaim_marker(path)
            if marker.exists():
                _recover_orphan_marker(path, marker, hostname, alive)
                continue
            if _write_new_lock(path, contents):
                return path
            try:
                existing, identity = _inspect_lock(path)
            except FileNotFoundError:
                continue
            if _holder_is_live(existing, hostname, alive):
                raise LockHeldError(f"lock is held: {path}")
            stale_path = path.with_name(f"{path.name}.stale-{run_id}")
            claimed = _try_reclaim(path, existing, identity, contents, stale_path)
            if claimed is None:
                continue
            return claimed


def release(lock_file: str | Path, run_id: str) -> bool:
    """Remove a lock only when it still belongs to the specified run."""
    path = Path(lock_file)
    with _mutation_guard(path):
        lock = _read_lock(path)
        if lock["run_id"] != run_id:
            raise LockHeldError(f"lock belongs to another run: {path}")
        current = _read_lock(path)
        if current["run_id"] != run_id:
            raise LockHeldError(f"lock belongs to another run: {path}")
        try:
            path.unlink()
        except FileNotFoundError as error:
            raise LockHeldError(f"lock disappeared before release: {path}") from error
    return True


def _hold(base_token: str, table_id: str, run_id: str) -> None:
    """Own a table lock in this process until an explicit release signal arrives."""
    stopping = threading.Event()

    def request_stop(_signal_number: int, _frame: Any) -> None:
        stopping.set()

    previous = {
        signal_number: signal.signal(signal_number, request_stop)
        for signal_number in (signal.SIGINT, signal.SIGTERM)
    }
    path: Path | None = None
    prospective_path = lock_path(canonical_lock_root(), base_token, table_id)
    prospective_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    control_token = secrets.token_hex(16)
    holder_process_identity = process_start_identity(os.getpid())
    if holder_process_identity is None:
        raise LockHeldError("cannot determine holder process start identity")
    control = prospective_path.with_name(
        f".{prospective_path.name}.{control_token[:16]}.control"
    )
    try:
        path = acquire(
            canonical_lock_root(), base_token, table_id, run_id,
            pid=os.getpid(), hostname=socket.gethostname(),
            started_at=datetime.datetime.now(datetime.timezone.utc).isoformat().replace("+00:00", "Z"),
            alive=pid_is_alive,
            control_path=str(control), control_token=control_token,
            process_identity=holder_process_identity,
        )
        _json_stdout({
            "holder_pid": os.getpid(), "lock_file": str(path), "run_id": run_id,
        }, flush=True)
        while not stopping.is_set():
            if _serve_control_once(control, control_token, run_id):
                stopping.set()
            else:
                stopping.wait(0.02)
    finally:
        if path is not None:
            try:
                release(path, run_id)
            except LockHeldError:
                pass
        for signal_number, handler in previous.items():
            signal.signal(signal_number, handler)


def request_release(lock_file: str | Path, run_id: str, *, timeout: float = 5.0) -> bool:
    """Ask the long-lived owner to release, and wait until its marker is gone."""
    path = Path(lock_file)
    lock = _read_lock(path)
    if lock["run_id"] != run_id:
        raise LockHeldError(f"lock belongs to another run: {path}")
    if lock["hostname"] != socket.gethostname():
        raise LockHeldError(f"lock owner is on another host: {path}")
    if "control_path" in lock:
        try:
            requested = _control_request(lock, "release")
        except OSError:
            requested = False
        if not requested:
            if _holder_is_live(lock, socket.gethostname(), pid_is_alive):
                raise LockHeldError(f"lock holder refused release: {path}")
            try:
                return release(path, run_id)
            except FileNotFoundError:
                return True
    elif not pid_is_alive(lock["pid"]):
        return release(path, run_id)
    else:
        raise LockHeldError(f"live legacy lock has no authenticated release channel: {path}")
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            current = _read_lock(path)
        except FileNotFoundError:
            return True
        if current["run_id"] != run_id:
            return True
        time.sleep(0.02)
    raise LockHeldError(f"lock holder did not release: {path}")


def _parser() -> argparse.ArgumentParser:
    class RunLockArgumentParser(argparse.ArgumentParser):
        def error(self, message: str) -> None:
            raise LockHeldError(message)

    parser = RunLockArgumentParser(prog="run_lock")
    commands = parser.add_subparsers(dest="command", required=True)
    hold_parser = commands.add_parser("hold")
    hold_parser.add_argument("--base-token", required=True)
    hold_parser.add_argument("--table-id", required=True)
    hold_parser.add_argument("--run-id", required=True)
    release_parser = commands.add_parser("release")
    release_parser.add_argument("--lock-file", required=True)
    release_parser.add_argument("--run-id", required=True)
    return parser


def _json_stdout(value: Any, *, flush: bool = False) -> None:
    print(
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
        flush=flush,
    )


def main(argv: list[str] | None = None) -> int:
    """Run the lock command-line interface."""
    try:
        args = _parser().parse_args(argv)
        if args.command == "hold":
            _hold(args.base_token, args.table_id, args.run_id)
        else:
            request_release(args.lock_file, args.run_id)
            _json_stdout({"released": True})
        return 0
    except (LockHeldError, OSError, TypeError, ValueError) as error:
        print(f"run-lock error: {error}", file=os.sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
