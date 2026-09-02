"""按《订单字段规则表》逐行重建：客户 → ERP字段分组 → 具体规则。"""
import json, sqlite3, sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from order_processor.infrastructure.persistence.rule_repository import RuleRepository

# (分组, 名称, 条件, 动作, 输入, 输出, 状态)
RULES = [
 ("主表生产标识","固定主表生产标识","客户代码 contains ''","本客户的主表生产标识固定填写为 JHT。",["客户代码"],["生产标识（主）"],"active"),
 ("标准订单号","计划WBS代码拼接订单号","计划号 is not blank","订单号 = 计划号 + '-' + wbs号 + '-' + 代码。输入 wbs号已经是横杠前部分，代码已经完成向上填充；必须直接复制两者原文，不删除首字符、不补零、不截断。",["计划号","wbs号","代码"],["订单号"],"active"),
 ("主表计划标记","固定主表计划标记","客户代码 contains ''","本客户的主表计划标记固定填写为 YP017-X305-0486。",["客户代码"],["计划标记（主）"],"active"),
 ("主表需求日期","客户需求日期直接复制","客户需求日期 is not blank","客户明确提供需求日期时，直接复制并格式化为 yyyyMMdd。",["客户需求日期"],["需求日期"],"active"),
 ("主表需求日期","当前日期计算需求日期","客户需求日期 is blank and 当前日期 is not blank","仅当客户需求日期为空时，解析当前日期后调用 add_workdays(当前日期, 45)，写入格式为yyyyMMdd的需求日期。",["客户需求日期","当前日期"],["需求日期"],"active"),
 ("下计划依据","用户输入下计划依据","下计划依据 contains ''","将用户输入写入下计划依据；字段来源待确认",["下计划依据"],["下计划依据"],"pending"),
 ("主表备注","固定主表备注","客户代码 contains ''","本客户的主表备注固定填写为 200厂已验收。",["客户代码"],["主表备注"],"active"),
 ("明细序号","原始序号复制","原始订单序号 contains ''","将原始订单序号复制为明细序号",["原始订单序号"],["明细序号"],"active"),
 ("明细序号","缺失序号自动生成","原始订单序号 == ''","原始文件无序号时从1开始自动编号",["原始订单序号"],["明细序号"],"active"),
 ("型号","组合生成型号","产品型号 is not blank","产品型号已是物料长描述第一个与第二个 | 之间的内容。只读取产品型号一次作为原文：若原文包含 J30J，增加前缀 '(QJB/K)'；若质量等级包含 '/K'，增加后缀 '(J/K)'；两个条件可同时满足，必须在同一个最终型号中同时保留前缀和后缀。不得读取或二次加工输出字段型号。",["产品型号","质量等级"],["型号"],"active"),
 ("料号","型号转物料号","产品型号 starts with '21E6' or 产品型号 starts with '21E8'","读取已生成的型号：若型号以 21E6 或 21E8 开头，料号 = 型号且将型号清空；其他情况料号为空且型号保持不变。",["型号"],["料号","型号"],"active"),
 ("客户型号","客户型号复制","客户型号 contains ''","客户型号复制；为空则输出空",["客户型号"],["客户型号"],"active"),
 ("子表生产标识","复制主表生产标识","生产标识（主） contains ''","主表生产标识直接复制到子表生产标识；为空则输出空",["生产标识（主）"],["生产标识（子）"],"active"),
 ("子表计划标记","J599质量等级计划标记","产品型号 contains 'J599' and 产品型号 contains 'jy' and 质量等级 contains '/K'","型号属于 J599%jy% 且最终会以 (J/K) 结尾时，计划标记（子）固定为 YPX305/486/215。",["产品型号","质量等级"],["计划标记（子）"],"active"),
 ("子表计划标记","一院验收计划标记","验收要求 contains '一院'","满足条件时固定映射为 YP017-X486",["验收要求"],["计划标记（子）"],"active"),
 ("子表计划标记","默认子表计划标记","产品型号 contains ''","不满足J599+/K及验收要求含一院时，计划标记（子）固定为YP017-X305-0486",["产品型号","验收要求"],["计划标记（子）"],"active"),
 ("交货日期","当前日期计算交货日期","当前日期 is not blank","解析当前日期，调用 add_workdays(当前日期, 45)，再调用 move_to_previous_workday，写入格式为yyyyMMdd的交货日期。若结果为周末则前移到最近工作日。",["当前日期"],["交货日期"],"active"),
 ("最小包装数量","最小包装数量复制","客户提供的最小包装数量 contains ''","复制；为空则输出空",["客户提供的最小包装数量"],["最小包装数量"],"active"),
 ("子表备注","默认子表备注为空","验收要求 contains ''","子表备注默认留空。",["验收要求"],["子表备注"],"active"),
 ("子表备注","一院验收子表备注","验收要求 contains '一院'","验收要求包含“一院”时，子表备注固定填写为 一院验收。",["验收要求"],["子表备注"],"active"),
 ("虚拟编码","虚拟编码复制","客户明确提供的虚拟编码 contains ''","复制；为空则输出空",["客户明确提供的虚拟编码"],["虚拟编码"],"active"),
 ("客户物资编码","客户物资编码复制","客户物资编码 contains ''","客户物资编码复制；为空则输出空",["客户物资编码"],["物资编码"],"active"),
 ("客户订单需求日期","当前日期计算客户订单需求日期","当前日期 is not blank","解析当前日期后直接调用 add_workdays(当前日期, 45)，写入格式为yyyyMMdd的客户订单需求日期。不需要预先判断当前日期是否为周末，也不读取客户输入的客户订单需求日期。",["当前日期"],["客户订单需求日期"],"active"),
 ("项目名称","项目名称复制","客户明确提供的项目名称 contains ''","复制；为空则输出空",["客户明确提供的项目名称"],["项目名称"],"active"),
 ("输入预处理","代码向上填充","代码 is blank","客户订单的代码列为空时，填充为前面最近一个非空代码值。此规则在逐行规则匹配前执行。",["代码"],["代码"],"active"),
]

