"""运行日志与大模型调用审计。"""

from __future__ import annotations

import sys
from datetime import datetime
from pathlib import Path
from typing import TextIO

_log_path = Path("logs/order_processor.log")


def set_log_path(path: str | Path) -> None:
    """设置本次运行（含 LLM 审计）的统一日志文件。"""
    global _log_path
    _log_path = Path(path)


class _Tee:
    """将 print 同时写入控制台和 UTF-8 日志文件。"""

    def __init__(self, console: TextIO, log_file: TextIO):
        self.console = console
        self.log_file = log_file

    def write(self, text: str) -> int:
        self.console.write(text)
        self.log_file.write(text)
        return len(text)

    def flush(self) -> None:
        self.console.flush()
        self.log_file.flush()

    def isatty(self) -> bool:
        return self.console.isatty()


class RunLog:
    """将本次运行的标准输出与错误输出追加到日志文件。"""

    def __init__(self, path: str | Path = "logs/order_processor.log"):
        self.path = Path(path)
        self._file: TextIO | None = None
        self._stdout: TextIO | None = None
        self._stderr: TextIO | None = None

    def __enter__(self) -> "RunLog":
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._file = self.path.open("a", encoding="utf-8")
        self._stdout, self._stderr = sys.stdout, sys.stderr
        sys.stdout = _Tee(self._stdout, self._file)
        sys.stderr = _Tee(self._stderr, self._file)
        print(f"\n{'=' * 70}\n运行开始: {datetime.now():%Y-%m-%d %H:%M:%S}\n{'=' * 70}")
        return self

    def __exit__(self, exc_type, exc_value, traceback) -> None:
        print(f"运行结束: {datetime.now():%Y-%m-%d %H:%M:%S}\n")
        sys.stdout, sys.stderr = self._stdout, self._stderr
        if self._file:
            self._file.close()


def log_llm_exchange(provider: str, model: str, prompt: str, response: str) -> None:
    """单独记录模型调用，便于在长运行日志中快速审计。"""
    path = _log_path
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as log_file:
        log_file.write(
            f"\n{'-' * 70}\n"
            f"LLM 调用时间: {datetime.now():%Y-%m-%d %H:%M:%S}\n"
            f"服务商: {provider}\n模型: {model}\n"
            f"[LLM 输入]\n{prompt}\n"
            f"[LLM 原始输出]\n{response}\n"
            f"{'-' * 70}\n"
        )
