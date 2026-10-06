"""工作流节点：**一类节点一个文件**，文件里 import 契约 -> 写函数 -> 当场注册。

    nodes/
      base.py            契约：NodeExecutor / NodeSpec / ConfigField / PortSpec / 运行时上下文
      port_types.py      端口类型定义表（唯一常改的地方：画布配色 / 图例 / 数据流语义都从它来）
      registry.py        注册表：register_node / declare_node_type / get_spec / load_node_modules
      triggers.py        内置节点：三个触发器（trigger-message 消息 / trigger-time 定时 / trigger-event 事件）
      end.py             内置节点：end（图终点）
      log.py             内置节点：log（按级别写业务日志）
      test.py            内置节点：test（调试：回显入口的值到日志，画布联调用）
      constant.py        内置节点：constant（一个节点一个常量值，从 value 端口送下去）
      http.py            内置节点：http（发一次 HTTP 请求，需要可选依赖 httpx）
      delay.py           内置节点：delay（异步等待：触发进 / 触发出，秒数可接线覆盖手填）
      json.py            内置节点：json（解析 JSON 文本 + 点路径取值，取不到送空串不打断流程）
      regex.py           内置节点：regex（正则提取 / 替换；抽不到送空串不打断流程）
      now.py             内置节点：now（当前时间：strftime 格式文本 + Unix 时间戳）
      condition.py       内置节点：condition（条件分支：true / false 双出口，引擎按选中出口剪枝）
      send.py            内置节点：send（把 message 发到 target 指向的会话：去向走 target 值端口 + 内容端口，走 ctx.gateway.reply；没有 target 就不发，回执不成功不打断流程）
      operator.py        内置节点：operator（算术：+ - * / %，结果文本化；算不出来送空串）
      cache.py           内置节点：cache（变量存取：get / set；作用域账号 / 图，前缀区分）
      ds_dict.py         内置节点：ds-dict（数据结构字典：缓存里的一份字典变量，new/set/get/contains/remove/keys/length）
      ds_container.py    内置节点：ds-container（复合数据结构：缓存里的一份 list/map 容器变量，顺序取用 + 存在检测）
      placeholder.py     内置节点：placeholder（占位：只透传不做事，参与画布理线）

**数据沿连线走**：上游的输出端口 -> 下游的输入端口，值由执行引擎按边投递，没有全局变量。

**写自己的节点**（不用改校验器 / 框架里的任何文件）：新建一个模块，用装饰器把
「执行函数 + 端口 + 字段 + 自定义校验器」一次声明完，启动时 import 进来即可::

    # my_pkg/dingtalk.py
    from tickneko.workflow.nodes import (
        ConfigField, NodeExecutionContext, PortSpec, input_value, register_node,
    )

    @register_node(
        "dingtalk",
        inputs=[PortSpec("text", "message", "消息内容", required=True)],  # 入口：接线或手填
        outputs=[PortSpec("sent", "message", "是否发出")],
        fields=[ConfigField("text", "消息内容")],  # 没接线时的手填兜底
    )
    async def exec_dingtalk(node, ctx: NodeExecutionContext) -> dict[str, object]:
        text = input_value(node, ctx, "text")
        ctx.logger.info("发钉钉消息", node_id=node.id, text=text)
        return {"sent": True}  # 键 = 输出端口名

    # 启动时（app.py 或自己的入口）
    from tickneko.workflow import load_node_modules
    load_node_modules("my_pkg.dingtalk")

类型**注册即合法**：校验器从同一张注册表推导「认不认识这个类型、要查哪些字段」，
不再维护任何框架侧白名单。

完整指南（契约、上下文、命名、失败语义、孤儿节点、可选依赖、测试写法）见
``docs/workflow/workflow.md`` 第 5 节。
"""

from __future__ import annotations

from .base import (
    CATEGORY_LABELS,
    MISSING_DEFAULT,
    NO_USER_ID,
    TRIGGER_PORT,
    ConfigField,
    NodeCategory,
    NodeConfigValidator,
    EnvironmentFailure,
    NodeExecutionContext,
    NodeExecutor,
    NodeFailure,
    NodeRole,
    NodeSpec,
    PortSpec,
    input_value,
)
from .cache import exec_cache
from .ds_dict import exec_ds_dict
from .ds_container import exec_ds_container
from .ai_service import exec_ai_service
from .port_types import PORT_TYPES, PortType, PortTypeDef
from .condition import exec_condition
from .constant import exec_constant
from .delay import exec_delay
from .end import exec_end
from .http import HTTP_METHODS, exec_http
from .json import exec_json
from .log import LOG_LEVELS, exec_log
from .now import exec_now
from .operator import exec_operator
from .pack import exec_pack_kook, exec_pack_onebot
from .placeholder import exec_placeholder
from .regex import exec_regex
from .send import exec_send
from .unpack import exec_unpack_kook, exec_unpack_onebot
from .registry import (
    declare_node_type,
    get_executor,
    get_spec,
    load_node_modules,
    register_executor,
    register_node,
    registered_types,
)
from .test import exec_test
from .triggers import (
    EVENT_TYPE_LABELS,
    EVENT_TYPE_OPTIONS,
    EVENT_TYPES,
    exec_trigger_event,
    exec_trigger_message,
    exec_trigger_time,
    validate_event_type,
    validate_time_cron,
    workflow_task_id,
)

__all__ = [
    # 契约（写节点用这些）
    "NodeExecutor",
    "NodeExecutionContext",
    "NodeFailure",
    "EnvironmentFailure",
    "NodeSpec",
    "NodeRole",
    "NodeCategory",
    "CATEGORY_LABELS",
    "NodeConfigValidator",
    "ConfigField",
    "PortSpec",
    "PortType",
    "PortTypeDef",
    "PORT_TYPES",
    "TRIGGER_PORT",
    "MISSING_DEFAULT",
    "NO_USER_ID",
    "input_value",
    # 注册表
    "register_executor",
    "register_node",
    "declare_node_type",
    "get_executor",
    "get_spec",
    "registered_types",
    "load_node_modules",
    # 内置节点：import 上面那些模块即完成注册，函数本身也导出（复用 / 测试 / 换实现）
    "exec_trigger_message",
    "exec_trigger_time",
    "exec_trigger_event",
    "exec_ai_service",
    "validate_time_cron",
    "validate_event_type",
    "EVENT_TYPE_OPTIONS",
    "EVENT_TYPE_LABELS",
    "EVENT_TYPES",
    "workflow_task_id",
    "exec_end",
    "exec_log",
    "LOG_LEVELS",
    "exec_constant",
    "exec_test",
    "exec_http",
    "HTTP_METHODS",
    "exec_delay",
    "exec_json",
    "exec_regex",
    "exec_now",
    "exec_condition",
    "exec_send",
    "exec_unpack_onebot",
    "exec_unpack_kook",
    "exec_pack_onebot",
    "exec_pack_kook",
    "exec_placeholder",
    "exec_operator",
    "exec_cache",
    "exec_ds_dict",
    "exec_ds_container",
]
