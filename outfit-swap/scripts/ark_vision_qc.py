"""Volcengine Ark transport and same-candidate visual-QC coordination."""

from __future__ import annotations

import base64
import json
import math
import mimetypes
import os
import secrets
import socket
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping, Protocol, Sequence

try:
    from . import payload_security, vision_qc
except ImportError:  # pragma: no cover - direct script-directory import
    import payload_security
    import vision_qc


ARK_CHAT_ENDPOINT = "https://ark.cn-beijing.volces.com/api/v3/chat/completions"
_SUPPORTED_IMAGE_MIME = frozenset({
    "image/gif", "image/jpeg", "image/png", "image/webp",
})
_MAX_RESPONSE_BYTES = 1_000_000
_MINIMUM_CONFIDENCE = 0.85
_DEFAULT_TIMEOUT_SECONDS = 120.0
_TIMEOUT_ENVIRONMENT = "OUTFIT_SWAP_ARK_TIMEOUT_SECONDS"
_QC_DIAGNOSTIC_CODES = frozenset({
    "candidate_mismatch", "evidence_wrong_type",
    "confidence_wrong_scale", "schema_invalid",
})


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


@dataclass(frozen=True)
class _CompletionOutcome:
    content: str | None
    error: str | None


@dataclass(frozen=True)
class _ReviewOutcome:
    result: QCReviewResult | None
    error: str | None


class ArkResponseArchive:
    """Persist exact bounded response bytes with sanitized private metadata."""

    def __init__(self, directory: Path) -> None:
        self._directory = Path(directory).resolve(strict=False)
        self._lock = threading.Lock()
        self._sequence = 0

    def record(
            self, body: bytes, *, http_status: int | None,
            response_headers: Mapping[str, str],
            forbidden_values: Sequence[str] = (),
    ) -> None:
        if not isinstance(body, bytes) or len(body) > _MAX_RESPONSE_BYTES + 1:
            raise ArkVisionError("Ark response archive payload is invalid")
        forbidden = tuple(
            value for value in forbidden_values
            if isinstance(value, str) and value
        )
        if any(value.encode("utf-8") in body for value in forbidden):
            raise ArkVisionError("Ark response contained a request credential")
        sanitized_headers = payload_security.redact_credentials(
            dict(response_headers),
        )
        sanitized_headers = {
            self._redact_exact_values(name, forbidden):
                self._redact_exact_values(value, forbidden)
            for name, value in sanitized_headers.items()
        }
        with self._lock:
            directory_fd = self._prepare_directory()
            self._sequence += 1
            stem = f"{self._sequence:06d}"
            body_name = f"{stem}.body"
            metadata_name = f"{stem}.json"
            metadata = json.dumps({
                "schema_version": 1,
                "captured_at_ns": time.time_ns(),
                "endpoint": ARK_CHAT_ENDPOINT,
                "http_status": http_status,
                "response_headers": sanitized_headers,
                "body_file": body_name,
                "body_bytes": len(body),
            }, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode(
                "utf-8",
            ) + b"\n"
            self._active_directory_fd = directory_fd
            body_committed = False
            try:
                self._atomic_write(self._directory / body_name, body)
                body_committed = True
                self._atomic_write(self._directory / metadata_name, metadata)
            except Exception:
                if body_committed:
                    try:
                        os.unlink(body_name, dir_fd=directory_fd)
                    except OSError:
                        pass
                raise
            finally:
                del self._active_directory_fd
                os.close(directory_fd)

    @staticmethod
    def _redact_exact_values(value: Any, forbidden: Sequence[str]) -> Any:
        if not isinstance(value, str):
            return value
        for secret in forbidden:
            value = value.replace(secret, payload_security.REDACTED)
        return value

    def _prepare_directory(self) -> int:
        descriptor = -1
        try:
            flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
            flags |= getattr(os, "O_NOFOLLOW", 0)
            descriptor = os.open(os.path.sep, flags)
            for component in self._directory.parts[1:]:
                try:
                    os.mkdir(component, mode=0o700, dir_fd=descriptor)
                except FileExistsError:
                    pass
                child = os.open(component, flags, dir_fd=descriptor)
                os.close(descriptor)
                descriptor = child
            os.fchmod(descriptor, 0o700)
            return descriptor
        except OSError:
            if descriptor >= 0:
                os.close(descriptor)
            raise ArkVisionError("Ark response archive is unavailable") from None

    def _atomic_write(self, path: Path, data: bytes) -> None:
        directory_fd = self._active_directory_fd
        descriptor = -1
        temporary = f".{path.name}.{secrets.token_hex(12)}.tmp"
        try:
            flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
            flags |= getattr(os, "O_NOFOLLOW", 0)
            descriptor = os.open(
                temporary, flags, 0o600, dir_fd=directory_fd,
            )
            os.fchmod(descriptor, 0o600)
            with os.fdopen(descriptor, "wb") as handle:
                descriptor = -1
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(
                temporary, path.name,
                src_dir_fd=directory_fd, dst_dir_fd=directory_fd,
            )
        except OSError:
            raise ArkVisionError("Ark response archive is unavailable") from None
        finally:
            if descriptor >= 0:
                os.close(descriptor)
            try:
                os.unlink(temporary, dir_fd=directory_fd)
            except OSError:
                pass


def _nonempty_environment(environ: Mapping[str, str], name: str) -> str:
    value = environ.get(name)
    if not isinstance(value, str) or not value.strip():
        raise ArkVisionError(f"missing required environment variable: {name}")
    return value.strip()


def _timeout_from_environment(environ: Mapping[str, str]) -> float:
    raw = environ.get(_TIMEOUT_ENVIRONMENT)
    if raw is None:
        return _DEFAULT_TIMEOUT_SECONDS
    try:
        value = float(raw.strip()) if isinstance(raw, str) else math.nan
    except ValueError:
        value = math.nan
    if not math.isfinite(value) or value <= 0:
        raise ValueError("Ark timeout must be a positive finite number")
    return value


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
    malformed = False
    payload: Any = None
    try:
        payload = json.loads(
            body.decode("utf-8"),
            object_pairs_hook=_strict_object,
            parse_constant=_reject_json_constant,
        )
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError):
        malformed = True
    if malformed:
        raise ArkVisionError("Ark vision response was not valid JSON")
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


