"""D4：F10 三路 poller / store / scheduler / 目标构建 测试。

纪律：
- 禁实网：全部经 FakeTransport 回放（含 S3 AccessDenied 真实形态——2026-10-18
  对 status.careers.gov.sg 实测录制），UrllibTransport 只测非法 URL 收敛。
- blocked ≠ outage：状态页 API 被源站拒绝是探针环境限制，不得判渠道故障。
- 目标构建用 openpyxl 写临时台账，测筛选口径（活跃裁决/截止/脏链接）。
"""

from __future__ import annotations

import json
from datetime import date, timedelta
from pathlib import Path

import openpyxl
import pytest

from apa.f10 import pollers, scheduler, store
from apa.f10.http import FakeTransport, HttpResponse, UrllibTransport

S3_DENIED_BODY = ('<?xml version="1.0" encoding="UTF-8"?>'
                  "<Error><Code>AccessDenied</Code><Message>Access Denied</Message>"
                  "</Error>")


# ---------- StatuspagePoller ----------

class TestStatuspagePoller:
    BASE = "https://status.example.gov.sg"
    URL = BASE + "/api/v2/summary.json"

    def _poller(self, resp: HttpResponse) -> pollers.StatuspagePoller:
        return pollers.StatuspagePoller(self.BASE, FakeTransport({(self.URL, "GET"): resp}))

    def test_operational_ok(self):
        resp = HttpResponse(200, json.dumps({
            "page": {"status_indicator": "none", "status_description": "All Systems Operational"},
            "components": [{"name": "jobs"}, {"name": "HRPS"}], "incidents": []}))
        r = self._poller(resp).probe()[0]
        assert r.outcome == "ok"
        assert "components=2" in r.detail

    @pytest.mark.parametrize("indicator,expected", [
        ("minor", "degraded"), ("maintenance", "degraded"),
        ("major", "outage"), ("critical", "outage"),
    ])
    def test_indicator_mapping(self, indicator, expected):
        resp = HttpResponse(200, json.dumps({
            "page": {"status_indicator": indicator, "status_description": "x"},
            "components": [], "incidents": [{"name": "i"}]}))
        r = self._poller(resp).probe()[0]
        assert r.outcome == expected
        assert "incidents=1" in r.detail

    def test_s3_access_denied_is_blocked_not_outage(self):
        """2026-10-18 实测形态：源站拒绝非浏览器客户端 → blocked，绝不判 outage。"""
        r = self._poller(HttpResponse(403, S3_DENIED_BODY)).probe()[0]
        assert r.outcome == "blocked"
        assert "not a channel failure" in r.detail

    def test_http_500_degraded(self):
        r = self._poller(HttpResponse(500, "oops")).probe()[0]
        assert r.outcome == "degraded"

    def test_non_json_parse_error(self):
        r = self._poller(HttpResponse(200, "<html>shell</html>")).probe()[0]
        assert r.outcome == "parse_error"

    def test_transport_error_unreachable(self):
        r = self._poller(HttpResponse(None, error="URLError: timeout")).probe()[0]
        assert r.outcome == "unreachable"


# ---------- JobUrlPoller ----------

class TestJobUrlPoller:
    URL = "https://jobs.careers.gov.sg/jobs/hrp/123/abc"

    def _probe(self, resp: HttpResponse) -> pollers.ProbeResult:
        return pollers.JobUrlPoller(FakeTransport({(self.URL, "HEAD"): resp})).probe_one(self.URL)

    @pytest.mark.parametrize("status,expected", [
        (200, "ok"), (301, "ok"), (403, "blocked"), (429, "blocked"),
        (404, "gone"), (500, "degraded"), (503, "degraded"),
    ])
    def test_status_classification(self, status, expected):
        r = self._probe(HttpResponse(status, ""))
        assert r.outcome == expected
        assert f"HTTP {status}" in r.detail

    def test_transport_error_unreachable(self):
        r = self._probe(HttpResponse(None, error="URLError: dns fail"))
        assert r.outcome == "unreachable"

    def test_head_405_falls_back_to_get(self):
        ft = FakeTransport({
            (self.URL, "HEAD"): HttpResponse(405, ""),
            (self.URL, "GET"): HttpResponse(200, "<html>job</html>"),
        })
        r = pollers.JobUrlPoller(ft).probe_one(self.URL)
        assert r.outcome == "ok"
        assert ft.calls == [(self.URL, "HEAD"), (self.URL, "GET")]

    # ---------- deep 深检（2026-10-19 用户线索「closed 标签」的实证落地） ----------

    def test_deep_appends_signals_without_changing_outcome(self):
        """deep：GET 正文信号记入 detail，outcome 仍由 HEAD 判定。"""
        body = (
            "<html>Closing on 27 Oct 2026<svg>path</svg>Closed</span>"
            '<script>"datePosted":"2026-10-12"</script></html>'
        )
        ft = FakeTransport({
            (self.URL, "HEAD"): HttpResponse(200, ""),
            (self.URL, "GET"): HttpResponse(200, body),
        })
        r = pollers.JobUrlPoller(ft).probe_one(self.URL, deep=True)
        assert r.outcome == "ok"
        assert "deep: closing_on=27 Oct 2026" in r.detail
        assert "closed_span=Y" in r.detail
        assert "posted=2026-10-12" in r.detail
        assert ft.calls == [(self.URL, "HEAD"), (self.URL, "GET")]

    def test_deep_skips_body_when_head_not_ok(self):
        """HEAD 非 ok 不再取正文（省流量，且 gone/blocked 与页面标签无关）。"""
        ft = FakeTransport({(self.URL, "HEAD"): HttpResponse(404, "")})
        r = pollers.JobUrlPoller(ft).probe_one(self.URL, deep=True)
        assert r.outcome == "gone"
        assert "deep:" not in r.detail
        assert ft.calls == [(self.URL, "HEAD")]

    def test_deep_default_off(self):
        """默认不深检（成本纪律：HEAD 足够回答渠道在不在）。"""
        ft = FakeTransport({
            (self.URL, "HEAD"): HttpResponse(200, ""),
            (self.URL, "GET"): HttpResponse(200, "<html>Closing on 27 Oct 2026</html>"),
        })
        r = pollers.JobUrlPoller(ft).probe_one(self.URL)
        assert "deep:" not in r.detail
        assert ft.calls == [(self.URL, "HEAD")]


