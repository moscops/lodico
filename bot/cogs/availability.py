from __future__ import annotations

import discord
from discord import app_commands
from discord.ext import commands

from ..db import Database
from ..common import CAT_START, CAT_VIEW, bot_of, ensure_member, meta
from ..render import availability_text, day_detail_text, heatmap_text, weekly_availability_text
from ..services import week_bounds
from ..timeutil import OPEN_END, WEEKDAYS, default_end, fmt_minutes, now_kst, to_epoch

MAX_HOURS = 14  # 시작 시각부터 최대 몇 시간까지 끝 시각으로 고를 수 있는지


class AvailabilityView(discord.ui.View):
    """요일(다중) → 시작 → 끝 을 고르고 저장. 저장하면 선택 요일의 기존 값을 덮어쓴다."""

    def __init__(self, db: Database, owner_id: int, member_id: int):
        super().__init__(timeout=600)
        self.db = db
        self.owner_id = owner_id
        self.member_id = member_id
        self.days: list[int] = []
        self.start: int | None = None  # 분
        self.end: int | None = None

        self.day_select = discord.ui.Select(
            placeholder="① 요일 선택 (여러 개 가능)", min_values=1, max_values=7, row=0,
            options=[discord.SelectOption(label=f"{d}요일", value=str(i)) for i, d in enumerate(WEEKDAYS)],
        )
        self.day_select.callback = self.on_days
        self.start_select = discord.ui.Select(
            placeholder="② 시작 시각", row=1,
            options=[discord.SelectOption(label=f"{h:02d}:00", value=str(h * 60)) for h in range(24)],
        )
        self.start_select.callback = self.on_start
        self.end_select = discord.ui.Select(
            placeholder="③ 끝 시각 (선택 · 시작 시각을 먼저 고르세요)", row=2, disabled=True,
            min_values=0,  # 고른 끝 시각을 다시 비울 수 있게
            options=[discord.SelectOption(label="-", value="0")],
        )
        self.end_select.callback = self.on_end
        for item in (self.day_select, self.start_select, self.end_select):
            self.add_item(item)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        return interaction.user.id == self.owner_id

    def content(self, notice: str = "") -> str:
        current = availability_text(self.db.list_availability(self.member_id))
        sel_days = ",".join(WEEKDAYS[d] for d in sorted(self.days)) or "?"
        sel_start = fmt_minutes(self.start) if self.start is not None else "?"
        if self.end is not None:
            sel_end = fmt_minutes(self.end)
        elif self.start is not None:
            sel_end = f"{fmt_minutes(default_end(self.start))} (끝 미선택)"
        else:
            sel_end = "?"
        parts = [
            "**평소 가능한 시간대**를 요일별로 한 번만 등록해 두세요. 요일당 한 구간이며, 다시 저장하면 덮어써요.\n"
            f"끝 시각은 선택이에요. 안 고르면 {fmt_minutes(OPEN_END)}까지로 저장돼요.",
            f"**현재 등록**\n{current}",
            f"**선택 중**: {sel_days} {sel_start}~{sel_end}",
        ]
        if notice:
            parts.append(notice)
        return "\n\n".join(parts)

    def _mark_defaults(self) -> None:
        for o in self.day_select.options:
            o.default = int(o.value) in self.days
        for o in self.start_select.options:
            o.default = self.start is not None and int(o.value) == self.start
        for o in self.end_select.options:
            o.default = self.end is not None and int(o.value) == self.end

    async def refresh(self, interaction: discord.Interaction, notice: str = "") -> None:
        self._mark_defaults()
        await interaction.response.edit_message(content=self.content(notice), view=self)

    async def on_days(self, interaction: discord.Interaction):
        self.days = [int(v) for v in self.day_select.values]
        await self.refresh(interaction)

    async def on_start(self, interaction: discord.Interaction):
        self.start = int(self.start_select.values[0])
        self.end = None
        self.end_select.options = [
            discord.SelectOption(label=fmt_minutes(self.start + h * 60), value=str(self.start + h * 60))
            for h in range(1, MAX_HOURS + 1)
        ]
        self.end_select.disabled = False
        self.end_select.placeholder = f"③ 끝 시각 (선택 · 비우면 {fmt_minutes(default_end(self.start))}까지)"
        await self.refresh(interaction)

    async def on_end(self, interaction: discord.Interaction):
        values = self.end_select.values
        self.end = int(values[0]) if values else None
        await self.refresh(interaction)

    @discord.ui.button(label="저장", style=discord.ButtonStyle.success, row=3)
    async def save(self, interaction: discord.Interaction, _: discord.ui.Button):
        if not self.days or self.start is None:
            await self.refresh(interaction, "⚠️ 요일과 시작 시각을 골라 주세요. (끝 시각은 선택)")
            return
        end = self.end if self.end is not None else default_end(self.start)
        for d in self.days:
            self.db.set_availability(self.member_id, d, self.start, end)
        await self.refresh(interaction, "✅ 저장했어요. 다른 요일도 이어서 설정할 수 있어요.")

    @discord.ui.button(label="선택 요일 비우기", style=discord.ButtonStyle.secondary, row=3)
    async def clear(self, interaction: discord.Interaction, _: discord.ui.Button):
        if not self.days:
            await self.refresh(interaction, "⚠️ 비울 요일을 먼저 골라 주세요.")
            return
        for d in self.days:
            self.db.clear_availability(self.member_id, d)
        await self.refresh(interaction, "🗑️ 선택한 요일을 '불가'로 비웠어요.")

    @discord.ui.button(label="닫기", style=discord.ButtonStyle.secondary, row=3)
    async def close(self, interaction: discord.Interaction, _: discord.ui.Button):
        self.stop()
        await interaction.response.edit_message(
            content="**등록된 가능 시간**\n" + availability_text(self.db.list_availability(self.member_id)),
            view=None,
        )