# 所有客户共用的直接复制规则；客户专属规则以更高优先级覆盖同一输出字段。
COMMON_RULES = [
 ("客户编码", "通用复制客户编码", "客户代码 contains ''", "客户代码直接复制为客户编码。", ["客户代码"], ["客户编码"], "direct_atomic", "copy_or_blank", {}, 10),
 ("收件人", "通用复制收件人", "客户名称 contains ''", "客户名称直接复制为收件人。", ["客户名称"], ["收件人"], "direct_atomic", "copy_or_blank", {}, 10),
 ("订货数量", "通用复制订货数量", "采购数量 contains ''", "采购数量直接复制为订货数量。", ["采购数量"], ["订货数量"], "direct_atomic", "copy_or_blank", {}, 10),
 ("单价", "通用复制单价", "单价 contains ''", "单价直接复制。", ["单价"], ["单价"], "direct_atomic", "copy_or_blank", {}, 10),
 ("不含税单价", "通用复制不含税单价", "不含税单价 contains ''", "不含税单价直接复制。", ["不含税单价"], ["不含税单价"], "direct_atomic", "copy_or_blank", {}, 10),
 ("最小包装数量", "通用复制最小包装数量", "最小包装数量 contains ''", "最小包装数量直接复制。", ["最小包装数量"], ["最小包装数量"], "direct_atomic", "copy_or_blank", {}, 10),
 ("虚拟编码", "通用复制虚拟编码", "虚拟编码 contains ''", "虚拟编码直接复制。", ["虚拟编码"], ["虚拟编码"], "direct_atomic", "copy_or_blank", {}, 10),
 ("客户序号", "通用复制客户序号", "客户序号 contains ''", "客户序号直接复制。", ["客户序号"], ["客户序号"], "direct_atomic", "copy_or_blank", {}, 10),
]

