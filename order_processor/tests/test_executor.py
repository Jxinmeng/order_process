import unittest

from order_processor.infrastructure.processing.executor import CodeExecutor


class CodeExecutorTests(unittest.TestCase):
    def test_persists_only_rule_state_for_cross_row_order_numbering(self) -> None:
        code = '''
if "_rule_order_used" not in globals():
    _rule_order_used = set()
_order_no = get_field(row, "订单号")
if _order_no in _rule_order_used:
    row = set_field(row, "订单号", _order_no + "-1")
else:
    _rule_order_used.add(_order_no)
'''
        state: dict = {}

        first = CodeExecutor.execute(code, {"订单号": "K-06-035"}, state)
        second = CodeExecutor.execute(code, {"订单号": "K-06-035"}, state)

        self.assertTrue(first["success"])
        self.assertEqual("K-06-035", first["data"]["订单号"])
        self.assertTrue(second["success"])
        self.assertEqual("K-06-035-1", second["data"]["订单号"])
        self.assertEqual({"_rule_order_used"}, set(state))


if __name__ == "__main__":
    unittest.main()
