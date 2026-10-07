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

默认只提取**题干 / 选项 / 答案**。用户要「考点还原 / 标准解析 / 总结分析」这类**首屏之外**的内容时，
两者要一起用：`--modules 考点还原,标准解析`（要提取什么）+ `--display wide`（把屏幕临时加长，一次截全）；
详见第 7 节（首屏之外的内容）。

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
- `--text` 只解析有把握的部分（题号区间、输出目录、教材章节、手机地址、"先别跑"、
  「要考点还原/标准解析」这类模块需求、「加长屏幕」这类显示需求）；解析不出来的仍以
  `missing` 返回。**显式命令行参数永远优先**，不会被自然语言覆盖。
- 配套的三个查询/运维命令（都支持 `--json`）：
  `drpilot modules --json`（可用模块目录 + 当前配置）、
  `drpilot display --json`（手机显示设置；`--probe 2.0` 试跑一次并自动复位，`--reset` 无条件复位）、
  `drpilot capture --from 1 --output-dir DIR`（只截当前题存 PNG，**不调 AI、不需要 Key**）。
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

两个和这次升级有关的 warn：

- `config` 检查会校验提取模块（重名 / 与固定字段撞名会直接 `fail`），并说明本次是否启用加长屏；
- `adb` 检查会报告**手机上的显示覆盖残留**（`Override size`）。看到它就让用户跑
  `drpilot display --reset`，别在残留状态下继续提取。

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
| `missing_api_key` | 没有任何可用 Key | 让用户在 GUI「入口 → 模型服务」里新增，或写 `.env` |
| `adb_unavailable` / `adb_error` | adb 不在 PATH / 没设备 / 掉线 | `drpilot devices --json`，让用户检查 USB/无线调试 |
| `model_unavailable` / `ai_error` | 模型下架 / 调用失败 | `drpilot check-model --json`，换模型或看 `diagnostics` |
| `output_error` | 写盘失败（权限/磁盘满） | 换输出目录 |
| `precheck_failed` | 屏幕题号与起始题号不一致 | 让用户把手机停到起始题 |
| `capture_failed` | 截图数据异常（`drpilot capture`） | 重试一次；仍失败就查 `adb screencap` |

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

选填但常问的两项（**别主动加，用户提了才加**）：

| 信息 | 对应参数 | 说明 |
| --- | --- | --- |
| 除了题干/选项/答案还要什么 | `--modules 考点还原,标准解析` | 不加就只提取三件套；加了才会去读解析类页块 |
| 这些内容在首屏下面 | `--display wide` | 临时把逻辑屏加长到 2 倍一次截全；**会改手机显示设置，结束/异常都会自动复位** |

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
| 「连考点还原和标准解析一起提取」 | 在三件套命令后加 `--modules 考点还原,标准解析 --display wide`（再加 `--batch-size 2`） |
| 「先看看加长屏能不能截全」 | `uv run drpilot capture --from 1 --output-dir DIR --display wide --json` |
| 「有哪些模块能提 / 手机显示设置是不是被改过」 | `uv run drpilot modules --json` / `uv run drpilot display --json` |
| 「手机屏幕看着不对劲（上次没复位）」 | `uv run drpilot display --reset --json` |
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
| `--display wide` `--display-scale` `--display-width-scale` `--display-density` `--display-scaling` | **会临时改手机的显示设置（wm size/density/scaling）**。程序在结束与异常时都会复位，进程被强杀后要用 `drpilot display --reset` 恢复；倍数/加宽越大，单张截图体积越大 |

---

## 7. 首屏之外的内容：提取模块 + 加长主屏

**这是本次升级的核心用法**：很多刷题 App 把「考点还原 / 标准解析 / 总结分析」放在第一屏下面，
默认截图截不到。两个参数配合就能一次截全：

```bash
uv run drpilot --from 1 --to 20 --output-dir "D:\题库" \
    --textbook 生物化学与分子生物学 --chapter "第三章 核酸的结构与功能" \
    --modules 考点还原,标准解析 --display wide --batch-size 2 --json
```

- `--modules A,B`：要提取的模块。**模块名同时是 JSONL 的键与 Markdown 的小标题**（想用英文键就起英文名）。
  命中内置预设会自动带上别名与定位说明，模型写成别名也能认。查目录：`drpilot modules --json`。
