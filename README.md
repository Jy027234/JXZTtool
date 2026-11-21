# ParseCore Starter Kit

ParseCore 是可嵌入宿主产品、也可独立部署的文档解析中台内核，优先保留源文档结构，并提供异步解析、结构化结果读取和按需的 RAG 能力。

## 能力边界

- 负责：文档解析、Block/Chunk 生成、结构化 metadata、异步任务与局部重跑，以及受配置管理的 embedding、轻量检索和 pgvector 接入。
- 不负责：通用模型路由或模型市场、模型训练与生命周期治理、合规比对、业务规则判定及宿主产品的复核界面。
- 模型和解析 Provider 由部署侧 TOML 与环境变量管理；宿主仍可消费 `semantic_role`、`embedding` 和版面 metadata，或接入自己的检索服务。

## 当前交付与验证边界

当前交付包含 CPU 可运行的 Python SDK、CLI、ASGI API 和 queue-worker；SQLite 提供本地持久化任务、正式版本发布、待审 run 读取与文档结果查询。常用解析接入支持 Word、Excel、PDF、文本及按需 OCR；异步 job、structured projection、records/导出、质量视图和 PDF part 操作可按需使用。

`bd6462d` 的本机 CPU/SQLite fast 发版门禁为 `ok / verified`。`f46b4bc` 的 GitHub Fast self-check 已通过，Runner 缺少样本，真实样本回归仍标为 `unverified`。真实中文双栏、Postgres/pgvector 正式发布及文档 run 隔离、生产部署继续待验证。Postgres/pgvector 示例限存储与索引联调。启用发布时，内置索引对 `rag` 产物 profile 的支持继续暂缓。[当前验收与边界](docs/parsecore-reliability-mineru-todo-20260920.md#15-导出收尾与当前验收) · [自检门禁](docs/self-check-gate.md)

## 按任务查阅

| 任务 | 入口 |
| --- | --- |
| 本机启动 API，提交解析并读取结果 | [使用指南：快速启动与推荐流程](docs/user-guide.md#快速启动) |
| 选择 profile、projection，读取 records 或导出 | [使用指南](docs/user-guide.md)、[配置手册](docs/configuration.md) |
| 阅读页、质量诊断、Provider 对比、RAG 覆盖 | [使用指南：字段与诊断](docs/user-guide.md#前端接入字段清单)、[Provider Comparison](docs/user-guide.md#provider-comparison-产品字段)、[Coverage](docs/user-guide.md#coverage-消费指南) |
| 大 PDF、分片和局部复跑 | [使用指南：大文件和超长 PDF](docs/user-guide.md#大文件和超长-pdf)、[配置手册](docs/configuration.md) |
| SQLite 发布、Postgres/pgvector 联调与部署边界 | [灰度部署说明](docs/gray-deployment.md)、[配置手册](docs/configuration.md)、[上线准备](docs/go-live-readiness.md) |
| OCR 网关 | [OCR 接入清单](docs/ocr-integration-checklist.md)、[OCR 网关契约](docs/ocr-gateway-contract.md) |
| 自检、样本回归与发版 | [自检门禁](docs/self-check-gate.md)、[性能与稳定性](docs/performance-stability.md)、[发版说明](docs/release-notes.md) |
| 回归基线来源、语料状态和运维命令 | [本轮结果与样本基线](docs/parsecore-reliability-mineru-todo-20260920.md#8-本轮开发结果2026-10-07)、[自检门禁](docs/self-check-gate.md)、[配置手册：验收命令](docs/configuration.md#验收命令) |
| 架构、接口决策和开发历史 | [架构说明](docs/architecture.md)、[run / publication 契约](docs/adr-p0-02-run-publication-contract.md)、[当前规划与历史验收](docs/parsecore-reliability-mineru-todo-20260920.md#13-产品接入精简与首批模块拆分) |

## 目录结构

```text
.
├─ archive/          历史材料
├─ docs/             使用、配置、部署与验收文档
├─ fixtures/         回归和评估样本清单
├─ src/parsecore/    SDK、运行时与 API 实现
├─ tests/            自动化测试
├─ tools/            自检和样本工具
├─ parsecore.toml    本地 CPU/SQLite 配置
└─ pyproject.toml    包与可选依赖
```

## 快速启动

Windows PowerShell，在仓库根目录执行以下命令启动本地 CPU/SQLite API（Python 3.13 或以上）：

```powershell
py -3.13 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e ".[api,parsers]"
.\.venv\Scripts\python.exe -m parsecore.cli serve --config parsecore.toml --host 127.0.0.1 --port 8090
```

服务启动后，用另一个终端检查状态：

```powershell
Invoke-RestMethod http://127.0.0.1:8090/health
```

提交作业、轮询状态和读取结果的完整请求见[使用指南](docs/user-guide.md#推荐解析流程)。按需运行自检或样本回归时，依照[自检门禁](docs/self-check-gate.md)检查语料是否实际可用；不要将 `UNVERIFIED` 或 smoke-only 结果记作样本验收通过。
