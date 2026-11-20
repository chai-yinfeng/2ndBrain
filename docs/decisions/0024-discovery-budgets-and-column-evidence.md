# 0024 — 独立发现预算与可解释栏目候选

- 状态：已接受（用户授权拆正文/下载预算，并要求理解人类栏目与机器接口的关系）
- 日期：2026-11-17

## 问题与证据

旧下载上限是 min(1MB, content_max_chars × 4)，默认800000字节，错误地将正文处理窗口绑定到网站响应大小。Qwen Research 的公开文章接口本次实际约4.9MB；其 sitemap 路径还可能返回 SPA HTML，不能直接作为XML解析。

[OpenAI Research](https://openai.com/research/) 是精选概览，也展示模型发布；[官方News RSS](https://openai.com/news/rss.xml) 同时含 Research、Product、Company 等分类。路径不同不代表内容不同，同域或同名分类也不证明等价。只筛 Research 标签会漏掉页面出现的一些 Product 条目。

## 决策

1. FetchSettings 增加 discovery_response_max_bytes（默认8MB）、discovery_total_max_bytes（默认16MB），均按解压后正文计数；请求数8/重定向3跳及公网检查不变。content_max_chars 仍只控制材料处理窗口。预算耗尽为 incomplete，不假称“网站只能手动浏览”。错误保留具体阶段、去查询值的请求路径与预算，不自动放开限制。
2. sitemap 解析前检查 HTML 响应，给出明确格式诊断，不将 SPA 页面当XML报行列号。仍保留有界 index 展开。
3. 页面声明feed或同源已知feed可作候选，但候选匹配不参与来源身份合并。比较实际URL重合与RSS分类，展示更广综合RSS、更窄同名分类、冲突及未核对链接；不承诺整站、主列表或全部历史等价。页面候选链接可能含导航，不能把它们当可靠覆盖率分母。
4. 不自动采用非等价候选，用户确认后按稳定候选key重新发现/验证；客户端不能提供任意采集URL。候选内容或可用性变化则要求重新预览。rss_categories 真实作用于后续采集，scope_note 保存确认时的有限依据。分类过滤与综合RSS不按传输地址误合并，既有homepage与人工属性保持。
5. 不支持范围时仍可显式仅保存主页；下载未完成与用户选择手动入口是两个不同状态。CLI提供 --candidate/--manual-only，Web展示同样的证据和操作。不调用模型来猜站点身份或批量产生费用。
6. Qwen新增 qwen_research 专用适配，生产不执行JavaScript：复用公开前端使用的固定GET接口 retrieval(type=qwen_ai,language=en-US) 与 research.research-list(language=en)。旧配置只取官方0.0.91的53项Uv展示白名单，再合并新接口，按可靠日期降序；本次共94条，预览至多50条。旧qwenlm.github.io/blog RSS范围和更新不同，保留为独立来源。
7. Qwen文章URL导入也复用公开接口匹配真实内容，避免存SPA导航；新文章实际HTML可提取，旧元数据摘要明确为摘要。标准Qwen文章ID不存在时明确失败，不回退成假正文。

## 边界

Qwen旧展示白名单来自当前公开前端，未来接口/页面规则变化需要维护；不伪装成通用动态网页抓取。RSS分类和页面样本仅是有限证据，用户仍需决定采用的订阅范围。所有验证另用临时数据，当前用户关注/材料/模型用量不重置；未提高AI费用限制。
