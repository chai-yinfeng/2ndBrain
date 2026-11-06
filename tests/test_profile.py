"""M4：反馈汇总、L1 few-shot、profile 修改提案、信源 trust 调整、/profile 页面。

全部使用 tests/fixtures/profile.md 与临时 SB_DATA_DIR / CONFIG_DIR，不触碰用户的真实数据。
"""

import shutil
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pytest
from conftest import FIXTURES
from fastapi.testclient import TestClient
from typer.testing import CliRunner

from secondbrain import config, db
from secondbrain.cli import app as cli_app
from secondbrain.llm.fake import FakeBackend
from secondbrain.llm.schemas import MasteryChange, ProfileProposal, SectionEdit
from secondbrain.models import FilterSettings, SourceConfig
from secondbrain.pipeline.run import run_pipeline, system_prompt
from secondbrain.profile.feedback import few_shot_block, polarity_examples, recent_feedback
from secondbrain.profile.loader import load_profile, parse_profile
from secondbrain.profile.rewrite import apply_proposal, generate_update, save_profile, text_sha
from secondbrain.profile.tune import feedback_distribution
from secondbrain.urls import content_hash
from secondbrain.web.app import create_app

NOW = datetime(2026, 11, 4, 12, tzinfo=UTC)
PROFILE_TEXT = (FIXTURES / "profile.md").read_text(encoding="utf-8")


# ---------- 测试数据 ----------


@pytest.fixture
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """临时 data 目录（fixture profile）与临时 config 目录（example 的副本）。"""
    data = tmp_path / "data"
    data.mkdir()
    shutil.copy(FIXTURES / "profile.md", data / "profile.md")
    conf = tmp_path / "config"
    conf.mkdir()
    for name in ("settings.example.yaml", "sources.example.yaml", "profile.example.md"):
        shutil.copy(config.CONFIG_DIR / name, conf / name)
    monkeypatch.setenv("SB_DATA_DIR", str(data))
    monkeypatch.setattr(config, "CONFIG_DIR", conf)
    monkeypatch.setattr(config, "ENV_FILE", tmp_path / "missing.env")
    monkeypatch.setenv("SB_LLM_BACKEND", "fake")
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    return data


def add_item(conn, n: int, title: str, source: str = "blog", topics=None, ctype="frontier") -> int:
    body = f"{title}. " + "Body text. " * 50
    db.insert_item(
        conn,
        db.NewItem(
            source_id=source,
            url=f"https://example.com/{source}/{n}",
            url_canonical=f"https://example.com/{source}/{n}",
            content_hash=content_hash(title, body),
            title=title,
            author=None,
            summary=title,
            content=body,
            extract_method="feed",
            published_at=NOW - timedelta(days=1),
        ),
        NOW,
    )
    item_id = conn.execute("SELECT MAX(id) FROM items").fetchone()[0]
    data = {"relevance": 7, "novelty": 6, "quality": 7, "refresher_value": 3}
    data |= {"content_type": ctype, "topics": topics or [], "one_line_reason": "r"}
    db.add_assessment(conn, item_id, "L1", data, NOW, "fake-cheap", 7.0)
    return item_id


@pytest.fixture
def conn(env: Path):
    c = db.connect()
    db.upsert_sources(
        c,
        [
            SourceConfig(id="blog", type="rss", url="https://b", trust=3),
            SourceConfig(id="news", type="rss", url="https://n", trust=3),
        ],
    )
    yield c
    c.close()


def seed_feedback(conn, when: datetime = NOW) -> dict[str, int]:
    """构造反馈：太浅（GPU kernels）、太难、不感兴趣、有收获、稍后读（不参与个性化）。
    返回 kind → 反馈 id。CLI / Web 按真实时钟取时间窗，调用时传 datetime.now(UTC)。"""
    ids = {}
    gpu = add_item(conn, 1, "Writing fast GPU kernels in Triton", topics=["gpu kernels"])
    cons = add_item(conn, 2, "Raft consensus explained", topics=["consensus"], ctype="explainer")
    news = add_item(conn, 3, "Funding news roundup", source="news")
    good = add_item(conn, 4, "Paged attention deep dive", topics=["kv cache"])
    later = add_item(conn, 5, "Saved for later")
    for item, kind in ((gpu, "too_easy"), (cons, "too_hard"), (news, "dismiss"), (good, "gained")):
        db.add_feedback(conn, item, kind, None, when - timedelta(hours=1))
        ids[kind] = conn.execute("SELECT MAX(id) FROM feedback").fetchone()[0]
    db.add_feedback(conn, later, "later", None, when)
    return ids


