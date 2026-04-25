# ParseCore Starter Kit

基于现有《解析设计》沉淀出的首版可嵌入解析管理骨架。

目标不是先做大而全的平台，而是先交付一个可植入其他产品的解析能力内核，满足以下约束：

- 嵌入优先，未来可外拔为独立服务
- 解析异步优先，RAG 保留同步和异步两种调用模式
- 以 Block / Chunk 为统一结构，不直接围绕全文字符串建模
- LLM 仅用于增强环节，不承担全文主解析

## 当前交付范围

当前仓库提供的是 Starter Kit，而不是完整业务实现：

- 项目目录骨架
- 核心数据模型与协议接口
- 可运行的最小 Runtime
- 可挂载的 ASGI API
- SQLite 持久化 JobStore 与查询接口
- 真实 DOCX 解析器与文本解析器
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

## 下一步优先级

1. 把 PDF 文本解析和图片 OCR 从占位实现推进到生产可用版本。
2. 把 SQLite 基线存储升级为 Postgres + pgvector。
3. 用当前 queue-worker 模式直接接入 jobcard 的文档与管理文库路由，开始双跑验证。
4. 在 jobcard 双跑稳定后，再把存储切到 Postgres 并补真实 OCR。
