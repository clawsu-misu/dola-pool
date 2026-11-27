"""dola 账号登录态采集工具（本机运行，登录过程全人工）。

用法：python tools/login.py <账号名> [选项]

打开本机浏览器访问 dola.com，你手动完成登录——Google / Apple / 手机号 / 任意方式都行，
滑块、cookie 同意卡、区号下拉都直接用手点，工具不碰页面元素。检测到登录态（sessionid）后：

1. 保存 profile 到 accounts/<账号名>/（会话仍有效时，下次重跑免登录；--fresh 从零登录）；
2. 导出 Cookie 到 accounts/<账号名>/cookies-export.json（dola-pool-cookies-v1，与服务器
   面板「Cookie 导入」互通），并复制到剪贴板——到服务器面板粘贴导入即可；
3. 指定 --server 时直接推送到服务器，等导入和在线验证结束并回报结果。

号在服务器上走纯 API 出片，不需要浏览器，官方精简镜像也能用。

代理（登录出口必须是日本/韩国）：
  --proxy URL      任意代理，如 http://user:pass@host:port
  --p1024-*        1024Proxy 账密隧道，按官方用户名格式自动拼：
                   <账号>-region-JP-sid-<会话>-t-120
  都不给           沿用本机代理库里该账号绑定的代理

Cookie 就是账号的登录凭证：导出文件不要外传；工具不会把 Cookie 值、代理密码、
管理员密码、会话令牌打印到终端。
"""
import argparse
import asyncio
import getpass
import json
import os
import re
import secrets
import shutil
import subprocess
import sys
import time
from pathlib import Path
from urllib.parse import quote, urlsplit

import requests
from patchright.async_api import async_playwright

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from dola_pool import proxy_store
from dola_pool.browser import (LAUNCH_ARGS, _dola_cookie, _kwargs_from_url, _save_cloak_cookies,
                               proxy_kwargs_for)

EXPORT_NAME = "cookies-export.json"   # 放在账号目录里：已 gitignore，也不会被对外静态服务
LOGIN_SECONDS = 900                   # 人工登录最长等 15 分钟
POLL_SECONDS = 5
HTTP_TIMEOUT = 20
PUSH_TIMEOUT = 240                    # 服务器导入 + 在线验证最长等 4 分钟
SUPPORTED_REGIONS = ("JP", "KR")      # dola 只接受日本 / 韩国出口
ACCOUNT_NAME_RE = re.compile(r"[A-Za-z0-9_-]+")
COOKIE_FIELDS = ("name", "value", "domain", "path", "expires", "secure", "httpOnly", "sameSite")


class ToolError(RuntimeError):
    """可以直接展示给操作者的错误：不含 Cookie 值、代理密码、会话令牌。"""


def build_1024_proxy(user, password, gateway, *, region="JP", session=None, ttl=120, protocol="http"):
    """1024Proxy 账密隧道：按官方用户名格式拼代理 URL。

    <账号>-region-<地区>-sid-<会话>-t-<粘性分钟>:<密码>@<网关 host:port>
    默认值与面板「添加代理 → 1024Proxy 预设」一致（出口日本、粘性 120 分钟）。
    """
    user, password, gateway = (user or "").strip(), password or "", (gateway or "").strip()
    if not (user and password and gateway):
        raise ToolError("1024Proxy 需要账号、密码和网关地址（host:port，在 1024 控制台「账密认证 → 去使用」查看）")
    if not re.fullmatch(r"[A-Za-z]{2,6}", region or ""):
        raise ToolError("1024Proxy 地区用字母代码，例如 JP、KR")
    if not isinstance(ttl, int) or not 1 <= ttl <= 120:
        raise ToolError("1024Proxy 粘性时长取 1-120 分钟")
    session = session or secrets.token_hex(4)
    if not re.fullmatch(r"[A-Za-z0-9]{1,32}", session):
        raise ToolError("1024Proxy 会话 ID 只能用 1-32 位字母数字")
    template = f"{{user}}-region-{region}-sid-{{session}}-t-{ttl}"
    login = proxy_store.render_session_username(template, user, session)
    return f"{protocol}://{quote(login, safe='')}:{quote(password, safe='')}@{gateway}"