# ---------- 反馈汇总与 few-shot ----------


def test_recent_feedback_only_signals_in_window(conn):
    seed_feedback(conn)
    old = add_item(conn, 9, "Old post")
    db.add_feedback(conn, old, "gained", None, NOW - timedelta(days=60))
    entries = recent_feedback(conn, NOW, window_days=30, limit=50)
    assert [e.kind for e in entries] == ["gained", "dismiss", "too_hard", "too_easy"]
    gpu = next(e for e in entries if e.kind == "too_easy")
    assert (gpu.topics, gpu.content_type, gpu.source_id) == (["gpu kernels"], "frontier", "blog")
    assert len(recent_feedback(conn, NOW, 30, limit=2)) == 2


def test_few_shot_block(conn):
    seed_feedback(conn)
    entries = recent_feedback(conn, NOW, 30, 50)
    block = few_shot_block(entries, per_polarity=5)
    assert block.startswith("## 最近反馈样例")
    assert "用户有收获（正例）：\n- [有收获] Paged attention deep dive" in block
    assert "（反例）：\n- [不感兴趣] Funding news roundup" in block
    assert "- [太浅] Writing fast GPU kernels in Triton" in block  # 校准样例
    assert "- [太难] Raft consensus explained" in block
    assert "稍后" not in block  # 稍后读不是偏好信号
    assert few_shot_block(entries, per_polarity=0) == ""
    assert few_shot_block([], per_polarity=5) == ""


def test_polarity_examples_dedupe_and_cap(conn):
    item = add_item(conn, 1, "Same item")
    db.add_feedback(conn, item, "gained", None, NOW - timedelta(hours=3))
    db.add_feedback(conn, item, "dismiss", None, NOW - timedelta(hours=1))  # 当前状态为不感兴趣
    for n in range(2, 6):
        liked = add_item(conn, n, f"Liked {n}")
        db.add_feedback(conn, liked, "gained", None, NOW - timedelta(hours=n))
    pos, neg = polarity_examples(recent_feedback(conn, NOW, 30, 50), per_polarity=2)
    assert [e.title for e in neg] == ["Same item"]
    assert len(pos) == 2 and all(e.title.startswith("Liked") for e in pos)


class RecordingFake(FakeBackend):
    def __init__(self):
        self.systems = []

    def complete_json(self, messages, schema, tier):
        if tier == "cheap":
            self.systems.append(messages[0]["content"])
        return super().complete_json(messages, schema, tier)


def _run(conn, backend):
    profile = load_profile(FIXTURES / "profile.md")
    run_pipeline(
        conn, FilterSettings(), backend, profile, limit=None, now=NOW, today=date(2026, 11, 4)
    )
    return profile


def test_l1_prompt_contains_feedback_examples(conn):
    seed_feedback(conn)
    add_item(conn, 20, "A new post to screen")
    conn.execute("UPDATE items SET status = 'new'")  # 让所有条目进入本次 run
    backend = RecordingFake()
    profile = _run(conn, backend)
    assert backend.systems
    base = system_prompt("l1_system", profile)
    for system in backend.systems:
        assert system.startswith(base.rstrip())  # profile + 规则在前，前缀稳定
        assert "## 最近反馈样例" in system
        assert "[有收获] Paged attention deep dive" in system
    assert len(set(backend.systems)) == 1


def test_l1_prompt_without_feedback_has_no_examples(conn):
    add_item(conn, 1, "A post")
    backend = RecordingFake()
    _run(conn, backend)
    assert backend.systems
    assert all("最近反馈样例" not in s for s in backend.systems)


# ---------- 提案校验与应用 ----------


def _feedback(conn):
    ids = seed_feedback(conn)
    return ids, recent_feedback(conn, NOW, 30, 50)


def _apply(fb, changes=(), edits=()):
    proposal = ProfileProposal(mastery_changes=list(changes), section_edits=list(edits))
    return apply_proposal(parse_profile(PROFILE_TEXT), proposal, fb)


