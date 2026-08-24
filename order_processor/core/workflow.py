"""工作流编排 - 组装所有节点"""

import re
from pathlib import Path
from typing import List, Dict, Any, Optional
from models.rule import Rule
from models.order import OrderInput, OrderOutput, ProcessResult
from core.rule_engine import RuleEngine
from core.orchestrator import LLMOrchestrator
from core.executor import CodeExecutor
from order_io.excel_reader import ExcelReader
from order_io.excel_writer import ExcelWriter
from storage.rule_repository import RuleRepository
from core.task_router import TaskRouter


class OrderWorkflow:
    """
    订单处理工作流
    规则引擎 → LLM编排 → 代码执行
    """
    OUTPUT_COLUMNS = [
        "客户编码", "生产标识（主）", "订单号", "计划标记（主）", "需求日期", "收件人", "下计划依据", "主表备注",
        "明细序号", "型号", "料号", "客户型号", "生产标识（子）", "计划标记（子）", "订货数量", "单价",
        "不含税单价", "交货日期", "最小包装数量", "子表备注", "虚拟编码", "客户序号", "物资编码",
        "客户订单需求日期", "项目名称",
    ]
    
    def __init__(self, rules: List[Rule], llm_api_key: Optional[str] = None,
                 rule_repository: Optional[RuleRepository] = None):
        self.rule_engine = RuleEngine(rules)
        self.rule_repository = rule_repository
        self.orchestrator = LLMOrchestrator(llm_api_key)
        self.executor = CodeExecutor()
        self.task_router = TaskRouter(self.orchestrator, rule_repository)
        self._compiled_code_cache: Dict[tuple[str, str], str] = {}
        self.reader = ExcelReader()
        self.writer = ExcelWriter()
    
    def process_row(self, row: dict) -> ProcessResult:
        """
        处理单行数据
        """
        # 1. 规则匹配 → 获取动作描述
        rule_engine = self._rule_engine_for(row)
        matched_rules = self._matched_rules(row, rule_engine)
        actions = [r.action_description for r in matched_rules]
        rule_names = [r.name for r in matched_rules]
        
        if not actions:
            return ProcessResult(
                success=False,
                data=None,
                error="未匹配到任何规则",
                route="no_rule",
                matched_rules=[],
                generated_code=""
            )
        
        # 2. LLM编排 → 生成代码
        code_parts, processed_row = [], row.copy()
        try:
            for rule in matched_rules:
                changes, trace = self.task_router.execute(rule, processed_row)
                processed_row.update(changes)
                code_parts.append(trace)
            result = {"success": True, "data": processed_row, "error": None}
        except Exception as error:
            result = {"success": False, "data": row, "error": str(error)}
        code = "\n\n".join(code_parts)

        if result["success"]:
            processed_row = self._ensure_delivery_date(actions, result["data"])
            requested_outputs = list(self.OUTPUT_COLUMNS)
            for rule in matched_rules:
                for field in rule.output_fields:
                    if field not in requested_outputs:
                        requested_outputs.append(field)
            output_data = OrderOutput.from_dict(
                # 输出表头由规则库声明决定；即使某个值为空，也必须保留对应列。
                {field: processed_row.get(field, "") for field in requested_outputs}
            )
            return ProcessResult(
                success=True,
                data=output_data,
                error=None,
                route="orchestrated",
                matched_rules=rule_names,
                generated_code=code
            )
        else:
            return ProcessResult(
                success=False,
                data=None,
                error=result["error"],
                route="execution_failed",
                matched_rules=rule_names,
                generated_code=code
            )
    
    def process(self, input_path: str, output_path: str) -> Dict[str, Any]:
        """
        处理整个Excel
        """
        print("\n" + "=" * 70)
        print("订单处理工作流 (规则驱动 + LLM编排)")
        print(f"  输入: {input_path}")
        print(f"  输出: {output_path}")
        print("=" * 70)
        
        # 1. 读取
        rows = self.reader.read(input_path)
        self._apply_preprocessing(rows)
        print(f"读取 {len(rows)} 行")
        print("-" * 70)
        
        if not rows:
            self.writer.write([], output_path)
            return {"success": True, "total": 0, "output_file": output_path}
        
        # 2. 逐行处理
        results: List[ProcessResult] = []
        
        for idx, row in enumerate(rows, 1):
            print(f"\n行 {idx}: order_id={row.get('order_id', '')}")
            
            # 打印匹配的规则
            matched = self._matched_rules(row)
            if matched:
                print(f"  匹配规则: {', '.join([r.name for r in matched])}")
                for r in matched:
                    print(f"    → 动作: {r.action_description}")
            else:
                print("  未匹配到规则")
            
            # 处理
            result = self.process_row(row)
            results.append(result)
            
            # 打印结果
            if result.success and result.data:
                print(f"  成功: 交货日期={result.data.delivery_date}, 型号={result.data.model}")
                print(f"  生成代码: {result.generated_code[:100]}...")
            else:
                print(f"  失败: {result.error}")
        
        # 3. 写入
        output_data = []
        for r in results:
            if r.success and r.data:
                output_data.append(r.data.to_dict())
        
        saved_files = self._write_output_files(output_data, output_path)
        
        # 4. 统计
        success_count = len([r for r in results if r.success])
        print("\n" + "=" * 70)
        print(f"统计: 总{len(rows)}行, 成功{success_count}行, 失败{len(results)-success_count}行")
        for saved_file in saved_files:
            print(f"已保存: {saved_file}")
        print("=" * 70)
        
        return {
            "success": success_count == len(rows),
            "total": len(rows),
            "success_count": success_count,
            "failed_count": len(results) - success_count,
            "output_file": saved_files[0] if len(saved_files) == 1 else None,
            "output_files": saved_files,
            "results": results,
        }

    def _write_output_files(self, output_data: List[dict], output_path: str) -> List[str]:
        """存在多个合同号时，按合同号分别输出 Excel 文件。"""
        grouped: Dict[str, List[dict]] = {}
        for row in output_data:
            contract = str(row.get("合同号") or "").strip()
            if contract:
                grouped.setdefault(contract, []).append(row)
        if len(grouped) <= 1:
            self.writer.write(output_data, output_path)
            return [output_path]

        output_dir = Path(output_path).parent
        output_dir.mkdir(parents=True, exist_ok=True)
        saved_files: List[str] = []
        for contract, rows in grouped.items():
            safe_name = re.sub(r'[\\/:*?"<>|]+', "_", contract)
            match = re.fullmatch(r"(\d+)-(\d+)", contract)
            if match:
                base, category = match.groups()
                suffix = {"1": "", "2": "-五院验收", "3": "-一院验收"}.get(category, f"-{category}")
                safe_name = f"AS{base}{suffix}"
            file_path = output_dir / f"{safe_name}.xlsx"
            self.writer.write(rows, str(file_path))
            saved_files.append(str(file_path))
        return saved_files

    def _ensure_delivery_date(self, actions: List[str], processed_row: dict) -> dict:
        """保证日期类规则输出到固定字段“交货日期”。

        大模型偶尔会把结果写回“日期”或臆造字段；输出 Excel 只认“交货日期”，
        因此缺失时以同一动作的本地确定性实现补齐。
        """
        is_date_action = any(
            keyword in action for action in actions
            for keyword in ("加30天", "加10天", "保持日期不变")
        )
        if not is_date_action or processed_row.get("交货日期"):
            return processed_row
        fallback = self.executor.execute(
            self.orchestrator.local_orchestrate(actions), processed_row
        )
        return fallback["data"] if fallback["success"] else processed_row

    def _get_execution_code(self, matched_rules: List[Rule]) -> str:
        """按规则复用编译代码；仅在某条规则首次出现或版本变化时调用模型。"""
        code_parts = []
        for rule in matched_rules:
            cache_key = (rule.id, rule.version)
            code = self._compiled_code_cache.get(cache_key)
            if code is None and rule.compiled_code:
                code = rule.compiled_code
            if code is None:
                code = self.orchestrator.compile_rule(rule)
                if self.rule_repository:
                    self.rule_repository.save_compiled_code(rule.id, rule.version, code)
            self._compiled_code_cache[cache_key] = code
            code_parts.append(code)
        return "\n\n".join(code_parts) or "# 无动作需要执行"

    def _rule_engine_for(self, row: dict) -> RuleEngine:
        """按 Excel 的“客户名称”字段加载该客户及通用规则。"""
        if not self.rule_repository:
            return self.rule_engine
        customer_name = str(row.get("客户名称") or "通用规则").strip()
        return RuleEngine(self.rule_repository.load_active_rules(customer_name))

    def _matched_rules(self, row: dict, engine: Optional[RuleEngine] = None) -> List[Rule]:
        """预处理规则仅在批量读取阶段执行，不参与单行沙箱执行。"""
        active_engine = engine or self._rule_engine_for(row)
        return [rule for rule in active_engine.match(row) if rule.task_type != "preprocess"]

    def _apply_preprocessing(self, rows: List[dict]) -> None:
        """执行规则库声明的跨行输入预处理（目前为代码列向上填充）。"""
        previous_values: Dict[str, Any] = {}
        filled_count = 0
        numbering_rules: Dict[str, tuple[Rule, List[dict]]] = {}
        for row in rows:
            for rule in self._rule_engine_for(row).rules:
                if rule.task_type != "preprocess":
                    continue
                if rule.executor_name == "number_within_contract":
                    numbering_rules.setdefault(rule.id, (rule, []))[1].append(row)
                    continue
                if rule.executor_name != "fill_down_from_previous":
                    continue
                field = str(rule.executor_config.get("field") or (rule.input_fields[0] if rule.input_fields else ""))
                if not field:
                    continue
                value = row.get(field)
                if value is None or str(value).strip() == "":
                    if field in previous_values:
                        row[field] = previous_values[field]
                        filled_count += 1
                else:
                    previous_values[field] = value
        if filled_count:
            print(f"输入预处理: 已向上填充 {filled_count} 个代码单元格")

        for rule, target_rows in numbering_rules.values():
            config = rule.executor_config
            group_field = str(config.get("group_field", "合同号"))
            fallback_group = str(config.get("fallback_group_field", "计划号"))
            order_field = str(config.get("order_field", "用户订单序号"))
            target_field = str(config.get("target_field", "明细序号"))
            grouped: Dict[str, List[dict]] = {}
            for row in target_rows:
                group_value = row.get(group_field) or row.get(fallback_group) or "__default__"
                grouped.setdefault(str(group_value), []).append(row)
            for group_rows in grouped.values():
                def sequence_key(item: dict) -> tuple[int, float | str]:
                    value = item.get(order_field)
                    try:
                        return 0, float(value)
                    except (TypeError, ValueError):
                        return 1, str(value or "")
                for index, row in enumerate(sorted(group_rows, key=sequence_key), 1):
                    row[target_field] = index
