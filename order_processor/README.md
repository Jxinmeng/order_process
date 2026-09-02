# 订单处理器

## 架构

程序入口现在通过 `order_processor/bootstrap.py` 装配应用用例，而不是直接创建工作流。
应用层仅依赖端口；Excel、SQLite、Agno 和存量 `core/` 流程均位于基础设施适配器中。
完整边界、依赖方向及迁移规则请见 [ARCHITECTURE.md](ARCHITECTURE.md)。安装依赖后，配置
`DEEPSEEK_API_KEY` 时动态规则编译和语义规则会通过 Agno 调用 DeepSeek；无 Key 时仍走原有本地确定性兜底。

## AgentOS 服务

项目将 Agno 锁定为 `2.5.0`，因为现有 `data/agentos.db` 的迁移记录来自该版本。不要使用
未锁版本的 `pip install -U agno`；升级前应先备份数据库并执行官方迁移。

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

规则首次命中时，程序会通过 Agno 调用模型把该规则编译为可复用代码，并保存到 `rules.compiled_code`。
同一规则的后续订单会直接执行缓存代码，不再调用模型；当规则 `version` 变化时会自动重新编译。

## 多源订单输入（邮件 / PDF / 图片）

用户页面的“订单来源”现在也接受 `.json`、`.eml`、`.pdf`、图片、`.docx`、`.txt` 和 `.md`。
图片包含 `.png`、`.jpg/.jpeg`、`.webp`、`.tif/.tiff`；多页 TIFF 会先转换为 PDF，再交由 MinerU 解析。
Excel 仍按原方式直接读取；符合格式的 JSON 会跳过模型、直接校验后进入规则流程；其它格式的流程是：

```
原文件 -> MinerU 转文本/Markdown -> Qwen 结构化抽取 -> data/extractions/*.json -> 原有规则流程 -> Excel
```

抽取 JSON 的固定格式为 `{"orders": [{"字段名": "值"}]}`。字段名单并不写死在提示词中，系统会从
管理后台配置的 `input_fields` 自动取得；模型输出中的其它字段会被丢弃，避免未声明字段进入规则引擎。
凡是 `input_fields.data_type` 为 `date` 的字段，最终 JSON 会统一校验并输出为 `yyyyMMdd`（例如 `20260902`）。

在项目根目录 `.env` 中配置（模型名可按你在平台实际开通的 Qwen 3.6 部署名修改）：

```dotenv
QWEN_API_KEY=你的_Qwen_Key
QWEN_BASE_URL=https://dashscope.aliyuncs.com/compatible-mode/v1
QWEN_MODEL=qwen3.6
MINERU_API_URL=https://你的_MinerU_服务/parse
MINERU_API_KEY=你的_MinerU_Key
```

`MINERU_API_URL` 需提供 `multipart/form-data` 的 `file` 上传接口，并返回 `markdown`、`text`，或
`data.markdown` / `data.text` 中任一文本字段。这样 MinerU 的自建、SaaS 或网关实现都能通过配置接入。

若使用 MinerU 官网的轻量文件解析 API，可直接配置：

```dotenv
MINERU_API_URL=https://mineru.net/api/v1/agent/parse/file
MINERU_LANGUAGE=ch
```

系统会自动执行签名上传、任务轮询和 Markdown 下载；轻量接口不需要 `MINERU_API_KEY`，但受文件大小、页数和 IP 限流约束。

若使用 MinerU **标准版 Token**，请改为：

```dotenv
MINERU_API_URL=https://mineru.net/api/v4
MINERU_API_KEY=你的MinerU标准版Token
MINERU_MODEL_VERSION=vlm
```

系统会自动调用标准版的 `file-urls/batch`、上传签名 URL、轮询批次结果，并读取结果 ZIP 中的 `full.md`。

页面提供可独立验证的四种模式：**1. 原始文件 → MinerU** 用于下载 PDF、图片或 Word 的原始 Markdown/文本，不调用 Qwen；
**2. MD/Excel/Word → JSON** 跳过 MinerU，只验证字段 JSON 转换；**3. 原始文件 → JSON** 验证 MinerU + Qwen 全链路；
**JSON → 执行规则** 仅接收已校对的 JSON（或原始 Excel），才会进入现有订单规则链。校对过的 JSON 可再次上传，直接跳过模型后执行规则。
上传框支持同时选择多份文件；系统按文件边界独立解析和执行规则，批量产生的 Markdown、JSON 或 Excel 会打包为 ZIP 下载。
对于 `.eml` 邮件，邮件正文与每一个附件会分别经 MinerU/Qwen 处理；抽取 JSON 的 `parts` 数组保留每一部分的来源名称、类型和订单结果，便于校对附件是否被误识别或漏识别。

## 运行日志

每次运行会追加写入 `logs/order_processor.log`（UTF-8）。文件包含完整运行过程，以及每次
DeepSeek 调用的模型名称、完整输入提示词、原始输出或调用错误。请注意日志中会包含订单字段
内容，不要将该日志提交到公开仓库或发送给无关人员。
