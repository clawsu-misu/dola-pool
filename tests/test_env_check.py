"""面板「环境检查」：本机运行环境探测、结果缓存、管理接口鉴权，以及面板页签与渲染。

探测函数都是纯本机检查（进程 / 文件 / 配置），测试用 monkeypatch 换掉具体探测，
不依赖跑测试的机器上装了什么；接口用 httpx 的 ASGI transport 直接打，不访问网络。
"""
import asyncio
import json
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from dola_pool import config
from dola_pool import env_check
from dola_pool import pure_signer
from dola_pool import updater

ROOT = Path(__file__).resolve().parents[1]
HTML = ROOT / "web" / "index.html"


@pytest.fixture(autouse=True)
def fresh_cache():
    env_check._cache.update(at=0.0, data=None)
    yield
    env_check._cache.update(at=0.0, data=None)


@pytest.fixture
def server(monkeypatch):
    from dola_pool import server as server_mod
    monkeypatch.setattr(server_mod, "ADMIN_SESSIONS",
                        {"test-token": {"username": "admin", "expires_at": time.time() + 600}})
    monkeypatch.setattr(server_mod, "user_get", lambda _: {"enabled": True})
    monkeypatch.setattr(server_mod, "pool", SimpleNamespace(accounts=["acc1", "acc2"]))
    return server_mod


def call(server_mod, path, token="test-token"):
    async def go():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=server_mod.app),
                                     base_url="http://test") as c:
            return await c.get(path, headers={"X-Admin-Key": token} if token else {})
    return asyncio.run(go())


def test_env_check_requires_an_admin_session(server):
    assert call(server, "/api/admin/env-check", token=None).status_code == 401
    assert call(server, "/api/admin/env-check", token="invalid").status_code == 401


def test_endpoint_returns_every_check_with_a_level_and_passes_the_account_count(server, monkeypatch):
    seen = {}
    monkeypatch.setattr(env_check, "_node", lambda: env_check._item("node", "ok", "v22.0.0", "/usr/bin/node"))
    real_run = env_check.run
    def spy(**kwargs):
        seen.update(kwargs)
        return real_run(**kwargs)
    monkeypatch.setattr(env_check, "run", spy)
    r = call(server, "/api/admin/env-check")
    assert r.status_code == 200
    data = r.json()
    assert seen["accounts"] == 2
    keys = [item["key"] for item in data["items"]]
    assert keys == ["python", "node", "ffmpeg", "browser", "signer", "deploy",
                    "storage", "disk", "accounts", "proxy"]
    assert set(data["summary"]) == set(env_check.LEVELS)
    assert sum(data["summary"].values()) == len(data["items"])
    for item in data["items"]:
        assert set(item) == {"key", "level", "value", "note"}
        assert item["level"] in env_check.LEVELS


def test_result_is_cached_until_refresh_is_requested(server, monkeypatch):
    calls = []
    def fake_disk():
        calls.append(1)
        return env_check._item("disk", "ok", "10 GB")
    monkeypatch.setattr(env_check, "_disk", fake_disk)
    assert call(server, "/api/admin/env-check").status_code == 200
    assert call(server, "/api/admin/env-check").status_code == 200
    assert len(calls) == 1                       # 第二次命中缓存，不再重跑探测
    assert call(server, "/api/admin/env-check?refresh=1").status_code == 200
    assert len(calls) == 2


def test_python_check_warns_only_when_the_runtime_can_be_upgraded(monkeypatch):
    monkeypatch.setattr(updater, "runtime_info", lambda: {"python": "3.10.12", "upgradable": True})
    monkeypatch.setattr(sys, "version_info", (3, 10, 12, "final", 0))
    assert env_check._python() == {"key": "python", "level": "warn", "value": "3.10.12", "note": ""}
    monkeypatch.setattr(updater, "runtime_info", lambda: {"python": "3.10.12", "upgradable": False})
    assert env_check._python()["level"] == "info"      # Docker 部署：随镜像走，不算问题


def test_python_check_is_ok_on_supported_runtimes(monkeypatch):
    monkeypatch.setattr(sys, "version_info", (3, 12, 4, "final", 0))
    assert env_check._python() == {"key": "python", "level": "ok", "value": "3.12.4", "note": ""}


