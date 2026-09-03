import unittest

from order_processor.infrastructure.persistence.rule_repository import RuleRepository


class CustomerCodeMatchingTests(unittest.TestCase):
    candidates = [
        ("K-05-002", "天津津航技术研究所", "天津津航技术研究所"),
        ("K-17-206", "中电科技（南京）", "中电科技南京"),
    ]

    def test_resolves_contract_full_name_to_customer_code(self):
        self.assertEqual(
            "K-05-002",
            RuleRepository._match_customer_code("天津津航计算技术研究所", self.candidates),
        )
        self.assertEqual(
            "K-17-206",
            RuleRepository._match_customer_code("中电科技南京电子信息发展有限公司", self.candidates),
        )

    def test_does_not_match_unknown_customer(self):
        self.assertIsNone(RuleRepository._match_customer_code("不存在的客户", self.candidates))
