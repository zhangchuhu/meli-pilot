import base64
import io
import json
import sys
import tempfile
import traceback
import unittest
import urllib.response
from email.message import Message
from pathlib import Path
from typing import Any


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


class ArkVisionClientTest(unittest.TestCase):
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
        self.assertIs(payload["stream"], False)
        self.assertEqual(payload["messages"][0], {
            "role": "system", "content": "Return strict JSON.",
        })
        user_content = payload["messages"][1]["content"]
        self.assertEqual(user_content[0], {
            "type": "text", "text": "Review this candidate.",
        })
        self.assertEqual(user_content[1], {
            "type": "image_url",
            "image_url": {
                "url": "data:image/png;base64," + base64.b64encode(b"png-bytes").decode("ascii"),
            },
        })
        self.assertEqual(user_content[2], {
            "type": "image_url",
            "image_url": {
                "url": "data:image/jpeg;base64," + base64.b64encode(b"jpeg-bytes").decode("ascii"),
            },
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
        client = ark_vision_qc.ArkVisionClient(
            environ={"ARK_API_KEY": "key", "ARK_VISION_MODEL": "model"},
            opener=RecordingOpener(
                f'{{"choices":["{remote_sentinel}"'.encode("utf-8"),
            ),
        )
        with tempfile.TemporaryDirectory() as directory:
            image = Path(directory) / "candidate.png"
            image.write_bytes(b"image")
            with self.assertRaises(ark_vision_qc.ArkVisionError) as raised:
                client.complete_json(
                    system_prompt="system", user_prompt="user", images=(image,),
                )

        self.assert_exception_is_sanitized(raised.exception, remote_sentinel)

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


if __name__ == "__main__":
    unittest.main()
