# P0-07 交付报告：宿主身份与租户边界

日期：2026-09-20
待办来源：[docs/parsecore-reliability-mineru-todo-20260920.md](parsecore-reliability-mineru-todo-20260920.md) P0-07
状态：已实施；真实宿主注入协议联调见第 6 节

## 1. 这一项解决什么

核查文档记录了这条缺口：**共享 API key 中间件没有把身份绑定到租户**。源码核查发现这不止是"边界不清"，而是可越界：

- 23 处 `tenant_id` 直接取自 `?tenant_id=` / 表单 / JSON body，调用方可以自选租户；
- `get_job` / `cancel_job` / `get_export_job` / `download_export_file` 只按 id 取资源，完全不校验归属。知道另一个租户的 `job_id` 或 `export_id` 就能读取内容、取消任务、下载导出包；
- `/v1/parse/events` 在无租户参数时返回全部租户的事件。

本轮把"谁决定租户"和"谁能读这个资源"两件事分开定义并分别实现。

## 2. 两种模式

| 模式 | 谁认证用户 | 谁决定租户 | 请求参数的作用 |
| --- | --- | --- | --- |
| `trusted`（默认） | 调用宿主 | 请求参数或宿主身份头 | 可以直接指定租户（既有语义） |
| `enforced` | 宿主 | 仅宿主注入的身份头 | 只能**复述**，不一致即 `403`；缺失身份即 `403` |

`trusted` 是刻意的默认值：现有嵌入式部署语义不变，边界以"记录谁决定了租户"的方式明确下来，而不是靠假设。对外多租户开放前必须切到 `enforced`，这一点写在配置文档与 `TENANT_BOUNDARY_POLICY` 里。

```toml
[runtime]
tenant_scope = "enforced"                      # trusted | enforced
tenant_header = "x-parsecore-tenant"
```

## 3. 归属决策点

新增 `src/parsecore/tenant_scope.py`，所有来源（身份头、query、表单、JSON body）都汇聚到 `resolve_tenant()` 一个函数，两种模式不可能各自漂移：

| 情形 | 结果 |
| --- | --- |
| 身份头 = `tenant-a`，参数 = `tenant-b` | `403 tenant_mismatch`（两种模式都拒绝） |
| 身份头 = `tenant-a`，参数空或相同 | 归属 `tenant-a`，来源 `host_authenticated` |
| 无身份头，参数 = `tenant-a`，`trusted` | 归属 `tenant-a`，来源 `request_parameter` |
| 无身份头，参数 = `tenant-a`，`enforced` | `403 tenant_identity_required` |
| 无身份头、无参数，`trusted` | 归属 `default`，来源 `default` |
| 无身份头、无参数，`enforced` | `403 tenant_identity_required` |

`TenantScopeMiddleware` 在鉴权之后、任何 handler 之前执行归属，因此不存在"某个 handler 忘了校验"的路径；无法归属的请求在任何 handler 之前就被拒绝。响应头 `x-parsecore-tenant` 与 `x-parsecore-tenant-source` 记录本次归属结论，供运维审计。

## 4. 资源授权

| 资源 | 校验方式 | 越界响应 |
| --- | --- | --- |
| job 查询 / 取消 | 持久化 `job.tenant_id` 与归属租户比对 | `404 job_not_found` |
| 导出包查询 / 下载 | manifest 记录的 `tenant_id` 与归属租户比对 | `404 export_not_found` |
| 文档 / records / search / quality / parts | 运行时按租户过滤的读路径（`GET /documents/{id}` 找不到即 404） | `404 document_not_found` |
| 发布指针 | 按 `(tenant, doc)` 主键读取 | `not_ready`（不泄露存在性） |
| events / metrics / dashboard | 请求声明租户时按租户过滤 | 列表为空 |
| jobs 列表 / 配额用量 | 请求声明租户时按租户过滤 | 列表为空 |

越界一律返回与"资源不存在"相同的响应，避免通过状态码探测其他租户的 id 是否存在。

**兼容性细节**：完全没有租户声明的请求（嵌入式宿主拿着已学到的 id 直接查询）保持既有的文档维度行为，不隐式收窄到 `default`；但一旦请求声明了租户，归属即被强制执行。这条规则写在 `_request_claims_tenant()` 与 `explicit_request_tenant_id()` 里并被测试覆盖——否则"兼容"会重新变成漏洞。

## 5. 验证

```text
.\.venv\Scripts\python.exe -m unittest tests.test_tenant_boundary -q
Ran 23 tests / OK

.\.venv\Scripts\python.exe -m unittest discover -s tests -t . -q
Ran 833 tests / OK (skipped=6)
```

跨租户探针（enforced 模式，两个租户各一份已解析文档）：

| 探针 | 结果 |
| --- | --- |
| 无身份头访问任意端点 | `403 tenant_identity_required` |
| 身份 `tenant-a` + `?tenant_id=tenant-b` | `403 tenant_mismatch` |
| tenant-b 读 tenant-a 的 job / 取消其 job | `404`，且任务状态未被改变 |
| tenant-b 读 tenant-a 的 document / reader / coverage / quality / records / providers / parts / search / structure-search / tasks / exports | 全部 `404`（publication 返回 `not_ready`） |
| tenant-b 触发 tenant-a 的 reparse / rechunk / re-embed / parts plan | 全部 `404` |
| tenant-b 读 tenant-a 的导出包与下载 | `404` |
| job 列表 / events | 各租户只看到自己的 |
| 归属来源可审计 | 响应头正确区分 `host_authenticated` / `request_parameter` / `default` |
| trusted 模式参数仍可选租户 | 通过（语义未变） |
| 无声明请求保持文档维度视图 | 通过（jobs 列表、job 查询、配额用量） |
| 声明租户后不能读他人 job | 通过 |

## 6. 未完成

1. **真实宿主注入协议联调**：本轮定义了身份头约定并实现，但宿主侧实际如何注入（网关、JWT claim、反向代理）需在部署时确认。
2. **批量端点与上传桥**：`/parse/batch`、`/parse/uploads` 的租户来自表单/body，已走同一决策点；但上传桥的独立 API key 与租户的绑定关系仍由宿主负责，本项未建立 key→tenant 映射表。
3. **审计日志表**：目前归属结论记录在响应头与事件流中，尚未落库为可查询的审计记录。

## 7. 回滚

- 代码回滚：`git revert` 本次提交。
- 运行期回滚：`runtime.tenant_scope = "trusted"` 即回到"参数即租户"的既有语义；但**资源归属校验仍在**（job/export 越界仍 404），因为它修的是越权读取而不是模式选择。
- 若宿主依赖"任意 job_id 可读"的旧行为，必须先修宿主，不建议回滚归属校验。
