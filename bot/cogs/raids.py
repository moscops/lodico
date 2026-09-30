"""레이드 정보 조회와 갱신. 골드/입장 레벨이 패치로 바뀌면 여기서 최신화한다."""
from __future__ import annotations

from datetime import timedelta

import discord
from discord import app_commands
from discord.ext import commands

from ..common import CAT_LEADER, CAT_VIEW, bot_of, event_raid_autocomplete, leader_only, meta, raid_autocomplete
from ..config import Config, Raid
from ..render import gold, raid_table_text
from ..services import RuleError
from ..timeutil import fmt_date, now_kst, parse_date, to_epoch


def _describe(r: Raid) -> str:
    bound = f", 귀속 {gold(r.bound_gold)}" if r.bound_gold else ""
    more = f" · 더보기 {gold(r.more_cost)}" if r.more_cost else ""
    return f"{r.min_item_level:g} · {r.party_size}인 · {gold(r.gold)}{bound}{more}"


def diff_raids(old: Config, new: Config) -> list[str]:
    lines = []
    for key, r in new.raids.items():
        before = old.raids.get(key)
        if before is None:
            lines.append(f"➕ {r.name}: {_describe(r)}")
        elif _describe(before) != _describe(r) or before.name != r.name:
            lines.append(f"✏️ {r.name}: {_describe(before)} → **{_describe(r)}**")
    for key, r in old.raids.items():
        if key not in new.raids:
            lines.append(f"➖ {r.name}")
    return lines


