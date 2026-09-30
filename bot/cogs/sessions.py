from __future__ import annotations

from datetime import datetime, timedelta

import discord
from discord import app_commands
from discord.ext import commands

from ..common import (
    CAT_INPUT, CAT_LEADER, CAT_START, CAT_VIEW, bot_of, ensure_member, find_guild_character,
    guild_character_autocomplete, leader_only, meta, my_character_autocomplete,
    raid_autocomplete, session_autocomplete,
)
from ..db import Character, Member, Session
from ..render import (
    OverviewCards, member_view_chunks, my_schedule_text, overview_cards, pack_by_limits, participants_block,
    session_title, weekly_availability_text,
)
from ..services import PERIODS, RuleError, SessionView, add_to_session, build_overview, party_count, week_bounds
from ..timeutil import KST, fmt_date, fmt_dt, from_epoch, now_kst, parse_date, parse_time, to_epoch


def _card_embeds(cards: OverviewCards, title: str, footer: str) -> list[discord.Embed]:
    """일정별 보기: 머리 임베드 + (끝난 일정) + 일정마다 임베드(1파티 왼쪽·2파티 오른쪽) + (골드 남은 캐릭터 등)."""
    color = discord.Color.gold()
    embeds = [discord.Embed(title=title, description=cards.notice or None, color=color)]
    if cards.past:
        embeds.append(discord.Embed(title="끝난 일정", description=cards.past, color=color))
    for card in cards.sessions:
        embed = discord.Embed(title=card.title, description=card.description or None, color=color)
        for name, value, inline in card.fields:
            embed.add_field(name=name, value=value, inline=inline)
        embeds.append(embed)
    if cards.tail:
        embeds.append(discord.Embed(description=cards.tail, color=color))
    embeds[-1].set_footer(text=footer)
    return embeds


def _pack_messages(embeds: list[discord.Embed]) -> list[list[discord.Embed]]:
    """메시지 하나에 임베드 10개·글자 합계 6000자 제한이 있어서 나눠 담는다."""
    return [[embeds[i] for i in group] for group in pack_by_limits([len(e) for e in embeds])]


def _load_session(interaction: discord.Interaction, session_id: int) -> Session:
    s = bot_of(interaction).db.get_session(session_id)
    if s is None or s.guild_id != interaction.guild_id:
        raise RuleError(f"#{session_id} 일정을 찾을 수 없어요.")
    if s.status != "scheduled":
        raise RuleError(f"#{session_id} 은 취소된 일정이에요.")
    return s


def _view(interaction: discord.Interaction, s: Session) -> SessionView:
    bot = bot_of(interaction)
    return SessionView(s, bot.cfg.raid(s.raid_key), bot.db.session_characters(s.id))


def _parse_start(date_text: str, time_text: str) -> datetime:
    today = now_kst().date()
    return datetime.combine(parse_date(date_text, today), parse_time(time_text), KST)


async def _announce(interaction: discord.Interaction, text: str, view: discord.ui.View | None = None) -> None:
    """현재 채널에 공개 응답하고, 알림 채널이 따로 있으면 거기에도 올린다."""
    await interaction.response.send_message(text, view=view or discord.utils.MISSING)
    settings = bot_of(interaction).db.get_settings(interaction.guild_id or 0)
    if settings.notify_channel_id and settings.notify_channel_id != interaction.channel_id:
        channel = interaction.client.get_channel(settings.notify_channel_id)
        if isinstance(channel, discord.abc.Messageable):
            await channel.send(text, view=view or discord.utils.MISSING)


def join_session(bot, s: Session, char: Character, by_leader: bool) -> str:
    """캐릭터를 일정에 넣고 공지 문구를 돌려준다. 규칙 위반이면 RuleError."""
    # 공대원 본인 참가는 서폿 규칙 강제, 공대장 수동 배정은 허용하되 경고
    party, warnings = add_to_session(bot.db, bot.cfg, s, char, now_kst(), strict=not by_leader)
    v = SessionView(s, bot.cfg.raid(s.raid_key), bot.db.session_characters(s.id))
    who = f"<@{char.discord_id}>" if by_leader else char.display_name
    where = f" {party}파티" if party_count(v.raid) > 1 else ""
    role = "서폿" if bot.cfg.is_support(char.cls) else "딜러"
    text = (
        f"➕ {who} {char.name}({char.cls} {char.item_level:g} · {role}) → "
        f"**{session_title(bot.cfg, s)}**{where} `{v.filled}`"
    )
    if warnings:
        text += "\n" + "\n".join(f"-# {w if w.startswith('⚠️') else '⚠️ ' + w}" for w in warnings)
    if v.raid and len(v.characters) == v.raid.party_size:
        text += "\n🟢 정원이 다 찼어요!"
    return text


