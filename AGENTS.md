# AGENTS.md — 让 Agent 安全、准确地驱动 DrPilot v4

本文件是给 **AI Agent / 自动化脚本** 看的操作手册。人类用户请看 [README.md](README.md)。

---

## 1. 这个程序是做什么的

DrPilot v4 用 ADB 让手机翻页并截图，把截图交给视觉大模型识别成题目，导出：

| 文件 | 用途 |
| --- | --- |
| <教材>_<章号>_<章节>.jsonl | 主产物，一行一题，自带教材/章节/题号元数据 |
| <教材>_<章号>_<章节>.md | 人读版（可关） |
| index.json | 索引：教材 → 章节 → 文件 → 已有题号列表 |

**题号规则（最重要）**：题号以 **截图右上角显示的数字** 为准，不是程序顺序号。
所以可以从任意题开始，漏题也只补录那一题，不会把后面的编号带偏。

---

## 2. 环境与安装（uv 管理）

```bash
uv sync              # 按 uv.lock 安装运行依赖（Python 3.13+）
uv sync --group dev  # 额外装 pytest / ruff（开发用）
uv run drpilot --version
```

不要用 `pip install` 混装；不要手改 `uv.lock`（用 `uv add` / `uv remove`）。

> **不要和用户的操作系统抢 `.venv`。** 项目常放在 `/mnt/d/...`（即 Windows 的 `D:\...`），
> 用户在 Windows 侧跑 `uv sync`。Agent 若在 WSL 里也跑一次 `uv sync`，会在共享目录留下 Linux 符号链接
> （`lib64` / `bin/python`），Windows 删不掉这些 reparse point，用户下次 `uv sync` 就会报
> `failed to remove file ...\.venv\lib64: 拒绝访问 (os error 5)`。
> 需要在 WSL 里跑测试时，把环境放到仓库外：
> `UV_PROJECT_ENVIRONMENT=$HOME/.venvs/drpilot uv sync --group dev`，并让 `.venv` 始终归 Windows 侧。

---

## 3. 三个「先问清楚」的入口（Agent 首选）

不用背参数，也不用猜——这三条命令就是为 Agent 设计的：

```bash
uv run drpilot schema --json      # 1) 完整契约：参数类型/必填/示例/关键词、退出码、危险参数、常用套路
uv run drpilot ask --json         # 2) 只回答：还缺什么、该问用户什么、凑齐后跑什么命令
uv run drpilot ask --text "药理学第二章 47到81题，存到 D:\题库" --json   # 3) 一句自然语言 -> 参数
```

- `ask --json` 的关键字段：`ready`（能否直接跑）、`missing`（缺的参数）、`questions`（**直接拿去问用户的句子**）、
  `ask_user`（拼好的一句话）、`command`（ready 时的可复制命令）、`command_template`（占位符版）。
  `ready=false` 时退出码是 2，并且 `ok=false`——**不要**拿默认值硬跑。
- `--text` 只解析有把握的部分（题号区间、输出目录、教材章节、手机地址、"先别跑"）；解析不出来的仍以
  `missing` 返回。**显式命令行参数永远优先**，不会被自然语言覆盖。
- 人在终端里跑时，缺参数可以加 `-i`（TTY 下也会自动进入）：CLI 逐项提问、补齐后确认再跑。
  Agent 不要用 `-i`（会阻塞等输入），改用 `ask --json` 自己问用户。

---

## 4. 第一件事：跑 doctor

任何操作之前先自检，它能一次性告诉你「能不能跑、缺什么、怎么修」：

```bash
uv run drpilot doctor            # 人读
uv run drpilot doctor --json     # 机器读（推荐给 Agent）
uv run drpilot doctor --full     # 不在第一个失败处停下，7 项全查
```

`--json` 输出结构：

```json
{
  "ok": false,
  "exit_code": 3,
  "exit_code_name": "env_unavailable",
  "counts": { "ok": 4, "warn": 0, "fail": 1, "skip": 0 },
  "checks": [
    { "name": "keys", "status": "fail", "detail": "未找到任何 API Key",
      "hint": "三种填法任选其一：…" }
  ],
  "remaining": ["config", "adb", "output", "model"],
  "log_file": "/path/logs/drpilot-20260929-111338-12238.log"
}
```

