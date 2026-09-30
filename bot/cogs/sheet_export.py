"""구글 시트로 이번 주 일정 · 클리어 현황 · 통계를 내보낸다 (읽기용 사본)."""
from __future__ import annotations

import asyncio
import logging

import discord
from discord import app_commands
from discord.ext import commands, tasks

from ..common import CAT_LEADER, leader_only, meta
from ..config import env
from ..gsheet import ExportError, build_export, load_export_config, write_export
from ..timeutil import now_kst

log = logging.getLogger(__name__)

AUTO_MINUTES = 30


class SheetExport(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.conf = load_export_config(env("GOOGLE_CREDENTIALS_FILE"), env("GOOGLE_SHEET_ID"))
        if self.conf and env("GOOGLE_AUTO_EXPORT", "1") != "0":
            self.auto.start()

    async def cog_unload(self):
        self.auto.cancel()

    async def export(self, guild_id: int) -> str:
        if self.conf is None:
            raise ExportError(
                "구글 시트 내보내기가 설정되지 않았어요. `.env` 의 GOOGLE_CREDENTIALS_FILE, GOOGLE_SHEET_ID 를 확인해 주세요."
            )
        tables = build_export(self.bot.db, self.bot.cfg, guild_id, now_kst())
        return await asyncio.to_thread(write_export, self.conf, tables)

    @tasks.loop(minutes=AUTO_MINUTES)
    async def auto(self):
        # 한 스프레드시트에 쓰므로 서버가 여러 개면 서로 덮어쓴다 — 서버 1개(공대 1곳) 사용을 전제로 한다
        for guild in self.bot.guilds:
            try:
                await self.export(guild.id)
            except Exception:
                log.exception("구글 시트 자동 내보내기 실패 (guild=%s)", guild.id)

    @auto.before_loop
    async def before_auto(self):
        await self.bot.wait_until_ready()

    @app_commands.command(
        name="시트내보내기",
        description="이번 주 일정·클리어 현황·통계를 구글 시트에 내보냅니다 (읽기용 사본)",
        extras=meta(
            CAT_LEADER,
            "/시트내보내기",
            "탭 3개(이번 주 일정 / 클리어 현황 / 통계)를 통째로 새로 써요. 시트를 직접 고쳐도 봇에는 반영되지 않아요.\n"
            f"설정돼 있으면 {AUTO_MINUTES}분마다 자동으로도 내보내요. 설정 방법은 README 의 '구글 시트 내보내기' 참고.",
            leader=True,
        ),
    )
    @app_commands.guild_only()
    @leader_only()
    async def export_command(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        url = await self.export(interaction.guild_id or 0)
        await interaction.followup.send(f"📤 구글 시트를 갱신했어요.\n{url}", ephemeral=True)


async def setup(bot: commands.Bot):
    await bot.add_cog(SheetExport(bot))