class Raids(commands.GroupCog, group_name="레이드", group_description="레이드 정보 (입장 레벨, 골드)"):
    @app_commands.command(
        name="목록",
        description="레이드별 입장 레벨, 인원, 클리어 골드를 봅니다",
        extras=meta(CAT_VIEW, "/레이드 목록"),
    )
    async def list_(self, interaction: discord.Interaction):
        cfg = bot_of(interaction).cfg
        embed = discord.Embed(
            title="레이드 정보", description=raid_table_text(cfg, now_kst().date()), color=discord.Color.orange()
        )
        embed.set_footer(
            text=f"기준일 {cfg.data_updated or '미기재'} · ✏️ = 디스코드에서 수정한 값 · "
            f"골드는 더보기 제외 합계"
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @app_commands.command(
        name="이벤트추가",
        description="기간 한정 이벤트 레이드를 추가합니다 ([이벤트] 표시, 종료일이 지나면 자동으로 숨김)",
        extras=meta(
            CAT_LEADER,
            "/레이드 이벤트추가 이름:할로윈 레이드 인원:8인 입장레벨:1600 종료일:10/31",
            "추가한 이벤트 레이드는 이름 앞에 [이벤트] 가 붙고, `/일정등록` 레이드 목록 맨 위에 나와요.\n"
            "골드 레이드 추천과 자동 일정 제안에는 들어가지 않아요. 종료일이 지나면 목록에서 사라지고, "
            "일찍 끝내려면 `/레이드 이벤트종료`.",
            leader=True,
        ),
    )
    @app_commands.describe(
        이름="이벤트 레이드 이름", 인원="파티 인원", 입장레벨="입장 아이템 레벨 (없으면 0)",
        종료일="이 날까지 표시 (예: 10/31). 비우면 이벤트종료 할 때까지", 소요시간="예상 소요 시간(분, 기본 60)",
    )
    @app_commands.choices(인원=[app_commands.Choice(name=f"{n}인", value=n) for n in (4, 8, 16)])
    @leader_only()
    async def event_add(
        self, interaction: discord.Interaction,
        이름: app_commands.Range[str, 1, 40],
        인원: app_commands.Choice[int],
        입장레벨: app_commands.Range[float, 0, 2000] = 0,
        종료일: str | None = None,
        소요시간: app_commands.Range[int, 10, 300] = 60,
    ):
        bot = bot_of(interaction)
        today = now_kst().date()
        ends = None
        if 종료일:
            ends = parse_date(종료일, today)
            if ends < today:
                raise RuleError(f"종료일 {fmt_date(ends)} 이 이미 지났어요.")
        name = 이름.strip()
        if any(r.event and r.is_active(today) and r.name.endswith(name) for r in bot.cfg.raids.values()):
            raise RuleError(f"'{name}' 이벤트 레이드가 이미 진행 중이에요.")
        key = bot.db.add_event_raid(
            name, 인원.value, 입장레벨, 소요시간, ends.isoformat() if ends else None, interaction.user.id
        )
        bot.reload_raids()
        raid = bot.cfg.raids[key]
        until = f" · {fmt_date(ends)}까지" if ends else ""
        await interaction.response.send_message(
            f"🎉 이벤트 레이드 추가: **{raid.name}** ({raid.party_size}인, {raid.min_item_level:g}+){until}\n"
            f"일정 등록: `/일정등록 레이드:{raid.name}`"
        )

    @app_commands.command(
        name="이벤트종료",
        description="이벤트 레이드를 종료해 목록에서 숨깁니다",
        extras=meta(CAT_LEADER, "/레이드 이벤트종료 레이드:[이벤트] 할로윈 레이드", leader=True),
    )
    @app_commands.autocomplete(레이드=event_raid_autocomplete)
    @leader_only()
    async def event_end(self, interaction: discord.Interaction, 레이드: str):
        bot = bot_of(interaction)
        raid = bot.cfg.raid(레이드)
        if raid is None or not raid.event:
            raise RuleError("목록에서 이벤트 레이드를 골라 주세요.")
        yesterday = (now_kst().date() - timedelta(days=1)).isoformat()
        if not bot.db.set_event_raid_end(raid.key, yesterday):
            raise RuleError("config.toml 에 적힌 이벤트예요. 파일의 end_date 를 고친 뒤 `/레이드 새로고침` 해 주세요.")
        bot.reload_raids()
        await interaction.response.send_message(
            f"🏁 **{raid.name}** 을 종료했어요. 목록에서 숨겨지고, 이미 잡힌 일정은 그대로 남아요."
        )

    @app_commands.command(
        name="수정",
        description="패치로 바뀐 레이드 골드/입장 레벨/인원을 바로 반영합니다",
        extras=meta(
            CAT_LEADER,
            "/레이드 수정 레이드:세르카 노말 골드:30000 귀속골드:15000",
            "입력한 항목만 바뀌고, 봇 DB 에 저장돼 config.toml 값보다 우선해요.\n"
            "되돌리려면 `/레이드 초기화`. 새 레이드 추가·삭제는 config.toml 을 고친 뒤 `/레이드 새로고침`.",
            leader=True,
        ),
    )
    @app_commands.describe(
        레이드="바꿀 레이드", 골드="클리어 골드 합계 (더보기 제외)", 귀속골드="그중 귀속 골드",
        입장레벨="입장 아이템 레벨", 인원="파티 인원", 더보기비용="전 관문 더보기 비용 합",
    )
    @app_commands.choices(인원=[app_commands.Choice(name=f"{n}인", value=n) for n in (4, 8, 16)])
    @app_commands.autocomplete(레이드=raid_autocomplete)
    @leader_only()
    async def edit(
        self, interaction: discord.Interaction, 레이드: str,
        골드: app_commands.Range[int, 0, 1_000_000] | None = None,
        귀속골드: app_commands.Range[int, 0, 1_000_000] | None = None,
        입장레벨: app_commands.Range[float, 0, 2000] | None = None,
        인원: app_commands.Choice[int] | None = None,
        더보기비용: app_commands.Range[int, 0, 1_000_000] | None = None,
    ):
        bot = bot_of(interaction)
        before = bot.cfg.raid(레이드)
        if before is None:
            raise RuleError("목록에서 레이드를 골라 주세요.")
        values = {
            k: v for k, v in {
                "gold": 골드, "bound_gold": 귀속골드, "min_item_level": 입장레벨,
                "party_size": 인원.value if 인원 else None, "more_cost": 더보기비용,
            }.items() if v is not None
        }
        if not values:
            raise RuleError("바꿀 항목(골드/귀속골드/입장레벨/인원/더보기비용)을 하나 이상 입력해 주세요.")
        if values.get("bound_gold", before.bound_gold) > values.get("gold", before.gold):
            raise RuleError("귀속 골드가 전체 골드보다 클 수 없어요.")

        bot.db.set_raid_override(before.key, interaction.user.id, to_epoch(now_kst()), **values)
        bot.reload_raids()
        after = bot.cfg.raids[before.key]
        await interaction.response.send_message(
            f"🔧 **{after.name}** 정보 변경\n{_describe(before)} → **{_describe(after)}**\n"
            "-# 캐릭터별 골드 레이드 추천에 바로 반영돼요."
        )

    @app_commands.command(
        name="초기화",
        description="/레이드 수정 으로 바꾼 값을 지우고 config.toml 값으로 되돌립니다",
        extras=meta(CAT_LEADER, "/레이드 초기화 레이드:세르카 노말", leader=True),
    )
    @app_commands.autocomplete(레이드=raid_autocomplete)
    @leader_only()
    async def reset(self, interaction: discord.Interaction, 레이드: str):
        bot = bot_of(interaction)
        before = bot.cfg.raid(레이드)
        if before is None:
            raise RuleError("목록에서 레이드를 골라 주세요.")
        if not bot.db.clear_raid_override(before.key):
            raise RuleError(f"{before.name}은 디스코드에서 수정한 값이 없어요.")
        bot.reload_raids()
        after = bot.cfg.raids[before.key]
        await interaction.response.send_message(
            f"↩️ **{after.name}** config.toml 값으로 복원\n{_describe(before)} → **{_describe(after)}**"
        )

    @app_commands.command(
        name="새로고침",
        description="config.toml 을 다시 읽어 레이드 목록을 갱신합니다 (재시작 불필요)",
        extras=meta(
            CAT_LEADER,
            "/레이드 새로고침",
            "config.toml 을 고친 뒤 실행하면 바뀐 항목을 보여 줘요. 파일에 오류가 있으면 기존 설정을 유지해요.",
            leader=True,
        ),
    )
    @leader_only()
    async def reload(self, interaction: discord.Interaction):
        bot = bot_of(interaction)
        old = bot.cfg
        try:
            bot.reload_raids()
        except Exception as e:  # toml 문법 오류, 값 검증 실패 등
            raise RuleError(f"config.toml 을 읽지 못해 기존 설정을 유지해요: {e}") from e
        changes = diff_raids(old, bot.cfg)
        body = "\n".join(changes) if changes else "바뀐 레이드가 없어요."
        await interaction.response.send_message(
            f"🔄 레이드 설정을 다시 읽었어요 (기준일 {bot.cfg.data_updated or '미기재'})\n{body}"[:2000],
            ephemeral=not changes,
        )


async def setup(bot: commands.Bot):
    await bot.add_cog(Raids())
