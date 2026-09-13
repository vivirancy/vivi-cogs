"""Audit: message edits/deletes and channel/role change reporting.

Everything here goes through ``log_event``, never ``create_case`` -- the
central assertion running through this file is that none of it ever shows up
in ``stored_cases``.
"""

from __future__ import annotations

import types
import unittest

import discord

from common.log_channels import set_category_channel, set_event_channel
from common.modlog_proxy import ModLogProxy
from common.modlog_render import reason_field

from tests.helpers import FakeGroup, FakeMember, RecordingChannel
from tests.test_consumers import ConsumerTestCase

from audit.audit import Audit


class FakeAuditGuildConfig:
    def __init__(self, data: dict, writes: list) -> None:
        self._data = data
        self.log_channels = FakeGroup(data, "log_channels", writes)

    async def all(self) -> dict:
        return dict(self._data)

    async def clear_raw(self, *path) -> None:
        node = self._data
        for part in path[:-1]:
            node = node.get(str(part), {})
        node.pop(str(path[-1]), None)


class FakeAuditConfig:
    #: Matches FakeGuild.id -- every test in this file operates on one guild.
    GUILD_ID = 999

    def __init__(self) -> None:
        self.data: dict = {}
        self.writes: list = []
        self._guild = FakeAuditGuildConfig(self.data, self.writes)

    def guild(self, guild) -> FakeAuditGuildConfig:
        return self._guild

    def guild_from_id(self, guild_id) -> FakeAuditGuildConfig:
        return self._guild

    async def all_guilds(self) -> dict:
        return {self.GUILD_ID: dict(self.data)}


class FakeAuditChannel(RecordingChannel):
    """A configured destination channel, distinct from the core modlog channel."""

    def __init__(self, channel_id: int) -> None:
        super().__init__()
        self.id = channel_id
        self.mention = f"<#{channel_id}>"

    def permissions_for(self, member):
        return types.SimpleNamespace(send_messages=True, embed_links=True)


class FakeGuildChannel:
    def __init__(self, *, id, name, guild, topic=None, slowmode_delay=0, nsfw=False, category=None):
        self.id = id
        self.name = name
        self.guild = guild
        self.mention = f"<#{id}>"
        self.topic = topic
        self.slowmode_delay = slowmode_delay
        self.nsfw = nsfw
        self.category = category


class FakeRole:
    def __init__(
        self,
        *,
        id,
        name,
        guild,
        colour=discord.Colour.default(),
        hoist=False,
        mentionable=False,
        permissions=None,
    ):
        self.id = id
        self.name = name
        self.guild = guild
        self.mention = f"<@&{id}>"
        self.colour = colour
        self.hoist = hoist
        self.mentionable = mentionable
        self.permissions = permissions if permissions is not None else discord.Permissions.none()


class FakeAuditLogEntry:
    def __init__(self, *, target, user) -> None:
        self.target = target
        self.user = user


class FakeRawEdit:
    def __init__(self, *, guild_id, channel_id, message_id, data, cached_message=None) -> None:
        self.guild_id = guild_id
        self.channel_id = channel_id
        self.message_id = message_id
        self.data = data
        self.cached_message = cached_message


class FakeRawDelete:
    def __init__(self, *, guild_id, channel_id, message_id, cached_message=None) -> None:
        self.guild_id = guild_id
        self.channel_id = channel_id
        self.message_id = message_id
        self.cached_message = cached_message


class FakeCachedMessage:
    def __init__(self, *, author, content) -> None:
        self.author = author
        self.content = content


class FakeOverviewContext:
    def __init__(self, *, guild) -> None:
        self.guild = guild

    async def embed_colour(self):
        return discord.Colour.blurple()


class FakeCommandContext(FakeOverviewContext):
    """Also records replies, for the channel-configuration commands."""

    def __init__(self, *, guild) -> None:
        super().__init__(guild=guild)
        self.sent: list = []

    async def send(self, content=None, **kwargs):
        self.sent.append(content if content is not None else kwargs)


