# jobcard 切流 readiness 2026-04-26

## 目标

这份文档用于回答一个更具体的问题：以 2026-04-26 的历史联调结果看，jobcard 是否已经具备把解析链路切到 ParseCore 的最低条件。

历史注记：这是一份归档的 readiness 判断记录。2026-04-29 起，主线已终止 jobcard 单一宿主切换计划；本文仅保留当时的判断、阻塞项和后续建议，不再作为当前主线执行指令。

结论先行：

- ParseCore 侧解析、落库、embedding 与检索链路已具备切流前验证能力。
- 历史 direct-file、store-backed 与宿主原生上传验证都已证明链路可用，详细记录已归档。
- 当时的判断是：可以继续做宿主小流量灰度验证；但还不能把旧链路退场，因为 `doc-main-wheel-r16` 仍暴露出宿主上传资产保全缺口，且原生样本池仍偏小。

## 当时的 readiness 结论

当时状态定义为：`基本就绪，可进入小流量灰度，暂不退场旧链路`。

可视为已经完成的前提：

1. `GET /health` 正常，`services.pdfplumber / python_docx / paddleocr = true`。
2. ParseCore live DOCX / PDF 基线已验证，包含 `chunk_embeddings` 与 `hybrid` 检索路径。
3. jobcard archived compare 已在 direct-file 模式下跑通 DOCX 与 PDF。
4. 第一条真实宿主 store-backed 样本 `doc-527d3fe173db` 已跑通，`text_similarity = 0.9580`。
5. 已通过 `archive/jobcard-dual-run/tools/seed_jobcard_live_store.py` 向 live store 补入两条 documents 样本 `doc-parsecore-docx-store-seed`、`doc-flight-ops-r2-store-seed` 与一条 `mgmt_documents` 样本 `mgmt-parsecore-docx-store-seed`，三条 store-backed compare 都已跑通。
6. 已通过 `archive/jobcard-dual-run/tools/upload_jobcard_native_sample.py` 走宿主原生上传接口生成 `doc-1f24155682db`，并完成 `documents` 路径的 store-backed compare，`text_similarity = 0.8890`。
7. 已通过同一原生上传工具生成 `mdoc-cb4a937bffe8`，并完成 `mgmt_documents` 路径的 store-backed compare，`text_similarity = 0.8212`。

当时仍然阻止切流的条件：

1. `doc-main-wheel-r16` 仍缺少上传文件 `sample-main-wheel-r16.pdf`，说明宿主上传资产保全策略还不稳定。
2. 虽然两条宿主文库路径都已有原生上传样本，但当前原生样本池仍偏小，暂不建议据此直接退场旧链路。

## 最低可交付门槛

| 项目 | 当时状态 | 证据 | 是否达标 |
| --- | --- | --- | --- |
| ParseCore 健康检查 | `status = ok`，能力矩阵正常 | [../../jobcard-dual-run/docs/jobcard-dual-run-record.2026-04-26.md](../../jobcard-dual-run/docs/jobcard-dual-run-record.2026-04-26.md) | 是 |
| ParseCore live DOCX 基线 | `blocks = 301`，`chunks = 301`，`retrieval_mode = hybrid` | [../../jobcard-dual-run/docs/jobcard-dual-run-record.2026-04-26.md](../../jobcard-dual-run/docs/jobcard-dual-run-record.2026-04-26.md) | 是 |
| ParseCore live PDF 基线 | `total_pages = 45`，`chunk_embeddings = 46` | [../../jobcard-dual-run/docs/jobcard-dual-run-record.2026-04-26.md](../../jobcard-dual-run/docs/jobcard-dual-run-record.2026-04-26.md) | 是 |
| direct-file DOCX 双跑 | 已执行，但块粒度差异大，需要宿主视角复验 | [../../jobcard-dual-run/docs/jobcard-dual-run-record.2026-04-26.md](../../jobcard-dual-run/docs/jobcard-dual-run-record.2026-04-26.md) | 部分达标 |
| direct-file PDF 双跑 | flight-ops 样本通过；`25-51-06.pdf` 仅作 CID 问题样本 | [../../jobcard-dual-run/docs/jobcard-dual-run-record.2026-04-26.md](../../jobcard-dual-run/docs/jobcard-dual-run-record.2026-04-26.md) | 是 |
| store-backed PDF 双跑 | `doc-527d3fe173db` 已通过 | [../../jobcard-dual-run/docs/jobcard-dual-run-record.2026-04-26.md](../../jobcard-dual-run/docs/jobcard-dual-run-record.2026-04-26.md) | 是 |
| store-backed DOCX 双跑 | `doc-parsecore-docx-store-seed` 已通过，`text_similarity = 0.8212` | [../../jobcard-dual-run/docs/jobcard-dual-run-record.2026-04-26.md](../../jobcard-dual-run/docs/jobcard-dual-run-record.2026-04-26.md) | 是 |
| 第二个 store-backed PDF 双跑 | `doc-flight-ops-r2-store-seed` 已通过，`text_similarity = 0.8890` | [../../jobcard-dual-run/docs/jobcard-dual-run-record.2026-04-26.md](../../jobcard-dual-run/docs/jobcard-dual-run-record.2026-04-26.md) | 是 |
| 原生上传 documents 路径 | `doc-1f24155682db` 已通过，`text_similarity = 0.8890` | [../../jobcard-dual-run/docs/jobcard-dual-run-record.2026-04-26.md](../../jobcard-dual-run/docs/jobcard-dual-run-record.2026-04-26.md) | 是 |
| 原生上传 mgmt_documents 路径 | `mdoc-cb4a937bffe8` 已通过，`text_similarity = 0.8212` | [../../jobcard-dual-run/docs/jobcard-dual-run-record.2026-04-26.md](../../jobcard-dual-run/docs/jobcard-dual-run-record.2026-04-26.md) | 是 |
| store-backed 样本池可持续复现 | 当时运行态 `documents = 5`、`mgmt_documents = 2`，其中含 2 条原生上传样本与 3 条 seed 样本；但仍有历史样本缺文件 | [../../jobcard-dual-run/docs/jobcard-dual-run-record.2026-04-26.md](../../jobcard-dual-run/docs/jobcard-dual-run-record.2026-04-26.md) | 部分达标 |

