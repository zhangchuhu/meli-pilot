#!/usr/bin/env python3

import base64
import importlib.util
import tempfile
import unittest
from pathlib import Path


MODULE_PATH = Path(__file__).with_name("doubao_imagegen.py")
SPEC = importlib.util.spec_from_file_location("doubao_imagegen", MODULE_PATH)
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


class DoubaoImagegenTests(unittest.TestCase):
    def test_explicit_size_validation(self):
        MODULE.validate_size("2048x1024", decompose=False)
        with self.assertRaises(MODULE.CliError):
            MODULE.validate_size("512x512", decompose=False)

    def test_transparency_constraints(self):
        payload = {
            "model": MODULE.DEFAULT_MODEL,
            "prompt": "edit",
            "image": ["a.png", "b.png"],
            "size": "2K",
            "output_format": "png",
            "background": "transparent",
        }
        with self.assertRaises(MODULE.CliError):
            MODULE.validate_payload(payload)

    def test_decomposition_requires_one_image(self):
        payload = {"model": MODULE.DEFAULT_MODEL, "image": [], "size": "auto", "output_format": "png"}
        with self.assertRaises(MODULE.CliError):
            MODULE.validate_payload(payload, decompose=True)

    def test_decomposition_local_input_format(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "input.webp"
            source.write_bytes(b"fixture")
            with self.assertRaises(MODULE.CliError):
                MODULE.image_value(str(source), allowed_suffixes={".png", ".jpg", ".jpeg"})

    def test_base64_save_and_versioning(self):
        content = b"not-a-real-image-but-valid-download-bytes"
        item = {"b64_json": base64.b64encode(content).decode("ascii")}
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "asset.png"
            first = MODULE.save_item(item, target, overwrite=False, timeout=1)
            second = MODULE.save_item(item, target, overwrite=False, timeout=1)
            self.assertEqual(first.read_bytes(), content)
            self.assertEqual(second.name, "asset-v2.png")


if __name__ == "__main__":
    unittest.main()
