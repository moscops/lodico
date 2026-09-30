"""/일정제안: 자동 매칭 결과를 보여 주고 공대장이 버튼으로 확정."""
from __future__ import annotations

import discord
from discord import app_commands
from discord.ext import commands

from .sessions import SessionButtons
from ..common import CAT_LEADER, bot_of, is_leader, leader_only, meta, raid_autocomplete
from ..parties import party_emoji
from ..planner import PlanResult, Proposal, propose
from ..render import truncate
from ..services import RuleError, SessionView, add_to_session, supports_first
from ..timeutil import fmt_dt, now_kst, to_epoch

MAX_PROPOSALS = 20  # 버튼 20개 + 모두 확정/닫기 = 디스코드 한 메시지 버튼 한도(25) 안


def proposal_line(i: int, p: Proposal, done: bool = False) -> str:
    who = ", ".join(f"{c.display_name}({c.name})" for c in p.characters)
    mark = "✅ " if done else ""
    party = f"{party_emoji(p.party_number)} 파티 {p.party_number} · " if p.party_number else ""
    return (
        f"{mark}**{i}.** {party}{fmt_dt(p.start)} **{p.raid.name}** `{len(p.characters)}/{p.raid.party_size}`\n"
        f"-# 　{who}"
    )


def plan_text(bot, result: PlanResult, confirmed: set[int]) -> str:
    lines = [proposal_line(i + 1, p, i in confirmed) for i, p in enumerate(result.proposals)]
    if not lines:
        lines.append("_지금 조건으로 파티를 채울 수 있는 시간이 없어요._")
    if result.leftovers:
        lines.append("\n**파티를 못 채운 골드 레이드**")
        for key, chars in result.leftovers.items():
            names = ", ".join(f"{c.display_name}({c.name})" for c in chars[:8])
            more = f" 외 {len(chars) - 8}" if len(chars) > 8 else ""
            lines.append(f"• {bot.cfg.raids[key].name} {len(chars)}명: {names}{more}")
    if result.party_unplaced:
        lines.append(
            "\n⚠️ 파티원 모두 되는 시간을 못 찾은 고정 파티: "
            + ", ".join(f"{party_emoji(n)} {n}" for n in result.party_unplaced)
            + " — 파티 시트에 요일·시각을 직접 적거나 가능 시간을 늘려 주세요."
        )
    if result.no_availability:
        lines.append(
            "\n⚠️ 가능 시간 미등록이라 제외: " + ", ".join(result.no_availability)
            + " — `/가능시간대` 또는 시트로 등록해 주세요."
        )
    return truncate("\n".join(lines), 4000)


async def create_from_proposal(interaction: discord.Interaction, p: Proposal) -> tuple[str, list[str], int]:
    """일정을 만들고 캐릭터를 넣는다. 제안 이후 상황이 바뀌었을 수 있어 규칙을 다시 검사한다."""
    bot = bot_of(interaction)
    now = now_kst()
    if p.start <= now:
        raise RuleError("이미 지난 시각의 제안이에요. /일정제안 을 다시 실행해 주세요.")
    guild_id = interaction.guild_id or 0
    memo = f"파티 {p.party_number}" if p.party_number else "자동 제안"
    session = bot.db.find_session(guild_id, p.raid.key, to_epoch(p.start)) or bot.db.create_session(
        guild_id, p.raid.key, to_epoch(p.start), interaction.user.id, memo, party_raid_id=p.party_raid_id
    )
    skipped = []
    for c in supports_first(bot.cfg, p.characters):
        try:
            add_to_session(bot.db, bot.cfg, bot.db.get_session(session.id), c, now, strict=False)
        except RuleError as e:
            skipped.append(f"{c.name}: {e}")
    v = SessionView(session, bot.cfg.raid(session.raid_key), bot.db.session_characters(session.id))
    mentions = " ".join(f"<@{c.discord_id}>" for c in v.characters if c.discord_id)
    text = (
        f"📌 일정 확정 **#{session.id} {fmt_dt(p.start)} {p.raid.name}** `{v.filled}`\n"
        + "\n".join(f"• {c.display_name} — {c.name} ({c.cls} {c.item_level:g})" for c in v.characters)
        + (f"\n{mentions}" if mentions else "")
    )
    return text, skipped, session.id


async def announce(interaction: discord.Interaction, text: str, session_id: int | None = None) -> None:
    bot = bot_of(interaction)
    settings = bot.db.get_settings(interaction.guild_id or 0)
    channel = bot.get_channel(settings.notify_channel_id) if settings.notify_channel_id else interaction.channel
    if isinstance(channel, discord.abc.Messageable):
        view = SessionButtons(session_id) if session_id else discord.utils.MISSING
        await channel.send(truncate(text, 2000), view=view)


