"""Cookie 号出片方式：面板开关（默认纯 API）+ 浏览器模式下现建 profile。

离线回归：不访问上游、不起浏览器。
"""
import json
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from dola_pool import config
from test_account_throttle import _pool
from test_async_compat import _run


@pytest.fixture
def pure_default(monkeypatch):
    monkeypatch.setattr(config, "PURE_API_ENABLED", True)


def test_cookie_account_uses_the_browser_when_pure_api_is_off(tmp_path, monkeypatch, pure_default):
    browser_pool, bp, _ = _pool(tmp_path, monkeypatch, {"acc1": "a"})
    monkeypatch.setattr(browser_pool, "is_cloak_cookie_account", lambda name: False)
    bp._conn.execute("UPDATE accounts_meta SET source='cookie' WHERE name='acc1'")
    bp._conn.commit()
    monkeypatch.setattr(config, "PURE_API_ENABLED", False)
    assert bp.is_pure_account("acc1") is False


def test_cookie_account_in_browser_mode_generates_through_the_browser(tmp_path, monkeypatch, pure_default):
    browser_pool, bp, _ = _pool(tmp_path, monkeypatch, {"acc1": "a"})
    monkeypatch.setattr(browser_pool, "is_cloak_cookie_account", lambda name: False)
    bp._conn.execute("UPDATE accounts_meta SET source='cookie' WHERE name='acc1'")
    bp._conn.commit()
    monkeypatch.setattr(config, "PURE_API_ENABLED", False)
    calls = {}

    async def browser_generate(account, prompt, ratio, duration, **kwargs):
        calls["browser"] = account
        return {"local_path": str(tmp_path / "out.mp4")}

    def pure_generate(**kwargs):
        calls["pure"] = kwargs["account"]
        return {"local_path": str(tmp_path / "pure.mp4")}

    monkeypatch.setattr(browser_pool, "generate_video", browser_generate)
    monkeypatch.setattr(browser_pool, "generate_pure_video", pure_generate)
    monkeypatch.setattr(bp, "_web_playable", lambda result: _async_result(result))
    result = _run(bp._generate_effective(
        "acc1", "一只猫", "9:16", 5, "seedance-2.0", on_conversation_id=None,
        on_poll=None, on_balance=None, reference_image_paths=None))
    assert calls == {"browser": "acc1"} and result["local_path"].endswith("out.mp4")


def test_cookie_account_stays_on_pure_api_by_default(tmp_path, monkeypatch, pure_default):
    browser_pool, bp, _ = _pool(tmp_path, monkeypatch, {"acc1": "a"})
    monkeypatch.setattr(browser_pool, "is_cloak_cookie_account", lambda name: False)
    bp._conn.execute("UPDATE accounts_meta SET source='cookie' WHERE name='acc1'")
    bp._conn.commit()
    calls = {}

    async def browser_generate(*args, **kwargs):
        calls["browser"] = True
        return {"local_path": ""}

    def pure_generate(**kwargs):
        calls["pure"] = kwargs["cookie_state_path"]
        return {"local_path": str(tmp_path / "pure.mp4")}

    monkeypatch.setattr(browser_pool, "generate_video", browser_generate)
    monkeypatch.setattr(browser_pool, "generate_pure_video", pure_generate)
    monkeypatch.setattr(bp, "_web_playable", lambda result: _async_result(result))
    _run(bp._generate_effective(
        "acc1", "一只猫", "9:16", 5, "seedance-2.0", on_conversation_id=None,
        on_poll=None, on_balance=None, reference_image_paths=None))
    assert "browser" not in calls and calls["pure"].endswith("cookie_state.json")


async def _async_result(result):
    return result


