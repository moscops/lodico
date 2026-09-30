"""/파티목록 — 고정 파티 보기, /파티일정생성 — 이번 주 고정 파티 일정 바로 만들기."""
from __future__ import annotations

import discord
from discord import app_commands
from discord.ext import commands

from ..common import CAT_LEADER, CAT_VIEW, bot_of, leader_only, meta
from ..parties import generate_party_sessions, party_emoji, party_time_text
from ..render import truncate
from ..timeutil import now_kst


def parties_text(bot, guild_id: int) -> str:
    parties = bot.db.list_parties(guild_id)
    if not parties:
        return "_고정 파티가 없어요. `/시트양식` 의 [일정] 시트에 적어서 `/시트가져오기` 로 올려 주세요._"
    lines = []
    for p in parties:
        label = f" · {p.label}" if p.label else ""
        lines.append(f"{party_emoji(p.number)} **파티 {p.number}**{label}")
        for r in p.raids:
            raid = bot.cfg.raid(r.raid_key)
            name = raid.name if raid else r.raid_key
            size = f"{len(r.characters)}/{raid.party_size}" if raid else str(len(r.characters))
            who = ", ".join(f"{c.display_name}({c.name})" for c in r.characters) or "비어 있음"
            lines.append(f"　**{name}** `{size}` · {party_time_text(r)}")
            lines.append(f"-# 　　{who}")
    return truncate("\n".join(lines), 4000)


class PartyView(commands.Cog):
    @app_commands.command(
        name="파티목록",
        description="고정 파티(파티별 레이드, 요일·시각, 참가 캐릭터)를 봅니다",
        extras=meta(
            CAT_VIEW,
            "/파티목록",
            "고정 파티는 [일정] 시트로 정해요. 요일·시각이 있는 레이드는 매주 리셋 때 일정이 자동으로 생기고, "
            "시간 미정인 레이드는 `/일정제안` 에서 파티원이 모두 되는 시간으로 이어서 제안돼요.",
        ),
    )
    async def list_(self, interaction: discord.Interaction):
        bot = bot_of(interaction)
        embed = discord.Embed(
            title="🧩 고정 파티", description=parties_text(bot, interaction.guild_id or 0), color=discord.Color.purple()
        )
        embed.set_footer(text="색 = 파티 번호 (시트 배경색과 같아요) · 수정은 시트로")
        await interaction.response.send_message(embed=embed)

    @app_commands.command(
        name="파티일정생성",
        description="요일·시각이 정해진 고정 파티의 이번 주 일정을 지금 만듭니다 (보통은 리셋 때 자동)",
        extras=meta(CAT_LEADER, "/파티일정생성", leader=True),
    )
    @leader_only()
    async def generate(self, interaction: discord.Interaction):
        bot = bot_of(interaction)
        result = generate_party_sessions(bot.db, bot.cfg, interaction.guild_id or 0, now_kst(), interaction.user.id)
        lines = result.created or ["새로 만들 일정이 없어요 (이미 있거나, 시간이 지났거나, 시간 미정)."]
        if result.skipped:
            lines += ["", "⚠️ 넣지 못한 캐릭터"] + [f"• {s}" for s in result.skipped]
        await interaction.response.send_message(truncate("\n".join(lines), 2000), ephemeral=True)


async def setup(bot: commands.Bot):
    await bot.add_cog(PartyView())
