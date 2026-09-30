"""/도움 — 등록된 명령어의 extras 로 자동 생성된다."""
from __future__ import annotations

import discord
from discord import app_commands
from discord.ext import commands

from ..common import CAT_VIEW, CATEGORIES, is_leader, meta

FAVORITES = ["레이드일정", "공대일정수정", "내일정", "공대시간대", "로펙"]

START_GUIDE = (
    "**처음이라면 이 순서로!**\n"
    "① `/캐릭터등록` 캐릭터마다 한 번씩\n"
    "② `/내시간대` 평소 되는 시간 한 번만\n"
    "③ `/레이드일정` 으로 확인하고 `/일정참가`"
)


def _commands(interaction: discord.Interaction) -> list[app_commands.Command]:
    tree = interaction.client.tree  # type: ignore[attr-defined]
    return all_commands(tree)


def all_commands(tree: app_commands.CommandTree) -> list[app_commands.Command]:
    """그룹 하위 명령어(/레이드 목록 등)까지 펼친 목록."""
    return [c for c in tree.walk_commands() if isinstance(c, app_commands.Command)]


def _visible(interaction: discord.Interaction, cmd: app_commands.Command) -> bool:
    return not cmd.extras.get("leader") or is_leader(interaction)


async def _command_autocomplete(interaction: discord.Interaction, current: str):
    return [
        app_commands.Choice(name=f"/{c.qualified_name} — {c.description}"[:100], value=c.qualified_name)
        for c in _commands(interaction)
        if _visible(interaction, c) and current.lstrip("/") in c.qualified_name
    ][:25]


def build_overview_embed(cmds: list[app_commands.Command]) -> discord.Embed:
    embed = discord.Embed(title="📖 공대 일정 봇 도움말", description=START_GUIDE, color=discord.Color.green())
    by_name = {c.qualified_name: c for c in cmds}
    favorites = [by_name[n] for n in FAVORITES if n in by_name]
    if favorites:
        embed.add_field(
            name="⭐ 자주 쓰는 기능 (채널의 📌 메뉴 버튼으로도 열려요)",
            value="\n".join(f"`/{c.qualified_name}` {c.description}" for c in favorites)[:1024],
            inline=False,
        )
    for cat in CATEGORIES:
        lines = [
            f"`/{c.qualified_name}` {c.description}\n-# 예) {c.extras.get('example', '')}"
            for c in cmds
            if c.extras.get("category") == cat
        ]
        if lines:
            embed.add_field(name=cat, value="\n".join(lines)[:1024], inline=False)
    embed.set_footer(text="자세히: /도움 명령어:<이름>")
    return embed


def build_detail_embed(cmd: app_commands.Command) -> discord.Embed:
    embed = discord.Embed(title=f"/{cmd.qualified_name}", description=cmd.description, color=discord.Color.green())
    if cmd.parameters:
        params = "\n".join(
            f"`{p.display_name}`{'' if p.required else ' (선택)'} — {p.description or ''}".rstrip(" —")
            for p in cmd.parameters
        )
        embed.add_field(name="입력값", value=params[:1024], inline=False)
    if ex := cmd.extras.get("example"):
        embed.add_field(name="예시", value=ex, inline=False)
    if detail := cmd.extras.get("detail"):
        embed.add_field(name="참고", value=detail[:1024], inline=False)
    if cmd.extras.get("leader"):
        embed.set_footer(text="공대장 전용")
    return embed


class Help(commands.Cog):
    @app_commands.command(
        name="도움",
        description="명령어 목록과 사용법을 봅니다",
        extras=meta(CAT_VIEW, "/도움  ·  /도움 명령어:일정참가"),
    )
    @app_commands.describe(명령어="특정 명령어의 자세한 설명")
    @app_commands.autocomplete(명령어=_command_autocomplete)
    async def help(self, interaction: discord.Interaction, 명령어: str | None = None):
        cmds = [c for c in _commands(interaction) if _visible(interaction, c)]
        if 명령어:
            cmd = next((c for c in cmds if c.qualified_name == 명령어.lstrip("/")), None)
            if cmd is None:
                await interaction.response.send_message(f"`{명령어}` 명령어를 찾지 못했어요.", ephemeral=True)
                return
            embed = build_detail_embed(cmd)
        else:
            embed = build_overview_embed(cmds)
        await interaction.response.send_message(embed=embed, ephemeral=True)


async def setup(bot: commands.Bot):
    await bot.add_cog(Help())
