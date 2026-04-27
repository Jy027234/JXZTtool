# jobcard 剥离评估

## 目的

当前方向已从“将 ParseCore 接入 jobcard”转为“不再落入 jobcard 宿主”。

这份文档用于回答两件事：

1. 之前的接线对 jobcard 产生了哪些影响。
2. 现在从 jobcard 剥离时，哪些代码可以直接删，哪些不能误删。

说明：本评估基于当前仓库内保留的接线代码、归档文档和测试证据，不直接读取 jobcard 仓库源码。因此下面的 jobcard 文件级位置是“按职责推断的拆除面”，不是对 jobcard 当前工作区的逐文件扫描结果。

## 已确认的接线影响

从当前仓库可确认，jobcard 方向上的改动主要落在 5 个面：

1. 宿主内嵌 ParseCore 子应用。
   - 证据：ParseCore 提供了 `mount_into_fastapi(...)`，默认挂载前缀是 `/internal/parsecore`，见 [src/parsecore/jobcard.py](src/parsecore/jobcard.py#L14)；宿主接线文档也明确要求把 ParseCore 子应用挂到该内部前缀，见 [archive/jobcard-host/docs/jobcard-integration.md](archive/jobcard-host/docs/jobcard-integration.md#L15) 和 [archive/jobcard-host/docs/jobcard-integration.md](archive/jobcard-host/docs/jobcard-integration.md#L108)。

2. jobcard 的两个解析入口被切到 ParseCore。
   - 证据：计划文档明确写了普通文档库和管理文库解析入口都切到 ParseCore，见 [docs/implementation-plan.md](docs/implementation-plan.md#L18-L20)；宿主接线说明也写明 `/documents/{id}/parse` 与 `/mgmt-documents/{id}/parse` 已切到 ParseCore，见 [docs/implementation-plan.md](docs/implementation-plan.md#L101)。

3. ParseCore 结果会回填到 jobcard 文档记录。
   - 证据：当前补丁结构会写 `parseStatus`、`parseError`、`parsedTextContent` 和 `parsecore` 字段，见 [src/parsecore/jobcard.py](src/parsecore/jobcard.py#L22-L41) 和 [src/parsecore/jobcard.py](src/parsecore/jobcard.py#L44-L52)；宿主接线文档也明确写了结果会回填到 `parsedTextContent`、`parseStatus` 和 `parsecore` 字段，见 [archive/jobcard-host/docs/jobcard-integration.md](archive/jobcard-host/docs/jobcard-integration.md#L87)。

4. jobcard 保留了“旧解析逻辑回退”与双跑比较资产。
   - 证据：宿主接线文档说明旧后台解析逻辑仍作为 ParseCore bridge 不可用时的回退路径，见 [archive/jobcard-host/docs/jobcard-integration.md](archive/jobcard-host/docs/jobcard-integration.md#L66) 和 [archive/jobcard-host/docs/jobcard-integration.md](archive/jobcard-host/docs/jobcard-integration.md#L88)。双跑资产则包括 `parsecore_compare.py`、历史报告、store-backed/direct-file 流程和辅助脚本，见 [docs/implementation-plan.md](docs/implementation-plan.md#L23-L24)、[archive/jobcard-dual-run/README.md](archive/jobcard-dual-run/README.md#L3-L18) 和 [archive/jobcard-dual-run/docs/jobcard-dual-run-record.2026-04-26.md](archive/jobcard-dual-run/docs/jobcard-dual-run-record.2026-04-26.md#L18)。

5. jobcard 方向上还引入了围绕 live store 和上传目录的辅助流程。
   - 证据：双跑 runbook 明确要求在 store-backed compare 时设置 `JOB_CARD_UPLOAD_DIR`，并提供 seed / native-upload 辅助脚本，见 [archive/jobcard-dual-run/docs/jobcard-dual-run-runbook.md](archive/jobcard-dual-run/docs/jobcard-dual-run-runbook.md#L37-L42)。

## 对 jobcard 的实际影响分级

### A. 高确定性、可直接删除的 ParseCore 接线层

这些内容如果 jobcard 后续不再承载 ParseCore，通常可以直接移除：

1. ParseCore 子应用挂载代码。
   - 典型形态：`from parsecore.jobcard import mount_into_fastapi` 以及 `mount_into_fastapi(app, ..., prefix="/internal/parsecore")`。
   - 删除原因：这是纯宿主接线代码，不服务 jobcard 自身业务。

2. 把 `/documents/{id}/parse` 与 `/mgmt-documents/{id}/parse` 改为优先走 ParseCore 的桥接逻辑。
   - 典型形态：路由、service 或 background task 中新增的 ParseCore 调用分支。
   - 删除原因：这是本轮方向变化后最核心的宿主耦合点。

3. ParseCore bridge 不可用时的 fallback 包装层。
   - 删除原因：既然不再接入 ParseCore，这层“先走 ParseCore，再决定是否回退”的控制流本身就没有价值。

4. 写回 `parsecore` 扩展字段的逻辑。
   - 删除原因：`parsecore` 字段是接 ParseCore 时新增的宿主侧调试/对照载荷，停止接入后应停止继续写入。

5. ParseCore 专用的双跑和 compare 工具链。
   - 典型形态：`parsecore_compare.py`、双跑报告生成、历史 compare 数据文件、seed live store 样本脚本、原生上传补样脚本。
   - 删除原因：这些资产只服务“旧解析 vs ParseCore”对照，不再是 jobcard 主线能力。

6. 仅为双跑存在的环境变量和运行说明。
   - 当前已确认的一项是 `JOB_CARD_UPLOAD_DIR`，它是 store-backed compare 的前置条件，见 [archive/jobcard-host/docs/jobcard-replacement-checklist.md](archive/jobcard-host/docs/jobcard-replacement-checklist.md#L52) 和 [archive/jobcard-dual-run/docs/jobcard-dual-run-record.2026-04-26.md](archive/jobcard-dual-run/docs/jobcard-dual-run-record.2026-04-26.md#L84)。
   - 删除原因：如果 compare 路径一并退场，这类开关也应一并清掉。

### B. 不能按“ParseCore 专用”直接删除的宿主通用部分

这些点虽然是在接线过程中出现，但不应未经确认就一并删掉：

1. jobcard 自己原有的解析结果字段。
   - `parseStatus`、`parseError`、`parsedTextContent` 在当前证据里被 ParseCore 补丁写入，见 [src/parsecore/jobcard.py](src/parsecore/jobcard.py#L27-L40) 和 [src/parsecore/jobcard.py](src/parsecore/jobcard.py#L46-L51)。
   - 但它们未必是 ParseCore 专属字段，很可能本来就是 jobcard 的文档解析展示字段。剥离时应删除“由 ParseCore 写入这些字段的代码”，而不是先删除字段本身。

2. jobcard JSON store 的死锁修复。
   - 文档明确说明这是在接线过程中发现并修复的宿主问题，见 [docs/implementation-plan.md](docs/implementation-plan.md#L22) 和 [archive/jobcard-host/docs/jobcard-integration.md](archive/jobcard-host/docs/jobcard-integration.md#L68)。
   - 这类修复如果已经落在 jobcard 自身存储代码里，应保留。因为它修的是宿主通用缺陷，不是 ParseCore 专用逻辑。

3. 与宿主上传、文档查询、前端显示相关的原有业务链路。
   - 双跑阶段只是“保持上传接口不变、把后台解析入口切到 ParseCore”，见 [archive/jobcard-host/docs/jobcard-integration.md](archive/jobcard-host/docs/jobcard-integration.md#L11-L18)。
   - 因此上传、文档详情、原有展示契约通常仍应保留，只需要把其中新增的 ParseCore 分支拆掉。

### C. 对 jobcard 数据层的残留影响

即使把代码删掉，也要考虑历史数据残留：

1. 文档记录里可能已经写入过 `parsecore` 扩展字段。
2. 旧双跑报告可能仍在 jobcard 的 `backend/data/` 目录下留存，证据见 [archive/jobcard-dual-run/docs/jobcard-dual-run-record.2026-04-26.md](archive/jobcard-dual-run/docs/jobcard-dual-run-record.2026-04-26.md#L51-L60)。
3. live store 中可能存在为双跑补入的 seed 样本或占位样本，证据见 [archive/jobcard-dual-run/docs/jobcard-dual-run-record.2026-04-26.md](archive/jobcard-dual-run/docs/jobcard-dual-run-record.2026-04-26.md#L71-L76)。

这些不一定要求立刻物理删除，但至少要避免让业务代码继续依赖它们。

## 建议的 jobcard 拆除顺序

### 第 1 步：先停用接线，不先删历史数据

先在 jobcard 中做最小停用：

1. 去掉 ParseCore 子应用挂载。
2. 把 `/documents/{id}/parse` 与 `/mgmt-documents/{id}/parse` 恢复到 jobcard 自己的解析实现。
3. 去掉“先走 ParseCore，再 fallback”的桥接逻辑。
4. 停止继续写入 `parsecore` 字段。

这样做的目标是先把线上控制流切干净，再处理遗留资产。

### 第 2 步：删除比较和验证工具链

在确认 jobcard 已经完全不走 ParseCore 后，再删：

1. `parsecore_compare.py` 及其调用入口。
2. 双跑报告生成代码。
3. seed live store 样本脚本。
4. 原生上传补样脚本。
5. `JOB_CARD_UPLOAD_DIR` 这类只服务双跑的配置说明。

### 第 3 步：清理依赖与配置

1. 删除 jobcard 中对 parsecore 包的依赖声明。
2. 删除 jobcard 中为 ParseCore 准备的配置文件、Docker service、环境变量注入。
3. 删除 jobcard 中与 `/internal/parsecore`、`/v1/parse/*` 相关的代理、路由或网关配置。

### 第 4 步：最后再决定是否清理历史数据

历史 `parsecore` 字段、双跑报告文件、补样记录可以按审计需求决定：

1. 若只需要业务回退，不需要立刻物理清理，可先保留只读。
2. 若要彻底去痕，再补一次离线数据清理脚本，把 `parsecore` 字段和双跑样本一起归档或删除。

## jobcard 拆除时的验证重点

从 jobcard 拆掉 ParseCore 后，至少检查这几项：

1. 普通文档库解析入口仍能跑通。
2. 管理文库解析入口仍能跑通。
3. 文档详情页或 API 读取不依赖 `parsecore` 字段存在。
4. 原前端展示如果消费 `parsedTextContent`，要确认 legacy 解析仍会继续写这个字段；如果不会，就要同步调整 UI。
5. JSON store / 文档保存链路不因去掉 ParseCore 分支而回退到旧死锁实现。
6. CI / 本地脚本里没有残留 `parsecore_compare`、`/internal/parsecore`、`JOB_CARD_UPLOAD_DIR` 相关调用。

## 建议保留的最低审计材料

即使最终从 jobcard 彻底剥离，仍建议保留以下文档归档：

1. [archive/jobcard-host/docs/jobcard-integration.md](archive/jobcard-host/docs/jobcard-integration.md)
2. [archive/jobcard-host/docs/jobcard-replacement-checklist.md](archive/jobcard-host/docs/jobcard-replacement-checklist.md)
3. [archive/jobcard-dual-run/docs/jobcard-dual-run-record.2026-04-26.md](archive/jobcard-dual-run/docs/jobcard-dual-run-record.2026-04-26.md)

原因不是为了继续推进接入，而是为了以后有人追问“jobcard 当时改过什么、哪些数据是双跑遗留”时，有证据可回溯。

## 当前结论

如果目标是“把 ParseCore 对 jobcard 的影响清掉，但不误伤 jobcard 自身解析和存储能力”，最合理的判断是：

1. 可以直接删的是接线层、桥接层、compare 层、补样层和 ParseCore 专用配置。
2. 不能直接删的是 jobcard 原有解析结果字段语义，以及接线过程中顺手修掉的宿主通用 bug。
3. 应优先拆控制流，再拆工具和依赖，最后再清历史数据。

这会比“一次性把所有看起来像 ParseCore 的东西都删掉”更安全。