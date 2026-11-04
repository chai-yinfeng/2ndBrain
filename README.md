# Second Brain

个人"知识过滤 + 分级推荐 + 知识 wiki"系统：从你信任的信源（博客、论文为主，之后加视频）自动采集内容，用 LLM 分层过滤、总结，按推荐等级推送到本地 Web UI；你的反馈驱动个性化；你"学了"的内容被编译进 Markdown 知识库，知识库再反过来告诉过滤层哪些是已知、哪些是新的。

架构见 [docs/design.md](docs/design.md)，路线图见 [docs/roadmap.md](docs/roadmap.md)。

## 快速开始

需要 [uv](https://docs.astral.sh/uv/) 与 Python 3.12+。

```bash
uv sync                    # 安装依赖
cp .env.example .env       # 填写 DEEPSEEK_API_KEY（.env 已 gitignore）
uv run sb doctor           # 检查环境与配置
```

可选：把 `config/*.example.*` 复制为不带 `.example` 的版本（`settings.yaml`、`sources.yaml`、`profile.md`）再按需修改；正式文件不存在时程序使用 example。

离线开发 / 测试不需要 API key：

```bash
SB_LLM_BACKEND=fake uv run sb doctor
uv run pytest -q
```

## 日常使用

```bash
uv run sb run --limit 30        # 抓取 → 过滤 → 分级，写入当天 digest（需要 DEEPSEEK_API_KEY）
uv run sb digest show           # 终端里看 digest
uv run sb serve                 # 本地 Web UI：http://127.0.0.1:8000/
uv run sb usage --since 7d      # token 与费用
```

`sb serve` 默认只监听 127.0.0.1（`--host` / `--port` 可改）。页面上的按钮（👍 / 👎 / 已读 / 稍后读 / 学了 / 需要回炉 / 已扎实）写入本地 `feedback` 表；"已读"的必读不再计入积压。

### 定时运行（macOS launchd）

```bash
uv run sb schedule install --dry-run            # 先看将写入的 plist（默认每天本地 07:00）
uv run sb schedule install --time 07:00         # 写入 ~/Library/LaunchAgents/com.secondbrain.run.plist 并 launchctl load
uv run sb schedule uninstall                    # 卸载
```

时间按本机时区解释，输出会说明换算成 UTC 后是否落在 DeepSeek 高峰时段（UTC 周一至周五 01:00–04:00、06:00–10:00，高峰全价、其余半价）。日志写到 `data/logs/run.log` 与 `run.err.log`。

不用 launchd 时可以用 cron（`crontab -e`），例如每天本地 07:00：

```cron
0 7 * * * cd /path/to/2ndbrain && /opt/homebrew/bin/uv run sb run >> data/logs/run.log 2>> data/logs/run.err.log
```

（cron 的环境变量很少，`uv` 请写绝对路径；`data/logs/` 需事先创建。）

### 第三方前端资源

`src/secondbrain/web/static/htmx.min.js` 是 vendored 的 [htmx](https://htmx.org) **2.0.4**，来源 `https://unpkg.com/htmx.org@2.0.4/dist/htmx.min.js`，sha256 `e209dda5c8235479f3166defc7750e1dbcd5a5c1808b7792fc2e6733768fb447`（BSD-2-Clause）。页面不依赖任何外部 CDN。

## 当前状态

- [x] M0 仓库与骨架：`sb --help`、`sb doctor`、配置示例、文档
- [x] M1 采集层（RSS/Atom、arXiv、HF Daily Papers）：`sb sources list`、`sb fetch`、`sb items list`
- [x] M2 LLM 后端 + 分层过滤 + 分级：`sb run`、`sb digest show`、`sb usage`
- [x] M3 本地 Web UI + 反馈 + 定时运行：`sb serve`、`sb schedule`
- [ ] M4 个性化与记忆
- [ ] M5 视频讲座与长内容
- [ ] M6 知识 Wiki 闭环

详见 [docs/progress.md](docs/progress.md)。
