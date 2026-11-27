"""1024Proxy 专用流程：账密隧道 + API 提取，与通用代理设置（静态 / 隧道 / 节点池）解耦。

官方两种接入方式，各是一条子流程：

- 账密隧道（mode=dynamic）：网关固定，出口由用户名里的会话决定，官方格式
  ``<账号>-region-<地区>-sid-<会话ID>-t-<分钟>``，换会话 ID = 换 IP，粘性 1-120 分钟。
  适合要保持登录态的号。
- API 提取（mode=extract）：靠 **IP 白名单**鉴权（调用方的公网 IP 要先加进 1024 控制台），
  调链接拿一批 ``host:port``（没有账号密码）。链接参数 ``region``（控制台默认 Rand 随机地区）、
  ``num``（数量）、``time``（粘性分钟，官方 10 / 15）、``type``（txt / json）、``format``（分隔符）。
  每个号租一个地址，到期自动换新；粘性很短，到期后出口 IP 一定会变。

数据仍落在 proxies / proxy_sessions / proxy_nodes 里 —— 账号绑定、换 IP、探测、同出口隔离
这些通用通道原样复用；专用部分只有两列：``provider='1024proxy'`` 标记这条记录走专用流程，
``settings``（JSON）存专用参数。proxy_store 在取用户名 / 提取 / 换节点几个入口按 provider
分派到这里，通用的 static / dynamic / extract 分支里不出现任何 1024 的细节。
"""
from __future__ import annotations

import ipaddress
import json
import re
import time
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from dola_pool import proxy_store

PROVIDER = "1024proxy"
# dola.com 只接受日本 / 韩国出口，专用流程只开放这两个地区
REGIONS = ("JP", "KR")
# API 提取的最短间隔（秒）。1024 的 API 按流量计费，间隔只为防刷；通用节点池默认 60 秒，
# 套在这里会让「换 IP」长时间拿不到新地址。
API_SPACING = 1

# 提取链接必须落在 1024Proxy 自己的域名上，防止贴错别家服务商的链接
_API_HOST = "1024proxy"
_MAX_MINUTES = 120
_WHITELIST_HINT = "请确认：部署服务器的公网 IP 已加入 1024 控制台白名单、套餐还有剩余流量"
_TUNNEL_DEFAULTS = {"region": "JP", "ttl": 120}
_API_DEFAULTS = {"region": "JP", "num": 1, "time": 10}
# 查本机公网 IP 的顺序：和提取请求一样直连，不走代理（白名单认的是服务器自己的出口）。
# 第一家和号池探测出口用的是同一家；被墙或超时时换第二家。
_PUBLIC_IP_SOURCES = (
    "http://ip-api.com/json/?fields=status,query",
    "https://api.ipify.org",
)
_PUBLIC_IP_TTL = 300
_public_ip_cache = {"ip": "", "at": 0.0}


def is_1024(info: dict | None) -> bool:
    return bool(info) and info.get("provider") == PROVIDER


def kind_of(info: dict) -> str:
    """tunnel = 账密隧道；api = API 提取。"""
    return "tunnel" if info.get("mode") == "dynamic" else "api"


def settings(info: dict, kind: str | None = None) -> dict:
    """记录上的专用参数；缺项 / 坏数据按默认值补齐，不影响取代理。

    kind 缺省按记录的 mode 推断；本地登录工具没有记录，直接传 "tunnel" 和现成的参数。
    """
    base = _TUNNEL_DEFAULTS if (kind or kind_of(info)) == "tunnel" else _API_DEFAULTS
    saved = info.get("provider_settings")
    if saved is None:
        try:
            saved = json.loads(info.get("settings") or "")
        except ValueError:
            saved = None
    saved = saved if isinstance(saved, dict) else {}
    return {**base, **{k: saved[k] for k in base if k in saved}}


# ===== 账密隧道 =====