class Availability(commands.Cog):
    @app_commands.command(
        name="가능시간대",
        description="요일별 평소 가능한 시간대를 등록합니다 (드롭다운)",
        extras=meta(
            CAT_START,
            "/가능시간대 → 요일 [월,화,목] · 시작 [21:00] · (끝 [익일 01:00]) → 저장",
            "요일마다 한 구간씩 저장돼요. 끝 시각은 선택이고, 안 고르면 익일 02:00까지로 저장돼요.\n"
            "자정을 넘기는 시간은 끝 시각에서 '익일'을 고르면 돼요.\n"
            "이번 주만 안 되는 날은 `/불가` 로 등록하면 돼요.",
        ),
    )
    async def availability(self, interaction: discord.Interaction):
        member = ensure_member(interaction)
        view = AvailabilityView(bot_of(interaction).db, interaction.user.id, member.id)
        await interaction.response.send_message(view.content(), view=view, ephemeral=True)

    @app_commands.command(
        name="가능현황",
        description="이번 주 날짜별로 누가 몇 시에 가능한지 봅니다 (일정 잡을 때 참고)",
        extras=meta(
            CAT_VIEW,
            "/가능현황  ·  /가능현황 요일:금  ·  /가능현황 보기:표",
            "기본은 날짜별 명단이에요 (예: 금 10/2 · 철수 21~02 · 영희 20~24).\n"
            "요일을 고르면 그 요일의 시간대별 가능한 사람, 보기:표 는 요일×시간 인원 수 표를 보여 줘요.",
        ),
    )
    @app_commands.describe(요일="그 요일의 시간대별 가능한 사람", 보기="명단(기본) 또는 표(요일×시간 인원 수)", 나만보기="나에게만 보이게")
    @app_commands.choices(
        요일=[app_commands.Choice(name=f"{d}요일", value=i) for i, d in enumerate(WEEKDAYS)],
        보기=[app_commands.Choice(name="명단", value="list"), app_commands.Choice(name="표", value="table")],
    )
    async def overview(
        self, interaction: discord.Interaction,
        요일: app_commands.Choice[int] | None = None,
        보기: app_commands.Choice[str] | None = None,
        나만보기: bool = False,
    ):
        bot = bot_of(interaction)
        db = bot.db
        guild_id = interaction.guild_id or 0
        avail = db.list_guild_availability(guild_id)
        names = {m.id: m.display_name for m in db.list_members(guild_id)}
        if 요일 is not None:
            body = day_detail_text(avail, 요일.value, names)
            title = f"{요일.name} 시간대별 가능한 사람"
        elif 보기 and 보기.value == "table":
            body = heatmap_text(avail, len({a.member_id for a in avail}))
            title = "요일×시간 가능 인원"
        else:
            week_start = week_bounds(bot.cfg, now_kst())[0]
            blocked = db.list_guild_unavailable(guild_id, to_epoch(week_start))
            body = weekly_availability_text(avail, names, week_start, blocked=blocked)
            title = "이번 주 날짜별 가능한 사람"
        embed = discord.Embed(title=title, description=body[:4000], color=discord.Color.teal())
        embed.set_footer(text="21~02 = 21시부터 익일 2시까지 · 등록/수정: /가능시간대 · 🚫 = /불가 예외")
        await interaction.response.send_message(embed=embed, ephemeral=나만보기)


async def setup(bot: commands.Bot):
    await bot.add_cog(Availability())
