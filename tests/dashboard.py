"""Harness for testing the dashboard in a real browser.

The frontend is 1,700 lines of vanilla JS with no build step, and every bug found
in it so far - the XSS, a missing field blanking the page, the sticky tooltip,
absent readings rendering green - was caught by driving headless Chrome by hand.
None of it was pinned down, so any of it could come back silently.

This renders the real `site/` against fixture JSON and returns the resulting DOM
to assert against. No new dependencies: Chrome is already on the machine and
Python already serves files.

Renders are **cached by scenario**, because launching Chrome costs a second or
two and most tests share a handful of setups. Ask for the same fixtures twice
and the second call is free.
"""
import http.server
import json
import shutil
import socket
import subprocess
import tempfile
import threading
from functools import lru_cache
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SITE = REPO / "site"

CHROME_CANDIDATES = [
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    "/Applications/Chromium.app/Contents/MacOS/Chromium",
    "/usr/bin/google-chrome",
    "/usr/bin/chromium",
]

# Enough for fetch + render; the page has no network calls beyond its own JSON.
VIRTUAL_TIME_BUDGET_MS = 2500


def chrome_path() -> str | None:
    return next((c for c in CHROME_CANDIDATES if Path(c).exists()), None)


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class _QuietHandler(http.server.SimpleHTTPRequestHandler):
    """SimpleHTTPRequestHandler logs every request to stderr; tests shouldn't."""

    def log_message(self, *args):
        pass


def _serve(directory: Path):
    handler = lambda *a, **kw: _QuietHandler(*a, directory=str(directory), **kw)  # noqa: E731
    port = _free_port()
    server = http.server.ThreadingHTTPServer(("127.0.0.1", port), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, port


def _apply(data_dir: Path, overrides: dict):
    """Applies fixture overrides to the copied site data.

    A value of None deletes the file, which is how "this optional feed is
    missing" is expressed. A dict is written as-is. A callable receives the real
    parsed JSON and returns the modified version, so a test can express "the
    real data, but without this one field".
    """
    for name, override in (overrides or {}).items():
        path = data_dir / name
        if override is None:
            path.unlink(missing_ok=True)
            continue
        if callable(override):
            current = json.loads(path.read_text()) if path.exists() else {}
            override = override(current)
        path.write_text(json.dumps(override))


def _dump_dom(overrides: dict) -> str:
    """Copies the real site, applies fixtures, serves it, returns the rendered DOM."""
    chrome = chrome_path()
    if chrome is None:
        raise RuntimeError("no chrome")

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp) / "site"
        shutil.copytree(SITE, root)
        _apply(root / "data", overrides)

        server, port = _serve(root)
        try:
            result = subprocess.run(
                [chrome, "--headless", "--disable-gpu", "--no-sandbox",
                 f"--virtual-time-budget={VIRTUAL_TIME_BUDGET_MS}",
                 "--dump-dom", f"http://127.0.0.1:{port}/index.html"],
                capture_output=True, text=True, timeout=60,
            )
        finally:
            server.shutdown()
    return result.stdout


@lru_cache(maxsize=32)
def _render_cached(overrides_json: str) -> str:
    return _dump_dom(json.loads(overrides_json))


def render(**overrides) -> str:
    """Renders the dashboard and returns its DOM.

    Fixture files are named without the `data/` prefix:

        render()                                  # the real data, unmodified
        render(**{"weather.json": DELETE})        # that optional feed missing
        render(**{"history.json": strip_targets}) # a callable mutates the real data

    Renders are cached by scenario, since Chrome costs a second or two to launch
    and most tests share a handful of setups. Callables can't be compared by
    value, so those render fresh each time - keep them to the few tests that
    genuinely need to mutate real data.
    """
    if any(callable(v) for v in overrides.values()):
        return _dump_dom(overrides)
    return _render_cached(json.dumps(overrides, sort_keys=True))


# Passed as a fixture value to remove that file entirely.
DELETE = None


def count(dom: str, needle: str) -> int:
    return dom.count(needle)


def waterbody(data: dict) -> dict:
    """The single water body in a history.json fixture."""
    return next(iter(data["waterbodies"].values()))
