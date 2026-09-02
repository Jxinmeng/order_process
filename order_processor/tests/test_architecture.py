"""架构边界的轻量回归测试，不依赖模型服务。"""

from pathlib import Path
import unittest

from order_processor.application.services import ProcessOrders


class _ProcessorStub:
    def process(self, input_path: str, output_path: str) -> dict:
        return {"input": input_path, "output": output_path}


class ArchitectureTests(unittest.TestCase):
    def test_application_depends_on_port_not_implementation(self) -> None:
        result = ProcessOrders(_ProcessorStub()).execute("in.xlsx", "out.xlsx")
        self.assertEqual({"input": "in.xlsx", "output": "out.xlsx"}, result)

    def test_agno_is_isolated_to_its_adapter(self) -> None:
        root = Path(__file__).resolve().parents[1] / "order_processor"
        agno_importers = []
        for source in root.rglob("*.py"):
            if "import agno" in source.read_text(encoding="utf-8") or "from agno" in source.read_text(encoding="utf-8"):
                agno_importers.append(source.relative_to(root).as_posix())
        self.assertEqual(
            ["agentos.py", "infrastructure/agno_rule_agent.py", "infrastructure/ingestion/source_extractor.py"],
            agno_importers,
        )

    def test_rule_agent_uses_agno_not_direct_openai_client(self) -> None:
        source = (Path(__file__).resolve().parents[1] / "order_processor" / "infrastructure" / "agno_rule_agent.py").read_text(encoding="utf-8")
        self.assertIn("from agno.agent import Agent", source)
        self.assertIn("from agno.models.openai import OpenAIChat", source)
        self.assertNotIn("from openai import OpenAI", source)


if __name__ == "__main__":
    unittest.main()