class AuditTestCase(ConsumerTestCase):
    """Extends the real-ModLog fixture with the surface Audit's listeners need."""

    async def asyncSetUp(self) -> None:
        self.cog = object.__new__(Audit)
        self.cog.bot = self.bot
        self.cog.config = FakeAuditConfig()
        self.cog.modlog = ModLogProxy(self.cog, action_types=Audit.ACTION_TYPES, core_fallback=False)
        await self.cog.modlog.refresh()

        self._channels: dict = {}
        self.guild.get_channel = lambda channel_id: self._channels.get(channel_id)
        self.guild.me.guild_permissions = types.SimpleNamespace(view_audit_log=True)
        self.bot.get_guild = lambda guild_id: self.guild if guild_id == self.guild.id else None
        self.set_audit_log_entries([])

    def add_channel(self, channel) -> None:
        self._channels[channel.id] = channel

    def set_audit_log_entries(self, entries) -> None:
        def audit_logs(*, action, limit=5):
            async def _gen():
                for entry in entries[:limit]:
                    yield entry

            return _gen()

        self.guild.audit_logs = audit_logs


class TestMessageAuditing(AuditTestCase):
    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        self.message_channel = FakeAuditChannel(111)
        self.add_channel(self.message_channel)
        await set_category_channel(self.cog.config.guild(self.guild).log_channels, "memberlog", 111)
        self.author = FakeMember(222, "someone", self.guild)
        self.author.bot = False

    @staticmethod
    def _fields(channel) -> dict:
        embed = channel.sent[-1]["embed"]
        return {field.name: field.value for field in embed.fields}

    def _edit(self, *, before, after, cached=True):
        return FakeRawEdit(
            guild_id=self.guild.id,
            channel_id=77,
            message_id=555,
            data={"content": after} if after is not None else {},
            cached_message=FakeCachedMessage(author=self.author, content=before) if cached else None,
        )

    async def test_content_changed_edit_logs_to_configured_channel(self):
        await self.cog.on_raw_message_edit(self._edit(before="old", after="new"))

        self.assertEqual(len(self.message_channel.sent), 1)
        self.assertEqual(self.stored_cases, {})

    async def test_edit_actor_field_shows_the_authors_tier_emoji(self):
        """The action type pins the field to a fixed "Member" label, but the
        emoji should still reflect the author's real tier, not go missing."""
        await self.cog.on_raw_message_edit(self._edit(before="old", after="new"))

        self.assertTrue(self._fields(self.message_channel)["Member:"].endswith("👤"))

    async def test_edit_by_an_admin_author_shows_the_admin_emoji(self):
        """The "Member" label stays fixed regardless of who the author is --
        it names the field, not the author's tier -- but the emoji must still
        track that the author happens to be an admin."""
        self.bot.admin_ids.add(self.author.id)

        await self.cog.on_raw_message_edit(self._edit(before="old", after="new"))

        self.assertTrue(self._fields(self.message_channel)["Member:"].endswith("⚔️"))

    async def test_delete_actor_field_shows_the_authors_tier_emoji(self):
        payload = FakeRawDelete(
            guild_id=self.guild.id,
            channel_id=77,
            message_id=555,
            cached_message=FakeCachedMessage(author=self.author, content="bye"),
        )

        await self.cog.on_raw_message_delete(payload)

        self.assertTrue(self._fields(self.message_channel)["Member:"].endswith("👤"))

    async def test_unchanged_content_is_a_noop(self):
        await self.cog.on_raw_message_edit(self._edit(before="same", after="same"))

        self.assertEqual(self.message_channel.sent, [])

    async def test_embed_only_update_is_a_noop(self):
        """Discord omits "content" entirely when it didn't change."""
        await self.cog.on_raw_message_edit(self._edit(before="whatever", after=None))

        self.assertEqual(self.message_channel.sent, [])

    async def test_no_channel_configured_is_a_noop(self):
        await set_category_channel(self.cog.config.guild(self.guild).log_channels, "memberlog", None)

        await self.cog.on_raw_message_edit(self._edit(before="old", after="new"))

        self.assertEqual(self.message_channel.sent, [])

    async def test_bot_author_is_ignored(self):
        self.author.bot = True

        await self.cog.on_raw_message_edit(self._edit(before="old", after="new"))

        self.assertEqual(self.message_channel.sent, [])

    async def test_delete_with_cached_message_logs(self):
        payload = FakeRawDelete(
            guild_id=self.guild.id,
            channel_id=77,
            message_id=555,
            cached_message=FakeCachedMessage(author=self.author, content="bye"),
        )

        await self.cog.on_raw_message_delete(payload)

        self.assertEqual(len(self.message_channel.sent), 1)
        self.assertEqual(self.stored_cases, {})

    async def test_delete_without_cached_message_is_a_noop(self):
        """Discord's delete event carries no author at all -- nothing to attribute this to."""
        payload = FakeRawDelete(
            guild_id=self.guild.id, channel_id=77, message_id=555, cached_message=None
        )

        await self.cog.on_raw_message_delete(payload)

        self.assertEqual(self.message_channel.sent, [])

    async def test_still_posts_when_modlog_is_absent(self):
        """log_event needs no backend at all."""
        self.bot.cogs.pop("ModLog")

        await self.cog.on_raw_message_edit(self._edit(before="old", after="new"))

        self.assertEqual(len(self.message_channel.sent), 1)