def test_upgrade_requires_too_easy_evidence(conn):
    ids, fb = _feedback(conn)
    ok = _apply(
        fb,
        [
            MasteryChange(
                action="update", row=3, mastery="想回炉", reason="r", evidence=[ids["too_easy"]]
            )
        ],
    )
    assert ok.applied and not ok.rejected
    assert "| GPU kernels | 想回炉 | 高 | 带补充说明 |" in ok.new_text
    # 未修改的行保留原文（包括空单元格的写法），diff 只有一行改动
    assert "| Consensus protocols | 陌生 | 中 | |" in ok.new_text
    changed = [x for x in ok.diff().splitlines() if x[:1] in "+-" and x[:3] not in ("+++", "---")]
    assert changed == [
        "-| GPU kernels | 接触过（偶尔写） | 高 | 带补充说明 |",
        "+| GPU kernels | 想回炉 | 高 | 带补充说明 |",
    ]
    ev = [ids["gained"]]
    bad = _apply(
        fb, [MasteryChange(action="update", row=3, mastery="想回炉", reason="r", evidence=ev)]
    )
    assert not bad.applied and "升级缺少" in bad.rejected[0]  # 有收获 只能用于 陌生 → 接触过
    assert not bad.changed
    two = _apply(
        fb,
        [
            MasteryChange(
                action="update", row=3, mastery="扎实", reason="r", evidence=[ids["too_easy"]]
            )
        ],
    )
    assert "每次只能升一级" in two.rejected[0]  # 不再有"已扎实可直升扎实"的特例


def test_gained_only_supports_first_step(conn):
    ids, fb = _feedback(conn)
    ev = [ids["gained"]]
    ok = _apply(
        fb, [MasteryChange(action="update", row=2, mastery="接触过", reason="r", evidence=ev)]
    )
    assert ok.applied and "| Consensus protocols | 接触过 | 中 |" in ok.new_text


def test_downgrade_one_level_with_too_hard(conn):
    ids, fb = _feedback(conn)
    ev = [ids["too_hard"]]
    one = _apply(
        fb, [MasteryChange(action="update", row=1, mastery="接触过", reason="r", evidence=ev)]
    )
    assert one.applied
    two = _apply(
        fb, [MasteryChange(action="update", row=1, mastery="陌生", reason="r", evidence=ev)]
    )
    assert "每次只能降一级" in two.rejected[0]
    no_ev = _apply(
        fb,
        [
            MasteryChange(
                action="update", row=1, mastery="接触过", reason="r", evidence=[ids["gained"]]
            )
        ],
    )
    assert "降级缺少" in no_ev.rejected[0]


def test_evidence_is_required_and_must_exist(conn):
    ids, fb = _feedback(conn)
    res = _apply(
        fb,
        [
            MasteryChange(action="update", row=3, mastery="扎实", reason="r", evidence=[]),
            MasteryChange(action="update", row=3, mastery="扎实", reason="r", evidence=[99999]),
            MasteryChange(
                action="update", row=42, mastery="扎实", reason="r", evidence=[ids["too_easy"]]
            ),
            MasteryChange(
                action="add", domain="X", mastery="精通", reason="r", evidence=[ids["gained"]]
            ),
        ],
    )
    assert not res.applied and len(res.rejected) == 4
    assert "没有给出反馈证据" in res.rejected[0]
    assert "不存在" in res.rejected[1]
    assert "行号无效" in res.rejected[2]
    assert "掌握度取值无效" in res.rejected[3]


