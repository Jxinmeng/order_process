"""入口文件 - 启动工作流"""

import os
import ast
import argparse
from pathlib import Path

from models.rule import Rule
from core.workflow import OrderWorkflow
from storage.rule_repository import RuleRepository
from utils.run_logger import RunLog, set_log_path
from utils.settings import load_project_env


def load_rules_from_yaml(yaml_path: str) -> list:
    """从YAML加载规则"""
    # 配置只用到一个简单的规则列表。使用标准库解析，项目在尚未执行
    # ``pip install -r requirements.txt`` 的环境中也能启动。
    data = {"rules": []}
    current_rule = None
    with open(yaml_path, 'r', encoding='utf-8') as f:
        for raw_line in f:
            line = raw_line.strip()
            if not line or line.startswith("#") or line == "rules:":
                continue
            if line.startswith("- "):
                current_rule = {}
                data["rules"].append(current_rule)
                line = line[2:].strip()
            if current_rule is None or ":" not in line:
                continue
            key, value = (part.strip() for part in line.split(":", 1))
            if value.lower() in {"true", "false"}:
                parsed_value = value.lower() == "true"
            else:
                try:
                    parsed_value = ast.literal_eval(value)
                except (ValueError, SyntaxError):
                    parsed_value = value.strip("'\"")
            current_rule[key] = parsed_value
    
    rules = []
    for rule_data in data.get('rules', []):
        rules.append(Rule(
            id=rule_data['id'],
            name=rule_data['name'],
            condition=rule_data['condition'],
            action_description=rule_data['action_description'],
            priority=rule_data.get('priority', 0),
            enabled=rule_data.get('enabled', True),
        ))
    return rules


def parse_arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="按 SQLite 规则库处理订单 Excel 文件")
    parser.add_argument("--input", default="input/input_orders.xlsx", help="待处理的 Excel 文件路径")
    parser.add_argument("--output", default="output/output_orders.xlsx", help="处理结果的 Excel 输出路径")
    parser.add_argument("--log", default="logs/order_processor.log", help="运行日志及 DeepSeek 调用审计日志路径")
    return parser.parse_args()


def main(args: argparse.Namespace):
    env_loaded = load_project_env()
    deepseek_enabled = bool(os.getenv("DEEPSEEK_API_KEY"))
    print(f"DeepSeek: {'已启用' if deepseek_enabled else '未启用'}" + ("（已加载 .env）" if env_loaded else ""))
    input_path = Path(args.input)
    output_path = Path(args.output)
    if not input_path.is_file():
        raise FileNotFoundError(
            f"未找到输入文件: {input_path}。请将 Excel 文件放入该路径，或通过 --input 指定路径。"
        )
    output_path.parent.mkdir(parents=True, exist_ok=True)
    
    # 2. 初始化 SQLite 规则库。YAML 仅在首次建库时作为初始数据来源。
    repository = RuleRepository("data/rules.db")
    repository.initialize()
    rules = repository.load_active_rules()
    print(f"已从 data/rules.db 加载 {len(rules)} 条规则动作")
    
    # 3. 创建工作流
    workflow = OrderWorkflow(
        rules=rules,
        llm_api_key=os.getenv("DEEPSEEK_API_KEY"),
        rule_repository=repository,
    )
    
    # 4. 执行
    result = workflow.process(
        input_path=str(input_path),
        output_path=str(output_path)
    )
    
    # 5. 打印结果
    print(f"\n最终结果: {result}")


if __name__ == "__main__":
    arguments = parse_arguments()
    set_log_path(arguments.log)
    with RunLog(arguments.log):
        main(arguments)
