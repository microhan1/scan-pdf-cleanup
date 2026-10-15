"""The test run never reaches the live site.

A GUI test that keeps the event loop turning for more than DELAY_MS would otherwise
let the real check go out: settings are isolated per test, so `update_check_last` is
always empty and the once-a-day guard is always open. Each such request is counted
on the site as someone starting the tool (that is what happened to 사진 정리:
511 requests in two days showed up as real launches).

Patching the module attribute is not enough. `fetch(*, opener=urllib.request.urlopen)`
and `check_async(*, fetcher=fetch)` bind their defaults when the module is imported,
so the defaults themselves are replaced here, along with `urllib.request.urlopen`.

Tests that need an answer pass their own `fetcher=` or `opener=` and are unaffected.
Anything that still tries to go out is recorded and fails that test, so a new test
cannot quietly start calling the site again.
"""
from __future__ import annotations

import os
import sys
import urllib.error
import urllib.request

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import update_check as uc  # noqa: E402

LIVE = "chaekgalpi.co.kr"


class Recorder:
    """What a test tried to send. `expected` is set by the guard's own test."""

    def __init__(self) -> None:
        self.calls: list[str] = []
        self.expected = False


@pytest.fixture(autouse=True)
def block_live_site(monkeypatch):
    rec = Recorder()

    def opener(req, *args, **kwargs):
        url = str(getattr(req, "full_url", req))
        if LIVE in url:
            rec.calls.append(url)
            raise urllib.error.URLError("the test run must not reach the live site")
        raise urllib.error.URLError(f"no network in tests: {url}")

    def fetcher(slug, version):
        rec.calls.append(f"fetch({slug}, {version}) with no fetcher given")
        return None  # as being offline does: nothing is shown, the day is not used up

    monkeypatch.setattr(urllib.request, "urlopen", opener)
    monkeypatch.setitem(uc.fetch.__kwdefaults__, "opener", opener)
    monkeypatch.setitem(uc.check_async.__kwdefaults__, "fetcher", fetcher)

    yield rec

    if rec.calls and not rec.expected:
        pytest.fail("this test tried to reach the live site:\n  " + "\n  ".join(rec.calls))
