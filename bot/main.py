from __future__ import annotations

import logging
from pathlib import Path

import discord
from discord import app_commands
from discord.ext import commands
from dotenv import load_dotenv

from .board import BoardManager
from .classicons import load_class_icons
from .config import ROOT, Config, add_event_raids, apply_overrides, env, load_config
from .db import Database
from .gsheet import ExportError
from .lostark_api import ApiError, LostArkClient
from .services import RuleError
from .timeutil import ParseError

log = logging.getLogger("raidbot")

COGS = [
    "characters", "lostark", "lopec", "availability", "unavailable", "sessions", "session_edit", "menu", "planning", "sheet_import", "sheet_export", "raids",
    "help",
    "admin", "notifier",
]


class RaidTree(app_commands.CommandTree):
    async def on_error(self, interaction: discord.Interaction, error: app_commands.AppCommandError) -> None:
        original = getattr(error, "original", error)
        if isinstance(original, (RuleError, ParseError, ApiError, ExportError, app_commands.CheckFailure)):
            message = f"⚠️ {original}"
        else:
            log.exception("명령어 처리 중 오류: /%s", interaction.command and interaction.command.name, exc_info=error)
            message = "😵 처리 중 오류가 났어요. 잠시 후 다시 시도하거나 공대장에게 알려 주세요."
        if interaction.response.is_done():
            await interaction.followup.send(message, ephemeral=True)
        else:
            await interaction.response.send_message(message, ephemeral=True)


class RaidBot(commands.Bot):
    def __init__(
        self, config_path: Path, db: Database, dev_guild_id: int | None, lostark_api_key: str | None = None
    ):
        super().__init__(
            command_prefix=commands.when_mentioned,  # 슬래시 명령어만 쓰므로 접두사는 사실상 미사용
            intents=discord.Intents.default(),
            tree_cls=RaidTree,
            allowed_mentions=discord.AllowedMentions(everyone=False, roles=False, users=True),
        )
        self.config_path = config_path
        self.db = db
        self.dev_guild_id = dev_guild_id
        self.lostark = LostArkClient(lostark_api_key) if lostark_api_key else None
        self._cleared_guild_commands = False
        self.board = BoardManager(self)
        db.on_change = self.board.request
        self.cfg: Config
        self.reload_raids()

    def reload_raids(self) -> None:
        """config.toml 을 다시 읽고 DB 의 /레이드 수정 값과 이벤트 레이드를 합친다. 실패하면 예외, 기존 설정 유지."""
        cfg = apply_overrides(load_config(self.config_path), self.db.get_raid_overrides())
        self.cfg = add_event_raids(cfg, self.db.list_event_raids())

    async def setup_hook(self) -> None:
        for name in COGS:
            await self.load_extension(f"bot.cogs.{name}")
        await load_class_icons(self)
        # 명령어는 '서버 전용' 또는 '전역' 한쪽에만 등록한다. 양쪽에 남아 있으면 / 목록에 두 번씩 보인다.
        if self.dev_guild_id:
            # 개발용: 특정 서버에만 즉시 반영 (전역 등록은 반영까지 시간이 걸릴 수 있음)
            guild = discord.Object(self.dev_guild_id)
            self.tree.copy_global_to(guild=guild)
            synced = await self.tree.sync(guild=guild)
            # 예전에 전역으로 등록된 명령어 제거 (트리는 그대로 두고 디스코드 쪽 등록만 비움)
            await self.http.bulk_upsert_global_commands(self.application_id, [])
            log.info("슬래시 명령어 %d개를 서버(%s)에 등록, 전역 등록은 비움", len(synced), self.dev_guild_id)
        else:
            synced = await self.tree.sync()
            log.info("슬래시 명령어 %d개를 전역 등록", len(synced))

    async def _clear_guild_commands(self) -> None:
        """전역 모드일 때, 예전에 서버 전용으로 등록된 명령어를 모든 서버에서 제거."""
        for guild in self.guilds:
            try:
                await self.http.bulk_upsert_guild_commands(self.application_id, guild.id, [])
            except discord.HTTPException as e:
                log.warning("서버(%s) 명령어 정리 실패: %s", guild.id, e)

    async def close(self) -> None:
        if self.lostark:
            await self.lostark.close()
        await super().close()

    async def on_ready(self) -> None:
        log.info("로그인: %s (서버 %d곳)", self.user, len(self.guilds))
        # on_ready 는 재접속 때마다 불리므로 한 번만. 서버 목록은 여기서야 알 수 있다.
        # 꺼져 있는 동안 바뀐 게 있을 수 있어 고정 일정 메시지를 한 번 맞춘다
        for guild in self.guilds:
            self.board.request(guild.id)
        if not self.dev_guild_id and not self._cleared_guild_commands:
            self._cleared_guild_commands = True
            await self._clear_guild_commands()


def run() -> None:
    load_dotenv(ROOT / ".env")
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    token = env("DISCORD_TOKEN")
    if not token:
        raise SystemExit(".env 에 DISCORD_TOKEN 을 넣어 주세요. (.env.example 참고)")
    guild_id = env("DEV_GUILD_ID")
    db_path = Path(env("DB_PATH", str(ROOT / "data" / "bot.db")))
    db_path.parent.mkdir(parents=True, exist_ok=True)

    api_key = env("LOSTARK_API_KEY")
    if not api_key:
        log.warning("LOSTARK_API_KEY 가 없어 아이템 레벨 자동 갱신(/원정대연동, /레벨갱신)이 꺼져 있습니다.")
    bot = RaidBot(ROOT / "config.toml", Database(db_path), int(guild_id) if guild_id else None, api_key)
    bot.run(token, log_handler=None)
