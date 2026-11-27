"""dola 账号登录态采集工具（本机运行，登录过程全人工）。

用法：
  python tools/login.py --web               网页界面：自动打开 http://127.0.0.1:<端口>/?token=...，全程点按钮
  python tools/login.py <账号名> [选项]      命令行

网页上还有一块「短信验证码」：把买号时给的整行粘进去（号码----收码链接，例如 xsd20vip 的 /smsrecord），
页面给出「复制号码」按钮（去掉 +1 区号），本机直接去读并把验证码显示出来，可手动刷新或每 3 秒自动刷新，不必再开一个浏览器窗口。

打开本机浏览器访问 dola.com，你手动完成登录——Google / Apple / 手机号 / 任意方式都行，
滑块、cookie 同意卡、区号下拉都直接用手点，工具不碰页面元素。检测到登录态（sessionid）后：

1. 保存 profile 到 accounts/<账号名>/（会话仍有效时，下次重跑免登录；--fresh 从零登录）；
2. 导出 Cookie 到 accounts/<账号名>/cookies-export.json（dola-pool-cookies-v1，与服务器
   面板「Cookie 导入」互通），并复制到剪贴板——到服务器面板粘贴导入即可；
3. 直推服务器（网页上点「推送」，命令行给 --server）：等导入和在线验证结束并回报结果。

号在服务器上走纯 API 出片，不需要浏览器，官方精简镜像也能用。

代理（登录出口必须是日本/韩国）：
  --proxy URL      任意代理，如 http://user:pass@host:port
  --p1024-*        1024Proxy 账密隧道，按官方用户名格式自动拼：
                   <账号>-region-JP-sid-<会话>-t-120
  都不给           沿用本机代理库里该账号绑定的代理

Cookie 就是账号的登录凭证：导出文件不要外传；工具不会把 Cookie 值、代理密码、
管理员密码、会话令牌打印到终端，网页界面的接口也不把它们返回给页面。
"""
import argparse
import asyncio
import getpass
import ipaddress
import json
import os
import re
import secrets
import shutil
import socket
import subprocess
import sys
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import parse_qs, quote, urlsplit

import requests
from patchright.async_api import async_playwright

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from dola_pool import proxy_1024, proxy_store
from dola_pool.browser import (LAUNCH_ARGS, _dola_cookie, _kwargs_from_url, _save_cloak_cookies,
                               proxy_kwargs_for)

EXPORT_NAME = "cookies-export.json"   # 放在账号目录里：已 gitignore，也不会被对外静态服务
PAGE_NAME = "login_web.html"          # 网页界面的页面，与本脚本同目录
LOGIN_SECONDS = 900                   # 人工登录最长等 15 分钟
POLL_SECONDS = 5
HTTP_TIMEOUT = 20
PUSH_TIMEOUT = 240                    # 服务器导入 + 在线验证最长等 4 分钟
SUPPORTED_REGIONS = ("JP", "KR")      # dola 只接受日本 / 韩国出口
ACCOUNT_NAME_RE = re.compile(r"[A-Za-z0-9_-]+")
SMS_URL_NAME = "sms-record.txt"          # 仓库根目录；/*.txt 已 gitignore，收码链接（含 token）不进仓库
_SMS_EMPTY = {"no", "0", "false", "fail", "error", "none", "null"}
_SMS_CODE_NEAR = re.compile(r"(?:验证码|校验码|驗證碼|code|otp|pin)\D{0,16}(\d{4,8})", re.I)
_SMS_DATE = re.compile(r"\d{4}-\d{1,2}-\d{1,2}|\d{1,2}:\d{2}(?::\d{2})?")
_SMS_STANDALONE = re.compile(r"(?<!\d)(\d{4,8})(?!\d)")
COOKIE_FIELDS = ("name", "value", "domain", "path", "expires", "secure", "httpOnly", "sameSite")


class ToolError(RuntimeError):
    """可以直接展示给操作者的错误：不含 Cookie 值、代理密码、会话令牌。"""


def check_account_name(account):
    account = (account or "").strip()
    if not ACCOUNT_NAME_RE.fullmatch(account):
        raise ToolError("账号名只能用字母、数字、下划线和短横线")
    return account


