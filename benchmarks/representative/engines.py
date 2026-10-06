"""Native parsers and dispatchers with benchmark-owned business handlers.

The AstrBot subclass only replaces handlers registered by our historical setup
helper, adds native filters, and changes the input. It never replaces a pipeline
stage or framework dispatcher.
"""
import asyncio
import sys

from benchmarks.compare import AstrBotEngine as AstrBotBase
from benchmarks.representative.cases import parts, payload


class TiffanyEngine:
    async def setup(self, case, consumer):
        from core import Bot, Envelope
        from adapters.onebot_fields import detect_event_kind, register_onebot_fields
        from fields import TEXT

        self.case, self.Envelope, self.detect = case, Envelope, detect_event_kind
        self.pieces = parts(case)
        self.text = "".join(self.pieces)
        self.bot = Bot()
        register_onebot_fields(self.bot, "onebot")

        async def handle(ctx):
            for _ in range(case.get("reads", 1)):
                text = ctx.resolve(TEXT)
            if case.get("burst"):
                raw = ctx.raw
                await consumer.accept(text, raw["message_id"], raw["user_id"] - 20000)
            else:
                await consumer.accept(text)

        async def unrelated(ctx):
            consumer.unmatched_calls += 1

        def reject(ctx):
            consumer.predicate_calls += 1
            return False

        for index in range(case["matching"]):
            self.bot.register_hook(handle, name=f"matching_{index}", on="message")
        for index in range(case.get("unmatched", 0)):
            self.bot.register_hook(unrelated, name=f"notice_{index}", on="notice")
        for index in range(case.get("predicates", 0)):
            self.bot.register_hook(unrelated, name=f"predicate_{index}", on="message", when=reject)
        if case.get("http"):
            scheduler = self.bot.runtime.scheduler
            scheduler.global_limit = case["sessions"]
            scheduler.configure_adapter("benchmark", active_limit=case["sessions"])
        await self.bot.__aenter__()

    async def emit(self, index, session):
        raw = payload(self.case, index, session, pieces=self.pieces, text=self.text)
        await self.bot.emit(self.Envelope("onebot", raw, kind=self.detect(raw), adapter_id="benchmark",
                                        connection_id="offline", session_id=f"private:{raw['user_id']}"))

    async def close(self):
        await self.bot.__aexit__(None, None, None)


class NoneBotEngine:
    initialized = False

    async def setup(self, case, consumer):
        import nonebot
        from nonebot.adapters.onebot.v11 import Adapter, Bot, MessageEvent
        from nonebot.matcher import matchers
        from nonebot.log import logger

        self.case = case
        self.pieces = parts(case)
        self.text = "".join(self.pieces)
        if not type(self).initialized:
            nonebot.init(driver="~none+~httpx", log_level="ERROR", _env_file=None)
            type(self).initialized = True
        logger.remove()
        logger.add(sys.stderr, level="ERROR")
        matchers.clear()
        self.adapter = Adapter(nonebot.get_driver())
        self.bot = Bot(self.adapter, "12345")

        async def handle(event):
            for _ in range(case.get("reads", 1)):
                text = event.get_plaintext()
            if case.get("burst"):
                await consumer.accept(text, event.message_id, event.user_id - 20000)
            else:
                await consumer.accept(text)

        handle.__annotations__["event"] = MessageEvent

        async def unrelated():
            consumer.unmatched_calls += 1

        async def reject():
            consumer.predicate_calls += 1
            return False

        for _ in range(case["matching"]):
            nonebot.on_message(priority=1, block=False).handle()(handle)
        for _ in range(case.get("unmatched", 0)):
            nonebot.on_notice(priority=1, block=False).handle()(unrelated)
        for _ in range(case.get("predicates", 0)):
            nonebot.on_message(rule=reject, priority=1, block=False).handle()(unrelated)

    async def emit(self, index, session):
        event = self.adapter.json_to_event(payload(self.case, index, session, pieces=self.pieces, text=self.text))
        assert event is not None, "NoneBot rejected the message"
        await self.bot.handle_event(event)

    async def close(self):
        from nonebot.matcher import matchers
        matchers.clear()


class AstrBotEngine(AstrBotBase):
    async def setup(self, case, consumer):
        self.case = case
        self.pieces = parts(case)
        self.text = "".join(self.pieces)
        await super().setup(dict(case, unmatched=case.get("unmatched", 0)), consumer)
        from astrbot.core.star.star_handler import EventType, star_handlers_registry
        from astrbot.core.star.register.star_handler import get_handler_or_create
        from astrbot.core.star.filter.custom_filter import CustomFilter

        async def handle(event):
            for _ in range(case.get("reads", 1)):
                text = event.message_str
            if case.get("burst"):
                message = event.message_obj
                await consumer.accept(text, int(message.message_id), int(message.sender.user_id) - 20000)
            else:
                await consumer.accept(text)

        # Replace only our generated business handlers, retaining native metadata.
        for metadata in star_handlers_registry.get_handlers_by_event_type(EventType.AdapterMessageEvent):
            metadata.handler = handle

        class FalseFilter(CustomFilter):
            def __init__(self):
                super().__init__(raise_error=False)

            def filter(self, event, cfg):
                consumer.predicate_calls += 1
                return False

        def unrelated_at(index):
            async def unrelated(event):
                consumer.unmatched_calls += 1
            unrelated.__name__ = f"false_predicate_{index}"
            unrelated.__module__ = "tiffany_comparison_plugin"
            return unrelated

        for index in range(case.get("predicates", 0)):
            metadata = get_handler_or_create(unrelated_at(index), EventType.AdapterMessageEvent)
            metadata.event_filters.append(FalseFilter())

    async def event(self, index, session):
        message = await self.adapter.convert_message(self.Event(
            payload(self.case, index, session, pieces=self.pieces, text=self.text)))
        assert message is not None, "AstrBot rejected the message"
        return self.MessageEvent(message.message_str, message, self.adapter.metadata,
                                 message.session_id, self.adapter.bot)


ENGINES = {"tiffany": TiffanyEngine, "nonebot": NoneBotEngine, "astrbot": AstrBotEngine}