- `--display wide`：运行期间把逻辑屏临时放大（高度 = 物理高 × `--display-scale`，默认 2 倍；
  宽度可选 `--display-width-scale`，实测 1260x2720 → 1764x5440），让 App 一屏把整页排出来。
  **仍然是「一题一张截图」**，不增加翻页动作、不拼接、不降分辨率。
- 程序会自动 **回读校验 + 降级**（设备会静默忽略过大的尺寸：实测高 8000 被忽略，
  于是按 0.875 / 0.75 / 0.625 逐级退让，高度上就是 1.75 → 1.5 → 1.25 倍），并 **等画面稳定**（实测约 3 秒）后才开始截图。
- **结束与异常都会自动复位**手机显示设置；进程被强杀留下残留时，跑 `drpilot display --reset`，
  `doctor` 也会在 `adb` 检查里报出来。
- 先验证再跑：`drpilot capture --from 1 --output-dir DIR --display wide --json` 只截当前题存 PNG
  （**不调 AI、不需要 Key**），可以肉眼确认「考点还原/标准解析」是否完整进屏。
- 还装不下时：`--display-width-scale 1.2~1.5`（画布更宽，长段落少折行；实测 1764x5440 能装下整段解析）、
  `--display-density 420`（更紧凑，但字形变小）或 `--max-tokens 8192` + `--batch-size 1`。
  密度也能**单独用**：`--display wide --display-scale 1 --display-density 420` 不加长、只把字变小
  （截图尺寸不变，翻页坐标照旧）；GUI 里就是宽长都留在物理尺寸、只填密度。
- **GUI 里是一个独立的「屏幕调节」窗口**（主页面「入口 → 屏幕调节」点开）：
  里面调 **宽 / 长 / 密度** 三个数，**只有滑块和输入框**（不再支持拖矩形），停手 0.4s 自动应用，
  左边是「原本」、右边是「调节后」的实时截图，另有「抓全尺寸截图」「回到物理尺寸」「一键复位显示」。
  手机不接受某组参数时窗口会用「请求 vs 手机实际」提醒用户调小。
  这条路径会**回填主页面三个隐藏字段并沿用配置**（开始提取时按同一套值跑），退出程序会复位；
  Agent 若用 GUI 帮用户调屏幕，调完记得点「一键复位显示」。
- **GUI 的页面层级**：主页面只放「任务范围 / 手机连接 / 识别与输出」三张卡 + 日志，
  其余四块设置各有一个入口窗口（翻页动作 / 提取模块 / 模型服务 / 屏幕调节），
  窗口里的改动实时回填主页面隐藏载体，开始提取用的始终是同一套值。
  让用户改「翻页方式（点按 / 滑动 / 录制）」「输出字段开关」「模型与 Key」「屏幕参数」时，
  让他们点对应的入口卡片，不要去命令行里凑参数。

JSONL 里多出来的形态（没配 `--modules` 时**不会**出现这个键）：

```json
{"id": 1, "...": "...", "answer": "C",
 "modules": {"考点还原": "（生物化学与分子生物学P3）…", "标准解析": "基因工程作为一门技术诞生于…"}}
```

产出侧的两个诊断字段（都在运行 JSON 与 `diagnostics` 里）：

| 字段 | 含义 | 下一步 |
| --- | --- | --- |
| `modules` | 本次提取的模块名数组 | 用于核对配置是否生效 |
| `missing_modules` | `required` 模块缺失的模块名 | 日志里有对应题号，按题号补录那几题 |
| `display` | `{mode, requested, actual, verified, settled_ms, attempts, width_scale, density_only}` | `verified=false` 说明设备没接受，按原始分辨率继续跑；`attempts` 里每个候选都带 width/height/scale |

---

## 8. 信息不足时必须向用户提问

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

## 9. 机器可读输出

`--json` 时：**stdout 只有一份 JSON**（包括 argparse 报错），所有日志/进度走 stderr。
这样 `... --json 2>/dev/null | jq` 永远能拿到干净结果。

```bash
uv run drpilot --from 1 --to 20 --output-dir /data/题库 --dry-run --json
```

