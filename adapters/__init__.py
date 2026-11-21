"""KarenBot 适配器层。

适配器把不同的聊天平台（QQ / Discord / Telegram / ...）抽象成统一接口。

公开 API：
    IAdapter              —— 所有适配器必须继承的抽象基类
    AdapterRegistry       —— 适配器实例的中心化管理
    EventCallback         —— 事件回调签名
    Target                —— 消息发送目标
    IncomingMessage 等    —— 统一的事件数据模型

注册自定义适配器类型：
    from adapters import register_adapter_type
    register_adapter_type("mychannel", MyChannelAdapter.from_config)
"""

from .base import (
    AdapterAPIError,
    AdapterCapabilities,
    AdapterError,
    AdapterNotConnectedError,
    EventCallback,
    IAdapter,
)
from .registry import (
    AdapterRegistry,
    build_adapter,
    known_adapter_types,
    register_adapter_type,
)
from .types import (
    AnyEvent,
    BaseEvent,
    EventType,
    FaceSegment,
    FileSegment,
    ForwardSegment,
    FriendInfo,
    GroupInfo,
    GroupMemberInfo,
    ImageSegment,
    IncomingMessage,
    IncomingNotice,
    IncomingRequest,
    MediaSegment,
    MediaType,
    MentionSegment,
    MessageSegment,
    MetaEvent,
    NoticeType,
    RawSegment,
    ReplySegment,
    RequestType,
    Target,
    TextSegment,
    UserInfo,
    VideoSegment,
    VoiceSegment,
)

__all__ = [
    # base
    "IAdapter",
    "AdapterCapabilities",
    "AdapterError",
    "AdapterAPIError",
    "AdapterNotConnectedError",
    "EventCallback",
    # registry
    "AdapterRegistry",
    "register_adapter_type",
    "build_adapter",
    "known_adapter_types",
    # types
    "Target",
    "EventType",
    "BaseEvent",
    "AnyEvent",
    "IncomingMessage",
    "IncomingNotice",
    "IncomingRequest",
    "MetaEvent",
    "NoticeType",
    "RequestType",
    "MediaSegment",
    "MediaType",
    "FriendInfo",
    "GroupInfo",
    "GroupMemberInfo",
    "UserInfo",
    # message segment IR
    "MessageSegment",
    "TextSegment",
    "MentionSegment",
    "ReplySegment",
    "ImageSegment",
    "VoiceSegment",
    "VideoSegment",
    "FileSegment",
    "FaceSegment",
    "ForwardSegment",
    "RawSegment",
]
