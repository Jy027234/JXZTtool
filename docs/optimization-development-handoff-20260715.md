# 解析管理中台优化开发交接（2026-07-15）

## 1. 交接目的

本文用于在新的 Codex 对话中继续推进本项目的性能、可靠性和稳定性优化。新对话应以本文和本轮审计工件为事实来源，不重新猜测已完成范围，也不能通过放宽质量预算掩盖回归。

当前结论不是“所有优化工作结束”，而是：

- H-01 已闭环：默认 `fast` Provider 对比已缩为受控 7 页窗口，完整 `self-check --profile fast` 不带 skip 可稳定通过，且 Windows timeout 路径会清理子进程树；
- H-02 已闭环：固定 297 页样本的 OCR-warm 纯延迟门禁 5/5 通过，P50 `22.902 s`、CV `2.466%`，Python allocation 峰值均值 `725,298.017 KB`；
- H-04 已闭环：审计占位项判定已集中为单一规则，3/3 fast regression baseline 与 P1 8/8 均通过；
- 当前 H-01/H-02 汇总审计为 `22/22` 门禁通过、`passed_with_observation`，建议 `proceed_with_tail_monitoring`；
- H-03 已闭环：默认配置仍不强制启用 embedding/rerank；阿里 Qwen production profile 已通过真实 pgvector 与固定 Safran 样本验收，4/4 获批查询 rank 1 命中、hit@3=1.0；
- P7-T04 已闭环：embedding 入库、查询向量化和 rerank 共用固定 Provider 失败类别，终态失败可由事件与 Prometheus 聚合；真实 `batch_size=16` HTTP 400 已复演为 2 次 `invalid_input`；
- P7-T07 已闭环：同步/桥接上传、导出和 PDF part 使用受控私有目录、独占写入与安全扩展名；API 级 Provider 故障事件和 Prometheus 演练已通过；
- P7-T03 已闭环：完成态解析写入脱敏 `document_quality` 事件，固定质量 gate/flag 与 quality/coverage/embedding/provider warning 摘要由 Prometheus 暴露；P7-T01 至 P7-T10 至此全部完成；
- H-06 工程项已闭环并保留性能观察：阶段 P50 趋势只在严格同通道时比较；质量观测已改为轻量快照且单列阶段；完整 fast 通过，但高竞争主机上的新 clean-latency P50 `32.340s` 未通过原 `24.5s` 预算，禁止刷新基线；
- 共享 SQLite 压测污染已修复：历史 `var/parsecore.db` 测试库（约 1.48 GB、8,248 条 pending）已清除；`large-pdf-stress` 现在默认使用报告结束即删除的临时 SQLite，只有显式 `--use-configured-job-store` 才会写入配置库；定向回归 `55 passed`、全量回归 `653 passed / 6 skipped / 62 subtests passed`；
- 候选 Provider 仍不得自动提升为默认路由。

## 2. 工作区和保护要求

| 项目 | 当前值 |
| --- | --- |
| 工作区 | `D:\个人文件\个人开发\解析管理中台` |
| Git 分支 | `main` |
| 当前 HEAD | `8510802` |
| Python | 项目 `.venv`，要求使用 `D:\个人文件\个人开发\解析管理中台\.venv\Scripts\python.exe` |
| 固定典型样本 | `D:\app\uploads\36d65cd6b61346e28e97dbaf829646de.pdf` |
| 样本大小 | `2,634,672` bytes |
| 样本物理页数 | `297` |
| 样本 SHA256 | `AAA5F0A33F6BB716E407052842AB60F505B5E319CFB87B448A7249D28B678DCF` |

工作树在本文生成时包含约 77 项已修改或未跟踪路径，绝大部分属于此前 P0、P1 和本轮审计成果。新对话开始后必须先执行 `git status --short`，并遵守以下约束：

1. 不执行 `git reset --hard`、`git checkout --`、`git clean`，不删除 `output/`、`tmp/` 或现有审计工件。
2. 不覆盖或移动 `D:\app\uploads` 下的原始样本。
3. 不为使门禁通过而直接提高阈值或重建更宽松的基线。
4. 如需更新基线，必须先证明解析语义发生了预期变化，并备份旧基线、记录差异原因。
5. 不把 `tracemalloc` 通道与纯延迟通道混成同一时间序列。
6. 不把 AI-assisted Gold 审核描述为独立人工签字。

## 3. 权威证据入口

优先阅读：

1. H-01/H-02 机器审计：`var/self-check/optimization-h02-result-audit.json`、`var/self-check/optimization-h02-result-audit.md`
2. H-02 纯延迟稳定性：`var/self-check/optimization-h02-latency-ocr-warm-stability.json`、`.md`
3. H-02 内存稳定性：`var/self-check/optimization-h02-tracked-ocr-warm-stability.json`、`.md`
4. H-02 进程遥测趋势：`var/self-check/h02-process-telemetry-trend-20260715.json`、`.md`（两条通道不可合并的事实证据）
5. H-01 当前完整快门禁：`var/self-check/optimization-h02-fast.json` 及 `provider-comparison.fast.json/.md`
6. H-02 当前 P1 契约验收：`var/self-check/optimization-h02-p1-contract.json`
7. [首轮历史优化审计](optimization-result-audit-20260715.md)
8. `var/self-check/optimization-audit-baseline-before-page-completeness-20260715/`：历史基线备份，禁止删除。
9. [P0 验收记录](p0-acceptance-20260714.md)
10. [P1 契约验收记录](p1-acceptance-20260715.md)
11. [历史性能优化记录](performance-optimization-20260714.md)
12. 项目后续阶段清单：[parsecore-productization-todo.md](parsecore-productization-todo.md)

发布范围工件：

