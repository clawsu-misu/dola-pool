"""本地登录采集工具：1024Proxy 代理拼接、Cookie 导出格式、服务器推送、登录采集与网页界面。不访问真实浏览器与网络。"""
import asyncio
import json
import re
import stat
import threading
import time
import urllib.error
import urllib.request
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
    # 默认：出口日本、粘性 120 分钟、随机会话 ID（与面板「1024Proxy」专用页签一致）
    assert re.fullmatch(r"http://acc1-region-JP-sid-[0-9a-f]{8}-t-120:pw@gw:1",
                        login_tool.build_1024_proxy("acc1", "pw", "gw:1"))


def test_build_1024_proxy_does_not_wrap_a_username_copied_from_the_console():
    """控制台「去使用」给的是拼好的用户名。再套一层网关会直接断开。"""
    full = "acct9-region-JP-sid-abCD1234-t-10"
    url = login_tool.build_1024_proxy(full, "pw", "us.example.com:3000", region="KR", session="ffff", ttl=120)
    assert url == "http://" + full + ":pw@us.example.com:3000"


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
    assert "localStorage" not in bundle          # 没抓到存储时保持旧格式
    with pytest.raises(login_tool.ToolError, match="sessionid"):
        login_tool.cookie_bundle("acc1", [dict(SESSION, name="ttwid")])


def test_cookie_bundle_keeps_page_storage_as_strings():
    storage = {"localStorage": {"__tea_cache_tokens_495671": '{"web_id":"123"}', "n": None},
               "sessionStorage": {}, "indexedDB": {"should": "drop"}}
    bundle = login_tool.cookie_bundle("acc1", [SESSION], storage)
    assert bundle["localStorage"] == {"__tea_cache_tokens_495671": '{"web_id":"123"}', "n": ""}
    assert "sessionStorage" not in bundle and "indexedDB" not in bundle


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
    bundle = login_tool.cookie_bundle("acc1", [SESSION],
                                      {"localStorage": {"__tea_cache_tokens_495671": '{"web_id":"123"}'}})
    assert "验证通过" in login_tool.push_to_server(base, "tok-1", bundle, proxy_id="p1")
    method, url, kwargs = calls[1]
    assert (method, url) == ("POST", base + "/api/admin/accounts")
    body = kwargs["json"]
    assert body["name"] == "acc1" and body["proxy_id"] == "p1" and body["cookie_skip_verify"] is False
    # 推整个包，不只是 cookies 数组：服务器靠 localStorage 还原设备标识
    sent = json.loads(body["cookies"])
    assert sent["cookies"] == bundle["cookies"]
    assert sent["localStorage"]["__tea_cache_tokens_495671"] == '{"web_id":"123"}'
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
    raw = json.dumps(login_tool.cookie_bundle(
        "acc1", [SESSION, other], {"localStorage": {"k": "v"}}), ensure_ascii=False)
    data = json.loads(raw)
    assert [c["name"] for c in cookie_login.normalize_cookies(data)] == ["sessionid", "ttwid"]
    assert cookie_login.normalize_storage_bundle(data) == {"localStorage": {"k": "v"}}
    state, = cookie_import.parse_cookie_header_text(raw)
    assert cookie_import.sessionid_from_state(state) == "s3cret"
    assert len(cookie_import.parse_cookie_header_text(raw + "\n" + raw)) == 2    # 连续粘贴 = 多个号


def fake_browser(monkeypatch, on_wait):
    """假的 patchright：launch 记录参数，on_wait 在轮询等待时被调用（模拟人工操作页面）。"""
    launched, closed = [], []
    state = {"logged_in": False}
    closed_by_user = type("TargetClosedError", (Exception,), {})   # 窗口被人关掉时 Playwright 抛的异常类型名

    class Page:
        async def goto(self, *args, **kwargs):
            pass

        async def wait_for_timeout(self, ms):
            on_wait(state)

        async def evaluate(self, script):
            if state.get("storage_fails"):
                raise RuntimeError("页面已跳走")
            return state.get("storage") or {}

    page = Page()

    class Context:
        pages = [page]

        async def cookies(self, url=None):
            if state.get("closed"):
                raise closed_by_user("Target page, context or browser has been closed")
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
    cookies, storage = _run(login_tool.capture_login("acc1", proxy))
    assert cookies == [SESSION] and storage == {}
    assert closed == [True]
    profile, kwargs = launched[0]
    assert profile == str(Path("accounts") / "acc1")      # 沿用原有行为：相对仓库根目录
    assert kwargs["headless"] is False and kwargs["locale"] == "ja-JP" and kwargs["proxy"] == proxy


