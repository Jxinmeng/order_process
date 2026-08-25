"""应用服务：只协调用例，不了解 Excel、SQLite 或 LLM SDK。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .ports import OrderProcessingPort


@dataclass
class ProcessOrders:
    """订单文件处理用例。"""

    processor: OrderProcessingPort

    def execute(self, input_path: str, output_path: str) -> dict[str, Any]:
        return self.processor.process(input_path, output_path)
