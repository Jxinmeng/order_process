"""DeepSeek 兼容接口适配器。

保留模块名是为了兼容既有装配代码；实际调用使用 OpenAI 兼容客户端，避免 Agno
将系统指令编码为 DeepSeek 不支持的 ``developer`` role。
"""

from __future__ import annotations

import re
import os

from openai import OpenAI


class AgnoRuleAgent:
    """使用 DeepSeek OpenAI 兼容接口执行规则编译与语义任务。"""

    def __init__(self, api_key: str, model_id: str, base_url: str) -> None:
        # 规则包生成可能包含多条业务规则；给模型调用独立的超时和重试配置，
        # 避免上游短暂波动直接导致管理员页面失败。
        timeout = float(os.getenv("DEEPSEEK_TIMEOUT_SECONDS", "120"))
        self._client = OpenAI(api_key=api_key, base_url=base_url, timeout=timeout, max_retries=2)
        self._model_id = model_id
        self._system_instruction = (
            "你是严谨的订单规则编译器。遵守用户给出的字段白名单。"
            "除非请求 JSON，否则只返回可执行 Python 代码，不要使用 Markdown 代码围栏。"
        )

    def run_text(self, prompt: str) -> str:
        response = self._client.chat.completions.create(
            model=self._model_id,
            temperature=0,
            max_tokens=int(os.getenv("DEEPSEEK_MAX_TOKENS", "2400")),
            messages=[
                {"role": "system", "content": self._system_instruction},
                {"role": "user", "content": prompt},
            ],
        )
        text = response.choices[0].message.content or ""
        return re.sub(r"^```(?:python|json)?\s*|\s*```$", "", text.strip())

    def compile_rule(self, prompt: str) -> str:
        return self.run_text(prompt)

    def understand_json(self, prompt: str) -> str:
        return self.run_text(f"{prompt}\n只输出合法 JSON 对象。")
