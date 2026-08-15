# Lark Base field contract

Use this reference when adapting the skill to a new storyboard table.

## Required input fields

| Default field | Type | Meaning |
|---|---|---|
| `倒推生成视频提示词` | text | Seedance prompt for one shot |
| `时长` | number | Desired seconds; normalized to Seedance's 4-30 second range |
| `镜头截图` | attachment | One or more reference images |

Override these names with `--prompt-field`, `--duration-field`, and `--image-field`.

## Output fields

The script can create these fields with `--ensure-output-fields`:

| Field | Type | Meaning |
|---|---|---|
| `Seedance任务ID` | text | Ark asynchronous task ID |
| `Seedance状态` | text | `running`, `succeeded`, `failed`, etc. |
| `Seedance视频URL` | text | Temporary Ark download URL |
| `Seedance错误` | text | Bounded error details |
| `Seedance生成视频` | attachment | Persisted MP4 uploaded back to Base |

Always use `lark-cli base +record-download-attachment` and `+record-upload-attachment` for Base attachments. Do not treat attachment tokens as ordinary Drive tokens or write attachment cells with batch update.

The bundled script resolves Base URLs, reads fields, exports records to NDJSON, and uses `record_id` as the stable row key. It passes `--as user` by default and keeps that identity for every subsequent command.