class ProposalView(discord.ui.View):
    def __init__(self, bot, result: PlanResult):
        super().__init__(timeout=900)
        self.bot = bot
        self.result = result
        self.confirmed: set[int] = set()
        for i, _ in enumerate(result.proposals[:MAX_PROPOSALS]):
            button = discord.ui.Button(label=f"{i + 1} 확정", style=discord.ButtonStyle.success, row=i // 5)
            button.callback = self._make_confirm(i)
            self.add_item(button)
        if result.proposals:
            all_btn = discord.ui.Button(label="모두 확정", style=discord.ButtonStyle.primary, row=4)
            all_btn.callback = self.confirm_all
            self.add_item(all_btn)
        close = discord.ui.Button(label="닫기", style=discord.ButtonStyle.secondary, row=4)
        close.callback = self.close
        self.add_item(close)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if not is_leader(interaction):
            await interaction.response.send_message("공대장만 확정할 수 있어요.", ephemeral=True)
            return False
        return True

    def _make_confirm(self, index: int):
        async def callback(interaction: discord.Interaction):
            await self._confirm(interaction, [index])
        return callback

    async def confirm_all(self, interaction: discord.Interaction):
        await self._confirm(interaction, [i for i in range(len(self.result.proposals)) if i not in self.confirmed])

    async def _confirm(self, interaction: discord.Interaction, indexes: list[int]):
        await interaction.response.defer()
        notes = []
        for i in indexes:
            if i in self.confirmed:
                continue
            try:
                text, skipped, session_id = await create_from_proposal(interaction, self.result.proposals[i])
            except RuleError as e:
                notes.append(f"{i + 1}번: {e}")
                continue
            self.confirmed.add(i)
            notes += [f"{i + 1}번 제외 — {s}" for s in skipped]
            await announce(interaction, text, session_id)
        for item in self.children:
            if isinstance(item, discord.ui.Button) and item.label and item.label.endswith("확정"):
                num = item.label.split()[0]
                if num.isdigit() and int(num) - 1 in self.confirmed:
                    item.disabled = True
        content = plan_text(self.bot, self.result, self.confirmed)
        if notes:
            content = truncate(content + "\n\n⚠️ " + "\n".join(notes), 4000)
        embed = interaction.message.embeds[0] if interaction.message and interaction.message.embeds else discord.Embed()
        embed.description = content
        await interaction.edit_original_response(embed=embed, view=self)

    async def close(self, interaction: discord.Interaction):
        self.stop()
        await interaction.response.edit_message(view=None)


async def send_proposals(
    interaction: discord.Interaction, raid_key: str | None = None, min_fill: int | None = None
) -> None:
    """interaction 은 이미 defer 된 상태여야 한다."""
    bot = bot_of(interaction)
    result = propose(
        bot.db, bot.cfg, interaction.guild_id or 0, now_kst(), raid_key, min_fill, max_proposals=MAX_PROPOSALS
    )
    embed = discord.Embed(
        title="🧩 자동 일정 제안" + (f" · {bot.cfg.raids[raid_key].name}" if raid_key else ""),
        description=plan_text(bot, result, set()),
        color=discord.Color.green(),
    )
    embed.set_footer(
        text="가능 시간 × 남은 골드 레이드 기준 · 확정하면 일정이 등록되고 알림 채널에 공지돼요"
    )
    await interaction.followup.send(embed=embed, view=ProposalView(bot, result), ephemeral=True)


class Planning(commands.Cog):
    @app_commands.command(
        name="일정제안",
        description="가능 시간과 남은 골드 레이드로 이번 주 일정을 자동으로 짜서 제안합니다",
        extras=meta(
            CAT_LEADER,
            "/일정제안  ·  /일정제안 레이드:세르카 노말  ·  /일정제안 최소인원:6",
            "캐릭터별로 아직 안 잡힌 골드 레이드를 모아, 공대원 가능 시간이 겹치는 시간에 파티를 짜요.\n"
            "골드가 많은 레이드부터 채우고, 한 사람이 같은 시간에 두 캐릭터로 들어가지 않게 해요.\n"
            "제안마다 [확정] 버튼을 누르면 일정이 등록되고 참가자에게 공지돼요. "
            "최소인원을 정원보다 작게 하면 빈자리가 있는 일정도 제안해요 (나머지는 모집).",
            leader=True,
        ),
    )
    @app_commands.describe(레이드="특정 레이드만 (난이도 묶음 기준)", 최소인원="파티 최소 인원 (기본: 정원)")
    @app_commands.autocomplete(레이드=raid_autocomplete)
    @leader_only()
    async def suggest(
        self, interaction: discord.Interaction, 레이드: str | None = None,
        최소인원: app_commands.Range[int, 1, 16] | None = None,
    ):
        bot = bot_of(interaction)
        if 레이드 and bot.cfg.raid(레이드) is None:
            raise RuleError("목록에서 레이드를 골라 주세요.")
        await interaction.response.defer(ephemeral=True, thinking=True)
        await send_proposals(interaction, 레이드, 최소인원)


async def setup(bot: commands.Bot):
    await bot.add_cog(Planning())
