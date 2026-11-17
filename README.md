# Second Brain

个人信源、稍后阅读与知识网络系统。主动关注博客/网站主页、选择值得读的材料，在原站或 ChatGPT 等外部环境阅读后登记，由 AI 增量整理进 Markdown 知识库。支持 URL、网站引用、PDF、文本与论文名；推荐保留为辅助能力。M13/M15 已验收，M14 的原生 Obsidian 剩余验收仍未完成。

架构见 [docs/design.md](docs/design.md)，路线图见 [docs/roadmap.md](docs/roadmap.md)。

## 快速开始

需要 [uv](https://docs.astral.sh/uv/) 与 Python 3.12+。

```bash
uv sync                    # 安装依赖
cp .env.example .env       # 填写 DEEPSEEK_API_KEY（.env 已 gitignore）
uv run sb doctor           # 检查环境与配置
```

可选：在 `config/settings.yaml` 中只写想改的键，程序会把它深合并覆盖在 `settings.example.yaml` 之上（也可以在 Web `/ops` 的"设置"里改模型与后端）。`profile.md` 不存在时使用 example；`sources.yaml` 不存在时表示尚未关注任何信源，example 仅供参考，不自动订阅。

离线开发 / 测试不需要 API key：

```bash
SB_LLM_BACKEND=fake uv run sb doctor
uv run pytest -q
```

## 日常使用

```bash
uv run sb serve                                      # 本地 Web：http://127.0.0.1:8000/
uv run sb sources follow https://example.com/         # 只识别主页并预览，不保存
uv run sb sources follow https://example.com/ --apply # 明确关注，不自动导入文章
uv run sb add https://example.com/post                # 获取正文并加入个人稍后清单
uv run sb add https://example.com/ --reference        # 仅保存网站引用，不请求页面
uv run sb add --pdf paper.pdf --title "论文标题"       # 本地 PDF，同样进入稍后清单
uv run sb add --text-file material.txt                # 文本材料
uv run sb paper add "PagedAttention Kwon 2023"         # 名称检索，无歧义时导入并加入清单
uv run sb paper choose <search_id> <candidate_id>     # 有歧义时明确选择候选
uv run sb reading list                               # 用户维护的稍后阅读清单
uv run sb reading add <item_id>                       # 加入清单，不调用模型
uv run sb reading remove <item_id>                    # 移出清单，保留材料
uv run sb reading read <item_id> --notes-file notes.md # 登记外部阅读并自动整理
uv run sb reading read <item_id> --discussion-url https://chatgpt.com/c/example
uv run sb paper add "完整论文标题" --read             # 导入时直接登记阅读并整理
uv run sb items show <item_id>                        # 原文、来源与阅读状态
uv run sb items notes <item_id> --file notes.md        # 仅保存个人笔记，不调用模型
uv run sb wiki graph --focus <名称或ID> --depth 2      # 正式知识网络，不包含未读草稿
uv run sb wiki relation <名称或ID> <名称或ID> pin --reason "我的理解与依据"
uv run sb wiki relation <名称或ID> <名称或ID> block --reason "不应直接关联"
uv run sb wiki relation <名称或ID> <名称或ID> reset    # 解除人工约束，不调用模型
uv run sb wiki reindex                               # 外部编辑 Markdown 后重建目录
uv run sb usage --since 7d                            # 模型用量和估算费用
```

登记阅读不要求评价或先生成 L2。原文/学习笔记不变时复用已有整理，不新增模型调用；主动加 `--force` 才强制重做。正文不完整时可补全正文，或提交学习笔记单独整理，后者明确标记“仅基于用户学习笔记”，不会冒充读过原文。讨论链接只作引用保存，不会抓取 ChatGPT 私密会话；需要纳入整理的内容请粘贴到学习笔记。

辅助能力仍可显式使用：`sb run --limit 30` / `sb digest show`（过滤和推荐）、`sb summary <id>` / `sb prereq <id>`（机器分析）、`sb wiki ingest <id>` 或导入的 `--ingest`（未读时生成草稿）、`sb wiki ingest --pending`（重试已登记阅读但未完成整理的材料）。草稿用 `sb wiki graph --drafts` 或 Web 图谱 `?drafts=1` 查看，不进入默认正式网络、复习或已知知识信号。

`sb serve` 默认只监听 127.0.0.1（`--host` / `--port` 可改）。

阅读列表独立存储，不是推荐反馈的别名。加入/移出清单不会产生偏好反馈；登记阅读移出清单并保留材料、笔记及失败记录，失败后可重试。旧库仅把最新仍为“稍后读”的条目迁入清单，不推断其他文章已经吸收。

推荐的历史反馈仍保留在详情页辅助区域，供画像和过滤使用；不再以“有收获”作为知识整理的门槛。知识页生成、登记阅读与概念掌握度相互独立；掌握度仍由显式复习调整。

Web 上的其他页面：

- `/`：信源、阅读列表、知识库三个主入口；`/materials` 是材料存档，不等同于稍后清单。
- `/sources`：添加博客/账号主页，先识别 RSS/HTML 列表/sitemap 并预览，确认后才关注；无法可靠提取时保存为手动浏览的主页，不假装存在 RSS。不执行站点 JavaScript、不绕过登录。预览文章后可逐篇加入清单，保留所选信源来源；关注本身不导入、不启动推荐。
- 信源页的参考目录默认折叠，可展开、筛选和清除筛选；只有明确选择的条目会写入 `config/sources.yaml`，原有正式配置、注释和格式保留。
- `/later`：自己维护的稍后清单；可打开原站、移出清单，或进入详情补充学习记录后登记阅读。
- `/add`：通用添加材料。URL 可获取正文或仅保存引用；也支持 PDF、文本和论文名。论文候选不明确时只询问身份/版本。
- 条目详情页：个人学习笔记、讨论引用、登记阅读、复用/强制整理、来源及完整性；机器分析与推荐反馈折叠为辅助功能。
- `/wiki`：正式知识库、已登记但整理失败的待处理材料、草稿入口；`/wiki/graph` 可直接在网页浏览图谱，无需启动 Obsidian。页面支持固定/禁止关联并保存原因，后续 AI 整理遵守人工约束，不覆盖个人笔记。
- `/ops`：设置与管理，包含用量、运行方式、API key 状态/配置、模型及画像入口；`/recommendations` 与 `/history` 保留为辅助推荐与历史。

知识整理按当前材料增量进行，不定期重写整库。继续受 `run.max_cost_usd`（example 为 $0.5）的调用前预估及已记录用量限制；这不是服务商侧硬性账单上限。预估、实际 token 用量和错误可追踪，未知用量不当成零费用。固定/禁止关联与保存笔记不调用模型。

深读条目（全文时间超过预算 × `deep_read_ratio`）按速读时间（`deep_summary_minutes`，默认 15 分钟）占用每日预算，卡片同时显示全文时间（ADR 0015）。

### 用 Obsidian 打开 wiki（可选）

`data/wiki` 是一个普通的 Markdown 目录，可以直接作为 Obsidian vault 打开：

- 页面在 `pages/{materials,concepts,entities,papers}/`；材料页使用稳定 `material-<id>`，旧 paper 页和链接保留；
- 原文在 `raw/<条目 id>.md`，原文件与各次提取文本在 `resources/`；个人笔记在 `notes/material-<id>-notes.md`，机器整理不覆盖。旧同名笔记自动迁移并更新页面引用，避免 Obsidian 将材料链接打开成笔记；
- `index.md` 与 `log.md` 由 `sb wiki` 维护，请勿手动编辑。

支持在 vault 根目录或其他知识子目录编辑页面；raw/resources/notes/隐藏目录不进入 Web 知识网络。唯一文件名保留原 ID，重复文件名使用 vault-relative `.md` 路径；重复标题/别名必须选择目标，不随机合并。编辑后可执行 `sb wiki reindex` 或点击 Web「从 Markdown 重新索引」。Obsidian 图谱可用 `-path:raw -path:resources -path:notes -file:index -file:log` 隐藏非知识文件。

`human_relations` 是本系统保存在 Markdown frontmatter 中的人工约束；Obsidian 原生图谱不理解“禁止关联”或本系统的草稿过滤。固定关联会写入普通 wikilink，但不承诺两端图完全一致。Obsidian 是闭源应用，并非知识图谱开放协议；本系统的可迁移数据是 Markdown、YAML 与链接。

导入不等于已读，生成概念页也不等于掌握。未读时显式整理生成草稿，登记阅读后进入正式网络；旧页面没有状态标记时保留为 legacy，不自动重写。新概念从「陌生」开始，首次显式复习前不设置日期。机器内容与个人笔记分开；只有摘要标为 `abstract`，提取受限标为 `partial`，不会冒充全文。

### 手动运行与每日自动（ADR 0016）

日常无需启动推荐流水线。在 `/sources` 主动选文或通过 `/add` 添加材料即可。

若需要辅助推荐，可到 `/ops` 点击「立即运行」，或使用 `uv run sb run`；`--all-time` 抓取不按 max_age_days 截断，但仍受每个信源 fetch_limit 限制。关注信源不会自行启用定时任务。

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
- `src/secondbrain/web/static/katex/`：vendored [KaTeX](https://katex.org) **0.16.22**（MIT，目录中含 LICENSE），来源 `https://registry.npmjs.org/katex/-/katex-0.16.22.tgz`；仅 JS/CSS 与 CSS 引用的字体。JS sha256 `e8d885505949f3a5f4abdd5dd0d53696bd1371ad26ffbf4f310dcd77c8cdae89`，CSS sha256 `19095127357ed6d29fe0a63a6b000c913a89f7f1963b765dd3715e97c9852e75`。Web 公式启用 `trust:false`，原始 HTML 与危险链接仍被净化。

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
- [x] M15 论文名智能导入

详见 [docs/progress.md](docs/progress.md)。