class TestChannelAuditing(AuditTestCase):
    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        self.structure_channel = FakeAuditChannel(222)
        self.add_channel(self.structure_channel)
        await set_category_channel(self.cog.config.guild(self.guild).log_channels, "adminlog", 222)

    @staticmethod
    def _fields(channel) -> dict:
        embed = channel.sent[-1]["embed"]
        return {field.name: field.value for field in embed.fields}

    async def test_create_logs(self):
        channel = FakeGuildChannel(id=1, name="general", guild=self.guild)

        await self.cog.on_guild_channel_create(channel)

        self.assertEqual(len(self.structure_channel.sent), 1)
        self.assertEqual(self.stored_cases, {})

    async def test_update_with_resolvable_actor_logs_with_actor(self):
        """The resolved actor's tier is looked up dynamically -- this one is
        registered as an admin, so the case is filed under "Admin", not a
        fixed "Actor" label."""
        before = FakeGuildChannel(id=1, name="general", guild=self.guild)
        after = FakeGuildChannel(id=1, name="general-renamed", guild=self.guild)
        actor = FakeMember(333, "admin", self.guild)
        self.bot.admin_ids.add(333)
        self.set_audit_log_entries([FakeAuditLogEntry(target=types.SimpleNamespace(id=1), user=actor)])

        await self.cog.on_guild_channel_update(before, after)

        self.assertEqual(len(self.structure_channel.sent), 1)
        self.assertTrue(self._fields(self.structure_channel)["Admin:"].endswith("⚔️"))
        self.assertEqual(self.stored_cases, {})

    async def test_update_with_unresolvable_actor_still_logs(self):
        before = FakeGuildChannel(id=1, name="general", guild=self.guild)
        after = FakeGuildChannel(id=1, name="general-renamed", guild=self.guild)
        # No audit log entries configured -- the actor cannot be resolved.

        await self.cog.on_guild_channel_update(before, after)

        self.assertEqual(len(self.structure_channel.sent), 1)
        self.assertNotIn("Actor:", self._fields(self.structure_channel))

    async def test_noop_update_produces_no_log_line(self):
        before = FakeGuildChannel(id=1, name="general", guild=self.guild)
        after = FakeGuildChannel(id=1, name="general", guild=self.guild)

        await self.cog.on_guild_channel_update(before, after)

        self.assertEqual(self.structure_channel.sent, [])

    async def test_delete_uses_the_pre_deletion_snapshot(self):
        channel = FakeGuildChannel(id=9, name="temp-channel", guild=self.guild)

        await self.cog.on_guild_channel_delete(channel)

        self.assertEqual(len(self.structure_channel.sent), 1)
        self.assertIn("temp-channel", self._fields(self.structure_channel)["Channel:"])


