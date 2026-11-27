"""本地登录采集工具：1024Proxy 代理拼接、Cookie 导出格式、服务器推送与登录采集。不访问真实浏览器与网络。"""
import json
import re
import stat
from pathlib import Path
from types import SimpleNamespace

import pytest

from test_task_deadline import _run

import tools.login as login_tool


SESSION = {"name": "sessionid", "value": "s3cret", "domain": ".dola.com", "path": "/",
           "expires": 1790000000, "secure": True, "httpOnly": True, "sameSite": "Lax",
           "size": 120}


def test_build_1024_proxy_uses_official_username_format():
    url = login_tool.build_1024_proxy("acc1", "p@ss:word", "gw.example.com:10000",
                                      region="KR", session="ab12cd34", ttl=60)
    assert url == "http://acc1-region-KR-sid-ab12cd34-t-60:p%40ss%3Aword@gw.example.com:10000"
    # 默认：出口日本、粘性 120 分钟、随机会话 ID（与面板 1024Proxy 预设一致）
    assert re.fullmatch(r"http://acc1-region-JP-sid-[0-9a-f]{8}-t-120:pw@gw:1",
                        login_tool.build_1024_proxy("acc1", "pw", "gw:1"))


@pytest.mark.parametrize("options", [
    {"gateway": ""}, {"region": "1"}, {"ttl": 0}, {"ttl": 121}, {"ttl": "30"},
    {"session": "a b"},
])
def test_build_1024_proxy_rejects_bad_options(options):
    kwargs = {"region": "JP", "ttl": 120}
    kwargs.update(options)
    with pytest.raises(login_tool.ToolError):
        login_tool.build_1024_proxy("acc1", "pw", kwargs.pop("gateway", "gw:1"), **kwargs)
    with pytest.raises(login_tool.ToolError, match="账号、密码和网关"):
        login_tool.build_1024_proxy("acc1", "", "gw:1")


def test_cookie_bundle_keeps_dola_cookies_with_whitelisted_fields():
    foreign = {"name": "x", "value": "y", "domain": "example.com", "path": "/"}
    bundle = login_tool.cookie_bundle("acc1", [SESSION, foreign])
    assert bundle["format"] == "dola-pool-cookies-v1" and bundle["account"] == "acc1"
    assert [c["name"] for c in bundle["cookies"]] == ["sessionid"]
    assert bundle["cookies"][0]["value"] == "s3cret" and "size" not in bundle["cookies"][0]
    with pytest.raises(login_tool.ToolError, match="sessionid"):
        login_tool.cookie_bundle("acc1", [dict(SESSION, name="ttwid")])


def test_export_bundle_writes_private_file_and_leaves_no_temp(tmp_path):
    bundle = login_tool.cookie_bundle("acc1", [SESSION])
    target = tmp_path / "acc1" / login_tool.EXPORT_NAME          # 目录不存在时一并创建
    assert login_tool.export_bundle(bundle, target) == target
    assert json.loads(target.read_text(encoding="utf-8")) == bundle
    assert stat.S_IMODE(target.stat().st_mode) == 0o600
    assert [p.name for p in target.parent.iterdir()] == [login_tool.EXPORT_NAME]


def test_clipboard_uses_platform_tool_and_reports_when_missing(monkeypatch):
    seen = []
    monkeypatch.setattr(login_tool.shutil, "which", lambda name: "/usr/bin/pbcopy")
    monkeypatch.setattr(login_tool.subprocess, "run",
                        lambda cmd, **kwargs: seen.append((cmd, kwargs["input"])))
    assert login_tool.copy_to_clipboard("{}", platform="darwin") is True
    assert seen == [(["pbcopy"], b"{}")]
    monkeypatch.setattr(login_tool.shutil, "which", lambda name: None)
    assert login_tool.copy_to_clipboard("{}", platform="darwin") is False