def leave_session(bot, s: Session, member: Member) -> str:
    mine = [c for c in bot.db.session_characters(s.id) if c.member_id == member.id]
    if not mine:
        raise RuleError("이 일정에 참가하고 있지 않아요.")
    for c in mine:
        bot.db.remove_slot(s.id, c.id)
    v = SessionView(s, bot.cfg.raid(s.raid_key), bot.db.session_characters(s.id))
    return (
        f"➖ {member.display_name}({', '.join(c.name for c in mine)}) → **{session_title(bot.cfg, s)}** "
        f"에서 빠졌어요 `{v.filled}`"
    )


# ---- 일정 카드의 참가/불참 버튼 ------------------------------------------
# 봇이 재시작돼도 옛 메시지의 버튼이 동작하도록 custom_id 에 일정 번호를 넣은 DynamicItem 으로 만든다.

def _button_session(interaction: discord.Interaction, session_id: int) -> Session:
    return _load_session(interaction, session_id)


class _SessionButton(discord.ui.DynamicItem[discord.ui.Button], template=r"raid:(?P<action>join|leave):(?P<sid>\d+)"):
    def __init__(self, action: str, session_id: int):
        label, style = ("참가", discord.ButtonStyle.success) if action == "join" else ("빠지기", discord.ButtonStyle.secondary)
        super().__init__(discord.ui.Button(label=label, style=style, custom_id=f"raid:{action}:{session_id}"))
        self.action = action
        self.session_id = session_id

    @classmethod
    async def from_custom_id(cls, interaction, item, match):
        return cls(match["action"], int(match["sid"]))

    async def callback(self, interaction: discord.Interaction):
        try:
            if self.action == "join":
                await self._join(interaction)
            else:
                bot = bot_of(interaction)
                member = ensure_member(interaction)
                s = _button_session(interaction, self.session_id)
                await interaction.response.send_message(leave_session(bot, s, member))
        except RuleError as e:
            if interaction.response.is_done():
                await interaction.followup.send(f"⚠️ {e}", ephemeral=True)
            else:
                await interaction.response.send_message(f"⚠️ {e}", ephemeral=True)

    async def _join(self, interaction: discord.Interaction):
        bot = bot_of(interaction)
        member = ensure_member(interaction)
        s = _button_session(interaction, self.session_id)
        raid = bot.cfg.raid(s.raid_key)
        in_session = {c.id for c in bot.db.session_characters(s.id)}
        chars = [
            c for c in bot.db.list_characters(member.id)
            if c.id not in in_session and (raid is None or c.item_level >= raid.min_item_level)
        ]
        if not chars:
            raise RuleError("참가할 수 있는 내 캐릭터가 없어요 (입장 레벨 미달이거나 이미 참가 중). `/캐릭터등록` 을 확인해 주세요.")
        if len(chars) == 1:
            await interaction.response.send_message(join_session(bot, s, chars[0], by_leader=False))
            return
        await interaction.response.send_message(
            "어떤 캐릭터로 참가할까요?", view=CharacterPickView(s.id, chars, interaction.user.id), ephemeral=True
        )


