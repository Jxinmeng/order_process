"""Agno 规则 Agent 适配器。

模型服务通过 OpenAI 兼容地址接入；可使用 DeepSeek 官网或百炼等服务。
"""

from __future__ import annotations

import re
import json
import os
from typing import Any

import httpx
from agno.agent import Agent
from agno.models.openai import OpenAIChat


class AgnoRuleAgent:
    """使用 Agno Agent 执行规则编译与语义任务。"""

    def __init__(self, api_key: str, model_id: str, base_url: str) -> None:
        self._model_id = model_id
        self.last_response_metadata = ""
        self.last_finish_reason: str | None = None
        self._system_instruction = (
            "你是严谨的订单规则编译器。遵守用户给出的字段白名单。"
            "除非请求 JSON，否则只返回可执行 Python 代码，不要使用 Markdown 代码围栏。"
        )
        # 使用 Agno 的 OpenAIChat 模型适配器。百炼为 OpenAI 兼容地址，因此不再
        # 需要旧版针对 DeepSeek 官网 API 的直接 OpenAI SDK 绕行。
        self._text_agent = self._build_agent(api_key, base_url, json_mode=False)
        self._json_agent = self._build_agent(api_key, base_url, json_mode=True)

    def _build_agent(self, api_key: str, base_url: str, *, json_mode: bool) -> Agent:
        model = OpenAIChat(
            id=self._model_id,
            api_key=api_key,
            base_url=base_url,
            timeout=float(os.getenv("DEEPSEEK_TIMEOUT_SECONDS", "300")),
            http_client=httpx.Client(timeout=float(os.getenv("DEEPSEEK_TIMEOUT_SECONDS", "300")), trust_env=False),
            # 百炼兼容接口仅接受 system/user/assistant 等标准角色。
            role_map={"system": "system", "user": "user", "assistant": "assistant", "tool": "tool", "model": "assistant"},
        )
        return Agent(
            name="Order Rule Compiler",
            model=model,
            # 使用项目原本已验证可用的 instructions 参数，兼容 Agno 2.5.0。
            instructions=[self._system_instruction] + (
                ["本次必须只输出合法 JSON 对象，不要输出解释或代码围栏。"] if json_mode else []
            ),
        )

    def run_text(self, prompt: str, *, json_output: bool = False) -> str:
        agent = self._json_agent if json_output else self._text_agent
        response = agent.run(prompt)
        content = str(response.content or "")
        self.last_finish_reason = str(getattr(response, "status", "completed"))
        self.last_response_metadata = json.dumps(
            {
                "finish_reason": self.last_finish_reason,
                "model": self._model_id,
                "provider": "Agno/OpenAIChat",
                "metrics": self._serializable(getattr(response, "metrics", None)),
            },
            ensure_ascii=False,
        )
        return re.sub(r"^```(?:python|json)?\s*|\s*```$", "", content.strip())

    @staticmethod
    def _serializable(value: Any) -> Any:
        """Agno 版本间 metrics 类型可能不同，审计失败不能影响主流程。"""
        try:
            json.dumps(value)
            return value
        except (TypeError, ValueError):
            return str(value) if value is not None else None

    def compile_rule(self, prompt: str) -> str:
        return self.run_text(prompt)

    def understand_json(self, prompt: str) -> str:
        return self.run_text(f"{prompt}\n只输出合法 JSON 对象。", json_output=True)
