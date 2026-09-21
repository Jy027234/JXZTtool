# 待办 4（首批）交付报告：OCR 按需处理与结果复用

日期：2026-09-21
待办来源：[docs/parsecore-reliability-mineru-todo-20260920.md](parsecore-reliability-mineru-todo-20260920.md) 待办 4
状态：决策门与复用行为已用测试与实测固定；区域级增强与合并见第 5 节

## 1. 这一项现在是什么状态

待办 4 的大头（缓存键含真实 provider 与影响结果的配置、页缓存容量上限、原子替换、refiner 状态按请求隔离）在前几轮已经落地。本轮补的是**"按需"这一半的证据与边界**，并把复用的实际收益量出来：

| 问题 | 现状 |
| --- | --- |
| 正常文字层是否会被重复 OCR | 不会。文字层完好的页面在决策门就返回，**既不初始化 OCR 引擎也不渲染页面**（本轮新增测试固定） |
| 哪些页面才会走 OCR | 文字层有 CID/乱码特征、页面无文字但含图像、或请求显式 `enable_ocr=true` |
| 同一输入重复处理的耗时 | 同一 parser 实例内重复解析：**24.1 s → 0.0 s**（解析缓存命中，块内容一致 2221 块） |
| 只改导出/分块配置是否会重复调用 OCR/模型 | 解析缓存键只含租户、来源文件指纹（sha256 逐字节）、请求选项；解析阶段与分块/导出阶段本就分离，改分块不会触发重新解析 |
| 缓存损坏能否恢复 | 页缓存把无法解析的 JSON 当作未命中（不抛错），下次写入用临时文件 + `os.replace` 覆盖 |
| 改变识别配置能否正确失效 | OCR 页缓存键含 `provider_tag` 与 `options_repr`（`ocr_cache_identity()`），换 provider/选项即换键 |

## 2. 交付物

| 文件 | 变更 |
| --- | --- |
| [tests/test_pdf_parser_options.py](../tests/test_pdf_parser_options.py) | 新增 2 项：文字层完好时 OCR 引擎与抽取函数都不被调用（`engine.assert_not_called()` / `extract.assert_not_called()`）；CID 乱码页才会被送去 OCR 且带 `cid_dense` 原因 |
| [tests/test_large_pdf_stress.py](../tests/test_large_pdf_stress.py) | 并行解析用例失败时输出 `errors` 内容，便于下次定位（本轮观测到 1 次偶发失败，见第 4 节） |

## 3. 实测

```text
.\.venv\Scripts\python.exe -m unittest tests.test_pdf_parser_options tests.test_large_pdf_stress
Ran 53 tests / OK
```

重复处理（样本 `36d65cd6b61346e28e97dbaf829646de.pdf`，297 页，仓库 `parsecore.toml`，`parse_cache = true`）：

| 场景 | 耗时 | 块数 |
| --- | --- | --- |
| 第 1 次（缓存空） | 24.1 s | 2221 |
| 第 2 次（同一 parser 实例，解析缓存命中） | **0.0 s** | 2221 |
| 第 3/4 次（`parse_cache = false` 绕过解析缓存） | 26.2 s / 26.5 s | 2221 |

按需 OCR（同一样本、同一配置的门禁输出）：该文档 46 页被判定为需要 OCR 候选，但 `ocr_total_s = 0.000`、`crops = 0` —— 本机没有安装 OCR 引擎，所有候选页都在 `provider_unavailable` 上被拒绝，没有任何页面被渲染。这同时说明两件事：健康页面确实没有被送去 OCR（否则会先渲染），以及**本机无法验证 OCR 提速与缓存复用的真实收益**。

## 4. 本轮的判断与取舍

1. **没有实现"合并新旧文字"**。当前行为是：OCR 返回非空文本时替换该页文字，不做新旧文本合并。待办 4 要求"合并新旧文字时检查重复和冲突，不能仅按字数更多选结果"——在替换语义下这条要求今天不成立（没有合并，也就无所谓按字数选）。真正需要它的是**混合页**（同一页部分区域乱码、部分区域文字层正常），那需要区域级 OCR，见第 5 节。这里刻意没有先加一个只会空转的去重/择优兜底规则。
2. **尝试过、并已回退的一处改动**：把 `SQLiteJobStore._connect()` 的默认 5 s 锁等待提到 30 s（与 run store 一致）并启用 WAL，理由是并行执行 part 时可能撞锁。回退原因：该假设没有被复现（专项探针 8/8 与模块 6/6 均无 `database is locked`），而这个改动让 4 个队列/上传用例变慢并因此失败——那些用例的轮询循环是 20 次无间隔查询，对时序敏感。没有证据支撑的并发改动不应留下，因此回退到原状。
3. **偶发失败如实记录**：`tests/test_large_pdf_stress.py::test_build_report_can_execute_parts_with_bounded_parallelism` 在本轮全量运行中出现过 1 次失败（`status: failed`），单独运行与模块内重复运行 6 轮均通过。已让该断言在失败时打印 `errors`，下次出现即可定位是并发写入、抢占还是 part 顺序问题。`tests/test_asgi.py` 的 PDF 分片轮询此前也修过一次同类"无间隔轮询"竞态。

## 5. 未完成

1. **区域级 OCR**：目前是整页粒度。混合页（部分区域扫描/乱码、部分区域文字层正常）仍会整页替换，正常区域的文字会丢失或被重复读取。需要按区域（行框/图框）决定增强范围并合并，同时检查重复与冲突。
2. **定向调整**：方向、裁剪与局部清晰度按区域参数化未实现；当前只有整页 `ocr_render_resolution` 与整页旋转检测（OCR 引擎自身的 cls 分支）。
3. **OCR 缓存复用的实测**：本机无 OCR 引擎，无法给出"OCR 命中 vs 冷跑"的耗时对比；引擎可用后应补测。
4. **并发计数与额度**：refiner 已按请求隔离；OCR 侧仍依赖 provider 自身的并发控制，未做跨请求的并发上限实测。

## 6. 回滚

- 本轮只改了测试（决策门断言 + 失败信息），无生产代码改动，回滚即 `git revert` 本次提交。
- 生产侧的缓存与按需行为是既有实现，未改动。
