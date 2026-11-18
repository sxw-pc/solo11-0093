# 湖表提交验台（lakecommit）

一个纯本地、完全离线、确定性可复现的**文件级湖表提交冲突验证台**。它用合成表、合成 writer、合成文件清单和逻辑时钟，验证快照链、乐观并发判定、幂等重放、陈旧快照安全 rebase、冲突解释、SQLite 审计台账与 golden 回归。

> 这里验证的是提交协议语义，不是生产级 Iceberg / Delta / Hudi 实现，也不是元数据服务或查询引擎。

## 快速开始

要求 Python 3.10+，运行期依赖仅 PyYAML 与 Flask，测试依赖 pytest。

```bash
# 完整演示：加载 → 校验 → 提交判定 → 快照 → 台账 → JSON/Markdown/HTML
python -m lakecommit demo --workdir .lakecommit-demo

# 全量测试
pytest -q

# 只读本地复核台（仅回环地址）
python -m lakecommit serve --host 127.0.0.1 --port 8765 --workdir .lakecommit-demo
```

演示使用 `fixtures/scenarios/13_mixed_workload.yaml`，共 7 次尝试：4 个接受判定（其中 1 次幂等重放、3 个实际应用提交、2 个安全 rebase），3 个拒绝判定（陈旧分区冲突、未知 base、幂等载荷冲突）。演示结束后会自动比对 golden。

`serve` 只接受 `--host 127.0.0.1`、`localhost`、`::1`；其他地址会以输入错误退出。复核台只注册 GET/HEAD 路由，任何写方法返回 405。

## 命令与退出码

| 命令 | 用途 |
| --- | --- |
| `python -m lakecommit demo --workdir DIR` | 运行内置混合负载场景并校验 golden |
| `python -m lakecommit run --scenario FILE --workdir DIR` | 运行任一 YAML 场景 |
| `python -m lakecommit explain --workdir DIR --attempt ID` | 从 SQLite 台账输出单个尝试的判定依据 |
| `python -m lakecommit verify --workdir DIR` | 校验快照链、清单哈希、当前指针和文件路径唯一性 |
| `python -m lakecommit verify-golden --workdir DIR` | 将规范化报告与 `fixtures/expected/` 的 golden 对比 |
| `python -m lakecommit serve ...` | 启动只监听回环地址的只读复核台 |
| `python -m lakecommit linecount` | 检查总代码行数预算与单文件上限 |

退出码稳定为：

- `0`：执行成功；即使场景中包含被拒绝的提交，只要协议运行完成也返回 0。
- `2`：输入错误（文件缺失、JSON/YAML 解析失败、非回环监听地址等）。
- `3`：输入校验失败、快照状态完整性校验失败或代码预算超限。
- `4`：golden 缺失或不一致。
- `130`：用户中断；`1`：未预期的内部失败。

## 每次运行生成什么

```text
DIR/
├── report.json                  # 规范化、稳定排序、带指纹的结果
├── report.md                    # 人读 Markdown
├── report.html                  # 自包含 HTML（内联样式，无 CDN/JS 依赖）
├── run.json                     # 场景元数据（非稳定报告）
└── state/
    ├── current                  # 仅在快照完整写完后原子替换
    ├── ledger.sqlite3           # 审计台账、幂等缓存、快照审计
    └── snapshots/
        └── <snapshot_id>/
            ├── manifest.json
            └── commit.json
```

报告不包含工作目录绝对路径、系统时间、随机 ID 或无序集合遍历结果。`run_fingerprint` 是报告去掉自身指纹后的规范化 SHA-256。

## 四个核心对象

- **表规格**：分区列、文件字段和初始快照，来自 `fixtures/tables/lake_sales_orders.json`。
- **提交尝试**：writer、idempotency key、base snapshot、逻辑时间和文件级操作，来自 JSONL。
- **快照**：不可变文件清单 + parent + sequence + 逻辑时间 + 提交摘要。
- **判定**：accepted/rejected、冲突码、可否 rebase/retry、可解释 reasons、是否新建快照、是否幂等重放。

## 支持的操作

| 操作 | 语义 |
| --- | --- |
| `add_files` | 增加文件；当前快照已存在的路径冲突；陈旧 base 上若目标分区被后续提交触碰则冲突，否则可安全 rebase |
| `replace_partition` | 替换目标分区全部文件；陈旧 base 上目标分区被后续提交触碰则冲突 |
| `replace_file_set` | 显式期望一组 base 文件并替换；任一期望文件消失或指纹变化则冲突 |

所有操作都从当前快照重新物化文件清单；被接受的陈旧提交会在 `commit.json` 和报告中记录 rebase 证据（原 base、当前快照、 intervening attempt/snapshot、策略、检查分区）。