class TestRoleAuditing(AuditTestCase):
    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        self.structure_channel = FakeAuditChannel(222)
        self.add_channel(self.structure_channel)
        await set_category_channel(self.cog.config.guild(self.guild).log_channels, "adminlog", 222)

    @staticmethod
    def _fields(channel) -> dict:
        embed = channel.sent[-1]["embed"]
        return {field.name: field.value for field in embed.fields}

    async def test_create_logs(self):
        role = FakeRole(id=1, name="Helper", guild=self.guild)

        await self.cog.on_guild_role_create(role)

        self.assertEqual(len(self.structure_channel.sent), 1)
        self.assertEqual(self.stored_cases, {})

    async def test_update_with_resolvable_actor_logs_with_actor(self):
        """The resolved actor's tier is looked up dynamically -- this one is
        registered as an admin, so the case is filed under "Admin", not a
        fixed "Actor" label."""
        before = FakeRole(id=1, name="Helper", guild=self.guild)
        after = FakeRole(id=1, name="Helpers", guild=self.guild)
        actor = FakeMember(333, "admin", self.guild)
        self.bot.admin_ids.add(333)
        self.set_audit_log_entries([FakeAuditLogEntry(target=types.SimpleNamespace(id=1), user=actor)])

        await self.cog.on_guild_role_update(before, after)

        self.assertEqual(len(self.structure_channel.sent), 1)
        self.assertTrue(self._fields(self.structure_channel)["Admin:"].endswith("⚔️"))
        self.assertEqual(self.stored_cases, {})

    async def test_permission_diff_is_reported(self):
        before = FakeRole(
            id=1, name="Mod", guild=self.guild,
            permissions=discord.Permissions(manage_messages=True),
        )
        after = FakeRole(
            id=1, name="Mod", guild=self.guild,
            permissions=discord.Permissions(manage_messages=True, kick_members=True),
        )

        await self.cog.on_guild_role_update(before, after)

        self.assertEqual(len(self.structure_channel.sent), 1)
        self.assertIn("kick_members", self._fields(self.structure_channel)["Changes:"])

    async def test_noop_update_produces_no_log_line(self):
        before = FakeRole(id=1, name="Helper", guild=self.guild)
        after = FakeRole(id=1, name="Helper", guild=self.guild)

        await self.cog.on_guild_role_update(before, after)

        self.assertEqual(self.structure_channel.sent, [])

    async def test_delete_uses_the_pre_deletion_snapshot(self):
        role = FakeRole(id=9, name="Temp Role", guild=self.guild)

        await self.cog.on_guild_role_delete(role)

        self.assertEqual(len(self.structure_channel.sent), 1)
        self.assertIn("Temp Role", self._fields(self.structure_channel)["Role:"])


class TestMemberRoleAuditing(AuditTestCase):
    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        self.structure_channel = FakeAuditChannel(222)
        self.add_channel(self.structure_channel)
        await set_category_channel(self.cog.config.guild(self.guild).log_channels, "memberlog", 222)

    async def test_multi_role_diff_produces_separate_added_removed_fields(self):
        role_a = FakeRole(id=10, name="RoleA", guild=self.guild)
        role_b = FakeRole(id=11, name="RoleB", guild=self.guild)
        role_c = FakeRole(id=12, name="RoleC", guild=self.guild)

        before = types.SimpleNamespace(roles=[role_a, role_c])
        after = FakeMember(222, "target", self.guild)
        after.roles = [role_a, role_b]

        actor = FakeMember(333, "admin", self.guild)
        self.set_audit_log_entries(
            [FakeAuditLogEntry(target=types.SimpleNamespace(id=after.id), user=actor)]
        )

        await self.cog.on_member_update(before, after)

        self.assertEqual(len(self.structure_channel.sent), 1)
        by_name = self._fields(self.structure_channel)
        self.assertIn(role_b.mention, by_name["Roles Added:"])
        self.assertIn(role_c.mention, by_name["Roles Removed:"])
        self.assertEqual(self.stored_cases, {})

    @staticmethod
    def _fields(channel) -> dict:
        embed = channel.sent[-1]["embed"]
        return {field.name: field.value for field in embed.fields}

    async def test_no_role_change_is_a_noop(self):
        same_roles = [FakeRole(id=10, name="RoleA", guild=self.guild)]
        before = types.SimpleNamespace(roles=same_roles)
        after = FakeMember(222, "target", self.guild)
        after.roles = same_roles

        await self.cog.on_member_update(before, after)

        self.assertEqual(self.structure_channel.sent, [])

    async def test_no_channel_configured_is_a_noop(self):
        await set_category_channel(self.cog.config.guild(self.guild).log_channels, "memberlog", None)
        role_a = FakeRole(id=10, name="RoleA", guild=self.guild)
        before = types.SimpleNamespace(roles=[])
        after = FakeMember(222, "target", self.guild)
        after.roles = [role_a]

        await self.cog.on_member_update(before, after)

        self.assertEqual(self.structure_channel.sent, [])


