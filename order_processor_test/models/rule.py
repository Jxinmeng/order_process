"""规则数据模型"""

from dataclasses import dataclass, field
from typing import Optional, List

@dataclass
class Rule:
    """规则定义"""
    id: str
    name: str
    condition: str           # 条件表达式，如 "交货日期要求 == '默认'"
    action_description: str  # 动作描述（自然语言），如 "在当前日期上加30天"
    priority: int = 0
    enabled: bool = True
    version: str = "v1"
    compiled_code: Optional[str] = None
    task_type: str = "deterministic"
    input_fields: List[str] = field(default_factory=list)
    output_fields: List[str] = field(default_factory=list)
    executor_name: Optional[str] = None
    executor_config: dict = field(default_factory=dict)
    
    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "name": self.name,
            "condition": self.condition,
            "action_description": self.action_description,
            "priority": self.priority,
            "enabled": self.enabled,
            "version": self.version,
            "compiled_code": self.compiled_code,
        }
