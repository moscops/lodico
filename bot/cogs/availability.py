from __future__ import annotations

import logging
import time
from collections import defaultdict
from datetime import date, timedelta

import discord
from discord import app_commands
from discord.ext import commands

from ..config import Config
from ..db import Database, Member
from ..common import CAT_START, CAT_VIEW, bot_of, ensure_member, is_leader, meta
from ..render import availability_text, day_detail_text, heatmap_text, team_availability_text, team_characters_text
from ..services import week_bounds
from ..timeutil import OPEN_END, WEEKDAYS, default_end, fmt_minutes, from_epoch, now_kst, to_epoch

log = logging.getLogger("raidbot")

MAX_HOURS = 14  # 시작 시각부터 최대 몇 시간까지 끝 시각으로 고를 수 있는지


class AvailabilityView(discord.ui.View):
    """요일(다중) → 시작 → 끝 을 고르고 저장. 저장하면 선택 요일의 기존 값을 덮어쓴다."""

    def __init__(self, db: Database, cfg: Config, owner_id: int, member_id: int | None, target_name: str | None = None):
        super().__init__(timeout=600)
        self.db = db
        self.cfg = cfg
        self.owner_id = owner_id
        self.member_id = member_id
        self.target_name = target_name  # 공대장이 남의 시간을 고칠 때 그 공대원 이름
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

    async def on_error(self, interaction: discord.Interaction, error: Exception, item: discord.ui.Item) -> None:
        """콜백에서 예외가 나면 응답이 안 가서 '무한 로딩'이 된다. 원인을 로그에 남기고 사용자에게 안내를 보낸다."""
        log.error(
            "가능시간 화면 오류 (%s, 공대원 %s)", type(item).__name__, self.member_id,
            exc_info=(type(error), error, error.__traceback__),
        )
        message = "⚠️ 처리 중 오류가 났어요. 화면을 닫고 명령어를 다시 열어 주세요."
        try:
            if interaction.response.is_done():
                await interaction.followup.send(message, ephemeral=True)
            else:
                await interaction.response.send_message(message, ephemeral=True)
        except discord.HTTPException:
            log.exception("가능시간 화면 오류 안내도 보내지 못했어요")

    def _current_text(self) -> str:
        week_start = week_bounds(self.cfg, now_kst())[0]  # 주간(리셋~리셋)이 바뀌면 날짜도 바뀐다
        return availability_text(self.db.list_availability(self.member_id), week_start)

    def content(self, notice: str = "") -> str:
        current = self._current_text()
        sel_days = ",".join(WEEKDAYS[d] for d in sorted(self.days)) or "?"
        sel_start = fmt_minutes(self.start) if self.start is not None else "?"
        if self.end is not None:
            sel_end = fmt_minutes(self.end)
        elif self.start is not None:
            sel_end = f"{fmt_minutes(default_end(self.start))} (끝 미선택)"
        else:
            sel_end = "?"
        intro = (
            f"**{self.target_name}** 님의 평소 가능한 시간대를 대신 수정해요 (공대장). "
            if self.target_name
            else "**평소 가능한 시간대**를 요일별로 한 번만 등록해 두세요. "
        )
        parts = [
            intro + "요일당 한 구간이며, 다시 저장하면 덮어써요.\n"
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
        started = time.perf_counter()
        await interaction.response.edit_message(content=self.content(notice), view=self)
        log.info("가능시간 화면 갱신: 디스코드 응답 %.2f초", time.perf_counter() - started)

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
        if self.member_id is None:
            await self.refresh(interaction, "⚠️ 수정할 공대원을 먼저 골라 주세요.")
            return
        if not self.days or self.start is None:
            await self.refresh(interaction, "⚠️ 요일과 시작 시각을 골라 주세요. (끝 시각은 선택)")
            return
        end = self.end if self.end is not None else default_end(self.start)
        for d in self.days:
            self.db.set_availability(self.member_id, d, self.start, end)
        await self.refresh(interaction, "✅ 저장했어요. 다른 요일도 이어서 설정할 수 있어요.")

    @discord.ui.button(label="선택 요일 비우기", style=discord.ButtonStyle.secondary, row=3)
    async def clear(self, interaction: discord.Interaction, _: discord.ui.Button):
        if self.member_id is None:
            await self.refresh(interaction, "⚠️ 수정할 공대원을 먼저 골라 주세요.")
            return
        if not self.days:
            await self.refresh(interaction, "⚠️ 비울 요일을 먼저 골라 주세요.")
            return
        for d in self.days:
            self.db.clear_availability(self.member_id, d)
        await self.refresh(interaction, "🗑️ 선택한 요일을 '불가'로 비웠어요.")

    @discord.ui.button(label="닫기", style=discord.ButtonStyle.secondary, row=3)
    async def close(self, interaction: discord.Interaction, _: discord.ui.Button):
        self.stop()
        await interaction.response.edit_message(**self.close_payload())

    def close_payload(self) -> dict:
        return {"content": "**등록된 가능 시간**\n" + self._current_text(), "view": None}


class TeamAvailabilityView(AvailabilityView):
    """/공대시간대 (공대장·서버 관리자): 위 임베드에 공대 전체 시간 목록, 아래는 /내시간대 와 같은 화면.
    맨 위 드롭다운으로 수정할 공대원을 고른다 (처음엔 나 자신)."""

    MEMBER_LIMIT = 25  # 드롭다운 한 개에 넣을 수 있는 최대 개수

    def __init__(self, db: Database, cfg: Config, owner_id: int, guild_id: int, me: Member):
        super().__init__(db, cfg, owner_id, me.id, me.display_name)
        self.guild_id = guild_id
        # 기본 화면의 드롭다운·버튼을 한 줄씩 아래로 내리고, 맨 위 줄에 공대원 선택을 넣는다
        items = list(self.children)
        self.clear_items()
        for item in items:
            item.row = (item.row or 0) + 1
            self.add_item(item)
        members = sorted(db.list_members(guild_id), key=lambda m: m.display_name)
        self.member_select = discord.ui.Select(
            placeholder="수정할 공대원 선택", row=0,
            options=[
                discord.SelectOption(label=m.display_name[:100], value=str(m.id), default=m.id == me.id)
                for m in members[: self.MEMBER_LIMIT]
            ],
        )
        self.member_select.callback = self.on_member
        self.add_item(self.member_select)

    def embed(self) -> discord.Embed:
        db = self.db
        week_start = week_bounds(self.cfg, now_kst())[0]
        names = {m.id: m.display_name for m in db.list_members(self.guild_id)}
        body = team_availability_text(
            db.list_guild_availability(self.guild_id), names, week_start,
            db.list_guild_unavailable(self.guild_id, to_epoch(week_start)),
        )
        embed = discord.Embed(title="이번 주 날짜별 가능한 사람", description=body[:4000], color=discord.Color.teal())
        embed.set_footer(text="이름 색은 공대원마다 고정 · 🚫 = /불가 예외 · 아래에서 공대원을 골라 시간을 수정할 수 있어요")
        return embed

    async def refresh(self, interaction: discord.Interaction, notice: str = "") -> None:
        self._mark_defaults()
        for o in self.member_select.options:
            o.default = int(o.value) == self.member_id
        started = time.perf_counter()
        content, embed = self.content(notice), self.embed()
        built = time.perf_counter()
        try:
            await interaction.response.edit_message(content=content, embed=embed, view=self)
        except discord.HTTPException:
            # 디스코드가 화면을 거부하면 응답이 안 가서 '무한 로딩'이 된다. 이유를 로그에 남기고 안내를 보낸다
            log.exception("공대시간대 화면 갱신 실패 (공대원 %s)", self.member_id)
            if not interaction.response.is_done():
                await interaction.response.send_message("⚠️ 화면을 갱신하지 못했어요. `/공대시간대` 를 다시 열어 주세요.", ephemeral=True)
            return
        log.info(
            "공대시간대 화면 갱신: 화면 만들기 %.3f초 · 디스코드 응답 %.2f초", built - started, time.perf_counter() - built
        )

    async def on_member(self, interaction: discord.Interaction):
        self.member_id = int(self.member_select.values[0])
        self.target_name = next(o.label for o in self.member_select.options if int(o.value) == self.member_id)
        self.days, self.start, self.end = [], None, None
        self.end_select.disabled = True
        await self.refresh(interaction)

    def close_payload(self) -> dict:
        return {"content": None, "embed": self.embed(), "view": None}  # 닫으면 공대 목록만 남긴다


class Availability(commands.Cog):
    @app_commands.command(
        name="내시간대",
        description="요일별 평소 가능한 시간대를 등록합니다 (드롭다운)",
        extras=meta(
            CAT_START,
            "/내시간대 → 요일 [월,화,목] · 시작 [21:00] · (끝 [익일 01:00]) → 저장",
            "요일마다 한 구간씩 저장돼요. 끝 시각은 선택이고, 안 고르면 익일 02:00까지로 저장돼요.\n"
            "자정을 넘기는 시간은 끝 시각에서 '익일'을 고르면 돼요.\n"
            "이번 주만 안 되는 날은 `/불가` 로 등록하면 돼요.",
        ),
    )
    async def availability(self, interaction: discord.Interaction):
        member = ensure_member(interaction)
        view = AvailabilityView(bot_of(interaction).db, bot_of(interaction).cfg, interaction.user.id, member.id)
        await interaction.response.send_message(view.content(), view=view, ephemeral=True)

    @app_commands.command(
        name="공대시간대",
        description="이번 주 날짜별로 누가 가능한지 봅니다. (공대장·관리자) 수정:True 로 공대원 시간도 고쳐요",
        extras=meta(
            CAT_VIEW,
            "/공대시간대  ·  /공대시간대 이름:캐릭터  ·  /공대시간대 요일:금  ·  /공대시간대 보기:표  ·  (공대장) /공대시간대 수정:True",
            "기본은 날짜별 명단이에요 (예: 10/2(금): 철수, 영희). 이름은 공대원마다 고정된 색으로 보여요. 몇 시에 되는지는 `요일` 을 골라 확인하세요.\n"
            "`이름:캐릭터` 로 열면 그 날짜 레이드 일정에 들어간 캐릭터 이름으로 보여 줘요 (같은 공대원의 캐릭터는 같은 색).\n"
            "(공대장·서버 관리자) `수정:True` 로 열면 명단 아래 드롭다운으로 공대원을 골라 `/내시간대` 와 같은 방식으로 시간을 대신 수정할 수 있어요.\n"
            "요일을 고르면 그 요일의 시간대별 가능한 사람, 보기:표 는 요일×시간 인원 수 표를 보여 줘요.",
        ),
    )
    @app_commands.describe(
        요일="그 요일의 시간대별 가능한 사람", 보기="명단(기본) 또는 표(요일×시간 인원 수)", 나만보기="나에게만 보이게",
        수정="(공대장·관리자) 공대원의 가능 시간을 대신 수정하는 화면으로 열기",
        이름="사람(기본) 이름 또는 그날 레이드 가는 캐릭터 이름으로 보기",
    )
    @app_commands.choices(
        요일=[app_commands.Choice(name=f"{d}요일", value=i) for i, d in enumerate(WEEKDAYS)],
        보기=[app_commands.Choice(name="명단", value="list"), app_commands.Choice(name="표", value="table")],
        이름=[app_commands.Choice(name="사람", value="person"), app_commands.Choice(name="캐릭터", value="character")],
    )
    async def overview(
        self, interaction: discord.Interaction,
        요일: app_commands.Choice[int] | None = None,
        보기: app_commands.Choice[str] | None = None,
        나만보기: bool = False,
        수정: bool = False,
        이름: app_commands.Choice[str] | None = None,
    ):
        bot = bot_of(interaction)
        db = bot.db
        guild_id = interaction.guild_id or 0
        avail = db.list_guild_availability(guild_id)
        names = {m.id: m.display_name for m in db.list_members(guild_id)}
        if 수정:
            if not is_leader(interaction):
                raise app_commands.CheckFailure(
                    "다른 공대원의 시간은 공대장만 수정할 수 있어요. (서버 관리 권한 또는 `/봇설정 공대장역할` 로 지정된 역할)"
                )
            # 공대장·관리자: 명단 + 수정 화면 (나에게만 보임)
            view = TeamAvailabilityView(db, bot.cfg, interaction.user.id, guild_id, ensure_member(interaction))
            await interaction.response.send_message(view.content(), embed=view.embed(), view=view, ephemeral=True)
            return
        if 요일 is not None:
            body = day_detail_text(avail, 요일.value, names)
            title = f"{요일.name} 시간대별 가능한 사람"
        elif 보기 and 보기.value == "table":
            body = heatmap_text(avail, len({a.member_id for a in avail}))
            title = "요일×시간 가능 인원"
        elif 이름 and 이름.value == "character":
            week_start = week_bounds(bot.cfg, now_kst())[0]
            chars_by_day: dict[date, list] = defaultdict(list)
            for s in db.list_sessions(guild_id, to_epoch(week_start), to_epoch(week_start + timedelta(days=7))):
                day = from_epoch(s.start_at).date()
                for c in db.session_characters(s.id):
                    if all(x.id != c.id for x in chars_by_day[day]):  # 같은 날 여러 일정에 든 캐릭터는 한 번만
                        chars_by_day[day].append(c)
            body = team_characters_text(chars_by_day, names, week_start)
            title = "이번 주 날짜별 레이드 가는 캐릭터"
        else:
            week_start = week_bounds(bot.cfg, now_kst())[0]
            blocked = db.list_guild_unavailable(guild_id, to_epoch(week_start))
            body = team_availability_text(avail, names, week_start, blocked)
            title = "이번 주 날짜별 가능한 사람"
        embed = discord.Embed(title=title, description=body[:4000], color=discord.Color.teal())
        embed.set_footer(text="이름 색은 공대원마다 고정 · 등록/수정: /내시간대 · 🚫 = /불가 예외")
        await interaction.response.send_message(embed=embed, ephemeral=나만보기)


async def setup(bot: commands.Bot):
    await bot.add_cog(Availability())
