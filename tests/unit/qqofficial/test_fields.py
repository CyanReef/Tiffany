import copy
import unittest

from adapters.qqofficial_fields import (
    ATTACHMENTS, CHANNEL_ID, EVENT_ID, GUILD_ID, detect_event_kind, register_qqofficial_fields,
)
from core import Bot, Context, Envelope
from fields import GROUP_ID, MESSAGE_ID, MESSAGE_TYPE, SELF_ID, TEXT, USER_ID


class QQOfficialFieldTests(unittest.TestCase):
    def setUp(self):
        self.bot = Bot()
        self.self_id = "ready-user-id"
        register_qqofficial_fields(self.bot, "qq_official", lambda: self.self_id)

    def context(self, raw):
        return Context(Envelope("qq_official", raw, kind="message"), self.bot.providers)

    def test_group_fields_are_lazy_cached_and_preserve_original_payload(self):
        raw = {"op": 0, "s": 123, "t": "GROUP_AT_MESSAGE_CREATE", "d": {
            "id": "message!001", "content": " /ping ", "group_openid": "group-001",
            "author": {"member_openid": "member-001"}, "attachments": [{"url": "untouched"}],
        }}
        original = copy.deepcopy(raw)
        ctx = self.context(raw)
        self.assertFalse(ctx.has(TEXT))
        self.assertEqual(ctx.resolve(TEXT), "/ping")
        self.assertEqual(ctx.resolve(MESSAGE_TYPE), "group")
        self.assertEqual(ctx.resolve(USER_ID), "member-001")
        self.assertEqual(ctx.resolve(GROUP_ID), "group-001")
        self.assertEqual(ctx.resolve(MESSAGE_ID), "message!001")
        self.assertEqual(ctx.resolve(SELF_ID), "ready-user-id")
        self.assertEqual(raw, original)
        self.self_id = "later-id"
        self.assertEqual(ctx.resolve(SELF_ID), "ready-user-id")

    def test_c2c_and_missing_fields_have_safe_native_values(self):
        ctx = self.context({"t": "C2C_MESSAGE_CREATE", "d": {"author": {"user_openid": "0000123"}}})
        self.assertEqual(ctx.resolve(USER_ID), "0000123")
        self.assertEqual(ctx.resolve(MESSAGE_TYPE), "private")
        self.assertIsNone(ctx.resolve(GROUP_ID))
        self.assertIsNone(ctx.resolve(MESSAGE_ID))
        self.assertEqual(ctx.resolve(TEXT), "")
        malformed = self.context({"d": []})
        self.assertIsNone(malformed.resolve(USER_ID))

    def test_routing_only_probes_event_name(self):
        for name in ("GROUP_AT_MESSAGE_CREATE", "GROUP_MESSAGE_CREATE", "C2C_MESSAGE_CREATE",
                     "AT_MESSAGE_CREATE", "MESSAGE_CREATE", "DIRECT_MESSAGE_CREATE"):
            self.assertEqual(detect_event_kind({"t": name}), "message")
        self.assertEqual(detect_event_kind({"t": "FRIEND_ADD"}), "notice")
        self.assertEqual(detect_event_kind({"t": "FUTURE_EVENT"}), "event")
        self.assertEqual(detect_event_kind({"t": "MESSAGE_AUDIT_PASS"}), "notice")
        self.assertEqual(detect_event_kind({"t": "INTERACTION_CREATE"}), "event")

    def test_channel_fields_strip_only_leading_self_mention(self):
        for prefix in ("<@!ready-user-id>", "<@ready-user-id>"):
            raw = {"t": "AT_MESSAGE_CREATE", "id": "dispatch", "d": {
                "id": "message", "author": {"id": "0001"}, "channel_id": "channel",
                "guild_id": "guild", "content": prefix + " ping <@other>",
                "attachments": [{"url": "https://unused.invalid"}],
            }}
            original = copy.deepcopy(raw)
            ctx = self.context(raw)
            self.assertEqual(ctx.resolve(TEXT), "ping <@other>")
            self.assertEqual(ctx.resolve(MESSAGE_TYPE), "channel")
            self.assertEqual(ctx.resolve(USER_ID), "0001")
            self.assertIsNone(ctx.resolve(GROUP_ID))
            self.assertEqual(ctx.resolve(CHANNEL_ID), "channel")
            self.assertEqual(ctx.resolve(GUILD_ID), "guild")
            self.assertEqual(ctx.resolve(EVENT_ID), "dispatch")
            self.assertIs(ctx.resolve(ATTACHMENTS), raw["d"]["attachments"])
            self.assertEqual(raw, original)
        ctx = self.context({"t": "AT_MESSAGE_CREATE", "d": {"content": "<@other> ping"}})
        self.assertEqual(ctx.resolve(TEXT), "<@other> ping")
        direct = self.context({"t": "DIRECT_MESSAGE_CREATE", "d": {"author": {"id": "sender"}}})
        self.assertEqual(direct.resolve(MESSAGE_TYPE), "direct")
        self.assertEqual(direct.resolve(USER_ID), "sender")

    def test_lifecycle_and_interaction_ids_are_not_message_ids(self):
        cases = [
            ("GROUP_ADD_ROBOT", {"group_openid": "group", "op_member_openid": "operator"}, "group", "operator"),
            ("FRIEND_ADD", {"openid": "friend"}, "private", "friend"),
            ("INTERACTION_CREATE", {"id": "interaction", "chat_type": 1,
                                    "group_openid": "group", "group_member_openid": "member"}, "group", "member"),
            ("INTERACTION_CREATE", {"scene": "c2c", "user_openid": "user"}, "private", "user"),
            ("INTERACTION_CREATE", {"chat_type": 0, "data": {"resolved": {"user_id": "user"}}}, "channel", "user"),
        ]
        for event, data, scene, user in cases:
            with self.subTest(event=event, scene=scene):
                ctx = self.context({"t": event, "id": "outer", "d": data})
                self.assertEqual(ctx.resolve(MESSAGE_TYPE), scene)
                self.assertEqual(ctx.resolve(USER_ID), user)
                self.assertIsNone(ctx.resolve(MESSAGE_ID))
                self.assertEqual(ctx.resolve(EVENT_ID), "outer")
        ctx = self.context({"t": "INTERACTION_CREATE", "d": {"id": "inner"}})
        self.assertEqual(ctx.resolve(EVENT_ID), "inner")
        self.assertEqual(ctx.resolve(ATTACHMENTS), [])

    def test_teardown_handles_revoke_only_qq_fields(self):
        bot = Bot()
        from adapters.onebot_fields import register_onebot_fields
        register_onebot_fields(bot, "napcat")
        handles = register_qqofficial_fields(bot, "qq_official", lambda: None)
        for handle in handles:
            handle.revoke()
        self.assertEqual(Context(Envelope("napcat", {"user_id": 12}), bot.providers).resolve(USER_ID), 12)
        with self.assertRaises(LookupError):
            bot.providers.get(TEXT, "qq_official")

    def test_second_account_does_not_revoke_first_accounts_providers(self):
        handles = self.bot.providers.handles_for_owner("protocol.qqofficial")
        with self.assertRaisesRegex(ValueError, "one QQ official account"):
            register_qqofficial_fields(self.bot, "qq_official", lambda: "other-bot")
        self.assertTrue(all(handle.active for handle in handles))
        self.assertEqual(self.context({}).resolve(SELF_ID), "ready-user-id")
