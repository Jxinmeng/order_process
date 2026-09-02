"""将已确认一致的客户规则归并到 COMMON，并在修改前备份规则库。"""

from __future__ import annotations

import shutil
import sqlite3
import json
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from order_processor.infrastructure.persistence.rule_repository import RuleRepository


DATABASE = ROOT / "data" / "rules.db"


def group_id(conn: sqlite3.Connection, customer_code: str, erp_field_id: str) -> int:
    row = conn.execute(
        """SELECT g.id FROM field_rule_groups g JOIN customers c ON c.id=g.customer_id
           WHERE c.customer_code=? AND g.erp_field_id=?""",
        (customer_code, erp_field_id),
    ).fetchone()
    if row is None:
        raise RuntimeError(f"缺少规则组：{customer_code} / {erp_field_id}")
    return int(row[0])


def rule_exists(conn: sqlite3.Connection, name: str) -> bool:
    return conn.execute("SELECT 1 FROM rules WHERE name=?", (name,)).fetchone() is not None


def add_common_rules(repo: RuleRepository) -> None:
    definitions = [
        ("erp_main_production_mark", "COMMON-主表生产标识固定 JII", "always true", "无条件将主表生产标识写为 JII。", [], "direct_atomic", "set_value", {"value": "JII"}),
        ("erp_sub_production_mark", "COMMON-子表生产标识固定 JII", "always true", "无条件将子表生产标识写为 JII。", [], "direct_atomic", "set_value", {"value": "JII"}),
        ("erp_main_plan_mark", "COMMON-主表计划标记默认 W", "always true", "无条件将主表计划标记写为 W。客户特殊规则可在后续覆盖。", [], "direct_atomic", "set_value", {"value": "W"}),
        ("erp_sub_plan_mark", "COMMON-子表计划标记默认 W", "always true", "无条件将子表计划标记写为 W。客户特殊规则可在后续覆盖。", [], "direct_atomic", "set_value", {"value": "W"}),
        ("erp_delivery_date", "COMMON-交货日期默认当前日期加 30 个工作日", "当前日期 is not blank", "读取当前日期，加 30 个工作日后格式化为 yyyyMMdd 写入交货日期；当前日期为空或无法解析时写为空。客户特殊交期规则可在后续覆盖。", ["input_current_date"], "deterministic", None, {}),
        ("erp_required_date", "COMMON-需求日期默认当前日期加 30 个工作日", "always true", "读取当前日期，加 30 个工作日后格式化为 yyyyMMdd 写入需求日期；当前日期为空或无法解析时写为空。客户特殊需求日期规则可在后续覆盖。", ["input_current_date"], "deterministic", None, {}),
    ]
    with repo._connect() as conn:
        for field, name, condition, action, inputs, task, executor, config in definitions:
            if rule_exists(conn, name):
                continue
            group = conn.execute(
                """SELECT g.id FROM field_rule_groups g JOIN customers c ON c.id=g.customer_id
                   WHERE c.customer_code='COMMON' AND g.erp_field_id=?""", (field,)
            ).fetchone()
            if group is None:
                repo.create_field_rule_group("COMMON", field)
                group = conn.execute(
                    """SELECT g.id FROM field_rule_groups g JOIN customers c ON c.id=g.customer_id
                       WHERE c.customer_code='COMMON' AND g.erp_field_id=?""", (field,)
                ).fetchone()
            repo.create_rule(int(group[0]), name, condition, action, inputs, 10, task, executor, json.dumps(config, ensure_ascii=False), True)


def main() -> None:
    if not DATABASE.exists():
        raise FileNotFoundError(DATABASE)
    backup_dir = DATABASE.parent / "backups"
    backup_dir.mkdir(exist_ok=True)
    backup = backup_dir / f"rules-before-common-consolidation-{datetime.now():%Y%m%d-%H%M%S}.db"
    shutil.copy2(DATABASE, backup)

    repo = RuleRepository(DATABASE)
    repo.initialize()
    add_common_rules(repo)

    redundant_rule_ids = [
        # 七个客户完全相同的 JII 生产标识。
        "K-05-002-R002", "K-06-035-R002", "K-09-028-R002", "K-17-206-R002", "K-17-1800-R002", "K-24-004-R002", "K-25-001-R002",
        "K-05-002-R013", "K-06-035-R013", "K-09-028-R010", "K-17-206-R010", "K-17-1800-R007", "K-24-004-R006", "K-25-001-R006",
        # COMMON 已有的采购数量/单价复制规则。
        "K-05-002-R015", "K-06-035-R016", "K-09-028-R012", "K-17-206-R012", "K-17-1800-R009", "K-24-004-R008", "K-25-001-R008",
        "K-05-002-R016", "K-09-028-R013", "K-17-206-R013", "K-17-1800-R010", "K-24-004-R009", "K-25-001-R009",
        # K-05-002 与 COMMON 的型号、料号及明细序号规则完全相同。
        "K-05-002-R009", "K-05-002-R010", "K-05-002-R011", "K-05-002-R012",
        # 三个客户均使用计划标记 W。
        "K-05-002-R004", "K-06-035-R005", "K-09-028-R004",
        "K-05-002-R014", "K-06-035-R014", "K-09-028-R011",
        # 已明确为“当前日期 +30 个工作日”的交货日期默认规则。
        "K-24-004-R010", "K-25-001-R010",
        # 已明确为“当前日期 +30 个工作日”的需求日期默认规则。
        "K-17-206-R005", "K-24-004-R005", "K-25-001-R005",
    ]
    with repo._connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        placeholders = ",".join("?" for _ in redundant_rule_ids)
        result = conn.execute(f"DELETE FROM rules WHERE id IN ({placeholders})", redundant_rule_ids)
        conn.execute("DELETE FROM field_rule_groups WHERE id NOT IN (SELECT DISTINCT field_rule_group_id FROM rules)")
        print(f"备份：{backup}")
        print(f"已删除客户重复规则：{result.rowcount} 条")


if __name__ == "__main__":
    main()
