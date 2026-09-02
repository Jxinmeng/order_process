"""应用层拥有的稳定边界（端口）。"""

from __future__ import annotations

from typing import Any, Protocol


class OrderProcessingPort(Protocol):
    """处理一个订单文件的用例边界。"""

    def process(self, input_path: str, output_path: str) -> dict[str, Any]: ...


class RuleCompilerPort(Protocol):
    """将规则转换为受字段白名单约束的可执行代码。"""

    def compile_rule(self, rule: Any) -> str: ...

    def understand_json(self, prompt: str) -> str: ...
