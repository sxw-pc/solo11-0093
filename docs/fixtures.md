# Fixture 规范

所有 fixture 都是合成数据：表名、writer、分区值、路径、哈希和时间均为测试用途，不含真实业务信息。

## 目录

```text
fixtures/
├── scenarios/         # 14 个 YAML 场景
├── tables/
│   └── lake_sales_orders.json
├── attempts/          # 14 个 JSONL 提交尝试文件
└── expected/          # 14 个规范化 JSON golden
```

## 表规格

`fixtures/tables/lake_sales_orders.json` 包含：

```json
{
  "table_name": "lake_sales_orders",
  "partition_columns": ["region"],
  "file_fields": ["path", "partition", "size_bytes", "row_count", "content_hash"],
  "initial_snapshot": {
    "snapshot_id": "snap-0000",
    "parent_snapshot_id": null,
    "sequence": 0,
    "logical_time": "2024-01-01T00:00:00Z",
    "files": [...]
  }
}
```

初始快照的四个文件分别位于 `ca`、`eu`、`us` 分区。所有场景共享同一个初始基线，但各自独立运行，互不共享工作目录。

文件条目：

```json
{
  "path": "data/region=us/part-0003.parquet",
  "partition": {"region": "us"},
  "size_bytes": 640,
  "row_count": 40,
  "content_hash": "3333..."
}
```

`content_hash` 是 fixture 内的合成内容标识，不一定对应真实文件内容；它用于文件级指纹比较。

## 场景 YAML

```yaml
id: safe_rebase_accepted
name: Safe additive rebase accepted
description: A stale additive commit targets an untouched partition.
table: ../tables/lake_sales_orders.json
attempts: ../attempts/03_safe_rebase.jsonl
protocol:
  allow_safe_rebase: true
```

字段：

| 字段 | 说明 |
| --- | --- |
| `id` | 场景稳定标识，也是 golden 文件名 |
| `name` | 报告标题 |
| `description` | 报告副标题 |
| `table` | 相对场景目录的表规格路径 |
| `attempts` | 相对场景目录的 JSONL 路径 |
| `protocol.allow_safe_rebase` | 是否允许通过检查的陈旧 base 安全 rebase |
| `protocol.allow_idempotency_mismatch` | 默认 false；为 true 时允许输入中出现故意制造载荷不一致的提交，以便在运行时验证 `IDEMPOTENCY_PAYLOAD_MISMATCH` |

场景引用本身允许 `../` 相对路径；表内的**逻辑文件路径**绝不允许绝对路径、`..`、反斜杠、盘符或 URL。

## 尝试 JSONL

一行一个 JSON 对象：

```json
{"attempt_id":"a-alpha-003","writer_id":"writer-alpha","idempotency_key":"idem-alpha-003","base_snapshot_id":"snap-0000","logical_time":"2024-01-01T00:03:00Z","operation":{"type":"add_files","files":[...]}}
```

规则：

- `attempt_id` 在单个场景内唯一。
- writer、key、base、logical time 使用稳定标识格式；逻辑时间格式固定为 `YYYY-MM-DDTHH:MM:SSZ`。
- 同一 `(writer_id, idempotency_key)` 的载荷默认必须一致；有意验证载荷冲突的场景需在协议中显式允许。
- 默认按文件行序到达，同一场景内不存在真实并发线程。
- `retry_of_attempt_id` 可选，必须指回本场景中已声明的 attempt。

支持操作：

- `add_files`
- `replace_partition`
- `replace_file_set`
- `swap_file_sets`

更完整语义见 [commit-semantics.md](commit-semantics.md)。

## 17 个场景

| 文件 | 场景 ID | 覆盖点 |
| --- | --- | --- |
| `01_add_files_accepted.yaml` | `add_files_accepted` | 当前 base 增量提交接受 |
| `02_duplicate_path_rejected.yaml` | `duplicate_path_rejected` | 重复路径 `FILE_PATH_DUPLICATE` |
| `03_safe_rebase_accepted.yaml` | `safe_rebase_accepted` | 陈旧 base、不同分区、安全 rebase |
| `04_partition_touched_conflict.yaml` | `partition_touched_conflict` | 陈旧整分区替换冲突 |
| `05_replace_partition_accepted.yaml` | `replace_partition_accepted` | 当前 base 整分区替换 |
| `06_replace_collision_rejected.yaml` | `replace_collision_rejected` | 替换路径与分区外文件冲突 |
| `07_file_set_accepted.yaml` | `file_set_accepted` | `replace_file_set` 接受 |
| `08_file_set_changed_conflict.yaml` | `file_set_changed_conflict` | expected 文件消失 |
| `09_base_unknown_rejected.yaml` | `base_unknown_rejected` | `BASE_SNAPSHOT_UNKNOWN` |
| `10_idempotent_replay.yaml` | `idempotent_replay` | 同载荷重放，不新增快照 |
| `11_idempotency_mismatch.yaml` | `idempotency_mismatch` | 同 key 不同载荷拒绝 |
| `12_reject_then_retry.yaml` | `reject_then_retry` | 拒绝后基于新快照重试成功 |
| `13_mixed_workload.yaml` | `mixed_workload` | 多 writer 混合、demo |
| `14_add_partition_conflict.yaml` | `add_partition_conflict` | 陈旧增量触碰已变分区 |
| `15_swap_accepted.yaml` | `swap_accepted` | 跨分区原子交换接受与幂等重放 |
| `16_swap_stale_routes.yaml` | `swap_stale_routes` | 同一不相关追加下 strict/preserve 两条路线分叉 |
| `17_swap_reject_retry.yaml` | `swap_reject_retry` | expected 被并发替换后拒绝，新 base 重试成功 |

### 确定性快照 ID 与重试 base

`12_reject_then_retry.jsonl`、`17_swap_reject_retry.jsonl` 中重试尝试使用前序接受提交生成的快照 ID（例如 `snap-0001-35b11505a737`、`snap-0001-404de0d43be2`）。这些 ID 由对应 attempt 的确定性内容计算得出。若修改前序 attempt 的操作、ID 或逻辑时间，需要重新生成该 base 值，并重新生成 golden。

## Golden

`fixtures/expected/<scenario id>.json` 是 `report.json` 的完整规范化副本，包含：

- 全部 attempt 判定、reason、冲突码、replay/rebase 证据；
- 快照链、最终文件清单；
- 状态自校验结果；
- `run_fingerprint`。

`python -m lakecommit verify-golden --workdir DIR` 按报告中的场景 ID 找到 golden，并比较规范化 JSON。golden 是回归基线，不是人工摘要；任何协议语义变化都应该显式更新。

## 新增场景

1. 在 `fixtures/tables/` 增加或复用表规格。
2. 在 `fixtures/attempts/` 增加 JSONL。
3. 在 `fixtures/scenarios/` 增加 YAML。
4. 运行：
   ```bash
   python -m lakecommit run --scenario fixtures/scenarios/99_new.yaml --workdir .tmp-new
   ```
5. 确认判定符合预期后，把 `.tmp-new/report.json` 复制到 `fixtures/expected/<scenario id>.json`。
6. 运行 `pytest -q`，包含 golden、确定性和离线检查。

## 校验错误定位

加载/校验错误包含：

- 稳定错误码；
- `location`（相对 fixture 位置；JSONL 解析错误带行号）；
- `field_path`（如 `attempts[2].operation.files[0].path`）；
- 必要时 `details`。

示例：

```json
{"error":{"code":"PATH_TRAVERSAL","message":"file paths may not contain '..'","location":"fixtures/tables/x.json","field_path":"initial_snapshot.files[0].path"}}
```
