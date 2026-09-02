import unittest
from datetime import date

from order_processor.infrastructure.ingestion.source_extractor import StructuredOrderExtractor


class DateNormalizationTests(unittest.TestCase):
    def test_normalizes_supported_date_representations(self):
        types = {"交货日期要求": "date"}
        self.assertEqual("20260902", StructuredOrderExtractor._normalize_row({"交货日期要求": "2026年09月02日"}, types)["交货日期要求"])
        self.assertEqual("20260902", StructuredOrderExtractor._normalize_row({"交货日期要求": date(2026, 9, 2)}, types)["交货日期要求"])
