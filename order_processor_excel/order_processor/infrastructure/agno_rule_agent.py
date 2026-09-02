"""DeepSeek 兼容接口适配器。

保留模块名是为了兼容既有装配代码；实际调用使用 OpenAI 兼容客户端，避免 Agno
将系统指令编码为 DeepSeek 不支持的 ``developer`` role。
"""

from __future__ import annotations

import re
import os
import json

from openai import OpenAI


class AgnoRuleAgent:
    """使用 DeepSeek OpenAI 兼容接口执行规则编译与语义任务。"""

    def __init__(self, api_key: str, model_id: str, base_url: str) -> None:
        # 规则包生成可能包含多条业务规则；给模型调用独立的超时和重试配置，
        # 避免上游短暂波动直接导致管理员页面失败。
        timeout = float(os.getenv("DEEPSEEK_TIMEOUT_SECONDS", "120"))
        self._client = OpenAI(api_key=api_key, base_url=base_url, timeout=timeout, max_retries=2)
        self._model_id = model_id
        self.last_response_metadata = ""
        self.last_finish_reason: str | None = None
        self._system_instruction = (
            "你是严谨的订单规则编译器。遵守用户给出的字段白名单。"
            "除非请求 JSON，否则只返回可执行 Python 代码，不要使用 Markdown 代码围栏。"
        )

    def run_text(self, prompt: str, *, json_output: bool = False) -> str:
        request = {
            "model": self._model_id,
            "temperature": 0,
            # 一份 Excel 规则表通常会拆成十余条规则，每条 action 都需要完整说明；
            # 2400 很容易在 JSON 对象尚未闭合时被截断。
            "max_tokens": int(os.getenv("DEEPSEEK_MAX_TOKENS", "8192")),
            "messages": [
                {"role": "system", "content": self._system_instruction},
                {"role": "user", "content": prompt},
            ],
        }
        if json_output:
            # DeepSeek V4 默认开启思考模式。规则草稿需要可直接解析的 JSON，
            # 因此显式关闭思考并使用服务端 JSON 输出约束。
            request["response_format"] = {"type": "json_object"}
            request["extra_body"] = {"thinking": {"type": "disabled"}}
        response = self._client.chat.completions.create(**request)
        choice = response.choices[0]
        usage = response.usage
        self.last_finish_reason = choice.finish_reason
        self.last_response_metadata = json.dumps(
            {
                "finish_reason": choice.finish_reason,
                "model": response.model,
                "prompt_tokens": getattr(usage, "prompt_tokens", None),
                "completion_tokens": getattr(usage, "completion_tokens", None),
                "total_tokens": getattr(usage, "total_tokens", None),
                "reasoning_tokens": getattr(getattr(usage, "completion_tokens_details", None), "reasoning_tokens", None),
            },
            ensure_ascii=False,
        )
        text = choice.message.content or ""
        return re.sub(r"^```(?:python|json)?\s*|\s*```$", "", text.strip())

    def compile_rule(self, prompt: str) -> str:
        return self.run_text(prompt)

    def understand_json(self, prompt: str) -> str:
        return self.run_text(f"{prompt}\n只输出合法 JSON 对象。", json_output=True)
