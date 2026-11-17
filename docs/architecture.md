# 架构说明

## 目标

本项目把“多个 writer 基于不同快照向同一张轻量文件湖表提交文件级变更”简化成一个可审计、确定性、离线的验证流程。它不模拟真实存储和分布式调度，而是把提交协议的关键状态显式落盘，从而能让冲突判定、安全 rebase、幂等重放和回归结果被逐项检查。

## 数据流

```text
scenario.yaml
  ├── 引用 table.json ──► 表规格 + 初始快照
  └── 引用 attempts.jsonl ──► 有序提交尝试
                │
                ▼
          loader + validator
                │  (稳定错误码、文件位置、字段路径)
                ▼
          ProtocolEngine ───────────────┐
             │                          │
             ├── SQLite idempotency cache
             ├── snapshot ancestry / changes since base
             ├── pure operation evaluation
             ├── SnapshotStore (manifest + commit + current)
             └── SQLite audit ledger
                │
                ▼
       normalized report (JSON)
          ├── Markdown
          ├── self-contained HTML
          └── run fingerprint / golden compare
```

## 模块划分

| 模块 | 职责 |
| --- | --- |
| `config.py` | 稳定常量、路径、正则、默认协议、回环地址白名单 |
| `errors.py` | 输入错误 2、校验错误 3、golden 错误 4 的稳定类型与 JSON 序列化 |
| `fieldvalidator.py` | 逻辑文件路径、分区、文件条目、逻辑时间等字段级校验 |
| `validator.py` | 表、初快照、尝试、操作、场景、幂等载荷冲突的语义校验 |
| `loader.py` | YAML/JSON/JSONL 读取、相对 fixture 引用解析、场景 bundling |
| `models.py` | FileEntry、Snapshot、Attempt、Decision、ScenarioBundle |
| `snapshot.py` | 规范化文件清单、manifest/commit 哈希、确定性 snapshot id |
| `store.py` | 本地不可变快照目录、原子 current 指针、快照链与完整性校验 |
| `operations.py` | 纯内存文件级操作判定与 rebase 证据构造 |
| `idempotency.py` | 幂等载荷哈希、首次结果重放、载荷不一致拒绝 |
| `ledger.py` | SQLite `attempt_audit`、`idempotency_cache`、`snapshot_audit` |
| `protocol.py` | 判定顺序、base 检查、陈旧 base 处理、快照发布、台账写入 |
| `report_json.py` | 规范化报告、摘要、最终文件清单、指纹 |
| `report_md.py` | 稳定 Markdown 报告 |
| `report_html.py` | 自包含 HTML 报告（仅内联 CSS，无外部资源） |
| `server.py` | 只读 Flask 复核台、GET/HEAD 限制、回环地址与安全响应头 |
| `runner.py` | 工作目录准备、场景执行、golden 比对、explain 查询 |
| `cli.py` | 统一命令入口与退出码 |
| `linecount.py` | 代码规模预算和单文件上限 |

## 快照存储布局

```text
state/
├── current
├── ledger.sqlite3
└── snapshots/
    ├── snap-0000/
    │   ├── manifest.json
    │   └── commit.json
    └── snap-0001-<hash>/
        ├── manifest.json
        └── commit.json
```

`manifest.json` 包含：

- snapshot id、parent、sequence、逻辑时间；
- 排序后的文件清单（路径、分区、大小、行数、内容哈希）；
- `manifest_hash`（文件清单规范化后的 SHA-256）；
- `commit_hash`（commit 规范化后的 SHA-256）。

`commit.json` 包含 attempt id、writer、操作类型、原始 base、payload hash、触碰分区、增删路径和 rebase 证据。

`current` 指针只在快照目录完整写入并原子 rename 后，通过临时文件 + `os.replace` 更新。拒绝的提交不会写入快照目录。

## 判定顺序

1. 结构/语义校验；失败则以 `VALIDATION_ERROR` 拒绝，不进入幂等缓存。
2. 幂等作用域与载荷哈希检查。
   - 已有 key + 相同载荷：返回首次结果，`outcome=replayed`。
   - 已有 key + 不同载荷：`IDEMPOTENCY_PAYLOAD_MISMATCH`。
3. 读取 current，检查 base 是否存在、是否为 current 祖先。
4. 计算 base 到 current 之间已接受快照的 commit 变更。
5. 按操作类型执行纯函数判定；必要时产生安全 rebase 证据。
6. 接受时从 current 物化最终文件清单，发布新快照，写台账，最后记录幂等首结果。
7. 运行结束后遍历快照链做完整性校验，再生成报告。

## 安全 rebase

安全 rebase 不是“自动合并任意文件”。它只覆盖以下可证明安全的情况：

- `add_files`：新增路径与 current 无重复，且新增文件涉及的分区没有在 base 之后被触碰。
- `replace_partition`：陈旧 base 上目标分区没有在 base 之后被触碰。
- `replace_file_set`：所有 expected 文件仍在 current 中且指纹与 base 相同，新增路径不与集合外文件冲突。

接受时 parent 始终指向真正 current 快照，而不是旧 base；commit 同时记录逻辑 base 和 rebase 证据，保证“writer 基于哪个快照提交”可追溯。

## 确定性设计

- 不读取系统时间；时间全部来自 fixture 的逻辑时钟。
- 不生成随机 UUID；snapshot id 由内容哈希决定。
- 所有 JSON 写入使用 `sort_keys=True`，列表显式排序。
- 快照文件、文件清单、分区、路径、冲突码、intervening 快照都稳定排序。
- 报告中的绝对工作目录路径被排除在规范化内容之外。
- golden 比对直接比较规范化 JSON 对象，报告自带内容指纹便于定位差异。

## 离线边界

- 不使用对象存储、远程数据库或第三方 API。
- HTML 不加载 CDN、字体、脚本、图片。
- 测试中的 `blocked_network` 会替换 socket 连接/DNS 函数；演示路径必须在该保护下运行成功。
- `serve` 在创建 Flask app 之前校验 host，只允许三个回环字符串。

## 一致性边界

快照发布与 SQLite 记录不是一个跨文件系统事务：先发布快照，再写台账和幂等缓存。若进程在两步之间崩溃，可能出现“快照已发布但台账未记录”的孤儿状态；`store.verify()` 会检查 current 可达性与孤儿目录。该项目定位为验证台，不实现生产级 WAL/恢复协议。