- `var/self-check/p0-core-readiness-20260715.json`：默认本地路径 5/5，本地 P0 `release_ready=true`。
- `var/self-check/p0-release-readiness-20260715.json`：生产范围仍有 2 个外部阻断，`release_ready=false`。
- `var/self-check/local-rag-acceptance-20260715.json`：本地 Transformer RAG 链路已验证，6/6 chunks embedded、4/4 查询 hit@3=1.0。
- `var/self-check/h03-aliyun-rerank-smoke-20260715.json`：阿里 Qwen `qwen/qwen3-vl-rerank` transport smoke 通过；仅记录排序索引和分数，不含密钥、查询或候选原文。
- `var/self-check/h03-aliyun-pgvector-rag-20260715.json`：隔离 Docker pgvector + 阿里 Qwen 端到端技术 smoke；5/5 chunks embedded，`vector(1024)`，检索为 `hybrid+rerank`。
- `var/self-check/h03-aliyun-pgvector-infrastructure-audit-20260715.json/.md`：H-03 基础设施验收汇总，状态 `passed_with_observation`；明确保留典型业务语料相关性验收项。
- `var/self-check/h03-aliyun-pgvector-fast-20260715/self-check.json`：数据库覆盖与 Compose 调整后的完整默认 fast 门禁；`601 passed`、`5 skipped`、3/3 regression 与 6/6 Provider run 通过。
- `var/self-check/h03-aliyun-safran-pages-204-206-draft-20260715.json`：首轮业务切片验收失败证据；`batch_size=16` 导致两批 HTTP 400，只有 5/37 chunks embedded，门禁正确失败。
- `var/self-check/h03-aliyun-safran-pages-204-206-batch5-draft-20260715.json` 与同名 `.md`：阿里 profile 改为实测 `batch_size=5` 后，37/37 chunks/units embedded，4/4 查询 rank 1 命中，hit@3/MRR@3 均为 1.0；查询套件仍标记为 agent-derived draft。
- `var/self-check/h03-aliyun-business-slice-fast-20260715/self-check.json`：业务切片工具和批量修正后的当前完整 fast 门禁，`601 passed`、`5 skipped`、3/3 regression、6/6 Provider run，gate=`accept`。
- `var/self-check/h03-aliyun-safran-pages-204-206-approved-20260715.json`：项目验收人批准 4 条查询与 hit@3=1.0 后的正式 production RAG 工件；37/37 chunks/units、4/4 rank 1、MRR@3=1.0。
- `var/self-check/h03-completion-audit-20260715.json/.md`：H-03 最终完成审计，状态 `passed`。
- `var/self-check/p7-provider-failure-http400-rehearsal-20260715.json`：固定 Safran 页段的真实 HTTP 400 复演；2 个失败批次均分类为 `invalid_input`，Prometheus 聚合值为 2，密钥和原始异常扫描为 0。
- `var/self-check/p7-provider-failure-category-audit-20260715.json/.md`：P7-T04 完成审计，状态 `passed`。
- `var/self-check/p7-provider-failure-fast-20260715/self-check.json`：P7-T04 后完整默认 fast 门禁；`605 passed`、`5 skipped`、3/3 regression、6/6 Provider run，gate=`accept`。
- `var/self-check/p7-private-files-audit-20260715.json/.md`：P7-T07 临时目录隔离与 API 运维演练完成审计，状态 `passed`。
- `var/self-check/p7-private-files-final-fast-20260715/self-check.json`：P7-T07 最终完整 fast 门禁；`610 passed`、`6 skipped`、3/3 regression、6/6 Provider run，gate=`accept`。
- `var/self-check/p7-quality-metrics-audit-20260715.json/.md`：P7-T03 质量指标系统化完成审计，状态 `passed`；未改变 report-only 门禁、阈值或 H-03 Gold。
- `var/self-check/p7-quality-metrics-fast-20260715/self-check.json`：P7-T03 后完整默认 fast 门禁；`612 passed`、`6 skipped`、3/3 regression、6/6 Provider run，gate=`accept`。
- `var/self-check/h06-stage-process-trend-20260715.json/.md`：H-06 严格同通道阶段/RSS 趋势；当前 clean-latency 总 P50 增加 `41.2%`，确定性质量观测开销已定位并修正。
- `var/self-check/h06-high-rss-optimization-task-20260715.json/.md`：高 RSS 专项观察任务；尚无获批绝对阈值，不改变 release gate。
- `var/self-check/h06-completion-audit-20260715.json/.md`：H-06 完成审计，状态 `passed_with_observation`。
- `var/self-check/h06-observability-fast-20260715/self-check.json`：H-06 完整默认 fast 门禁；`615 passed`、`6 skipped`、3/3 regression、6/6 Provider run，gate=`accept`。
- `var/self-check/h06-controlled-rerun-preflight-20260715.json/.md`：受控性能复测预检；连续六次仍有明显 Defender/WSL CPU 竞争和低空闲内存，正式 1+5 SLA 复测未启动，未修改任何系统或 Docker 设置。
- `var/self-check/h03-aliyun-rerank-fast-20260715/self-check.json`：加入配置化 rerank 后的完整默认 fast 门禁，未使用 `--skip-provider-comparison`；`599 passed`、`5 skipped`，3/3 回归与 6/6 Provider run 均通过。
- `var/self-check/optimization-audit-baseline-before-page-completeness-20260715/`：刷新 297 页完整性基线前的备份，禁止删除。
- `var/regression/baseline.json`、`baseline.table-structure.primary.json`、`baseline.strip_hf.json`：当前 297 页回归基线。

## 4. 已完成并验证的改进

### 4.1 首轮历史性能结果（仅用于趋势对比）

固定样本、固定配置、默认 `pdf-text` 路径的当前结果：

| 指标 | 历史值 | 当前值 | 改善 |
| --- | ---: | ---: | ---: |
| 带 Python 内存追踪的端到端耗时 | 148.144 s | 中位数 117.0 s | 21.023% |
| 相对上一轮稳定性中位数 | 135.035 s | 117.0 s | 13.356% |
| 不带 `tracemalloc` 的纯延迟 | 29.168 s | 中位数 23.276 s | 20.200% |
| Python 峰值内存 | 775,464.208 KB | 均值 729,078.254 KB | 5.982% |