检查项顺序：`python` → `dependencies` → `keys` → `config` → `adb` → `output` → `model`。
`status` 取值：`ok` / `warn`（可继续）/ `fail`（阻塞）/ `skip`（显式跳过）。
默认遇到第一个 `fail` 就停，没跑的检查列在 `remaining` 里。

---

## 5. 退出码与错误码（Agent 靠它决定下一步）

| 码 | 名字 | 含义 | Agent 该做什么 |
| --- | --- | --- | --- |
| 0 | `ok` | 成功 | 读取输出文件 |
| 1 | `run_failed` | 跑起来之后失败（批量失败、写盘失败、掉线） | 读 `error_code` / `diagnostics` / `retry_command`，修复后**从失败题号续跑** |
| 2 | `config_invalid` | 参数/配置错误 | 按 `missing` / `questions` / `hint` 补齐后重跑，**不要重试同一命令** |
| 3 | `env_unavailable` | 环境不可用（缺依赖/adb/Key/模型下架） | 走 doctor 的 `hint`；需要用户提供信息时**停下来问用户** |
| 4 | `precheck_failed` | 预检：屏幕题号 ≠ 起始题号 | 让用户把手机停在正确题目，或确认后用 `--force-start` |

**退出码 2 和 3 不要盲目重试**，它们不会因为重试而变好。

失败 JSON 里的 `error_code` 是稳定的机器可读错误码，常见取值：

| error_code | 含义 | 下一步 |
| --- | --- | --- |
| `missing_required_arguments` | 缺 --from / --to / --output-dir | 按 `questions` 问用户，或补参数重跑 |
| `invalid_arguments` | 参数写法错误（拼错、互斥冲突） | 查 `drpilot schema --json` |
| `config_file_invalid` | `--config` 指向的文件读不了 | 检查文件路径/JSON 语法 |
| `config_invalid` | 参数值不合法（范围、格式） | 按 `error` 修正 |
| `missing_api_key` | 没有任何可用 Key | 让用户在 GUI「模型服务」里新增，或写 `.env` |
| `adb_unavailable` / `adb_error` | adb 不在 PATH / 没设备 / 掉线 | `drpilot devices --json`，让用户检查 USB/无线调试 |
| `model_unavailable` / `ai_error` | 模型下架 / 调用失败 | `drpilot check-model --json`，换模型或看 `diagnostics` |
| `output_error` | 写盘失败（权限/磁盘满） | 换输出目录 |
| `precheck_failed` | 屏幕题号与起始题号不一致 | 让用户把手机停到起始题 |

---

## 6. 自然语言 → 参数 → 命令

用户说人话，你要落成精确参数。**必须凑齐的最小信息集**：

| 信息 | 对应参数 | 缺了会怎样 |
| --- | --- | --- |
| 从第几题开始 | `--from N` | 无法运行（退出码 2） |
| 到第几题结束 | `--to M` | 无法运行（退出码 2） |
| 输出到哪个文件夹 | `--output-dir DIR` | 无法运行（退出码 2） |
| 教材名 | `--textbook 药理学` | 能跑，但文件名退化为 `题目_起-止`，且缺元数据 |
| 章节名 | `--chapter "第二章 药物代谢动力学"` | 同上 |
| 手机地址（无线调试） | `--connect 192.168.1.5:5555` | 能跑，但需设备已连着 |

### 示例对照

