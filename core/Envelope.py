"""
Envelope 是框架接收到外部消息后的第一层包装。

它的目标不是解析消息，而是“保留现场”：
NapCat 发来的原始 JSON 长什么样，这里就尽量原样保存。

为什么要有 Envelope，而不是直接把 raw dict 到处传？
1. 以后支持多个平台时，可以用 platform 标记消息来自哪里。
2. 业务 hook 不需要知道 socket、HTTP、Webhook 等接入细节。
3. raw 永远保留，方便调试，也方便以后处理平台特有字段。
"""

from dataclasses import dataclass
from typing import Any


@dataclass(slots=True)
class Envelope:
    """
    原始消息信封。

    platform:
        消息来自哪个平台。现在是 "napcat"，以后可以是 "qq_official"、"telegram" 等。

    raw:
        平台发来的原始消息字典。这里不做全量解析，是整个轻量框架的关键。
        只有 hook 真的需要某个字段时，Context 才会从 raw 里按需读取。

    为什么用 dataclass？
        Envelope 只是一个简单数据容器，用 dataclass 可以少写 __init__。

    为什么用 slots=True？
        slots 会减少每个对象的内存开销，也防止随手给对象塞不存在的字段。
        对机器人这种高频消息场景，这是一个便宜但有用的小优化。
    """

    platform: str
    raw: dict[str, Any]
