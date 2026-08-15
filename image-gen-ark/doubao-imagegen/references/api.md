# Doubao Seedream API Reference

This reference is distilled from the supplied Volcengine Ark PDFs dated August 2026.

## Contents

- [Endpoint and authentication](#endpoint-and-authentication)
- [Core request](#core-request)
- [Seedream 5.0 pro constraints](#seedream-50-pro-constraints)
- [Image inputs](#image-inputs)
- [Transparency](#transparency)
- [Layer decomposition](#layer-decomposition)
- [Responses and persistence](#responses-and-persistence)

## Endpoint and authentication

- Endpoint: `POST https://ark.cn-beijing.volces.com/api/v3/images/generations`
- Headers: `Content-Type: application/json` and `Authorization: Bearer $ARK_API_KEY`
- Default model used by this skill: `doubao-seedream-5-0-pro-260628`

## Core request

```json
{
  "model": "doubao-seedream-5-0-pro-260628",
  "prompt": "...",
  "image": "data:image/png;base64,...",
  "size": "2K",
  "output_format": "png",
  "response_format": "url",
  "background": "opaque",
  "watermark": false,
  "optimize_prompt_options": {"mode": "standard"}
}
```

`image` may be one string or an array. Omit it for text-to-image. `response_format` is `url` or `b64_json`; the bundled CLI uses `url` by default and immediately downloads every result.

## Seedream 5.0 pro constraints

- Resolution tiers: `1K`, `1.5K`, `2K`; default `2K`.
- Explicit `WIDTHxHEIGHT`: total pixels from 921,600 through 4,624,220 inclusive; aspect ratio from 1:16 through 16:1.
- Common 2K mappings: 1:1 `2048x2048`, 4:3 `2368x1776`, 3:4 `1776x2368`, 16:9 `2816x1584`, 9:16 `1584x2816`, 3:2 `2496x1664`, 2:3 `1664x2496`, 21:9 `3136x1344`.
- Output format: `png` or `jpeg`.
- Prompt optimization: `standard` (higher quality, slower) or `fast` (lower latency).
- Seedream 5.0 pro does not support streaming or sequential multi-image output. Create variants as separate requests.

## Image inputs

- Accepted for normal image generation/editing: JPEG, PNG, WebP, BMP, TIFF, GIF, HEIC, HEIF.
- Maximum 10 reference images, 30 MB each.
- Each image: total pixels `[196, 36,000,000]`, both sides greater than 14 px, aspect ratio `[1/16, 16]`.
- Supply an accessible HTTP(S) URL or a lowercase MIME data URL: `data:image/png;base64,...`.

## Transparency

`background=transparent` has strict limits:

- image-to-image only;
- exactly one input image;
- input must already contain an alpha channel and use a transparency-capable format;
- output must be PNG; JPEG causes an error.

Use `background=opaque` for all other requests.

## Layer decomposition

Set `layer_decomposition=true` with Seedream 5.0 pro.

- Exactly one PNG or JPEG input is required.
- Input total pixels: 262,144 through 36,000,000; aspect ratio `[1/16,16]`; max 30 MB.
- `prompt` is optional. Omit it for automatic decomposition, describe elements in natural language, or identify them with `<bbox>` tags.
- `size`: `auto`, `1K`, `1.5K`, or `2K`; default `auto` is best for preserving the source scale.
- Response contains a base (`z_index=0`) and up to 16 transparent PNG layers.
- Each layer may include `bounding_box.absolute`, `bounding_box.normalized`, `name`, and `description`.
- Reconstruct by sorting ascending `z_index`; place layers using absolute coordinates for the original canvas or normalized coordinates for a resized canvas.

Normalized placement on canvas `W x H`:

```text
x = left / 1000 * W
y = top / 1000 * H
w = (right - left) / 1000 * W
h = (bottom - top) / 1000 * H
```

## Responses and persistence

Typical response:

```json
{
  "model": "doubao-seedream-5-0-pro-260628",
  "created": 1784696685,
  "data": [{"url": "https://...", "size": "2048x2048", "output_format": "png"}],
  "usage": {"input_images": 0, "generated_images": 1, "total_tokens": 1234}
}
```

Result URLs expire after 24 hours. Download immediately. For `b64_json`, decode immediately. Treat the number of returned items as dynamic, especially for layer decomposition.