| 用户原话 | 你要执行的命令 |
| --- | --- |
| 「把这章 47 到 81 题提取出来，存到 D:\题库」 | `uv run drpilot --from 47 --to 81 --output-dir "D:\题库"` |
| 「药理学第二章，1 到 20 题，输出到桌面题库」 | `uv run drpilot --from 1 --to 20 --output-dir "C:\Users\<用户名>\Desktop\题库" --textbook 药理学 --chapter "第二章 药物代谢动力学"` |
| 同一句，懒得拆参数 | `uv run drpilot --text "药理学第二章 1到20题，输出到桌面题库" --json` |
| 「先别跑，看看配置对不对」 | `uv run drpilot --from 1 --to 20 --output-dir DIR --dry-run --json` |
| 「还缺什么，去问用户」 | `uv run drpilot ask --from 47 --json` 然后照 `questions` 问 |
| 「连一下手机看看有没有连上」 | `uv run drpilot devices --json` |
| 「模型还能用吗」 | `uv run drpilot check-model --json` |
| 「打开图形界面」 | `uv run drpilot --gui` |
| 「漏了第 17 题，补一下」 | `uv run drpilot --from 17 --to 17 --output-dir 同一目录 --textbook 同一教材 --chapter 同一章节`（按题号合并写回，不影响其它题） |
| 「接着上次没跑完的继续」 | 用上次的 `--output-dir` + 未完成的 `--from`/`--to`；已存在的题号会被合并，不会重复 |

### 危险参数（先跟用户确认再用）

| 参数 | 风险 |
| --- | --- |
| `--force-start` | 跳过题号一致性预检，可能整批编号错位 |
| `--no-preflight` | 不做起始题预检，起始屏幕不对也不会提醒 |
| `--no-model-check` | 不测模型连通性，模型下架时会整批失败 |
| 改 `--swipe-*` 坐标 | 换机型需要重录翻页动作，错了会原地重复截图 |

---

## 7. 信息不足时必须向用户提问

不要猜。缺以下任何一项，**先问清楚再跑**：

1. **题号范围**：从第几题到第几题？（明确说明「以截图右上角数字为准」）
2. **输出位置**：存到哪个文件夹？
3. **教材与章节**：哪本书、哪一章？（影响文件名与元数据）
4. **手机怎么连**：USB 还是无线调试？无线的 IP:端口是多少？
5. **模型与 Key**：用哪个平台/模型？Key 配置了吗？（不知道就先跑 `doctor --json`，把 `keys` 检查结果告诉用户）

最省事的做法是让 CLI 替你组织提问：

```bash
uv run drpilot ask --from 47 --output-dir "D:\题库" --json | jq '.ask_user, .questions'
```

`ask_user` 就是一句可以直接发给用户的话，例如：
> 请补充：① 结束题号（--to）：到第几题结束？（包含这一题） ② 输出文件夹（--output-dir）：提取结果存到哪个文件夹？

---

## 8. 机器可读输出

`--json` 时：**stdout 只有一份 JSON**（包括 argparse 报错），所有日志/进度走 stderr。
这样 `... --json 2>/dev/null | jq` 永远能拿到干净结果。

```bash
uv run drpilot --from 1 --to 20 --output-dir /data/题库 --dry-run --json
```

```json
{
  "dry_run": true, "page_from": 1, "page_to": 20, "total": 20,
  "model": "Qwen/Qwen3.5-4B", "base_url": "https://api.siliconflow.cn/v1",
  "output_dir": "/data/题库", "jsonl": "/data/题库/药理学_02_药物代谢动力学.jsonl",
  "markdown": "/data/题库/药理学_02_药物代谢动力学.md", "index": "/data/题库/index.json",
  "key_count": 3, "ok": true, "exit_code": 0, "exit_code_name": "ok",
  "log_file": "/data/drpilot/logs/drpilot-20260929-111338-12238.log"
}
```

真实运行的结果字段：`questions`（识别题数）、`jsonl`/`markdown`/`index`、`stopped`、`error`、
`diagnostics`、`log_file`。出错时一定有 `ok:false` + `exit_code` + `error_code` + `phase`。

批量失败（部分题没识别出来）时额外给：

```json
{
  "failed_question_numbers": [5, 6, 11],
  "retry_command": "uv run drpilot --from 5 --to 11 --output-dir D:\题库 --textbook 药理学 --chapter 第二章 药物代谢动力学",
  "diagnostics": {
    "phase": "done",
    "error_context": { "phase": "batch", "worker": 1, "batch": 2,
                       "question_numbers": [5, 6], "error": "HTTP 429" },
    "batch_failures": [ { "batch": 2, "question_numbers": [5, 6], "error": "HTTP 429" } ]
  }
}
```

**修复策略**：直接跑 `retry_command`（同一输出目录，按题号合并写回），不用整段重跑。

---

