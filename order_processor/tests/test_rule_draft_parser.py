import unittest

from order_processor.interfaces.rule_draft_parser import parse_rule_draft


class RuleDraftParserTests(unittest.TestCase):
    def test_accepts_json_inside_markdown_fence(self) -> None:
        self.assertEqual({"rules": []}, parse_rule_draft("```json\n{\"rules\": []}\n```"))

    def test_reports_empty_model_response_clearly(self) -> None:
        with self.assertRaisesRegex(ValueError, "模型没有返回"):
            parse_rule_draft("  ")


if __name__ == "__main__":
    unittest.main()
