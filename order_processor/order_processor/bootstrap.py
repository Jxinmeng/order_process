"""组合根：唯一允许组装具体基础设施实现的位置。"""

from __future__ import annotations

from order_processor.application.services import ProcessOrders
from order_processor.infrastructure.order_processor_adapter import OrderProcessorAdapter
from order_processor.infrastructure.persistence.rule_repository import RuleRepository


def build_process_orders(api_key: str | None = None, database_path: str = "data/rules.db") -> ProcessOrders:
    repository = RuleRepository(database_path)
    repository.initialize()
    adapter = OrderProcessorAdapter(repository.load_active_rules(), api_key, repository)
    return ProcessOrders(adapter)
