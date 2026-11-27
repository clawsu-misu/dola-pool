"""1024Proxy 专用流程（proxy_1024）：账密隧道、API 提取、与通用代理设置的解耦、旧预设记录的迁移。

不访问真实账号与外网：提取接口用 monkeypatch 掉的 requests.get 模拟。
"""
from __future__ import annotations

import asyncio
import json
import os
import sqlite3
import subprocess
import sys
import time
import types
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest

from dola_pool import config
from dola_pool import proxy_1024
from dola_pool import proxy_store

REPO_ROOT = Path(__file__).resolve().parents[1]
# 控制台生成的链接原样：地区默认 Rand（随机）、一次 1 个、粘性 10 分钟
LINK = "https://white.1024proxy.com/white/api?region=Rand&num=1&time=10&format=1&type=txt"


@pytest.fixture(autouse=True)
def _stubs():
    """没装 patchright 时也能导入 server / browser_pool（与 test_batch_delete 一致）。"""
    sys.modules.setdefault("patchright", types.ModuleType("patchright"))
    pa = types.ModuleType("patchright.async_api")

    def _no(*_a, **_k):
        raise RuntimeError("stub")

    pa.async_playwright = _no
    sys.modules.setdefault("patchright.async_api", pa)
    yield


@pytest.fixture(autouse=True)
def _isolated_db(tmp_path, monkeypatch):
    monkeypatch.setattr(proxy_store, "DB_PATH", tmp_path / "pool_usage.db")
    monkeypatch.setattr(config, "PROXY", "")
    proxy_store._ready_paths.clear()
    yield


@pytest.fixture
def no_spacing(monkeypatch):
    """提取不设间隔：同一个用例里连续取 / 连续换 IP 都立刻再提一次。"""
    monkeypatch.setattr(proxy_1024, "API_SPACING", 0)


class _Resp:
    def __init__(self, text: str):
        self.text = text

    def raise_for_status(self):
        pass


def _stub_api(monkeypatch, body, calls=None):
    """body 是字符串，或「第几次调用 → 返回内容」的函数。"""
    import requests

    seen = []

    def fake_get(url, timeout=None, **kw):
        seen.append(url)
        if calls is not None:
            calls.append(url)
        return _Resp(body(len(seen)) if callable(body) else body)

    monkeypatch.setattr(requests, "get", fake_get)


def _mk_tunnel(**kw):
    options = {"name": "1024-tunnel", "gateway": "us.1024proxy.io:3000",
               "username": "acct", "password": "pw", **kw}
    return proxy_1024.create(kind="tunnel", **options)


def _mk_api(**kw):
    return proxy_1024.create(kind="api", **{"name": "1024-api", "link": LINK, **kw})


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


# ===== 账密隧道 =====

def test_tunnel_username_follows_official_format():
    pid = _mk_tunnel(region="KR", ttl=60)["id"]
    proxy_store.set_account_proxy("acc1", pid)
    url = proxy_store.proxy_url_for("acc1")
    session = proxy_store.get_session("acc1")["session"]
    assert url == f"http://acct-region-KR-sid-{session}-t-60:pw@us.1024proxy.io:3000"
    assert proxy_store.proxy_url_for("acc1") == url                      # 出口稳定
    assert proxy_store.egress_key_for("acc1") == f"http://us.1024proxy.io:3000#{session}"


def test_tunnel_record_is_a_provider_record():
    info = proxy_store.get_proxy(_mk_tunnel()["id"])
    assert info["provider"] == "1024proxy" and info["mode"] == "dynamic"
    assert json.loads(info["settings"]) == {"region": "JP", "ttl": 120}
    assert info["sticky_ttl"] == 7200 and info["session_template"] == ""