class TestParseJobPage:
    """parse_job_page 纯函数：信号抽取 + 不稳定标签的边界（实证 2026-10-19）。"""

    def test_extracts_all_signals(self):
        body = (
            "Closing on 22 Oct 2026 and other text"
            "<span>Closed</span>"
            '"datePosted":"2026-10-12T00:00:00Z"'
        )
        sig = pollers.parse_job_page(body)
        assert sig.closing_on == "22 Oct 2026"
        assert sig.closed_span is True
        assert sig.date_posted == "2026-10-12T00:00:00Z"
        assert sig.body_len == len(body)

    def test_missing_signals_are_none(self):
        sig = pollers.parse_job_page("<html>no tags here</html>")
        assert sig.closing_on is None
        assert sig.closed_span is False
        assert sig.date_posted is None

    def test_closing_in_days_not_in_detail_ssr(self):
        """用户观察的 'Closing in N day(s)' 不在详情页 SSR——正则不应误命中。"""
        sig = pollers.parse_job_page("<html>Closing in 1 day(s) until ad closes</html>")
        assert sig.closing_on is None  # 只认 "Closing on <date>" 形态


class TestJobUrlPollerBatch:
    URL = "https://jobs.careers.gov.sg/jobs/hrp/123/abc"

    def test_batch_probe(self):
        urls = [self.URL, "https://www.mycareersfuture.gov.sg/job/x"]
        ft = FakeTransport({
            (urls[0], "HEAD"): HttpResponse(200, ""),
            (urls[1], "HEAD"): HttpResponse(404, ""),
        })
        results = pollers.JobUrlPoller(ft).probe(urls)
        assert [r.outcome for r in results] == ["ok", "gone"]


# ---------- MaintenancePoller ----------

class TestMaintenancePoller:
    URL = "https://status.example.gov.sg/maintenance"

    def _probe(self, resp: HttpResponse) -> pollers.ProbeResult:
        return pollers.MaintenancePoller(self.URL, FakeTransport({(self.URL, "GET"): resp})).probe()

    def test_spa_shell_parse_error(self):
        """新版 Statuspage SPA 外壳（698 字节）→ 如实记 parse_error 并说明主渠道。"""
        shell = ('<!DOCTYPE html><html><head><title>t</title></head><body>'
                 '<script src="/src/status-pages-site-E2IQLH3W.min.js"></script></body></html>')
        r = self._probe(HttpResponse(200, shell))
        assert r.outcome == "parse_error"
        assert "manual announcement archiving remains the main channel" in r.detail

    def test_real_content_ok(self):
        body = "<html><body>Planned Maintenance window scheduled</body></html>" + "x" * 5000
        r = self._probe(HttpResponse(200, body))
        assert r.outcome == "ok"
        assert "1 maintenance-related marker(s)" in r.detail

    def test_transport_error(self):
        r = self._probe(HttpResponse(None, error="timeout"))
        assert r.outcome == "unreachable"


# ---------- store ----------

class TestStore:
    def test_write_and_latest(self, tmp_path: Path):
        db = tmp_path / "t.db"
        rows = [
            pollers.ProbeResult("job_url", "https://a/1", "ok", "HTTP 200", 100),
            pollers.ProbeResult("job_url", "https://a/1", "gone", "HTTP 404", 120),
            pollers.ProbeResult("statuspage", "https://s/api", "blocked", "denied", 50),
        ]
        assert store.write_results(db, rows) == 3
        latest = store.latest_by_target(db)
        by_key = {(r["kind"], r["target"]): r for r in latest}
        assert by_key[("job_url", "https://a/1")]["outcome"] == "gone"   # 取最新
        assert by_key[("statuspage", "https://s/api")]["outcome"] == "blocked"
        assert store.count_outcomes(db) == {"ok": 1, "gone": 1, "blocked": 1}

    def test_write_empty(self, tmp_path: Path):
        assert store.write_results(tmp_path / "t.db", []) == 0

    def test_schema_idempotent(self, tmp_path: Path):
        """ensure_schema 幂等：连开两次不炸（Task Scheduler 常驻重复调用）。"""
        from apa import models
        db = tmp_path / "t.db"
        c1 = models.connect(db)
        c1.close()
        c2 = models.connect(db)
        c2.close()


