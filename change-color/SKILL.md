---
name: change-color
description: Extract a representative clothing RGB/HEX color from a user-provided target image, use imagegen to recolor the clothing in every gallery image of a product link's currently selected main SKU while preserving models and scenes, and save only the finished images in a separate folder. Use for Mercado Libre or similar fashion product links paired with a local target-color image.
---

# Change Clothing Color

Accept exactly two inputs:

- `product_link`: product whose current main-SKU gallery must be recolored.
- `target_color_image`: local image whose garment supplies the destination color.

Return the representative color as `RGB(r, g, b)` and `#RRGGBB`, plus a clean folder containing all recolored gallery images.

## Workflow

1. Inspect `target_color_image` and identify broad, well-lit garment interiors. If several garment colors are present and the intended color is ambiguous, ask the user which garment to sample.
2. Run `scripts/extract_target_color.py` with one or more normalized garment sample rectangles. Visually verify its swatch against the target garment.
3. Open `product_link` in the user-requested browser, or the available browser selected for that URL. Identify the currently selected color/main SKU and its gallery count.
4. Collect the highest-resolution URL for every image in that gallery. Exclude recommendations, reviews, videos, variant thumbnails from another color, and unrelated products.
5. Download the originals into a temporary work folder in source order as `source_01`, `source_02`, and so on.
6. Read and follow `$imagegen`. Recolor one source image with its default built-in `image_gen` edit mode and validate it as the calibration result.
7. Process the remaining sources serially with one separate built-in call per image and the same destination RGB/HEX.
8. Compare every result with its corresponding source. Retry only failed images with tighter garment-boundary and preservation instructions.
9. Put only approved images in a new output folder and report the color, count, and absolute folder path.

## Extract the Target Color

Use the bundled script with normalized `left,top,right,bottom` rectangles:

```bash
python scripts/extract_target_color.py target.webp \
  --sample 0.30,0.10,0.64,0.30 \
  --sample 0.32,0.48,0.62,0.82 \
  --swatch /tmp/target-color-swatch.png
```

- Sample both top and bottom for a matching set.
- Exclude skin, hair, background, logos, seams, highlights, and deep folds.
- Prefer several small interior rectangles over one large box crossing garment edges.
- Treat the result as the garment base color. Preserve tonal variation in the output instead of painting flat RGB.

## Recoloring

Use `$imagegen` in its default built-in tool mode. Treat each recolor as a `precise-object-edit`. Do not use its CLI/API fallback unless the user explicitly requests that fallback after being told it requires `OPENAI_API_KEY`.

For every source image:

- Inspect the local gallery image with `view_image` so it is visible in conversation context, then pass it as the edit target.
- Inspect the generated color swatch with `view_image` and label it as a color-only reference. State the extracted RGB and HEX explicitly.
- Change only the garment color. Preserve fabric luminance, folds, seams, texture, highlights, and shadows.
- Preserve the exact model, face, hair, skin, body, hands, pose, accessories, footwear, garment cut and construction, background, text, camera angle, framing, lighting, and composition.
- Do not add, remove, beautify, restyle, or reconstruct anything.
- Invoke the built-in `image_gen` tool exactly once for this source. Do not use one call or an `n` parameter to stand in for multiple distinct gallery edits.
- Inspect the returned image. Copy or move an accepted result from the imagegen default location under `$CODEX_HOME/generated_images/` into the task output folder as `product_XX_recolored.png`. Never leave an accepted project deliverable only under `$CODEX_HOME`.
- Never use one generated result as the next edit target.

Do not invent unsupported built-in tool parameters for destination path, model, quality, dimensions, or masks. Preserve the source aspect ratio and framing through explicit prompt constraints.

For very dark destination colors, map source luminance into a dark tonal range with enough contrast to retain fabric detail; do not fill with solid black.

## Validation

Reject and retry any result where:

- Any original garment color remains at edges, overlaps, pockets, waistbands, or hems.
- The face, body, pose, garment design, scene, text, logo, or framing changes.
- Top and bottom no longer share the intended destination color.
- An image is missing, duplicated, reordered, or has an unintended aspect ratio change.

Informational collages and size charts count as gallery images. Recolor only garment depictions inside them and preserve their text and layout.

## Output Contract

Create a separate folder in the current workspace:

```text
change-color-output-YYYYMMDD-HHMMSS/
  product_01_recolored.png
  product_02_recolored.png
  ...
```

Keep downloads, swatches, previews, and failed attempts outside this folder. Use lossless PNG unless requested otherwise.

Report:

- `RGB(r, g, b)` and `#RRGGBB`
- Final image count versus source gallery count
- Absolute clickable output-folder path
- Any image for which exact preservation cannot be guaranteed

Finalize browser tabs after collecting the gallery resources.

If the built-in image tool fails temporarily, preserve accepted outputs and retry only the failed source once with the same inputs and a more explicit invariant. If the built-in tool is unavailable, explain that imagegen offers a CLI fallback requiring `OPENAI_API_KEY`; proceed only after the user explicitly requests it. Never silently switch to an API or CLI workflow.

## Resource

- `scripts/extract_target_color.py`: Compute a stable median RGB/HEX value from user-selected normalized garment interiors and optionally write a swatch.
