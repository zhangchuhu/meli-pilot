"""Volcengine Ark transport and same-candidate visual-QC coordination."""

from __future__ import annotations

import base64
import json
import math
import mimetypes
import os
import socket
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping, Protocol, Sequence

try:
    from . import vision_qc
except ImportError:  # pragma: no cover - direct script-directory import
    import vision_qc


ARK_CHAT_ENDPOINT = "https://ark.cn-beijing.volces.com/api/v3/chat/completions"
_SUPPORTED_IMAGE_MIME = frozenset({
    "image/gif", "image/jpeg", "image/png", "image/webp",
})
_MAX_RESPONSE_BYTES = 1_000_000
_MINIMUM_CONFIDENCE = 0.85


class ArkVisionError(RuntimeError):
    """Raised for sanitized Ark transport or QC coordination failures."""


class VisionClient(Protocol):
    def complete_json(
            self, *, system_prompt: str, user_prompt: str,
            images: Sequence[Path],
    ) -> str: ...


@dataclass(frozen=True)
class QCReviewResult:
    report: vision_qc.QCReport
    review_count: int
    adjudicated: bool

    @property
    def request_count(self) -> int:
        return self.review_count + int(self.adjudicated)


def _nonempty_environment(environ: Mapping[str, str], name: str) -> str:
    value = environ.get(name)
    if not isinstance(value, str) or not value.strip():
        raise ArkVisionError(f"missing required environment variable: {name}")
    return value.strip()


def _data_url(path: Path) -> str:
    mime, _encoding = mimetypes.guess_type(path.name, strict=False)
    if mime not in _SUPPORTED_IMAGE_MIME:
        raise ArkVisionError("unsupported Ark vision image type")
    encoded = base64.b64encode(path.read_bytes()).decode("ascii")
    return f"data:{mime};base64,{encoded}"


def _strict_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for name, value in pairs:
        if name in result:
            raise ValueError("duplicate response field")
        result[name] = value
    return result


def _reject_json_constant(_value: str) -> None:
    raise ValueError("non-standard response number")


def _extract_content(body: bytes) -> str:
    if not isinstance(body, bytes) or len(body) > _MAX_RESPONSE_BYTES:
        raise ArkVisionError("Ark vision response was invalid")
    try:
        payload = json.loads(
            body.decode("utf-8"),
            object_pairs_hook=_strict_object,
            parse_constant=_reject_json_constant,
        )
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError):
        raise ArkVisionError("Ark vision response was not valid JSON") from None
    if not isinstance(payload, dict):
        raise ArkVisionError("Ark vision response had an invalid shape")
    choices = payload.get("choices")
    if not isinstance(choices, list) or len(choices) != 1:
        raise ArkVisionError("Ark vision response had an invalid shape")
    choice = choices[0]
    if not isinstance(choice, dict):
        raise ArkVisionError("Ark vision response had an invalid shape")
    finish_reason = choice.get("finish_reason")
    if finish_reason == "content_filter":
        raise ArkVisionError("Ark vision response was blocked by the content filter")
    if finish_reason == "length":
        raise ArkVisionError("Ark vision response was truncated")
    if finish_reason != "stop":
        raise ArkVisionError("Ark vision response did not complete")
    message = choice.get("message")
    if not isinstance(message, dict):
        raise ArkVisionError("Ark vision response had an invalid shape")
    content = message.get("content")
    if not isinstance(content, str) or not content:
        raise ArkVisionError("Ark vision response had no JSON content")
    return content