## 稳定冲突码

- `BASE_SNAPSHOT_UNKNOWN`
- `BASE_SNAPSHOT_NOT_ANCESTOR`
- `FILE_PATH_DUPLICATE`
- `PARTITION_TOUCHED_SINCE_BASE`
- `FILE_SET_CHANGED_SINCE_BASE`
- `REPLACE_PARTITION_CONFLICT`
- `IDEMPOTENCY_PAYLOAD_MISMATCH`
- `VALIDATION_ERROR`
- `STALE_BASE_REBASE_DISABLED`
- `STATE_INTEGRITY_ERROR`

`explain` 会展示判定时产生的全部 reason 记录，包括被接受时使用的 base/rebase 信息。

## 幂等语义

幂等作用域是 `(writer_id, idempotency_key)`，载荷身份包含：

```text
base_snapshot_id + operation
```

attempt id 与 logical time 是传输元数据，不参与载荷哈希，因此“换了 attempt id、逻辑时间更晚但操作相同”的重复提交会返回首次结果。base 或操作不同则触发 `IDEMPOTENCY_PAYLOAD_MISMATCH`。

首次判定（接受或拒绝）和重放次数都会写入 SQLite；新进程重建 `Ledger` 和 `ProtocolEngine` 后仍可重放。重放不会新增快照。

## 场景清单

`fixtures/scenarios/` 中共有 14 个可运行场景：

1. 当前 base 增加文件并被接受
2. 重复逻辑路径被拒绝
3. 安全增量 rebase 被接受
4. 陈旧分区替换冲突
5. 整分区替换成功
6. 替换路径跨分区冲突
7. 文件集合替换成功
8. base 后被替换文件已变化
9. 未知 base snapshot
10. 幂等重复提交重放
11. 幂等 key 载荷不一致
12. 拒绝后基于新快照重试
13. 混合 writer 负载（demo）
14. 陈旧新增触碰已变更分区

具体 attempt 与预期行为见 [docs/fixtures.md](docs/fixtures.md)。

## 离线与确定性

- 运行期不访问外部网络、CDN、对象存储、数据库或第三方 API。
- 复核台只允许绑定回环地址。
- 报告中的时间全部来自场景逻辑时钟（`YYYY-MM-DDTHH:MM:SSZ`）。
- snapshot id 由 parent、sequence、attempt id、payload hash 和逻辑时间确定性计算。
- JSON 使用键排序；所有路径、文件清单和冲突码集合都显式排序。
- `tools/network_guard.py` 可在测试中拦截 socket 连接并扫描禁止依赖/外部 URL。
- `tests/test_determinism.py` 对同一场景运行两次并逐字节比较 JSON/Markdown/HTML。

## 合成湖快照区域可用性计划

`lakecommit/storage_resilience.py` 从已提交快照文件清单核快照身份与区域覆盖，再做两阶段规划：情景揭晓前只做一次对所有情景相同的加固（默认最多 1 台），揭晓后才按该情景修复最多 1 台故障节点。

```bash
python -m lakecommit.storage_resilience --fixture fixtures/storage_resilience/intake.json
```

fixture 事先授权两份合同：`continuous_region` 要求故障初期与修复后都无区域中断（在 B 加固，成本 6）；`eu_repair_window` 允许 east 初期仅 eu 中断并按合同补偿 2，揭晓后修复 B/C 一台，目标成本 5。旧 `preview_scenario_hardening` 只会按情景各选最便宜的 A/C，那是使用未来信息，不能合并成一份事前承诺。计划输出包含快照/文件身份、逐情景初期与恢复后逐区域状态、中断时序、可执行修复、费用/补偿与最优性证据；未知节点/区域、重复情景、负成本、未提交快照和无可行方案都会被拒绝。

## 项目结构

```text
lakecommit/          # CLI、加载校验、协议、快照存储、台账、报告、服务、区域计划
fixtures/            # 14 个 YAML 场景、表清单、JSONL 尝试、golden、区域计划 intake
docs/                # 架构、提交语义、fixtures、验收、限制
tests/               # 单元、集成、golden、确定性、离线、预算、区域计划测试
tools/               # 独立行数统计与网络保护工具
```

进一步阅读：

- [架构说明](docs/architecture.md)
- [提交语义](docs/commit-semantics.md)
- [Fixture 规范](docs/fixtures.md)
- [验收清单](docs/acceptance.md)
- [边界与限制](docs/limitations.md)

## 明确不做的事

不实现真实湖表格式兼容、对象存储、元数据服务、SQL 查询、时间旅行、模式演进、行级删除、压缩/重写/快照过期、分布式共识或真实并发线程调度。它是提交语义验证台，不是生产运行时。
