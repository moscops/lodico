from __future__ import annotations

import discord
from discord import app_commands
from discord.ext import commands

from ..common import CAT_INPUT, CAT_START, CAT_VIEW, bot_of, ensure_member, meta, my_character_autocomplete
from ..render import character_line, character_plan_text
from ..lostark_api import ApiError
from ..services import RuleError, homework_plan


class Characters(commands.Cog):
    @app_commands.command(
        name="캐릭터등록",
        description="캐릭터를 등록하거나 정보(직업, 아이템 레벨)를 수정합니다",
        extras=meta(
            CAT_START,
            "/캐릭터등록 이름:달빛바드  ·  /캐릭터등록 이름:달빛바드 직업:바드 아이템레벨:1720",
            "직업/아이템레벨을 비우면 로스트아크 공식 API에서 가져와요 (API 키가 설정된 경우).\n"
            "같은 이름으로 다시 등록하면 직업/레벨이 갱신돼요. 여러 캐릭터를 한 번에 등록하려면 `/원정대연동`.\n"
            "골드 획득 캐릭터(아이템 레벨 상위 6개)와 캐릭터별 골드 레이드(상위 3개)는 자동으로 정해져요.",
        ),
    )
    @app_commands.describe(
        이름="캐릭터 이름", 직업="직업 (비우면 API로 조회)", 아이템레벨="아이템 레벨 (비우면 API로 조회)",
    )
    async def register(
        self,
        interaction: discord.Interaction,
        이름: app_commands.Range[str, 1, 32],
        직업: app_commands.Range[str, 1, 16] | None = None,
        아이템레벨: app_commands.Range[float, 0, 2000] | None = None,
    ):
        bot = bot_of(interaction)
        name = 이름.strip()
        from_api = 직업 is None or 아이템레벨 is None
        if from_api:
            if bot.lostark is None:
                raise RuleError("API 키가 설정되지 않아 자동 조회를 할 수 없어요. 직업과 아이템레벨을 입력해 주세요.")
            await interaction.response.defer(ephemeral=True, thinking=True)
            try:
                profile = await bot.lostark.profile(name)
            except ApiError as e:
                raise RuleError(f"{e}\n직업과 아이템레벨을 직접 입력하면 API 없이도 등록돼요.") from e
            if profile is None:
                raise RuleError(f"'{name}' 캐릭터를 찾을 수 없어요. 닉네임을 확인해 주세요.")
            직업 = 직업 or profile.cls
            아이템레벨 = 아이템레벨 if 아이템레벨 is not None else profile.item_level

        member = ensure_member(interaction)
        char, created = bot.db.upsert_character(member.id, name, 직업.strip(), round(아이템레벨, 2))
        hw = homework_plan(bot.cfg, bot.db.list_characters(member.id))[char.id]
        raids = hw.gold
        tier = f" (구간 {hw.tier}{'' if hw.explicit else ' 자동'})" if hw.tier else ""
        gold_info = (
            f"💰 숙제{tier}: " + " · ".join(r.name for r in raids) if raids
            else f"골드 획득 캐릭터가 아니에요 (원정대 아이템 레벨 상위 {bot.cfg.gold_characters}개 밖)"
        )
        verb = "등록" if created else "수정"
        text = (
            f"{verb}했어요: {character_line(char)}\n{gold_info}\n"
            + ("다음 단계: `/내시간대` 로 평소 가능한 시간을 알려 주세요." if created else "")
        )
        if from_api:
            await interaction.followup.send(text, ephemeral=True)
        else:
            await interaction.response.send_message(text, ephemeral=True)

    @app_commands.command(
        name="캐릭터삭제",
        description="내 캐릭터를 삭제합니다 (참가 중인 일정에서도 빠집니다)",
        extras=meta(CAT_INPUT, "/캐릭터삭제 이름:달빛바드"),
    )
    @app_commands.describe(이름="삭제할 캐릭터")
    @app_commands.autocomplete(이름=my_character_autocomplete)
    async def delete(self, interaction: discord.Interaction, 이름: str):
        member = ensure_member(interaction)
        ok = bot_of(interaction).db.delete_character(member.id, 이름)
        msg = f"**{이름}** 을(를) 삭제했어요." if ok else f"**{이름}** 이라는 내 캐릭터가 없어요."
        await interaction.response.send_message(msg, ephemeral=True)

    @app_commands.command(
        name="캐릭터목록",
        description="캐릭터별 숙제 구간·레이드와 클골, 더보기 비용, 더보기 후 남는 골드를 봅니다",
        extras=meta(CAT_VIEW, "/캐릭터목록  또는  /캐릭터목록 공대원:@김철수"),
    )
    @app_commands.describe(공대원="다른 공대원의 목록을 볼 때 선택")
    async def list_(self, interaction: discord.Interaction, 공대원: discord.Member | None = None):
        bot = bot_of(interaction)
        db = bot.db
        target = 공대원 or interaction.user
        member = db.get_member(interaction.guild_id or 0, target.id)
        chars = db.list_characters(member.id) if member else []
        if not chars:
            hint = " `/캐릭터등록` 으로 추가해 주세요." if target == interaction.user else ""
            await interaction.response.send_message(
                f"{target.display_name} 님의 등록된 캐릭터가 없어요.{hint}", ephemeral=True
            )
            return
        embed = discord.Embed(
            title=f"{target.display_name} 님의 캐릭터 ({len(chars)})",
            description=character_plan_text(chars, homework_plan(bot.cfg, chars)),
            color=discord.Color.blurple(),
        )
        embed.set_footer(
            text=f"💰 골드 획득 (아이템 레벨 상위 {bot.cfg.gold_characters}캐릭) · "
            "구간 옆 '자동' = 레벨로 정해진 기본 구간 · 바꾸기: /숙제구간"
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)


