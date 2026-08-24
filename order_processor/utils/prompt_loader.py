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
        file_path = cls.PROJECT_ROOT / "prompts" / filename
        
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
    def get_prompt_files(cls) -> list:
        """获取所有提示词文件列表"""
        prompt_dir = cls.PROJECT_ROOT / "prompts"  
        return [f.name for f in prompt_dir.glob("*.txt")]


# 使用示例
if __name__ == "__main__":
    # 加载原子函数说明书
    doc = PromptLoader.load_atomic_units_doc()
    print("原子函数说明书:")
    print(doc[:200] + "...")
    
    # 列出所有提示词文件
    print("\n可用的提示词文件:")
    for f in PromptLoader.get_prompt_files():
        print(f"  - {f}")