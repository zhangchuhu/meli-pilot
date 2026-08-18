"""End-to-end contract for the standalone table entry point."""

from __future__ import annotations

import io
import json
import os
import struct
import tempfile
import unittest
import zlib
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

from scripts.run_table import main
from scripts.run_record import RecordContext, RecordResult, RecordServices


def _write_png(path: Path, width: int = 64, height: int = 64) -> None:
    raw = b"".join(b"\x00" + b"\x80\x80\x80" * width for _ in range(height))

    def chunk(kind: bytes, data: bytes) -> bytes:
        return (
            struct.pack(">I", len(data)) + kind + data
            + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)
        )

    path.write_bytes(
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(raw))
        + chunk(b"IEND", b"")
    )


class _FakeArk:
    def complete_json(
            self, *, system_prompt: str, user_prompt: str,
            images: tuple[Path, ...] | list[Path],
    ) -> str:
        self.assert_safe(system_prompt, user_prompt, images)
        if "target classification" in system_prompt:
            return json.dumps({
                "schema_version": 1,
                "classification": "front",
            })
        if "source garment evidence" in system_prompt:
            return json.dumps({
                "schema_version": 1,
                "sources": [
                    {
                        "token": f"source_{index}",
                        "angle": "front",
                        "roles": roles,
                        "information_score": 100 - index,
                    }
                    for index, roles in enumerate((
                        ["model"], ["upper_construction"],
                        ["full_outfit_flat_lay", "skirt_hem"],
                    ), start=1)
                ],
                "garment_facts": {
                    "required": ["preserve the complete garment construction"],
                    "forbidden": ["do not retain the replaced clothing"],
                },
                "unique_requirement": None,
            })
        if "visual quality reviewer" in system_prompt:
            marker = "Return the candidate field exactly as '"
            candidate = user_prompt.split(marker, 1)[1].split("'", 1)[0]
            return json.dumps({
                "schema_version": 1,
                "candidate": candidate,
                "scores": {
                    "garment_construction": 95,
                    "color_material": 94,
                    "garment_details": 93,
                    "target_preservation": 96,
                    "text_layout": None,
                },
                "critical_defects": [],
                "primary_defect": None,
                "evidence": [],
                "confidence": 0.99,
                "decision": "accept",
            })
        raise AssertionError("unexpected Ark production request")

    @staticmethod
    def assert_safe(
            system_prompt: str, user_prompt: str,
            images: tuple[Path, ...] | list[Path],
    ) -> None:
        if not system_prompt or not user_prompt or not images:
            raise AssertionError("incomplete Ark request")
        if any(Path(path).suffix != ".png" for path in images):
            raise AssertionError("Ark inputs must use content-derived canonical suffixes")