def test_add_remove_and_section_edits(conn):
    ids, fb = _feedback(conn)
    res = _apply(
        fb,
        [
            MasteryChange(
                action="add",
                domain="Compilers（MLIR）",
                mastery="接触过",
                priority="中",
                reason="r",
                evidence=[ids["gained"]],
            ),
            MasteryChange(action="remove", row=4, reason="r", evidence=[ids["dismiss"]]),
        ],
        [
            SectionEdit(
                section="不想看的内容",
                action="append",
                text="行业新闻汇总",
                reason="r",
                evidence=[ids["dismiss"]],
            ),
            SectionEdit(
                section="不想看的内容",
                action="remove",
                text="- 融资新闻",
                reason="r",
                evidence=[ids["dismiss"]],
            ),
            SectionEdit(
                section="不存在的段落",
                action="append",
                text="x",
                reason="r",
                evidence=[ids["dismiss"]],
            ),
        ],
    )
    assert len(res.applied) == 4 and len(res.rejected) == 1
    text = res.new_text
    assert "| Compilers（MLIR） | 接触过 | 中 |  |" in text
    assert "| Compilers | 精通 |" not in text
    assert "- 行业新闻汇总" in text and "- 融资新闻" not in text
    # 其余内容保持不变
    assert "## 背景\n\n- 一名虚构的后端工程师，正在学习推理系统。" in text
    diff = res.diff()
    assert diff.startswith("--- a/profile.md\n+++ b/profile.md")
    assert "+- 行业新闻汇总" in diff


def test_profile_without_table_rejects_mastery_changes(conn):
    ids, fb = _feedback(conn)
    profile = parse_profile("# 画像\n\n## 不想看的内容\n\n- 广告\n")
    proposal = ProfileProposal(
        mastery_changes=[
            MasteryChange(
                action="add", domain="X", mastery="陌生", reason="r", evidence=[ids["gained"]]
            )
        ],
        section_edits=[
            SectionEdit(
                section="不想看的内容",
                action="append",
                text="新闻",
                reason="r",
                evidence=[ids["dismiss"]],
            )
        ],
    )
    res = apply_proposal(profile, proposal, fb)
    assert "没有可解析的掌握度表" in res.rejected[0]
    assert res.new_text == "# 画像\n\n## 不想看的内容\n\n- 广告\n- 新闻\n"


def test_generate_update_with_fake_backend(conn):
    _, fb = _feedback(conn)
    res = generate_update(FakeBackend(), PROFILE_TEXT, fb)
    assert res.changed
    assert "+| GPU kernels | 想回炉 | 高 | 带补充说明 |" in res.diff()
    assert "+- 类似《Funding news roundup》的内容" in res.diff()
    assert res.usage is not None


def test_save_profile_backs_up_old_version(tmp_path: Path):
    path = tmp_path / "profile.md"
    path.write_text("old", encoding="utf-8")
    first = save_profile(path, "new", tmp_path / "history", NOW)
    second = save_profile(path, "newer", tmp_path / "history", NOW)
    assert first.name == f"profile-{NOW.astimezone():%Y%m%d-%H%M%S}.md"
    assert second.name.endswith("-2.md")
    assert first.read_text(encoding="utf-8") == "old"
    assert second.read_text(encoding="utf-8") == "new"
    assert path.read_text(encoding="utf-8") == "newer"


# ---------- CLI：sb profile update ----------

runner = CliRunner()


