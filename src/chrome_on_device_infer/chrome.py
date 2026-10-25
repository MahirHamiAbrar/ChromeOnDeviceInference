"""Finding, launching, and talking to Chrome, plus a throwaway local page.

This module owns everything that is about *the browser process* rather than the
model: locating the binary, the ``XDG_CONFIG_HOME`` workaround for Chrome's
remote-debugging restriction, the HTTP endpoints that hand out CDP WebSocket
URLs, and a tiny ``127.0.0.1`` page server used as the Prompt API's execution
context.
"""

from __future__ import annotations

import http.server
import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

from .cdp import CDPClient

# Temp XDG_CONFIG_HOME created for a launch; removed once the process is gone.
_xdg_temp: str | None = None


# ---------------------------------------------------------------------------
# Locating things
# ---------------------------------------------------------------------------
def default_user_data_dir() -> str:
    """Chrome's default profile directory for this platform."""
    if sys.platform == "darwin":
        return os.path.expanduser("~/Library/Application Support/Google/Chrome")
    if os.name == "nt":
        return os.path.join(
            os.environ.get("LOCALAPPDATA", os.path.expanduser("~")),
            "Google", "Chrome", "User Data",
        )
    base = os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config")
    return os.path.join(base, "google-chrome")


def find_chrome() -> str | None:
    """Return a path to a Chrome/Chromium binary, or None."""
    for name in (
        "google-chrome",
        "google-chrome-stable",
        "google-chrome-beta",
        "chromium",
        "chromium-browser",
        "chrome",
    ):
        path = shutil.which(name)
        if path:
            return path
    for path in ("/opt/google/chrome/chrome", "/usr/bin/google-chrome"):
        if os.path.exists(path):
            return path
    return None


def chrome_is_running(user_data_dir: str) -> bool:
    """True only if a *live* Chrome holds this profile.

    Chrome leaves a ``SingletonLock`` symlink pointing at ``<host>-<pid>``.  The
    symlink can outlive a crash or a clean quit, so we resolve the pid and check
    that it actually exists before believing the lock.
    """
    lock = os.path.join(user_data_dir, "SingletonLock")
    if not os.path.lexists(lock):
        return False
    try:
        target = os.readlink(lock)
        pid = int(target.rsplit("-", 1)[-1])
    except (OSError, ValueError):
        return True  # unparseable; assume the worst
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False  # stale lock
    except PermissionError:
        return True
    return True


# ---------------------------------------------------------------------------
# Launching
# ---------------------------------------------------------------------------
def _prepare_xdg_config() -> str | None:
    """Point XDG_CONFIG_HOME at a temporary mirror of ``~/.config``.

    Chrome 136+ refuses to open a remote-debugging port when the data directory
    is the *default* one, but the check compares against Chrome's computed
    default (``$XDG_CONFIG_HOME/google-chrome``).  By making XDG_CONFIG_HOME a
    temp directory that mirrors the user's config (so fonts and friends still
    resolve) but has no ``google-chrome`` child, the real profile path becomes
    "non-default" and the check passes.

    Returns the temp directory, or None if it could not be prepared.
    """
    config = os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config")
    if not os.path.isdir(config):
        return None

    global _xdg_temp
    temp = os.path.join(tempfile.gettempdir(), f"chrome-cdp-xdg-{os.getpid()}")
    try:
        os.makedirs(temp, exist_ok=True)
        for name in os.listdir(config):
            if name == "google-chrome":
                continue  # must stay absent, or the default path would match
            link = os.path.join(temp, name)
            if not os.path.lexists(link):
                os.symlink(os.path.join(config, name), link)
    except OSError:
        return None
    _xdg_temp = temp
    return temp


def cleanup_xdg_config() -> None:
    """Remove the temp XDG_CONFIG_HOME, if one was created."""
    global _xdg_temp
    if _xdg_temp is not None:
        shutil.rmtree(_xdg_temp, ignore_errors=True)
        _xdg_temp = None


def launch_chrome(chrome: str, user_data_dir: str, port: int) -> subprocess.Popen:
    """Start Chrome with a DevTools port, reusing the given profile."""
    env = dict(os.environ)
    if sys.platform.startswith("linux"):
        xdg = _prepare_xdg_config()
        if xdg:
            env["XDG_CONFIG_HOME"] = xdg

    args = [
        chrome,
        f"--remote-debugging-port={port}",
        "--remote-allow-origins=*",
        f"--user-data-dir={user_data_dir}",
        "--no-first-run",
        "--no-default-browser-check",
        # Expose the web Prompt API even if the profile never set the flag.
        "--enable-features=AIPromptAPI,AIPromptAPILegacyIdentifiers",
    ]
    kwargs = {}
    if os.name != "nt":
        # Own process group: Chrome re-execs itself, so signalling the original
        # pid alone is unreliable.
        kwargs["start_new_session"] = True
    return subprocess.Popen(
        args, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, **kwargs
    )


