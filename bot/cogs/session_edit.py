"""/공대일정수정 — 일정을 여러 개 골라 한 번에 시각·메모를 바꾸거나, 완료/미완료를 표시하거나, 내 캐릭터를 빼고 교체한다.
/일정완료 — 방금 끝난 일정 하나에 완료/미완료를 바로 표시한다.

공대장·서버 관리자는 모든 일정, 그 외에는 내 캐릭터가 참가한 일정만 목록에 나온다.
"""
from __future__ import annotations

from datetime import timedelta

import discord
from discord import app_commands
from discord.ext import commands

from ..common import CAT_INPUT, bot_of, ensure_member, is_leader, meta
from ..render import participants_block, session_title, truncate
from ..services import RuleError, SessionView
from ..session_edit import (
    EditRequest, apply_edit, editable_sessions, leave_mine, mark_cleared, parse_shift, session_line, swap_mine,
)
from ..timeutil import now_kst, to_epoch

PAGE_SIZE = 25  # 디스코드 드롭다운 한도


async def _announce(interaction: discord.Interaction, text: str) -> None:
    """알림 채널이 있으면 거기에, 없으면 현재 채널에 공개로 알린다."""
    bot = bot_of(interaction)
    settings = bot.db.get_settings(interaction.guild_id or 0)
    channel = bot.get_channel(settings.notify_channel_id) if settings.notify_channel_id else interaction.channel
    if isinstance(channel, discord.abc.Messageable):
        await channel.send(truncate(text, 2000), allowed_mentions=discord.AllowedMentions(users=True))