纯延迟 5 次为：

```text
22.759 / 22.937 / 23.435 / 23.691 / 23.276 s
```

- 中位数：23.276 s
- 极差：0.932 s
- 变异系数：1.446%
- 5/5 运行成功

带 `tracemalloc` 的 5 次中有一次 152.561 s 长尾，但同次结构、Provider 和内存指纹均未漂移；第 5 次确认恢复为 117.0 s。该异常已保留，不得删除或用平均值掩盖。

### 4.2 质量和结构结果

10 次解析（5 次内存追踪、5 次纯延迟）均得到相同产品指纹：

```text
raw blocks = 2078
content blocks = 2035
chunks = 2035
tables = 44
figures = 411
physical pages = 297
primary/best provider = pdf-text/pdf-text
```

历史内容块、chunks 和表格数分别为 2,035 / 2,035 / 44，均未退化。当前多出的 43 个 raw blocks 是空白或不可提取页的显式审计证据：

- `semantic_role=parse_artifact`
- `index_policy=skip`
- `missing_reason=page_without_extractable_content` 或 `ocr_empty_text`
- 不生成 chunk，不进入 Reader 正文，不进入内容质量分母

物理页覆盖由历史 254 页提高为完整 297 页，内容质量分母仍为 2,035，噪声率保持 0。

### 4.3 本轮代码和工具修正

1. `src/parsecore/quality.py`
   - 空白/OCR 空文本审计块仍进入页数分母；
   - 不再进入 very-short、页眉页脚、numeric-heavy 和内容块分母。
2. `src/parsecore/pipelines.py`
   - 结构质量区分 raw items、quality denominator items 和 audit artifact items；
   - 审计占位项不再被误判为结构噪声。
3. `tools/parse_perf_baseline.py`
   - 保留默认 `tracemalloc` 口径，兼容历史内存基线；
   - 新增 `--no-track-python-memory` 纯延迟通道；
   - 未追踪内存时 `peak_kb=null`，Provider memory axis 保持 pending。
4. `tools/optimization_result_audit.py`
   - 聚合双通道性能、结构指纹、回归、自检和 P1 验收；
   - 输出可机读 JSON 和 Markdown，当前 10/10 门禁通过。
5. 新增/更新测试：
   - `tests/test_quality.py`
   - `tests/test_parse_perf_baseline.py`
   - `tests/test_optimization_result_audit.py`

本轮未通过提高预算掩盖回归。基线刷新仅用于接受 297 页完整物理页证据，旧基线已备份。

### 4.4 首轮自动化验证（历史记录）

- 正式核心快门禁：557 tests passed，5 skipped。
- Payload contracts：6/6 passed。
- 长文回归 suite：3/3 passed。
- P1 验收：8/8 checks、24/24 payloads passed。
- 补充全量 pytest：588 passed、5 skipped、51 subtests passed。
- 5 个 skipped 主要由 `PARSECORE_TEST_POSTGRES_URL` 未配置等可选外部环境条件产生，不是当前产品失败。

### 4.5 2026-07-15 H-01 / H-02 闭环证据（当前权威）

H-01 已将 `provider-suite.fast.json` 从重复三次 297 页整文档解析改为 7 页受控窗口；fast preflight 强制大 PDF 声明页段/页数预算，timeout 时通过 `taskkill /PID <pid> /T /F` 清理 Windows 子进程树。`optimization-h01-fast-r1/r2/r3.json` 和最新 `optimization-h02-fast.json` 均为 `ok`；最新完整门禁包含 Provider comparison，结果为 `samples=3`、`completed=6`、`failed=0`、`identity_drift=0`、`admission_update=0`，默认路由仍是 `pdf-text`。最后一次运行结束后未发现残留 Python 进程。

H-02 引入 `--runs`、`--warmup-runs`、`--cache-mode`、阶段计时、可选 `psutil` RSS/working set/CPU/I/O 遥测、P50/P95/max/CV、异常样本保留和策略门禁。正式 SLA 使用 `ocr_warm`：每次请求均显式 `parse_cache=false`，保留并审计页面 OCR 缓存；因此不会把约 `0.8 s` 的整文档解析缓存命中当作解析性能。独立 all-cache-disabled 冷解析探针为 `70.799 s`，仅作为冷态证据保存，不与 OCR-warm SLA 混合比较。

| 通道 | 运行 | 结果 |
| --- | --- | --- |
| 纯延迟（OCR-warm） | 1 次显式预热 + 5 次计量 | `22.376 / 22.544 / 23.410 / 22.902 / 23.918 s`；P50 `22.902 s`，P95 `23.816 s`，CV `2.466%`，5/5 成功 |
| Python allocation（`tracemalloc`） | 1 次显式预热 + 3 次计量 | 峰值均值 `725,298.017 KB`（预算 `750,000 KB`），3/3 成功；耗时仅作历史观测，不作 SLA |
| 结构 | 两通道所有计量运行 | 每次均为 `2078 raw / 2035 content / 2035 chunks / 44 tables / 411 figures / 297 pages` |
| 进程遥测 | 纯延迟 / 内存通道 | 延迟 RSS 最大 `887,869,440 B`；内存通道 RSS 最大 `1,883,668,480 B`。后者是必须持续监控的资源观察项，不改变 Python allocation 门禁结论 |

两条通道的每次计量运行都报告 `parse_cache_state=disabled`、`parse_cache_hit=0`；OCR-warm 运行每次观察到 `474` 个页面 OCR cache hit。`var/regression/performance-stability.297-page.json` 将这一缓存语义、质量指纹、延迟和内存预算固定为策略。`optimization-h02-result-audit.json` 最终为 `22/22` 门禁通过，保留三项非阻断观察：内存追踪耗时非 SLA、`rag_chunks_not_embedded`、43 个物理页审计占位项。

