"""SQLite 多级规则库。"""

from __future__ import annotations

import sqlite3
import json
from pathlib import Path
from typing import Iterable, List

from order_processor.domain.rule import Rule


class RuleRepository:
    """管理 客户 -> 规则组 -> 规则 的本地 SQLite 规则库。"""

    def __init__(self, database_path: str | Path = "data/rules.db"):
        self.database_path = Path(database_path)

    def initialize(self) -> None:
        """创建数据库及表结构（已存在时不会覆盖现有规则）。"""
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            if self._has_category_schema(conn):
                self._migrate_category_schema(conn)
            conn.executescript(
                """
                PRAGMA foreign_keys = ON;

                CREATE TABLE IF NOT EXISTS customers (
                    id INTEGER PRIMARY KEY,
                    customer_code TEXT NOT NULL UNIQUE,
                    customer_name TEXT NOT NULL UNIQUE,
                    enabled INTEGER NOT NULL DEFAULT 1
                );
                CREATE TABLE IF NOT EXISTS rule_groups (
                    id INTEGER PRIMARY KEY,
                    customer_id INTEGER NOT NULL REFERENCES customers(id),
                    name TEXT NOT NULL,
                    sort_order INTEGER NOT NULL DEFAULT 0,
                    enabled INTEGER NOT NULL DEFAULT 1,
                    UNIQUE(customer_id, name)
                );
                CREATE TABLE IF NOT EXISTS rules (
                    id TEXT PRIMARY KEY,
                    group_id INTEGER NOT NULL REFERENCES rule_groups(id),
                    name TEXT NOT NULL,
                    condition_expression TEXT NOT NULL,
                    action_description TEXT NOT NULL,
                    priority INTEGER NOT NULL DEFAULT 0,
                    enabled INTEGER NOT NULL DEFAULT 1,
                    version TEXT NOT NULL DEFAULT 'v1',
                    compiled_code TEXT,
                    task_type TEXT NOT NULL DEFAULT 'deterministic',
                    input_fields TEXT NOT NULL DEFAULT '[]',
                    output_fields TEXT NOT NULL DEFAULT '[]',
                    executor_name TEXT
                    ,executor_config TEXT NOT NULL DEFAULT '{}'
                    ,status TEXT NOT NULL DEFAULT 'active'
                );
                CREATE INDEX IF NOT EXISTS idx_rules_group_enabled ON rules(group_id, enabled);
                CREATE TABLE IF NOT EXISTS input_fields (
                    field_id TEXT PRIMARY KEY,
                    display_name TEXT NOT NULL UNIQUE,
                    data_type TEXT NOT NULL DEFAULT 'text',
                    enabled INTEGER NOT NULL DEFAULT 1
                );
                CREATE TABLE IF NOT EXISTS erp_fields (
                    field_id TEXT PRIMARY KEY,
                    customer_id INTEGER REFERENCES customers(id),
                    display_name TEXT NOT NULL UNIQUE,
                    data_type TEXT NOT NULL DEFAULT 'text',
                    sort_order INTEGER NOT NULL,
                    default_export INTEGER NOT NULL DEFAULT 1,
                    enabled INTEGER NOT NULL DEFAULT 1
                );
                """
            )
            self._ensure_rule_columns(conn)
            self._migrate_field_catalog(conn)
            self._migrate_to_meaningful_field_ids(conn)
            conn.execute("DROP TABLE IF EXISTS input_field_aliases")
            conn.execute("DROP TABLE IF EXISTS customer_erp_field_settings")
            self._remove_obsolete_cache_columns(conn)
            conn.execute("CREATE INDEX IF NOT EXISTS idx_rules_group_enabled ON rules(group_id, enabled)")
            conn.executescript(
                """
                DROP VIEW IF EXISTS v_rules_overview;
                CREATE VIEW v_rules_overview AS
                SELECT c.customer_code, c.customer_name, g.name AS group_name,
                       r.id AS rule_id, r.name AS rule_name, r.condition_expression,
                       r.action_description, r.task_type, r.input_fields, r.output_fields,
                       r.executor_name, r.executor_config, r.priority, r.enabled, r.status, r.version, r.compiled_code
                FROM rules r
                JOIN rule_groups g ON g.id = r.group_id
                JOIN customers c ON c.id = g.customer_id;
                CREATE TRIGGER IF NOT EXISTS clear_compiled_code_on_rule_change
                AFTER UPDATE OF condition_expression, action_description, task_type,
                                input_fields, output_fields, executor_name ON rules
                FOR EACH ROW
                BEGIN
                    UPDATE rules SET compiled_code = NULL WHERE id = NEW.id;
                END;
                """
            )

    def is_empty(self) -> bool:
        with self._connect() as conn:
            return conn.execute("SELECT COUNT(*) FROM rules").fetchone()[0] == 0

    def seed(self, rules: Iterable[Rule]) -> None:
        """首次初始化时导入规则；已有库不应重复调用。"""
        with self._connect() as conn:
            for rule in rules:
                group_name = self._infer_group(rule)
                customer_id = self._get_or_create_customer(conn, "COMMON", "通用规则")
                group_id = self._get_or_create_group(conn, customer_id, group_name)
                conn.execute(
                    """INSERT INTO rules
                       (id, group_id, name, condition_expression, action_description, priority, enabled, version)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                    (rule.id, group_id, rule.name, rule.condition, rule.action_description,
                     rule.priority, int(rule.enabled), rule.version),
                )

    def load_active_rules(self, customer_code: str = "COMMON") -> List[Rule]:
        """按稳定客户代码读取通用规则及指定客户的已启用规则。"""
        query = """
            SELECT r.id, r.name, r.condition_expression, r.action_description,
                   r.priority, r.enabled, r.version, r.compiled_code,
                   r.task_type, r.input_fields, r.output_fields, r.executor_name, r.executor_config
            FROM rules r
            JOIN rule_groups g ON g.id = r.group_id AND g.enabled = 1
            JOIN customers c ON c.id = g.customer_id AND c.enabled = 1
            WHERE r.enabled = 1
              AND (
                    c.customer_code = ?
                    OR (
                        c.customer_code = 'COMMON'
                        AND NOT EXISTS (
                            SELECT 1
                            FROM rule_groups customer_group
                            JOIN customers customer ON customer.id = customer_group.customer_id
                            WHERE customer.customer_code = ?
                              AND customer.enabled = 1
                              AND customer_group.enabled = 1
                              AND customer_group.name = g.name
                        )
                    )
                  )
            ORDER BY CASE WHEN c.customer_code = 'COMMON' THEN 0 ELSE 1 END,
                     g.sort_order, r.priority ASC, r.id
        """
        with self._connect() as conn:
            field_names = self._field_display_names(conn)
            return [
                Rule(id=row[0], name=row[1], condition=self._decode_expression(row[2], field_names), action_description=row[3],
                     priority=row[4], enabled=bool(row[5]), version=row[6],
                     compiled_code=row[7], task_type=row[8],
                     input_fields=[field_names.get(value, value) for value in json.loads(row[9] or "[]")],
                     output_fields=[field_names.get(value, value) for value in json.loads(row[10] or "[]")],
                     executor_name=row[11], executor_config=json.loads(row[12] or "{}"))
                for row in conn.execute(query, (customer_code, customer_code))
            ]

    def output_columns(self, customer_code: str) -> List[str]:
        """返回该客户启用的固定 ERP 输出列，按 ERP 模板顺序排列。"""
        with self._connect() as conn:
            rows = conn.execute(
                """SELECT e.display_name, e.sort_order FROM erp_fields e
                   LEFT JOIN customers c ON c.id = e.customer_id
                   WHERE e.enabled = 1 AND (e.customer_id IS NULL OR c.customer_code = ?)
                   ORDER BY sort_order""", (customer_code,)
            ).fetchall()
            return [row[0] for row in rows]

    def hierarchy(self) -> list[tuple[str, str, str]]:
        """返回用于管理界面或日志展示的规则树扁平视图。"""
        with self._connect() as conn:
            return conn.execute(
                """SELECT c.customer_name, g.name, r.name FROM rules r
                   JOIN rule_groups g ON g.id = r.group_id
                   JOIN customers c ON c.id = g.customer_id
                   ORDER BY c.customer_name, g.sort_order, r.priority DESC"""
            ).fetchall()

    def admin_snapshot(self) -> dict[str, list[tuple]]:
        """供管理端展示的字段与规则概览；不向用户处理端暴露写权限。"""
        with self._connect() as conn:
            return {
                "customers": conn.execute("SELECT customer_code, customer_name, enabled FROM customers ORDER BY customer_code").fetchall(),
                "inputs": conn.execute("SELECT field_id, display_name, data_type, enabled FROM input_fields ORDER BY field_id").fetchall(),
                "erp": conn.execute("""SELECT e.field_id, e.display_name, c.customer_code, e.sort_order, e.enabled
                                      FROM erp_fields e LEFT JOIN customers c ON c.id=e.customer_id
                                      ORDER BY e.sort_order""").fetchall(),
                "rules": conn.execute("""SELECT c.customer_code, g.name, r.name, r.condition_expression, r.enabled
                                        FROM rules r JOIN rule_groups g ON g.id=r.group_id
                                        JOIN customers c ON c.id=g.customer_id
                                        ORDER BY c.customer_code, g.sort_order, r.priority, r.id""").fetchall(),
            }

    def create_customer(self, code: str, name: str) -> None:
        with self._connect() as conn:
            conn.execute("INSERT INTO customers(customer_code, customer_name, enabled) VALUES (?, ?, 1)", (code, name))

    def update_customer(self, code: str, name: str, enabled: bool) -> None:
        with self._connect() as conn:
            conn.execute("UPDATE customers SET customer_name=?, enabled=? WHERE customer_code=?", (name, int(enabled), code))

    def save_compiled_code(self, rule_id: str, version: str, code: str) -> None:
        """保存已审核/生成的规则代码；规则版本变化时旧缓存自动失效。"""
        with self._connect() as conn:
            conn.execute(
                """UPDATE rules
                   SET compiled_code = ?
                   WHERE id = ?""",
                (code, rule_id),
            )

    def clear_compiled_code(self, rule_id: str) -> None:
        """清除无效的规则编译缓存，使其在下次执行时重新由模型编译。"""
        with self._connect() as conn:
            conn.execute("UPDATE rules SET compiled_code = NULL WHERE id = ?", (rule_id,))

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.database_path)
        conn.execute("PRAGMA foreign_keys = ON")
        return conn

    @staticmethod
    def _decode_expression(expression: str, field_names: dict[str, str]) -> str:
        for field_id, display_name in sorted(field_names.items(), key=lambda item: len(item[0]), reverse=True):
            expression = expression.replace(field_id, display_name)
        return expression

    @staticmethod
    def _field_display_names(conn: sqlite3.Connection) -> dict[str, str]:
        return dict(conn.execute("SELECT field_id, display_name FROM input_fields UNION ALL SELECT field_id, display_name FROM erp_fields"))

    @staticmethod
    def _migrate_to_meaningful_field_ids(conn: sqlite3.Connection) -> None:
        """将顺序号字段 ID 一次性迁移为可读、稳定的业务 ID。"""
        if not (conn.execute("SELECT 1 FROM erp_fields WHERE field_id LIKE 'erp_%'").fetchone() or conn.execute("SELECT 1 FROM input_fields WHERE field_id GLOB 'input_[0-9]*'").fetchone()):
            return
        input_ids = {
            "AS订单号": "input_as_order_number", "wbs号": "input_wbs_number", "产品型号": "input_product_model",
            "原始订单序号": "input_original_order_line", "子表的备注": "input_subtable_note", "客户代码": "input_customer_code",
            "客户名称": "input_customer_name", "客户提供的最小包装数量": "input_minimum_package_quantity",
            "客户明确提供的虚拟编码": "input_virtual_code", "客户明确提供的项目名称": "input_project_name",
            "客户物资编码": "input_customer_material_code",
            "客户订单需求日期": "input_customer_order_due_date", "客户需求日期": "input_customer_due_date",
            "当前日期": "input_current_date", "物料编号": "input_material_code", "物料长描述": "input_material_description",
            "用户订单序号": "input_user_order_line", "计划号": "input_plan_number", "请购编号": "input_requisition_number",
            "采购数量": "input_purchase_quantity", "验收要求": "input_acceptance_requirement", "代码": "input_code",
        }
        erp_ids = {
            "客户编码": "erp_customer_code", "生产标识（主）": "erp_main_production_mark", "订单号": "erp_order_number",
            "计划标记（主）": "erp_main_plan_mark", "需求日期": "erp_required_date", "收件人": "erp_recipient",
            "下计划依据": "erp_planning_basis", "主表备注": "erp_main_note", "明细序号": "erp_line_number",
            "型号": "erp_model", "料号": "erp_material_number", "客户型号": "erp_customer_model",
            "生产标识（子）": "erp_sub_production_mark", "计划标记（子）": "erp_sub_plan_mark",
            "订货数量": "erp_order_quantity", "单价": "erp_unit_price", "不含税单价": "erp_unit_price_excluding_tax",
            "交货日期": "erp_delivery_date", "最小包装数量": "erp_minimum_package_quantity", "子表备注": "erp_sub_note",
            "虚拟编码": "erp_virtual_code", "客户序号": "erp_customer_line_number", "物资编码": "erp_material_code",
            "客户订单需求日期": "erp_customer_order_due_date", "项目名称": "erp_project_name", "质量等级": "erp_quality_grade", "合同号": "erp_contract_number",
        }
        replacements = {}
        for table, mapping in (("input_fields", input_ids), ("erp_fields", erp_ids)):
            for old_id, name in conn.execute(f"SELECT field_id, display_name FROM {table}"):
                new_id = mapping.get(name)
                if new_id and old_id != new_id:
                    replacements[old_id] = new_id
        for old_id, new_id in replacements.items():
            conn.execute("UPDATE rules SET condition_expression=replace(condition_expression, ?, ?), input_fields=replace(input_fields, ?, ?), output_fields=replace(output_fields, ?, ?), compiled_code=NULL", (old_id, new_id, old_id, new_id, old_id, new_id))
        for old_id, new_id in replacements.items():
            if conn.execute("SELECT 1 FROM input_fields WHERE field_id=?", (old_id,)).fetchone():
                conn.execute("UPDATE input_fields SET field_id=? WHERE field_id=?", (new_id, old_id))
            else:
                conn.execute("UPDATE erp_fields SET field_id=? WHERE field_id=?", (new_id, old_id))

    @staticmethod
    def _migrate_field_catalog(conn: sqlite3.Connection) -> None:
        """一次性将旧规则中的中文字段引用改为稳定 ID；运行时再还原显示名。"""
        if conn.execute("SELECT COUNT(*) FROM erp_fields").fetchone()[0]:
            return
        output_columns = [
            "客户编码", "生产标识（主）", "订单号", "计划标记（主）", "需求日期", "收件人", "下计划依据", "主表备注",
            "明细序号", "型号", "料号", "客户型号", "生产标识（子）", "计划标记（子）", "订货数量", "单价",
            "不含税单价", "交货日期", "最小包装数量", "子表备注", "虚拟编码", "客户序号", "物资编码",
            "客户订单需求日期", "项目名称",
        ]
        name_to_id: dict[str, str] = {}
        for index, name in enumerate(output_columns, 1):
            field_id = f"erp_{index:03d}"
            conn.execute("INSERT INTO erp_fields(field_id, display_name, sort_order) VALUES (?, ?, ?)", (field_id, name, index))
            name_to_id[name] = field_id
        input_names: set[str] = set()
        for inputs, outputs in conn.execute("SELECT input_fields, output_fields FROM rules"):
            input_names.update(json.loads(inputs or "[]"))
            for name in json.loads(outputs or "[]"):
                if name not in name_to_id:
                    field_id = f"erp_{len(name_to_id) + 1:03d}"
                    conn.execute("INSERT INTO erp_fields(field_id, display_name, sort_order, default_export) VALUES (?, ?, ?, 0)", (field_id, name, len(name_to_id) + 1))
                    name_to_id[name] = field_id
        for name in sorted(input_names - set(name_to_id)):
            field_id = f"input_{len([key for key in name_to_id if key.startswith('__input__')]) + 1:03d}"
            conn.execute("INSERT INTO input_fields(field_id, display_name) VALUES (?, ?)", (field_id, name))
            name_to_id[name] = field_id
            name_to_id[f"__input__{field_id}"] = field_id
        replacements = {name: field_id for name, field_id in name_to_id.items() if not name.startswith("__input__")}
        for rule_id, condition, inputs, outputs in conn.execute("SELECT id, condition_expression, input_fields, output_fields FROM rules"):
            for name in sorted(replacements, key=len, reverse=True):
                condition = condition.replace(name, replacements[name])
            mapped_inputs = [replacements.get(name, name) for name in json.loads(inputs or "[]")]
            mapped_outputs = [replacements.get(name, name) for name in json.loads(outputs or "[]")]
            conn.execute("UPDATE rules SET condition_expression=?, input_fields=?, output_fields=?, compiled_code=NULL WHERE id=?", (condition, json.dumps(mapped_inputs, ensure_ascii=False), json.dumps(mapped_outputs, ensure_ascii=False), rule_id))

    @staticmethod
    def _ensure_rule_columns(conn: sqlite3.Connection) -> None:
        columns = {row[1] for row in conn.execute("PRAGMA table_info(rules)")}
        for column, definition in (
            ("compiled_code", "TEXT"),
            ("task_type", "TEXT NOT NULL DEFAULT 'deterministic'"),
            ("input_fields", "TEXT NOT NULL DEFAULT '[]'"),
            ("output_fields", "TEXT NOT NULL DEFAULT '[]'"),
            ("executor_name", "TEXT"),
            ("executor_config", "TEXT NOT NULL DEFAULT '{}'"),
            ("status", "TEXT NOT NULL DEFAULT 'active'"),
        ):
            if column not in columns:
                conn.execute(f"ALTER TABLE rules ADD COLUMN {column} {definition}")

    @staticmethod
    def _remove_obsolete_cache_columns(conn: sqlite3.Connection) -> None:
        """移除与 version 重复的编译元数据，保留实际可执行的 compiled_code。"""
        columns = {row[1] for row in conn.execute("PRAGMA table_info(rules)")}
        if not ({"compiled_version", "compiled_at"} & columns):
            return
        conn.executescript(
            """
            DROP INDEX IF EXISTS idx_rules_group_enabled;
            ALTER TABLE rules RENAME TO rules_cache_legacy;
            CREATE TABLE rules (
                id TEXT PRIMARY KEY,
                group_id INTEGER NOT NULL REFERENCES rule_groups(id),
                name TEXT NOT NULL,
                condition_expression TEXT NOT NULL,
                action_description TEXT NOT NULL,
                priority INTEGER NOT NULL DEFAULT 0,
                enabled INTEGER NOT NULL DEFAULT 1,
                version TEXT NOT NULL DEFAULT 'v1',
                compiled_code TEXT,
                task_type TEXT NOT NULL DEFAULT 'deterministic',
                input_fields TEXT NOT NULL DEFAULT '[]',
                output_fields TEXT NOT NULL DEFAULT '[]',
                executor_name TEXT
            );
            INSERT INTO rules
                (id, group_id, name, condition_expression, action_description, priority, enabled, version,
                 compiled_code, task_type, input_fields, output_fields, executor_name)
            SELECT id, group_id, name, condition_expression, action_description, priority, enabled, version,
                   compiled_code, task_type, input_fields, output_fields, executor_name
            FROM rules_cache_legacy;
            DROP TABLE rules_cache_legacy;
            """
        )

    @staticmethod
    def _has_category_schema(conn: sqlite3.Connection) -> bool:
        return conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'rule_categories'"
        ).fetchone() is not None

    @staticmethod
    def _migrate_category_schema(conn: sqlite3.Connection) -> None:
        """将“分类 -> 分组 -> 规则”迁移为“客户 -> 分组 -> 规则”。"""
        conn.executescript(
            """
            ALTER TABLE rule_groups RENAME TO rule_groups_legacy;
            ALTER TABLE rules RENAME TO rules_legacy;
            CREATE TABLE customers (
                id INTEGER PRIMARY KEY,
                customer_code TEXT NOT NULL UNIQUE,
                customer_name TEXT NOT NULL UNIQUE,
                enabled INTEGER NOT NULL DEFAULT 1
            );
            INSERT INTO customers (id, customer_code, customer_name) VALUES (1, 'COMMON', '通用规则');
            CREATE TABLE rule_groups (
                id INTEGER PRIMARY KEY,
                customer_id INTEGER NOT NULL REFERENCES customers(id),
                name TEXT NOT NULL,
                sort_order INTEGER NOT NULL DEFAULT 0,
                enabled INTEGER NOT NULL DEFAULT 1,
                UNIQUE(customer_id, name)
            );
            INSERT INTO rule_groups (id, customer_id, name, sort_order, enabled)
            SELECT g.id, 1, c.name || ' - ' || g.name, g.sort_order, g.enabled
            FROM rule_groups_legacy g JOIN rule_categories c ON c.id = g.category_id;
            CREATE TABLE rules (
                id TEXT PRIMARY KEY,
                group_id INTEGER NOT NULL REFERENCES rule_groups(id),
                name TEXT NOT NULL,
                condition_expression TEXT NOT NULL,
                action_description TEXT NOT NULL,
                priority INTEGER NOT NULL DEFAULT 0,
                enabled INTEGER NOT NULL DEFAULT 1,
                version TEXT NOT NULL DEFAULT 'v1'
                ,compiled_code TEXT
                ,compiled_version TEXT
                ,compiled_at TEXT
            );
            INSERT INTO rules SELECT * FROM rules_legacy;
            DROP TABLE rules_legacy;
            DROP TABLE rule_groups_legacy;
            DROP TABLE rule_categories;
            """
        )

    @staticmethod
    def _infer_group(rule: Rule) -> str:
        """把首次导入的通用规则归到一个规则组。"""
        if "日期" in rule.name or "日期" in rule.condition:
            return "交期规则"
        if "型号" in rule.name or "型号" in rule.condition:
            return "产品规则"
        return "默认规则组"

    @staticmethod
    def _get_or_create_customer(conn: sqlite3.Connection, code: str, name: str) -> int:
        conn.execute(
            "INSERT OR IGNORE INTO customers (customer_code, customer_name) VALUES (?, ?)", (code, name)
        )
        return conn.execute("SELECT id FROM customers WHERE customer_name = ?", (name,)).fetchone()[0]

    @staticmethod
    def _get_or_create_group(conn: sqlite3.Connection, customer_id: int, name: str) -> int:
        conn.execute(
            "INSERT OR IGNORE INTO rule_groups (customer_id, name) VALUES (?, ?)",
            (customer_id, name),
        )
        return conn.execute(
            "SELECT id FROM rule_groups WHERE customer_id = ? AND name = ?", (customer_id, name)
        ).fetchone()[0]
