"""代码执行器 - 执行LLM生成的代码"""

from typing import Dict, Any

from core.atomic_units import AtomicUnits


class CodeExecutor:
    """
    代码执行器
    在安全沙箱中执行LLM生成的代码
    """
    
    # 注入到执行环境的原子函数
    ATOMIC_FUNCTIONS = AtomicUnits.get_function_map()
    
    @classmethod
    def execute(cls, code: str, row: dict) -> Dict[str, Any]:
        """
        执行代码，返回处理后的row
        
        返回:
        {
            "success": True/False,
            "data": 处理后的row,
            "error": 错误信息（如有）
        }
        """
        safe_globals = {
            "__builtins__": {
                "print": print,
                "len": len,
                "str": str,
                "int": int,
                "float": float,
                "list": list,
                "dict": dict,
                "isinstance": isinstance,
            },
            **cls.ATOMIC_FUNCTIONS,
            "row": row.copy(),
        }
        
        try:
            exec(code, safe_globals)
            # 兼容历史模型输出的 ``def process(row): ...`` 封装；新提示词要求顶层代码，
            # 但旧缓存不应因只定义函数而静默丢失字段更新。
            processor = safe_globals.get("process")
            if callable(processor):
                returned = processor(safe_globals["row"])
                if isinstance(returned, dict):
                    safe_globals["row"] = returned
            result_row = safe_globals.get("row", row)
            return {"success": True, "data": result_row, "error": None}
        except Exception as e:
            return {"success": False, "data": row, "error": str(e)}