def tunnel_username(info: dict, session: str) -> str:
    """账密隧道的用户名：<账号>-region-<地区>-sid-<会话ID>-t-<分钟>。"""
    options = settings(info, "tunnel")
    return (f"{(info.get('username') or '').strip()}-region-{options['region']}"
            f"-sid-{session}-t-{options['ttl']}")


# ===== API 提取 =====


def api_url(info: dict) -> str:
    """实际请求的提取链接：在用户链接上改写地区 / 数量 / 粘性时长，固定返回 txt。"""
    options = settings(info, "api")
    parts = urlsplit((info.get("extract_url") or "").strip())
    query = dict(parse_qsl(parts.query, keep_blank_values=True))
    query.update(region=options["region"], num=str(options["num"]),
                 time=str(options["time"]), type="txt")
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query), ""))


def fetch_nodes(info: dict) -> dict:
    """调一次 API 提取一批节点（host:port，白名单鉴权，没有账号密码）。

    节点有效期 = 粘性时长：到期后 1024 会给这个地址换 IP，号池到点也会给号换新地址。
    """
    import requests
    from dola_pool.account_transport import redact_error

    pid = info["id"]
    if not (info.get("extract_url") or "").strip():
        return _extract_failed(pid, "该代理未配置提取链接")
    try:
        resp = requests.get(api_url(info), timeout=proxy_store._extract_timeout())
        resp.raise_for_status()
        text = resp.text
    except Exception as exc:  # noqa: BLE001 - 失败原因要回报给面板
        return _extract_failed(pid, redact_error(exc, limit=120))
    nodes = proxy_store.parse_extract_lines(text)
    if not nodes:
        # 接口出错时通常 200 + 一句提示（IP 未加白名单、余额不足等），原样带出来
        return _extract_failed(pid, " ".join(text.split())[:120] or "返回内容为空")
    for node in nodes:
        node["ttl"] = settings(info, "api")["time"] * 60
    added = proxy_store._insert_nodes(pid, nodes, info.get("protocol") or "http")
    if not added:
        return _extract_failed(
            pid, f"返回的 {len(nodes)} 个地址都已在节点池里，没有拿到新地址"
                 "（可能正被别的号使用；可调大「每次提取数量」）")
    proxy_store._record_extract_result(pid, added, "")
    return {"ok": True, "added": added, "total_lines": len(nodes)}


def server_public_ip(*, refresh: bool = False) -> dict:
    """本机直连出去时对方看到的公网 IP，也就是 1024 API 白名单要填的地址。

    面板上打开 1024Proxy 页签就会来问一次，用来复制粘贴到 1024 控制台。
    成功结果缓存几分钟；失败不缓存，下次打开再试。refresh=True 是面板「重新检测」。
    """
    now = time.time()
    cached = _public_ip_cache["ip"]
    if not refresh and cached and now - float(_public_ip_cache["at"]) < _PUBLIC_IP_TTL:
        return {"ok": True, "ip": cached}
    ip, error = _query_public_ip()
    if not ip:
        return {"ok": False, "ip": "", "error": error}
    _public_ip_cache["ip"] = ip
    _public_ip_cache["at"] = now
    return {"ok": True, "ip": ip}


def _query_public_ip() -> tuple[str, str]:
    import requests
    from dola_pool.account_transport import redact_error

    last = "出网失败"
    for url in _PUBLIC_IP_SOURCES:
        try:
            resp = requests.get(url, timeout=8)
            resp.raise_for_status()
            ip = _ip_from_response(resp)
        except Exception as exc:  # noqa: BLE001 - 一家失败就换下一家
            last = redact_error(exc, limit=120)
            continue
        if ip:
            return ip, ""
        last = "查询服务没有返回 IP"
    return "", last


def _ip_from_response(resp) -> str:
    text = (resp.text or "").strip()
    if text.startswith("{"):
        try:
            data = resp.json()
        except ValueError:
            return ""
        if not isinstance(data, dict) or data.get("status") not in (None, "success"):
            return ""
        text = str(data.get("query") or data.get("ip") or "").strip()
    return text if _is_ip(text) else ""


