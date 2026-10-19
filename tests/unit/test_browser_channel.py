"""Browser channel tests (added after the 2026-10-19 design correction).

The pinned core discipline: **browser channel failures must be reported
honestly, never silently falling back to HTTP**. Falling back would disguise
"browser channel failed" as "the channel returned empty" — exactly the
misattribution this module fixes (APA once recorded an unrendered SPA as
"Workday anti-bot").

Real validation evidence lives in the singapore workspace's evidence doc:
playwright-core + Chrome 155 work on this machine, and
``waitForTimeout(6000~10000)`` is the necessary wait for SPA rendering.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from apa.f10.browser import (DEFAULT_WAIT_MS, MIN_BODY_CHARS, BrowserFetch,
                              BrowserUnavailable, FetchResult, LoginSession)


class TestPreflight:
    def test_missing_chrome_is_reported_not_ignored(self, tmp_path: Path):
        f = BrowserFetch(chrome_path=tmp_path / "nope.exe")
        with pytest.raises(BrowserUnavailable) as ei:
            f.preflight()
        assert "APA_CHROME_PATH" in str(ei.value)     # actionable

    def test_missing_playwright_names_the_workspace(self, tmp_path: Path,
                                                    monkeypatch):
        chrome = tmp_path / "chrome.exe"
        chrome.write_text("x", encoding="utf-8")
        monkeypatch.setattr("apa.f10.browser._workspace", lambda: tmp_path / "nowhere")
        f = BrowserFetch(chrome_path=chrome)
        with pytest.raises(BrowserUnavailable) as ei:
            f.preflight()
        assert "playwright-core" in str(ei.value)
        assert "blocked" in str(ei.value)   # explains that "falling back to HTTP records blocked"


class TestFetchResult:
    def test_render_gate_rejects_short_body(self):
        """Measured calibration: bodyLen>2000 && links>0. Short = not finished
        rendering, not a site problem."""
        assert not FetchResult(url="u", text="x" * 10).rendered_ok
        assert FetchResult(url="u", text="x" * MIN_BODY_CHARS).rendered_ok

    def test_to_dict_is_serialisable(self):
        d = FetchResult(url="u", status=200, text="y" * 3000).to_dict()
        assert json.loads(json.dumps(d))["rendered_ok"] is True


class TestBrowserFetchNeverDegradesSilently:
    def test_preflight_failure_becomes_result_error_not_empty_success(self, tmp_path):
        """Core discipline: if the browser is unavailable, say so — never
        return "200 + empty body"."""
        f = BrowserFetch(chrome_path=tmp_path / "missing.exe")
        res = f.fetch("https://example.test/job")
        assert res.error is not None
        assert "APA_CHROME_PATH" in res.error
        assert res.status is None
        assert not res.rendered_ok

    def test_node_missing_is_reported(self, tmp_path, monkeypatch):
        chrome = tmp_path / "chrome.exe"
        chrome.write_text("x", encoding="utf-8")
        monkeypatch.setattr("apa.f10.browser._workspace",
                            lambda: tmp_path)          # no node_modules → preflight fails
        res = BrowserFetch(chrome_path=chrome).fetch("https://example.test")
        assert res.error and "playwright-core" in res.error

    def test_empty_node_output_is_an_error_not_a_blank_page(self, tmp_path, monkeypatch):
        """node ran but produced no JSON → error. Producing empty JSON also
        counts as failure."""
        chrome = tmp_path / "chrome.exe"
        chrome.write_text("x", encoding="utf-8")
        ws = tmp_path / "ws"
        (ws / "node_modules" / "playwright-core").mkdir(parents=True)
        monkeypatch.setattr("apa.f10.browser._workspace", lambda: ws)

        import subprocess as sp

        def fake_run(cmd, **kw):
            return sp.CompletedProcess(cmd, 0, stdout="", stderr="chrome died")

        monkeypatch.setattr("apa.f10.browser.subprocess.run", fake_run)
        res = BrowserFetch(chrome_path=chrome).fetch("https://example.test")
        assert res.error and "chrome died" in res.error

    def test_malformed_node_output_is_an_error(self, tmp_path, monkeypatch):
        chrome = tmp_path / "chrome.exe"
        chrome.write_text("x", encoding="utf-8")
        ws = tmp_path / "ws"
        (ws / "node_modules" / "playwright-core").mkdir(parents=True)
        monkeypatch.setattr("apa.f10.browser._workspace", lambda: ws)

        import subprocess as sp

        real_run = sp.run

        def fake_run(cmd, **kw):
            # simulate node exiting successfully but writing bad JSON
            out = Path(cmd[4])
            out.write_text("{not json", encoding="utf-8")
            return sp.CompletedProcess(cmd, 0, stdout="", stderr="")

        monkeypatch.setattr("apa.f10.browser.subprocess.run", fake_run)
        res = BrowserFetch(chrome_path=chrome).fetch("https://example.test")
        assert res.error and "unreadable" in res.error


class TestDefaultsFromRealEvidence:
    def test_wait_default_matches_measured_range(self):
        """Measured: waitForTimeout(6000~10000); the default takes 8000."""
        assert 6000 <= DEFAULT_WAIT_MS <= 10000

    def test_locale_default_is_singapore_english(self):
        """Measured parameter locale: 'en-SG' — affects dates and sorting; not
        optional."""
        assert BrowserFetch().locale == "en-SG"

    def test_headed_is_off_by_default(self):
        """Headless by default: a probe should not pop up windows. Pass
        headed=True explicitly when a human needs to operate it."""
        assert BrowserFetch().headed is False


class TestLoginSessionCredentials:
    def test_persist_dir_lives_under_data(self):
        s = LoginSession(persist_dir="data/browser-session")
        # on Windows it is a backslash — compare parts, not the prefix
        assert s.persist_dir.parts[0] == "data"

    def test_script_is_written_and_has_no_credential_handling(self, tmp_path):
        """Zero credential contact: the script must not contain
        form-filling/cookie-reading actions."""
        s = LoginSession(persist_dir=tmp_path)
        p = s.script_path()
        src = p.read_text(encoding="utf-8")
        assert "launchPersistentContext" in src
        assert "headless: false" in src                 # headed, visible to the human
        for forbidden in ("fill(", "type(", "cookie", "password"):
            assert forbidden not in src, f"must not automate {forbidden}"

    def test_launch_requires_existing_chrome(self, tmp_path):
        s = LoginSession(persist_dir=tmp_path, chrome_path=tmp_path / "no.exe")
        with pytest.raises(BrowserUnavailable):
            s.launch("https://example.test")


class TestCliDispatch:
    """`apa fetch` / `apa auth login` wiring (2026-10-19: the implementation
    existed but the dispatch was not hooked up).

    Same discipline as this file's theme: failures propagate honestly — if all
    fetches fail, exit 1; never a silent success.
    """

    def test_fetch_reaches_handler_and_reports_per_url(self, tmp_path, monkeypatch,
                                                       capsys):
        from apa.cli import main

        class FakeFetch:
            def __init__(self, **kw):
                self.kw = kw

            def fetch(self, url):
                return FetchResult(url=url, status=200, text="x" * 3000,
                                   title="Job page")

        monkeypatch.setattr("apa.f10.browser.BrowserFetch", FakeFetch)
        out = tmp_path / "bodies.md"
        rc = main(["fetch", "https://a.test/1", "https://a.test/2",
                   "--out", str(out)])
        assert rc == 0
        assert "ok  200" in capsys.readouterr().out
        assert "written:" in capsys.readouterr().out or out.exists()
        assert out.exists() and "a.test/1" in out.read_text(encoding="utf-8")

    def test_fetch_all_failed_is_exit_1_not_silent_success(self, monkeypatch, capsys):
        from apa.cli import main

        class DeadFetch:
            def __init__(self, **kw):
                pass

            def fetch(self, url):
                return FetchResult(url=url, error="chrome missing")

        monkeypatch.setattr("apa.f10.browser.BrowserFetch", DeadFetch)
        rc = main(["fetch", "https://a.test/1"])
        assert rc == 1
        assert "FAILED" in capsys.readouterr().out

    def test_auth_login_dispatches_without_opening_real_browser(self, monkeypatch,
                                                                capsys):
        from apa.cli import main

        launched = {}

        class FakeSession:
            def __init__(self, **kw):
                pass

            def launch(self, url, wait_s=300):
                launched["url"] = url
                launched["wait_s"] = wait_s

        monkeypatch.setattr("apa.f10.browser.LoginSession", FakeSession)
        rc = main(["auth", "login", "https://sggovterp.wd102.myworkdayjobs.com",
                   "--wait", "42"])
        assert rc == 0
        assert launched == {"url": "https://sggovterp.wd102.myworkdayjobs.com",
                            "wait_s": 42}
        assert "never sees your credentials" in capsys.readouterr().out

    def test_auth_login_unavailable_browser_is_exit_1(self, monkeypatch, capsys):
        from apa.cli import main

        class NoBrowser:
            def __init__(self, **kw):
                pass

            def launch(self, url, wait_s=300):
                raise BrowserUnavailable("no chrome")

        monkeypatch.setattr("apa.f10.browser.LoginSession", NoBrowser)
        assert main(["auth", "login"]) == 1
        assert "cannot open a browser" in capsys.readouterr().out

    def test_auth_without_subcommand_prints_help(self, capsys):
        from apa.cli import main

        assert main(["auth"]) == 1
        assert "login" in capsys.readouterr().out