def test_capture_login_returns_page_storage_and_survives_a_read_failure(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    storage = {"localStorage": {"__tea_cache_tokens_495671": '{"web_id":"9"}'}, "sessionStorage": {}}

    def login_with_storage(state):
        state.update(logged_in=True, storage=storage)

    fake_browser(monkeypatch, login_with_storage)
    cookies, got = _run(login_tool.capture_login("acc1", None))
    assert cookies == [SESSION] and got == storage

    def login_but_storage_fails(state):
        state.update(logged_in=True, storage_fails=True)

    fake_browser(monkeypatch, login_but_storage_fails)
    cookies, got = _run(login_tool.capture_login("acc2", None, log=lambda *a: None))
    assert cookies == [SESSION] and got == {}      # 存储读失败只丢掉存储，登录不白做


def test_capture_login_times_out_when_nobody_logs_in(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _, closed = fake_browser(monkeypatch, lambda state: None)
    monkeypatch.setattr(login_tool, "LOGIN_SECONDS", 0)
    with pytest.raises(login_tool.ToolError, match="未检测到登录"):
        _run(login_tool.capture_login("acc1", None))
    assert closed == [True]


def test_capture_login_stops_when_the_browser_binary_is_missing(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)

    class Playwright:
        async def __aenter__(self):
            return SimpleNamespace(chromium=SimpleNamespace(executable_path=str(tmp_path / "no-such-chrome")))

        async def __aexit__(self, *args):
            pass

    monkeypatch.setattr(login_tool, "async_playwright", Playwright)
    with pytest.raises(login_tool.ToolError, match="浏览器内核没装"):
        _run(login_tool.capture_login("acc1", None))


def test_capture_login_treats_a_closed_window_as_cancel(tmp_path, monkeypatch, capsys):
    """网页界面没有 Ctrl+C：关掉登录窗口就是取消，要说人话而不是甩一屏 Playwright 异常。"""
    monkeypatch.chdir(tmp_path)
    _, closed = fake_browser(monkeypatch, lambda state: state.update(closed=True))
    with pytest.raises(login_tool.ToolError, match="浏览器窗口已关闭") as caught:
        _run(login_tool.capture_login("acc1", None))
    assert "Target" not in str(caught.value) and closed == [True]


def stub_run(monkeypatch, seen):
    """main() / 网页界面集成测试的公共桩：登录采集、剪贴板、出口探测都不碰外部。"""
    async def capture(account, proxy_kwargs, log=print):
        seen["proxy"] = proxy_kwargs
        log(f"[{account}] 浏览器已打开")
        return [dict(SESSION)], {"localStorage": {"__tea_cache_tokens_495671": '{"web_id":"7"}'}}
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


def test_unreachable_proxy_stops_before_the_browser_opens(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(login_tool.proxy_store, "probe_exit_ip", lambda url, timeout=15: {
        "ok": False, "ip": "", "error": "Remote end closed connection"})

    async def capture(*args, **kwargs):
        pytest.fail("探测失败不应打开浏览器")

    monkeypatch.setattr(login_tool, "capture_login", capture)
    with pytest.raises(login_tool.ToolError, match="不会打开浏览器") as caught:
        _run(login_tool.login_and_export("acc1", {"server": "http://gw:1", "username": "u"}, "http://u:p@gw:1"))
    assert "Remote end closed connection" in str(caught.value)


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


# ---------- 网页界面 ----------

START = {"account": "acc1", "proxy_kind": "url", "proxy_url": "http://u:p@1.2.3.4:8080"}


def _http(method, origin, path, body=None, token="tok-9"):
    """直接调网页界面的 HTTP 接口，返回 (状态码, 响应体)；4xx 不抛异常。body 为 bytes 时原样发送。"""
    raw = body if isinstance(body, bytes) else (json.dumps(body).encode("utf-8") if body is not None else None)
    request = urllib.request.Request(origin + path, method=method, data=raw,
                                      headers={"X-Token": token} if token else {})
    try:
        with urllib.request.urlopen(request, timeout=5) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read()


@pytest.fixture
def web(tmp_path, monkeypatch):
    """在随机端口起网页界面；登录采集 / 剪贴板 / 出口探测都走桩，不开浏览器不联网。"""
    monkeypatch.chdir(tmp_path)
    stub_run(monkeypatch, {})
    server, url = login_tool.serve(login_tool.WebApp(), token="tok-9", port=0)
    threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True).start()
    origin = url.split("/?")[0]
    yield SimpleNamespace(server=server, url=url, tmp_path=tmp_path, origin=origin,
                          call=lambda method, path, body=None, token="tok-9": _http(method, origin, path, body, token))
    server.shutdown()
    server.server_close()


def wait_finished(web):
    for _ in range(500):
        state = json.loads(web.call("GET", "/api/status")[1])
        if state["status"] != "running":
            return state
        time.sleep(0.01)
    raise AssertionError("登录任务没有结束")


def test_web_page_and_api_only_answer_the_url_token(web):
    assert web.server.server_address[0] == "127.0.0.1"            # 只监听本机，局域网访问不到
    assert web.url.startswith("http://127.0.0.1:") and web.url.endswith("/?token=tok-9")
    assert web.call("GET", "/api/status", token="")[0] == 403     # 别的网页拿不到 token，调不动接口
    assert web.call("GET", "/", token="")[0] == 403
    assert web.call("POST", "/api/start", START, token="bad")[0] == 403
    assert json.loads(web.call("GET", "/api/status")[1])["status"] == "idle"    # 被拒的请求没启动任务
    code, page = web.call("GET", "/?token=tok-9", token="")
    assert code == 200 and b'id="loginForm"' in page and b'id="pushForm"' in page
    assert b'id="ipText"' in page and b'id="ipCopy"' in page


def test_web_login_job_exports_and_never_hands_cookies_to_the_page(web):
    assert web.call("POST", "/api/start", START)[0] == 200
    state = wait_finished(web)
    assert state["status"] == "done" and state["exported"] == 1 and state["clipboard"] is True
    assert state["account"] == "acc1"
    assert Path(state["export_path"]) == Path("accounts") / "acc1" / login_tool.EXPORT_NAME
    assert any("1.2.3.4:8080" in line for line in state["lines"])       # 进度日志进页面
    assert "s3cret" not in json.dumps(state)                            # Cookie 值只落文件，不进页面
    exported = json.loads(Path(state["export_path"]).read_text(encoding="utf-8"))
    assert exported["format"] == "dola-pool-cookies-v1" and exported["cookies"][0]["value"] == "s3cret"


def test_web_refuses_a_second_job_while_one_is_running(web, monkeypatch):
    gate, entered = threading.Event(), threading.Event()

    async def capture(account, proxy_kwargs, log=print):
        entered.set()
        while not gate.is_set():
            await asyncio.sleep(0.01)
        return [dict(SESSION)]

    monkeypatch.setattr(login_tool, "capture_login", capture)
    assert web.call("POST", "/api/start", START)[0] == 200
    assert entered.wait(5)
    code, body = web.call("POST", "/api/start", dict(START, account="acc2"))
    assert code == 409 and "已有登录" in body.decode()
    gate.set()                                                     # 结束上一个任务
    assert wait_finished(web)["account"] == "acc1"
    assert web.call("POST", "/api/start", dict(START, account="acc2"))[0] == 200
    assert wait_finished(web)["account"] == "acc2"                 # 结束后可以接着登下一个号


def test_web_pushes_the_exported_bundle(web, monkeypatch):
    push = {"server": "srv.example.com", "user": "admin", "pass": "hunter2-admin-pw", "proxy_id": "p1"}
    code, body = web.call("POST", "/api/push", push)
    assert code == 409 and "先完成一次登录" in body.decode()           # 还没导出就不能推
    calls = fake_server(monkeypatch)
    assert web.call("POST", "/api/start", START)[0] == 200
    wait_finished(web)
    code, data = web.call("POST", "/api/push", push)
    assert code == 200 and json.loads(data)["ok"] is True and "验证通过" in json.loads(data)["message"]
    assert calls[0][:2] == ("POST", "https://srv.example.com/api/admin/login")
    posted = next(c for c in calls if c[0] == "POST" and c[1].endswith("/api/admin/accounts"))
    assert posted[2]["json"]["name"] == "acc1" and posted[2]["json"]["proxy_id"] == "p1"
    sent = json.loads(posted[2]["json"]["cookies"])
    assert sent["cookies"][0]["value"] == "s3cret"
    assert sent["localStorage"]["__tea_cache_tokens_495671"]   # 页面存储跟着 Cookie 一起推过去
    page = web.call("GET", "/api/status")[1].decode()
    assert "hunter2-admin-pw" not in page and "tok-1" not in page and "s3cret" not in page
    assert any("验证通过" in line for line in json.loads(page)["lines"])


def test_web_push_survives_starting_the_next_login(web, monkeypatch):
    """推送要等服务器在线验证（几秒到几分钟），这期间点「开始登录」不能把正在推的导出包清掉。"""
    assert web.call("POST", "/api/start", START)[0] == 200
    wait_finished(web)
    calls = fake_server(monkeypatch)
    inner, gate, pushing = login_tool.requests.request, threading.Event(), threading.Event()

    def request(method, url, **kwargs):
        if url.endswith("/api/admin/login"):
            pushing.set()
            gate.wait(5)                          # 卡在服务器登录，模拟慢服务器
        return inner(method, url, **kwargs)

    monkeypatch.setattr(login_tool.requests, "request", request)
    results = []
    thread = threading.Thread(target=lambda: results.append(web.call(
        "POST", "/api/push", {"server": "srv.example.com", "user": "admin", "pass": "pw"})), daemon=True)
    thread.start()
    assert pushing.wait(5)
    assert web.call("POST", "/api/start", dict(START, account="acc2"))[0] == 200    # 推送中途登下一个号
    wait_finished(web)
    gate.set()
    thread.join(5)
    code, data = results[0]
    assert code == 200 and json.loads(data)["message"] == "acc1：已导入，服务器在线验证通过"
    posted = next(c for c in calls if c[0] == "POST" and c[1].endswith("/api/admin/accounts"))
    assert posted[2]["json"]["name"] == "acc1"
    assert json.loads(posted[2]["json"]["cookies"])["cookies"][0]["value"] == "s3cret"


def test_web_reports_failures_in_the_log_instead_of_hanging(web):
    assert web.call("POST", "/api/start", dict(START, proxy_url="http://"))[0] == 200   # 没有主机名
    state = wait_finished(web)
    assert state["status"] == "error" and state["exported"] == 0
    assert any("代理地址无法解析" in line for line in state["lines"])
    assert web.call("POST", "/api/push", {})[0] == 409
    assert web.call("POST", "/api/start", dict(START, account="bad/name"))[0] == 200
    assert any("账号名" in line for line in wait_finished(web)["lines"])


def test_web_uses_1024_credentials_verbatim(web, monkeypatch):
    """网页上的 1024 账密按控制台 curl 原样用，不再拼 -region-。"""
    monkeypatch.delenv("DOLA_P1024_PASS", raising=False)
    monkeypatch.setattr(login_tool.getpass, "getpass", lambda *a: pytest.fail("网页模式没有终端可交互"))
    user = "acct9-region-JP-sid-abCD1234-t-5"
    form = {"account": "acc1", "proxy_kind": "p1024", "p1024_user": user, "p1024_gateway": "us.example.com:3000"}
    assert web.call("POST", "/api/start", form)[0] == 200            # 没填密码
    assert any("网关、用户名和密码" in line for line in wait_finished(web)["lines"])
    assert web.call("POST", "/api/start", dict(form, p1024_pass="hunter2-1024-pw"))[0] == 200
    state = wait_finished(web)
    assert state["status"] == "done"
    assert any(f"账号 {user}）" in line for line in state["lines"])
    assert "region-JP-sid-abCD1234-t-5-region" not in json.dumps(state)
    assert "hunter2-1024-pw" not in json.dumps(state)


def _accept_loop(handler):
    """在 127.0.0.1 上接连接，每个连接丢给 handler。返回 (socket, port)。"""
    import socket
    import threading

    srv = socket.socket()
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(("127.0.0.1", 0))
    srv.listen(8)
    srv.settimeout(0.2)

    def run():
        while True:
            try:
                conn, _ = srv.accept()
            except socket.timeout:
                continue
            except OSError:
                return
            threading.Thread(target=handler, args=(conn,), daemon=True).start()

    threading.Thread(target=run, daemon=True).start()
    return srv, srv.getsockname()[1]


def _read_until(conn, marker, limit=8192):
    buf = b""
    while marker not in buf and len(buf) < limit:
        chunk = conn.recv(1024)
        if not chunk:
            break
        buf += chunk
    return buf


def _http_ok(body):
    return (b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nConnection: close\r\n"
            b"Content-Length: " + str(len(body)).encode() + b"\r\n\r\n" + body)


_EXIT_JSON = (b'{"status":"success","country":"Japan","countryCode":"JP",'
              b'"regionName":"Tokyo","isp":"test","query":"203.0.113.10"}')


def _serve_socks5(body):
    """无鉴权 SOCKS5：问候回 \\x05\\x00，CONNECT 成功后回固定 HTTP。"""
    import socket

    def handle(conn):
        try:
            conn.settimeout(5)
            greet = conn.recv(3)
            if greet != b"\x05\x01\x00":
                return
            conn.sendall(b"\x05\x00")
            head = conn.recv(4)
            if len(head) < 4:
                return          # 认协议的那次连接发完问候就关
            atyp = head[3]
            if atyp == 1:
                need = 6
            elif atyp == 3:
                n = conn.recv(1)
                need = (n[0] if n else 0) + 2
            elif atyp == 4:
                need = 18
            else:
                return
            got = b""
            while len(got) < need:
                chunk = conn.recv(need - len(got))
                if not chunk:
                    return
                got += chunk
            conn.sendall(b"\x05\x00\x00\x01\x00\x00\x00\x00\x00\x00")
            _read_until(conn, b"\r\n\r\n")
            conn.sendall(_http_ok(body))
        except OSError:
            return
        finally:
            conn.close()

    return _accept_loop(handle)


def _serve_http_proxy(body):
    """HTTP 代理。SOCKS 问候不认（回两个字节），HTTP 请求回固定内容。"""

    def handle(conn):
        try:
            conn.settimeout(5)
            first = conn.recv(1)
            if first == b"\x05":
                conn.sendall(b"NO")
                return
            _read_until(conn, b"\r\n\r\n")
            conn.sendall(_http_ok(body))
        except OSError:
            return
        finally:
            conn.close()

    return _accept_loop(handle)


def test_api_extract_speaks_the_protocol_the_node_actually_uses(monkeypatch):
    """不桩探测：本地 SOCKS5 / HTTP 代理各起一个，认协议并真的打通出口查询。"""
    socks, socks_port = _serve_socks5(_EXIT_JSON)
    http, http_port = _serve_http_proxy(_EXIT_JSON)
    api = "https://white.1024proxy.com/white/api?region=JP&num=1&type=txt"

    real_get = login_tool.requests.get

    def get(url, timeout=20, **kwargs):
        # 提取链接是桩；探测出口必须走真的 requests，经本地代理打出去
        if "proxies" in kwargs:
            return real_get(url, timeout=timeout, **kwargs)
        return SimpleNamespace(status_code=200, text=calls["text"], raise_for_status=lambda: None)

    calls = {"text": f"127.0.0.1:{socks_port}"}
    monkeypatch.setattr(login_tool.requests, "get", get)
    try:
        url = login_tool.fetch_1024_api(api)
        assert url == f"socks5://127.0.0.1:{socks_port}"
        assert login_tool._kwargs_from_url(url) == {"server": url}
        lines = []
        login_tool.describe_exit(url, log=lines.append)
        assert any("203.0.113.10" in line and "Japan" in line for line in lines)

        calls["text"] = f"127.0.0.1:{http_port}"
        url = login_tool.fetch_1024_api(api)
        assert url == f"http://127.0.0.1:{http_port}"
        lines = []
        login_tool.describe_exit(url, log=lines.append)
        assert any("203.0.113.10" in line for line in lines)
    finally:
        socks.close()
        http.close()


def test_public_ip_is_the_direct_address_for_the_whitelist(monkeypatch):
    """白名单要的是本机直连 IP，解析失败不能把一段说明文字当成地址。"""
    def get(url, timeout=8):
        assert url.startswith("http://ip-api.com/json/")
        return SimpleNamespace(status_code=200, raise_for_status=lambda: None,
                               json=lambda: {"status": "success", "query": "203.0.113.9"})

    monkeypatch.setattr(login_tool.requests, "get", get)
    assert login_tool.public_ip() == {"ok": True, "ip": "203.0.113.9"}
    monkeypatch.setattr(login_tool.requests, "get", lambda url, timeout=8: SimpleNamespace(
        status_code=200, raise_for_status=lambda: None, json=lambda: {"status": "success", "query": "not-an-ip"}))
    assert login_tool.public_ip()["ok"] is False and login_tool.public_ip()["ip"] == ""


def test_parse_sms_record_ignores_the_expiry_date_when_nothing_arrived():
    """收码站没来信时整段是 no|到期说明。日期里的数字不能被当成验证码。"""
    text = "no|该号将于2026-12-10 00:00:00到期，请立即换绑，如因逾期换绑的，后果自负！！！ 暂时未有邮件...试试重发吧."
    got = login_tool.parse_sms_record(text)
    assert got["ok"] is True and got["empty"] is True and got["code"] == ""
    assert "暂时未有邮件" in got["text"] and "2026" not in got["code"]


def test_parse_sms_record_reads_the_latest_code():
    first = login_tool.parse_sms_record("yes|【Dola】验证码 654321，5分钟内有效。该号将于2026-12-10 00:00:00到期")
    assert first["code"] == "654321" and first["empty"] is False
    bare = login_tool.parse_sms_record("yes|654321|您的验证码是654321")
    assert bare["code"] == "654321" and "您的验证码" in bare["text"]
    latest = login_tool.parse_sms_record("yes|验证码111111\nyes|验证码222222")
    assert latest["code"] == "222222"


def test_sms_url_must_be_an_external_http_address():
    assert login_tool.public_sms_url("https://sms.example/smsrecord?token=abc").startswith("https://")
    for bad in ("", "file:///etc/passwd", "http://127.0.0.1/sms", "http://10.1.2.3/sms",
                "http://192.168.1.9/sms", "http://user:pw@sms.example/x", "not a url"):
        with pytest.raises(login_tool.ToolError):
            login_tool.public_sms_url(bad)


def test_split_sms_source_takes_the_number_off_the_front():
    """输入框里是买号时给的整行：号码----收码链接。只贴链接也要能用。"""
    line = "+18647089355----https://sms.example/smsrecord?token=abc"
    assert login_tool.split_sms_source(line) == ("+18647089355", "https://sms.example/smsrecord?token=abc")
    assert login_tool.split_sms_source(" +18647089355 ---- https://sms.example/smsrecord?token=abc ") == (
        "+18647089355", "https://sms.example/smsrecord?token=abc")
    assert login_tool.split_sms_source("https://sms.example/smsrecord?token=abc") == (
        "", "https://sms.example/smsrecord?token=abc")
    assert login_tool.split_sms_source("") == ("", "")


def test_web_sms_endpoint_fetches_and_remembers_without_opening_a_browser(web, monkeypatch):
    """验证码由本机 HTTP 去读，页面只拿解析结果；整行写在当前目录，测试的临时目录不会碰到真链接。"""
    seen = {}

    def get(url, timeout=8, allow_redirects=True, headers=None):
        seen["url"] = url
        seen["redirects"] = allow_redirects
        return SimpleNamespace(status_code=200, text="yes|验证码 654321，该号将于2026-12-10到期")

    monkeypatch.setattr(login_tool.requests, "get", get)
    line = "+18647089355----https://sms.example/smsrecord?token=secret"
    assert json.loads(web.call("GET", "/api/sms-source")[1]) == {"ok": True, "source": ""}
    assert web.call("GET", "/api/sms-source", token="")[0] == 403
    code, body = web.call("POST", "/api/sms", {"source": line})
    assert code == 200
    assert json.loads(body)["code"] == "654321"
    link = "https://sms.example/smsrecord?token=secret"
    assert seen["url"] == link and seen["redirects"] is False          # 只请求链接，不带号码
    assert json.loads(web.call("GET", "/api/sms-source")[1])["source"] == line
    assert web.call("POST", "/api/sms", {"source": "https://sms.example/x"}, token="")[0] == 403
    code, body = web.call("POST", "/api/sms", {"source": "+18647089355----http://127.0.0.1/sms"})
    assert code == 400 and "外部" in body.decode()

    def explode(url, timeout=8, allow_redirects=True, headers=None):
        raise login_tool.requests.RequestException("timed out https://sms.example/smsrecord?token=secret")

    monkeypatch.setattr(login_tool.requests, "get", explode)
    code, body = web.call("POST", "/api/sms", {"source": line})
    assert code == 400 and b"secret" not in body and "收码链接" in body.decode()
    # 链接不合法或读失败，都不覆盖上一次读成功的那一行
    assert (web.tmp_path / "sms-record.txt").read_text(encoding="utf-8").strip() == line


def test_web_ip_endpoint_is_token_gated(web, monkeypatch):
    monkeypatch.setattr(login_tool, "public_ip", lambda timeout=8: {"ok": True, "ip": "203.0.113.9"})
    assert web.call("GET", "/api/ip", token="")[0] == 403
    code, body = web.call("GET", "/api/ip")
    assert code == 200 and json.loads(body) == {"ok": True, "ip": "203.0.113.9"}


def test_web_fetches_one_address_from_the_1024_api(web, monkeypatch):
    seen = {}
    stub_run(monkeypatch, seen)

    def get(url, timeout=20):
        assert url.startswith("https://white.1024proxy.com/white/api")
        return SimpleNamespace(status_code=200, text='[{"host":"165.1.2.3","port":"7098"}]',
                               raise_for_status=lambda: None)

    monkeypatch.setattr(login_tool.requests, "get", get)
    monkeypatch.setattr(login_tool, "_open_proxy_scheme", lambda host, port, timeout=3: "http")
    form = {"account": "acc1", "proxy_kind": "p1024api",
            "p1024_api": "https://white.1024proxy.com/white/api?region=JP&num=1&time=10&type=json"}
    assert web.call("POST", "/api/start", form)[0] == 200
    assert wait_finished(web)["status"] == "done"
    assert seen["proxy"] == {"server": "http://165.1.2.3:7098"}
    assert web.call("POST", "/api/start", dict(form, p1024_api="https://evil.example/api"))[0] == 200
    assert any("API 提取链接" in line for line in wait_finished(web)["lines"])
    monkeypatch.setattr(login_tool.requests, "get",
                        lambda url, timeout=20: SimpleNamespace(status_code=200, text="IP not in whitelist",
                                                                raise_for_status=lambda: None))
    assert web.call("POST", "/api/start", form)[0] == 200
    assert any("白名单" in line for line in wait_finished(web)["lines"])


def test_web_push_needs_the_admin_password_in_the_form(web, monkeypatch):
    monkeypatch.delenv("DOLA_ADMIN_PASSWORD", raising=False)
    monkeypatch.setattr(login_tool.getpass, "getpass", lambda *a: pytest.fail("网页模式没有终端可交互"))
    assert web.call("POST", "/api/start", START)[0] == 200
    wait_finished(web)
    code, body = web.call("POST", "/api/push", {"server": "srv.example.com", "user": "admin"})
    assert code == 400 and "管理员密码" in body.decode()


def test_web_reports_a_browser_crash_without_leaking_proxy_secrets(web, monkeypatch):
    """浏览器内核没装 / 启动失败：只打异常类型名（原文可能夹带代理密码）。"""
    async def crash(account, proxy_kwargs, log=print):
        raise RuntimeError(f"launch failed, password={proxy_kwargs['password']}")

    monkeypatch.setattr(login_tool, "capture_login", crash)
    payload = dict(START, proxy_url="http://u:hunter2-proxy-pw@1.2.3.4:8080")
    assert web.call("POST", "/api/start", payload)[0] == 200
    state = wait_finished(web)
    assert state["status"] == "error"
    blob = json.dumps(state)
    assert any("登录采集失败（RuntimeError）" in line for line in state["lines"])
    assert "hunter2-proxy-pw" not in blob and "password=[REDACTED]" in blob


def test_web_rejects_bad_usernames_and_requests(web):
    assert web.call("POST", "/api/start", START)[0] == 200
    wait_finished(web)
    code, body = web.call("POST", "/api/push", {"server": "srv.example.com"})          # 没填管理员用户名
    assert code == 400 and "管理员用户名" in body.decode()
    assert web.call("POST", "/api/nope", {})[0] == 404
    assert web.call("POST", "/api/start", b"{not json")[0] == 400
    assert web.call("POST", "/api/start", b"[1, 2]")[0] == 400


def test_run_web_reports_a_busy_port(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(login_tool.webbrowser, "open", lambda url: pytest.fail("没起来就不该开浏览器"))
    busy = login_tool.ThreadingHTTPServer(("127.0.0.1", 0), login_tool.BaseHTTPRequestHandler)
    try:
        assert login_tool.main(["--web", "--port", str(busy.server_address[1])]) == 1
    finally:
        busy.server_close()
    assert "被占用" in capsys.readouterr().out


def test_web_can_reuse_the_local_proxy_pool_binding(web, monkeypatch):
    seen = {}
    stub_run(monkeypatch, seen)
    monkeypatch.setattr(login_tool, "proxy_kwargs_for", lambda account: {"server": "http://127.0.0.1:12345"})
    monkeypatch.setattr(login_tool.proxy_store, "account_proxy_url", lambda account: "http://127.0.0.1:12345")
    (web.tmp_path / "accounts" / "acc1" / "old-profile").mkdir(parents=True)
    assert web.call("POST", "/api/start", {"account": "acc1", "proxy_kind": "local", "fresh": True})[0] == 200
    state = wait_finished(web)
    assert state["status"] == "done" and seen["proxy"] == {"server": "http://127.0.0.1:12345"}
    assert not (web.tmp_path / "accounts" / "acc1" / "old-profile").exists()


def test_run_web_opens_the_browser_and_stops_cleanly(monkeypatch, capsys):
    opened = []
    monkeypatch.setattr(login_tool.webbrowser, "open", lambda url: opened.append(url))

    def ctrl_c(self, *args, **kwargs):
        raise KeyboardInterrupt
    monkeypatch.setattr(login_tool.ThreadingHTTPServer, "serve_forever", ctrl_c)
    assert login_tool.main(["--web"]) == 0
    assert re.fullmatch(r"http://127\.0\.0\.1:\d+/\?token=[A-Za-z0-9_-]{16,}", opened[0])
    assert opened[0] in capsys.readouterr().out                 # 地址打在终端：没自动打开就手动粘
    assert login_tool.main([]) == 1                             # 既没 --web 也没给账号名
    assert "账号名" in capsys.readouterr().out


PAGE_HARNESS = r'''
const assert = require('assert');
const tick = () => new Promise(resolve => setImmediate(resolve));
const nodes = {};
const el = (selector, init = {}) => {
  if (!nodes[selector]) {
    nodes[selector] = Object.assign(
      {value: '', checked: false, hidden: false, disabled: false, textContent: '', className: '', scrollTop: 0, scrollHeight: 0},
      {handlers: {}, addEventListener(type, fn) { this.handlers[type] = fn; }}, init);
  }
  return nodes[selector];
};
const radios = ['p1024', 'p1024api', 'url', 'local'].map(kind => el('radio:' + kind, {value: kind}));
radios[0].checked = true;
Object.assign(el('#f_account'), {value: ' acc1 '});
Object.assign(el('#f_user'), {value: 'u1'});
Object.assign(el('#f_pass'), {value: 'pw-1024'});
Object.assign(el('#f_gateway'), {value: 'gw:10000'});
Object.assign(el('#f_api'), {value: ''});
Object.assign(el('#s_server'), {value: 'srv.example.com'});
Object.assign(el('#s_user'), {value: 'admin'});
Object.assign(el('#s_pass'), {value: 'pw-admin'});
Object.assign(el('#s_proxy_id'), {value: 'p1'});
globalThis.document = {
  querySelector: selector => selector === 'input[name=proxy]:checked' ? radios.find(r => r.checked) : el(selector),
  querySelectorAll: () => radios,
};
globalThis.location = {search: '?token=tok-9'};
globalThis.copiedIp = '';
navigator.clipboard = {writeText: async text => { globalThis.copiedIp = text; }};
const calls = [];
const queue = {
  '/api/status': [{status: 200, body: {status: 'idle', lines: []}}],
  '/api/sms-source': [{status: 200, body: {ok: true, source: ''}}],
  '/api/ip': [{status: 200, body: {ok: true, ip: '203.0.113.8'}}],
  '/api/start': [{status: 200, body: {ok: true}}],
  '/api/push': [{status: 200, body: {ok: true, message: '已导入，服务器在线验证通过'}}],
};
globalThis.fetch = async (path, init) => {
  const call = {path, method: init.method, headers: init.headers};
  if (init.body !== undefined) call.body = JSON.parse(init.body);
  calls.push(call);
  const reply = queue[path].shift();
  return {ok: reply.status === 200, status: reply.status, json: async () => reply.body};
};
const timers = [];
globalThis.setTimeout = fn => timers.push(fn);
globalThis.clearTimeout = () => {};
'''

PAGE_CHECKS = r'''
  await tick(); await tick();
  assert.deepEqual(calls.splice(0), [
    {path: '/api/status', method: 'GET', headers: {'X-Token': 'tok-9'}},
    {path: '/api/sms-source', method: 'GET', headers: {'X-Token': 'tok-9'}},
  ]);
  radios[1].checked = true; radios[0].checked = false; radios[1].handlers.change();
  await tick(); await tick();
  assert.deepEqual([el('#box_p1024').hidden, el('#box_p1024api').hidden, el('#box_url').hidden, el('#local_hint').hidden],
                   [true, false, true, true]);
  assert.deepEqual(calls.splice(0), [{path: '/api/ip', method: 'GET', headers: {'X-Token': 'tok-9'}}]);
  assert.equal(el('#ipText').textContent, '203.0.113.8');
  assert.equal(el('#ipCopy').disabled, false);
  await el('#ipCopy').handlers.click();
  await tick();
  assert.equal(globalThis.copiedIp, '203.0.113.8');
  assert.equal(el('#ipCopy').textContent, '已复制');
  radios[2].checked = true; radios[1].checked = false; radios[2].handlers.change();
  assert.deepEqual([el('#box_p1024').hidden, el('#box_p1024api').hidden, el('#box_url').hidden, el('#local_hint').hidden],
                   [true, true, false, true]);
  radios[3].checked = true; radios[2].checked = false; radios[3].handlers.change();
  assert.deepEqual([el('#box_p1024').hidden, el('#box_url').hidden, el('#local_hint').hidden], [true, true, false]);
  radios[0].checked = true; radios[3].checked = false; radios[0].handlers.change();
  assert.equal(el('#box_p1024').hidden, false);

  // 账号名没填：页面自己拦下，不发请求
  const account = el('#f_account').value;
  el('#f_account').value = '   ';
  await el('#loginForm').handlers.submit({preventDefault() {}});
  assert.equal(el('#loginBanner').textContent, '请先填账号名');
  assert.equal(calls.length, 0);
  el('#f_account').value = account;

  // 开始登录 → 轮询状态
  queue['/api/status'].push({status: 200, body: {status: 'running', account: 'acc1', lines: ['[acc1] 浏览器已打开']}});
  queue['/api/status'].push({status: 200, body: {status: 'done', account: 'acc1', exported: 3, clipboard: true,
    export_path: 'accounts/acc1/cookies-export.json', lines: ['[acc1] 浏览器已打开', 'x 已导出 3 条 Cookie']}});
  await el('#loginForm').handlers.submit({preventDefault() {}});
  await tick(); await tick();
  assert.deepEqual(calls.splice(0), [
    {path: '/api/start', method: 'POST', headers: {'X-Token': 'tok-9', 'Content-Type': 'application/json'}, body: {
      account: 'acc1', proxy_kind: 'p1024', fresh: false, proxy_url: '',
      p1024_user: 'u1', p1024_pass: 'pw-1024', p1024_gateway: 'gw:10000', p1024_api: ''}},
    {path: '/api/status', method: 'GET', headers: {'X-Token': 'tok-9'}},
  ]);
  assert.equal(el('#startBtn').disabled, true);
  assert.equal(el('#pushBox').hidden, true);
  assert.equal(el('#loginBanner').className, 'banner warn');
  assert.equal(el('#log').textContent, '[acc1] 浏览器已打开');
  assert.equal(timers.length, 1);
  await timers.pop()();
  await tick();
  assert.deepEqual(calls.splice(0), [{path: '/api/status', method: 'GET', headers: {'X-Token': 'tok-9'}}]);
  assert.equal(el('#startBtn').disabled, false);
  assert.equal(el('#pushBox').hidden, false);
  assert.equal(el('#loginBanner').className, 'banner ok');
  assert.equal(el('#loginBanner').textContent, '已导出 3 条 Cookie，并复制到剪贴板：accounts/acc1/cookies-export.json');
  assert.equal(timers.length, 0);                                // 结束后不再轮询

  // 推送
  await el('#pushForm').handlers.submit({preventDefault() {}});
  assert.deepEqual(calls.splice(0), [{path: '/api/push', method: 'POST', headers: {'X-Token': 'tok-9',
    'Content-Type': 'application/json'}, body: {
    server: 'srv.example.com', user: 'admin', pass: 'pw-admin', proxy_id: 'p1', skip_verify: false}}]);
  assert.equal(el('#pushBanner').className, 'banner ok');
  assert.equal(el('#pushBanner').textContent, '✓ 已导入，服务器在线验证通过');
  assert.equal(el('#s_pass').value, '');                         // 推完清掉密码框
  assert.equal(el('#pushBtn').disabled, false);

  // 服务器报错：原话显示在推送区
  el('#s_pass').value = 'wrong';
  queue['/api/push'].push({status: 400, body: {ok: false, error: '服务器返回 401：用户名或密码错误'}});
  await el('#pushForm').handlers.submit({preventDefault() {}});
  assert.equal(el('#pushBanner').className, 'banner bad');
  assert.equal(el('#pushBanner').textContent, '服务器返回 401：用户名或密码错误');

  // 开始登下一个号：上一个号的推送结果不残留
  queue['/api/start'].push({status: 200, body: {ok: true}});
  queue['/api/status'].push({status: 200, body: {status: 'running', account: 'acc2', lines: []}});
  await el('#loginForm').handlers.submit({preventDefault() {}});
  await tick(); await tick();
  assert.equal(el('#pushBox').hidden, true);
  assert.equal(el('#pushBanner').className, 'banner');
  assert.equal(el('#pushBanner').textContent, '');
  timers.length = 0;

  // 收码：粘贴「号码----收码链接」整行 → 出现「复制号码」→ 读码；自动刷新再排一次
  calls.splice(0);
  const line = '+18647089355----https://sms.example/smsrecord?token=abc';
  el('#smsSource').value = line;
  el('#smsSource').handlers.input();
  assert.equal(el('#smsDial').hidden, false);
  assert.equal(el('#smsDial').textContent, '复制号码 8647089355');   // 登录页区号单独选，只要 +1 后面十位
  assert.equal(el('#smsCode').textContent, '尚未读取');
  el('#smsAuto').checked = true;
  queue['/api/sms'] = [{status: 200, body: {ok: true, empty: false, code: '654321', text: '验证码 654321', status: 'yes'}}];
  await el('#smsRefresh').handlers.click();
  await tick();
  assert.deepEqual(calls.splice(0), [{path: '/api/sms', method: 'POST', headers: {'X-Token': 'tok-9',
    'Content-Type': 'application/json'}, body: {source: line}}]);
  assert.equal(el('#smsCode').textContent, '654321');
  assert.equal(el('#smsCode').className, '');
  assert.equal(el('#smsCopy').disabled, false);
  assert.equal(timers.length, 1);
  await el('#smsCopy').handlers.click();
  await tick();
  assert.equal(globalThis.copiedIp, '654321');
  await el('#smsDial').handlers.click();
  await tick();
  assert.equal(globalThis.copiedIp, '8647089355');
  assert.equal(el('#smsDial').textContent, '已复制 8647089355');

  // 读取期间换了号：旧号的回应不画，改读新号
  const realFetch = globalThis.fetch;
  const waiting = [];
  globalThis.fetch = (path, init) => new Promise(resolve => waiting.push({resolve, init}));
  const older = '+18647089356----https://sms.example/smsrecord?token=def';
  const newer = '+18647089357----https://sms.example/smsrecord?token=ghi';
  el('#smsSource').value = older;
  el('#smsSource').handlers.input();
  const changed = el('#smsSource').handlers.change();
  assert.equal(waiting.length, 1);
  assert.equal(JSON.parse(waiting[0].init.body).source, older);
  el('#smsSource').value = newer;
  el('#smsSource').handlers.input();
  waiting[0].resolve({ok: true, status: 200, json: async () => ({ok: true, empty: false, code: '111111', text: '旧号'})});
  await tick();
  assert.equal(waiting.length, 2);
  assert.equal(JSON.parse(waiting[1].init.body).source, newer);
  assert.equal(el('#smsCode').textContent, '尚未读取');
  waiting[1].resolve({ok: true, status: 200, json: async () => ({ok: true, empty: false, code: '222222', text: '新号'})});
  await changed;
  assert.equal(el('#smsCode').textContent, '222222');
  globalThis.fetch = realFetch;

  // 只贴链接、没有号码：不出「复制号码」
  el('#smsSource').value = 'https://sms.example/smsrecord?token=abc';
  el('#smsSource').handlers.input();
  assert.equal(el('#smsDial').hidden, true);

  // 重开页面：记住的整行自动填回，「复制号码」直接可用
  el('#smsSource').value = '';
  queue['/api/sms-source'].push({status: 200, body: {ok: true, source: line}});
  queue['/api/sms'].push({status: 200, body: {ok: true, empty: true, code: '', text: '暂时未有邮件'}});
  await loadSmsSource();
  assert.equal(el('#smsSource').value, line);
  assert.equal(el('#smsDial').hidden, false);
  assert.equal(el('#smsDial').textContent, '复制号码 8647089355');
  assert.equal(el('#smsCode').textContent, '暂无验证码');
  assert.equal(el('#smsCopy').textContent, '复制验证码');             // 换了号，上一个「已复制」不作数
'''


def test_page_submits_the_form_polls_status_and_pushes():
    """页面脚本在 node 里跑：假 DOM + 假 fetch，走通「开始登录 → 轮询 → 推送」。"""
    import shutil
    import subprocess
    node = shutil.which("node")
    if not node:
        pytest.skip("node required")
    html = (Path(__file__).resolve().parents[1] / "tools" / login_tool.PAGE_NAME).read_text(encoding="utf-8")
    script = html[html.index("<script>") + len("<script>"):html.index("</script>")]
    program = "\n".join([PAGE_HARNESS, "(async () => {", script, PAGE_CHECKS,
                         "})().catch(e => {console.error(e); process.exit(1);});"])
    subprocess.run([node, "-e", program], check=True, capture_output=True, timeout=15)