def test_missing_node_is_an_error_with_the_dola_node_hint(monkeypatch, tmp_path):
    monkeypatch.delenv("DOLA_NODE", raising=False)
    monkeypatch.setattr(pure_signer, "resolve_node", lambda: str(tmp_path / "node"))
    item = env_check._node()
    assert item["level"] == "error" and "PATH" in item["note"]
    monkeypatch.setenv("DOLA_NODE", str(tmp_path / "gone"))
    assert "DOLA_NODE" in env_check._node()["note"]


def test_node_version_is_read_from_the_binary(monkeypatch, tmp_path):
    node = tmp_path / "node"
    node.write_text("#!/bin/sh\necho v22.11.0\n", encoding="utf-8")
    node.chmod(0o755)
    monkeypatch.setattr(pure_signer, "resolve_node", lambda: str(node))
    assert env_check._node() == {"key": "node", "level": "ok", "value": "v22.11.0", "note": str(node)}


def test_browser_kernel_reports_the_driver_reason_and_the_missing_binary(monkeypatch):
    from dola_pool import cookie_login
    monkeypatch.setattr(cookie_login, "browser_backend_available",
                        lambda: (False, "patchright 驱动不存在（未安装或包被裁剪）"))
    item = env_check._browser()
    assert item["level"] == "warn" and "驱动不存在" in item["note"]
    monkeypatch.setattr(cookie_login, "browser_backend_available", lambda: (True, ""))
    monkeypatch.setattr(env_check, "_chromium_executable", lambda: "")
    assert "Chromium 内核" in env_check._browser()["note"]


def test_storage_check_creates_missing_dirs_and_flags_unwritable_ones(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "DB_PATH", str(tmp_path / "a" / "tasks.db"))
    monkeypatch.setattr(config, "POOL_DB_PATH", str(tmp_path / "b" / "pool.db"))
    monkeypatch.setattr(config, "DOWNLOAD_DIR", str(tmp_path / "c"))
    monkeypatch.setattr(config, "UPDATE_DIR", str(tmp_path / "d"))
    item = env_check._storage()
    assert item["level"] == "ok"
    assert all((tmp_path / name).is_dir() for name in "abcd")

    monkeypatch.setattr(env_check.os, "access", lambda path, mode: False)
    broken = env_check._storage()
    assert broken["level"] == "error" and "没有写权限" in broken["note"]


def test_accounts_and_proxy_report_the_configured_facts(monkeypatch):
    assert env_check._accounts(0)["level"] == "warn"
    assert env_check._accounts(3) == {"key": "accounts", "level": "ok", "value": "3", "note": ""}
    monkeypatch.setattr(config, "PROXY", "http://user:hunter2@jp.example.test:3128")
    assert env_check._proxy() == {"key": "proxy", "level": "info",
                                 "value": "http://jp.example.test:3128", "note": ""}
    monkeypatch.setattr(config, "PROXY", "")
    assert env_check._proxy()["value"] == ""


def test_run_summarises_the_items(monkeypatch):
    monkeypatch.setattr(env_check, "_python", lambda: env_check._item("python", "ok", "3.12.4"))
    monkeypatch.setattr(env_check, "_node", lambda: env_check._item("node", "error", "", "缺 node"))
    monkeypatch.setattr(env_check, "_ffmpeg", lambda: env_check._item("ffmpeg", "ok", "6.1"))
    monkeypatch.setattr(env_check, "_browser", lambda: env_check._item("browser", "warn", "", "没内核"))
    monkeypatch.setattr(env_check, "_signer", lambda: env_check._item("signer", "ok", "", "在"))
    monkeypatch.setattr(env_check, "_deploy", lambda: env_check._item("deploy", "ok", "source"))
    monkeypatch.setattr(env_check, "_storage", lambda: env_check._item("storage", "ok", "", "/srv"))
    monkeypatch.setattr(env_check, "_disk", lambda: env_check._item("disk", "ok", "10 GB"))
    monkeypatch.setattr(env_check, "_proxy", lambda: env_check._item("proxy", "info", ""))
    data = env_check.run(accounts=1)
    assert data["summary"] == {"ok": 7, "warn": 1, "error": 1, "info": 1}
    assert data["deploy_mode"] in ("source", "docker")


# ---------------------------------------------------------------- 面板