class _OtherCog:
    """Stands in for a cog like moderation, whose real cases the overview reads."""

    qualified_name = "Other"

    def __init__(self, bot):
        self.bot = bot


OTHER_ACTION_TYPES = (
    {"type": "ban", "name": "Ban", "color": discord.Colour.red(), "emoji": "🔨"},
    {"type": "warn", "name": "Warning", "color": discord.Colour.yellow(), "emoji": "⚠️"},
)


class TestOverview(AuditTestCase):
    async def test_groups_and_counts_real_cases(self):
        other = _OtherCog(self.bot)
        other_proxy = ModLogProxy(other, action_types=OTHER_ACTION_TYPES)
        await other_proxy.refresh()

        await other_proxy.create_case(self.guild, action_type="ban", target=1, actor=2, fields=reason_field("x"))
        await other_proxy.create_case(self.guild, action_type="ban", target=3, actor=2, fields=reason_field("y"))
        await other_proxy.create_case(self.guild, action_type="warn", target=4, actor=2, fields=reason_field("z"))

        embed = await self.cog._overview_embed(FakeOverviewContext(guild=self.guild), 24)

        self.assertIn("Ban", embed.description)
        self.assertIn("Warning", embed.description)
        self.assertIn("2", embed.description)

    async def test_excludes_audits_own_log_event_activity(self):
        """log_event never creates a case, so it must never show up here."""
        channel = FakeAuditChannel(999)
        await self.cog.modlog.log_event(
            self.guild, action_type="message_edited", target=1, fields=reason_field("x"), channel=channel
        )

        embed = await self.cog._overview_embed(FakeOverviewContext(guild=self.guild), 24)

        self.assertEqual(embed.description, "No modlog case activity in this window.")

    async def test_empty_window_says_so(self):
        embed = await self.cog._overview_embed(FakeOverviewContext(guild=self.guild), 24)

        self.assertEqual(embed.description, "No modlog case activity in this window.")


class TestChannelRoutingPrecedence(AuditTestCase):
    async def test_nothing_configured_resolves_to_none(self):
        resolved = await self.cog._channel_for(self.guild, "message_edited")

        self.assertIsNone(resolved)

    async def test_category_default_is_used_when_no_override(self):
        channel = FakeAuditChannel(111)
        self.add_channel(channel)
        await set_category_channel(self.cog.config.guild(self.guild).log_channels, "memberlog", 111)

        resolved = await self.cog._channel_for(self.guild, "message_edited")

        self.assertIs(resolved, channel)

    async def test_event_override_wins_over_category(self):
        category_channel = FakeAuditChannel(111)
        event_channel = FakeAuditChannel(222)
        self.add_channel(category_channel)
        self.add_channel(event_channel)
        await set_category_channel(self.cog.config.guild(self.guild).log_channels, "memberlog", 111)
        await set_event_channel(self.cog.config.guild(self.guild).log_channels, "message_edited", 222)

        resolved = await self.cog._channel_for(self.guild, "message_edited")

        self.assertIs(resolved, event_channel)