class _RejectRedirects(urllib.request.HTTPRedirectHandler):
    def redirect_request(
            self, original_request: Any, file_pointer: Any, code: int, message: str,
            headers: Any, new_url: str,
    ) -> None:
        return None


def _build_rejecting_opener(*handlers: Any) -> Callable[..., Any]:
    return urllib.request.build_opener(_RejectRedirects(), *handlers).open


def _response_metadata(response: Any) -> tuple[int | None, dict[str, str]]:
    status = getattr(response, "status", None)
    if not isinstance(status, int) or isinstance(status, bool):
        getcode = getattr(response, "getcode", None)
        candidate = getcode() if callable(getcode) else None
        status = (
            candidate
            if isinstance(candidate, int) and not isinstance(candidate, bool)
            else None
        )
    headers = getattr(response, "headers", None)
    if headers is None:
        info = getattr(response, "info", None)
        headers = info() if callable(info) else None
    items = getattr(headers, "items", None)
    if not callable(items):
        return status, {}
    return status, {str(name): str(value) for name, value in items()}


def _complete_json_outcome(
        *, environ: Mapping[str, str], opener: Callable[..., Any],
        timeout_seconds: float, system_prompt: str, user_prompt: str,
        images: Sequence[Path], response_archive: ArkResponseArchive | None,
) -> _CompletionOutcome:
    try:
        api_key = _nonempty_environment(environ, "ARK_API_KEY")
        model = _nonempty_environment(environ, "ARK_VISION_MODEL")
        if not images:
            raise ArkVisionError("at least one image is required for Ark vision QC")
        image_content = [
            {"type": "image_url", "image_url": {"url": _data_url(Path(path))}}
            for path in images
        ]
        payload = {
            "model": model,
            "messages": [
                {"role": "user", "content": [
                    *image_content,
                    {
                        "type": "text",
                        "text": (
                            "Instructions:\n" + system_prompt
                            + "\n\nTask:\n" + user_prompt
                        ),
                    },
                ]},
            ],
            "response_format": {"type": "json_object"},
            "thinking": {"type": "disabled"},
            "stream": False,
        }
        request_body = json.dumps(
            payload, ensure_ascii=False, separators=(",", ":"),
        ).encode("utf-8")
        ark_request = urllib.request.Request(
            ARK_CHAT_ENDPOINT,
            data=request_body,
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        with opener(ark_request, timeout=timeout_seconds) as response:
            if response.geturl() != ARK_CHAT_ENDPOINT:
                return _CompletionOutcome(
                    content=None,
                    error="Ark vision response came from an unapproved endpoint",
                )
            body = response.read(_MAX_RESPONSE_BYTES + 1)
            if response_archive is not None:
                status, headers = _response_metadata(response)
                response_archive.record(
                    body, http_status=status, response_headers=headers,
                    forbidden_values=(api_key,),
                )
        content = _extract_content(body)
        return _CompletionOutcome(content=content, error=None)
    except (TimeoutError, socket.timeout):
        return _CompletionOutcome(
            content=None, error="Ark vision request timed out",
        )
    except urllib.error.HTTPError as exc:
        archive_error: str | None = None
        try:
            body = exc.read(_MAX_RESPONSE_BYTES + 1)
        except Exception:
            body = None
        if (
                body is not None and response_archive is not None
                and exc.geturl() == ARK_CHAT_ENDPOINT
        ):
            try:
                status, headers = _response_metadata(exc)
                response_archive.record(
                    body, http_status=status, response_headers=headers,
                    forbidden_values=(api_key,),
                )
            except ArkVisionError as archive_exception:
                archive_error = str(archive_exception)
        close = getattr(exc, "close", None)
        if callable(close):
            try:
                close()
            except Exception:
                pass
        return _CompletionOutcome(
            content=None,
            error=archive_error or "Ark vision request failed",
        )
    except urllib.error.URLError as exc:
        timed_out = isinstance(exc.reason, (TimeoutError, socket.timeout))
        close = getattr(exc, "close", None)
        if callable(close):
            try:
                close()
            except Exception:
                pass
        return _CompletionOutcome(
            content=None,
            error=(
                "Ark vision request timed out"
                if timed_out else "Ark vision request failed"
            ),
        )
    except ArkVisionError as exc:
        message = exc.args[0] if len(exc.args) == 1 else None
        return _CompletionOutcome(
            content=None,
            error=message if isinstance(message, str) else "Ark vision request failed",
        )
    except Exception:
        return _CompletionOutcome(content=None, error="Ark vision request failed")


class ArkVisionClient:
    def __init__(
            self, *, environ: Mapping[str, str] | None = None,
            opener: Callable[..., Any] | None = None,
            timeout_seconds: float | None = None,
            response_archive_dir: Path | None = None,
    ) -> None:
        resolved_environ = os.environ if environ is None else environ
        if timeout_seconds is None:
            timeout_seconds = _timeout_from_environment(resolved_environ)
        if (not isinstance(timeout_seconds, (int, float))
                or isinstance(timeout_seconds, bool)
                or not math.isfinite(timeout_seconds)
                or timeout_seconds <= 0):
            raise ValueError("timeout_seconds must be a positive finite number")
        self._environ = resolved_environ
        if opener is None and response_archive_dir is None:
            raise ValueError("Ark network client requires a response archive")
        self._opener = _build_rejecting_opener() if opener is None else opener
        self._timeout_seconds = float(timeout_seconds)
        self._response_archive = (
            None if response_archive_dir is None
            else ArkResponseArchive(Path(response_archive_dir))
        )

    def complete_json(
            self, *, system_prompt: str, user_prompt: str,
            images: Sequence[Path],
    ) -> str:
        outcome = _complete_json_outcome(
            environ=self._environ,
            opener=self._opener,
            timeout_seconds=self._timeout_seconds,
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            images=images,
            response_archive=self._response_archive,
        )
        del self, system_prompt, user_prompt, images
        content = outcome.content
        error = outcome.error
        del outcome
        if error is not None:
            raise ArkVisionError(error)
        if content is None:
            raise ArkVisionError("Ark vision response was invalid")
        return content


def _valid_report(
        client: VisionClient, *, system_prompt: str, user_prompt: str,
        images: tuple[Path, ...], candidate: str, infographic: bool,
        diagnostics: list[str] | None = None,
) -> vision_qc.QCReport | None:
    try:
        raw = client.complete_json(
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            images=images,
        )
        report = vision_qc.parse_report(
            _normalize_qc_report(raw), infographic=infographic,
        )
    except ArkVisionError:
        return None
    except vision_qc.VisionQCError as error:
        code = str(error)
        if diagnostics is not None:
            diagnostics.append(
                code if code in _QC_DIAGNOSTIC_CODES else "schema_invalid",
            )
        return None
    if report.candidate != candidate:
        if diagnostics is not None:
            diagnostics.append("candidate_mismatch")
        return None
    return report


def _normalize_qc_report(raw: str) -> str:
    """Normalize only two observed, bounded Ark schema deviations."""
    if not isinstance(raw, str):
        raise vision_qc.VisionQCError("schema_invalid")
    try:
        value = json.loads(
            raw,
            object_pairs_hook=_strict_object,
            parse_constant=_reject_json_constant,
        )
    except (json.JSONDecodeError, TypeError, ValueError):
        raise vision_qc.VisionQCError("schema_invalid") from None
    if not isinstance(value, dict):
        raise vision_qc.VisionQCError("schema_invalid")

    evidence = value.get("evidence")
    if isinstance(evidence, str) and evidence.strip():
        value["evidence"] = [evidence]
    elif not isinstance(evidence, list):
        raise vision_qc.VisionQCError("evidence_wrong_type")

    confidence = value.get("confidence")
    if (isinstance(confidence, (int, float))
            and not isinstance(confidence, bool)
            and math.isfinite(confidence)
            and 1 < confidence <= 100):
        value["confidence"] = confidence / 100
    elif (not isinstance(confidence, (int, float))
          or isinstance(confidence, bool)
          or not math.isfinite(confidence)
          or not 0 <= confidence <= 1):
        raise vision_qc.VisionQCError("confidence_wrong_scale")

    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    )