# 客户二：航天二院。产品型号为直接输入的原始型号，处理方式与 C001 一致。
C002_RULES = [
 ("标准订单号", "C002生成订单号", "订单号 is not blank", "输入订单号为纯编号，例如 20260306。生成输出订单号 = '773S-' + 输入订单号，例如 773S-20260306；只拼接一次，不重复增加前缀。", ["订单号"], ["订单号"], "deterministic", None, {}, 50),
 ("主表计划标记", "C002主计划标记默认", "客户代码 contains ''", "航天二院主表计划标记默认固定为 TYPC。", ["客户代码"], ["计划标记（主）"], "direct_atomic", "set_value", {"value": "TYPC"}, 10),
 ("主表计划标记", "C002主计划标记一院", "验收要求 contains '一院'", "验收要求包含一院时，主表计划标记固定为 YP017-X486。", ["验收要求"], ["计划标记（主）"], "direct_atomic", "set_value", {"value": "YP017-X486"}, 100),
 ("主表生产标识", "C002固定主生产标识", "客户代码 contains ''", "主表生产标识固定为 JHT。", ["客户代码"], ["生产标识（主）"], "direct_atomic", "set_value", {"value": "JHT"}, 50),
 ("子表生产标识", "C002复制子生产标识", "生产标识（主） contains ''", "主表生产标识复制到子表生产标识。", ["生产标识（主）"], ["生产标识（子）"], "direct_atomic", "copy_or_blank", {}, 50),
 ("主表备注", "C002固定主表备注", "客户代码 contains ''", "主表备注固定为 200厂已验收。", ["客户代码"], ["主表备注"], "direct_atomic", "set_value", {"value": "200厂已验收"}, 50),
 ("明细序号", "C002合同内序号", "用户订单序号 is not blank", "同一合同内按用户订单序号排序，从 1 开始连续编号。", ["合同号", "用户订单序号"], ["明细序号"], "preprocess", "number_within_contract", {"group_field": "合同号", "fallback_group_field": "计划号", "order_field": "用户订单序号", "target_field": "明细序号"}, 50),
 ("型号", "C002组合生成型号", "产品型号 is not blank", "产品型号就是直接输入的原始型号。只读取产品型号一次：原始型号含 J30J 时加前缀 (QJB/K)，质量等级含 /K 时加后缀 (J/K)，两个条件可同时叠加并写入同一个最终型号。不得二次加工输出字段型号。", ["产品型号", "质量等级"], ["型号"], "deterministic", None, {}, 50),
 ("料号", "C002型号转物料号", "产品型号 starts with '21E6' or 产品型号 starts with '21E8'", "读取已生成型号；型号以 21E6 或 21E8 开头时料号等于型号，型号清空；否则料号为空。", ["型号"], ["料号", "型号"], "deterministic", None, {}, 50),
 ("子表计划标记", "C002子计划标记默认", "客户代码 contains ''", "子表计划标记默认固定为 YP017-X305-0486。", ["客户代码"], ["计划标记（子）"], "direct_atomic", "set_value", {"value": "YP017-X305-0486"}, 10),
 ("子表计划标记", "C002子计划标记J599", "物料长描述 matches 'J599.*jy.*' and 质量等级 contains '/K'", "型号属于 J599%jy% 且以 (J/K) 结尾时，子表计划标记固定为 YPX305/486/215。", ["物料长描述", "质量等级"], ["计划标记（子）"], "direct_atomic", "set_value", {"value": "YPX305/486/215"}, 100),
 ("子表计划标记", "C002子计划标记一院", "验收要求 contains '一院'", "验收要求含一院时，子表计划标记固定为 YP017-X486。", ["验收要求"], ["计划标记（子）"], "direct_atomic", "set_value", {"value": "YP017-X486"}, 100),
 ("交货日期", "C002交货日期", "当前日期 is not blank", "解析当前日期后调用 add_workdays(当前日期, 45)，格式化为 yyyyMMdd 写入交货日期。", ["当前日期"], ["交货日期"], "deterministic", None, {}, 50),
 ("主表需求日期", "C002客户需求日期直接复制", "客户需求日期 is not blank", "客户明确提供需求日期时，直接复制并格式化为 yyyyMMdd。", ["客户需求日期"], ["需求日期"], "direct_atomic", "copy_or_blank", {}, 100),
 ("主表需求日期", "C002当前日期计算需求日期", "客户需求日期 is blank and 当前日期 is not blank", "仅当客户需求日期为空时，解析当前日期后调用 add_workdays(当前日期, 45)，格式化为 yyyyMMdd 写入需求日期。", ["客户需求日期", "当前日期"], ["需求日期"], "deterministic", None, {}, 10),
 ("子表备注", "C002子表备注默认", "验收要求 contains ''", "子表备注默认留空。", ["验收要求"], ["子表备注"], "direct_atomic", "set_blank", {}, 10),
 ("子表备注", "C002子表备注一院", "验收要求 contains '一院'", "验收要求含一院时，子表备注固定为 一院验收。", ["验收要求"], ["子表备注"], "direct_atomic", "set_value", {"value": "一院验收"}, 100),
]