class TestLegacyChannelMigration(AuditTestCase):
    """The one-time move from the old two-channel scheme to per-event overrides."""

    async def test_message_channel_migrates_to_its_two_events_only(self):
        self.cog.config.data["message_audit_channel_id"] = 111

        await self.cog._migrate_legacy_channels()

        log_channels = self.cog.config.guild(self.guild).log_channels
        self.assertEqual(await log_channels.get_raw("events", "message_edited", default=None), 111)
        self.assertEqual(await log_channels.get_raw("events", "message_deleted", default=None), 111)
        self.assertIsNone(await log_channels.get_raw("categories", "memberlog", default=None))
        self.assertNotIn("message_audit_channel_id", self.cog.config.data)

    async def test_structure_channel_migrates_to_all_seven_events(self):
        """Including member_roles_changed, even though its category is now
        memberlog rather than adminlog -- the old channel used to receive it,
        so the migration must keep sending it there."""
        self.cog.config.data["structure_audit_channel_id"] = 222

        await self.cog._migrate_legacy_channels()

        log_channels = self.cog.config.guild(self.guild).log_channels
        for event in (
            "channel_created", "channel_updated", "channel_deleted",
            "role_created", "role_updated", "role_deleted",
            "member_roles_changed",
        ):
            with self.subTest(event=event):
                self.assertEqual(await log_channels.get_raw("events", event, default=None), 222)
        self.assertNotIn("structure_audit_channel_id", self.cog.config.data)

    async def test_both_legacy_channels_migrate_independently(self):
        self.cog.config.data["message_audit_channel_id"] = 111
        self.cog.config.data["structure_audit_channel_id"] = 222

        await self.cog._migrate_legacy_channels()

        log_channels = self.cog.config.guild(self.guild).log_channels
        self.assertEqual(await log_channels.get_raw("events", "message_edited", default=None), 111)
        self.assertEqual(await log_channels.get_raw("events", "role_created", default=None), 222)

    async def test_migration_is_idempotent(self):
        """A second run must not clobber an override set afterward."""
        self.cog.config.data["message_audit_channel_id"] = 111
        await self.cog._migrate_legacy_channels()

        log_channels = self.cog.config.guild(self.guild).log_channels
        await set_event_channel(log_channels, "message_edited", 999)

        await self.cog._migrate_legacy_channels()

        self.assertEqual(await log_channels.get_raw("events", "message_edited", default=None), 999)

    async def test_nothing_to_migrate_is_a_noop(self):
        await self.cog._migrate_legacy_channels()  # must not raise

        self.assertEqual(self.cog.config.writes, [])

    async def test_cog_load_runs_the_migration(self):
        self.cog.config.data["message_audit_channel_id"] = 111

        await self.cog.cog_load()

        log_channels = self.cog.config.guild(self.guild).log_channels
        self.assertEqual(await log_channels.get_raw("events", "message_edited", default=None), 111)