NODE = r'''
const fs=require('fs'),assert=require('assert');
const html=fs.readFileSync(process.argv[1],'utf8');
const start=html.indexOf('/* ---- 环境检查 ---- */'),end=html.indexOf('async function setKey(',start);
assert.ok(start>0&&end>start,'找不到环境检查面板代码');
const es=html.indexOf('function esc(s){'),ee=html.indexOf('\n',es);
const ns=html.indexOf('function noTr(s){'),ne=html.indexOf('\n',ns);
const ts=html.indexOf('function table(heads,rows){'),te=html.indexOf('\nasync function setSched(',ts);
assert.ok(ts>0&&te>ts);
(async()=>{
  const nodes={};
  const q=s=>nodes[s]||(nodes[s]={innerHTML:'',style:{},dataset:{},textContent:''});
  const cap={urls:[]};
  let api=async(url)=>{cap.urls.push(url);return cap.response;};
  let alert=()=>{},toast=()=>{},copy=()=>{};
  eval(html.slice(es,ee)+html.slice(ns,ne)+html.slice(ts,te)+html.slice(start,end));
  cap.response={items:[
    {key:'python',level:'warn',value:'3.10.12',note:''},
    {key:'node',level:'ok',value:'v22.11.0',note:'/usr/bin/node'},
    {key:'signer',level:'error',value:'',note:'签名脚本缺失：/opt/x/bdms_sign_url.js'}
  ],summary:{ok:1,warn:1,error:1,info:0},checked_at:1700000000,deploy_mode:'source'};
  await loadEnv();
  await loadEnv(true);
  console.log(JSON.stringify({urls:cap.urls,summary:q('#envSummary').innerHTML,table:q('#envTable').innerHTML}));
})().catch(e=>{console.error(e);process.exit(1)});
'''


@pytest.fixture(scope="module")
def panel():
    node = shutil.which("node")
    if not node:
        pytest.skip("需要 node")
    r = subprocess.run([node, "-e", NODE, str(HTML)], capture_output=True, text=True,
                       encoding="utf-8", timeout=60)
    assert r.returncode == 0, r.stderr
    return json.loads(r.stdout.strip().splitlines()[-1])


def test_panel_requests_the_endpoint_and_can_force_a_recheck(panel):
    assert panel["urls"] == ["/api/admin/env-check", "/api/admin/env-check?refresh=1"]


def test_panel_summary_counts_by_level(panel):
    assert "正常 1" in panel["summary"] and "注意 1" in panel["summary"] and "异常 1" in panel["summary"]


def test_panel_rows_show_facts_untranslated_and_explain_the_fix(panel):
    assert '<span translate="no">3.10.12</span>' in panel["table"]
    assert '<span translate="no">/usr/bin/node</span>' in panel["table"]
    assert "签名脚本缺失" in panel["table"]                 # 后端报错原文，不参与翻译
    assert "处理：右上角「版本」→「升级运行环境」" in panel["table"]
    assert "处理：" in panel["table"] and "python -m patchright" not in panel["table"]


def test_panel_nav_has_an_env_tab_wired_into_switch_tab():
    html = HTML.read_text(encoding="utf-8")
    assert '<button class="nav-item" data-tab="env" onclick="switchTab(\'env\')"' in html
    assert '<section id="tab-env" style="display:none">' in html
    switch = html[html.index("function switchTab(t){"):]
    switch = switch[:switch.index("\n}")]
    assert "'env'" in switch
    assert "else if(tab==='env')await loadEnv();" in html


def test_every_check_has_panel_copy_and_no_untranslated_backend_text():
    """后端只给事实，说明文字全在面板里；新增一项检查忘了补文案会在这里挂掉。"""
    html = HTML.read_text(encoding="utf-8")
    block = html[html.index("/* ---- 环境检查 ---- */"):html.index("async function setKey(")]
    keys = set(re.findall(r'_item\("(\w+)"', Path(env_check.__file__).read_text(encoding="utf-8")))
    assert keys == {"python", "node", "ffmpeg", "browser", "signer", "deploy",
                    "storage", "disk", "accounts", "proxy"}
    for key in keys:
        assert re.search(rf"\b{key}:\{{", block), f"{key} 缺少面板文案"
        assert re.search(rf"\b{key}:'", block), f"{key} 缺少处理办法"
