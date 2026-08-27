"""将规则库中明确以自然日计算的交期/需求日期改为工作日。"""

from __future__ import annotations

import shutil
import sqlite3
from datetime import datetime
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
DATABASE = ROOT / "data" / "rules.db"


REPLACEMENTS = {
    "K-05-002-R005": ("需求日期为当前日期加35个工作日", "无条件执行：读取原始输入字段“当前日期”，将其加上 35 个工作日，并将结果以 yyyyMMdd 格式写入 ERP 字段“需求日期”；若“当前日期”为空或无法解析，则 ERP 字段“需求日期”写为空字符串。"),
    "K-05-002-R017": ("交货日期为当前日期加35个工作日并格式化", "无条件执行：读取原始输入字段“当前日期”，将其加上 35 个工作日，并将结果转换为 yyyyMMdd 格式后写入 ERP 字段“交货日期”；若“当前日期”为空或无法解析，则 ERP 字段“交货日期”写为空字符串。"),
    "K-06-035-R006": ("需求日期为当前日期加35个工作日", "无条件执行：读取原始输入字段“当前日期”，将其加上 35 个工作日，并将结果以 yyyyMMdd 格式写入 ERP 字段“需求日期”；若当前日期为空，则需求日期写为空。"),
    "K-06-035-R018": ("交货日期为当前日期加35个工作日并格式化为yyyyMMdd", "无条件执行：读取原始输入字段“当前日期”，将其加上 35 个工作日，并将结果格式化为“yyyyMMdd”后写入 ERP 字段“交货日期”；若当前日期为空，则交货日期写为空。"),
    "K-09-028-R005": ("需求日期为当前日期加45个工作日", "无条件执行：读取原始输入字段“当前日期”，将其加上 45 个工作日，并将结果以 yyyyMMdd 格式写入 ERP 字段“需求日期”；若“当前日期”为空或无法解析，则“需求日期”写为空字符串。"),
    "K-09-028-R014": ("交货日期为当前日期加45个工作日并格式化为yyyyMMdd", "无条件执行：读取原始输入字段“当前日期”，将其加上 45 个工作日，并将结果格式化为 yyyyMMdd 字符串写入 ERP 字段“交货日期”；若“当前日期”为空或无法解析，则“交货日期”写为空字符串。"),
    "K-17-1800-R004": ("需求日期为当前日期加45个工作日", "无条件执行：读取原始输入字段“当前日期”，将其转换为日期后增加 45 个工作日，并将结果以 yyyyMMdd 格式写入 ERP 字段“需求日期”；若“当前日期”为空或格式不正确，则保持“需求日期”为空。"),
    "K-17-1800-R011": ("交货日期为当前日期加30个工作日并格式化", "无条件执行：读取原始输入字段“当前日期”，将其转换为日期后增加 30 个工作日，并将结果以 yyyyMMdd 格式写入 ERP 字段“交货日期”；若“当前日期”为空或格式不正确，则保持“交货日期”为空。"),
    "K-17-206-R005": ("需求日期为当前日期加30个工作日", "无条件执行：读取原始输入字段“当前日期”，将其解析为日期后加 30 个工作日，将结果以 yyyyMMdd 格式写入 ERP 字段“需求日期”；若当前日期为空或格式无法解析，则 ERP 字段“需求日期”写为空字符串。"),
    "K-17-206-R014": ("交货日期为当前日期加30个工作日并格式化", "无条件执行：读取原始输入字段“当前日期”，将其解析为日期后加 30 个工作日，将结果以 yyyyMMdd 格式写入 ERP 字段“交货日期”；若当前日期为空或格式无法解析，则 ERP 字段“交货日期”写为空字符串。"),
    "K-24-004-R005": ("需求日期为当前日期加30个工作日", "无条件执行：读取原始输入字段“当前日期”，将其转换为日期后增加 30 个工作日，再将结果以 yyyyMMdd 格式写入 ERP 字段“需求日期”；若“当前日期”为空或格式无法解析，则“需求日期”写为空字符串。"),
}


def main() -> None:
    if not DATABASE.exists():
        raise FileNotFoundError(DATABASE)
    backup_dir = DATABASE.parent / "backups"
    backup_dir.mkdir(exist_ok=True)
    backup = backup_dir / f"rules-before-workday-normalization-{datetime.now():%Y%m%d-%H%M%S}.db"
    shutil.copy2(DATABASE, backup)

    with sqlite3.connect(DATABASE) as conn:
        changed = 0
        for rule_id, (name, action) in REPLACEMENTS.items():
            result = conn.execute(
                "UPDATE rules SET name=?, action_description=?, compiled_code=NULL WHERE id=?",
                (name, action, rule_id),
            )
            changed += result.rowcount
    print(f"备份：{backup}")
    print(f"已改为工作日计算并清除编译缓存：{changed} 条")


if __name__ == "__main__":
    main()
