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
import urllib.request
import urllib.error

BASE_URL = "https://ilinkai.weixin.qq.com"
CHANNEL_VERSION = "1.0.3"
SK_ROUTE_TAG = "1001"

# 运行态数据放用户级目录，绝不依赖安装位置（site-packages 不可写、且多用户共享会冲突）。
APP_DIR = os.path.join(os.path.expanduser("~"), ".workbuddy", "wechat-clawbot-push")
os.makedirs(APP_DIR, exist_ok=True)
CONFIG_PATH = os.path.join(APP_DIR, "config.json")
CACHE_PATH = os.path.join(APP_DIR, "push_cache.json")
SETTINGS_PATH = os.path.expanduser(r"~/.workbuddy/settings.json")


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
    url = BASE_URL + "/ilink/bot/get_bot_qrcode?bot_type=3"
    req = urllib.request.Request(
        url,
        data=json.dumps({"local_token_list": []}).encode("utf-8"),
        method="POST",
    )
    req.add_header("Content-Type", "application/json")
    req.add_header("AuthorizationType", "ilink_bot_token")
    req.add_header("X-WECHAT-UIN", make_uin_header())
    req.add_header("iLink-App-Id", "bot")
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            return _json_response(resp)
    except urllib.error.HTTPError as e:
        try:
            return _json_response(e)
        except Exception:
            return {"status": "error", "errmsg": "HTTP %s" % e.code}
    except Exception as e:
        return {"status": "error", "errmsg": str(e)}


def get_login_status(qrcode, base_url=BASE_URL, verify_code=None):
    query = {"qrcode": qrcode}
    if verify_code:
        query["verify_code"] = verify_code
    url = (base_url or BASE_URL).rstrip("/") + "/ilink/bot/get_qrcode_status?" + urllib.parse.urlencode(query)
    req = urllib.request.Request(url, method="GET")
    req.add_header("iLink-App-Id", "bot")
    try:
        with urllib.request.urlopen(req, timeout=40) as resp:
            return _json_response(resp)
    except Exception as e:
        return {"status": "wait", "errmsg": str(e)}


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


def ilink_post(path, body, secret, base_url=None, timeout=45):
    url = (base_url or BASE_URL).rstrip("/") + path
    data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(url, data=data, method="POST")
    req.add_header("Content-Type", "application/json")
    req.add_header("AuthorizationType", "ilink_bot_token")
    req.add_header("Authorization", "Bearer " + secret)
    req.add_header("X-WECHAT-UIN", make_uin_header())
    req.add_header("SKRouteTag", SK_ROUTE_TAG)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            payload = resp.read().decode("utf-8")
            return resp.status, (json.loads(payload) if payload else {})
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read().decode("utf-8"))
        except Exception:
            return e.code, {"ret": -1, "errmsg": str(e)}
    except Exception as e:
        return -1, {"ret": -1, "errmsg": str(e)}


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
                    "serverInfo": {"name": "wechat-clawbot-push", "version": "2.0.2"},
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
    args = ap.parse_args()
    config = load_config()
    if args.login:
        cmd_login()
    elif args.mcp:
        run_mcp(config)
    elif args.refresh:
        cmd_refresh(config)
    elif args.test:
        cmd_test(config, args.test)
    else:
        ap.print_help()


if __name__ == "__main__":
    main()