def test_tunnel_accounts_get_their_own_session():
    pid = _mk_tunnel()["id"]
    for name in ("acc1", "acc2"):
        proxy_store.set_account_proxy(name, pid)
    url1, url2 = proxy_store.proxy_url_for("acc1"), proxy_store.proxy_url_for("acc2")
    assert url1 != url2
    assert proxy_store.egress_key_for("acc1") != proxy_store.egress_key_for("acc2")
    assert proxy_store.get_session("acc1")["session"] != proxy_store.get_session("acc2")["session"]


def test_tunnel_rotate_changes_ip():
    pid = _mk_tunnel()["id"]
    proxy_store.set_account_proxy("acc1", pid)
    before = proxy_store.proxy_url_for("acc1")
    assert proxy_store.rotate_account_session("acc1", force=True)
    after = proxy_store.proxy_url_for("acc1")
    assert after != before and "-region-JP-" in after and "-t-120" in after


def test_tunnel_gateway_may_include_scheme():
    created = _mk_tunnel(name="gw", gateway="http://hk.1024proxy.io:3000/")
    assert (created["host"], created["port"]) == ("hk.1024proxy.io", 3000)


@pytest.mark.parametrize("kw,message", [
    ({"username": " "}, "账号"),
    ({"password": ""}, "密码"),
    ({"gateway": "us.1024proxy.io"}, "host:port"),
    ({"gateway": "us.1024proxy.io:70000"}, "host:port"),
    ({"gateway": ""}, "host:port"),
    ({"region": "US"}, "出口地区仅支持 JP（日本）或 KR（韩国）"),
    ({"region": ""}, "出口地区仅支持"),
    ({"ttl": 121}, "粘性时长取 1-120 分钟"),
    ({"ttl": 0}, "粘性时长取 1-120 分钟"),
])
def test_tunnel_create_validates_input(kw, message):
    with pytest.raises(ValueError, match=message):
        _mk_tunnel(**kw)
    assert proxy_store.list_proxies() == []            # 校验不过不留半条记录


def test_tunnel_update_changes_region_ttl_gateway_and_password():
    pid = _mk_tunnel()["id"]
    proxy_store.set_account_proxy("acc1", pid)
    proxy_store.proxy_url_for("acc1")
    proxy_1024.update(pid, region="KR", ttl=30)
    info = proxy_store.get_proxy(pid)
    assert info["sticky_ttl"] == 1800 and info["password"] == "pw"     # 不传密码 = 沿用
    assert info["provider_settings"] == {"region": "KR", "ttl": 30}
    session = proxy_store.get_session("acc1")["session"]
    assert proxy_store.proxy_url_for("acc1") == (
        f"http://acct-region-KR-sid-{session}-t-30:pw@us.1024proxy.io:3000")

    proxy_1024.update(pid, gateway="hk.1024proxy.io:3000", username="acct2", password="new")
    assert proxy_store.proxy_url_for("acc1") == (
        f"http://acct2-region-KR-sid-{session}-t-30:new@hk.1024proxy.io:3000")
    with pytest.raises(ValueError, match="账号"):
        proxy_1024.update(pid, username="  ")
    with pytest.raises(ValueError, match="代理名称不能为空"):
        proxy_1024.update(pid, name="  ")


# ===== API 提取 =====

def test_api_request_overrides_region_num_time(monkeypatch):
    pid = _mk_api(region="KR", num=5, time=15)["id"]
    calls = []
    _stub_api(monkeypatch, "".join(f"1.1.1.{i}:8080\r\n" for i in range(5)), calls)
    assert proxy_store.fetch_extract_nodes(pid)["added"] == 5
    query = parse_qs(urlsplit(calls[0]).query)
    assert query["region"] == ["KR"] and query["num"] == ["5"]
    assert query["time"] == ["15"] and query["type"] == ["txt"]
    assert query["format"] == ["1"]                              # 链接里的其它参数原样保留
    assert urlsplit(calls[0]).netloc == "white.1024proxy.com"


