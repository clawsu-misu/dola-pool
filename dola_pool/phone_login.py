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
# 手机号登录入口（2026-11-26 实测 dola 弹窗 testid：login_third_line/apple/facebook/phone_verify_code）
ENTRY_SELECTORS = ('[data-testid="login_third_phone_verify_code"]', '[data-testid*="phone" i]',
                   '[aria-label*="电话"]', '[aria-label*="phone" i]')
LOGIN_BUTTON_SELECTORS = ('[data-testid="to_login_button"]',)
PHONE_INPUT_SELECTORS = ('[data-testid="login_phone_number_input"]', 'input[type="tel"]')
NEXT_SELECTORS = ('[data-testid="login_next_button"]',)
# 区号选择是 Semi Design 自绘下拉（无原生 <select>），行结构 .country-code-select-item > [class*=left]（区号文本）
COUNTRY_SELECT_SELECTOR = '[class*=country-code-select]'
COUNTRY_ITEM_SELECTOR = '[class*=country-code-select-item]'
# 发送验证码 / 提交按钮文案兜底（首选 login_next_button testid）
SEND_RE = re.compile(r"^(?:发送验证码|获取验证码|发送短信|下一步|继续|続ける|続行|Continue|Next|Send code|Get code)$", re.I)
CODE_INPUT_SELECTOR = ('input[autocomplete="one-time-code"], input[id^="char"], '
                       'input[id*="security-code"], input[name="securityCode"], '
                       '[data-testid*="verify_code"] input, [data-testid*="code_input"] input')
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


async def first_visible(scope, selectors):
    for selector in selectors:
        item = await visible(scope, selector)
        if item is not None:
            return item
    return None


async def dump_form(page, note: str) -> None:
    """失败时留痕：截图 + 把可见输入框/按钮的 testid 打进服务端日志，方便修选择器。"""
    try:
        await page.screenshot(path="dbg_phone_login.png")
        summary = await page.evaluate("""() => JSON.stringify({
            testids: [...document.querySelectorAll('[data-testid]')]
                .map(e => e.getAttribute('data-testid'))
                .filter(t => t && /login|phone|code|country|verify/i.test(t)),
            inputs: [...document.querySelectorAll('input')].filter(e => e.offsetWidth || e.offsetHeight)
                .map(e => e.getAttribute('data-testid') || e.type),
            buttons: [...document.querySelectorAll('button')].filter(e => e.offsetWidth || e.offsetHeight)
                .map(e => (e.textContent || '').trim().slice(0, 20)).filter(Boolean).slice(0, 10),
        })""")
        print(f"[phone-login] {note} 页面元素={summary}", flush=True)
    except Exception:
        pass


async def _click_button(form, pattern):
    buttons = form.get_by_role("button", name=pattern)
    for index in range(await buttons.count()):
        button = buttons.nth(index)
        if await button.is_visible() and await button.is_enabled():
            await button.click(timeout=5000)
            return True
    return False


async def _select_country(form, country: str) -> bool:
    """区号切到目标（默认 +1）。Semi 自绘下拉的选项行 .country-code-select-item
    左格就是区号文本；国家名随界面语言变化，区号（+1）不随，按它匹配。"""
    try:
        rows = form.locator(COUNTRY_ITEM_SELECTOR)
        if not await rows.count():
            return False
        clicked = await form.evaluate("""(country) => {
            const rows = document.querySelectorAll('[class*=country-code-select-item]');
            for (const row of rows) {
                const left = row.querySelector('[class*=left]');
                if (left && (left.textContent || '').trim() === country) { row.click(); return true; }
            }
            return false;
        }""", country)
        if clicked:
            return True
        select = await visible(form, COUNTRY_SELECT_SELECTOR)
        if select is None:
            return False
        await select.click(timeout=5000)
        rows = form.locator(COUNTRY_ITEM_SELECTOR)
        for index in range(await rows.count()):
            row = rows.nth(index)
            if not await row.is_visible():
                continue
            left = row.locator("[class*=left]")
            if await left.count() and (await left.first.inner_text()).strip() == country:
                await row.click(timeout=5000)
                return True
        return False
    except Exception:
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
    # 短信码同样走 login_next_button 提交；文案按钮兜底
    for selector in NEXT_SELECTORS:
        try:
            button = await visible(form, selector)
            if button is not None and await button.is_enabled():
                await button.click(timeout=5000)
                return
        except Exception:
            continue
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
                tel = await first_visible(frame, PHONE_INPUT_SELECTORS)
                if tel is not None:
                    tel_frame = frame
        if code_box is not None:
            value = await control.ask("code", "等待短信验证码，请输入手机收到的六位数字",
                                      max(1, deadline - time.monotonic()))
            await _fill_code(code_frame, value)
            await asyncio.sleep(3)
            continue
        if tel is not None:
            control.update(stage="正在选择区号并填写号码")
            await _select_country(tel_frame, country)
            await tel.fill(phone, timeout=5000)
            control.update(stage="正在发送短信验证码")
            if not await _click_next(tel_frame):
                raise PhoneLoginError("发送验证码按钮不可用，请稍后重试")
            submitted = True
            control.update(stage="验证码已发送，等待手机收短信")
            await asyncio.sleep(3)
            continue
        await asyncio.sleep(1)
    raise PhoneLoginError("10 分钟内未完成手机号登录，请重试；需要验证码时请在面板输入")


async def _click_next(form):
    for selector in NEXT_SELECTORS:
        try:
            button = await visible(form, selector)
            if button is not None and await button.is_enabled():
                await button.click(timeout=5000)
                return True
        except Exception:
            continue
    return await _click_button(form, SEND_RE)


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
                # 登出状态弹窗通常会自动弹出；没弹就点登录入口（testid 优先，文案兜底）
                entry = await first_visible(page, ENTRY_SELECTORS)
                if entry is None:
                    for _ in range(2):
                        login_button = await first_visible(page, LOGIN_BUTTON_SELECTORS)
                        if login_button is None:
                            try:
                                await page.get_by_role("button", name=LOGIN_RE).first.click(timeout=20000)
                            except Exception:
                                pass
                        else:
                            await login_button.click(timeout=5000)
                        await page.wait_for_timeout(2000)
                        entry = await first_visible(page, ENTRY_SELECTORS)
                        if entry is not None:
                            break
                if entry is None:
                    await dump_form(page, "未找到手机号登录入口")
                    raise PhoneLoginError("未找到手机号登录入口（截图 dbg_phone_login.png，服务端日志有页面元素）")
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
    except Exception as exc:
        # 不透传浏览器异常（可能夹带代理密码），但把异常类型名打进日志方便修选择器
        print(f"[phone-login] 登录流程异常: {type(exc).__name__}", flush=True)
        raise PhoneLoginError("手机号登录页面或代理连接失败，请检查线路后重试") from None
    finally:
        control.clear()
        if staging.exists():
            shutil.rmtree(staging)