class CharacterPickView(discord.ui.View):
    def __init__(self, session_id: int, chars: list[Character], owner_id: int):
        super().__init__(timeout=120)
        self.session_id = session_id
        self.owner_id = owner_id
        self.select = discord.ui.Select(
            placeholder="캐릭터 선택",
            options=[
                discord.SelectOption(label=f"{c.name} ({c.cls} {c.item_level:g})"[:100], value=str(c.id))
                for c in sorted(chars, key=lambda c: -c.item_level)[:25]
            ],
        )
        self.select.callback = self.picked
        self.add_item(self.select)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        return interaction.user.id == self.owner_id

    async def picked(self, interaction: discord.Interaction):
        bot = bot_of(interaction)
        try:
            s = _button_session(interaction, self.session_id)
            char = bot.db.get_character(int(self.select.values[0]))
            text = join_session(bot, s, char, by_leader=False)
        except RuleError as e:
            await interaction.response.edit_message(content=f"⚠️ {e}", view=None)
            return
        await interaction.response.edit_message(content="✅ 참가했어요.", view=None)
        await interaction.followup.send(text)  # 공개 메시지


class SessionButtons(discord.ui.View):
    """일정 공지 메시지에 붙이는 [참가] [빠지기] 버튼."""

    def __init__(self, session_id: int):
        super().__init__(timeout=None)
        self.add_item(_SessionButton("join", session_id))
        self.add_item(_SessionButton("leave", session_id))