# 客户三：航天三院。所有日期增量均为工作日。
C003_RULES = [
 ("主表生产标识", "C003主生产标识默认", "客户代码 contains ''", "主表生产标识默认固定为 JHT。", ["客户代码"], ["生产标识（主）"], "direct_atomic", "set_value", {"value": "JHT"}, 10),
 ("下计划依据", "C003邮件号映射", "客户代码 contains ''", "航天三院客户 ID 映射的邮件号固定为 3mail。", ["客户代码"], ["下计划依据"], "direct_atomic", "set_value", {"value": "3mail"}, 50),
 ("标准订单号", "C003复制AS订单号", "AS订单号 is not blank", "订单号直接复制 AS订单号。", ["AS订单号"], ["订单号"], "direct_atomic", "copy_or_blank", {}, 50),
 ("子表生产标识", "C003子生产标识质量等级映射", "质量等级 is not blank", "生产标识（子）按质量等级映射：JHT→JHT，CAST H1→CASTH，YB→YB，YC→YC，YH/厂家宇航→YH。", ["质量等级"], ["生产标识（子）"], "direct_atomic", "map_value", {"mapping": {"JHT": "JHT", "CAST H1": "CASTH", "YB": "YB", "YC": "YC", "YH": "YH", "厂家宇航": "YH", "YH/厂家宇航": "YH"}}, 10),
 ("子表计划标记", "C003计划标识按生产标识映射", "生产标识（子） contains ''", "计划标记（子）按最终生产标识（子）映射：CASTH→YPCASTH-A，YB→TYPC-B，YC→TYPC-C，YH→TYPC-A；未列出的生产标识不在本规则中强行赋值。", ["生产标识（子）"], ["计划标记（子）"], "direct_atomic", "map_value", {"mapping": {"CASTH": "YPCASTH-A", "YB": "TYPC-B", "YC": "TYPC-C", "YH": "TYPC-A"}}, 10),
 ("子表计划标记", "C003计划标识直接发货", "产品型号 matches 'RP.*\\(A\\)' and 子表的备注 contains '直接发货'", "RP%(A)% 等光连接器且子表备注为直接发货时，计划标识为 YPBKFDXJ。", ["产品型号", "子表的备注"], ["计划标记（子）"], "direct_atomic", "set_value", {"value": "YPBKFDXJ"}, 100),
 ("子表计划标记", "C003计划标识JHT", "产品型号 matches 'RP.*\\(A\\)' and 质量等级 == 'JHT'", "RP%(A)% 等光连接器且质量等级为 JHT 时，计划标识为 YPBKFDXJ。", ["产品型号", "质量等级"], ["计划标记（子）"], "direct_atomic", "set_value", {"value": "YPBKFDXJ"}, 100),
 ("子表计划标记", "C003计划标识BKFX", "产品型号 not matches 'RP.*\\(A\\)' and 子表的备注 contains '直接发货' or 产品型号 not matches 'RP.*\\(A\\)' and 质量等级 == 'JHT'", "非 RP%(A)% 等光连接器且子表备注为直接发货，或质量等级为 JHT 时，计划标记（子）为 BKFX。", ["产品型号", "子表的备注", "质量等级"], ["计划标记（子）"], "direct_atomic", "set_value", {"value": "BKFX"}, 80),
 ("子表计划标记", "C003计划标识一院", "产品型号 not matches 'RP.*\\(A\\)' and 子表的备注 contains '一院'", "非 RP%(A)% 等光连接器且子表备注含一院验收信息时，计划标记（子）为 YPBKFX017X486。该规则优先覆盖 BKFX。", ["产品型号", "子表的备注"], ["计划标记（子）"], "direct_atomic", "set_value", {"value": "YPBKFX017X486"}, 200),
 ("质量等级", "C003质量等级复制子生产标识", "生产标识（子） contains ''", "质量等级直接复制最终的生产标识（子），保证两字段一致。", ["生产标识（子）"], ["质量等级"], "direct_atomic", "copy_or_blank", {}, 50),
 ("型号", "C003复制产品型号", "产品型号 contains ''", "型号直接复制产品型号。", ["产品型号"], ["型号"], "direct_atomic", "copy_or_blank", {}, 50),
 ("合同拆分", "C003合同号与主表备注", "AS订单号 is not blank", "按子表备注拆分：普通为 -1，含五院为 -2，含一院为 -3。合同号为去掉 AS 前缀后的订单号加数字后缀；主表备注保留 AS 前缀，并为五院/一院增加验收说明。", ["AS订单号", "子表的备注"], ["合同号", "主表备注"], "direct_atomic", "classify_c003_contract", {}, 50),
 ("明细序号", "C003复制原始订单序号", "原始订单序号 contains ''", "明细序号直接复制输入表中的原始订单序号；不按拆分后的合同重新编号。", ["原始订单序号"], ["明细序号"], "direct_atomic", "copy_or_blank", {}, 50),
 ("子表备注", "C003子表备注", "请购编号 is not blank", "子表备注 = 请购编号 + '*' + 物料编号。星号必须位于两个字段之间，不能出现在末尾。", ["请购编号", "物料编号"], ["子表备注"], "deterministic", None, {}, 50),
 ("主表需求日期", "C003当前日期计算需求日期", "当前日期 is not blank", "解析当前日期后调用 add_workdays(当前日期, 45)，格式化为 yyyyMMdd 写入需求日期。", ["当前日期"], ["需求日期"], "deterministic", None, {}, 50),
 ("交货日期", "C003交货日期默认", "当前日期 is not blank", "解析当前日期；默认交货日期为当前日期加 45 个工作日，格式 yyyyMMdd。", ["当前日期"], ["交货日期"], "deterministic", None, {}, 10),
 ("交货日期", "C003交货日期JHT", "质量等级 == 'JHT'", "质量等级为 JHT 时，交货日期为当前日期加 25 个工作日，格式 yyyyMMdd。", ["当前日期", "质量等级"], ["交货日期"], "deterministic", None, {}, 80),
 ("交货日期", "C003交货日期五院验收", "子表的备注 contains '五院'", "在当前交货日期的基础上再加 7 个工作日；即 JHT 为当前日期加 32 个工作日，其他质量等级为当前日期加 52 个工作日，格式 yyyyMMdd。", ["当前日期", "质量等级", "子表的备注"], ["交货日期"], "deterministic", None, {}, 100),
]


