# Second Brain

个人"知识过滤 + 分级推荐 + 知识 wiki"系统：从你信任的信源（博客、论文为主，之后加视频）自动采集内容，用 LLM 分层过滤、总结，按推荐等级推送到本地 Web UI；你的反馈驱动个性化；你读完"有收获"的内容被编译进 Markdown 知识库，知识库再反过来告诉过滤层哪些是已知、哪些是新的。

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
uv run sb profile update        # 根据反馈生成 profile 修改提案，只打印 diff
uv run sb profile update --apply   # 备份到 data/profile_history/ 后写入
uv run sb sources tune          # 按反馈给出信源 trust 调整建议（--apply 写入 config/sources.yaml）
uv run sb deep <item_id>        # 按需精读单条（长内容自动分块；视频要点带时间戳链接）
uv run sb wiki ingest --pending # 把标了"有收获"的条目编进 data/wiki（Obsidian 可直接打开）
uv run sb wiki due              # 今天到期复习的概念（复习：sb wiki review <slug> remembered|fuzzy|forgot|needs_refresh|mastered）
uv run sb wiki lint             # 检查断链、孤立页、缺字段
```

`sb serve` 默认只监听 127.0.0.1（`--host` / `--port` 可改）。

文章上的反馈按钮分两组（ADR 0013），写入本地 `feedback` 表：

- 读之前：**稍后读**（进入 `/later`，不计入积压、没有期限）、**不感兴趣**（负面信号，移出积压）。
- 读完后：**太浅**、**有收获**（正面信号，进入"编进 wiki"队列）、**太难**（会提示补哪些前置知识）、**没价值**（负面信号）。

每篇文章只有一个当前状态（最新一次点击），再点一次高亮的按钮即撤销。掌握度只在 wiki 概念页的复习按钮里调整：还记得 / 模糊 / 忘了 / 需要回炉 / 已扎实。

Web 上的其他页面：

- 首页：可以把"有收获"的条目编进 wiki（后台运行，显示进度和费用）。
- `/ops`：今天 / 近 7 天的 LLM 用量；"立即运行"，与 `sb run` 相同，共用 `data/run.lock`，同一时间只跑一个；信源 trust 调整建议。
- `/sources`：信源列表、试抓后添加、启用 / 停用、删除。修改写入 `config/sources.yaml`，**写回时注释不保留**，字段说明见 `config/sources.example.yaml`。
- `/wiki`：体检、到期复习、知识图谱（`/wiki/graph`）。

### 用 Obsidian 打开 wiki（可选）

`data/wiki` 是一个普通的 Markdown 目录，可以直接作为 Obsidian vault 打开：

- 页面在 `pages/{concepts,entities,papers}/`；
- 原文在 `raw/<条目 id>.md`，页面"来源"段的链接可以直接点开；
- `index.md` 与 `log.md` 由 `sb wiki` 维护，请勿手动编辑。

在 Obsidian 的图谱视图里，可以在过滤框输入 `-path:raw` 隐藏原文节点，只看知识页面。

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

- `src/secondbrain/web/static/htmx.min.js`：vendored 的 [htmx](https://htmx.org) **2.0.4**，来源 `https://unpkg.com/htmx.org@2.0.4/dist/htmx.min.js`，sha256 `e209dda5c8235479f3166defc7750e1dbcd5a5c1808b7792fc2e6733768fb447`（BSD-2-Clause）。
- `src/secondbrain/web/static/cytoscape.min.js`：vendored 的 [Cytoscape.js](https://js.cytoscape.org) **3.34.4**（知识图谱，M7），来源 `https://registry.npmjs.org/cytoscape/-/cytoscape-3.34.4.tgz` 中的 `package/dist/cytoscape.min.js`，sha256 `081591ab735b2b6a3b31a3c60f8f3a3e2fee8c92939e0c7820ad1208571b6cd8`（MIT）。

页面不依赖任何外部 CDN。

## 当前状态

- [x] M0 仓库与骨架：`sb --help`、`sb doctor`、配置示例、文档
- [x] M1 采集层（RSS/Atom、arXiv、HF Daily Papers）：`sb sources list`、`sb fetch`、`sb items list`
- [x] M2 LLM 后端 + 分层过滤 + 分级：`sb run`、`sb digest show`、`sb usage`
- [x] M3 本地 Web UI + 反馈 + 定时运行：`sb serve`、`sb schedule`
- [x] M4 个性化与记忆：反馈 few-shot、`sb profile update`、`sb sources tune`、`/profile`
- [x] M5 视频讲座与长内容：YouTube 字幕、map-reduce 精读、`sb deep`、经典回补
- [x] M6 知识 Wiki 闭环：`sb wiki ingest / lint / due / review`、`/wiki`、到期概念进温故栏、L1 已知知识
- [x] M7 反馈语义与 Web 运维：两组文章反馈、`/later`、`/ops`、`/sources`、后台任务与运行锁、`/wiki/graph`

详见 [docs/progress.md](docs/progress.md)。
