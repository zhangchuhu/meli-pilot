# Seedance 2.5 API contract

Use this reference when changing model parameters, media inputs, polling, or output handling.

## Request

- Base URL: `https://ark.cn-beijing.volces.com/api/v3`
- Create: `POST /contents/generations/tasks`
- Get: `GET /contents/generations/tasks/{id}`
- Authentication: `Authorization: Bearer $ARK_API_KEY`
- Model: `doubao-seedance-2-5-260628`

Minimal create body:

```json
{
  "model": "doubao-seedance-2-5-260628",
  "content": [
    {"type": "text", "text": "prompt"},
    {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64,..."}}
  ],
  "generate_audio": false,
  "ratio": "adaptive",
  "resolution": "720p",
  "duration": 5,
  "watermark": false
}
```

Poll until `status` is terminal. On success, read `content.video_url` and download immediately.

## Validated constraints

- Output duration: 4-30 seconds.
- Output ratio: `21:9`, `16:9`, `4:3`, `1:1`, `3:4`, `9:16`, or `adaptive`.
- Output resolution: 480p, 720p, 1080p, or 4k; 4k is 10-bit.
- Output format: MP4 by default.
- Reference images: 1-30; each is under 30 MB, 300-6000 px per side, aspect ratio 0.4-2.5.
- Reference videos: up to 10, total duration no more than 30 seconds, each under 200 MB.
- Reference audio: up to 10, total duration no more than 30 seconds, each under 15 MB.
- Task records remain queryable for 7 days. Generated video URLs remain available for 24 hours and at most 100 downloads.
- Generation is asynchronous and may return `queued`, `running`, `succeeded`, or `failed`.

Source: the workspace PDFs `火山方舟_视频生成教程_1786693550.pdf` and `火山方舟_Doubao Seedance 2.5 教程_1786693555.pdf`.
