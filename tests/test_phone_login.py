"""手机号登录：状态机、挑战交互与账号保存边界，不访问真实登录或生成接口。"""
import asyncio
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from test_task_deadline import _run


def control_stub(stages, code="654321"):
    async def ask(kind, stage, remaining):
        stages.append({"ask": kind})
        return code
    return SimpleNamespace(update=lambda **kw: stages.append(kw), ask=ask)


class Node:
    def __init__(self, frame, key, text="", bump=True):
        self.frame, self.key, self.text, self.bump = frame, key, text, bump

    def locator(self, selector):
        return self.frame.locator(selector)

    async def is_visible(self):
        return True

    async def is_enabled(self):
        return True

    async def inner_text(self):
        return self.text

    async def fill(self, value, **kwargs):
        self.frame.values[self.key] = value
        if self.key == "code":
            self.frame.step = 2  # 填完验证码 → 登录完成

    async def select_option(self, **kwargs):
        self.frame.values["country"] = kwargs.get("index")

    async def click(self, **kwargs):
        if self.bump:
            self.frame.step += 1


class Items:
    def __init__(self, items=()):
        self.items = list(items)

    @property
    def first(self):
        return self.items[0]

    async def count(self):
        return len(self.items)

    def nth(self, index):
        return self.items[index]


class CountryRow:
    def __init__(self, frame):
        self.frame = frame

    async def is_visible(self):
        return True

    async def is_enabled(self):
        return True

    def locator(self, selector):
        from dola_pool.phone_login import COUNTRY_ITEM_SELECTOR
        assert "[class*=left]" in selector
        left = Node(self.frame, "left")
        left.text = "+1"
        return Items([left])

    async def inner_text(self):
        return "+1"

    async def click(self, **kwargs):
        self.frame.values["country"] = "+1"


class Frame:
    """step 0 = 号码页，step 1 = 验证码页，step 2 = 登录完成。"""
    url = "https://www.dola.com/chat"

    def __init__(self):
        self.step, self.values = 0, {}

    async def evaluate(self, script, arg=None):
        return False  # evaluate 路径没找到行，走逐行遍历兜底

    def locator(self, selector):
        from dola_pool.phone_login import CODE_INPUT_SELECTOR, COUNTRY_ITEM_SELECTOR, COUNTRY_SELECT_SELECTOR
        if selector == COUNTRY_ITEM_SELECTOR:
            return Items([CountryRow(self)]) if self.step == 0 else Items()
        if selector == COUNTRY_SELECT_SELECTOR:
            return Items([Node(self, "country", bump=False)]) if self.step == 0 else Items()
        if selector == "[class*=left]":
            left = Node(self, "left")
            left.text = "+1"
            return Items([left])
        if selector == CODE_INPUT_SELECTOR:
            return Items([Node(self, "code")] if self.step == 1 else [])
        if selector in ('[data-testid="login_phone_number_input"]', 'input[type="tel"]'):
            return Items([Node(self, "phone")] if self.step == 0 else [])
        return Items()

    def get_by_role(self, role, **kwargs):
        return Items([Node(self, "send")] if self.step == 0 else [])


def test_phone_login_fills_country_number_then_challenge_code():
    from dola_pool import phone_login as pl
    from dola_pool.apple_login import AppleLoginControl
    frame = Frame()
    updates = []
    control = AppleLoginControl(lambda **fields: updates.append(fields))

    async def scripted_ask(kind, stage, remaining):
        updates.append(stage)
        assert kind == "code"
        return "654321"

    control.ask = scripted_ask
    async def cookies(*args):
        return [{"name": "sessionid", "value": "test-session"}] if frame.step >= 2 else []
    context = SimpleNamespace(pages=[SimpleNamespace(is_closed=lambda: False, frames=[frame])],
                              cookies=cookies)
    async def no_wait(seconds):
        pass
    monkey = pytest.MonkeyPatch()
    monkey.setattr(pl.asyncio, "sleep", no_wait)
    try:
        _run(pl.complete_phone_login(context, "2025551234", control, "+1"))
    finally:
        monkey.undo()
    assert frame.values["country"] == "+1"       # 区号选择器选到 +1
    assert frame.values["phone"] == "2025551234"
    assert frame.values["code"] == "654321"
    assert any("验证码已发送" in str(u) for u in updates)
    assert any("短信验证码" in str(u) for u in updates)


