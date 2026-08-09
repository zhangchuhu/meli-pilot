---
name: change-color-skill
description: Extract the representative RGB color of clothing from a reference product link, transfer that color to the clothing in every main-SKU image from a target product link, preserve the target models, poses, garment design, accessories, lighting, composition, and scenes, and save only the finished images in a separate folder. Use when the user provides link 1 as a clothing-color reference and link 2 as the product whose complete current image set must be recolored, especially for Mercado Libre or similar fashion product pages.
---

# Change Clothing Color

Accept two inputs:

- `link 1`: color-reference garment.
- `link 2`: target product whose clothing color must change.

Return the representative color as `RGB(r, g, b)` and `#RRGGBB`, plus a clean folder containing every recolored main-SKU image from link 2.

## Workflow

1. Inspect both product pages in the browser explicitly requested by the user. If Chrome is requested, use the Chrome browser skill and follow its setup, documentation, and tab-finalization rules.
2. Identify the currently selected main SKU on each page. Do not mix recommendation cards, reviews, videos, variant thumbnails from another color, or unrelated products into the image set.
3. Collect the highest-resolution source URLs for:
   - At least one clear garment image from link 1.
   - Every gallery image belonging to link 2's currently selected main SKU.
4. Download the selected originals into temporary working folders. Preserve the source order and use stable names such as `target_01`, `target_02`, and so on.
5. Extract the representative garment color from link 1.
6. Recolor every target image.
7. Inspect the full batch, correct failures, and copy only final images into a separate output folder.
8. Report the RGB, HEX value, image count, output folder, and a contact-sheet preview when useful.

## RGB Extraction

Use pixels from broad, well-lit interior garment areas. Exclude skin, hair, background, logos, seams, specular highlights, deep folds, and cast shadows.

- Sample both top and bottom when the product is a matching set.
- Prefer the median or dominant cluster of valid garment pixels over a whole-image average.
- Treat the extracted value as the garment's base color. Preserve natural highlight and shadow variation during recoloring rather than painting every pixel to one flat RGB value.
- Visually verify that the computed swatch matches the reference garment.
- If link 1 contains several garment colors or the selected SKU is ambiguous, stop and ask which color to use.

## Recoloring Strategy

Preserve link 2 as the source of truth for everything except clothing color.

Prefer a deterministic masked color transformation when the target garment is cleanly separable by hue, saturation, or luminance. This approach must keep all unmasked pixels unchanged and retain folds, texture, seams, highlights, and shadows.

Use the available provider-based raster image-editing skill when semantic segmentation or localized correction is required. When using an image model:

- Pass each link 2 image as the edit target.
- Pass link 1 only as a color reference.
- Use a garment mask whenever practical.
- Instruct the model to change only garment color to the extracted RGB/HEX value.
- Explicitly prohibit changes to the model, face, hair, skin, body, hands, pose, accessories, footwear, garment cut, garment construction, background, furniture, text, camera angle, framing, lighting, and composition.
- Do not transfer the person, clothing design, or scene from link 1.
- Generate one result first and validate it before processing the remaining batch.

For dark destination colors, map the source garment's luminance into a dark tonal range instead of replacing it with solid black. Keep enough local contrast to show fabric texture.

## Batch Validation

Compare every result against its corresponding original at the same dimensions.

Verify all of the following:

- The top and bottom garments use the reference color consistently.
- No original target garment color remains along edges, hair overlaps, hands, pockets, waistbands, or hems.
- Skin, hair, face, hands, accessories, shoes, background, furniture, text, and logos are unchanged.
- Garment silhouette, seams, folds, texture, and shadows remain natural.
- Informational collages and size charts are recolored only where they display the target garment; their text and layout remain intact.
- No target gallery image is missing or duplicated.

If a semantic image edit changes identity, pose, scene, garment design, or typography, reject it. Retry with a tighter mask or use deterministic pixel editing.

## Output Contract

Create a new folder in the current workspace, for example:

```text
change-color-output-YYYYMMDD-HHMMSS/
  link2_01_recolored.png
  link2_02_recolored.png
  ...
```

Keep downloads, masks, model experiments, previews, and failed attempts outside this final folder. Preserve the original aspect ratio and pixel dimensions when deterministic editing is used. Use lossless PNG unless the user requests another format.

Report:

- `RGB(r, g, b)`
- `#RRGGBB`
- Number of final images
- Absolute clickable path to the final folder
- Any image where exact preservation could not be guaranteed

Finalize browser tabs after all required page information has been collected.