class StandaloneEntryTest(unittest.TestCase):
    def test_bare_main_materializes_and_processes_a_real_record(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            state = root / "remote-state.json"
            image = root / "input.png"
            _write_png(image)
            state.write_text(json.dumps({"outputs": [], "detail": None}), encoding="utf-8")
            trace = root / "lark-calls.ndjson"
            lark = root / "lark-cli"
            lark.write_text(
                "#!/usr/bin/env python3\n"
                "import json, os, pathlib, shutil, sys\n"
                "args = sys.argv[1:]\n"
                "state_path = pathlib.Path(os.environ['OUTFIT_TEST_REMOTE_STATE'])\n"
                "state = json.loads(state_path.read_text())\n"
                "trace = pathlib.Path(os.environ['OUTFIT_TEST_LARK_TRACE'])\n"
                "trace.open('a').write(json.dumps({'argv': args, 'cwd': os.getcwd()}) + '\\n')\n"
                "command = args[1]\n"
                "if command == '+url-resolve':\n"
                " print(json.dumps({'base_token':'app_exact','table_id':'tbl_exact','view_id':'vew_exact'}))\n"
                "elif command == '+field-list':\n"
                " print(json.dumps({'items':["
                "{'field_name':'原图','field_id':'fld_source','type':'attachment'},"
                "{'field_name':'爆款图','field_id':'fld_target','type':'attachment'},"
                "{'field_name':'输出图','field_id':'fld_output','type':'attachment'},"
                "{'field_name':'任务状态','field_id':'fld_status','type':'single_select','options':['未开始','成功','失败']},"
                "{'field_name':'处理明细','field_id':'fld_detail','type':'text'}], 'has_more':False}))\n"
                "elif command == '+record-list':\n"
                " output = pathlib.Path(args[args.index('--output') + 1])\n"
                " record = {'record_id':'rec_1','fields':{'原图':["
                "{'file_token':'source_1','name':'one.jpg'},"
                "{'file_token':'source_2','name':'../two.jpg'},"
                "{'file_token':'source_3','name':'three.jpg'}],"
                "'爆款图':[{'file_token':'target_1','name':'../../target.jpg'}],"
                "'输出图':state['outputs'],'任务状态':['未开始'],'处理明细':state['detail']}}\n"
                " output.write_text(json.dumps(record) + '\\n')\n"
                " print(json.dumps({'records_count':1,'has_more':False}))\n"
                "elif command == '+record-download-attachment':\n"
                " shutil.copyfile(os.environ['OUTFIT_TEST_IMAGE'], args[args.index('--output') + 1])\n"
                "elif command == '+record-upload-attachment':\n"
                " name = pathlib.Path(args[args.index('--file') + 1]).name\n"
                " mapping = {'file_token':'uploaded_1','name':name}\n"
                " state['outputs'].append(mapping); state_path.write_text(json.dumps(state))\n"
                " print(json.dumps(mapping))\n"
                "elif command == '+record-batch-update':\n"
                " payload = json.loads(pathlib.Path(args[args.index('--json') + 1][1:]).read_text())\n"
                " fields = payload['update_records']['rec_1']\n"
                " state['detail'] = fields.get('处理明细', state['detail'])\n"
                " state['status'] = fields.get('任务状态', state.get('status'))\n"
                " state_path.write_text(json.dumps(state)); print(json.dumps({'ok':True}))\n"
                "elif command == '+record-get':\n"
                " print(json.dumps({'record':{'record_id':'rec_1','fields':{'输出图':state['outputs'],'处理明细':state['detail'],'任务状态':state.get('status')}}}))\n"
                "else:\n"
                " raise SystemExit(9)\n",
                encoding="utf-8",
            )
            lark.chmod(0o755)
            doubao = root / "doubao_imagegen.py"
            doubao.write_text(
                "import pathlib, shutil, sys\n"
                "args = sys.argv[1:]\n"
                "shutil.copyfile(args[args.index('--image') + 1], args[args.index('--out') + 1])\n",
                encoding="utf-8",
            )
            env = {
                "OUTFIT_SWAP_LARK_CLI": str(lark),
                "OUTFIT_SWAP_DOUBAO_SCRIPT": str(doubao),
                "OUTFIT_SWAP_STATE_ROOT": str(root / "state"),
                "OUTFIT_SWAP_RUNS_ROOT": str(root / "runs"),
                "OUTFIT_TEST_REMOTE_STATE": str(state),
                "OUTFIT_TEST_LARK_TRACE": str(trace),
                "OUTFIT_TEST_IMAGE": str(image),
                "ARK_API_KEY": "test-key",
                "ARK_VISION_MODEL": "test-model",
            }
            stdout = io.StringIO()
            stderr = io.StringIO()
            with mock.patch.dict(os.environ, env, clear=False), mock.patch(
                "scripts.production_runtime._make_ark_client",
                return_value=_FakeArk(),
            ), redirect_stdout(stdout), redirect_stderr(stderr):
                code = main(["https://example.invalid/base/table"])

            self.assertEqual(code, 0, stderr.getvalue())
            self.assertEqual(json.loads(stdout.getvalue()), {
                "failed": 0, "selected": 1, "stopped": 0, "succeeded": 1,
            })
            remote = json.loads(state.read_text(encoding="utf-8"))
            self.assertEqual(remote["status"], ["成功"])
            self.assertEqual(len(remote["outputs"]), 1)
            calls = [json.loads(line) for line in trace.read_text().splitlines()]
            for call in calls:
                argv = call["argv"]
                for flag in ("--filter-json", "--output", "--file", "--json"):
                    if flag in argv:
                        value = argv[argv.index(flag) + 1].removeprefix("@")
                        self.assertFalse(Path(value).is_absolute())

    def test_terminal_write_failure_sets_the_global_stop(self) -> None:
        from scripts import production_runtime

        class Stop:
            stopped = False

            def set(self) -> None:
                self.stopped = True

        class Finalizer:
            def terminalize_record(self, _context: object, _status: str) -> None:
                raise RuntimeError("readback failed")

        stop = Stop()
        services = RecordServices(
            generator=object(), qc=object(), finalizer=Finalizer(),
            events=object(), stop_signal=stop,
        )
        context = RecordContext(Path.cwd(), "rec_1", ())
        with mock.patch.object(
            production_runtime, "run_record",
            return_value=RecordResult("rec_1", "success", 0),
        ), self.assertRaises(RuntimeError):
            production_runtime._terminal_worker(context, services)
        self.assertTrue(stop.stopped)


if __name__ == "__main__":
    unittest.main()