## 9. 日志与排障（意外情况看这里）

* 每次运行都写日志：默认 `logs/drpilot-<日期>-<时间>-<pid>.log`（仓库根目录，已 gitignore）。
  JSON 里的 `log_file` 就是路径；`--log-file PATH` 指定，`--no-log-file` 关闭。
* 每行格式：`2026-09-29 11:13:59.123 | ERROR | batch | [W1] 批次 2 失败：HTTP 429（涉及题号 [5, 6]，可用 --from/--to 补录）`
  —— 时间 / 级别 / **阶段** / 内容，可以直接 grep 阶段或题号：
  ```bash
  grep "ERROR" logs/drpilot-*.log | tail -20
  grep -n "题号 \[5, 6\]" logs/drpilot-*.log
  ```
* `phase` 取值：`args` → `config` → `connect` → `model` → `preflight` → `prepare` →
  `capture` → `batch` → `write` → `done`。报错停在哪一步一目了然。
* `--debug`：日志记 DEBUG 细节与堆栈，JSON 里多一个 `traceback` 字段。
* 日志里的 Key 一律脱敏（连命令行回显都会先掩码）；Key 永远不会出现在日志、JSON、配置文件里。

---

## 10. API Key 的存取规矩

* 运行期只从**环境变量**读取：`SILICONFLOW_API_KEY_1..16` / `SILICONFLOW_API_KEYS` / `DRPILOT_API_KEY_*`。
* GUI 的「模型服务 → API Key」卡片（新增 / 删除按钮）或 `keys.append_api_keys()` / `keys.save_api_keys()`
  会写进**项目根目录的 `.env`**（已在 `.gitignore` 里）。
* 桥方法：`add_keys({text})` 追加、`delete_key({index})` 删除第 index 个（1 起，顺序同「已保存」列表）、
  `clear_keys()` 清空、`reveal_keys()` 仅在用户主动点「显示」时返回明文。
* **绝对不要**把 Key 写进 `drpilot_config.json`、源码、日志、提交信息或测试夹具。
* 展示一律脱敏（`sk-********1234`）；只有用户主动操作才输出明文。
* 并发数不要超过 Key 数量，否则多出的 worker 共用同一个 Key，容易被限流。

---

## 11. 配置持久化

* `drpilot_config.json`（项目根目录，已 gitignore）：保存非敏感参数，GUI 里改动会写入。
* 优先级：**命令行参数 > 指定的 `--config` 文件 > 默认配置文件**；`--text` 解析出的参数排在命令行参数之后。
* `--chapter` 显式传入而没给 `--chapter-no` 时，章号会从新章节名重新解析（避免配置文件里的旧章号把文件名带偏）。
* 显式 `--chapter` 但没给 `--chapter-total` 时，若配置文件里有旧的章总题数，会打印提示——因为它无法自动推算。

---

## 12. 给 Agent 的硬性禁令

1. 不要修改 `uv.lock`（用 `uv add/remove`）。
2. 不要提交 `.env`，不要在输出里回显任何 Key 明文。
3. 不要在未确认的情况下使用 `--force-start` / `--no-preflight`。
4. 不要把 `--from`/`--to` 当成「页码」——它是**题目编号**。
5. 不要为了「省事」跳过 `doctor`：环境类失败的修复提示都在里面。
6. 任务中途失败不要从头重跑整段：读 `error_code` + `diagnostics.error_context.question_numbers`，
   直接跑 `retry_command` 补录。
7. 不要用 `-i` / `--interactive` 驱动自动化（会阻塞等输入）；要问用户就用 `ask --json` 的 `questions`。

---

## 13. 开发自检（改代码的人/Agent）

```bash
uv run python -m unittest discover -s tests -v   # 全部单元测试
uv run ruff check src tests                      # 静态检查
uv run python tools/ui_selftest.py               # 无头浏览器点遍 GUI 按钮（含 API Key 新增/删除）
uv run python tools/ui_preview.py --out /tmp/ui.png   # 渲染界面截图，调版面用
uv run drpilot schema --json | jq '.required_for_run'  # 契约是否跟着参数一起更新
```

版本：`uv run drpilot --version` → `drpilot 4.0.0`。
