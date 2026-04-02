from discord.ext import commands
import discord
import config


def is_fixer():
    async def predicate(ctx):
        # in-guild messages
        if isinstance(ctx.author, discord.Member):
            if any(r.id == config.FIXER_ROLE_ID for r in ctx.author.roles):
                return True
            raise commands.CheckFailure("Fixer role required")

        # DMs or thread-posts: fetch member object from main guild
        guild = ctx.bot.get_guild(config.GUILD_ID)
        if not guild:
            raise commands.CheckFailure("Fixer role required")

        member = guild.get_member(ctx.author.id)
        if not member:
            try:
                member = await guild.fetch_member(ctx.author.id)
            except discord.NotFound:
                raise commands.CheckFailure("Fixer role required")

        if any(r.id == config.FIXER_ROLE_ID for r in member.roles):
            return True
        raise commands.CheckFailure("Fixer role required")

    return commands.check(predicate)

def is_cs_approver():
    """Check that the command author has the CS Approver role."""

    async def predicate(ctx):
        guild = ctx.bot.get_guild(config.GUILD_ID)
        if not guild:
            raise commands.CheckFailure("CS Approver role required")

        member = ctx.author
        if not isinstance(member, discord.Member):
            member = guild.get_member(ctx.author.id)
            if not member:
                try:
                    member = await guild.fetch_member(ctx.author.id)
                except discord.NotFound:
                    raise commands.CheckFailure("CS Approver role required")

        if any(r.id == config.CS_APPROVER_ROLE_ID for r in getattr(member, "roles", [])):
            return True
        raise commands.CheckFailure("CS Approver role required")

    return commands.check(predicate)


def is_ripperdoc():
    """Check that the command author has the Ripperdoc role."""

    async def predicate(ctx):
        guild = ctx.bot.get_guild(config.GUILD_ID)
        if not guild:
            raise commands.CheckFailure("Ripperdoc role required")

        member = ctx.author
        if not isinstance(member, discord.Member):
            member = guild.get_member(ctx.author.id)
            if not member:
                try:
                    member = await guild.fetch_member(ctx.author.id)
                except discord.NotFound:
                    raise commands.CheckFailure("Ripperdoc role required")

        if any(r.id == config.RIPPERDOC_ROLE_ID for r in getattr(member, "roles", [])):
            return True
        raise commands.CheckFailure("Ripperdoc role required")

    return commands.check(predicate)


def is_store_owner():
    """Check that the command author has the Wholesaler Store role (gun store owner)."""

    async def predicate(ctx):
        guild = ctx.bot.get_guild(config.GUILD_ID)
        if not guild:
            raise commands.CheckFailure("Gun store owner role required")

        member = ctx.author
        if not isinstance(member, discord.Member):
            member = guild.get_member(ctx.author.id)
            if not member:
                try:
                    member = await guild.fetch_member(ctx.author.id)
                except discord.NotFound:
                    raise commands.CheckFailure("Gun store owner role required")

        raw = config.WHOLESALER_STORE_ROLE_IDS
        store_ids = {int(raw)} if isinstance(raw, (int, float, str)) and str(raw).strip().isdigit() else {int(x) for x in raw}
        if any(r.id in store_ids for r in getattr(member, "roles", [])):
            return True
        raise commands.CheckFailure("Gun store owner role required")

    return commands.check(predicate)
