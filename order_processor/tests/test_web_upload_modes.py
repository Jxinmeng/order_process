import unittest
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from order_processor.interfaces import web
from order_processor.interfaces.web import register_web_ui, validate_upload_mode


class WebUploadModeTests(unittest.TestCase):
    def test_reviewed_json_mode_rejects_raw_documents(self) -> None:
        with self.assertRaises(HTTPException) as raised:
            validate_upload_mode("process", [".pdf"])

        self.assertEqual(400, raised.exception.status_code)

    def test_full_process_mode_accepts_all_raw_source_types(self) -> None:
        validate_upload_mode(
            "full_process",
            [
                ".pdf", ".png", ".jpg", ".jpeg", ".webp", ".tif", ".tiff",
                ".eml", ".docx", ".txt", ".md", ".xlsx", ".json",
            ],
        )

    def test_home_submits_distinct_modes_for_reviewed_and_raw_files(self) -> None:
        with TemporaryDirectory() as project_dir:
            app = FastAPI()
            register_web_ui(app, Path(project_dir))

            response = TestClient(app).get("/")

        self.assertEqual(200, response.status_code)
        self.assertIn("value='process' type='submit'>JSON → 执行订单规则", response.text)
        self.assertIn("value='full_process' type='submit'>原始文件 → 完整订单处理", response.text)
        self.assertIn("下载 MinerU 中间结果", response.text)

    def test_full_process_saves_and_returns_intermediate_artifacts(self) -> None:
        class FakeIngestionService:
            def __init__(self, repository, archive_dir):
                self.archive_dir = Path(archive_dir)

            def ingest_batch(self, source_paths, **kwargs):
                markdown_path = Path(kwargs["intermediate_markdown_path"])
                markdown_path.parent.mkdir(parents=True, exist_ok=True)
                markdown_path.write_text("parsed source", encoding="utf-8")
                self.archive_dir.mkdir(parents=True, exist_ok=True)
                json_path = self.archive_dir / f"{kwargs['archive_stem']}.json"
                json_path.write_text(json.dumps({"orders": [{"型号": "A"}]}), encoding="utf-8")
                return [{"型号": "A"}], str(json_path)

        class FakeProcessOrders:
            @staticmethod
            def execute_rows(rows, output_path):
                return {
                    "success": True, "total": 1, "success_count": 1, "failed_count": 0,
                    "output_files": [output_path],
                }

        with TemporaryDirectory() as project_dir:
            root = Path(project_dir)
            app = FastAPI()
            register_web_ui(app, root)
            with (
                patch.object(web, "SourceIngestionService", FakeIngestionService),
                patch.object(web, "build_process_orders", return_value=FakeProcessOrders()),
                patch.object(web, "load_project_env"),
            ):
                response = TestClient(app).post(
                    "/ui/process",
                    data={"mode": "full_process", "output_name": "批次A.xlsx"},
                    files={"files": ("order.txt", b"raw order", "text/plain")},
                )

            body = response.json()
            self.assertEqual(200, response.status_code)
            self.assertEqual("/ui/extractions/批次A.json", body["extraction_download_url"])
            self.assertEqual("/ui/mineru-outputs/批次A.md", body["mineru_download_url"])
            self.assertTrue((root / "data" / "test" / "extractions" / "批次A.json").is_file())
            self.assertTrue((root / "data" / "test" / "mineru_outputs" / "批次A.md").is_file())

    def test_unknown_mode_is_rejected(self) -> None:
        with self.assertRaises(HTTPException) as raised:
            validate_upload_mode("unknown", [".pdf"])

        self.assertEqual(400, raised.exception.status_code)