def build_1024_proxy(user, password, gateway, *, region="JP", session=None, ttl=120, protocol="http"):
    """1024Proxy 账密隧道：按官方用户名格式拼代理 URL。

    <账号>-region-<地区>-sid-<会话>-t-<粘性分钟>:<密码>@<网关 host:port>
    默认值与面板「添加代理 → 1024Proxy」专用页签一致（出口日本、粘性 120 分钟）。
    """
    user, password, gateway = (user or "").strip(), password or "", (gateway or "").strip()
    if not (user and password and gateway):
        raise ToolError("1024Proxy 需要账号、密码和网关地址（host:port，在 1024 控制台「账密认证 → 去使用」查看）")
    # 控制台「去使用」给的是拼好的完整用户名。再套一层会变成
    # …-t-10-region-JP-sid-…-t-10，网关直接断开。这种原样用，表单里的地区 / 会话 / 时长不再生效。
    if re.fullmatch(r".+-region-[A-Za-z]{2,6}-sid-[A-Za-z0-9]{1,32}-t-(?:[1-9]\d?|1[01]\d|120)", user):
        login = user
    else:
        if not re.fullmatch(r"[A-Za-z]{2,6}", region or ""):
            raise ToolError("1024Proxy 地区用字母代码，例如 JP、KR")
        if not isinstance(ttl, int) or not 1 <= ttl <= 120:
            raise ToolError("1024Proxy 粘性时长取 1-120 分钟")
        session = session or secrets.token_hex(4)
        if not re.fullmatch(r"[A-Za-z0-9]{1,32}", session):
            raise ToolError("1024Proxy 会话 ID 只能用 1-32 位字母数字")
        # 用户名格式只在 proxy_1024 里维护一份，面板的 1024Proxy 专用流程走同一个函数
        login = proxy_1024.tunnel_username(
            {"username": user, "provider_settings": {"region": region, "ttl": ttl}}, session)
    return f"{protocol}://{quote(login, safe='')}:{quote(password, safe='')}@{gateway}"


def resolve_proxy(args, account, prompt=getpass.getpass):
    """定下浏览器要用的代理：--proxy > --p1024-* > 本机代理库里该账号绑定的代理。

    返回 (Playwright 代理参数 或 None, 探测出口用的 URL 或 None)。
    网页模式没有终端，prompt=None 时缺密码直接报错。
    """
    if args.proxy and args.p1024_user:
        raise ToolError("--proxy 和 --p1024-* 只能给一个")
    if args.p1024_user:
        password = args.p1024_pass or os.getenv("DOLA_P1024_PASS")
        if not password:
            if prompt is None:
                raise ToolError("请填写 1024Proxy 密码（或设置环境变量 DOLA_P1024_PASS）")
            password = prompt("1024Proxy 密码: ")
        url = build_1024_proxy(args.p1024_user, password, args.p1024_gateway,
                               region=args.p1024_region, session=args.p1024_session, ttl=args.p1024_ttl)
    elif args.proxy:
        url = args.proxy
    else:
        return proxy_kwargs_for(account), proxy_store.account_proxy_url(account)
    try:
        kwargs = _kwargs_from_url(url)
    except ValueError as exc:
        from dola_pool.account_transport import redact_error
        raise ToolError("代理地址无法解析：" + redact_error(exc)[:200]) from None
    if not kwargs:
        raise ToolError("代理地址无法解析（应为 host:port 或 协议://用户:密码@host:port）")
    return kwargs, url


def admin_password(raw, username, prompt=getpass.getpass):
    """管理员密码：参数 > 环境变量 DOLA_ADMIN_PASSWORD > 交互输入（网页模式 prompt=None 时缺密码直接报错）。"""
    password = raw or os.getenv("DOLA_ADMIN_PASSWORD")
    if not password:
        if prompt is None:
            raise ToolError("请填写服务器管理员密码（或设置环境变量 DOLA_ADMIN_PASSWORD）")
        password = prompt(f"服务器管理员 {username} 的密码: ")
    return password


def describe_exit(proxy_url, log=print):
    """登录前查一次出口：连不上就停，不打开浏览器。dola 只认日本 / 韩国出口。"""
    if not proxy_url or proxy_url == "direct":
        log("代理出口：未配置代理，用本机网络直连（请自行确认出口在日本/韩国）")
        return
    # requests 对 socks5:// 会在本机解析目标域名再把 IP 交给代理，dola 的 CDN 经常因此连不上。
    # 探测走 socks5h（域名交给代理解析）。浏览器不认 socks5h，仍用原来的 socks5://。
    probe_url = "socks5h://" + proxy_url[len("socks5://"):] if proxy_url.startswith("socks5://") else proxy_url
    info = proxy_store.probe_exit_ip(probe_url)
    if not info.get("ok"):
        reason = (info.get("error") or "未知原因").replace("\n", " ")
        raise ToolError("代理不可用，已停止，不会打开浏览器：" + reason)
    where = " / ".join(x for x in (info.get("country"), info.get("region"), info.get("isp")) if x)
    log(f"代理出口：{info.get('ip')}  {where}")
    if info.get("country_code") not in SUPPORTED_REGIONS:
        log(f"⚠ 出口不是日本/韩国（{info.get('country_code')}），dola 可能拒绝访问")


