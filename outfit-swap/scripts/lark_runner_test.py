"""Behavior tests for the argv-safe lark-cli Base transport."""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path

from scripts.lark_runner import LarkBaseClient, LarkRunnerError


class LarkBaseClientTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.root = Path(self.tempdir.name)
        self.task_dir = self.root / "record-task"
        self.task_dir.mkdir()
        self.trace = self.root / "calls.ndjson"
        self.executable = self.root / "fake-lark-cli"
        self.executable.write_text(
            "#!/usr/bin/env python3\n"
            "import json, os, pathlib, sys\n"
            "trace = pathlib.Path(os.environ['LARK_TEST_TRACE'])\n"
            "trace.open('a', encoding='utf-8').write(json.dumps(\n"
            "    {'argv': sys.argv[1:], 'cwd': os.getcwd()}) + '\\n')\n"
            "if os.environ.get('LARK_TEST_FAIL') == '1':\n"
            "    print(os.environ['LARK_TEST_SECRET'], file=sys.stderr)\n"
            "    raise SystemExit(7)\n"
            "for flag in ('--output',):\n"
            "    if flag in sys.argv:\n"
            "        pathlib.Path(sys.argv[sys.argv.index(flag) + 1].lstrip('@')).write_text('artifact')\n"
            "print(json.dumps({'ok': True}))\n",
            encoding="utf-8",
        )
        self.executable.chmod(0o755)
        self.old_trace = os.environ.get("LARK_TEST_TRACE")
        os.environ["LARK_TEST_TRACE"] = str(self.trace)
        self.client = LarkBaseClient(task_dir=self.task_dir, executable=self.executable)

    def tearDown(self) -> None:
        if self.old_trace is None:
            os.environ.pop("LARK_TEST_TRACE", None)
        else:
            os.environ["LARK_TEST_TRACE"] = self.old_trace
        self.tempdir.cleanup()

    def _calls(self) -> list[dict]:
        return [json.loads(line) for line in self.trace.read_text().splitlines()]

    def test_file_backed_commands_use_bare_relative_arguments_and_parent_cwd(self) -> None:
        """Fails if a file-backed command can leak a local path into argv."""
        candidate = self.task_dir / "candidate.png"
        payload = self.task_dir / "update.json"
        candidate.write_bytes(b"candidate")
        payload.write_text('{"update_records":{}}', encoding="utf-8")

        self.assertEqual(
            self.client.upload_attachment(
                file=Path("candidate.png"), app_token="app-token", table_id="tbl123",
            ),
            {"ok": True},
        )
        self.assertEqual(
            self.client.update_record(
                app_token="app-token", table_id="tbl123", record_id="rec123",
                payload=Path("update.json"),
            ),
            {"ok": True},
        )
        self.assertEqual(
            self.client.list_records(
                app_token="app-token", table_id="tbl123", view_id="vew123",
                output=Path("records.ndjson"),
            ),
            self.task_dir.resolve() / "records.ndjson",
        )

        calls = self._calls()
        self.assertEqual([call["cwd"] for call in calls], [str(self.task_dir.resolve())] * 3)
        self.assertEqual(calls[0]["argv"], [
            "base", "+record-upload-attachment", "--base-token", "app-token",
            "--table-id", "tbl123", "--file", "./candidate.png", "--as", "user",
        ])
        self.assertEqual(calls[1]["argv"], [
            "base", "+record-batch-update", "--base-token", "app-token",
            "--table-id", "tbl123", "--json", "@./update.json", "--as", "user",
        ])
        self.assertEqual(calls[2]["argv"], [
            "base", "+record-list", "--base-token", "app-token", "--table-id", "tbl123",
            "--view-id", "vew123", "--output", "./records.ndjson", "--minimal-stdout",
            "--as", "user",
        ])

    def test_rejects_unsafe_or_missing_file_inputs_before_cli_execution(self) -> None:
        """Fails if traversal, escape, or absent input can reach lark-cli."""
        escaped = self.root / "outside.png"
        escaped.write_bytes(b"outside")
        (self.task_dir / "escaped.png").symlink_to(escaped)
        (self.task_dir / "candidate.png").write_bytes(b"candidate")

        unsafe_inputs = [
            Path("/tmp/candidate.png"),
            Path("../candidate.png"),
            Path("nested/candidate.png"),
            Path("missing.png"),
            Path("escaped.png"),
        ]
        for unsafe in unsafe_inputs:
            with self.subTest(unsafe=unsafe):
                with self.assertRaises(LarkRunnerError):
                    self.client.upload_attachment(
                        file=unsafe, app_token="app-token", table_id="tbl123",
                    )
        self.assertFalse(self.trace.exists())

    def test_failure_is_sanitized_of_attachment_tokens_and_file_contents(self) -> None:
        """Fails if child diagnostics expose sensitive command data to callers."""
        secret = "attachment-token-secret-local-file-content"
        payload = self.task_dir / "update.json"
        payload.write_text(secret, encoding="utf-8")
        old_fail = os.environ.get("LARK_TEST_FAIL")
        old_secret = os.environ.get("LARK_TEST_SECRET")
        os.environ["LARK_TEST_FAIL"] = "1"
        os.environ["LARK_TEST_SECRET"] = secret
        try:
            with self.assertRaises(LarkRunnerError) as raised:
                self.client.update_record(
                    app_token="app-token", table_id="tbl123", record_id="rec123",
                    payload=Path("update.json"),
                )
        finally:
            if old_fail is None:
                os.environ.pop("LARK_TEST_FAIL", None)
            else:
                os.environ["LARK_TEST_FAIL"] = old_fail
            if old_secret is None:
                os.environ.pop("LARK_TEST_SECRET", None)
            else:
                os.environ["LARK_TEST_SECRET"] = old_secret

        self.assertNotIn(secret, str(raised.exception))
        self.assertNotIn("app-token", str(raised.exception))
        self.assertEqual(str(raised.exception), "lark-cli command failed with status 7")

    def test_non_file_calls_return_json_from_the_cli(self) -> None:
        """Fails if typed read commands do not parse the CLI's JSON response."""
        self.assertEqual(
            self.client.resolve_base("https://example.larkoffice.com/base/app-token?table=tbl123"),
            {"ok": True},
        )
        self.assertEqual(
            self.client.get_record(
                app_token="app-token", table_id="tbl123", record_id="rec123",
            ),
            {"ok": True},
        )


if __name__ == "__main__":
    unittest.main()
