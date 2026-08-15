import io
import json
import os
import shutil
import struct
import subprocess
import sys
import tempfile
import unittest
import zlib
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch


sys.path.insert(0, str(Path(__file__).parent))
import image_qc


FFMPEG_MISSING = not shutil.which("ffmpeg") or not shutil.which("ffprobe")
FFMPEG_SKIP_REASON = "ffmpeg and ffprobe are required for image QC integration tests"
LIBX265_AVAILABLE = False
if not FFMPEG_MISSING:
    encoder_list = subprocess.run(
        ["ffmpeg", "-hide_banner", "-encoders"],
        capture_output=True, text=True, check=False, timeout=10,
    )
    LIBX265_AVAILABLE = encoder_list.returncode == 0 and "libx265" in encoder_list.stdout


def write_png(path: Path, width: int, height: int) -> None:
    raw = b"".join(b"\x00" + b"\x80\x80\x80" * width for _ in range(height))

    def chunk(kind: bytes, data: bytes) -> bytes:
        return (
            struct.pack(">I", len(data))
            + kind
            + data
            + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)
        )

    path.write_bytes(
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(raw))
        + chunk(b"IEND", b"")
    )


def read_ppm(path: Path) -> tuple[int, int, bytes]:
    magic, dimensions, maximum, pixels = path.read_bytes().split(b"\n", 3)
    width, height = (int(value) for value in dimensions.split())
    if magic != b"P6" or maximum != b"255" or len(pixels) != width * height * 3:
        raise AssertionError("invalid test PPM")
    return width, height, pixels


def ppm_pixel(pixels: bytes, width: int, x: int, y: int) -> bytes:
    start = (y * width + x) * 3
    return pixels[start:start + 3]


