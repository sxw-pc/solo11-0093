# 提交语义

## 输入模型

每个提交尝试形如：

```json
{
  "attempt_id": "a-alpha-001",
  "writer_id": "writer-alpha",
  "idempotency_key": "idem-alpha-001",
  "base_snapshot_id": "snap-0000",
  "logical_time": "2024-01-01T00:01:00Z",
  "operation": {
    "type": "add_files",
    "files": []
  }
}
```

判定不依赖线程调度或真实并发。JSONL 的记录顺序就是本验证台中的提交到达顺序；每个提交都读取当前 SQLite + 快照状态。通过这种方式可以确定性复现“多个 writer 基于不同快照提交”的交互结果。

## 判定优先级

1. **结构校验**：ID、逻辑时间、分区、路径、字段类型、操作类型、载荷内重复路径等。失败时 `status=rejected`、`conflict_codes=["VALIDATION_ERROR"]`，不写幂等缓存。
2. **幂等检查**：作用域为 writer + key，载荷身份为 base + operation。
3. **base 检查**：base 不存在 → `BASE_SNAPSHOT_UNKNOWN`；存在但不是 current 祖先 → `BASE_SNAPSHOT_NOT_ANCESTOR`。
4. **操作判定**：路径、分区触碰、文件集合变化。
5. **接受后物化**：基于 current 的文件集合应用操作，发布快照。

## 操作语义

### add_files

```json
{"type": "add_files", "files": [FileEntry, ...]}
```

- 任一新增路径已存在于 current → `FILE_PATH_DUPLICATE`。
- 若 base 不是 current：计算 base 之后已接受快照触碰的分区集合；新增文件涉及这些分区 → `PARTITION_TOUCHED_SINCE_BASE`。
- 否则在协议允许时安全 rebase：结果 = current 文件 + 新文件。
- 纯增量且路径不重复、目标分区未被触碰时，陈旧 base 可安全接受。

### replace_partition

```json
{
  "type": "replace_partition",
  "partition": {"region": "eu"},
  "files": [FileEntry, ...]
}
```

- 所有替换文件必须属于目标分区（校验阶段）。
- 新路径不得与目标分区之外的文件冲突 → `FILE_PATH_DUPLICATE`。
- 若 base 陈旧且目标分区在 base 之后被触碰：
  - `REPLACE_PARTITION_CONFLICT`
  - 同时记录 `PARTITION_TOUCHED_SINCE_BASE` 以解释原因
  - `retry_required=true`
- 若目标分区未被触碰，允许安全 rebase：结果 = target 分区之外的文件 + 替换文件。
- 目标分区从有文件替换为空不在支持范围内；文件列表必须非空。

### replace_file_set

```json
{
  "type": "replace_file_set",
  "expected_files": ["data/region=us/part-0001.parquet"],
  "files": [FileEntry, ...]
}
```

- 每个 expected 文件必须存在于 base。
- 每个 expected 文件必须仍存在于 current，且文件完整指纹（路径、分区、大小、行数、内容哈希）与 base 一致；缺失或变化 → `FILE_SET_CHANGED_SINCE_BASE`。
- 新路径不得与 expected 集合之外的 current 文件冲突 → `FILE_PATH_DUPLICATE`。
- expected 之外的并发变化不影响判定；这是显式文件集合语义。
- 被接受时，先移除 expected 文件，再加入新文件。

### swap_file_sets（跨分区原子交换）

```json
{
  "type": "swap_file_sets",
  "swap_id": "swap-gen-015",
  "authorization": "strict_partition",
  "groups": [
    {"partition": {"region": "us"}, "expected_files": ["..."], "files": [FileEntry, ...]},
    {"partition": {"region": "eu"}, "expected_files": ["..."], "files": [FileEntry, ...]}
  ]
}
```

- 两个 `groups` 必须声明两个**不同**分区；两组 `expected_files` 必须互不相交，替换路径也不得跨组重复或与任何 expected 路径重叠（结构校验阶段拒绝）。
- 每个替换文件必须属于该组声明的分区；每个 expected 文件在 base 中必须存在且实际所属分区与声明一致，否则 `SWAP_EXPECTED_PARTITION_MISMATCH`。
- 双侧判定共享同一 base/current：任一 expected 文件在 current 中缺失或完整指纹（路径、分区、大小、行数、内容哈希）相对 base 变化，整个交换以 `FILE_SET_CHANGED_SINCE_BASE` 拒绝，**没有单侧发布**。
- 替换路径与 expected 集合之外的 current 文件冲突 → `FILE_PATH_DUPLICATE`，同样整体拒绝。
- 通过所有检查时只物化一次：从 current 移除两组 expected 后加入全部替换文件，协议只发布**一个**快照，`touched_partitions` 同时包含两个分区，rebase 策略为 `SWAP_PARTITION_REBASE`。
- `swap_id` 是版本配对标识，参与载荷哈希；同一 writer/key 下改动 `swap_id` 或 `authorization` 都会得到不同的 payload_hash。
- 陈旧 base 的两条授权路线（仅在 base 与 current 不同时有区别）：
  - `strict_partition`：任一目标分区出现在 base 之后的 `touched_partitions` 中即 `SWAP_PARTITION_TOUCHED_SINCE_BASE`，`retry_required=true`，本次交换不发布业务快照。
  - `preserve_unrelated`：忽略目标分区内与 expected 无关的并发追加，只要 expected 指纹未变就接受并保留这些追加；若 expected 本身被并发替换，仍以 `FILE_SET_CHANGED_SINCE_BASE` 拒绝。
