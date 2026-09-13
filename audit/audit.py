from __future__ import annotations

import logging
from datetime import timedelta
from typing import List, Optional

import discord
from redbot.core import Config, commands
from redbot.core.bot import Red
from redbot.core.utils.chat_formatting import humanize_list

from ._common.log_channels import (
    missing_send_permissions,
    resolve_channel,
    set_category_channel,
    set_event_channel,
)
from ._common.modlog_proxy import ModLogProxy
from ._common.modlog_render import field

log = logging.getLogger("red.vivi-cogs.audit")

MAX_CONTENT_PREVIEW = 500


def _truncate(content: str) -> str:
    content = content or "*empty*"
    if len(content) > MAX_CONTENT_PREVIEW:
        return content[:MAX_CONTENT_PREVIEW] + "…"
    return content


class Audit(commands.Cog):
    """Watch message edits/deletes and channel/role changes, and report them.

    Everything here is reported through :meth:`ModLogProxy.log_event`, never
    `create_case`. The modlog case model is shaped around actions taken
    against a member -- an actor, a target, a reason -- and none of these
    events fit that: a channel rename has no member it happened "to", and
    member role changes already get a real case from whichever cog assigned
    the role (mute/quarantine/etc.) when it matters. Reporting all of it as
    plain log lines instead keeps `[p]cases` meaning "things that happened to
    this member" rather than a mix of that and server-structure noise.
    """

    __author__ = "vivirancy"
    __version__ = "1.0.0"

    # Declared purely so `log_event` can render a name/colour/emoji for each of
    # these -- none of them are ever passed to `create_case`.
    ACTION_TYPES = (
        {"type": "message_edited", "name": "Message Edited",
         "color": discord.Colour.orange(), "emoji": "✏️", "category": "memberlog"},
        {"type": "message_deleted", "name": "Message Deleted",
         "color": discord.Colour.red(), "emoji": "🗑️", "category": "memberlog"},
        {"type": "channel_created", "name": "Channel Created",
         "color": discord.Colour.green(), "emoji": "📁", "category": "adminlog"},
        {"type": "channel_updated", "name": "Channel Updated",
         "color": discord.Colour.blurple(), "emoji": "🛠️", "category": "adminlog"},
        {"type": "channel_deleted", "name": "Channel Deleted",
         "color": discord.Colour.red(), "emoji": "🗑️", "category": "adminlog"},
        {"type": "role_created", "name": "Role Created",
         "color": discord.Colour.green(), "emoji": "🏷️", "category": "adminlog"},
        {"type": "role_updated", "name": "Role Updated",
         "color": discord.Colour.blurple(), "emoji": "🛠️", "category": "adminlog"},
        {"type": "role_deleted", "name": "Role Deleted",
         "color": discord.Colour.red(), "emoji": "🏷️", "category": "adminlog"},
        {"type": "member_roles_changed", "name": "Member Roles Changed",
         "color": discord.Colour.blurple(), "emoji": "🧩", "category": "memberlog"},
        {"type": "member_joined", "name": "Member Joined",
         "color": discord.Colour.green(), "emoji": "📥", "category": "memberlog"},
        {"type": "member_left", "name": "Member Left",
         "color": discord.Colour.dark_orange(), "emoji": "📤", "category": "memberlog"},
    )

    #: Categories audit actually emits events into -- "modlog" is deliberately
    #: excluded, since every audit event is a non-case log line (see the class
    #: docstring), so offering it as a settable category here would be a dead
    #: option that never receives anything.
    CATEGORIES = ("adminlog", "memberlog")

    _CATEGORY_BY_TYPE = {declared["type"]: declared["category"] for declared in ACTION_TYPES}

    DEFAULT_GUILD = {
        "log_channels": {"categories": {"adminlog": None, "modlog": None, "memberlog": None}, "events": {}},
    }

    DEFAULT_MEMBER: dict = {}

    def __init__(self, bot: Red) -> None:
        self.bot = bot
        self.config = Config.get_conf(self, identifier=7108470101, force_registration=True)
        self.config.register_guild(**self.DEFAULT_GUILD)
        self.config.register_member(**self.DEFAULT_MEMBER)
        # No case this cog creates is ever posted anywhere except its own
        # dedicated channels, so there is nothing for core modlog to fall
        # back on -- `create_case` is simply never called.
        self.modlog = ModLogProxy(self, action_types=self.ACTION_TYPES, core_fallback=False)

    async def cog_load(self) -> None:
        await self._migrate_legacy_channels()
        await self.modlog.refresh()

    async def _migrate_legacy_channels(self) -> None:
        """One-time move from the old two-channel scheme to per-event overrides.

        Preserves exactly what each legacy channel used to receive: a channel
        set as ``message_audit_channel_id`` only ever got message events, and
        one set as ``structure_audit_channel_id`` only ever got channel/role
        definition changes *and* member role changes -- so both map to
        per-event overrides for precisely those types, not to a category
        default. Mapping ``structure_audit_channel_id`` to the ``adminlog``
        category default, for instance, would silently stop routing
        `member_roles_changed` there, since that type's category is
        `memberlog`.

        Idempotent: each legacy key is cleared once migrated, so a second run
        finds nothing left to do and can't clobber an override set afterward
        through `[p]auditset`.
        """
        message_events = ("message_edited", "message_deleted")
        structure_events = (
            "channel_created", "channel_updated", "channel_deleted",
            "role_created", "role_updated", "role_deleted",
            "member_roles_changed",
        )

        all_guilds = await self.config.all_guilds()

        for guild_id, data in all_guilds.items():
            message_channel_id = data.get("message_audit_channel_id")
            structure_channel_id = data.get("structure_audit_channel_id")

            if message_channel_id is None and structure_channel_id is None:
                continue

            guild_conf = self.config.guild_from_id(guild_id)

            if message_channel_id is not None:
                for event in message_events:
                    await set_event_channel(guild_conf.log_channels, event, message_channel_id)
                await guild_conf.clear_raw("message_audit_channel_id")

            if structure_channel_id is not None:
                for event in structure_events:
                    await set_event_channel(guild_conf.log_channels, event, structure_channel_id)
                await guild_conf.clear_raw("structure_audit_channel_id")

    @commands.Cog.listener()
    async def on_cog_add(self, cog: commands.Cog) -> None:
        await self.modlog.on_cog_add(cog)

    def format_help_for_context(self, ctx: commands.Context) -> str:
        return f"{super().format_help_for_context(ctx)}\n\nAuthor: {self.__author__}\nVersion: {self.__version__}"

    ### ----------------------------------------------------------------
    ### Configured channels
    ### ----------------------------------------------------------------

    async def _channel_for(self, guild: discord.Guild, action_type: str) -> Optional[discord.TextChannel]:
        return await resolve_channel(
            guild,
            self.config.guild(guild).log_channels,
            action_type=action_type,
            category=self._CATEGORY_BY_TYPE[action_type],
        )

    ### ----------------------------------------------------------------
    ### Audit-log actor resolution
    ### ----------------------------------------------------------------

    async def _find_actor(
        self, guild: discord.Guild, *, action: discord.AuditLogAction, target_id: int
    ) -> Optional[discord.abc.User]:
        """Best-effort lookup of who did this, via the audit log.

        Discord's own channel/role/member-update events never include the
        acting user. This is a single, unretried scan of the most recent
        entries for the action -- good enough for a log line, and not worth
        the complexity of retrying through Discord's replication lag.
        """
        if not guild.me.guild_permissions.view_audit_log:
            return None
        try:
            async for entry in guild.audit_logs(action=action, limit=5):
                if entry.target is not None and getattr(entry.target, "id", None) == target_id:
                    return entry.user
        except discord.Forbidden:
            return None
        return None

    ### ----------------------------------------------------------------
    ### Message edits / deletes
    ### ----------------------------------------------------------------

    @commands.Cog.listener()
    async def on_raw_message_edit(self, payload: discord.RawMessageUpdateEvent) -> None:
        if payload.guild_id is None:
            return

        guild = self.bot.get_guild(payload.guild_id)
        if guild is None:
            return

        channel = await self._channel_for(guild, "message_edited")
        if channel is None:
            return

        # Discord omits "content" entirely from the raw payload for updates
        # that didn't touch it (e.g. an embed appearing from a link unfurl).
        if "content" not in payload.data:
            return

        cached = payload.cached_message
        author_data = payload.data.get("author") or {}

        if cached is not None:
            if cached.author.bot:
                return
            after_content = payload.data.get("content", "")
            if cached.content == after_content:
                return
            content_fields = [
                field("Before", _truncate(cached.content)),
                field("After", _truncate(after_content)),
            ]
            actor = cached.author
        else:
            if author_data.get("bot"):
                return
            author_id = author_data.get("id")
            if author_id is None:
                log.debug("Edited message %s had no cached content or author; skipping.", payload.message_id)
                return
            actor = guild.get_member(int(author_id)) or int(author_id)
            content_fields = [field("Note", "Message content unavailable (not cached).")]

        source_channel = guild.get_channel(payload.channel_id)
        location = source_channel.mention if source_channel else f"<#{payload.channel_id}>"
        jump_url = f"https://discord.com/channels/{guild.id}/{payload.channel_id}/{payload.message_id}"

        fields = [
            field(f"Channel", location, inline=True),
            field("Message ID", f"`{str(payload.message_id)}`", inline=True),
            *content_fields,
            field("Jump to message", f"[Jump]({jump_url})"),
        ]

        await self.modlog.log_event(
            guild,
            action_type="message_edited",
            actor=actor,
            fields=fields,
            channel=channel,
        )

    @commands.Cog.listener()
    async def on_raw_message_delete(self, payload: discord.RawMessageDeleteEvent) -> None:
        if payload.guild_id is None:
            return

        guild = self.bot.get_guild(payload.guild_id)
        if guild is None:
            return

        channel = await self._channel_for(guild, "message_deleted")
        if channel is None:
            return

        cached = payload.cached_message
        if cached is None:
            # Discord's delete gateway event carries no author at all, so an
            # uncached delete has nothing to attribute this to.
            log.debug("Deleted message %s was not cached; skipping.", payload.message_id)
            return

        if cached.author.bot:
            return

        source_channel = guild.get_channel(payload.channel_id)
        location = source_channel.mention if source_channel else f"<#{payload.channel_id}>"
        content = _truncate(cached.content) if cached.content else "*No text content.*"

        # No "Jump to message" here, unlike edits -- a deleted message's link
        # no longer resolves to anything.
        await self.modlog.log_event(
            guild,
            action_type="message_deleted",
            actor=cached.author,
            fields=[
                field(f"Channel", location, inline=True),
                field("Message ID", f"`{str(payload.message_id)}`", inline=True),
                field("Content", f"{content}"),
            ],
            channel=channel,
        )

    # Note: channel purges (`on_raw_bulk_message_delete`) are not handled --
    # logging one line per deleted message would flood the audit channel on
    # every purge. Left as a known gap for now.

    ### ----------------------------------------------------------------
    ### Channel changes
    ### ----------------------------------------------------------------

    @staticmethod
    def _channel_diff(before: discord.abc.GuildChannel, after: discord.abc.GuildChannel) -> List[str]:
        changes = []

        if before.name != after.name:
            changes.append(f"Name: `{before.name}` → `{after.name}`")

        before_topic = getattr(before, "topic", None)
        after_topic = getattr(after, "topic", None)
        if before_topic != after_topic:
            changes.append(f"Topic: `{before_topic or '*none*'}` → `{after_topic or '*none*'}`")

        before_slowmode = getattr(before, "slowmode_delay", None)
        after_slowmode = getattr(after, "slowmode_delay", None)
        if before_slowmode != after_slowmode:
            changes.append(f"Slowmode: `{before_slowmode or 0}s` → `{after_slowmode or 0}s`")

        before_nsfw = getattr(before, "nsfw", None)
        after_nsfw = getattr(after, "nsfw", None)
        if before_nsfw != after_nsfw:
            changes.append(f"NSFW: `{before_nsfw}` → `{after_nsfw}`")

        before_category = getattr(before, "category", None)
        after_category = getattr(after, "category", None)
        if before_category != after_category:
            changes.append(
                f"Category: `{before_category.name if before_category else '*none*'}` → "
                f"`{after_category.name if after_category else '*none*'}`"
            )

        return changes

    @commands.Cog.listener()
    async def on_guild_channel_create(self, channel: discord.abc.GuildChannel) -> None:
        log_channel = await self._channel_for(channel.guild, "channel_created")
        if log_channel is None:
            return

        actor = await self._find_actor(
            channel.guild, action=discord.AuditLogAction.channel_create, target_id=channel.id
        )

        await self.modlog.log_event(
            channel.guild,
            action_type="channel_created",
            actor=actor,
            fields=[
                field("Channel", f"{channel.mention}", inline=True),
                field("Channel ID", f"`{channel.id}`", inline=True)
            ],
            channel=log_channel,
        )

    @commands.Cog.listener()
    async def on_guild_channel_update(
        self, before: discord.abc.GuildChannel, after: discord.abc.GuildChannel
    ) -> None:
        log_channel = await self._channel_for(after.guild, "channel_updated")
        if log_channel is None:
            return

        diff = self._channel_diff(before, after)
        if not diff:
            return

        actor = await self._find_actor(
            after.guild, action=discord.AuditLogAction.channel_update, target_id=after.id
        )

        await self.modlog.log_event(
            after.guild,
            action_type="channel_updated",
            actor=actor,
            fields=[
                field("Channel", f"{after.mention}", inline=True),
                field("Channel ID", f"`{after.id}`", inline=True),
                field("Changes", "\n".join(diff)),
            ],
            channel=log_channel,
        )

    @commands.Cog.listener()
    async def on_guild_channel_delete(self, channel: discord.abc.GuildChannel) -> None:
        log_channel = await self._channel_for(channel.guild, "channel_deleted")
        if log_channel is None:
            return

        actor = await self._find_actor(
            channel.guild, action=discord.AuditLogAction.channel_delete, target_id=channel.id
        )

        await self.modlog.log_event(
            channel.guild,
            action_type="channel_deleted",
            actor=actor,
            fields=[
                field("Channel", f"`#{channel.name}`", inline=True),
                field("Channel ID", f"`{channel.id}`", inline=True)
            ],
            channel=log_channel,
        )

    ### ----------------------------------------------------------------
    ### Role-definition changes
    ### ----------------------------------------------------------------

    @staticmethod
    def _role_diff(before: discord.Role, after: discord.Role) -> List[str]:
        changes = []

        if before.name != after.name:
            changes.append(f"Name: `{before.name}` → `{after.name}`")

        if before.colour != after.colour:
            changes.append(f"Colour: `{before.colour}` → `{after.colour}`")

        if before.hoist != after.hoist:
            changes.append(f"Hoisted: `{before.hoist}` → `{after.hoist}`")

        if before.mentionable != after.mentionable:
            changes.append(f"Mentionable: `{before.mentionable}` → `{after.mentionable}`")

        if before.permissions != after.permissions:
            before_perms = dict(before.permissions)
            after_perms = dict(after.permissions)
            granted = [name for name, value in after_perms.items() if value and not before_perms[name]]
            revoked = [name for name, value in before_perms.items() if value and not after_perms[name]]
            if granted:
                changes.append(f"Permissions granted: {humanize_list(granted)}")
            if revoked:
                changes.append(f"Permissions removed: {humanize_list(revoked)}")

        return changes

    @commands.Cog.listener()
    async def on_guild_role_create(self, role: discord.Role) -> None:
        log_channel = await self._channel_for(role.guild, "role_created")
        if log_channel is None:
            return

        actor = await self._find_actor(
            role.guild, action=discord.AuditLogAction.role_create, target_id=role.id
        )

        await self.modlog.log_event(
            role.guild,
            action_type="role_created",
            actor=actor,
            fields=[
                field("Role", f"{role.mention}", inline=True),
                field("Role ID", f"`{role.id}`", inline=True),
            ],
            channel=log_channel,
        )

    @commands.Cog.listener()
    async def on_guild_role_update(self, before: discord.Role, after: discord.Role) -> None:
        log_channel = await self._channel_for(after.guild, "role_updated")
        if log_channel is None:
            return

        diff = self._role_diff(before, after)
        if not diff:
            return

        actor = await self._find_actor(
            after.guild, action=discord.AuditLogAction.role_update, target_id=after.id
        )

        await self.modlog.log_event(
            after.guild,
            action_type="role_updated",
            actor=actor,
            fields=[
                field("Role", f"{after.mention}", inline=True),
                field("Role ID", f"`{after.id}`", inline=True),
                field("Changes", "\n".join(diff)),
            ],
            channel=log_channel,
        )

    @commands.Cog.listener()
    async def on_guild_role_delete(self, role: discord.Role) -> None:
        log_channel = await self._channel_for(role.guild, "role_deleted")
        if log_channel is None:
            return

        actor = await self._find_actor(
            role.guild, action=discord.AuditLogAction.role_delete, target_id=role.id
        )

        await self.modlog.log_event(
            role.guild,
            action_type="role_deleted",
            actor=actor,
            fields=[
                field("Role", f"{role.mention}", inline=True),
                field("Role ID", f"`{role.id}`", inline=True),
            ],
            channel=log_channel,
        )

    ### ----------------------------------------------------------------
    ### Member role grants / revokes
    ### ----------------------------------------------------------------

    @commands.Cog.listener()
    async def on_member_update(self, before: discord.Member, after: discord.Member) -> None:
        if before.roles == after.roles:
            return

        log_channel = await self._channel_for(after.guild, "member_roles_changed")
        if log_channel is None:
            return

        before_ids = {role.id for role in before.roles}
        after_ids = {role.id for role in after.roles}
        added = [role for role in after.roles if role.id not in before_ids]
        removed = [role for role in before.roles if role.id not in after_ids]

        if not added and not removed:
            return

        actor = await self._find_actor(
            after.guild, action=discord.AuditLogAction.member_role_update, target_id=after.id
        )

        fields = []
        if added:
            fields.append(field("Roles Added", humanize_list([role.mention for role in added])))
        if removed:
            fields.append(field("Roles Removed", humanize_list([role.mention for role in removed])))

        await self.modlog.log_event(
            after.guild,
            action_type="member_roles_changed",
            target=after,
            actor=actor,
            fields=fields,
            channel=log_channel,
        )

    ### ----------------------------------------------------------------
    ### Member joins / leaves
    ### ----------------------------------------------------------------

    @commands.Cog.listener()
    async def on_member_join(self, member: discord.Member) -> None:
        log_channel = await self._channel_for(member.guild, "member_joined")
        if log_channel is None:
            return

        await self.modlog.log_event(
            member.guild,
            action_type="member_joined",
            actor=member,
            fields=[
                field("Member", member.mention, inline=True),
                field("Username", f"`{member.name}`", inline=True),
                field("Member ID", f"`{member.id}`", inline=True),
                field("Account Created", discord.utils.format_dt(member.created_at, "R"), inline=True),
                field("Member Count", f"`{member.guild.member_count}`", inline=True),
            ],
            channel=log_channel,
        )

    @commands.Cog.listener()
    async def on_member_remove(self, member: discord.Member) -> None:
        log_channel = await self._channel_for(member.guild, "member_left")
        if log_channel is None:
            return

        actor = await self._find_actor(
            member.guild, action=discord.AuditLogAction.kick, target_id=member.id
        )

        joined = discord.utils.format_dt(member.joined_at, "R") if member.joined_at else "*unknown*"

        await self.modlog.log_event(
            member.guild,
            action_type="member_left",
            actor=actor,
            fields=[
                field("Member", member.mention, inline=True),
                field("Username", f"`{member.name}`", inline=True),
                field("Member ID", f"`{member.id}`", inline=True),
                field("Joined", joined, inline=True),
                field("Member Count", f"`{member.guild.member_count}`", inline=True),
            ],
            channel=log_channel,
        )

    ### ----------------------------------------------------------------
    ### Configuration commands
    ### ----------------------------------------------------------------

    async def _set_log_channel(
        self, ctx: commands.Context, *, channel: Optional[discord.TextChannel], label: str, setter
    ) -> None:
        guild = ctx.guild

        if channel is None:
            await setter(None)
            await ctx.send(f"{label} channel cleared. Those events will no longer be logged.")
            return

        missing = await missing_send_permissions(guild, channel)
        if missing:
            await ctx.send(f"I need {humanize_list(missing)} in {channel.mention} first.")
            return

        await setter(channel.id)
        await ctx.send(f"{label} channel set to {channel.mention}.")

    @commands.guild_only()
    @commands.admin_or_permissions(manage_guild=True)
    @commands.group(name="auditset")
    async def auditset(self, ctx: commands.Context) -> None:
        """Configure the Audit cog."""

    @auditset.command(name="category")
    async def auditset_category(
        self, ctx: commands.Context, category: str, channel: Optional[discord.TextChannel] = None
    ) -> None:
        """Set (or clear) the default channel for a category of event (adminlog/memberlog)."""
        await self._auditset_category(ctx, category, channel)

    async def _auditset_category(
        self, ctx: commands.Context, category: str, channel: Optional[discord.TextChannel]
    ) -> None:
        category = category.lower()

        if category not in self.CATEGORIES:
            await ctx.send(f"Unknown category `{category}`. Choose one of: {humanize_list(list(self.CATEGORIES))}.")
            return

        guild = ctx.guild

        async def setter(channel_id: Optional[int]) -> None:
            await set_category_channel(self.config.guild(guild).log_channels, category, channel_id)

        await self._set_log_channel(ctx, channel=channel, label=f"`{category}`", setter=setter)

    @auditset.command(name="event")
    async def auditset_event(
        self, ctx: commands.Context, event_type: str, channel: Optional[discord.TextChannel] = None
    ) -> None:
        """Set (or clear) the channel a specific event type is logged to."""
        await self._auditset_event(ctx, event_type, channel)

    async def _auditset_event(
        self, ctx: commands.Context, event_type: str, channel: Optional[discord.TextChannel]
    ) -> None:
        if event_type not in self._CATEGORY_BY_TYPE:
            known = humanize_list(sorted(self._CATEGORY_BY_TYPE))
            await ctx.send(f"Unknown event type `{event_type}`. Choose one of: {known}.")
            return

        guild = ctx.guild

        async def setter(channel_id: Optional[int]) -> None:
            await set_event_channel(self.config.guild(guild).log_channels, event_type, channel_id)

        await self._set_log_channel(ctx, channel=channel, label=f"`{event_type}`", setter=setter)

    @auditset.command(name="settings")
    async def auditset_settings(self, ctx: commands.Context) -> None:
        """Show the current configuration."""
        await self._auditset_settings(ctx)

    async def _auditset_settings(self, ctx: commands.Context) -> None:
        conf = await self.config.guild(ctx.guild).log_channels()

        def render_channel(channel_id: Optional[int]) -> str:
            channel = ctx.guild.get_channel(channel_id) if channel_id else None
            return channel.mention if channel else "*not set*"

        embed = discord.Embed(title="Audit settings", colour=await ctx.embed_colour())
        embed.add_field(
            name="Categories",
            value="\n".join(
                f"`{category}`: {render_channel(conf['categories'].get(category))}" for category in self.CATEGORIES
            ),
            inline=False,
        )

        events = conf.get("events", {})
        embed.add_field(
            name="Event overrides",
            value=(
                "\n".join(f"`{event}`: {render_channel(channel_id)}" for event, channel_id in sorted(events.items()))
                if events
                else "*none set*"
            ),
            inline=False,
        )

        await ctx.send(embed=embed)

    ### ----------------------------------------------------------------
    ### Overview
    ### ----------------------------------------------------------------

    @commands.guild_only()
    @commands.mod_or_permissions(manage_guild=True)
    @commands.group(name="audit")
    async def audit(self, ctx: commands.Context) -> None:
        """View audit information for this server."""

    async def _overview_embed(self, ctx: commands.Context, hours: int) -> discord.Embed:
        since = discord.utils.utcnow() - timedelta(hours=hours)
        cases = await self.modlog.recent_cases(ctx.guild, since=since)

        embed = discord.Embed(
            title=f"Overview — last {hours}h",
            colour=await ctx.embed_colour(),
        )

        if not cases:
            embed.description = "No modlog case activity in this window."
        else:
            counts: dict = {}
            for case in cases:
                counts[case.action_name] = counts.get(case.action_name, 0) + 1
            embed.description = "\n".join(
                f"**{name}:** {count}" for name, count in sorted(counts.items(), key=lambda item: -item[1])
            )
            embed.set_footer(text=f"{len(cases)} case(s) total")

        embed.add_field(
            name="Not shown here",
            value=(
                "Message edits/deletes and channel/role changes aren't case-shaped and "
                "are logged separately — see this server's configured audit channels."
            ),
            inline=False,
        )

        return embed

    @audit.command(name="overview")
    async def audit_overview(self, ctx: commands.Context, hours: int = 24) -> None:
        """Summarize real modlog case activity from the last `hours` hours (1-168).

        This does not include audit's own event log (message edits/deletes,
        channel/role changes) -- those were never case-shaped to begin with,
        and live in their own configured channels instead.
        """
        if not 1 <= hours <= 168:
            await ctx.send("Pick a number of hours between 1 and 168 (a week).")
            return

        embed = await self._overview_embed(ctx, hours)
        await ctx.send(embed=embed)