async def tier_autocomplete(interaction: discord.Interaction, current: str):
    bot = bot_of(interaction)
    member = bot.db.get_member(interaction.guild_id or 0, interaction.user.id)
    name = getattr(interaction.namespace, "캐릭터", None)
    char = next((c for c in bot.db.list_characters(member.id) if c.name == name), None) if member else None
    choices = [app_commands.Choice(name="자동 (레벨에 맞는 기본 구간)", value="자동")]
    for t in sorted(bot.cfg.tiers.values(), key=lambda t: (-t.min_level, t.label)):
        if char and t.min_level > char.item_level:
            continue
        raids = [bot.cfg.raids[k].name for k in t.raids]
        extra = f" +{bot.cfg.raids[t.no_gold].name}(골드X)" if t.no_gold else ""
        label = f"{t.label} · {', '.join(raids)}{extra}"
        if current in label:
            choices.append(app_commands.Choice(name=label[:100], value=t.label))
    return choices[:25]


class Tiers(commands.Cog):
    @app_commands.command(
        name="숙제구간",
        description="캐릭터의 숙제 구간(1780+, 1750+_3 …)을 정합니다. 구간에 따라 주간 레이드가 정해져요",
        extras=meta(
            CAT_INPUT,
            "/숙제구간 캐릭터:달빛바드 구간:1750+_3",
            "구간을 안 정하면 아이템 레벨에 맞는 기본 구간(1780+, 1770+, 1750+, 1730+, 1720+, 1710+)을 써요.\n"
            "_2, _3 처럼 난이도를 낮추거나 골드X 레이드가 붙은 변형 구간을 고를 수 있어요. "
            "구간표는 `/레이드 목록` 아래에서 볼 수 있어요.",
        ),
    )
    @app_commands.describe(캐릭터="내 캐릭터", 구간="숙제 구간 (자동 = 레벨로 결정)")
    @app_commands.autocomplete(캐릭터=my_character_autocomplete, 구간=tier_autocomplete)
    async def set_tier(self, interaction: discord.Interaction, 캐릭터: str, 구간: str):
        bot = bot_of(interaction)
        member = ensure_member(interaction)
        char = next((c for c in bot.db.list_characters(member.id) if c.name == 캐릭터), None)
        if char is None:
            raise RuleError(f"'{캐릭터}' 은(는) 내 캐릭터가 아니에요.")
        if 구간 != "자동":
            tier = bot.cfg.tiers.get(구간)
            if tier is None:
                raise RuleError("목록에서 구간을 골라 주세요.")
            if tier.min_level > char.item_level:
                raise RuleError(f"{char.name}({char.item_level:g})은 {tier.label} 구간(최소 {tier.min_level:g})에 못 들어가요.")
        bot.db.set_character_tier(char.id, None if 구간 == "자동" else 구간)
        hw = homework_plan(bot.cfg, bot.db.list_characters(member.id))[char.id]
        raids = " · ".join(r.name for r in hw.gold) + "".join(f" · {r.name}(골드X)" for r in hw.extra)
        await interaction.response.send_message(
            f"✅ **{char.name}** 숙제 구간: `{hw.tier or '없음'}{'' if hw.explicit else ' 자동'}`\n{raids or '골드 획득 캐릭터가 아니에요'}",
            ephemeral=True,
        )


async def setup(bot: commands.Bot):
    await bot.add_cog(Characters())
    await bot.add_cog(Tiers())
