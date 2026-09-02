"""提示词加载工具 - 从文件加载提示词模板"""

import os
from pathlib import Path
from typing import Optional


class PromptLoader:
    """加载提示词模板"""
    
    # 项目根目录
    PROJECT_ROOT = Path(__file__).parent.parent
    
    @classmethod
    def load(cls, filename: str) -> str:
        """
        从 prompts 目录加载提示词文件
        
        Args:
            filename: 文件名，如 "atomic_units_prompt.txt"
            
        Returns:
            文件内容
        """
        file_path = cls.PROJECT_ROOT / "infrastructure" / "prompts" / filename
        
        if not file_path.exists():
            raise FileNotFoundError(f"提示词文件不存在: {file_path}")
        
        with open(file_path, 'r', encoding='utf-8') as f:
            return f.read()
    
    @classmethod
    def load_atomic_units_doc(cls) -> str:
        """加载原子函数说明书"""
        return cls.load("atomic_units_prompt.txt")
    
    @classmethod
    def load_orchestrator_prompt(cls) -> str:
        """加载编排器提示词模板"""
        return cls.load("orchestrator_prompt.txt")

    @classmethod
    def load_rule_library_draft_prompt(cls) -> str:
        """加载自然语言规则库草稿生成提示词。"""
        return cls.load("rule_library_draft_prompt.txt")

    @classmethod
    def load_order_batch_extraction_prompt(cls) -> str:
        """加载合并批次的统一订单抽取提示词。"""
        return cls.load("order_batch_extraction_prompt.txt")
