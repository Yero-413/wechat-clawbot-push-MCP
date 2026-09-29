#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
个人微信 ClawBot 定时主动推送桥 —— 纯 MCP 服务器版（合并 获取token + 主动推送）
=============================================================================
一个符合 MCP(stdio) 协议的服务器。注册为 WorkBuddy 连接器后，云端 WB 自动化即可
直接调用，无需再维护单独的命令行脚本。

暴露的工具:
  - push_wechat_message(text[, auto_acquire]): 主动推送一条文本到用户个人微信。
        发送前自动验证 context_token：已获取则直接推送；未获取则返回明确指引，
        提示先调用 acquire_token 并在手机给 bot 发一条消息完成绑定。
        auto_acquire=true 时，token 缺失会尝试自动长轮询获取（需手机配合，会阻塞等待）。
  - acquire_token(): 获取/刷新 context_token（长轮询，期间需用手机给 bot 发一条消息）。
  - bridge_status(): 返回当前 token 状态，供自动化推送前自检。

契约(已核对腾讯官方开源 OpenClaw / iLink 协议):
  - 收消息(长轮询): POST https://ilinkai.weixin.qq.com/ilink/bot/getupdates
  - 发消息(主动):   POST https://ilinkai.weixin.qq.com/ilink/bot/sendmessage
  - 鉴权: AuthorizationType: ilink_bot_token + Authorization: Bearer {bot_id:secret}
  - 每次请求: X-WECHAT-UIN = base64(随机uint32) 防重放
  - 主动发须带回 context_token(随入站消息返回，持久化可复用)

token 默认从 ~/.workbuddy/settings.json 的
claw.users.*.channels.weixinClawBot.botToken (= "bot_id:secret") 自动读取。
context_token / 用户微信id / 游标 缓存到用户级目录
~/.workbuddy/wechat-clawbot-push/push_cache.json（与安装位置无关，多用户隔离）。

用法:
  python -m wechat_clawbot_push --mcp     # 以 stdio MCP 服务器运行（WB 连接器，主用）
  python -m wechat_clawbot_push --login   # 通过二维码获取并保存独立 bot_token
  python -m wechat_clawbot_push --refresh # 本地手动获取 context_token（手机发消息）
  python -m wechat_clawbot_push --test "x" # 本地手动推送一条（调试用）
  wechat-clawbot-push --mcp               # 安装 console script 后等价
