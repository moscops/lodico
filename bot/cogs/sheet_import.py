"""/시트양식 · /시트가져오기 — 엑셀로 공대원, 가능 시간, 캐릭터, 일정을 한 번에 넣는다."""
from __future__ import annotations

import io
import logging
from dataclasses import dataclass, field

import discord
from discord import app_commands
from discord.ext import commands

from ..common import CAT_LEADER, bot_of, is_leader, leader_only, meta
from ..lostark_api import ApiError
from ..render import truncate
from ..roster import min_raid_level
from ..services import RuleError, add_to_session, supports_first
from ..parties import apply_party_inputs, generate_party_sessions, party_emoji
from ..sheet import SheetData, build_template, parse_workbook
from ..timeutil import fmt_dt, now_kst, to_epoch
from .planning import send_proposals

log = logging.getLogger(__name__)
MAX_FILE_BYTES = 2 * 1024 * 1024


@dataclass
class Resolved:
    """시트 공대원 이름 → 디스코드 계정."""
    ids: dict[str, int] = field(default_factory=dict)
    display: dict[str, str] = field(default_factory=dict)
    errors: list[str] = field(default_factory=list)


async def resolve_members(guild: discord.Guild, bot, data: SheetData) -> Resolved:
    """디스코드ID → 봇 DB 의 이름 → 서버 멤버 검색 순으로 찾는다. (서버 멤버 권한 없이 이름 검색 가능)"""
    out = Resolved()
    by_sheet_id = {m.name: m.discord_id for m in data.members if m.discord_id}
    db_members = bot.db.list_members(guild.id)
    for name in data.member_names():
        if name in by_sheet_id:
            uid = by_sheet_id[name]
            member = guild.get_member(uid)
            if member is None:
                try:
                    member = await guild.fetch_member(uid)
                except discord.NotFound:
                    out.errors.append(f"'{name}' 의 디스코드ID {uid} 는 이 서버에 없어요.")
                    continue
            out.ids[name], out.display[name] = uid, member.display_name
            continue

        known = [m for m in db_members if m.display_name == name]
        if len(known) == 1:
            out.ids[name], out.display[name] = known[0].discord_id, known[0].display_name
            continue

        try:
            found = await guild.query_members(query=name, limit=20)
        except Exception as e:  # 게이트웨이 시간 초과 등
            log.warning("멤버 검색 실패 %s: %s", name, e)
            found = []
        lowered = name.lower()
        exact = [
            m for m in found
            if lowered in {m.display_name.lower(), m.name.lower(), (m.global_name or "").lower()}
        ]
        if len(exact) == 1:
            out.ids[name], out.display[name] = exact[0].id, exact[0].display_name
        elif len(exact) > 1:
            out.errors.append(f"'{name}' 과 이름이 같은 사람이 여러 명이에요. 공대원 시트에 디스코드ID 를 적어 주세요.")
        else:
            out.errors.append(f"'{name}' 을 서버에서 찾지 못했어요. 서버 별명과 똑같이 적거나 디스코드ID 를 적어 주세요.")
    return out


