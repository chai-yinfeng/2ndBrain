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
uv run sb sources follow https://example.com/research # 查看范围证据与候选 key
uv run sb sources follow https://example.com/research --candidate <key> --apply
uv run sb sources follow https://example.com/ --manual-only --apply # 明确仅保存主页
uv run sb sources repair                            # 检查全部关注，默认不写配置、不调用模型
uv run sb sources repair <source_id> --apply          # 原位修复采集配置，保留关注身份与人工字段
uv run sb sources edit <source_id> --set 'name=我的博客' # 改显示名称；id=新标识会同步迁移引用
uv run sb sources analyze <source_id>                # 显式分析内容领域/性质，可能调用 cheap 模型
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

个人记录不要长期放在 `/tmp`。本机已确认的持久工作区为 `~/Library/Application Support/SecondBrain`，包含 `data/brain.db`、`data/web_tasks.db`、`data/wiki`（原件和笔记也在其中）、画像与 `config/`。迁移应停服整体复制并校验，不要重新导入或只移动数据库；已授权迁移的恢复副本位于该目录的 `migration-backups/`。

在本机以持久目录启动（不安装或修改 launchd）：

```bash
workspace="$HOME/Library/Application Support/SecondBrain"
SB_DATA_DIR="$workspace/data" SB_CONFIG_DIR="$workspace/config" \
SB_ENV_FILE="$workspace/empty.env" SB_LAUNCH_AGENTS_DIR="$workspace/LaunchAgents" \
uv run sb serve --port 8765
```

凭据沿用启动进程的环境变量，或在 Web 服务配置中设置到该工作区的 env 文件；迁移不会复制原项目 `.env` 的密钥。普通 `uv run sb serve` 不带上述覆盖时仍使用项目默认路径，不会自动切到个人工作区。

当前开发代码位于独立 worktree `2ndbrain-material-network`，分支 `m13-m15-material-network`；分支创建记录是 `2026-11-16T03:50:10-05:00`，对应用户要求的 M13–M15 隔离开发。它共享主仓库的 Git 对象，不是额外克隆。当前主目录的 main 尚未整合这些提交，上述启动命令应在这个 worktree 中执行；不要删掉它后直接用旧main打开新数据。整合代码并切换服务入口后，可用 `git worktree remove` 清理，个人工作区无需随源码移动。

`migration-backups/from-temp-20261118-041702` 保留迁移前完整副本。恢复前先停服；旧副本的来源配置含已退役类型，恢复data时应保留迁移后的正式sources配置，或先按ADR 0026转换两项类型，不能直接用旧配置启动新版代码。

阅读列表独立存储，不是推荐反馈的别名。加入/移出清单不会产生偏好反馈；登记阅读移出清单并保留材料、笔记及失败记录，失败后可重试。旧库仅把最新仍为“稍后读”的条目迁入清单，不推断其他文章已经吸收。

推荐的历史反馈仍保留在详情页辅助区域，供画像和过滤使用；不再以“有收获”作为知识整理的门槛。知识页生成、登记阅读与概念掌握度相互独立；掌握度仍由显式复习调整。

Web 上的其他页面：

- `/`：信源、阅读列表、知识库三个主入口；`/materials` 是材料存档，不等同于稍后清单。
- `/sources`：手动输入主页，优先复用已关注配置和既有适配规则，再做 RSS、HTML 列表、sitemap 通用发现。网络失败、合法空结果和手动浏览明确区分，失败不覆盖已关注配置。关注本身不导入文章、不启动推荐；不再提供临时参考目录。
- 已关注支持最近导入、首字母排序，以及列表／封面卡片切换，偏好保存在浏览器，不改配置顺序。名称和封面打开设置，右侧主页去原站。设置、识别、选文、采集检查与修复都在可关闭窗口中，长结果不挤压列表；关闭不取消后台任务。封面在设置中显式更新，优先网页封面、其次站点图标、最后名称首字母；展示本地安全缓存，不调用模型。
- `/later`：自己维护的稍后清单；可打开原站、移出清单，或进入详情补充学习记录后登记阅读。
- `/add`：链接、论文、PDF、文本共用稍后阅读、仅存档、外部已读登记；已读时可填写学习笔记和讨论链接。论文身份不明确时等待选择，保留输入，不冒充已经导入。任务入口可找回进度、结果和历史候选，详细日志默认折叠，不持续堆在表单下；服务重启后未完成任务标中断，不自动重放。
- 条目详情页：个人学习笔记、讨论引用、登记阅读、复用/强制整理、来源及完整性；机器分析与推荐反馈折叠为辅助功能。
- `/wiki`：正式知识库、已登记但整理失败的待处理材料、草稿入口；`/wiki/graph` 可直接在网页浏览图谱，无需启动 Obsidian。页面支持固定/禁止关联并保存原因，后续 AI 整理遵守人工约束，不覆盖个人笔记。
- `/ops`：设置与管理，包含用量、运行方式、API key 状态/配置、模型及画像入口；`/recommendations` 与 `/history` 保留为辅助推荐与历史。

