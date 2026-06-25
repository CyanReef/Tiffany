"""
core 包的公开出口。

外部代码可以写：
    from core import Bot, Envelope

而不需要知道 Bot 在 core/Bot.py，Envelope 在 core/Envelope.py。
这能让 main.py 更干净，也能减少以后调整文件结构带来的影响。
"""

from .Bot import Bot
from .Context import Context
from .Envelope import Envelope
from .Hook import Hook

__all__ = ["Bot", "Context", "Envelope", "Hook"]
