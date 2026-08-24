"""SQLite 多级规则库。"""

from __future__ import annotations

import sqlite3
import json
from pathlib import Path
from typing import Iterable, List

from models.rule import Rule


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
                """
            )
            self._ensure_rule_columns(conn)
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

    def load_active_rules(self, customer_name: str = "通用规则") -> List[Rule]:
        """读取通用规则及指定客户的已启用规则。"""
        query = """
            SELECT r.id, r.name, r.condition_expression, r.action_description,
                   r.priority, r.enabled, r.version, r.compiled_code,
                   r.task_type, r.input_fields, r.output_fields, r.executor_name, r.executor_config
            FROM rules r
            JOIN rule_groups g ON g.id = r.group_id AND g.enabled = 1
            JOIN customers c ON c.id = g.customer_id AND c.enabled = 1
            WHERE r.enabled = 1
              AND (
                    c.customer_name = ?
                    OR (
                        c.customer_name = '通用规则'
                        AND NOT EXISTS (
                            SELECT 1
                            FROM rule_groups customer_group
                            JOIN customers customer ON customer.id = customer_group.customer_id
                            WHERE customer.customer_name = ?
                              AND customer.enabled = 1
                              AND customer_group.enabled = 1
                              AND customer_group.name = g.name
                        )
                    )
                  )
            ORDER BY CASE WHEN c.customer_name = '通用规则' THEN 0 ELSE 1 END,
                     g.sort_order, r.priority ASC, r.id
        """
        with self._connect() as conn:
            return [
                Rule(id=row[0], name=row[1], condition=row[2], action_description=row[3],
                     priority=row[4], enabled=bool(row[5]), version=row[6],
                     compiled_code=row[7], task_type=row[8],
                     input_fields=json.loads(row[9] or "[]"), output_fields=json.loads(row[10] or "[]"),
                     executor_name=row[11], executor_config=json.loads(row[12] or "{}"))
                for row in conn.execute(query, (customer_name, customer_name))
            ]

    def hierarchy(self) -> list[tuple[str, str, str]]:
        """返回用于管理界面或日志展示的规则树扁平视图。"""
        with self._connect() as conn:
            return conn.execute(
                """SELECT c.customer_name, g.name, r.name FROM rules r
                   JOIN rule_groups g ON g.id = r.group_id
                   JOIN customers c ON c.id = g.customer_id
                   ORDER BY c.customer_name, g.sort_order, r.priority DESC"""
            ).fetchall()

    def save_compiled_code(self, rule_id: str, version: str, code: str) -> None:
        """保存已审核/生成的规则代码；规则版本变化时旧缓存自动失效。"""
        with self._connect() as conn:
            conn.execute(
                """UPDATE rules
                   SET compiled_code = ?
                   WHERE id = ?""",
                (code, rule_id),
            )

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.database_path)
        conn.execute("PRAGMA foreign_keys = ON")
        return conn

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
