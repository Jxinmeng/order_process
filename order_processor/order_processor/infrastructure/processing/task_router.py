"""按规则 task_type 分流，并严格限制输入/输出字段。"""

import json

from order_processor.domain.rule import Rule
from order_processor.infrastructure.persistence.rule_repository import RuleRepository
from order_processor.infrastructure.processing.developer_executors import EXECUTORS
from order_processor.infrastructure.processing.executor import CodeExecutor
from order_processor.infrastructure.processing.orchestrator import LLMOrchestrator


class TaskRouter:
    def __init__(self, orchestrator: LLMOrchestrator, repository: RuleRepository | None):
        self.orchestrator, self.repository = orchestrator, repository
        self.memory_cache: dict[tuple[str, str], str] = {}

    def execute(self, rule: Rule, row: dict) -> tuple[dict, str]:
        inputs = {name: row.get(name) for name in rule.input_fields} if rule.input_fields else row.copy()
        if rule.task_type == "developer_executor":
            handler = EXECUTORS.get(rule.executor_name or "")
            if not handler:
                raise ValueError(f"未注册开发者执行器: {rule.executor_name}")
            result, trace = handler(inputs), f"developer_executor:{rule.executor_name}"
        elif rule.task_type == "direct_atomic":
            result, trace = self._direct_atomic(rule, inputs)
        elif rule.task_type == "semantic":
            result, trace = self._semantic(rule, inputs)
        elif rule.task_type == "deterministic":
            code = self._compiled_code(rule)
            executed = CodeExecutor.execute(code, inputs)
            if not executed["success"]:
                raise RuntimeError(executed["error"])
            result, trace = executed["data"], code
        else:
            raise ValueError(f"未知 task_type: {rule.task_type}")
        outputs = rule.output_fields or list(result.keys())
        selected = {name: result.get(name) for name in outputs if name in result}
        # ERP 输出字段统一使用“是/否”，避免 JSON 布尔值显示为 TRUE/FALSE。
        if "是否加急" in selected:
            value = selected["是否加急"]
            selected["是否加急"] = "是" if str(value).strip().lower() in {"true", "1", "是", "y", "yes"} else "否"
        return selected, trace

    @staticmethod
    def _direct_atomic(rule: Rule, inputs: dict) -> tuple[dict, str]:
        """执行数据库中声明的高频固定动作，完全不调用模型。"""
        action = rule.executor_name or ""
        source = rule.input_fields[0] if rule.input_fields else ""
        target = rule.output_fields[0] if rule.output_fields else ""
        if action.startswith("copy_or_blank"):
            value = inputs.get(source)
            return {target: "" if value is None else value}, f"direct_atomic:copy_or_blank({source}->{target})"
        if action == "map_value":
            mapping = rule.executor_config.get("mapping", {})
            value = inputs.get(source)
            # Excel 中的枚举值常被手工录入为带空格或引号的文本；用规范化后的键
            # 匹配映射，未命中时仍保留原值，避免误清空业务数据。
            lookup_key = str(value).strip().strip("'\"“”") if value is not None else ""
            mapped = mapping.get(lookup_key, value)
            result = {field: "" if mapped is None else mapped for field in rule.output_fields}
            return result, f"direct_atomic:map_value({source}->{','.join(rule.output_fields)})"
        if action == "set_value":
            value = rule.executor_config.get("value", "")
            return {field: value for field in rule.output_fields}, f"direct_atomic:set_value({','.join(rule.output_fields)})"
        if action == "classify_c003_contract":
            as_number = str(inputs.get("AS订单号") or "")
            note = str(inputs.get("子表的备注") or "")
            if "五院" in note:
                contract_suffix, note_suffix = "-2", "-五院验收"
            elif "一院" in note:
                contract_suffix, note_suffix = "-3", "-一院验收"
            else:
                contract_suffix, note_suffix = "-1", ""
            contract = as_number.removeprefix("AS") + contract_suffix if as_number else ""
            main_note = as_number + note_suffix if as_number else ""
            return {"合同号": contract, "主表备注": main_note}, f"direct_atomic:classify_c003_contract({contract})"
        if action == "format_template":
            template = rule.executor_config.get("template", "")
            value = template
            for field in rule.input_fields:
                value = value.replace("{" + field + "}", "" if inputs.get(field) is None else str(inputs.get(field)))
            return {field: value for field in rule.output_fields}, f"direct_atomic:format_template({','.join(rule.output_fields)})"
        if action.startswith("set_blank"):
            return {target: ""}, f"direct_atomic:set_blank({target})"
        raise ValueError(f"未知 direct_atomic 执行单元: {action}")

    def _compiled_code(self, rule: Rule) -> str:
        key = (rule.id, rule.version)
        code = self.memory_cache.get(key) or rule.compiled_code
        # 旧版本在未配置模型时会把“无需修改”缓存到数据库；该缓存会让后续即使
        # 配好 DeepSeek 也永远不再编译，进而造成型号/料号空白。发现后立即废弃。
        if code and code.strip() in {"# 无需修改", "#无需修改"}:
            if self.repository:
                self.repository.clear_compiled_code(rule.id)
            code = None
        if not code:
            code = self.orchestrator.compile_rule(rule)
            if self.repository:
                self.repository.save_compiled_code(rule.id, rule.version, code)
        self.memory_cache[key] = code
        return code

    def _semantic(self, rule: Rule, inputs: dict) -> tuple[dict, str]:
        if not self.orchestrator.api_key:
            note = str(inputs.get("客户备注", ""))
            result = {"是否加急": any(k in note for k in ("加急", "尽快", "展会")),
                      "处理建议": "待配置 DeepSeek 后生成完整语义建议"}
            return result, "semantic_local_fallback"
        prompt = (f"根据以下输入完成语义任务：{rule.action_description}\n输入：{json.dumps(inputs, ensure_ascii=False)}\n"
                  f"仅输出 JSON，对象字段只能是：{json.dumps(rule.output_fields, ensure_ascii=False)}")
        content = self.orchestrator.understand_json(prompt)
        return json.loads(content), content
