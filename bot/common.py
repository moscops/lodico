"""여러 cog 가 함께 쓰는 권한 체크, 자동완성, 조회 헬퍼."""
from __future__ import annotations

from typing import TYPE_CHECKING

import discord
from discord import app_commands

from .db import Character, Member
from .render import session_label
from .services import SessionView
from .timeutil import now_kst, to_epoch

if TYPE_CHECKING:
    from .main import RaidBot

# /도움 분류. 순서가 곧 출력 순서.
CAT_START = "처음 시작하기"
CAT_VIEW = "일정 확인"
CAT_INPUT = "입력·수정"
CAT_LEADER = "공대장 전용"
CATEGORIES = [CAT_START, CAT_VIEW, CAT_INPUT, CAT_LEADER]


def meta(category: str, example: str, detail: str = "", leader: bool = False) -> dict:
    """명령어 extras. /도움 이 이 값으로 자동 생성된다."""
    return {"category": category, "example": example, "detail": detail, "leader": leader}


def bot_of(interaction: discord.Interaction) -> "RaidBot":
    return interaction.client  # type: ignore[return-value]


def is_leader(interaction: discord.Interaction) -> bool:
    user = interaction.user
    if not isinstance(user, discord.Member):
        return False
    if user.guild_permissions.manage_guild:
        return True
    settings = bot_of(interaction).db.get_settings(user.guild.id)
    if settings.leader_role_id is None:
        return False
    if settings.leader_role_id == user.guild.id:  # @everyone 역할 = 누구나
        return True
    return any(r.id == settings.leader_role_id for r in user.roles)


def leader_only():
    async def predicate(interaction: discord.Interaction) -> bool:
        if not is_leader(interaction):
            raise app_commands.CheckFailure(
                "공대장만 쓸 수 있는 명령어예요. (서버 관리 권한 또는 `/봇설정 공대장역할` 로 지정된 역할)"
            )
        return True

    return app_commands.check(predicate)


def ensure_member(interaction: discord.Interaction) -> Member:
    assert interaction.guild_id is not None
    return bot_of(interaction).db.upsert_member(
        interaction.guild_id, interaction.user.id, interaction.user.display_name
    )


# ---- 자동완성 -------------------------------------------------------------

async def raid_autocomplete(interaction: discord.Interaction, current: str):
    cfg = bot_of(interaction).cfg
    today = now_kst().date()
    # 진행 중인 이벤트 레이드를 맨 위에, 끝난 이벤트는 숨김
    raids = sorted((r for r in cfg.raids.values() if r.is_active(today)), key=lambda r: not r.event)
    return [
        app_commands.Choice(name=f"{r.name} ({r.party_size}인, {r.min_item_level:g}+)"[:100], value=r.key)
        for r in raids
        if current.lower() in r.name.lower() or current.lower() in r.key
    ][:25]


async def event_raid_autocomplete(interaction: discord.Interaction, current: str):
    cfg = bot_of(interaction).cfg
    today = now_kst().date()
    return [
        app_commands.Choice(name=r.name, value=r.key)
        for r in cfg.raids.values()
        if r.event and r.is_active(today) and current in r.name
    ][:25]


def upcoming_views(interaction: discord.Interaction, days: int = 14) -> list[SessionView]:
    bot = bot_of(interaction)
    now = now_kst()
    sessions = bot.db.list_sessions(
        interaction.guild_id or 0, to_epoch(now) - 3 * 3600, to_epoch(now) + days * 86400
    )
    return [SessionView(s, bot.cfg.raid(s.raid_key), bot.db.session_characters(s.id)) for s in sessions]


async def session_autocomplete(interaction: discord.Interaction, current: str):
    choices = []
    for v in upcoming_views(interaction):
        label = session_label(v)
        if current.strip("#") in label:
            choices.append(app_commands.Choice(name=label, value=v.session.id))
    return choices[:25]


async def my_character_autocomplete(interaction: discord.Interaction, current: str):
    bot = bot_of(interaction)
    member = bot.db.get_member(interaction.guild_id or 0, interaction.user.id)
    if member is None:
        return []
    chars = bot.db.list_characters(member.id)
    # 일정이 이미 선택돼 있으면 입장 가능한 캐릭터만
    session_id = getattr(interaction.namespace, "일정", None)
    session = bot.db.get_session(session_id) if isinstance(session_id, int) else None
    raid = bot.cfg.raid(session.raid_key) if session else None
    return [
        app_commands.Choice(name=f"{c.name} ({c.cls} {c.item_level:g})", value=c.name)
        for c in chars
        if current in c.name and (raid is None or c.item_level >= raid.min_item_level)
    ][:25]


async def guild_character_autocomplete(interaction: discord.Interaction, current: str):
    bot = bot_of(interaction)
    chars = bot.db.list_guild_characters(interaction.guild_id or 0)
    session_id = getattr(interaction.namespace, "일정", None)
    session = bot.db.get_session(session_id) if isinstance(session_id, int) else None
    raid = bot.cfg.raid(session.raid_key) if session else None
    in_session = {c.id for c in bot.db.session_characters(session.id)} if session else set()
    out = []
    for c in chars:
        if current and current not in c.name and current not in (c.display_name or ""):
            continue
        if raid and c.item_level < raid.min_item_level:
            continue
        mark = "✓ " if c.id in in_session else ""
        out.append(
            app_commands.Choice(
                name=f"{mark}{c.name} ({c.cls} {c.item_level:g}) — {c.display_name}"[:100],
                value=str(c.id),
            )
        )
    return out[:25]


def find_guild_character(interaction: discord.Interaction, value: str) -> Character | None:
    """자동완성 값(id) 또는 직접 입력한 캐릭터 이름으로 찾는다."""
    bot = bot_of(interaction)
    chars = bot.db.list_guild_characters(interaction.guild_id or 0)
    if value.isdigit():
        for c in chars:
            if c.id == int(value):
                return c
    matches = [c for c in chars if c.name == value]
    return matches[0] if len(matches) == 1 else None
