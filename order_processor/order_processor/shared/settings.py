"""项目运行配置。"""

import os
from pathlib import Path


def load_project_env() -> bool:
    """从项目根目录 .env 加载缺失的环境变量，不覆盖系统环境变量。"""
    # settings.py 位于 order_processor/shared/；项目根目录需要再向上一级。
    env_path = Path(__file__).resolve().parents[2] / ".env"
    if not env_path.is_file():
        return False
    for raw_line in env_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip("'\""))
    return True