- 两条路线都保证 expected 集合的原子替换与无丢失；对同一个“不相关并发追加”场景，前者拒绝、后者接受并保留追加，因此业务快照是否发布不同。
- 协议 `allow_safe_rebase=false` 时，陈旧 base 上即使 `preserve_unrelated` 也会以 `STALE_BASE_REBASE_DISABLED` 拒绝。

## 分区触碰的定义

一个接受快照会记录 `touched_partitions`：

- `add_files`：新增文件的所属分区。
- `replace_partition`：目标分区。
- `replace_file_set`：删除文件与新增文件涉及的分区并集。

`changes_since(base)` 返回 base 之后、按 sequence 升序的 commit 列表；触碰分区是其并集。初次快照（bootstrap）只在 base 等于初始快照时不会被算作“base 之后”的变更。

## 冲突码与重试建议

| 冲突码 | 含义 | `retry_required` |
| --- | --- | --- |
| `VALIDATION_ERROR` | 提交结构/语义不合法 | false |
| `IDEMPOTENCY_PAYLOAD_MISMATCH` | 同 writer+key 的载荷不同 | false |
| `BASE_SNAPSHOT_UNKNOWN` | base 不存在 | true |
| `BASE_SNAPSHOT_NOT_ANCESTOR` | base 存在但不在 current 链上 | true |
| `FILE_PATH_DUPLICATE` | 路径与当前文件集合冲突 | false |
| `PARTITION_TOUCHED_SINCE_BASE` | 陈旧 base 的新增/替换触碰已变化分区 | true |
| `REPLACE_PARTITION_CONFLICT` | 陈旧 base 的整分区替换冲突 | true |
| `FILE_SET_CHANGED_SINCE_BASE` | expected 文件缺失或指纹变化 | true |
| `STALE_BASE_REBASE_DISABLED` | 协议显式禁止安全 rebase | true |
| `SWAP_EXPECTED_PARTITION_MISMATCH` | 交换的 expected 文件不属于声明分区 | false |
| `SWAP_PARTITION_TOUCHED_SINCE_BASE` | strict_partition 交换的目标分区被并发触碰 | true |
| `SWAP_PATH_OVERLAP` | 交换替换路径与 expected 路径重叠（结构校验） | false |

需要 rebase/retry 的 writer 应重新读取 current，生成新的 base snapshot、新的 attempt id，并通常使用新的 idempotency key；如果沿用旧 key，因 base 已变，载荷哈希也会变化，从而得到 `IDEMPOTENCY_PAYLOAD_MISMATCH`。

## 幂等重放细节

首次提交（无论 accepted 还是 conflict rejected）会保存到 `idempotency_cache`：

```text
(writer_id, idempotency_key) → payload_hash + first_decision_json + replay_count
```

重放条件：

- writer_id 与 key 相同；
- 载荷哈希相同（base + operation）；
- attempt id 和 logical_time 可以不同。

重放结果：

- 镜像首次 status 与 conflict codes；
- `outcome=replayed`；
- `snapshot_created=false`；
- `resulting_snapshot_id` 保留首次快照；
- 新增一条 `IDEMPOTENT_REPLAY` info reason；
- `replay_count` 持久化递增；
- 不写入新的快照目录。

载荷不同时返回 `IDEMPOTENCY_PAYLOAD_MISMATCH`，且不会覆盖首次缓存。

## 解释记录

每个决策的 `reasons` 是有序记录列表，每项包含稳定 `code`、`severity`、`message`，必要时包含 `details`。例如：

- 当前 base 接受：`ACCEPTED_ON_CURRENT_BASE`；
- 安全陈旧 base 接受：`SAFE_REBASE_ACCEPTED`；
- 幂等重放：`IDEMPOTENT_REPLAY`；
- 冲突：具体冲突码及路径/分区/快照细节。

`explain --attempt` 从 SQLite 台账读取首次或重放记录，输出原始载荷、状态、冲突码、reasons、replay 来源和结果快照。报告中的每次尝试也有同样信息。

## 示例：安全 rebase

```text
base=snap-0000
writer A: add us/part-0003  → accepted (snap-0001, touched us)
writer B: add ca/part-0002  → stale base, but ca untouched, no path collision
                            → accepted as safe rebase, parent=snap-0001
```

B 的快照 parent 是 A 的快照；commit 中记录逻辑 base 为 `snap-0000`，并带有 `rebase` 证据。

## 示例：重试

```text
base=snap-0000
A: add us/part-0003 → accepted (snap-0001)
B: replace us on base snap-0000 → REPLACE_PARTITION_CONFLICT
B retry: replace us on base snap-0001, new attempt id/key → accepted (snap-0002)
```

重试是新的提交，不能依赖旧尝试的判定结果；只有在幂等 key 与载荷都相同时才会重放。
