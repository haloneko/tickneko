"""端口类型定义 —— **唯一要改的地方**。

画布能连什么类型的线、每种类型什么色、是不是数据端口，全由 :data:`PORT_TYPES` 决定。
加/改端口类型**只改这个文件**：目录接口 ``port_types`` 把它下发到前端（见
``api/workflow/responses.py``），画布配色 / 面板图例 / 接线语义自动跟着变，前端零改动。

``PortType`` 是给节点端口声明做类型检查用的 Literal（``PortSpec("text", "message", ...)``），
**必须与 :data:`PORT_TYPES` 的键保持一致** —— 加了新类型记得两边同步。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

#: 端口类型字面量（给 :class:`PortSpec` 声明做静态检查），键与 :data:`PORT_TYPES` 一致。
#: 语义：trigger 控制流（决定何时执行下一节点）；message / target / list / dict 数据流
#: （内容 / 会话定位 / 列表容器 / 字典容器，均原样投递）；generic 透传泛型（输入什么
#: 输出什么，只接数据流端口，见 :func:`port_types_compatible`）。
PortType = Literal["trigger", "message", "target", "list", "dict", "generic"]


@dataclass(frozen=True)
class PortTypeDef:
    """一种端口类型的展示信息：画布图例 / 端口配色 / 「是否数据端口」全从它来。

    目录接口 ``port_types`` 把它下发给前端 —— 加端口类型只改下面的 :data:`PORT_TYPES`，
    画布不用动。``data=True`` 表示沿边送值（数据流端口），``data=False`` 只表达先后
    （trigger）。泛型不需要额外标记字段：接线时按类型名 ``generic`` 判断（见
    :func:`port_types_compatible`）。
    """

    type: str
    label: str
    color: str
    data: bool = True


#: 端口类型定义表（顺序即目录接口里 ``port_types`` 的顺序）。
#: 画布配色 / 图例 / 数据流语义都跟着它走，别在前端再抄一份。
PORT_TYPES: dict[str, PortTypeDef] = {
    "trigger": PortTypeDef("trigger", "触发（控制流）", "#22c55e", data=False),
    "message": PortTypeDef("message", "消息（数据流）", "#3b82f6"),
    "target": PortTypeDef("target", "会话定位（target）", "#f59e0b"),
    "list": PortTypeDef("list", "列表（数据流）", "#a855f7"),
    "dict": PortTypeDef("dict", "字典（数据流）", "#06b6d4"),
    "generic": PortTypeDef("generic", "透传（泛型）", "#64748b"),
}


def port_types_compatible(source_type: str, target_type: str) -> bool:
    """两种端口能否互接：**同类互通；generic 与任何数据流端口互接**（不接触发）；其余
    不同类不放行。泛型按类型名 ``generic`` 判断，认不出的类型不放行 —— 宁可挡下。
    前端画布用同一份语义（``catalog.ts`` 的 ``portCompatible``）。
    """
    if source_type == target_type:
        return True
    if source_type == "generic":
        target_def = PORT_TYPES.get(target_type)
        return bool(target_def and target_def.data)
    if target_type == "generic":
        source_def = PORT_TYPES.get(source_type)
        return bool(source_def and source_def.data)
    return False
