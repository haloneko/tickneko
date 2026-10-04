# AI 服务节点

`ai-service` 是普通动作节点，不接管 Gateway，不加入全局消费标志，也不修改消息路由。
用已有 `condition` 的未命中出口连接它，就能把 AI 当默认分支。

例如：开始 → 条件（消息包含 awa）→ 满足：发送 awa → 结束；不满足：AI 服务 → 结束。
未命中的内容只请求一次；没有连接 AI 节点的分支不会请求 AI。

## 接口

POST `endpoint`，Bearer 凭据从服务器 `token_env` 指定的环境变量读取；图中只存变量名。
JSON 字段：`platform`、`chat`（group/private）、`chat_id`、`message_id`、`user_id`、`message`。
接收方应验证真实消息身份、按平台/会话/消息编号去重，并返回 `{"accepted": true}`。
这表示接单，不表示已经生成正文。接收方继续负责聊天历史、人设、模型备用链、图片和最终发送。

消息编号与发言人来自本次触发，不由模型猜测；不能用于没有真实消息的定时触发。
超时只限制接单（1–60 秒），不限制模型生成。HTTP 错误/无效接单结果是 NodeFailure；
网络错误是 EnvironmentFailure。接单状态不确定时不自动重投，避免重复回复。

本机小猫桥接是本地 Koishi 插件，不属于 TickNeko 平台层。它保留原始 OneBot 消息段与引用，
默认聊天仍由旧服务处理，本地工作流只负责选择路线。

## 可选预设，不是框架默认行为

`examples/ai_chat_workflow.py` 的 `build_graph(endpoint, token_env, ignored_users)`
生成一张普通工作流：`awa`、`bot ping` 命中时使用现有条件/发送节点；未命中才进入 AI 服务。
不创建全局默认路由，也不自动启用；没有这个图的用户不受影响。后续本地功能继续在画布中接分支。

可以在项目根目录执行：

```sh
python examples/ai_chat_workflow.py --endpoint http://localhost:5140/catbot/pipeline --token-env AI_SERVICE_TOKEN
```

图通过既有工作流 API 保存、提交版本、发布并开启运行开关；发布本身不代表启用。
其他启用的消息工作流仍会运行，因此迁移时应停用旧的重复回复工作流，保留其版本。
服务地址、私人屏蔽名单、凭据只在本机配置，示例不携带。

本机桥接对消息编号进行原始事件查找，保留昵称、图片段和引用，不允许工作流伪造发言人。
此桥接不支持改写消息正文；`message` 仅供服务契约中的其他实现使用。
接入部署必须先建立并验证工作流，再切换原服务的消息入口；否则没有 AI 分支时原服务不会回复。