后续的 `perf_trend_report.py` 已将 parse performance baseline 的进程遥测接入趋势输出。它要求 clean latency 与 Python allocation-tracked 等测量通道分别积累历史，并将 RSS 变化显式保留为 observation，不自动转换为发布门禁或告警。当前双通道渲染工件为 `h02-process-telemetry-trend-20260715.json/.md`，结论为 `incompatible_measurement_channels`，因此不存在被错误聚合的 RSS “回归”。

## 5. 已关闭项目与下一轮优先级

### H-01：默认 fast Provider 门禁 — 已完成

- `provider-suite.fast.json` 改为受控页段，快门禁不再重复处理三次 297 页整文档；heavy Provider 保留在 full/perf lane。
- fast preflight 现在要求大 PDF 声明 `page_range`，并对单样本/总页数执行预算校验。
- Windows timeout 通过 `taskkill.exe /PID <pid> /T /F` 清理子进程树，测试覆盖清理与输出 drain；最终完整 fast 自检自然退出后无残留 Python 进程。
- 完整 `self-check --profile fast` 已连续三次并在 H-02 后再次通过，Provider comparison 真实执行，默认路由保持 `pdf-text`，候选保持 shadow/evaluate。
- H-03 配置准备后又运行了不带 skip 的完整 fast 门禁：`var/self-check/h03-config-fast-20260715/self-check.json` 为 `ok`，单测 `578 passed`、`5 skipped`，3/3 回归基线和 3 个受控样本的 6 次 Provider run 全部通过；`identity_drift=0`、`admission_update=0`。
- 阿里 Qwen rerank 接入后再次运行不带 skip 的完整 fast 门禁：`var/self-check/h03-aliyun-rerank-fast-20260715/self-check.json` 为 `ok`，单测 `599 passed`、`5 skipped`，3/3 回归基线与 6/6 Provider run 通过；`identity_drift=0`、`admission_update=0`，没有残留相关 Python 子进程。
- 真实 pgvector 技术验收后再次运行不带 skip 的完整 fast 门禁：`var/self-check/h03-aliyun-pgvector-fast-20260715/self-check.json` 为 `ok`，单测 `601 passed`、`5 skipped`，3/3 回归基线与 6/6 Provider run 通过；Provider gate 为 `accept`，无失败或跳过的 Provider run。

### H-02：可持续性能与长尾遥测门禁 — 已完成

- `tools/parse_perf_baseline.py` 一条命令完成 N 次运行、预热、P50/P95/max/CV、阶段耗时、异常样本和可选进程遥测。
- 纯延迟与 `tracemalloc` 内存通道严格分开；审计聚合器不再把内存追踪耗时当作发布 SLA。
- 计量缓存模式被固定为 `ocr_warm`：整文档 parse cache 始终绕过，页面 OCR cache 的 warm hit 被显式要求和记录；all-cache-disabled 冷态工件单独保存。
- H-02 质量、延迟、Python allocation 和工件审计门禁均通过。RSS 高峰仍作为 tail monitoring 观察项，不能被误称为已消除。

### H-03：关闭 embedding/rerank 配置和质量告警的产品边界 — 已完成

#### 现状

`parsecore.toml` 当前：

```toml
[providers.embedding]
enabled = false
provider = "openai-compatible"
model = "text-embedding-3-small"

[providers.rerank]
enabled = false
provider = "dashscope-compatible"
model = "qwen/qwen3-vl-rerank"
```

因此典型文档质量门禁为 `accept_with_warning`，唯一 flag 是 `rag_chunks_not_embedded`。这不是正文、表格或图示退化，也不能通过删除告警解决。

已有本地证据：

- 本地 Transformer RAG：6/6 chunks embedded；
- 4/4 查询 hit@3=1.0，MRR=1.0；
- OpenAI-compatible 本地兼容网关 transport smoke 已通过；
- 用户已选择阿里 Qwen 网关：`qwen/text-embedding-v4` 与 `qwen/qwen3-vl-rerank`；密钥仅由本轮命令临时注入环境变量，未写入仓库或工件；
- 远程 embedding transport 已实测返回 `1024` 维；Qwen rerank 已实测 `POST /v1/rerank` 的 `input.query` / `input.documents` 协议，并返回 `output.results[].index` / `relevance_score`；
- `var/self-check/h03-aliyun-rerank-smoke-20260715.json` 为 live transport 证据，`result_indexes=[1,2,0]`。这证明协议和账户可用，不替代真实业务语料的相关性验收。
- 独立 Docker 项目 `parsecore-h03-aliyun` 已在宿主端口 `55433` 提供健康 pgvector `0.8.2`；未占用其他项目的 `5432`，也未修改其他项目容器。
- `var/self-check/h03-aliyun-pgvector-rag-20260715.json` 已完成真实远程 embedding、pgvector 写入、hybrid 初检与真实远程 rerank：5/5 chunks embedded、`embedded_chunk_ratio=1.0`、数据库列为 `vector(1024)`、检索为 `hybrid+rerank`，3 个命中均有 `retrieval_score/rerank_score`；工件密钥特征扫描为 0。
- 上述端到端 smoke 使用工具生成的受控 DOCX，证明技术基础设施链路，不代表典型业务语料相关性已经获批。
- 新增 `tools/production_rag_acceptance.py`，可对外部文档或 PDF 页段执行可复现的 production profile 门禁；工件不持久化命中文本，只记录 chunk id、语义角色、初检分、排序分与期望短语命中。历史 draft 套件保留审计，正式 Gold 为 `fixtures/rag/safran-cmm-pages-204-206.approved.json`。
- 固定 Safran 样本的 204–206 页产生 37 chunks。首轮 `batch_size=16` 时前两批远程请求 HTTP 400、仅最后 5 条成功；质量门禁因 `embedded_chunk_ratio=0.135135` 正确失败。将阿里模板改为实测 `batch_size=5` 后，37/37 chunks、37/37 indexable units 全部为 1024 维，4/4 draft 查询 rank 1 命中、hit@3=1.0、MRR@3=1.0，全部为 `hybrid+rerank`。
- 项目验收人已明确批准这 4 条查询及 `hit@3=1.0` 为 H-03 正式标准；最终工件写入 `approval_status=business_owner_approved`、批准日期、角色与验收标准。

