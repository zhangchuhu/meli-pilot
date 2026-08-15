import io
import json
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path


sys.path.insert(0, str(Path(__file__).parent))
import safe_edit


class SafeEditTest(unittest.TestCase):
    def test_prompt_is_one_literal_argv_value_and_never_shell_syntax(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            capture = root / "argv.json"
            sentinel = root / "must-not-exist"
            fake_doubao = root / "fake_doubao.py"
            fake_doubao.write_text(
                "import json, os, sys\n"
                "from pathlib import Path\n"
                "Path(os.environ['CAPTURE_PATH']).write_text(json.dumps(sys.argv[1:]))\n",
                encoding="utf-8",
            )
            prompt = root / "prompt.txt"
            literal = (
                f'Visible text: "quoted" `backtick` $(touch {sentinel})\n'
                "second line with 'apostrophe'"
            )
            prompt.write_text(literal, encoding="utf-8")
            target = root / "target.png"
            source = root / "source.png"
            target.write_bytes(b"target")
            source.write_bytes(b"source")
            output = root / "attempt-01-deadbeefcafe-01.png"

            old_capture = os.environ.get("CAPTURE_PATH")
            os.environ["CAPTURE_PATH"] = str(capture)
            try:
                with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                    status = safe_edit.main([
                        "--doubao-script", str(fake_doubao),
                        "--prompt-file", str(prompt),
                        "--image", str(target), "--image", str(source),
                        "--out", str(output), "--dry-run",
                    ])
            finally:
                if old_capture is None:
                    os.environ.pop("CAPTURE_PATH", None)
                else:
                    os.environ["CAPTURE_PATH"] = old_capture

            self.assertEqual(status, 0)
            argv = json.loads(capture.read_text(encoding="utf-8"))
            prompt_index = argv.index("--prompt")
            self.assertEqual(argv[prompt_index + 1], literal)
            self.assertFalse(sentinel.exists())
            self.assertEqual(argv[:2], ["--dry-run", "edit"])
            self.assertIn("--no-watermark", argv)

    def test_rejects_existing_output_before_invoking_doubao(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            script = root / "doubao.py"
            prompt = root / "prompt.txt"
            image_1 = root / "target.png"
            image_2 = root / "source.png"
            output = root / "attempt-01-deadbeefcafe-01.png"
            for path in (script, prompt, image_1, image_2, output):
                path.write_text("value", encoding="utf-8")
            stderr = io.StringIO()
            with redirect_stdout(io.StringIO()), redirect_stderr(stderr):
                status = safe_edit.main([
                    "--doubao-script", str(script),
                    "--prompt-file", str(prompt),
                    "--image", str(image_1), "--image", str(image_2),
                    "--out", str(output),
                ])
            self.assertEqual(status, 1)
            self.assertIn("output already exists", stderr.getvalue())


if __name__ == "__main__":
    unittest.main()
