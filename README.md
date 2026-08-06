# wechat-clawbot-push

个人微信 ClawBot 定时主动推送桥 —— 一个符合 MCP(stdio) 协议的服务器。注册为
WorkBuddy 连接器后，云端 WB 自动化即可直接调用，把定时/触发任务的结果**主动推送到
用户自己的个人微信**。无需企业微信，无需抓包。

## 安装

```bash
pip install wechat-clawbot-push
# 或本地源码安装
pip install .
```

## 作为 WorkBuddy 连接器使用

在 `~/.workbuddy/mcp.json` 注册（command 指向你的 python，args 用 -m）：

```json
{
  "mcpServers": {
    "wechat-clawbot-push": {
      "command": "python",
      "args": ["-m", "wechat_clawbot_push", "--mcp"]
    }
  }
}
```

或安装后直接用 console script：

```json
{
  "mcpServers": {
    "wechat-clawbot-push": {
      "command": "wechat-clawbot-push",
      "args": ["--mcp"]
    }
  }
}
```

## 暴露的工具

| 工具 | 作用 |
|---|---|
| `push_wechat_message(text[, auto_acquire])` | 主动推送一条文本到用户个人微信（发送前自动验证 token） |
| `acquire_token()` | 获取/刷新 context_token（长轮询，需手机给 bot 发一条消息） |
| `bridge_status()` | 返回当前 token 状态，供自动化推送前自检 |

## 首次授权

每个用户首次使用前需执行一次 `acquire_token`：退出 WB → 手机给 bot 发一条消息 →
脚本捕获并缓存 context_token。之后 `push_wechat_message` 即可全自动推送。

## 协议契约（已核对腾讯官方 iLink / OpenClaw）

- 收消息：`POST https://ilinkai.weixin.qq.com/ilink/bot/getupdates`
- 发消息：`POST https://ilinkai.weixin.qq.com/ilink/bot/sendmessage`
- 鉴权：`Authorization: Bearer {bot_id:secret}`（token 自动从 `~/.workbuddy/settings.json` 读取）
- 主动发须带回 `context_token`（随入站消息返回，持久化可复用）

## 实现说明（stdio 铁律）

- stdout 仅输出 newline-delimited JSON-RPC 消息；所有日志走 stderr
- 二进制缓冲 + UTF-8 编解码，规避 Windows GBK 中文乱码
- 运行态（token/缓存）写入用户级目录 `~/.workbuddy/wechat-clawbot-push/`，与安装位置无关、多用户隔离

## License

MIT
