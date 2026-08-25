"""订单数据模型"""

from dataclasses import dataclass, field
from typing import Optional, List

@dataclass
class OrderInput:
    """输入订单数据"""
    order_id: str
    date: str
    delivery_req: str
    model: str
    
    @classmethod
    def from_dict(cls, data: dict) -> "OrderInput":
        return cls(
            order_id=str(data.get("order_id", "")),
            date=str(data.get("日期", "")),
            delivery_req=str(data.get("交货日期要求", "")),
            model=str(data.get("型号", "")),
        )
    
    def to_dict(self) -> dict:
        return {
            "order_id": self.order_id,
            "日期": self.date,
            "交货日期要求": self.delivery_req,
            "型号": self.model,
        }


@dataclass
class OrderOutput:
    """输出订单数据"""
    order_id: str
    delivery_date: str
    model: str
    extra_fields: dict = field(default_factory=dict)
    ordered_fields: dict = field(default_factory=dict)
    
    @classmethod
    def from_dict(cls, data: dict) -> "OrderOutput":
        known = {"order_id", "交货日期", "型号"}
        return cls(
            order_id=str(data.get("order_id", "")),
            delivery_date=str(data.get("交货日期", "")),
            model=str(data.get("型号", "")),
            extra_fields={key: value for key, value in data.items() if key not in known},
            ordered_fields=dict(data),
        )
    
    def to_dict(self) -> dict:
        if self.ordered_fields:
            return dict(self.ordered_fields)
        output = {}
        if self.order_id:
            output["order_id"] = self.order_id
        if self.delivery_date:
            output["交货日期"] = self.delivery_date
        if self.model:
            output["型号"] = self.model
        output.update(self.extra_fields)
        return output


@dataclass
class ProcessResult:
    """处理结果"""
    success: bool
    data: Optional[OrderOutput] = None
    error: Optional[str] = None
    route: str = "unknown"
    matched_rules: List[str] = field(default_factory=list)
    generated_code: str = ""
