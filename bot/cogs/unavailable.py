"""이번 주만 안 되는 날짜·시간(예외) 등록. 평소 가능 시간(/내시간대)보다 우선한다."""
from __future__ import annotations

from datetime import datetime, time, timedelta

import discord
from discord import app_commands
from discord.ext import commands

from ..common import CAT_INPUT, bot_of, ensure_member, meta
from ..db import Unavailable
from ..timeutil import KST, fmt_date, fmt_minutes, from_epoch, now_kst, parse_date, parse_time_range, to_epoch


def unavailable_line(u: Unavailable) -> str:
    start, end = from_epoch(u.start_at), from_epoch(u.end_at)
    if start.time() == time(0) and end - start == timedelta(days=1):
        when = f"{fmt_date(start.date())} 하루 종일"
    else:
        end_txt = f"{end:%H:%M}" if end.date() == start.date() else f"{fmt_date(end.date())} {end:%H:%M}"
        when = f"{fmt_date(start.date())} {start:%H:%M}~{end_txt}"
    return f"`#{u.id}` {when}"


def build_interval(day_text: str, time_text: str | None, today) -> tuple[int, int]:
    """(날짜, 시간 범위) → (시작 epoch, 끝 epoch). 시간을 비우면 그날 하루 종일."""
    day = parse_date(day_text, today)
    start_min, end_min = parse_time_range(time_text) if time_text else (0, 1440)
    midnight = datetime.combine(day, time(0), KST)
    return to_epoch(midnight + timedelta(minutes=start_min)), to_epoch(midnight + timedelta(minutes=end_min))


async def unavailable_autocomplete(interaction: discord.Interaction, current: str):
    bot = bot_of(interaction)
    member = bot.db.get_member(interaction.guild_id or 0, interaction.user.id)
    if member is None:
        return []
    rows = bot.db.list_unavailable(member.id, to_epoch(now_kst()))
    return [
        app_commands.Choice(name=unavailable_line(u).replace("`", "")[:100], value=u.id)
        for u in rows
        if current.strip("#") in str(u.id) or not current
    ][:25]


class UnavailableCog(commands.Cog):
    @app_commands.command(
        name="불가",
        description="특정 날짜나 시간대를 이번 주만 안 되는 것으로 등록합니다 (예외 처리)",
        extras=meta(
            CAT_INPUT,
            "/불가 날짜:10/3 시간:20~24  ·  /불가 날짜:금 (하루 종일)",
            "평소 가능 시간(`/내시간대`)은 그대로 두고, 그 날짜의 그 시간만 빠져요.\n"
            "자동 일정 제안(`/일정제안`)에서 제외되고, 그 시간대 일정에 참가하면 경고가 떠요.\n"
            "시간을 비우면 하루 종일이고, 지난 예외는 자동으로 사라져요. 취소는 `/불가취소`, 확인은 `/불가목록`.",
        ),
    )
    @app_commands.describe(날짜="예: 10/3, 금, 내일", 시간="예: 20~24, 21~02 (비우면 하루 종일)")
    async def add(self, interaction: discord.Interaction, 날짜: str, 시간: str | None = None):
        bot = bot_of(interaction)
        member = ensure_member(interaction)
        now = now_kst()
        start, end = build_interval(날짜, 시간, now.date())
        if end <= to_epoch(now):
            await interaction.response.send_message("⚠️ 이미 지난 시간이에요.", ephemeral=True)
            return
        u = bot.db.add_unavailable(member.id, start, end)
        await interaction.response.send_message(
            f"🚫 불가로 등록했어요: {unavailable_line(u)}\n-# 이 시간은 자동 일정 제안에서 제외돼요. 취소: `/불가취소`",
            ephemeral=True,
        )

    @app_commands.command(
        name="불가목록",
        description="내가 등록한 불가 예외를 봅니다",
        extras=meta(CAT_INPUT, "/불가목록"),
    )
    async def list_(self, interaction: discord.Interaction):
        bot = bot_of(interaction)
        member = ensure_member(interaction)
        rows = bot.db.list_unavailable(member.id, to_epoch(now_kst()))
        text = "\n".join(unavailable_line(u) for u in rows) or "_등록된 불가 예외가 없어요._"
        await interaction.response.send_message("**내 불가 예외**\n" + text, ephemeral=True)

    @app_commands.command(
        name="불가취소",
        description="등록한 불가 예외를 취소합니다",
        extras=meta(CAT_INPUT, "/불가취소 항목:(목록에서 선택)"),
    )
    @app_commands.describe(항목="취소할 불가 예외")
    @app_commands.autocomplete(항목=unavailable_autocomplete)
    async def cancel(self, interaction: discord.Interaction, 항목: int):
        member = ensure_member(interaction)
        ok = bot_of(interaction).db.delete_unavailable(member.id, 항목)
        await interaction.response.send_message(
            "✅ 취소했어요." if ok else "⚠️ 그런 불가 예외가 없어요. `/불가목록` 으로 확인해 주세요.", ephemeral=True
        )


async def setup(bot: commands.Bot):
    await bot.add_cog(UnavailableCog())
