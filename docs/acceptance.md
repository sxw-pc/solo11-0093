# 验收清单

本文件把提示词中的硬约束与功能要求映射到可执行命令、测试与产物。所有检查均在本地离线环境运行。

> 提示词标题为 F1–F8，但正文只展开到 F5；F6–F8 按项目目标（报告、回归与只读复核台）补齐并单列。

## 快速验收

```bash
# 1. 全量测试
pytest -q

# 2. 完整演示（含 golden 比对）
python -m lakecommit demo --workdir .lakecommit-demo

# 3. 独立复核演示结果
python -m lakecommit verify-golden --workdir .lakecommit-demo

# 4. 校验快照链与当前指针
python -m lakecommit verify --workdir .lakecommit-demo

# 5. 查看单个尝试
python -m lakecommit explain --workdir .lakecommit-demo --attempt a-beta-013

# 6. 代码规模预算
python -m lakecommit linecount

# 7. 离线静态扫描
python tools/network_guard.py scan
```

预期：1–7 退出码均为 0；测试全部通过；`linecount` 报告在 2500–4500 行之间且无单文件超过 300 行。

## 硬约束映射

| 约束 | 实现/证据 | 验证 |
| --- | --- | --- |
| 完全离线，无外部网络 | 无网络客户端；HTML 自包含；socket/DNS 测试保护 | `tests/test_offline_guard.py`、`tools/network_guard.py scan` |
| 技术栈常见且可离线安装 | Python 3.10+、PyYAML、Flask、pytest | `pyproject.toml` |
| 一条命令跑完整演示 | `demo` 加载、校验、判定、快照、台账、报告、golden | `python -m lakecommit demo --workdir .lakecommit-demo` |
| 一条命令跑测试 | pytest 配置含 `pythonpath = ["."]` | `pytest -q` |
| 只读本地服务，仅回环地址 | host 白名单 + GET/HEAD 路由 + 写方法 405 | `python -m lakecommit serve ...`、`tests/test_server.py` |
| 无真实密钥/公司/域名/个人信息 | 全部 fixture 为合成主体；静态扫描检查外部 URL | 人工审阅 + `network_guard.py scan` |
| 输出确定 | 逻辑时钟、内容哈希 ID、键排序、稳定列表 | `tests/test_determinism.py`、golden |
| 2500–4500 行、单文件 ≤300 | `linecount.py` 预算常量与检查 | `python -m lakecommit linecount`、`tests/test_size_budget.py` |
| ≥12 个冲突场景 | 17 个 YAML 场景 | `fixtures/scenarios/`、`tests/test_golden.py` |
| 不接受只有文档/只有 CLI | 实现 + 14 个测试文件 + 17 份 golden | `pytest -q` |

## F1：项目骨架与命令行入口

| 要求 | 证据 |
| --- | --- |
| `demo` 全链路 | `cli.py::_cmd_demo` → `runner.run_scenario` → 引擎/存储/台账/报告 |
| `run --scenario --workdir` | `cli.py::_cmd_run` |
| `explain --workdir --attempt` | `cli.py::_cmd_explain` → `runner.explain_attempt` |
| `verify-golden --workdir` | `cli.py::_cmd_verify_golden` → `runner.verify_golden` |
| `serve` 默认 `127.0.0.1:8765` | `config.DEFAULT_HOST`、`config.DEFAULT_PORT` |
| 稳定退出码 0/2/3/4 | `errors.py` 异常类型 → `cli.py` 映射 |
| `python -m lakecommit --help` | `lakecommit/__main__.py`、`argparse` 子命令 |

验收命令：

```bash
python -m lakecommit --help
python -m lakecommit demo --workdir .lakecommit-demo
python -m lakecommit serve --host 192.0.2.1 --port 8765   # 退出码 2
python -m lakecommit verify-golden --workdir .lakecommit-demo
python -m lakecommit linecount
```

测试：`test_golden.py`（退出码 4）、`test_server.py`（host/端口/写方法）、`test_size_budget.py`。

## F2：场景与清单加载校验