def normalize_comparative_report(raw: str) -> str:
    """Apply the same bounded compatibility rules to comparative candidates."""
    if not isinstance(raw, str):
        raise vision_qc.VisionQCError("schema_invalid")
    try:
        value = json.loads(
            raw,
            object_pairs_hook=_strict_object,
            parse_constant=_reject_json_constant,
        )
    except (json.JSONDecodeError, TypeError, ValueError):
        raise vision_qc.VisionQCError("schema_invalid") from None
    if not isinstance(value, dict) or not isinstance(value.get("candidates"), list):
        raise vision_qc.VisionQCError("schema_invalid")
    normalized: list[dict[str, Any]] = []
    for candidate in value["candidates"]:
        candidate_raw = json.dumps(
            candidate, ensure_ascii=False, separators=(",", ":"),
        )
        candidate_value = json.loads(_normalize_qc_report(candidate_raw))
        normalized.append(candidate_value)
    value["candidates"] = normalized
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    )


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


def _review_candidate_outcome(
        client: VisionClient, *, system_prompt: str, user_prompt: str,
        images: Sequence[Path], candidate: str, infographic: bool,
) -> _ReviewOutcome:
    adjudicating = False
    diagnostics: list[str] = []
    try:
        same_images = tuple(Path(path) for path in images)
        first = _valid_report(
            client,
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            images=same_images,
            candidate=candidate,
            infographic=infographic,
            diagnostics=diagnostics,
        )
        if first is not None and first.confidence >= _MINIMUM_CONFIDENCE:
            return _ReviewOutcome(
                result=QCReviewResult(
                    report=first, review_count=1, adjudicated=False,
                ),
                error=None,
            )

        second = _valid_report(
            client,
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            images=same_images,
            candidate=candidate,
            infographic=infographic,
            diagnostics=diagnostics,
        )
        if (
                first is not None
                and second is not None
                and first.decision != second.decision
        ):
            adjudicating = True
            adjudicated = _valid_report(
                client,
                system_prompt=system_prompt,
                user_prompt=_adjudication_prompt(user_prompt, first, second),
                images=same_images,
                candidate=candidate,
                infographic=infographic,
                diagnostics=diagnostics,
            )
            if (
                    adjudicated is not None
                    and adjudicated.confidence >= _MINIMUM_CONFIDENCE
            ):
                return _ReviewOutcome(
                    result=QCReviewResult(
                        report=adjudicated, review_count=2, adjudicated=True,
                    ),
                    error=None,
                )
            return _ReviewOutcome(
                result=None,
                error=(
                    "Ark vision QC failed after same-candidate review "
                    "and adjudication"
                    + _diagnostic_suffix(diagnostics)
                ),
            )
        if second is not None and second.confidence >= _MINIMUM_CONFIDENCE:
            return _ReviewOutcome(
                result=QCReviewResult(
                    report=second, review_count=2, adjudicated=False,
                ),
                error=None,
            )
        return _ReviewOutcome(
            result=None,
            error=(
                "Ark vision QC failed after same-candidate review"
                + _diagnostic_suffix(diagnostics)
            ),
        )
    except Exception:
        return _ReviewOutcome(
            result=None,
            error=(
                "Ark vision QC failed after same-candidate review and adjudication"
                if adjudicating
                else "Ark vision QC failed after same-candidate review"
            ),
        )


def _diagnostic_suffix(diagnostics: Sequence[str]) -> str:
    codes = tuple(dict.fromkeys(
        code for code in diagnostics if code in _QC_DIAGNOSTIC_CODES
    ))
    return "" if not codes else " [diagnostic=" + ",".join(codes) + "]"


def review_candidate(
        client: VisionClient, *, system_prompt: str, user_prompt: str,
        images: Sequence[Path], candidate: str, infographic: bool,
) -> QCReviewResult:
    """Review one candidate, retrying only QC and adjudicating valid disagreement."""
    outcome = _review_candidate_outcome(
        client,
        system_prompt=system_prompt,
        user_prompt=user_prompt,
        images=images,
        candidate=candidate,
        infographic=infographic,
    )
    del client, system_prompt, user_prompt, images, candidate, infographic
    result = outcome.result
    error = outcome.error
    del outcome
    if error is not None:
        raise ArkVisionError(error)
    if result is None:
        raise ArkVisionError("Ark vision QC failed after same-candidate review")
    return result
