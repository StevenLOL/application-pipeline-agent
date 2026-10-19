"""Browser channel: render pages with a real browser for harvesting and probing.

**Why this channel is necessary** (2026-10-19, ``<private-workspace>``):

APA once mislabeled "Workday / status page returns 403 or a body of length 0 to HTTP requests" as
**"channel anti-bot protection"** — that attribution was wrong. The two real reasons:

1. **The SPA was not waited on long enough**: measured parameter ``waitForTimeout(6000~10000)``,
   with the original note "**Workday-style SPAs must be waited on, otherwise the body is empty**".
   In other words a body of 0 means **we did not wait**, not that the site refused.
2. **A login session is required**: the finding was "Workday only returns content after a Singpass login",
   and it was explicitly noted "**this involves your account and requires your explicit authorization**".

Incident 092 begins with the same mistaken record:
> "trigger:
> the corresponding headed and headless environments**" ->

So this module provides two things:

- :class:`BrowserFetch` — fetch rendered DOM text (solves "SPA not rendered" even without login)
- :class:`LoginSession` — **the user logs in themselves in the browser**; we only reuse that session;
  credentials never pass through this process (never typed, never stored, never read as cookie values)

Discipline:
- **Off by default**: ``apa probe`` still defaults to UrllibTransport (thin, reliable, always-on);
  the browser channel is an explicit opt-in (``--browser``) because it is heavy, slow, and spins up a process.
- **Zero credential contact**: login is performed by the user in the browser; this module only calls "page
  is logged in", and implements no login-form automation (this is discipline, not a missing capability).
- **Process discipline**: the browser channel is **never registered as a Task Scheduler persistent task**
  (092 measured that the agent-browser daemon hangs on cold start and needs 24 chrome processes killed).
"""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

DEFAULT_WAIT_MS = 8000          # 092 measured 6000~10000
DEFAULT_LOCALE = "en-SG"        # 092 measured parameter; affects date and sorting
DEFAULT_CHROME = Path(
    os.environ.get("APA_CHROME_PATH",
                   r"C:/Users/Administrator/.agent-browser/browsers/"
                   r"chrome-155.0.8059.39/chrome.exe"))
#: 092 measured: render gate bodyLen>2000 && links>0 (096 reuses)
MIN_BODY_CHARS = 2000


class BrowserUnavailable(RuntimeError):
    """Browser channel unavailable (playwright-core not installed / Chrome missing / launch failed).

    **Must report the real error rather than silently fall back to HTTP** — falling back would disguise
    a "browser channel failure" as a "channel returned empty", which is exactly the wrong attribution
    this module exists to fix.
    """


@dataclass
class FetchResult:
    """Result of a render-fetch."""

    url: str
    status: Optional[int] = None
    title: str = ""
    text: str = ""
    html: str = ""
    error: Optional[str] = None

    @property
    def rendered_ok(self) -> bool:
        """Render gate (096 measured criterion): only a long-enough body counts as truly rendered.

        A very short body usually means the SPA has not finished rendering — **extend the wait before
        blaming the site**.
        """
        return len(self.text) >= MIN_BODY_CHARS

    def to_dict(self) -> dict[str, Any]:
        return {"url": self.url, "status": self.status, "title": self.title,
                "text_chars": len(self.text), "rendered_ok": self.rendered_ok,
                "error": self.error}


def _node_binary() -> str:
    return os.environ.get("APA_NODE", "node")


def _workspace() -> Path:
    """The managed workspace where playwright-core is installed (092 measured installed here)."""
    return Path(os.environ.get("APA_NODE_WORKSPACE",
                               r"C:/Users/Administrator/.workbuddy/binaries/node/workspace"))