class TestChannelCommands(AuditTestCase):
    async def test_setting_a_category_channel(self):
        channel = FakeAuditChannel(111)
        ctx = FakeCommandContext(guild=self.guild)

        await self.cog._auditset_category(ctx, "adminlog", channel)

        log_channels = self.cog.config.guild(self.guild).log_channels
        stored = await log_channels.get_raw("categories", "adminlog", default=None)
        self.assertEqual(stored, 111)
        self.assertIn("set to", ctx.sent[-1])

    async def test_clearing_a_category_channel(self):
        log_channels = self.cog.config.guild(self.guild).log_channels
        await set_category_channel(log_channels, "adminlog", 111)
        ctx = FakeCommandContext(guild=self.guild)

        await self.cog._auditset_category(ctx, "adminlog", None)

        self.assertIsNone(await log_channels.get_raw("categories", "adminlog", default=None))

    async def test_unknown_category_is_rejected(self):
        ctx = FakeCommandContext(guild=self.guild)

        # "modlog" is a valid category elsewhere, but audit never emits into
        # it -- it must not be offered as a settable option here.
        await self.cog._auditset_category(ctx, "modlog", FakeAuditChannel(1))

        self.assertIn("Unknown category", ctx.sent[-1])

    async def test_setting_an_event_channel(self):
        channel = FakeAuditChannel(222)
        ctx = FakeCommandContext(guild=self.guild)

        await self.cog._auditset_event(ctx, "role_updated", channel)

        log_channels = self.cog.config.guild(self.guild).log_channels
        stored = await log_channels.get_raw("events", "role_updated", default=None)
        self.assertEqual(stored, 222)

    async def test_unknown_event_type_is_rejected(self):
        ctx = FakeCommandContext(guild=self.guild)

        await self.cog._auditset_event(ctx, "nonsense", FakeAuditChannel(1))

        self.assertIn("Unknown event type", ctx.sent[-1])

    async def test_missing_permissions_are_reported_and_nothing_is_set(self):
        class NoPermsChannel(FakeAuditChannel):
            def permissions_for(self, member):
                return types.SimpleNamespace(send_messages=False, embed_links=True)

        ctx = FakeCommandContext(guild=self.guild)

        await self.cog._auditset_category(ctx, "adminlog", NoPermsChannel(333))

        self.assertIn("Send Messages", ctx.sent[-1])
        log_channels = self.cog.config.guild(self.guild).log_channels
        self.assertIsNone(await log_channels.get_raw("categories", "adminlog", default=None))

    async def test_settings_renders_categories_and_events(self):
        self.add_channel(FakeAuditChannel(111))
        self.add_channel(FakeAuditChannel(222))
        log_channels = self.cog.config.guild(self.guild).log_channels
        await set_category_channel(log_channels, "adminlog", 111)
        await set_event_channel(log_channels, "role_updated", 222)
        ctx = FakeCommandContext(guild=self.guild)

        await self.cog._auditset_settings(ctx)

        embed = ctx.sent[-1]["embed"]
        fields = {field.name: field.value for field in embed.fields}
        self.assertIn("<#111>", fields["Categories"])
        self.assertIn("<#222>", fields["Event overrides"])


class TestMemberJoinLeaveAuditing(AuditTestCase):
    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        self.member_channel = FakeAuditChannel(444)
        self.add_channel(self.member_channel)
        await set_category_channel(self.cog.config.guild(self.guild).log_channels, "memberlog", 444)

    @staticmethod
    def _fields(channel) -> dict:
        embed = channel.sent[-1]["embed"]
        return {field.name: field.value for field in embed.fields}

    async def test_join_logs(self):
        member = FakeMember(555, "newbie", self.guild)

        await self.cog.on_member_join(member)

        self.assertEqual(len(self.member_channel.sent), 1)
        fields = self._fields(self.member_channel)
        self.assertEqual(fields["Member:"], member.mention)
        self.assertEqual(fields["Username:"], "`newbie`")
        self.assertEqual(self.stored_cases, {})

    async def test_join_without_configured_channel_is_not_logged(self):
        self.cog.config.data.clear()
        member = FakeMember(555, "newbie", self.guild)

        await self.cog.on_member_join(member)

        self.assertEqual(self.member_channel.sent, [])

    async def test_remove_without_kick_entry_logs_no_actor(self):
        member = FakeMember(555, "someone", self.guild)

        await self.cog.on_member_remove(member)

        self.assertEqual(len(self.member_channel.sent), 1)
        fields = self._fields(self.member_channel)
        self.assertNotIn("Actor:", fields)
        self.assertEqual(fields["Member:"], member.mention)
        self.assertEqual(fields["Username:"], "`someone`")
        self.assertEqual(self.stored_cases, {})

    async def test_remove_with_kick_entry_logs_actor(self):
        member = FakeMember(555, "someone", self.guild)
        kicker = FakeMember(333, "mod", self.guild)
        self.bot.mod_ids.add(333)
        self.set_audit_log_entries([FakeAuditLogEntry(target=types.SimpleNamespace(id=555), user=kicker)])

        await self.cog.on_member_remove(member)

        self.assertEqual(len(self.member_channel.sent), 1)
        self.assertTrue(self._fields(self.member_channel)["Actor:"].endswith("🛡️"))
        self.assertEqual(self.stored_cases, {})


if __name__ == "__main__":
    unittest.main()