class Sessions(commands.Cog):
    # ---- 공대장 ----------------------------------------------------------
    @app_commands.command(
        name="일정등록",
        description="레이드 일정을 수동으로 등록합니다",
        extras=meta(
            CAT_LEADER,
            "/일정등록 레이드:에기르 하드 날짜:10/3 시각:21:00",
            "날짜는 10/3, 2026-10-03, 오늘, 내일, 금 같은 형식을 받아요. 시각은 21:00, 21시, 21시30분.\n"
            "시각을 비우면 기본 시각(config.toml 의 default_start)으로 만들고 메모에 '시간 미정' 을 붙여요. `/일정수정` 으로 나중에 바꾸세요.\n"
            "등록 후 공대원이 `/일정참가` 로 캐릭터를 넣거나, 공대장이 `/일정배정` 으로 넣을 수 있어요.",
            leader=True,
        ),
    )
    @app_commands.describe(레이드="레이드", 날짜="예: 10/3, 내일, 금", 시각="예: 21:00 (비우면 기본 시각)", 메모="선택 (예: 숙련 파티)")
    @app_commands.autocomplete(레이드=raid_autocomplete)
    @leader_only()
    async def create(
        self, interaction: discord.Interaction, 레이드: str, 날짜: str, 시각: str | None = None,
        메모: app_commands.Range[str, 1, 100] | None = None,
    ):
        bot = bot_of(interaction)
        raid = bot.cfg.raid(레이드)
        if raid is None:
            raise RuleError("목록에서 레이드를 골라 주세요.")
        # 시각을 비우면 기본 시각으로 만들고 메모에 표시한다 (나중에 /일정수정 으로 시각을 바꾼다)
        start = _parse_start(날짜, 시각 or bot.cfg.default_start)
        if not 시각:
            메모 = f"⏱️ 시간 미정 (임시 {bot.cfg.default_start})" + (f" · {메모}" if 메모 else "")
        if start <= now_kst():
            raise RuleError(f"{fmt_dt(start)} 은 이미 지난 시각이에요.")
        if raid.event and not raid.is_active(start.date()):
            raise RuleError(f"{raid.name} 은 {fmt_date(raid.ends_on)} 에 끝나는 이벤트라 그 이후엔 잡을 수 없어요.")
        s = bot.db.create_session(interaction.guild_id or 0, raid.key, to_epoch(start), interaction.user.id, 메모)
        await _announce(
            interaction,
            f"📌 새 일정 **{session_title(bot.cfg, s)}** ({raid.party_size}인, {raid.min_item_level:g}+)"
            + (f"\n> {메모}" if 메모 else "")
            + f"\n참가: 아래 버튼 또는 `/일정참가 일정:#{s.id}`",
            SessionButtons(s.id),
        )

    @app_commands.command(
        name="일정수정",
        description="일정의 날짜/시각/메모를 바꾸고 참가자에게 알립니다",
        extras=meta(CAT_LEADER, "/일정수정 일정:#12 시각:22:00", leader=True),
    )
    @app_commands.describe(일정="바꿀 일정", 날짜="새 날짜 (안 바꾸면 비워 두기)", 시각="새 시각", 메모="새 메모 (- 입력 시 삭제)")
    @app_commands.autocomplete(일정=session_autocomplete)
    @leader_only()
    async def edit(
        self, interaction: discord.Interaction, 일정: int,
        날짜: str | None = None, 시각: str | None = None,
        메모: app_commands.Range[str, 1, 100] | None = None,
    ):
        bot = bot_of(interaction)
        s = _load_session(interaction, 일정)
        if 날짜 is None and 시각 is None and 메모 is None:
            raise RuleError("바꿀 항목(날짜/시각/메모)을 하나 이상 입력해 주세요.")
        old = from_epoch(s.start_at)
        changes = []
        if 날짜 is not None or 시각 is not None:
            new_date = parse_date(날짜, now_kst().date()) if 날짜 else old.date()
            new_time = parse_time(시각) if 시각 else old.timetz().replace(tzinfo=None)
            new = datetime.combine(new_date, new_time, KST)
            if new <= now_kst():
                raise RuleError(f"{fmt_dt(new)} 은 이미 지난 시각이에요.")
            bot.db.update_session_time(s.id, to_epoch(new))
            changes.append(f"🕘 {fmt_dt(old)} → **{fmt_dt(new)}**")
        if 메모 is not None:
            memo = None if 메모.strip() == "-" else 메모
            bot.db.update_session_memo(s.id, memo)
            changes.append(f"📝 메모: {memo or '(삭제)'}")
        s = bot.db.get_session(s.id)
        v = _view(interaction, s)
        await _announce(
            interaction,
            f"✏️ 일정 변경 **{session_title(bot.cfg, s)}**\n" + "\n".join(changes)
            + "\n" + participants_block(v.characters, mention=True),
        )

    @app_commands.command(
        name="일정취소",
        description="일정을 취소하고 참가자에게 알립니다",
        extras=meta(CAT_LEADER, "/일정취소 일정:#12", leader=True),
    )
    @app_commands.autocomplete(일정=session_autocomplete)
    @leader_only()
    async def cancel(self, interaction: discord.Interaction, 일정: int):
        bot = bot_of(interaction)
        s = _load_session(interaction, 일정)
        v = _view(interaction, s)
        bot.db.cancel_session(s.id)
        await _announce(
            interaction,
            f"❌ 일정 취소 ~~{session_title(bot.cfg, s)}~~\n" + participants_block(v.characters, mention=True),
        )

    @app_commands.command(
        name="일정배정",
        description="공대원의 캐릭터를 일정에 넣습니다",
        extras=meta(CAT_LEADER, "/일정배정 일정:#12 캐릭터:달빛바드", leader=True),
    )
    @app_commands.autocomplete(일정=session_autocomplete, 캐릭터=guild_character_autocomplete)
    @leader_only()
    async def assign(self, interaction: discord.Interaction, 일정: int, 캐릭터: str):
        char = find_guild_character(interaction, 캐릭터)
        if char is None:
            raise RuleError(f"'{캐릭터}' 캐릭터를 찾을 수 없어요. 목록에서 골라 주세요.")
        await self._join(interaction, 일정, char.id, by_leader=True)

    @app_commands.command(
        name="일정제외",
        description="일정에서 캐릭터를 뺍니다",
        extras=meta(CAT_LEADER, "/일정제외 일정:#12 캐릭터:달빛바드", leader=True),
    )
    @app_commands.autocomplete(일정=session_autocomplete, 캐릭터=guild_character_autocomplete)
    @leader_only()
    async def unassign(self, interaction: discord.Interaction, 일정: int, 캐릭터: str):
        bot = bot_of(interaction)
        s = _load_session(interaction, 일정)
        char = find_guild_character(interaction, 캐릭터)
        if char is None or not bot.db.remove_slot(s.id, char.id):
            raise RuleError(f"'{캐릭터}' 은(는) 이 일정에 없어요.")
        v = _view(interaction, s)
        await interaction.response.send_message(
            f"➖ {char.display_name}({char.name}) → **{session_title(bot.cfg, s)}** 에서 제외 `{v.filled}`"
        )

    # ---- 공대원 ----------------------------------------------------------
    @app_commands.command(
        name="일정참가",
        description="내 캐릭터로 일정에 참가합니다",
        extras=meta(
            CAT_INPUT,
            "/일정참가 일정:#12 캐릭터:달빛바드",
            "입장 레벨, 정원, 같은 캐릭터의 주간 중복을 확인해요. 같은 시간대 다른 일정에 참가 중이어도 막지 않고 경고만 해요.",
        ),
    )
    @app_commands.autocomplete(일정=session_autocomplete, 캐릭터=my_character_autocomplete)
    async def join(self, interaction: discord.Interaction, 일정: int, 캐릭터: str):
        member = ensure_member(interaction)
        char = next((c for c in bot_of(interaction).db.list_characters(member.id) if c.name == 캐릭터), None)
        if char is None:
            raise RuleError(f"'{캐릭터}' 은(는) 내 캐릭터가 아니에요. `/캐릭터등록` 먼저 해 주세요.")
        await self._join(interaction, 일정, char.id, by_leader=False)

    async def _join(self, interaction: discord.Interaction, session_id: int, character_id: int, by_leader: bool):
        bot = bot_of(interaction)
        s = _load_session(interaction, session_id)
        char = bot.db.get_character(character_id)
        assert char is not None
        text = join_session(bot, s, char, by_leader)
        await interaction.response.send_message(text)

    @app_commands.command(
        name="일정빠지기",
        description="참가한 일정에서 내 캐릭터를 뺍니다",
        extras=meta(CAT_INPUT, "/일정빠지기 일정:#12"),
    )
    @app_commands.autocomplete(일정=session_autocomplete)
    async def leave(self, interaction: discord.Interaction, 일정: int):
        bot = bot_of(interaction)
        member = ensure_member(interaction)
        s = _load_session(interaction, 일정)
        await interaction.response.send_message(leave_session(bot, s, member))

    # ---- 조회 ------------------------------------------------------------
    @app_commands.command(
        name="공대일정",
        description="공대 일정을 봅니다 (날짜순 일정별 보기 / 공대원별로 캐릭터 묶어 보기)",
        extras=meta(
            CAT_START,
            "/공대일정  ·  /공대일정 기간:금  ·  /공대일정 기간:오늘  ·  /공대일정 보기:공대원별",
            "기본은 일정별 보기예요: 날짜순 일정 목록과 인원 현황(6/8)을 보여 줘요.\n"
            "보기:공대원별 은 공대원마다 캐릭터를 묶고, 캐릭터별로 잡힌 일정과 아직 안 잡힌 골드 레이드를 보여 줘요. "
            "내 캐릭터가 맨 위에 와요.\n"
            "기간에 요일(월~일)을 고르면 이번 주 그 요일의 공대 일정만 봐요.\n"
            "보기:가능시간 은 이번 주 날짜별로 누가 몇 시에 되는지 보여 줘요.\n"
            "이번 주는 수요일 06:00 리셋 기준. 기본은 채널에 모두 보이게 올리고, 나만보기:True 면 나만 봐요.",
        ),
    )
    @app_commands.describe(
        기간="이번주(기본) / 오늘 / 요일 — 요일은 이번 주 그 날", 보기="일정별(기본) / 공대원별 / 가능시간",
        레이드="특정 레이드만", 나만보기="나에게만 보이게 (기본: 채널에 공개)",
    )
    @app_commands.choices(
        기간=[app_commands.Choice(name=p if len(p) > 1 else f"{p}요일", value=p) for p in PERIODS],
        보기=[
            app_commands.Choice(name="일정별", value="session"),
            app_commands.Choice(name="공대원별", value="member"),
            app_commands.Choice(name="가능시간 (날짜별 누가 되는지)", value="availability"),
        ],
    )
    @app_commands.autocomplete(레이드=raid_autocomplete)
    async def overview(
        self, interaction: discord.Interaction,
        기간: app_commands.Choice[str] | None = None,
        보기: app_commands.Choice[str] | None = None,
        레이드: str | None = None,
        나만보기: bool = False,
    ):
        bot = bot_of(interaction)
        period = 기간.value if 기간 else "이번주"
        mode = 보기.value if 보기 else "session"
        if 레이드 and bot.cfg.raid(레이드) is None:
            raise RuleError("목록에서 레이드를 골라 주세요.")
        ov = build_overview(bot.db, bot.cfg, interaction.guild_id or 0, now_kst(), period, 레이드)
        if not ov.single_day:
            title = f"이번 주 공대 일정 ({ov.start.month}/{ov.start.day} ~ {ov.end.month}/{ov.end.day} 06:00)"
        else:
            title = ("오늘 · " if period == "오늘" else "") + f"{fmt_date(ov.start.date())} 공대 일정"
        if 레이드:
            title += f" · {bot.cfg.raids[레이드].name}"

        if mode == "availability":
            guild_id = interaction.guild_id or 0
            names = {m.id: m.display_name for m in bot.db.list_members(guild_id)}
            week_start = week_bounds(bot.cfg, now_kst())[0]
            only = ov.start.weekday() if ov.single_day else None
            chunks = [weekly_availability_text(
                bot.db.list_guild_availability(guild_id), names, week_start, only,
                blocked=bot.db.list_guild_unavailable(guild_id, to_epoch(week_start)),
            )]
            title = title.replace("공대 일정", "공대 가능 시간")
            footer = "등록/수정: /가능시간대 · 시간별 인원 표: /가능현황 보기:표"
        elif mode == "session":
            chunks = []
            footer = "✅ 끝난 일정  ❌ 미완료  ·  참가: /일정참가"
        else:
            chunks = member_view_chunks(ov, interaction.user.id, cfg=bot.cfg)
            title += " · 공대원별"
            footer = "✅ 끝난 일정  🗓️ 예정  미정 = 아직 안 잡힌 숙제 수  ·  일정별로 보기: /공대일정"

        if mode == "session":
            messages = _pack_messages(_card_embeds(overview_cards(bot.cfg, ov, now_kst()), title, footer))
        else:
            embeds = []
            for i, chunk in enumerate(chunks):
                embed = discord.Embed(
                    title=title if i == 0 else f"{title} ({i + 1}/{len(chunks)})",
                    description=chunk, color=discord.Color.gold(),
                )
                if i == len(chunks) - 1:
                    embed.set_footer(text=footer)
                embeds.append(embed)
            messages = [[e] for e in embeds]
        # 메시지 하나당 임베드 합계 6000자 제한이 있어, 넘치는 부분은 이어지는 메시지로 보낸다
        await interaction.response.send_message(embeds=messages[0], ephemeral=나만보기)
        for group in messages[1:]:
            await interaction.followup.send(embeds=group, ephemeral=나만보기)

    @app_commands.command(
        name="내일정",
        description="내가 들어간 일정(함께하는 공대원 포함)과 아직 안 잡힌 골드 레이드를 봅니다",
        extras=meta(
            CAT_VIEW,
            "/내일정",
            "이번 주와 다음 주에 내 캐릭터가 들어간 일정을 시간순으로, 같은 파티 공대원과 함께 보여 줘요. "
            "이번 주 골드 레이드 중 아직 안 잡힌 것도 캐릭터별로 알려 줘요.",
        ),
    )
    async def my_schedule(self, interaction: discord.Interaction):
        bot = bot_of(interaction)
        member = ensure_member(interaction)
        guild_id = interaction.guild_id or 0
        now = now_kst()
        ov = build_overview(bot.db, bot.cfg, guild_id, now)
        views = [
            SessionView(s, bot.cfg.raid(s.raid_key), bot.db.session_characters(s.id))
            for s in bot.db.list_sessions(guild_id, to_epoch(ov.start), to_epoch(ov.end + timedelta(days=7)))
        ]
        mine = [v for v in views if any(c.member_id == member.id for c in v.characters)]
        row = next((m for m in ov.members if m.member_id == member.id), None)
        remaining = [(r.character, r.remaining) for r in row.characters if r.remaining] if row else []
        embed = discord.Embed(
            title=f"{member.display_name} 님의 일정 ({ov.start.month}/{ov.start.day}~)",
            description=my_schedule_text(mine, member.id, remaining, now),
            color=discord.Color.gold(),
        )
        embed.set_footer(text="✅ 끝난 일정  🗓️ 예정  ·  참가: /일정참가  ·  빠지기: /일정빠지기")
        await interaction.response.send_message(embed=embed, ephemeral=True)


async def setup(bot: commands.Bot):
    bot.add_dynamic_items(_SessionButton)
    await bot.add_cog(Sessions())