def _is_ip(text: str) -> bool:
    try:
        ipaddress.ip_address(text)
    except ValueError:
        return False
    return True


def release_node(node_id: int) -> None:
    """账号换到新地址后释放旧地址：直接删掉，保证同一个出口不会再次分给任何号
    （池子小的时候否则「换 IP」会换回上一个 IP）。只删没有任何账号引用的节点。"""
    conn = proxy_store._connect()
    try:
        conn.execute(
            "DELETE FROM proxy_nodes WHERE id=? AND NOT EXISTS "
            "(SELECT 1 FROM proxy_sessions WHERE node_id=?)",
            (node_id, node_id),
        )
        conn.commit()
    finally:
        conn.close()


def _extract_failed(proxy_id: str, detail: str) -> dict:
    error = f"1024Proxy 没有返回可用节点（{detail}）。{_WHITELIST_HINT}"
    proxy_store._record_extract_result(proxy_id, 0, error)
    return {"ok": False, "added": 0, "error": error}


# ===== 新建 / 编辑（面板「1024Proxy」页签） =====


def create(*, kind=None, name: str = "", protocol: str = "http", remark: str = "",
           region: str = "JP", gateway: str = "", username: str = "",
           password: str = "", ttl: int = 120, link: str = "",
           num: int = 1, time: int = 10) -> dict:
    """新建一条 1024Proxy 专用记录：kind=tunnel 账密隧道，kind=api API 提取。"""
    if kind == "tunnel":
        host, port = _gateway(gateway)
        if not (username or "").strip():
            raise ValueError("请填写 1024Proxy 账号（控制台「账密认证 → 账户管理」里创建）")
        if not password:
            raise ValueError("请填写 1024Proxy 密码")
        options = {"region": _region(region), "ttl": _minutes(ttl)}
        return proxy_store.create_proxy(
            name, protocol, host, port, username.strip(), password, remark,
            mode="dynamic", sticky_ttl=options["ttl"] * 60,
            provider=PROVIDER, settings=options)
    if kind == "api":
        options = {"region": _region(region), "num": _count(num), "time": _minutes(time)}
        return proxy_store.create_proxy(
            name, protocol, "", 0, remark=remark, mode="extract",
            extract_url=_api_link(link), extract_interval=API_SPACING,
            provider=PROVIDER, settings=options)
    raise ValueError("接入方式仅支持账密隧道或 API 提取")


def update(proxy_id: str, *, kind=None, name=None, protocol=None, remark=None,
           region=None, gateway=None, username=None, password=None, ttl=None,
           link=None, num=None, time=None) -> dict:
    """改一条 1024Proxy 专用记录：传 None 的字段保持不变，密码留空 = 沿用原密码。"""
    info = proxy_store.get_proxy(proxy_id)
    if not is_1024(info):
        raise ValueError("代理不存在或不是 1024Proxy 专用记录")
    current = kind_of(info)
    if kind is not None and kind != current:
        raise ValueError("接入方式新建后不能改，请删除后重新添加")
    if name is not None and not name.strip():
        raise ValueError("代理名称不能为空")
    options = settings(info)
    if region is not None:
        options["region"] = _region(region)
    fields: dict = {}
    if current == "tunnel":
        if gateway:
            fields["host"], fields["port"] = _gateway(gateway)
        if username is not None:
            if not username.strip():
                raise ValueError("请填写 1024Proxy 账号（控制台「账密认证 → 账户管理」里创建）")
            fields["username"] = username.strip()
        if ttl is not None:
            options["ttl"] = _minutes(ttl)
            fields["sticky_ttl"] = options["ttl"] * 60
    else:
        if link is not None:
            fields["extract_url"] = _api_link(link)
        if num is not None:
            options["num"] = _count(num)
        if time is not None:
            options["time"] = _minutes(time)
    if password:
        fields["password"] = password
    fields.update({k: v for k, v in (("name", name), ("protocol", protocol),
                                     ("remark", remark)) if v is not None})
    fields["settings"] = options
    return proxy_store.update_proxy(proxy_id, **fields)


