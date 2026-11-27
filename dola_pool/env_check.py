"""面板「环境检查」：本机运行环境是否齐备（Python / Node / ffmpeg / 浏览器内核 / 数据目录）。

只查本机 —— 进程、文件、配置、磁盘，不访问外网：面板切到这一页就会调一次，网络探测留在
「代理管理」的出口探测里。结果缓存 CACHE_TTL 秒（浏览器内核探测要起一次 patchright 驱动），
面板「重新检查」传 refresh=True 立即重跑。

每项只有事实：{key, level, value, note}。value / note 是机器数据（版本号、路径、探测报错原文），
面板用 noTr 原样显示；给人看的说明和处理办法写在 web/index.html 的 ENV_TEXT 里（那里参与多语言）。
level 取 ok / warn / error / info，决定面板上的颜色。
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import time
from pathlib import Path
from urllib.parse import urlsplit

from dola_pool import config
from dola_pool import pure_signer
from dola_pool import updater

CACHE_TTL = 60
LEVELS = ("ok", "warn", "error", "info")
_cache: dict = {"at": 0.0, "data": None}


def _item(key: str, level: str, value: str = "", note: str = "") -> dict:
    return {"key": key, "level": level, "value": value, "note": note}


def _first_line(text: str, limit: int = 60) -> str:
    lines = (text or "").strip().splitlines()
    return lines[0][:limit] if lines else ""


def _python() -> dict:
    version = ".".join(str(x) for x in sys.version_info[:3])
    if sys.version_info[:2] >= updater.PY_MIN:
        return _item("python", "ok", version)
    # 3.10 靠 async_compat 兼容层跑得起来；能不能一键换解释器由 updater 判断
    return _item("python", "warn" if updater.runtime_info()["upgradable"] else "info", version)


def _node() -> dict:
    path = pure_signer.resolve_node()
    if not Path(path).is_file():
        note = "DOLA_NODE 指向的文件不存在" if os.environ.get("DOLA_NODE") else "PATH 里没有 node"
        return _item("node", "error", "", note)
    try:
        proc = subprocess.run([path, "--version"], capture_output=True, text=True, timeout=5)
        version = _first_line(proc.stdout or proc.stderr, 20)
    except Exception:  # noqa: BLE001 - 版本读不到不影响“能不能用”的判断
        version = ""
    return _item("node", "ok", version, path)


def _ffmpeg() -> dict:
    path = shutil.which("ffmpeg")
    if not path:
        return _item("ffmpeg", "warn", "", "PATH 里没有 ffmpeg")
    try:
        proc = subprocess.run([path, "-version"], capture_output=True, text=True, timeout=5)
        version = _first_line(proc.stdout, 60)
    except Exception:  # noqa: BLE001
        version = ""
    return _item("ffmpeg", "ok", version, path)


def _chromium_executable() -> str:
    """patchright 的 Chromium 可执行文件路径；没装内核 / 驱动起不来时返回空串。"""
    try:
        from patchright.sync_api import sync_playwright

        with sync_playwright() as p:
            return str(getattr(p.chromium, "executable_path", "") or "")
    except Exception:  # noqa: BLE001 - 探测失败就当没装
        return ""


def _browser() -> dict:
    from dola_pool import cookie_login

    ok, reason = cookie_login.browser_backend_available()
    if not ok:
        return _item("browser", "warn", "", reason)
    binary = _chromium_executable()
    if binary and Path(binary).is_file():
        return _item("browser", "ok", binary)
    return _item("browser", "warn", "", "patchright 驱动可用，但没找到 Chromium 内核")


def _signer() -> dict:
    script = pure_signer.signer_script()
    if script.is_file():
        return _item("signer", "ok", str(script))
    return _item("signer", "error", "", f"签名脚本缺失：{script}")


def _deploy() -> dict:
    agent = updater.agent_info()
    if agent["ready"]:
        return _item("deploy", "ok", agent["mode"])
    return _item("deploy", "warn", agent["mode"], agent["reason"])


def _storage() -> dict:
    dirs = {
        "任务库": Path(config.DB_PATH).resolve().parent,
        "号池库": Path(config.POOL_DB_PATH).resolve().parent,
        "视频目录": Path(config.DOWNLOAD_DIR).resolve(),
        "更新目录": Path(config.UPDATE_DIR).resolve(),
    }
    bad = []
    for name, path in dirs.items():
        try:
            path.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            bad.append(f"{name} {path}：{type(exc).__name__}")
            continue
        if not os.access(path, os.W_OK):
            bad.append(f"{name} {path}：没有写权限")
    if bad:
        return _item("storage", "error", "", "；".join(bad))
    return _item("storage", "ok", "", str(dirs["视频目录"]))


def _disk() -> dict:
    root = Path(config.DOWNLOAD_DIR)
    if not root.is_dir():
        root = Path(".")
    try:
        usage = shutil.disk_usage(str(root))
    except OSError as exc:
        return _item("disk", "warn", "", f"读不到磁盘用量：{type(exc).__name__}")
    free_gb = usage.free / 1024 ** 3
    value = f"{free_gb:.1f} GB"
    if free_gb < 1:
        return _item("disk", "error", value, "磁盘快满了，视频写不进来")
    if free_gb < 5:
        return _item("disk", "warn", value, "剩余空间偏少")
    return _item("disk", "ok", value)


def _accounts(count: int) -> dict:
    if count <= 0:
        return _item("accounts", "warn", "0", "号池里还没有账号")
    return _item("accounts", "ok", str(count))


def _proxy() -> dict:
    url = (config.PROXY or "").strip()
    if not url or url.lower() in ("direct", "none", "off", "no"):
        return _item("proxy", "info", "")
    if "://" not in url:
        url = "http://" + url
    try:
        parts = urlsplit(url)
        shown = f"{parts.scheme}://{parts.hostname}:{parts.port or ''}".rstrip(":")
    except ValueError:
        shown = ""
    return _item("proxy", "info", shown)


def _summary(items: list) -> dict:
    summary = {level: 0 for level in LEVELS}
    for item in items:
        summary[item["level"]] = summary.get(item["level"], 0) + 1
    return summary


def run(*, accounts: int = 0, refresh: bool = False) -> dict:
    """跑一遍检查。accounts 由 server 传入（env_check 不 import server，避免循环依赖）。"""
    now = time.time()
    if not refresh and _cache["data"] and now - float(_cache["at"]) < CACHE_TTL:
        return _cache["data"]
    items = [_python(), _node(), _ffmpeg(), _browser(), _signer(), _deploy(),
             _storage(), _disk(), _accounts(accounts), _proxy()]
    data = {"items": items, "summary": _summary(items), "checked_at": now,
            "deploy_mode": updater.deploy_mode()}
    _cache.update(at=now, data=data)
    return data
