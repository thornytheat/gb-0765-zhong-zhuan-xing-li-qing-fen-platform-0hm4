# 深度登记 中转行李清分台

中转主管逐牌核对航段窗口与动作末态。跨日同航班号不能替代航段实例。

## 功能模块

| 模块 | 使用者动作 | 入口 | 核心文件 | 测试 |
|---|---|---|---|---|
| M1 扫描原件采信 | 查看射频重报和隔离行 | `/desk/scans` `/api/stage/scans` | `app/modules/scans/service.py` | `tests/test_scans.py` |
| M2 航段候选关联 | 核对候选段和提示 | `/desk/legs` `/api/stage/legs` | `app/modules/legs/service.py` | `tests/test_legs.py` |
| M3 扫件有序回放 | 按生效时刻核对动作 | `/desk/replay` `/api/stage/replay` | `app/modules/replay/service.py` | `tests/test_replay.py` |
| M4 错载与漏扫归因 | 按牌追踪待证与错装 | `/desk/exceptions` `/api/stage/exceptions` | `app/modules/exceptions/service.py` | `tests/test_exceptions.py` |
| M5 行李去向对账 | 勾稽计划段与去向件数 | `/` `/api/stage/ledger` | `app/modules/ledger/service.py` | `tests/test_ledger.py` |
| M6 批次留档复跑 | 保存并复算原件 | `/desk/archive` `POST /api/archive` | `app/modules/archive/service.py` `app/modules/archive/store.py` | `tests/test_archive.py` |
| M7 联程证据签发 | 验摘要并下载联程证据 | `/desk/evidence` `/api/evidence/<run>/export` | `app/modules/evidence/bundle.py` | `tests/test_evidence.py` |
| M8 错装处置单交接 | 班组交接、回放关单与归因 | `/desk/exceptions` `/api/runs/<run>/dispositions` | `app/modules/handovers/service.py` `app/modules/handovers/store.py` | `tests/test_handovers.py` |

## 陷阱登记

| TRAP | 现象 | 朴素实现 | 本项目处理 | 处理位置 | 测试 |
|---|---|---|---|---|---|
| TRAP-02 | 脏扫描首列 BOM | 扫描号丢失 | 解码时去首 BOM | `app/modules/scans/csvio.py` | `tests/test_scans.py::test_bom` |
| TRAP-04 | 脏扫描第 16 行重复表头 | 误计为扫描 | 计独立分段 | `app/modules/scans/csvio.py` | `tests/test_scans.py::test_sections` |
| TRAP-05 | E17 多余分隔列 | 宽度误收 | 原行隔离 | `app/modules/scans/csvio.py` | `tests/test_scans.py::test_bad_width` |
| TRAP-07 | E18 时间 N/A | 默认零时 | 空值隔离 | `app/modules/scans/normalize.py` | `tests/test_scans.py::test_sentinel` |
| TRAP-08 | 清单 000101 | 强转丢零 | 字符串关联 | `app/modules/legs/contracts.py` | `tests/test_legs.py::test_tag_preserved` |
| TRAP-11 | E03 特殊偏移重复 | 误认成不同事件 | UTC 后比较 | `app/modules/scans/normalize.py` | `tests/test_scans.py::test_equivalent_offset_duplicate` |
| TRAP-13 | E02 迟到撤下 | 收件顺序末态错误 | 标迟到后按生效回放 | `app/modules/replay/radio.py` | `tests/test_replay.py::test_late_offload_before_reload` |
| TRAP-14 | E12 同键相反动作 | 后写覆盖 | 全组隔离 | `app/modules/scans/service.py` | `tests/test_scans.py::test_conflicting_id` |
| TRAP-23 | E04 恰逢跨日换窗 | 同时匹配两段 | 左闭右开 | `app/modules/legs/service.py` | `tests/test_legs.py::test_boundary_handoff` |
| TRAP-24 | 原班新班同时提交交接 | 两组都去撤货 | `BEGIN IMMEDIATE` 串行事务加责任组条件更新，同单仅一方成功 | `app/modules/handovers/store.py` | `tests/test_handovers.py::test_original_and_new_crew_submit_concurrently_only_one_wins` |
| TRAP-25 | 交接请求超时重发 | 重复交接、双责任组 | 幂等键与交接同事务登记，同键重放首次结局 | `app/modules/handovers/store.py` | `tests/test_handovers.py::test_same_key_in_parallel_never_double_handoff` |
| TRAP-26 | 撤下未确认即关单 | 漏管错装件 | 回放末态须为 OFFLOAD 且时间线有撤下采信记录 | `app/modules/handovers/policy.py` | `tests/test_handovers.py::test_close_blocked_until_replay_confirms_offload` |

## 处置单交接规则

- 处置单按归档的错装记录（牌+错装航段）幂等生成，初始责任班组为原班，单号由 `归档号+牌+航段` 派生。
- 交接请求必须带 `idempotency_key`、`from_crew`、`to_crew`；`from_crew` 不是当前责任班组返回 `OWNER_MISMATCH`，已关单返回 `ORDER_CLOSED`，同键不同载荷返回 `REQUEST_CONFLICT`。
- 关单须经回放确认撤下，可指定 `confirm_run` 引用含撤下扫件的另一归档；关单后对账的待处置/已处置件数随之更新，归档结果原文不被改写。
- 归因页可查每张单的当前责任班组与 OPEN/HANDOVER/CLOSE 事件经过，接口为 `GET /api/dispositions/<order>/attribution`。

## 对账规则

脏样例清单 12 = 3 已装 + 8 待证 + 1 明确未装，错装 2 件另列。扫描原件记录 24 = 17 采信 + 4 隔离 + 1 重报 + 1 重复表头 + 1 空行。

## 已知范围

文件内容通过限长 JSON 提交。样例为机场单站观测，时间窗由机场清单提供。扫描尾播放已采信记录的收件流。
