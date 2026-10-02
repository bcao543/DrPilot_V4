# DrPilot v4

通过 ADB 模拟滑动翻页并截图，把截图交给 AI 识别成题目，按章节导出 **JSONL + Markdown + index.json**。
同时提供命令行（CLI）和图形界面（GUI）。设计目标是让后续的 AI 学习系统用最少的 token 精确定位到
「某本教材、某一章、第 N 题」。

## v4 新增

- **模型服务卡片内置 API Key 管理**：在「模型服务」里直接粘贴 Key（**隐私输入模式**，默认圆点，
  点小眼睛才临时显示），点「**＋ 新增 API Key**」追加写入 `.env`；下面列出已保存的 Key（脱敏显示），
  每个后面有「✕」**删除**按钮，也可以「全部删除」。明文只在输入框里存在，后端只回脱敏摘要。
- **CLI 为 Agent 而优化**：
  - `drpilot schema --json` —— 机器可读的参数契约（类型 / 必填 / 示例 / 危险参数 / 退出码 / 常用套路）；
  - `drpilot ask --json` —— 只回答「还缺什么参数、该问用户什么、凑齐后跑什么命令」；
  - `drpilot --text "药理学第二章 47到81题，存到 D:\题库"` —— 一句自然语言直接落成参数；
  - `drpilot -i` —— 缺参数时逐项向用户提问（TTY 下缺参数也会自动问），补齐后确认再跑；
  - 每次运行写 `logs/drpilot-<时间>.log`，JSON 里回传 `log_file`；
  - 出错一定有 `error_code` / `phase` / `context` / `suggestion`，批量失败还给
    `failed_question_numbers` 与可直接续跑的 `retry_command`。
- **界面排版继续收窄**：参数区按内容自适应、日志吸收剩余高度，卡片内不再出现大片空白；
  两栏按「任务范围 + 手机连接 / 模型服务 + 识别与输出」重新分组，常用项都在一屏内。

## 从 GitHub 克隆后，3 步跑起来

```bash
git clone <仓库地址> DrPilot_V4 && cd DrPilot_V4
uv sync                 # 1) 建虚拟环境 + 按 uv.lock 装依赖（需要 uv）
uv run drpilot doctor   # 2) 自检：能不能跑、缺什么、怎么修，它会逐条说
uv run drpilot --gui    # 3) 打开图形界面（命令行用法见下面「命令行用法」）
```

前置条件只有三样：

