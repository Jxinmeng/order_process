"""将邮件、图片和文档转换为受输入字段目录约束的订单 JSON。"""

from __future__ import annotations

import json
import io
import http.client
import logging
import os
import re
import tempfile
import time
import uuid
from dataclasses import dataclass
from datetime import date, datetime
from email import policy
from email.parser import BytesParser
from pathlib import Path
from typing import Any
from urllib import request
from urllib.parse import urlsplit
from xml.etree import ElementTree as ET
from zipfile import ZipFile

import httpx
from agno.agent import Agent
from agno.models.openai import OpenAIChat
from PIL import Image, ImageSequence

from order_processor.infrastructure.persistence.rule_repository import RuleRepository
from order_processor.infrastructure.excel.excel_reader import ExcelReader
from order_processor.shared.prompt_loader import PromptLoader

# 使用 Uvicorn 的错误日志通道，确保在当前 AgentOS/Uvicorn 控制台的 INFO 级别下可见。
logger = logging.getLogger("uvicorn.error")


class MinerUClient:
    """MinerU HTTP 适配器。

    MINERU_API_URL 应指向部署方提供的“上传文件并返回 markdown/text”的接口。
    兼容常见的 {markdown}, {text} 或 {data:{markdown/text}} 返回结构。
    """

    def extract_text(self, source: Path) -> str:
        logger.info("开始解析非结构化文件：%s", source.name)
        if source.suffix.lower() in {".tif", ".tiff"}:
            return self._extract_tiff(source)
        endpoint = os.getenv("MINERU_API_URL")
        if not endpoint:
            raise RuntimeError("未配置 MINERU_API_URL，PDF、图片和 Office 文件无法解析")
        # MinerU 官网的 Agent Lightweight API 不是 multipart /parse 接口，
        # 而是签名上传加异步轮询流程；按官方 URL 自动选择该适配器。
        if endpoint.rstrip().endswith("/api/v1/agent/parse/file"):
            return self._extract_official_agent(source, endpoint.rstrip())
        # 标准版使用 Token 和 /file-urls/batch；该地址是你当前 .env 的配置。
        if endpoint.rstrip().endswith("/api/v4"):
            return self._extract_official_standard(source, endpoint.rstrip())
        boundary = f"----order-processor-{uuid.uuid4().hex}"
        body = (
            f"--{boundary}\r\nContent-Disposition: form-data; name=\"file\"; filename=\"{source.name}\"\r\n"
            f"Content-Type: application/octet-stream\r\n\r\n"
        ).encode() + source.read_bytes() + f"\r\n--{boundary}--\r\n".encode()
        headers = {"Content-Type": f"multipart/form-data; boundary={boundary}"}
        if os.getenv("MINERU_API_KEY"):
            headers["Authorization"] = f"Bearer {os.environ['MINERU_API_KEY']}"
        try:
            with self._urlopen(request.Request(endpoint, body, headers), timeout=180) as response:
                payload = json.loads(response.read().decode("utf-8"))
        except Exception as error:
            raise RuntimeError(f"MinerU 解析失败：{error}") from error
        text = self._find_text(payload)
        if not text:
            raise RuntimeError("MinerU 返回中未找到 markdown 或 text 内容")
        logger.info("非结构化文件解析完成：%s", source.name)
        return text

    def _extract_tiff(self, source: Path) -> str:
        """将单页或多页 TIFF 转为 PDF，再复用同一 MinerU 流程。"""
        try:
            with Image.open(source) as image, tempfile.TemporaryDirectory(prefix="order-tiff-") as temp_dir:
                pages = [frame.convert("RGB") for frame in ImageSequence.Iterator(image)]
                if not pages:
                    raise RuntimeError("TIFF 中没有可解析的图像页")
                converted = Path(temp_dir) / f"{source.stem}.pdf"
                pages[0].save(converted, "PDF", save_all=True, append_images=pages[1:], resolution=200.0)
                return self.extract_text(converted)
        except RuntimeError:
            raise
        except Exception as error:
            raise RuntimeError(f"TIFF 转 PDF 失败：{error}") from error

    def _extract_official_standard(self, source: Path, base_url: str) -> str:
        """调用 MinerU 标准版 API，并从结果 ZIP 中读取 full.md。"""
        token = os.getenv("MINERU_API_KEY")
        if not token:
            raise RuntimeError("MinerU 标准版需要配置 MINERU_API_KEY")
        data_id = uuid.uuid4().hex
        headers = {"Authorization": f"Bearer {token}"}
        created = self._json_request(
            f"{base_url}/file-urls/batch",
            {
                "files": [{"name": source.name, "data_id": data_id}],
                "model_version": os.getenv("MINERU_MODEL_VERSION", "vlm"),
                "language": os.getenv("MINERU_LANGUAGE", "ch"),
                "enable_table": True,
                "enable_formula": True,
            },
            headers=headers,
        )
        upload_data = self._require_success(created, "申请标准版上传地址")
        batch_id, urls = upload_data.get("batch_id"), upload_data.get("file_urls")
        if not batch_id or not isinstance(urls, list) or not urls:
            raise RuntimeError("MinerU 标准版未返回 batch_id 或上传地址")
        try:
            # 官方签名 URL 明确要求上传时不要附带 Content-Type/Authorization。
            self._put_signed_file(str(urls[0]), source.read_bytes())
        except Exception as error:
            raise RuntimeError(f"上传文件到 MinerU 标准版失败：{error}") from error

        deadline = time.monotonic() + int(os.getenv("MINERU_TIMEOUT_SECONDS", "600"))
        result_url = f"{base_url}/extract-results/batch/{batch_id}"
        while time.monotonic() < deadline:
            status = self._json_request(result_url, method="GET", headers=headers)
            result = self._require_success(status, "查询标准版解析任务")
            items = result.get("extract_result")
            item = next((entry for entry in items or [] if entry.get("data_id") == data_id), (items or [{}])[0])
            state = item.get("state")
            if state == "done":
                zip_url = item.get("full_zip_url")
                if not zip_url:
                    raise RuntimeError("MinerU 标准版任务已完成，但未返回 full_zip_url")
                return self._download_standard_markdown(str(zip_url))
            if state == "failed":
                raise RuntimeError(f"MinerU 标准版解析失败：{item.get('err_msg', '未知错误')}")
            time.sleep(3)
        raise RuntimeError(f"MinerU 标准版解析超时；批次 ID：{batch_id}")

    @staticmethod
    def _download_standard_markdown(zip_url: str) -> str:
        try:
            with MinerUClient._urlopen(zip_url, timeout=180) as response:
                archive = ZipFile(io.BytesIO(response.read()))
                markdown_name = next((name for name in archive.namelist() if name.endswith("/full.md") or name == "full.md"), None)
                if not markdown_name:
                    raise RuntimeError("标准版结果 ZIP 中未找到 full.md")
                return archive.read(markdown_name).decode("utf-8")
        except RuntimeError:
            raise
        except Exception as error:
            raise RuntimeError(f"下载 MinerU 标准版结果失败：{error}") from error

    @staticmethod
    def _put_signed_file(upload_url: str, content: bytes) -> None:
        """向 OSS 签名地址上传，不让 urllib 自动添加会破坏签名的 Content-Type。"""
        target = urlsplit(upload_url)
        if target.scheme not in {"https", "http"} or not target.netloc:
            raise RuntimeError("MinerU 返回的签名上传地址无效")
        connection_type = http.client.HTTPSConnection if target.scheme == "https" else http.client.HTTPConnection
        connection = connection_type(target.netloc, timeout=180)
        try:
            request_path = (target.path or "/") + (f"?{target.query}" if target.query else "")
            connection.request("PUT", request_path, body=content, headers={"Content-Length": str(len(content))})
            response = connection.getresponse()
            if response.status not in (200, 201):
                detail = response.read().decode("utf-8", errors="replace")[:500]
                raise RuntimeError(f"HTTP {response.status}: {detail}")
            response.read()
        finally:
            connection.close()

    def _extract_official_agent(self, source: Path, endpoint: str) -> str:
        """调用 MinerU 官网无需 Token 的轻量文件解析接口。"""
        settings = {
            "file_name": source.name,
            "language": os.getenv("MINERU_LANGUAGE", "ch"),
            "enable_table": True,
            "enable_formula": True,
            "is_ocr": os.getenv("MINERU_FORCE_OCR", "false").lower() == "true",
        }
        created = self._json_request(endpoint, settings)
        data = self._require_success(created, "申请上传地址")
        task_id, upload_url = data.get("task_id"), data.get("file_url")
        if not task_id or not upload_url:
            raise RuntimeError("MinerU 未返回 task_id 或签名上传地址")
        try:
            self._put_signed_file(str(upload_url), source.read_bytes())
        except Exception as error:
            raise RuntimeError(f"上传文件到 MinerU 失败：{error}") from error

        timeout_seconds = int(os.getenv("MINERU_TIMEOUT_SECONDS", "300"))
        deadline = time.monotonic() + timeout_seconds
        task_url = f"{endpoint.rsplit('/file', 1)[0]}/{task_id}"
        while time.monotonic() < deadline:
            status = self._json_request(task_url, method="GET")
            result = self._require_success(status, "查询解析任务")
            state = result.get("state")
            if state == "done":
                markdown_url = result.get("markdown_url")
                if not markdown_url:
                    raise RuntimeError("MinerU 任务已完成，但未返回 markdown_url")
                try:
                    with self._urlopen(markdown_url, timeout=120) as response:
                        return response.read().decode("utf-8")
                except Exception as error:
                    raise RuntimeError(f"下载 MinerU Markdown 失败：{error}") from error
            if state == "failed":
                raise RuntimeError(f"MinerU 解析失败：{result.get('err_msg', '未知错误')}")
            time.sleep(3)
        raise RuntimeError(f"MinerU 解析超时（{timeout_seconds} 秒）；任务 ID：{task_id}")

    @staticmethod
    def _json_request(url: str, payload: dict | None = None, method: str = "POST",
                      headers: dict[str, str] | None = None) -> dict[str, Any]:
        headers = dict(headers or {})
        if payload is not None:
            headers["Content-Type"] = "application/json"
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8") if payload is not None else None
        try:
            with MinerUClient._urlopen(request.Request(url, data, headers, method=method), timeout=120) as response:
                result = json.loads(response.read().decode("utf-8"))
        except Exception as error:
            raise RuntimeError(f"MinerU API 请求失败：{error}") from error
        if not isinstance(result, dict):
            raise RuntimeError("MinerU API 返回不是 JSON 对象")
        return result

    @staticmethod
    def _require_success(payload: dict[str, Any], action: str) -> dict[str, Any]:
        if payload.get("code") != 0 or not isinstance(payload.get("data"), dict):
            raise RuntimeError(f"MinerU {action}失败：{payload.get('msg', payload)}")
        return payload["data"]

    @staticmethod
    def _urlopen(target: str | request.Request, timeout: int):
        """MinerU 直连请求，避免 Windows 系统代理导致上传或轮询卡住。"""
        return request.build_opener(request.ProxyHandler({})).open(target, timeout=timeout)

    @staticmethod
    def _find_text(payload: Any) -> str:
        if isinstance(payload, str):
            return payload
        if not isinstance(payload, dict):
            return ""
        for key in ("markdown", "text", "content"):
            if isinstance(payload.get(key), str):
                return payload[key]
        data = payload.get("data") or payload.get("result")
        return MinerUClient._find_text(data)


