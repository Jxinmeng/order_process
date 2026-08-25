"""AgentOS 运行时：将订单处理工作流和专职 Agent 作为 API 对外提供。"""

from __future__ import annotations

import os
import json
from pathlib import Path
from typing import Any

from agno.agent import Agent
from agno.db.sqlite import SqliteDb
from agno.models.openai import OpenAIChat
from agno.os import AgentOS
from agno.workflow import Step, Workflow
from agno.workflow.types import StepInput, StepOutput

from order_processor.bootstrap import build_process_orders
from order_processor.interfaces.web import register_web_ui
from order_processor.shared.settings import load_project_env

PROJECT_ROOT = Path(__file__).resolve().parent.parent
AGENTOS_DB = PROJECT_ROOT / "data" / "agentos.db"


def _model() -> OpenAIChat | None:
    api_key = os.getenv("DEEPSEEK_API_KEY")
    if not api_key:
        return None
    return OpenAIChat(
        id=os.getenv("DEEPSEEK_MODEL", "deepseek-v4-flash"),
        api_key=api_key,
        base_url=os.getenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com"),
    )


def _process_order_file(step_input: StepInput) -> StepOutput:
    """Workflow Step：只允许处理项目 input/ 与 output/ 目录内的文件。"""
    if isinstance(step_input.input, dict):
        payload = step_input.input
    elif isinstance(step_input.input, str):
        try:
            payload = json.loads(step_input.input)
        except json.JSONDecodeError:
            return StepOutput(success=False, error="Workflow 输入必须是 JSON 对象")
    else:
        return StepOutput(success=False, error="Workflow 输入不能为空")
    if not isinstance(payload, dict):
        return StepOutput(success=False, error="Workflow 输入必须是 JSON 对象")
    input_path = Path(str(payload.get("input_path", ""))).resolve()
    output_path = Path(str(payload.get("output_path", ""))).resolve()
    input_root, output_root = (PROJECT_ROOT / "input").resolve(), (PROJECT_ROOT / "output").resolve()
    if input_root not in input_path.parents or output_root not in output_path.parents:
        return StepOutput(success=False, error="input_path 必须位于 input/，output_path 必须位于 output/")
    try:
        result = build_process_orders(os.getenv("DEEPSEEK_API_KEY")).execute(str(input_path), str(output_path))
        # AgentOS 会持久化 Workflow 输出；ProcessResult 属于领域对象，不能直接 JSON 序列化。
        content = {key: value for key, value in result.items() if key != "results"}
        return StepOutput(content=content, success=bool(content.get("success")), error=None)
    except Exception as error:
        return StepOutput(success=False, error=str(error))


def build_agentos() -> AgentOS:
    """创建可部署的 AgentOS 应用；数据库保存会话、运行记录与追踪。"""
    load_project_env()
    db = SqliteDb(db_file=str(AGENTOS_DB))
    model = _model()
    agents = []
    if model:
        agents = [
            Agent(
                name="Rule Compiler", model=model,
                description="将订单规则编译为受字段白名单约束的 Python 代码。",
                instructions=["只输出代码或用户要求的 JSON。", "不得访问文件系统、数据库或网络工具。"], db=db,
            ),
            Agent(
                name="Order Semantic Analyst", model=model,
                description="分析订单备注并返回受声明输出字段限制的 JSON。",
                instructions=["只输出合法 JSON。", "不得执行订单写入。"], db=db,
            ),
        ]
    order_processing = Workflow(
        id="order-processing",
        name="Order Processing",
        description="读取 input/ 下订单文件，执行规则并写入 output/。",
        db=db,
        steps=[Step(name="process_order_file", executor=_process_order_file)],
    )
    return AgentOS(
        name="Order Processor AgentOS",
        agents=agents,
        workflows=[order_processing],
        db=db,
        tracing=True,
    )


agent_os = build_agentos()
app = agent_os.get_app()
register_web_ui(app, PROJECT_ROOT)