此前的 `local-rag-acceptance-20260715.json` 曾显示 chunks 6/6 embedded，但 `index_manifest.embedded_unit_count=0`。该问题已于本轮核实为 manifest 汇总遗漏，而不是 chunk 层与 KnowledgeUnit 层的语义差异：运行期已经逐个计算 `unit.embedded`，但没有把它聚合为 unit 计数；`ir/coverage/reader` 投影重建 `rag_coverage` 时也同样漏掉该字段。

已在 runtime 与投影层统一补齐 `embedded_unit_count / unembedded_unit_count`。`embedded_unit_count` 只统计“可索引、至少有一个 chunk，且该 unit 的全部 chunk 都已有向量”的 KnowledgeUnit；`unembedded_unit_count` 统计已有 chunk 但尚未全部向量化的可索引 KnowledgeUnit。新工件 `var/self-check/local-rag-acceptance-h03-20260715.json` 已验证本地 MiniLM 路径为 6/6 chunks、6/6 indexable units embedded、0 unembedded units，4/4 query hit@3=1.0、MRR=1.0。

本轮新增配置管理的二阶段排序实现：

- `src/parsecore/config.py` 新增 `[providers.rerank]`，包括 `provider/model/base_url/api_key_env/timeout_seconds/max_retries/candidate_limit/options`；默认关闭，`candidate_limit` 严格为正整数。
- `src/parsecore/rerank.py` 新增 `dashscope-compatible` Provider，按已验证的 Qwen 协议调用，并拒绝重复、越界、非整数或非有限分数的响应索引。
- `src/parsecore/runtime.py` 先保留现有 hybrid/keyword 初检，再对受限候选集重排；成功时输出 `hybrid+rerank` 或 `keyword-fallback+rerank`，命中保留 `retrieval_score/rerank_score`；失败时保持初检排序并记录不含密钥和正文的 `rerank_skipped`。
- `parsecore.pgvector.aliyun-rag.toml.example` 是选定的 Qwen `1024` 维 pgvector profile；既有不同维度 schema 必须迁移或新建，不能只改配置。
- `tools/_rerank_smoke.py` 可独立验证排序网关，不连接数据库、不写入文档，也不输出 query/candidate/credential。
- `PARSECORE_DATABASE_URL` 可在不复制配置和不落盘数据库凭证的前提下覆盖 `[storage].database_url`；Docker Compose 同时向 API/worker 转发该变量以及 embedding/rerank 密钥变量，并允许用独立宿主端口启动 pgvector/API。
- `parsecore.pgvector.aliyun-rag.toml.example` 的 `batch_size` 已从通用默认 16 收敛为该网关实测通过的 5，防止大于已验证批量的请求静默部分降级。

#### 建议实现

1. 不全局抑制 `rag_chunks_not_embedded`。
2. 性能基准明确标记 `parse_only`/embedding disabled，使告警成为“预期 observation”，而不是误认为解析失败。
3. [x] 核实并修正 `index_manifest.embedded_unit_count` 与实际 embedded chunks/units 的一致性；本地验收工具现在也将 unit-level embedding coverage 作为通过条件。
4. [x] 已提供明确的本地、通用远程和阿里 Qwen 生产配置示例，不在默认配置中写入密钥；`index.embedding_dimension` 已显式化，阿里 `text-embedding-v4` profile 固定为实测 `1024` 维。
5. [x] 用户已选择远程阿里 Qwen 模型，embedding/rerank transport 均已验证；`qwen/qwen3-vl-rerank` 也已接入配置、runtime、观测和安全降级。
6. [x] 已用隔离 pgvector 和受控 DOCX 完成真实 embedding/index/search/rerank 技术验收；rerank 故障时保留初检结果由 runtime 回归覆盖。
7. [x] 已在固定 Safran 典型样本的 204–206 页执行真实 production profile，37/37 chunks/units embedded，draft 查询 hit@3/MRR@3 均为 1.0。
8. [x] 项目验收人已批准 4 条查询、期望短语及 `hit@3=1.0` 预算，正式 Gold 和完成审计均已落盘；H-03 标记完成。

配置准备回归：模板解析、配置校验、pgvector dimension 路由、embedding/runtime/payload 覆盖共 `114 passed`；本轮 rerank provider、smoke、bootstrap、runtime 与 self-check 定向回归 `118 passed`；数据库覆盖补齐后的配置、bootstrap、embedding、rerank、runtime 定向回归为 `89 passed`；正式 Gold 固化后的定向回归为 `67 passed`；完整默认 fast 门禁为 `601 passed`、`5 skipped`、3/3 regression 和 6/6 Provider run 通过。新 P1 工件 `var/self-check/p1-contract-acceptance-h03-config-20260715.json` 为 `8/8` 通过、`24/24` payload。H-03 的数据库、模型路由、批处理、相关性与审批口径均已闭环。

#### 验收口径

- parse-only 审计继续保留并解释 `rag_chunks_not_embedded`。
- production RAG profile 的 `embedded_chunk_ratio=1.0`。
- manifest 的 chunk/unit embedding 计数与实际向量一致。
- 查询验收保持 hit@3=1.0 或由业务给出新的明确预算。
- 生产 RAG profile 不再出现 `rag_chunks_not_embedded`。
- 若生产 profile 开启 rerank，`retrieval_mode` 必须标记 `+rerank`，命中必须同时可审计初检分与 rerank 分；网关故障时必须保留初检顺序且查询成功返回。

### P7-T04：Provider 失败分类与运维聚合 — 已完成