# 只从 dola 自己的源读存储。登录页会嵌第三方脚本，它们的 localStorage 里
# 可能有追踪标识，不该跟着账号一起送到服务器。
_STORAGE_JS = r"""() => {
  const own = (storage) => {
    const out = {};
    for (let i = 0; i < storage.length; i++) {
      const key = storage.key(i);
      if (location.hostname.endsWith('dola.com')) out[key] = storage.getItem(key);
    }
    return out;
  };
  return { localStorage: own(localStorage), sessionStorage: own(sessionStorage) };
}"""


async def capture_login(account, proxy_kwargs, log=print):
    """开有头浏览器让人工登录，抓到 sessionid 后返回 (dola 域 cookie, 页面存储)。

    页面存储是 localStorage / sessionStorage：dola 的设备标识（web_id 等）写在里面，
    只传 Cookie 时服务器端的纯 API 拿不到，提交容易被判「访问频繁」。
    """
    profile_dir = Path("accounts") / account
    profile_dir.mkdir(parents=True, exist_ok=True)
    kwargs = {"headless": False, "args": LAUNCH_ARGS, "locale": "ja-JP", "timezone_id": "Asia/Tokyo"}
    if proxy_kwargs:
        kwargs["proxy"] = proxy_kwargs
    async with async_playwright() as playwright:
        binary = getattr(playwright.chromium, "executable_path", "")
        if binary and not Path(binary).is_file():
            raise ToolError("浏览器内核没装，已停止。先运行：python -m patchright install chromium")
        context = await playwright.chromium.launch_persistent_context(str(profile_dir), **kwargs)
        try:
            page = context.pages[0] if context.pages else await context.new_page()
            await page.goto("https://www.dola.com/chat", timeout=60000)
            log(f"[{account}] 浏览器已打开，请在窗口里手动完成登录（任意方式，登进 dola 即可）...")
            deadline = time.monotonic() + LOGIN_SECONDS
            while time.monotonic() < deadline:
                cookies = await context.cookies("https://www.dola.com")
                if any(c.get("name") == "sessionid" and c.get("value") for c in cookies):
                    log(f"[{account}] 检测到登录态，等会话 cookie 写完...")
                    await page.wait_for_timeout(4000)
                    storage = {}
                    try:
                        storage = await page.evaluate(_STORAGE_JS) or {}
                    except Exception as exc:  # noqa: BLE001 - 存储读不到不该让登录白做
                        log(f"[{account}] 页面存储读取失败（{type(exc).__name__}），只导出 Cookie")
                    return cookies, storage
                await page.wait_for_timeout(POLL_SECONDS * 1000)
            raise ToolError(f"{LOGIN_SECONDS // 60} 分钟内未检测到登录，请重新开始")
        except Exception as exc:
            if type(exc).__name__ == "TargetClosedError":
                raise ToolError("浏览器窗口已关闭，登录已取消") from None
            raise
        finally:
            try:
                await context.close()
            except Exception:
                pass      # 窗口已被关掉时浏览器连接可能已断，清理失败无所谓


async def login_and_export(account, proxy_kwargs, proxy_url, *, fresh=False, out=None, clipboard=True, log=print):
    """登录 → 抓 Cookie → 导出文件（→ 剪贴板）。命令行和网页界面共用。

    返回 (导出包, 文件路径, 是否已复制到剪贴板)。
    """
    if proxy_kwargs:
        who = f"（账号 {proxy_kwargs['username']}）" if proxy_kwargs.get("username") else ""
        log(f"浏览器代理：{proxy_kwargs['server']}{who}")
    else:
        log("浏览器代理：未配置，直连")
    describe_exit(proxy_url, log=log)
    profile_dir = Path("accounts") / account
    if fresh and profile_dir.exists():
        shutil.rmtree(profile_dir)
        log("已清掉本地旧 profile，从零登录")
    cookies, storage = await capture_login(account, proxy_kwargs, log=log)
    bundle = cookie_bundle(account, cookies, storage)
    target = export_bundle(bundle, Path(out) if out else profile_dir / EXPORT_NAME)
    stored = sum(len(v) for v in (bundle.get("localStorage"), bundle.get("sessionStorage")) if v)
    log(f"✓ 已导出 {len(bundle['cookies'])} 条 Cookie"
        + (f"、{stored} 项页面存储" if stored else "") + f" → {target}")
    copied = False
    if clipboard:
        copied = copy_to_clipboard(json.dumps(bundle, ensure_ascii=False))
        if copied:
            log("✓ 已复制到剪贴板：到服务器面板「Cookie 导入」直接粘贴")
        else:
            log(f"  没有可用的剪贴板命令，请打开 {target} 手动复制全部内容")
    return bundle, target, copied