def test_profile_update_without_feedback_skips_llm(env: Path, monkeypatch: pytest.MonkeyPatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("无反馈时不应创建 LLM 后端")

    monkeypatch.setattr("secondbrain.cli.make_backend", forbidden)
    result = runner.invoke(cli_app, ["profile", "update", "--dry-run"])
    assert result.exit_code == 0, result.output
    assert "无足够反馈，不提议修改" in result.output


def test_profile_update_dry_run_and_apply(env: Path, conn):
    seed_feedback(conn, datetime.now(UTC))
    conn.commit()
    before = (env / "profile.md").read_text(encoding="utf-8")

    result = runner.invoke(cli_app, ["profile", "update"])  # 默认 dry-run
    assert result.exit_code == 0, result.output
    assert "+| GPU kernels | 想回炉 |" in result.output
    assert "dry-run：未写入" in result.output
    assert (env / "profile.md").read_text(encoding="utf-8") == before
    assert not (env / "profile_history").exists()

    result = runner.invoke(cli_app, ["profile", "update", "--apply"])
    assert result.exit_code == 0, result.output
    backups = list((env / "profile_history").glob("profile-*.md"))
    assert len(backups) == 1 and backups[0].read_text(encoding="utf-8") == before
    assert "| GPU kernels | 想回炉 |" in (env / "profile.md").read_text(encoding="utf-8")
    calls = conn.execute("SELECT stage, backend FROM llm_calls").fetchall()
    assert [tuple(r) for r in calls] == [("PROFILE", "fake"), ("PROFILE", "fake")]


# ---------- 信源 trust 调整 ----------


def _votes(conn, source: str, up: int, down: int, start: int = 100, when: datetime = NOW):
    for n in range(up + down):
        item = add_item(conn, start + n, f"{source} post {n}", source=source)
        kind = "gained" if n < up else "dismiss"
        db.add_feedback(conn, item, kind, None, when - timedelta(hours=1))


def test_feedback_distribution_uses_current_state(conn):
    """ADR 0018：反馈只做分布统计（不再产生 trust 建议）；每个条目只看当前状态。"""
    _votes(conn, "blog", up=5, down=1)
    flip = add_item(conn, 1, "flip-flop", source="news")
    for kind in ("gained", "gained", "dismiss"):
        db.add_feedback(conn, flip, kind, None, NOW - timedelta(hours=1))
    dist = feedback_distribution(conn)
    assert dist["blog"]["gained"] == 5 and dist["blog"]["dismiss"] == 1
    assert dist["news"] == {
        "gained": 0,
        "too_easy": 0,
        "too_hard": 0,
        "worthless": 0,
        "dismiss": 1,
        "later": 0,
    }


def test_sources_tune_cli_is_read_only(env: Path, conn):
    db.upsert_sources(
        conn, [SourceConfig(id="dan-luu", type="rss", url="u", trust=3, role="expansion")]
    )
    _votes(conn, "dan-luu", up=5, down=0, when=datetime.now(UTC))
    conn.commit()
    result = runner.invoke(cli_app, ["sources", "tune"])
    assert result.exit_code == 0, result.output
    assert "反馈分布" in result.output and "只供参考" in result.output
    line = next(x for x in result.output.splitlines() if x.startswith("dan-luu"))
    assert "trust 3" in line and "反馈   5   0   0   0   0   0" in line
    assert "→" not in result.output  # 不再有 trust 调整建议
    assert runner.invoke(cli_app, ["sources", "tune", "--apply"]).exit_code != 0  # 选项已删除
    assert not (config.CONFIG_DIR / "sources.yaml").exists()


# ---------- Web：/profile ----------


@pytest.fixture
def client(env: Path) -> TestClient:
    return TestClient(create_app())


def test_profile_page(client: TestClient):
    resp = client.get("/profile")
    assert resp.status_code == 200
    assert "领域掌握度（解析结果）" in resp.text
    assert "GPU kernels" in resp.text
    assert 'href="/profile"' in client.get("/").text


def test_profile_save_creates_backup(client: TestClient, env: Path):
    resp = client.post(
        "/profile", data={"text": "# 新画像\r\n\r\n内容\r\n"}, follow_redirects=False
    )
    assert resp.status_code == 303
    assert (env / "profile.md").read_text(encoding="utf-8") == "# 新画像\n\n内容\n"
    (backup,) = (env / "profile_history").glob("profile-*.md")
    assert backup.read_text(encoding="utf-8") == PROFILE_TEXT


def test_profile_propose_without_feedback(client: TestClient):
    resp = client.post("/profile/propose")
    assert resp.status_code == 200
    assert "无足够反馈" in resp.text


def test_profile_propose_and_apply(client: TestClient, env: Path, conn):
    now = datetime.now(UTC)
    seed_feedback(conn, now)
    conn.commit()
    resp = client.post("/profile/propose")
    assert resp.status_code == 200
    assert "+| GPU kernels | 想回炉 |" in resp.text
    assert 'hx-post="/profile/apply"' in resp.text

    stale = client.post("/profile/apply", data={"new_text": "x", "base_sha": "0" * 64})
    assert stale.status_code == 409
    assert (env / "profile.md").read_text(encoding="utf-8") == PROFILE_TEXT

    new = generate_update(FakeBackend(), PROFILE_TEXT, recent_feedback(conn, now, 30, 50)).new_text
    ok = client.post("/profile/apply", data={"new_text": new, "base_sha": text_sha(PROFILE_TEXT)})
    assert ok.status_code == 200 and "已应用" in ok.text
    assert (env / "profile.md").read_text(encoding="utf-8") == new
    assert list((env / "profile_history").glob("profile-*.md"))
