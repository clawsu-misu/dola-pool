"""Cookie 号出片方式：纯 API（默认）或浏览器。面板开关的纯逻辑，便于单测。"""
from dola_pool import config

MODES = ("pure", "browser")


def unavailable_reason() -> str:
    """浏览器模式开不了时说明原因；能开时返回空串。"""
    from dola_pool import cookie_login
    from dola_pool.env_check import _chromium_executable

    ok, reason = cookie_login.browser_backend_available()
    if not ok:
        return reason or "浏览器后端不可用"
    if not _chromium_executable():
        return "patchright 驱动可用，但没找到 Chromium 内核"
    return ""


def settings_view() -> dict:
    reason = unavailable_reason()
    return {"mode": "pure" if config.PURE_API_ENABLED else "browser",
            "browser_available": not reason, "reason": reason}


def apply_saved(saved: str | None) -> dict:
    """启动时恢复面板的选择。

    没存过就沿用环境变量 DOLA_PURE_API，也不探测浏览器（探测要起一次驱动）。
    存过 browser 但这个环境开不了浏览器时退回纯 API。
    """
    if saved == "browser":
        reason = unavailable_reason()
        config.PURE_API_ENABLED = bool(reason)
        return {"mode": "pure" if reason else "browser",
                "browser_available": not reason, "reason": reason}
    if saved == "pure":
        config.PURE_API_ENABLED = True
    mode = "pure" if config.PURE_API_ENABLED else "browser"
    return {"mode": mode, "browser_available": True, "reason": ""}