def cookie_bundle(account, cookies, storage=None):
    """按 dola-pool-cookies-v1 打包 dola 域 cookie（字段白名单，与服务器导出格式互通）。

    storage 是页面的 localStorage / sessionStorage。dola 的设备标识写在 localStorage 里，
    服务器导入时会一并落盘；没有就只导 Cookie，和旧格式兼容。
    """
    items = [{k: c[k] for k in COOKIE_FIELDS if k in c}
             for c in cookies if isinstance(c, dict) and _dola_cookie(c)]
    if not any(c["name"] == "sessionid" and c.get("value") for c in items):
        raise ToolError("抓到的 Cookie 里没有 sessionid，登录没有完成")
    bundle = {"format": "dola-pool-cookies-v1", "account": account, "source": "local-login",
              "cookies": items}
    for key in ("localStorage", "sessionStorage"):
        data = (storage or {}).get(key)
        if isinstance(data, dict) and data:
            bundle[key] = {str(k): "" if v is None else str(v) for k, v in data.items()}
    return bundle


def export_bundle(bundle, path):
    """原子写入导出文件（权限 0600），返回路径。"""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    _save_cloak_cookies(path, bundle)
    return path


def copy_to_clipboard(text, platform=None):
    """把导出内容复制到剪贴板；系统没有对应命令（或复制失败）时返回 False。"""
    command = {"darwin": ["pbcopy"], "linux": ["xclip", "-selection", "clipboard"],
               "win32": ["clip"]}.get(platform or sys.platform)
    if not command or not shutil.which(command[0]):
        return False
    try:
        subprocess.run(command, input=text.encode("utf-8"), check=True, timeout=10,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except (OSError, subprocess.SubprocessError):
        return False
    return True


def normalize_server(raw, log=print):
    """补全协议（默认 https），记账本地址给出提醒。"""
    base = (raw or "").strip().rstrip("/")
    if "://" not in base:
        base = "https://" + base
    parts = urlsplit(base)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise ToolError("服务器地址不正确，例如 https://dola.example.com")
    if parts.scheme == "http" and parts.hostname not in ("localhost", "127.0.0.1", "::1"):
        log("⚠ 服务器是 http：管理员密码与 Cookie 会明文经过网络，建议改用 https")
    return base


def _call(base, token, method, path, **kwargs):
    """调 dola-pool 管理接口。非 2xx 时把服务端说明原样抛出（服务端已脱敏）。"""
    try:
        resp = requests.request(method, base + path, headers={"X-Admin-Key": token} if token else {},
                                timeout=HTTP_TIMEOUT, **kwargs)
    except requests.RequestException as exc:
        raise ToolError(f"连不上服务器 {base}（{type(exc).__name__}）") from None
    if resp.status_code >= 400:
        try:
            detail = str((resp.json() or {}).get("detail") or "")
        except ValueError:
            detail = ""
        raise ToolError(f"服务器返回 {resp.status_code}：{detail[:300] or resp.reason}")
    return resp.json() if resp.content else {}


def server_login(base, username, password):
    """用面板的管理员账号换会话令牌（管理接口只认令牌，没有静态 admin key）。"""
    data = _call(base, None, "POST", "/api/admin/login", json={"username": username, "password": password})
    token = data.get("token")
    if not token:
        raise ToolError("服务器没有返回登录令牌")
    return token


def push_to_server(base, token, bundle, *, skip_verify=False, proxy_id=None):
    """推送导出的登录态到服务器，等导入（含在线验证）结束，返回结果说明。

    传整个导出包而不只是 cookies 数组：包里的 localStorage / sessionStorage
    服务器会原样恢复，纯 API 出片才拿得到 web_id 这些设备标识。
    """
    name = bundle["account"]
    _call(base, token, "POST", "/api/admin/accounts", json={
        "name": name,
        "cookies": json.dumps(bundle, ensure_ascii=False),
        "cookie_skip_verify": bool(skip_verify),
        "proxy_id": proxy_id,
    })
    for _ in range(PUSH_TIMEOUT // 2):
        job = _call(base, token, "GET", "/api/admin/jobs").get("jobs", {}).get(name) or {}
        if job.get("status") != "running":
            break
        time.sleep(2)
    else:
        raise ToolError("服务器导入超时，请稍后到面板查看该账号状态")
    if job.get("status") != "success":
        error = (job.get("error") or "未知原因")[:300]
        if "已存在" in error:
            error += "（续期请先在服务器上删除同名旧号再重推）"
        raise ToolError("服务器导入失败：" + error)
    if skip_verify:
        return "已导入（跳过了在线验证）"
    accounts = _call(base, token, "GET", "/api/admin/accounts").get("accounts") or []
    account = next((a for a in accounts if a.get("name") == name), {})
    if account.get("login_ok"):
        return "已导入，服务器在线验证通过"
    return "已导入，但服务器在线验证未通过：到面板给该号绑定日本/韩国代理后点「验证」"


class WebApp:
    """网页界面的全部状态：同一时间只跑一个登录任务（后台线程），页面轮询 /api/status。

    导出的 Cookie 只留在本进程内存里，推送时才发给你的服务器；任何接口都不把它返回给页面。
    """

    def __init__(self):
        self.lock = threading.Lock()
        self.state = self._idle()
        self.bundle = None

    @staticmethod
    def _idle(status="idle"):
        return {"status": status, "lines": [], "account": "", "export_path": "", "exported": 0, "clipboard": None}

    def _set(self, **fields):
        with self.lock:
            self.state.update(fields)

    def log(self, text):
        with self.lock:
            self.state["lines"].append(text)
        print(text, flush=True)                      # 终端同步一份

    def status(self):
        with self.lock:
            return {**self.state, "lines": list(self.state["lines"])}

    def start(self, payload):
        with self.lock:
            if self.state["status"] == "running":
                return 409, {"ok": False, "error": "已有登录任务在进行"}
            self.state, self.bundle = self._idle(status="running"), None
        threading.Thread(target=self._run, args=(payload,), daemon=True).start()
        return 200, {"ok": True}

    def _run(self, payload):
        try:
            account = check_account_name(_form_text(payload, "account"))
            self._set(account=account)
            proxy_kwargs, proxy_url = resolve_proxy(web_proxy_args(payload), account, prompt=None)
            bundle, target, copied = asyncio.run(login_and_export(
                account, proxy_kwargs, proxy_url, fresh=bool(payload.get("fresh")), log=self.log))
            self.bundle = bundle                 # 先放好导出包，页面才会显示「推送」
            self._set(status="done", export_path=str(target), exported=len(bundle["cookies"]), clipboard=copied)
        except ToolError as exc:
            self.log(f"✗ {exc}")
            self._set(status="error")
        except Exception as exc:
            self.log(_public_failure(exc))
            self._set(status="error")

    def push(self, payload):
        with self.lock:
            bundle = self.bundle          # 取一份本地引用：推送期间开始下一个登录会清掉 self.bundle
        if not bundle:
            return 409, {"ok": False, "error": "还没有导出的 Cookie：先完成一次登录"}
        username = _form_text(payload, "user")
        try:
            if not username:
                raise ToolError("请填写服务器面板的管理员用户名")
            base = normalize_server(_form_text(payload, "server"), log=self.log)
            token = server_login(base, username,
                                 admin_password(str(payload.get("pass") or ""), username, prompt=None))
            message = f"{bundle['account']}：" + push_to_server(
                base, token, bundle, skip_verify=bool(payload.get("skip_verify")),
                proxy_id=_form_text(payload, "proxy_id") or None)
        except ToolError as exc:
            return 400, {"ok": False, "error": str(exc)}
        self.log("✓ " + message)
        return 200, {"ok": True, "message": message}


def _form_text(payload, key):
    return str(payload.get(key) or "").strip()


def public_ip(timeout=8):
    """查本机访问外网时的公网 IP。和提取接口走同一条网络，1024 白名单要加的就是它。"""
    try:
        resp = requests.get("http://ip-api.com/json/?fields=status,query", timeout=timeout)
        resp.raise_for_status()
        data = resp.json() or {}
    except (requests.RequestException, ValueError) as exc:
        from dola_pool.account_transport import redact_error
        return {"ok": False, "ip": "", "error": "没查到本机公网 IP：" + redact_error(exc)[:160]}
    ip = str(data.get("query") or "").strip()
    try:
        ipaddress.ip_address(ip)
    except ValueError:
        ip = ""
    if data.get("status") != "success" or not ip:
        return {"ok": False, "ip": "", "error": "没查到本机公网 IP"}
    return {"ok": True, "ip": ip}


def _public_failure(exc):
    """异常原文可能夹带代理密码。脱敏后再给操作者看，别只甩「去装浏览器内核」。"""
    from dola_pool.account_transport import redact_error
    safe = " ".join(redact_error(exc).split())[:200]
    return f"✗ 登录采集失败（{type(exc).__name__}）：{safe}"


def _open_proxy_scheme(host, port, timeout=3):
    """提取接口只给 host:port，协议得连上去认。

    1024 白名单节点实测回 SOCKS5 无鉴权问候（\\x05\\x00）。按 HTTP 代理去连，对端直接断开，
    requests 只报 RemoteDisconnected。问候不是 SOCKS5 就当 HTTP。
    """
    sock = socket.socket()
    sock.settimeout(timeout)
    try:
        sock.connect((host, int(port)))
        sock.sendall(b"\x05\x01\x00")
        reply = sock.recv(2)
    except OSError:
        return "http"
    finally:
        sock.close()
    if reply == b"\x05\x00":
        return "socks5"
    if reply[:1] == b"\x05":
        raise ToolError("提取到的节点要求 SOCKS5 账号密码，API 链接没有提供，没法用")
    return "http"


def verbatim_proxy(user, password, gateway):
    """1024 账密按控制台 curl 原样用：-x 是网关，-U 是 用户名:密码。不再拼接地区和会话。"""
    user, password, gateway = (user or "").strip(), password or "", (gateway or "").strip()
    if not (user and password and gateway):
        raise ToolError("1024 账密要填网关、用户名和密码（控制台 curl 的 -x 和 -U，原样填写）")
    if "://" in gateway or "/" in gateway:
        raise ToolError("网关只填 host:port，例如 us.1024proxy.io:3000")
    return f"http://{quote(user, safe='')}:{quote(password, safe='')}@{gateway}"


def fetch_1024_api(url):
    """调 1024 白名单提取链接，拿第一个 host:port。链接原样请求，不改地区和时长。"""
    raw = (url or "").strip()
    parts = urlsplit(raw)
    if parts.scheme not in ("http", "https") or "1024proxy" not in (parts.hostname or "").lower():
        raise ToolError("请粘贴 1024 控制台生成的 API 提取链接"
                        "（https://white.1024proxy.com/white/api?region=JP&num=1&time=10&type=txt）")
    try:
        resp = requests.get(raw, timeout=HTTP_TIMEOUT)
        resp.raise_for_status()
        text = resp.text
    except requests.RequestException as exc:
        from dola_pool.account_transport import redact_error
        raise ToolError("API 提取失败：" + redact_error(exc)[:200]) from None
    nodes = proxy_store.parse_extract_lines(text)
    if not nodes:
        detail = " ".join(text.split())[:120] or "返回内容为空"
        raise ToolError("API 没有返回可用地址：" + detail
                        + "。请确认这台电脑的公网 IP 已加入 1024 白名单，且链接里 region 是 JP 或 KR")
    node = nodes[0]
    host = node["host"]
    scheme = _open_proxy_scheme(host, node["port"])
    shown = f"[{host}]" if ":" in host else host
    auth = ""
    if node.get("username"):
        auth = quote(node["username"], safe="") + ":" + quote(node.get("password") or "", safe="") + "@"
    return f"{scheme}://{auth}{shown}:{node['port']}"


def split_sms_source(text):
    """输入框里的一行：`+18647089355----https://…/smsrecord?token=…`，只贴链接也行。返回 (号码, 链接)。"""
    found = re.match(r"\s*(.*?)\s*-{4,}\s*(\S+)\s*$", text or "")
    if not found:
        return "", (text or "").strip()
    return found.group(1).strip(), found.group(2)


def public_sms_url(raw):
    """收码链接只允许外部 http(s)。拒绝本机 / 内网，避免这个接口变成访问内网的跳板。"""
    text = (raw or "").strip()
    if not text:
        raise ToolError("请先粘贴号码和收码链接")
    if len(text) > 2000:
        raise ToolError("收码链接太长")
    parts = urlsplit(text)
    host = (parts.hostname or "").lower().rstrip(".")
    if parts.scheme not in ("http", "https") or not host or parts.username or parts.password:
        raise ToolError("收码链接要以 http:// 或 https:// 开头，且不能带账号密码")
    if host in {"localhost", "localhost.localdomain"} or host.endswith((".local", ".internal")):
        raise ToolError("收码链接必须是外部网址，不能指向本机或内网")
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        ip = None
    if ip is not None and (ip.is_private or ip.is_loopback or ip.is_link_local
                           or ip.is_reserved or ip.is_multicast or ip.is_unspecified):
        raise ToolError("收码链接必须是外部网址，不能指向本机或内网")
    return text


def _pick_sms_code(body):
    """从短信正文里取验证码。到期日、时分秒里的数字不算。"""
    found = _SMS_CODE_NEAR.search(body)
    if found:
        return found.group(1)
    nums = _SMS_STANDALONE.findall(_SMS_DATE.sub(" ", body))
    six = [item for item in nums if len(item) == 6]
    return (six or nums)[-1] if nums else ""


def _parse_sms_line(line):
    plain = " ".join(re.sub(r"<[^>]*>", " ", line).split())
    status, body = "", plain
    if "|" in plain:
        status, body = plain.split("|", 1)
        status, body = status.strip().lower(), body.strip()
    code = ""
    if status not in _SMS_EMPTY:
        head, sep, tail = body.partition("|")
        if sep and re.fullmatch(r"\d{4,8}", head.strip()):
            code, body = head.strip(), (tail.strip() or body)
        else:
            code = _pick_sms_code(body)
    return {"code": code, "text": body, "status": status}


def parse_sms_record(text):
    """解析收码站正文。xsd20vip 的 /smsrecord 是 `no|说明` 或 `yes|短信`，多条时取最后一条有码的。"""
    raw = (text or "").replace("\r\n", "\n").strip()
    if not raw:
        return {"ok": True, "empty": True, "code": "", "text": "收码站没有返回内容", "status": ""}
    parsed = [_parse_sms_line(line) for line in raw.split("\n") if line.strip()]
    chosen = next((item for item in reversed(parsed) if item["code"]), parsed[-1])
    return {"ok": True, "empty": not chosen["code"], "code": chosen["code"],
            "text": chosen["text"], "status": chosen["status"]}


def _sms_failure(exc):
    from dola_pool.account_transport import redact_error
    text = re.sub(r"(token=)[^&\s]+", r"\1***", redact_error(exc), flags=re.I)
    text = re.sub(r"https?://\S+", "收码链接", text)
    return "收码链接请求失败：" + " ".join(text.split())[:160]


def fetch_sms_record(url, timeout=8):
    """直接 HTTP 读取收码链接。不跟随跳转（跳到内网会绕过上面的检查），也不开浏览器。"""
    target = public_sms_url(url)
    try:
        resp = requests.get(target, timeout=timeout, allow_redirects=False,
                            headers={"User-Agent": "Mozilla/5.0"})
    except requests.RequestException as exc:
        raise ToolError(_sms_failure(exc)) from None
    if 300 <= resp.status_code < 400:
        raise ToolError("收码链接发生跳转，请填写最终地址")
    if resp.status_code >= 400:
        raise ToolError(f"收码链接返回 {resp.status_code}")
    return parse_sms_record((resp.text or "")[:8000])


def saved_sms_source():
    """上次读过的那一行（号码----收码链接），存在当前目录的 sms-record.txt。从仓库根目录启动才能找到。"""
    try:
        text = Path(SMS_URL_NAME).read_text(encoding="utf-8")
    except OSError:
        return ""
    lines = text.strip().splitlines()
    source = lines[0].strip() if lines else ""
    try:
        public_sms_url(split_sms_source(source)[1])
    except ToolError:
        return ""
    return source


def sms_api(payload):
    """页面把输入框里的整行发来：只拿链接去读，整行原样记住（下次打开连号码一起填回）。"""
    source = _form_text(payload, "source")
    try:
        target = public_sms_url(split_sms_source(source)[1])
        data = fetch_sms_record(target)
    except ToolError as exc:
        return 400, {"ok": False, "error": str(exc)}
    try:
        Path(SMS_URL_NAME).write_text(source + "\n", encoding="utf-8")
    except OSError:
        pass
    return 200, data


def web_proxy_args(payload):
    """网页表单 → resolve_proxy 认的参数对象。1024 两种都不在这里重拼用户名。"""
    kind = _form_text(payload, "proxy_kind") or "local"
    if kind == "p1024":
        proxy = verbatim_proxy(_form_text(payload, "p1024_user"), str(payload.get("p1024_pass") or ""),
                               _form_text(payload, "p1024_gateway"))
    elif kind == "p1024api":
        proxy = fetch_1024_api(_form_text(payload, "p1024_api"))
    else:
        proxy = _form_text(payload, "proxy_url") if kind == "url" else None
    return SimpleNamespace(
        proxy=proxy, p1024_user=None, p1024_pass=None, p1024_gateway="",
        p1024_region="JP", p1024_session=None, p1024_ttl=120)


def serve(app, *, token, port=0):
    """起网页界面的本地服务，返回 (server, 带 token 的访问地址)。只监听 127.0.0.1。

    token 是防别人的网页借浏览器之手调本机接口（也防局域网）：没有 token 的请求一律 403。
    """
    page = Path(__file__).with_name(PAGE_NAME).read_text(encoding="utf-8")

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *args):      # 默认实现往 stderr 打访问日志（含带 token 的地址），不要
            pass

        def _reply(self, code, body, content_type="application/json; charset=utf-8"):
            data = body.encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(data)

        def _authorized(self):
            in_query = parse_qs(urlsplit(self.path).query).get("token") == [token]
            return in_query or self.headers.get("X-Token") == token

        def do_GET(self):
            if not self._authorized():
                return self._reply(403, '{"ok": false, "error": "token 不正确：请用终端里打印的完整地址打开"}')
            path = urlsplit(self.path).path
            if path == "/api/status":
                return self._reply(200, json.dumps(app.status(), ensure_ascii=False))
            if path == "/api/ip":
                return self._reply(200, json.dumps(public_ip(), ensure_ascii=False))
            if path == "/api/sms-source":
                return self._reply(200, json.dumps({"ok": True, "source": saved_sms_source()}, ensure_ascii=False))
            return self._reply(200, page, "text/html; charset=utf-8")

        def do_POST(self):
            if not self._authorized():
                return self._reply(403, '{"ok": false, "error": "token 不正确"}')
            route = urlsplit(self.path).path
            if route not in ("/api/start", "/api/push", "/api/sms"):
                return self._reply(404, '{"ok": false, "error": "接口不存在"}')
            try:
                length = min(int(self.headers.get("Content-Length") or 0), 65536)
                payload = json.loads(self.rfile.read(length) or b"{}")
            except ValueError:
                return self._reply(400, '{"ok": false, "error": "请求体不是合法 JSON"}')
            if not isinstance(payload, dict):
                return self._reply(400, '{"ok": false, "error": "请求体要是一个 JSON 对象"}')
            if route == "/api/sms":
                code, data = sms_api(payload)
            else:
                code, data = app.start(payload) if route == "/api/start" else app.push(payload)
            self._reply(code, json.dumps(data, ensure_ascii=False))

    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    return server, f"http://127.0.0.1:{server.server_address[1]}/?token={token}"


def run_web(port=0):
    """网页界面：起本地服务并自动打开浏览器页面，Ctrl+C 结束。"""
    token = secrets.token_urlsafe(24)
    try:
        server, url = serve(WebApp(), token=token, port=port)
    except OSError:
        raise ToolError(f"本机端口 {port} 被占用，换一个：--port 其他端口") from None
    print(f"网页界面：{url}\n浏览器没有自动打开时，把上面这行完整地址粘到地址栏；回到这里按 Ctrl+C 结束。",
          flush=True)
    webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n已关闭")
    finally:
        server.server_close()
    return 0


def main(argv=None):
    tunnel = "1024Proxy 账密隧道（登录出口日本/韩国）"
    server = "推送到服务器（可选；不填则只导出文件 + 复制到剪贴板）"
    parser = argparse.ArgumentParser(
        prog="python tools/login.py", description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("account", nargs="?", help="账号名（与服务器上的号同名）；不给则要配 --web")
    parser.add_argument("--web", action="store_true", help="打开网页界面操作（推荐），忽略下面的命令行选项")
    parser.add_argument("--port", type=int, default=0, help="--web 监听的本机端口，默认随机")
    parser.add_argument("--proxy", help="代理地址，如 http://user:pass@host:port 或 host:port")
    p1024 = parser.add_argument_group(tunnel)
    p1024.add_argument("--p1024-user", help="1024Proxy 账号；不给则不走 1024Proxy")
    p1024.add_argument("--p1024-pass", help="1024Proxy 密码（缺省读环境变量 DOLA_P1024_PASS，再缺省交互输入）")
    p1024.add_argument("--p1024-gateway", default="", help="网关 host:port，在 1024 控制台「账密认证 → 去使用」查看")
    p1024.add_argument("--p1024-region", default="JP", help="出口地区，默认 JP")
    p1024.add_argument("--p1024-session", default=None, help="会话 ID，缺省随机（同一 ID 出口 IP 不变）")
    p1024.add_argument("--p1024-ttl", type=int, default=120, help="粘性时长 1-120 分钟，默认 120")
    parser.add_argument("--fresh", action="store_true", help="清掉本地旧 profile，从零登录（会话过期续期时用）")
    parser.add_argument("--out", help="导出文件路径，默认 accounts/<账号名>/%s" % EXPORT_NAME)
    parser.add_argument("--no-clipboard", action="store_true", help="不往剪贴板复制")
    push = parser.add_argument_group(server)
    push.add_argument("--server", help="服务器面板地址，如 https://dola.example.com")
    push.add_argument("--server-user", help="服务器面板的管理员用户名")
    push.add_argument("--server-pass", help="管理员密码（缺省读环境变量 DOLA_ADMIN_PASSWORD，再缺省交互输入）")
    push.add_argument("--proxy-id", help="导入后在服务器上给这个号绑定的代理 ID")
    push.add_argument("--skip-verify", action="store_true", help="导入后不做在线验证")
    args = parser.parse_args(argv)

    try:
        if args.web:
            return run_web(args.port)
        if not args.account:
            raise ToolError("要给账号名（或用 --web 打开网页界面）")
        account = check_account_name(args.account)
        if args.server and not args.server_user:
            raise ToolError("指定 --server 时还要给 --server-user")
        proxy_kwargs, proxy_url = resolve_proxy(args, account)
        bundle, _, _ = asyncio.run(login_and_export(
            account, proxy_kwargs, proxy_url, fresh=args.fresh, out=args.out, clipboard=not args.no_clipboard))
        if args.server:
            base = normalize_server(args.server)
            token = server_login(base, args.server_user, admin_password(args.server_pass, args.server_user))
            print("✓ " + push_to_server(base, token, bundle,
                                        skip_verify=args.skip_verify, proxy_id=args.proxy_id))
        print("提示：直接关掉浏览器窗口即可，不要在 dola 页面点「退出登录」（会让这份 Cookie 作废）")
        return 0
    except ToolError as exc:
        print(f"✗ {exc}")
        return 1
    except KeyboardInterrupt:
        print("\n已取消")
        return 130
    except Exception as exc:
        # 浏览器异常原文可能夹带代理密码，只打类型名
        print(_public_failure(exc))
        return 1


if __name__ == "__main__":
    sys.exit(main())
