"""디스코드에 접속하지 않고 모든 cog 와 슬래시 명령어 정의가 유효한지 확인한다."""
import importlib
import unittest

import discord

from bot.common import CATEGORIES
from bot.config import ROOT
from bot.db import Database
from bot.main import COGS, RaidBot
from bot.cogs.help import all_commands, build_detail_embed, build_overview_embed


class BotLoadTest(unittest.IsolatedAsyncioTestCase):
    async def test_all_commands_register_and_have_help_meta(self):
        bot = RaidBot(ROOT / "config.toml", Database(":memory:"), None)
        # tick 루프가 시작되지 않도록 notifier 는 제외하고 로드한 뒤 따로 import 만 확인
        for name in COGS:
            if name != "notifier":
                await bot.load_extension(f"bot.cogs.{name}")
        importlib.import_module("bot.cogs.notifier")

        cmds = all_commands(bot.tree)
        names = {c.qualified_name for c in cmds}
        for expected in ["캐릭터등록", "원정대연동", "레벨갱신", "시트양식", "시트가져오기", "시트내보내기", "일정제안", "내일정", "로펙", "레이드 이벤트추가", "레이드 이벤트종료", "가능시간대", "불가", "불가목록", "불가취소", "일정등록", "공대일정수정", "일정완료", "메뉴", "공대일정", "도움", "봇설정", "레이드 수정"]:
            self.assertIn(expected, names)
        for c in cmds:
            self.assertIn(c.extras.get("category"), CATEGORIES, c.name)
            self.assertTrue(c.extras.get("example"), c.name)
            self.assertIsInstance(build_detail_embed(c), discord.Embed)

        for top in bot.tree.get_commands():
            top.to_dict(bot.tree)  # 디스코드 API 로 보낼 형태로 직렬화 가능해야 함

        # 레이드 목록이 자동완성 한도(25개) 안에 있어야 한다
        self.assertLessEqual(len(bot.cfg.raids), 25)

        embed = build_overview_embed(cmds)
        for field in embed.fields:
            self.assertLessEqual(len(field.value), 1024, field.name)
        self.assertLessEqual(len(embed), 6000)
        await bot.close()


if __name__ == "__main__":
    unittest.main()


class MenuActionsTest(unittest.IsolatedAsyncioTestCase):
    async def test_every_menu_button_resolves_to_a_command_and_ids_match(self):
        from bot.cogs.menu import ACTIONS, _MenuButton
        bot = RaidBot(ROOT / "config.toml", Database(":memory:"), None)
        for name in COGS:
            if name != "notifier":
                await bot.load_extension(f"bot.cogs.{name}")
        for action, (_, _, cog_name, attr, kwargs) in ACTIONS.items():
            cog = bot.get_cog(cog_name)
            self.assertIsNotNone(cog, action)
            command = getattr(cog, attr)
            self.assertTrue(callable(command.callback), action)
            params = {p.name for p in command.parameters}
            self.assertTrue(set(kwargs) <= params, f"{action}: {set(kwargs) - params}")
            self.assertTrue(_MenuButton.__discord_ui_compiled_template__.fullmatch(f"raid:menu:{action}"))
        # 도움말 맨 위에 자주 쓰는 기능
        cmds = all_commands(bot.tree)
        embed = build_overview_embed(cmds)
        self.assertTrue(embed.fields[0].name.startswith("⭐"))
        for n in ["공대일정", "공대일정수정", "내일정", "가능현황", "로펙"]:
            self.assertIn(f"`/{n}`", embed.fields[0].value)
        for field in embed.fields:
            self.assertLessEqual(len(field.value), 1024)
        self.assertLessEqual(len(embed), 6000)
        await bot.close()


class MenuInvokeTest(unittest.IsolatedAsyncioTestCase):
    async def test_buttons_invoke_commands_and_reply_ephemerally(self):
        from types import SimpleNamespace
        from unittest.mock import AsyncMock
        from bot.cogs.menu import ACTIONS, _MenuButton
        bot = RaidBot(ROOT / "config.toml", Database(":memory:"), None)
        for name in COGS:
            if name != "notifier":
                await bot.load_extension(f"bot.cogs.{name}")
        m = bot.db.upsert_member(7, 42, "철수")
        bot.db.upsert_character(m.id, "철수1", "블레이드", 1720)
        for action in ACTIONS:
            sent = []
            resp = SimpleNamespace(
                send_message=AsyncMock(side_effect=lambda *a, **k: sent.append(k)),
                is_done=lambda: bool(sent),
            )
            user = SimpleNamespace(id=42, display_name="철수")
            interaction = SimpleNamespace(
                client=bot, guild_id=7, user=user, response=resp, followup=SimpleNamespace(send=AsyncMock()),
                channel=None, guild=None, namespace=None,
            )
            await _MenuButton(action).callback(interaction)
            self.assertTrue(sent, f"{action}: 응답이 없어요")
            self.assertTrue(sent[0].get("ephemeral"), f"{action}: 나에게만 보여야 해요")
        await bot.close()
