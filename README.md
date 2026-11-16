# Second Brain

个人学习材料与知识网络系统。M13 起支持独立导入 PDF、URL 与文本、阅读和整理入 wiki；订阅与时间预算推荐保留为辅助功能。目标架构见 ADR 0021，M14–M15 的实施和验收状态见路线图。

架构见 [docs/design.md](docs/design.md)，路线图见 [docs/roadmap.md](docs/roadmap.md)。

## 快速开始

需要 [uv](https://docs.astral.sh/uv/) 与 Python 3.12+。

```bash
uv sync                    # 安装依赖
cp .env.example .env       # 填写 DEEPSEEK_API_KEY（.env 已 gitignore）
uv run sb doctor           # 检查环境与配置
```

可选：在 `config/settings.yaml` 中只写想改的键，程序会把它深合并覆盖在 `settings.example.yaml` 之上（也可以在 Web `/ops` 的"设置"里改模型与后端）。`sources.yaml` 与 `profile.md` 仍是整份文件，不存在时使用 example。

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
uv run sb sources tune          # 只读的信源统计：反馈分布、近期产出、低产出停用建议（反馈不再调整 trust）
uv run sb add --pdf paper.pdf --title "论文标题"  # 独立本地 PDF，无需 URL；默认未读
uv run sb add --text-file notes.txt --ingest     # 文本导入后整理入 wiki，不代表已读
uv run sb add https://example.com/post           # URL 与订阅共用材料服务，保留多来源
uv run sb items show <item_id>                   # 阅读原文与来源，不要求运行推荐
uv run sb items read-state <item_id> reading     # unread / reading / read
uv run sb items notes <item_id> --file notes.md  # 个人笔记独立存入同一 vault
uv run sb wiki ingest <item_id>                  # 任意材料显式整理，不要求 L2/反馈
uv run sb items incomplete      # 列出"正文不完整"的条目（只有摘要等），可在详情页粘贴正文或上传 PDF 补全
uv run sb deep <item_id>        # 按需精读单条（长内容自动分块；视频要点带时间戳链接）
uv run sb summary <item_id>     # 深度速读：读全文，写问题 / 方法 / 结果 / 局限 / 前置知识（缓存；--force 重新生成）
uv run sb prereq <item_id>      # 补前置：wiki 已有的给链接，没有的生成简短讲解（AI 生成）
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

- `/materials`：材料库，可按名称与阅读状态筛选；无信源、无 digest 也能使用。
- `/ops`：今天 / 近 7 天的 LLM 用量；"立即运行"，与 `sb run` 相同，共用 `data/run.lock`，同一时间只跑一个；运行方式与设置。
- `/sources`：信源全部由你手动维护。列表（反馈分布、产出、低产出停用建议）、预览、启用 / 停用、删除；"信源目录"列出 example 中还没添加的信源，可以逐个预览、添加。修改按条目文本块写入 `config/sources.yaml`，**保留注释与格式**；`sources: []` 是合法的空配置。
- `/add`：独立导入 URL / PDF / 文本；可选择已读、稍后读或保留原阅读状态，整理入 wiki 不要求读后反馈，不进入每日分级与预算。
- `/wiki`：体检、到期复习、知识图谱（`/wiki/graph`）。
- 首页与 `/history`：按形式（论文 / 博客 / 视频）和类型（前沿 / 讲解 / 综述 / 教程 / 观点）筛选，只影响显示。
- `/ops` 的"设置"：查看 API key 是否已设置（不显示值），写入或清除 `.env` 中的 key；测试连接；选择 cheap / strong 模型与后端（只把与 example 不同的键写入 `config/settings.yaml`）。
- 条目详情页：原文、导入来源和原文件、独立阅读状态、个人笔记、整理入 wiki；深度速读和补前置作为可选机器分析保留。

深读条目（全文时间超过预算 × `deep_read_ratio`）按速读时间（`deep_summary_minutes`，默认 15 分钟）占用每日预算，卡片同时显示全文时间（ADR 0015）。

### 用 Obsidian 打开 wiki（可选）

`data/wiki` 是一个普通的 Markdown 目录，可以直接作为 Obsidian vault 打开：

- 页面在 `pages/{materials,concepts,entities,papers}/`；材料页使用稳定 `material-<id>`，旧 paper 页和链接保留；
- 原文在 `raw/<条目 id>.md`，原文件与各次提取文本在 `resources/`；个人笔记在 `notes/material-<id>.md`，机器整理不覆盖；
- `index.md` 与 `log.md` 由 `sb wiki` 维护，请勿手动编辑。

在 Obsidian 的图谱视图里，可以在过滤框输入 `-path:raw` 隐藏原文节点，只看知识页面。

导入不等于已读，生成概念页也不等于掌握。新整理出的概念从「陌生」开始，首次显式复习前不设置复习日期。机器内容与个人笔记分开；重复整理保留已有文字。只有摘要的材料会标为 `abstract`，提取范围受限时标为 `partial`，不会冒充全文。

### 手动运行与每日自动（ADR 0016）

默认推荐**手动**：`uv run sb serve` 后打开首页，点顶部的「开始今日过滤」。

- 按钮旁写着大致耗时与费用；
- 勾选"不限时间窗（只去重）"时，抓取不按 max_age_days 截断，每个信源仍取最新 fetch_limit 条；终端里对应 `sb run --all-time`；
- 今天已运行过时，首页只留"再运行一次"入口（在 `/ops`）。

在两种方式之间切换：

- **Web**：打开 `/ops` 的"运行方式"，点"开启每日自动"（可以填时间，并提示是否落在 DeepSeek 高峰）或"改为手动"。有运行在进行时不允许改为手动，因为卸载会终止它。
- **终端**：`uv run sb schedule install --time 07:00`（每日自动）或 `uv run sb schedule uninstall`（手动），细节见下一节。

是否已安装定时任务就是开关本身，没有额外的配置项。

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
- [x] M8 深度速读与前置知识：深读按速读时间计入预算、`sb summary`、`sb prereq`、存为概念页
- [x] M9 手动运行与信源产出率：首页"开始今日过滤"、`sb run --all-time`、`/ops` 运行方式开关、速览 / 归档折叠、低产出信源
- [x] M10 筛选、信源主页与运维设置：`?form=&ctype=` 筛选、信源主页链接、`/ops` 设置 API key / 模型 / 测试连接、CSRF 与 Host 校验
- [x] M11 信源手动维护与手动添加文章：信源目录、`sb add`、`/add`、DOI/PDF 兜底
- [x] M12 html_index 与论文导入增强：跨标识去重、正文补全、`sb items incomplete`
- [x] M13 统一材料入口与独立知识整理
- [ ] M14 Wiki 知识网络与浏览主入口
- [ ] M15 论文名智能导入

详见 [docs/progress.md](docs/progress.md)。
