"""봇의 애플리케이션 이모지 중 직업·골드·색 숫자·역할 아이콘(cls_*, lodico_gold, lv_*, role_*)을 지운다.

아이콘 이미지를 바꾼 뒤 배포할 때 한 번 실행하면, 봇이 다시 켜지면서 새 이미지를 올린다
(같은 이름의 이모지가 남아 있으면 새로 올리지 않기 때문).   실행: python tools/reset_icons.py
"""
from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

import discord
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from bot.classicons import EMOJI_NAMES, GOLD_EMOJI, LEVEL_STEMS, level_emoji_name  # noqa: E402


async def main() -> None:
    load_dotenv(ROOT / ".env")
    token = os.environ.get("DISCORD_TOKEN")
    if not token:
        raise SystemExit(".env 에 DISCORD_TOKEN 이 없어요.")
    ours = set(EMOJI_NAMES.values()) | {GOLD_EMOJI} | {level_emoji_name(stem) for stem in LEVEL_STEMS}
    client = discord.Client(intents=discord.Intents.none())
    await client.login(token)
    try:
        deleted = 0
        for emoji in await client.fetch_application_emojis():
            if emoji.name in ours:
                await emoji.delete()
                deleted += 1
        print(f"아이콘 이모지 {deleted}개 삭제")
    finally:
        await client.close()


if __name__ == "__main__":
    asyncio.run(main())
