"""节点注册表：类型名 -> :class:`~tickneko.workflow.nodes.base.NodeSpec`。

一张**进程级、内存里**的表（不落库、没有配置文件）：包一被 import，各节点模块就自己往
这里登记一次。所以「注册一个节点」= 让那个模块被 import 到，两种写法任选：

* 写在自己的节点模块里（推荐，跟内置节点一样）：:func:`register_node` 装饰器，
  必填字段 / 默认值 / 自定义校验器 / 拓扑角色都在装饰器参数里一次声明完；
* 只有类型声明、执行器还没实现（先让画布能保存这种节点）：:func:`declare_node_type`；
* 或者运行时手工登记：:func:`register_executor`。

注册即校验规则——校验器从同一张表推导「认不认识这个类型、要查哪些字段」，新增节点类型
**不需要改校验器的任何代码**。

别人的模块怎么被 import 进来？启动时 :func:`load_node_modules` 一行搞定，不用改框架里的
任何文件::

    from tickneko.workflow import load_node_modules
    load_node_modules("my_pkg.dingtalk")     # 它自己的注册随之生效
"""
from __future__ import annotations

from collections.abc import Callable, Sequence
from importlib import import_module

from .base import (
    ConfigField,
    NodeCategory,
    NodeConfigValidator,
    NodeExecutor,
    NodeRole,
    NodeSpec,
    PortSpec,
)
from .variable_viewer import VariableViewer

#: 节点类型名 -> 注册规格（执行器 + 字段规则 + 拓扑约束）
_SPECS: dict[str, NodeSpec] = {}
_VARIABLE_VIEWERS: set[type[VariableViewer]] = set()


def register_executor(
    node_type: str,
    executor: NodeExecutor,
    *,
    fields: Sequence[ConfigField] = (),
    validator: NodeConfigValidator | None = None,
    role: NodeRole = "normal",
    min_outgoing: int = 0,
    max_outgoing: int | None = None,
    expression_field: str | None = None,
    branching: bool = False,
    label: str = "",
    color: str = "",
    order: int = 100,
    category: NodeCategory = "data",
    inputs: Sequence[PortSpec] = (),
    outputs: Sequence[PortSpec] = (),
    variable_viewer: type[VariableViewer] | None = None,
) -> None:
    """注册某类型节点的执行函数及其校验规则；重复注册覆盖。

    仅位置参数的旧写法（``register_executor("http", fn)``）照旧可用，其余规则都有默认值。
    """
    _SPECS[node_type] = NodeSpec(
        node_type=node_type,
        executor=executor,
        fields=tuple(fields),
        validator=validator,
        role=role,
        min_outgoing=min_outgoing,
        max_outgoing=max_outgoing,
        expression_field=expression_field,
        branching=branching,
        label=label,
        color=color,
        order=order,
        category=category,
        inputs=tuple(inputs),
        outputs=tuple(outputs),
        variable_viewer=variable_viewer,
    )


def declare_node_type(
    node_type: str,
    *,
    fields: Sequence[ConfigField] = (),
    validator: NodeConfigValidator | None = None,
    role: NodeRole = "normal",
    min_outgoing: int = 0,
    max_outgoing: int | None = None,
    expression_field: str | None = None,
    branching: bool = False,
    label: str = "",
    color: str = "",
    order: int = 100,
    category: NodeCategory = "data",
    inputs: Sequence[PortSpec] = (),
    outputs: Sequence[PortSpec] = (),
    variable_viewer: type[VariableViewer] | None = None,
) -> None:
    """只登记类型与校验规则、执行器留空。

    给「类型先占个位、执行器以后再写」的内置 / 扩展节点用：图能保存、能校验，真跑到它时
    运行器报「暂无执行器」。之后调用 :func:`register_executor` 补上执行函数即可。
    """
    _SPECS[node_type] = NodeSpec(
        node_type=node_type,
        executor=None,
        fields=tuple(fields),
        validator=validator,
        role=role,
        min_outgoing=min_outgoing,
        max_outgoing=max_outgoing,
        expression_field=expression_field,
        branching=branching,
        label=label,
        color=color,
        order=order,
        category=category,
        inputs=tuple(inputs),
        outputs=tuple(outputs),
        variable_viewer=variable_viewer,
    )


