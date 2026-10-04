"""OneBot 适配器：把 :class:`~tickneko.platforms.onebot.OneBotServer` 包成第一个 ``BotAdapter``。

本模块是 bridge 里**唯一**允许 import ``tickneko.platforms.onebot`` 的地方（包 ``__init__``
不碰它，没装 ``tickneko[onebot]`` 照样能用模型 / 协议 / 总线）。包一层、不改一层 ——
``tickneko/platforms/onebot/`` 一行不动，这里做三件事：**事件翻译**（``OneBotEvent`` ->
:class:`~tickneko.platforms.bridge.models.PlatformEvent`，心跳不用滤，服务端 ``_emit`` 调
handler 前已滤）、**能力转述**（``clients()`` / ``send()``）、**兼容面**（roster / kick /
revoke_by_id / set_token_enabled / tokens / connections 原样透传，接口层 ``OneBotLike``
协议由本适配器结构化满足，装配时注到原注入点即可，下游零改动）。详见
``docs/bridge/bridge.md``。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from tickneko.core.logger import BaseLogger
from tickneko.platforms.onebot import OneBotOptions, OneBotServer
from tickneko.platforms.onebot.models import (
    ActionResponse,
    MessageEvent,
    MetaEvent,
    NoticeEvent,
    OneBotEvent,
    RequestEvent,
)
from tickneko.platforms.onebot.server import ClientEntry, OneBotConnection

from .gateway import EventSubscriber
from .logging import bridge_logger
from .models import ActionResult, BotClient, ChatKind, ChatTarget, PlatformEvent

if TYPE_CHECKING:
    from tickneko.api.api.onebot.protocols import TokenRegistry


#: 本适配器的平台标识（路由键；Gateway 里不得与其它适配器重复）
PLATFORM = "onebot"


@dataclass(frozen=True)
class OneBotTarget:
    """OneBot 的会话定位（回程地址）：号一律**整数**（OneBot 协议口径）。

    OneBot 的群号 / 用户号 / 消息号在协议里就是整数，target 直接原样存 —— 回复时
    不用像统一 ``EventTarget`` 时代那样「翻译成字符串、再 int() 转回来」。生产与消费
    同平台（见 :class:`ChatTarget`）：本类是适配器翻译事件时构造、塞进
    ``PlatformEvent.target`` 的，reply 时原样传回本适配器，字段形状自己认。
    """

    #: 这条连接属于谁（回复发给「谁的」连接）
    owner_id: str
    #: 事件来源平台（回复时按它路由回原适配器）
    platform: str = PLATFORM
    #: 会话指向：群聊 / 私聊；``"other"`` 说明定位不出会话，回不了
    chat: ChatKind = "other"
    #: 群号（群聊）；没有是 ``None``
    group_id: int | None = None
    #: 对方用户号（私聊）；没有是 ``None``
    user_id: int | None = None
    #: 消息号（撤回一类动作要用）；没有是 ``None``
    message_id: int | None = None


def _translate(conn: OneBotConnection, event: OneBotEvent) -> PlatformEvent:
    """一条平台事件 -> 规范化事件（翻译不了的字段整条挂在 ``raw`` 上兜底）。"""
    self_id = str(event.self_id) if event.self_id else ""
    if isinstance(event, MessageEvent):
        chat = event.message_type  # "private" / "group"，恰好就是归一口径
        chat_id = str(event.group_id) if event.message_type == "group" else str(event.user_id)
        return PlatformEvent(
            platform=PLATFORM,
            owner_id=conn.id,
            self_id=self_id,
            kind="message",
            chat=chat,
            chat_id=chat_id,
            user_id=str(event.user_id),
            text=event.raw_message,
            message_id=str(event.message_id) if event.message_id is not None else "",
            time=float(event.time),
            raw=event,
            target=OneBotTarget(
                owner_id=conn.id,
                chat=chat,
                group_id=int(event.group_id) if event.message_type == "group" else None,
                user_id=int(event.user_id),
                message_id=int(event.message_id) if event.message_id is not None else None,
            ),
        )
    if isinstance(event, NoticeEvent):
        # 通知：群通知带 group_id 算群事件，其余算不出会话指向
        has_group = event.group_id is not None
        # 撤回类通知（friend_recall / group_msg_recall）带 message_id：NoticeEvent 没声明
        # 这字段，但 extra="allow" 的额外字段可属性访问——取到就带上，下游不用下探 raw
        recall_id = getattr(event, "message_id", None)
        return PlatformEvent(
            platform=PLATFORM,
            owner_id=conn.id,
            self_id=self_id,
            kind="notice",
            event_type=event.notice_type,  # 平台原生类型（poke / group_increase / friend_recall …）
            chat="group" if has_group else "other",
            chat_id=str(event.group_id) if has_group else "",
            user_id=str(event.user_id) if event.user_id is not None else "",
            message_id=str(recall_id) if recall_id is not None else "",
            time=float(event.time),
            raw=event,
            target=(
                OneBotTarget(
                    owner_id=conn.id,
                    chat="group",
                    group_id=int(event.group_id),
                    user_id=int(event.user_id) if event.user_id is not None else None,
                    message_id=int(recall_id) if recall_id is not None else None,
                )
                if has_group
                else None
            ),
        )
    if isinstance(event, RequestEvent):
        has_group = event.group_id is not None
        return PlatformEvent(
            platform=PLATFORM,
            owner_id=conn.id,
            self_id=self_id,
            kind="request",
            event_type=event.request_type,  # 平台原生类型（friend / group）
            chat="group" if has_group else "private",
            chat_id=str(event.group_id) if has_group else "",
            user_id=str(event.user_id) if event.user_id is not None else "",
            text=event.comment,
            time=float(event.time),
            raw=event,
            target=OneBotTarget(
                owner_id=conn.id,
                chat="group" if has_group else "private",
                group_id=int(event.group_id) if has_group else None,
                user_id=int(event.user_id) if event.user_id is not None else None,
            ),
        )
    if isinstance(event, MetaEvent):
        # 生命周期一类，没有会话指向；文本留空
        return PlatformEvent(
            platform=PLATFORM,
            owner_id=conn.id,
            self_id=self_id,
            kind="meta",
            time=float(event.time),
            raw=event,
        )
    # 四类穷举完还到不了这里：OneBotEvent 联合扩了新类别而翻译没跟上——宁可当场炸，
    # 也别静默归成 meta（python -O 下 assert 会被整条剥掉，靠不住）
    raise TypeError(f"未认识的 OneBot 事件类型：{type(event).__name__}")


def _client(entry: ClientEntry) -> BotClient:
    """在线列表一行 -> 规范化一行（身份统一字符串口径）。"""
    return BotClient(
        client_id=entry.client_id,
        owner_id=entry.id,
        account=entry.account,
        self_id="" if entry.self_id is None else str(entry.self_id),
        remote=entry.remote,
        connected_at=entry.connected_at,
    )


class OneBotAdapter:
    """OneBot 平台适配器：包一个 ``OneBotServer``，实现 ``BotAdapter`` + 兼容面。

    服务端是**自己建的**（构造时把 ``handler=self._on_event`` 装进去）：``OneBotServer``
    的事件钩子在构造时定死，事后没有换钩子的口子——与其让调用方先建好服务端再传进来
    （还得叮嘱它别配 handler），不如适配器连建带包一步到位，``.server`` 属性留给要下探
    平台细节的场合。
    """

    def __init__(
        self,
        options: OneBotOptions | None = None,
        *,
        publish: EventSubscriber | None = None,
        tokens: TokenRegistry | None = None,
        logger: BaseLogger | None = None,
    ) -> None:
        """
        :param options: 监听地址 / 路径 / 动作超时（原样交给 ``OneBotServer``）；
        :param publish: Gateway 的投递口（``gateway.publish``）：没给（或事后才建 Gateway）
            事件只记 debug 丢弃——不抛，适配器自己也能单测；
        :param tokens: 令牌注册表（原样交给 ``OneBotServer``，鉴权 / 归属都在那边）；
        :param logger: 业务日志实例，默认 ``bridge`` 那个。
        """
        self._publish: EventSubscriber | None = publish
        self._log: BaseLogger = logger if logger is not None else bridge_logger()
        self._server: OneBotServer = OneBotServer(
            options, handler=self._on_event, tokens=tokens
        )

    #: 平台标识（BotAdapter 协议的路由键）
    platform: str = PLATFORM

    @property
    def server(self) -> OneBotServer:
        """被包的 OneBot 服务端（下探平台细节用的口子；常规能力走本适配器）。"""
        return self._server

    # ------------------------------------------------------------------ 事件翻译
    async def _on_event(self, conn: OneBotConnection, event: OneBotEvent) -> None:
        """服务端事件钩子：翻译成规范化事件投给 Gateway。

        心跳到不了这里（服务端 ``_emit`` 已滤）；本方法抛不出的异常由服务端兜底记日志。
        """
        publish = self._publish
        if publish is None:
            self._log.debug(
                "事件没有投递口，已丢弃",
                platform=PLATFORM,
                owner_id=conn.id,
                post_type=event.post_type,
            )
            return
        await publish(_translate(conn, event))

    # ------------------------------------------------------------------ BotAdapter 协议
    async def start(self) -> None:
        """开始监听（幂等，语义对齐 ``OneBotServer.start``）。"""
        await self._server.start()

    async def stop(self) -> None:
        """停服：关监听并断开所有客户端（幂等）。"""
        await self._server.stop()

    def clients(self, *, owner_id: str | None = None) -> tuple[BotClient, ...]:
        """在线列表快照（规范化口径）；给 ``owner_id`` 就只看那个归属下的。"""
        return tuple(_client(entry) for entry in self._server.roster(id=owner_id))

    async def send(self, owner_id: str, action: str, /, **params: object) -> ActionResult:
        """给 ``owner_id`` 的在线连接发一个动作并等回执。

        挑连接的规则：归属匹配 + 取最近连上的那条。参数原样转述（群号 / 用户号转整数
        是**调用方**的事）。

        :raises ConnectionError: 这个归属下没有在线连接（环境问题当场抛）。
        """
        matches = [conn for conn in self._server.connections if conn.id == owner_id]
        if not matches:
            online = "、".join(
                sorted({conn.id or "(匿名)" for conn in self._server.connections})
            ) or "无"
            raise ConnectionError(
                f"[onebot] 没有归属 {owner_id or '(空)'} 的在线连接（当前在线：{online}）"
            )
        conn = max(matches, key=lambda item: item.connected_at)
        response: ActionResponse = await conn.call(action, **params)
        return ActionResult(
            ok=response.ok,
            message="" if response.ok else f"status={response.status} retcode={response.retcode}",
            data=response.data,
            raw=response,
        )

    async def reply(self, target: ChatTarget, content: str) -> ActionResult:
        """回复到 ``target`` 指向的会话：群聊回群、私聊回私聊（号已是整数，直接用）。

        ``target`` 是**本适配器**翻译事件时构造的 :class:`OneBotTarget`（生产与消费
        同平台，见 :class:`ChatTarget`），回复时原样传回即可；不是本平台的 target
        当场 ValueError（装配错位看得见）。
        """
        if not isinstance(target, OneBotTarget):
            raise ValueError(
                f"回复目标不是 OneBot 的 target（{type(target).__name__}），"
                "生产与消费必须同平台"
            )
        if target.chat == "group":
            if target.group_id is None:
                raise ValueError("群聊回复需要 group_id（会话定位缺群号）")
            return await self.send(
                target.owner_id,
                "send_group_msg",
                group_id=target.group_id,
                message=content,
            )
        if target.chat == "private":
            if target.user_id is None:
                raise ValueError("私聊回复需要 user_id（会话定位缺对方账号）")
            return await self.send(
                target.owner_id,
                "send_private_msg",
                user_id=target.user_id,
                message=content,
            )
        raise ValueError(f"会话定位的会话指向不明（chat={target.chat!r}），回不了")

    def make_target(
        self,
        *,
        owner_id: str,
        chat: str = "other",
        chat_id: str = "",
        user_id: str = "",
        message_id: str = "",
    ) -> ChatTarget:
        """从通用会话字段构造 OneBot 的回程地址（号转**整数**，协议口径）。

        画布上手动填的会话号是字符串（表单都是文本），这里按会话指向转回整数：
        群聊用 ``chat_id`` 当群号、私聊用 ``user_id``（缺省回退 ``chat_id``）当对方账号。
        """
        if chat == "group":
            group_id = int(chat_id) if chat_id else None
            target_user_id = int(user_id) if user_id else None
        else:
            group_id = None
            target_user_id = int(user_id or chat_id) if (user_id or chat_id) else None
        return OneBotTarget(
            owner_id=owner_id,
            chat=chat,
            group_id=group_id,
            user_id=target_user_id,
            message_id=int(message_id) if message_id else None,
        )

    # ------------------------------------------------------------------ 兼容面（透传）
    # 接口层 OneBotLike 协议，P2 验收线：下游零改动。
    @property
    def tokens(self) -> TokenRegistry | None:
        """令牌注册表（没配就是 ``None`` = 不校验）。"""
        return self._server.tokens

    @property
    def connections(self) -> tuple[OneBotConnection, ...]:
        """当前连着的客户端（快照）；供观测 / 测试用。"""
        return self._server.connections

    def roster(self, *, id: str | None = None) -> tuple[ClientEntry, ...]:
        """在线客户端列表（快照，**平台口径**：``self_id`` 仍是 ``int | None``）。"""
        return self._server.roster(id=id)

    async def kick(self, client_id: str, *, revoke: bool = False) -> bool:
        """踢掉一个客户端；``revoke=True`` 连令牌一起吊销。"""
        return await self._server.kick(client_id, revoke=revoke)

    async def revoke_by_id(self, token_id: str) -> bool:
        """吊销一个令牌（按记录 id），并把正用它连着的客户端断开。"""
        return await self._server.revoke_by_id(token_id)

    async def set_token_enabled(self, token_id: str, enabled: bool) -> bool:
        """启用 / 停用一条令牌；停用会连同断开正用它连着的客户端。"""
        return await self._server.set_token_enabled(token_id, enabled)

    async def serve_forever(self) -> None:
        """起服务并一直等到被停（bootstrap 的主协程停在这；对齐 ``OneBotServer``）。"""
        await self._server.serve_forever()
