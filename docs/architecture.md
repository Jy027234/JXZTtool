# ParseCore 架构说明

## 1. 定位

ParseCore 是一个面向解析管理场景的可嵌入内核。

它优先服务两类场景：

1. 新产品需要快速接入文档解析、Chunk、索引和基础 RAG 能力。
2. 现有系统需要逐步替换旧解析实现，但又不能一次性重构业务系统。

## 2. 架构分层

### 2.1 契约层

定义稳定的数据模型与扩展接口，避免业务系统直接依赖具体解析器。

- ParseRequest / ParseJob / ParseOutcome
- Block / Chunk
- ParserAdapter / ChunkBuilder / IndexAdapter / TranslationAdapter / ProductAdapter / JobStore

### 2.2 Runtime 层

负责状态流转、解析编排、Chunk 生成、索引入库和产品侧通知。

当前首版采用同步执行，目的是先把状态机和接口边界做稳定。后续可替换为队列驱动。

### 2.3 适配器层

把真实基础设施接入到契约层：

- 文档解析器：PyMuPDF / python-docx / OCR
- 存储：Postgres / pgvector / 对象存储
- 索引：pgvector / Elasticsearch / OpenSearch
- 翻译：外部 LLM 或 MT 服务
- 宿主适配器：Agent、知识库、其他业务系统

### 2.4 脚手架层

保存所有会频繁变化的资产：

- 配置
- Prompt
- 评测集
- 双跑差异报告模板与基线样本
- 适配器模板
- 接入说明

## 3. 关键设计决策

### 3.1 Block 优先于全文

解析的最小管理单元是 Block，而不是全文字符串。

原因：

- 表格、标题、图片、段落的处理策略不同
- Chunk 必须来自结构化 Block 聚合
- 后续人工校验、重分块、增量更新都依赖 Block 边界

### 3.2 异步优先

真正落到生产后，解析必须走异步作业模型：

- 上传成功不等于解析完成
- 解析与索引需要可重试、可补偿、可追踪
- 页面只关心 job 状态，不等待全文解析完成

### 3.3 LLM 只做增强

首版明确禁止把 LLM 当成全文主解析器。

允许的场景：

- 表格解释
- 图片说明
- 摘要
- 翻译
- RAG 回答

### 3.4 产品接入边界要独立

ParseCore 不直接写入业务对象。

业务差异通过 ProductAdapter 隔离，例如：

- 宿主系统的任务状态回写与事件通知
- Agent 项目的知识注入流程
- 其他产品的索引同步和事件通知

### 3.5 质量评估要与 Runtime 解耦

双跑验证、字段级与页面级差异摘要、Block/Chunk 统计不应混入 Runtime 主流程。

原因：

- Runtime 负责生产解析，不负责评测展示
- 质量评估需要允许按文件离线复跑，不依赖业务路由
- 差异报告需要支持逐步加字段，而不破坏主解析链路；当前除了字段级摘要外，还包含 raw/display 两套文本口径，以及基于页内相似度匹配的 Block 对位摘要，用于区分“展示噪声”与“真实结构差异”

因此评估能力应作为独立工具层存在；历史宿主双跑与差异分析资料统一保留在 archive，而不是继续耦合进主线 runtime。

## 4. 推荐部署形态

### 4.1 L1 嵌入式

直接作为 Python 包嵌入产品后端，优点是接入快、成本低。

### 4.2 L2 Sidecar

拆成内部 API + worker，产品后端通过内部 HTTP 或消息队列调用。

### 4.3 L3 平台化

多个产品复用后，再补多租户、配额、观测与策略治理。

## 5. 推荐技术选择

- 语言：Python
- API：FastAPI
- 队列：Celery 或 Dramatiq
- 存储：Postgres
- 向量：pgvector
- 解析器：PyMuPDF、python-docx、OCR 兜底

## 6. V1 与 V2 边界

### V1 必须交付

- PDF / DOCX / 图片输入
- ParseJob 状态机
- Block / Chunk 存储
- 基础索引接口
- 懒翻译接口
- 宿主接入适配器接口
- 基础双跑差异报告能力

### V2 再考虑

- 多租户
- 平台控制台
- 解析质量评分面板
- 多模型自动路由
- 多系统统一治理
