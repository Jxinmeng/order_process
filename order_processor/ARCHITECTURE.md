# 架构边界

本项目采用端口—适配器（Hexagonal）架构，并以 Agno 作为 AI 编排适配器。

```
interfaces (main.py / CLI)
          ↓
application (用例、ports)  ← 只依赖 domain/标准库
          ↓
infrastructure (Agno、SQLite、Excel、规则执行适配器)
          ↓
external SDKs / filesystem
```

## 模块职责

- `order_processor/domain/`：领域实体与结果对象，不依赖 SDK 或文件系统。
- `order_processor/application/`：用例编排及协议（端口），禁止导入 Agno、openpyxl、sqlite3。
- `order_processor/agentos.py`：唯一的 AgentOS 组合根，注册 Agent、Workflow、会话数据库与追踪。
- `order_processor/infrastructure/agno_rule_agent.py`：将 DeepSeek 的 OpenAI 兼容接口封装为规则 Agent。
- `order_processor/infrastructure/processing/`：规则匹配、任务分流和受限代码执行。
- `order_processor/infrastructure/excel/` 与 `persistence/`：Excel 和 SQLite 实现。

## 依赖治理规则

1. 业务规则的确定性路径不得调用模型；仅 `semantic` 和缺少已编译代码的动态规则可进入 Agno。
2. 应用服务只能通过 `Protocol` 端口访问技术实现，构造具体对象只能出现在 `bootstrap.py`。
3. LLM 输出必须经过既有字段白名单和 `CodeExecutor`；不得让 Agent 直接读写 Excel 或 SQLite。
4. 禁止跨越应用端口直接从接口层创建基础设施对象；替换实现只修改组合根。