def test_api_nodes_live_for_the_sticky_minutes(monkeypatch):
    pid = _mk_api(time=15)["id"]
    _stub_api(monkeypatch, "\ufeff105.33.13.23:8080\r\n")      # 带 BOM 的 txt 也要认
    assert proxy_store.fetch_extract_nodes(pid)["added"] == 1
    node = proxy_store.list_nodes(pid)[0]
    assert (node["host"], node["port"], node["username"], node["password"]) == (
        "105.33.13.23", 8080, "", "")
    assert 800 < node["expires_at"] - time.time() <= 900         # txt 里没有凭据，时长来自链接


def test_api_json_body_is_accepted(monkeypatch):
    pid = _mk_api()["id"]
    _stub_api(monkeypatch, '[{"host":"165.1.2.3","port":"7098"}]')
    assert proxy_store.fetch_extract_nodes(pid)["added"] == 1
    assert proxy_store.list_nodes(pid)[0]["host"] == "165.1.2.3"


def test_api_uses_its_own_short_spacing():
    """通用节点池默认 60 秒限流；1024 API 按流量计费，不该让「换 IP」等一分钟。"""
    assert _mk_api()["extract_interval"] == proxy_1024.API_SPACING == 1


def test_api_accounts_get_a_node_each_on_demand(monkeypatch, no_spacing):
    pid = _mk_api()["id"]                                        # 链接一次只提 1 个
    calls = []
    _stub_api(monkeypatch, lambda n: f"10.0.0.{n}:8080\r\n", calls)
    names = ("acc1", "acc2", "acc3")
    for name in names:
        proxy_store.set_account_proxy(name, pid)
    urls = {proxy_store.proxy_url_for(name) for name in names}
    assert len(urls) == 3 and len(calls) == 3                    # 池里没有空闲地址就立刻各提一个
    assert all(url.startswith("http://10.0.0.") and url.endswith(":8080") for url in urls)
    assert len({proxy_store.egress_key_for(name) for name in names}) == 3


def test_api_rotate_gets_a_fresh_node_and_retires_the_old_one(monkeypatch, no_spacing):
    pid = _mk_api()["id"]
    calls = []
    _stub_api(monkeypatch, lambda n: f"10.0.0.{n}:8080\r\n", calls)
    proxy_store.set_account_proxy("acc1", pid)
    first = proxy_store.proxy_url_for("acc1")
    old_id = proxy_store.get_session("acc1")["node_id"]

    assert proxy_store.rotate_account_session("acc1", force=True)
    second = proxy_store.proxy_url_for("acc1")
    assert second != first
    # 旧地址从池里删掉：不会被别的号拿走，下次换 IP 也不会换回它
    assert [n["id"] for n in proxy_store.list_nodes(pid)] == [proxy_store.get_session("acc1")["node_id"]]
    assert proxy_store._node_by_id(old_id) == {}

    assert proxy_store.rotate_account_session("acc1", force=True)
    third = proxy_store.proxy_url_for("acc1")
    assert third not in (first, second)


def test_api_expired_node_is_replaced(monkeypatch, no_spacing):
    pid = _mk_api()["id"]
    _stub_api(monkeypatch, lambda n: f"10.0.0.{n}:8080\r\n")
    proxy_store.set_account_proxy("acc1", pid)
    first = proxy_store.proxy_url_for("acc1")
    conn = sqlite3.connect(str(proxy_store.DB_PATH))
    conn.execute("UPDATE proxy_nodes SET expires_at=?", (time.time() - 1,))
    conn.commit()
    conn.close()
    assert proxy_store.proxy_url_for("acc1") != first
    assert len(proxy_store.list_nodes(pid)) == 1                 # 过期的旧地址一并清掉


