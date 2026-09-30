"""/메뉴 — 자주 쓰는 기능 5개를 버튼으로 모은 메시지를 채널에 올리고 고정한다.

디스코드는 슬래시 명령어 목록의 순서를 봇이 정하거나 특정 명령어를 고정할 수 없다(사용 빈도순 자동 정렬).
대신 이 메뉴 메시지를 채널에 고정해 두면 버튼 한 번으로 같은 기능을 (나에게만 보이게) 실행할 수 있다.
버튼은 custom_id 에 동작 이름을 담은 DynamicItem 이라 봇을 재시작해도 옛 메시지의 버튼이 계속 동작한다.
"""
from __future__ import annotations

import logging

import discord
from discord import app_commands
from discord.ext import commands

from ..common import CAT_LEADER, leader_only, meta

log = logging.getLogger(__name__)

# 동작 이름 → (버튼 라벨, 스타일, cog 클래스 이름, 명령어 속성 이름, 넘길 인자)
# 명령어의 원래 함수를 그대로 부르므로 슬래시 명령어와 결과가 같고, 나만보기가 있으면 켠다.
ACTIONS: dict[str, tuple[str, discord.ButtonStyle, str, str, dict]] = {
    "schedule": ("📅 공대일정", discord.ButtonStyle.primary, "Sessions", "overview", {"나만보기": True}),
    "edit": ("✏️ 일정 수정", discord.ButtonStyle.primary, "SessionEdit", "edit_sessions", {}),
    "mine": ("🗓️ 내 일정", discord.ButtonStyle.success, "Sessions", "my_schedule", {}),
    "avail": ("🕐 가능현황", discord.ButtonStyle.secondary, "Availability", "overview", {"나만보기": True}),
    "lopec": ("🔎 로펙", discord.ButtonStyle.secondary, "Lopec", "lopec", {}),
}


class _MenuButton(discord.ui.DynamicItem[discord.ui.Button], template=r"raid:menu:(?P<action>[a-z]+)"):
    def __init__(self, action: str):
        label, style, *_ = ACTIONS[action]
        super().__init__(discord.ui.Button(label=label, style=style, custom_id=f"raid:menu:{action}"))
        self.action = action

    @classmethod
    async def from_custom_id(cls, interaction, item, match):
        action = match["action"]
        if action not in ACTIONS:
            raise ValueError(f"알 수 없는 메뉴 동작: {action}")
        return cls(action)

    async def callback(self, interaction: discord.Interaction):
        _, _, cog_name, attr, kwargs = ACTIONS[self.action]
        cog = interaction.client.get_cog(cog_name)
        command = getattr(cog, attr, None) if cog else None
        if command is None:
            await interaction.response.send_message("이 기능을 지금은 쓸 수 없어요.", ephemeral=True)
            return
        await command.callback(cog, interaction, **kwargs)

    async def on_error(self, interaction: discord.Interaction, error: Exception) -> None:
        # 슬래시 명령어와 같은 오류 안내(규칙 위반은 그대로, 나머지는 일반 문구)를 쓴다
        await interaction.client.tree.on_error(interaction, error)  # type: ignore[attr-defined]


class MenuView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)
        for action in ACTIONS:
            self.add_item(_MenuButton(action))


def menu_embed() -> discord.Embed:
    embed = discord.Embed(
        title="📌 공대 일정 메뉴",
        description="버튼을 누르면 나에게만 보이는 창이 열려요.\n" + "\n".join(
            [
                "📅 **공대일정** — 이번 주 전체 일정 (파티 색·완료 표시)",
                "✏️ **일정 수정** — 시각·메모 수정, 완료/미완료 표시, 캐릭터 교체",
                "🗓️ **내 일정** — 내가 들어간 일정과 남은 숙제",
                "🕐 **가능현황** — 날짜별로 누가 몇 시에 되는지",
                "🔎 **로펙** — 내 캐릭터의 로펙 페이지",
            ]
        ),
        color=discord.Color.blurple(),
    )
    embed.set_footer(text="그 밖의 기능은 /도움")
    return embed


class Menu(commands.Cog):
    @app_commands.command(
        name="메뉴",
        description="자주 쓰는 기능 버튼 메뉴를 이 채널에 올리고 고정합니다 (공대장)",
        extras=meta(
            CAT_LEADER,
            "/메뉴",
            "공대일정·일정 수정·내 일정·가능현황·로펙 버튼이 달린 메시지를 올리고 채널 상단에 고정해요.\n"
            "봇에 '메시지 고정' 권한이 없으면 올리기만 하고 알려 드려요. 옛 메뉴는 직접 지우세요.",
            leader=True,
        ),
    )
    @app_commands.guild_only()
    @leader_only()
    async def menu(self, interaction: discord.Interaction):
        await interaction.response.send_message(embed=menu_embed(), view=MenuView())
        message = await interaction.original_response()
        try:
            await message.pin(reason="공대 일정 메뉴")
        except discord.HTTPException as e:
            log.warning("메뉴 메시지 고정 실패: %s", e)
            await interaction.followup.send(
                "⚠️ 메뉴는 올렸지만 고정하지 못했어요. 봇에 **메시지 고정** 권한을 주거나 직접 고정해 주세요.", ephemeral=True
            )


async def setup(bot: commands.Bot):
    bot.add_dynamic_items(_MenuButton)
    await bot.add_cog(Menu())
