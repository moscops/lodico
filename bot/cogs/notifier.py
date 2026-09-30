"""일일 아침 요약 + 시작 전 리마인더.

정확한 시각 예약 대신 30초마다 '보낼 게 있는지' 확인한다. 봇이 재시작되거나
설정이 바뀌어도 따로 예약을 다시 걸 필요가 없고, 보낸 기록은 DB 에 남아 중복 전송을 막는다.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta

import discord
from discord.ext import commands, tasks

from ..parties import generate_party_sessions
from ..render import morning_text, reminder_text, reset_eve_text, truncate
from ..services import SessionView, build_overview, week_bounds
from ..timeutil import from_epoch, now_kst, parse_hhmm, to_epoch

log = logging.getLogger(__name__)

# 봇이 꺼져 있어서 아침 알림 시각을 놓쳤을 때, 이 시간 안에 켜지면 늦게라도 보낸다
MORNING_GRACE = timedelta(hours=2)


class Notifier(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.tick.start()

    async def cog_unload(self):
        self.tick.cancel()

    @tasks.loop(seconds=30)
    async def tick(self):
        now = now_kst()
        self.bot.db.purge_unavailable(to_epoch(now) - 86400)
        for guild in self.bot.guilds:
            try:
                await self.morning(guild, now)
                await self.reminders(guild, now)
                await self.party_week(guild, now)
                await self.reset_eve(guild, now)
                await self.bot.board.check_rollover(guild.id)
            except Exception:
                log.exception("알림 처리 실패 (guild=%s)", guild.id)

    @tick.before_loop
    async def before_tick(self):
        await self.bot.wait_until_ready()

    def _channel(self, channel_id: int | None) -> discord.abc.Messageable | None:
        if channel_id is None:
            return None
        ch = self.bot.get_channel(channel_id)
        return ch if isinstance(ch, discord.abc.Messageable) else None

    def _views(self, guild_id: int, start: datetime, end: datetime) -> list[SessionView]:
        db, cfg = self.bot.db, self.bot.cfg
        return [
            SessionView(s, cfg.raid(s.raid_key), db.session_characters(s.id))
            for s in db.list_sessions(guild_id, to_epoch(start), to_epoch(end))
        ]

    async def morning(self, guild: discord.Guild, now: datetime) -> None:
        db, cfg = self.bot.db, self.bot.cfg
        settings = db.get_settings(guild.id)
        channel = self._channel(settings.notify_channel_id)
        today = now.date().isoformat()
        if channel is None or settings.last_morning_date == today:
            return
        midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
        send_time = parse_hhmm(settings.morning_time or cfg.morning_time)
        due = midnight.replace(hour=send_time.hour, minute=send_time.minute)
        if not (due <= now < due + MORNING_GRACE):
            return
        # 먼저 기록해서 전송 도중 오류가 나도 30초마다 재전송되지 않게 한다
        db.update_settings(guild.id, last_morning_date=today)
        views = self._views(guild.id, now, midnight + timedelta(days=1))
        await channel.send(
            morning_text(cfg, now, views),
            allowed_mentions=discord.AllowedMentions(users=True),
        )

    async def party_week(self, guild: discord.Guild, now: datetime) -> None:
        """주간이 바뀌면 요일·시각이 정해진 고정 파티 일정을 만든다."""
        db, cfg = self.bot.db, self.bot.cfg
        week = week_bounds(cfg, now)[0].date().isoformat()
        if db.get_settings(guild.id).parties_week == week:
            return
        db.update_settings(guild.id, parties_week=week)
        result = generate_party_sessions(db, cfg, guild.id, now)
        channel = self._channel(db.get_settings(guild.id).notify_channel_id)
        if channel and (result.created or result.skipped or result.filled):
            lines = [f"🧩 이번 주 고정 파티 일정 {len(result.created)}개를 만들었어요."] + result.created
            lines += [f"🔧 {s}" for s in result.filled]
            if result.skipped:
                lines += ["⚠️ 넣지 못한 캐릭터"] + [f"• {s}" for s in result.skipped]
            await channel.send(truncate("\n".join(lines), 2000), allowed_mentions=discord.AllowedMentions.none())

    async def reset_eve(self, guild: discord.Guild, now: datetime) -> None:
        """리셋 전날, 아침 알림 시각에 아직 못 끝낸 골드 레이드를 요약해 알린다 (주 1회)."""
        db, cfg = self.bot.db, self.bot.cfg
        settings = db.get_settings(guild.id)
        channel = self._channel(settings.notify_channel_id)
        week = week_bounds(cfg, now)[0].date().isoformat()
        if channel is None or settings.eve_week == week or now.weekday() != (cfg.reset_weekday - 1) % 7:
            return
        midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
        send_time = parse_hhmm(settings.morning_time or cfg.morning_time)
        due = midnight.replace(hour=send_time.hour, minute=send_time.minute)
        if not (due <= now < due + MORNING_GRACE):
            return
        db.update_settings(guild.id, eve_week=week)  # 먼저 기록: 전송 중 오류가 나도 재전송 방지
        text = reset_eve_text(cfg, build_overview(db, cfg, guild.id, now), now)
        if text:
            await channel.send(text, allowed_mentions=discord.AllowedMentions(users=True))

    async def reminders(self, guild: discord.Guild, now: datetime) -> None:
        db, cfg = self.bot.db, self.bot.cfg
        settings = db.get_settings(guild.id)
        minutes = settings.reminder_minutes if settings.reminder_minutes is not None else cfg.reminder_minutes
        channel = self._channel(settings.notify_channel_id)
        if channel is None or minutes <= 0:
            return
        for v in self._views(guild.id, now, now + timedelta(minutes=minutes)):
            if v.session.reminded:
                continue
            db.mark_reminded(v.session.id)
            left = max(1, round((from_epoch(v.session.start_at) - now).total_seconds() / 60))
            await channel.send(
                reminder_text(cfg, v, left),
                allowed_mentions=discord.AllowedMentions(users=True),
            )


async def setup(bot: commands.Bot):
    await bot.add_cog(Notifier(bot))
