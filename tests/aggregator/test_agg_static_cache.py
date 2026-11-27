"""A new version's scripts are fetched at once, not from the browser's cache."""

import re

import pytest

from aggregator import api, config


@pytest.fixture
async def web(aiohttp_client, agg_store, monkeypatch):
    monkeypatch.setattr(config, "BUILD_VERSION", "v9.9.9")
    api._index_cache.clear()
    client = await aiohttp_client(api.create_web_app())
    yield client
    api._index_cache.clear()


async def test_index_links_our_assets_with_the_version(web):
    resp = await web.get("/")
    assert resp.status == 200
    assert resp.headers["Cache-Control"] == "no-cache"
    html = await resp.text()
    assert 'src="/static/js/aggregator.js?v=v9.9.9"' in html
    assert 'href="/static/shared/app.css?v=v9.9.9"' in html
    assert 'src="/static/shared/theme.js?v=v9.9.9"' in html
    # vendor files carry their version in the name; icons are left alone
    assert re.search(r'vendor/wavesurfer-[\d.]+\.min\.js"', html)
    assert 'favicon.svg"' in html


async def test_static_files_are_revalidated(web):
    resp = await web.get("/static/js/aggregator.js?v=v9.9.9")
    assert resp.status == 200
    assert resp.headers["Cache-Control"] == "no-cache"
    assert "ETag" in resp.headers or "Last-Modified" in resp.headers


async def test_api_is_not_given_the_static_header(web):
    resp = await web.get("/api/v1/status")
    assert resp.headers.get("Cache-Control") != "no-cache"