def test_browser_mode_builds_the_profile_from_cookie_state_on_first_launch(tmp_path, monkeypatch):
    from dola_pool import browser

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(browser, "proxy_kwargs_for", lambda account: None)
    account = tmp_path / "accounts" / "acc1"
    account.mkdir(parents=True)
    state = {
        "cookies_list": [{"name": "sessionid", "value": "sid", "domain": ".dola.com", "path": "/",
                          "expires": 1893456000, "secure": True, "httpOnly": True, "sameSite": "Lax"}],
        "storage": {"localStorage": {"samantha_web_web_id": '{"web_id":"42"}'}},
    }
    (account / "cookie_state.json").write_text(json.dumps(state), encoding="utf-8")
    seen = {}

    class Context:
        def __init__(self):
            self.cookies_added = []
            self.closed = False
            self.pages = [self]

        async def add_cookies(self, cookies):
            self.cookies_added.extend(cookies)

        async def goto(self, url, **kwargs):
            seen["url"] = url

        async def evaluate(self, script, arg):
            seen["storage_keys"] = sorted((arg.get("localStorage") or {}))
            return {"local": 1, "session": 0, "indexedDB": 0, "errors": []}

        async def close(self):
            self.closed = True

    context = Context()

    async def launch(profile_dir, **kwargs):
        seen["profile"] = str(profile_dir)
        return context

    class Playwright:
        def __init__(self):
            self.chromium = SimpleNamespace(launch_persistent_context=launch)

    monkeypatch.setattr(browser, "is_cloak_cookie_account", lambda name: False)
    result = _run(browser.launch_account_context(Playwright(), "acc1"))
    assert result is context and not context.closed
    assert Path(seen["profile"]).resolve() == account.resolve()
    assert context.cookies_added and context.cookies_added[0]["name"] == "sessionid"
    assert seen["url"] == "https://www.dola.com/chat"
    assert seen["storage_keys"] == ["samantha_web_web_id"]


def test_browser_mode_launch_leaves_an_existing_profile_untouched(tmp_path, monkeypatch):
    from dola_pool import browser

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(browser, "proxy_kwargs_for", lambda account: None)
    account = tmp_path / "accounts" / "acc1"
    (account / "Default").mkdir(parents=True)
    (account / "cookie_state.json").write_text(json.dumps({
        "cookies_list": [{"name": "sessionid", "value": "sid", "domain": ".dola.com", "path": "/"}],
    }), encoding="utf-8")
    marker = account / "Default" / "keep-me"
    marker.write_text("untouched", encoding="utf-8")

    class Context:
        def __init__(self):
            self.cookies_added = []

        async def add_cookies(self, cookies):
            self.cookies_added.extend(cookies)

    context = Context()

    async def launch(profile_dir, **kwargs):
        return context

    monkeypatch.setattr(browser, "is_cloak_cookie_account", lambda name: False)
    _run(browser.launch_account_context(SimpleNamespace(
        chromium=SimpleNamespace(launch_persistent_context=launch)), "acc1"))
    assert context.cookies_added == []                 # 已有 profile 不再灌入导入的 cookie
    assert marker.read_text(encoding="utf-8") == "untouched"


def test_browser_mode_launch_closes_the_browser_when_materializing_fails(tmp_path, monkeypatch):
    from dola_pool import browser

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(browser, "proxy_kwargs_for", lambda account: None)
    account = tmp_path / "accounts" / "acc1"
    account.mkdir(parents=True)
    (account / "cookie_state.json").write_text("{ broken", encoding="utf-8")
    closed = []

    class Context:
        async def close(self):
            closed.append(True)

    async def launch(profile_dir, **kwargs):
        (Path(profile_dir) / "Default").mkdir()
        return Context()

    monkeypatch.setattr(browser, "is_cloak_cookie_account", lambda name: False)
    with pytest.raises(json.JSONDecodeError):
        _run(browser.launch_account_context(SimpleNamespace(
            chromium=SimpleNamespace(launch_persistent_context=launch)), "acc1"))
    assert closed == [True]
    assert not (account / "Default").exists()          # 建到一半的 profile 不留着


def test_login_account_without_cookie_state_launches_as_before(tmp_path, monkeypatch):
    from dola_pool import browser

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(browser, "proxy_kwargs_for", lambda account: None)
    account = tmp_path / "accounts" / "acc1"
    account.mkdir(parents=True)
    launched = {}

    async def launch(profile_dir, **kwargs):
        launched["dir"] = str(profile_dir)
        return SimpleNamespace()

    monkeypatch.setattr(browser, "is_cloak_cookie_account", lambda name: False)
    _run(browser.launch_account_context(SimpleNamespace(
        chromium=SimpleNamespace(launch_persistent_context=launch)), "acc1"))
    assert Path(launched["dir"]).resolve() == account.resolve()
    assert not (account / "Default").exists()          # 登录号不造 profile


# ---------------------------------------------------------------- 面板开关

@pytest.fixture
def settings_server(monkeypatch, tmp_path):
    from dola_pool import server as server_mod

    monkeypatch.setattr(server_mod, "ADMIN_SESSIONS",
                        {"test-token": {"username": "admin", "expires_at": 1e13}})
    monkeypatch.setattr(server_mod, "user_get", lambda _: {"enabled": True})
    saved = {}
    monkeypatch.setattr(server_mod.store, "get_setting", lambda key: saved.get(key))
    monkeypatch.setattr(server_mod.store, "set_setting", lambda key, value: saved.__setitem__(key, value))
    monkeypatch.setattr(config, "PURE_API_ENABLED", True)
    return server_mod, saved