def test_phone_login_times_out_when_no_form_appears(monkeypatch):
    from dola_pool import phone_login as pl
    async def cookies(*args):
        return []
    context = SimpleNamespace(pages=[], cookies=cookies)
    async def no_sleep(seconds):
        pass
    monkeypatch.setattr(pl.asyncio, "sleep", no_sleep)
    monkeypatch.setattr(pl, "LOGIN_SECONDS", 0)
    with pytest.raises(pl.PhoneLoginError, match="10 分钟内未完成"):
        _run(pl.complete_phone_login(context, "2025551234", control_stub([]), "+1"))


def test_wait_entry_polls_until_slow_dialog_renders():
    """回归：点完登录按钮只等 2 秒会漏掉慢渲染的弹窗，入口要持续轮询。"""
    from dola_pool import phone_login as pl

    class SlowPage:
        def __init__(self):
            self.polls = 0

        def locator(self, selector):
            return Items([Node(None, "entry")]) if self.polls >= 3 else Items()

        async def wait_for_timeout(self, ms):
            self.polls += 1

    page = SlowPage()
    entry = _run(pl._wait_entry(page, 15000))
    assert entry is not None and page.polls == 3
    assert _run(pl._wait_entry(SlowPage(), 0)) is None


@pytest.fixture
def srv(tmp_path, monkeypatch):
    from dola_pool import server
    monkeypatch.setattr(server, "_admin_auth", lambda key: None)
    monkeypatch.setattr(server, "JOBS", {})
    monkeypatch.setattr(server, "APPLE_LOGINS", {})
    monkeypatch.setattr(server, "APPLE_ADD_TASKS", {})
    monkeypatch.setattr(server, "ADD_ACCOUNT_SEM", asyncio.Semaphore(1))
    monkeypatch.setattr(server, "ADD_SEM_HOLDER", {"what": ""})
    monkeypatch.setattr(server, "validate_proxy_ids", lambda ids: ids)
    monkeypatch.setattr(server, "get_account_proxy", lambda name: None)
    events = []
    monkeypatch.setattr(server, "pool", SimpleNamespace(accounts=[], find_accounts_by_email=lambda email: [],
        ensure_account=lambda n: events.append(("account", n)), set_email=lambda *a: None,
        set_source=lambda n, source: events.append(("source", source)), set_login_status=lambda *a: None))
    monkeypatch.setattr(server.cred_store, "store", lambda *a: pytest.fail("手机号登录不能保存凭据"))
    return server, events


def test_phone_job_uses_its_flow_and_marks_the_source(srv, monkeypatch):
    server, events = srv
    monkeypatch.setattr(server, "bind_selected_proxy", lambda *a: events.append(("bind", a)))
    async def flow(name, phone, control, country):
        assert events[0][0] == "bind" and server.JOBS[name]["source"] == "phone"
        assert (phone, country) == ("2025551234", "+1")
        assert server.JOBS[name]["phone"] == "2025551234"
        assert server.JOBS[name]["sms_link"] == "https://sms.example.com/r"
        events.append(("phone", name))
    monkeypatch.setattr(server, "phone_login_flow", flow)
    _run(server._run_add_job("new_phone", "", "", "", proxy_id="p1",
                             login_method="phone", phone="2025551234", country="+1",
                             sms_link="https://sms.example.com/r"))
    assert server.JOBS["new_phone"]["status"] == "success"
    assert ("source", "phone") in events
    assert not server.APPLE_LOGINS and not server.ADD_SEM_HOLDER["what"]


def test_add_accepts_phone_without_email_or_credentials(srv, monkeypatch):
    server, _ = srv
    received = {}
    async def flow(*args, **kwargs):
        received.update(kwargs)
    monkeypatch.setattr(server, "_run_add_job", flow)
    async def scenario():
        body = server.AccountAdd(name="new_phone", phone="2025551234",
                                 login_method="phone", proxy_id="p1",
                                 sms_link="https://sms.example.com/record?token=DOLtest")
        return await server.admin_account_add(body)
    assert _run(scenario())["job"] == "running"
    assert received["login_method"] == "phone" and received["phone"] == "2025551234"
    assert received["country"] == "+1"
    assert received["sms_link"] == "https://sms.example.com/record?token=DOLtest"