def preview_text(cfg, data: SheetData, resolved: Resolved) -> str:
    with_avail = sum(1 for m in data.members if m.availability is not None)
    with_roster = sum(1 for m in data.members if m.roster_name)
    lines = [
        f"👥 공대원 **{len(resolved.ids)}**명 확인 (가능 시간 {with_avail}명, 원정대 연동 {with_roster}명)",
        f"🧙 캐릭터 **{len(data.characters)}**개",
        f"🗓️ 일정 **{len(data.sessions)}**개",
    ]
    if data.parties is not None:
        timed = sum(1 for p in data.parties for r in p.rows if r.weekday is not None)
        total = sum(len(p.rows) for p in data.parties)
        lines.insert(2, f"🧩 고정 파티 **{len(data.parties)}**개 · 레이드 {total}줄 (요일·시각 지정 {timed}줄) — 기존 고정 파티는 이 내용으로 바뀌어요")
        for p in data.parties[:10]:
            raids = ", ".join(cfg.raids[r.raid_key].name for r in p.rows)
            lines.insert(3, f"-# 　{party_emoji(p.number)} 파티 {p.number}{' · ' + p.label if p.label else ''}: {raids}")
    for s in data.sessions[:10]:
        lines.append(f"-# 　{fmt_dt(s.start)} {cfg.raids[s.raid_key].name} · {len(s.character_names)}명")
    errors = data.errors + resolved.errors
    if errors:
        lines.append(f"\n⚠️ **확인이 필요한 항목 {len(errors)}건** (이 항목들은 빼고 적용돼요)")
        lines += [f"• {e}" for e in errors[:25]]
        if len(errors) > 25:
            lines.append(f"• …외 {len(errors) - 25}건")
    lines.append("\n[적용]을 누르면 반영돼요. 가능 시간이 적힌 공대원은 그 주 패턴이 시트 내용으로 바뀌어요.")
    return truncate("\n".join(lines), 4000)


async def apply_sheet(interaction: discord.Interaction, data: SheetData, resolved: Resolved) -> list[str]:
    """DB 에 반영하고 결과 줄 목록을 돌려준다."""
    bot = bot_of(interaction)
    guild_id = interaction.guild_id or 0
    db = bot.db
    report: list[str] = []
    errors: list[str] = []

    # 1) 공대원 + 가능 시간
    member_ids: dict[str, int] = {}
    for name, uid in resolved.ids.items():
        member_ids[name] = db.upsert_member(guild_id, uid, resolved.display[name]).id
    avail_count = 0
    for m in data.members:
        if m.name not in member_ids or m.availability is None:
            continue
        mid = member_ids[m.name]
        for d in range(7):
            if d in m.availability:
                db.set_availability(mid, d, *m.availability[d])
            else:
                db.clear_availability(mid, d)
        avail_count += 1
    report.append(f"👥 공대원 {len(member_ids)}명 · 가능 시간 {avail_count}명 반영")

    # 2) 원정대 연동 (대표캐릭터)
    lostark = interaction.client.get_cog("LostArk")
    linked = 0
    for m in data.members:
        if not m.roster_name or m.name not in member_ids:
            continue
        if bot.lostark is None or lostark is None:
            errors.append(f"{m.name}: API 키가 없어 대표캐릭터 연동을 건너뛰었어요.")
            continue
        db.set_roster(member_ids[m.name], m.roster_name)
        member = db.get_member(guild_id, resolved.ids[m.name])
        try:
            await lostark.sync_member(member, add_new=True, min_level=min_raid_level(bot.cfg))
            linked += 1
        except ApiError as e:
            errors.append(f"{m.name}: 원정대 연동 실패 — {e}")
            if e.fatal:
                break
    if linked:
        report.append(f"🔗 원정대 연동 {linked}명 (캐릭터·레벨 자동 등록)")

    # 3) 캐릭터
    owner_of = {c.name: c.member_id for c in db.list_guild_characters(guild_id)}
    char_count = 0
    for c in data.characters:
        if c.member_name not in member_ids:
            continue  # 이름 확인 실패는 미리보기 오류에 이미 나옴
        mid = member_ids[c.member_name]
        if owner_of.get(c.name, mid) != mid:
            errors.append(f"[캐릭터 {c.row}행] {c.name} 은 다른 공대원이 이미 등록했어요.")
            continue
        cls, level = c.cls, c.item_level
        if cls is None or level is None:
            if bot.lostark is None:
                errors.append(f"[캐릭터 {c.row}행] {c.name}: 직업/아이템레벨이 비어 있고 API 키가 없어요.")
                continue
            try:
                profile = await bot.lostark.profile(c.name)
            except ApiError as e:
                errors.append(f"[캐릭터 {c.row}행] {c.name}: {e}")
                continue
            if profile is None:
                errors.append(f"[캐릭터 {c.row}행] {c.name} 을 로스트아크에서 찾지 못했어요.")
                continue
            cls, level = cls or profile.cls, level if level is not None else profile.item_level
        saved, _ = db.upsert_character(mid, c.name, cls, round(level, 2))
        if c.tier_given:
            db.set_character_tier(saved.id, c.tier)
        owner_of[c.name] = mid
        char_count += 1
    report.append(f"🧙 캐릭터 {char_count}개 반영 (숙제 구간 포함)")

    # 3-1) 고정 파티 (시트가 원본: 통째로 교체) → 요일·시각 있는 줄은 이번 주 일정 생성
    if data.parties is not None:
        errors += apply_party_inputs(db, guild_id, data.parties, member_ids)
        report.append(f"🧩 고정 파티 {len(data.parties)}개 반영")
        gen = generate_party_sessions(db, bot.cfg, guild_id, now_kst(), interaction.user.id)
        if gen.created:
            report.append(f"🗓️ 고정 파티 이번 주 일정 {len(gen.created)}개 생성")
            report += [f"-# 　{line}" for line in gen.created[:10]]
        errors += gen.skipped

    # 4) 일정
    now = now_kst()
    by_name = {}
    for ch in db.list_guild_characters(guild_id):
        by_name.setdefault(ch.name, []).append(ch)
    created = joined = 0
    for s in data.sessions:
        if s.start <= now:
            errors.append(f"[일정 {s.row}행] {fmt_dt(s.start)} 은 이미 지난 시각이에요.")
            continue
        session = db.find_session(guild_id, s.raid_key, to_epoch(s.start))
        if session is None:
            session = db.create_session(guild_id, s.raid_key, to_epoch(s.start), interaction.user.id, s.memo)
            created += 1
        in_session = {c.id for c in db.session_characters(session.id)}
        found = []
        for name in s.character_names:
            matches = by_name.get(name, [])
            if len(matches) != 1:
                errors.append(f"[일정 {s.row}행] 캐릭터 '{name}' 을 찾을 수 없어요." if not matches
                              else f"[일정 {s.row}행] 캐릭터 '{name}' 이 여러 명이에요.")
                continue
            if matches[0].id not in in_session:
                found.append(matches[0])
        for ch in supports_first(bot.cfg, found):  # 서폿부터 넣어야 파티마다 흩어진다
            try:
                add_to_session(db, bot.cfg, db.get_session(session.id), ch, now, strict=False)
            except RuleError as e:
                errors.append(f"[일정 {s.row}행] {ch.name}: {e}")
                continue
            in_session.add(ch.id)
            joined += 1
    report.append(f"🗓️ 일정 {created}개 새로 등록 · 참가 {joined}건")

    if errors:
        report.append(f"\n⚠️ 반영하지 못한 항목 {len(errors)}건")
        report += [f"• {e}" for e in errors[:25]]
        if len(errors) > 25:
            report.append(f"• …외 {len(errors) - 25}건")
    return report