class SessionEditView(discord.ui.View):
    def __init__(self, bot, guild_id: int, user_id: int, member_id: int, leader: bool):
        super().__init__(timeout=600)
        self.bot = bot
        self.guild_id, self.user_id, self.member_id, self.leader = guild_id, user_id, member_id, leader
        self.selected: set[int] = set()
        self.page = 0
        self.picker: discord.ui.Select | None = None
        self.sessions: list[SessionView] = []
        self._reload_sessions()
        if not self.sessions:
            for b in (self.edit_button, self.leave_button, self.swap_button,
                      self.done_button, self.failed_button, self.auto_button, self.prev_button, self.next_button):
                self.remove_item(b)
        self._rebuild()

    # ---- 상태 ------------------------------------------------------------
    def _reload_sessions(self) -> None:
        self.sessions = editable_sessions(self.bot.db, self.bot.cfg, self.guild_id, now_kst(), self.member_id, self.leader)
        ids = {v.session.id for v in self.sessions}
        self.selected &= ids
        pages = max(1, -(-len(self.sessions) // PAGE_SIZE))
        # 처음 열 때는 지금과 가장 가까운(아직 안 끝난) 일정이 있는 페이지로
        self.page = min(self.page, pages - 1)

    @property
    def pages(self) -> int:
        return max(1, -(-len(self.sessions) // PAGE_SIZE))

    def _page_items(self) -> list[SessionView]:
        return self.sessions[self.page * PAGE_SIZE:(self.page + 1) * PAGE_SIZE]

    def _rebuild(self) -> None:
        """현재 페이지의 드롭다운과 페이지 버튼 상태를 맞춘다."""
        if self.picker is not None:
            self.remove_item(self.picker)
            self.picker = None
        items = self._page_items()
        if items:
            now = now_kst()
            self.picker = discord.ui.Select(
                placeholder=f"고칠 일정을 고르세요 (여러 개 가능) · {self.page + 1}/{self.pages} 쪽",
                min_values=0, max_values=len(items), row=0,
                options=[
                    discord.SelectOption(
                        label=session_line(v, now)[:100], value=str(v.session.id),
                        description=(v.session.memo or "")[:100] or None, default=v.session.id in self.selected,
                    )
                    for v in items
                ],
            )
            self.picker.callback = self.on_pick
            self.add_item(self.picker)
        if self.sessions:
            self.prev_button.disabled = self.page <= 0
            self.next_button.disabled = self.page >= self.pages - 1

    def content(self, notice: str = "") -> str:
        who = "공대장/관리자: 모든 일정을 고칠 수 있어요." if self.leader else "내가 참가한 일정만 고칠 수 있어요."
        if not self.sessions:
            body = "_고칠 수 있는 일정이 없어요._"
        else:
            now = now_kst()
            body = "\n".join(
                ("☑️ " if v.session.id in self.selected else "▫️ ") + session_line(v, now) for v in self._page_items()
            )
        parts = [
            f"**공대 일정 수정** — {who}\n"
            f"일정을 고른 뒤 아래 버튼으로 고른 일정 **전체**에 적용해요. (선택 {len(self.selected)}개, 쪽을 넘겨도 유지)\n"
            "-# 시각·메모는 시작 전 일정만, 완료/미완료 표시는 시작한 일정만 바꿀 수 있어요.",
            body,
        ]
        if notice:
            parts.append(notice)
        return truncate("\n\n".join(parts), 1900)

    async def refresh(self, interaction: discord.Interaction, notice: str = "", reload: bool = True) -> None:
        if reload:
            self._reload_sessions()
        self._rebuild()
        await interaction.response.edit_message(content=self.content(notice), view=self)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.user_id:
            await interaction.response.send_message("이 창을 연 사람만 쓸 수 있어요.", ephemeral=True)
            return False
        return True

    async def on_pick(self, interaction: discord.Interaction):
        page_ids = {v.session.id for v in self._page_items()}
        self.selected = (self.selected - page_ids) | {int(x) for x in self.picker.values}
        await self.refresh(interaction, reload=False)

    async def _need_selection(self, interaction: discord.Interaction) -> bool:
        if self.selected:
            return True
        await self.refresh(interaction, "⚠️ 먼저 고칠 일정을 골라 주세요.", reload=False)
        return False

    def _ordered_selection(self) -> list[int]:
        order = {v.session.id: i for i, v in enumerate(self.sessions)}
        return sorted(self.selected, key=lambda i: order.get(i, 0))

    # ---- 버튼 ------------------------------------------------------------
    @discord.ui.button(label="시각·메모 수정", style=discord.ButtonStyle.primary, row=1)
    async def edit_button(self, interaction: discord.Interaction, _: discord.ui.Button):
        if await self._need_selection(interaction):
            await interaction.response.send_modal(EditModal(self))

    @discord.ui.button(label="내 캐릭터 빼기", style=discord.ButtonStyle.secondary, row=1)
    async def leave_button(self, interaction: discord.Interaction, _: discord.ui.Button):
        if not await self._need_selection(interaction):
            return
        now = now_kst()
        done, notes = [], []
        for sid in self._ordered_selection():
            try:
                text = leave_mine(self.bot.db, self.bot.cfg, self.guild_id, sid, self.member_id, now)
            except RuleError as e:
                notes.append(f"⚠️ {e}")
                continue
            done.append(f"{session_title(self.bot.cfg, self.bot.db.get_session(sid))} — {text}")
        if done:
            await _announce(interaction, f"➖ **{interaction.user.display_name}** 일정 빠짐\n" + "\n".join(done))
        await self.refresh(interaction, "\n".join((["✅ 처리했어요."] if done else []) + notes))

    @discord.ui.button(label="내 캐릭터 교체", style=discord.ButtonStyle.secondary, row=1)
    async def swap_button(self, interaction: discord.Interaction, _: discord.ui.Button):
        if not await self._need_selection(interaction):
            return
        chars = self.bot.db.list_characters(self.member_id)
        if not chars:
            return await self.refresh(interaction, "⚠️ 등록된 내 캐릭터가 없어요. `/캐릭터등록` 먼저 해 주세요.", reload=False)
        await interaction.response.send_message(
            "선택한 일정의 내 캐릭터를 어떤 캐릭터로 바꿀까요?", view=SwapView(self, chars), ephemeral=True
        )

    async def _mark(self, interaction: discord.Interaction, value: int | None):
        if not await self._need_selection(interaction):
            return
        now = now_kst()
        ok, notes = 0, []
        for sid in self._ordered_selection():
            try:
                mark_cleared(self.bot.db, self.guild_id, sid, value, self.member_id, self.leader, now)
                ok += 1
            except RuleError as e:
                notes.append(f"⚠️ {e}")
        label = {1: "✅ 완료", 0: "❌ 미완료", None: "↩️ 자동"}[value]
        await self.refresh(interaction, "\n".join(([f"{label} 로 {ok}개 표시했어요."] if ok else []) + notes))

    @discord.ui.button(label="✅ 완료 표시", style=discord.ButtonStyle.success, row=2)
    async def done_button(self, interaction: discord.Interaction, _: discord.ui.Button):
        await self._mark(interaction, 1)

    @discord.ui.button(label="❌ 미완료 표시", style=discord.ButtonStyle.danger, row=2)
    async def failed_button(self, interaction: discord.Interaction, _: discord.ui.Button):
        await self._mark(interaction, 0)

    @discord.ui.button(label="↩️ 자동으로", style=discord.ButtonStyle.secondary, row=2)
    async def auto_button(self, interaction: discord.Interaction, _: discord.ui.Button):
        await self._mark(interaction, None)

    @discord.ui.button(label="◀ 이전", style=discord.ButtonStyle.secondary, row=3)
    async def prev_button(self, interaction: discord.Interaction, _: discord.ui.Button):
        self.page = max(0, self.page - 1)
        await self.refresh(interaction, reload=False)

    @discord.ui.button(label="다음 ▶", style=discord.ButtonStyle.secondary, row=3)
    async def next_button(self, interaction: discord.Interaction, _: discord.ui.Button):
        self.page = min(self.pages - 1, self.page + 1)
        await self.refresh(interaction, reload=False)

    @discord.ui.button(label="닫기", style=discord.ButtonStyle.secondary, row=3)
    async def close_button(self, interaction: discord.Interaction, _: discord.ui.Button):
        self.stop()
        await interaction.response.edit_message(content="닫았어요.", view=None)


class EditModal(discord.ui.Modal, title="일정 일괄 수정"):
    date = discord.ui.TextInput(label="날짜 (비우면 유지)", placeholder="예: 10/3, 금, 내일", required=False, max_length=20)
    time = discord.ui.TextInput(label="시각 (비우면 유지)", placeholder="예: 21:00", required=False, max_length=10)
    shift = discord.ui.TextInput(
        label="시간 밀기 (분, 비우면 없음)", placeholder="예: +60 (1시간 뒤로) / -30", required=False, max_length=7
    )
    memo = discord.ui.TextInput(label="메모 (비우면 유지, - 면 삭제)", required=False, max_length=100)

    def __init__(self, parent: SessionEditView):
        super().__init__()
        self.parent = parent

    async def on_submit(self, interaction: discord.Interaction):
        p = self.parent
        try:
            req = EditRequest(self.date.value, self.time.value, parse_shift(self.shift.value), self.memo.value)
            if req.empty:
                raise RuleError("바꿀 항목을 하나 이상 입력해 주세요.")
        except RuleError as e:
            return await p.refresh(interaction, f"⚠️ {e}", reload=False)
        now = now_kst()
        lines, notes = [], []
        for sid in p._ordered_selection():
            try:
                changes = apply_edit(p.bot.db, p.bot.cfg, p.guild_id, sid, req, p.member_id, p.leader, now)
            except RuleError as e:
                notes.append(f"⚠️ {e}")
                continue
            s = p.bot.db.get_session(sid)
            chars = p.bot.db.session_characters(sid)
            lines.append(
                f"✏️ **{session_title(p.bot.cfg, s)}**\n" + "\n".join(changes)
                + ("\n" + participants_block(chars, mention=True) if chars else "")
            )
        if lines:
            await _announce(interaction, f"🛠️ **{interaction.user.display_name}** 님이 일정을 수정했어요\n\n" + "\n\n".join(lines))
        head = [f"✅ {len(lines)}개 수정했어요."] if lines else []
        await p.refresh(interaction, "\n".join(head + notes))

    async def on_error(self, interaction: discord.Interaction, error: Exception) -> None:
        msg = f"⚠️ {error}" if isinstance(error, RuleError) else "😵 처리 중 오류가 났어요."
        if interaction.response.is_done():
            await interaction.followup.send(msg, ephemeral=True)
        else:
            await interaction.response.send_message(msg, ephemeral=True)


class SwapView(discord.ui.View):
    def __init__(self, parent: SessionEditView, chars):
        super().__init__(timeout=120)
        self.parent = parent
        self.chars = {str(c.id): c for c in chars}
        select = discord.ui.Select(
            placeholder="바꿀 캐릭터",
            options=[
                discord.SelectOption(label=f"{c.name} ({c.cls} {c.item_level:g})"[:100], value=str(c.id))
                for c in sorted(chars, key=lambda c: -c.item_level)[:25]
            ],
        )
        select.callback = self.picked
        self.add_item(select)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        return interaction.user.id == self.parent.user_id

    async def picked(self, interaction: discord.Interaction):
        p = self.parent
        char = self.chars[self.children[0].values[0]]
        now = now_kst()
        done, notes = [], []
        for sid in p._ordered_selection():
            try:
                text = swap_mine(p.bot.db, p.bot.cfg, p.guild_id, sid, char, now)
            except RuleError as e:
                notes.append(f"⚠️ {e}")
                continue
            done.append(f"{session_title(p.bot.cfg, p.bot.db.get_session(sid))} — {text}")
        self.stop()
        await interaction.response.edit_message(
            content="\n".join(([f"✅ {len(done)}개 교체했어요."] if done else []) + notes) or "바꿀 게 없어요.", view=None
        )
        if done:
            await _announce(interaction, f"🔁 **{interaction.user.display_name}** 캐릭터 교체\n" + "\n".join(done))


async def recent_session_autocomplete(interaction: discord.Interaction, current: str):
    """/일정완료 용: 최근 3일 안에 시작한 일정과 곧 시작할 일정."""
    bot = bot_of(interaction)
    now = now_kst()
    sessions = bot.db.list_sessions(interaction.guild_id or 0, to_epoch(now - timedelta(days=3)), to_epoch(now + timedelta(hours=3)))
    choices = []
    for s in reversed(sessions):  # 최근 것부터
        v = SessionView(s, bot.cfg.raid(s.raid_key), bot.db.session_characters(s.id))
        label = session_line(v, now)[:100]
        if current.strip("#") in label:
            choices.append(app_commands.Choice(name=label, value=s.id))
    return choices[:25]


class SessionEdit(commands.Cog):
    @app_commands.command(
        name="공대일정수정",
        description="공대 일정을 여러 개 골라 한 번에 시각·메모 수정, 완료/미완료 표시, 캐릭터 교체를 합니다",
        extras=meta(
            CAT_INPUT,
            "/공대일정수정 → 일정 고르기(여러 개) → [시각·메모 수정] / [✅ 완료 표시] / [❌ 미완료 표시] / [내 캐릭터 교체]",
            "공대장·서버 관리자는 모든 일정을, 그 외에는 내 캐릭터가 참가한 일정만 고칠 수 있어요. 일정이 많으면 ◀ ▶ 로 쪽을 넘겨요.\n"
            "[시각·메모 수정]: 날짜·시각을 정하거나, '시간 밀기'로 +60(1시간 뒤)/-30 처럼 한꺼번에 옮겨요. "
            "비운 항목은 그대로 둬요. 바뀐 내용은 알림 채널에 참가자 멘션과 함께 올라가요.\n"
            "[✅ 완료 표시]/[❌ 미완료 표시]: 시작한 일정에 결과를 표시해요. 미완료는 그 레이드가 다시 '안 잡힌 숙제'로 돌아가요. "
            "표시하지 않으면 일정 끝 시각이 지난 뒤 자동으로 완료로 봐요.\n"
            "[내 캐릭터 교체]: 고른 일정마다 규칙(입장 레벨·주 1회·서폿)을 검사하고, 안 되는 일정은 원래대로 둬요.",
        ),
    )
    @app_commands.guild_only()
    async def edit_sessions(self, interaction: discord.Interaction):
        bot = bot_of(interaction)
        member = ensure_member(interaction)
        view = SessionEditView(bot, interaction.guild_id or 0, interaction.user.id, member.id, is_leader(interaction))
        await interaction.response.send_message(view.content(), view=view, ephemeral=True)

    @app_commands.command(
        name="일정완료",
        description="끝난 일정에 완료/미완료를 표시합니다 (표시 안 하면 끝 시각이 지난 뒤 자동으로 완료)",
        extras=meta(
            CAT_INPUT,
            "/일정완료 일정:#12  ·  /일정완료 일정:#12 결과:미완료",
            "시작한 일정에 결과를 직접 표시해요. 미완료로 표시하면 ❌ 로 보이고, 그 레이드는 다시 잡아야 할 숙제로 돌아가요.\n"
            "잘못 눌렀으면 결과:자동 으로 되돌려요. 공대장·서버 관리자는 모든 일정, 그 외에는 내가 참가한 일정만 표시할 수 있어요.\n"
            "여러 개를 한꺼번에 표시하려면 `/공대일정수정`.",
        ),
    )
    @app_commands.describe(일정="표시할 일정 (최근 3일 안에 시작한 일정)", 결과="완료(기본) / 미완료 / 자동")
    @app_commands.choices(결과=[
        app_commands.Choice(name="완료", value="done"),
        app_commands.Choice(name="미완료", value="failed"),
        app_commands.Choice(name="자동 (끝 시각 기준)", value="auto"),
    ])
    @app_commands.autocomplete(일정=recent_session_autocomplete)
    @app_commands.guild_only()
    async def mark(self, interaction: discord.Interaction, 일정: int, 결과: app_commands.Choice[str] | None = None):
        bot = bot_of(interaction)
        member = ensure_member(interaction)
        value = {"done": 1, "failed": 0, "auto": None}[결과.value if 결과 else "done"]
        text = mark_cleared(bot.db, interaction.guild_id or 0, 일정, value, member.id, is_leader(interaction), now_kst())
        s = bot.db.get_session(일정)
        await interaction.response.send_message(f"{text}: **{session_title(bot.cfg, s)}**", ephemeral=True)


async def setup(bot: commands.Bot):
    await bot.add_cog(SessionEdit())
