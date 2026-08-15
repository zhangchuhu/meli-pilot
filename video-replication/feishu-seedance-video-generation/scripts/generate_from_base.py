#!/usr/bin/env python3
"""Generate Seedance 2.5 videos from storyboard records in a Lark Base."""

from __future__ import annotations

import argparse
import base64
import json
import math
import mimetypes
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Iterable


DEFAULT_MODEL = "doubao-seedance-2-5-260628"
DEFAULT_BASE_URL = "https://ark.cn-beijing.volces.com/api/v3"
CREATE_PATH = "/contents/generations/tasks"
OUTPUT_FIELDS = [
    {"name": "Seedance任务ID", "type": "text"},
    {"name": "Seedance状态", "type": "text"},
    {"name": "Seedance视频URL", "type": "text"},
    {"name": "Seedance错误", "type": "text"},
    {"name": "Seedance生成视频", "type": "attachment"},
]
TERMINAL_STATUSES = {"succeeded", "failed", "cancelled", "expired"}


class WorkflowError(RuntimeError):
    pass


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Read storyboard rows from Lark Base and generate Seedance 2.5 videos."
    )
    parser.add_argument("--base-url", required=True, help="Lark Base URL with table query")
    parser.add_argument("--as", dest="identity", choices=("user", "bot"), default="user")
    parser.add_argument("--record-id", action="append", default=[], help="Only process this record; repeatable")
    parser.add_argument("--max-records", type=int, default=0, help="Maximum rows to process; 0 means all")
    parser.add_argument("--prompt-field", default="倒推生成视频提示词")
    parser.add_argument("--duration-field", default="时长")
    parser.add_argument("--image-field", default="镜头截图")
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument("--ark-base-url", default=DEFAULT_BASE_URL)
    parser.add_argument("--ratio", default="adaptive", choices=("adaptive", "21:9", "16:9", "4:3", "1:1", "3:4", "9:16"))
    parser.add_argument("--resolution", default="720p", choices=("480p", "720p", "1080p", "4k"))
    parser.add_argument("--default-duration", type=int, default=5)
    parser.add_argument("--duration-policy", choices=("clamp", "reject", "default"), default="clamp")
    parser.add_argument("--generate-audio", action="store_true")
    parser.add_argument("--watermark", action="store_true")
    parser.add_argument("--poll-interval", type=float, default=10.0)
    parser.add_argument("--timeout", type=float, default=1800.0)
    parser.add_argument("--output-dir", default="seedance_output", help="Relative directory below cwd")
    parser.add_argument("--ensure-output-fields", action="store_true", help="Create missing output fields")
    parser.add_argument("--submit-only", action="store_true", help="Create tasks and write task IDs without polling")
    parser.add_argument("--dry-run", action="store_true", help="Read Base and print a plan; do not call Ark or write Base")
    parser.add_argument("--yes", action="store_true", help="Confirm paid generation and Base writes")
    return parser.parse_args(argv)


def require_relative(path: Path) -> Path:
    if path.is_absolute() or ".." in path.parts:
        raise WorkflowError(f"Path must be relative to cwd and cannot contain '..': {path}")
    return path


def run_lark(args: list[str]) -> dict[str, Any]:
    command = ["lark-cli", *args]
    proc = subprocess.run(command, text=True, capture_output=True, check=False)
    raw = proc.stdout.strip() or proc.stderr.strip()
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise WorkflowError(f"lark-cli returned non-JSON output (exit {proc.returncode}): {raw[:500]}") from exc
    if proc.returncode != 0 or payload.get("ok") is False:
        error = payload.get("error", payload)
        raise WorkflowError(f"lark-cli failed: {json.dumps(error, ensure_ascii=False)}")
    return payload


def envelope_data(payload: dict[str, Any]) -> dict[str, Any]:
    data = payload.get("data")
    return data if isinstance(data, dict) else payload


def resolve_base(base_url: str, identity: str) -> tuple[str, str]:
    result = envelope_data(run_lark(["base", "+url-resolve", "--url", base_url, "--as", identity]))
    base_token = result.get("base_token")
    table_id = result.get("table_id") or (result.get("block_id") if result.get("block_type") == "table" else None)
    if not base_token or not table_id:
        raise WorkflowError("The URL did not resolve to a Base table; include ?table=<table_id> in the URL")
    return str(base_token), str(table_id)