class ArkVisionClient:
    def __init__(
            self, *, environ: Mapping[str, str] | None = None,
            opener: Callable[..., Any] | None = None,
            timeout_seconds: float = 30.0,
    ) -> None:
        if (not isinstance(timeout_seconds, (int, float))
                or isinstance(timeout_seconds, bool)
                or not math.isfinite(timeout_seconds)
                or timeout_seconds <= 0):
            raise ValueError("timeout_seconds must be a positive finite number")
        self._environ = os.environ if environ is None else environ
        self._opener = urllib.request.urlopen if opener is None else opener
        self._timeout_seconds = float(timeout_seconds)

    def complete_json(
            self, *, system_prompt: str, user_prompt: str,
            images: Sequence[Path],
    ) -> str:
        api_key = _nonempty_environment(self._environ, "ARK_API_KEY")
        model = _nonempty_environment(self._environ, "ARK_VISION_MODEL")
        if not images:
            raise ArkVisionError("at least one image is required for Ark vision QC")
        image_content = [
            {"type": "image_url", "image_url": {"url": _data_url(Path(path))}}
            for path in images
        ]
        payload = {
            "model": model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": [
                    {"type": "text", "text": user_prompt},
                    *image_content,
                ]},
            ],
            "response_format": {"type": "json_object"},
            "stream": False,
        }
        request_body = json.dumps(
            payload, ensure_ascii=False, separators=(",", ":"),
        ).encode("utf-8")
        try:
            request = urllib.request.Request(
                ARK_CHAT_ENDPOINT,
                data=request_body,
                headers={
                    "Authorization": f"Bearer {api_key}",
                    "Content-Type": "application/json",
                },
                method="POST",
            )
            with self._opener(request, timeout=self._timeout_seconds) as response:
                body = response.read(_MAX_RESPONSE_BYTES + 1)
        except (TimeoutError, socket.timeout):
            raise ArkVisionError("Ark vision request timed out") from None
        except urllib.error.URLError as exc:
            if isinstance(exc.reason, (TimeoutError, socket.timeout)):
                raise ArkVisionError("Ark vision request timed out") from None
            raise ArkVisionError("Ark vision request failed") from None
        except Exception:
            raise ArkVisionError("Ark vision request failed") from None
        return _extract_content(body)


def _valid_report(
        client: VisionClient, *, system_prompt: str, user_prompt: str,
        images: tuple[Path, ...], candidate: str, infographic: bool,
) -> vision_qc.QCReport | None:
    try:
        raw = client.complete_json(
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            images=images,
        )
        report = vision_qc.parse_report(raw, infographic=infographic)
    except (ArkVisionError, vision_qc.VisionQCError):
        return None
    if report.candidate != candidate:
        return None
    return report


def _report_summary(report: vision_qc.QCReport) -> dict[str, Any]:
    return {
        "candidate": report.candidate,
        "scores": {
            "garment_construction": report.scores.garment_construction,
            "color_material": report.scores.color_material,
            "garment_details": report.scores.garment_details,
            "target_preservation": report.scores.target_preservation,
            "text_layout": report.scores.text_layout,
        },
        "critical_defects": [defect.value for defect in report.critical_defects],
        "primary_defect": (
            None if report.primary_defect is None else report.primary_defect.value
        ),
        "confidence": report.confidence,
        "decision": report.decision,
    }


def _adjudication_prompt(
        user_prompt: str, first: vision_qc.QCReport,
        second: vision_qc.QCReport,
) -> str:
    reports = json.dumps(
        {"review_one": _report_summary(first), "review_two": _report_summary(second)},
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    return (
        f"{user_prompt}\n\nAdjudicate the two valid but disagreeing QC reports below "
        "against the same candidate images. Return one fresh report using the "
        f"required strict JSON schema. Reports: {reports}"
    )


def review_candidate(
        client: VisionClient, *, system_prompt: str, user_prompt: str,
        images: Sequence[Path], candidate: str, infographic: bool,
) -> QCReviewResult:
    """Review one candidate, retrying only QC and adjudicating valid disagreement."""
    same_images = tuple(Path(path) for path in images)
    first = _valid_report(
        client,
        system_prompt=system_prompt,
        user_prompt=user_prompt,
        images=same_images,
        candidate=candidate,
        infographic=infographic,
    )
    if first is not None and first.confidence >= _MINIMUM_CONFIDENCE:
        return QCReviewResult(report=first, review_count=1, adjudicated=False)

    second = _valid_report(
        client,
        system_prompt=system_prompt,
        user_prompt=user_prompt,
        images=same_images,
        candidate=candidate,
        infographic=infographic,
    )
    if first is not None and second is not None and first.decision != second.decision:
        adjudicated = _valid_report(
            client,
            system_prompt=system_prompt,
            user_prompt=_adjudication_prompt(user_prompt, first, second),
            images=same_images,
            candidate=candidate,
            infographic=infographic,
        )
        if adjudicated is not None and adjudicated.confidence >= _MINIMUM_CONFIDENCE:
            return QCReviewResult(
                report=adjudicated, review_count=2, adjudicated=True,
            )
        raise ArkVisionError(
            "Ark vision QC failed after same-candidate review and adjudication",
        )
    if second is not None and second.confidence >= _MINIMUM_CONFIDENCE:
        return QCReviewResult(report=second, review_count=2, adjudicated=False)
    raise ArkVisionError("Ark vision QC failed after same-candidate review")