- 新增 `src/parsecore/provider_failures.py`，固定 9 个低基数失败类别；Provider comparison 与 runtime 使用同一分类器。
- embedding 批量入库、查询向量化和 rerank 的终态失败均记录 `provider_failure`；事件携带 tenant/doc 定位信息，但不携带查询正文、候选正文或原始异常。
- Prometheus 新增 `parse_provider_failure_total{provider_type,provider_id,failure_category}`；未知类别归并为 `provider_failed`，标签值按 Prometheus 规则转义。
- 重试只增加原有 `embedding_retry`，最终失败才增加 Provider 失败计数；既有部分向量化降级、keyword fallback 和 rerank fallback 语义保持不变。
- `tools/production_rag_acceptance.py` 新增脱敏 Provider 故障摘要。固定 Safran 页段用临时 `batch_size=16` 复演真实网关限制，37 chunks 中 5 个成功，2 个失败批次都分类为 `invalid_input`，Prometheus 值为 2；门禁按预期失败。
- 定向回归 `106 passed`；完整 fast 为 `605 passed`、`5 skipped`、3/3 regression、6/6 Provider run，gate=`accept`。P7-T04 完成审计见 `var/self-check/p7-provider-failure-category-audit-20260715.json/.md`。

P7-T04 完成后按主线继续处理 P7-T07 与 API 运维面板演练，结果如下；能力边界仍不得扩张成宿主问答平台。

### P7-T07：临时目录隔离与 API 运维演练 — 已完成

- 新增 `src/parsecore/private_files.py`：受控根目录边界复核、独占文件创建、安全扩展名、POSIX `0700/0600` 和 Windows 继承 ACL/best-effort chmod。
- 同步上传改写入本地对象存储 `_api_transient` 并在请求结束后删除；异步桥接继续使用 `_api_uploads` 和 retention；非本地对象存储桥接继续拒绝。
- 导出包 `_exports/exp_<uuid>` 与 PDF part `_parsecore_parts/<doc>/<job>` 使用同一私有文件控制；NTFS ADS 风格或异常扩展名降级为 `.bin`。
- API 演练验证查询向量网关不可用时搜索仍为 `keyword-fallback`，`/v1/parse/events` 与 `/v1/parse/prometheus` 返回一致的 `provider_unavailable`，事件不含原始异常。
- 定向回归 `179 passed`、`1 skipped`；最终完整 fast `610 passed`、`6 skipped`、3/3 regression、6/6 Provider run，gate=`accept`。Windows skip 是当前环境不能创建目录符号链接。

P7-T04/P7-T07 和 API 运维演练均已完成；P7-T03 质量指标系统化结果如下。

### P7-T03：质量指标系统化 — 已完成

- 每次完成态解析写入一条 `document_quality` 事件，使用既有 `/quality` 的配置阈值、coverage 语义和 report-only gate 优先级，但不在 execute 中重建完整诊断投影；事件不含正文或原始异常。
- Prometheus 新增固定 gate/flag 计数，以及质量分、三项 coverage、embedding ratio、质量信号数和 Provider warning 数的全局 `_sum/_count`；不使用 `doc_id` 标签，未知值归并 `other`。
- 分片父文档只在所有 part 合并并进入 `DONE` 后记录最终观测；质量遥测自身失败不会反向导致解析失败。
- 定向回归 `133 passed`；完整 fast 为 `612 passed`、`6 skipped`、3/3 regression、6/6 Provider run，gate=`accept`。
- 完成审计见 `var/self-check/p7-quality-metrics-audit-20260715.json/.md`。P7-T01 至 P7-T10 至此全部闭环，后续只按真实灰度数据做趋势采集，不凭空新增阈值。

### H-04：集中化审计占位项判定，防止质量口径再次分叉（中高优先级）

#### 现状

本轮已经修正质量分母，但相同判定目前分别存在于：

- `src/parsecore/quality.py::_is_non_content_audit_block`
- `src/parsecore/pipelines.py::_is_non_content_audit_item`

两个实现使用相同 missing reasons，但长期维护可能发生漂移。

#### 本轮 H-04 结果（2026-07-15）

已新增无依赖公共模块 `src/parsecore/audit_placeholders.py`，作为唯一的分类来源。只有同时满足 `semantic_role=parse_artifact`、`index_policy=skip`，且 `missing_reason` 精确为 `page_without_extractable_content` 或 `ocr_empty_text` 时，记录才会从内容质量分母排除。`quality.py` 和 `pipelines.py` 均已委托给该规则；普通正文即使携带部分相似 metadata 仍保留在分母中。

已审计相关消费者：`evaluate_blocks` 与 `_build_structure_quality` 使用共享规则；`regression_baseline.py`、`ocr_benchmark.py` 和性能/审计工具只消费前两者的输出或显式展示 raw count，不再各自重判。新单测覆盖两种 missing reason、正例、反例以及两个质量消费者。

#### 建议实现

1. [x] 将 audit placeholder 分类规则集中到无循环依赖的公共模块。
2. [x] 审计所有使用 block/item 总数作为质量分母的消费者，明确 raw count、content count、quality denominator count。
3. [x] 同时覆盖 `page_without_extractable_content` 和 `ocr_empty_text`。
4. [x] 防止普通用户正文仅因 metadata 部分相似就被错误排除。
5. [x] 保持现有 API 向后兼容；未新增 payload 字段，P1 schema/contract 验收已通过。

#### 验收口径

- [x] 297 页默认/表格路径仍为 2,078 raw、2,035 content、43 audit artifacts。
- [x] very-short ratio 保持 0.0619，noise ratio 保持 0。
- [x] 3/3 fast regression baseline 与 P1 8/8 全部通过。
- [x] 已新增正例、反例和两种 missing reason 的单测。

### H-05：候选 Provider 和生产范围阻断（不得自动批准）

这些项目不能通过普通代码修改直接视为关闭：

