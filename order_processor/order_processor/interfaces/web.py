"""订单处理的轻量 Web 页面；不将 HTML 或 HTTP 细节泄漏到应用层。"""

from __future__ import annotations

import os
import json
import shutil
import zipfile
import hmac
import secrets
from html import escape
from pathlib import Path
from typing import List
from uuid import uuid4

from fastapi import Cookie, Depends, FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse

from order_processor.bootstrap import build_process_orders
from order_processor.infrastructure.persistence.rule_repository import RuleRepository
from order_processor.infrastructure.processing.workflow import OrderWorkflow
from order_processor.infrastructure.processing.developer_executors import EXECUTORS as DEVELOPER_EXECUTORS
from order_processor.infrastructure.processing.orchestrator import LLMOrchestrator
from order_processor.shared.settings import load_project_env


def register_web_ui(app: FastAPI, project_root: Path) -> None:
    input_dir, output_dir = project_root / "input", project_root / "output"
    admin_sessions: set[str] = set()
    rule_draft_packages: dict[str, tuple[str, list[dict]]] = {}
    direct_executors = {"copy_or_blank", "map_value", "set_value", "classify_c003_contract", "format_template", "set_blank"}

    def require_admin(admin_session: str | None = Cookie(default=None)) -> None:
        if admin_session not in admin_sessions:
            raise HTTPException(401, "请先登录管理后台")

    @app.get("/admin/login", response_class=HTMLResponse, include_in_schema=False)
    def admin_login_page() -> str:
        return """<!doctype html><html lang='zh-CN'><meta charset='utf-8'><title>管理员登录</title><style>body{font:16px system-ui;max-width:420px;margin:80px auto;padding:0 24px}form{display:grid;gap:14px;border:1px solid #d7dce5;padding:24px;border-radius:12px}input,button{font:inherit;padding:10px}button{background:#1769e0;color:#fff;border:0;border-radius:7px}</style><h1>业务规则管理</h1><form method='post'><label>管理员密码<input name='password' type='password' required autofocus></label><button>登录</button></form></html>"""

    @app.post("/admin/login", include_in_schema=False)
    def admin_login(password: str = Form(...)) -> RedirectResponse:
        load_project_env()
        expected = os.getenv("ADMIN_PASSWORD")
        if not expected:
            raise HTTPException(503, "未配置 ADMIN_PASSWORD，管理后台已禁用")
        if not hmac.compare_digest(password, expected):
            raise HTTPException(401, "管理员密码错误")
        token = secrets.token_urlsafe(32)
        admin_sessions.add(token)
        response = RedirectResponse("/admin", status_code=303)
        response.set_cookie("admin_session", token, httponly=True, samesite="lax")
        return response

    @app.post("/admin/customers", include_in_schema=False)
    def create_customer(code: str = Form(...), name: str = Form(...), _: None = Depends(require_admin)) -> RedirectResponse:
        code, name = code.strip().upper(), name.strip()
        if not code or not name:
            raise HTTPException(400, "客户代码和客户名称不能为空")
        try:
            RuleRepository(project_root / "data" / "rules.db").create_customer(code, name)
        except Exception as error:
            raise HTTPException(400, f"无法新增客户：{error}") from error
        return RedirectResponse("/admin", status_code=303)

    @app.post("/admin/customers/{code}", include_in_schema=False)
    def update_customer(code: str, name: str = Form(...), enabled: str | None = Form(None), _: None = Depends(require_admin)) -> RedirectResponse:
        RuleRepository(project_root / "data" / "rules.db").update_customer(code, name.strip(), enabled == "on")
        return RedirectResponse("/admin", status_code=303)

    @app.post("/admin/input-fields", include_in_schema=False)
    def create_input_field(field_id: str = Form(...), display_name: str = Form(...), data_type: str = Form("text"), _: None = Depends(require_admin)) -> RedirectResponse:
        RuleRepository(project_root / "data" / "rules.db").create_input_field(field_id.strip(), display_name.strip(), data_type)
        return RedirectResponse("/admin", status_code=303)

    @app.post("/admin/input-fields/{field_id}", include_in_schema=False)
    def update_input_field(field_id: str, display_name: str = Form(...), data_type: str = Form("text"), enabled: str | None = Form(None), _: None = Depends(require_admin)) -> RedirectResponse:
        RuleRepository(project_root / "data" / "rules.db").update_input_field(field_id, display_name.strip(), data_type, enabled == "on")
        return RedirectResponse("/admin", status_code=303)

    @app.post("/admin/erp-fields", include_in_schema=False)
    def create_erp_field(field_id: str = Form(...), display_name: str = Form(...), sort_order: int = Form(...), owner_customer_code: str = Form(""), _: None = Depends(require_admin)) -> RedirectResponse:
        RuleRepository(project_root / "data" / "rules.db").create_erp_field(field_id.strip(), display_name.strip(), sort_order, owner_customer_code.strip() or None)
        return RedirectResponse("/admin", status_code=303)

    @app.post("/admin/erp-fields/{field_id}", include_in_schema=False)
    def update_erp_field(field_id: str, display_name: str = Form(...), sort_order: int = Form(...), owner_customer_code: str = Form(""), enabled: str | None = Form(None), _: None = Depends(require_admin)) -> RedirectResponse:
        RuleRepository(project_root / "data" / "rules.db").update_erp_field(field_id, display_name.strip(), sort_order, owner_customer_code.strip() or None, enabled == "on")
        return RedirectResponse("/admin", status_code=303)

    @app.post("/admin/field-rule-groups", include_in_schema=False)
    def create_group(customer_code: str = Form(...), erp_field_id: str = Form(...), _: None = Depends(require_admin)) -> RedirectResponse:
        # 执行顺序由 ERP 字段目录的列顺序自动确定，业务人员无需填写。
        RuleRepository(project_root / "data" / "rules.db").create_field_rule_group(customer_code, erp_field_id)
        return RedirectResponse("/admin", status_code=303)

    @app.post("/admin/rules", include_in_schema=False)
    def create_rule(group_id: int = Form(...), name: str = Form(...), condition: str = Form("always true"), action: str = Form(...), input_field_ids: List[str] = Form([]), priority: int = Form(10), task_type: str = Form("deterministic"), executor_name: str = Form(""), executor_config: str = Form("{}"), enabled: str | None = Form(None), _: None = Depends(require_admin)) -> RedirectResponse:
        if executor_name and executor_name not in direct_executors | set(DEVELOPER_EXECUTORS):
            raise HTTPException(400, "请选择系统已支持的执行器名称")
        try:
            json.loads(executor_config or "{}")
        except json.JSONDecodeError as error:
            raise HTTPException(400, f"执行器配置必须是 JSON：{error}") from error
        RuleRepository(project_root / "data" / "rules.db").create_rule(group_id, name.strip(), condition.strip(), action.strip(), input_field_ids, priority, task_type, executor_name.strip() or None, executor_config, enabled == "on")
        return RedirectResponse("/admin", status_code=303)

    def render_rule_draft(customer_code: str, requirement: str, draft: dict, catalog: dict) -> HTMLResponse:
        inputs = {field_id: name for field_id, name, _, enabled in catalog["inputs"] if enabled}
        erp_fields = [(field_id, name, owner) for field_id, name, _, owner, enabled in catalog["erp"] if enabled and (owner is None or owner == customer_code)]
        selected_inputs = {str(value) for value in draft.get("input_field_ids", []) if str(value) in inputs}
        selected_erp = str(draft.get("erp_field_id", ""))
        if selected_erp not in {field_id for field_id, _, _ in erp_fields}:
            selected_erp = erp_fields[0][0] if erp_fields else ""
        executor_name = str(draft.get("executor_name") or "")
        if executor_name not in direct_executors | set(DEVELOPER_EXECUTORS):
            executor_name = ""
        config = draft.get("executor_config", {})
        if not isinstance(config, dict):
            config = {}
        erp_options = "".join(f"<option value='{escape(field_id)}' {'selected' if field_id == selected_erp else ''}>{escape(name)}</option>" for field_id, name, _ in erp_fields)
        input_options = "".join(f"<option value='{escape(field_id)}' {'selected' if field_id in selected_inputs else ''}>{escape(name)}</option>" for field_id, name in inputs.items())
        executor_options = "<option value='' " + ("selected" if not executor_name else "") + ">不使用执行器</option><optgroup label='直接执行'>" + "".join(f"<option value='{name}' {'selected' if name == executor_name else ''}>{label}</option>" for name, label in (("copy_or_blank", "复制输入值"), ("map_value", "按映射表转换值"), ("set_value", "固定值"), ("set_blank", "清空字段"), ("format_template", "按模板拼接字段"), ("classify_c003_contract", "C003 合同号分类"))) + "</optgroup><optgroup label='开发者执行器'>" + "".join(f"<option value='{escape(name)}' {'selected' if name == executor_name else ''}>{escape(name)}</option>" for name in sorted(DEVELOPER_EXECUTORS)) + "</optgroup>"
        return HTMLResponse("""<!doctype html><html lang='zh-CN'><meta charset='utf-8'><title>校对 AI 规则草稿</title><style>body{font:15px/1.5 system-ui,-apple-system,"Microsoft YaHei",sans-serif;max-width:980px;margin:36px auto;padding:0 24px;background:#f6f8fc;color:#172033}.card{background:#fff;border:1px solid #e1e7f0;border-radius:14px;padding:22px;box-shadow:0 3px 12px #19355a0a}form{display:flex;gap:12px;flex-wrap:wrap;align-items:flex-end}label{display:grid;gap:5px;font-weight:600;color:#36465f}input,select,textarea,button{font:inherit;padding:9px 10px;border:1px solid #cbd5e1;border-radius:8px;background:#fff}textarea{min-width:310px;min-height:86px}button{border:0;background:#1769e0;color:#fff;font-weight:650;cursor:pointer}.hint{color:#60708a}code{background:#eef4ff;padding:2px 5px;border-radius:4px}</style><p><a href='/admin'>← 返回管理端</a></p><div class='card'><h1>校对 AI 规则草稿</h1><p class='hint'>AI 只生成草稿，未写入规则库。请核对并修改后再发布。</p><p><b>原始需求：</b>""" + escape(requirement) + """</p><form method='post' action='/admin/ai-rule-draft/publish'><input type='hidden' name='customer_code' value='""" + escape(customer_code) + """'><label>输出 ERP 字段*<select name='erp_field_id' required>""" + erp_options + """</select></label><label>规则名称*<input name='name' value='""" + escape(str(draft.get("name", "AI 生成规则"))) + """' required></label><label>优先级<input name='priority' type='number' value='""" + escape(str(draft.get("priority", 10))) + """'></label><label>执行方式<select name='task_type'>""" + "".join(f"<option value='{value}' {'selected' if value == draft.get('task_type') else ''}>{label}</option>" for value, label in (("direct_atomic", "直接执行"), ("deterministic", "模型编排"), ("developer_executor", "开发者执行器"), ("semantic", "语义理解"))) + """</select></label><label>执行器名称<select name='executor_name'>""" + executor_options + """</select></label><label>触发条件<textarea name='condition'>""" + escape(str(draft.get("condition", "always true"))) + """</textarea></label><label>动作说明*<textarea name='action' required>""" + escape(str(draft.get("action", ""))) + """</textarea></label><label>依赖输入字段（可多选）<select name='input_field_ids' multiple size='7'>""" + input_options + """</select></label><label>执行器配置（JSON）<textarea name='executor_config'>""" + escape(json.dumps(config, ensure_ascii=False)) + """</textarea></label><label><input name='enabled' type='checkbox' checked> 发布后立即启用</label><button>确认并发布规则</button></form></div></html>""")

    def render_rule_package(customer_code: str, requirement: str, rules: list[dict]) -> HTMLResponse:
        package_id = uuid4().hex
        rule_draft_packages[package_id] = (customer_code, rules)
        cards = "".join(f"<details class='rule'><summary>{index + 1}. {escape(str(rule.get('name', 'AI 规则草稿')))} <span>{escape(str(rule.get('condition', 'always true')))}</span></summary><label>条件<textarea data-key='condition'>{escape(str(rule.get('condition', 'always true')))}</textarea></label><label>结果<textarea data-key='action'>{escape(str(rule.get('action', '')))}</textarea></label><button type='button' class='delete'>删除此规则</button><input type='hidden' data-key='index' value='{index}'></details>" for index, rule in enumerate(rules))
        return HTMLResponse("""<!doctype html><meta charset='utf-8'><title>校对规则包</title><style>body{font:14px/1.4 system-ui;max-width:900px;margin:24px auto;background:#f6f8fc;padding:0 20px}.rule{background:#fff;border:1px solid #dfe7f3;border-radius:9px;padding:10px 12px;margin:8px 0}.rule summary{font-weight:650;cursor:pointer}.rule summary span{color:#60708a;font-weight:400;margin-left:10px}label{display:block;margin:7px 0;font-weight:600}textarea{display:block;width:100%;min-height:54px;margin-top:3px;padding:7px}button{padding:8px 11px;border:0;border-radius:7px;background:#1769e0;color:#fff;cursor:pointer}.delete{background:#fff;color:#b42318;border:1px solid #f1b9b3}.hint{color:#60708a}</style><p><a href='/admin'>← 返回管理端</a></p><h1>校对规则包</h1><p class='hint'>已生成 """ + str(len(rules)) + """ 条草稿。点击规则展开校对；确认后一次性发布。</p><p class='hint'>""" + escape(requirement) + """</p><form method='post' action='/admin/ai-rule-package/publish' id='publish'><input type='hidden' name='package_id' value='""" + package_id + """'><input type='hidden' name='rules_json' id='rules_json'>""" + cards + """<button>确认并发布全部规则</button></form><script>document.querySelectorAll('.delete').forEach(b=>b.onclick=()=>b.closest('.rule').remove());document.querySelector('#publish').onsubmit=()=>{let base=""" + json.dumps(rules, ensure_ascii=False) + """;document.querySelectorAll('.rule').forEach(c=>{let i=+c.querySelector('[data-key=index]').value;base[i].condition=c.querySelector('[data-key=condition]').value;base[i].action=c.querySelector('[data-key=action]').value});let kept=[...document.querySelectorAll('.rule')].map(c=>base[+c.querySelector('[data-key=index]').value]);document.querySelector('#rules_json').value=JSON.stringify(kept)}</script>""")

    @app.post("/admin/ai-rule-draft", response_class=HTMLResponse, include_in_schema=False)
    def generate_rule_draft(customer_code: str = Form(...), requirement: str = Form(""), rules_file: UploadFile | None = File(None), _: None = Depends(require_admin)) -> HTMLResponse:
        if rules_file and rules_file.filename:
            if not rules_file.filename.lower().endswith(".txt"):
                raise HTTPException(400, "仅支持上传 TXT 规则文件")
            try:
                requirement = rules_file.file.read().decode("utf-8-sig").strip()
            except UnicodeDecodeError as error:
                raise HTTPException(400, "TXT 文件必须使用 UTF-8 编码") from error
        if not requirement.strip():
            raise HTTPException(400, "请粘贴业务规则或上传 TXT 文件")
        repo = RuleRepository(project_root / "data" / "rules.db")
        repo.initialize()
        catalog = repo.field_catalog()
        inputs = [{"id": field_id, "name": name, "type": kind} for field_id, name, kind, enabled in catalog["inputs"] if enabled]
        outputs = [{"id": field_id, "name": name} for field_id, name, _, owner, enabled in catalog["erp"] if enabled and (owner is None or owner == customer_code)]
        try:
            raw = LLMOrchestrator(os.getenv("DEEPSEEK_API_KEY")).draft_rule_library(customer_code, requirement, inputs, outputs)
            draft = json.loads(raw)
            if not isinstance(draft, dict):
                raise ValueError("模型未返回 JSON 对象")
        except Exception as error:
            status = 504 if "timed out" in str(error).lower() else 502
            hint = "模型接口响应超时，请稍后重试；也可先将规则拆成较小的规则包生成。" if status == 504 else str(error)
            raise HTTPException(status, f"生成规则草稿失败：{hint}") from error
        rules = draft.get("rules") if isinstance(draft.get("rules"), list) else [draft]
        return render_rule_package(customer_code, requirement, [rule for rule in rules if isinstance(rule, dict)])

    @app.post("/admin/ai-rule-package/publish", include_in_schema=False)
    def publish_rule_package(package_id: str = Form(...), rules_json: str = Form(...), _: None = Depends(require_admin)) -> RedirectResponse:
        package = rule_draft_packages.pop(package_id, None)
        if not package:
            raise HTTPException(400, "规则草稿已失效，请重新生成")
        customer_code, _ = package
        try:
            rules = json.loads(rules_json)
            if not isinstance(rules, list): raise ValueError()
        except (json.JSONDecodeError, ValueError) as error:
            raise HTTPException(400, "规则草稿格式无效") from error
        repo = RuleRepository(project_root / "data" / "rules.db"); repo.initialize()
        catalog = repo.field_catalog(); inputs = {row[0] for row in catalog["inputs"]}; erp = {row[0] for row in catalog["erp"]}
        for rule in rules:
            field = str(rule.get("erp_field_id", ""))
            if field not in erp: raise HTTPException(400, f"草稿包含未知 ERP 字段：{field}")
            # 后续规则可读取已经生成的 ERP 输出状态（例如“型号”初始化后再判断型号）。
            used = [value for value in rule.get("input_field_ids", []) if value in inputs | erp]
            group = next((row for row in repo.field_catalog()["groups"] if row[1] == customer_code and row[2] == field), None)
            group_id = group[0] if group else repo.create_field_rule_group(customer_code, field)
            repo.create_rule(group_id, str(rule.get("name") or "AI 规则"), str(rule.get("condition") or "always true"), str(rule.get("action") or ""), used, int(rule.get("priority") or 10), str(rule.get("task_type") or "deterministic"), str(rule.get("executor_name") or "") or None, json.dumps(rule.get("executor_config") or {}, ensure_ascii=False), True)
        return RedirectResponse("/admin", status_code=303)

    @app.post("/admin/ai-rule-draft/publish", include_in_schema=False)
    def publish_rule_draft(customer_code: str = Form(...), erp_field_id: str = Form(...), name: str = Form(...), condition: str = Form("always true"), action: str = Form(...), input_field_ids: List[str] = Form([]), priority: int = Form(10), task_type: str = Form("deterministic"), executor_name: str = Form(""), executor_config: str = Form("{}"), enabled: str | None = Form(None), _: None = Depends(require_admin)) -> RedirectResponse:
        if executor_name and executor_name not in direct_executors | set(DEVELOPER_EXECUTORS):
            raise HTTPException(400, "请选择系统已支持的执行器名称")
        try:
            json.loads(executor_config or "{}")
        except json.JSONDecodeError as error:
            raise HTTPException(400, f"执行器配置必须是 JSON：{error}") from error
        repo = RuleRepository(project_root / "data" / "rules.db")
        repo.initialize()
        group = next((row for row in repo.field_catalog()["groups"] if row[1] == customer_code and row[2] == erp_field_id), None)
        group_id = group[0] if group else repo.create_field_rule_group(customer_code, erp_field_id)
        repo.create_rule(group_id, name.strip(), condition.strip(), action.strip(), input_field_ids, priority, task_type, executor_name.strip() or None, executor_config, enabled == "on")
        return RedirectResponse("/admin", status_code=303)

    @app.post("/admin/sample-run", response_class=HTMLResponse, include_in_schema=False)
    def sample_run(customer_code: str = Form(...), row_json: str = Form(...), _: None = Depends(require_admin)) -> HTMLResponse:
        try:
            row = json.loads(row_json)
            if not isinstance(row, dict):
                raise ValueError("样本必须是 JSON 对象")
        except (json.JSONDecodeError, ValueError) as error:
            raise HTTPException(400, f"样本 JSON 无效：{error}") from error
        row["客户代码"] = customer_code
        repo = RuleRepository(project_root / "data" / "rules.db")
        repo.initialize()
        workflow = OrderWorkflow(repo.load_active_rules(customer_code), os.getenv("DEEPSEEK_API_KEY"), repo)
        matched = workflow._matched_rules(row)
        result = workflow.process_row(row)
        output = result.data.to_dict() if result.success and result.data else {}
        changes = {key: value for key, value in output.items() if row.get(key) != value}
        body = {"route_customer": customer_code, "matched_rules": [rule.name for rule in matched], "field_changes": changes, "success": result.success, "error": result.error}
        return HTMLResponse("<meta charset='utf-8'><p><a href='/admin'>← 返回管理端</a></p><h1>样本试运行结果</h1><pre>" + escape(json.dumps(body, ensure_ascii=False, indent=2, default=str)) + "</pre>")

    @app.get("/admin", response_class=HTMLResponse, include_in_schema=False)
    def admin_home(admin_session: str | None = Cookie(default=None)):
        load_project_env()
        if not os.getenv("ADMIN_PASSWORD"):
            raise HTTPException(503, "未配置 ADMIN_PASSWORD，管理后台已禁用")
        if admin_session not in admin_sessions:
            return RedirectResponse("/admin/login", status_code=303)
        snapshot = RuleRepository(project_root / "data" / "rules.db")
        snapshot.initialize()
        data = snapshot.admin_snapshot()
        catalog = snapshot.field_catalog()
        def table(headers: list[str], rows: list[tuple]) -> str:
            head = "".join(f"<th>{escape(value)}</th>" for value in headers)
            body = "".join("<tr>" + "".join(f"<td>{escape(str(value))}</td>" for value in row) + "</tr>" for row in rows)
            return f"<table><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table>"
        customer_rows = "".join(
            f"<tr><td>{escape(code)}</td><td><input form='customer-{escape(code)}' name='name' value='{escape(name)}' required></td><td><form class='inline-form' id='customer-{escape(code)}' method='post' action='/admin/customers/{escape(code)}'><label><input name='enabled' type='checkbox' {'checked' if enabled else ''}> 启用</label><button>保存</button></form></td></tr>"
            for code, name, enabled in data["customers"]
        )
        customer_table = "<table class='customer-table'><thead><tr><th>客户代码</th><th>客户名称</th><th>状态</th></tr></thead><tbody>" + customer_rows + "</tbody></table>"
        def options(rows, value_index: int, label_index: int) -> str:
            return "".join(f"<option value='{escape(str(row[value_index]))}'>{escape(str(row[label_index]))}</option>" for row in rows)
        customer_options = options(catalog["customers"], 0, 1)
        business_customer_options = "".join(f"<option value='{escape(code)}'>{escape(name)}</option>" for code, name in catalog["customers"] if code != "COMMON")
        erp_options = "".join(f"<option value='{escape(field_id)}'>{escape(name)}{'（默认输出）' if owner is None else '（仅 ' + escape(owner) + '）'}</option>" for field_id, name, _, owner, _ in catalog["erp"])
        erp_owner_options = "<option value=''>不绑定客户（默认输出）</option>" + "".join(f"<option value='{escape(code)}'>{escape(name)}</option>" for code, name in catalog["customers"] if code != "COMMON")
        executor_options = "<option value=''>不使用执行器（模型编排 / 语义理解）</option><optgroup label='直接执行'>" + "".join(f"<option value='{name}'>{label}</option>" for name, label in (("copy_or_blank", "复制输入值；空值则输出空"), ("map_value", "按映射表转换值"), ("set_value", "固定值"), ("set_blank", "清空字段"), ("format_template", "按模板拼接字段"), ("classify_c003_contract", "C003 合同号分类"))) + "</optgroup><optgroup label='开发者执行器'>" + "".join(f"<option value='{escape(name)}'>{escape(name)}</option>" for name in sorted(DEVELOPER_EXECUTORS)) + "</optgroup>"
        group_options = "".join(f"<option value='{group_id}'>{escape(code)} / {escape(field)} / 顺序{order}</option>" for group_id, code, _, field, order, _ in catalog["groups"])
        input_options = "".join(f"<option value='{escape(field_id)}'>{escape(name)}</option>" for field_id, name, _, enabled in catalog["inputs"] if enabled)
        common_rows = [(field, name, condition, priority, task) for _, code, _, field, _, _ in catalog["groups"] if code == "COMMON" for _, _, rule_code, rule_field, name, condition, _, _, priority, task, *_ in catalog["rules"] if rule_code == "COMMON" and rule_field == field]
        common_table = table(["ERP 字段", "通用规则", "条件", "优先级", "执行方式"], common_rows) or "<p>暂无通用规则。</p>"
        input_editor = "".join(
            f"<form method='post' action='/admin/input-fields/{escape(field_id)}'><code>{escape(field_id)}</code><input name='display_name' value='{escape(name)}' required><select name='data_type'>" +
            "".join(f"<option value='{kind}' {'selected' if kind == data_type else ''}>{kind}</option>" for kind in ("text", "date", "number")) +
            f"</select><label><input name='enabled' type='checkbox' {'checked' if enabled else ''}>启用</label><button>保存</button></form>"
            for field_id, name, data_type, enabled in data["input_fields"]
        )
        erp_editor = "".join(
            f"<form class='editor-row' method='post' action='/admin/erp-fields/{escape(field_id)}'><code>{escape(field_id)}</code><label>名称<input name='display_name' value='{escape(name)}' required></label><label>列顺序<input name='sort_order' type='number' min='1' value='{order}' required></label><label>归属客户<select name='owner_customer_code'><option value='' {'selected' if owner is None else ''}>默认输出</option>" + "".join(f"<option value='{escape(code)}' {'selected' if code == owner else ''}>{escape(customer_name)}</option>" for code, customer_name in catalog["customers"] if code != "COMMON") + f"</select></label><label><input name='enabled' type='checkbox' {'checked' if enabled else ''}> 启用</label><button>保存修改</button></form>"
            for field_id, name, order, owner, enabled in data["erp_fields"]
        )
        manual_rules = "<details class='card'><summary>手工规则维护</summary><p class='hint'>仅在不使用 AI 生成时手工维护。</p><form method='post' action='/admin/field-rule-groups'><label>客户<select name='customer_code'>" + customer_options + "</select></label><label>ERP 字段<select name='erp_field_id'>" + erp_options + "</select></label><button>建立规则组</button></form><form method='post' action='/admin/rules'><label>规则组<select name='group_id'>" + group_options + "</select></label><label>规则名称<input name='name' required></label><label>条件<textarea name='condition'>always true</textarea></label><label>动作<textarea name='action' required></textarea></label><input name='priority' type='hidden' value='10'><input name='task_type' type='hidden' value='deterministic'><input name='executor_config' type='hidden' value='{}'><button>新增规则</button></form></details>"
        input_fields_section = "<details class='card'><summary>输入字段管理</summary><form method='post' action='/admin/input-fields'><label>字段 ID<input name='field_id' placeholder='input_xxx' required></label><label>显示名称<input name='display_name' placeholder='例如 验收要求' required></label><label>类型<select name='data_type'><option>text</option><option>date</option><option>number</option></select></label><button>新增输入字段</button></form>" + input_editor + "</details>"
        erp_fields_section = "<details class='card'><summary>ERP 输出字段管理</summary><form method='post' action='/admin/erp-fields'><label>字段 ID<input name='field_id' placeholder='erp_xxx' required></label><label>显示名称<input name='display_name' required></label><label>列顺序<input name='sort_order' type='number' min='1' required></label><label>归属客户<select name='owner_customer_code'>" + erp_owner_options + "</select></label><button>新增 ERP 字段</button></form>" + erp_editor + "</details>"
        other_section = "<details class='card'><summary>预处理规则与全部规则清单</summary><h3>预处理规则</h3>" + table(["规则 ID", "客户", "类型", "执行顺序", "启用"], data["preprocess_rules"]) + "<h3>全部规则</h3>" + table(["客户", "ERP 字段", "规则名称", "条件", "优先级", "启用"], data["rules"]) + "</details>"
        return HTMLResponse("""<!doctype html><html lang='zh-CN'><meta charset='utf-8'><title>业务规则管理</title><style>body{font:14px/1.4 system-ui,-apple-system,"Microsoft YaHei",sans-serif;max-width:1100px;margin:0 auto;padding:22px;background:#f6f8fc;color:#172033}.card,details{background:#fff;border:1px solid #e1e7f0;border-radius:10px;padding:13px;margin:9px 0}summary{cursor:pointer;font-size:16px;font-weight:700}h1,h2,h3,p{margin:0 0 7px}.hint{color:#60708a}form{display:flex;gap:8px;align-items:end;flex-wrap:wrap;margin:8px 0}label{display:grid;gap:3px;font-weight:600}input,select,textarea,button{font:inherit;padding:7px 8px;border:1px solid #cbd5e1;border-radius:7px}textarea{min-width:240px;min-height:58px}button{background:#1769e0;color:white;border:0;font-weight:650}.inline-form{margin:0}.customer-table{width:100%;border-collapse:collapse}.customer-table td,.customer-table th{padding:7px;border-bottom:1px solid #e8edf5;text-align:left}.customer-table input{max-width:260px}.editor-row{padding:7px;border:1px solid #e5eaf2;border-radius:7px}</style><body><h1>业务规则管理</h1><p class='hint'>先选择或新增客户，再用自然语言生成规则草稿。</p>""" +
            "<section class='card'><h2>客户</h2><form method='post' action='/admin/customers'><label>客户代码<input name='code' placeholder='例如 C004' required></label><label>客户名称<input name='name' placeholder='例如 航天四院' required></label><button>新增客户</button></form>" + customer_table + "</section>" +
            "<section class='card'><h2>AI 生成规则</h2><p class='hint'>粘贴业务规则或上传 TXT；生成后逐条展开校对，确认后再发布。</p><form id='ai-rule-form' method='post' action='/admin/ai-rule-draft' enctype='multipart/form-data'><label>客户<select name='customer_code'>" + business_customer_options + "</select></label><label>业务规则<textarea name='requirement' placeholder='例如：验收要求包含一院时，计划标记填 YP017-X486。'></textarea></label><label>或上传 TXT<input name='rules_file' type='file' accept='.txt,text/plain'></label><button id='ai-rule-button'>生成规则草稿</button><span id='ai-rule-status' class='hint'></span></form></section>" + manual_rules + input_fields_section + erp_fields_section + other_section + "<script>const f=document.querySelector('#ai-rule-form');f.onsubmit=()=>{const b=document.querySelector('#ai-rule-button'),s=document.querySelector('#ai-rule-status');b.disabled=true;b.textContent='正在生成…';s.textContent='正在读取规则并调用模型，通常需要 10–60 秒；较长规则包可能更久。';setTimeout(()=>{if(b.disabled)s.textContent='仍在生成中，请保持页面开启；超过约 2 分钟将显示超时提示。'},30000)}</script></body></html>")
        # 默认页面仅展示业务人员日常需要的内容；字段 ID 与完整表结构收进高级设置。
        return HTMLResponse("""<!doctype html><html lang='zh-CN'><head><meta charset='utf-8'><title>业务规则管理</title><style>
*{box-sizing:border-box}body{font:14px/1.4 system-ui,-apple-system,"Microsoft YaHei",sans-serif;max-width:1180px;margin:0 auto;padding:24px 24px 48px;color:#172033;background:#f6f8fc}h1{font-size:28px;margin:0 0 4px}h2{font-size:18px;margin:0 0 6px}.hint{color:#60708a;margin:0 0 8px}.card,details{border:1px solid #e1e7f0;border-radius:11px;padding:14px;margin:10px 0;background:#fff;box-shadow:0 2px 8px #19355a0a}form{display:flex;gap:8px;align-items:flex-end;flex-wrap:wrap;margin:9px 0}.inline-form{margin:0;gap:6px;align-items:center;flex-wrap:nowrap}label{display:grid;gap:3px;font-weight:600;color:#36465f}small{font-weight:400;color:#74839a}input,select,textarea,button{font:inherit;padding:7px 8px;border:1px solid #cbd5e1;border-radius:7px;background:#fff}input:focus,select:focus,textarea:focus{outline:2px solid #9cc3ff;border-color:#4f8df7}textarea{min-width:280px;min-height:68px;resize:vertical}button{background:#1769e0;color:#fff;border:0;border-radius:7px;font-weight:650;cursor:pointer;min-height:34px}button:hover{background:#0959c9}table{border-collapse:separate;border-spacing:0;width:100%;margin:8px 0;overflow:hidden;border:1px solid #e1e7f0;border-radius:8px}th,td{padding:7px 8px;border-bottom:1px solid #e8edf5;text-align:left;vertical-align:middle}tr:last-child td{border-bottom:0}th{background:#f3f7ff;color:#42536e}.customer-table input{max-width:265px}.customer-table td{height:48px}.step{display:inline-block;margin-bottom:4px;color:#1769e0;font-weight:750;font-size:12px;letter-spacing:.04em}.editor-row{padding:8px;border:1px solid #e5eaf2;border-radius:8px;background:#fbfcff}.editor-row code{padding:7px;background:#eef4ff;border-radius:5px;color:#275fae}</style></head><body>
<h1>业务规则管理</h1><p class='hint'>先定义字段，再按客户建立规则。带 * 的内容必须填写。</p>"""+
"<section class='card'><span class='step'>AI 辅助</span><h2>用自然语言生成规则草稿</h2><p class='hint'>粘贴邮件、会议纪要或 Word 中的规则文字，也可上传 TXT。AI 会按输出字段和条件拆成规则草稿；不会直接发布。</p><form method='post' action='/admin/ai-rule-draft' enctype='multipart/form-data'><label>客户*<select name='customer_code'>"+business_customer_options+"</select></label><label>业务规则<textarea name='requirement' placeholder='例如：规则1：生产标识：JHT；交货日期：当前日期加45天，格式 yyyyMMdd；验收要求含“一院”时，计划标记为 YP017-X486。'></textarea><small>可直接粘贴多条规则。</small></label><label>或上传 TXT<input name='rules_file' type='file' accept='.txt,text/plain'><small>TXT 内容会替代上方粘贴内容。</small></label><button>生成规则草稿</button></form></section>"+
"<section class='card'><span class='step'>步骤 1</span><h2>客户</h2><p class='hint'>示例：客户代码填 <code>C004</code>；客户名称填 <code>航天四院</code>。</p><form method='post' action='/admin/customers'><label>客户代码*<input name='code' placeholder='例如 C004' required></label><label>客户名称*<input name='name' placeholder='例如 航天四院' required></label><button>新增客户</button></form>"+customer_table+"</section>"+
"<section class='card'><span class='step'>步骤 2</span><h2>选择 ERP 字段并建立客户规则组</h2><p>示例：客户选“航天四院”，ERP 字段选“计划标记”。如果通用规则已满足需求，不要建立规则组；仅客户有例外时创建。</p><form method='post' action='/admin/field-rule-groups'><label>客户* <select name='customer_code'>"+customer_options+"</select></label><label>ERP 字段* <select name='erp_field_id'>"+erp_options+"</select></label><button>建立规则组</button></form><details><summary>查看通用规则参考（只读）</summary>"+common_table+"</details></section>"+
"<section class='card'><span class='step'>步骤 3</span><h2>为客户规则组新增规则</h2><p class='hint'>输出字段由规则组自动确定。每个框都可按下面的示例填写；默认规则优先级为 10，客户特殊覆盖通常为 100，必须覆盖可填 200。</p><form method='post' action='/admin/rules'><label>规则组* <select name='group_id'>"+group_options+"</select><small>示例：C004 / 计划标记 / 顺序 8</small></label><label>规则名称*<input name='name' placeholder='例如 一院验收计划标记' required></label><label>优先级*<input name='priority' type='number' value='10'><small>例如 10</small></label><label>执行方式*<select name='task_type'><option value='direct_atomic'>直接执行（复制/映射/固定值）</option><option value='deterministic'>模型编排原子单元</option><option value='developer_executor'>开发者执行器</option><option value='semantic'>语义理解</option></select><small>固定值可选“直接执行”</small></label><label>执行器名称<select name='executor_name'>"+executor_options+"</select><small>直接执行或开发者执行器时选择；模型编排可不选</small></label><label>触发条件<textarea name='condition'>always true</textarea><small>例如：验收要求 contains '一院'；无条件：always true</small></label><label>动作说明*<textarea name='action' placeholder='例如 验收要求含一院时，计划标记设为 YP017-X486' required></textarea></label><label>依赖输入字段（可多选）<select name='input_field_ids' multiple size='5'>"+input_options+"</select><small>例如：选择“验收要求”</small></label><label>执行器配置（JSON）<textarea name='executor_config'>{}</textarea><small>固定值示例：&#123;&quot;value&quot;:&quot;YP017-X486&quot;&#125;</small></label><label><input name='enabled' type='checkbox' checked> 立即启用</label><button>新增规则</button></form></section>"+
"<section class='card'><span class='step'>步骤 4</span><h2>样本试运行</h2><p class='hint'>示例：客户选“航天四院”；输入一行 JSON，用于查看命中规则、字段变更和最终输出列。</p><form method='post' action='/admin/sample-run'><label>客户*<select name='customer_code'>"+customer_options+"</select></label><label>样本数据（JSON）*<textarea name='row_json' required>&#123;&quot;产品型号&quot;:&quot;J599/20GJ19PN&quot;,&quot;质量等级&quot;:&quot;JHT&quot;,&quot;验收要求&quot;:&quot;一院验收&quot;&#125;</textarea></label><button>试运行</button></form></section>"+
"<details><summary>高级设置：输入字段、ERP 字段、全部规则与预处理规则</summary><h3>输入字段（可新增、修改）</h3><p class='hint'>Excel 原始数据中可被规则引用的列。字段 ID 是系统关联键，新增后请勿改动。</p><form method='post' action='/admin/input-fields'><label>字段 ID*<input name='field_id' placeholder='例如 input_acceptance_requirement' required></label><label>显示名称*<input name='display_name' placeholder='例如 验收要求' required></label><label>数据类型*<select name='data_type'><option value='text'>文本（例如 一院验收）</option><option value='date'>日期（例如 2026-08-25）</option><option value='number'>数字（例如 100）</option></select></label><button>新增输入字段</button></form><p class='hint'>修改已有输入字段：</p>"+input_editor+"<h3>ERP 输出字段（可新增、修改）</h3><p class='hint'>不选择归属客户时为所有客户默认输出；选择客户后仅该客户输出。</p><form method='post' action='/admin/erp-fields'><label>字段 ID*<input name='field_id' placeholder='例如 erp_plan_mark' required></label><label>显示名称*<input name='display_name' placeholder='例如 计划标记' required></label><label>Excel 列顺序*<input name='sort_order' type='number' min='1' placeholder='例如 8' required></label><label>归属客户<select name='owner_customer_code'>"+erp_owner_options+"</select><small>不选＝默认输出；选“航天四院”＝仅该客户输出</small></label><button>新增 ERP 字段</button></form><p class='hint'>修改已有 ERP 输出字段：</p>"+erp_editor+"<h3>预处理规则</h3>"+table(["规则 ID","客户","类型","执行顺序","启用"],data["preprocess_rules"])+"<h3>全部规则</h3>"+table(["客户","ERP 字段","规则名称","条件","优先级","启用"],data["rules"])+"</details></body></html>")
        return HTMLResponse("""<!doctype html><html lang='zh-CN'><head><meta charset='utf-8'><title>业务规则管理</title><style>
body{font:15px system-ui;max-width:1200px;margin:36px auto;padding:0 24px;color:#172033}h1{margin-bottom:4px}.hint{color:#60708a}table{border-collapse:collapse;width:100%;margin:12px 0 30px}th,td{padding:8px;border-bottom:1px solid #d7dce5;text-align:left}th{background:#f3f7ff}code{font-size:13px}form{display:flex;gap:8px;flex-wrap:wrap;margin:10px 0}input,select,textarea,button{font:inherit;padding:6px}textarea{min-width:280px;min-height:55px}details{border:1px solid #d7dce5;padding:10px;margin:12px 0;border-radius:8px}</style></head><body>
<h1>业务规则管理</h1><p class='hint'>管理员视图。用户处理端保持在首页，不显示规则配置。</p>""" +
            "<h2>客户</h2><form method='post' action='/admin/customers'><input name='code' placeholder='客户代码，如 C004' required><input name='name' placeholder='客户名称' required><button>新增客户</button></form>" + customer_table +
            "<h2>输入字段</h2><form method='post' action='/admin/input-fields'><input name='field_id' placeholder='input_xxx' required><input name='display_name' placeholder='Excel 输入字段名' required><select name='data_type'><option>text</option><option>date</option><option>number</option></select><button>新增输入字段</button></form>" + table(["字段 ID", "显示名称", "类型", "启用"], data["input_fields"]) +
            "<h2>ERP 字段</h2><form method='post' action='/admin/erp-fields'><input name='field_id' placeholder='erp_xxx' required><input name='display_name' placeholder='ERP 字段名' required><input name='sort_order' type='number' placeholder='列顺序' required><label><input name='default_export' type='checkbox' checked>默认输出</label><button>新增 ERP 字段</button></form>" + table(["字段 ID", "显示名称", "Excel 列顺序", "启用"], data["erp_fields"]) +
            "<h2>数据预处理规则</h2>" + table(["规则 ID", "客户", "类型", "执行顺序", "启用"], data["preprocess_rules"]) +
            "<h2>新增 ERP 字段规则组</h2><p class='hint'>规则组名称就是 ERP 字段名称。创建客户规则组会覆盖该字段的通用规则；如需基于通用规则调整，可勾选复制为草稿。</p><form method='post' action='/admin/field-rule-groups'><select name='customer_code'>" + customer_options + "</select><select name='erp_field_id'>" + erp_options + "</select><input name='execution_order' type='number' placeholder='实际执行顺序' required><label><input name='copy_common' type='checkbox'>复制通用规则为草稿</label><button>新增规则组</button></form><h3>通用规则（只读参考）</h3>" + common_table +
            "<h2>ERP 字段规则组</h2>" + table(["客户", "ERP 字段", "执行顺序", "启用"], data["field_rule_groups"]) +
            "<details><summary><b>新增具体规则</b>（输出字段由所选规则组自动确定）</summary><form method='post' action='/admin/rules'><select name='group_id'>" + group_options + "</select><input name='name' placeholder='规则名称' required><input name='priority' type='number' value='10'><select name='task_type'><option value='direct_atomic'>direct_atomic</option><option value='deterministic'>deterministic</option><option value='developer_executor'>developer_executor</option><option value='semantic'>semantic</option></select><input name='executor_name' placeholder='执行器，如 copy_or_blank'><textarea name='condition' placeholder='条件，例如 产品型号 starts with 21E6'>always true</textarea><textarea name='action' placeholder='业务动作描述' required></textarea><select name='input_field_ids' multiple size='6'>" + input_options + "</select><textarea name='executor_config' placeholder='执行器配置 JSON'>{}</textarea><label><input name='enabled' type='checkbox' checked>启用</label><button>新增规则</button></form></details>" +
            "<h2>样本试运行</h2><p class='hint'>粘贴一行输入 JSON；会显示路由客户、命中规则、字段变更和最终输出列。</p><form method='post' action='/admin/sample-run'><select name='customer_code'>" + customer_options + "</select><textarea name='row_json' required>{}</textarea><button>试运行</button></form>" +
            "<h2>具体规则</h2>" + table(["客户", "ERP 字段", "规则名称", "条件", "优先级", "启用"], data["rules"]) + "</body></html>")

    @app.get("/", response_class=HTMLResponse, include_in_schema=False)
    def home() -> str:
        return """<!doctype html><html lang='zh-CN'><head><meta charset='utf-8'>
<title>订单处理器</title><style>
body{font:16px system-ui;max-width:680px;margin:60px auto;padding:0 24px;color:#172033}
form{border:1px solid #d7dce5;border-radius:12px;padding:24px;display:grid;gap:16px}
input,button{font:inherit;padding:10px}button{background:#1769e0;color:#fff;border:0;border-radius:7px;cursor:pointer}
#result{margin-top:20px;padding:14px;border-radius:7px;background:#f3f7ff;white-space:pre-wrap}.hint{color:#60708a}
</style></head><body><h1>订单处理器</h1><p class='hint'>上传 Excel，系统按规则处理后生成新的 Excel 文件。</p>
<form id='form'><label>订单 Excel<input name='file' type='file' accept='.xlsx' required></label>
<label>输出文件名<input name='output_name' value='处理结果.xlsx' required></label><button>开始处理</button></form>
<div id='result'>等待上传文件。</div><script>
const form=document.querySelector('#form'), result=document.querySelector('#result');
form.onsubmit=async e=>{e.preventDefault();result.textContent='正在处理，请稍候…';const r=await fetch('/ui/process',{method:'POST',body:new FormData(form)});const d=await r.json();
if(!r.ok){result.textContent='处理失败：'+(d.detail||'未知错误');return}const label=d.output_file_count>1?`下载拆分结果（${d.output_file_count} 个 Excel，ZIP）`:'下载处理结果';result.innerHTML=`处理完成：共 ${d.total} 行，成功 ${d.success_count} 行。<br><a href="${d.download_url}">${label}</a>`};
</script></body></html>"""

    @app.post("/ui/process", include_in_schema=False)
    def process_upload(
        file: UploadFile = File(...), output_name: str = Form("处理结果.xlsx"),
    ) -> dict:
        if not file.filename or Path(file.filename).suffix.lower() != ".xlsx":
            raise HTTPException(400, "请上传 .xlsx 格式的 Excel 文件")
        safe_output = Path(output_name).name
        if Path(safe_output).suffix.lower() != ".xlsx":
            safe_output += ".xlsx"
        input_dir.mkdir(exist_ok=True)
        output_dir.mkdir(exist_ok=True)
        uploaded_path = input_dir / f"upload_{uuid4().hex}_{Path(file.filename).name}"
        with uploaded_path.open("wb") as target:
            shutil.copyfileobj(file.file, target)
        output_path = output_dir / safe_output
        # Web 路径不再依赖 AgentOS 的启动顺序；每次处理均显式加载项目 .env。
        load_project_env()
        result = build_process_orders(os.getenv("DEEPSEEK_API_KEY")).execute(str(uploaded_path), str(output_path))
        if not result.get("success"):
            raise HTTPException(500, f"订单处理失败：{result.get('failed_count', 0)} 行未成功")
        saved_files = [Path(path) for path in result.get("output_files", [])]
        if not saved_files:
            raise HTTPException(500, "订单处理完成但未生成输出文件")

        # 一个合同直接交付 Excel；按合同拆分出多个文件时，统一打包为 ZIP，
        # 避免 Web 页仅返回单一下载链接而遗漏其他子表。
        if len(saved_files) == 1:
            download_name = saved_files[0].name
        else:
            archive_name = f"{Path(safe_output).stem}_拆分结果.zip"
            archive_path = output_dir / archive_name
            with zipfile.ZipFile(archive_path, "w", zipfile.ZIP_DEFLATED) as archive:
                for saved_file in saved_files:
                    if saved_file.is_file():
                        archive.write(saved_file, arcname=saved_file.name)
            download_name = archive_name
        return {
            "total": result["total"], "success_count": result["success_count"],
            "output_file_count": len(saved_files),
            "download_url": f"/ui/outputs/{download_name}",
        }

    @app.get("/ui/outputs/{filename}", include_in_schema=False)
    def download_output(filename: str) -> FileResponse:
        path = output_dir / Path(filename).name
        if not path.is_file():
            raise HTTPException(404, "输出文件不存在")
        return FileResponse(path, filename=path.name)

    # AgentOS 自带根路由在注册时已存在；将用户首页置于路由表首位以覆盖它。
    homepage_route = next(route for route in reversed(app.router.routes) if getattr(route, "path", None) == "/")
    app.router.routes.remove(homepage_route)
    app.router.routes.insert(0, homepage_route)
