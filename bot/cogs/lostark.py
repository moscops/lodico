"""공식 API 연동: 원정대 연동, 수동 갱신, 주기적 자동 갱신."""
from __future__ import annotations

import logging
from collections import defaultdict

import discord
from discord import app_commands
from discord.ext import commands, tasks

from ..common import CAT_INPUT, CAT_START, bot_of, ensure_member, is_leader, meta
from ..db import Member
from ..lostark_api import ApiCharacter, ApiError, LostArkClient
from ..render import character_plan_text, sync_result_text, truncate
from ..roster import SyncResult, apply_roster, min_raid_level
from ..services import RuleError, homework_plan
from ..timeutil import now_kst, to_epoch

log = logging.getLogger(__name__)

AUTO_SYNC_CHECK_MINUTES = 10  # 이 간격으로 '갱신할 때가 된' 공대원을 찾는다


def _client(bot) -> LostArkClient:
    if bot.lostark is None:
        raise RuleError("봇에 로스트아크 API 키(LOSTARK_API_KEY)가 설정되지 않았어요. 관리자에게 알려 주세요.")
    return bot.lostark


class LostArk(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        if bot.lostark is not None:
            self.auto_sync.start()

    async def cog_unload(self):
        self.auto_sync.cancel()

    async def sync_member(
        self, member: Member, add_new: bool | None = None, min_level: float | None = None,
        roster: list[ApiCharacter] | None = None,
    ) -> SyncResult:
        bot = self.bot
        if roster is None:
            roster = await _client(bot).siblings(member.roster_name or "")
        if not roster:
            raise ApiError(
                f"'{member.roster_name}' 캐릭터를 찾을 수 없어요. 닉네임을 바꿨다면 `/원정대연동` 을 다시 해 주세요."
            )
        result = apply_roster(
            bot.db, bot.cfg, member.id, roster,
            min_level if min_level is not None else min_raid_level(bot.cfg),
            bot.cfg.auto_add_new if add_new is None else add_new,
        )
        bot.db.mark_synced(member.id, to_epoch(now_kst()))
        return result

    # ---- 명령어 ----------------------------------------------------------
    @app_commands.command(
        name="원정대연동",
        description="대표 캐릭터 이름으로 원정대를 불러와 캐릭터를 한 번에 등록하고 레벨을 자동 갱신합니다",
        extras=meta(
            CAT_START,
            "/원정대연동 대표캐릭터:달빛바드",
            "로스트아크 공식 API로 원정대 캐릭터의 직업과 아이템 레벨을 가져와요.\n"
            "최소레벨(기본: 가장 낮은 레이드 입장 레벨) 이상 캐릭터만 등록되고, "
            "이후 레벨은 몇 시간마다 자동으로 갱신돼요. 바로 갱신하려면 `/레벨갱신`.",
        ),
    )
    @app_commands.describe(대표캐릭터="원정대의 아무 캐릭터 이름", 최소레벨="이 레벨 이상 캐릭터만 등록")
    async def link(
        self, interaction: discord.Interaction,
        대표캐릭터: app_commands.Range[str, 1, 32],
        최소레벨: app_commands.Range[float, 0, 2000] | None = None,
    ):
        bot = bot_of(interaction)
        client = _client(bot)
        await interaction.response.defer(ephemeral=True, thinking=True)
        member = ensure_member(interaction)
        name = 대표캐릭터.strip()
        roster = await client.siblings(name)
        if not roster:
            raise RuleError(f"'{name}' 캐릭터를 찾을 수 없어요. 닉네임을 정확히 입력해 주세요.")

        # 다른 공대원이 이미 등록한 캐릭터가 섞여 있으면 거절 (남의 원정대 연동 방지)
        roster_names = {a.name for a in roster}
        taken = [
            f"{c.name}({c.display_name})" for c in bot.db.list_guild_characters(interaction.guild_id or 0)
            if c.member_id != member.id and c.name in roster_names
        ]
        if taken:
            raise RuleError(f"이미 다른 공대원이 등록한 캐릭터가 있어요: {', '.join(taken)}")

        bot.db.set_roster(member.id, name)
        member = bot.db.get_member(member.guild_id, member.discord_id)
        min_level = 최소레벨 if 최소레벨 is not None else min_raid_level(bot.cfg)
        await self.sync_member(member, add_new=True, min_level=min_level, roster=roster)

        chars = bot.db.list_characters(member.id)
        embed = discord.Embed(
            title=f"원정대 연동 완료 — {name}",
            description=truncate(character_plan_text(chars, homework_plan(bot.cfg, chars)), 4000),
            color=discord.Color.blurple(),
        )
        embed.set_footer(
            text=f"원정대 {len(roster)}캐릭 중 {min_level:g} 이상 등록 · "
            f"{bot.cfg.sync_interval_hours:g}시간마다 레벨 자동 갱신"
        )
        await interaction.followup.send(embed=embed, ephemeral=True)

    @app_commands.command(
        name="원정대해제",
        description="원정대 연동(레벨 자동 갱신)을 끕니다. 등록된 캐릭터는 남아 있어요",
        extras=meta(CAT_INPUT, "/원정대해제"),
    )
    async def unlink(self, interaction: discord.Interaction):
        bot = bot_of(interaction)
        member = ensure_member(interaction)
        if not member.roster_name:
            raise RuleError("연동된 원정대가 없어요.")
        bot.db.set_roster(member.id, None)
        await interaction.response.send_message(
            f"'{member.roster_name}' 원정대 연동을 해제했어요. 레벨은 이제 `/캐릭터등록` 으로 직접 고쳐야 해요.",
            ephemeral=True,
        )

    @app_commands.command(
        name="레벨갱신",
        description="공식 API로 내 캐릭터 아이템 레벨을 지금 바로 갱신합니다",
        extras=meta(
            CAT_INPUT,
            "/레벨갱신  ·  (공대장) /레벨갱신 전체:True",
            "`/원정대연동` 을 한 공대원만 갱신돼요. 레벨이 올라 골드 레이드가 바뀌면 알려 줘요.",
        ),
    )
    @app_commands.describe(전체="(공대장) 연동한 공대원 전체를 갱신")
    async def refresh(self, interaction: discord.Interaction, 전체: bool = False):
        bot = bot_of(interaction)
        _client(bot)
        if 전체 and not is_leader(interaction):
            raise app_commands.CheckFailure("전체 갱신은 공대장만 할 수 있어요.")
        if not 전체:
            member = ensure_member(interaction)
            if not member.roster_name:
                raise RuleError("먼저 `/원정대연동 대표캐릭터:이름` 으로 원정대를 연동해 주세요.")
            await interaction.response.defer(ephemeral=True, thinking=True)
            text = sync_result_text(await self.sync_member(member)) or "바뀐 내용이 없어요. 이미 최신이에요."
            await interaction.followup.send(truncate(f"🔄 **레벨 갱신**\n{text}", 2000), ephemeral=True)
            return

        await interaction.response.defer(thinking=True)
        members = bot.db.list_linked_members(interaction.guild_id)
        parts, failed = [], []
        for m in members:
            try:
                text = sync_result_text(await self.sync_member(m), owner=m.display_name)
            except ApiError as e:
                failed.append(f"{m.display_name}: {e}")
                if e.fatal:
                    break
                continue
            if text:
                parts.append(text)
        body = "\n".join(parts) or "바뀐 내용이 없어요."
        if failed:
            body += "\n\n⚠️ 실패\n" + "\n".join(failed)
        embed = discord.Embed(
            title=f"전체 레벨 갱신 ({len(members)}명)", description=truncate(body, 4000),
            color=discord.Color.blurple(),
        )
        await interaction.followup.send(embed=embed)

    # ---- 자동 갱신 -------------------------------------------------------
    @tasks.loop(minutes=AUTO_SYNC_CHECK_MINUTES)
    async def auto_sync(self):
        try:
            await self.auto_sync_once()
        except Exception:  # 루프가 멈추지 않도록 여기서 삼킨다
            log.exception("자동 레벨 갱신 오류")

    async def auto_sync_once(self) -> None:
        bot = self.bot
        cutoff = to_epoch(now_kst()) - int(bot.cfg.sync_interval_hours * 3600)
        guild_ids = {g.id for g in bot.guilds}
        due = [
            m for m in bot.db.list_linked_members()
            if m.guild_id in guild_ids and (m.synced_at is None or m.synced_at <= cutoff)
        ]
        gold_news: dict[int, list[str]] = defaultdict(list)
        for m in due:
            try:
                result = await self.sync_member(m)
            except ApiError as e:
                log.warning("자동 레벨 갱신 실패 (%s): %s", m.display_name, e)
                if e.fatal:
                    break  # 점검/키 오류면 이번 회차는 중단하고 다음에 다시
                continue
            # 골드 레이드가 바뀐 경우만 공지 (레벨 소폭 변화는 조용히 반영)
            news = SyncResult(changes=result.gold_changes)
            if news.changes:
                gold_news[m.guild_id].append(sync_result_text(news, owner=m.display_name))

        for guild_id, items in gold_news.items():
            settings = bot.db.get_settings(guild_id)
            channel = bot.get_channel(settings.notify_channel_id) if settings.notify_channel_id else None
            if isinstance(channel, discord.abc.Messageable):
                await channel.send(
                    truncate("📈 **레벨 변화로 골드 레이드가 바뀐 캐릭터**\n" + "\n".join(items), 2000),
                    allowed_mentions=discord.AllowedMentions.none(),
                )

    @auto_sync.before_loop
    async def before_auto_sync(self):
        await self.bot.wait_until_ready()


async def setup(bot: commands.Bot):
    await bot.add_cog(LostArk(bot))