| 项目 | 当前状态 | 要求 |
| --- | --- | --- |
| `pymupdf4llm-local` | shadow only | 存在许可证/商业使用确认和结构差异；获得明确批准前不得改路由 |
| `docling-local` | evaluate/pending | 50 页平均 Gold 53.021、39/50 critical token 缺失、最大耗时 234.983 s，不得替代默认 Provider |
| MinerU | skipped | 依赖/接入未就绪，不得假定可用 |
| 远程 embedding/RAG | production profile 已验收、默认关闭 | 阿里 Qwen + pgvector + rerank 已有正式 Gold 与 live 证据；保持按宿主部署显式启用，不提升为默认必选能力 |
| 独立人工 Gold | optional governance | 只有治理要求时补做；当前 AI-assisted 结果不能冒充人工签字 |

`var/self-check/p0-release-readiness-20260715.json` 是 H-03 之前的历史快照，其中远程 embedding live 阻断现已由 H-03 关闭；候选 Provider 许可证/商业审批仍未关闭。默认 `p0-core` 本地路径不被这些外部条件阻断。

### H-06：继续完成 P6/P7 可观测与运维项 — 已执行，保留性能观察

完成 H-01/H-02 后，继续按 [parsecore-productization-todo.md](parsecore-productization-todo.md) 的 P6/P7 推进：

- 质量、coverage、Provider、性能趋势；
- upload/parse/normalize/chunk/embed/export/rerun 阶段耗时；
- 稳定错误分类和 fallback 原因；
- 工件保留期和 dry-run 清理；
- 配置化回滚：关闭 local routing、回到默认 Provider、关闭候选 profile；
- 日志密钥和业务原文脱敏。

本轮已完成剩余工程项：`perf_trend_report.py` 新增严格同通道阶段 P50 趋势和缺失阶段报告；P7-T03 质量观测从完整 `/quality` 重建改为 blocks/index manifest 轻量摘要，并单列 `quality_observability` 阶段。定向测试 `154 passed`，完整 fast `615 passed / 6 skipped`、3/3 regression、6/6 Provider run、gate=`accept`。

新的 5 次 clean-latency OCR-warm 复测未通过：P50 `32.340s`，原预算 `24.5s`。趋势显示 parse P50 较 H-02 增加 `28.7%`；执行时同时观察到 Defender/WSL CPU 竞争和约 `15.47%` 空闲物理内存。质量遥测的确定性开销已经修正，拆分后单次 `quality_observability=0.117s`；剩余 parse 变慢必须在受控主机复测，当前失败工件不得删除，预算不得放宽。

后续受控复测已做启动前预检，但连续六次总 CPU 仍为 `31–36%`，Defender 为 `76–159%`，WSL 峰值 `225%`，空闲物理内存 `15.63–16.15%`。为避免重复生成已知污染的 SLA 工件，本次没有再次启动 1+5 正式运行，也没有终止其他项目进程或修改系统设置。

## 6. 推荐的新对话开发顺序

### 已完成：Phase A / B

H-01 完整 fast Provider 门禁和 H-02 双通道稳定性门禁均已完成。后续改动不得回退为 `--skip-provider-comparison`，也不得将 `tracemalloc` 耗时混入纯延迟 SLA。

### 已完成：Phase C embedding/rerank 产品化

1. [x] 已核实 manifest embedded unit count 与实际 embedded chunks/units 的语义一致性，并用 runtime、projection 与本地端到端验收覆盖。
2. [x] 已选择阿里 Qwen 远程 profile，并完成 endpoint、模型、临时凭证注入和 `batch_size=5` 的实测收敛。
3. [x] 已在固定业务样本上通过获批 Gold、真实 pgvector 与完整 fast gate；密钥未落盘。

### 已完成：Phase D 质量口径去重

1. [x] 集中审计占位项规则。
2. [x] 补齐正反例和契约测试。
3. [x] 重跑 3 个 fast 回归 baseline check；没有语义变化，未刷新 baseline。

### Phase E：P6/P7 可观测与运维

1. [x] H-02 进程遥测已纳入长期趋势入口：`tools/perf_trend_report.py` 可读取 parse performance baseline，汇总 RSS / working set / VMS / CPU / I/O，并仅在测量通道完全一致时比较；`var/self-check/h02-process-telemetry-trend-20260715.json/.md` 已证明 clean latency 与 Python allocation-tracked 两条通道保持分离。
2. [x] H-02 阶段耗时已纳入同样严格的长期趋势口径；只比较共有阶段，缺失阶段显式列出，结果保持 observation-only。
3. [x] 高 RSS 已建立 `H06-RSS-01` 专项观察任务；业务阈值仍待部署负责人批准，不以 Python allocation 阈值替代。
4. [x] Provider comparison 工件已补保留期和清理入口：`cleanup-provider-comparison-artifacts` 默认 dry-run，只匹配 self-check 生成的 `provider-comparison.<profile>.json/.md`；审核清单后才可显式 `--execute`。回滚、脱敏和其他工件清理继续按现有口径演练。
5. [x] P7-T03 质量指标已系统化：完成态 `document_quality` 事件、固定 gate/flag 计数和 quality/coverage/embedding/provider warning 摘要均已接入 `/events` 与 `/prometheus`；report-only 语义保持不变。

P7 清理入口回归：定向安全/CLI/配置测试 `116 passed`，P1 契约 `8/8`、`24/24 payload` 通过；对现有 `var/self-check` 的 dry-run 工件 `provider-comparison-cleanup-dry-run-20260715.json` 为 `candidates=0 / removed=0`。随后完整 fast 门禁 `var/self-check/p7-provider-cleanup-fast-20260715/self-check.json` 为 `ok`：单测 `583 passed`、`5 skipped`，3/3 回归基线和 6/6 Provider run 均通过，`identity_drift=0`、`admission_update=0`。

## 7. 验证命令

所有命令在工作区根目录执行。

### 7.1 开始前

```powershell
Set-Location 'D:\个人文件\个人开发\解析管理中台'
git status --short
Get-FileHash -Algorithm SHA256 'D:\app\uploads\36d65cd6b61346e28e97dbaf829646de.pdf'
```

### 7.2 定向测试