@dataclass(frozen=True)
class SourcePart:
    """一个可独立抽取的来源片段，例如邮件正文或单个附件。"""

    name: str
    kind: str
    text: str


class SourceTextReader:
    """文本型输入本地读取；其它格式交给 MinerU，避免在规则层理解文件格式。"""

    def __init__(self, mineru: MinerUClient | None = None) -> None:
        self.mineru = mineru or MinerUClient()

    def read(self, path: str | Path) -> str:
        """兼容仅测试 MinerU 的纯文本结果；邮件会以分隔标题展示各部分。"""
        parts = self.read_parts(path)
        return "\n\n".join(f"## {part.kind}: {part.name}\n{part.text}" for part in parts)

    def read_parts(self, path: str | Path) -> list[SourcePart]:
        """返回独立来源片段，绝不把邮件正文和附件混成一个模型输入。"""
        source = Path(path)
        if not source.is_file():
            raise FileNotFoundError(f"输入文件不存在: {source}")
        suffix = source.suffix.lower()
        if suffix in {".txt", ".md", ".json"}:
            return [SourcePart(source.name, "文件正文", source.read_text(encoding="utf-8"))]
        if suffix == ".eml":
            return self._read_email_parts(source)
        kind = "TIFF 文件" if suffix in {".tif", ".tiff"} else "文件"
        return [SourcePart(source.name, kind, self.mineru.extract_text(source))]

    def read_prepared_text(self, path: str | Path) -> str:
        """读取已准备的文本，不调用 MinerU；用于单独测试 JSON 抽取阶段。"""
        source = Path(path)
        if source.suffix.lower() in {".md", ".txt"}:
            return source.read_text(encoding="utf-8")
        if source.suffix.lower() == ".docx":
            try:
                with ZipFile(source) as archive:
                    root = ET.fromstring(archive.read("word/document.xml"))
                namespace = {"w": "http://schemas.openxmlformats.org/wordprocessingml/2006/main"}
                return "\n".join(node.text or "" for node in root.findall(".//w:t", namespace)).strip()
            except Exception as error:
                raise RuntimeError(f"读取 Word 文本失败：{error}") from error
        raise RuntimeError("准备文件 → JSON 仅支持 .md、.txt、.docx、.xlsx 和 .json")

    def _read_email_parts(self, source: Path) -> list[SourcePart]:
        message = BytesParser(policy=policy.default).parsebytes(source.read_bytes())
        body = [
            f"发件人: {message.get('From', '')}", f"收件人: {message.get('To', '')}",
            f"主题: {message.get('Subject', '')}", f"邮件时间: {message.get('Date', '')}",
        ]
        for part in message.walk():
            if part.get_content_type() == "text/plain" and not part.get_content_disposition():
                body.append(part.get_content())
        results = [SourcePart("邮件正文", "邮件正文", "\n".join(str(item) for item in body if item))]
        # 附件保留独立来源标记；上层会将邮件正文与所有附件合并为同一批次模型输入。
        attachments = [part for part in message.iter_attachments() if part.get_payload(decode=True)]
        if attachments:
            with tempfile.TemporaryDirectory(prefix="order-email-") as temp_dir:
                for index, part in enumerate(attachments, 1):
                    name = Path(part.get_filename() or f"attachment_{index}").name
                    attachment = Path(temp_dir) / name
                    attachment.write_bytes(part.get_payload(decode=True))
                    if attachment.suffix.lower() in {".txt", ".md"}:
                        text = attachment.read_text(encoding="utf-8", errors="replace")
                    elif attachment.suffix.lower() == ".docx":
                        text = self.read_prepared_text(attachment)
                    elif attachment.suffix.lower() == ".xlsx":
                        text = json.dumps(ExcelReader.read(str(attachment)), ensure_ascii=False, indent=2)
                    else:
                        text = self.mineru.extract_text(attachment)
                    results.append(SourcePart(name, "邮件附件", text))
        return results


