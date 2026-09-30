"""/로펙 — 캐릭터별 로펙(lopec.kr) 페이지 바로가기.

로펙은 공개 API 가 없어 점수를 직접 가져오지 않는다. 비공개 내부 주소를 몰래 호출하면
사이트 구조가 바뀔 때 깨지고, 운영자 허락 없이 부하를 주게 된다. 공식 API 가 생기면 여기서 연동한다.
"""
from __future__ import annotations

import discord
from discord import app_commands
from discord.ext import commands

from ..common import CAT_VIEW, bot_of, meta
from ..render import lopec_url
from ..services import RuleError


class Lopec(commands.Cog):
    @app_commands.command(
        name="로펙",
        description="내 캐릭터(또는 다른 공대원·캐릭터)의 로펙 점수 페이지를 엽니다",
        extras=meta(
            CAT_VIEW,
            "/로펙  ·  /로펙 공대원:@철수  ·  /로펙 캐릭터:달빛바드",
            "로펙(lopec.kr)은 공개 API 가 없어 점수를 봇이 직접 가져오지는 못해요. "
            "캐릭터 이름을 누르면 로펙 페이지가 열려요.",
        ),
    )
    @app_commands.describe(공대원="다른 공대원의 캐릭터 보기", 캐릭터="특정 캐릭터 이름 (등록 안 된 캐릭터도 가능)")
    async def lopec(
        self, interaction: discord.Interaction,
        공대원: discord.Member | None = None,
        캐릭터: app_commands.Range[str, 1, 32] | None = None,
    ):
        if 캐릭터:
            name = 캐릭터.strip()
            view = discord.ui.View()
            view.add_item(discord.ui.Button(label=f"{name} 로펙 보기", url=lopec_url(name)))
            await interaction.response.send_message(f"🔎 **{name}** 로펙 페이지", view=view, ephemeral=True)
            return

        bot = bot_of(interaction)
        target = 공대원 or interaction.user
        member = bot.db.get_member(interaction.guild_id or 0, target.id)
        chars = bot.db.list_characters(member.id) if member else []
        if not chars:
            raise RuleError(f"{target.display_name} 님의 등록된 캐릭터가 없어요. `/로펙 캐릭터:이름` 으로 직접 찾거나 `/원정대연동` 을 해 주세요.")
        lines = [f"[{c.name}]({lopec_url(c.name)}) · {c.cls} {c.item_level:g}" for c in chars]
        embed = discord.Embed(
            title=f"🔎 {target.display_name} 님의 캐릭터 로펙", description="\n".join(lines)[:4000],
            color=discord.Color.purple(),
        )
        embed.set_footer(text="캐릭터 이름을 누르면 lopec.kr 이 열려요")
        await interaction.response.send_message(embed=embed, ephemeral=True)


async def setup(bot: commands.Bot):
    await bot.add_cog(Lopec())
