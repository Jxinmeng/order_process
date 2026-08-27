import unittest

from order_processor.infrastructure.processing.developer_executors import normalize_j30_j29_length


class DeveloperExecutorTests(unittest.TestCase):
    def test_normalizes_metres_and_millimetres_for_j30_j29_models(self) -> None:
        cases = {
            "J30J-25TJL(L=0.5M)": "J30J-25TJL(L500)",
            "J30J-37TJL(L=500mm)": "J30J-37TJL(L500)",
            "J29-25TJP(L=0.3m)": "J29-25TJP(L300)",
            "J30J-25ZKL(L=0.5)": "J30J-25ZKL(L=0.5)",
            "ABC(L=0.5M)": "ABC(L=0.5M)",
        }
        for source, expected in cases.items():
            with self.subTest(source=source):
                self.assertEqual(expected, normalize_j30_j29_length({"型号": source})["型号"])


if __name__ == "__main__":
    unittest.main()
