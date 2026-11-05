# 0017 — 形式 / 类型筛选、信源主页、运维页设置与 CSRF 防护（M10）

- 状态：已接受
- 日期：2026-11-05
- 修订：CLAUDE.md 中的配置读取规则（settings 由整份替换改为深合并）

## 背景

用户希望：

- 首页可以按"论文 / 博客 / 视频"和"前沿 / 讲解 / 综述……"筛选；
- 在 /sources 上直接打开信源主页；
- 在运维页设置 API key 与模型，不用改文件。

另一方面，Web 现在可以修改 `.env` 与配置，必须先防住跨站请求（CSRF）与 DNS rebinding。

GitHub repo 信源不在本次范围。

## 决策

### 1. 形式 / 类型筛选（只影响显示）

- 形式由纯函数 `web/filters.item_form(信源类型, URL, 提取方式)` 推导，不调用 LLM，不加 DB 字段：
  - **paper**：信源类型为 arxiv / hf_papers，或 host 为 arxiv.org，或路径以 .pdf 结尾，或提取方式为 arxiv_html / pdf；
  - **video**：信源类型为 youtube，或 URL 是 youtube.com / youtu.be；
  - **blog**：其余全部。
- 类型取 L1 的 content_type：frontier / explainer / survey / tutorial / opinion，中文标签分别为 前沿 / 讲解 / 综述 / 教程 / 观点。
- 首页与 `/history` 上有两排 chip，都是普通链接，通过 `?form=&ctype=` 组合筛选，"全部"清除该维度，非法取值视为"全部"。历史页的日期链接会带上当前筛选。
- 筛选只影响显示：
  - 预算进度条按全部条目计算；
  - tiering 与 digest 数据不变；
  - 各 lane 的计数按筛选后的条目计算，被筛空的 lane 显示空状态；
  - 有筛选时不显示概念复习（它没有形式与类型）；
  - 速览和归档保持折叠。

### 2. 信源主页

- 配置新增可选字段 `homepage`，优先使用。没配时按类型推导：

  | 类型 | 主页 |
  |---|---|
  | rss | 抓取时从 feed 的 channel `<link>` 读出，存入 `sources.homepage`（schema **v8**） |
  | sitemap | `scheme://host/` |
  | arxiv | query 中有 `cat:` 时用 `https://arxiv.org/list/<cat>/recent`，否则用 arxiv 搜索 URL |
  | hf_papers | `https://huggingface.co/papers` |
  | youtube | `https://www.youtube.com/channel/<channel_id>` |

  都拿不到时不显示链接。
- 只接受 http / https 且有 host 的链接，拒绝 `javascript:` 等 scheme。链接以 `target="_blank" rel="noopener noreferrer"` 打开。
- "进入前排"改名为"进入推荐"，tooltip 为"被分进 必读/温故/意外/值得读 的条数（不含速览、归档）"。

### 3. settings 深合并

- `config/settings.yaml` 存在时，深合并覆盖在 `settings.example.yaml` 之上：dict 递归合并，list 与标量直接替换。原来是整份替换。
- 环境变量 `SB_LLM_BACKEND` 优先级仍然最高；`.env` 仍不覆盖已存在的环境变量。
- **对现有用户没有影响**：用户目前没有 settings.yaml，读到的仍是 example。今后的 settings.yaml 只需写与 example 不同的键。
- 新增 `SB_CONFIG_DIR` 与 `SB_ENV_FILE`，可覆盖配置目录与 .env 路径，供验收和临时环境使用。测试中由 conftest 的 autouse fixture 指向临时目录。

### 4. 运维页"设置"

- **API key**：
  - 显示：只显示当前后端所需的 key（`DEEPSEEK_API_KEY`）"已设置 / 未设置"，以及来源（`.env` 还是环境变量）。来源记录在 `load_env` 中：凡是由 `.env` 写进 `os.environ` 的键都会被记下。不回显值、部分值或长度。
  - 修改：在 password 输入框中提交，写入 `.env`：
    - 只替换或追加该变量；同名的重复行一并删除；保留其他行与注释；
    - 写临时文件后 `os.replace`，权限 chmod 600；
    - 同时更新当前进程的 `os.environ`，立即生效；
    - 如果该变量来自进程启动时的环境变量，按规则 `.env` 不覆盖它：页面提示"环境变量优先，需要重启或 unset"，并且不修改 `os.environ`。
  - 空提交不做任何操作；值中含换行时拒绝。
  - 响应与异常信息只给出结果或错误类型，从不包含 key。
  - "清除"按钮需要确认，作用是删除 `.env` 中该行。
- **测试连接**：用当前后端与 cheap 模型发一个 `max_tokens=1`、不重试的请求，返回成功与否、模型和耗时。失败时只给出错误类型与状态码，例如 `AuthenticationError（HTTP 401）`，不包含响应正文或请求头。成功时记入用量，stage 为 `PING`。fake 后端直接返回成功。
- **模型与后端**：
  - cheap / strong 的选项来自价格表的 keys；backend 可选 deepseek / fake。
  - 保存时只把**与 example 不同**的键写入 `config/settings.yaml`（yaml safe_dump，原子写入），与 example 相同的键会从文件中删除。全部与 example 相同且文件不存在时，不创建文件。
  - "恢复默认"会删除这些键。
  - 设置了 `SB_LLM_BACKEND` 时，页面提示它会覆盖 backend 设置。
- **生效时机**：settings 没有任何缓存（每次请求或运行都重新读取），所以保存后立即生效。有运行在进行（run.lock 被占用）时也允许保存，并提示"下次运行生效"。

### 5. CSRF 与 Host 校验（middleware，作用于所有请求）

1. **Host**：必须是 `127.0.0.1` / `localhost` / `::1`（可带端口，不区分大小写），或 `sb serve --host X` 时的 X。否则返回 403，用来防 DNS rebinding。GET 也要校验。
2. **非 GET / HEAD / OPTIONS 的请求**：
   - 有 `Origin` 时，scheme 必须是 http(s)，且 netloc 必须与 Host 头完全一致，否则返回 403。`Origin: null` 也拒绝。
   - 没有 `Origin` 时，对 `Referer` 做同样的检查。
   - **两者都没有时放行**。这是取舍：CSRF 需要由浏览器发起，而现代浏览器对 POST 一定会带 Origin；两个头都没有的请求来自 curl、脚本或 TestClient 这类非浏览器客户端，它们本来就能直接访问本机服务，拦下它们没有安全收益，反而会让现有测试和 roadmap 里的 curl 验收命令失效。
3. 测试中 TestClient 的 Host 是 `testserver`：只在测试里通过 conftest 把它加入白名单，生产代码的白名单不包含它。

## 后果

- 正面：
  - 按形式和类型浏览，不影响分级；
  - 信源可以一键打开主页；
  - API key 与模型能在页面上配置，key 不会泄露到页面或日志；
  - Web 的所有修改操作都有同源与 Host 校验。
- 负面：
  - 非浏览器客户端不受 CSRF 校验（见上文的取舍）；
  - 页面上通过环境变量设置的 key 无法修改，只能提示；
  - 写回 settings.yaml 不保留注释（文件由程序维护，只包含少量键）；
  - 筛选状态下不显示概念复习。
- 依赖：无新增。