# ---------- scheduler ----------

class TestScheduler:
    def test_three_tasks_with_frequencies(self):
        cmds = scheduler.build_commands()
        assert len(cmds) == 3
        by_name = {c.name: c.command for c in cmds}
        assert "/sc minute /mo 5" in by_name["APAProbeStatuspage"]
        assert "/sc minute /mo 15" in by_name["APAProbeJobs"]
        assert "/sc daily /st 08:30" in by_name["APAProbeMaintenance"]
        assert all("/F" in c for c in by_name.values())          # 幂等覆盖
        assert all("--kind" in c for c in by_name.values())

    def test_dry_run_returns_without_executing(self):
        """dry-run 只返回命令清单，不调 subprocess（无 --apply 不注册）。"""
        cmds = scheduler.register(apply=False)
        assert len(cmds) == 3 and all(c.command.startswith("schchts") is False for c in cmds)

    def test_unregister_dry_run(self):
        cmds = scheduler.unregister(apply=False)
        assert all("/Delete" in c for c in cmds)


# ---------- UrllibTransport 边界 ----------

def test_urllib_invalid_url_converges_not_raises():
    """脏链接（含空格/注释）必须收敛为 error 响应——常驻任务不炸。"""
    resp = UrllibTransport(timeout=5).request("https://www.proseware.example (x0pa API)")
    assert resp.status is None and resp.error is not None


# ---------- 目标构建筛选口径 ----------

HEADER = ["记录ID", "来源", "岗位标题", "雇主/机构", "真实链接", "抓取时间",
          "平台更新时间", "HTTP", "原文存档", "截止日期", "薪资(标头)", "雇佣类型",
          "评分", "档位", "裁决", "母本/批次", "依据文档", "最后更新", "备注", "雇主类型"]


def _row(jid, url, verdict, deadline, source="GOV-HRP"):
    r = [""] * len(HEADER)
    r[0], r[2], r[3], r[4] = jid, f"title-{jid}", "employer", url
    r[1], r[9], r[14] = source, deadline, verdict
    return r


class TestBuildProbeTargets:
    def _write_ledger(self, tmp_path: Path, rows) -> Path:
        p = tmp_path / "ledger.xlsx"
        wb = openpyxl.Workbook()
        ws = wb.active
        ws.title = "总台账"
        ws.append(HEADER)
        for r in rows:
            ws.append(r)
        wb.save(p)
        return p

    def test_filters(self, tmp_path: Path):
        today = date.today()
        rows = [
            _row("A1", "https://jobs.careers.gov.sg/jobs/hrp/1/x", "可投", None),
            _row("A2", "https://jobs.careers.gov.sg/jobs/hrp/2/x", "可投",
                 (today + timedelta(days=5)).isoformat()),
            _row("A3", "https://jobs.careers.gov.sg/jobs/hrp/3/x", "可投",
                 (today - timedelta(days=1)).isoformat()),          # 已截止剔除
            _row("A4", "https://jobs.careers.gov.sg/jobs/hrp/4/x", "不投", None),   # 非活跃
            _row("A5", "https://jobs.careers.gov.sg/jobs/hrp/5/x", "未评", None),   # 未裁决
            _row("A6", "https://jobs.careers.gov.sg/jobs/hrp/6/x", "可投", None,
                 source="MCF"),                                       # 渠道误标由 host 纠正
            _row("A7", "无链接", "可投", None),                        # 无 URL
            _row("A8", "https://www.proseware.example (x0pa API)", "可投", None),  # 脏链接
            _row("A9", "https://jobs.careers.gov.sg/jobs/hrp/1/x", "可投", None),  # 重复 URL
        ]
        # 脚本文件名以数字开头不能 import，按文件路径加载
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            "build_targets", Path(__file__).resolve().parents[2] / "scripts" / "03_build_probe_targets.py")
        m = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(m)
        data = m.build(self._write_ledger(tmp_path, rows))
        urls = {t["url"] for t in data["targets"]}
        assert urls == {
            "https://jobs.careers.gov.sg/jobs/hrp/1/x",
            "https://jobs.careers.gov.sg/jobs/hrp/2/x",
            "https://jobs.careers.gov.sg/jobs/hrp/6/x",
            "https://www.proseware.example",
        }
        proseware = next(t for t in data["targets"] if t["jd_id"] == "A8")
        assert proseware["url_note"] == "(x0pa API)"
        assert data["stats"]["past_deadline"] == 1
        assert data["stats"]["no_url"] == 1
        assert data["stats"]["malformed_url_fixed"] == 1
        assert data["stats"]["dupe_url"] == 1
        mcf = next(t for t in data["targets"] if t["jd_id"] == "A6")
        assert mcf["channel"] == "hrp"   # host 是地面真值，纠正来源列误标