class StructuredOrderExtractor:
    """Qwen 结构化抽取：模型只能输出当前 input_fields 中声明的中文字段名。"""

    def __init__(self, repository: RuleRepository, client: Any | None = None) -> None:
        self.repository = repository
        self.client = client
        self._cached_agent: Agent | None = None
        self._cached_max_tokens: int = 0
        self.last_call_metrics: dict[str, Any] = {"called": False}
        self.order_batch_prompt_template = PromptLoader.load_order_batch_extraction_prompt()

    def extract(self, source_text: str) -> list[dict[str, Any]]:
        fields = self._fields()
        if not fields:
            raise RuntimeError("输入字段目录为空；请先在管理后台配置 input_fields")
        # 客户代码必须由规则库按客户名称补全，不能让模型把原单的公司代码误认为客户代码。
        extraction_fields = [field for field in fields if field["name"] != "客户代码"]
        prompt = (
            self._order_batch_prompt("仅输出合法 JSON，格式为 {\"orders\":[{字段名:值}]}。") + "\n"
            + f"允许字段（不得新增字段）：{json.dumps(extraction_fields, ensure_ascii=False)}\n"
            + "不得输出 Markdown 或解释。\n原文：\n" + source_text
        )
        content = self._qwen_completion(prompt, "订单字段抽取")
        orders = self._validate(content, {item["name"] for item in extraction_fields}, self._field_types())
        return self._assign_missing_original_order_numbers(self._assign_customer_codes(orders))

    def validate_payload(self, payload: Any) -> list[dict[str, Any]]:
        """直接上传的标准 JSON 不再调用模型，但仍执行字段白名单校验。"""
        fields = [field for field in self._fields() if field["name"] != "客户代码"]
        orders = self._validate(json.dumps(payload, ensure_ascii=False), {item["name"] for item in fields}, self._field_types())
        return self._assign_missing_original_order_numbers(self._assign_customer_codes(orders))

    def _assign_customer_codes(self, orders: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """仅以抽取出的客户名称查询 rules.db，补全客户代码。"""
        for order in orders:
            customer_name = str(order.get("客户名称") or "").strip()
            customer_code = self.repository.find_customer_code_by_name(customer_name)
            if customer_code:
                order["客户代码"] = customer_code
            elif customer_name:
                logger.warning("客户名称未在 rules.db 中唯一匹配，未填客户代码：%s", customer_name)
        return orders

    @staticmethod
    def _assign_missing_original_order_numbers(orders: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """原文未提供明细序号时，按同一订单的输出顺序补齐 1、2、3……。"""
        groups: dict[str, list[dict[str, Any]]] = {}
        group_fields = ("合同编号", "单据编号", "标准采购订单", "用户合同号")
        for row in orders:
            group_key = next((str(row.get(field)).strip() for field in group_fields if str(row.get(field) or "").strip()), "__batch__")
            groups.setdefault(group_key, []).append(row)
        for rows in groups.values():
            if any(str(row.get("原始订单序号") or "").strip() for row in rows):
                continue
            for index, row in enumerate(rows, 1):
                row["原始订单序号"] = str(index)
        return orders

    def _order_batch_prompt(self, output_contract: str) -> str:
        return self.order_batch_prompt_template.format(output_contract=output_contract)

    def _qwen_completion(self, prompt: str, stage: str) -> str:
        """统一通过 Agno Agent 调用 Qwen，并把网络错误转换为页面可见的业务错误。"""
        model = os.getenv("QWEN_MODEL", "qwen3.6")
        try:
            max_tokens = int(os.getenv("QWEN_MAX_TOKENS", "8192"))
        except ValueError as error:
            raise RuntimeError("QWEN_MAX_TOKENS 必须是整数，例如 8192") from error
        try:
            timeout_seconds = float(os.getenv("QWEN_TIMEOUT_SECONDS", "300"))
        except ValueError as error:
            raise RuntimeError("QWEN_TIMEOUT_SECONDS 必须是秒数，例如 120") from error
        logger.info("Qwen 请求已发出：阶段=%s，模型=%s，输入字符数=%d，最大输出=%d，超时=%ds", stage, model, len(prompt), max_tokens, int(timeout_seconds))
        import concurrent.futures
        try:
            agent = self.client or self._get_or_create_agent(max_tokens)
        except Exception as error:
            logger.exception("Qwen Agent 创建失败：阶段=%s，错误类型=%s", stage, type(error).__name__)
            raise RuntimeError(f"Qwen Agent 创建失败（{type(error).__name__}）：{error}") from error
        call_started_at = time.perf_counter()
        try:
            with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
                future = executor.submit(agent.run, prompt)
                response = future.result(timeout=timeout_seconds + 30)
        except concurrent.futures.TimeoutError:
            elapsed_seconds = time.perf_counter() - call_started_at
            logger.error("Qwen 请求超时：阶段=%s，耗时=%.2fs，超时阈值=%ds，输入字符数=%d", stage, elapsed_seconds, int(timeout_seconds) + 30, len(prompt))
            raise RuntimeError(
                f"Qwen 请求超时（{int(timeout_seconds) + 30}秒无响应）：阶段={stage}。"
                f"输入文本有 {len(prompt)} 字符，可能超出模型处理能力；请减少上传文件数量或拆分批次。"
            )
        except Exception as error:
            elapsed_seconds = time.perf_counter() - call_started_at
            logger.exception("Qwen 请求失败：阶段=%s，耗时=%.2fs，错误类型=%s", stage, elapsed_seconds, type(error).__name__)
            raise RuntimeError(f"Qwen 请求失败（{type(error).__name__}）：{error}") from error
        elapsed_seconds = time.perf_counter() - call_started_at
        metrics = getattr(response, "metrics", None)
        input_tokens = getattr(metrics, "input_tokens", None) if metrics is not None else None
        output_tokens = getattr(metrics, "output_tokens", None) if metrics is not None else None
        total_tokens = getattr(metrics, "total_tokens", None) if metrics is not None else None
        usage_returned = any(value not in (None, 0) for value in (input_tokens, output_tokens, total_tokens))
        self.last_call_metrics = {
            "called": True,
            "elapsed_seconds": round(elapsed_seconds, 3),
            "input_tokens": input_tokens if usage_returned else None,
            "output_tokens": output_tokens if usage_returned else None,
            "total_tokens": total_tokens if usage_returned else None,
        }
        logger.info(
            "Qwen 调用完成：阶段=%s，耗时=%.2fs，输入Token=%s，输出Token=%s，总Token=%s",
            stage, elapsed_seconds,
            input_tokens if usage_returned else "未返回",
            output_tokens if usage_returned else "未返回",
            total_tokens if usage_returned else "未返回",
        )
        content = str(response.content or "").strip()
        if not content:
            raise RuntimeError(f"Qwen 请求未返回内容：阶段={stage}；请检查上方 Agno/模型提供方错误日志")
        logger.info("Qwen 响应已收到：阶段=%s", stage)
        return content

    def _get_or_create_agent(self, max_tokens: int) -> Agent:
        if self._cached_agent is not None and self._cached_max_tokens == max_tokens:
            return self._cached_agent
        agent = self._agent(max_tokens)
        self._cached_agent = agent
        self._cached_max_tokens = max_tokens
        return agent

    def _fields(self) -> list[dict[str, str]]:
        fields = [
            {"name": name, "type": data_type}
            for _, name, data_type, enabled in self.repository.field_catalog()["inputs"] if enabled
        ]
        if not fields:
            raise RuntimeError("输入字段目录为空；请先在管理后台配置 input_fields")
        return fields

    def _field_types(self) -> dict[str, str]:
        return {
            name: data_type for _, name, data_type, enabled in self.repository.field_catalog()["inputs"] if enabled
        }

    @staticmethod
    def _agent(max_tokens: int) -> Agent:
        api_key = os.getenv("QWEN_API_KEY")
        if not api_key:
            raise RuntimeError("未配置 QWEN_API_KEY，无法执行结构化订单抽取")
        if any(ord(char) > 127 for char in api_key):
            raise RuntimeError("QWEN_API_KEY 包含非 ASCII 字符；请在 .env 中填入百炼控制台创建的真实 API Key，不要填“你的 Key”等中文占位文本")
        try:
            timeout_seconds = float(os.getenv("QWEN_TIMEOUT_SECONDS", "300"))
        except ValueError as error:
            raise RuntimeError("QWEN_TIMEOUT_SECONDS 必须是秒数，例如 120") from error
        model = OpenAIChat(
            id=os.getenv("QWEN_MODEL", "qwen3.6"),
            api_key=api_key,
            base_url=os.getenv("QWEN_BASE_URL", "https://dashscope.aliyuncs.com/compatible-mode/v1"),
            timeout=timeout_seconds,
            max_tokens=max_tokens,
            # Windows/Agno 默认读取系统代理；该代理会使百炼请求一直等不到响应。
            # 百炼直连已验证可达，因此此处明确不继承系统代理配置。
            http_client=httpx.Client(timeout=timeout_seconds, trust_env=False),
            # 百炼兼容接口不支持 OpenAI 新增的 developer 角色。
            role_map={"system": "system", "user": "user", "assistant": "assistant", "tool": "tool", "model": "assistant"},
        )
        return Agent(
            name="Order Structured Extractor",
            model=model,
            instructions=["只返回用户请求的合法 JSON，不要使用 Markdown 代码围栏或解释。"],
        )

    @staticmethod
    def _validate(content: str, allowed_fields: set[str], field_types: dict[str, str] | None = None) -> list[dict[str, Any]]:
        content = re.sub(r"^```(?:json)?\s*|\s*```$", "", content.strip(), flags=re.IGNORECASE)
        try:
            payload = json.loads(content)
        except json.JSONDecodeError as error:
            raise RuntimeError(f"Qwen 未返回合法 JSON：{error}") from error
        orders = payload.get("orders") if isinstance(payload, dict) else payload
        if not isinstance(orders, list) or not orders:
            raise RuntimeError("Qwen 返回 JSON 中缺少非空 orders 数组")
        normalized = [StructuredOrderExtractor._normalize_row(
            {key: value for key, value in row.items() if key in allowed_fields}, field_types or {}
        ) for row in orders if isinstance(row, dict)]
        if not normalized:
            raise RuntimeError("orders 必须至少包含一个 JSON 对象")
        return normalized

    @staticmethod
    def _normalize_row(row: dict[str, Any], field_types: dict[str, str]) -> dict[str, Any]:
        result = dict(row)
        for field, data_type in field_types.items():
            if data_type == "date" and field in result:
                result[field] = StructuredOrderExtractor._format_date(result[field], field)
        return result

    @staticmethod
    def _format_date(value: Any, field: str) -> str:
        if value is None or str(value).strip() == "":
            return ""
        if isinstance(value, datetime):
            return value.strftime("%Y%m%d")
        if isinstance(value, date):
            return value.strftime("%Y%m%d")
        text = str(value).strip()
        for pattern in ("%Y%m%d", "%Y-%m-%d", "%Y/%m/%d", "%Y.%m.%d", "%Y年%m月%d日", "%Y-%m-%d %H:%M:%S"):
            try:
                return datetime.strptime(text, pattern).strftime("%Y%m%d")
            except ValueError:
                continue
        raise RuntimeError(f"日期字段“{field}”无法转换为 yyyyMMdd：{value}")


class SourceIngestionService:
    """将同一批来源合并为一次模型抽取，并返回给现有规则工作流。"""

    def __init__(self, repository: RuleRepository, archive_dir: str | Path = "data/extractions") -> None:
        self.reader = SourceTextReader()
        self.extractor = StructuredOrderExtractor(repository)
        self.archive_dir = Path(archive_dir)

    def ingest_batch(
        self,
        source_paths: list[str | Path],
        *,
        prepared: bool = False,
        archive_stem: str | None = None,
        intermediate_markdown_path: str | Path | None = None,
    ) -> tuple[list[dict[str, Any]], str]:
        """一次上传的一组文件只调用一次 Qwen，可跨页、跨附件组成同一订单。"""
        sources = [Path(path) for path in source_paths]
        if not sources:
            raise RuntimeError("至少需要一个来源文件")
        segments: list[dict[str, str]] = []
        existing_orders: list[dict[str, Any]] = []
        for source in sources:
            suffix = source.suffix.lower()
            logger.info("准备批次来源：%s", source.name)
            if suffix == ".json":
                try:
                    existing_orders.extend(self.extractor.validate_payload(json.loads(source.read_text(encoding="utf-8"))))
                except json.JSONDecodeError as error:
                    raise RuntimeError(f"输入 JSON 格式错误：{error}") from error
                continue
            # Office 文档本地读取；PDF/图片及其它非结构化附件才交给 MinerU。
            # 不论来源如何解析，都会加入同一个 batch_text 并只调用一次 Qwen。
            if suffix == ".xlsx":
                text = json.dumps(ExcelReader.read(str(source)), ensure_ascii=False, indent=2)
                segments.append({"source_name": source.name, "source_type": "Excel 表格", "text": text})
            elif suffix == ".docx":
                segments.append({"source_name": source.name, "source_type": "Word 文档", "text": self.reader.read_prepared_text(source)})
            elif prepared:
                segments.append({"source_name": source.name, "source_type": "准备文件", "text": self.reader.read_prepared_text(source)})
            else:
                segments.extend(
                    {"source_name": f"{source.name} / {part.name}", "source_type": part.kind, "text": part.text}
                    for part in self.reader.read_parts(source)
                )

        # 以清晰的来源边界拼接，模型可关联多页内容但不会误把文件名当订单字段。
        batch_text = "\n\n".join(
            f"===== 来源开始：{item['source_name']}（{item['source_type']}）=====\n{item['text']}\n===== 来源结束 ====="
            for item in segments
        )
        if intermediate_markdown_path is not None:
            intermediate = Path(intermediate_markdown_path)
            intermediate.parent.mkdir(parents=True, exist_ok=True)
            intermediate.write_text(batch_text, encoding="utf-8")
            logger.info("已保存批次中间文本：%s", intermediate)
        logger.info("批次文本已准备完成：%d 个来源片段；开始调用 Qwen", len(segments))
        extracted_orders = self.extractor.extract(batch_text) if batch_text else []
        logger.info("Qwen 抽取完成：得到 %d 条订单", len(extracted_orders))
        orders = existing_orders + extracted_orders
        if not orders:
            raise RuntimeError("批次中未得到可用订单；请检查文件内容或 JSON")
        self.archive_dir.mkdir(parents=True, exist_ok=True)
        prefix = "prepared_batch" if prepared else "batch"
        safe_stem = Path(archive_stem or f"{prefix}_{uuid.uuid4().hex}").stem
        archive = self.archive_dir / f"{safe_stem}.json"
        archive.write_text(json.dumps({
            "source_files": [source.name for source in sources],
            "source_stage": "prepared" if prepared else "raw",
            "model_call_count": 1 if batch_text else 0,
            "segments": [{key: value for key, value in item.items() if key != "text"} for item in segments],
            "orders": orders,
        }, ensure_ascii=False, indent=2), encoding="utf-8")
        return orders, str(archive)
