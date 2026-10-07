# CLAUDE.md

这个仓库的操作手册只有一份：**[AGENTS.md](AGENTS.md)**。开工前请完整读一遍它，再动手。

最短路径（Claude Code 速用）：

```bash
uv run drpilot doctor --json          # 1) 环境自检：能不能跑、缺什么、怎么修
uv run drpilot ask --json             # 2) 还缺哪些参数、该问用户什么、凑齐后跑什么命令
uv run drpilot schema --json          # 3) 完整参数契约（含危险参数清单）
```

三条硬规矩（详见 AGENTS.md 第 13 节）：

1. 不要改 `uv.lock`，不要提交 `.env`，不要在输出里回显 Key 明文；
2. 不要在未确认时用 `--force-start` / `--no-preflight` / `--display wide`；
3. 用了 `--display wide` 必须确认手机显示设置复位（异常时跑 `drpilot display --reset`）。

要「考点还原 / 标准解析」这类首屏之外的内容：`--modules 考点还原,标准解析 --display wide --batch-size 2`
（见 AGENTS.md 第 7 节）。