def list_fields(base_token: str, table_id: str, identity: str) -> list[dict[str, Any]]:
    result = envelope_data(run_lark([
        "base", "+field-list", "--base-token", base_token, "--table-id", table_id, "--as", identity
    ]))
    fields = result.get("fields") or result.get("items") or []
    if not isinstance(fields, list):
        raise WorkflowError("Unexpected field-list response")
    return fields


def ensure_fields(
    fields: list[dict[str, Any]], base_token: str, table_id: str, identity: str, create: bool
) -> list[dict[str, Any]]:
    names = {str(field.get("name")) for field in fields}
    missing = [field for field in OUTPUT_FIELDS if field["name"] not in names]
    if not missing:
        return fields
    if not create:
        missing_names = ", ".join(field["name"] for field in missing)
        raise WorkflowError(f"Missing output fields: {missing_names}. Re-run with --ensure-output-fields after approval.")
    run_lark([
        "base", "+field-create", "--base-token", base_token, "--table-id", table_id,
        "--json", json.dumps(missing, ensure_ascii=False), "--as", identity,
    ])
    return list_fields(base_token, table_id, identity)


def export_records(
    base_token: str,
    table_id: str,
    identity: str,
    field_names: Iterable[str],
    output_file: Path,
) -> list[dict[str, Any]]:
    output_file.parent.mkdir(parents=True, exist_ok=True)
    args = ["base", "+record-list", "--base-token", base_token, "--table-id", table_id]
    for field_name in field_names:
        args.extend(["--field-id", field_name])
    args.extend(["--format", "ndjson", "--output", str(output_file), "--overwrite", "--as", identity])
    manifest = run_lark(args)
    if manifest.get("has_more"):
        raise WorkflowError("Record export exceeded 2000 rows; filter the Base or process explicit --record-id values")
    records: list[dict[str, Any]] = []
    with output_file.open("r", encoding="utf-8") as stream:
        for line in stream:
            if line.strip():
                records.append(json.loads(line))
    return records


def normalized_duration(value: Any, default: int, policy: str) -> int:
    try:
        duration = int(math.ceil(float(value))) if value is not None else default
    except (TypeError, ValueError):
        duration = default
    if 4 <= duration <= 30:
        return duration
    if policy == "reject":
        raise WorkflowError(f"Duration {duration}s is outside Seedance 2.5 output range 4-30s")
    if policy == "default":
        if not 4 <= default <= 30:
            raise WorkflowError("--default-duration must be within 4-30 seconds")
        return default
    return max(4, min(30, duration))


def effective_prompt(prompt: str, source_duration: Any, output_duration: int) -> str:
    try:
        source = float(source_duration)
    except (TypeError, ValueError):
        source = None
    if source is None:
        return f"{prompt}\n输出视频总时长为 {output_duration} 秒。"
    if source < output_duration:
        return f"{prompt}\n输出视频总时长为 {output_duration} 秒；完成上述动作后保持结尾停帧至结束。"
    if source > output_duration:
        return f"{prompt}\n将上述时间轴等比例压缩到 {output_duration} 秒内完成。"
    return prompt


def completed_record(record: dict[str, Any]) -> bool:
    status = str(record.get("Seedance状态") or "").lower()
    attachments = record.get("Seedance生成视频") or []
    return status == "succeeded" and bool(attachments)


def resumable_task_id(record: dict[str, Any]) -> str:
    status = str(record.get("Seedance状态") or "").lower()
    task_id = str(record.get("Seedance任务ID") or "")
    return task_id if task_id and status not in {"failed", "cancelled", "expired"} else ""


def download_record_images(
    record: dict[str, Any],
    image_field: str,
    base_token: str,
    table_id: str,
    identity: str,
    output_dir: Path,
) -> list[Path]:
    attachments = record.get(image_field) or []
    if not attachments:
        return []
    record_dir = output_dir / "inputs" / str(record["record_id"])
    record_dir.mkdir(parents=True, exist_ok=True)
    paths: list[Path] = []
    for index, attachment in enumerate(attachments, 1):
        token = attachment.get("file_token")
        name = Path(str(attachment.get("name") or f"reference_{index}.jpg")).name
        if not token:
            continue
        target = record_dir / name
        run_lark([
            "base", "+record-download-attachment", "--base-token", base_token,
            "--table-id", table_id, "--record-id", str(record["record_id"]),
            "--file-token", str(token), "--output", str(target), "--overwrite", "--as", identity,
        ])
        paths.append(target)
    return paths