def resolve_proxy(args, account):
    """定下浏览器要用的代理：--proxy > --p1024-* > 本机代理库里该账号绑定的代理。

    返回 (Playwright 代理参数 或 None, 探测出口用的 URL 或 None)。
    """
    if args.proxy and args.p1024_user:
        raise ToolError("--proxy 和 --p1024-* 只能给一个")
    if args.p1024_user:
        password = args.p1024_pass or os.getenv("DOLA_P1024_PASS") or getpass.getpass("1024Proxy 密码: ")
        url = build_1024_proxy(args.p1024_user, password, args.p1024_gateway,
                               region=args.p1024_region, session=args.p1024_session, ttl=args.p1024_ttl)
    elif args.proxy:
        url = args.proxy
    else:
        return proxy_kwargs_for(account), proxy_store.account_proxy_url(account)
    kwargs = _kwargs_from_url(url)
    if not kwargs:
        raise ToolError("代理地址无法解析（应为 host:port 或 协议://用户:密码@host:port）")
    return kwargs, url


def describe_exit(proxy_url):
    """登录前查一次出口归属地：dola 只认日本 / 韩国出口，先确认，免得白登一场。"""
    if not proxy_url or proxy_url == "direct":
        print("代理出口：未配置代理，用本机网络直连（请自行确认出口在日本/韩国）")
        return
    info = proxy_store.probe_exit_ip(proxy_url)
    if not info.get("ok"):
        print(f"代理出口探测失败（不影响登录，请确认代理可用）：{info.get('error', '')}")
        return
    where = " / ".join(x for x in (info.get("country"), info.get("region"), info.get("isp")) if x)
    print(f"代理出口：{info.get('ip')}  {where}")
    if info.get("country_code") not in SUPPORTED_REGIONS:
        print(f"⚠ 出口不是日本/韩国（{info.get('country_code')}），dola 可能拒绝访问")


async def capture_login(account, proxy_kwargs):
    """开有头浏览器让人工登录，抓到 sessionid 后返回 dola 域 cookie 列表。"""
    profile_dir = Path("accounts") / account
    profile_dir.mkdir(parents=True, exist_ok=True)
    kwargs = {"headless": False, "args": LAUNCH_ARGS, "locale": "ja-JP", "timezone_id": "Asia/Tokyo"}
    if proxy_kwargs:
        kwargs["proxy"] = proxy_kwargs
    async with async_playwright() as playwright:
        context = await playwright.chromium.launch_persistent_context(str(profile_dir), **kwargs)
        try:
            page = context.pages[0] if context.pages else await context.new_page()
            await page.goto("https://www.dola.com/chat", timeout=60000)
            print(f"[{account}] 浏览器已打开，请在窗口里手动完成登录（任意方式，登进 dola 即可）...", flush=True)
            deadline = time.monotonic() + LOGIN_SECONDS
            while time.monotonic() < deadline:
                cookies = await context.cookies("https://www.dola.com")
                if any(c.get("name") == "sessionid" and c.get("value") for c in cookies):
                    print(f"[{account}] 检测到登录态，等会话 cookie 写完...", flush=True)
                    await page.wait_for_timeout(4000)
                    return cookies
                await page.wait_for_timeout(POLL_SECONDS * 1000)
            raise ToolError(f"{LOGIN_SECONDS // 60} 分钟内未检测到登录，请重跑本工具")
        finally:
            await context.close()