| 校验点 | 证据 |
| --- | --- |
| 表名/分区列/文件字段/快照 ID/writer/attempt 格式 | `validator.py`、`fieldvalidator.py` |
| 逻辑路径拒绝绝对路径、`..`、URL、反斜杠、盘符 | `fieldvalidator.validate_file_path` |
| 分区值匹配声明列 | `fieldvalidator.validate_partition` |
| 大小/行数非负整数 | `fieldvalidator.validate_file_entry` |
| 操作类型限定四种 | `validator.validate_operation` |
| attempt ID 场景内唯一 | `validator.validate_attempts` |
| 同 writer+key 载荷一致 | `validator.validate_attempts`（可通过场景协议显式放开，仅用于运行时冲突探针） |
| 稳定错误码 + location + field_path | `errors.py`、`loader.py` |

测试：`test_loader.py`、`test_validator.py`。

## F3：本地快照存储与提交日志

| 检查点 | 证据 |
| --- | --- |
| 每次接受生成独立不可变目录 | `store.publish` 写 `manifest.json` + `commit.json` |
| snapshot id/parent/sequence/logical_time/文件清单/哈希/提交摘要 | `snapshot.py`、`store.py` |
| `current` 完整写入后原子替换 | 临时文件 + `os.replace` |
| 拒绝提交不进入当前快照但保留证据 | `ledger.attempt_audit` 保存决策，快照不发布 |
| `verify` 检查 parent 连续、清单哈希、指针、重复路径 | `store.verify`、`python -m lakecommit verify` |
| 不实现过期/孤儿删除/小文件合并 | 明确未实现，见 `limitations.md` |

测试：`test_snapshot_store.py` 覆盖发布、链、指纹、篡改与坏指针。

## F4：乐观并发提交协议引擎

| 操作/能力 | 证据 |
| --- | --- |
| 基于 base snapshot 判定 | `protocol.py` 读取 current 与 ancestry |
| `add_files` | `operations.evaluate_add_files` |
| `replace_partition` | `operations.evaluate_replace_partition` |
| `replace_file_set` | `operations.evaluate_replace_file_set` |
| `swap_file_sets` 跨分区原子交换 | `swap.evaluate_swap`，双侧判定共享一次快照发布；`strict_partition`/`preserve_unrelated` 两条授权路线 |
| 安全 rebase + 证据 | 四类操作的可证明检查；`commit.json` 与报告记录策略/分区/inserting attempts |
| 稳定冲突码 | `conflicts.py` + `operations.py` + `swap.py` |
| 所有判定写入理由列表 | `Decision.reasons`，`explain` 可查 |

测试：`test_protocol.py`、`test_conflicts.py`、`test_swap.py`，golden 场景 01–09、12、14–17。

## F5：幂等、重试与重复提交

| 要求 | 证据 |
| --- | --- |
| 同 writer+key 同载荷返回首次结果，不新建快照 | `idempotency.py`、`ledger.record_replay` |
| 同 key 不同载荷 → `IDEMPOTENCY_PAYLOAD_MISMATCH` | `idempotency.py` |
| 拒绝后新 attempt 可重新判定 | 场景 12、`test_protocol.py` |
| 记录首次结果/重放次数/来源 attempt | `idempotency_cache.first_attempt_id`、`replay_count` |
| SQLite 持久化，重启可恢复 | `ledger.py`；`test_idempotency.py` 重建引擎 |
| 不使用系统时间 | 仅 fixture 逻辑时间；SQLite 表无 wall-clock 列（`test_ledger.py`） |

测试：`test_idempotency.py`、`test_ledger.py`。

## F6：JSON / Markdown / 自包含 HTML 报告

- `report.json`：规范化对象、稳定排序、摘要、冲突计数、快照链、最终文件清单、自校验、`run_fingerprint`。
- `report.md`：稳定人读版本。
- `report.html`：内联 CSS，无脚本、CDN、外部图片/字体。
- 报告不含绝对工作目录、系统时间、随机 UUID 或集合遍历顺序。

测试：`test_reports.py`（三种产物、指纹、自包含 HTML、排序）。