def image_data_url(path: Path) -> str:
    mime = mimetypes.guess_type(path.name)[0] or "image/jpeg"
    encoded = base64.b64encode(path.read_bytes()).decode("ascii")
    return f"data:{mime};base64,{encoded}"


def build_payload(
    prompt: str,
    duration: int,
    images: list[Path],
    model: str,
    ratio: str,
    resolution: str,
    generate_audio: bool,
    watermark: bool,
) -> dict[str, Any]:
    content: list[dict[str, Any]] = [{"type": "text", "text": prompt}]
    content.extend({"type": "image_url", "image_url": {"url": image_data_url(path)}} for path in images)
    return {
        "model": model,
        "content": content,
        "generate_audio": generate_audio,
        "ratio": ratio,
        "resolution": resolution,
        "duration": duration,
        "watermark": watermark,
    }


def ark_request(method: str, url: str, api_key: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8") if payload is not None else None
    request = urllib.request.Request(
        url,
        data=data,
        method=method,
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
    )
    for attempt in range(5):
        try:
            with urllib.request.urlopen(request, timeout=120) as response:
                return json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")
            if exc.code == 429 or 500 <= exc.code < 600:
                if attempt < 4:
                    time.sleep(min(2 ** attempt, 10))
                    continue
            raise WorkflowError(f"Ark API HTTP {exc.code}: {body[:1000]}") from exc
        except urllib.error.URLError as exc:
            if attempt < 4:
                time.sleep(min(2 ** attempt, 10))
                continue
            raise WorkflowError(f"Ark API connection failed: {exc.reason}") from exc
    raise WorkflowError("Ark API request exhausted retries")


def poll_task(base_url: str, api_key: str, task_id: str, interval: float, timeout: float) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    while True:
        task = ark_request("GET", f"{base_url.rstrip('/')}{CREATE_PATH}/{task_id}", api_key)
        status = str(task.get("status", "unknown")).lower()
        if status in TERMINAL_STATUSES:
            return task
        if time.monotonic() >= deadline:
            raise WorkflowError(f"Timed out waiting for task {task_id}; last status: {status}")
        time.sleep(interval)


def download_video(url: str, path: Path) -> None:
    request = urllib.request.Request(url, headers={"User-Agent": "seedance-base-skill/1.0"})
    with urllib.request.urlopen(request, timeout=300) as response, path.open("wb") as stream:
        while True:
            chunk = response.read(1024 * 1024)
            if not chunk:
                break
            stream.write(chunk)


def update_record(
    base_token: str, table_id: str, record_id: str, identity: str, fields: dict[str, Any]
) -> None:
    body = {"update_records": {record_id: fields}}
    run_lark([
        "base", "+record-batch-update", "--base-token", base_token, "--table-id", table_id,
        "--json", json.dumps(body, ensure_ascii=False), "--as", identity,
    ])


def upload_video(
    base_token: str,
    table_id: str,
    record_id: str,
    identity: str,
    video: Path,
    existing_attachments: list[dict[str, Any]],
) -> None:
    if any(str(item.get("name")) == video.name for item in existing_attachments):
        return
    run_lark([
        "base", "+record-upload-attachment", "--base-token", base_token,
        "--table-id", table_id, "--record-id", record_id,
        "--field-id", "Seedance生成视频", "--file", str(video), "--as", identity,
    ])


def error_text(value: Any) -> str:
    if value is None:
        return "未知错误"
    if isinstance(value, str):
        return value[:2000]
    return json.dumps(value, ensure_ascii=False)[:2000]


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    output_dir = require_relative(Path(args.output_dir))
    output_dir.mkdir(parents=True, exist_ok=True)
    if not args.dry_run and not args.yes:
        raise WorkflowError("Paid generation and Base writes require explicit --yes; use --dry-run first")
    api_key = os.environ.get("ARK_API_KEY", "")
    if not args.dry_run and not api_key:
        raise WorkflowError("ARK_API_KEY is not set")

    base_token, table_id = resolve_base(args.base_url, args.identity)
    fields = list_fields(base_token, table_id, args.identity)
    field_names = {str(field.get("name")) for field in fields}
    for required in (args.prompt_field, args.duration_field, args.image_field):
        if required not in field_names:
            raise WorkflowError(f"Required Base field not found: {required}")
    if not args.dry_run:
        fields = ensure_fields(fields, base_token, table_id, args.identity, args.ensure_output_fields)
        field_names = {str(field.get("name")) for field in fields}

    requested_fields = [
        args.prompt_field,
        args.duration_field,
        args.image_field,
        "Seedance状态",
        "Seedance任务ID",
        "Seedance生成视频",
    ]
    requested_fields = [name for name in requested_fields if name in field_names]
    records = export_records(
        base_token, table_id, args.identity, requested_fields,
        output_dir / "base_records.ndjson",
    )
    selected_ids = set(args.record_id)
    prompted = [record for record in records if record.get(args.prompt_field)]
    if selected_ids:
        prompted = [record for record in prompted if record.get("record_id") in selected_ids]
        found_ids = {str(record.get("record_id")) for record in prompted}
        missing_ids = sorted(selected_ids - found_ids)
        if missing_ids:
            raise WorkflowError(f"Requested records were not found or have no prompt: {', '.join(missing_ids)}")
    eligible = [record for record in prompted if not completed_record(record)]
    if args.max_records > 0:
        eligible = eligible[: args.max_records]

    plan: list[dict[str, Any]] = []
    for record in eligible:
        duration = normalized_duration(record.get(args.duration_field), args.default_duration, args.duration_policy)
        prompt = str(record[args.prompt_field])
        adjusted_prompt = effective_prompt(prompt, record.get(args.duration_field), duration)
        plan.append({
            "record_id": record["record_id"],
            "duration": duration,
            "source_duration": record.get(args.duration_field),
            "reference_images": len(record.get(args.image_field) or []),
            "prompt": prompt,
            "effective_prompt": adjusted_prompt,
            "prompt_adjusted": adjusted_prompt != prompt,
            "resume_task_id": resumable_task_id(record) or None,
            "existing_output_attachments": record.get("Seedance生成视频") or [],
        })
    print(json.dumps({"base_token": base_token, "table_id": table_id, "records": plan}, ensure_ascii=False, indent=2))
    if args.dry_run:
        return 0
    if not plan:
        print("No eligible records found.", file=sys.stderr)
        return 0

    failures = 0
    successes = 0
    submitted = 0
    for item, record in zip(plan, eligible):
        record_id = str(item["record_id"])
        try:
            task_id = str(item.get("resume_task_id") or "")
            if not task_id:
                images = download_record_images(
                    record, args.image_field, base_token, table_id, args.identity, output_dir
                )
                payload = build_payload(
                    str(item["effective_prompt"]), int(item["duration"]), images,
                    args.model, args.ratio, args.resolution, args.generate_audio, args.watermark,
                )
                created = ark_request("POST", f"{args.ark_base_url.rstrip('/')}{CREATE_PATH}", api_key, payload)
                task_id = str(created.get("id") or "")
                if not task_id:
                    raise WorkflowError(f"Ark create response did not contain task id: {created}")
                update_record(base_token, table_id, record_id, args.identity, {
                    "Seedance任务ID": task_id, "Seedance状态": "running", "Seedance错误": ""
                })
                submitted += 1
            if args.submit_only:
                continue
            result = poll_task(args.ark_base_url, api_key, task_id, args.poll_interval, args.timeout)
            status = str(result.get("status", "unknown")).lower()
            if status != "succeeded":
                message = error_text(result.get("error"))
                update_record(base_token, table_id, record_id, args.identity, {
                    "Seedance状态": status, "Seedance错误": message
                })
                failures += 1
                continue
            video_url = str((result.get("content") or {}).get("video_url") or "")
            if not video_url:
                raise WorkflowError("Succeeded task has no content.video_url")
            video_path = output_dir / f"{record_id}_{task_id}.mp4"
            download_video(video_url, video_path)
            upload_video(
                base_token,
                table_id,
                record_id,
                args.identity,
                video_path,
                list(item.get("existing_output_attachments") or []),
            )
            update_record(base_token, table_id, record_id, args.identity, {
                "Seedance状态": "succeeded", "Seedance视频URL": video_url, "Seedance错误": ""
            })
            successes += 1
        except Exception as exc:  # Keep processing independent storyboard rows.
            failures += 1
            try:
                update_record(base_token, table_id, record_id, args.identity, {
                    "Seedance状态": "failed", "Seedance错误": error_text(str(exc))
                })
            except Exception:
                pass
            print(f"Record {record_id} failed: {exc}", file=sys.stderr)
    print(json.dumps({
        "processed": len(plan),
        "succeeded": successes,
        "submitted": submitted,
        "failed": failures,
        "output_dir": str(output_dir),
    }, ensure_ascii=False, indent=2))
    return 1 if failures else 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except WorkflowError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        raise SystemExit(2)