def test_api_fetch_failure_mentions_whitelist(monkeypatch):
    import requests

    def forbidden(*_a, **_k):
        raise requests.exceptions.HTTPError(
            "403 Client Error: Forbidden for url: https://white.1024proxy.com/white/api?region=JP&num=1")

    monkeypatch.setattr(requests, "get", forbidden)
    pid = _mk_api()["id"]
    result = proxy_store.fetch_extract_nodes(pid)
    assert result["ok"] is False
    assert "403" in result["error"] and "白名单" in result["error"]
    assert "white.1024proxy.com" not in result["error"]           # 链接不进报错
    assert "白名单" in proxy_store.get_proxy(pid)["extract_last_error"]


def test_api_error_body_is_surfaced(monkeypatch):
    pid = _mk_api()["id"]
    _stub_api(monkeypatch, "IP not in whitelist\n")
    result = proxy_store.fetch_extract_nodes(pid)
    assert result["ok"] is False and "IP not in whitelist" in result["error"]


def test_api_same_address_returned_twice_is_reported(monkeypatch, no_spacing):
    """接口连续返回同一个地址时说清楚原因，而不是笼统地报「没有空闲节点」。"""
    pid = _mk_api()["id"]
    _stub_api(monkeypatch, "105.33.13.23:8080\r\n")
    proxy_store.set_account_proxy("acc1", pid)
    proxy_store.set_account_proxy("acc2", pid)
    assert proxy_store.proxy_url_for("acc1")
    assert proxy_store.proxy_url_for("acc2") == ""               # 不让两个号共用一个出口
    assert "都已在节点池里" in proxy_store.get_proxy(pid)["extract_last_error"]


def test_rotate_ip_failure_shows_the_api_error(tmp_path, monkeypatch, no_spacing):
    from dola_pool import browser_pool
    pid = _mk_api()["id"]
    calls = []
    _stub_api(monkeypatch, lambda n: "10.0.0.1:8080\r\n" if n == 1 else "IP not in whitelist", calls)
    proxy_store.set_account_proxy("acc1", pid)
    pool = browser_pool.BrowserPool(accounts_dir=str(tmp_path / "accounts"),
                                    db_path=str(tmp_path / "bp.db"))
    proxy_store.proxy_url_for("acc1")
    result = pool.rotate_account_ip("acc1", force=True)
    assert result["rotated"] is False
    assert "IP not in whitelist" in result["reason"] and "白名单" in result["reason"]


@pytest.mark.parametrize("kw,message", [
    ({"link": ""}, "API 提取链接"),
    ({"link": "https://api.other-vendor.com/get?num=1"}, "API 提取链接"),
    ({"link": "white.1024proxy.com/white/api"}, "API 提取链接"),
    ({"region": "Rand"}, "出口地区仅支持"),
    ({"num": 0}, "每次提取数量至少为 1"),
    ({"time": 0}, "粘性时长取 1-120 分钟"),
    ({"time": 121}, "粘性时长取 1-120 分钟"),
])
def test_api_create_validates_input(kw, message):
    with pytest.raises(ValueError, match=message):
        _mk_api(**kw)
    assert proxy_store.list_proxies() == []


def test_api_update_keeps_unmentioned_fields():
    pid = _mk_api()["id"]
    updated = proxy_1024.update(pid, region="KR")
    info = proxy_store.get_proxy(pid)
    assert info["provider_settings"] == {"region": "KR", "num": 1, "time": 10}
    assert info["extract_url"] == LINK and info["extract_interval"] == 1
    proxy_1024.update(pid, num=4, time=15)
    assert proxy_store.get_proxy(pid)["provider_settings"] == {"region": "KR", "num": 4, "time": 15}


def test_unknown_kind_is_rejected():
    with pytest.raises(ValueError, match="接入方式"):
        proxy_1024.create(kind="rotating", name="x")


# ===== 与通用代理设置解耦 =====

def test_kind_cannot_be_changed_and_generic_records_are_rejected():
    tunnel = _mk_tunnel()["id"]
    with pytest.raises(ValueError, match="接入方式新建后不能改"):
        proxy_1024.update(tunnel, kind="api")
    generic = proxy_store.create_proxy("generic", "http", "1.2.3.4", 8080)["id"]
    with pytest.raises(ValueError, match="不是 1024Proxy 专用记录"):
        proxy_1024.update(generic, region="KR")


