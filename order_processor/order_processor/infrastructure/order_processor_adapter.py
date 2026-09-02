"""把尚未迁移的流程封装在基础设施边界内。

该适配器将具体订单处理流程封装为应用层端口实现。
"""

from __future__ import annotations

from typing import Any, Optional

from order_processor.domain.rule import Rule
from order_processor.infrastructure.persistence.rule_repository import RuleRepository
from order_processor.infrastructure.processing.workflow import OrderWorkflow


class OrderProcessorAdapter:
    def __init__(
        self,
        rules: list[Rule],
        llm_api_key: Optional[str],
        rule_repository: RuleRepository,
    ) -> None:
        self._workflow = OrderWorkflow(rules, llm_api_key, rule_repository)

    def process(self, input_path: str, output_path: str) -> dict[str, Any]:
        return self._workflow.process(input_path, output_path)

    def process_rows(self, rows: list[dict[str, Any]], output_path: str) -> dict[str, Any]:
        return self._workflow.process_rows(rows, output_path)
