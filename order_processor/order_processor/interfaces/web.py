"""订单处理的轻量 Web 页面；不将 HTML 或 HTTP 细节泄漏到应用层。"""

from __future__ import annotations

import os
import shutil
import zipfile
import hmac
import secrets
from html import escape
from pathlib import Path
from uuid import uuid4

from fastapi import Cookie, Depends, FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse

from order_processor.bootstrap import build_process_orders
from order_processor.infrastructure.persistence.rule_repository import RuleRepository
from order_processor.shared.settings import load_project_env


def register_web_ui(app: FastAPI, project_root: Path) -> None:
    input_dir, output_dir = project_root / "input", project_root / "output"
    admin_sessions: set[str] = set()

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
        def table(headers: list[str], rows: list[tuple]) -> str:
            head = "".join(f"<th>{escape(value)}</th>" for value in headers)
            body = "".join("<tr>" + "".join(f"<td>{escape(str(value))}</td>" for value in row) + "</tr>" for row in rows)
            return f"<table><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table>"
        return HTMLResponse("""<!doctype html><html lang='zh-CN'><head><meta charset='utf-8'><title>业务规则管理</title><style>
body{font:15px system-ui;max-width:1200px;margin:36px auto;padding:0 24px;color:#172033}h1{margin-bottom:4px}.hint{color:#60708a}table{border-collapse:collapse;width:100%;margin:12px 0 30px}th,td{padding:8px;border-bottom:1px solid #d7dce5;text-align:left}th{background:#f3f7ff}code{font-size:13px}</style></head><body>
<h1>业务规则管理</h1><p class='hint'>管理员视图。用户处理端保持在首页，不显示规则配置。</p>""" +
            "<h2>客户</h2><form method='post' action='/admin/customers'><input name='code' placeholder='客户代码，如 C004' required><input name='name' placeholder='客户名称' required><button>新增客户</button></form>" + table(["客户代码", "客户名称", "启用"], data["customers"]) +
            "<h2>输入字段</h2>" + table(["字段 ID", "显示名称", "类型", "启用"], data["inputs"]) +
            "<h2>ERP 字段</h2>" + table(["字段 ID", "显示名称", "客户归属", "列顺序", "启用"], data["erp"]) +
            "<h2>规则</h2>" + table(["客户", "规则组", "规则名称", "条件", "启用"], data["rules"]) + "</body></html>")

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
