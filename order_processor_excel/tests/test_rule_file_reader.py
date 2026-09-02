from io import BytesIO
import unittest

from openpyxl import Workbook

from order_processor.interfaces.rule_file_reader import read_rule_file


class RuleFileReaderTests(unittest.TestCase):
    def test_reads_utf8_text_file(self) -> None:
        self.assertEqual("订单号：自动生成", read_rule_file("rules.txt", "订单号：自动生成".encode()))

    def test_reads_field_and_description_columns_from_excel(self) -> None:
        workbook = Workbook()
        sheet = workbook.active
        sheet.append(["", "订单录入规则示例", ""])
        sheet.append(["", "录入erp的字段", "规则描述（示例）"])
        sheet.append(["主表信息", "客户代码", "K-06-035"])
        sheet.append(["", "需求日期", "当前日期+35天"])
        sheet.append(["子表信息", "型号", "用户订单上的规格型号"])
        output = BytesIO()
        workbook.save(output)

        self.assertEqual(
            "客户代码：K-06-035\n需求日期：当前日期+35天\n型号：用户订单上的规格型号",
            read_rule_file("rules.xlsx", output.getvalue()),
        )

    def test_rejects_legacy_excel_format(self) -> None:
        with self.assertRaisesRegex(ValueError, "XLSX 或 XLSM"):
            read_rule_file("rules.xls", b"not an excel file")


if __name__ == "__main__":
    unittest.main()
