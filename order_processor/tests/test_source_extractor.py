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
