"""SQLite 规则库：客户 → 预处理规则 / ERP 字段规则组 → 规则。"""

from __future__ import annotations

import json
import re
import sqlite3
from pathlib import Path
from typing import Any, List

from order_processor.domain.rule import Rule


class RuleRepository:
    """管理业务可维护的五类核心规则表。"""

    def __init__(self, database_path: str | Path = "data/rules.db"):
        self.database_path = Path(database_path)

    def initialize(self) -> None:
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            if self._table_exists(conn, "rule_groups"):
                self._migrate_legacy_rule_groups(conn)
            self._create_schema(conn)
            self._ensure_erp_field_ownership_column(conn)
            self._create_view(conn)

    def load_active_rules(self, customer_code: str = "COMMON") -> List[Rule]:
        """按规则组业务顺序、再按组内优先级读取 ERP 规则。"""
        query = """
            SELECT r.id, r.name, r.condition_expression, r.action_description,
                   r.priority, r.enabled, r.version, r.compiled_code, r.task_type,
                   r.input_fields, r.output_fields, r.executor_name, r.executor_config
              FROM rules r
              JOIN field_rule_groups g ON g.id = r.field_rule_group_id AND g.enabled = 1
              JOIN customers c ON c.id = g.customer_id AND c.enabled = 1
             WHERE r.enabled = 1 AND (
                 c.customer_code = ? OR
                 (c.customer_code = 'COMMON' AND NOT EXISTS (
                    SELECT 1 FROM field_rule_groups cg
                    JOIN customers cc ON cc.id = cg.customer_id
                    WHERE cc.customer_code = ? AND cc.enabled = 1 AND cg.enabled = 1
                      AND cg.erp_field_id = g.erp_field_id
                 ))
             )
               ORDER BY g.execution_order,
                        CASE WHEN c.customer_code = 'COMMON' THEN 0 ELSE 1 END,
                        r.priority, r.id
        """
        with self._connect() as conn:
            names = self._field_display_names(conn)
            return [self._rule_from_row(row, names) for row in conn.execute(query, (customer_code, customer_code))]

    def load_active_preprocess_rules(self, customer_code: str) -> list[dict[str, Any]]:
        """预处理规则在 Excel 读取后、ERP 字段规则执行前运行。"""
        query = """
            SELECT p.id, p.preprocess_type, p.input_field_id, p.execution_order, p.executor_config
              FROM preprocess_rules p JOIN customers c ON c.id = p.customer_id
             WHERE p.enabled = 1 AND c.enabled = 1 AND c.customer_code IN ('COMMON', ?)
             ORDER BY CASE WHEN c.customer_code = 'COMMON' THEN 0 ELSE 1 END, p.execution_order, p.id
        """
        with self._connect() as conn:
            names = self._field_display_names(conn)
            return [{"id": row[0], "preprocess_type": row[1], "input_field": names.get(row[2], row[2]),
                     "execution_order": row[3], "config": self._decode_config(json.loads(row[4] or "{}"), names)}
                    for row in conn.execute(query, (customer_code,))]

    def output_columns(self, customer_code: str) -> List[str]:
        with self._connect() as conn:
            return [row[0] for row in conn.execute(
                """SELECT e.display_name FROM erp_fields e
                   WHERE e.enabled=1 AND (e.owner_customer_code IS NULL OR e.owner_customer_code=?)
                   ORDER BY e.sort_order,e.field_id""", (customer_code,)
            )]

    def save_compiled_code(self, rule_id: str, version: str, code: str) -> None:
        with self._connect() as conn:
            conn.execute("UPDATE rules SET compiled_code=? WHERE id=?", (code, rule_id))

    def clear_compiled_code(self, rule_id: str) -> None:
        with self._connect() as conn:
            conn.execute("UPDATE rules SET compiled_code=NULL WHERE id=?", (rule_id,))

    def hierarchy(self) -> list[tuple[str, str, str]]:
        with self._connect() as conn:
            return conn.execute("""SELECT c.customer_name,e.display_name,r.name FROM rules r
                JOIN field_rule_groups g ON g.id=r.field_rule_group_id JOIN customers c ON c.id=g.customer_id
                JOIN erp_fields e ON e.field_id=g.erp_field_id ORDER BY c.customer_code,g.execution_order,r.priority""").fetchall()

    def admin_snapshot(self) -> dict[str, list[tuple]]:
        with self._connect() as conn:
            return {
                "customers": conn.execute("SELECT customer_code,customer_name,enabled FROM customers ORDER BY customer_code").fetchall(),
                "input_fields": conn.execute("SELECT field_id,display_name,data_type,enabled FROM input_fields ORDER BY field_id").fetchall(),
                  "erp_fields": conn.execute("SELECT field_id,display_name,sort_order,owner_customer_code,enabled FROM erp_fields ORDER BY sort_order").fetchall(),
                "preprocess_rules": conn.execute("SELECT p.id,c.customer_code,p.preprocess_type,p.execution_order,p.enabled FROM preprocess_rules p JOIN customers c ON c.id=p.customer_id ORDER BY c.customer_code,p.execution_order").fetchall(),
                "field_rule_groups": conn.execute("SELECT c.customer_code,e.display_name,g.execution_order,g.enabled FROM field_rule_groups g JOIN customers c ON c.id=g.customer_id JOIN erp_fields e ON e.field_id=g.erp_field_id ORDER BY c.customer_code,g.execution_order").fetchall(),
                "rules": conn.execute("SELECT c.customer_code,e.display_name,r.name,r.condition_expression,r.priority,r.enabled FROM rules r JOIN field_rule_groups g ON g.id=r.field_rule_group_id JOIN customers c ON c.id=g.customer_id JOIN erp_fields e ON e.field_id=g.erp_field_id ORDER BY c.customer_code,g.execution_order,r.priority").fetchall(),
            }

    def create_customer(self, code: str, name: str) -> None:
        with self._connect() as conn:
            conn.execute("INSERT INTO customers(customer_code,customer_name,enabled) VALUES(?,?,1)", (code, name))

    def update_customer(self, code: str, name: str, enabled: bool) -> None:
        with self._connect() as conn:
            conn.execute("UPDATE customers SET customer_name=?,enabled=? WHERE customer_code=?", (name, int(enabled), code))

    def field_catalog(self) -> dict[str, list[tuple]]:
        with self._connect() as conn:
            return {
                "customers": conn.execute("SELECT customer_code,customer_name FROM customers ORDER BY customer_code").fetchall(),
                "inputs": conn.execute("SELECT field_id,display_name,data_type,enabled FROM input_fields ORDER BY display_name").fetchall(),
                "erp": conn.execute("SELECT field_id,display_name,sort_order,owner_customer_code,enabled FROM erp_fields ORDER BY sort_order").fetchall(),
                "groups": conn.execute("SELECT g.id,c.customer_code,e.field_id,e.display_name,g.execution_order,g.enabled FROM field_rule_groups g JOIN customers c ON c.id=g.customer_id JOIN erp_fields e ON e.field_id=g.erp_field_id ORDER BY c.customer_code,g.execution_order").fetchall(),
                "rules": conn.execute("SELECT r.id,g.id,c.customer_code,e.display_name,r.name,r.condition_expression,r.action_description,r.input_fields,r.priority,r.task_type,r.executor_name,r.executor_config,r.enabled FROM rules r JOIN field_rule_groups g ON g.id=r.field_rule_group_id JOIN customers c ON c.id=g.customer_id JOIN erp_fields e ON e.field_id=g.erp_field_id ORDER BY c.customer_code,g.execution_order,r.priority").fetchall(),
            }

    def create_input_field(self, field_id: str, display_name: str, data_type: str = "text") -> None:
        with self._connect() as conn:
            conn.execute("INSERT INTO input_fields(field_id,display_name,data_type,enabled) VALUES(?,?,?,1)", (field_id, display_name, data_type))

    def update_input_field(self, field_id: str, display_name: str, data_type: str, enabled: bool) -> None:
        with self._connect() as conn:
            conn.execute("UPDATE input_fields SET display_name=?,data_type=?,enabled=? WHERE field_id=?", (display_name, data_type, int(enabled), field_id))

    def create_erp_field(self, field_id: str, display_name: str, sort_order: int, owner_customer_code: str | None = None) -> None:
        with self._connect() as conn:
            self._validate_erp_owner(conn, owner_customer_code)
            conn.execute("INSERT INTO erp_fields(field_id,display_name,sort_order,default_export,owner_customer_code,enabled) VALUES(?,?,?,?,?,1)", (field_id, display_name, sort_order, int(owner_customer_code is None), owner_customer_code or None))

    def update_erp_field(self, field_id: str, display_name: str, sort_order: int, owner_customer_code: str | None, enabled: bool) -> None:
        with self._connect() as conn:
            self._validate_erp_owner(conn, owner_customer_code)
            conn.execute("UPDATE erp_fields SET display_name=?,sort_order=?,default_export=?,owner_customer_code=?,enabled=? WHERE field_id=?", (display_name, sort_order, int(owner_customer_code is None), owner_customer_code or None, int(enabled), field_id))

    def create_field_rule_group(self, customer_code: str, erp_field_id: str, execution_order: int | None = None) -> int:
        with self._connect() as conn:
            owner = conn.execute("SELECT owner_customer_code FROM erp_fields WHERE field_id=?", (erp_field_id,)).fetchone()
            if owner is None:
                raise ValueError("ERP 字段不存在")
            if owner[0] and owner[0] != customer_code:
                raise ValueError(f"ERP 字段仅绑定客户 {owner[0]}，不能为 {customer_code} 建立规则组")
            customer_id = conn.execute("SELECT id FROM customers WHERE customer_code=?", (customer_code,)).fetchone()[0]
            if execution_order is None:
                # 客户覆盖通用字段时，沿用通用字段既有的业务执行顺序；
                # 只有新增字段才按创建先后追加，业务人员不必手填序号。
                inherited = conn.execute("""SELECT g.execution_order
                    FROM field_rule_groups g JOIN customers c ON c.id=g.customer_id
                    WHERE c.customer_code='COMMON' AND g.erp_field_id=?""", (erp_field_id,)).fetchone()
                if inherited is not None:
                    execution_order = inherited[0]
                else:
                    maximum = conn.execute("""SELECT COALESCE(MAX(g.execution_order), 0)
                        FROM field_rule_groups g JOIN customers c ON c.id=g.customer_id
                        WHERE c.customer_code IN ('COMMON', ?)""", (customer_code,)).fetchone()[0]
                    execution_order = maximum + 1
            conn.execute("INSERT INTO field_rule_groups(customer_id,erp_field_id,execution_order,enabled) VALUES(?,?,?,1)", (customer_id, erp_field_id, execution_order))
            group_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
            return group_id

    def update_field_rule_group(self, group_id: int, execution_order: int, enabled: bool) -> None:
        with self._connect() as conn:
            conn.execute("UPDATE field_rule_groups SET execution_order=?,enabled=? WHERE id=?", (execution_order, int(enabled), group_id))

    def common_rules_for_field(self, erp_field_id: str) -> list[tuple]:
        with self._connect() as conn:
            return conn.execute("""SELECT r.name,r.condition_expression,r.action_description,r.priority,r.task_type FROM rules r
                JOIN field_rule_groups g ON g.id=r.field_rule_group_id JOIN customers c ON c.id=g.customer_id
                WHERE c.customer_code='COMMON' AND g.erp_field_id=? AND r.enabled=1 ORDER BY r.priority""", (erp_field_id,)).fetchall()

    def create_rule(self, group_id: int, name: str, condition: str, action: str, input_field_ids: list[str], priority: int, task_type: str, executor_name: str | None, executor_config: str, enabled: bool) -> str:
        with self._connect() as conn:
            # 先获取写锁，再计算客户内编号，避免并发新增时产生相同的可读规则 ID。
            conn.execute("BEGIN IMMEDIATE")
            group = conn.execute("""SELECT g.erp_field_id,c.customer_code FROM field_rule_groups g
                JOIN customers c ON c.id=g.customer_id WHERE g.id=?""", (group_id,)).fetchone()
            if group is None:
                raise ValueError("规则组不存在")
            output, customer_code = group
            rule_id = self._next_rule_id(conn, customer_code)
            names = self._field_display_names(conn)
            encoded = self._encode_expression(condition, names)
            conn.execute("""INSERT INTO rules(id,field_rule_group_id,name,condition_expression,action_description,priority,enabled,version,task_type,input_fields,output_fields,executor_name,executor_config,status)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""", (rule_id, group_id, name, encoded, action, priority, int(enabled), "v1", task_type, json.dumps(input_field_ids, ensure_ascii=False), json.dumps([output], ensure_ascii=False), executor_name or None, executor_config or "{}", "active"))
            return rule_id

    @staticmethod
    def _next_rule_id(conn: sqlite3.Connection, customer_code: str) -> str:
        """为客户生成 C003-R018 形式的下一个可读规则 ID。"""
        pattern = re.compile(rf"{re.escape(customer_code)}-R(\d+)$")
        sequence = max((int(match.group(1)) for (rule_id,) in conn.execute("""SELECT r.id FROM rules r
            JOIN field_rule_groups g ON g.id=r.field_rule_group_id
            JOIN customers c ON c.id=g.customer_id WHERE c.customer_code=?""", (customer_code,))
            if (match := pattern.fullmatch(rule_id))), default=0)
        return f"{customer_code}-R{sequence + 1:03d}"

    def update_rule(self, rule_id: str, name: str, condition: str, action: str, input_field_ids: list[str], priority: int, task_type: str, executor_name: str | None, executor_config: str, enabled: bool) -> None:
        with self._connect() as conn:
            names = self._field_display_names(conn)
            conn.execute("""UPDATE rules SET name=?,condition_expression=?,action_description=?,input_fields=?,priority=?,task_type=?,executor_name=?,executor_config=?,enabled=?,compiled_code=NULL WHERE id=?""", (name, self._encode_expression(condition, names), action, json.dumps(input_field_ids, ensure_ascii=False), priority, task_type, executor_name or None, executor_config or "{}", int(enabled), rule_id))

    def _migrate_legacy_rule_groups(self, conn: sqlite3.Connection) -> None:
        """把旧 rule_groups/rules.group_id 原子迁移为字段规则组模型。"""
        conn.execute("PRAGMA foreign_keys=OFF")
        conn.executescript("ALTER TABLE rules RENAME TO rules_legacy; ALTER TABLE rule_groups RENAME TO rule_groups_legacy;")
        self._create_schema(conn)
        names = self._field_display_names(conn)
        erp_ids = {key for key, _ in conn.execute("SELECT field_id,display_name FROM erp_fields")}
        groups = conn.execute("SELECT id,customer_id,name,erp_field_id,sort_order,enabled FROM rule_groups_legacy ORDER BY customer_id,sort_order,id").fetchall()
        for old_id, customer_id, group_name, field_id, order, enabled in groups:
            old_rules = conn.execute("""SELECT id,name,condition_expression,action_description,priority,enabled,version,compiled_code,task_type,input_fields,output_fields,executor_name,executor_config,status
                FROM rules_legacy WHERE group_id=? ORDER BY priority,id""", (old_id,)).fetchall()
            for row in [x for x in old_rules if x[8] == "preprocess"]:
                inputs = json.loads(row[9] or "[]")
                input_id = inputs[-1] if row[11] == "number_within_contract" and inputs else (inputs[0] if inputs else "")
                conn.execute("INSERT INTO preprocess_rules(id,customer_id,input_field_id,preprocess_type,execution_order,executor_config,enabled) VALUES(?,?,?,?,?,?,?)",
                             (row[0], customer_id, input_id, row[11] or "custom", order, row[12] or "{}", row[5]))
            normal = [x for x in old_rules if x[8] != "preprocess"]
            if not normal:
                continue
            outputs = {field for row in normal for field in json.loads(row[10] or "[]")}
            if outputs == {"erp_material_number", "erp_model"}:
                model_group = self._insert_group(conn, customer_id, "erp_model", max(1, order - 1), enabled)
                material_group = self._insert_group(conn, customer_id, "erp_material_number", order, enabled)
                for row in normal:
                    self._insert_rule(conn, row, material_group, ["input_product_model"], ["erp_material_number"], "direct_atomic", "copy_or_blank", {})
                    clear = list(row); clear[0] = f"{row[0]}-CLEAR-MODEL"; clear[1] = f"{row[1]}-清空型号"; clear[3] = "原始产品型号以 21E6 或 21E8 开头时，型号输出为空；料号由料号字段规则独立生成。"
                    self._insert_rule(conn, tuple(clear), model_group, ["input_product_model"], ["erp_model"], "direct_atomic", "set_blank", {})
                continue
            if outputs == {"erp_contract_number", "erp_main_note"}:
                for target, offset in (("erp_contract_number", 0), ("erp_main_note", 1)):
                    group_id = self._insert_group(conn, customer_id, target, order + offset, enabled)
                    for row in normal:
                        split = list(row); split[0] = f"{row[0]}-{target}"; split[1] = f"{row[1]}-{names.get(target, target)}"
                        self._insert_rule(conn, tuple(split), group_id, output_fields=[target])
                continue
            target = field_id if field_id in erp_ids else next(iter(outputs & erp_ids), None)
            if not target:
                raise RuntimeError(f"无法为旧规则组 {group_name} 确定唯一 ERP 输出字段")
            group_id = self._insert_group(conn, customer_id, target, order, enabled)
            for row in normal:
                self._insert_rule(conn, row, group_id)
        conn.executescript("DROP TABLE rules_legacy; DROP TABLE rule_groups_legacy;")
        conn.execute("PRAGMA foreign_keys=ON")

    @staticmethod
    def _create_schema(conn: sqlite3.Connection) -> None:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS customers (id INTEGER PRIMARY KEY,customer_code TEXT NOT NULL UNIQUE,customer_name TEXT NOT NULL UNIQUE,enabled INTEGER NOT NULL DEFAULT 1);
            CREATE TABLE IF NOT EXISTS input_fields (field_id TEXT PRIMARY KEY,display_name TEXT NOT NULL UNIQUE,data_type TEXT NOT NULL DEFAULT 'text',enabled INTEGER NOT NULL DEFAULT 1);
            CREATE TABLE IF NOT EXISTS erp_fields (field_id TEXT PRIMARY KEY,display_name TEXT NOT NULL UNIQUE,data_type TEXT NOT NULL DEFAULT 'text',sort_order INTEGER NOT NULL,default_export INTEGER NOT NULL DEFAULT 1,owner_customer_code TEXT,enabled INTEGER NOT NULL DEFAULT 1);
            CREATE TABLE IF NOT EXISTS preprocess_rules (id TEXT PRIMARY KEY,customer_id INTEGER NOT NULL REFERENCES customers(id),input_field_id TEXT NOT NULL REFERENCES input_fields(field_id),preprocess_type TEXT NOT NULL,execution_order INTEGER NOT NULL DEFAULT 0,executor_config TEXT NOT NULL DEFAULT '{}',enabled INTEGER NOT NULL DEFAULT 1);
            CREATE TABLE IF NOT EXISTS field_rule_groups (id INTEGER PRIMARY KEY,customer_id INTEGER NOT NULL REFERENCES customers(id),erp_field_id TEXT NOT NULL REFERENCES erp_fields(field_id),execution_order INTEGER NOT NULL DEFAULT 0,enabled INTEGER NOT NULL DEFAULT 1,UNIQUE(customer_id,erp_field_id));
            CREATE TABLE IF NOT EXISTS rules (id TEXT PRIMARY KEY,field_rule_group_id INTEGER NOT NULL REFERENCES field_rule_groups(id),name TEXT NOT NULL,condition_expression TEXT NOT NULL,action_description TEXT NOT NULL,priority INTEGER NOT NULL DEFAULT 0,enabled INTEGER NOT NULL DEFAULT 1,version TEXT NOT NULL DEFAULT 'v1',compiled_code TEXT,task_type TEXT NOT NULL DEFAULT 'deterministic',input_fields TEXT NOT NULL DEFAULT '[]',output_fields TEXT NOT NULL DEFAULT '[]',executor_name TEXT,executor_config TEXT NOT NULL DEFAULT '{}',status TEXT NOT NULL DEFAULT 'active');
            CREATE INDEX IF NOT EXISTS idx_preprocess_customer_order ON preprocess_rules(customer_id,execution_order);
            CREATE INDEX IF NOT EXISTS idx_field_group_customer_order ON field_rule_groups(customer_id,execution_order);
            CREATE INDEX IF NOT EXISTS idx_rules_field_group_priority ON rules(field_rule_group_id,priority);
        """)

    @staticmethod
    def _ensure_erp_field_ownership_column(conn: sqlite3.Connection) -> None:
        columns = {row[1] for row in conn.execute("PRAGMA table_info(erp_fields)")}
        if "owner_customer_code" not in columns:
            conn.execute("ALTER TABLE erp_fields ADD COLUMN owner_customer_code TEXT")

    @staticmethod
    def _validate_erp_owner(conn: sqlite3.Connection, owner_customer_code: str | None) -> None:
        if not owner_customer_code:
            return
        if not conn.execute("SELECT 1 FROM customers WHERE customer_code=?", (owner_customer_code,)).fetchone():
            raise ValueError(f"客户 {owner_customer_code} 不存在")

    @staticmethod
    def _insert_group(conn: sqlite3.Connection, customer_id: int, erp_field_id: str, order: int, enabled: int) -> int:
        conn.execute("INSERT INTO field_rule_groups(customer_id,erp_field_id,execution_order,enabled) VALUES(?,?,?,?) ON CONFLICT(customer_id,erp_field_id) DO UPDATE SET execution_order=MIN(execution_order,excluded.execution_order)", (customer_id, erp_field_id, order, enabled))
        return conn.execute("SELECT id FROM field_rule_groups WHERE customer_id=? AND erp_field_id=?", (customer_id, erp_field_id)).fetchone()[0]

    @staticmethod
    def _insert_rule(conn: sqlite3.Connection, row: tuple, group_id: int, input_fields=None, output_fields=None, task_type=None, executor_name=None, executor_config=None) -> None:
        ins = input_fields if input_fields is not None else json.loads(row[9] or "[]")
        outs = output_fields if output_fields is not None else json.loads(row[10] or "[]")
        cfg = executor_config if executor_config is not None else json.loads(row[12] or "{}")
        conn.execute("""INSERT INTO rules(id,field_rule_group_id,name,condition_expression,action_description,priority,enabled,version,compiled_code,task_type,input_fields,output_fields,executor_name,executor_config,status) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (row[0],group_id,row[1],row[2],row[3],row[4],row[5],row[6],None,task_type or row[8],json.dumps(ins,ensure_ascii=False),json.dumps(outs,ensure_ascii=False),executor_name if executor_name is not None else row[11],json.dumps(cfg,ensure_ascii=False),row[13]))

    def _rule_from_row(self, row: tuple, names: dict[str, str]) -> Rule:
        return Rule(id=row[0],name=row[1],condition=self._decode_expression(row[2],names),action_description=row[3],priority=row[4],enabled=bool(row[5]),version=row[6],compiled_code=row[7],task_type=row[8],input_fields=[names.get(x,x) for x in json.loads(row[9] or "[]")],output_fields=[names.get(x,x) for x in json.loads(row[10] or "[]")],executor_name=row[11],executor_config=self._decode_config(json.loads(row[12] or "{}"),names))

    @staticmethod
    def _field_display_names(conn: sqlite3.Connection) -> dict[str, str]:
        return dict(conn.execute("SELECT field_id,display_name FROM input_fields UNION ALL SELECT field_id,display_name FROM erp_fields"))

    @staticmethod
    def _decode_expression(expression: str, names: dict[str, str]) -> str:
        for field_id, name in sorted(names.items(), key=lambda item: len(item[0]), reverse=True):
            expression = expression.replace(field_id, name)
        return expression

    @staticmethod
    def _encode_expression(expression: str, names: dict[str, str]) -> str:
        for field_id, name in sorted(names.items(), key=lambda item: len(item[1]), reverse=True):
            expression = expression.replace(name, field_id)
        return expression

    @staticmethod
    def _decode_config(config: dict[str, Any], names: dict[str, str]) -> dict[str, Any]:
        return {key: names.get(value,value) if isinstance(value,str) else value for key,value in config.items()}

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.database_path)
        conn.execute("PRAGMA foreign_keys=ON")
        return conn

    @staticmethod
    def _table_exists(conn: sqlite3.Connection, name: str) -> bool:
        return conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)).fetchone() is not None

    @staticmethod
    def _create_view(conn: sqlite3.Connection) -> None:
        conn.executescript("""
            DROP VIEW IF EXISTS v_rules_overview;
            CREATE VIEW v_rules_overview AS SELECT c.customer_code,c.customer_name,e.display_name AS erp_field_name,g.execution_order,r.id AS rule_id,r.name AS rule_name,r.condition_expression,r.action_description,r.priority,r.task_type,r.input_fields,r.output_fields,r.executor_name,r.executor_config,r.enabled,r.status,r.version,r.compiled_code FROM rules r JOIN field_rule_groups g ON g.id=r.field_rule_group_id JOIN customers c ON c.id=g.customer_id JOIN erp_fields e ON e.field_id=g.erp_field_id;
        """)
