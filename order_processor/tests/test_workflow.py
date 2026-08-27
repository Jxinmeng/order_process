import unittest

from order_processor.domain.rule import Rule
from order_processor.infrastructure.processing.workflow import OrderWorkflow


class _RuleRepositoryStub:
    def __init__(self, rules):
        self._rules = rules

    def load_active_rules(self, customer_code):
        return self._rules

    def output_columns(self, customer_code):
        return ["型号", "长度规则已调用"]


class OrderWorkflowTests(unittest.TestCase):
    def test_later_rule_matches_value_written_by_model_initialization(self):
        """条件必须在前序规则更新行数据后再判断，不能在开始时统一筛选。"""
        rules = [
            Rule(
                id="initialize-model",
                name="型号初始化",
                condition="产品型号 is not blank",
                action_description="将产品型号写入型号",
                task_type="direct_atomic",
                input_fields=["产品型号"],
                output_fields=["型号"],
                executor_name="copy_or_blank",
            ),
            Rule(
                id="j30-length",
                name="J30长度规则",
                condition="型号 contains 'J30'",
                action_description="标记长度规则已调用",
                task_type="direct_atomic",
                output_fields=["长度规则已调用"],
                executor_name="set_value",
                executor_config={"value": "是"},
            ),
        ]
        workflow = OrderWorkflow(rules, rule_repository=_RuleRepositoryStub(rules))

        result = workflow.process_row({"客户代码": "K-17-206", "产品型号": "J30J-25TJL(L=0.3M)"})

        self.assertTrue(result.success)
        self.assertEqual("J30J-25TJL(L=0.3M)", result.data.model)
        self.assertEqual("是", result.data.to_dict()["长度规则已调用"])
        self.assertEqual(["型号初始化", "J30长度规则"], result.matched_rules)
