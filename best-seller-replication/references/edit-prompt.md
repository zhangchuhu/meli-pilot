# Per-image edit prompt

Fill the bracketed fields for each target image. Keep the target as Image 1.

```text
Use case: precise-object-edit, virtual try-on.

Image 1 is the edit target. Preserve the exact target model identity, face, body, skin, hair, pose, hands, feet, accessories, carried objects, shoes, camera angle, framing, crop, background, lighting, shadows, and color grade.

Images 2-N are garment references for the exact source product. Replace ONLY the clothing worn in Image 1 with the source garment. Reproduce its exact [COLOR], [MATERIAL], [NECKLINE OR COLLAR], [SLEEVES], [CLOSURES], [WAIST CONSTRUCTION], [BELT], [SILHOUETTE], [LENGTH], [HEM], and [VISIBLE DETAILS]. Render it naturally for this [ANGLE] view and [POSE], with physically plausible folds, occlusion, perspective, lighting, reflections, and shadows.

Preserve these target-specific invariants: [INVARIANTS].

If this source angle is missing, infer only structurally consistent [SIDE OR BACK] construction from the supplied references. Do not invent openings, straps, bows, slits, decorations, text, or logos.

Remove every visible remnant of the target's original clothing. Do not change the person or scene. No extra limbs, altered hands, new jewelry, new props, or new accessories.
```

Use the filled prompt for exactly one built-in `image_gen` edit call for the corresponding target. Keep the target as Image 1 and label every additional reference by role.

## Infographic variant

Use this for detail callouts, color grids, size charts, and comparison layouts. Enumerate every clothing instance that must change.

```text
Use case: precise-object-edit, fashion-product infographic. Image 1 is the edit target. Preserve the exact canvas, layout, existing text, typography, tables, arrows, icons, model identities, poses, crops, backgrounds, and all non-clothing elements. Images 2-N are exact source-garment references. Replace [EXACT INSTANCE COUNT AND LOCATIONS] with the same source garment, including every original-color outfit and garment-detail swatch that must change. Transfer the exact [GARMENT SPECIFICATION]. Remove all original-clothing remnants. Keep all existing text unchanged; add no text or logos. Do not change people, hands, accessories, props, or layout.
```

For a multi-model color grid, say explicitly: `All [COUNT] grid cells must show the source garment.` Do not rely on a general instruction such as “replace the burgundy outfit,” because other dark-color cells may remain unchanged.
