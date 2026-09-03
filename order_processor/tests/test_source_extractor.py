import json
import unittest

from order_processor.infrastructure.ingestion.source_extractor import StructuredOrderExtractor


class SourceExtractorTests(unittest.TestCase):
    def test_only_declared_fields_are_passed_to_workflow(self):
        rows = StructuredOrderExtractor._validate(
            json.dumps({"orders": [{"客户代码": "C001", "型号": "A-1", "臆造字段": "不能进入规则引擎"}]}),
            {"客户代码", "型号"},
        )
        self.assertEqual([{"客户代码": "C001", "型号": "A-1"}], rows)

    def test_rejects_empty_orders(self):
        with self.assertRaises(RuntimeError):
            StructuredOrderExtractor._validate('{"orders": []}', {"型号"})

    def test_generates_original_order_sequence_only_when_source_has_none(self):
        rows = [{"合同编号": "A"}, {"合同编号": "A"}, {"合同编号": "A"}]
        StructuredOrderExtractor._assign_missing_original_order_numbers(rows)
        self.assertEqual(["1", "2", "3"], [row["原始订单序号"] for row in rows])

        source_rows = [{"合同编号": "B", "原始订单序号": "10"}, {"合同编号": "B"}]
        StructuredOrderExtractor._assign_missing_original_order_numbers(source_rows)
        self.assertEqual("10", source_rows[0]["原始订单序号"])
        self.assertNotIn("原始订单序号", source_rows[1])