def test_generic_tunnel_still_uses_its_template():
    """通用隧道与 1024 的模板长得一样时，行为和以前完全一样，不会被专用流程接管。"""
    pid = proxy_store.create_proxy(
        "generic", "http", "gate.example.com", 8000, username="u", password="p",
        mode="dynamic", session_template="{user}-region-JP-sid-{session}-t-120")["id"]
    info = proxy_store.get_proxy(pid)
    assert info["provider"] == "" and info["provider_settings"] == {}
    proxy_store.set_account_proxy("acc1", pid)
    session = proxy_store.get_session("acc1")
    assert session is None                            # 还没取过代理，不建会话
    url = proxy_store.proxy_url_for("acc1")
    session = proxy_store.get_session("acc1")["session"]
    assert url == f"http://u-region-JP-sid-{session}-t-120:p@gate.example.com:8000"


def test_generic_pool_keeps_released_nodes(monkeypatch):
    """通用节点池：换 IP 后旧节点留在池里给别的号用（专用流程才会作废旧节点）。"""
    pid = proxy_store.create_proxy(
        "generic", "http", "", 0, mode="extract", extract_url="https://api.example.com/get",
        extract_interval=0)["id"]
    _stub_api(monkeypatch, "1.1.1.1:8080:u1:p\r\n1.1.1.2:8080:u2:p\r\n")
    proxy_store.set_account_proxy("acc1", pid)
    proxy_store.proxy_url_for("acc1")
    assert proxy_store.rotate_account_session("acc1", force=True)
    assert len(proxy_store.list_nodes(pid)) == 2


def test_import_order_does_not_matter():
    """proxy_store 顶层 import proxy_1024，后者又 import proxy_store：谁先被导入都要能用。"""
    code = ("import dola_pool.proxy_1024, dola_pool.proxy_store;"
            " assert dola_pool.proxy_store.proxy_1024.PROVIDER == '1024proxy'; print('ok')")
    env = {**os.environ, "PYTHONPATH": str(REPO_ROOT)}
    result = subprocess.run([sys.executable, "-c", code], cwd=REPO_ROOT, env=env,
                            capture_output=True, text=True, timeout=120)
    assert result.returncode == 0 and result.stdout.strip() == "ok", result.stderr


# ===== 升级：旧预设建的记录自动改走专用流程 =====

LEGACY_SCHEMA = """
CREATE TABLE proxies (
    id TEXT PRIMARY KEY, name TEXT NOT NULL UNIQUE,
    protocol TEXT NOT NULL DEFAULT 'http', host TEXT NOT NULL, port INTEGER NOT NULL,
    username TEXT DEFAULT '', password TEXT DEFAULT '', remark TEXT DEFAULT '',
    enabled INTEGER NOT NULL DEFAULT 1, created_at REAL,
    mode TEXT NOT NULL DEFAULT 'static', session_template TEXT DEFAULT '',
    sticky_ttl INTEGER NOT NULL DEFAULT 0, rotate_on_risk INTEGER NOT NULL DEFAULT 1,
    rotate_min_interval INTEGER NOT NULL DEFAULT 0, extract_url TEXT DEFAULT '',
    extract_interval INTEGER NOT NULL DEFAULT 0, extract_last_at REAL NOT NULL DEFAULT 0,
    extract_last_count INTEGER NOT NULL DEFAULT 0, extract_last_error TEXT DEFAULT ''
);
INSERT INTO proxies VALUES
 ('px_tunnel','tunnel','http','us.1024proxy.io',3000,'acct','pw','',1,0,'dynamic',
  '{user}-region-JP-sid-{session}-t-120',7200,1,60,'',0,0,0,''),
 ('px_api','api','http','',0,'','','',1,0,'extract','',0,1,60,'%s',60,0,0,''),
 ('px_static','static','http','1.2.3.4',8080,'u','p','',1,0,'static','',0,1,60,'',0,0,0,''),
 ('px_other','other','http','gate.example.com',8000,'u','p','',1,0,'dynamic',
  '{user}-session-{session}',0,1,60,'',0,0,0,'');
"""


