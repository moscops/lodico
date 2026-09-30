from __future__ import annotations

import discord
from discord import app_commands
from discord.ext import commands

from ..common import CAT_LEADER, bot_of, meta
from ..timeutil import parse_hhmm


class Admin(commands.Cog):
    @app_commands.command(
        name="봇설정",
        description="알림 채널, 아침 알림 시각, 리마인더, 공대장 역할을 설정합니다 (서버 관리자)",
        extras=meta(
            CAT_LEADER,
            "/봇설정 알림채널:#레이드공지 아침알림:09:00 리마인더분:30",
            "아무 값 없이 실행하면 현재 설정을 보여 줘요.\n"
            "리마인더분을 0으로 하면 시작 전 알림을 끕니다.\n"
            "공대장역할을 @everyone 으로 지정하면 누구나 일정을 등록/수정할 수 있어요.",
            leader=True,
        ),
    )
    @app_commands.describe(
        알림채널="일일 요약, 리마인더, 변경 공지를 올릴 채널",
        아침알림="오늘 일정 요약을 보낼 시각 (HH:MM)",
        리마인더분="시작 몇 분 전에 참가자를 멘션할지 (0 = 끔)",
        공대장역할="서버 관리 권한 없이도 공대장 명령을 쓸 수 있는 역할",
    )
    @app_commands.default_permissions(manage_guild=True)
    @app_commands.guild_only()
    async def settings(
        self,
        interaction: discord.Interaction,
        알림채널: discord.TextChannel | None = None,
        아침알림: str | None = None,
        리마인더분: app_commands.Range[int, 0, 720] | None = None,
        공대장역할: discord.Role | None = None,
    ):
        bot = bot_of(interaction)
        guild_id = interaction.guild_id or 0
        updates: dict[str, object] = {}
        if 알림채널 is not None:
            updates["notify_channel_id"] = 알림채널.id
            # 고정 일정 메시지를 새 채널에 다시 만든다 (예전 채널의 메시지는 직접 정리)
            updates["board_message_id"] = None
            updates["board_week"] = None
        if 아침알림 is not None:
            updates["morning_time"] = parse_hhmm(아침알림).strftime("%H:%M")
        if 리마인더분 is not None:
            updates["reminder_minutes"] = 리마인더분
        if 공대장역할 is not None:
            updates["leader_role_id"] = 공대장역할.id
        if updates:
            bot.db.update_settings(guild_id, **updates)
            if 알림채널 is not None:
                bot.board.request(guild_id)

        s = bot.db.get_settings(guild_id)
        morning = s.morning_time or bot.cfg.morning_time
        reminder = s.reminder_minutes if s.reminder_minutes is not None else bot.cfg.reminder_minutes
        role = (
            "(없음 — 서버 관리 권한자만)" if s.leader_role_id is None
            else "@everyone (누구나)" if s.leader_role_id == guild_id
            else f"<@&{s.leader_role_id}>"
        )
        embed = discord.Embed(
            title="봇 설정" + (" — 저장됨" if updates else ""),
            color=discord.Color.dark_grey(),
        )
        embed.add_field(
            name="알림 채널",
            value=f"<#{s.notify_channel_id}>" if s.notify_channel_id else "⚠️ 미설정 (알림이 가지 않아요)",
            inline=False,
        )
        embed.add_field(name="아침 알림", value=morning)
        embed.add_field(name="리마인더", value=f"시작 {reminder}분 전" if reminder else "끔")
        embed.add_field(name="공대장 역할", value=role, inline=False)
        board = "없음 (이번 주 첫 일정이 생기면 알림 채널에 고정돼요)"
        if s.board_message_id and s.notify_channel_id:
            board = f"https://discord.com/channels/{guild_id}/{s.notify_channel_id}/{s.board_message_id}"
        embed.add_field(name="📌 고정 일정 메시지", value=board, inline=False)
        if 알림채널 is not None:
            perms = 알림채널.permissions_for(interaction.guild.me)
            if not perms.pin_messages:
                embed.add_field(
                    name="⚠️ 권한 필요",
                    value=f"{알림채널.mention} 에서 봇에게 **메시지 고정** 권한이 없어 일정 메시지를 고정할 수 없어요.",
                    inline=False,
                )
        await interaction.response.send_message(embed=embed, ephemeral=True)


async def setup(bot: commands.Bot):
    await bot.add_cog(Admin())