class BrowserFetch:
    """Render-based fetch: launch headless Chrome, wait for the SPA to render, then take the DOM text.

    **Use the existing node + playwright-core, do not introduce a Python-side browser dependency** —
    092 verified this channel works on this machine, and APA's apa env stays free of browser dependencies
    (the thinner the Task Scheduler persistent process, the more reliable).

    With ``headed=True`` a headed window opens: **the user can see and operate it manually**.
    """

    def __init__(self, wait_ms: int = DEFAULT_WAIT_MS, locale: str = DEFAULT_LOCALE,
                 headed: bool = False, chrome_path: Optional[Path] = None,
                 timeout_s: int = 60):
        self.wait_ms = wait_ms
        self.locale = locale
        self.headed = headed
        self.chrome_path = Path(chrome_path or DEFAULT_CHROME)
        self.timeout_s = timeout_s

    # -- pre-flight: error out if missing, never silently degrade ----

    def preflight(self) -> None:
        if not self.chrome_path.exists():
            raise BrowserUnavailable(
                f"chrome not found at {self.chrome_path}. "
                f"Set APA_CHROME_PATH to your Chrome/Edge executable.")
        if not (_workspace() / "node_modules" / "playwright-core").exists():
            raise BrowserUnavailable(
                f"playwright-core not installed in {_workspace()}. "
                f"npm i playwright-core in that workspace, or run with the "
                f"default HTTP channel (that limitation is recorded as blocked, "
                f"not as a channel outage).")

    def fetch(self, url: str) -> FetchResult:
        """Render-fetch a URL. All failures collapse into FetchResult(error=...)."""
        try:
            self.preflight()
        except BrowserUnavailable as exc:
            return FetchResult(url=url, error=str(exc))

        script = _RENDER_SCRIPT
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "out.json"
            profile = Path(tmp) / "profile"      # isolated profile; never touch the user's everyday browser
            cmd = [
                _node_binary(), "-e", script,
                url, str(out), str(self.chrome_path), str(profile),
                str(self.wait_ms), self.locale, "1" if self.headed else "0",
            ]
            try:
                proc = subprocess.run(
                    cmd, cwd=str(_workspace()), capture_output=True,
                    text=True, timeout=self.timeout_s,
                    env={**os.environ, "PYTHONIOENCODING": "utf-8"})
            except subprocess.TimeoutExpired:
                return FetchResult(url=url,
                                   error=f"browser timed out after {self.timeout_s}s")
            except OSError as exc:
                return FetchResult(url=url, error=f"cannot start node: {exc}")

            if not out.exists():
                detail = (proc.stderr or proc.stdout or "").strip()[-400:]
                return FetchResult(url=url, error=f"render script produced no output: {detail}")
            try:
                data = json.loads(out.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError) as exc:
                return FetchResult(url=url, error=f"unreadable render output: {exc}")

        return FetchResult(
            url=url, status=data.get("status"), title=data.get("title", ""),
            text=data.get("text", ""), html=data.get("html", "")[:262144],
            error=data.get("error"),
        )


class LoginSession:
    """Browser session for user self-login — **zero credential contact**.

    Usage (the only supported mode):

    1. ``session = LoginSession(persist_dir="data/browser-session")``
    2. ``session.open(url)`` → opens a headed Chrome, **the user enters account/password/Singpass themselves**
    3. After logging in within the browser, the user returns to the terminal ``input()`` (or presses a UI button)
    4. Afterwards :meth:`fetch` reuses that profile's login state to fetch pages

    **This class implements no login-form automation and reads no cookie values.** Reasons:
    - Automated login means holding credentials on the user's behalf → security and legal risk should not be introduced by the tool
    - Singpass involves MFA / one-time passwords; automation would only make it more fragile
    - The user logs in within **their own browser**; credentials never pass through this process

    The profile directory lives under ``data/`` (gitignored); the user may delete it at any time.
    """

    def __init__(self, persist_dir: str | Path = "data/browser-session",
                 chrome_path: Optional[Path] = None,
                 locale: str = DEFAULT_LOCALE):
        self.persist_dir = Path(persist_dir)
        self.chrome_path = Path(chrome_path or DEFAULT_CHROME)
        self.locale = locale
        self._pid: Optional[int] = None

    def script_path(self) -> Path:
        """Write the login script to its landing location (invoked by ``apa auth login``)."""
        self.persist_dir.mkdir(parents=True, exist_ok=True)
        p = self.persist_dir / "login.mjs"
        p.write_text(_LOGIN_SCRIPT, encoding="utf-8")
        return p

    def launch(self, url: str, wait_s: int = 300) -> None:
        """Open a headed browser to ``url``, keeping the window open for ``wait_s`` seconds for the user to log in.

        **The window does not auto-close**: the user may need to handle MFA, SMS, or image captchas.

        The call shape is ``node login.mjs <url> <profile> <chrome> <locale> <wait>``;
        inside the script parameters are taken uniformly via ``argv.slice(1)`` (same convention as the render script).
        """
        if not self.chrome_path.exists():
            raise BrowserUnavailable(f"chrome not found at {self.chrome_path}")
        script = self.script_path()
        proc = subprocess.Popen(
            [_node_binary(), str(script), url, str(self.persist_dir),
             str(self.chrome_path), self.locale, str(wait_s)],
            cwd=str(_workspace()),
            env={**os.environ, "PYTHONIOENCODING": "utf-8"})
        self._pid = proc.pid

    def wait_for_window(self, timeout_s: int | None = None) -> int:
        """Wait for the login-window process to end (returns once the user logs in and closes the window)."""
        import time as _t

        deadline = _t.monotonic() + (timeout_s or 900)
        while _t.monotonic() < deadline:
            rc = subprocess.run(
                ["tasklist", "/FI", f"PID eq {self._pid}", "/NH"],
                capture_output=True, text=True, shell=False).stdout
            if str(self._pid) not in rc:
                return 0
            _t.sleep(2)
        raise BrowserUnavailable(
            "login window still open after the timeout — close it when you are done")

    def fetch(self, url: str, wait_ms: int = DEFAULT_WAIT_MS,
              timeout_s: int = 60) -> FetchResult:
        """Render-fetch using the login state. Semantics match :meth:`BrowserFetch.fetch`."""
        fetcher = BrowserFetch(wait_ms=wait_ms, locale=self.locale, headed=False,
                               chrome_path=self.chrome_path, timeout_s=timeout_s)
        return fetcher._fetch_with_profile(url, str(self.persist_dir))