def fake_server(monkeypatch, *, job_status="success", login_ok=True, status_code=200, detail=""):
    """假的 dola-pool 管理接口：记录请求，第一次查任务还在导入、第二次结束。"""
    calls, state = [], {"polls": 0}

    def request(method, url, **kwargs):
        calls.append((method, url, kwargs))
        if status_code >= 400:
            return SimpleNamespace(status_code=status_code, reason="Bad Request",
                                   content=b"{}", json=lambda: {"detail": detail})
        if url.endswith("/api/admin/login"):
            data = {"ok": True, "token": "tok-1"}
        elif method == "POST":
            data = {"ok": True, "job": "running"}
        elif url.endswith("/api/admin/jobs"):
            state["polls"] += 1
            running = state["polls"] == 1
            data = {"jobs": {"acc1": {"status": "running" if running else job_status,
                                      "error": "账号已存在: acc1"}}}
        else:
            data = {"accounts": [{"name": "other", "login_ok": 1},
                                 {"name": "acc1", "login_ok": 1 if login_ok else 0}]}
        return SimpleNamespace(status_code=200, reason="OK", content=b"{}", json=lambda: dict(data))

    monkeypatch.setattr(login_tool.requests, "request", request)
    monkeypatch.setattr(login_tool.time, "sleep", lambda seconds: None)
    return calls


def test_push_logs_in_posts_cookies_and_reports_verification(monkeypatch):
    calls = fake_server(monkeypatch)
    base = "https://srv.example.com"
    assert login_tool.server_login(base, "admin", "pw") == "tok-1"
    bundle = login_tool.cookie_bundle("acc1", [SESSION])
    assert "验证通过" in login_tool.push_to_server(base, "tok-1", bundle, proxy_id="p1")
    method, url, kwargs = calls[1]
    assert (method, url) == ("POST", base + "/api/admin/accounts")
    body = kwargs["json"]
    assert body["name"] == "acc1" and body["proxy_id"] == "p1" and body["cookie_skip_verify"] is False
    assert json.loads(body["cookies"]) == bundle["cookies"]
    assert calls[0][2]["json"] == {"username": "admin", "password": "pw"}
    assert all(c[2]["headers"] == {"X-Admin-Key": "tok-1"} for c in calls[1:])


def test_push_reports_unverified_account_without_failing(monkeypatch):
    fake_server(monkeypatch, login_ok=False)
    result = login_tool.push_to_server("https://srv", "tok", login_tool.cookie_bundle("acc1", [SESSION]))
    assert "未通过" in result


def test_push_skip_verify_skips_account_lookup(monkeypatch):
    calls = fake_server(monkeypatch)
    result = login_tool.push_to_server("https://srv", "tok", login_tool.cookie_bundle("acc1", [SESSION]),
                                       skip_verify=True)
    assert "跳过" in result
    assert not [c for c in calls if c[0] == "GET" and c[1].endswith("/api/admin/accounts")]


def test_push_surfaces_server_error_without_leaking_secrets(monkeypatch):
    bundle = login_tool.cookie_bundle("acc1", [SESSION])
    fake_server(monkeypatch, job_status="failed")
    with pytest.raises(login_tool.ToolError, match="账号已存在"):
        login_tool.push_to_server("https://srv", "tok", bundle)
    fake_server(monkeypatch, status_code=401, detail="用户名或密码错误")
    with pytest.raises(login_tool.ToolError, match="401.*用户名或密码错误") as caught:
        login_tool.server_login("https://srv", "admin", "hunter2")
    assert "hunter2" not in str(caught.value)


def test_push_timeout_is_reported(monkeypatch):
    fake_server(monkeypatch, job_status="running")
    monkeypatch.setattr(login_tool, "PUSH_TIMEOUT", 0)
    with pytest.raises(login_tool.ToolError, match="导入超时"):
        login_tool.push_to_server("https://srv", "tok", login_tool.cookie_bundle("acc1", [SESSION]))