def register_node(
    node_type: str,
    *,
    fields: Sequence[ConfigField] = (),
    validator: NodeConfigValidator | None = None,
    role: NodeRole = "normal",
    min_outgoing: int = 0,
    max_outgoing: int | None = None,
    expression_field: str | None = None,
    branching: bool = False,
    label: str = "",
    color: str = "",
    order: int = 100,
    category: NodeCategory = "data",
    inputs: Sequence[PortSpec] = (),
    outputs: Sequence[PortSpec] = (),
    variable_viewer: type[VariableViewer] | None = None,
) -> Callable[[NodeExecutor], NodeExecutor]:
    """装饰器写法：在节点函数上标类型与规则即完成注册。

        @register_node(
            "http",
            fields=[
                ConfigField("url", "请求地址", required=True),
                ConfigField("method", "请求方法", required=True),
                ConfigField("timeout", default=10),
            ],
            validator=validate_http,
        )
        async def exec_http(node, ctx: NodeExecutionContext) -> dict[str, Any]:
            ...

    无额外参数的旧写法（``@register_node("end")``）照旧；返回原函数（不做包装），
    装饰完照样能直接调用 / 拿去测试。
    """

    def decorate(executor: NodeExecutor) -> NodeExecutor:
        register_executor(
            node_type,
            executor,
            fields=fields,
            validator=validator,
            role=role,
            min_outgoing=min_outgoing,
            max_outgoing=max_outgoing,
            expression_field=expression_field,
            branching=branching,
            label=label,
            color=color,
            order=order,
            category=category,
            inputs=inputs,
            outputs=outputs,
            variable_viewer=variable_viewer,
        )
        return executor

    return decorate


def get_spec(node_type: str) -> NodeSpec | None:
    """取某类型的注册规格；没注册返回 ``None``（校验报未知类型，运行报暂无执行器）。"""
    return _SPECS.get(node_type)


def get_executor(node_type: str) -> NodeExecutor | None:
    """取某类型节点的执行函数；没注册或只声明了类型返回 ``None``。"""
    spec = _SPECS.get(node_type)
    return spec.executor if spec is not None else None


def registered_types() -> tuple[str, ...]:
    """已登记的类型名（排序后）。排查「我那个节点到底注册上没有」时看它。"""
    return tuple(sorted(_SPECS))


def register_variable_viewer(viewer: type[VariableViewer]) -> None:
    """显式注册基础兜底；节点查看器由 NodeSpec 自动收集、同类去重。"""
    if not issubclass(viewer, VariableViewer):
        raise ValueError("查看器必须是 VariableViewer 子类")
    _VARIABLE_VIEWERS.add(viewer)


def registered_variable_viewers() -> tuple[type[VariableViewer], ...]:
    viewers = _VARIABLE_VIEWERS | {
        spec.variable_viewer for spec in _SPECS.values() if spec.variable_viewer is not None
    }
    return tuple(sorted(viewers, key=lambda viewer: (viewer.__module__, viewer.__qualname__)))


def load_node_modules(*module_names: str) -> list[str]:
    """把外部的节点模块 import 进来（它们自己的注册随之生效），返回加载的模块名。

    写的节点放在自己的包里，启动时这样装进来即可::

        load_node_modules("my_pkg.dingtalk", "my_pkg.jira")

    * 同一个模块重复加载是幂等的（命中 ``sys.modules`` 缓存，模块体只会执行一次）；
    * 模块 import 失败**直接抛出去** —— 「节点没注册上」要当场看见，别等到跑图时才报
      「暂无执行器」。
    """
    for name in module_names:
        import_module(name)
    return list(module_names)
