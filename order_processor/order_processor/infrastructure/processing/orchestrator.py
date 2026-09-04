"""LLM编排器 - 从文件加载提示词模板"""

import os
import re
import json
from typing import List, Dict, Any, Optional

from order_processor.domain.rule import Rule
from order_processor.shared.prompt_loader import PromptLoader
from order_processor.shared.run_logger import log_llm_exchange
from order_processor.infrastructure.agno_rule_agent import AgnoRuleAgent


class LLMOrchestrator:
    """
    LLM编排器
    输入：动作描述列表
    输出：组合后的Python代码
    """
    
    DEFAULT_BASE_URL = "https://api.deepseek.com"
    DEFAULT_MODEL = "deepseek-v4-flash"

    def __init__(self, api_key: Optional[str] = None, model: Optional[str] = None,
                 base_url: Optional[str] = None):
        self.api_key = api_key or os.getenv("DEEPSEEK_API_KEY")
        # 变量名保留 DEEPSEEK_* 以兼容既有配置；其值可指向官网或百炼等
        # OpenAI 兼容服务。此前这里硬编码官网地址，导致 .env 的切换不生效。
        self.model = model or os.getenv("DEEPSEEK_MODEL", self.DEFAULT_MODEL)
        self.base_url = base_url or os.getenv("DEEPSEEK_BASE_URL", self.DEFAULT_BASE_URL)
        self._agno_agent = None
        if self.api_key:
            # Agno 是唯一的模型调用编排层；导入失败时延迟报出可操作错误，
            # 无 Key 的本地确定性模式不受新增依赖影响。
            self._agno_agent = AgnoRuleAgent(self.api_key, self.model, self.base_url)
        
        # 从文件加载提示词模板
        self._load_prompts()
    
    def _load_prompts(self):
        """从文件加载提示词"""
        try:
            self.atomic_units_doc = PromptLoader.load_atomic_units_doc()
            self.orchestrator_template = PromptLoader.load_orchestrator_prompt()
            self.rule_library_draft_template = PromptLoader.load_rule_library_draft_prompt()
        except FileNotFoundError as e:
            print(f"⚠️ 提示词文件加载失败: {e}")
            print("使用默认提示词...")
            self.atomic_units_doc = self._get_default_atomic_doc()
            self.orchestrator_template = self._get_default_template()
            self.rule_library_draft_template = "Convert the business rule text to JSON rule drafts only."
    
    def _get_default_atomic_doc(self) -> str:
        """默认原子函数说明书"""
        return """
## 可用的最小执行单元

### 日期处理
- parse_date(date_str): 解析日期
- format_date(dt, fmt="%Y.%m.%d"): 格式化日期
- add_days(dt, days): 日期加天数
- add_workdays(dt, days): 按工作日加减天数
- previous_workday(dt): 返回严格早于该日期的最近工作日

### 字段操作
- get_field(row, field, default=""): 获取字段值
- set_field(row, field, value): 设置字段值

### 条件判断
- if_contains(value, keyword): 判断包含
- if_equals(value, target): 判断相等

### 值映射
- map_value(value, mapping): 值映射
"""
    
    def _get_default_template(self) -> str:
        """默认编排器模板"""
        return """
{atomic_units_doc}

## 当前数据行
{row_json}

## 需要执行的动作
{actions_list}

请生成Python代码实现上述动作，只输出代码。

重要：交期计算只能写入 row["交货日期"]，不得修改 row["日期"] 或创建其他日期字段。
"""
    
    def _build_prompt(self, row: dict, actions: List[str]) -> str:
        """构建提示词"""
        return self.orchestrator_template.format(
            atomic_units_doc=self.atomic_units_doc,
            row_json=json.dumps(row, ensure_ascii=False, indent=2),
            actions_list="\n".join([f"{i+1}. {action}" for i, action in enumerate(actions)])
        )

    def _build_rule_prompt(self, rule: Rule) -> str:
        """生成可跨订单复用的单条规则代码提示词，不包含某一行订单值。"""
        return f"""{self.atomic_units_doc}

## 规则条件
{rule.condition}

## 规则动作
{rule.action_description}

## 本规则允许读取的输入字段（字段名必须完全一致）
{json.dumps(rule.input_fields, ensure_ascii=False)}

## 本规则必须写入的输出字段（字段名必须完全一致）
{json.dumps(rule.output_fields, ensure_ascii=False)}

请仅输出 Python 代码。代码必须对所有满足该规则的订单通用，不能硬编码某一条订单的
日期、型号或订单号。输入和输出均为 `row` 字典；交期结果只能写入 `row[\"交货日期\"]`，
不能覆盖 `row[\"日期\"]`，也不能创建其他日期字段。只能使用上述原子单元，无需 import。
必须输出可直接执行的顶层语句：禁止定义 `def process`、任何其他函数或 class，禁止要求调用方再调用函数。
本规则允许写入的输出字段仅为：{json.dumps(rule.output_fields, ensure_ascii=False)}。不得创建或写入
其他字段；例如输出字段为 `型号` 时，必须写入 `row[\"型号\"]`，不得写入 `ERP编码`。
不得读取未列出的字段，不得自行改写字段名。必须为每个输出字段赋值；没有值时写入空字符串。
涉及 Excel 输入日期时必须使用 `parse_excel_date`，不要使用 `parse_date`；日期输出使用 `format_date(dt, "%Y%m%d")`。

## 型号字段的强制编排规范
若输出字段包含“型号”，且动作描述要求对“产品型号”增加前缀或后缀：先且仅先执行
`source_model = get_field(row, "产品型号")`，再一次性用 `set_field` 写入最终型号。
`source_model` 是不可拆分的原文：禁止逐字符/逐子串循环替换，禁止重复拼接，禁止读取
`row["型号"]` 作为输入，也禁止在已经生成的型号上再次加工。前缀规则应为
`row = set_field(row, "型号", "前缀" + source_model)`，而不是 replace、for 循环或对“型号”再拼接。
"""
    
    def _call_llm(self, prompt: str) -> str:
        """调用大模型"""
        if not self.api_key:
            raise RuntimeError("未配置 DEEPSEEK_API_KEY")
        if self._agno_agent:
            try:
                content = self._agno_agent.run_text(prompt)
                log_llm_exchange("Agno/DeepSeek", self.model, prompt, content)
                return content
            except Exception as error:
                log_llm_exchange("Agno/DeepSeek", self.model, prompt, f"[调用失败] {error}")
                raise
        raise RuntimeError("Agno Agent 未初始化；请检查 DEEPSEEK_API_KEY 与 agno 依赖")
    
    def _mock_orchestrate(self, actions: List[str]) -> str:
        """模拟LLM（无API Key时）"""
        code = []
        for action in actions:
            if "加30天" in action:
                code.append('dt = parse_date(get_field(row, "日期"))\nif dt: row = set_field(row, "交货日期", format_date(add_days(dt, 30)))')
            elif "加10天" in action:
                code.append('dt = parse_date(get_field(row, "日期"))\nif dt: row = set_field(row, "交货日期", format_date(add_days(dt, 10)))')
            elif "保持日期不变" in action:
                code.append('dt = parse_date(get_field(row, "日期"))\nif dt: row = set_field(row, "交货日期", format_date(dt))')
            elif "客户A款" in action or "A-2026-011" in action:
                code.append('row = set_field(row, "型号", "A-2026-011")')
            elif "客户B款" in action or "B-2026-011" in action:
                code.append('row = set_field(row, "型号", "B-2026-011")')
        return "\n\n".join(code) or "# 无需修改"

    def local_orchestrate(self, actions: List[str]) -> str:
        """执行确定性兜底动作，确保关键输出字段符合订单输出格式。"""
        return self._mock_orchestrate(actions)

    def compile_rule(self, rule: Rule) -> str:
        """将一条规则编译为可复用的本地代码。"""
        if not self.api_key:
            message = "确定性规则需要 DeepSeek 编译，但未配置 DEEPSEEK_API_KEY；已拒绝执行，避免静默输出空字段。"
            log_llm_exchange("模型未配置", "none", self._build_rule_prompt(rule), f"[拒绝执行] {message}")
            raise RuntimeError(message)
        return self._call_llm(self._build_rule_prompt(rule))

    def understand_json(self, prompt: str) -> str:
        """执行语义任务并强制模型返回 JSON。"""
        if self._agno_agent:
            content = self._agno_agent.understand_json(prompt)
            response = content + f"\n[LLM 响应元数据]\n{self._agno_agent.last_response_metadata}"
            log_llm_exchange("Agno/DeepSeek-语义任务", self.model, prompt, response)
            if self._agno_agent.last_finish_reason == "length":
                raise RuntimeError(
                    "模型输出超过长度上限，规则草稿在 JSON 完成前被截断。"
                    "请将规则表拆分后重试，或检查模型服务商的输出长度限制"
                )
            return content
        raise RuntimeError("语义任务需要已配置的 DEEPSEEK_API_KEY 与 agno 依赖")

    def draft_rule_library(self, customer_code: str, business_rule_text: str, input_fields: list[dict], erp_fields: list[dict]) -> str:
        """将业务自然语言转换为待校对的规则库 JSON 草稿。"""
        prompt = self.rule_library_draft_template.format(
            customer_code=customer_code,
            business_rule_text=business_rule_text,
            input_fields_json=json.dumps(input_fields, ensure_ascii=False),
            erp_fields_json=json.dumps(erp_fields, ensure_ascii=False),
        )
        return self.understand_json(prompt)
    
    def orchestrate(self, row: dict, actions: List[str]) -> str:
        """编排：根据动作描述生成代码"""
        if not actions:
            return "# 无动作需要执行"
        
        if not self.api_key:
            code = self.local_orchestrate(actions)
            log_llm_exchange("本地兜底（未调用模型）", "local", self._build_prompt(row, actions), code)
            return code
        return self._call_llm(self._build_prompt(row, actions))