def test_push_reports_unreachable_server(monkeypatch):
    def request(method, url, **kwargs):
        raise login_tool.requests.ConnectionError("boom")
    monkeypatch.setattr(login_tool.requests, "request", request)
    with pytest.raises(login_tool.ToolError, match="连不上服务器"):
        login_tool.server_login("https://srv", "admin", "pw")


@pytest.mark.parametrize("raw,base", [
    ("srv.example.com", "https://srv.example.com"),
    ("https://srv.example.com/", "https://srv.example.com"),
    ("http://127.0.0.1:8000", "http://127.0.0.1:8000"),
])
def test_server_address_defaults_to_https(raw, base):
    assert login_tool.normalize_server(raw) == base
    with pytest.raises(login_tool.ToolError, match="服务器地址"):
        login_tool.normalize_server("ftp://srv.example.com")


def test_plain_http_server_outside_localhost_gets_a_warning(monkeypatch, capsys):
    assert not login_tool.normalize_server("http://127.0.0.1:8000").startswith("https")
    assert "明文" not in capsys.readouterr().out
    login_tool.normalize_server("http://dola.example.com")
    assert "明文" in capsys.readouterr().out


def test_exported_bundle_is_accepted_by_both_server_import_parsers():
    """对接缝：导出包要能原样走服务器两条导入路——单个添加（normalize_cookies）与批量粘贴。"""
    from dola_pool import cookie_import, cookie_login

    other = {"name": "ttwid", "value": "1", "domain": ".dola.com", "path": "/"}
    raw = json.dumps(login_tool.cookie_bundle("acc1", [SESSION, other]), ensure_ascii=False)
    data = json.loads(raw)
    assert [c["name"] for c in cookie_login.normalize_cookies(data)] == ["sessionid", "ttwid"]
    assert cookie_login.normalize_storage_bundle(data) == {}
    state, = cookie_import.parse_cookie_header_text(raw)
    assert cookie_import.sessionid_from_state(state) == "s3cret"
    assert len(cookie_import.parse_cookie_header_text(raw + "\n" + raw)) == 2    # 连续粘贴 = 多个号


def fake_browser(monkeypatch, on_wait):
    """假的 patchright：launch 记录参数，on_wait 在轮询等待时被调用（模拟人工操作页面）。"""
    launched, closed = [], []
    state = {"logged_in": False}

    class Page:
        async def goto(self, *args, **kwargs):
            pass

        async def wait_for_timeout(self, ms):
            on_wait(state)

    page = Page()

    class Context:
        pages = [page]

        async def cookies(self, url=None):
            return [dict(SESSION)] if state["logged_in"] else []

        async def close(self):
            closed.append(True)

    async def launch(profile, **kwargs):
        launched.append((profile, kwargs))
        return Context()

    class Playwright:
        async def __aenter__(self):
            return SimpleNamespace(chromium=SimpleNamespace(launch_persistent_context=launch))

        async def __aexit__(self, *args):
            pass

    monkeypatch.setattr(login_tool, "async_playwright", Playwright)
    return launched, closed