def test_add_rejects_non_http_sms_link(srv):
    server, _ = srv
    with pytest.raises(HTTPException) as caught:
        _run(server.admin_account_add(server.AccountAdd(name="new_phone", phone="2025551234",
                                                        login_method="phone", proxy_id="p1",
                                                        sms_link="javascript:alert(1)")))
    assert caught.value.status_code == 400 and "接码链接" in caught.value.detail


def test_phone_requires_number_and_proxy(srv):
    server, _ = srv
    with pytest.raises(HTTPException) as caught:
        _run(server.admin_account_add(server.AccountAdd(name="new_phone", phone="2025551234",
                                                        login_method="phone")))
    assert caught.value.status_code == 400 and "代理" in caught.value.detail
    with pytest.raises(HTTPException) as caught:
        _run(server.admin_account_add(server.AccountAdd(name="new_phone", phone="abc",
                                                        login_method="phone", proxy_id="p1")))
    assert caught.value.status_code == 400 and "手机号码" in caught.value.detail


def test_phone_source_is_not_automatically_relogged_through_google(srv, monkeypatch):
    server, _ = srv
    monkeypatch.setattr(server.cred_store, "all_names", lambda: [])
    monkeypatch.setattr(server, "pool", SimpleNamespace(list_accounts=lambda: [
        {"name": "old_phone", "source": "phone", "login_ok": 0}]))
    monkeypatch.setattr(server.cred_store, "load", lambda *a: pytest.fail("Must not load Google credentials"))
    _run(server._cred_try_refresh_once())


@pytest.mark.parametrize("outcome", ["success", "failure", "cancel"])
def test_profile_only_enters_the_pool_after_a_completed_login(tmp_path, monkeypatch, outcome):
    from dola_pool import phone_login as pl
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(pl.proxy_store, "get_account_proxy", lambda name: {"enabled": True})
    monkeypatch.setattr(pl, "http_proxy", lambda name: "http://127.0.0.1:12345")
    closed, launched = [], []
    entered = asyncio.Event()
    async def no_action(*args, **kwargs):
        pass
    async def _vis():
        return True
    async def _count():
        return 1
    entry = SimpleNamespace(wait_for=no_action, click=no_action, is_visible=_vis,
                            count=_count, nth=lambda index: entry)
    page = SimpleNamespace(goto=no_action, locator=lambda sel: entry,
                           wait_for_timeout=no_action, evaluate=lambda *a, **k: "{}")
    cookie = {"name": "sessionid", "value": "test-session", "domain": ".dola.com", "path": "/", "expires": -1}
    async def cookies(*args):
        return [cookie]
    async def close():
        closed.append(True)
    context = SimpleNamespace(pages=[page], cookies=cookies, close=close)
    async def launch(profile, **kwargs):
        launched.append(kwargs)
        (Path(profile) / "Default").mkdir()
        assert not (tmp_path / "accounts/new_phone").exists()
        return context
    class Playwright:
        async def __aenter__(self):
            return SimpleNamespace(chromium=SimpleNamespace(launch_persistent_context=launch))
        async def __aexit__(self, *args):
            pass
    monkeypatch.setitem(sys.modules, "patchright.async_api", SimpleNamespace(async_playwright=Playwright))
    async def complete(*args):
        entered.set()
        if outcome == "failure":
            raise RuntimeError("2025551234 https://www.dola.com/?token=test")
        if outcome == "cancel":
            await asyncio.Event().wait()
    monkeypatch.setattr(pl, "complete_phone_login", complete)
    control = SimpleNamespace(update=lambda **kw: None, clear=lambda: None)
    async def scenario():
        task = asyncio.create_task(pl.phone_login_flow("new_phone", "2025551234", control, "+1"))
        if outcome == "cancel":
            await entered.wait()
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        elif outcome == "failure":
            with pytest.raises(pl.PhoneLoginError) as caught:
                await task
            assert "2025551234" not in str(caught.value) and "token" not in str(caught.value)
        else:
            await task
    _run(scenario())
    assert len(closed) == 1 and launched[0]["proxy"] == {"server": "http://127.0.0.1:12345"}
    assert launched[0]["locale"] == "ja-JP"
    assert not list((tmp_path / "accounts").glob(".phone-*"))
    target = tmp_path / "accounts/new_phone"
    assert target.exists() == (outcome == "success")
    if outcome == "success":
        assert json.loads((target / pl.SESSION_FILE).read_text()) == [cookie]


