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

## WorkBuddy 凭证加密时的独立登录

如果 WorkBuddy 的 `settings.json` 中 `botToken` 已经是加密字符串，桥无法从中恢复明文凭证。
可以使用官方二维码登录流程生成桥自己的 `bot_token`：

```bash
python -m wechat_clawbot_push --login
```

程序会打开二维码地址。使用微信扫码，若手机要求验证码，就把验证码输入回终端。登录成功后，
凭证会保存到 `~/.workbuddy/wechat-clawbot-push/config.json`，之后 MCP 推送会优先使用它。
请不要把这个文件或 `bot_token` 发到群聊、日志或 issue 中。

## 暴露的工具

| 工具 | 作用 |
|---|---|
| `push_wechat_message(text[, auto_acquire])` | 主动推送一条文本到用户个人微信（发送前自动验证 token） |
| `acquire_token()` | 获取/刷新 context_token（长轮询，需手机给 bot 发一条消息） |
| `bridge_status()` | 返回当前 token 状态，供自动化推送前自检 |

## 首次授权

每个用户首次使用前需执行一次 `acquire_token`：手机给 bot 发一条普通文本消息，脚本捕获并缓存
`context_token`。不需要退出 WorkBuddy；如果 WorkBuddy 正在独占收消息通道，请暂时停止它的
微信连接后再执行获取。

这里有两个容易混淆的 token：`bot_token` 是账号鉴权凭证，`context_token` 是会话上下文凭证。
后者会过期或在重新连接后变化，不能当作永久 token。发送失败且服务端返回 `ret=-2` 或
`errcode=-14` 时，桥会自动尝试读取新入站消息刷新上下文，然后重试；新版 iLink 还会尝试
省略过期的 `context_token`，使用最近活跃会话发送。

如果 WorkBuddy 的 `settings.json` 版本变化导致自动读取不到账号凭证，可以在
`~/.workbuddy/wechat-clawbot-push/config.json` 中显式配置（文件权限请保持仅当前用户可读）：

```json
{
  "bot_token": "bot_id:secret"
}
```

也可以使用环境变量 `WECHAT_BOT_TOKEN`。桥不会解密或绕过 WorkBuddy 的加密凭证；如果文件中
保存的确实是不可逆的密文，应该从 WorkBuddy 的已连接配置或官方二维码登录流程重新取得
`bot_token`，而不是把 `context_token` 当作替代品。

## 协议契约（已核对腾讯官方 iLink / OpenClaw）

- 收消息：`POST https://ilinkai.weixin.qq.com/ilink/bot/getupdates`
- 发消息：`POST https://ilinkai.weixin.qq.com/ilink/bot/sendmessage`
- 鉴权：`Authorization: Bearer {bot_id:secret}`（token 自动从 `~/.workbuddy/settings.json` 读取）
- 主动发优先带回最近一次入站消息的 `context_token`；会话失效时自动刷新或尝试省略该字段
- `--login` 使用官方 `get_bot_qrcode` / `get_qrcode_status` 流程获取独立 `bot_token`

## 实现说明（stdio 铁律）

- stdout 仅输出 newline-delimited JSON-RPC 消息；所有日志走 stderr
- 二进制缓冲 + UTF-8 编解码，规避 Windows GBK 中文乱码
- 运行态（token/缓存）写入用户级目录 `~/.workbuddy/wechat-clawbot-push/`，与安装位置无关、多用户隔离

## License

MIT
