# Content-driven image validation

## Goal

Accept supported static image attachments even when their filename extension does not match their encoded bitmap format. In particular, a PNG bitmap downloaded with a `.jpg` filename must not cause an `invalid-target` record failure.

## Validation design

`scripts/image_qc.py` will continue to require a supported image filename extension. After probing, ordinary raster attachments will be accepted when their actual codec is any codec supported by the image-QC contract, independent of the filename extension.

The change applies symmetrically to source and target attachments because both roles use `validate_image`. No task-state or record-error mapping changes are required.

The following protections remain unchanged:

- unsupported filename extensions are rejected;
- video containers disguised with image extensions are rejected;
- corrupt or incomplete bitmaps are rejected by full decode;
- file-size, dimensions, pixel-count, and aspect-ratio limits remain enforced;
- HEIC and HEIF inputs still require a recognized HEIF brand and exactly one frame.

## Alternatives considered

Renaming downloaded files to match their probed codec was rejected because it would mutate paths referenced by manifests and attachment workflows. Adding only JPG-to-PNG exceptions was rejected because it would preserve the same failure for other supported static format mismatches and would not meet the requested scope.

## Tests

Add a regression test that writes valid PNG bytes to a `.jpg` path and verifies that `validate_image` succeeds and reports the PNG codec. The existing disguised-video test must continue to pass, demonstrating that removing extension/codec coupling does not admit video content.

Run the focused image-QC test module first, followed by the repository's complete Python test suite.
