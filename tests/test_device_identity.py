"""dola 纯 API 的 device_id：页面发的就是 launch/core 返回的 web_id。

离线回归：不访问上游。device_id 的取值依据是 dola 前端打包代码
（samantha_web_web_id.web_id 来自 /alice/user/launch/core 的 core.web_id，
请求公共参数的 device_id 直接用它）。这里只锁住我们这边的对应行为。
"""
import json
from pathlib import Path

import pytest
import requests

from dola_pool.protocol import dola_pure_api as dola
from dola_pool import pure_api_gen


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("离线回归禁止真实网络请求")
    monkeypatch.setattr(requests.sessions.Session, "request", forbidden)


def test_launch_core_web_id_becomes_device_id_and_nothing_else_moves():
    ctx = dola.fresh_ctx("JP", "test")
    ctx["web_id"] = "tea-web"
    ctx["tea_uuid"] = "tea-web"
    dola.apply_launch_core_to_ctx(ctx, {"code": 0, "data": {"core": {"web_id": "core-web", "ttwid": "x"}}})
    assert ctx["device_id"] == "core-web"
    assert ctx["web_id"] == ctx["tea_uuid"] == "tea-web"   # tea 的 web_id 不动


@pytest.mark.parametrize("payload", [
    {"code": 0, "data": {"core": {"web_id": ""}}},
    {"code": 0, "data": {"core": {"web_id": "0"}}},
    {"code": 0, "data": {}},
    {"code": 0, "data": "not-an-object"},
    {"code": 0},
])
def test_launch_core_without_a_web_id_leaves_the_context_alone(payload):
    ctx = dola.fresh_ctx("JP", "test")
    ctx["device_id"] = "kept"
    dola.apply_launch_core_to_ctx(ctx, payload)
    assert ctx["device_id"] == "kept"


def test_saved_device_id_is_never_overwritten():
    ctx = dola.fresh_ctx("JP", "test")
    dola.apply_state_to_ctx(ctx, {"ctx": {"device_id": "saved-device", "web_id": "saved-web"}})
    dola.apply_launch_core_to_ctx(ctx, {"code": 0, "data": {"core": {"web_id": "fresh"}}})
    assert ctx["device_id"] == "saved-device"


def test_device_id_is_reused_from_the_saved_context(tmp_path):
    state = tmp_path / "cookie_state.json"
    state.write_text(json.dumps({
        "cookies_list": [{"name": "sessionid", "value": "test", "domain": ".dola.com"}],
        "ctx": {"device_id": "saved-device", "web_id": "saved-web", "fp": "saved-fp"},
    }), encoding="utf-8")
    session, ctx, _ = dola.setup_session(login_pure=True, login_state_file=state)
    try:
        assert ctx["device_id"] == "saved-device"
        assert ctx["web_id"] == "saved-web" and ctx["fp"] == "saved-fp"
        assert dola.base_params(ctx)["device_id"] == "saved-device"
    finally:
        session.close()


def test_identity_is_saved_atomically_and_keeps_everything_else(tmp_path):
    state = tmp_path / "cookie_state.json"
    original = {
        "source": "cookie",
        "cookies_list": [{"name": "sessionid", "value": "keep-me", "domain": ".dola.com"}],
        "storage": {"localStorage": {"k": "v"}},
        "ctx": {"keep": "yes", "web_id": "old"},
    }
    state.write_text(json.dumps(original), encoding="utf-8")
    ctx = {"fp": "new-fp", "web_id": "new-web", "tea_uuid": "new-web", "device_id": "new-device",
           "region": "JP", "aid": "495671"}
    dola.save_identity_ctx(state, ctx)
    saved = json.loads(state.read_text(encoding="utf-8"))
    assert saved["cookies_list"] == original["cookies_list"]
    assert saved["storage"] == original["storage"] and saved["source"] == "cookie"
    assert saved["ctx"]["keep"] == "yes"                       # 原有字段保留
    assert saved["ctx"]["device_id"] == "new-device" and saved["ctx"]["web_id"] == "new-web"
    assert saved["ctx"]["fp"] == "new-fp" and saved["ctx"]["tea_uuid"] == "new-web"
    assert "region" not in saved["ctx"] and "aid" not in saved["ctx"]
    assert not list(tmp_path.glob(".cookie_state_*.tmp"))
    assert state.stat().st_mode & 0o777 == 0o600


