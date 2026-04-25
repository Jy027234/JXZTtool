# ParseCore Starter Kit

基于现有《解析设计》沉淀出的首版可嵌入解析管理骨架。

目标不是先做大而全的平台，而是先交付一个可植入其他产品的解析能力内核，满足以下约束：

- 嵌入优先，未来可外拔为独立服务
- 解析异步优先，RAG 保留同步和异步两种调用模式
- 以 Block / Chunk 为统一结构，不直接围绕全文字符串建模
- LLM 仅用于增强环节，不承担全文主解析

## 能力边界

ParseCore 当前只负责解析流水线内的公共能力，不吞并宿主产品的检索与业务判断：

- 负责：文档解析、Block/Chunk 生成、结构化 metadata、可选 embedding 产出、异步任务与重跑
- 不负责：RAG 检索 API、向量库产品选型、合规比对、SOP/工卡匹配、业务规则判定
- 宿主产品可直接消费 `semantic_role`、`embedding`、layout metadata 等字段，自行决定写入 pgvector、Qdrant 或其他检索层

## 当前交付范围

当前仓库提供的是 Starter Kit，而不是完整业务实现：

- 项目目录骨架
- 核心数据模型与协议接口
- 可运行的最小 Runtime
- 可挂载的 ASGI API
- SQLite 持久化 JobStore 与查询接口
- 真实 DOCX 解析器与文本解析器
- PDF / OCR 结构块 `semantic_role` 标注（如 `toc_entry`、`highlights_entry`、`warning`）
- 可选 OpenAI-compatible embedding provider 与 chunk 级 embedding 落库
- 可切换的 `inline` / `queue-worker` 执行模式
- 独立 worker 入口与容器运行骨架
- 配置模板
- 面向 jobcard 的接入建议与补丁适配器
- 基础单元测试

## 建议演进路径

1. 先在当前仓库把 ParseCore 的契约、状态机和产品接入边界定稿。
2. 再把真实解析器、任务队列、数据库和向量检索逐步替换进来。
3. 用双跑方式接入 jobcard，先保持接口兼容，再替换解析实现。

## 目录结构

```text
.
├─ docs/
│  ├─ architecture.md
│  ├─ implementation-plan.md
│  └─ jobcard-integration.md
├─ src/
│  └─ parsecore/
│     ├─ __init__.py
│     ├─ asgi.py
│     ├─ bootstrap.py
│     ├─ cli.py
│     ├─ config.py
│     ├─ contracts.py
│     ├─ jobcard.py
│     ├─ models.py
│     ├─ runtime.py
│     ├─ stores.py
│     ├─ worker.py
│     └─ stubs.py
├─ tests/
│  ├─ test_asgi.py
│  ├─ test_jobcard.py
│  └─ test_runtime.py
├─ .dockerignore
├─ Dockerfile
├─ app.py
├─ docker-compose.yml
├─ parsecore.toml

├─ parsecore.queue.toml
└─ pyproject.toml
```

## 快速开始

安装为开发模式：

```powershell
d:/个人文件/个人开发/解析管理中台/.venv/Scripts/python.exe -m pip install -e .
```

如果要启动 API：

```powershell
d:/个人文件/个人开发/解析管理中台/.venv/Scripts/python.exe -m pip install -e ".[api]"
```

查看当前骨架描述：

```powershell
d:/个人文件/个人开发/解析管理中台/.venv/Scripts/python.exe -m parsecore.cli describe --config parsecore.toml
```

模拟提交一个解析任务：

```powershell
d:/个人文件/个人开发/解析管理中台/.venv/Scripts/python.exe -m parsecore.cli submit --config parsecore.toml --doc-id demo-doc --file-path samples/demo.docx --media-type application/vnd.openxmlformats-officedocument.wordprocessingml.document
```

启动本地 API：

```powershell
d:/个人文件/个人开发/解析管理中台/.venv/Scripts/python.exe -m parsecore.cli serve --config parsecore.toml --host 127.0.0.1 --port 8090
```

启动本地 queue worker：

```powershell
d:/个人文件/个人开发/解析管理中台/.venv/Scripts/python.exe -m parsecore.cli worker --config parsecore.queue.toml
```

用容器启动 API + worker：

```powershell
docker compose up -d --build
```

运行测试：

```powershell
d:/个人文件/个人开发/解析管理中台/.venv/Scripts/python.exe -m unittest discover -s tests -p "test_*.py"
```

只重算 chunk / embedding（跳过重新解析源文件）：

```powershell
d:/个人文件/个人开发/解析管理中台/.venv/Scripts/python.exe -m parsecore.cli submit --config parsecore.toml --doc-id demo-doc --file-path samples/demo.docx --media-type application/vnd.openxmlformats-officedocument.wordprocessingml.document --mode rerun_chunks_only
```

显式 API 路由：

- `POST /v1/parse/documents/{doc_id}/reparse`：重新执行完整解析
- `POST /v1/parse/documents/{doc_id}/rechunk`：复用已存 blocks，重算 chunk / embedding / index
- `POST /v1/parse/documents/{doc_id}/re-embed`：复用已存 blocks + chunks，仅重算 embedding / index
- `GET /v1/parse/documents/{doc_id}/search?q=...&role=warning&role=title`：基于已存 chunks 的轻量检索，支持 `semantic_role` 过滤并对 title/warning/note 等角色做内建权重

搜索响应包含：

- `retrieval_mode = "hybrid"`：query embedding 可用且至少有一条 chunk 向量参与排序
- `retrieval_mode = "keyword-fallback"`：query embedding 不可用，或无可参与的 chunk 向量，自动回退关键词排序

检索策略：

- 默认采用混合检索：向量优先（query embedding + cosine），关键词得分兜底
- 当查询 embedding 不可用（未配置 key/服务异常/维度不匹配）时自动回退到纯关键词，不中断请求
- 语义角色权重在融合后生效：title/warning 等提高排序优先级，toc_entry/lep_entry 适度降权

搜索说明：

- 当前是 runtime 内置的轻量检索面，优先服务嵌入式接入和本地验证
- 当宿主后续接入 pgvector / 外部检索层时，可以沿用相同的 `semantic_role` 过滤语义

启用 embedding provider：

```toml
[providers.embedding]
enabled = true
provider = "openai-compatible"
model = "text-embedding-3-small"
base_url = "https://api.openai.com/v1"
api_key_env = "PARSECORE_EMBEDDING_API_KEY"
batch_size = 16
```

真实 embedding 端到端 smoke test：

```powershell
$env:PARSECORE_EMBEDDING_API_KEY = "..."
d:/个人文件/个人开发/解析管理中台/.venv/Scripts/python.exe tools/_embedding_smoke.py
```

说明：

- 脚本会临时强制启用 embedding provider，构造一个 DOCX 样本，跑完整 submit 流程
- 输出包含 `embedded_chunk_ratio`、`mean_embedding_dim_norm`、`embedding_dim` 和一组 search 命中样本
- 如果未配置 `PARSECORE_EMBEDDING_API_KEY`，默认输出 `skipped` 并退出；传 `--require-live` 会改为非零退出

## 下一步优先级

1. 把 PDF 文本解析和图片 OCR 从占位实现推进到生产可用版本。
2. 把 SQLite 基线存储升级为 Postgres + pgvector。
3. 用当前 queue-worker 模式直接接入 jobcard 的文档与管理文库路由，开始双跑验证。
4. 在 jobcard 双跑稳定后，再把存储切到 Postgres 并补真实 OCR。