def _call(server_mod, method, path, body=None, token="test-token"):
    async def go():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=server_mod.app),
                                     base_url="http://test") as c:
            return await c.request(method, path, json=body,
                                   headers={"X-Admin-Key": token} if token else {})
    return _run(go())


def test_cookie_mode_endpoint_requires_an_admin_session(settings_server):
    server_mod, _ = settings_server
    assert _call(server_mod, "GET", "/api/admin/settings/cookie-mode", token=None).status_code == 401
    assert _call(server_mod, "PATCH", "/api/admin/settings/cookie-mode",
                 {"mode": "browser"}, token="invalid").status_code == 401


def test_cookie_mode_defaults_to_pure_and_reports_browser_availability(settings_server, monkeypatch):
    server_mod, _ = settings_server
    monkeypatch.setattr(server_mod, "browser_mode_blocker", lambda: "")
    data = _call(server_mod, "GET", "/api/admin/settings/cookie-mode").json()
    assert data == {"mode": "pure", "browser_available": True, "reason": ""}


def test_cookie_mode_switch_takes_effect_immediately_and_persists(settings_server, monkeypatch):
    server_mod, saved = settings_server
    monkeypatch.setattr(server_mod, "browser_mode_blocker", lambda: "")
    r = _call(server_mod, "PATCH", "/api/admin/settings/cookie-mode", {"mode": "browser"})
    assert r.status_code == 200 and r.json()["mode"] == "browser"
    assert config.PURE_API_ENABLED is False and saved[server_mod.COOKIE_MODE_KEY] == "browser"
    r = _call(server_mod, "PATCH", "/api/admin/settings/cookie-mode", {"mode": "pure"})
    assert r.json()["mode"] == "pure" and config.PURE_API_ENABLED is True


def test_cookie_mode_rejects_browser_when_the_browser_is_unusable(settings_server, monkeypatch):
    server_mod, saved = settings_server
    monkeypatch.setattr(server_mod, "browser_mode_blocker", lambda: "没找到 Chromium 内核")
    r = _call(server_mod, "PATCH", "/api/admin/settings/cookie-mode", {"mode": "browser"})
    assert r.status_code == 422 and "Chromium" in r.json()["detail"]
    assert config.PURE_API_ENABLED is True and server_mod.COOKIE_MODE_KEY not in saved


def test_unset_cookie_mode_keeps_the_env_and_does_not_probe(monkeypatch):
    from dola_pool import cookie_mode

    monkeypatch.setattr(config, "PURE_API_ENABLED", False)

    def boom():
        raise AssertionError("没存过设置不该探测浏览器")

    monkeypatch.setattr(cookie_mode, "unavailable_reason", boom)
    view = cookie_mode.apply_saved(None)
    assert config.PURE_API_ENABLED is False and view["mode"] == "browser"

    monkeypatch.setattr(config, "PURE_API_ENABLED", False)
    view = cookie_mode.apply_saved("pure")
    assert config.PURE_API_ENABLED is True and view["mode"] == "pure"


def test_saved_browser_mode_falls_back_to_pure_when_the_browser_is_gone(settings_server, monkeypatch):
    from dola_pool import cookie_mode

    server_mod, saved = settings_server
    saved[server_mod.COOKIE_MODE_KEY] = "browser"
    monkeypatch.setattr(cookie_mode, "unavailable_reason", lambda: "驱动无法执行")
    view = cookie_mode.apply_saved("browser")
    assert view["mode"] == "pure" and view["browser_available"] is False
    assert config.PURE_API_ENABLED is True


def test_unknown_cookie_mode_is_rejected(settings_server, monkeypatch):
    server_mod, _ = settings_server
    monkeypatch.setattr(server_mod, "browser_mode_blocker", lambda: "")
    assert _call(server_mod, "PATCH", "/api/admin/settings/cookie-mode",
                 {"mode": "edge"}).status_code == 422


def test_panel_has_a_cookie_mode_entry_wired_to_the_endpoint():
    html = (Path(__file__).resolve().parents[1] / "web" / "index.html").read_text(encoding="utf-8")
    assert 'onclick="closeTopMenu();openCookieModeSettings()"' in html
    assert "/api/admin/settings/cookie-mode" in html
    assert "Cookie 号出片方式" in html
