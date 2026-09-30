"""고정 파티: 파티 번호별 색, 요일·시각이 정해진 줄의 주간 일정 자동 생성."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from .config import Config
from .db import Database, PartyRaid
from .services import RuleError, add_to_session, supports_first, week_bounds
from .timeutil import KST, WEEKDAYS, fmt_dt, fmt_minutes, to_epoch

# 파티 번호별 색. 시트(배경색)와 디스코드(이모지)가 같은 순서를 쓴다.
# 공대 시트의 구간 색(보라·파랑·주황·초록·노랑)에 빨강·갈색·회색을 더했다.
PALETTE = [
    ("🟪", "D9D2E9"), ("🟦", "CFE2F3"), ("🟧", "FBE4D5"), ("🟩", "E2EFD9"),
    ("🟨", "FEF2CB"), ("🟥", "F4CCCC"), ("🟫", "E6D5C3"), ("⚫", "EDEDED"),
]


def party_emoji(number: int) -> str:
    return PALETTE[(number - 1) % len(PALETTE)][0]


def party_fill(number: int) -> str:
    return PALETTE[(number - 1) % len(PALETTE)][1]


def party_time_text(r: PartyRaid) -> str:
    if not r.timed:
        return "시간 미정"
    return f"{WEEKDAYS[r.weekday]} {fmt_minutes(r.start_min).replace('익일 ', '')}"


def slot_in_week(week_start: datetime, weekday: int, start_min: int) -> datetime:
    """주간(리셋~리셋) 안에서 그 요일·시각. 리셋 전 새벽(수 05:00 등)은 주간 끝쪽 날짜로."""
    day = week_start.date() + timedelta(days=(weekday - week_start.weekday()) % 7)
    at = datetime.combine(day, datetime.min.time(), KST) + timedelta(minutes=start_min)
    if at < week_start:
        at += timedelta(days=7)
    return at


@dataclass
class GenerateResult:
    created: list[str]
    skipped: list[str]


def generate_party_sessions(
    db: Database, cfg: Config, guild_id: int, now: datetime, created_by: int = 0
) -> GenerateResult:
    """요일·시각이 정해진 파티 줄을 이번 주 일정으로 만든다. 이미 만든 줄, 지난 시각은 건너뛴다."""
    week_s, week_e = week_bounds(cfg, now)
    out = GenerateResult([], [])
    for party in db.list_parties(guild_id):
        for r in party.raids:
            raid = cfg.raid(r.raid_key)
            if not r.timed or raid is None:
                continue
            start = slot_in_week(week_s, r.weekday, r.start_min)
            if start <= now or db.find_party_session(r.id, to_epoch(week_s), to_epoch(week_e)):
                continue
            # 파티 연결 없이 예전에 만든 같은 파티·레이드 일정이 이 주에 있으면 새로 만들지 않고 그 일정을 파티에 연결한다
            legacy = db.find_unlinked_party_session(guild_id, raid.key, party.number, to_epoch(week_s), to_epoch(week_e))
            if legacy:
                db.link_party_raid(legacy.id, r.id)
                continue
            session = db.find_session(guild_id, raid.key, to_epoch(start)) or db.create_session(
                guild_id, raid.key, to_epoch(start), created_by, f"파티 {party.number}", party_raid_id=r.id
            )
            joined = 0
            for c in supports_first(cfg, r.characters):
                try:
                    add_to_session(db, cfg, db.get_session(session.id), c, now, strict=False)
                except RuleError as e:
                    if "이미 이 일정에 참가 중" not in str(e):
                        out.skipped.append(f"{party_emoji(party.number)} 파티 {party.number} {raid.name} {c.name}: {e}")
                    continue
                joined += 1
            out.created.append(
                f"{party_emoji(party.number)} 파티 {party.number} · {fmt_dt(start)} {raid.name} ({joined}명)"
            )
    return out


def party_covered(db: Database, cfg: Config, guild_id: int) -> set[tuple[int, str]]:
    """(캐릭터 id, 레이드 group) 중 고정 파티가 맡은 것. 일반 자동 제안에서 뺀다."""
    covered = set()
    for party in db.list_parties(guild_id):
        for r in party.raids:
            raid = cfg.raid(r.raid_key)
            if raid:
                covered |= {(c.id, raid.group) for c in r.characters}
    return covered


def apply_party_inputs(db: Database, guild_id: int, parties, member_ids: dict[str, int]) -> list[str]:
    """시트의 레이드 파티를 DB 고정 파티로 바꾼다. 캐릭터는 그 공대원 열의 공대원 소유여야 한다.

    parties: sheet.PartyInput 목록, member_ids: 시트 공대원 이름 → members.id
    """
    errors: list[str] = []
    chars_by_member: dict[int, dict[str, int]] = {}
    for c in db.list_guild_characters(guild_id):
        chars_by_member.setdefault(c.member_id, {})[c.name] = c.id
    all_chars = db.list_guild_characters(guild_id)
    owner = {c.name: c.display_name for c in all_chars}
    by_name: dict[str, list[int]] = {}
    for c in all_chars:
        by_name.setdefault(c.name, []).append(c.id)

    rows_out = []
    for p in parties:
        raids = []
        for r in p.rows:
            ids = []
            for member, name in r.entries:
                if member is None:  # '캐릭터' 칸: 이름만으로 찾는다
                    found = by_name.get(name, [])
                    if len(found) == 1:
                        if found[0] not in ids:
                            ids.append(found[0])
                    else:
                        why = "등록된 캐릭터가 아니에요" if not found else "같은 이름이 여러 명이에요"
                        errors.append(f"[일정 {r.row}행] 캐릭터 '{name}' — {why}.")
                    continue
                mid = member_ids.get(member)
                if mid is None:
                    errors.append(f"[일정 {r.row}행] '{member}' 열의 공대원을 찾지 못해 {name} 을 뺐어요.")
                    continue
                cid = chars_by_member.get(mid, {}).get(name)
                if cid is None:
                    who = f" ({owner[name]} 님의 캐릭터예요)" if name in owner else ""
                    errors.append(f"[일정 {r.row}행] {member} 님의 캐릭터 '{name}' 이 없어요{who}.")
                    continue
                ids.append(cid)
            raids.append({"raid_key": r.raid_key, "weekday": r.weekday, "start_min": r.start_min, "character_ids": ids})
        rows_out.append({"number": p.number, "label": p.label, "raids": raids})
    db.replace_parties(guild_id, rows_out)
    return errors
