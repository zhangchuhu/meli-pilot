import base64
import io
import json
import os
import stat
import sys
import tempfile
import threading
import traceback
import unittest
import urllib.error
import urllib.response
from email.message import Message
from pathlib import Path
from typing import Any
from unittest import mock


sys.path.insert(0, str(Path(__file__).parent))
import ark_vision_qc


def report_json(
        *, candidate: str = "candidate.png", confidence: float = 0.94,
        decision: str = "accept",
) -> str:
    return json.dumps({
        "schema_version": 1,
        "candidate": candidate,
        "scores": {
            "garment_construction": 96,
            "color_material": 94,
            "garment_details": 93,
            "target_preservation": 92,
            "text_layout": None,
        },
        "critical_defects": [],
        "primary_defect": None,
        "evidence": [],
        "confidence": confidence,
        "decision": decision,
        "exact_text": None, "added_text": None, "missing_text": None,
        "instances_exact": None, "panel_count_exact": None,
        "panel_layout_exact": None,
    })


def response_body(
        content: Any = None, *, finish_reason: str = "stop",
) -> bytes:
    if content is None:
        content = report_json()
    return json.dumps({
        "id": "chatcmpl-safe-id",
        "object": "chat.completion",
        "choices": [{
            "index": 0,
            "message": {"role": "assistant", "content": content},
            "finish_reason": finish_reason,
        }],
    }).encode("utf-8")


class FakeResponse:
    def __init__(
            self, body: bytes,
            url: str = "https://ark.cn-beijing.volces.com/api/v3/chat/completions",
    ) -> None:
        self.body = body
        self.url = url

    def __enter__(self) -> "FakeResponse":
        return self

    def __exit__(self, *args: object) -> None:
        return None

    def read(self, amount: int = -1) -> bytes:
        if amount < 0:
            return self.body
        return self.body[:amount]

    def geturl(self) -> str:
        return self.url


class RecordingOpener:
    def __init__(self, body: bytes) -> None:
        self.body = body
        self.calls: list[tuple[Any, float]] = []

    def __call__(self, request: Any, *, timeout: float) -> FakeResponse:
        self.calls.append((request, timeout))
        return FakeResponse(self.body)


class _SanitizedErrorAssertions:
    def value_contains_sentinel(
            self, value: object, sentinel: str, seen: set[int],
    ) -> bool:
        if id(value) in seen:
            return False
        seen.add(id(value))
        if isinstance(value, str):
            return sentinel in value
        if isinstance(value, bytes):
            return sentinel.encode("utf-8") in value
        if isinstance(value, dict):
            return any(
                self.value_contains_sentinel(item, sentinel, seen)
                for pair in value.items() for item in pair
            )
        if isinstance(value, (list, tuple, set, frozenset)):
            return any(
                self.value_contains_sentinel(item, sentinel, seen)
                for item in value
            )
        attributes = getattr(value, "__dict__", None)
        if isinstance(attributes, dict):
            return self.value_contains_sentinel(
                attributes, sentinel, seen,
            )
        return False

    def assert_traceback_locals_are_sanitized(
            self, exception: BaseException, *sentinels: str,
    ) -> None:
        traceback_node = exception.__traceback__
        production_frames = []
        while traceback_node is not None:
            frame = traceback_node.tb_frame
            if Path(frame.f_code.co_filename).resolve() == Path(
                    ark_vision_qc.__file__,
            ).resolve():
                production_frames.append(frame)
            traceback_node = traceback_node.tb_next
        self.assertTrue(production_frames, "expected a production traceback frame")
        for frame in production_frames:
            for name, value in frame.f_locals.items():
                for sentinel in sentinels:
                    self.assertFalse(
                        self.value_contains_sentinel(value, sentinel, set()),
                        f"{sentinel!r} retained by traceback local {name!r} "
                        f"in {frame.f_code.co_name}",
                    )

    def capture_ark_error(self, operation: Any) -> ark_vision_qc.ArkVisionError:
        try:
            operation()
        except ark_vision_qc.ArkVisionError as exception:
            return exception
        self.fail("ArkVisionError was not raised")

    def assert_exception_is_sanitized(
            self, exception: BaseException, *sentinels: str,
    ) -> None:
        pending: list[BaseException] = [exception]
        seen: set[int] = set()
        graph: list[BaseException] = []
        while pending:
            current = pending.pop()
            if id(current) in seen:
                continue
            seen.add(id(current))
            graph.append(current)
            if current.__cause__ is not None:
                pending.append(current.__cause__)
            if current.__context__ is not None:
                pending.append(current.__context__)
        rendered_graph = "\n".join(
            repr((type(item).__name__, item.args, vars(item))) for item in graph
        )
        formatted = "".join(traceback.format_exception(exception))
        for sentinel in sentinels:
            self.assertNotIn(sentinel, rendered_graph)
            self.assertNotIn(sentinel, formatted)
        self.assertIsNone(exception.__cause__)
        self.assertIsNone(exception.__context__)


