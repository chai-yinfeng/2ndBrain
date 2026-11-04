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

## 当前状态

- [x] M0 仓库与骨架：`sb --help`、`sb doctor`、配置示例、文档
- [x] M1 采集层（RSS/Atom、arXiv、HF Daily Papers）：`sb sources list`、`sb fetch`、`sb items list`
- [x] M2 LLM 后端 + 分层过滤 + 分级：`sb run`、`sb digest show`、`sb usage`
- [ ] M3 本地 Web UI + 反馈 + 定时运行
- [ ] M4 个性化与记忆
- [ ] M5 视频讲座与长内容
- [ ] M6 知识 Wiki 闭环

详见 [docs/progress.md](docs/progress.md)。
