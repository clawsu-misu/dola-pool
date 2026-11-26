"""手机号短信验证码登录：复用 Apple 的挑战交互与 Cloak 快照机制。

面板提交账号名 + 区号 + 号码（默认 +1）；流程打开 Dola 登录弹窗点电话图标，
填号码发短信，验证码通过 challenge 机制由人工在面板输入。
失败、取消都关闭并清理本次临时资料目录；成功才改名交给号池。
选择器按现行登录弹窗编写，上游改版后第一次真实运行会以截图报错指路。
"""
import asyncio
import re
import shutil
import tempfile
import time
from pathlib import Path

from dola_pool import proxy_store
from dola_pool.account_transport import http_proxy
from dola_pool.browser import LAUNCH_ARGS, _kwargs_from_url, _save_cloak_cookies, _dola_cookie
from dola_pool.apple_login import LOGIN_RE

PHONE_RE = re.compile(r"^[0-9]{5,15}$")
# 手机号登录入口：现行弹窗用 login_third_* 系列 testid（Apple 为 login_third_apple）
ENTRY_SELECTORS = ('[data-testid="login_third_phone"]', '[data-testid*="phone" i]',
                   '[aria-label*="电话"]', '[aria-label*="phone" i]')
# 发送验证码 / 提交按钮文案
SEND_RE = re.compile(r"^(?:发送验证码|获取验证码|发送短信|继续|続ける|続行|Continue|Send code|Get code|Next)$", re.I)
CODE_INPUT_SELECTOR = ('input[autocomplete="one-time-code"], input[id^="char"], '
                       'input[id*="security-code"], input[name="securityCode"]')
SESSION_FILE = ".apple-session-cookies.json"  # 与 Apple 登录共用快照，browser.py 按文件名识别 Cloak
LOGIN_SECONDS = 600


class PhoneLoginError(RuntimeError):
    """可以展示的固定错误说明，不带浏览器异常和登录 URL。"""


def phone_frames(context):
    """本次登录专用的浏览器里所有可用 frame（dola 及其跳转域）。"""
    frames = []
    for page in context.pages:
        if page.is_closed():
            continue
        frames.extend(page.frames)
    return frames


async def visible(scope, selector):
    items = scope.locator(selector)
    for index in range(await items.count()):
        item = items.nth(index)
        if await item.is_visible():
            return item
    return None


async def _click_button(form, pattern):
    buttons = form.get_by_role("button", name=pattern)
    for index in range(await buttons.count()):
        button = buttons.nth(index)
        if await button.is_visible() and await button.is_enabled():
            await button.click(timeout=5000)
            return True
    return False


async def _select_country(form, country: str) -> bool:
    """区号选择器若存在，选到目标区号（默认 +1）；找不到按页面默认值发送。"""
    selects = form.locator("select")
    for index in range(await selects.count()):
        select = selects.nth(index)
        options = select.locator("option")
        for pos in range(await options.count()):
            text = (await options.nth(pos).inner_text()).strip()
            if country in text:
                await select.select_option(index=pos)
                return True
    return False


async def _fill_code(form, code: str):
    inputs = form.locator(CODE_INPUT_SELECTOR)
    boxes = [inputs.nth(i) for i in range(await inputs.count())
             if await inputs.nth(i).is_visible()]
    if len(boxes) == 1:
        await boxes[0].fill(code, timeout=4000)
    elif len(boxes) == 6:
        for box, digit in zip(boxes, code):
            await box.fill(digit, timeout=4000)
    else:
        # 兜底：分位验证码用 tel/数字键盘输入框逐格填
        tels = form.locator('input[type="tel"], input[inputmode="numeric"]')
        boxes = [tels.nth(i) for i in range(await tels.count())
                 if await tels.nth(i).is_visible()]
        if len(boxes) != 6:
            raise PhoneLoginError("验证码页面已变化，请重新登录")
        for box, digit in zip(boxes, code):
            await box.fill(digit, timeout=4000)
    try:
        await _click_button(form, SEND_RE)
    except Exception:
        # 六格验证码通常自动提交，按钮可能随页面一起消失。
        pass


