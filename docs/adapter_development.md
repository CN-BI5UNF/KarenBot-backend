# 渠道适配器开发指南

如何接入新的聊天平台（Discord / Telegram / 飞书 / QQ 频道等）。

---

## 核心契约

实现 `adapters/base.py` 的 `IAdapter` 抽象基类。所有适配器一视同仁。

```python
from adapters.base import AdapterCapabilities, IAdapter
from adapters.types import Target, IncomingMessage, FriendInfo, GroupInfo

class MyAdapter(IAdapter):
    def __init__(self, name: str, config: MyAdapterConfig):
        super().__init__(name)

    @property
    def capabilities(self) -> AdapterCapabilities:
        return AdapterCapabilities(
            send_image=True,
            send_voice=False,
            group_admin=False,
            ...
        )
```

`capabilities` 不是可选项：`build_default_registry()` 会据此过滤工具，
不支持的能力对应的工具不会出现在模型工具列表里。

## 必实现的方法

### 生命周期
```python
async def start(self) -> None
async def stop(self) -> None
@property
def is_connected(self) -> bool
```

### 消息发送
```python
async def send_message(self, target: Target, segments: Sequence[MessageSegment]) -> str | None
async def send_text(self, target: Target, content: str) -> str | None
async def send_image(self, target, *, image_path=None, image_url=None, image_b64=None) -> str | None
async def recall(self, message_id: str) -> bool
```

`send_message` 是新的主发送接口；基类默认只处理「单文本 / 单图片」，
新适配器应覆盖它以支持混排消息。`send_text` 继续保留，保证旧调用方可用。

### 联系人
```python
async def list_friends(self) -> list[FriendInfo]
async def list_groups(self) -> list[GroupInfo]
async def list_group_members(self, group_id: str) -> list[GroupMemberInfo]
async def get_user_info(self, user_id: str) -> UserInfo
```

### 请求
```python
async def handle_friend_request(self, flag, approve, remark="")
async def handle_group_request(self, flag, sub_type, approve, reason="")
```

### 兜底
```python
async def call_api(self, action: str, **params) -> dict
```

## 可选实现（默认抛 NotImplementedError）

```python
async def send_voice(self, target, audio_path) -> str | None
async def get_message(self, message_id) -> dict
async def send_poke(self, target, user_id) -> None
async def set_reaction(self, target, message_id, emoji_id, *, remove=False) -> None
async def group_kick(self, group_id, user_id, *, reject_add_request=False) -> None
async def group_ban(self, group_id, user_id, duration_seconds) -> None
async def group_whole_ban(self, group_id, enable) -> None
async def group_leave(self, group_id, *, dismiss=False) -> None
async def fetch_voice_text(self, message_id) -> str
async def get_file_url(self, file_id) -> str | None
async def get_image_url(self, file_id) -> str | None
async def upload_file(self, target, file_path, *, display_name=None)
async def get_forward_msg(self, forward_id) -> list[dict]
async def get_group_history(self, group_id, count=100) -> list[dict]
```

工具层不要再 `getattr(adapter, "send_voice", None)` 探测；基类方法一定存在，
不支持时抛 `NotImplementedError`，同时 `capabilities.send_voice=False` 会让
工具根本不可见。

## 事件投递

收到平台事件 → 转成 `adapters/types.py` 的统一类型 → 调 `self._emit(event)`：

```python
from adapters.types import IncomingMessage, MediaSegment, MediaType

event = IncomingMessage(
    adapter=self.name,
    timestamp=time.time(),
    self_id=str(self.bot_id),
    message_id=str(msg.id),
    scope="private",                # 或 "group"
    user_id=str(msg.user_id),
    nickname=msg.nickname,
    group_id=str(msg.group_id) if group else None,
    text=msg.text,
    raw_message=msg.raw_text,
    media=[MediaSegment(type=MediaType.IMAGE, url="https://...")],
    reply_to=str(msg.reply_to_id) if msg.reply_to_id else None,
    raw=msg.to_dict(),
)
await self._emit(event)
```