def _legacy_db(tmp_path, monkeypatch):
    db = tmp_path / "legacy.db"
    conn = sqlite3.connect(str(db))
    conn.executescript(LEGACY_SCHEMA % (LINK,))
    conn.close()
    monkeypatch.setattr(proxy_store, "DB_PATH", db)
    proxy_store._ready_paths.clear()
    return db


def test_legacy_preset_records_are_adopted(tmp_path, monkeypatch):
    _legacy_db(tmp_path, monkeypatch)
    rows = {row["name"]: row for row in proxy_store.list_proxies()}

    tunnel, api = rows["tunnel"], rows["api"]
    assert tunnel["provider"] == "1024proxy"
    assert tunnel["provider_settings"] == {"region": "JP", "ttl": 120}
    assert tunnel["sticky_ttl"] == 7200                       # 原有的会话有效期不动
    assert api["provider"] == "1024proxy"
    assert api["provider_settings"] == {"region": "JP", "num": 1, "time": 10}
    assert api["extract_interval"] == proxy_1024.API_SPACING  # 原来的 60 秒限流换成专用间隔
    assert rows["static"]["provider"] == "" and rows["other"]["provider"] == ""

    # 用户名与原模板拼出来的完全一致，升级不影响正在用的号
    proxy_store.set_account_proxy("acc1", "px_tunnel")
    url = proxy_store.proxy_url_for("acc1")
    session = proxy_store.get_session("acc1")["session"]
    assert url == (
        f"http://acct-region-JP-sid-{session}-t-120:pw@us.1024proxy.io:3000")
    # 旧链接里控制台默认的 Rand（随机地区）改按日本提取
    assert proxy_1024.api_url(proxy_store.get_proxy("px_api")).count("region=JP") == 1
    assert "Rand" not in proxy_1024.api_url(proxy_store.get_proxy("px_api"))


def test_legacy_tagging_runs_once_per_database(tmp_path, monkeypatch):
    db = _legacy_db(tmp_path, monkeypatch)
    proxy_store.list_proxies()
    # 升级之后才建的通用记录不会再被认领
    conn = sqlite3.connect(str(db))
    conn.execute(
        "UPDATE proxies SET provider='', settings='' WHERE id='px_other'")
    conn.execute("UPDATE proxies SET session_template='{user}-region-JP-sid-{session}-t-120' "
                 "WHERE id='px_other'")
    conn.commit()
    conn.close()
    assert proxy_store.get_proxy("px_other")["provider"] == ""


# ===== 面板接口 =====

@pytest.fixture
def server(monkeypatch):
    from dola_pool import server as server_mod
    monkeypatch.setattr(server_mod, "_admin_auth", lambda key: None)
    return server_mod


def test_admin_endpoints_create_and_update_1024_records(server):
    from fastapi import HTTPException

    created = _run(server.admin_proxy_create_1024(server.Proxy1024In(
        kind="tunnel", name="t", gateway="us.1024proxy.io:3000", username="acct", password="pw"),
        x_admin_key="k"))["created"]
    assert created["provider"] == "1024proxy" and created["mode"] == "dynamic"
    assert "password" not in created and created["has_password"] is True

    with pytest.raises(HTTPException) as exc:
        _run(server.admin_proxy_create_1024(server.Proxy1024In(
            kind="api", name="a", link="https://api.other-vendor.com/get"), x_admin_key="k"))
    assert exc.value.status_code == 400 and "1024" in exc.value.detail

    updated = _run(server.admin_proxy_update_1024(
        created["id"], server.Proxy1024In(region="KR"), x_admin_key="k"))["updated"]
    assert updated["provider_settings"] == {"region": "KR", "ttl": 120}

    with pytest.raises(HTTPException) as exc:
        _run(server.admin_proxy_update_1024("px_nope", server.Proxy1024In(region="KR"),
                                            x_admin_key="k"))
    assert exc.value.status_code == 400


