# Prompting Doubao Seedream

## Structure

Shape prompts in this order, including only useful fields:

```text
Asset type: <where it will be used>
Primary request: <main instruction>
Input images: <Image 1 role; Image 2 role>
Scene/backdrop: <environment>
Subject: <main subject>
Style/medium: <photo, illustration, 3D, etc.>
Composition/framing: <viewpoint, crop, aspect ratio>
Lighting/mood: <lighting and mood>
Text (verbatim): "<exact copy>"
Constraints: <must keep and must avoid>
```

If the user's prompt is already detailed, normalize it without adding creative requirements. If it is generic, add only practical composition, intended-use, or polish hints that materially improve the output.

## Generate

- Describe the intended asset and aspect ratio in the prompt when using a resolution tier; Seedream chooses the exact dimensions.
- For photorealism, specify real texture, camera framing, and natural lighting.
- Put literal in-image text in quotes and require verbatim rendering with no extra characters.
- Avoid unrequested characters, props, brands, slogans, or story beats.

## Edit and multi-image fusion

- Label references by API order: `Image 1`, `Image 2`, etc.
- State each role explicitly: edit target, garment source, style reference, background source, or compositing insert.
- Repeat invariants: `Change only X. Keep face, pose, framing, lighting, and all unmentioned regions unchanged.`
- For compositing, require matched perspective, scale, light direction, shadows, and color temperature.
- Iterate with one change rather than rewriting all instructions.

## Point and box editing

Use Seedream 5.0 pro spatial markup directly in the prompt:

```text
Replace the lamp near Image 1 <point>520 460</point> with a vase.
Change only the object near the marked point; keep the room unchanged.
```

```text
Replace the object in Image 1 <bbox>120 180 640 760</bbox> with a flower garden.
Keep all pixels and objects outside the marked box unchanged.
```

For cross-image placement:

```text
Place the subject from Image 1 <bbox>179 283 796 986</bbox> into Image 2 <bbox>118 331 933 871</bbox>.
Match Image 2's perspective, scale, lighting, and shadows. Keep everything else unchanged.
```

Coordinates are normalized to `[0,999]`. Name the target object as well as its box when several objects overlap.

## Layer decomposition

- Omit the prompt to let the model identify main subjects, text, background, and decorations.
- To control the split, say which semantic elements should become independent layers.
- For exact regions, use one `<bbox>` tag per desired layer.