def insert_customer_rules(conn, customer_code, customer_name, rules):
    """写入一个客户及其字段组、规则；规则记录保持同一张 rules 表。"""
    conn.execute("INSERT INTO customers (customer_code,customer_name,enabled) VALUES (?,?,1)", (customer_code, customer_name))
    customer_id = conn.execute("SELECT id FROM customers WHERE customer_code=?", (customer_code,)).fetchone()[0]
    groups = {}
    for index, group_name in enumerate(dict.fromkeys(rule[0] for rule in rules), 1):
        conn.execute("INSERT INTO rule_groups (customer_id,name,sort_order,enabled) VALUES (?,?,?,1)", (customer_id, group_name, index))
        groups[group_name] = conn.execute("SELECT id FROM rule_groups WHERE customer_id=? AND name=?", (customer_id, group_name)).fetchone()[0]
    for index, (group, name, condition, action, inputs, outputs, task_type, executor_name, executor_config, priority) in enumerate(rules, 1):
        conn.execute("""INSERT INTO rules
            (id,group_id,name,condition_expression,action_description,priority,enabled,version,task_type,input_fields,output_fields,executor_name,executor_config,status)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (f"{customer_code}-R{index:03}", groups[group], name, condition, action, priority, 1, "v1", task_type,
             json.dumps(inputs, ensure_ascii=False), json.dumps(outputs, ensure_ascii=False), executor_name,
             json.dumps(executor_config, ensure_ascii=False), "active"))


def normalize_rule_names(conn):
    """规则显示名称与规则 ID 采用相同的客户前缀，便于管理界面筛选。"""
    conn.execute("UPDATE rules SET name='COMMON-' || name WHERE id LIKE 'COMMON-%'")
    conn.execute("UPDATE rules SET name='C001-' || name WHERE id LIKE 'C001-%'")
    conn.execute("UPDATE rules SET name='C002-' || substr(name, 5) WHERE id LIKE 'C002-%'")
    conn.execute("UPDATE rules SET name='C003-' || substr(name, 5) WHERE id LIKE 'C003-%'")

def main():
 r=RuleRepository('data/rules.db'); r.initialize()
 with sqlite3.connect(r.database_path) as c:
  c.execute('PRAGMA foreign_keys=ON'); c.execute('DELETE FROM rules'); c.execute('DELETE FROM rule_groups'); c.execute('DELETE FROM customers')
  insert_customer_rules(c, "COMMON", "通用规则", COMMON_RULES)
  c.execute("INSERT INTO customers (customer_code,customer_name,enabled) VALUES ('C001','航天一院',1)"); cid=c.execute("SELECT id FROM customers WHERE customer_code='C001'").fetchone()[0]
  gids={}
  for i,g in enumerate(dict.fromkeys(x[0] for x in RULES),1):
   c.execute('INSERT INTO rule_groups (customer_id,name,sort_order,enabled) VALUES (?,?,?,1)',(cid,g,i)); gids[g]=c.execute('SELECT id FROM rule_groups WHERE customer_id=? AND name=?',(cid,g)).fetchone()[0]
  for i,(g,n,cond,act,ins,outs,status) in enumerate(RULES,1):
   c.execute('INSERT INTO rules (id,group_id,name,condition_expression,action_description,priority,enabled,version,task_type,input_fields,output_fields,status) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)',(f'C001-R{i:03}',gids[g],n,cond,act,i,status=='active','v1','deterministic',json.dumps(ins,ensure_ascii=False),json.dumps(outs,ensure_ascii=False),status))
  # 规则表中明确写为 copy / default_blank 的高频动作，改由本地原子单元直接执行。
  direct_names = ["客户代码直接复制", "客户名称复制收件人", "原始序号复制", "客户型号复制", "采购数量复制", "客户单价复制", "不含税单价复制", "最小包装数量复制", "虚拟编码复制", "客户序号复制", "客户物资编码复制", "项目名称复制", "复制主表生产标识", "客户需求日期直接复制"]
  placeholders = ','.join('?' for _ in direct_names)
  c.execute(f"UPDATE rules SET task_type='direct_atomic', executor_name='copy_or_blank' WHERE name IN ({placeholders})", direct_names)
  fixed_values = {
   "固定主表生产标识": "JHT", "固定主表计划标记": "YP017-X305-0486", "固定主表备注": "200厂已验收",
   "默认子表计划标记": "YP017-X305-0486", "一院验收子表备注": "一院验收",
  }
  for name, value in fixed_values.items():
   c.execute("UPDATE rules SET task_type='direct_atomic',executor_name='set_value',executor_config=? WHERE name=?", (json.dumps({"value": value}, ensure_ascii=False), name))
  c.execute("UPDATE rules SET task_type='direct_atomic',executor_name='set_blank' WHERE name='默认子表备注为空'")
  c.execute("UPDATE rules SET task_type='preprocess',executor_name='fill_down_from_previous',executor_config=? WHERE name='代码向上填充'", (json.dumps({"field": "代码"}, ensure_ascii=False),))
  plan_fixed_values = {
   "J599质量等级计划标记": "YPX305/486/215",
   "一院验收计划标记": "YP017-X486",
  }
  for name, value in plan_fixed_values.items():
   c.execute("UPDATE rules SET task_type='direct_atomic',executor_name='set_value',executor_config=? WHERE name=?", (json.dumps({"value": value}, ensure_ascii=False), name))
  # priority 在每个字段分组内独立定义：明确输入/特殊条件为 100，默认或兜底计算为 10。
  high = ["客户需求日期直接复制", "原始序号复制", "J599质量等级计划标记", "一院验收计划标记", "一院验收子表备注"]
  low = ["当前日期计算需求日期", "缺失序号自动生成", "默认子表计划标记", "默认子表备注为空"]
  c.execute(f"UPDATE rules SET priority=100 WHERE name IN ({','.join('?' for _ in high)})", high)
  c.execute(f"UPDATE rules SET priority=10 WHERE name IN ({','.join('?' for _ in low)})", low)
  insert_customer_rules(c, "C002", "航天二院", C002_RULES)
  insert_customer_rules(c, "C003", "航天三院", C003_RULES)
  normalize_rule_names(c)
 print(f'已导入 {len(RULES)} 条规则，其中待确认禁用 {sum(x[-1]=="pending" for x in RULES)} 条。')
if __name__=='__main__': main()
