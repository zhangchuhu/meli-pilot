import importlib.util
import tempfile
import unittest
from pathlib import Path


MODULE_PATH = Path(__file__).with_name("generate_from_base.py")
SPEC = importlib.util.spec_from_file_location("generate_from_base", MODULE_PATH)
MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
SPEC.loader.exec_module(MODULE)


class GenerateFromBaseTests(unittest.TestCase):
    def test_duration_clamps_to_seedance_range(self):
        self.assertEqual(MODULE.normalized_duration(1.07, 5, "clamp"), 4)
        self.assertEqual(MODULE.normalized_duration(8.53, 5, "clamp"), 9)
        self.assertEqual(MODULE.normalized_duration(99, 5, "clamp"), 30)

    def test_duration_rejects_out_of_range(self):
        with self.assertRaises(MODULE.WorkflowError):
            MODULE.normalized_duration(1.8, 5, "reject")

    def test_payload_uses_text_and_reference_image(self):
        with tempfile.TemporaryDirectory() as directory:
            image = Path(directory) / "frame.jpg"
            image.write_bytes(b"jpeg-test")
            payload = MODULE.build_payload(
                "镜头缓慢推近", 5, [image], MODULE.DEFAULT_MODEL,
                "adaptive", "720p", False, False,
            )
        self.assertEqual(payload["model"], "doubao-seedance-2-5-260628")
        self.assertEqual(payload["duration"], 5)
        self.assertEqual(payload["content"][0], {"type": "text", "text": "镜头缓慢推近"})
        self.assertTrue(payload["content"][1]["image_url"]["url"].startswith("data:image/jpeg;base64,"))

    def test_output_path_must_be_relative(self):
        with self.assertRaises(MODULE.WorkflowError):
            MODULE.require_relative(Path("/tmp/output"))
        with self.assertRaises(MODULE.WorkflowError):
            MODULE.require_relative(Path("../output"))

    def test_short_timeline_gets_explicit_hold(self):
        prompt = MODULE.effective_prompt("0-1.8 秒：人物转身。", 1.8, 4)
        self.assertIn("总时长为 4 秒", prompt)
        self.assertIn("保持结尾停帧", prompt)

    def test_running_task_is_resumed(self):
        record = {"Seedance状态": "running", "Seedance任务ID": "cgt-123"}
        self.assertEqual(MODULE.resumable_task_id(record), "cgt-123")
        record["Seedance状态"] = "failed"
        self.assertEqual(MODULE.resumable_task_id(record), "")

    def test_completed_record_requires_uploaded_video(self):
        record = {"Seedance状态": "succeeded", "Seedance生成视频": []}
        self.assertFalse(MODULE.completed_record(record))
        record["Seedance生成视频"] = [{"name": "video.mp4"}]
        self.assertTrue(MODULE.completed_record(record))


if __name__ == "__main__":
    unittest.main()
