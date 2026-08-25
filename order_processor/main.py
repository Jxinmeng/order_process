"""入口文件 - 启动工作流"""

import os
import argparse
from pathlib import Path

from order_processor.bootstrap import build_process_orders
from order_processor.shared.run_logger import RunLog, set_log_path
from order_processor.shared.settings import load_project_env


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
    
    # 组合根集中装配 SQLite、Excel 与 Agno/本地规则执行器。
    process_orders = build_process_orders(os.getenv("DEEPSEEK_API_KEY"))
    result = process_orders.execute(
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
