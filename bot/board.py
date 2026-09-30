"""알림 채널에 고정하는 '이번 주 공대 일정' 메시지.

- 일정이 생기거나 바뀌면(DB 변경 콜백) 몇 초 모아서 한 번 갱신한다. 시트 가져오기처럼 한꺼번에 바뀌어도 한 번만 수정.
- 이번 주 첫 일정이 생기면 새 메시지를 보내고 고정한다.
- 주간 리셋(수 06:00)이 지나면 지난주 메시지는 고정 해제 후 삭제한다. 새 주 일정이 생기면 다시 만든다.
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import replace

import discord

from .render import overview_text
from .services import build_overview, week_bounds
from .timeutil import now_kst

log = logging.getLogger(__name__)
DEBOUNCE_SECONDS = 3


class BoardManager:
    def __init__(self, bot):
        self.bot = bot
        self._pending: dict[int, asyncio.Task] = {}

    def request(self, guild_id: int) -> None:
        """갱신 예약. DB 변경 콜백에서 불리므로 동기 함수. 이벤트 루프 밖(테스트 등)이면 무시."""
        task = self._pending.get(guild_id)
        if task and not task.done():
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        self._pending[guild_id] = loop.create_task(self._delayed(guild_id))

    async def _delayed(self, guild_id: int) -> None:
        await asyncio.sleep(DEBOUNCE_SECONDS)
        try:
            await self.refresh(guild_id)
        except Exception:
            log.exception("고정 일정 메시지 갱신 실패 (guild=%s)", guild_id)

    def _channel(self, guild_id: int):
        settings = self.bot.db.get_settings(guild_id)
        channel = self.bot.get_channel(settings.notify_channel_id) if settings.notify_channel_id else None
        return settings, channel if hasattr(channel, "send") and hasattr(channel, "fetch_message") else None

    def embed(self, ov, now) -> discord.Embed:
        # 고정 메시지에는 일정만 (골드 레이드 요약은 /레이드일정 에서)
        body = overview_text(self.bot.cfg, replace(ov, unassigned_by_raid={}, idle_members=[]))
        embed = discord.Embed(
            title=f"📌 이번 주 공대 일정 ({ov.start.month}/{ov.start.day} ~ {ov.end.month}/{ov.end.day} 06:00)",
            description=body, color=discord.Color.gold(),
        )
        embed.set_footer(text=f"자동 갱신 {now:%m/%d %H:%M} · 자세히 /레이드일정 · 내 일정 /내일정")
        return embed

    async def refresh(self, guild_id: int) -> None:
        db = self.bot.db
        settings, channel = self._channel(guild_id)
        if channel is None:
            return
        now = now_kst()
        ov = build_overview(db, self.bot.cfg, guild_id, now)
        week = ov.start.date().isoformat()

        old = None
        if settings.board_message_id:
            try:
                old = await channel.fetch_message(settings.board_message_id)
            except discord.HTTPException:
                old = None  # 누가 지웠거나 채널이 바뀜
        if old is not None and settings.board_week != week:
            await self._remove(old)
            old = None
        if old is None and settings.board_message_id:
            db.update_settings(guild_id, board_message_id=None, board_week=None)

        if old is not None:
            await old.edit(embed=self.embed(ov, now))
            return
        if not ov.sessions:
            return  # 이번 주 일정이 생길 때 만든다

        message = await channel.send(embed=self.embed(ov, now))
        try:
            await message.pin(reason="이번 주 공대 일정")
        except discord.Forbidden:
            log.warning("메시지 고정 권한(Pin Messages)이 없어 고정하지 못했어요 (guild=%s)", guild_id)
        db.update_settings(guild_id, board_message_id=message.id, board_week=week)

    async def _remove(self, message: discord.Message) -> None:
        try:
            if message.pinned:
                await message.unpin(reason="지난주 공대 일정")
            await message.delete()
        except discord.HTTPException as e:
            log.warning("지난주 고정 메시지 정리 실패: %s", e)

    async def check_rollover(self, guild_id: int) -> None:
        """주간이 바뀌었으면 지난주 고정 메시지를 정리한다 (알림 루프에서 주기적으로 호출)."""
        settings = self.bot.db.get_settings(guild_id)
        if not settings.board_week:
            return
        current = week_bounds(self.bot.cfg, now_kst())[0].date().isoformat()
        if settings.board_week != current:
            await self.refresh(guild_id)