"""

import json
import os
import sys
import time
import base64
import argparse
import urllib.parse
import webbrowser
import socket
import ssl
import http.client
import urllib.request
import urllib.error
import urllib.parse

BASE_URL = "https://ilinkai.weixin.qq.com"
CHANNEL_VERSION = "1.0.3"
SK_ROUTE_TAG = "1001"

# 网络容错参数。DNS 可能返回「建连成功但 TLS 握手卡死」的地址，
# 因此常规通路首探用较短超时，失败后立刻改用 IPv4 直连逐个轮换。
FIRST_TRY_TIMEOUT = 6
IP_TRY_TIMEOUT = 8
# --diag 里逐 IP 探测 TLS 握手用的短超时：好 IP 通常 0.1s 内完成，
# 卡死的那个值得快点判死刑，别让诊断命令本身等上一分钟。
TLS_PROBE_TIMEOUT = 5
# 诊断命令用的请求超时。短于长轮询的服务端挂起时间，配合上一步的探测结果判断，
# 既能快速出结论，又不会把"在等消息"误判成"连不上"。
DIAG_PROBE_TIMEOUT = 10

# 上一轮验证可用的 IP，命中时可跳过选路并直接使用完整 timeout。
# 具体读写见文件后段的 IP 缓存小节（依赖 APP_DIR，故在此仅声明）。
_LAST_GOOD_IP = None

# 运行态数据放用户级目录，绝不依赖安装位置（site-packages 不可写、且多用户共享会冲突）。
APP_DIR = os.path.join(os.path.expanduser("~"), ".workbuddy", "wechat-clawbot-push")
os.makedirs(APP_DIR, exist_ok=True)
CONFIG_PATH = os.path.join(APP_DIR, "config.json")
CACHE_PATH = os.path.join(APP_DIR, "push_cache.json")
SETTINGS_PATH = os.path.expanduser(r"~/.workbuddy/settings.json")
IP_CACHE_PATH = os.path.join(APP_DIR, "ip_cache.json")


# ---- IP 缓存：把上次验证可用的地址记到磁盘，重启后首请求也不必重新探测 ----
def _load_ip_cache():
    """载入上次可用的 IP，避免每个新进程都从零开始探测。"""
    global _LAST_GOOD_IP
    try:
        if os.path.exists(IP_CACHE_PATH):
            with open(IP_CACHE_PATH, encoding="utf-8") as f:
                _LAST_GOOD_IP = json.load(f).get("ip")
    except Exception:
        _LAST_GOOD_IP = None


def _save_ip_cache(ip):
    try:
        tmp = IP_CACHE_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"ip": ip, "saved_at": int(time.time())}, f)
        os.replace(tmp, IP_CACHE_PATH)
    except Exception:
        pass


def _clear_ip_cache():
    global _LAST_GOOD_IP
    _LAST_GOOD_IP = None
    try:
        if os.path.exists(IP_CACHE_PATH):
            os.remove(IP_CACHE_PATH)
    except Exception:
        pass


_load_ip_cache()


def load_config():
    if os.path.exists(CONFIG_PATH):
        try:
            with open(CONFIG_PATH, encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return {}
    return {}


def save_config(data):
    os.makedirs(APP_DIR, exist_ok=True)
    tmp = CONFIG_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, CONFIG_PATH)


def load_cache():
    if os.path.exists(CACHE_PATH):
        try:
            with open(CACHE_PATH, encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return {}
    return {}


def save_cache(data):
    tmp = CACHE_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, CACHE_PATH)


def _find_bot_token(value):
    """在 WorkBuddy 配置的不同版本结构中查找 bot token。"""
    if isinstance(value, dict):
        for key in ("botToken", "bot_token"):
            candidate = value.get(key)
            if isinstance(candidate, str) and ":" in candidate:
                return candidate.strip()
        for child in value.values():
            found = _find_bot_token(child)
            if found:
                return found
    elif isinstance(value, list):
        for child in value:
            found = _find_bot_token(child)
            if found:
                return found
    return None


def get_full_token_from_settings(config=None):
    """读取鉴权 bot_token；兼容 WorkBuddy 配置移动、环境变量和手工配置。"""
    config = config or {}
    for value in (
        os.environ.get("WECHAT_BOT_TOKEN"),
        config.get("bot_token"),
        config.get("botToken"),
    ):
        if isinstance(value, str) and ":" in value:
            return value.strip()
    if not os.path.exists(SETTINGS_PATH):
        raise RuntimeError("找不到 settings.json: " + SETTINGS_PATH)
    with open(SETTINGS_PATH, encoding="utf-8") as f:
        d = json.load(f)
    found = _find_bot_token(d)
    if found:
        return found
    raise RuntimeError(
        "未找到可用的 bot_token。请确认 WorkBuddy 已完成微信连接，或在 "
        "~/.workbuddy/wechat-clawbot-push/config.json 中配置 bot_token。"
    )


def resolve_token(config):
    raw = (config.get("bot_token") or "AUTO").strip()
    if not raw or raw.upper() == "AUTO":
        return get_full_token_from_settings(config)
    return raw


def resolve_base_url(config):
    return (config.get("base_url") or BASE_URL).rstrip("/")


def make_uin_header():
    u = int.from_bytes(os.urandom(4), "big")
    return base64.b64encode(str(u).encode("ascii")).decode("ascii")


def _json_response(resp):
    payload = resp.read().decode("utf-8")
    return json.loads(payload) if payload else {}


def get_login_qrcode():
    """申请独立 Bot 登录二维码，不依赖 WorkBuddy 的加密 settings.json。"""
    status, resp = _http_json(
        BASE_URL + "/ilink/bot/get_bot_qrcode?bot_type=3",
        data=json.dumps({"local_token_list": []}),
        headers={
            "Content-Type": "application/json",
            "AuthorizationType": "ilink_bot_token",
            "X-WECHAT-UIN": make_uin_header(),
            "iLink-App-Id": "bot",
        },
        method="POST",
        timeout=20,
    )
    if status > 0 and isinstance(resp, dict):
        return resp
    if isinstance(resp, dict):
        return {"status": "error", "errmsg": resp.get("errmsg", "HTTP %s" % status)}
    return {"status": "error", "errmsg": "HTTP %s" % status}


def get_login_status(qrcode, base_url=BASE_URL, verify_code=None):
    query = {"qrcode": qrcode}
    if verify_code:
        query["verify_code"] = verify_code
    url = (base_url or BASE_URL).rstrip("/") + "/ilink/bot/get_qrcode_status?" + urllib.parse.urlencode(query)
    status, resp = _http_json(
        url,
        headers={"iLink-App-Id": "bot"},
        method="GET",
        timeout=40,
    )
    if status > 0 and isinstance(resp, dict):
        return resp
    # 轮询期间的单次网络抖动不应中断登录流程，交给上层继续下一轮。
    return {"status": "wait", "errmsg": str((resp or {}).get("errmsg", "请求失败"))}


def cmd_login():
    """通过官方二维码登录并把 bot_token 保存到 config.json。"""
    result = get_login_qrcode()
    qrcode = result.get("qrcode")
    image = result.get("qrcode_img_content")
    if not qrcode:
        raise RuntimeError("获取登录二维码失败: " + str(result.get("errmsg") or result))

    print("请使用微信扫描 ClawBot 二维码。")
    if image:
        print("二维码地址: " + image)
        if isinstance(image, str) and image.startswith(("http://", "https://")):
            try:
                webbrowser.open(image)
            except Exception:
                pass

    poll_base = BASE_URL
    verify_code = None
    deadline = time.time() + 300
    while time.time() < deadline:
        status = get_login_status(qrcode, poll_base, verify_code)
        state = status.get("status")
        if state == "confirmed":
            bot_token = status.get("bot_token")
            if not bot_token or ":" not in bot_token:
                raise RuntimeError("扫码成功但服务端没有返回有效 bot_token")
            config = load_config()
            config["bot_token"] = bot_token
            if status.get("baseurl"):
                config["base_url"] = status["baseurl"]
            if status.get("ilink_bot_id"):
                config["ilink_bot_id"] = status["ilink_bot_id"]
            save_config(config)
            print("登录成功，bot_token 已保存到: " + CONFIG_PATH)
            return
        if state == "need_verifycode":
            verify_code = input("请输入微信上显示的验证码: ").strip()
        elif state == "scaned_but_redirect":
            redirect = status.get("redirect_host") or status.get("baseurl")
            if redirect:
                poll_base = redirect if redirect.startswith("http") else "https://" + redirect
        elif state in ("expired", "verify_code_blocked", "binded_redirect"):
            raise RuntimeError("二维码登录未完成，状态: " + str(state))
        time.sleep(2)
    raise RuntimeError("二维码已等待 5 分钟仍未完成，请重新运行 --login")


def _log(msg):
    """日志统一写 stderr。stdout 在 MCP 模式下只允许 JSON-RPC，绝不能被污染。"""
    try:
        sys.stderr.write("[wechat-clawbot-push] " + msg + "\n")
        sys.stderr.flush()
    except Exception:
        pass


def _ipv4_candidates(host):
    """列出域名的全部 IPv4 地址（去重、保序）。"""
    seen, ips = set(), []
    try:
        infos = socket.getaddrinfo(host, 443, socket.AF_INET, socket.SOCK_STREAM)
    except Exception:
        return ips
    for info in infos:
        ip = info[4][0]
        if ip not in seen:
            seen.add(ip)
            ips.append(ip)
    return ips


def _request_on_ip(host, path, ip, data, headers, timeout, method="POST"):
    """把单个 IPv4 直接当目标发起请求，SNI 与 Host 仍用原域名。

    resolve(DNS) 常常返回「TCP 能连上、TLS 握手却卡死」的地址，而
    socket.create_connection 只在建连阶段失败才换下一个地址，
    握手卡住不会触发轮换，于是上层只能干等到超时。这里手动逐个 IP 试。
    """
    ctx = ssl.create_default_context()
    try:
        raw = socket.create_connection((ip, 443), timeout=timeout)
        sock = ctx.wrap_socket(raw, server_hostname=host)
    except Exception:
        return -1, None
    conn = None
    try:
        conn = http.client.HTTPSConnection(host, 443, timeout=timeout, context=ctx)
        conn.sock = sock
        conn.request(method, path, body=(data or None), headers=headers)
        resp = conn.getresponse()
        payload = resp.read().decode("utf-8")
        body_json = json.loads(payload) if payload else {}
        return resp.status, body_json
    except Exception:
        return -1, None
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass


def _http_json(url, data=None, headers=None, method="POST", timeout=20):
    """统一网络出口：常规通路优先，失败后自动改用 IPv4 直连轮换。"""
    parts = urllib.parse.urlsplit(url)
    host = parts.hostname or ""
    path = parts.path or "/"
    if parts.query:
        path += "?" + parts.query
    body = data.encode("utf-8") if isinstance(data, str) else (data or b"")
    hdrs = dict(headers or {})

    # 第零程：复用上次验证可用的 IP，且传完整 timeout。
    # 长轮询(getupdates)依赖服务端挂起几十秒返回消息，不能被短超时掐断。
    global _LAST_GOOD_IP
    if _LAST_GOOD_IP:
        status, resp_json = _request_on_ip(host, path, _LAST_GOOD_IP, body, hdrs, timeout, method)
        if status > 0:
            return status, (resp_json or {})
        _log("已缓存的 IP %s 失效，重新选路" % _LAST_GOOD_IP)
        _clear_ip_cache()

    # 第一程：常规通路（保留代理设置）。首探刻意用较短超时，
    # 免得被坏 IP 的 TLS 握手长时间拖住；失败还有第二程兜底。
    req = urllib.request.Request(url, data=body, headers=hdrs, method=method)
    try:
        with urllib.request.urlopen(req, timeout=min(timeout, FIRST_TRY_TIMEOUT)) as resp:
            payload = resp.read().decode("utf-8")
            return resp.status, (json.loads(payload) if payload else {})
    except urllib.error.HTTPError as e:
        # HTTP 层已通（4xx/5xx 属于服务端业务结果），不必再走兜底。
        try:
            return e.code, json.loads(e.read().decode("utf-8"))
        except Exception:
            return e.code, {"ret": -1, "errmsg": str(e)}
    except Exception as e:
        _log("常规通路失败(%s)，改用 IPv4 直连轮换" % str(e)[:60])

    # 第二程：逐个 IPv4 强制轮换重试。
    for ip in _ipv4_candidates(host):
        status, resp_json = _request_on_ip(
            host, path, ip, body, hdrs, min(timeout, IP_TRY_TIMEOUT), method
        )
        if status > 0:
            _log("IPv4 直连成功 %s" % ip)
            _LAST_GOOD_IP = ip
            _save_ip_cache(ip)
            return status, (resp_json or {})
    return -1, {"ret": -1, "errmsg": "常规通路与 IPv4 直连轮换均失败"}


def ilink_post(path, body, secret, base_url=None, timeout=45):
    """带鉴权的 iLink 请求，走统一容错出口。"""
    base = (base_url or BASE_URL).rstrip("/")
    return _http_json(
        base + path,
        data=json.dumps(body),
        headers={
            "Content-Type": "application/json",
            "AuthorizationType": "ilink_bot_token",
            "Authorization": "Bearer " + secret,
            "X-WECHAT-UIN": make_uin_header(),
            "SKRouteTag": SK_ROUTE_TAG,
        },
        method="POST",
        timeout=timeout,
    )


def _is_context_expired(resp):
    """iLink 对过期 context_token 既可能返回 -14，也可能只返回 ret=-2。"""
    return resp.get("errcode") in (-14, "-14") or resp.get("ret") in (-14, -2, "-14", "-2")


def _is_success(resp):
    """兼容 ret/errcode 缺省、为 0 或以字符串返回的成功响应。"""
    return (
        resp.get("ret") in (None, 0, "0")
        and resp.get("errcode") in (None, 0, "0")
    )


def _save_messages(cache, msgs):
    """保存本轮最新会话，避免只取第一条消息导致 token 落后。"""
    valid = [m for m in msgs if m.get("from_user_id") and m.get("context_token")]
    if not valid:
        return False
    message = valid[-1]
    cache["user_id"] = message.get("from_user_id")
    cache["context_token"] = message.get("context_token")
    cache["context_token_updated_at"] = int(time.time())
    return True


def acquire_token_once(config, wait_seconds=35):
    """长轮询一次获取 context_token + user_id。返回 (ok, detail)。"""
    bearer = resolve_token(config)
    cache = load_cache()
    cursor = cache.get("get_updates_buf", "")
    status, resp = ilink_post(
        "/ilink/bot/getupdates",
        {"get_updates_buf": cursor, "base_info": {"channel_version": CHANNEL_VERSION}},
        bearer,
        base_url=resolve_base_url(config),
    )
    if not _is_success(resp):
        return False, "getupdates 失败: errcode %s %s" % (resp.get("errcode"), resp.get("errmsg", ""))
    if resp.get("get_updates_buf"):
        cache["get_updates_buf"] = resp["get_updates_buf"]
    msgs = resp.get("msgs") or []
    if not msgs:
        return False, "本轮无新消息（%d 秒内手机未给 bot 发消息）。token 未变化。" % wait_seconds
    if not _save_messages(cache, msgs):
        return False, "收到消息但未包含可用 context_token；请让手机给 bot 发一条普通文本消息。"
    save_cache(cache)
    return True, "已获取并缓存 context_token / user_id: %s" % cache.get("user_id")


def _refresh_context(config, cache):
    """非阻塞地消费一次新消息，用于发送前刷新会话上下文。"""
    bearer = resolve_token(config)
    status, resp = ilink_post(
        "/ilink/bot/getupdates",
        {"get_updates_buf": cache.get("get_updates_buf", ""),
         "base_info": {"channel_version": CHANNEL_VERSION}},
        bearer,
        base_url=resolve_base_url(config),
        timeout=45,
    )
    if resp.get("get_updates_buf"):
        cache["get_updates_buf"] = resp["get_updates_buf"]
    if not _is_success(resp):
        return False
    changed = _save_messages(cache, resp.get("msgs") or [])
    if changed:
        save_cache(cache)
    return changed


def do_send(text):
    """用缓存的 context_token 主动发一条文本到用户微信。返回 (ok, code, detail)。"""
    config = load_config()
    bearer = resolve_token(config)
    cache = load_cache()
    user_id = config.get("user_id") or cache.get("user_id")
    ctx = cache.get("context_token")
    if not user_id or not ctx:
        return False, "NO_TOKEN", "尚未获取 token：请先调用 acquire_token 工具，并在手机给 bot 发一条消息完成绑定，再执行推送。"
    msg = {
        "from_user_id": "",
        "to_user_id": user_id,
        "client_id": "push-" + os.urandom(8).hex(),
        "message_type": 2,
        "message_state": 2,
        "context_token": ctx,
        "item_list": [{"type": 1, "text_item": {"text": text}}],
    }
    body = {"msg": msg, "base_info": {"channel_version": CHANNEL_VERSION}}
    status, resp = ilink_post(
        "/ilink/bot/sendmessage",
        body,
        bearer,
        base_url=resolve_base_url(config),
        timeout=20,
    )
    if not _is_success(resp):
        err = resp.get("errcode")
        if _is_context_expired(resp):
            # 先尝试在不阻塞的情况下接收用户刚发来的新消息并刷新上下文。
            if _refresh_context(config, cache):
                refreshed = load_cache()
                msg["to_user_id"] = refreshed.get("user_id") or user_id
                msg["context_token"] = refreshed.get("context_token")
                retry_status, retry_resp = ilink_post(
                    "/ilink/bot/sendmessage",
                    {"msg": msg, "base_info": {"channel_version": CHANNEL_VERSION}},
                    bearer,
                    base_url=resolve_base_url(config),
                    timeout=20,
                )
                if _is_success(retry_resp):
                    return True, "OK", "会话上下文已自动刷新，发送成功"
                status, resp = retry_status, retry_resp
            # 新版 iLink 会在缺省 context_token 时使用最近活跃会话。
            msg.pop("context_token", None)
            fallback_status, fallback_resp = ilink_post(
                "/ilink/bot/sendmessage",
                {"msg": msg, "base_info": {"channel_version": CHANNEL_VERSION}},
                bearer,
                base_url=resolve_base_url(config),
                timeout=20,
            )
            if _is_success(fallback_resp):
                return True, "OK", "已使用当前活跃会话发送成功（未携带过期 context_token）"
            return False, "TOKEN_EXPIRED", (
                "会话 context_token 已失效，自动刷新和无 context_token 发送均失败 "
                "(HTTP %s, ret %s, errcode %s)。请让手机给 bot 发一条普通消息后重试。"
                % (fallback_status, fallback_resp.get("ret"), fallback_resp.get("errcode"))
            )
        return False, "SEND_FAIL", "HTTP %s | errcode %s | %s | ret %s" % (status, err, resp.get("errmsg", ""), resp.get("ret"))
    return True, "OK", "HTTP %s | 发送成功" % status


# ---- MCP 服务器（stdio）----
def run_mcp(config):
    TOOLS = [
        {
            "name": "push_wechat_message",
            "description": (
                "向用户【个人微信】(ClawBot)主动推送一条文本消息。发送前自动验证 context_token："
                "已获取则直接推送；未获取或已失效则返回明确指引，提示先调用 acquire_token 并在手机给 bot 发消息。"
                "供 WB 自动化在定时/触发任务完成后调用，把结果推送到用户微信。"
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "text": {"type": "string", "description": "要推送到微信的文本内容"},
                    "auto_acquire": {
                        "type": "boolean",
                        "description": "可选。token 缺失时是否自动尝试获取(会阻塞等待手机消息，默认 false)",
                    },
                },
                "required": ["text"],
            },
        },
        {
            "name": "acquire_token",
            "description": (
                "获取/刷新 context_token（长轮询，约 35 秒内需用手机给 bot 发一条消息以完成绑定）。"
                "推送前若 bridge_status 显示未获取 token，应先调用本工具。"
            ),
            "inputSchema": {"type": "object", "properties": {}},
        },
        {
            "name": "bridge_status",
            "description": "返回推送桥当前状态：是否已缓存 context_token、目标用户微信id。供自动化推送前自检。",
            "inputSchema": {"type": "object", "properties": {}},
        },
    ]

    def send(obj):
        # stdout 铁律：只能写 JSON-RPC 消息（newline-delimited），且必须 UTF-8 字节。
        # 任何日志/print 都只能走 stderr，否则会破坏协议解析。
        data = (json.dumps(obj, ensure_ascii=False) + "\n").encode("utf-8")
        sys.stdout.buffer.write(data)
        sys.stdout.buffer.flush()

    def log(msg):
        sys.stderr.write("[mcp] " + msg + "\n")
        sys.stderr.flush()

    log("推送桥 MCP 服务器启动（stdio）。")

    # 二进制缓冲 + UTF-8 解码（Windows 默认 GBK 会把中文参数解成乱码 → 微信收到乱码）。
    for raw_bytes in sys.stdin.buffer:
        raw = raw_bytes.decode("utf-8").strip()
        if not raw:
            continue
        try:
            msg = json.loads(raw)
        except Exception as e:
            log("JSON 解析失败: " + str(e))
            continue
        method = msg.get("method")
        mid = msg.get("id")
        params = msg.get("params") or {}

        if method == "initialize":
            send({
                "jsonrpc": "2.0",
                "id": mid,
                "result": {
                    "protocolVersion": params.get("protocolVersion", "2024-11-05"),
                    "capabilities": {"tools": {}},
                    "serverInfo": {"name": "wechat-clawbot-push", "version": "2.0.3"},
                },
            })
        elif method == "notifications/initialized":
            continue
        elif method == "tools/list":
            send({"jsonrpc": "2.0", "id": mid, "result": {"tools": TOOLS}})
        elif method == "tools/call":
            name = params.get("name")
            arguments = params.get("arguments") or {}
            if name == "push_wechat_message":
                text = (arguments.get("text") or "").strip()
                auto_acquire = bool(arguments.get("auto_acquire", False))
                if not text:
                    send({"jsonrpc": "2.0", "id": mid, "result": {
                        "content": [{"type": "text", "text": "缺少 text 参数"}], "isError": True}})
                    continue
                cache = load_cache()
                if not (cache.get("context_token") and (config.get("user_id") or cache.get("user_id"))):
                    if auto_acquire:
                        ok, detail = acquire_token_once(config)
                        if not ok:
                            send({"jsonrpc": "2.0", "id": mid, "result": {"content": [{"type": "text", "text": "token 缺失且自动获取失败：" + detail + "。请改显式调用 acquire_token 并在手机给 bot 发消息。"}], "isError": True}})
                            continue
                        log("auto_acquire 成功，继续推送。")
                    else:
                        send({"jsonrpc": "2.0", "id": mid, "result": {"content": [{"type": "text", "text": "尚未获取 token：请先调用 acquire_token 工具，并在手机给 bot 发一条消息完成绑定，再执行推送。"}], "isError": True}})
                        continue
                ok, code, detail = do_send(text)
                if not ok and code == "TOKEN_EXPIRED":
                    send({"jsonrpc": "2.0", "id": mid, "result": {"content": [{"type": "text", "text": detail + " 请调用 acquire_token 重新获取后再推送。"}], "isError": True}})
                    continue
                send({"jsonrpc": "2.0", "id": mid, "result": {"content": [{"type": "text", "text": detail}], "isError": not ok}})
            elif name == "acquire_token":
                ok, detail = acquire_token_once(config)
                send({"jsonrpc": "2.0", "id": mid, "result": {"content": [{"type": "text", "text": detail}], "isError": not ok}})
            elif name == "bridge_status":
                cache = load_cache()
                send({"jsonrpc": "2.0", "id": mid, "result": {"content": [{"type": "text", "text": json.dumps({
                    "has_context_token": bool(cache.get("context_token")),
                    "user_id": cache.get("user_id"),
                    "cache_path": CACHE_PATH,
                }, ensure_ascii=False)}], "isError": False}})
            else:
                send({"jsonrpc": "2.0", "id": mid, "result": {"content": [{"type": "text", "text": "未知工具: " + str(name)}], "isError": True}})
        else:
            if mid is not None:
                send({"jsonrpc": "2.0", "id": mid, "result": {}})


# ---- 本地 CLI 调试（保留，便于无 WB 时验证）----
def cmd_refresh(config):
    ok, detail = acquire_token_once(config)
    print(("HTTP 成功 | " if ok else "[FAIL] ") + detail)


def cmd_diag(config):
    """一键自检：把「凭证 / 会话 / 代理 / DNS / 链路」五层逐项摊开。

    排查时最容易混淆的是「没人发消息」和「压根连不上」——两者在上层工具返回的
    业务文案里长得一模一样（都像"本轮无新消息"）。这个命令绕过所有业务包装，
    直接压到网络层，把真相打出来。
    """
    host = urllib.parse.urlsplit(BASE_URL).hostname or ""
    print("=== wechat-clawbot-push 自检 ===")

    print("\n[1] 凭证")
    token, source = None, None
    for label, value in (
        ("环境变量 WECHAT_BOT_TOKEN", os.environ.get("WECHAT_BOT_TOKEN")),
        ("config.json", config.get("bot_token") or config.get("botToken")),
    ):
        if isinstance(value, str) and ":" in value:
            token, source = value.strip(), label
            break
    if token is None:
        try:
            token = get_full_token_from_settings(config)
            source = "WorkBuddy settings.json"
        except Exception as e:
            print("  读取失败: %s" % str(e)[:110])
    if token:
        print("  来源: %s" % source)
        print("  形态: 长度 %d，bot_id=%s" % (len(token), token.split(":", 1)[0]))
    else:
        print("  [缺失] 无可用凭证。若 WorkBuddy 已加密其凭证，请用 --login 为桥单独申请。")
    print("  配置: %s" % CONFIG_PATH)

    print("\n[2] 会话缓存 (context_token)")
    cache = load_cache()
    uid, ctx = cache.get("user_id"), cache.get("context_token")
    print("  user_id      : %s" % (uid or "(无)"))
    print("  context_token: %s" % ("已缓存 %d 字符" % len(ctx) if ctx else "(无)"))
    if uid and ctx:
        print("  [OK] 已绑定，之后 token 失效会自动恢复。")
    else:
        print("  [待绑定] 请用手机给 bot 发任意一条消息。")

    print("\n[3] 代理环境")
    proxies = urllib.request.getproxies()
    print("  %s" % (proxies if proxies else "(无，直连)"))
    print("  注: WorkBuddy 注入的代理端口是动态的，别在配置里写死清空/强制策略。")
    print("  当前缓存 IP: %s" % (_LAST_GOOD_IP or "(无，下次请求将重新选路)"))

    print("\n[4] DNS 与 TLS 握手（逐个 IPv4，超时 %ss）" % TLS_PROBE_TIMEOUT)
    print("  注: 部分 IP 能完成 TCP 建连却在 TLS 握手时卡死，")
    print("      而 create_connection 只在建连失败时才换地址，不会感知握手卡死。")
    ips = _ipv4_candidates(host)
    print("  %s 解析出 %d 个地址" % (host, len(ips)))
    good = []
    for ip in ips:
        t0 = time.time()
        try:
            raw = socket.create_connection((ip, 443), timeout=TLS_PROBE_TIMEOUT)
            ssl.create_default_context().wrap_socket(raw, server_hostname=host).close()
            good.append(ip)
            print("  %-16s OK   %.2fs" % (ip, time.time() - t0))
        except Exception as e:
            print("  %-16s FAIL %s  %.2fs" % (ip, type(e).__name__, time.time() - t0))
    if not good:
        print("  [严重] 无一 IP 能完成握手，属本机网络或 DNS 层故障。")

    # 刻意不用 getupdates：它是长轮询，服务端会挂起等待消息（可达 35 秒），
    # 用短超时去打它必然超时，还会被误判成"IP 坏了"进而清空缓存。
    # 验证链路要选一个「立即返回」的接口，get_bot_qrcode 正合适（且无需鉴权）。
    print("\n[5] 实际链路请求（超时 %ss，探测接口 get_bot_qrcode）" % DIAG_PROBE_TIMEOUT)
    t0 = time.time()
    probe = get_login_qrcode()
    elapsed = time.time() - t0
    if probe.get("qrcode"):
        print("  HTTP 200  (%.2fs)" % elapsed)
        print("  errmsg 业务字段: %s" % (probe.get("errmsg") or "(无)"))
        print("  => 链路与鉴权层均正常。")
        status = 200
    else:
        print("  失败  (%.2fs)" % elapsed)
        print("  错误: %s" % str(probe.get("errmsg"))[:140])
        print("  => 三程选路全部失败，请检查本机外网。")
        status = -1

    print("\n=== 摘要 ===")
    print("  凭证   %s" % ("OK  " if token else "缺失"))
    print("  会话   %s" % ("OK  " if (uid and ctx) else "待绑定"))
    print("  网络   %s" % ("OK  " if status > 0 else "失败"))
    print("  可用IP %s" % (", ".join(good) if good else "无"))


def cmd_test(config, text):
    ok, code, detail = do_send(text)
    print(detail)
    if ok:
        print("[OK] 已主动推送。请检查手机微信是否收到。")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--mcp", action="store_true", help="以 stdio MCP 服务器模式运行（主用）")
    ap.add_argument("--login", action="store_true", help="通过微信二维码登录并保存 bot_token")
    ap.add_argument("--refresh", action="store_true", help="本地获取 token（需退出 WB 后手机发消息）")
    ap.add_argument("--test", metavar="TEXT", help="本地手动推送一条（调试）")
    ap.add_argument("--diag", action="store_true", help="自检：凭证/会话/代理/DNS/链路逐项诊断")
    args = ap.parse_args()
    config = load_config()
    if args.login:
        cmd_login()
    elif args.mcp:
        run_mcp(config)
    elif args.refresh:
        cmd_refresh(config)
    elif args.diag:
        cmd_diag(config)
    elif args.test:
        cmd_test(config, args.test)
    else:
        ap.print_help()


if __name__ == "__main__":
    main()