def terminate_chrome(proc: subprocess.Popen) -> None:
    """Stop a Chrome we launched, escalating from TERM to KILL."""
    if os.name == "nt":
        proc.terminate()
        try:
            proc.wait(timeout=8)
        except subprocess.TimeoutExpired:
            proc.kill()
        return

    try:
        pgid = os.getpgid(proc.pid)
    except ProcessLookupError:
        return
    for sig in (signal.SIGTERM, signal.SIGKILL):
        try:
            os.killpg(pgid, sig)
        except ProcessLookupError:
            return
        try:
            proc.wait(timeout=8)
            return
        except subprocess.TimeoutExpired:
            continue


# ---------------------------------------------------------------------------
# HTTP side of the DevTools Protocol
# ---------------------------------------------------------------------------
def cdp_http(port: int, path: str, method: str = "GET"):
    """Call one of Chrome's HTTP DevTools endpoints and parse the JSON."""
    request = urllib.request.Request(f"http://127.0.0.1:{port}{path}", method=method)
    with urllib.request.urlopen(request, timeout=10) as response:
        return json.loads(response.read().decode("utf-8"))


def wait_for_cdp(port: int, timeout: float = 45.0, proc: subprocess.Popen | None = None) -> bool:
    """Block until the DevTools HTTP endpoint answers (or Chrome exits)."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        if proc is not None and proc.poll() is not None:
            return False
        try:
            cdp_http(port, "/json/version")
            return True
        except Exception:
            time.sleep(0.3)
    return False


def open_page(port: int, url: str) -> str:
    """Create (or reuse) a top-level page target; return its WebSocket URL."""
    query = "/json/new?" + urllib.parse.quote(url, safe="")
    for method in ("PUT", "GET"):
        try:
            target = cdp_http(port, query, method=method)
            if target.get("webSocketDebuggerUrl"):
                return target["webSocketDebuggerUrl"]
        except urllib.error.HTTPError:
            continue
    for target in cdp_http(port, "/json/list"):
        if target.get("type") == "page" and target.get("webSocketDebuggerUrl"):
            return target["webSocketDebuggerUrl"]
    raise RuntimeError("could not find or create a page target in Chrome")


def prepare_page(client: CDPClient, url: str, timeout: float = 15.0) -> bool:
    """Navigate and wait until the Prompt API exists on the loaded document.

    This matters: a freshly created target starts on ``about:blank``, which has
    an opaque origin and therefore no ``LanguageModel`` binding.  Evaluating too
    early reports the API as missing.
    """
    try:
        client.call("Page.enable")
    except Exception:
        pass
    try:
        client.call("Page.navigate", {"url": url})
    except Exception:
        pass

    deadline = time.time() + timeout
    loaded_at = None
    while time.time() < deadline:
        try:
            state = client.evaluate(
                "({r: document.readyState,"
                " lm: typeof globalThis.LanguageModel === 'function'})"
            )
        except Exception:
            state = None
        if state and state.get("lm"):
            return True
        if state and state.get("r") == "complete":
            loaded_at = loaded_at or time.time()
            if time.time() - loaded_at > 3:
                return True  # loaded but no API; let the caller report it
        time.sleep(0.3)
    return False


# ---------------------------------------------------------------------------
# Local execution context
# ---------------------------------------------------------------------------
_PAGE_HTML = b"<!doctype html><meta charset=utf-8><title>prompt</title><body>ok</body>"


class _PageHandler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):  # noqa: N802 (BaseHTTPRequestHandler API)
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(_PAGE_HTML)))
        self.end_headers()
        self.wfile.write(_PAGE_HTML)

    def log_message(self, *args):  # keep the console quiet
        pass


class LocalPageServer:
    """Serves a blank page over ``http://127.0.0.1:<port>/``.

    localhost is a *secure context*, which is what the Prompt API requires, so
    this gives us a valid execution context without any network access or
    third-party page.
    """

    def __init__(self):
        self._server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _PageHandler)
        self.port = self._server.server_address[1]
        threading.Thread(target=self._server.serve_forever, daemon=True).start()

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}/"

    def close(self) -> None:
        try:
            self._server.shutdown()
            self._server.server_close()
        except Exception:
            pass