def test_flow_rejects_bad_numbers_and_missing_proxy(tmp_path, monkeypatch):
    from dola_pool import phone_login as pl
    monkeypatch.chdir(tmp_path)
    with pytest.raises(pl.PhoneLoginError, match="手机号码格式"):
        _run(pl.phone_login_flow("new_phone", "abc", SimpleNamespace(), "+1"))
    monkeypatch.setattr(pl.proxy_store, "get_account_proxy", lambda name: None)
    with pytest.raises(pl.PhoneLoginError, match="代理"):
        _run(pl.phone_login_flow("new_phone", "2025551234", SimpleNamespace(), "+1"))
    assert not (tmp_path / "accounts").exists()


@pytest.mark.parametrize("raw,phone,country,link", [
    ("202-555-1234", "2025551234", "+1", ""),
    ("+18647089355----https://sms.example.com/record?token=DOLtest",
     "8647089355", "+1", "https://sms.example.com/record?token=DOLtest"),
    ("+85291234567----https://sms.example.com/x", "91234567", "+852", "https://sms.example.com/x"),
])
def test_panel_phone_submit_parses_paste_and_posts_body(raw, phone, country, link):
    import shutil
    import subprocess
    node = shutil.which("node")
    if not node:
        pytest.skip("node required")
    html = (Path(__file__).resolve().parents[1] / "web/index.html").read_text(encoding="utf-8")
    start = html.index("function parsePhonePaste(raw){")
    end = html.index("async function retryAppleLogin", start)
    code = html[start:end]
    script = ("const RAW=" + json.dumps(raw) + ",PHONE=" + json.dumps(phone)
              + ",COUNTRY=" + json.dumps(country) + ",LINK=" + json.dumps(link) + ";\n") + r'''
const assert=require('assert');
let dialogEpoch=1,addProxyLoading=false,addProxyError=false,singleAddProxy='p1',opened=null;
const nodes={'#f_name':{value:'acc1'},'#f_phone':{value:RAW},'#f_cc':{value:'+1',
  options:[{value:'+1'},{value:'+44'},{value:'+81'},{value:'+65'},{value:'+61'},{value:'+852'}]},
  '#phonePasteBox':{hidden:true},'#phonePastePhone':{textContent:''},'#phonePasteLink':{href:'',textContent:''}};
const q=s=>nodes[s];
const alert=(m)=>{throw new Error('alert:'+m);};
const dialogText=(s)=>s;
let posted=[];
const api=async(url,opts)=>{posted.push({url,body:opts.body});return {ok:true};};
const refresh=()=>{};
const openAppleProgress=(name,title)=>{opened={name,title};};
''' + code + r'''
(async()=>{
 renderPhonePaste();
 assert.equal(q('#phonePasteBox').hidden,LINK?false:true);
 if(LINK){
   assert.equal(q('#phonePastePhone').textContent,PHONE);
   assert.equal(q('#phonePasteLink').href,LINK);
   assert.equal(q('#f_cc').value,COUNTRY);
 }
 await submitAddPhone();
 assert.equal(posted[0].body.login_method,'phone');
 assert.equal(posted[0].body.phone,PHONE);
 assert.equal(posted[0].body.country,COUNTRY);
 assert.equal(posted[0].body.sms_link,LINK||undefined);
 assert.deepEqual(opened,{name:'acc1',title:'手机号登录'});
})().catch(e=>{console.error(e);process.exit(1)});
'''
    subprocess.run([node, "-e", script], check=True, capture_output=True, timeout=15)