同理 `IncomingNotice`（撤回/入群）/ `IncomingRequest`（验证）/ `MetaEvent`（心跳）。

## 参考：OneBot11Adapter

`adapters/onebot11/` 是完整实现：

- `adapter.py` - IAdapter 实现（OneBot 11 标准 + 方言）
- `api_call.py` - WS echo / HTTP API 双通道
- `events.py` - JSON → 统一事件
- `segments.py` - 数组 / CQ ↔ MessageSegment IR
- `dialects.py` - NapCat / LLOneBot / Lagrange / go-cqhttp / generic 能力表
- `process.py` - 可选托管协议端进程

`adapters/transports/` 提供可复用的 WS 客户端/服务端、HTTP API、Webhook。
`adapters/napcat/` 只是兼容薄壳，新代码请直接 `from adapters.onebot11 import ...`。

## Discord 骨架示例

```python
import discord
from adapters.base import IAdapter
from adapters.types import Target, IncomingMessage, MediaSegment, MediaType

class DiscordAdapter(IAdapter):
    def __init__(self, name, config):
        super().__init__(name)
        self.client = discord.Client(intents=...)

        @self.client.event
        async def on_message(msg):
            if msg.author.bot:
                return
            event = IncomingMessage(
                adapter=self.name, timestamp=msg.created_at.timestamp(),
                self_id=str(self.client.user.id), message_id=str(msg.id),
                scope="group" if msg.guild else "private",
                user_id=str(msg.author.id), nickname=msg.author.display_name,
                group_id=str(msg.guild.id) if msg.guild else None,
                text=msg.content, raw_message=msg.content,
                media=[MediaSegment(type=MediaType.IMAGE, url=a.url)
                       for a in msg.attachments if a.content_type.startswith("image/")],
                raw=msg.to_dict(),
            )
            await self._emit(event)

    async def send_text(self, target, content):
        if target.scope == "private":
            user = await self.client.fetch_user(int(target.target_id))
            sent = await user.send(content)
        else:
            channel = self.client.get_channel(int(target.target_id))
            sent = await channel.send(content)
        return str(sent.id)
```

## CQ 码兼容

CQ 码只是 OneBot 11 的传输方言，不应泄漏到业务层。新适配器请直接消费
`MessageSegment`：`send_text` 收到的字符串最多是旧的快捷写法，建议先转成
`TextSegment`/`MentionSegment`/`ReplySegment`，再按平台原生格式编码。

OneBot 11 的完整 CQ ↔ IR 实现在 `adapters/onebot11/segments.py`，可作为参考，
但不要把 `utils/cq_parser.py` 引入非 OneBot 适配器。

## 配置 schema

`app_config/schema.py`:

```python
class MyAdapterConfig(StrictModel):
    type: Literal["my_adapter"] = "my_adapter"
    enabled: bool = True
    bot_token_id: str | None = None

AdapterConfig = Annotated[
    NapCatAdapterConfig | MyAdapterConfig,
    Field(discriminator="type"),
]
```

## 测试

```python
@pytest.fixture
def adapter():
    a = MyAdapter("test", cfg)
    a._client = FakeMyPlatform()
    return a

@pytest.mark.asyncio
async def test_send_text(adapter):
    msg_id = await adapter.send_text(Target("test", "private", "123"), "hi")
    assert msg_id == "..."
```

参考 `tests/test_onebot11_segments.py`、`tests/test_onebot11_events.py`、
`tests/test_onebot11_http.py`、`tests/test_onebot11_webhook.py`。

## 提交 PR

- ✅ 实现 `IAdapter` 所有抽象方法
- ✅ 声明 `AdapterCapabilities`，给可选方法设置正确的默认 `NotImplementedError`
- ✅ 单元测试 send / receive / 联系人 / 请求 / 能力过滤
- ✅ 配置 schema 加好并加入 `AdapterConfig` 判别联合体
- ✅ 在 `adapters/catalog.py` 登记协议卡片
- ✅ 在 `docs/protocols/` 新增协议文档（用途/配置/映射/限制/前端契约）
- ✅ 不修改 `IAdapter` 抽象（除非破坏性变更经讨论）
