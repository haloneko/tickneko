# AI 服务节点

`ai-service` 是普通动作节点，不接管 Gateway，不加入全局消费标志，也不修改消息路由。
用已有 `condition` 的未命中出口连接它，就能把 AI 当默认分支。

例如：开始 → 条件（消息包含 awa）→ 满足：发送 awa → 结束；不满足：AI 服务 → 结束。
未命中的内容只请求一次；没有连接 AI 节点的分支不会请求 AI。

## 有自己的 NapCat 和 AI 服务的使用者

连接自己的 NapCat 后，在自己的工作流里配置服务地址和凭据变量名。服务地址没有默认值，
节点不绑定本机小猫、某个 QQ 账号或某个模型供应商。自己的 AI 服务继续管理模型、人设和记忆。
只给某些消息接 AI，就把 AI 节点放在对应条件的出口；希望未匹配的消息都聊天，就接在最后一个条件的未命中出口。
这两种接法都支持，不用改框架默认行为。

`response_mode` 有两种方式，需要与自己的服务约定一致：

| 方式 | 服务返回 | 谁发送 QQ 回复 | 后续接线 |
| --- | --- | --- | --- |
| `service-sends`（兼容原流程） | `{"accepted": true}` | 服务通过自己的连接发送 | AI 服务 → 结束，不再接发送节点 |
| `return-message` | `{"message": "生成的正文"}` | TickNeko 通过触发消息所属的 NapCat 发送 | AI 服务 → 发送 → 结束 |

返回正文方式的数据线：`AI 服务.message → 发送.message`，`开始.target → 发送.target`。
这样回复去向来自原始消息，不让模型猜 QQ 号，也不采用服务返回的 `target` 等字段。
AI 节点本身不调用 NapCat 发送；下游发送节点仍是框架原有节点。

服务接口不是任意模型平台 API 的通用格式。已有服务需要接受下述消息字段，并按所选方式返回 JSON；
不能直接把任意供应商网址填进去就当作接入完成。我们的 Koishi 桥接是 `service-sends` 的一个实现，
别人采用 `return-message` 不需要安装我们的 Koishi 插件。

## 没有 AI 的使用者

节点出现在目录中不代表启用 AI，也不会自动给现有或新建的工作流加 AI 分支。
只有主动配置节点、接到实际执行的分支并启用工作流，才可能投递请求。
`endpoint` 和 `token_env` 都没有默认值，缺少时是配置错误；填写变量名但没有相应凭据时，
执行前明确报「AI 服务尚未配置」，HTTP 请求数为零。不会扫描其他变量、借用本机小猫的密钥、
自动改用别的供应商或在 AI 节点失败后偷偷兜底。
不含 AI 节点的普通流程，以及没有走到 AI 的本地分支，不需要 AI 凭据，正常运行。

## 接口

POST `endpoint`，Bearer 凭据从服务器 `token_env` 指定的环境变量读取；图中只存变量名。
JSON 字段：`platform`、`chat`（group/private）、`chat_id`、`message_id`、`user_id`、`message`。
接收方应验证真实消息身份、按平台/会话/消息编号去重。
`service-sends` 返回 `{"accepted": true}`，这表示接单，不表示已经生成正文；接收方继续负责最终发送。
`return-message` 等待服务返回非空字符串 `message`，正文作为输出交给下游，不在节点内发送。
只有接单确认、空正文、非字符串正文或显式 `accepted: false` 都属于业务失败，不向下传播。
聊天历史、人设、模型备用链仍由各使用者自己的服务管理；目前正文返回方式仅支持文本。

消息编号与发言人来自本次触发，不由模型猜测；不能用于没有真实消息的定时触发。
`service-sends` 的超时只限制接单（1–60 秒），不限制模型生成；
`return-message` 的超时限制等待正文（1–300 秒）。原节点默认 15 秒，正文返回预设采用 60 秒，可按服务速度调整。
按框架既有错误机制：HTTP 拒绝、无效 JSON、无效接单结果或空正文抛 `NodeFailure`，只停止当前分支；
连接失败、超时抛 `EnvironmentFailure`，中止流程并记录一行原因；配置缺失保留普通配置异常。
不新增错误类型，不靠修改核心偷偷改路由。接单状态不确定时不自动重投，避免重复回复。

本机小猫桥接是本地 Koishi 插件，不属于 TickNeko 平台层。它保留原始 OneBot 消息段与引用，
默认聊天仍由旧服务处理，本地工作流只负责选择路线。

## 可选预设，不是框架默认行为

`examples/ai_chat_workflow.py` 的 `build_graph(endpoint, token_env, ignored_users, response_mode=...)`
生成一张普通工作流：`awa`、`bot ping` 命中时使用现有条件/发送节点；未命中才进入 AI 服务。
不创建全局默认路由，也不自动启用；没有这个图的用户不受影响。后续本地功能继续在画布中接分支。

可以在项目根目录执行：

```sh
python examples/ai_chat_workflow.py --endpoint http://localhost:5140/catbot/pipeline --token-env AI_SERVICE_TOKEN
```

自己的服务返回正文时：

```sh
python examples/ai_chat_workflow.py --endpoint http://localhost:9000/chat --token-env MY_AI_SERVICE_TOKEN --response-mode return-message
```

两种命令都只输出工作流 JSON，不会自动替任何账号创建、发布或启用工作流。

图通过既有工作流 API 保存、提交版本、发布并开启运行开关；发布本身不代表启用。
其他启用的消息工作流仍会运行，因此迁移时应停用旧的重复回复工作流，保留其版本。
服务地址、私人屏蔽名单、凭据只在本机配置，示例不携带。

本机桥接对消息编号进行原始事件查找，保留昵称、图片段和引用，不允许工作流伪造发言人。
此桥接不支持改写消息正文；`message` 仅供服务契约中的其他实现使用。
接入部署必须先建立并验证工作流，再切换原服务的消息入口；否则没有 AI 分支时原服务不会回复。