_RENDER_SCRIPT = r"""
// Note: when running `node -e <script> <args...>`, process.argv starts at index 1 for the parameters
// (argv[0] is the node executable itself, argv[1] is the first user parameter) —
// so we take them uniformly with slice(1) to avoid confusion with the `node file.mjs` form.
const { chromium } = require('playwright-core');
const A = process.argv.slice(1);
const [url, outPath, chromePath, profileDir, waitMs, locale, headed] = A;
(async () => {
  const ctx = await chromium.launchPersistentContext(profileDir, {
    executablePath: chromePath,
    headless: headed !== '1',
    locale: locale || 'en-SG',
    args: ['--no-sandbox', '--disable-dev-shm-usage'],
  });
  try {
    const page = ctx.pages()[0] || await ctx.newPage();
    const resp = await page.goto(url, { waitUntil: 'domcontentloaded', timeout: 45000 });
    await page.waitForTimeout(parseInt(waitMs || '8000', 10));
    const text = await page.evaluate(() => document.body ? document.body.innerText : '');
    const html = await page.content();
    const fs = require('fs');
    fs.writeFileSync(outPath, JSON.stringify({
      status: resp ? resp.status() : null,
      title: await page.title(),
      text: text || '',
      html: html || '',
      error: null,
    }));
  } catch (e) {
    const fs = require('fs');
    fs.writeFileSync(outPath, JSON.stringify({ status: null, title: '', text: '', html: '',
      error: String(e && e.message || e) }));
  } finally {
    await ctx.close();
  }
})();
"""

_LOGIN_SCRIPT = r"""
// User self-login window: credentials live only in the browser; this script touches no credentials.
const { chromium } = require('playwright-core');
const A = process.argv.slice(1);
const [url, profileDir, chromePath, locale, waitS] = A;
(async () => {
  const ctx = await chromium.launchPersistentContext(profileDir, {
    executablePath: chromePath,
    headless: false,
    locale: locale || 'en-SG',
    args: ['--no-sandbox', '--disable-dev-shm-usage'],
  });
  const page = ctx.pages()[0] || await ctx.newPage();
  await page.goto(url, { waitUntil: 'domcontentloaded', timeout: 45000 }).catch(() => {});
  console.log('Log in inside this browser window. It stays open for ' + waitS + ' seconds.');
  console.log('Credentials never touch this process — only the browser profile is kept.');
  await page.waitForTimeout(parseInt(waitS || '300', 10) * 1000);
  await ctx.close();
})();
"""


def _fetch_with_profile(self: BrowserFetch, url: str, profile_dir: str) -> FetchResult:
    """Login-state variant of BrowserFetch.fetch (reuses the persistent profile)."""
    try:
        self.preflight()
    except BrowserUnavailable as exc:
        return FetchResult(url=url, error=str(exc))
    with tempfile.TemporaryDirectory() as tmp:
        out = Path(tmp) / "out.json"
        cmd = [_node_binary(), "-e", _RENDER_SCRIPT, url, str(out),
               str(self.chrome_path), profile_dir, str(self.wait_ms),
               self.locale, "0"]
        try:
            proc = subprocess.run(cmd, cwd=str(_workspace()), capture_output=True,
                                  text=True, timeout=self.timeout_s,
                                  env={**os.environ, "PYTHONIOENCODING": "utf-8"})
        except subprocess.TimeoutExpired:
            return FetchResult(url=url, error=f"browser timed out after {self.timeout_s}s")
        if not out.exists():
            detail = (proc.stderr or proc.stdout or "").strip()[-400:]
            return FetchResult(url=url, error=f"render failed: {detail}")
        try:
            data = json.loads(out.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as exc:
            return FetchResult(url=url, error=f"unreadable output: {exc}")
    return FetchResult(url=url, status=data.get("status"), title=data.get("title", ""),
                       text=data.get("text", ""), html=data.get("html", "")[:262144],
                       error=data.get("error"))


BrowserFetch._fetch_with_profile = _fetch_with_profile   # type: ignore[attr-defined]