class AcceptedOutputPromotionTest(unittest.TestCase):
    def test_promotion_is_atomic_exactly_named_and_preserves_attempt(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            attempt = root / "attempt-01-deadbeefcafe-04.png"
            accepted = root / "look-01-deadbeefcafe.png"
            attempt.write_bytes(b"accepted pixels")
            accepted.write_bytes(b"stale pixels")

            with patch.object(image_qc.os, "replace", wraps=os.replace) as replace:
                promoted = image_qc.promote_output(attempt, accepted)

            self.assertEqual(promoted, accepted)
            self.assertEqual(accepted.read_bytes(), b"accepted pixels")
            self.assertEqual(attempt.read_bytes(), b"accepted pixels")
            replace.assert_called_once()
            temporary, destination = replace.call_args.args
            self.assertEqual(Path(temporary).parent, accepted.parent)
            self.assertEqual(Path(destination), accepted)
            self.assertFalse(any("-v2" in path.name for path in root.iterdir()))


class RasterContentContractTest(unittest.TestCase):
    def test_multi_frame_heif_probe_is_rejected_without_optional_encoder(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "video.heic"
            path.write_bytes(b"multi-frame placeholder")
            info = image_qc.ImageInfo(
                path, 64, 64, "hevc", path.stat().st_size,
                format_name="mov,mp4,m4a,3gp,3g2,mj2",
                major_brand="heic", frame_count=10,
            )
            with (
                patch.object(image_qc, "probe_image", return_value=info),
                patch.object(
                    image_qc, "_run",
                    return_value=subprocess.CompletedProcess([], 0, "", ""),
                ),
            ):
                with self.assertRaisesRegex(
                    image_qc.ImageQCError, "unsupported raster content",
                ):
                    image_qc.validate_image(path)


@unittest.skipIf(FFMPEG_MISSING, FFMPEG_SKIP_REASON)
class ImageValidationTest(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)

    def tearDown(self) -> None:
        self.directory.cleanup()

    def test_valid_square_passes(self) -> None:
        path = self.root / "valid.png"
        write_png(path, 64, 64)
        info = image_qc.validate_image(path)
        self.assertEqual((info.width, info.height, info.pixels), (64, 64, 4096))
        self.assertEqual(info.codec_name, "png")

    def test_non_file_is_rejected_before_probe(self) -> None:
        with self.assertRaisesRegex(image_qc.ImageQCError, "not a file"):
            image_qc.validate_image(self.root)

    def test_file_over_thirty_megabytes_is_rejected_before_probe(self) -> None:
        path = self.root / "large.png"
        with path.open("wb") as stream:
            stream.truncate(30 * 1024 * 1024 + 1)
        with self.assertRaisesRegex(image_qc.ImageQCError, "30 MiB"):
            image_qc.validate_image(path)

    def test_side_of_fourteen_pixels_is_rejected(self) -> None:
        path = self.root / "narrow.png"
        write_png(path, 14, 64)
        with self.assertRaisesRegex(image_qc.ImageQCError, "greater than 14"):
            image_qc.validate_image(path)

    def test_ratio_over_sixteen_is_rejected(self) -> None:
        path = self.root / "wide.png"
        write_png(path, 257, 16)
        with self.assertRaisesRegex(image_qc.ImageQCError, "aspect ratio"):
            image_qc.validate_image(path)

    def test_corrupt_image_is_rejected_by_full_decode(self) -> None:
        path = self.root / "corrupt.png"
        write_png(path, 64, 64)
        path.write_bytes(path.read_bytes()[:-8])
        with self.assertRaisesRegex(image_qc.ImageQCError, "decode"):
            image_qc.validate_image(path)

    def test_pixel_count_over_thirty_six_million_is_rejected(self) -> None:
        path = self.root / "huge.png"
        path.write_bytes(b"placeholder")
        info = image_qc.ImageInfo(path, 6001, 6000, "png", len(b"placeholder"))
        with patch.object(image_qc, "probe_image", return_value=info):
            with self.assertRaisesRegex(image_qc.ImageQCError, "pixel count"):
                image_qc.validate_image(path)

    def test_mp4_and_video_content_renamed_as_png_are_rejected(self) -> None:
        video = self.root / "video.mp4"
        result = subprocess.run([
            "ffmpeg", "-v", "error", "-f", "lavfi", "-i",
            "color=red:s=64x64:d=0.2", "-c:v", "mpeg4", "-y", str(video),
        ], capture_output=True, text=True, check=False, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)

        with self.assertRaisesRegex(image_qc.ImageQCError, "unsupported image suffix"):
            image_qc.validate_image(video)

        disguised = self.root / "video.png"
        disguised.write_bytes(video.read_bytes())
        with self.assertRaisesRegex(image_qc.ImageQCError, "unsupported raster content"):
            image_qc.validate_image(disguised)

    def test_raw_hevc_stream_renamed_as_heic_is_rejected(self) -> None:
        disguised = self.root / "video.heic"
        disguised.write_bytes(b"raw hevc placeholder")
        info = image_qc.ImageInfo(
            disguised, 64, 64, "hevc", disguised.stat().st_size,
            format_name="hevc",
        )
        with (
            patch.object(image_qc, "probe_image", return_value=info),
            patch.object(
                image_qc, "_run",
                return_value=subprocess.CompletedProcess([], 0, "", ""),
            ),
        ):
            with self.assertRaisesRegex(
                image_qc.ImageQCError, "unsupported raster content",
            ):
                image_qc.validate_image(disguised)

    @unittest.skipUnless(LIBX265_AVAILABLE, "ffmpeg libx265 encoder is unavailable")
    def test_branded_multi_frame_heic_video_is_rejected(self) -> None:
        video = self.root / "video.heic"
        result = subprocess.run([
            "ffmpeg", "-v", "error", "-f", "lavfi", "-i",
            "color=red:s=64x64:r=10:d=1", "-c:v", "libx265",
            "-preset", "ultrafast", "-tag:v", "hvc1", "-brand", "heic",
            "-f", "mp4", "-y", str(video),
        ], capture_output=True, text=True, check=False, timeout=20)
        self.assertEqual(result.returncode, 0, result.stderr)
        with self.assertRaisesRegex(
            image_qc.ImageQCError, "unsupported raster content",
        ):
            image_qc.validate_image(video)

    def test_subprocess_timeout_is_bounded_and_translated(self) -> None:
        path = self.root / "slow.png"
        path.write_bytes(b"placeholder")
        with patch.object(
            image_qc.subprocess, "run",
            side_effect=subprocess.TimeoutExpired(["ffprobe"], timeout=30),
        ) as run:
            with self.assertRaisesRegex(image_qc.ImageQCError, "timed out after 30 seconds"):
                image_qc.probe_image(path)
        self.assertEqual(run.call_args.kwargs["timeout"], 30)


@unittest.skipIf(FFMPEG_MISSING, FFMPEG_SKIP_REASON)
class ManifestAndContactSheetTest(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)

    def tearDown(self) -> None:
        self.directory.cleanup()

    def item(self, identifier: str, classification: str = "front") -> dict[str, str]:
        path = self.root / f"{identifier}.png"
        write_png(path, 64, 64)
        return {"id": identifier, "path": str(path), "classification": classification}

    def test_all_approved_classifications_are_preserved(self) -> None:
        classifications = [
            "front", "front three-quarter", "side", "back three-quarter",
            "back", "detail or flat lay", "infographic",
        ]
        items = [self.item(f"S{index:02d}", value)
                 for index, value in enumerate(classifications, start=1)]
        validated = image_qc.validate_manifest(items)
        self.assertEqual(
            [item["classification"] for item in validated], classifications,
        )

    def test_empty_manifest_is_rejected(self) -> None:
        with self.assertRaisesRegex(image_qc.ImageQCError, "must not be empty"):
            image_qc.validate_manifest([])

    def test_duplicate_id_is_rejected(self) -> None:
        path = self.root / "same.png"
        write_png(path, 64, 64)
        items = [
            {"id": "S01", "path": str(path), "classification": "front"},
            {"id": "S01", "path": str(path), "classification": "side"},
        ]
        with self.assertRaisesRegex(image_qc.ImageQCError, "duplicate image id"):
            image_qc.validate_manifest(items)

    def test_unsafe_id_is_rejected(self) -> None:
        item = self.item("S01")
        item["id"] = "../S01"
        with self.assertRaisesRegex(image_qc.ImageQCError, "unsafe image id"):
            image_qc.validate_manifest([item])

    def test_id_longer_than_five_characters_is_rejected(self) -> None:
        item = self.item("S01")
        item["id"] = "ABCDEF"
        with self.assertRaisesRegex(image_qc.ImageQCError, "unsafe image id"):
            image_qc.validate_manifest([item])

    def test_unknown_classification_is_rejected(self) -> None:
        item = self.item("S01")
        item["classification"] = "hero"
        with self.assertRaisesRegex(image_qc.ImageQCError, "unknown classification"):
            image_qc.validate_manifest([item])

    def test_non_string_classification_is_rejected_as_unknown(self) -> None:
        item = self.item("S01")
        item["classification"] = ["front"]
        with self.assertRaisesRegex(image_qc.ImageQCError, "unknown classification"):
            image_qc.validate_manifest([item])

    def test_three_images_produce_a_decodable_jpeg_contact_sheet(self) -> None:
        items = [self.item("S01", "front"), self.item("S02", "side"),
                 self.item("S03", "back")]
        output = self.root / "sheet.jpg"
        image_qc.build_contact_sheet(items, output)
        info = image_qc.validate_image(output)
        self.assertEqual((info.codec_name, info.width, info.height), ("mjpeg", 640, 640))

    def test_contact_sheet_command_has_separate_inputs_and_all_labels(self) -> None:
        items = [self.item("S01", "front"), self.item("S02", "side"),
                 self.item("S03", "back")]
        output = self.root / "sheet.jpg"

        def write_fake_output(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
            Path(command[-1]).write_bytes(b"fake jpeg")
            return subprocess.CompletedProcess(command, 0, "", "")

        with (
            patch.object(image_qc, "validate_manifest", return_value=items),
            patch.object(
                image_qc,
                "validate_image",
                return_value=image_qc.ImageInfo(output, 640, 640, "mjpeg", 9),
            ),
            patch.object(image_qc.subprocess, "run", side_effect=write_fake_output) as run,
        ):
            image_qc.build_contact_sheet(items, output)

        run.assert_called_once()
        command = run.call_args.args[0]
        for item in items:
            self.assertIn(["-i", item["path"]],
                          [command[index:index + 2] for index in range(len(command) - 1)])
        filter_graph = command[command.index("-filter_complex") + 1]
        for label in ("S01 · front", "S02 · side", "S03 · back"):
            self.assertIn(f"drawtext=text='{label}'", filter_graph)

    def test_output_cannot_replace_an_input(self) -> None:
        item = self.item("S01")
        with self.assertRaisesRegex(image_qc.ImageQCError, "output equals an input"):
            image_qc.build_contact_sheet([item], Path(item["path"]))

    def test_drawtext_escaping_covers_filter_metacharacters(self) -> None:
        self.assertEqual(
            image_qc.escape_drawtext(r"a\b'c:d%e,f"),
            "a\\\\b'" + "\\" * 3 + "''c\\:d\\%e\\,f",
        )

    def test_apostrophe_escape_survives_real_ffmpeg_filter_parsing(self) -> None:
        value = "owner's front"
        escaped = image_qc.escape_drawtext(value)
        filter_graph = (
            f"metadata=mode=add:key=label:value='{escaped}',"
            "metadata=mode=print:key=label:file=-"
        )
        result = subprocess.run([
            "ffmpeg", "-v", "error", "-f", "lavfi", "-i",
            "color=white:s=16x16:d=0.04", "-vf", filter_graph,
            "-frames:v", "1", "-f", "null", "-",
        ], capture_output=True, text=True, check=False)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(f"label={value}", result.stdout)

    def test_bitmap_fallback_renders_expected_lowercase_glyph_pixels(self) -> None:
        label = self.root / "lower.ppm"
        image_qc._write_bitmap_label(label, "a")
        width, height, pixels = read_ppm(label)
        self.assertEqual((width, height), (320, 40))
        sampled = tuple(
            "".join(
                "1" if ppm_pixel(pixels, width, 10 + column * 3 + 1,
                                 7 + row * 3 + 1) == b"\x00\x00\x00" else "0"
                for column in range(3)
            )
            for row in range(5)
        )
        self.assertEqual(sampled, ("000", "011", "101", "111", "101"))

    def test_every_maximum_id_classification_label_renders_its_final_glyph(self) -> None:
        for classification in sorted(image_qc.CLASSIFICATIONS):
            with self.subTest(classification=classification):
                value = f"ABCDE · {classification}"
                label = self.root / "label.ppm"
                without_final = self.root / "without-final.ppm"
                image_qc._write_bitmap_label(label, value)
                image_qc._write_bitmap_label(without_final, value[:-1] + " ")
                width, height, pixels = read_ppm(label)
                other_width, other_height, other_pixels = read_ppm(without_final)
                self.assertEqual((width, height), (320, 40))
                self.assertEqual((other_width, other_height), (width, height))
                self.assertNotEqual(
                    pixels, other_pixels,
                    "the final classification glyph must contribute visible pixels",
                )


@unittest.skipIf(FFMPEG_MISSING, FFMPEG_SKIP_REASON)
class CommandLineTest(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)

    def tearDown(self) -> None:
        self.directory.cleanup()

    def write_manifest(self, items: list[dict[str, str]]) -> Path:
        path = self.root / "items.json"
        path.write_text(json.dumps(items), encoding="utf-8")
        return path

    def test_validate_writes_report_and_prints_compact_summary(self) -> None:
        image = self.root / "S01.png"
        write_png(image, 64, 64)
        source = self.write_manifest([
            {"id": "S01", "path": str(image), "classification": "front"},
        ])
        output = self.root / "report.json"
        stdout = io.StringIO()
        with redirect_stdout(stdout):
            status = image_qc.main([
                "validate", "--input", str(source), "--output", str(output),
            ])
        self.assertEqual(status, 0)
        report = json.loads(output.read_text(encoding="utf-8"))
        self.assertEqual((report[0]["id"], report[0]["pixels"]), ("S01", 4096))
        self.assertEqual(
            stdout.getvalue(),
            json.dumps({"count": 1, "output": str(output)}, separators=(",", ":")) + "\n",
        )

    def test_contact_sheet_cli_prints_compact_summary(self) -> None:
        image = self.root / "S01.png"
        write_png(image, 64, 64)
        source = self.write_manifest([
            {"id": "S01", "path": str(image), "classification": "front"},
        ])
        output = self.root / "sheet.jpg"
        stdout = io.StringIO()
        with redirect_stdout(stdout):
            status = image_qc.main([
                "contact-sheet", "--input", str(source), "--output", str(output),
            ])
        self.assertEqual(status, 0)
        self.assertTrue(output.is_file())
        self.assertEqual(
            stdout.getvalue(),
            json.dumps({"count": 1, "output": str(output)}, separators=(",", ":")) + "\n",
        )

    def test_empty_output_contact_sheet_exists_before_any_acceptance(self) -> None:
        output = self.root / "output-contact-sheet.jpg"
        result = subprocess.run([
            sys.executable, str(Path(image_qc.__file__)), "empty-contact-sheet",
            "--output", str(output), "--label", "NO ACCEPTED OUTPUTS",
        ], capture_output=True, text=True, check=False)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            result.stdout,
            json.dumps({"count": 0, "output": str(output)}, separators=(",", ":")) + "\n",
        )
        info = image_qc.validate_image(output)
        self.assertEqual((info.codec_name, info.width, info.height), ("mjpeg", 320, 320))

        decoded = subprocess.run([
            "ffmpeg", "-v", "error", "-i", str(output), "-f", "rawvideo",
            "-pix_fmt", "gray", "-",
        ], capture_output=True, check=False)
        self.assertEqual(decoded.returncode, 0, decoded.stderr.decode(errors="replace"))
        self.assertLess(min(decoded.stdout), 100, "empty sheet must contain a visible label")

    def test_validation_error_uses_stderr_prefix_and_returns_one(self) -> None:
        source = self.write_manifest([
            {"id": "S01", "path": str(self.root / "missing.png"),
             "classification": "front"},
        ])
        stderr = io.StringIO()
        with redirect_stderr(stderr):
            status = image_qc.main([
                "validate", "--input", str(source),
                "--output", str(self.root / "report.json"),
            ])
        self.assertEqual(status, 1)
        self.assertTrue(stderr.getvalue().startswith("image-qc error: image is not a file:"))


if __name__ == "__main__":
    unittest.main()
