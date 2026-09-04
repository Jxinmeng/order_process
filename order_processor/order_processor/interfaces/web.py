"""订单处理的轻量 Web 页面；不将 HTML 或 HTTP 细节泄漏到应用层。"""

from __future__ import annotations

import os
import json
import logging
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
from order_processor.infrastructure.ingestion.source_extractor import SourceIngestionService, SourceTextReader
from order_processor.infrastructure.excel.excel_reader import ExcelReader
from order_processor.interfaces.rule_draft_parser import parse_rule_draft
from order_processor.interfaces.rule_file_reader import read_rule_file
from order_processor.shared.settings import load_project_env

logger = logging.getLogger("uvicorn.error")


def register_web_ui(app: FastAPI, project_root: Path) -> None:
    input_dir, output_dir = project_root / "input", project_root / "output"
    test_data_dir = project_root / "data" / "test"
    test_input_dir = test_data_dir / "input"
    test_mineru_dir = test_data_dir / "mineru_outputs"
    test_extraction_dir = test_data_dir / "extractions"
    admin_sessions: set[str] = set()
    rule_draft_packages: dict[str, tuple[str, list[dict]]] = {}
    rule_file_imports: dict[str, tuple[str, str, str]] = {}
    direct_executors = {"copy_or_blank", "map_value", "set_value", "classify_c003_contract", "format_template", "set_blank"}

    def require_admin(admin_session: str | None = Cookie(default=None)) -> None:
        if admin_session not in admin_sessions:
            raise HTTPException(401, "请先登录管理后台")

    @app.get("/admin/login", response_class=HTMLResponse, include_in_schema=False)
    def admin_login_page() -> str:
        return """<!doctype html><html lang='zh-CN'><head><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'><title>管理员登录</title><style>:root{--ink:#17213a;--muted:#697386;--indigo:#3659c9}*{box-sizing:border-box}body{display:grid;min-height:100vh;margin:0;padding:24px;place-items:center;background:radial-gradient(circle at 18% 12%,#e7edff 0,transparent 30%),radial-gradient(circle at 88% 88%,#e3f7f5 0,transparent 26%),#f5f7fb;color:var(--ink);font:14px/1.5 system-ui,-apple-system,"Segoe UI","Microsoft YaHei",sans-serif}.shell{width:min(100%,430px)}.brand{display:flex;align-items:center;justify-content:center;gap:10px;margin-bottom:20px;color:#263759;font-weight:800}.mark{display:grid;width:36px;height:36px;place-items:center;border-radius:11px;background:linear-gradient(145deg,#6682ef,#31bfc0);color:#fff;box-shadow:0 7px 18px #3659c938}.card{padding:34px;border:1px solid #e3e8f1;border-radius:20px;background:#fffffff2;box-shadow:0 22px 55px #243a6420;backdrop-filter:blur(10px)}.eyebrow{margin:0 0 5px;color:var(--indigo);font-size:10px;font-weight:850;letter-spacing:.14em}h1{margin:0;font-size:27px;letter-spacing:-.03em}.hint{margin:8px 0 24px;color:var(--muted)}form{display:grid;gap:16px}label{display:grid;gap:7px;color:#344054;font-size:12px;font-weight:750}input,button{min-height:45px;padding:10px 12px;border:1px solid #d7deea;border-radius:10px;background:#fff;color:var(--ink);font:inherit}input:focus{outline:0;border-color:#7d94e5;box-shadow:0 0 0 4px #3659c918}button{border:0;background:linear-gradient(135deg,var(--indigo),#4d70dc);color:#fff;font-weight:800;cursor:pointer;box-shadow:0 8px 18px #3659c934}button:hover{background:linear-gradient(135deg,#2949ae,#3c5ec9)}.back{display:block;margin-top:18px;color:#72809a;text-align:center;text-decoration:none}.back:hover{color:var(--indigo)}</style></head><body><main class='shell'><div class='brand'><span class='mark'>R</span><span>业务规则管理</span></div><section class='card'><p class='eyebrow'>ADMIN CONSOLE</p><h1>欢迎回来</h1><p class='hint'>登录后管理客户、字段和订单处理规则。</p><form method='post'><label>管理员密码<input name='password' type='password' placeholder='请输入管理员密码' required autofocus></label><button>进入规则配置中心</button></form></section><a class='back' href='/'>← 返回订单处理首页</a></main></body></html>"""

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
        def task_type_options(selected: str) -> str:
            return "".join(f"<option value='{value}' {'selected' if value == selected else ''}>{label}</option>" for value, label in (("direct_atomic", "直接执行"), ("deterministic", "模型编排"), ("developer_executor", "开发者执行器"), ("semantic", "语义理解")))

        cards = "".join(f"<details class='rule'><summary>{index + 1}. {escape(str(rule.get('name', 'AI 规则草稿')))} <span>{escape(str(rule.get('condition', 'always true')))}</span></summary><label>规则名称<input data-key='name' value='{escape(str(rule.get('name', 'AI 规则草稿')))}'></label><label>输出 ERP 字段 ID<input data-key='erp_field_id' value='{escape(str(rule.get('erp_field_id', '')))}'></label><label>优先级<input data-key='priority' type='number' value='{escape(str(rule.get('priority', 10)))}'></label><label>执行方式<select data-key='task_type'>{task_type_options(str(rule.get('task_type') or 'deterministic'))}</select></label><label>执行器名称<input data-key='executor_name' value='{escape(str(rule.get('executor_name') or ''))}' placeholder='例如 set_value'></label><label>依赖字段 ID（JSON 数组）<textarea data-key='input_field_ids'>{escape(json.dumps(rule.get('input_field_ids') or [], ensure_ascii=False))}</textarea></label><label>执行器配置（JSON）<textarea data-key='executor_config'>{escape(json.dumps(rule.get('executor_config') or {}, ensure_ascii=False))}</textarea></label><label>条件<textarea data-key='condition'>{escape(str(rule.get('condition', 'always true')))}</textarea></label><label>结果<textarea data-key='action'>{escape(str(rule.get('action', '')))}</textarea></label><label><input data-key='enabled' type='checkbox' {'checked' if rule.get('enabled', True) else ''}> 发布后立即启用</label><button type='button' class='delete'>删除此规则</button><input type='hidden' data-key='index' value='{index}'></details>" for index, rule in enumerate(rules))
        return HTMLResponse("""<!doctype html><meta charset='utf-8'><title>校对规则包</title><style>body{font:14px/1.4 system-ui;max-width:900px;margin:24px auto;background:#f6f8fc;padding:0 20px}.rule{background:#fff;border:1px solid #dfe7f3;border-radius:9px;padding:10px 12px;margin:8px 0}.rule summary{font-weight:650;cursor:pointer}.rule summary span{color:#60708a;font-weight:400;margin-left:10px}label{display:block;margin:7px 0;font-weight:600}textarea,input,select{display:block;width:100%;box-sizing:border-box;min-height:36px;margin-top:3px;padding:7px;border:1px solid #cbd5e1;border-radius:6px}textarea{min-height:54px}input[type=checkbox]{display:inline;width:auto;min-height:auto}button{padding:8px 11px;border:0;border-radius:7px;background:#1769e0;color:#fff;cursor:pointer}.delete{background:#fff;color:#b42318;border:1px solid #f1b9b3}.hint{color:#60708a}</style><p><a href='/admin'>← 返回管理端</a></p><h1>校对规则包</h1><p class='hint'>可修改条件、动作说明及完整执行定义；确认后一次性发布。</p><p class='hint'>""" + escape(requirement) + """</p><form method='post' action='/admin/ai-rule-package/publish' id='publish'><input type='hidden' name='package_id' value='""" + package_id + """'><input type='hidden' name='rules_json' id='rules_json'>""" + cards + """<button>确认并发布全部规则</button></form><script>document.querySelectorAll('.delete').forEach(b=>b.onclick=()=>b.closest('.rule').remove());document.querySelector('#publish').onsubmit=e=>{let base=""" + json.dumps(rules, ensure_ascii=False) + """;try{document.querySelectorAll('.rule').forEach(c=>{let i=+c.querySelector('[data-key=index]').value,r=base[i];for(const k of ['name','erp_field_id','priority','task_type','executor_name','condition','action'])r[k]=c.querySelector('[data-key='+k+']').value;r.input_field_ids=JSON.parse(c.querySelector('[data-key=input_field_ids]').value||'[]');r.executor_config=JSON.parse(c.querySelector('[data-key=executor_config]').value||'{}');r.enabled=c.querySelector('[data-key=enabled]').checked});document.querySelector('#rules_json').value=JSON.stringify([...document.querySelectorAll('.rule')].map(c=>base[+c.querySelector('[data-key=index]').value]))}catch(err){e.preventDefault();alert('依赖字段和执行器配置必须是合法 JSON：'+err.message)}}</script>""")

    def render_rule_import_preview(customer_code: str, filename: str, requirement: str) -> HTMLResponse:
        import_id = uuid4().hex
        rule_file_imports[import_id] = (customer_code, filename, requirement)
        return HTMLResponse("""<!doctype html><meta charset='utf-8'><title>确认导入规则</title><style>body{font:14px/1.5 system-ui;max-width:960px;margin:28px auto;background:#f6f8fc;padding:0 20px}.card{background:#fff;border:1px solid #dfe7f3;border-radius:10px;padding:18px}pre{white-space:pre-wrap;word-break:break-word;padding:12px;background:#f8fafc;border:1px solid #e2e8f0;border-radius:7px;max-height:65vh;overflow:auto}button{padding:9px 13px;border:0;border-radius:7px;background:#1769e0;color:#fff;font-weight:650;cursor:pointer}.hint{color:#60708a}</style><p><a href='/admin'>← 重新选择文件</a></p><div class='card'><h1>确认导入内容</h1><p class='hint'>文件：""" + escape(filename) + """。以下文字会原样发送给 AI；如果不是你的规则，请返回并选择正确的工作表文件。</p><pre>""" + escape(requirement) + """</pre><form method='post' action='/admin/ai-rule-imports/""" + import_id + """/generate'><button>确认使用以上规则生成草稿</button></form></div>""")

    def generate_rule_package(customer_code: str, requirement: str) -> HTMLResponse:
        if not requirement.strip():
            raise HTTPException(400, "请粘贴业务规则或上传规则文件")
        repo = RuleRepository(project_root / "data" / "rules.db")
        repo.initialize()
        catalog = repo.field_catalog()
        inputs = [{"id": field_id, "name": name, "type": kind} for field_id, name, kind, enabled in catalog["inputs"] if enabled]
        outputs = [{"id": field_id, "name": name} for field_id, name, _, owner, enabled in catalog["erp"] if enabled and (owner is None or owner == customer_code)]
        try:
            raw = LLMOrchestrator(os.getenv("DEEPSEEK_API_KEY")).draft_rule_library(customer_code, requirement, inputs, outputs)
            draft = parse_rule_draft(raw)
        except Exception as error:
            status = 504 if "timed out" in str(error).lower() else 502
            hint = "模型接口响应超时，请稍后重试；也可先将规则拆成较小的规则包生成。" if status == 504 else str(error)
            raise HTTPException(status, f"生成规则草稿失败：{hint}") from error
        rules = draft.get("rules") if isinstance(draft.get("rules"), list) else [draft]
        return render_rule_package(customer_code, requirement, [rule for rule in rules if isinstance(rule, dict)])

    @app.post("/admin/ai-rule-draft", response_class=HTMLResponse, include_in_schema=False)
    def generate_rule_draft(customer_code: str = Form(...), requirement: str = Form(""), rules_file: UploadFile | None = File(None), _: None = Depends(require_admin)) -> HTMLResponse:
        if rules_file is not None and rules_file.filename:
            try:
                requirement = read_rule_file(rules_file.filename, rules_file.file.read())
            except ValueError as error:
                raise HTTPException(400, str(error)) from error
            return render_rule_import_preview(customer_code, rules_file.filename, requirement)
        return generate_rule_package(customer_code, requirement)

    @app.post("/admin/ai-rule-imports/{import_id}/generate", response_class=HTMLResponse, include_in_schema=False)
    def generate_imported_rule_draft(import_id: str, _: None = Depends(require_admin)) -> HTMLResponse:
        imported = rule_file_imports.pop(import_id, None)
        if not imported:
            raise HTTPException(400, "导入预览已失效，请重新上传规则文件")
        customer_code, _, requirement = imported
        return generate_rule_package(customer_code, requirement)

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
            executor_name = str(rule.get("executor_name") or "")
            if executor_name and executor_name not in direct_executors | set(DEVELOPER_EXECUTORS):
                raise HTTPException(400, "请选择系统已支持的执行器名称")
            task_type = str(rule.get("task_type") or "deterministic")
            if task_type not in {"direct_atomic", "deterministic", "developer_executor", "semantic"}:
                raise HTTPException(400, "执行方式无效")
            config = rule.get("executor_config") or {}
            if not isinstance(config, dict):
                raise HTTPException(400, "执行器配置必须是 JSON 对象")
            if not isinstance(rule.get("input_field_ids", []), list):
                raise HTTPException(400, "依赖字段必须是 JSON 数组")
            # 后续规则可读取已经生成的 ERP 输出状态（例如“型号”初始化后再判断型号）。
            used = [value for value in rule.get("input_field_ids", []) if value in inputs | erp]
            group = next((row for row in repo.field_catalog()["groups"] if row[1] == customer_code and row[2] == field), None)
            group_id = group[0] if group else repo.create_field_rule_group(customer_code, field)
            repo.create_rule(group_id, str(rule.get("name") or "AI 规则"), str(rule.get("condition") or "always true"), str(rule.get("action") or ""), used, int(rule.get("priority") or 10), task_type, executor_name or None, json.dumps(config, ensure_ascii=False), bool(rule.get("enabled", True)))
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
            f"<tr><td>{escape(code)}</td><td><input form='customer-{escape(code)}' name='name' value='{escape(name)}' required></td><td><form class='inline-form' id='customer-{escape(code)}' method='post' action='/admin/customers/{escape(code)}'><label class='check-label'><input name='enabled' type='checkbox' {'checked' if enabled else ''}>启用</label><button>保存</button></form></td></tr>"
            for code, name, enabled in data["customers"]
        )
        customer_table = "<div class='table-shell'><table class='customer-table'><thead><tr><th>客户代码</th><th>客户名称</th><th>状态与操作</th></tr></thead><tbody>" + customer_rows + "</tbody></table></div><div id='customer-pagination' class='pagination' aria-label='客户信息分页'></div>"
        def options(rows, value_index: int, label_index: int) -> str:
            return "".join(f"<option value='{escape(str(row[value_index]))}'>{escape(str(row[label_index]))}</option>" for row in rows)
        customer_options = options(catalog["customers"], 0, 1)
        business_customer_options = "".join(f"<option value='{escape(code)}'>{escape(name)}</option>" for code, name in catalog["customers"] if code != "COMMON")
        customer_picker = "<div class='search-combobox' data-source='customers' data-required='true'><input class='search-combobox-input' placeholder='搜索客户代码或名称' autocomplete='off' required><input class='search-combobox-value' name='customer_code' type='hidden'><div class='search-combobox-menu' hidden></div></div>"
        customer_picker_data = json.dumps(
            [{"code": code, "name": name} for code, name in catalog["customers"] if code != "COMMON"],
            ensure_ascii=False,
        ).replace("<", "\\u003c")
        erp_picker = "<div class='search-combobox' data-source='erp' data-required='true'><input class='search-combobox-input' placeholder='搜索 ERP 字段代码或名称' autocomplete='off' required><input class='search-combobox-value' name='erp_field_id' type='hidden'><div class='search-combobox-menu' hidden></div></div>"
        owner_customer_picker = "<div class='search-combobox' data-source='customers'><input class='search-combobox-input' placeholder='不绑定客户（默认输出）；也可搜索客户' autocomplete='off'><input class='search-combobox-value' name='owner_customer_code' type='hidden'><div class='search-combobox-menu' hidden></div></div>"
        erp_picker_data = json.dumps(
            [{"code": field_id, "name": name} for field_id, name, _, _, enabled in catalog["erp"] if enabled],
            ensure_ascii=False,
        ).replace("<", "\\u003c")
        erp_options = "".join(f"<option value='{escape(field_id)}'>{escape(name)}{'（默认输出）' if owner is None else '（仅 ' + escape(owner) + '）'}</option>" for field_id, name, _, owner, _ in catalog["erp"])
        erp_owner_options = "<option value=''>不绑定客户（默认输出）</option>" + "".join(f"<option value='{escape(code)}'>{escape(name)}</option>" for code, name in catalog["customers"] if code != "COMMON")
        executor_options = "<option value=''>不使用执行器（模型编排 / 语义理解）</option><optgroup label='直接执行'>" + "".join(f"<option value='{name}'>{label}</option>" for name, label in (("copy_or_blank", "复制输入值；空值则输出空"), ("map_value", "按映射表转换值"), ("set_value", "固定值"), ("set_blank", "清空字段"), ("format_template", "按模板拼接字段"), ("classify_c003_contract", "C003 合同号分类"))) + "</optgroup><optgroup label='开发者执行器'>" + "".join(f"<option value='{escape(name)}'>{escape(name)}</option>" for name in sorted(DEVELOPER_EXECUTORS)) + "</optgroup>"
        group_options = "".join(f"<option value='{group_id}'>{escape(code)} / {escape(field)} / 顺序{order}</option>" for group_id, code, _, field, order, _ in catalog["groups"])
        input_options = "".join(f"<option value='{escape(field_id)}'>{escape(name)}</option>" for field_id, name, _, enabled in catalog["inputs"] if enabled)
        common_rows = [(field, name, condition, priority, task) for _, code, _, field, _, _ in catalog["groups"] if code == "COMMON" for _, _, rule_code, rule_field, name, condition, _, _, priority, task, *_ in catalog["rules"] if rule_code == "COMMON" and rule_field == field]
        common_table = table(["ERP 字段", "通用规则", "条件", "优先级", "执行方式"], common_rows) or "<p>暂无通用规则。</p>"
        input_editor = "".join(
            f"<form class='editor-row input-editor' method='post' action='/admin/input-fields/{escape(field_id)}'><div class='field-id'><span>字段 ID</span><code>{escape(field_id)}</code></div><label>显示名称<input name='display_name' value='{escape(name)}' required></label><label>类型<select name='data_type'>" +
            "".join(f"<option value='{kind}' {'selected' if kind == data_type else ''}>{kind}</option>" for kind in ("text", "date", "number")) +
            f"</select></label><label class='check-label'><input name='enabled' type='checkbox' {'checked' if enabled else ''}>启用</label><button>保存</button></form>"
            for field_id, name, data_type, enabled in data["input_fields"]
        )
        input_editor = "<div class='editor-list'>" + input_editor + "</div>"
        erp_editor = "".join(
            f"<form class='editor-row erp-editor' method='post' action='/admin/erp-fields/{escape(field_id)}'><div class='field-id'><span>字段 ID</span><code>{escape(field_id)}</code></div><label>显示名称<input name='display_name' value='{escape(name)}' required></label><label>列顺序<input name='sort_order' type='number' min='1' value='{order}' required></label><label>归属客户<select name='owner_customer_code'><option value='' {'selected' if owner is None else ''}>默认输出</option>" + "".join(f"<option value='{escape(code)}' {'selected' if code == owner else ''}>{escape(customer_name)}</option>" for code, customer_name in catalog["customers"] if code != "COMMON") + f"</select></label><label class='check-label'><input name='enabled' type='checkbox' {'checked' if enabled else ''}>启用</label><button>保存修改</button></form>"
            for field_id, name, order, owner, enabled in data["erp_fields"]
        )
        erp_editor = "<div class='editor-list'>" + erp_editor + "</div>"
        manual_rules = "<section id='manual-rules' class='card'><div class='card-head'><div><span class='card-kicker'>RULE BUILDER</span><h2>手工规则维护</h2><p class='hint'>先建立客户与 ERP 字段的规则组，再为规则组添加具体逻辑。</p></div><span class='section-icon'>⌘</span></div><form method='post' action='/admin/field-rule-groups'><label>客户" + customer_picker + "</label><label>ERP 字段" + erp_picker + "</label><button>建立规则组</button></form><form method='post' action='/admin/rules'><label>规则组<select name='group_id'>" + group_options + "</select></label><label>规则名称<input name='name' required></label><label>条件<textarea name='condition'>always true</textarea></label><label>动作<textarea name='action' required></textarea></label><input name='priority' type='hidden' value='10'><input name='task_type' type='hidden' value='deterministic'><input name='executor_config' type='hidden' value='{}'><button>新增规则</button></form></section>"
        input_fields_section = "<section id='input-fields' class='card'><div class='card-head'><div><span class='card-kicker'>INPUT CATALOG</span><h2>输入字段管理</h2><p class='hint'>维护订单源数据中可供规则引用的字段。</p></div><span class='section-icon'>↘</span></div><form method='post' action='/admin/input-fields'><label>字段 ID<input name='field_id' placeholder='input_xxx' required></label><label>显示名称<input name='display_name' placeholder='例如 验收要求' required></label><label>类型<select name='data_type'><option>text</option><option>date</option><option>number</option></select></label><button>新增输入字段</button></form>" + input_editor + "</section>"
        erp_fields_section = "<section id='erp-fields' class='card'><div class='card-head'><div><span class='card-kicker'>OUTPUT CATALOG</span><h2>ERP 输出字段管理</h2><p class='hint'>配置输出列顺序，并可将字段限定给特定客户。</p></div><span class='section-icon'>↗</span></div><form method='post' action='/admin/erp-fields'><label>字段 ID<input name='field_id' placeholder='erp_xxx' required></label><label>显示名称<input name='display_name' required></label><label>列顺序<input name='sort_order' type='number' min='1' required></label><label>归属客户" + owner_customer_picker + "</label><button>新增 ERP 字段</button></form>" + erp_editor + "</section>"
        other_section = "<section id='rules-list' class='card'><div class='card-head'><div><span class='card-kicker'>RULE INVENTORY</span><h2>全部规则清单</h2><p class='hint'>按客户或 ERP 字段快速定位当前规则。</p></div><span class='section-icon'>≡</span></div><div class='list-filters'><input id='rule-customer-search' placeholder='按客户代码搜索'><input id='rule-erp-search' placeholder='按 ERP 字段搜索'></div><h3>预处理规则</h3><div class='table-shell'>" + table(["规则 ID", "客户", "类型", "执行顺序", "启用"], data["preprocess_rules"]) + "</div><h3>全部规则</h3><div id='all-rules-table'>" + table(["客户", "ERP 字段", "规则名称", "条件", "优先级", "启用"], data["rules"]) + "</div><div id='rules-pagination' class='pagination' aria-label='全部规则分页'></div></section>"
        return HTMLResponse("""<!doctype html><html lang='zh-CN'><head><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'><title>业务规则管理</title><style>
:root{--navy:#14213d;--indigo:#3659c9;--indigo-dark:#2846ab;--cyan:#19a7ae;--ink:#182133;--muted:#667085;--line:#e5e9f2;--line-strong:#d8dfeb;--bg:#f4f6fa;--surface:#fff;--soft:#f8faff;--success:#16805f;--shadow:0 12px 34px rgba(23,43,77,.08)}
*{box-sizing:border-box}html{scroll-behavior:smooth}body{margin:0;background:radial-gradient(circle at 82% 0,#eaf0ff 0,transparent 28%),var(--bg);color:var(--ink);font:14px/1.55 system-ui,-apple-system,"Segoe UI","Microsoft YaHei",sans-serif;-webkit-font-smoothing:antialiased}.layout{display:grid;grid-template-columns:264px minmax(0,1fr);min-height:100vh}.sidebar{position:sticky;top:0;display:flex;height:100vh;flex-direction:column;padding:24px 18px;background:linear-gradient(180deg,#111c35 0%,#17284b 58%,#193357 100%);color:#d9e4ff;box-shadow:8px 0 30px rgba(15,29,55,.08)}.brand{display:flex;align-items:center;gap:12px;padding:4px 8px 22px;border-bottom:1px solid rgba(255,255,255,.1)}.brand-mark{display:grid;width:42px;height:42px;place-items:center;border-radius:13px;background:linear-gradient(145deg,#6f8cff,#35c4c7);color:#fff;font-size:20px;font-weight:800;box-shadow:0 8px 22px rgba(68,118,239,.32)}.brand-copy b{display:block;color:#fff;font-size:17px;letter-spacing:.01em}.brand-copy span{display:block;margin-top:2px;color:#91a8d2;font-size:10px;letter-spacing:.16em}.nav-label{margin:22px 10px 8px;color:#7f96c0;font-size:10px;font-weight:800;letter-spacing:.14em}.nav{display:grid;gap:4px}.nav a{display:flex;align-items:center;gap:11px;padding:10px 12px;border:1px solid transparent;border-radius:10px;color:#bac8e4;text-decoration:none;font-weight:650;transition:.18s ease}.nav a::before{width:7px;height:7px;border:2px solid currentColor;border-radius:50%;content:'';opacity:.65}.nav a:hover{background:rgba(255,255,255,.07);color:#fff}.nav a.active{border-color:rgba(133,161,255,.16);background:linear-gradient(90deg,rgba(88,120,233,.3),rgba(88,120,233,.12));color:#fff;box-shadow:inset 3px 0 #6f8cff}.nav a.active::before{border-color:#4ce0d2;background:#4ce0d2;box-shadow:0 0 0 4px rgba(76,224,210,.12)}.sidebar-foot{margin-top:auto;padding:16px 10px 4px;border-top:1px solid rgba(255,255,255,.1);color:#8298bd;font-size:12px}.sidebar-foot a{color:#b8c8e6;text-decoration:none}.content{width:min(100%,1240px);padding:34px 42px 72px}.topbar{display:flex;align-items:flex-start;justify-content:space-between;gap:24px;margin-bottom:22px}.eyebrow{margin:0 0 4px;color:var(--indigo);font-size:11px;font-weight:800;letter-spacing:.14em;text-transform:uppercase}.page-title{margin:0;color:var(--navy);font-size:30px;line-height:1.2;letter-spacing:-.035em}.page-subtitle{margin:7px 0 0;color:var(--muted)}.status-pill{display:flex;align-items:center;gap:8px;flex:0 0 auto;margin-top:8px;padding:8px 12px;border:1px solid #dbe7e2;border-radius:999px;background:#f2fbf7;color:var(--success);font-size:12px;font-weight:750}.status-pill::before{width:7px;height:7px;border-radius:50%;background:#20aa7b;box-shadow:0 0 0 4px #dff6ed;content:''}.metrics{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:12px;margin-bottom:20px}.metric{position:relative;overflow:hidden;padding:17px 18px;border:1px solid var(--line);border-radius:14px;background:rgba(255,255,255,.8);box-shadow:0 4px 16px rgba(23,43,77,.035);backdrop-filter:blur(8px)}.metric::after{position:absolute;right:-12px;bottom:-18px;width:66px;height:66px;border-radius:50%;background:var(--metric-color,#eef2ff);content:''}.metric span{display:block;color:var(--muted);font-size:12px}.metric b{display:block;margin-top:3px;color:var(--navy);font-size:23px;line-height:1.2}.card{margin-top:0;padding:27px 28px 30px;border:1px solid var(--line);border-radius:18px;background:var(--surface);box-shadow:var(--shadow);animation:fade-in .22s ease}.card[hidden]{display:none}.card#manual-rules{margin-top:18px}.card-head{display:flex;align-items:flex-start;justify-content:space-between;gap:20px;margin-bottom:22px;padding-bottom:19px;border-bottom:1px solid #eef1f6}.card-kicker{display:inline-block;margin-bottom:5px;color:var(--indigo);font-size:10px;font-weight:850;letter-spacing:.12em}.section-icon{display:grid;flex:0 0 auto;width:40px;height:40px;place-items:center;border-radius:12px;background:#eef2ff;color:var(--indigo);font-size:18px}h2{margin:0;color:var(--navy);font-size:20px;letter-spacing:-.015em}h3{margin:24px 0 8px;color:var(--navy);font-size:15px}.hint{margin:5px 0 0;color:var(--muted)}form{display:flex;flex-wrap:wrap;gap:14px;align-items:end;margin:18px 0;padding:18px;border:1px solid #e9edf4;border-radius:13px;background:var(--soft)}label{display:grid;gap:6px;min-width:160px;color:#344054;font-size:12px;font-weight:750}small{color:var(--muted);font-weight:450}input:not([type=checkbox]),select,textarea,button{min-height:42px;padding:9px 11px;border:1px solid var(--line-strong);border-radius:9px;background:#fff;color:var(--ink);font:inherit;transition:border-color .16s,box-shadow .16s,transform .16s}input::placeholder,textarea::placeholder{color:#98a2b3}input:focus,select:focus,textarea:focus{outline:0;border-color:#7892e8;box-shadow:0 0 0 3px rgba(79,105,204,.12)}input[type=checkbox]{width:16px;height:16px;margin:0;accent-color:var(--indigo)}textarea{min-width:300px;min-height:92px;resize:vertical}button{border-color:transparent;background:linear-gradient(135deg,var(--indigo),#4b6edb);color:#fff;font-weight:750;cursor:pointer;box-shadow:0 5px 12px rgba(54,89,201,.18)}button:hover{background:linear-gradient(135deg,var(--indigo-dark),#385bc7);box-shadow:0 7px 16px rgba(54,89,201,.24);transform:translateY(-1px)}button:disabled{cursor:wait;opacity:.65;transform:none}.inline-form{justify-content:flex-end;margin:0;padding:0;border:0;background:transparent;gap:9px;align-items:center;flex-wrap:nowrap}.check-label{display:flex;min-width:auto;align-items:center;gap:7px;white-space:nowrap}.customer-tools,.list-filters{display:flex;gap:10px;align-items:center;margin:16px 0 12px}.customer-tools input,.list-filters input{width:min(100%,320px);padding-left:36px;background:#fff url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='16' height='16' viewBox='0 0 24 24' fill='none' stroke='%2398a2b3' stroke-width='2'%3E%3Ccircle cx='11' cy='11' r='8'/%3E%3Cpath d='m21 21-4.35-4.35'/%3E%3C/svg%3E") no-repeat 11px center}.search-combobox{position:relative;min-width:250px}.search-combobox-input{width:100%}.search-combobox-menu{position:absolute;z-index:10;top:calc(100% + 5px);width:100%;max-height:230px;overflow:auto;border:1px solid #cbd5f0;border-radius:10px;background:#fff;box-shadow:0 14px 32px rgba(23,43,77,.16)}.search-combobox-menu button{display:block;width:100%;min-height:0;padding:10px 12px;border:0;border-bottom:1px solid #eef1f6;border-radius:0;background:#fff;color:#27364e;text-align:left;box-shadow:none}.search-combobox-menu button:hover{background:#f2f5ff;color:var(--indigo);transform:none}.table-shell{overflow:auto;border:1px solid var(--line);border-radius:12px}table{width:100%;border-spacing:0;background:#fff}th,td{padding:11px 13px;border-bottom:1px solid #edf0f5;text-align:left;vertical-align:middle}tbody tr:last-child td{border-bottom:0}tbody tr:hover{background:#fafbfe}th{background:#f7f8fb;color:#586780;font-size:11px;font-weight:800;letter-spacing:.035em;text-transform:uppercase}.customer-table{table-layout:fixed}.customer-table th:nth-child(1){width:22%}.customer-table th:nth-child(2){width:53%}.customer-table th:nth-child(3){width:25%}.customer-table input[name=name]{width:100%}#all-rules-table{overflow:auto;border:1px solid var(--line);border-radius:12px}#all-rules-table table{min-width:900px;table-layout:fixed}#all-rules-table th:nth-child(1){width:10%}#all-rules-table th:nth-child(2){width:17%}#all-rules-table th:nth-child(3){width:24%}#all-rules-table th:nth-child(4){width:35%}#all-rules-table th:nth-child(5),#all-rules-table th:nth-child(6){width:7%}#all-rules-table td:nth-child(2),#all-rules-table td:nth-child(3){overflow:hidden;text-overflow:ellipsis;white-space:nowrap}#all-rules-table td:nth-child(4){white-space:normal;overflow-wrap:anywhere;word-break:break-word;line-height:1.65;vertical-align:top}.pagination{display:flex;align-items:center;justify-content:space-between;gap:12px;margin-top:14px;color:var(--muted);font-size:12px}.pagination-info{white-space:nowrap}.pagination-actions{display:flex;align-items:center;gap:7px}.pagination button{min-width:0;min-height:34px;padding:6px 11px;border:1px solid var(--line-strong);background:#fff;color:#475467;box-shadow:none}.pagination button:hover:not(:disabled){border-color:#9cace0;background:#f4f6ff;color:var(--indigo);transform:none}.pagination button:disabled{background:#f6f7f9;color:#b0b7c3}.pagination select{min-height:34px;padding:5px 28px 5px 9px}.editor-list{display:grid;gap:10px;margin-top:14px}.editor-row{display:grid;gap:12px;align-items:end;width:100%;min-width:0;margin:0;padding:14px;border:1px solid #e8ecf3;border-radius:11px;background:#fbfcfe}.editor-row>*{min-width:0}.editor-row label{width:100%;min-width:0}.editor-row input:not([type=checkbox]),.editor-row select{width:100%}.editor-row:hover{border-color:#d8e0f0;background:#f9fbff}.field-id{display:grid;gap:6px;min-width:0;color:#344054;font-size:12px;font-weight:750}.field-id code{display:flex;align-items:center;width:100%;min-height:42px;overflow:hidden;padding:9px 11px;border-radius:9px;background:#eef2ff;color:var(--indigo);font-size:12px;font-weight:500;text-overflow:ellipsis;white-space:nowrap}.editor-row .check-label{align-self:end;justify-content:center;width:auto;min-height:42px}.editor-row button{align-self:end;min-width:76px;white-space:nowrap}.input-editor{grid-template-columns:minmax(180px,.8fr) minmax(230px,1.35fr) minmax(130px,.55fr) 72px 82px}.erp-editor{grid-template-columns:minmax(160px,.75fr) minmax(190px,1fr) 90px minmax(170px,1fr) 72px 96px}#customers>form:first-of-type{display:grid;grid-template-columns:minmax(180px,.65fr) minmax(280px,1.4fr) max-content}#input-fields>form:first-of-type{display:grid;grid-template-columns:minmax(180px,.8fr) minmax(230px,1.35fr) minmax(130px,.55fr) max-content}#erp-fields>form:first-of-type{display:grid;grid-template-columns:minmax(160px,.75fr) minmax(190px,1fr) 90px minmax(210px,1fr) max-content}#input-fields>form:first-of-type>*,#erp-fields>form:first-of-type>*{min-width:0}#input-fields>form:first-of-type input,#input-fields>form:first-of-type select,#erp-fields>form:first-of-type input,#erp-fields>form:first-of-type select{width:100%}#ai-rule-form label:nth-of-type(3){flex:1}.file-note{display:inline-flex;align-items:center;min-height:24px;color:var(--muted)}@keyframes fade-in{from{opacity:.5;transform:translateY(4px)}to{opacity:1;transform:none}}
.pagination-actions label{display:flex;align-items:center;gap:6px;min-width:0;white-space:nowrap}
@media(max-width:1180px){.content{padding:30px 26px 60px}.input-editor,.erp-editor,#input-fields>form:first-of-type,#erp-fields>form:first-of-type{grid-template-columns:repeat(2,minmax(0,1fr))}.editor-row .check-label{justify-content:flex-start}.input-editor button,.erp-editor button{justify-self:start}}
@media(max-width:760px){.layout{display:block}.sidebar{position:static;height:auto;padding:16px}.brand{padding-bottom:14px}.nav-label,.sidebar-foot{display:none}.nav{display:flex;overflow-x:auto;padding-top:12px}.nav a{flex:0 0 auto;white-space:nowrap}.nav a::before{display:none}.content{padding:24px 14px 48px}.topbar{align-items:center}.status-pill{display:none}.metrics{grid-template-columns:1fr}.card{padding:21px 16px}.card-head{margin-bottom:16px}form,.editor-row,#customers>form:first-of-type,#input-fields>form:first-of-type,#erp-fields>form:first-of-type{display:grid;grid-template-columns:1fr}.inline-form{display:flex}.list-filters,.customer-tools{display:grid}.list-filters input,.customer-tools input{width:100%}.pagination{align-items:flex-start;flex-direction:column}.pagination-actions{flex-wrap:wrap}textarea,.search-combobox{min-width:100%}.customer-table{min-width:620px}}
</style></head><body><div class='layout'><aside class='sidebar'><div class='brand'><span class='brand-mark'>R</span><div class='brand-copy'><b>业务规则管理</b><span>RULES CONSOLE</span></div></div><p class='nav-label'>工作台导航</p><nav class='nav'><a href='#customers'>客户管理</a><a href='#ai-rules'>规则生成与维护</a><a href='#input-fields'>输入字段管理</a><a href='#erp-fields'>ERP 输出字段</a><a href='#rules-list'>全部规则清单</a></nav><div class='sidebar-foot'>订单处理系统 · 管理端<br><a href='/'>返回订单处理首页 →</a></div></aside><main class='content'><header class='topbar'><div><p class='eyebrow'>Business Rule Workspace</p><h1 class='page-title'>规则配置中心</h1><p class='page-subtitle'>集中维护客户、字段与订单处理规则，让配置更清楚、更可控。</p></div><span class='status-pill'>规则库已连接</span></header>""" +
            "<div class='metrics'><div class='metric' style='--metric-color:#e8edff'><span>客户数量</span><b>" + str(len(data["customers"])) + "</b></div><div class='metric' style='--metric-color:#e6f8f5'><span>ERP 输出字段</span><b>" + str(len(data["erp_fields"])) + "</b></div><div class='metric' style='--metric-color:#fff1df'><span>当前规则</span><b>" + str(len(data["rules"])) + "</b></div></div>" +
            "<section id='customers' class='card'><div class='card-head'><div><span class='card-kicker'>CUSTOMER DIRECTORY</span><h2>客户管理</h2><p class='hint'>新增客户，或搜索并维护已有客户信息。</p></div><span class='section-icon'>◎</span></div><form method='post' action='/admin/customers'><label>客户代码<input name='code' placeholder='例如 C004' required></label><label>客户名称<input name='name' placeholder='例如 航天四院' required></label><button>新增客户</button></form><div class='customer-tools'><input id='customer-search' placeholder='按客户代码搜索' aria-label='按客户代码搜索'><input id='customer-name-search' placeholder='按客户名称搜索' aria-label='按客户名称搜索'></div>" + customer_table + "</section>" +
            "<section id='ai-rules' class='card'><div class='card-head'><div><span class='card-kicker'>AI ASSISTED</span><h2>AI 生成规则</h2><p class='hint'>粘贴业务规则或上传文件，生成可校对的规则草稿；确认后才会发布。</p></div><span class='section-icon'>✦</span></div><form id='ai-rule-form' method='post' action='/admin/ai-rule-draft' enctype='multipart/form-data'><label>客户" + customer_picker + "</label><label>上传规则文件<input id='rules_file' name='rules_file' type='file' accept='.txt,text/plain,.xlsx,application/vnd.openxmlformats-officedocument.spreadsheetml.sheet,.xlsm,application/vnd.ms-excel.sheet.macroenabled.12'><small id='rules_file_name' class='file-note'>尚未选择文件</small></label><label>业务规则<textarea name='requirement' placeholder='例如：验收要求包含一院时，计划标记填 YP017-X486。'></textarea></label><button id='ai-rule-button'>生成规则草稿</button><span id='ai-rule-status' class='hint'></span></form></section>" + manual_rules + input_fields_section + erp_fields_section + other_section + "<script>const customers=" + customer_picker_data + ",erpFields=" + erp_picker_data + ";const sections=[...document.querySelectorAll('section.card[id]')],nav=[...document.querySelectorAll('.nav a')];function showSection(id){const requested=id.replace('#','')||'customers',target=requested==='manual-rules'?'ai-rules':requested,isRulePage=target==='ai-rules';sections.forEach(s=>s.hidden=!(s.id===target||(isRulePage&&s.id==='manual-rules')));nav.forEach(a=>a.classList.toggle('active',a.getAttribute('href')==='#'+target));if(location.hash!=='#'+target)history.replaceState(null,'','#'+target)}nav.forEach(a=>a.onclick=e=>{e.preventDefault();showSection(a.getAttribute('href'));scrollTo({top:0,behavior:'smooth'})});showSection(location.hash);const f=document.querySelector('#ai-rule-form'),file=document.querySelector('#rules_file'),fileName=document.querySelector('#rules_file_name'),customerCode=document.querySelector('#customer-search'),customerName=document.querySelector('#customer-name-search');file.onchange=()=>fileName.textContent=file.files.length?'已选择：'+file.files[0].name:'尚未选择文件';function createPager(rowSelector,pagerSelector,matches){const rows=[...document.querySelectorAll(rowSelector)],host=document.querySelector(pagerSelector);let page=1,size=10;host.innerHTML=`<span class='pagination-info'></span><div class='pagination-actions'><label>每页 <select aria-label='每页显示数量'><option>10</option><option>20</option><option>50</option></select> 条</label><button type='button' class='page-prev'>上一页</button><span class='page-number'></span><button type='button' class='page-next'>下一页</button></div>`;const info=host.querySelector('.pagination-info'),number=host.querySelector('.page-number'),prev=host.querySelector('.page-prev'),next=host.querySelector('.page-next'),sizeSelect=host.querySelector('select');function render(){const filtered=rows.filter(matches),pages=Math.max(1,Math.ceil(filtered.length/size));page=Math.min(page,pages);rows.forEach(row=>row.hidden=true);filtered.slice((page-1)*size,page*size).forEach(row=>row.hidden=false);const start=filtered.length?(page-1)*size+1:0,end=Math.min(page*size,filtered.length);info.textContent=`显示 ${start}–${end} 条，共 ${filtered.length} 条`;number.textContent=`${page} / ${pages}`;prev.disabled=page<=1;next.disabled=page>=pages}prev.onclick=()=>{if(page>1){page--;render()}};next.onclick=()=>{page++;render()};sizeSelect.onchange=()=>{size=Number(sizeSelect.value);page=1;render()};render();return{reset(){page=1;render()},render}}const customerPager=createPager('.customer-table tbody tr','#customer-pagination',row=>{const code=customerCode.value.trim().toLowerCase(),name=customerName.value.trim().toLowerCase(),cells=row.cells;return(!code||cells[0].textContent.toLowerCase().includes(code))&&(!name||cells[1].textContent.toLowerCase().includes(name))});customerCode.oninput=customerName.oninput=()=>customerPager.reset();document.querySelectorAll('.search-combobox').forEach(box=>{const items=box.dataset.source==='erp'?erpFields:customers,input=box.querySelector('.search-combobox-input'),value=box.querySelector('.search-combobox-value'),menu=box.querySelector('.search-combobox-menu');const render=()=>{const q=input.value.trim().toLowerCase(),matches=items.filter(c=>!q||c.code.toLowerCase().includes(q)||c.name.toLowerCase().includes(q));menu.replaceChildren(...matches.map(c=>{const b=document.createElement('button');b.type='button';b.textContent=c.name+'（'+c.code+'）';b.onclick=()=>{input.value=c.name+'（'+c.code+'）';value.value=c.code;input.setCustomValidity('');menu.hidden=true};return b}));menu.hidden=!matches.length};input.onfocus=render;input.oninput=()=>{const exact=items.find(c=>c.code.toLowerCase()===input.value.trim().toLowerCase()||c.name===input.value.trim());value.value=exact?.code||'';input.setCustomValidity('');render()};input.onblur=()=>setTimeout(()=>menu.hidden=true,150);box.closest('form').addEventListener('submit',e=>{if(box.dataset.required==='true'&&!value.value){e.preventDefault();input.setCustomValidity('请从搜索结果中选择');input.reportValidity()}})});const ruleCustomer=document.querySelector('#rule-customer-search'),ruleErp=document.querySelector('#rule-erp-search'),rulesPager=createPager('#all-rules-table tbody tr','#rules-pagination',row=>{const customer=ruleCustomer.value.trim().toLowerCase(),erp=ruleErp.value.trim().toLowerCase(),cells=row.cells;return(!customer||cells[0].textContent.toLowerCase().includes(customer))&&(!erp||cells[1].textContent.toLowerCase().includes(erp))});ruleCustomer.oninput=ruleErp.oninput=()=>rulesPager.reset();f.onsubmit=()=>{const b=document.querySelector('#ai-rule-button'),s=document.querySelector('#ai-rule-status');b.disabled=true;b.textContent='正在生成…';s.textContent='正在读取规则并调用模型，通常需要 10–60 秒；较长规则包可能更久。';setTimeout(()=>{if(b.disabled)s.textContent='仍在生成中，请保持页面开启；超过约 2 分钟将显示超时提示。'},30000)}</script></main></div></body></html>")
        # 默认页面仅展示业务人员日常需要的内容；字段 ID 与完整表结构收进高级设置。
        return HTMLResponse("""<!doctype html><html lang='zh-CN'><head><meta charset='utf-8'><title>业务规则管理</title><style>
*{box-sizing:border-box}body{font:14px/1.4 system-ui,-apple-system,"Microsoft YaHei",sans-serif;max-width:1180px;margin:0 auto;padding:24px 24px 48px;color:#172033;background:#f6f8fc}h1{font-size:28px;margin:0 0 4px}h2{font-size:18px;margin:0 0 6px}.hint{color:#60708a;margin:0 0 8px}.card,details{border:1px solid #e1e7f0;border-radius:11px;padding:14px;margin:10px 0;background:#fff;box-shadow:0 2px 8px #19355a0a}form{display:flex;gap:8px;align-items:flex-end;flex-wrap:wrap;margin:9px 0}.inline-form{margin:0;gap:6px;align-items:center;flex-wrap:nowrap}label{display:grid;gap:3px;font-weight:600;color:#36465f}small{font-weight:400;color:#74839a}input,select,textarea,button{font:inherit;padding:7px 8px;border:1px solid #cbd5e1;border-radius:7px;background:#fff}input:focus,select:focus,textarea:focus{outline:2px solid #9cc3ff;border-color:#4f8df7}textarea{min-width:280px;min-height:68px;resize:vertical}button{background:#1769e0;color:#fff;border:0;border-radius:7px;font-weight:650;cursor:pointer;min-height:34px}button:hover{background:#0959c9}table{border-collapse:separate;border-spacing:0;width:100%;margin:8px 0;overflow:hidden;border:1px solid #e1e7f0;border-radius:8px}th,td{padding:7px 8px;border-bottom:1px solid #e8edf5;text-align:left;vertical-align:middle}tr:last-child td{border-bottom:0}th{background:#f3f7ff;color:#42536e}.customer-table input{max-width:265px}.customer-table td{height:48px}.step{display:inline-block;margin-bottom:4px;color:#1769e0;font-weight:750;font-size:12px;letter-spacing:.04em}.editor-row{padding:8px;border:1px solid #e5eaf2;border-radius:8px;background:#fbfcff}.editor-row code{padding:7px;background:#eef4ff;border-radius:5px;color:#275fae}</style></head><body>
<h1>业务规则管理</h1><p class='hint'>先定义字段，再按客户建立规则。带 * 的内容必须填写。</p>"""+
"<section class='card'><span class='step'>AI 辅助</span><h2>用自然语言生成规则草稿</h2><p class='hint'>粘贴邮件、会议纪要或 Word 中的规则文字，也可上传 TXT 或 Excel。AI 会按输出字段和条件拆成规则草稿；不会直接发布。</p><form method='post' action='/admin/ai-rule-draft' enctype='multipart/form-data'><label>客户*<select name='customer_code'>"+business_customer_options+"</select></label><label>业务规则<textarea name='requirement' placeholder='例如：规则1：生产标识：JHT；交货日期：当前日期加45天，格式 yyyyMMdd；验收要求含“一院”时，计划标记为 YP017-X486。'></textarea><small>可直接粘贴多条规则。</small></label><label>或上传规则文件<input name='rules_file' type='file' accept='.txt,text/plain,.xlsx,application/vnd.openxmlformats-officedocument.spreadsheetml.sheet,.xlsm,application/vnd.ms-excel.sheet.macroenabled.12'><small>Excel 需包含“录入ERP的字段”和“规则描述”两列；文件内容会替代上方粘贴内容。</small></label><button>生成规则草稿</button></form></section>"+
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
:root{color-scheme:light;--navy:#172554;--blue:#2563eb;--blue-dark:#1d4ed8;--ink:#172033;--muted:#64748b;--line:#dbe4f0;--surface:#fff;--soft:#eff6ff}*{box-sizing:border-box}body{min-height:100vh;margin:0;padding:54px 24px;font:15px/1.55 system-ui,-apple-system,"Microsoft YaHei",sans-serif;color:var(--ink);background:radial-gradient(circle at 15% 5%,#dbeafe 0,transparent 28rem),linear-gradient(135deg,#f8fbff 0%,#f3f7fb 100%)}.shell{width:min(100%,820px);margin:auto}.eyebrow{margin:0 0 8px;color:var(--blue);font-size:13px;font-weight:750;letter-spacing:.1em}.hero{margin-bottom:28px}.hero h1{margin:0;color:var(--navy);font-size:clamp(30px,5vw,42px);letter-spacing:-.04em}.hero p{max-width:610px;margin:10px 0 0;color:var(--muted);font-size:16px}.steps{display:grid;grid-template-columns:repeat(3,1fr);gap:12px;margin:0 0 18px}.step{display:flex;gap:10px;align-items:center;padding:12px 14px;border:1px solid var(--line);border-radius:12px;background:#ffffffb8;color:var(--muted)}.step b{display:grid;place-items:center;flex:0 0 auto;width:25px;height:25px;border-radius:50%;background:#dbeafe;color:var(--blue);font-size:13px}.step strong{color:#334155}.card{overflow:hidden;border:1px solid #d8e2ef;border-radius:18px;background:var(--surface);box-shadow:0 18px 46px #1e3a5f16}.card-header{padding:22px 26px 18px;border-bottom:1px solid #e8eef6}.card-header h2{margin:0;color:var(--navy);font-size:20px}.card-header p{margin:5px 0 0;color:var(--muted)}form{display:grid;gap:20px;padding:26px}label{display:grid;gap:7px;font-weight:700;color:#334155}input{width:100%;padding:11px 12px;border:1px solid #cbd5e1;border-radius:9px;background:#fff;color:var(--ink);font:inherit;transition:border-color .18s,box-shadow .18s}input:focus{outline:0;border-color:#60a5fa;box-shadow:0 0 0 4px #bfdbfe80}input[type=file]{padding:10px;background:#f8fafc;font-weight:400;cursor:pointer}input[type=file]::file-selector-button{margin-right:10px;padding:7px 10px;border:0;border-radius:6px;background:#e0edff;color:#1d4ed8;font:inherit;font-weight:650;cursor:pointer}.field-grid{display:grid;grid-template-columns:1fr 1fr;gap:18px}.hint{color:var(--muted);font-size:13px;font-weight:400}.actions{display:flex;flex-wrap:wrap;gap:12px;padding-top:3px}button{min-height:43px;padding:10px 16px;border:1px solid transparent;border-radius:9px;background:var(--blue);color:#fff;font:inherit;font-weight:700;cursor:pointer;box-shadow:0 4px 10px #2563eb38;transition:transform .16s,background .16s,box-shadow .16s}button:hover{background:var(--blue-dark);box-shadow:0 7px 15px #2563eb45;transform:translateY(-1px)}button:active{transform:translateY(0)}button.secondary{border-color:#cbd5e1;background:#fff;color:#334155;box-shadow:none}button.secondary:hover{background:#f8fafc;border-color:#94a3b8}.complete{margin-left:auto;background:#0f766e}.complete:hover{background:#0b5d57;box-shadow:0 7px 15px #0f766e40}#result{display:none;margin-top:18px;padding:15px 17px;border:1px solid #bfdbfe;border-radius:12px;background:var(--soft);color:#1e3a5f;white-space:pre-wrap}#result.show{display:block}#result.error{border-color:#fecaca;background:#fff1f2;color:#9f1239}#result a{color:#1d4ed8;font-weight:700}@media(max-width:700px){.steps{grid-template-columns:1fr}.complete{margin-left:0}}@media(max-width:600px){body{padding:32px 16px}.field-grid{grid-template-columns:1fr}.card-header,form{padding:20px}.actions button{width:100%}}
</style></head><body><main class='shell'><header class='hero'><p class='eyebrow'>ORDER WORKFLOW</p><h1>订单处理器</h1><p>既可分步校对，也可从原始材料一键完成订单处理。</p></header><div class='steps'><div class='step'><b>1</b><span><strong>提取并校对</strong><br>原始材料转为 JSON</span></div><div class='step'><b>2</b><span><strong>执行订单规则</strong><br>校对后的 JSON 导出结果</span></div><div class='step'><b>✓</b><span><strong>完整订单处理</strong><br>原始材料直接导出 Excel</span></div></div><section class='card'><div class='card-header'><h2>上传订单材料</h2><p>一次可选择同一客户的一批文件。</p></div>
<form id='form'><label>订单来源（可多选）<input name='files' type='file' multiple accept='.xlsx,.json,.eml,.pdf,.png,.jpg,.jpeg,.webp,.tif,.tiff,.docx,.txt,.md' required><span class='hint'>PDF、图片和邮件会经 MinerU 解析；Word、Excel 本地读取后，材料将合并抽取。</span></label>
<div class='field-grid'><label>提取批次名称<input name='batch_name' placeholder='例如 K-17-206'><span class='hint'>仅“提取并校对 JSON”需要填写；用于保存中间结果和 JSON。</span></label><label>规则处理输出文件名<input name='output_name' value='处理结果.xlsx'><span class='hint'>“执行订单规则”和“完整订单处理”使用，可保留默认名称。</span></label></div><div class='actions'><button name='mode' value='extract' type='submit'>提取并校对 JSON</button><button class='secondary' name='mode' value='process' type='submit'>JSON → 执行订单规则</button><button class='complete' name='mode' value='process' type='submit'>原始文件 → 完整订单处理</button></div></form></section>
<div id='result' role='status' aria-live='polite'>等待上传文件。</div></main><script>
const form=document.querySelector('#form'), result=document.querySelector('#result');
form.onsubmit=async e=>{e.preventDefault();const mode=e.submitter?.value||'process',fd=new FormData(form);fd.set('mode',mode);result.className='show';result.textContent=mode==='extract'?'正在提取订单字段，请稍候…':'正在执行订单规则，请稍候…';const r=await fetch('/ui/process',{method:'POST',body:fd});const d=await r.json();
if(!r.ok){result.className='show error';result.textContent='处理失败：'+(d.detail||'未知错误');return}result.className='show';if(d.mode==='extract'){const mineru=d.mineru_download_url?`<br><a href="${d.mineru_download_url}">下载 MinerU 中间结果</a>`:'';result.innerHTML=`抽取完成：${d.file_count} 个文件，共 ${d.extracted_count} 行。<br><a href="${d.extraction_download_url}">下载并校对 JSON</a>${mineru}`;return}const label=d.output_file_count>1?`下载拆分结果（${d.output_file_count} 个 Excel，ZIP）`:'下载处理结果';const json=d.extraction_download_url?`<br><a href="${d.extraction_download_url}">下载抽取 JSON</a>`:'';result.innerHTML=`处理完成：${d.file_count} 个文件，共 ${d.total} 行，成功 ${d.success_count} 行。<br><a href="${d.download_url}">${label}</a>${json}`};
</script></body></html>"""

    @app.post("/ui/process", include_in_schema=False)
    def process_upload(
        files: List[UploadFile] = File(...), batch_name: str = Form(""), output_name: str = Form("处理结果.xlsx"), mode: str = Form("process"),
    ) -> dict:
        allowed_suffixes = {".xlsx", ".json", ".eml", ".pdf", ".png", ".jpg", ".jpeg", ".webp", ".tif", ".tiff", ".docx", ".txt", ".md"}
        if not files:
            raise HTTPException(400, "请至少上传一个文件")
        suffixes = [Path(item.filename or "").suffix.lower() for item in files]
        if any(suffix not in allowed_suffixes for suffix in suffixes):
            raise HTTPException(400, "支持 .xlsx、.json、.eml、.pdf、图片（含 TIFF）、.docx、.txt 和 .md 文件")
        if mode not in {"mineru", "prepared_json", "extract", "process"}:
            raise HTTPException(400, "不支持的处理模式")
        prepared_suffixes = {".md", ".txt", ".docx", ".xlsx", ".json"}
        if mode == "prepared_json" and any(suffix not in prepared_suffixes for suffix in suffixes):
            raise HTTPException(400, "MD/Excel/Word → JSON 仅支持 .md、.txt、.docx、.xlsx 和 .json；PDF、图片和邮件请使用原始文件 → JSON")
        if mode == "process" and any(suffix not in {".json", ".xlsx"} for suffix in suffixes):
            raise HTTPException(400, "JSON → 执行规则仅支持已校对的 .json；原始 Excel 可直接执行")
        requested_batch_name = batch_name.strip()
        if mode == "process":
            safe_output = Path(output_name).name
            if Path(safe_output).suffix.lower() != ".xlsx":
                safe_output += ".xlsx"
            safe_batch_name = Path(safe_output).stem
            upload_dir = input_dir
            upload_dir.mkdir(exist_ok=True)
        else:
            safe_batch_name = Path(requested_batch_name).stem
            if not safe_batch_name or safe_batch_name in {".", ".."} or Path(requested_batch_name).name != requested_batch_name:
                raise HTTPException(400, "前置数据处理必须填写批次名称，且不能包含文件夹路径")
            safe_output = f"{safe_batch_name}.xlsx"
            upload_dir = test_input_dir
            upload_dir.mkdir(parents=True, exist_ok=True)
        output_dir.mkdir(exist_ok=True)
        uploaded_paths: list[Path] = []
        for index, file in enumerate(files, 1):
            suffix = Path(file.filename or "").suffix.lower()
            if mode == "process":
                uploaded_path = upload_dir / f"upload_{uuid4().hex}_{Path(file.filename or 'upload').name}"
            else:
                uploaded_name = f"{safe_batch_name}{suffix}" if len(files) == 1 else f"{safe_batch_name}_{index}{suffix}"
                uploaded_path = upload_dir / uploaded_name
                if uploaded_path.exists():
                    raise HTTPException(409, f"测试输入文件已存在：{uploaded_path.name}；请更换批次名称或先处理已有文件")
            with uploaded_path.open("wb") as target:
                shutil.copyfileobj(file.file, target)
            uploaded_paths.append(uploaded_path)
        output_path = output_dir / safe_output

        def bundle(paths: list[Path], directory: Path, archive_name: str) -> Path:
            archive_path = directory / archive_name
            with zipfile.ZipFile(archive_path, "w", zipfile.ZIP_DEFLATED) as archive:
                for path in paths:
                    if path.is_file():
                        archive.write(path, arcname=path.name)
            return archive_path

        # Web 路径不再依赖 AgentOS 的启动顺序；每次处理均显式加载项目 .env。
        load_project_env()
        logger.info("收到订单处理请求：模式=%s，文件数=%d，文件=%s", mode, len(uploaded_paths), [path.name for path in uploaded_paths])
        if mode == "mineru":
            mineru_dir = test_mineru_dir
            mineru_dir.mkdir(parents=True, exist_ok=True)
            raw_paths: list[Path] = []
            try:
                for uploaded_path in uploaded_paths:
                    raw_path = mineru_dir / f"{uploaded_path.stem}.md"
                    if raw_path.exists():
                        raise HTTPException(409, f"MinerU 输出已存在：{raw_path.name}；请更换批次名称或先处理已有文件")
                    reader = SourceTextReader()
                    if uploaded_path.suffix.lower() == ".xlsx":
                        raw_text = json.dumps(ExcelReader.read(str(uploaded_path)), ensure_ascii=False, indent=2)
                    elif uploaded_path.suffix.lower() == ".docx":
                        raw_text = reader.read_prepared_text(uploaded_path)
                    else:
                        raw_text = reader.read(uploaded_path)
                    raw_path.write_text(raw_text, encoding="utf-8")
                    raw_paths.append(raw_path)
            except RuntimeError as error:
                raise HTTPException(502, str(error)) from error
            download = raw_paths[0] if len(raw_paths) == 1 else bundle(raw_paths, mineru_dir, f"{safe_batch_name}_mineru.zip")
            return {"mode": "mineru", "file_count": len(files), "mineru_download_url": f"/ui/mineru-outputs/{download.name}"}
        process_orders = build_process_orders(os.getenv("DEEPSEEK_API_KEY"))
        extraction_paths: list[Path] = []
        extracted_count = 0
        saved_files: list[Path] = []
        total = success_count = failed_count = 0
        try:
            # 同一次上传的全部材料合并后仅调用一次 Qwen；多张图片/多份附件
            # 因此可共同补足同一订单，而不是被错误地拆成独立订单。
            batch_paths = [path for path in uploaded_paths if not (mode == "process" and path.suffix.lower() == ".xlsx")]
            if batch_paths:
                logger.info("开始准备合并批次：%d 个文件", len(batch_paths))
                ingestion = SourceIngestionService(
                    RuleRepository(project_root / "data" / "rules.db"), test_extraction_dir,
                )
                intermediate_path = test_mineru_dir / f"{safe_batch_name}.md" if mode == "extract" else None
                if intermediate_path is not None and intermediate_path.exists():
                    raise HTTPException(409, f"MinerU 中间结果已存在：{intermediate_path.name}；请更换批次名称或先处理已有文件")
                if mode in {"extract", "prepared_json"}:
                    json_path = test_extraction_dir / f"{safe_batch_name}.json"
                    if json_path.exists():
                        raise HTTPException(409, f"JSON 输出已存在：{json_path.name}；请更换批次名称或先处理已有文件")
                rows, extraction_json = ingestion.ingest_batch(
                    batch_paths, prepared=(mode == "prepared_json"),
                    archive_stem=safe_batch_name if mode in {"extract", "prepared_json"} else None,
                    intermediate_markdown_path=intermediate_path,
                )
                logger.info("合并批次完成：抽取到 %d 条订单", len(rows))
                extracted_count += len(rows)
                extraction_paths.append(Path(extraction_json))
                if mode not in {"extract", "prepared_json"}:
                    result = process_orders.execute_rows(rows, str(output_path))
                    total += result.get("total", 0)
                    success_count += result.get("success_count", 0)
                    failed_count += result.get("failed_count", 0)
                    if not result.get("success"):
                        raise HTTPException(500, f"订单处理失败：{result.get('failed_count', 0)} 行未成功")
                    saved_files.extend(Path(path) for path in result.get("output_files", []))
            for index, uploaded_path in enumerate(uploaded_paths, 1):
                if mode != "process" or uploaded_path.suffix.lower() != ".xlsx":
                    continue
                target_path = output_path if len(uploaded_paths) == 1 else output_dir / f"{Path(safe_output).stem}_{index}_{uploaded_path.stem}.xlsx"
                result = process_orders.execute(str(uploaded_path), str(target_path))
                total += result.get("total", 0)
                success_count += result.get("success_count", 0)
                failed_count += result.get("failed_count", 0)
                if not result.get("success"):
                    raise HTTPException(500, f"订单处理失败：{result.get('failed_count', 0)} 行未成功")
                saved_files.extend(Path(path) for path in result.get("output_files", []))
        except RuntimeError as error:
            raise HTTPException(502, str(error)) from error
        if mode in {"extract", "prepared_json"}:
            download = extraction_paths[0] if len(extraction_paths) == 1 else bundle(extraction_paths, test_extraction_dir, f"{safe_batch_name}_extractions.zip")
            return {"mode": "extract", "file_count": len(files), "extracted_count": extracted_count,
                    "extraction_download_url": f"/ui/extractions/{download.name}",
                    "mineru_download_url": f"/ui/mineru-outputs/{safe_batch_name}.md" if mode == "extract" else None}
        if not saved_files:
            raise HTTPException(500, "订单处理完成但未生成输出文件")

        # 一个合同直接交付 Excel；按合同拆分出多个文件时，统一打包为 ZIP，
        # 避免 Web 页仅返回单一下载链接而遗漏其他子表。
        if len(saved_files) == 1:
            download_name = saved_files[0].name
        else:
            download_name = bundle(saved_files, output_dir, f"{Path(safe_output).stem}_拆分结果.zip").name
        extraction_download_url = None
        if extraction_paths:
            extraction_download = extraction_paths[0] if len(extraction_paths) == 1 else bundle(
                extraction_paths, test_extraction_dir, f"{safe_batch_name}_extractions.zip"
            )
            extraction_download_url = f"/ui/extractions/{extraction_download.name}"
        return {
            "file_count": len(files), "total": total, "success_count": success_count,
            "output_file_count": len(saved_files),
            "extraction_download_url": extraction_download_url,
            "download_url": f"/ui/outputs/{download_name}",
        }

    @app.get("/ui/outputs/{filename}", include_in_schema=False)
    def download_output(filename: str) -> FileResponse:
        path = output_dir / Path(filename).name
        if not path.is_file():
            raise HTTPException(404, "输出文件不存在")
        return FileResponse(path, filename=path.name)

    @app.get("/ui/extractions/{filename}", include_in_schema=False)
    def download_extraction(filename: str) -> FileResponse:
        path = test_extraction_dir / Path(filename).name
        if not path.is_file():
            raise HTTPException(404, "抽取 JSON 不存在")
        return FileResponse(path, filename=path.name, media_type="application/zip" if path.suffix == ".zip" else "application/json")

    @app.get("/ui/mineru-outputs/{filename}", include_in_schema=False)
    def download_mineru_output(filename: str) -> FileResponse:
        path = test_mineru_dir / Path(filename).name
        if not path.is_file():
            raise HTTPException(404, "MinerU 输出不存在")
        return FileResponse(path, filename=path.name, media_type="application/zip" if path.suffix == ".zip" else "text/markdown")

    # AgentOS 自带根路由在注册时已存在；将用户首页置于路由表首位以覆盖它。
    homepage_route = next(route for route in reversed(app.router.routes) if getattr(route, "path", None) == "/")
    app.router.routes.remove(homepage_route)
    app.router.routes.insert(0, homepage_route)
