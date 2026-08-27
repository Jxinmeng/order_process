"""规则引擎 - 匹配条件，返回动作描述"""

import re
from typing import List, Dict, Any, Optional
from order_processor.domain.rule import Rule


class RuleEngine:
    """
    规则引擎
    功能：根据当前数据匹配规则，返回动作描述列表
    """
    
    def __init__(self, rules: List[Rule]):
        # 低优先级规则先执行，高优先级（通常为客户例外规则）最后执行，
        # 从而让更具体的规则覆盖通用规则的处理结果。
        # RuleRepository 已按“规则组顺序 → 组内优先级”排序。
        # 这里不能再次按全局 priority 排序，否则不同字段分组会相互影响。
        self.rules = list(rules)
    
    def _eval_condition(self, condition: str, row: dict) -> bool:
        """
        评估条件表达式
        
        支持语法:
        - field == value        : 精确匹配
        - field contains value  : 包含
        - field not contains value : 不包含
        - field in [a, b, c]    : 在列表中
        - field not in [a, b, c]: 不在列表中
        """
        condition = condition.strip()
        if condition.lower() in {"always true", "无条件执行"}:
            return True

        # 先分解逻辑表达式，避免把 ``and`` 后的内容当成字段名。
        if " or " in condition:
            return any(self._eval_condition(part, row) for part in condition.split(" or "))
        if " and " in condition:
            return all(self._eval_condition(part, row) for part in condition.split(" and "))

        if condition.endswith(" is blank"):
            field = condition[:-9].strip()
            return row.get(field) in (None, "")
        if condition.endswith(" is not blank"):
            field = condition[:-13].strip()
            return row.get(field) not in (None, "")
        
        # 处理 ==
        if " == " in condition:
            left, right = condition.split(" == ", 1)
            left = left.strip()
            right = right.strip().strip("'\"")
            return str(row.get(left, "")) == right

        # 处理 starts with
        if " starts with " in condition:
            left, right = condition.split(" starts with ", 1)
            return str(row.get(left.strip(), "")).startswith(right.strip().strip("'\""))

        if " not matches " in condition:
            left, pattern = condition.split(" not matches ", 1)
            return re.search(pattern.strip().strip("'\""), str(row.get(left.strip(), ""))) is None

        if " matches " in condition:
            left, pattern = condition.split(" matches ", 1)
            return re.search(pattern.strip().strip("'\""), str(row.get(left.strip(), ""))) is not None
        
        # 处理 not contains
        if " not contains " in condition:
            left, right = condition.split(" not contains ", 1)
            left = left.strip()
            right = right.strip().strip("'\"")
            return right not in str(row.get(left, ""))

        # 处理 contains
        if " contains " in condition:
            left, right = condition.split(" contains ", 1)
            left = left.strip()
            right = right.strip().strip("'\"")
            return right in str(row.get(left, ""))
        
        # 处理 not in
        if " not in " in condition:
            left, right = condition.split(" not in ", 1)
            left = left.strip()
            right = right.strip().strip("[]").split(",")
            right = [v.strip().strip("'\"") for v in right]
            return str(row.get(left, "")) not in right

        # 处理 in
        if " in " in condition:
            left, right = condition.split(" in ", 1)
            left = left.strip()
            right = right.strip().strip("[]").split(",")
            right = [v.strip().strip("'\"") for v in right]
            return str(row.get(left, "")) in right
        
        return True
    
    def match(self, row: dict) -> List[Rule]:
        """
        匹配所有满足条件的规则
        返回按优先级排序的规则列表
        """
        matched = []
        for rule in self.rules:
            if rule.enabled and self._eval_condition(rule.condition, row):
                matched.append(rule)
        return matched

    def matches_rule(self, rule: Rule, row: dict) -> bool:
        """判断单条规则是否匹配当前（可能已被前序规则更新的）行数据。"""
        return rule.enabled and self._eval_condition(rule.condition, row)
    
    def get_actions(self, row: dict) -> List[str]:
        """
        获取匹配的动作描述列表
        这是给LLM看的
        """
        matched_rules = self.match(row)
        return [rule.action_description for rule in matched_rules]
    
    def get_matched_rule_names(self, row: dict) -> List[str]:
        """获取匹配的规则名称"""
        return [rule.name for rule in self.match(row)]