class ImportView(discord.ui.View):
    def __init__(self, data: SheetData, resolved: Resolved, author_id: int):
        super().__init__(timeout=900)
        self.data, self.resolved, self.author_id = data, resolved, author_id

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.author_id:
            await interaction.response.send_message("시트를 올린 사람만 적용할 수 있어요.", ephemeral=True)
            return False
        return True

    @discord.ui.button(label="적용", style=discord.ButtonStyle.success)
    async def apply(self, interaction: discord.Interaction, _: discord.ui.Button):
        self.stop()
        await interaction.response.edit_message(content="⏳ 반영 중이에요… (원정대 연동이 많으면 조금 걸려요)", embed=None, view=None)
        report = await apply_sheet(interaction, self.data, self.resolved)
        embed = discord.Embed(title="✅ 시트 반영 완료", description=truncate("\n".join(report), 4000),
                              color=discord.Color.green())
        await interaction.edit_original_response(content=None, embed=embed, view=AfterImportView())

    @discord.ui.button(label="취소", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, _: discord.ui.Button):
        self.stop()
        await interaction.response.edit_message(content="취소했어요. 아무것도 바뀌지 않았어요.", embed=None, view=None)


class AfterImportView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=900)

    @discord.ui.button(label="🧩 이번 주 일정 제안 받기", style=discord.ButtonStyle.primary)
    async def suggest(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not is_leader(interaction):
            await interaction.response.send_message("공대장만 쓸 수 있어요.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        await send_proposals(interaction)


class SheetImport(commands.Cog):
    @app_commands.command(
        name="시트양식",
        description="공대원·가능 시간·캐릭터·일정을 한 번에 넣는 엑셀 양식을 받습니다",
        extras=meta(
            CAT_LEADER,
            "/시트양식  ·  /시트양식 현재정보:True",
            "기본은 예시 데이터만 있는 빈 .xlsx 양식이에요. `현재정보:True` 면 지금 봇에 등록된 공대원·캐릭터·"
            "가능 시간·고정 파티를 채워서 줘요 (고칠 때 편해요).\n"
            "채워서 `/시트가져오기` 로 올리면 돼요. 구글 시트에서 작업했다면 '파일 → 다운로드 → .xlsx' 로 받아 올려 주세요.",
            leader=True,
        ),
    )
    @app_commands.describe(현재정보="지금 봇에 등록된 공대원·캐릭터·고정 파티를 채워서 받기 (기본: 예시만 있는 빈 양식)")
    @leader_only()
    async def template(self, interaction: discord.Interaction, 현재정보: bool = False):
        bot = bot_of(interaction)
        content = build_template(bot.db, bot.cfg, interaction.guild_id or 0, prefill=현재정보)
        file = discord.File(io.BytesIO(content), filename="공대일정_입력양식.xlsx")
        await interaction.response.send_message(
            "📄 입력 양식이에요. **안내** 시트를 먼저 읽어 주세요.\n"
            "다 채우면 `/시트가져오기 파일:` 로 올려 주세요.",
            file=file, ephemeral=True,
        )

    @app_commands.command(
        name="시트가져오기",
        description="작성한 엑셀 양식을 올려 공대원·가능 시간·캐릭터·일정을 한 번에 반영합니다",
        extras=meta(
            CAT_LEADER,
            "/시트가져오기 파일:공대일정_입력양식.xlsx",
            "올리면 먼저 미리보기(인식된 인원, 오류 목록)를 보여 주고, [적용]을 눌러야 반영돼요.\n"
            "적용 후 [이번 주 일정 제안 받기] 버튼으로 바로 자동 일정 제안을 받을 수 있어요.",
            leader=True,
        ),
    )
    @app_commands.describe(파일="/시트양식 으로 받은 .xlsx 파일")
    @leader_only()
    async def import_(self, interaction: discord.Interaction, 파일: discord.Attachment):
        if not 파일.filename.lower().endswith(".xlsx"):
            raise RuleError(".xlsx 파일만 올릴 수 있어요. 구글 시트는 '파일 → 다운로드 → Microsoft Excel(.xlsx)'.")
        if 파일.size > MAX_FILE_BYTES:
            raise RuleError("파일이 너무 커요 (최대 2MB).")
        await interaction.response.defer(ephemeral=True, thinking=True)
        bot = bot_of(interaction)
        data = parse_workbook(await 파일.read(), bot.cfg, now_kst().date())
        if not (data.members or data.characters or data.sessions):
            msg = "\n".join(data.errors) or "시트에 읽을 내용이 없어요. 예시 줄('예시'로 시작)은 무시돼요."
            await interaction.followup.send(f"⚠️ {msg}", ephemeral=True)
            return
        resolved = await resolve_members(interaction.guild, bot, data)
        embed = discord.Embed(title=f"📥 시트 미리보기 — {파일.filename}", description=preview_text(bot.cfg, data, resolved),
                              color=discord.Color.blurple())
        await interaction.followup.send(embed=embed, view=ImportView(data, resolved, interaction.user.id), ephemeral=True)


async def setup(bot: commands.Bot):
    await bot.add_cog(SheetImport())