## 当前阻塞项

### 阻塞 1：宿主上传资产保全仍有缺口

- 已确认 `doc-main-wheel-r16` 的 archived compare probe 返回 `missing_files = 1`。
- 对应报告中 `file_path = null`，原因是 `document file not found in upload directory`。
- 进一步排查发现，这条记录本身来自 `jobcard/backend/store.py` 的占位 seed，`file.name = sample-main-wheel-r16.pdf` 且 `file.size = 0`，并不是当前机器上可追溯的一条真实上传资产。
- 同时，`jobcard/backend/data/store.document*.json` 中虽存在一条更真实的同族样本 `doc-3db0059c4e7f`（`C20195162` / `R16 TR32-7`），但其上传文件 `ea79cf24213f481080a26674690af96c.pdf` 也已不在 `D:\app\uploads`。
- 这说明当前问题在宿主上传资产，而不是 ParseCore 或 compare 脚本；当前机器上已经没有可直接恢复的主轮 CMM PDF 样本。

### 阻塞 2：宿主原生样本池仍偏小

- 当时运行态已经提升到 `documents = 5`、`mgmt_documents = 2`。
- `documents` 与 `mgmt_documents` 两条路径都已具备“宿主原生上传 -> store -> compare/回填”的首批证据。
- 但要把 readiness 继续上调到可退场旧链路，仍建议继续扩 1 到 2 轮原生上传样本，避免当前判断过度依赖单个新样本。

## 当时给出的切流判断

当时只建议做以下动作：

1. 继续保留旧链路，不允许直接切到单跑。
2. 把 `doc-527d3fe173db`、`doc-parsecore-docx-store-seed`、`doc-flight-ops-r2-store-seed`、`mgmt-parsecore-docx-store-seed`、`doc-1f24155682db`、`mdoc-cb4a937bffe8` 作为当时的宿主兼容性历史样本集合。
3. 当时默认门禁改为 ParseCore 自检：单测、回归基线、`GET /health` 和最小运行时 smoke。
4. 只有在需要复现宿主兼容性问题时，才回到归档目录里的双跑记录和辅助脚本。

当时不建议做以下动作：

1. 不允许宣称 jobcard 已具备“可直接退场旧链路”的全面切流条件。
2. 不允许仅凭当前 2 条原生上传样本就关闭宿主回填验证与灰度观察。
3. 不允许在宿主上传资产仍可能丢失的前提下把旧解析链路退场。

## 后续解锁条件

这是当时记录的后续解锁条件：满足以下任一组合后，才建议把 readiness 状态从“基本就绪，可进入小流量灰度，暂不退场旧链路”继续上调：

1. 恢复 `sample-main-wheel-r16.pdf`，并成功重跑 `doc-main-wheel-r16` 的 store-backed compare。
2. 对宿主上传目录建立稳定保全策略，避免 store 记录存在但上传文件缺失。
3. 至少再补 1 轮由宿主原生上传流程产生的 `documents` / `mgmt_documents` live 样本，并持续复验证明样本池可复现。
4. 若继续追 `C20195162` 这条主轮样本，必须改走两种路径之一：从老系统 `document_source_files` 导出真实 PDF，或重新通过宿主原生上传入口上传一份真实主轮 CMM PDF。

## 当时建议的执行顺序

1. 先执行 ParseCore 自检门禁，确认当前解析质量、回归指标和运行态健康。
2. 再修复宿主上传资产保全问题，避免 `store` 有记录但磁盘已无文件。
3. 对 `C20195162` 主轮样本，优先从老系统导出 `document_source_files` 中的真实 PDF，导不出来就直接重新走宿主原生上传。
4. 然后回到 [jobcard-replacement-checklist.md](jobcard-replacement-checklist.md) 按灰度替换清单逐项关闭；若要回看历史联调证据，则到 [../../jobcard-dual-run/README.md](../../jobcard-dual-run/README.md)。

## 关联文档

- 详细历史联调结果见 [../../jobcard-dual-run/docs/jobcard-dual-run-record.2026-04-26.md](../../jobcard-dual-run/docs/jobcard-dual-run-record.2026-04-26.md)
- 替换总清单见 [jobcard-replacement-checklist.md](jobcard-replacement-checklist.md)
- 历史双跑归档见 [../../jobcard-dual-run/README.md](../../jobcard-dual-run/README.md)