class ArkVisionClientTest(_SanitizedErrorAssertions, unittest.TestCase):
    def test_archives_exact_success_and_malformed_response_bodies_privately(self) -> None:
        bodies = (
            response_body("{\"result\":\"ok\"}"),
            b'{"choices":["malformed-response-sentinel"',
        )
        for body in bodies:
            with self.subTest(body=body[:20]), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                archive = root / "ark-responses"
                image = root / "candidate.png"
                image.write_bytes(b"image")
                client = ark_vision_qc.ArkVisionClient(
                    environ={"ARK_API_KEY": "secret-key", "ARK_VISION_MODEL": "model"},
                    opener=RecordingOpener(body),
                    response_archive_dir=archive,
                )

                try:
                    client.complete_json(
                        system_prompt="system", user_prompt="user", images=(image,),
                    )
                except ark_vision_qc.ArkVisionError:
                    pass

                body_files = sorted(archive.glob("*.body"))
                metadata_files = sorted(archive.glob("*.json"))
                self.assertEqual(len(body_files), 1)
                self.assertEqual(len(metadata_files), 1)
                self.assertEqual(body_files[0].read_bytes(), body)
                metadata = json.loads(metadata_files[0].read_text(encoding="utf-8"))
                self.assertEqual(metadata["body_file"], body_files[0].name)
                self.assertEqual(metadata["body_bytes"], len(body))
                serialized = metadata_files[0].read_text(encoding="utf-8")
                self.assertNotIn("secret-key", serialized)
                self.assertNotIn("Authorization", serialized)
                self.assertEqual(stat.S_IMODE(archive.stat().st_mode), 0o700)
                self.assertEqual(stat.S_IMODE(body_files[0].stat().st_mode), 0o600)
                self.assertEqual(stat.S_IMODE(metadata_files[0].stat().st_mode), 0o600)

    def test_archives_http_error_body_and_redacts_credential_response_headers(self) -> None:
        body = b'{"error":{"message":"invalid model"}}'
        headers = Message()
        headers["Content-Type"] = "application/json"
        headers["Set-Cookie"] = "session=remote-secret"

        def opener(request: Any, *, timeout: float) -> FakeResponse:
            raise urllib.error.HTTPError(
                request.full_url, 400, "Bad Request", headers, io.BytesIO(body),
            )

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            archive = root / "ark-responses"
            image = root / "candidate.png"
            image.write_bytes(b"image")
            client = ark_vision_qc.ArkVisionClient(
                environ={"ARK_API_KEY": "request-secret", "ARK_VISION_MODEL": "model"},
                opener=opener,
                response_archive_dir=archive,
            )
            with self.assertRaises(ark_vision_qc.ArkVisionError):
                client.complete_json(
                    system_prompt="system", user_prompt="user", images=(image,),
                )

            self.assertEqual(next(archive.glob("*.body")).read_bytes(), body)
            metadata_text = next(archive.glob("*.json")).read_text(encoding="utf-8")
            metadata = json.loads(metadata_text)
            self.assertEqual(metadata["http_status"], 400)
            self.assertEqual(metadata["response_headers"]["Set-Cookie"], "[REDACTED]")
            self.assertNotIn("request-secret", metadata_text)
            self.assertNotIn("remote-secret", metadata_text)

    def test_archive_never_persists_api_key_echoed_in_unrelated_header_or_body(self) -> None:
        api_key = "exact-request-api-key-sentinel"
        headers = Message()
        headers["X-Debug"] = f"upstream echoed {api_key}"
        headers[f"X-{api_key}"] = "safe-value"
        safe_body = response_body("{\"result\":\"ok\"}")

        class HeaderResponse(FakeResponse):
            status = 200

            def __init__(self) -> None:
                super().__init__(safe_body)
                self.headers = headers

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            archive = root / "ark-responses"
            image = root / "candidate.png"
            image.write_bytes(b"image")
            client = ark_vision_qc.ArkVisionClient(
                environ={"ARK_API_KEY": api_key, "ARK_VISION_MODEL": "model"},
                opener=lambda _request, *, timeout: HeaderResponse(),
                response_archive_dir=archive,
            )
            client.complete_json(
                system_prompt="system", user_prompt="user", images=(image,),
            )
            metadata = next(archive.glob("*.json")).read_text(encoding="utf-8")
            self.assertNotIn(api_key, metadata)
            self.assertIn("[REDACTED]", metadata)

        echoed_body = response_body(api_key)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            archive = root / "ark-responses"
            image = root / "candidate.png"
            image.write_bytes(b"image")
            client = ark_vision_qc.ArkVisionClient(
                environ={"ARK_API_KEY": api_key, "ARK_VISION_MODEL": "model"},
                opener=RecordingOpener(echoed_body),
                response_archive_dir=archive,
            )
            with self.assertRaisesRegex(
                    ark_vision_qc.ArkVisionError, "contained a request credential",
            ):
                client.complete_json(
                    system_prompt="system", user_prompt="user", images=(image,),
                )
            self.assertFalse(archive.exists())

    def test_network_client_requires_a_response_archive(self) -> None:
        with self.assertRaisesRegex(ValueError, "response archive"):
            ark_vision_qc.ArkVisionClient(environ={
                "ARK_API_KEY": "key", "ARK_VISION_MODEL": "model",
            })

    def test_http_error_does_not_hide_response_archive_failure(self) -> None:
        body = b'{"error":"invalid model"}'

        def opener(request: Any, *, timeout: float) -> FakeResponse:
            raise urllib.error.HTTPError(
                request.full_url, 400, "Bad Request", Message(), io.BytesIO(body),
            )

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            blocked_archive = root / "ark-responses"
            blocked_archive.write_text("not a directory", encoding="utf-8")
            image = root / "candidate.png"
            image.write_bytes(b"image")
            client = ark_vision_qc.ArkVisionClient(
                environ={"ARK_API_KEY": "key", "ARK_VISION_MODEL": "model"},
                opener=opener,
                response_archive_dir=blocked_archive,
            )

            with self.assertRaisesRegex(
                    ark_vision_qc.ArkVisionError, "archive is unavailable",
            ):
                client.complete_json(
                    system_prompt="system", user_prompt="user", images=(image,),
                )

    def test_sidecar_failure_removes_committed_body(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            archive_dir = Path(directory) / "ark-responses"
            archive = ark_vision_qc.ArkResponseArchive(archive_dir)
            original = archive._atomic_write
            calls = 0

            def fail_second(path: Path, data: bytes) -> None:
                nonlocal calls
                calls += 1
                if calls == 2:
                    raise ark_vision_qc.ArkVisionError(
                        "Ark response archive is unavailable",
                    )
                original(path, data)

            archive._atomic_write = fail_second
            with self.assertRaises(ark_vision_qc.ArkVisionError):
                archive.record(
                    b"raw response", http_status=200, response_headers={},
                )
            self.assertEqual(list(archive_dir.glob("*.body")), [])

    def test_directory_swap_cannot_redirect_archive_writes(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            archive_dir = root / "ark-responses"
            displaced = root / "original-archive"
            escape = root / "escape"
            escape.mkdir()
            archive = ark_vision_qc.ArkResponseArchive(archive_dir)
            original = archive._prepare_directory

            def swap_after_validation() -> object:
                result = original()
                archive_dir.rename(displaced)
                archive_dir.symlink_to(escape, target_is_directory=True)
                return result

            archive._prepare_directory = swap_after_validation
            archive.record(
                b"raw response", http_status=200, response_headers={},
            )

            self.assertEqual(list(escape.iterdir()), [])
            self.assertEqual(len(list(displaced.glob("*.body"))), 1)
            self.assertEqual(len(list(displaced.glob("*.json"))), 1)

    def test_parent_directory_swap_cannot_redirect_archive_writes(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            run = root / "run"
            archive_dir = run / "ark-responses"
            displaced = root / "original-run"
            escape = root / "escape"
            run.mkdir()
            escape.mkdir()
            archive = ark_vision_qc.ArkResponseArchive(archive_dir)
            original_open = os.open
            swapped = False

            def racing_open(path: object, flags: int, *args: object, **kwargs: object) -> int:
                nonlocal swapped
                if not swapped and Path(path).name == "ark-responses":
                    swapped = True
                    run.rename(displaced)
                    run.symlink_to(escape, target_is_directory=True)
                return original_open(path, flags, *args, **kwargs)

            with mock.patch.object(ark_vision_qc.os, "open", side_effect=racing_open):
                archive.record(
                    b"raw response", http_status=200, response_headers={},
                )

            self.assertTrue(swapped)
            self.assertEqual(list(escape.iterdir()), [])
            self.assertEqual(len(list((displaced / "ark-responses").glob("*.body"))), 1)
            self.assertEqual(len(list((displaced / "ark-responses").glob("*.json"))), 1)

    def test_concurrent_responses_use_distinct_archive_files(self) -> None:
        body = response_body("{\"result\":\"ok\"}")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            archive = root / "ark-responses"
            image = root / "candidate.png"
            image.write_bytes(b"image")
            client = ark_vision_qc.ArkVisionClient(
                environ={"ARK_API_KEY": "key", "ARK_VISION_MODEL": "model"},
                opener=RecordingOpener(body),
                response_archive_dir=archive,
            )
            threads = [threading.Thread(target=lambda: client.complete_json(
                system_prompt="system", user_prompt="user", images=(image,),
            )) for _ in range(8)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(timeout=2)

            self.assertTrue(all(not thread.is_alive() for thread in threads))
            self.assertEqual(len(list(archive.glob("*.body"))), 8)
            self.assertEqual(len(list(archive.glob("*.json"))), 8)
            self.assertEqual(
                {path.name for path in archive.glob("*.body")},
                {f"{index:06d}.body" for index in range(1, 9)},
            )

    def test_posts_exact_multimodal_request_with_env_model_and_credentials(self) -> None:
        opener = RecordingOpener(response_body("{\"result\":\"ok\"}"))
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            png = root / "look.png"
            jpeg = root / "detail.jpg"
            png.write_bytes(b"png-bytes")
            jpeg.write_bytes(b"jpeg-bytes")
            client = ark_vision_qc.ArkVisionClient(
                environ={"ARK_API_KEY": "secret-key", "ARK_VISION_MODEL": "ep-vision"},
                opener=opener,
                timeout_seconds=7.5,
            )

            content = client.complete_json(
                system_prompt="Return strict JSON.",
                user_prompt="Review this candidate.",
                images=(png, jpeg),
            )

        self.assertEqual(content, "{\"result\":\"ok\"}")
        self.assertEqual(len(opener.calls), 1)
        request, timeout = opener.calls[0]
        self.assertEqual(request.full_url, ark_vision_qc.ARK_CHAT_ENDPOINT)
        self.assertEqual(timeout, 7.5)
        headers = {name.lower(): value for name, value in request.header_items()}
        self.assertEqual(headers["authorization"], "Bearer secret-key")
        self.assertEqual(headers["content-type"], "application/json")
        payload = json.loads(request.data)
        self.assertEqual(payload["model"], "ep-vision")
        self.assertEqual(payload["response_format"], {"type": "json_object"})
        self.assertIn("thinking", payload)
        self.assertEqual(payload["thinking"], {"type": "disabled"})
        self.assertIs(payload["stream"], False)
        self.assertEqual(len(payload["messages"]), 1)
        self.assertEqual(payload["messages"][0]["role"], "user")
        user_content = payload["messages"][0]["content"]
        self.assertEqual(user_content[0], {
            "type": "image_url",
            "image_url": {
                "url": "data:image/png;base64," + base64.b64encode(b"png-bytes").decode("ascii"),
            },
        })
        self.assertEqual(user_content[1], {
            "type": "image_url",
            "image_url": {
                "url": "data:image/jpeg;base64," + base64.b64encode(b"jpeg-bytes").decode("ascii"),
            },
        })
        self.assertEqual(user_content[2], {
            "type": "text",
            "text": (
                "Instructions:\nReturn strict JSON.\n\n"
                "Task:\nReview this candidate."
            ),
        })

    def test_timeout_defaults_to_120_seconds(self) -> None:
        opener = RecordingOpener(response_body("{\"result\":\"ok\"}"))
        with tempfile.TemporaryDirectory() as directory:
            image = Path(directory) / "target.png"
            image.write_bytes(b"image")
            client = ark_vision_qc.ArkVisionClient(
                environ={"ARK_API_KEY": "key", "ARK_VISION_MODEL": "model"},
                opener=opener,
            )
            client.complete_json(
                system_prompt="system", user_prompt="user", images=(image,),
            )

        self.assertEqual(opener.calls[0][1], 120.0)

    def test_timeout_accepts_positive_environment_override(self) -> None:
        opener = RecordingOpener(response_body("{\"result\":\"ok\"}"))
        with tempfile.TemporaryDirectory() as directory:
            image = Path(directory) / "target.png"
            image.write_bytes(b"image")
            client = ark_vision_qc.ArkVisionClient(
                environ={
                    "ARK_API_KEY": "key", "ARK_VISION_MODEL": "model",
                    "OUTFIT_SWAP_ARK_TIMEOUT_SECONDS": "75.5",
                },
                opener=opener,
            )
            client.complete_json(
                system_prompt="system", user_prompt="user", images=(image,),
            )

        self.assertEqual(opener.calls[0][1], 75.5)

    def test_timeout_rejects_invalid_environment_values(self) -> None:
        for value in ("", "0", "-1", "nan", "inf", "not-a-number"):
            with self.subTest(value=value), self.assertRaisesRegex(
                ValueError, "Ark timeout",
            ):
                ark_vision_qc.ArkVisionClient(environ={
                    "ARK_API_KEY": "key", "ARK_VISION_MODEL": "model",
                    "OUTFIT_SWAP_ARK_TIMEOUT_SECONDS": value,
                })

    def test_missing_or_blank_environment_fails_before_file_or_network_io(self) -> None:
        for environ in (
            {},
            {"ARK_API_KEY": "", "ARK_VISION_MODEL": "ep-vision"},
            {"ARK_API_KEY": "secret-key"},
            {"ARK_API_KEY": "secret-key", "ARK_VISION_MODEL": "  "},
        ):
            with self.subTest(environ=environ):
                opener = RecordingOpener(response_body())
                client = ark_vision_qc.ArkVisionClient(environ=environ, opener=opener)
                with self.assertRaises(ark_vision_qc.ArkVisionError):
                    client.complete_json(
                        system_prompt="system",
                        user_prompt="user",
                        images=(Path("missing-image.png"),),
                    )
                self.assertEqual(opener.calls, [])

    def test_timeout_is_reported_without_request_material(self) -> None:
        api_key = "never-leak-api-key"
        base64_fragment = base64.b64encode(b"private-image").decode("ascii")

        def timeout_opener(_request: Any, *, timeout: float) -> FakeResponse:
            self.assertEqual(timeout, 2.0)
            raise TimeoutError(f"timed out {api_key} {base64_fragment}")

        with tempfile.TemporaryDirectory() as directory:
            image = Path(directory) / "candidate.png"
            image.write_bytes(b"private-image")
            client = ark_vision_qc.ArkVisionClient(
                environ={"ARK_API_KEY": api_key, "ARK_VISION_MODEL": "ep-vision"},
                opener=timeout_opener,
                timeout_seconds=2.0,
            )
            with self.assertRaisesRegex(
                    ark_vision_qc.ArkVisionError, "request timed out",
            ) as raised:
                client.complete_json(
                    system_prompt="system", user_prompt="user", images=(image,),
                )

        message = str(raised.exception)
        self.assertNotIn(api_key, message)
        self.assertNotIn(base64_fragment, message)
        self.assert_exception_is_sanitized(
            raised.exception, api_key, base64_fragment,
        )

    def test_remote_failures_never_expose_credentials_base64_or_response_text(self) -> None:
        api_key = "never-leak-api-key"
        remote_body = "complete remote response with private diagnostic"

        def failing_opener(_request: Any, *, timeout: float) -> FakeResponse:
            raise OSError(f"{api_key} data:image/png;base64,AAAA {remote_body}")

        with tempfile.TemporaryDirectory() as directory:
            image = Path(directory) / "candidate.png"
            image.write_bytes(b"private-image")
            client = ark_vision_qc.ArkVisionClient(
                environ={"ARK_API_KEY": api_key, "ARK_VISION_MODEL": "ep-vision"},
                opener=failing_opener,
            )
            with self.assertRaises(ark_vision_qc.ArkVisionError) as raised:
                client.complete_json(
                    system_prompt="system", user_prompt="user", images=(image,),
                )

        message = str(raised.exception)
        self.assertEqual(message, "Ark vision request failed")
        self.assertNotIn(api_key, message)
        self.assertNotIn("base64", message.lower())
        self.assertNotIn(remote_body, message)
        self.assert_exception_is_sanitized(
            raised.exception, api_key, "AAAA", remote_body,
        )

    def test_malformed_response_exception_graph_does_not_retain_remote_body(self) -> None:
        remote_sentinel = "private-remote-response-sentinel"
        api_key = "malformed-api-key-sentinel"
        system_prompt = "malformed-system-prompt-sentinel"
        user_prompt = "malformed-user-prompt-sentinel"
        image_bytes = b"malformed-image-sentinel"
        image_base64 = base64.b64encode(image_bytes).decode("ascii")
        client = ark_vision_qc.ArkVisionClient(
            environ={"ARK_API_KEY": api_key, "ARK_VISION_MODEL": "model"},
            opener=RecordingOpener(
                f'{{"choices":["{remote_sentinel}"'.encode("utf-8"),
            ),
        )
        with tempfile.TemporaryDirectory() as directory:
            image = Path(directory) / "candidate.png"
            image.write_bytes(image_bytes)
            error = self.capture_ark_error(lambda: client.complete_json(
                system_prompt=system_prompt,
                user_prompt=user_prompt,
                images=(image,),
            ))

        self.assert_exception_is_sanitized(error, remote_sentinel)
        self.assert_traceback_locals_are_sanitized(
            error,
            remote_sentinel,
            api_key,
            system_prompt,
            user_prompt,
            image_base64,
            "Authorization",
        )

    def test_transport_error_traceback_frames_do_not_retain_sensitive_locals(self) -> None:
        api_key = "traceback-api-key-sentinel"
        system_prompt = "traceback-system-prompt-sentinel"
        user_prompt = "traceback-user-prompt-sentinel"
        image_bytes = b"traceback-image-sentinel"
        image_base64 = base64.b64encode(image_bytes).decode("ascii")

        def failing_opener(_request: Any, *, timeout: float) -> FakeResponse:
            raise OSError("external-transport-sentinel")

        client = ark_vision_qc.ArkVisionClient(
            environ={"ARK_API_KEY": api_key, "ARK_VISION_MODEL": "model"},
            opener=failing_opener,
        )
        with tempfile.TemporaryDirectory() as directory:
            image = Path(directory) / "candidate.png"
            image.write_bytes(image_bytes)
            error = self.capture_ark_error(lambda: client.complete_json(
                system_prompt=system_prompt,
                user_prompt=user_prompt,
                images=(image,),
            ))

        self.assert_traceback_locals_are_sanitized(
            error,
            api_key,
            system_prompt,
            user_prompt,
            image_base64,
            "Authorization",
            "external-transport-sentinel",
        )

    def test_same_host_and_cross_host_redirects_are_not_followed(self) -> None:
        self.assertTrue(
            hasattr(ark_vision_qc, "_build_rejecting_opener"),
            "Ark transport must build an opener that rejects redirects",
        )
        url_request = getattr(ark_vision_qc.urllib, "request")

        class RedirectingHTTPSHandler(url_request.BaseHandler):
            handler_order = 100

            def __init__(self, location: str, code: int) -> None:
                self.location = location
                self.code = code
                self.requests: list[Any] = []
                self.responses: list[Any] = []

            def https_open(self, request: Any) -> Any:
                self.requests.append(request)
                headers = Message()
                headers["Location"] = self.location
                response = urllib.response.addinfourl(
                    io.BytesIO(b"private redirect body"),
                    headers,
                    request.full_url,
                    code=self.code,
                )
                response.msg = "Redirect"
                self.responses.append(response)
                return response

        key_sentinel = "redirect-secret-key"
        redirect_targets = (
            "https://ark.cn-beijing.volces.com/api/v3/other",
            "https://redirect-secret.invalid/collect",
        )
        for code in (301, 302, 303):
            for target in redirect_targets:
                with self.subTest(code=code, target=target):
                    handler = RedirectingHTTPSHandler(target, code)
                    opener = ark_vision_qc._build_rejecting_opener(handler)
                    client = ark_vision_qc.ArkVisionClient(
                        environ={
                            "ARK_API_KEY": key_sentinel,
                            "ARK_VISION_MODEL": "model",
                        },
                        opener=opener,
                    )
                    with tempfile.TemporaryDirectory() as directory:
                        image = Path(directory) / "candidate.png"
                        image.write_bytes(b"image")
                        with self.assertRaises(ark_vision_qc.ArkVisionError) as raised:
                            client.complete_json(
                                system_prompt="system",
                                user_prompt="private redirect prompt",
                                images=(image,),
                            )

                    self.assertEqual(len(handler.requests), 1)
                    self.assertTrue(handler.responses[0].closed)
                    self.assertEqual(
                        handler.requests[0].full_url,
                        ark_vision_qc.ARK_CHAT_ENDPOINT,
                    )
                    self.assert_exception_is_sanitized(
                        raised.exception,
                        key_sentinel,
                        target,
                        "private redirect body",
                        "private redirect prompt",
                    )

    def test_rejects_success_response_with_nonapproved_final_url(self) -> None:
        for final_url in (
            "https://ark.cn-beijing.volces.com/api/v3/other",
            "https://redirect.invalid/api/v3/chat/completions",
        ):
            with self.subTest(final_url=final_url):
                client = ark_vision_qc.ArkVisionClient(
                    environ={"ARK_API_KEY": "key", "ARK_VISION_MODEL": "model"},
                    opener=lambda _request, *, timeout: FakeResponse(
                        response_body("private redirected response"), url=final_url,
                    ),
                )
                with tempfile.TemporaryDirectory() as directory:
                    image = Path(directory) / "candidate.png"
                    image.write_bytes(b"image")
                    with self.assertRaises(ark_vision_qc.ArkVisionError) as raised:
                        client.complete_json(
                            system_prompt="system", user_prompt="user", images=(image,),
                        )
                self.assert_exception_is_sanitized(
                    raised.exception, final_url, "private redirected response",
                )

    def test_rejects_content_filter_and_truncated_completions(self) -> None:
        for finish_reason, expected in (
            ("content_filter", "content filter"),
            ("length", "truncated"),
        ):
            with self.subTest(finish_reason=finish_reason):
                client = ark_vision_qc.ArkVisionClient(
                    environ={"ARK_API_KEY": "key", "ARK_VISION_MODEL": "model"},
                    opener=RecordingOpener(response_body(
                        "private response body", finish_reason=finish_reason,
                    )),
                )
                with tempfile.TemporaryDirectory() as directory:
                    image = Path(directory) / "candidate.png"
                    image.write_bytes(b"image")
                    with self.assertRaisesRegex(
                            ark_vision_qc.ArkVisionError, expected,
                    ) as raised:
                        client.complete_json(
                            system_prompt="system", user_prompt="user", images=(image,),
                        )
                self.assertNotIn("private response body", str(raised.exception))

    def test_rejects_malformed_json_and_response_shape_without_echoing_body(self) -> None:
        bodies = (
            b'{"choices": [',
            json.dumps({"choices": []}).encode("utf-8"),
            response_body(content={"not": "text"}),
            response_body("", finish_reason="stop"),
        )
        for body in bodies:
            with self.subTest(body=body[:20]):
                client = ark_vision_qc.ArkVisionClient(
                    environ={"ARK_API_KEY": "key", "ARK_VISION_MODEL": "model"},
                    opener=RecordingOpener(body),
                )
                with tempfile.TemporaryDirectory() as directory:
                    image = Path(directory) / "candidate.png"
                    image.write_bytes(b"image")
                    with self.assertRaises(ark_vision_qc.ArkVisionError) as raised:
                        client.complete_json(
                            system_prompt="system", user_prompt="user", images=(image,),
                        )
                self.assertNotIn(body.decode("utf-8", errors="ignore"), str(raised.exception))


class FakeVisionClient:
    def __init__(self, results: list[str | Exception]) -> None:
        self.results = list(results)
        self.calls: list[dict[str, object]] = []

    def complete_json(
            self, *, system_prompt: str, user_prompt: str,
            images: tuple[Path, ...],
    ) -> str:
        self.calls.append({
            "system_prompt": system_prompt,
            "user_prompt": user_prompt,
            "images": images,
        })
        result = self.results.pop(0)
        if isinstance(result, Exception):
            raise result
        return result


class SensitiveVisionClient(FakeVisionClient):
    def __init__(
            self, results: list[str | Exception], *, api_key: str,
            image_base64: str, authorization: str, remote_body: str,
    ) -> None:
        super().__init__(results)
        self.api_key = api_key
        self.image_base64 = image_base64
        self.authorization = authorization
        self.remote_body = remote_body


class ReviewCandidateTest(unittest.TestCase):
    images = (Path("reference.png"), Path("candidate.png"))

    def review(self, client: FakeVisionClient) -> ark_vision_qc.QCReviewResult:
        return ark_vision_qc.review_candidate(
            client,
            system_prompt="strict QC system prompt",
            user_prompt="review candidate against reference",
            images=self.images,
            candidate="candidate.png",
            infographic=False,
        )

    def test_one_valid_confident_review_returns_without_retry(self) -> None:
        client = FakeVisionClient([report_json()])

        result = self.review(client)

        self.assertEqual(result.report.candidate, "candidate.png")
        self.assertEqual(result.review_count, 1)
        self.assertFalse(result.adjudicated)
        self.assertEqual(result.request_count, 1)
        self.assertEqual(len(client.calls), 1)

    def test_invalid_first_output_retries_the_identical_candidate_once(self) -> None:
        client = FakeVisionClient(["not JSON", report_json()])

        result = self.review(client)

        self.assertEqual(result.review_count, 2)
        self.assertFalse(result.adjudicated)
        self.assertEqual(result.request_count, 2)
        self.assertEqual(client.calls[0]["images"], self.images)
        self.assertEqual(client.calls[1]["images"], self.images)
        self.assertEqual(
            client.calls[0]["user_prompt"], client.calls[1]["user_prompt"],
        )

    def test_low_confidence_first_review_retries_without_adjudicating_agreement(self) -> None:
        client = FakeVisionClient([
            report_json(confidence=0.50, decision="accept"),
            report_json(confidence=0.95, decision="accept"),
        ])

        result = self.review(client)

        self.assertEqual(result.report.confidence, 0.95)
        self.assertEqual(result.review_count, 2)
        self.assertFalse(result.adjudicated)
        self.assertEqual(result.request_count, 2)

    def test_two_valid_disagreeing_reports_trigger_one_same_candidate_adjudication(self) -> None:
        state = {"attempts": 1, "status": "qc-pending"}
        paid_generation_calls: list[str] = []
        client = FakeVisionClient([
            report_json(confidence=0.50, decision="accept"),
            report_json(confidence=0.95, decision="reject"),
            report_json(confidence=0.98, decision="reject"),
        ])

        result = self.review(client)

        self.assertEqual(result.report.decision, "reject")
        self.assertEqual(result.review_count, 2)
        self.assertTrue(result.adjudicated)
        self.assertEqual(result.request_count, 3)
        self.assertEqual([call["images"] for call in client.calls], [self.images] * 3)
        self.assertIn("adjudicat", str(client.calls[2]["user_prompt"]).lower())
        self.assertEqual(state, {"attempts": 1, "status": "qc-pending"})
        self.assertEqual(paid_generation_calls, [])

    def test_invalid_report_does_not_count_as_disagreement_for_adjudication(self) -> None:
        client = FakeVisionClient(["not JSON", report_json(decision="reject")])

        result = self.review(client)

        self.assertEqual(result.report.decision, "reject")
        self.assertFalse(result.adjudicated)
        self.assertEqual(result.request_count, 2)

    def test_persistent_invalid_or_low_confidence_results_fail_safely(self) -> None:
        cases = (
            ["not JSON", "still not JSON"],
            [
                report_json(confidence=0.50, decision="accept"),
                report_json(confidence=0.60, decision="accept"),
            ],
            [
                ark_vision_qc.ArkVisionError("safe first failure"),
                ark_vision_qc.ArkVisionError("safe second failure"),
            ],
        )
        for results in cases:
            with self.subTest(results=results):
                client = FakeVisionClient(results)
                with self.assertRaisesRegex(
                        ark_vision_qc.ArkVisionError,
                        "failed after same-candidate review",
                ) as raised:
                    self.review(client)
                self.assertEqual(len(client.calls), 2)
                self.assertNotIn("not JSON", str(raised.exception))
                self.assertNotIn("safe first failure", str(raised.exception))

    def test_wrong_candidate_is_invalid_and_never_adjudicated(self) -> None:
        client = FakeVisionClient([
            report_json(candidate="other.png"),
            report_json(candidate="candidate.png"),
        ])

        result = self.review(client)

        self.assertEqual(result.report.candidate, "candidate.png")
        self.assertFalse(result.adjudicated)
        self.assertEqual(result.request_count, 2)

    def test_persistent_wrong_candidate_reports_bounded_diagnostic_code(self) -> None:
        client = FakeVisionClient([
            report_json(candidate="other-a.png"),
            report_json(candidate="other-b.png"),
        ])

        with self.assertRaisesRegex(
                ark_vision_qc.ArkVisionError, "diagnostic=candidate_mismatch",
        ):
            self.review(client)

    def test_live_percent_confidence_and_string_evidence_are_normalized_once(self) -> None:
        fixture = Path(__file__).parent / "fixtures" / "ark-qc-live-second.json"
        client = FakeVisionClient([fixture.read_text(encoding="utf-8")])

        result = ark_vision_qc.review_candidate(
            client,
            system_prompt="strict QC system prompt",
            user_prompt="review candidate against reference",
            images=self.images,
            candidate="attempt-01-31421aa326f5-01.png",
            infographic=False,
        )

        self.assertEqual(
            result.report.candidate, "attempt-01-31421aa326f5-01.png",
        )
        self.assertEqual(result.report.confidence, 0.92)
        self.assertEqual(result.report.decision, "reject")
        self.assertEqual(result.request_count, 1)
        self.assertEqual(len(client.calls), 1)

    def test_live_first_response_normalizes_structure_but_not_candidate(self) -> None:
        first = json.loads((
            Path(__file__).parent / "fixtures" / "ark-qc-live-first.json"
        ).read_text(encoding="utf-8"))
        second = json.loads(report_json())
        first["candidate"] = "attempt-01-3141aa326f5-01.png"
        second["candidate"] = "candidate.png"
        client = FakeVisionClient([
            json.dumps(first), json.dumps(second),
        ])

        result = self.review(client)

        self.assertEqual(result.report.candidate, "candidate.png")
        self.assertEqual(result.request_count, 2)

    def test_compatibility_layer_does_not_accept_arbitrary_values(self) -> None:
        invalid = json.loads(report_json())
        invalid["evidence"] = {"summary": "visible"}
        invalid["confidence"] = 101
        client = FakeVisionClient([json.dumps(invalid), report_json()])

        result = self.review(client)

        self.assertEqual(result.request_count, 2)
        self.assertEqual(result.report.confidence, 0.94)

    def test_comparative_candidates_use_the_same_bounded_normalization(self) -> None:
        candidate = json.loads(report_json(candidate="candidate_1"))
        candidate["evidence"] = "Visible garment construction."
        candidate["confidence"] = 93
        raw = json.dumps({
            "schema_version": 1,
            "candidates": [candidate],
            "ranking": ["candidate_1"],
            "selected_alias": "candidate_1",
        })

        normalized = json.loads(ark_vision_qc.normalize_comparative_report(raw))

        self.assertEqual(
            normalized["candidates"][0]["evidence"],
            ["Visible garment construction."],
        )
        self.assertEqual(normalized["candidates"][0]["confidence"], 0.93)


class ReviewCandidateTracebackTest(_SanitizedErrorAssertions, unittest.TestCase):
    images = (Path("sensitive-reference.png"), Path("sensitive-candidate.png"))
    api_key = "coordinator-api-key-sentinel"
    system_prompt = "coordinator-system-prompt-sentinel"
    user_prompt = "coordinator-user-prompt-sentinel"
    image_base64 = "Y29vcmRpbmF0b3ItaW1hZ2Utc2VudGluZWw="
    authorization = "Bearer coordinator-authorization-sentinel"
    remote_body = "coordinator-remote-body-sentinel"

    def client(self, results: list[str | Exception]) -> SensitiveVisionClient:
        return SensitiveVisionClient(
            results,
            api_key=self.api_key,
            image_base64=self.image_base64,
            authorization=self.authorization,
            remote_body=self.remote_body,
        )

    def capture_review_error(
            self, client: SensitiveVisionClient,
    ) -> ark_vision_qc.ArkVisionError:
        return self.capture_ark_error(lambda: ark_vision_qc.review_candidate(
            client,
            system_prompt=self.system_prompt,
            user_prompt=self.user_prompt,
            images=self.images,
            candidate="sensitive-candidate.png",
            infographic=False,
        ))

    def assert_coordinator_traceback_is_sanitized(
            self, error: ark_vision_qc.ArkVisionError,
    ) -> None:
        self.assert_exception_is_sanitized(
            error,
            self.api_key,
            self.system_prompt,
            self.user_prompt,
            self.image_base64,
            self.authorization,
            self.remote_body,
        )
        self.assert_traceback_locals_are_sanitized(
            error,
            self.api_key,
            self.system_prompt,
            self.user_prompt,
            self.image_base64,
            self.authorization,
            self.remote_body,
        )

    def test_malformed_reports_leave_no_sensitive_coordinator_traceback_locals(self) -> None:
        client = self.client([
            self.remote_body + " malformed one",
            self.remote_body + " malformed two",
        ])

        error = self.capture_review_error(client)

        self.assert_coordinator_traceback_is_sanitized(error)

    def test_transport_failures_leave_no_sensitive_coordinator_traceback_locals(self) -> None:
        client = self.client([
            ark_vision_qc.ArkVisionError(self.remote_body + " transport one"),
            ark_vision_qc.ArkVisionError(self.remote_body + " transport two"),
        ])

        error = self.capture_review_error(client)

        self.assert_coordinator_traceback_is_sanitized(error)

    def test_persistent_low_confidence_leaves_no_sensitive_coordinator_traceback_locals(self) -> None:
        client = self.client([
            report_json(
                candidate="sensitive-candidate.png", confidence=0.40,
                decision="accept",
            ),
            report_json(
                candidate="sensitive-candidate.png", confidence=0.50,
                decision="accept",
            ),
        ])

        error = self.capture_review_error(client)

        self.assert_coordinator_traceback_is_sanitized(error)

    def test_failed_adjudication_leaves_no_sensitive_coordinator_traceback_locals(self) -> None:
        client = self.client([
            report_json(
                candidate="sensitive-candidate.png", confidence=0.40,
                decision="accept",
            ),
            report_json(
                candidate="sensitive-candidate.png", confidence=0.95,
                decision="reject",
            ),
            self.remote_body + " malformed adjudication",
        ])

        error = self.capture_review_error(client)

        self.assert_coordinator_traceback_is_sanitized(error)


if __name__ == "__main__":
    unittest.main()
