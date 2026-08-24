# 订单处理器

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

规则层级为：`客户 -> 规则组 -> 规则`。`customers` 表保存客户名称和唯一客户编码，
`rule_groups` 表归属到某个客户，`rules` 表保存动作描述、版本号、条件、优先级和启用状态。
Excel 可选填写 `客户名称` 列：程序会读取该客户的规则与“通用规则”；未填写时只使用
“通用规则”。规则按优先级从小到大执行，因此客户例外规则可设置更高优先级（如 `100`），
在最后覆盖通用规则的结果。

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