验收：

```bash
python -m lakecommit demo --workdir .lakecommit-demo
ls .lakecommit-demo/report.json .lakecommit-demo/report.md .lakecommit-demo/report.html
```

## F7：Golden 回归与确定性

- `fixtures/expected/` 含 17 份完整规范化报告。
- `verify-golden` 直接比较规范化对象；缺失或不一致退出 4。
- 同一场景运行两次，JSON/Markdown/HTML 逐字节一致。
- 逻辑时钟来自 fixture；snapshot id 由内容哈希生成。

测试：`test_golden.py`（17 个场景 + 退出码 4）、`test_determinism.py`（双次运行逐字节比较 + 无系统时间假设）。

## F8：只读本地复核台与离线保护

- Flask app 只注册 `GET`/`HEAD`；`POST`/`PUT`/`PATCH`/`DELETE` 返回 405。
- 路由：`/`、`/report`、`/api/result`、`/api/attempts`、`/api/snapshots`、`/api/verify`、`/api/attempt/<id>`、`/healthz`。
- host 仅允许 `127.0.0.1`、`localhost`、`::1`；端口 1–65535。
- 响应带 CSP、`X-Content-Type-Options: nosniff`、`X-Frame-Options: DENY` 等头。
- 测试中的 socket/DNS 保护确保场景与演示不发起外部连接。

测试：`test_server.py`、`test_offline_guard.py`。

验收：

```bash
python -m lakecommit serve --host 127.0.0.1 --port 8765 --workdir .lakecommit-demo
# 另开终端
curl -s http://127.0.0.1:8765/api/verify
curl -s -o /dev/null -w '%{http_code}\n' -X POST http://127.0.0.1:8765/api/result
```

预期：`/api/verify` 返回状态 JSON，POST 返回 405。

## 测试文件覆盖

| 测试文件 | 覆盖 |
| --- | --- |
| `test_loader.py` | 场景/表/JSONL 加载、解析错误、幂等严格性开关、无时钟假设 |
| `test_validator.py` | 路径、分区、字段、重复、操作、重试引用 |
| `test_snapshot_store.py` | 发布、链、变更、篡改、指针 |
| `test_protocol.py` | 接受/拒绝、rebase、重试、文件集合 |
| `test_swap.py` | 跨分区原子交换、双侧拒绝、授权路线、重放与重试 |
| `test_conflicts.py` | 稳定冲突码、理由、重试建议、计数 |
| `test_idempotency.py` | 载荷身份、重放、冲突、重启持久化 |
| `test_ledger.py` | 审计行、幂等 JSON、只读重开、无 wall-clock 列 |
| `test_reports.py` | 三报告产物、指纹、自包含、排序 |
| `test_server.py` | 路由、405、host 白名单、端口、安全头 |
| `test_golden.py` | 17 场景 golden、退出码 4 |
| `test_determinism.py` | 两次运行字节一致、逻辑时钟 |
| `test_offline_guard.py` | socket 阻断、离线运行、扫描 |
| `test_size_budget.py` | 总行数、单文件上限、必需模块/场景/golden |

## 代码规模

`python -m lakecommit linecount` 输出项目 Python 文件总数、总行数、最大文件与预算结论。当前预算常量：

```text
MIN_LINES = 2500
MAX_LINES = 4500
MAX_FILE_LINES = 300
```

生成目录（`.lakecommit-demo`、`.pytest_cache`、`__pycache__` 等）不参与统计。

## 退出码验收

| 场景 | 预期 |
| --- | --- |
| `demo`、`run`、`explain`、`verify`、`verify-golden` 正常完成 | 0 |
| 不存在的场景文件、坏 YAML/JSONL、非回环 host | 2 |
| fixture 校验失败、快照状态损坏、linecount 超预算 | 3 |
| golden 缺失或与当前报告不一致 | 4 |
| 用户 Ctrl-C | 130 |
| 未捕获内部异常 | 1 |

单个尝试被拒绝**不会**让命令失败；协议完成即返回 0。这一点由 golden 场景和 `test_protocol.py` 共同保证。