来源的获取方式与内容领域分开：`rss` 读取订阅，`html_index` 提取列表页文章链接，`sitemap` 从网站地图枚举 URL；`arxiv / youtube` 保留平台级入口。Qwen Research 与 HF Papers 的专用采集已清退，原关注可保留为手动主页，不再列出专用获取方式。来源页免费显示匹配的既有规则标签和历史文章主题，并注明依据；“分析领域与性质”才显式采样最多10条标题/摘要并调用 cheap 模型。建议不覆盖手动标签，不读取全文或导入材料；同输入复用缓存，失败保留旧结果，用量和错误记为 `SOURCE`，沿用现有预估费用上限。有限样本分类不等于理解整个网站。

预览提示不都代表失败：`html_index` 已列出候选时，“按路径推断范围、尚未抓正文”只是中性说明；只有 `bookmark` 才表示当前仅有手动入口。关注与预览都不会自动推送或加入阅读清单。

### 下载预算与栏目范围

发现/预览下载预算独立于正文处理长度：`fetch.discovery_response_max_bytes` 默认8MB，`fetch.discovery_total_max_bytes` 默认16MB，按解压后字节计数；`content_max_chars` 仍只控制入库处理窗口。超限显示具体阶段、请求路径和“识别未完成”，不自动放开上限，也不将它解释成网站只能手动浏览。

人类 Research 页面与机器 RSS 可以是不同范围。识别会展示实际 URL 重合、分类、未覆盖/未核对链接，分别提供综合 RSS 和更窄分类候选，由你确认后采用。`rss_categories` 作用于实际后续采集；确认时的 `scope_note` 与主页一起保留。样本证据不是整站/历史等价证明，同站候选不会自动合并身份。

不再为单个网站新增专用采集器：通用发现不能可靠枚举时，可明确保存为手动浏览入口，再逐篇添加材料。原 `qwen_research / hf_papers` 配置在切换前原位迁为手动主页，保持ID、名称和历史，不留旧枚举别名。正文提取和栏目枚举是两种能力，不能从“某篇可读”推导“整站可自动订阅”。决策见 ADR 0026。

### 公开全文与导入结果

取得摘要不代表全文查找结束：现有元数据明确提供的开放PDF/arXiv和公开落地页标准PDF入口会继续尝试；身份不符拒绝合并，无法核实版本时明确标为未知。CLI/Web结果展示全文、部分正文、仅摘要或未取得正文，以及实际选中内容的地址、提取方式、版本和获取尝试。没有接入额外跨站搜索服务，不承诺发现索引之外的全部作者主页。

HTTP 403表示本次请求被拒绝，不能直接推导为必须登录。后台不自动使用浏览器Cookie、不绕验证码或付费墙。可保留链接手动浏览，或显式提供你能正常访问的正文/PDF；仅引用或摘要不会被描述成全文。


### 界面与图谱

桌面使用左侧导航，窄屏通过顶部菜单打开导航抽屉。侧栏底部「外观」提供亮色、暗色、跟随系统，默认跟随系统；偏好只存在当前浏览器，不改服务端配置。跟随系统时会立即响应系统配色变化。

阅读列表是紧凑行，支持按标题/来源即时搜索；标题进入材料详情，「原站」在外部打开，更多操作可移出清单。添加材料按链接/论文/文件/文本切换，分别保留本页输入草稿，非当前类型字段不会提交。

知识库分为知识页面和图谱。图谱使用按连接度分层的稳定布局、节点类型颜色与图例、标签降噪；点击节点后突出一跳关系并在右侧显示完整名称、别名、来源和相连页面。可切换局部中心、逐步展开、缩放、适应或重置；移动端详情移到画布下方。布局位置不表示因果、学科分类或掌握度。页面选择、筛选和主题切换不会重新布局；明确改变局部范围时只布局当前可见子图，返回全局恢复原位置。

视觉参考 shadcn/ui 的中性色、细边框和控件层级，仍使用现有 Jinja2/HTMX/Cytoscape；没有引入 React、MCP 服务、外部 CDN 或另一份知识图数据。

页面的 CSS/JS URL 带内容摘要版本；文件变化后地址自动更新，无需手动清浏览器缓存。HTML 不存储，静态文件可缓存但每次使用需验证更新（未变时返回304）。若旧标签仍显示改版前样式，重新打开页面获取新版 HTML；不需要清除个人设置或浏览器全部数据。


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