def test_capture_login_waits_for_manual_login_then_returns_cookies(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    launched, closed = fake_browser(monkeypatch, lambda state: state.update(logged_in=True))
    proxy = {"server": "http://127.0.0.1:12345", "username": "u", "password": "p"}
    assert _run(login_tool.capture_login("acc1", proxy)) == [SESSION]
    assert closed == [True]
    profile, kwargs = launched[0]
    assert profile == str(Path("accounts") / "acc1")      # 沿用原有行为：相对仓库根目录
    assert kwargs["headless"] is False and kwargs["locale"] == "ja-JP" and kwargs["proxy"] == proxy


def test_capture_login_times_out_when_nobody_logs_in(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _, closed = fake_browser(monkeypatch, lambda state: None)
    monkeypatch.setattr(login_tool, "LOGIN_SECONDS", 0)
    with pytest.raises(login_tool.ToolError, match="未检测到登录"):
        _run(login_tool.capture_login("acc1", None))
    assert closed == [True]


def stub_run(monkeypatch, seen):
    """main() 集成测试的公共桩：登录采集、剪贴板、出口探测都不碰外部。"""
    async def capture(account, proxy_kwargs):
        seen["proxy"] = proxy_kwargs
        return [dict(SESSION)]
    monkeypatch.setattr(login_tool, "capture_login", capture)
    monkeypatch.setattr(login_tool, "copy_to_clipboard", lambda text, platform=None: seen.setdefault("copied", text) is not None)
    monkeypatch.setattr(login_tool.proxy_store, "probe_exit_ip", lambda url, timeout=15: {
        "ok": True, "ip": "1.2.3.4", "country": "Japan", "country_code": "JP", "region": "Tokyo", "isp": "isp"})


def test_main_exports_copies_and_starts_fresh(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "accounts" / "acc9").mkdir(parents=True)
    (tmp_path / "accounts" / "acc9" / "old-profile").write_text("x")
    seen = {}
    stub_run(monkeypatch, seen)
    assert login_tool.main(["acc9", "--proxy", "http://u:p@1.2.3.4:8080", "--fresh"]) == 0
    exported = tmp_path / "accounts" / "acc9" / login_tool.EXPORT_NAME
    bundle = json.loads(exported.read_text(encoding="utf-8"))
    assert bundle["account"] == "acc9" and [c["name"] for c in bundle["cookies"]] == ["sessionid"]
    assert json.loads(seen["copied"]) == bundle
    assert seen["proxy"] == {"server": "http://1.2.3.4:8080", "username": "u", "password": "p"}
    assert not (tmp_path / "accounts" / "acc9" / "old-profile").exists()
    out = capsys.readouterr().out
    assert "s3cret" not in out and "1.2.3.4:8080" in out          # 不打印 Cookie 值；出口 IP 正常显示


def test_main_builds_1024proxy_tunnel(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    seen = {}
    stub_run(monkeypatch, seen)
    assert login_tool.main(["acc1", "--p1024-user", "acc1", "--p1024-pass", "pw",
                            "--p1024-gateway", "gw:10000", "--p1024-session", "ab12cd34",
                            "--no-clipboard"]) == 0
    assert seen["proxy"] == {"server": "http://gw:10000",
                             "username": "acc1-region-JP-sid-ab12cd34-t-120", "password": "pw"}


def test_main_warns_when_exit_is_outside_supported_regions(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    stub_run(monkeypatch, {})
    monkeypatch.setattr(login_tool.proxy_store, "probe_exit_ip", lambda url, timeout=15: {   # 覆盖公共桩的日本出口
        "ok": True, "ip": "5.6.7.8", "country": "United States", "country_code": "US", "region": "", "isp": ""})
    assert login_tool.main(["acc1", "--proxy", "http://1.2.3.4:8080", "--no-clipboard"]) == 0
    assert "不是日本/韩国" in capsys.readouterr().out


def test_main_pushes_to_server_with_login_token(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    calls = fake_server(monkeypatch)
    stub_run(monkeypatch, {})
    assert login_tool.main(["acc1", "--proxy", "http://u:p@1.2.3.4:8080", "--no-clipboard",
                            "--server", "srv.example.com", "--server-user", "admin",
                            "--server-pass", "hunter2-admin-pw", "--proxy-id", "p1"]) == 0
    assert calls[0][:2] == ("POST", "https://srv.example.com/api/admin/login")
    out = capsys.readouterr().out
    assert "验证通过" in out                       # 管理员密码与会话令牌不进输出
    assert "hunter2-admin-pw" not in out and "tok-1" not in out


def test_main_rejects_unsafe_account_name(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    assert login_tool.main(["bad/name", "--proxy", "http://1.2.3.4:8080"]) == 1
    assert "账号名" in capsys.readouterr().out
    assert not (tmp_path / "accounts").exists()
