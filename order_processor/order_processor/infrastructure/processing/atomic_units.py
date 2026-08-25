"""最小执行单元库 - 只有代码，没有文档"""

import re
from datetime import datetime, timedelta
from typing import Optional, Any, Dict


class AtomicUnits:
    """
    最小执行单元库
    所有原子函数都在这里，供代码执行器调用
    """
    
    # ===== 日期处理 =====
    
    @staticmethod
    def parse_date(date_str: str) -> Optional[datetime]:
        """解析日期字符串为datetime对象"""
        if not date_str:
            return None
        date_str = str(date_str).strip()
        for fmt in ["%Y%m%d", "%Y.%m.%d", "%Y-%m-%d", "%Y/%m/%d"]:
            try:
                return datetime.strptime(date_str, fmt)
            except ValueError:
                continue
        nums = re.findall(r'\d+', date_str)
        if len(nums) >= 3:
            try:
                return datetime(int(nums[0]), int(nums[1]), int(nums[2]))
            except ValueError:
                pass
        return None
    
    @staticmethod
    def format_date(dt: datetime, fmt: str = "%Y.%m.%d") -> str:
        """格式化日期"""
        if dt is None:
            return ""
        return dt.strftime(fmt)
    
    @staticmethod
    def add_days(dt: datetime, days: int) -> Optional[datetime]:
        """日期加天数"""
        if dt is None:
            return None
        return dt + timedelta(days=days)
    
    # ===== 字段操作 =====
    
    @staticmethod
    def get_field(row: dict, field: str, default: str = "") -> str:
        """获取字段值"""
        value = row.get(field, default)
        return default if value is None else str(value)
    
    @staticmethod
    def set_field(row: dict, field: str, value: Any) -> dict:
        """设置字段值"""
        row[field] = value
        return row

    @staticmethod
    def copy_or_blank(row: dict, source: str, target: str) -> dict:
        """复制字段；源字段为 None 或空值时，目标字段也明确写为空字符串。"""
        value = row.get(source)
        row[target] = "" if value is None else value
        return row

    @staticmethod
    def set_blank(row: dict, target: str) -> dict:
        row[target] = ""
        return row
    
    # ===== 值映射 =====
    
    @staticmethod
    def map_value(value: str, mapping: Dict[str, str]) -> str:
        """值映射"""
        if not value:
            return value
        return mapping.get(value, value)

    @staticmethod
    def split_before(value: Any, separator: str) -> str:
        return str(value or "").split(separator, 1)[0]

    @staticmethod
    def remove_prefix(value: Any, count: int = 1) -> str:
        return str(value or "")[count:]

    @staticmethod
    def pad_left(value: Any, length: int, char: str = "0") -> str:
        return str(value or "").zfill(length) if char == "0" else str(value or "").rjust(length, char)

    @staticmethod
    def concat(values: list, separator: str = "") -> str:
        return separator.join(str(v) for v in values if v not in (None, ""))

    @staticmethod
    def parse_excel_date(value: Any) -> Optional[datetime]:
        if isinstance(value, (int, float)) and value > 1000:
            return datetime(1899, 12, 30) + timedelta(days=value)
        return AtomicUnits.parse_date(value)

    @staticmethod
    def move_to_previous_workday(dt: Optional[datetime]) -> Optional[datetime]:
        while dt and dt.weekday() >= 5:
            dt -= timedelta(days=1)
        return dt

    @staticmethod
    def previous_workday(dt: Optional[datetime]) -> Optional[datetime]:
        """返回严格早于 dt 的最近一个工作日。"""
        if dt is None:
            return None
        return AtomicUnits.move_to_previous_workday(dt - timedelta(days=1))

    @staticmethod
    def add_workdays(dt: Optional[datetime], days: int) -> Optional[datetime]:
        if dt is None:
            return None
        direction = 1 if days >= 0 else -1
        remaining = abs(days)
        while remaining:
            dt += timedelta(days=direction)
            if dt.weekday() < 5:
                remaining -= 1
        return dt
    
    # ===== 条件判断 =====
    
    @staticmethod
    def if_contains(value: str, keyword: str) -> bool:
        """判断字符串是否包含关键词"""
        return keyword in str(value)
    
    @staticmethod
    def if_equals(value: str, target: str) -> bool:
        """判断字符串是否相等"""
        return str(value) == str(target)
    
    # ===== 获取函数映射（供执行器使用） =====
    
    @staticmethod
    def get_function_map() -> dict:
        """获取函数名到函数的映射"""
        return {
            "parse_date": AtomicUnits.parse_date,
            "format_date": AtomicUnits.format_date,
            "add_days": AtomicUnits.add_days,
            "get_field": AtomicUnits.get_field,
            "set_field": AtomicUnits.set_field,
            "copy_or_blank": AtomicUnits.copy_or_blank,
            "set_blank": AtomicUnits.set_blank,
            "map_value": AtomicUnits.map_value,
            "split_before": AtomicUnits.split_before,
            "remove_prefix": AtomicUnits.remove_prefix,
            "pad_left": AtomicUnits.pad_left,
            "concat": AtomicUnits.concat,
            "parse_excel_date": AtomicUnits.parse_excel_date,
            "move_to_previous_workday": AtomicUnits.move_to_previous_workday,
            "previous_workday": AtomicUnits.previous_workday,
            "add_workdays": AtomicUnits.add_workdays,
            "if_contains": AtomicUnits.if_contains,
            "if_equals": AtomicUnits.if_equals,
        }
