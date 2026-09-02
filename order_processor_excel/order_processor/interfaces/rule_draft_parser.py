"""规则草稿模型输出的容错 JSON 解析。"""

from __future__ import annotations

import json
import re


def parse_rule_draft(raw: str) -> dict:
    """解析 JSON 对象，并兼容模型常见的 Markdown 代码围栏。"""
    text = raw.strip()
    if not text:
        raise ValueError(
            "模型没有返回规则草稿内容。请检查 DEEPSEEK_API_KEY、模型服务状态和日志中的 LLM 原始输出后重试"
        )
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text, flags=re.IGNORECASE).strip()
    try:
        result = json.loads(text)
    except json.JSONDecodeError as error:
        raise ValueError("模型返回的规则草稿不是合法 JSON，请重试；详细原始输出已写入运行日志") from error
    if not isinstance(result, dict):
        raise ValueError("模型返回的规则草稿必须是 JSON 对象，请重试")
    return result
