# 0013 — 文章反馈的两组语义、Web 运维与知识图谱（M7）

- 状态：已接受
- 日期：2026-11-04
- 修订：ADR 0003（掌握度信号的来源）、ADR 0008（个性化规则）、ADR 0010（wiki 新页面的掌握度、raw 目录）

## 背景

M3–M6 的文章反馈有 7 种按钮（👍 / 👎 / 已读 / 稍后读 / 学了 / 需要回炉 / 已扎实），存在几个问题：

- 偏好（喜不喜欢）、阅读状态（读没读）和掌握度（会不会）混在同一排按钮里，用户不知道该点哪个，也会同时点好几个。
- "需要回炉 / 已扎实"描述的是**概念**的掌握程度，而不是某篇文章；挂在文章上，只能间接推断对应的领域。
- 日常操作（立即运行、编进 wiki、wiki 体检、信源增删、trust 调整）只能在终端里做。

默认数据库中 feedback 表为 0 行、wiki 为空骨架、`data/raw` 没有文件，因此本次改动不需要迁移数据。

## 决策

### 1. 文章反馈改为两组（按"读之前 / 读完后"区分）

| 组 | kind | 按钮 | 含义与作用 |
|---|---|---|---|
| 读之前 | `later` | 稍后读 | 进入 `/later` 列表；不计入积压、不降级、没有期限；不是偏好信号 |
| 读之前 | `dismiss` | 不感兴趣 | 负面信号；移出积压 |
| 读完后 | `too_easy` | 太浅 | 校准样例（已掌握，novelty 应低）；profile 升级的证据 |
| 读完后 | `gained` | 有收获 | 正面信号；进入"编进 wiki"队列；只能作为 陌生 → 接触过 的证据 |
| 读完后 | `too_hard` | 太难 | 校准样例（不代表不感兴趣）；profile 降级的证据；其 L2 前置知识注入 L1 |
| 读完后 | `worthless` | 没价值 | 负面信号 |

- **当前状态 = 最新的一次点击**，后点覆盖先点；再点一次当前高亮的按钮记录一条 `clear`（撤销，状态回到无）。feedback 表保留全部历史，所有统计只看当前状态。
- 读完后的四种设置 `items.read_at`（保留最早的时间）；读之前的两种与撤销清空 `read_at`。积压 = 未读的必读，且当前状态不是 later / dismiss。
- 删除文章层的 up / down / read / learned / needs_refresh / mastered。
- 各信号的用法：
  - L1 few-shot：正例 gained；反例 dismiss / worthless；校准样例 too_easy、too_hard。
  - 近期 too_hard 条目的 L2 前置知识（去重，长度上限 `personalize.too_hard_prereq_max_chars`，默认 600）放进 L1 的 user 消息，system 前缀不变。
  - profile 修改提案的证据规则：
    - 升级需要 too_easy；gained 只能作为 陌生 → 接触过 的证据；
    - 降级需要 too_hard；
    - 每次最多升 / 降一级，去掉原来"已扎实可直升扎实"的特例。
  - sources tune：gained 为正，dismiss / worthless 为负，too_easy / too_hard / later 不计。
  - wiki 队列（`--pending`）：当前状态为 gained 且尚未编入的条目。

### 2. 掌握度只在概念层调整

- 新建的概念页 mastery 一律从"接触过"起步，不再从 profile 继承；首次复习间隔为 `wiki.first_review_days`（默认 3 天）。更新已有页面时不改 mastery。
- 概念页的复习按钮在 还记得 / 模糊 / 忘了 之外增加两个：
  - "需要回炉"：mastery 改为想回炉，间隔 0，立即到期；
  - "已扎实"：mastery 改为扎实，间隔取 `max(间隔 × ease, wiki.mastered_interval_days)`，默认至少 30 天。
- `profile/loader.mastery_for` 因此不再被调用；保留未删。

### 3. 后台任务与运行锁

- `ops.py` 把 `sb run` 的完整流程、编入 wiki、应用 trust 建议抽成 CLI 与 Web 共用的函数，进度通过 echo 回调输出。
- `web/tasks.py`：进程内的简单注册表，每个任务一个线程，同一类任务同时只跑一个；页面用 HTMX 每 2 秒轮询状态片段，任务结束后停止轮询。不引入任务队列依赖。任务只保存在内存里，服务重启后清空。
- **运行锁**：`data/run.lock` 上的非阻塞 `fcntl.flock`。CLI `sb run`、launchd 定时任务、Web"立即运行"共用这把锁，已在运行时直接拒绝并提示。锁由内核持有，进程崩溃时自动释放，不会留下死锁。编入 wiki 不加这把锁（它不改 digest）。

