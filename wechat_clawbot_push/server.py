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
  python -m wechat_clawbot_push --refresh # 本地手动获取 token（退出 WB 后，手机发消息）
  python -m wechat_clawbot_push --test "x" # 本地手动推送一条（调试用）
  wechat-clawbot-push --mcp               # 安装 console script 后等价
"""

import json
import os
import sys
import time
import base64
import argparse
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


def get_full_token_from_settings():
    if not os.path.exists(SETTINGS_PATH):
        raise RuntimeError("找不到 settings.json: " + SETTINGS_PATH)
    with open(SETTINGS_PATH, encoding="utf-8") as f:
        d = json.load(f)
    users = d.get("claw", {}).get("users", {})
    for uid, u in users.items():
        ch = u.get("channels", {}).get("weixinClawBot", {})
        bt = ch.get("botToken")
        if bt and ":" in bt:
            return bt
    raise RuntimeError("settings.json 中未找到 weixinClawBot.botToken")


def resolve_token(config):
    raw = (config.get("bot_token") or "AUTO").strip()
    if not raw or raw.upper() == "AUTO":
        return get_full_token_from_settings()
    return raw


def make_uin_header():
    u = int.from_bytes(os.urandom(4), "big")
    return base64.b64encode(str(u).encode("ascii")).decode("ascii")


def ilink_post(path, body, secret):
    url = BASE_URL + path
    data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(url, data=data, method="POST")
    req.add_header("Content-Type", "application/json")
    req.add_header("AuthorizationType", "ilink_bot_token")
    req.add_header("Authorization", "Bearer " + secret)
    req.add_header("X-WECHAT-UIN", make_uin_header())
    req.add_header("SKRouteTag", SK_ROUTE_TAG)
    try:
        with urllib.request.urlopen(req, timeout=45) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read().decode("utf-8"))
        except Exception:
            return e.code, {"ret": -1, "errmsg": str(e)}
    except Exception as e:
        return -1, {"ret": -1, "errmsg": str(e)}


def acquire_token_once(config, wait_seconds=35):
    """长轮询一次获取 context_token + user_id。返回 (ok, detail)。"""
    bearer = resolve_token(config)
    cache = load_cache()
    cursor = cache.get("get_updates_buf", "")
    status, resp = ilink_post(
        "/ilink/bot/getupdates",
        {"get_updates_buf": cursor, "base_info": {"channel_version": CHANNEL_VERSION}},
        bearer,
    )
    if "errcode" in resp:
        return False, "getupdates 失败: errcode %s %s" % (resp.get("errcode"), resp.get("errmsg", ""))
    if resp.get("get_updates_buf"):
        cache["get_updates_buf"] = resp["get_updates_buf"]
    msgs = resp.get("msgs") or []
    if not msgs:
        return False, "本轮无新消息（%d 秒内手机未给 bot 发消息）。token 未变化。" % wait_seconds
    m0 = msgs[0]
    cache["user_id"] = m0.get("from_user_id")
    cache["context_token"] = m0.get("context_token")
    save_cache(cache)
    return True, "已获取并缓存 context_token / user_id: %s" % cache.get("user_id")


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
    status, resp = ilink_post("/ilink/bot/sendmessage", body, bearer)
    if "errcode" in resp or resp.get("ret") == -2:
        err = resp.get("errcode")
        if err == -14:
            return False, "TOKEN_EXPIRED", "token 已失效(errcode -14)：请重新调用 acquire_token 获取。"
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
                    "serverInfo": {"name": "wechat-clawbot-push", "version": "2.0.1"},
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
    ap.add_argument("--refresh", action="store_true", help="本地获取 token（需退出 WB 后手机发消息）")
    ap.add_argument("--test", metavar="TEXT", help="本地手动推送一条（调试）")
    args = ap.parse_args()
    config = load_config()
    if args.mcp:
        run_mcp(config)
    elif args.refresh:
        cmd_refresh(config)
    elif args.test:
        cmd_test(config, args.test)
    else:
        ap.print_help()


if __name__ == "__main__":
    main()