```powershell
& '.\.venv\Scripts\python.exe' -m pytest `
  tests/test_self_check.py `
  tests/test_provider_comparison_report.py `
  tests/test_parse_perf_baseline.py `
  tests/test_optimization_result_audit.py `
  tests/test_quality.py -q
```

### 7.3 当前完整 fast 门禁

```powershell
& '.\.venv\Scripts\python.exe' -m parsecore.cli self-check `
  --profile fast `
  --out var/self-check/optimization-next-fast.json
```

该命令必须包含并通过 Provider comparison，不得带 `--skip-provider-comparison`。如需稳定性复测，连续运行三次并保留每份 JSON/Provider 工件。

### 7.5 纯延迟复测

```powershell
& '.\.venv\Scripts\python.exe' tools/parse_perf_baseline.py `
  --config parsecore.toml `
  --sample-dir 'D:\app\uploads' `
  --sample 'D:\app\uploads\36d65cd6b61346e28e97dbaf829646de.pdf' `
  --no-track-python-memory `
  --cache-mode ocr_warm `
  --runs 5 `
  --warmup-runs 1 `
  --stability-policy var/regression/performance-stability.297-page.json `
  --enforce-stability-gate `
  --out-json var/self-check/optimization-next-latency-stability.json `
  --out-md var/self-check/optimization-next-latency-stability.md
```

### 7.6 内存追踪复测

```powershell
& '.\.venv\Scripts\python.exe' tools/parse_perf_baseline.py `
  --config parsecore.toml `
  --sample-dir 'D:\app\uploads' `
  --sample 'D:\app\uploads\36d65cd6b61346e28e97dbaf829646de.pdf' `
  --track-python-memory `
  --cache-mode ocr_warm `
  --runs 3 `
  --warmup-runs 1 `
  --stability-policy var/regression/performance-stability.297-page.json `
  --enforce-stability-gate `
  --out-json var/self-check/optimization-next-memory-stability.json `
  --out-md var/self-check/optimization-next-memory-stability.md
```

### 7.7 最终回归

```powershell
& '.\.venv\Scripts\python.exe' -m pytest -q
& '.\.venv\Scripts\python.exe' -m parsecore.cli p1-contract-acceptance `
  --out var/self-check/optimization-next-p1-contract.json
```

审计聚合器参数较多，先执行：

```powershell
& '.\.venv\Scripts\python.exe' tools/optimization_result_audit.py --help
```

然后将新的 tracked/latency 工件和当前 self-check/P1 工件全部显式传入，不得复用旧 current 工件冒充新结果。

## 8. 下一阶段 Definition of Done

H-01/H-02/H-03/H-04 均已满足完成口径。H-03 production RAG 的完成条件与结果如下：

1. [x] 用户已选择阿里 Qwen 远程 profile，并提供 endpoint、模型名与临时凭证注入方式；密钥不得写入配置、代码或工件。
2. [x] `index_manifest.embedded_unit_count` 与 chunk/unit 实际 embedding 计数的语义已核实并有测试覆盖。
3. [x] parser-only 与 production RAG profile 的告警、路由和验收口径已分开；未全局抑制 `rag_chunks_not_embedded`。
4. [x] 选择的 production profile 已在固定业务样本页段实测 `embedded_chunk_ratio=1.0`、hit@3=1.0、MRR@3=1.0；项目验收人已批准查询 Gold 与预算。
5. [x] 选定 profile 已证明 `hybrid+rerank` 且命中同时保留初检分/排序分；网关不可用时维持初检顺序并成功返回已有回归覆盖。典型业务查询的排序预算仍归入第 4 项。
6. [x] 默认 `pdf-text` 路由未被未经批准的候选替换；rerank 接入后的完整 fast 门禁继续通过，H-01/H-02 的 fast/性能策略未回归。
7. [x] 新的机器审计 JSON 和中文 Markdown 已落盘，包含 RSS、高内存、Docker 镜像网络等未关闭观察项。

## 9. 可复制到新对话的启动指令

```text
请继续开发“解析管理中台”的性能与可靠性优化。

工作区：D:\个人文件\个人开发\解析管理中台

开始前完整阅读：
1. docs/optimization-development-handoff-20260715.md
2. var/self-check/optimization-h02-result-audit.md
3. var/self-check/optimization-h02-result-audit.json

先执行 git status --short，保护现有约 77 项未提交 P0/P1/审计改动，不得 reset、clean 或覆盖 D:\app\uploads。

H-01/H-02/H-03/H-04、H-06 与 P7-T01 至 P7-T10 的工程项已完成，先不要重复修改 fast suite、性能阈值、审计占位项、RAG Gold、Provider 失败枚举、私有目录规则或质量指标口径。H-06 当前为 `passed_with_observation`：高竞争主机上的 clean-latency P50 `32.340s` 未通过原预算，必须保留失败工件并在受控主机复测，禁止刷新或放宽基线。H-03 正式工件为 `h03-aliyun-safran-pages-204-206-approved-20260715.json`，阿里 profile 必须保持实测 `batch_size=5`；16 只用于 P7 故障复演，会导致 HTTP 400 和部分向量化。后续按真实灰度数据持续抓取趋势，并单独处理候选 Provider 许可证/商业准入，不要扩张成问答平台。

保持 `tracemalloc` 内存通道与 `--no-track-python-memory` 纯延迟通道严格分离。正式性能复测使用 `--cache-mode ocr_warm` 和 `var/regression/performance-stability.297-page.json`；不得把 all-cache-disabled 冷态或整文档 parse cache hit 混入 SLA。

阿里网关密钥只能临时注入当前进程环境，绝不写入配置、代码、日志或交接工件。隔离 pgvector 容器为 `parsecore-h03-aliyun-parsecore-postgres-1`，宿主端口 `55433`；不要占用或修改其他项目数据库。若缺少典型业务样本、候选 Provider 许可证或独立人工 Gold，标记为外部阻断并继续推进可独立完成的工程工作，不得自行批准。
```
