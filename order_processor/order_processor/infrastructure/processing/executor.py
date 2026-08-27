"""代码执行器 - 执行LLM生成的代码"""

from typing import Dict, Any

from order_processor.infrastructure.processing.atomic_units import AtomicUnits


class CodeExecutor:
    """
    代码执行器
    在安全沙箱中执行LLM生成的代码
    """
    
    # 注入到执行环境的原子函数
    ATOMIC_FUNCTIONS = AtomicUnits.get_function_map()
    
    @classmethod
    def execute(cls, code: str, row: dict, runtime_state: dict | None = None) -> Dict[str, Any]:
        """
        执行代码，返回处理后的row
        
        返回:
        {
            "success": True/False,
            "data": 处理后的row,
            "error": 错误信息（如有）
        }
        """
        persistent_state = runtime_state if runtime_state is not None else {}
        safe_globals = {
            "__builtins__": {
                "print": print,
                "len": len,
                "str": str,
                "int": int,
                "float": float,
                "list": list,
                "set": set,
                "dict": dict,
                "isinstance": isinstance,
            },
            **cls.ATOMIC_FUNCTIONS,
            # 兼容规则编译器为跨行编号生成的 ``globals()`` 检查；只暴露
            # 本批次的私有规则状态，而不是完整的 Python 全局命名空间。
            "globals": lambda: persistent_state,
            "row": row.copy(),
            **persistent_state,
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
            # 仅持久化规则专用变量，避免把输入行或执行器函数带到下一行。
            persistent_state.update(
                {key: value for key, value in safe_globals.items() if key.startswith("_rule_")}
            )
            result_row = safe_globals.get("row", row)
            return {"success": True, "data": result_row, "error": None}
        except Exception as e:
            return {"success": False, "data": row, "error": str(e)}