```json
{
  "dry_run": true, "page_from": 1, "page_to": 20, "total": 20,
  "model": "deepseek-flash", "base_url": "https://api.deepseek.com",
  "output_dir": "/data/题库", "jsonl": "/data/题库/药理学_02_药物代谢动力学.jsonl",
  "markdown": "/data/题库/药理学_02_药物代谢动力学.md", "index": "/data/题库/index.json",
  "key_count": 3, "ok": true, "exit_code": 0, "exit_code_name": "ok",
  "log_file": "/data/drpilot/logs/drpilot-20260929-111338-12238.log"
}
```

真实运行的结果字段：`questions`（识别题数）、`jsonl`/`markdown`/`index`、`stopped`、`error`、
`modules`（本次提取的模块名）、`missing_modules`（必读模块缺失）、`display`（加长屏结果）、
`diagnostics`、`log_file`。出错时一定有 `ok:false` + `exit_code` + `error_code` + `phase`。

`--display wide` 时 dry-run 会多两行（`modules` / `display` 字段），可以直接拿来核对：
`display.verified` 只在真跑时才有值，dry-run 只报计划。

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

## 10. 日志与排障（意外情况看这里）

* 每次运行都写日志：默认 `logs/drpilot-<日期>-<时间>-<pid>.log`（仓库根目录，已 gitignore）。
  JSON 里的 `log_file` 就是路径；`--log-file PATH` 指定，`--no-log-file` 关闭。
  **图形界面也写同一份日志文件**（以前只存在界面面板里，关掉窗口就没了）：用户说「GUI 里跑失败了」
  时，直接读那个 `log_file` 就能看到完整过程，不用让他手工复制界面日志。
* 每行格式：`2026-09-29 11:13:59.123 | ERROR | batch | [W1] 批次 2 失败：HTTP 429（涉及题号 [5, 6]，可用 --from/--to 补录）`
  —— 时间 / 级别 / **阶段** / 内容，可以直接 grep 阶段或题号：
  ```bash
  grep "ERROR" logs/drpilot-*.log | tail -20
  grep -n "题号 \[5, 6\]" logs/drpilot-*.log
  ```
* `phase` 取值：`args` → `config` → `connect` → `display` → `model` → `preflight` → `prepare` →
  `capture` → `batch` → `write` → `done`。报错停在哪一步一目了然。
  （`display` 只在 `--display wide` 时出现；加长/加宽/改密度的应用与复位日志都打在这一段。）
* 与加长屏相关的日志关键词：`加长主屏已生效` / `加长屏没生效` / `已复位手机显示设置` /
  `显示设置可能没复位干净`。看到最后一条就让用户跑 `drpilot display --reset`。
* 「识别失败占位」相关：`模型只给出 N/M 题` + `逐张重问救回` 表示这一批回复被截断、程序已自动
  逐张补问（不用管）；`单张重问仍未识别` 才是真失败，此时结果 JSON 里一定有
  `failed_question_numbers` 与 `retry_command`，让用户把 `max_tokens` 调到 8192+ 或批大小降到 1 再补录。
  补录时**不会**用本次的失败占位覆盖文件里已有的好内容（日志写 `[保留] 第 N 题…`）。
* `[错误] 翻页失败：画面连续多次没有变化` = App 没翻到下一题（坐标/时长不对或页面卡住）。
  程序会**停止继续截图**（不再把同一张图当成下一题写出重复内容），已识别的部分照常写盘，
  结果 JSON 里 `capture_stalled=true`；让用户到「翻页动作」窗口重录/试一次后再补录后面的题号。
* **手机上「没设备」先看是不是被另一边占了**：WSL 和 Windows 各有一个 adb server，同一台手机
  同一时间只归一边。用户在用 Windows GUI 时，WSL 里的 `drpilot devices` 会显示没设备；
  要接手就先 `adb kill-server`，再在你这侧 `adb connect IP:端口`（别两边同时抢，会互相踢下线）。
* `--debug`：日志记 DEBUG 细节与堆栈，JSON 里多一个 `traceback` 字段。
* 日志里的 Key 一律脱敏（连命令行回显都会先掩码）；Key 永远不会出现在日志、JSON、配置文件里。

---

## 11. API Key 的存取规矩