# ===== 升级：把旧版用「1024Proxy 预设」建的通用记录认领成专用记录 =====


def tag_legacy_records(conn) -> None:
    """按特征找出旧预设建的记录，打上 provider 并从模板 / 链接里还原出专用参数。

    只在 proxies 表第一次加上 provider 列时调用（每个库一次）。认错也不会出问题：
    专用流程拼出来的用户名与原模板完全一致。
    """
    conn.execute(
        "UPDATE proxies SET provider=? WHERE provider='' AND ("
        " (mode='dynamic' AND session_template LIKE ?) OR"
        " (mode='extract' AND instr(lower(coalesce(extract_url,'')), ?)>0))",
        (PROVIDER, "%-region-%-sid-{session}-t-%", _API_HOST),
    )
    rows = conn.execute(
        "SELECT id, mode, session_template, extract_url, extract_interval"
        " FROM proxies WHERE provider=? AND settings=''", (PROVIDER,)).fetchall()
    for row in rows:
        if row["mode"] == "dynamic":
            region = re.search(r"-region-([A-Za-z]{2})-", row["session_template"] or "")
            ttl = re.search(r"-t-(\d+)", row["session_template"] or "")
            options = {"region": region.group(1).upper() if region else "JP",
                       "ttl": _legacy_minutes(ttl.group(1) if ttl else 0, 120)}
        else:
            query = dict(parse_qsl(urlsplit(row["extract_url"] or "").query))
            # 控制台默认的 Rand 是随机地区，dola 不接受；旧链接没指定日韩的一律按日本
            region = query.get("region", "").upper()
            options = {"region": region if region in REGIONS else "JP",
                       "num": max(1, _to_int(query.get("num"), 1)),
                       "time": _legacy_minutes(query.get("time"), 10)}
            conn.execute("UPDATE proxies SET extract_interval=? WHERE id=?",
                         (API_SPACING, row["id"]))
        conn.execute("UPDATE proxies SET settings=? WHERE id=?",
                     (json.dumps(options), row["id"]))


def _legacy_minutes(value, default: int) -> int:
    """旧模板 / 链接里的分钟数；没有或超出 1-120 时用默认值。"""
    minutes = _to_int(value, 0)
    return minutes if 1 <= minutes <= _MAX_MINUTES else default


def _to_int(value, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


# ===== 校验 =====


def _region(value) -> str:
    code = str(value or "").strip().upper()
    if code not in REGIONS:
        raise ValueError("出口地区仅支持 JP（日本）或 KR（韩国）：dola 只接受日韩 IP")
    return code


def _minutes(value, what: str = "粘性时长") -> int:
    minutes = _to_int(value, 0)
    if not 1 <= minutes <= _MAX_MINUTES:
        raise ValueError(f"{what}取 1-120 分钟")
    return minutes


def _count(value) -> int:
    count = _to_int(value, 0)
    if count < 1:
        raise ValueError("每次提取数量至少为 1")
    return count


def _gateway(text: str) -> tuple[str, int]:
    """网关地址 host:port；带上 http:// 前缀也认。"""
    value = re.sub(r"^[a-z0-9]+://", "", (text or "").strip(), flags=re.I).rstrip("/")
    host, _, port = value.rpartition(":")
    if (not re.fullmatch(r"[^:/@\s]+", host) or not re.fullmatch(r"\d{1,5}", port)
            or not 0 < int(port) < 65536):
        raise ValueError("网关地址格式应为 host:port，例如 us.1024proxy.io:3000"
                         "（1024 控制台「账密认证 → 去使用」页查看）")
    return host, int(port)


def _api_link(link: str) -> str:
    value = (link or "").strip()
    parts = urlsplit(value)
    if (parts.scheme not in ("http", "https")
            or _API_HOST not in (parts.hostname or "").lower()):
        raise ValueError("请粘贴 1024 控制台生成的 API 提取链接"
                         "（形如 https://white.1024proxy.com/white/api?region=JP&num=1&time=10）")
    return value
