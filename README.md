# wechat-clawbot-push

**让自动化任务的结果不再困在电脑里 —— 直接推到你手机微信。**

一个符合 MCP(stdio) 协议的服务器。注册为 WorkBuddy 连接器后，自动化 / 定时任务的结果
可以**主动推送到用户自己的个人微信**。无需企业微信，无需抓包。

---

## ⚠️ 开始前必读：与 WorkBuddy 自带微信连接互斥，二选一

微信侧的「ClawBot」名额**只有一个**，扫码授权会顶掉上一次授权。两者**无法同时工作**
（2026-09-30 双向实测确认）：

| 你绑定的是 | 结果 |
|---|---|
| 本包（扫码 `--login`） | WorkBuddy 自带微信连接**失效**，无法在微信里跟 AI 对话 |
| WorkBuddy 自带微信连接 | 本包的推送**失效** |

微信里只会显示一个 ClawBot 联系人 —— 这不是 bug，是名额限制。

选择建议：

- 想要**「自动化结果推到微信」** → 用本包，放弃原生双向
- 想要**「在微信里跟 AI 对话」** → 用 WorkBuddy 自带的微信连接，**不要装本包**

## 它不是什么

- **单向通道**：只能「WorkBuddy → 你的微信」，不能接收你的消息，也无法实现微信内对话
- **不是聊天机器人**：你给它发消息，它不会回
- **不替代** WorkBuddy 自带的微信连接 —— 见上方互斥说明

## 安装

```bash
pip install -i https://pypi.tuna.tsinghua.edu.cn/simple wechat-clawbot-push==2.0.3
# 或本地源码安装
pip install .
```

> 加 `-i` 是因为国内部分镜像（如阿里云）同步可能滞后数小时以上。不加的话，
> 指定版本会报 `No matching distribution found`，不指定版本则会**静默装上旧版**
> （旧版缺少网络容错，表现为推送时 TLS 握手卡死）。

## 怎么用：触发端在 WorkBuddy，不在微信

本包只负责「送出去」，**触发它的是 WorkBuddy**：

- 在对话里直接说「把结果推到我微信」
- 或建自动化任务，在 prompt 里写明「结果用微信推送工具发给我」

直接给微信里的 bot 发「帮我建个自动化任务」是**没有用的** —— bot 不是 WorkBuddy 的输入口。

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

## 命令行

- `--mcp`：stdio MCP 服务模式（主用）
- `--login`：扫码登录，生成桥独立的 `bot_token`
- `--refresh`：`acquire_token` 一次，用于获取 `context_token`
- `--diag`：自检，逐层打印凭证 / 会话 / 代理 / DNS / 链路状态
- `--test "文本"`：手动推送一条

## 网络容错

DNS 轮询可能返回「TCP 能建连、TLS 握手却卡死」的地址，而 `socket.create_connection`
只在建连失败时才换下一个 IP，**握手卡死不会触发轮换**，于是上层只能干等超时。
因此 `ilink_post` 采用三程选路：

1. 复用上次可用 IP（持久化在 `ip_cache.json`），传完整超时以保住长轮询等待窗口
2. 常规通路（保留现有代理设置），首探短超时以便快速失败
3. 解析全部 IPv4 逐个直连重试，SNI 与 Host 仍用原域名

遇到推送异常时先跑 `--diag`：它会把「连不上」和「没人发消息」这两种表现相同、
成因完全不同的情况区分开。

## 实现说明（stdio 铁律）

- stdout 仅输出 newline-delimited JSON-RPC 消息；所有日志走 stderr
- 二进制缓冲 + UTF-8 编解码，规避 Windows GBK 中文乱码
- 运行态（token/缓存）写入用户级目录 `~/.workbuddy/wechat-clawbot-push/`，与安装位置无关、多用户隔离

## License

MIT