### 4. Web 运维页面

- 首页显示"待编进 wiki：N 条"和后台 ingest 按钮，完成后列出新建 / 更新页面的链接与费用。
- `/wiki` 新增体检按钮（不调用 LLM）。
- `/ops` 页面：
  - 今天与近 7 天的用量；
  - "立即运行"按钮，附当前是否 DeepSeek 高峰与费用倍率；
  - trust 建议与"应用"；
  - 画像入口。
- `/sources` 页面：
  - 列表：最近一次抓取结果、入库条数、正 / 负反馈数。最近抓取结果来自 schema v6 新增的 `sources.last_fetch_*` 四列，由 `fetch_sources` 写入。
  - 添加：先试抓（只拉取并解析，不抓正文、不写库），确认后再保存。
  - 启用 / 停用；删除需要二次确认（确认页 + `confirm=yes`）。
- 每个会花时间或费用的按钮旁都有一行说明：做什么、大约耗时、大约费用。

### 5. sources.yaml 写回

- `/sources` 的修改写入 `config/sources.yaml`。该文件是个人配置，已在 .gitignore 中；不存在时先从 example 复制。
- 写入前用 `SourceConfig` 校验、检查 id 唯一，然后原子写入（临时文件 + `os.replace`）。
- **取舍**：用 PyYAML 整体写回会丢失注释和手工排版。我们接受这一点：页面、文件头注释和 README 都注明"字段说明见 config/sources.example.yaml"。
- `sb sources tune --apply` 与 `/ops` 的应用 trust 只做行级替换，仍然保留注释。

### 6. 知识图谱与 Cytoscape vendoring

- `/wiki/graph.json`：
  - 节点 = 所有 wiki 页面（类型、掌握度、是否今天到期、度数）；
  - 边 = 已存在页面之间的 `[[wikilink]]`（无向、去重）；
  - 指向不存在页面的链接不画（更简单，lint 会报告）。
- `/wiki/graph` 页面：
  - 颜色表示掌握度，粗边框表示今天到期，节点大小按度数；
  - 可按类型和掌握度筛选，点击节点打开页面；
  - 空 wiki 时显示引导文字。
- 前端使用 Cytoscape.js，与 htmx 一样 vendored 到 `web/static/`，运行时不加载任何 CDN：
  - 版本 **3.34.4**；
  - 来源 `https://registry.npmjs.org/cytoscape/-/cytoscape-3.34.4.tgz` 中的 `package/dist/cytoscape.min.js`。tarball 的 sha512 integrity 与 npm registry 记录一致；
  - 文件 sha256 `081591ab735b2b6a3b31a3c60f8f3a3e2fee8c92939e0c7820ad1208571b6cd8`；
  - MIT 许可，版权头保留在文件内。
- 不新增 Python 依赖。

### 7. raw 原文搬到 data/wiki/raw

- `config.raw_dir()` 改为 `data/wiki/raw`，页面中的原文链接改为 `../../raw/<id>.md`（相对 `pages/<type>/x.md`）。这样用 Obsidian 打开 `data/wiki` 时链接可以直接点开。
- raw/ 不是页面目录：lint、index 重建、图谱都不读取它。在 Obsidian 的图谱里可以用 `-path:raw` 过滤掉原文节点。
- 已有页面中的旧链接 `../../../raw/<id>.md` 仍能在 Web 中渲染为条目链接，但在 Obsidian 中会失效。默认 wiki 为空，不需要迁移。

## 后果

- 正面：
  - 按钮语义清楚，每个条目只有一个状态，可以撤销；
  - 掌握度只来自概念复习和明确的太浅 / 太难证据；
  - 常用运维操作在浏览器里就能完成，CLI 与 Web 共用同一套逻辑和同一把锁。
- 负面：
  - 旧的 7 种反馈不再被识别（默认库无数据，所以无影响）；
  - 任务状态不持久化，Web 服务重启时进行中的任务会中断（`sb run` 的锁随之释放）；
  - 通过 Web 修改信源会丢失 sources.yaml 中的注释；
  - Web 中同时运行 run 与 wiki ingest 时会并发写 SQLite，依靠 SQLite 自身的锁等待。
