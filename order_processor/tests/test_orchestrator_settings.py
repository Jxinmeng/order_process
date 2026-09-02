import os
import unittest
from unittest.mock import patch

from order_processor.infrastructure.processing.orchestrator import LLMOrchestrator


class OrchestratorSettingsTests(unittest.TestCase):
    def test_reads_model_and_base_url_from_environment(self):
        with patch.dict(os.environ, {
            "DEEPSEEK_API_KEY": "test-key",
            "DEEPSEEK_MODEL": "deepseek-v4-flash-0731",
            "DEEPSEEK_BASE_URL": "https://dashscope.aliyuncs.com/compatible-mode/v1",
        }, clear=False):
            orchestrator = LLMOrchestrator()
        self.assertEqual("deepseek-v4-flash-0731", orchestrator.model)
        self.assertEqual("https://dashscope.aliyuncs.com/compatible-mode/v1", orchestrator.base_url)
