"""适配器层统一数据模型 —— 屏蔽平台差异。

设计原则：
    - 所有适配器（OneBot 11 / Telegram / Discord / ...）都向上提供同一套数据模型
    - 消息正文保留原始字符串（含 CQ 码或平台特定标记），由适配器负责解析/转换
    - 结构化消息通过 MessageSegment 表达；业务层不需要理解任何平台字段
    - 媒体段同时保留 MediaSegment（旧接口），便于现有媒体管线平滑过渡
    - Event 携带原始 JSON（raw），便于调试和特殊场景兜底

跨平台扩展点：
    - 非 QQ 平台把 @ 提及映射为 MentionSegment，不再伪造 CQ 码
    - Telegram 把 reply_to 映射到 ReplySegment / reply_message_id
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Literal

# ============================================================
# 目标（发送方向）
# ============================================================


@dataclass(frozen=True, slots=True)
class Target:
    """消息发送目标。"""

    adapter: str
    """适配器名称（如 'napcat_default'）"""

    scope: Literal["private", "group", "channel"]
    """会话类型：私聊 / 群聊 / 频道（Discord、QQ 频道、Satori 等使用 channel）"""

    target_id: str
    """私聊时是用户 ID，群聊时是群 ID。统一用字符串避免大数 ID 精度问题。"""

    guild_id: str | None = None
    """频道场景的上级服务器/频道组 ID；普通私聊与群聊保持 None。"""

    thread_id: str | None = None
    """论坛/话题型平台的子线程 ID；普通会话保持 None。"""

    self_id: str | None = None
    """发送使用的机器人账号 ID（多账号协议端用）。None 表示由适配器自行决定。"""

    def __str__(self) -> str:
        return f"{self.adapter}:{self.scope}:{self.target_id}"


# ============================================================
# 结构化消息段 IR
# ============================================================


@dataclass(frozen=True, slots=True)
class TextSegment:
    type: Literal["text"] = "text"
    text: str = ""


@dataclass(frozen=True, slots=True)
class MentionSegment:
    type: Literal["mention"] = "mention"
    user_id: str = ""
    """被提及用户 ID；"all" 表示 @全体成员。"""

    display_name: str | None = None


@dataclass(frozen=True, slots=True)
class ReplySegment:
    type: Literal["reply"] = "reply"
    message_id: str = ""


@dataclass(frozen=True, slots=True)
class ImageSegment:
    type: Literal["image"] = "image"
    file_id: str | None = None
    url: str | None = None
    path: str | None = None
    b64: str | None = None
    summary: str | None = None
    """表情包/图片摘要（如有）。"""


@dataclass(frozen=True, slots=True)
class VoiceSegment:
    type: Literal["voice"] = "voice"
    file_id: str | None = None
    url: str | None = None
    path: str | None = None
    duration: float | None = None


@dataclass(frozen=True, slots=True)
class VideoSegment:
    type: Literal["video"] = "video"
    file_id: str | None = None
    url: str | None = None
    path: str | None = None


@dataclass(frozen=True, slots=True)
class FileSegment:
    type: Literal["file"] = "file"
    file_id: str | None = None
    url: str | None = None
    path: str | None = None
    name: str | None = None
    size: int | None = None


@dataclass(frozen=True, slots=True)
class FaceSegment:
    type: Literal["face"] = "face"
    face_id: str = ""
    label: str | None = None


@dataclass(frozen=True, slots=True)
class ForwardSegment:
    type: Literal["forward"] = "forward"
    forward_id: str = ""
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class RawSegment:
    """平台特有但核心无需理解的段（如 rps/dice/music/json/embed），原样透传。"""

    type: Literal["raw"] = "raw"
    platform_type: str = ""
    data: dict[str, Any] = field(default_factory=dict)
    fallback_text: str = ""


MessageSegment = (
    TextSegment
    | MentionSegment
    | ReplySegment
    | ImageSegment
    | VoiceSegment
    | VideoSegment
    | FileSegment
    | FaceSegment
    | ForwardSegment
    | RawSegment
)


# ============================================================
# 媒体段（图片/语音/文件等）
# ============================================================


class MediaType(str, Enum):
    IMAGE = "image"
    VOICE = "voice"
    VIDEO = "video"
    FILE = "file"
    FORWARD = "forward"
    FACE = "face"
    RECORD = "record"   # OneBot 语音


@dataclass(slots=True)
class MediaSegment:
    """一条消息中夹带的非文本资源。"""

    type: MediaType
    file_id: str | None = None
    """平台内部的资源 ID（如 NapCat 的 file_id）"""

    url: str | None = None
    """可直接下载的 URL（如果适配器能解析出来）"""

    name: str | None = None
    """文件名/显示名"""

    extra: dict[str, Any] = field(default_factory=dict)
    """适配器特有的附加字段"""


# ============================================================
# 事件
# ============================================================


class EventType(str, Enum):
    MESSAGE = "message"
    NOTICE = "notice"
    REQUEST = "request"
    META = "meta"


@dataclass(slots=True, kw_only=True)
class BaseEvent:
    """所有事件的基类。"""

    adapter: str
    """事件来源适配器名"""

    event_type: EventType

    timestamp: float
    """事件发生时间（Unix 秒）"""

    self_id: str
    """机器人自身 ID（如机器人 QQ 号）"""

    raw: dict[str, Any] = field(default_factory=dict)
    """适配器上报的原始 JSON，供调试和兜底使用"""


@dataclass(slots=True, kw_only=True)
class IncomingMessage(BaseEvent):
    """收到的消息事件。"""

    event_type: EventType = EventType.MESSAGE
    message_id: str
    """平台分配的消息 ID"""

    scope: Literal["private", "group", "channel"]
    user_id: str
    """发送者 ID"""

    nickname: str
    """发送者昵称（群聊优先群名片）"""

    group_id: str | None = None
    """仅群聊/频道有效"""

    thread_id: str | None = None
    """话题/论坛型群组的子线程 ID（Telegram topic、Discord thread）。"""

    text: str = ""
    """纯文本内容（去除 CQ 码后的可读文本）"""

    raw_message: str = ""
    """含 CQ 码的原始消息字符串"""

    segments: list[MessageSegment] = field(default_factory=list)
    """结构化消息段；适配器应尽可能填充，旧适配器可保持空列表。"""

    media: list[MediaSegment] = field(default_factory=list)
    """提取出的媒体段（旧接口，仍由媒体管线消费）"""

    reply_to: str | None = None
    """如果消息回复了别的消息，这里是被回复的 message_id"""

    self_sent: bool = False
    """是否为机器人自身账号发出的消息回显（OneBot message_sent）。"""

    image_assets: list[dict[str, Any]] = field(
        default_factory=list,
        init=False,
        repr=False,
    )
    """仅由内部媒体管线填充的可信图片资产引用；不从 raw/extra 复制。"""

    image_input_errors: list[dict[str, Any]] = field(
        default_factory=list,
        init=False,
        repr=False,
    )
    """入站图片失败的安全错误（code/index/message），不含 URL/base64。"""

    video_assets: list[dict[str, Any]] = field(
        default_factory=list,
        init=False,
        repr=False,
    )
    """仅由内部媒体管线填充的可信视频资产引用；不从 raw/extra 复制。"""

    video_input_errors: list[dict[str, Any]] = field(
        default_factory=list,
        init=False,
        repr=False,
    )
    """入站视频失败的安全错误（code/index/message），不含 URL/base64。"""

    def is_private(self) -> bool:
        return self.scope == "private"

    def is_group(self) -> bool:
        return self.scope == "group"

    def is_channel(self) -> bool:
        return self.scope == "channel"

    @property
    def source_target(self) -> Target:
        """消息来源对应的 Target（用于回复）。"""
        target_id = self.group_id if self.scope != "private" else self.user_id
        return Target(
            adapter=self.adapter,
            scope=self.scope,
            target_id=target_id or self.user_id,
            thread_id=self.thread_id,
            self_id=self.self_id or None,
        )


# ----- Notice 子类型 -----


class NoticeType(str, Enum):
    GROUP_RECALL = "group_recall"
    FRIEND_RECALL = "friend_recall"
    GROUP_INCREASE = "group_increase"
    GROUP_DECREASE = "group_decrease"
    GROUP_ADMIN = "group_admin"
    GROUP_UPLOAD = "group_upload"
    GROUP_BAN = "group_ban"
    FRIEND_ADD = "friend_add"
    POKE = "poke"
    LUCKY_KING = "lucky_king"
    HONOR = "honor"
    OTHER = "other"


@dataclass(slots=True, kw_only=True)
class IncomingNotice(BaseEvent):
    """通知事件（撤回/入群/退群/戳一戳等）。"""

    event_type: EventType = EventType.NOTICE
    notice_type: NoticeType
    user_id: str | None = None
    group_id: str | None = None
    operator_id: str | None = None
    """触发者 ID（如撤回操作者）"""
    message_id: str | None = None
    sub_type: str = ""
    """OneBot 原始 sub_type（如 ban / lift_ban、leave / kick、honor 类型）。"""

    target_id: str | None = None
    """通知目标（如被禁言/被踢成员、运气王）。"""

    duration: int | None = None
    """禁言时长（秒）；非禁言通知为 None。"""

    file: dict[str, Any] = field(default_factory=dict)
    """群文件上传等通知附带的文件元数据。"""


# ----- Request 子类型 -----


class RequestType(str, Enum):
    FRIEND = "friend"
    GROUP_ADD = "group_add"
    GROUP_INVITE = "group_invite"


@dataclass(slots=True, kw_only=True)
class IncomingRequest(BaseEvent):
    """请求事件（加好友/加群/邀请入群）。"""

    event_type: EventType = EventType.REQUEST
    request_type: RequestType
    flag: str
    """通过/拒绝时需要传回的标识"""

    user_id: str
    group_id: str | None = None
    comment: str = ""
    """附加消息（用户填写的验证消息）"""


@dataclass(slots=True, kw_only=True)
class MetaEvent(BaseEvent):
    """元事件：连接、心跳、生命周期等。"""

    event_type: EventType = EventType.META
    meta_type: str
    """如 'lifecycle' / 'heartbeat'"""

    sub_type: str = ""


# ----- 公共联合类型 -----


AnyEvent = IncomingMessage | IncomingNotice | IncomingRequest | MetaEvent


# ============================================================
# 联系人/群信息（统一返回结构）
# ============================================================


@dataclass(slots=True)
class FriendInfo:
    user_id: str
    nickname: str
    remark: str = ""


@dataclass(slots=True)
class GroupInfo:
    group_id: str
    group_name: str
    member_count: int = 0


@dataclass(slots=True)
class GroupMemberInfo:
    user_id: str
    nickname: str
    card: str = ""
    """群名片，没有时退回 nickname"""

    role: Literal["owner", "admin", "member"] = "member"

    @property
    def display_name(self) -> str:
        return self.card or self.nickname


@dataclass(slots=True)
class UserInfo:
    user_id: str
    nickname: str
    sex: str = "unknown"
    age: int = 0
    extra: dict[str, Any] = field(default_factory=dict)