1. **Python 3.13+** 与 **[uv](https://docs.astral.sh/uv/)**（`pip install uv`，或按官网安装脚本）；
2. **Android platform-tools**：`adb` 在 PATH 里（或用 `--adb` 指定路径），手机开启 USB 调试并授权；
3. **API Key**：图形界面「模型服务 → API Key」里粘贴后点「＋ 新增 API Key」，
   或复制 `.env.example` 为 `.env` 自己填。

图形界面还需要 pywebview（`uv sync` 会自动装）和 Windows 上的 Edge WebView2 运行时（Windows 11 自带）。
只用命令行则这两样都不需要。

> 仓库里**不含** `.env`、`drpilot_config.json`、`logs/`、`.venv/`：克隆下来是干净源码，
> 密钥与本地配置都由你自己生成，且已被 `.gitignore` 忽略。

## 输出物

以教材「药理学」、章节「第二章 药物代谢动力学」为例，在目标文件夹里生成：

| 文件 | 用途 |
| --- | --- |
| `药理学_02_药物代谢动力学.jsonl` | 机器读取：一行一题，每行自带教材/章节/题号 |
| `药理学_02_药物代谢动力学.md` | 人阅读：按固定格式排版 |
| `index.json` | 索引：教材 → 章节 → 文件 → 已有题号列表 |

### JSONL（主产物）

每行一个 JSON 对象，不带代码块标记：

```json
{"id": 16, "textbook": "药理学", "chapter": "第二章 药物代谢动力学", "chapter_no": 2, "total": 53, "type": "single", "stem": "...", "options": {"A": "..."}, "answer": "A"}
```

- `id`：**以截图右上角显示的数字为准**（显示 `16/53` 就是 16），不是程序顺序号。
- 每行自包含教材/章节信息，AI 拿到任意一行都知道出处；文件改名、单独取用也不丢上下文。
- 同一章节再次提取时按 `id` 合并：漏了第 17 题，就单独提取第 17 题，其他题不受影响。

### Markdown

严格按指定格式（选项行末尾两个空格，答案用三级标题加粗）：

```markdown
## 第 1 题 【单选题】
药物被动转运的特点不包括
A. 需要载体  
B. 顺浓度梯度转运  
### **答案：A**
```

### index.json

```json
{
  "version": 1,
  "generated_at": "2026-09-10 10:00:00",
  "entries": [
    {
      "textbook": "药理学",
      "chapter": "第二章 药物代谢动力学",
      "chapter_no": 2,
      "total": 53,
      "file": "药理学_02_药物代谢动力学.jsonl",
      "markdown_file": "药理学_02_药物代谢动力学.md",
      "count": 20,
      "question_ids": [1, 2, 3, 4, 5],
      "updated_at": "2026-09-10 10:00:00"
    }
  ]
}
```

AI 先读 `index.json` 就能定位到具体文件和题号，不必把整个题库塞进上下文。

## 题号规则（重要）

- 题号以 AI 从截图右上角读到的数字为准；程序里的「预期题号」只用于提示不一致。
- 开始前会先截一张图做**预检**：若屏幕显示第 47 题、而起始题号填的是 1，会弹窗（CLI 报错）让你确认，
  避免整批编号错位。
- 因此可以从任意一题开始提取；漏题时只补录那一题，后面的题号不会被带偏。
- 提示词里刻意不出现任何具体题号数字——旧版本示例中的 `47/81` 会被小模型照抄成结果，
  这就是之前 1~20 题里前三题变成 47/48/49 的原因。

## 分辨率自适应与滑动校准

滑动坐标以「基准分辨率」为参照，运行时按连接设备的实际分辨率（`adb shell wm size`）等比缩放：

- **首次运行**会把当前设备分辨率自动记录为基准并写入 `drpilot_config.json`，日志会打印
  `已自动记录滑动基准分辨率：1080x2340`。
- 同一台手机上坐标原样使用，行为和你之前调好的固定坐标完全一致。
- 换机型时自动换算：基准 `1080x2340`、实际 `1440x3200` 时，
  `1060,553 → 270,551` 会变成 `1413,756 → 360,754`（x 按宽度、y 按高度分别缩放，
  超出屏幕的坐标会夹回 0 ~ 宽/高-1）。
- 手动指定基准：`--swipe-reference 1440x3200`；恢复自动记录：`--swipe-reference auto`。

## 翻页动作录制（适配点击 / 滑动）

不同刷题 App 切换下一题的方式不一样（有的点按钮、有的滑动、有的先看广告再点关闭），
所以除了固定滑动坐标，还可以**录一次真实动作**，之后每次都回放这一套：

1. GUI 里展开「翻页动作（点击 / 滑动）」，点 **开始录制**；
2. 在手机上正常完成一次「下一题」动作（点 / 长按 / 滑动都行；多步动作请连续操作）；
3. 点 **停止录制**（或录到第一个动作后静默 2.5 秒自动结束），界面会列出识别出的步骤；
4. 点 **试一次**：程序回放这套动作并对比前后截图，提示「画面已变化 ✓ / 没有变化 ✗」；
5. 动作与基准分辨率会写进 `drpilot_config.json` 的 `next_action` 字段，换机型按基准自动缩放。

要点：

- 录制走 `adb shell getevent` 读真实触摸事件，不需要 root、不需要手机端 App；
- 点按会保留真实按压时长（长按不会被当成轻点），滑动保留起止点与时长；
- 支持多步：比如「点箭头 → 等待 300ms → 点广告关闭」，一次录制、一次回放；
- 步骤在配置里是白名单数据（`tap / swipe / path / key / wait`），不会被当成 shell 命令执行；
- 设备不支持录制（`getevent` 权限受限 / 找不到触摸屏）时，界面会提示改用「手动点按 X/Y」或下方手动滑动坐标，不会静默失败；
- 支持方向键翻页的 App，可以在动作文件里写 `{"kind": "key", "keycode": "KEYCODE_DPAD_RIGHT"}`；
- **录制和试动作都会让手机前进一题**，开始提取前记得把手机切回起始题（预检也会拦一次）。

命令行等价用法：

```bash
# 用 GUI 录好导出（或手写）的动作文件；参考分辨率可省略（用 --swipe-reference 指定）
drpilot --from 1 --to 20 --output-dir D:/题库 --textbook 药理学 --chapter "第二章 药物代谢动力学" \
    --next-action-file D:/actions/click-next.json

# 纯点击类 App：直接给一个坐标，不必录制
drpilot --from 1 --to 20 --output-dir D:/题库 --textbook 药理学 --chapter "第二章 药物代谢动力学" \
    --tap 630,2380

# 多步动作：--tap 可重复
drpilot ... --tap 630,2380 --tap 630,2380
```

> `--tap` 与界面里的「手动点按」写的是**当前设备像素**，会带 `absolute: true` 不做缩放；
> 录制得到的动作则以录制分辨率 + `reference` 为基准缩放。

动作文件格式（`reference` 与 `steps` 都可省略）：

```json
{
  "reference": "1260x2720",
  "steps": [
    {"kind": "tap", "x": 630, "y": 2380, "duration_ms": 80},
    {"kind": "wait", "wait_ms": 300},
    {"kind": "swipe", "x": 1000, "y": 1300, "x2": 200, "y2": 1300, "duration_ms": 260}
  ]
}
```
- GUI 的「滑动设置」面板可以改起点/终点坐标、时长、重试次数和基准分辨率：
  「设为当前设备」把当前分辨率写成基准，「试滑一次」会执行一次滑动并对比前后截图，
  提示「画面已变化 ✓ / 画面没有变化 ✗」，换机校准不用盲试。

## 安装（uv 管理）

```bash
uv sync              # 按 uv.lock 安装运行依赖（Python 3.13+），并创建 .venv
uv sync --group dev  # 开发额外装 pytest / ruff
uv run drpilot --version
```

依赖一律用 uv 管理：加包用 `uv add <包>`，删包用 `uv remove <包>`（会自动更新 `uv.lock`）。
不建议 `pip install` 混装，也不建议手改 `uv.lock`。

> **不要让 WSL 和 Windows 共用同一个 `.venv`**（本项目在 `D:` 盘时很容易踩到）。
> 在 `/mnt/d/...` 这种共享目录里，两边各跑一次 `uv sync` 会互相覆盖：WSL 建的 `.venv` 里有
> Linux 符号链接（`lib64`、`bin/python` 等），Windows 删不掉这些 reparse point，
> 于是报 `error: failed to remove file ...\.venv\lib64: 拒绝访问 (os error 5)`。
>
> 解法：删掉冲突的 `.venv` 再重建——WSL 里 `rm -rf .venv`，Windows 里 `rmdir /s /q .venv`，
> 然后只在你要用的那一边 `uv sync`。如果确实要在 WSL 里单独跑测试，把环境放到仓库外：
> `UV_PROJECT_ENVIRONMENT=$HOME/.venvs/drpilot uv sync --group dev`。

还需要：

1. 安装 Android platform-tools，确保 `adb` 在 PATH 中，或用 `--adb` 指定路径；
2. 手机开启 USB 调试并授权；
3. 图形界面还需要 **pywebview**（`uv sync` 会自动装）和 **Edge WebView2 运行时**：
   Windows 11 自带；Win10 若提示缺失，装一次 Microsoft Edge WebView2 Runtime 即可。
   只用命令行（`drpilot --from …`）则不需要这两样。

4. 配置 API Key，两种方式任选：

   **方式一（推荐，不用碰文件）**：打开图形界面，在「模型服务」卡片的 **API Key** 输入框里粘贴
   （默认隐私输入模式，点小眼睛才临时显示明文），点「＋ 新增 API Key」——会写进项目根目录的 `.env`；
   已保存的 Key 会以脱敏形式列在下面，点每个后面的「✕」即可删除。

   **方式二（手动）**：复制 `.env.example` 为 `.env`，填入：

   ```dotenv
   SILICONFLOW_API_KEY_1=sk-xxxx
   SILICONFLOW_API_KEY_2=sk-yyyy
   ```

> API Key 只从环境变量 / `.env` 读取，永远不会写进 `drpilot_config.json`、源码或日志。
> `.env` 已在 `.gitignore` 里；写入后会尽力把文件权限收紧到 0600（Windows 上不支持则跳过）。
> 多个 Key 会分配给不同 worker 并发使用，并发数不要超过 Key 数量。

## 快速开始

1. 手机打开医考帮，进入教材章节，停在起始题，切到背题模式（正确选项前有勾选圆圈）。
2. 图形界面：

   ```bash
   uv run drpilot --gui
   ```

   填好教材、章节、目标文件夹后点「开始」。若用无线调试，先在「手机连接」里填手机 IP 与端口，
   点「连接」，顶部胶囊变成「设备：1 台在线」即可（比在 PowerShell 里敲 adb connect 省事）。

3. 命令行：

   ```bash
   uv run drpilot --from 1 --to 20 --output-dir D:/题库 \
       --textbook 药理学 --chapter "第二章 药物代谢动力学"
   ```

## 命令行用法

> 所有命令都可以用 `drpilot <子命令>` 或等价的扁平写法：`drpilot doctor` ≡ `drpilot --doctor`。

### 先自检：drpilot doctor

跑任务之前先自检，它会一次性检查 **Python 版本 / 依赖包 / API Key / 配置 / ADB 设备 / 输出目录 / 模型连通性**，
每项失败都给出可直接照做的修复建议：

```bash
drpilot doctor            # 人读；遇到第一个失败就停（附「还没查的项」）
drpilot doctor --full     # 7 项全查
drpilot doctor --json     # 机器读：stdout 只有一份 JSON，日志走 stderr
```

### 退出码（脚本与 Agent 靠它判断下一步）

| 码 | 含义 | 该怎么办 |
| --- | --- | --- |
| 0 | 成功 | — |
| 1 | 运行期失败（批量失败 / 掉线 / 写盘失败） | 看输出里的具体题号，用 `--from N --to N` 补录 |
| 2 | 参数或配置错误 | 按错误里的 `missing` / 提示改参数，**别原样重试** |
| 3 | 环境不可用（缺依赖 / adb / Key / 模型下架） | 按 `doctor` 的建议修环境 |
| 4 | 前置检查未通过（预检题号不一致） | 把手机停在正确题目，或确认后加 `--force-start` |

### 让 Agent 自己驱动：schema / ask / --text

这三件套是给「Agent 读用户的一句话，然后跑命令」设计的：

| 命令 | 作用 |
| --- | --- |
| `drpilot schema --json` | 完整契约：每个参数的类型/必填/示例/关键词、退出码、危险参数、常用套路 |
| `drpilot ask --json` | 只回答三件事：还缺什么、该问用户什么、凑齐后跑什么命令（`ready` / `missing` / `questions` / `command`） |
| `drpilot --text "…"` | 把一句自然语言解析成参数；解析不出来的一律不猜，显式命令行参数优先级最高 |
| `drpilot -i` | 缺参数时逐项向用户提问（TTY 下缺参数会自动进入），最后确认一次再跑 |

例子：

```bash
# 用户说：「药理学第二章，47 到 81 题，存到 D:\题库」
uv run drpilot ask --text "药理学第二章 47到81题，存到 D:\题库" --json
# -> ready: true, command: uv run drpilot --from 47 --to 81 --output-dir "D:\题库" ...

# 参数不够时，CLI 会告诉 Agent 该问用户什么
uv run drpilot ask --from 47 --json
# -> questions: ["到第几题结束？（包含这一题）", "提取结果存到哪个文件夹？"]
#    ask_user: "请补充：① 结束题号（--to）：… ② 输出文件夹（--output-dir）：…"

# 直接按自然语言跑（先 dry-run 看会输出到哪）
uv run drpilot --text "先别跑，看看配置 1到20题 存到 D:/题库 药理学第二章" --dry-run --json
```

### 报错与日志：Agent 怎么定位问题

- 每次运行都写一份日志：`logs/drpilot-<日期>-<时间>-<pid>.log`（目录已 gitignore）。
  JSON 结果里的 `log_file` 就是它的路径；用 `--log-file PATH` 可以指定，`--no-log-file` 关掉。
  每行格式：`2026-09-29 11:13:59.123 | ERROR | batch | [W1] 批次 2 失败：HTTP 429（涉及题号 [5, 6]…）`
- 失败时 JSON 一定有：
  - `error_code`：机器可读错误码（`missing_required_arguments` / `missing_api_key` / `adb_error` /
    `ai_error` / `output_error` / `precheck_failed` / `config_file_invalid` …）；
  - `phase`：出错阶段（`args` / `config` / `env` / `connect` / `preflight` / `capture` / `batch` / `write`）；
  - `context`：如 `question_numbers` / `worker` / `batch` / `error_type`；
  - `suggestion`：下一步怎么办；
  - 批量失败还带 `failed_question_numbers` 和可直接复制的 `retry_command`。
- `--debug` 会在日志里记 DEBUG 详情与堆栈，并把 `traceback` 放进 JSON。
- 日志里的 Key 一律脱敏（连命令行回显都会先过一遍掩码），不会出现明文。

### 常用命令

```bash
# 列出设备
drpilot --devices

# 只测模型能不能调用（平台下架模型时能第一时间发现）
drpilot --check-model

# 无线调试：先 adb connect，再列设备 / 再跑任务
drpilot --connect 192.168.1.5:5555 --devices
drpilot --connect 192.168.1.5:5555 --from 1 --to 20 --output-dir D:/题库 --textbook 药理学 --chapter "第二章 药物代谢动力学"

# 提取 1~20 题，自动生成 药理学_02_药物代谢动力学.jsonl/.md + index.json
drpilot --from 1 --to 20 --output-dir D:/题库 --textbook 药理学 --chapter "第二章 药物代谢动力学"

# 只补录第 17 题（会和已有文件按题号合并）
drpilot --from 17 --to 17 --output-dir D:/题库 --textbook 药理学 --chapter "第二章 药物代谢动力学"

# 不生成 Markdown / 不生成索引
drpilot --from 1 --to 20 --output-dir D:/题库 --textbook 药理学 --chapter "第二章 药物代谢动力学" --no-md --no-index

# 屏幕题号与起始题号不一致时强制继续
drpilot --from 1 --to 20 --output-dir D:/题库 --textbook 药理学 --chapter "第二章 药物代谢动力学" --force-start

# 用显式前缀代替教材/章节命名（高级用法）
drpilot --from 1 --to 20 --output ch6

# 启动图形界面
drpilot --gui
python main.py --gui
python drpilot_v4.py --gui

# Agent / 脚本友好：stdout 只出 JSON（日志走 stderr）
drpilot devices --json
drpilot check-model --json
drpilot --from 1 --to 20 --output-dir D:/题库 --json

# 只校验配置并算出输出路径，不连手机、不调 AI（改参数时先跑这个）
drpilot --from 1 --to 20 --output-dir D:/题库 \
    --textbook 药理学 --chapter "第二章 药物代谢动力学" --dry-run --json

# 用一句自然语言给参数（解析不出来的部分仍会以问题形式返回）
drpilot --text "药理学第二章 47到81题，存到 D:\\题库" --dry-run --json

# 只问不跑：还缺什么参数、该问用户什么、凑齐后跑什么命令
drpilot ask --from 47 --json
drpilot ask --text "药理学第二章 47到81题，存到 D:\\题库" --json

# 缺参数时让 CLI 自己问用户（TTY 下缺参数也会自动进入）
drpilot -i

# 机器可读的完整契约：参数 / 退出码 / 危险参数 / 常用套路
drpilot schema --json
```

常用参数（完整列表见 `drpilot --help`）：

| 参数 | 说明 | 默认 |
| --- | --- | --- |
| `--from` / `--to` | 起始 / 结束题号（章节内） | 必填 |
| `--textbook` | 教材名称，如 `药理学` | 空 |
| `--chapter` | 章节名称，如 `第二章 药物代谢动力学` | 空 |
| `--chapter-no` | 章号；不填则从章节名解析（支持中文数字） | 自动 |
| `--chapter-total` | 本章总题数；不填则用预检读到的值 | 自动 |
| `--output-dir` | 目标文件夹，文件名自动生成 | 与 `--output` 二选一 |
| `--output` | 显式输出前缀，覆盖自动命名 | 与 `--output-dir` 二选一 |
| `--no-md` | 不生成 Markdown | 生成 |
| `--no-index` | 不生成 index.json | 生成 |
| `--no-preflight` | 跳过起始题号预检 | 启用预检 |
| `--force-start` | 预检不一致时仍继续 | 否 |
| `--model` | 模型 ID | `Qwen/Qwen3.5-4B` |
| `--base-url` | API 地址 | `https://api.siliconflow.cn/v1` |
| `--batch-size` | 每次请求发送的截图数 | 3 |
| `--workers` | 并发数；超过 API Key 数量时多出的 worker 轮换共用 Key（可能限流，会重试） | 2 |
| `--wait` / `--wait-ms` | 滑动后等待时间（秒 / 毫秒） | 1.0 秒 |
| `--swipe-start-x/y` `--swipe-end-x/y` `--swipe-duration` | 滑动坐标与时长 | 1060,553 → 270,551，150ms |
| `--swipe-reference` | 基准分辨率 `WxH`；`auto` 表示首次运行自动记录 | 自动 |
| `--next-action-file` | 翻页动作 JSON（`{"reference":"WxH","steps":[…]}` 或裸步骤数组） | 空 |
| `--tap` | 追加一个点按坐标，可重复，如 `--tap 630,2380` | 空 |
| `--max-retries` `--retry-delay` `--retry-backoff` | API 重试策略 | 5 / 1.0 / 2.0 |
| `--request-timeout` | 单次 API 请求超时秒数 | 60 |
| `--request-deadline` | 单次 AI 调用的总时间预算（含重试），到点就放弃，避免某个 worker 永久卡住 | 自动（超时 x2 + 10） |
| `--max-swipe-retries` `--retry-wait` | 重复截图重试 | 5 / 1.2 |
| `--serial` | 指定设备（多设备时才需要） | 自动选择唯一在线设备 |
| `--connect` | 运行前先 `adb connect IP:端口`（无线调试） | 空 |
| `--check-model` | 只测试模型连通性并退出 | — |
| `--no-model-check` | 运行前不测试模型连通性 | 会测试 |
| `--adb` | adb 可执行文件路径 | PATH |
| `--config` | 配置文件路径 | `drpilot_config.json` |
| `--doctor` | 环境自检并退出（等价子命令 `drpilot doctor`） | — |
| `--json` | stdout 只输出 JSON 结果，日志走 stderr | 关 |
| `--full` | 配合 `--doctor`：不在第一个失败处停下 | 关 |
| `--dry-run` | 只校验配置、算输出路径，不连手机不调 AI | 关 |
| `--text TEXT` | 一句自然语言需求，自动解析成参数（显式参数优先） | 空 |
| `-i` / `--interactive` | 缺参数时逐项提问补齐（TTY 下缺参数也会自动问） | 关 |
| `-y` / `--yes` | 交互模式不再二次确认，直接开始 | 关 |
| `--log-file` | 指定运行日志路径（默认 `logs/drpilot-<时间>.log`） | 自动 |
| `--no-log-file` | 不写运行日志文件 | 写日志 |
| `--debug` | 日志记 DEBUG 与堆栈，JSON 带 `traceback` | 关 |
| `--schema` | 打印机器可读契约后退出（等价子命令 `drpilot schema`） | — |
| `--ask` | 只报告缺什么参数后退出（等价子命令 `drpilot ask`） | — |

## GUI

`drpilot --gui` 打开图形界面：HTML/CSS 渲染的新拟物界面（pywebview + Edge WebView2），
窗口大小按屏幕自适应。

- 顶栏：状态胶囊（设备 / 模型，绿=可用、红=不通）+ 退出
- 工具栏（一行）：开始提取 / 停止 / 进度条 / 当前题号 / 状态 / 每个 worker 的进度。
  点「停止」= 不再截新图，**并丢掉还没开跑的批次**，只等已经开始的那几个批次收尾
  （已识别的结果会保留，可随时重跑续录）；状态显示「已停止」，蓝色奔跑的点阵马也会立刻停下
- 左栏
  - **任务范围**：题号范围（实时显示共几题）、教材、章节、章号、本章总题数、输出目录（「选择…」），
    下方实时预览会生成哪些文件 —— 改题号/教材/章节/目录，或切换右栏的「生成 Markdown / index.json」，
    这一行都会立刻跟着变
  - **手机连接**：手机 IP + 端口（默认 5555），「连接」自动 `adb connect`、「断开」、「刷新设备」，
    以及设备列表与分辨率
- 右栏
  - **模型服务**：模型 ID、API 地址、**测试连通性** 与最近一次自检结果；同一张卡片里就是 **API Key 管理**：
    - 输入框默认**隐私模式**（圆点），点右侧小眼睛才临时显示你输入的内容；
    - 「**＋ 新增 API Key**」把输入框里的 Key 追加进 `.env`（可多行 / 逗号分隔，没保存过的才写入）；
    - 下方「已保存」列表是脱敏后的 Key，每个后面的「✕」**删除**单个，「全部删除」清空（都有二次确认）；
    - 新增/删除后，右栏「并发」旁边的 Key 数量提示会立刻跟着变。
  - **识别与输出**：批大小、并发数（旁边显示已加载的 Key 数量，并发超过 Key 数会变黄提醒「多出的共用」）、
    翻页等待；开关：生成 Markdown、生成 index.json、开始前预检题号、运行前测试模型
  - **翻页动作（点击 / 滑动）**（可折叠）：**开始录制 / 停止录制 / 试一次 / 清除动作** 与步骤列表，
    手动点按兜底（X、Y、按住 ms）；下方保留手动滑动坐标（无录制时使用）、重试与基准分辨率，
    「设为当前设备」「试滑一次」
- 底部：**日志**固定可见，错误红 / 警告黄 / 成功绿

界面不依赖 Python 后端也能看：没有桥的时候页面自动进入「界面预览模式」，
所以直接双击 `src/drpilot/webui/assets/index.html` 就能预览版面。

运行参数会保存到 `drpilot_config.json`（不含 API Key）。运行日志写在 `logs/`（已 gitignore）。

界面布局是「参数区自适应高度 + 日志吸收剩余高度」：参数区内容多时它自己滚动，日志区始终可见、
不会被挤没；卡片之间没有装饰性留白。窗口最小 980x660，窄屏时两栏自动并成一栏。

## 界面里的点阵马（背景动效）

日志区右下角有一只点阵马，用 `D:/running-pixel-horse` 的**位移模糊版**算法渲染
（位移采样 → 多点模糊 → Bayer 8×8 有序抖动 → 阈值 → 像素点，点始终待在网格上）：

- **未运行**：粒子灰色、马儿静止（起势那一帧）；
- **开始提取**：粒子变成界面同色系的蓝、马儿奔跑，奔跑时叠加一点水平位移模糊；
- 画在日志文字**下面**，并做了径向遮罩淡出，长时间日志压在上面也照样看得清；
- 只有跑任务时才走 requestAnimationFrame，空闲不占 CPU；系统开了「减少动态效果」时只静态显示。

素材是把 96 帧 1280×720 的序列压成的一张 240×136 精灵图（`assets/horse-sheet.png`，约 375 KB），
需要重新生成时：

```bash
python tools/build_horse_sheet.py --src D:/running-pixel-horse/video
```

> 实现细节：因为要用 canvas 读像素做抖动，界面必须同源。pywebview 那边传的是本地**路径**而不是
> `file://`，它会自动起一个 `http://127.0.0.1:端口` 的本地服务，这样 `getImageData` 才不会被判跨域。

## 项目结构

```
src/drpilot/
  actions.py   翻页动作：录制（getevent）、缩放与回放（tap / swipe / 多步）
  adb.py       ADB 设备、截图、滑动、点按 / 按键、无线连接（adb connect）
  ai.py        AI 调用与重试、消息构建、模型连通性自检
  config.py    配置模型 / 教材章节命名 / 路径解析
  keys.py      API Key 的解析、脱敏与安全落盘（.env）
  doctor.py    环境自检：Python/依赖/Key/配置/设备/输出目录/模型
  exitcodes.py 统一退出码（Agent 与 CI 依赖的稳定契约）
  images.py    截图转 JPEG base64
  models.py    题目数据结构
  output.py    JSONL / Markdown / index.json 输出与按题号合并
  parser.py    AI 返回解析、屏幕题号解析
  pipeline.py  自检、预检、截图与识别编排
  prompts.py   系统提示词
  cli.py       命令行入口
  webui/       HTML/CSS 图形界面（pywebview）
    bridge.py  前端 ↔ Python 的桥：状态、日志、后台线程、提问（不依赖 pywebview，可单测）
    app.py     创建窗口、启动事件循环
    assets/    index.html / style.css / app.js（新拟物样式在这里）
  contract.py  CLI 契约（schema）：参数元数据、退出码、常用套路
  nlparse.py   自然语言 -> 参数（只解析有把握的）
  interactive.py 交互式补参：缺参数时逐项问用户
  runlog.py    运行日志：时间戳 / 阶段 / 密钥脱敏，供 Agent 定位问题
tests/         单元测试
tools/
  ui_preview.py  界面预览：用无头 Edge 渲染界面截图，调版面用（不需要真机与 API Key）
  ui_selftest.py 界面自检：无头浏览器里把每个按钮点一遍，确认都能调到 Python 接口、点阵马画得出来
  build_horse_sheet.py  把 96 帧马序列压成界面用的精灵图
```

## 测试与静态检查

```bash
uv run python -m unittest discover -s tests -v    # 全部单元测试
uv run pytest -q                                 # 也可以用 pytest 跑
uv run ruff check src tests                      # 静态检查
```

测试用 mock 替代 adb 与 AI，不需要真机、不需要 API Key，也不需要开窗口
（`tests/test_webui_bridge.py` 直接测界面桥：连接状态、日志、提问、参数解析）。

想直接看界面长什么样（用 Edge / Chrome 无头模式渲染，不接手机、不调接口）：

```bash
uv run python tools/ui_preview.py                 # 1180x920 预览图
uv run python tools/ui_preview.py --expand        # 展开「翻页动作（点击 / 滑动）」面板
uv run python tools/ui_preview.py --size 1024x700 # 小窗口
uv run python tools/ui_preview.py --idle          # 未运行时的样子（灰色静止的点阵马）

uv run python tools/ui_selftest.py                # 界面自检：每个按钮都要能调到后端，且无脚本异常
```

`ui_selftest.py` 会先在静态层面检查 id 是否重复 / 是否引用了不存在的控件，再在无头浏览器里用假后端
把按钮点一遍 —— 界面「点了没反应」这类问题会在这里被抓住。

## 常见问题

- **题号错乱**：见上文「题号规则」。现在题号以截图为准，提示词里也不再出现具体数字。
- **界面打不开 / 提示 WebView2**：图形界面用 Edge WebView2 渲染，Windows 11 自带；Win10 装一次
  Microsoft Edge WebView2 Runtime 即可；也可以先用命令行。
- **界面按钮点了没反应**：界面脚本一旦出错会立刻弹提示并写进日志（不会静默失败）。
  若仍有异常，跑 `python tools/ui_selftest.py` 定位，并把输出发出来。
- **某个 worker 一直卡在「处理批次 0」**：说明它那次请求一直没返回（服务端排队，或模型太慢）。
  现在每次调用都有**总时间预算**（默认 = `--request-timeout` × 2 + 10 秒，可用 `--request-deadline` 调整），
  到点就放弃这次调用并重试；重试完仍失败就记占位并继续，不会再永久卡住。
  如果日志里频繁出现「用了 90s」，说明模型太重 —— 换成更小的视觉模型（例如默认的 `Qwen/Qwen3.5-4B` 一档），
  或把 `--batch-size` 调小到 1~2 降低单次请求耗时。自检延迟 > 5s 时，模型卡片上也会直接给出提醒。
- **worker 明明在干活，输出文件夹却是空的**：正常情况下**每完成一个批次就立刻写盘**（按题号与已有文件合并），
  某个批次慢、在重试，也不会挡住其它批次落盘。如果你还看到长时间空文件，请把日志发出来 ——
  写入失败会以 `[Wn] 写入失败：…` 的形式显示在日志里。
- **点了停止，日志还在写、马还在跑**：已经排队的批次会被丢弃（现在是这样），但**已经发给模型的请求**
  会做完再退出——这是为了不浪费已经花掉的调用，通常只有几个批次、几秒钟。若长时间不停，
  多半是单个请求在超时重试（可调小 `--request-timeout` / `--max-retries`）。
- **并发调到 3，却只出现 W1、W2**：以前并发数会被 API Key 数量静默压住（2 个 Key 最多 2 个 worker）。
  现在按你填的并发数启动，多出来的 worker 轮换共用同一个 Key：界面上「并发」旁边会显示
  「2 个 Key，多出的共用」，日志里也有提示。共用 Key 可能触发 429 限流（会自动重试），
  想真正提速就在 `.env` 里再加一个 `SILICONFLOW_API_KEY_3`。
- **未找到 adb**：安装 platform-tools 并加入 PATH，或 `--adb C:/platform-tools/adb.exe`。
- **未检测到设备**：检查 USB 调试授权；无线调试需先 `adb connect`。
- **未找到 API Key**：确认 `.env` 在项目根目录，或已设置 `SILICONFLOW_API_KEY_1`。
- **模型不可用 / 已被平台下架**：运行前自检会直接报错，GUI 顶部胶囊变红、日志给出平台上的相似模型名。
  换一个模型 ID 后点「测试连通性」确认，再开始提取；确需跳过自检可关掉「运行前测试模型」或加
  `--no-model-check`。
- **无线连接失败**：确认手机与电脑在同一 Wi-Fi、手机「开发者选项 → 无线调试」已打开，
  并填写该页面显示的 IP 与端口（部分机型每次重开无线调试端口都会变）；连接成功前 `adb devices`
  里不会出现设备。
- **预检报「屏幕显示第 X 题」**：把手机停在起始题再运行，或加 `--force-start`。
- **滑动无效**：日志里现在会打印「翻页动作：…」；没录动作时仍是旧滑动坐标。换机型已自动缩放，
  若仍无效，用 GUI 的「试滑一次」校准坐标，或 `--swipe-*` 手动调整，「设为当前设备」重置基准分辨率。
- **点击类 App 怎么用**：展开「翻页动作」，点「开始录制」后在手机上点一次「下一题」，点「停止录制」，
  再点「试一次」确认画面变化；也可以直接填「手动点按 X / Y」，或用命令行 `--tap 630,2380`。
- **录制报「没有找到触摸屏设备 / 不允许读取触摸事件」**：少数 ROM 限制 `getevent`。改用「手动点按」
  或在下方手动滑动坐标里填坐标即可，其它功能不受影响。
- **录制后题号不对**：录制/试动作会让手机前进一题，开始提取前把手机切回起始题；预检发现不一致时也会提示。
  另外录制只能在「不影响翻页」的前提下进行 —— 双指手势只录主触点，横竖屏切换后建议重录。
- **想重新提取整章**：直接扩大 `--from/--to` 范围即可，已识别题目会按题号覆盖更新。