class _IpResp:
    def __init__(self, payload):
        self._payload = payload
        self.text = payload if isinstance(payload, str) else json.dumps(payload)

    def raise_for_status(self):
        pass

    def json(self):
        if isinstance(self._payload, str):
            raise ValueError("not json")
        return self._payload


def _reset_ip_cache():
    proxy_1024._public_ip_cache.update(ip="", at=0.0)


def test_server_public_ip_uses_direct_lookup_and_caches(monkeypatch):
    """白名单要的是服务器直连出口，查到就缓存；重新检测才再出网。"""
    import requests

    _reset_ip_cache()
    calls = []

    def fake_get(url, timeout=None, **kw):
        calls.append(url)
        assert timeout
        return _IpResp({"status": "success", "query": "203.0.113.8"})

    monkeypatch.setattr(requests, "get", fake_get)
    assert proxy_1024.server_public_ip() == {"ok": True, "ip": "203.0.113.8"}
    assert proxy_1024.server_public_ip()["ip"] == "203.0.113.8"
    assert calls == [proxy_1024._PUBLIC_IP_SOURCES[0]]
    assert proxy_1024.server_public_ip(refresh=True)["ip"] == "203.0.113.8"
    assert len(calls) == 2


def test_server_public_ip_falls_back_when_the_first_lookup_fails(monkeypatch):
    import requests

    _reset_ip_cache()
    calls = []

    def fake_get(url, timeout=None, **kw):
        calls.append(url)
        if "ip-api" in url:
            raise OSError("timed out")
        return _IpResp("203.0.113.9\n")

    monkeypatch.setattr(requests, "get", fake_get)
    assert proxy_1024.server_public_ip()["ip"] == "203.0.113.9"
    assert [url for url in calls] == list(proxy_1024._PUBLIC_IP_SOURCES)


def test_server_public_ip_reports_failure_without_raising(monkeypatch):
    import requests

    _reset_ip_cache()

    def fake_get(url, timeout=None, **kw):
        raise OSError("network down")

    monkeypatch.setattr(requests, "get", fake_get)
    failed = proxy_1024.server_public_ip()
    assert failed["ok"] is False and failed["ip"] == "" and failed["error"]
    assert proxy_1024.server_public_ip()["ok"] is False          # 失败不缓存


def test_server_public_ip_rejects_a_non_ip_body(monkeypatch):
    import requests

    _reset_ip_cache()
    monkeypatch.setattr(requests, "get",
                        lambda url, timeout=None, **kw: _IpResp("please login"))
    assert proxy_1024.server_public_ip() == {
        "ok": False, "ip": "", "error": "查询服务没有返回 IP"}


def test_admin_public_ip_endpoint(server, monkeypatch):
    seen = {}
    monkeypatch.setattr(proxy_1024, "server_public_ip",
                        lambda refresh=False: seen.update(refresh=refresh) or
                        {"ok": True, "ip": "203.0.113.8"})
    assert _run(server.admin_proxy_1024_public_ip(refresh=True, x_admin_key="k")) == {
        "ok": True, "ip": "203.0.113.8"}
    assert seen["refresh"] is True


def test_admin_generic_endpoints_stay_vendor_neutral(server):
    """通用添加代理接口不认识 provider：专用记录只能从专用接口建。"""
    created = _run(server.admin_proxy_create(server.ProxyCreate(
        name="g", host="1.2.3.4", port=8080), x_admin_key="k"))["created"]
    assert created["provider"] == "" and created["provider_settings"] == {}