async def complete_phone_login(context, phone: str, control, country: str = "+1"):
    deadline = time.monotonic() + LOGIN_SECONDS
    submitted = False
    while time.monotonic() < deadline:
        cookies = await context.cookies("https://www.dola.com")
        if any(c.get("name") == "sessionid" and c.get("value") for c in cookies):
            return
        code_box = code_frame = tel = tel_frame = None
        for frame in phone_frames(context):
            if code_box is None:
                hit = await visible(frame, CODE_INPUT_SELECTOR)
                if hit is not None:
                    code_box, code_frame = hit, frame
                    continue
            if tel is None and not submitted:
                tel = await visible(frame, 'input[type="tel"]')
                if tel is not None:
                    tel_frame = frame
        if code_box is not None:
            value = await control.ask("code", "等待短信验证码，请输入手机收到的六位数字",
                                      max(1, deadline - time.monotonic()))
            await _fill_code(code_frame, value)
            await asyncio.sleep(3)
            continue
        if tel is not None:
            await _select_country(tel_frame, country)
            await tel.fill(phone, timeout=5000)
            control.update(stage="正在发送短信验证码")
            if not await _click_button(tel_frame, SEND_RE):
                raise PhoneLoginError("发送验证码按钮不可用，请稍后重试")
            submitted = True
            control.update(stage="验证码已发送，等待手机收短信")
            await asyncio.sleep(3)
            continue
        await asyncio.sleep(1)
    raise PhoneLoginError("10 分钟内未完成手机号登录，请重试；需要验证码时请在面板输入")


async def phone_login_flow(account: str, phone: str, control, country: str = "+1"):
    """成功后才把隐藏的临时资料目录交给号池；失败、取消都关闭并清理本次目录。"""
    from patchright.async_api import async_playwright

    if not PHONE_RE.match(phone):
        raise PhoneLoginError("手机号码格式不正确（仅数字，5-15 位）")
    if not proxy_store.get_account_proxy(account):
        raise PhoneLoginError("手机号登录需要先选择一个已启用的账号代理")
    proxy = http_proxy(account)
    if not proxy:
        raise PhoneLoginError("账号代理不可用，手机号登录已停止")
    root = Path("accounts")
    root.mkdir(exist_ok=True)
    target = root / account
    if target.exists():
        raise PhoneLoginError("账号资料目录已存在，请使用新的账号名")
    staging = Path(tempfile.mkdtemp(prefix=f".phone-{account}-", dir=root))
    try:
        async with async_playwright() as playwright:
            context = await playwright.chromium.launch_persistent_context(
                str(staging), headless=False, args=LAUNCH_ARGS,
                locale="ja-JP", timezone_id="Asia/Tokyo", proxy=_kwargs_from_url(proxy))
            try:
                control.update(stage="正在打开 Dola 登录弹窗")
                page = context.pages[0] if context.pages else await context.new_page()
                await page.goto("https://www.dola.com/chat", timeout=60000, wait_until="domcontentloaded")
                entry = page.locator(ENTRY_SELECTORS[0])
                try:
                    await entry.wait_for(state="visible", timeout=15000)
                except Exception:
                    await page.get_by_role("button", name=LOGIN_RE).first.click(timeout=20000)
                    await entry.wait_for(state="visible", timeout=15000)
                await entry.click(timeout=8000)
                control.update(stage="已点开手机号登录，等待输入号码")
                await complete_phone_login(context, phone, control, country)
                _save_cloak_cookies(staging / SESSION_FILE,
                                    [c for c in await context.cookies() if _dola_cookie(c)])
            finally:
                await context.close()
        staging.rename(target)
    except asyncio.CancelledError:
        raise
    except PhoneLoginError:
        raise
    except Exception:
        raise PhoneLoginError("手机号登录页面或代理连接失败，请检查线路后重试") from None
    finally:
        control.clear()
        if staging.exists():
            shutil.rmtree(staging)
