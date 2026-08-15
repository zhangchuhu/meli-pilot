---
name: doubao-imagegen
description: Generate, edit, composite, precisely region-edit, or decompose raster images with the Volcengine Ark Doubao Seedream API. Use when Codex should call Doubao/Seedream for text-to-image, image-to-image, multi-reference fusion, point or bounding-box editing, native transparent PNG editing, image variants, or Seedream 5.0 pro layer decomposition and save the resulting bitmap assets locally.
---

# Doubao Image Generation

Use the bundled `scripts/doubao_imagegen.py` CLI for every live API call. Do not create one-off SDK runners.

## Workflow

1. Classify the request as `generate`, `edit`, or `decompose`.
2. Decide whether each supplied image is an edit target, content reference, style reference, or compositing input.
3. Read `references/prompting.md` and shape the user's request without inventing unsupported creative requirements.
4. For API controls and model constraints, read `references/api.md`.
5. Collect the prompt, exact text, constraints, input images, output size/format, and destination before calling the API.
6. For edits, state invariants explicitly: `change only X; keep Y unchanged`.
7. Run the bundled CLI. Prefer the default model `doubao-seedream-5-0-pro-260628` for high-precision generation and editing.
8. Inspect every saved image with `view_image`; verify subject, composition, text, edit invariants, and transparency where requested.
9. Iterate with one targeted change at a time.
10. Report the saved path(s), final prompt(s), model, and important execution controls.

## Commands

Generate one image:

```bash
python scripts/doubao_imagegen.py generate \
  --prompt "<prompt>" \
  --size 2K \
  --out output/doubao/<name>.png
```

Edit or fuse one or more images. Local files are converted to data URLs automatically; HTTP(S) URLs pass through unchanged:

```bash
python scripts/doubao_imagegen.py edit \
  --prompt "<prompt with image roles and invariants>" \
  --image <path-or-url> [--image <path-or-url> ...] \
  --out output/doubao/<name>.png
```

Precisely edit with normalized coordinates in the prompt:

```text
Replace the object in Image 1 <bbox>120 180 640 760</bbox> with a flower garden.
Keep everything outside the bounding box unchanged.
```

Decompose one image into a base plus up to 16 transparent layers:

```bash
python scripts/doubao_imagegen.py decompose \
  --image <path-or-url> \
  --out-dir output/doubao/layers
```

Generate multiple distinct assets with one API call per JSONL row:

```bash
python scripts/doubao_imagegen.py generate-batch \
  --input prompts.jsonl \
  --out-dir output/doubao/batch
```

Each JSONL object accepts the same request fields as the API (`prompt`, `image`, `size`, `output_format`, and supported advanced fields) plus optional `filename`.

## Operating Rules

- Require `ARK_API_KEY` only for live calls. Never ask the user to paste it into chat; ask them to set it locally.
- Use `--dry-run` to validate and inspect payloads without a key or network access.
- Save final project assets under the workspace. The API's result URLs expire after 24 hours; never leave a deliverable only as a URL.
- Do not overwrite existing files unless the user explicitly requests replacement. Choose a versioned sibling name instead.
- Use one CLI request per distinct image or variant. Use `generate-batch` only for multiple independent prompt jobs.
- Use native transparency only for a single transparent input image with `edit --background transparent --output-format png`; the API does not support this for text-only generation.
- Use `decompose` only with Seedream 5.0 pro and exactly one input image.
- Do not enable watermarking unless the user asks for it.
- Do not claim success until each downloaded or decoded output exists and has been inspected.

## Input Image Semantics

- No input images: `generate`.
- Existing image whose content must change: `edit`.
- Images used only to guide style, composition, or subject: `edit` because the Doubao API supplies all references through `image`, but describe each role clearly in the prompt.
- Multiple images: label them in prompt order as `Image 1`, `Image 2`, and so on.
- Local inputs must be supported raster files and no larger than 30 MB. The CLI rejects unsupported extensions and excessive counts before sending.

## Interactive Editing

Seedream 5.0 pro accepts spatial tags in the prompt. Coordinates are integers in `[0,999]`, with `(0,0)` at top-left and `(999,999)` at bottom-right:

- Point: `<point>x y</point>`
- Box: `<bbox>x1 y1 x2 y2</bbox>`

Convert display pixels using `round(pixel / displayed_dimension * 1000)`, then clamp to `[0,999]`. For a box, normalize both corners and order them left/top/right/bottom. When a region contains multiple objects, also name the intended object. Explicitly mark protected regions as unchanged when useful.

## Output Policy

- Default to PNG, `2K`, standard prompt optimization, opaque background, and no watermark.
- Prefer `1.5K` over `1K` for Seedream 5.0 pro because the documentation states they have equal price and 1.5K generally yields better results.
- Use `fast` prompt optimization only for latency-sensitive drafts; use `standard` for finals.
- For layer decomposition, preserve `manifest.json`; it records `z_index`, bounding boxes, descriptions, and local filenames needed for reconstruction.

## Environment

Set the API key locally:

```bash
export ARK_API_KEY="..."
```

The CLI uses only the Python standard library. No SDK installation is required.