* 运行期只从**环境变量**读取：`SILICONFLOW_API_KEY_1..16` / `SILICONFLOW_API_KEYS` / `DRPILOT_API_KEY_*`。
* GUI 的「入口 → 模型服务」窗口（新增 / 删除按钮）或 `keys.append_api_keys()` / `keys.save_api_keys()`
  会写进**项目根目录的 `.env`**（已在 `.gitignore` 里）。
* 桥方法：`add_keys({text})` 追加、`delete_key({index})` 删除第 index 个（1 起，顺序同「已保存」列表）、
  `clear_keys()` 清空、`reveal_keys()` 仅在用户主动点「显示」时返回明文。
* **绝对不要**把 Key 写进 `drpilot_config.json`、源码、日志、提交信息或测试夹具。
* 展示一律脱敏（`sk-********1234`）；只有用户主动操作才输出明文。
* 并发数不要超过 Key 数量，否则多出的 worker 共用同一个 Key，容易被限流。

---

## 12. 配置持久化

* `drpilot_config.json`（项目根目录，已 gitignore）：保存非敏感参数，GUI 里改动会写入。
* 优先级：**命令行参数 > 指定的 `--config` 文件 > 默认配置文件**；`--text` 解析出的参数排在命令行参数之后。
* `--chapter` 显式传入而没给 `--chapter-no` 时，章号会从新章节名重新解析（避免配置文件里的旧章号把文件名带偏）。
* 显式 `--chapter` 但没给 `--chapter-total` 时，若配置文件里有旧的章总题数，会打印提示——因为它无法自动推算。
* 提取模块与加长屏也会写进 `drpilot_config.json`（`modules` / `display_mode` / `display_scale` /
  `display_width_scale` / `display_density` / `display_scaling` / `max_tokens`）：用户配过一次，
  下次不带参数也会照着跑（GUI 的「屏幕调节」窗口改完会回填这三个数）。
  翻页方式（`page_action` = auto/tap/swipe/record）、手动点按坐标（`tap_x`/`tap_y`/`tap_ms`）
  与**输出字段开关**（`output_fields`，默认题号/题目/答案三项都能单独关掉 JSON 或 Markdown）
  同样会写进配置文件；GUI 的「翻页动作」「提取模块」窗口改的就是它们。
  想临时清掉模块：`--modules ""`；想临时关掉加长屏：`--display off`。

---

## 13. 给 Agent 的硬性禁令

1. 不要修改 `uv.lock`（用 `uv add/remove`）。
2. 不要提交 `.env`，不要在输出里回显任何 Key 明文。
3. 不要在未确认的情况下使用 `--force-start` / `--no-preflight`。
4. 不要把 `--from`/`--to` 当成「页码」——它是**题目编号**。
5. 不要为了「省事」跳过 `doctor`：环境类失败的修复提示都在里面。
6. 任务中途失败不要从头重跑整段：读 `error_code` + `diagnostics.error_context.question_numbers`，
   直接跑 `retry_command` 补录。
7. 不要用 `-i` / `--interactive` 驱动自动化（会阻塞等输入）；要问用户就用 `ask --json` 的 `questions`。
8. 用了 `--display wide` 就要确认手机复位：运行 JSON 的 `diagnostics.display` 与日志都会写，
   异常中断后用 `drpilot display --reset` 收尾，**不要把用户的手机留在加长状态**。

---

## 14. 开发自检（改代码的人/Agent）

```bash
uv run python -m unittest discover -s tests -v   # 全部单元测试
uv run ruff check src tests                      # 静态检查
uv run python tools/ui_selftest.py               # 无头浏览器点遍五个页面（主界面 + 四个入口窗口）
uv run python tools/ui_preview.py --out /tmp/ui.png   # 渲染界面截图，调版面用
uv run drpilot schema --json | jq '.required_for_run'  # 契约是否跟着参数一起更新
uv run drpilot modules --json | jq '.presets[].name'      # 模块目录是否正常
uv run drpilot display --json | jq '.state'               # 手机上有没有残留的显示覆盖
```

在 WSL 里跑测试时不要把环境建到仓库里（见第 2 节）：
`UV_PROJECT_ENVIRONMENT=$HOME/.venvs/drpilot_v4 uv run python -m unittest discover -s tests`

版本：`uv run drpilot --version` → `drpilot 4.2.0`。
