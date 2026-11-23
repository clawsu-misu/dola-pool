"""让测试的 sys.modules stub 在包布局下继续生效。

平铺时代 `from browser import x` 每次都先查 sys.modules，stub 一放就灵。
包化后 `from dola_pool import browser` 走的是包对象的属性（真模块一旦在会话早段
被缓存，后续 stub 会被无视），于是「先跑的测试缓存真模块 → 后跑的测试 stub 失灵」。
autouse 地在每个测试期间摘掉包属性，强制这类 import 回落到 sys.modules 查找，
即恢复平铺时代的语义；测试结束后原样放回。
"""
from __future__ import annotations

import pytest

import dola_pool

# 测试里会用 sys.modules 顶替的包内模块（与各 test_*.py 的 stub 名保持一致）
STUBBABLE = (
    "browser", "browser_pool", "browser_feedback", "browser_hello_probe",
    "doubao_gen", "pure_api_gen", "video_link", "video_worker",
    "video_worker_agent", "video_worker_ui",
)


@pytest.fixture(autouse=True)
def _stub_friendly_imports():
    saved = {}
    for name in STUBBABLE:
        saved[name] = dola_pool.__dict__.get(name)
        dola_pool.__dict__.pop(name, None)
    yield
    for name, mod in saved.items():
        if mod is not None:
            setattr(dola_pool, name, mod)
