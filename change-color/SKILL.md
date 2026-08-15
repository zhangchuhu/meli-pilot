---
name: change-color
description: Extract a representative clothing RGB/HEX color from a user-provided target image, use the ImageEye Chrome extension to download and filter the product page's currently selected main-SKU gallery, use imagegen to recolor only those verified gallery images while preserving models and scenes, and save only the finished images in a separate folder. Use for Mercado Libre or similar fashion product links paired with a local target-color image.
---

# Change Clothing Color

Accept exactly two inputs:

- `product_link`: product whose current main-SKU gallery must be recolored.
- `target_color_image`: local image whose garment supplies the destination color.

Return the representative color as `RGB(r, g, b)` and `#RRGGBB`, plus a clean folder containing all recolored gallery images.

## Workflow

1. Inspect `target_color_image` and identify broad, well-lit garment interiors. If several garment colors are present and the intended color is ambiguous, ask the user which garment to sample.
2. Run `scripts/extract_target_color.py` with one or more normalized garment sample rectangles. Visually verify its swatch against the target garment.
3. Open `product_link` in Chrome and identify the currently selected color/main SKU, garment construction, and visible gallery order. Do not switch variants.
4. Use the installed ImageEye Chrome extension to collect and download page-image candidates into a temporary candidate folder. Follow **ImageEye Gallery Acquisition** below; do not substitute raw page-assets inventory, search results, an API, or guessed CDN URLs.
5. Filter the candidates against the visible product gallery. Keep only verified images of the currently selected main SKU, then copy them in gallery order into the work folder as `source_01`, `source_02`, and so on. Record a keep/exclude manifest before generation.
6. Read and follow `$imagegen`. Recolor one source image with its default built-in `image_gen` edit mode and validate it as the calibration result.
7. Process the remaining sources serially with one separate built-in call per image and the same destination RGB/HEX.
8. Compare every successfully generated result with its corresponding source. If generation succeeded but visual validation fails, retry that image only with tighter garment-boundary and preservation instructions. If the `image_gen` call itself fails, stop immediately and return the error as described under Failure Handling.
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

## ImageEye Gallery Acquisition

ImageEye in Chrome is the required acquisition surface for product images.

1. Confirm Chrome displays the requested product ID and the intended currently selected color/main SKU. Expand or step through the product gallery so every gallery image has been rendered at least once.
2. Open ImageEye from the Chrome extensions toolbar on that product tab. If ImageEye is missing, disabled, cannot inspect the tab, or Chrome asks for unapproved extension access, stop before generation and report the exact blocker. Do not install an extension or grant new permissions without user confirmation.
3. In ImageEye, refresh or rescan after the gallery has been rendered. Before selecting or downloading anything, set all three filters exactly as follows:
   - `Size = Large`
   - `Layout = Tall` (the ImageEye label corresponding to the user's `layout=tail` rule)
   - `Type = WEBP`
4. Verify the ImageEye result view shows all three active filters. If any filter is unavailable or cannot be confirmed, stop before generation and report which filter failed. Do not silently broaden the filter set.
5. Select candidates only from the filtered result view. Prefer the highest-resolution copy when duplicates remain. Download candidates into `change-color-work-YYYYMMDD-HHMMSS/imageeye-candidates/`; never download directly into the final output folder.
6. Treat the filtered ImageEye download as an untrusted candidate pool, not as the gallery itself. These filters reduce noise but do not prove SKU membership; results may still include recommendations, ads, reviews, variant images, and unrelated products.
7. Inspect every candidate with `view_image` and compare it with the visible product gallery. Create `gallery-manifest.tsv` with `gallery_index`, `candidate_filename`, `decision`, and `reason`.

Keep a candidate only when all of these are true:

- It depicts the same garment type, cut, construction, and component set as the product's currently selected main SKU.
- Its garment color or print matches the currently selected variant. Exclude different-color variant thumbnails even when construction matches.
- It appears in the product's own gallery in the same visible order. A visually similar recommendation is not enough.
- It is a full gallery asset or an informational collage or size chart belonging to that SKU, not a thumbnail, icon, review image, video poster, ad, or recommendation.
- It is sufficiently large and clear for editing. For duplicate resolutions of the same image, keep only the highest-resolution copy.
- Its downloaded file is WEBP and it came from the simultaneously active ImageEye filters `Large + Tall + WEBP`.

Exclude and record the reason for every candidate that fails any rule. Do not accept JPG, PNG, SVG, square, landscape, small, or medium ImageEye results outside the required filter set. Deduplicate by visual content, not filename alone. Compare retained membership with the product's visible tall WEBP gallery assets; do not require excluded videos or non-tall/non-WEBP gallery assets to appear in this filtered batch. If membership cannot be reconciled confidently, stop before `image_gen`, preserve the candidate folder and manifest, and report the mismatch. Never fill gaps with recommendations, other colors, similar products, search images, or inferred CDN variants.

Copy only retained candidates to the work folder in visible gallery order as `source_01`, `source_02`, and so on. Finalize the Chrome product and ImageEye pages after the gallery resources and manifest are safely local.

## Recoloring

Use `$imagegen` in its default built-in tool mode. Treat each recolor as a `precise-object-edit`. This skill has no fallback image generator: never invoke `$api-image`, an image API, the imagegen CLI, or another raster-editing tool when built-in `image_gen` fails.

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

Finalize Chrome product and ImageEye pages after collecting and verifying the gallery resources.

## Failure Handling

Distinguish an unsuccessful tool call from a successful image that fails visual quality control:

- If Chrome or ImageEye cannot confirm the simultaneous filters `Size=Large`, `Layout=Tall`, and `Type=WEBP`, or cannot produce a confidently verified current-main-SKU gallery from those filtered results, stop before generation. Report active filters, candidate count, retained count, expected eligible gallery count when known, and the exact blocker or mismatch. Do not call `image_gen`.

- If built-in `image_gen` returns an error, times out, is unavailable, or produces no usable output, stop the generation workflow immediately. Do not retry that call and do not process later source images.
- Never call `$api-image`, an image API, the imagegen CLI, or any other image generator as a fallback, even if credentials or those tools are available.
- Preserve already accepted local outputs, clean up browser pages, and report that the batch is incomplete.
- Return the exact affected source filename/index and the provider/tool error message. Do not replace the error with a guessed diagnosis and do not claim completion.
- A retry is allowed only when `image_gen` successfully returned an image but that image fails the visual validation rules above. Such a retry must still use built-in `image_gen` and must change only the relevant preservation or garment-boundary instruction.

## Resource

- `scripts/extract_target_color.py`: Compute a stable median RGB/HEX value from user-selected normalized garment interiors and optionally write a swatch.
