# 订单处理器

## 架构

程序入口现在通过 `order_processor/bootstrap.py` 装配应用用例，而不是直接创建工作流。
应用层仅依赖端口；Excel、SQLite、Agno 和存量 `core/` 流程均位于基础设施适配器中。
完整边界、依赖方向及迁移规则请见 [ARCHITECTURE.md](ARCHITECTURE.md)。安装依赖后，配置
`DEEPSEEK_API_KEY` 时动态规则编译和语义规则会通过 Agno 调用 DeepSeek；无 Key 时仍走原有本地确定性兜底。

## AgentOS 服务

完整 Agno 平台入口为 `order_processor.agentos:app`。它注册两个 Agent（规则编译、订单语义分析）和
一个 `order-processing` Workflow，并将会话、运行记录与追踪写入 `data/agentos.db`。

```powershell
docker compose up --build
```

服务启动后直接访问 `http://localhost:8000/`，上传 Excel、输入输出文件名并下载结果即可。`/docs` 保留给
接口调试或系统集成使用。调用 `POST /workflows/order-processing/runs` 时，使用表单字段 `message` 传入以下 JSON：

```json
{"input_path":"input/input_test.xlsx","output_path":"output/result.xlsx"}
```

直接读取已有 Excel 文件，不会自动创建或覆盖输入文件。

运行（默认读取 `input/input_orders.xlsx`）：

```powershell
python main.py
```

指定文件路径：

```powershell
python main.py --input "D:\订单\待处理订单.xlsx" --output "D:\订单\处理结果.xlsx"
```

规则存储在 `data/rules.db`（SQLite 单文件）。首次运行时会从
`config/rules.yaml` 导入初始规则，之后程序只读取数据库；修改 YAML 不会覆盖
已存在的数据库规则。

规则层级为：`客户 -> 数据预处理规则 / ERP 字段规则组 -> 规则`。

- `customers`：客户代码、客户名称与启用状态；
- `input_fields`：输入 Excel 字段字典；
- `erp_fields`：ERP 输出字段字典，`sort_order` 只决定 Excel 列顺序；
- `preprocess_rules`：读取 Excel 后、ERP 规则执行前的跨行处理，例如代码向上填充；
- `field_rule_groups`：唯一键为 `(customer_id, erp_field_id)`，`execution_order` 决定字段实际处理顺序；
- `rules`：一个规则只写入所属 ERP 字段组对应的一个输出字段，组内按 `priority` 从小到大执行。

同一字段先执行“通用规则”，再执行客户规则组中的规则；客户规则可用更高优先级覆盖默认值。
例如计划标记（子）可先执行默认规则 `priority=10`，再执行特殊覆盖规则 `priority=100/200`。

## DeepSeek API（可选）

未配置 API Key 时，程序使用本地内置动作处理，不会联网。若要使用 DeepSeek 生成处理代码，
先安装依赖，并任选一种方式配置 Key，再启动程序：

```powershell
pip install -r requirements.txt
$env:DEEPSEEK_API_KEY = "你的 DeepSeek API Key"
python main.py
```

或复制 `.env.example` 为项目根目录 `.env`，再填入 `DEEPSEEK_API_KEY`。`.env` 不应提交或发送给他人。

程序使用 DeepSeek 的 OpenAI 兼容接口 `https://api.deepseek.com`，默认模型为
`deepseek-v4-flash`。可在 `core/orchestrator.py` 的 `LLMOrchestrator` 初始化参数中
替换模型。

规则首次命中时，程序会调用模型把该规则编译为可复用代码，并保存到 `rules.compiled_code`。
同一规则的后续订单会直接执行缓存代码，不再调用模型；当规则 `version` 变化时会自动重新编译。

## 运行日志

每次运行会追加写入 `logs/order_processor.log`（UTF-8）。文件包含完整运行过程，以及每次
DeepSeek 调用的模型名称、完整输入提示词、原始输出或调用错误。请注意日志中会包含订单字段
内容，不要将该日志提交到公开仓库或发送给无关人员。