def test_identity_save_does_not_create_a_missing_state_file(tmp_path):
    missing = tmp_path / "nope.json"
    dola.save_identity_ctx(missing, {"device_id": "x"})
    assert not missing.exists()


def test_identity_save_keeps_the_file_when_the_state_is_unreadable(tmp_path):
    state = tmp_path / "cookie_state.json"
    state.write_text("{ this is not json", encoding="utf-8")
    dola.save_identity_ctx(state, {"device_id": "x"})
    assert state.read_text(encoding="utf-8") == "{ this is not json"


def test_prepare_session_fills_device_id_from_launch_core_and_persists_it(tmp_path, monkeypatch):
    state = tmp_path / "cookie_state.json"
    state.write_text(json.dumps({
        "cookies_list": [{"name": "sessionid", "value": "test", "domain": ".dola.com"}],
        "storage": {"localStorage": {"__tea_cache_tokens_495671": '{"web_id":"tea-web"}'}},
    }), encoding="utf-8")
    calls = []

    def post(session, ctx, endpoint, body, **kwargs):
        calls.append((endpoint, ctx.get("device_id")))
        if endpoint == "/alice/user/get_web_anon_id":
            # 真实的 dola_api_post_json 会把 anon 身份套进 ctx；桩要照做。
            dola.apply_anon_id_to_ctx(ctx, {"web_id": "anon-web", "uid": "7"})
            return {"code": 0, "data": {"web_id": "anon-web", "uid": "7"}}
        if endpoint == "/alice/user/launch/core":
            return {"code": 0, "data": {"core": {"web_id": "core-web"}}}
        return {"code": 0, "data": {}}

    monkeypatch.setattr(dola, "dola_api_post_json", post)
    monkeypatch.setitem(pure_api_gen.prepare_pure_session.__globals__, "_dola", lambda: dola)
    session, ctx, _ = pure_api_gen.prepare_pure_session(state, proxy="")
    try:
        assert [c[0] for c in calls] == [
            "/alice/user/get_web_anon_id", "/alice/user/launch/core", "/alice/user/launch"]
        assert calls[1][1] == ""               # launch/core 之前 device_id 还是空
        assert ctx["device_id"] == "core-web"
        # web_id 以 get_web_anon_id 的返回为准（原有行为，请求 URL 不带 web_id）；
        # 真正要稳定的是 device_id，它来自 launch/core 并写回 ctx。
        assert ctx["web_id"] == "anon-web"
    finally:
        session.close()
    saved = json.loads(state.read_text(encoding="utf-8"))
    assert saved["ctx"]["device_id"] == "core-web"
    assert saved["ctx"]["web_id"] == "anon-web" and saved["ctx"]["fp"]


def test_prepare_session_keeps_going_when_launch_core_fails(tmp_path, monkeypatch):
    state = tmp_path / "cookie_state.json"
    state.write_text(json.dumps({
        "cookies_list": [{"name": "sessionid", "value": "test", "domain": ".dola.com"}],
    }), encoding="utf-8")

    def post(session, ctx, endpoint, body, **kwargs):
        if endpoint == "/alice/user/launch/core":
            raise dola.DolaError("launch/core returned error: {}")
        if endpoint == "/alice/user/get_web_anon_id":
            dola.apply_anon_id_to_ctx(ctx, {"web_id": "anon-web"})
        return {"code": 0, "web_id": "anon-web"}

    monkeypatch.setattr(dola, "dola_api_post_json", post)
    monkeypatch.setitem(pure_api_gen.prepare_pure_session.__globals__, "_dola", lambda: dola)
    session, ctx, _ = pure_api_gen.prepare_pure_session(state, proxy="")
    try:
        assert ctx["device_id"] == ""          # 拿不到就保持空，和旧行为一致
        assert ctx["web_id"] == "anon-web"
    finally:
        session.close()
    assert "ctx" not in json.loads(state.read_text(encoding="utf-8"))