def cookie_bundle(account, cookies):
    """按 dola-pool-cookies-v1 打包 dola 域 cookie（字段白名单，与服务器导出格式互通）。"""
    items = [{k: c[k] for k in COOKIE_FIELDS if k in c}
             for c in cookies if isinstance(c, dict) and _dola_cookie(c)]
    if not any(c["name"] == "sessionid" and c.get("value") for c in items):
        raise ToolError("抓到的 Cookie 里没有 sessionid，登录没有完成")
    return {"format": "dola-pool-cookies-v1", "account": account, "source": "local-login", "cookies": items}


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


def normalize_server(raw):
    """补全协议（默认 https），记账本地址给出提醒。"""
    base = (raw or "").strip().rstrip("/")
    if "://" not in base:
        base = "https://" + base
    parts = urlsplit(base)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise ToolError("服务器地址不正确，例如 https://dola.example.com")
    if parts.scheme == "http" and parts.hostname not in ("localhost", "127.0.0.1", "::1"):
        print("⚠ 服务器是 http：管理员密码与 Cookie 会明文经过网络，建议改用 https")
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
    """推送导出的 Cookie 到服务器，等导入（含在线验证）结束，返回结果说明。"""
    name = bundle["account"]
    _call(base, token, "POST", "/api/admin/accounts", json={
        "name": name,
        "cookies": json.dumps(bundle["cookies"], ensure_ascii=False),
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
        return "已导入（按 --skip-verify 跳过了在线验证）"
    accounts = _call(base, token, "GET", "/api/admin/accounts").get("accounts") or []
    account = next((a for a in accounts if a.get("name") == name), {})
    if account.get("login_ok"):
        return "已导入，服务器在线验证通过"
    return "已导入，但服务器在线验证未通过：到面板给该号绑定日本/韩国代理后点「验证」"


def main(argv=None):
    tunnel = "1024Proxy 账密隧道（登录出口日本/韩国）"
    server = "推送到服务器（可选；不填则只导出文件 + 复制到剪贴板）"
    parser = argparse.ArgumentParser(
        prog="python tools/login.py", description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("account", help="账号名（与服务器上的号同名）")
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
        account = args.account.strip()
        if not ACCOUNT_NAME_RE.fullmatch(account):
            raise ToolError("账号名只能用字母、数字、下划线和短横线")
        if args.server and not args.server_user:
            raise ToolError("指定 --server 时还要给 --server-user")
        proxy_kwargs, proxy_url = resolve_proxy(args, account)
        if proxy_kwargs:
            who = f"（账号 {proxy_kwargs['username']}）" if proxy_kwargs.get("username") else ""
            print(f"浏览器代理：{proxy_kwargs['server']}{who}")
        else:
            print("浏览器代理：未配置，直连")
        describe_exit(proxy_url)
        profile_dir = Path("accounts") / account
        if args.fresh and profile_dir.exists():
            shutil.rmtree(profile_dir)
            print("已清掉本地旧 profile，从零登录")
        cookies = asyncio.run(capture_login(account, proxy_kwargs))
        bundle = cookie_bundle(account, cookies)
        target = export_bundle(bundle, Path(args.out) if args.out else profile_dir / EXPORT_NAME)
        print(f"✓ 已导出 {len(bundle['cookies'])} 条 Cookie → {target}")
        if not args.no_clipboard:
            if copy_to_clipboard(json.dumps(bundle, ensure_ascii=False)):
                print("✓ 已复制到剪贴板：到服务器面板「Cookie 导入」直接粘贴")
            else:
                print(f"  没有可用的剪贴板命令，请打开 {target} 手动复制全部内容")
        if args.server:
            base = normalize_server(args.server)
            password = (args.server_pass or os.getenv("DOLA_ADMIN_PASSWORD")
                        or getpass.getpass(f"服务器管理员 {args.server_user} 的密码: "))
            token = server_login(base, args.server_user, password)
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
        print(f"✗ 登录采集失败（{type(exc).__name__}）：确认代理可用，"
              "且已安装浏览器内核（python -m patchright install chromium）")
        return 1


if __name__ == "__main__":
    sys.exit(main())
