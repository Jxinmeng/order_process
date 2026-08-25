"""开发者预置的复杂业务执行器。"""

from datetime import datetime, timedelta

from order_processor.infrastructure.processing.atomic_units import AtomicUnits


def _value(row: dict, name: str) -> str:
    """读取 Excel 字段，空值统一为无内容字符串。"""
    value = row.get(name)
    return "" if value is None else str(value).strip()


def _date_number(value: object, move_weekend_back: bool = False) -> str:
    """统一输出 yyyyMMdd；交期如遇周末，前移到最近工作日。"""
    if isinstance(value, (int, float)) and value > 1000:
        date = datetime(1899, 12, 30) + timedelta(days=value)
    else:
        date = AtomicUnits.parse_date(value)
    if not date:
        return ""
    if move_weekend_back:
        while date.weekday() >= 5:
            date = AtomicUnits.add_days(date, -1)
    return AtomicUnits.format_date(date, "%Y%m%d")


def calculate_complex_delivery(row: dict) -> dict:
    """案例：VIP 加急且金额超过 10 万时五天交付，否则十天交付。"""
    date = AtomicUnits.parse_date(AtomicUnits.get_field(row, "日期"))
    amount = float(row.get("订单金额") or 0)
    is_vip_urgent = row.get("客户等级") == "VIP" and row.get("交货日期要求") == "加急"
    days = 5 if is_vip_urgent and amount >= 100000 else 10
    if date:
        row["交货日期"] = AtomicUnits.format_date(AtomicUnits.add_days(date, days))
    row["交期说明"] = f"复杂交期算法：{days} 天"
    return row


EXECUTORS = {"calculate_complex_delivery": calculate_complex_delivery}


def transform_master_detail_order(row: dict) -> dict:
    """将一条扁平原始订单明细转换为 ERP 主表字段 + 子表字段。

    每个输入行代表一个子表明细；同一标准订单号的多行自然共享同一组主表字段。
    """
    plan, wbs, serial = _value(row, "计划号"), _value(row, "wbs号"), _value(row, "代码")
    wbs_prefix = wbs.split("-", 1)[0] if wbs else ""
    order_wbs = wbs_prefix[1:] if len(wbs_prefix) > 1 and wbs_prefix[0].isalpha() else wbs_prefix
    serial_text = str(int(float(serial))).zfill(4) if serial else ""
    standard_order = "-".join(part for part in (plan, order_wbs, serial_text) if part)
    production_map = {"C001": "JHT", "C002": "JHT2"}
    customer_code = _value(row, "客户代码")
    production = production_map.get(customer_code, customer_code)
    main_plan = f"YP017-{wbs_prefix}-{serial_text}" if wbs_prefix and serial_text else ""

    raw_model, quality = _value(row, "产品型号"), _value(row, "质量等级")
    material_no, model = "", raw_model
    if raw_model.startswith(("21E6", "21E8")):
        material_no, model = raw_model, ""
    elif "J30J" in raw_model:
        model = f"(QJB/K){raw_model}"
    elif "/K" in quality:
        model = f"{raw_model}(J/K)"

    acceptance = _value(row, "验收要求")
    if raw_model.startswith("J599") and "/K" in quality:
        detail_plan = f"YP{wbs_prefix}/{serial_text.lstrip('0')}/215" if wbs_prefix else ""
    elif "一院" in acceptance:
        detail_plan = f"YP017-X{serial_text.lstrip('0')}" if serial_text else ""
    else:
        detail_plan = main_plan

    line = _value(row, "原始订单序号")
    line_id = f"{standard_order}-{line.zfill(3)}" if standard_order and line else ""
    row.update({
        "主表编号": standard_order,
        "子表编号": line_id,
        "客户编码": customer_code,
        "生产标识（主）": production,
        "订单号": standard_order,
        "计划标记（主）": main_plan,
        "需求日期": _date_number(row.get("客户需求日期")),
        "收件人": _value(row, "客户名称"),
        "下计划依据": _value(row, "下计划依据"),
        "主表备注": _value(row, "订单备注"),
        "明细序号": line,
        "型号": model,
        "料号": material_no,
        "客户型号": _value(row, "客户型号"),
        "生产标识（子）": production,
        "计划标记（子）": detail_plan,
        "订货数量": row.get("采购数量", ""),
        "单价": row.get("客户订单中的单价", ""),
        "不含税单价": row.get("客户提供的不含税单价", ""),
        "交货日期": _date_number(row.get("交货日期"), move_weekend_back=True),
        "最小包装数量": row.get("客户提供的最小包装数量", ""),
        "子表备注": _value(row, "子表的备注"),
        "虚拟编码": _value(row, "客户明确提供的虚拟编码"),
        "客户序号": _value(row, "客户序号"),
        "物资编码": _value(row, "客户物资编码"),
        "客户订单需求日期": _date_number(row.get("客户订单需求日期")),
        "项目名称": _value(row, "客户明确提供的项目名称"),
    })
    return row


EXECUTORS["transform_master_detail_order"] = transform_master_detail_order
