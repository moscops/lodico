"""고정 파티: 파티 번호별 색, 요일·시각이 정해진 줄의 주간 일정 자동 생성."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta

from .config import Config
from .db import Database, PartyRaid
from .services import RuleError, add_to_session, supports_first, week_bounds
from .timeutil import KST, WEEKDAYS, fmt_dt, fmt_minutes, to_epoch

# 파티 번호별 시트 배경색 (엑셀·구글 시트). 공대 시트의 구간 색(보라·파랑·주황·초록·노랑)에 빨강·갈색·회색을 더했다.
PALETTE = ["D9D2E9", "CFE2F3", "FBE4D5", "E2EFD9", "FEF2CB", "F4CCCC", "E6D5C3", "EDEDED"]


def party_fill(number: int) -> str:
    return PALETTE[(number - 1) % len(PALETTE)]


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
    filled: list[str] = field(default_factory=list)  # 이미 있던 파티 일정에 빠진 파티원을 채운(또는 다른 파티원을 정리한) 줄


def generate_party_sessions(
    db: Database, cfg: Config, guild_id: int, now: datetime, created_by: int = 0
) -> GenerateResult:
    """요일·시각이 정해진 파티 줄을 이번 주 일정으로 만든다. 지난 시각은 건너뛴다.

    파티 줄마다 일정을 따로 만든다 (같은 레이드·같은 시각이어도 다른 파티와 합치지 않는다).
    이미 있는 파티 일정은 새로 만들지 않고, 빠진 파티원을 채우고 다른 파티 줄 멤버가 섞여 있으면 뺀다."""
    week_s, week_e = week_bounds(cfg, now)
    out = GenerateResult([], [])
    parties = db.list_parties(guild_id)
    # 레이드별로 각 파티 줄의 캐릭터 id (다른 파티 줄 멤버 구별용)
    rows_by_raid: dict[str, list[tuple[int, set[int]]]] = {}
    for party in parties:
        for r in party.raids:
            rows_by_raid.setdefault(r.raid_key, []).append((r.id, {c.id for c in r.characters}))
    for party in parties:
        for r in party.raids:
            raid = cfg.raid(r.raid_key)
            if not r.timed or raid is None:
                continue
            start = slot_in_week(week_s, r.weekday, r.start_min)
            if start <= now:
                continue
            existing = db.find_party_session(r.id, to_epoch(week_s), to_epoch(week_e))
            if existing:
                mine = {c.id for c in r.characters}
                foreign = {cid for rid, ids in rows_by_raid[raid.key] if rid != r.id for cid in ids} - mine
                _fill_party_session(db, cfg, party.number, r, raid, existing, foreign, start, now, out)
                continue
            # 파티 연결 없이 예전에 만든 같은 파티·레이드 일정이 이 주에 있으면 새로 만들지 않고 그 일정을 파티에 연결한다
            legacy = db.find_unlinked_party_session(guild_id, raid.key, party.number, to_epoch(week_s), to_epoch(week_e))
            if legacy:
                db.link_party_raid(legacy.id, r.id)
                continue
            # 같은 레이드·시각의 일정이 이미 있어도 다른 파티 것이면 쓰지 않는다 (파티 연결이 없는 일정만 이어받는다)
            found = db.find_session(guild_id, raid.key, to_epoch(start))
            if found is not None and found.party_raid_id is None:
                db.link_party_raid(found.id, r.id)
                session = found
            else:
                session = db.create_session(
                    guild_id, raid.key, to_epoch(start), created_by, f"파티 {party.number}", party_raid_id=r.id
                )
            joined = 0
            for c in supports_first(cfg, r.characters):
                try:
                    add_to_session(db, cfg, db.get_session(session.id), c, now, strict=False)
                except RuleError as e:
                    if "이미 이 일정에 참가 중" not in str(e):
                        out.skipped.append(f"파티 {party.number} {raid.name} {c.name}: {e}")
                    continue
                joined += 1
            out.created.append(
                f"파티 {party.number} · {fmt_dt(start)} {raid.name} ({joined}명)"
            )
    return out


def _fill_party_session(
    db: Database, cfg: Config, number: int, r: PartyRaid, raid, session, foreign: set[int],
    start: datetime, now: datetime, out: GenerateResult,
) -> None:
    """이미 만든 파티 일정에 빠진 파티원을 넣고, 다른 파티 줄 멤버(같은 레이드)가 섞여 있으면 뺀다."""
    present = {c.id for c in db.session_characters(session.id)}
    removed = 0
    for cid in present & foreign:
        if db.remove_slot(session.id, cid):
            removed += 1
    joined = 0
    missing = [c for c in r.characters if c.id not in present or c.id in foreign]
    for c in supports_first(cfg, missing):
        try:
            add_to_session(db, cfg, db.get_session(session.id), c, now, strict=False)
        except RuleError as e:
            if "이미 이 일정에 참가 중" not in str(e):
                out.skipped.append(f"파티 {number} {raid.name} {c.name}: {e}")
            continue
        joined += 1
    if joined or removed:
        note = f"+{joined}명" + (f", 다른 파티원 {removed}명 정리" if removed else "")
        out.filled.append(f"파티 {number} · {fmt_dt(start)} {raid.name} ({note})")


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


def parties_text(db: Database, cfg: Config, guild_id: int) -> str:
    """/레이드일정 보기:고정파티 — 고정 파티(파티별 레이드·요일·시각·파티원)를 글로."""
    from .render import truncate  # render 가 parties 를 쓰지 않아 순환은 아니지만, 가져오는 쪽을 가볍게 둔다

    parties = db.list_parties(guild_id)
    if not parties:
        return "_고정 파티가 없어요. `/시트양식` 의 [일정] 시트에 적어서 `/시트가져오기` 로 올려 주세요._"
    lines = []
    for p in parties:
        label = f" · {p.label}" if p.label else ""
        lines.append(f"**파티 {p.number}**{label}")
        for r in p.raids:
            raid = cfg.raid(r.raid_key)
            name = raid.name if raid else r.raid_key
            size = f"{len(r.characters)}/{raid.party_size}" if raid else str(len(r.characters))
            who = ", ".join(f"{c.display_name}({c.name})" for c in r.characters) or "비어 있음"
            lines.append(f"　**{name}** `{size}` · {party_time_text(r)}")
            lines.append(f"-# 　　{who}")
    return truncate("\n".join(lines), 4000)


def generate_report(result: GenerateResult) -> str:
    """/레이드일정 보기:고정파티생성 결과 안내문."""
    from .render import truncate

    lines = result.created[:]
    if result.filled:
        lines += ["", "🔧 이미 있던 일정에 파티원을 채웠어요"] + [f"• {s}" for s in result.filled]
    if not (result.created or result.filled):
        lines = ["새로 만들거나 채울 일정이 없어요 (이미 다 채워졌거나, 시간이 지났거나, 시간 미정)."]
    if result.skipped:
        lines += ["", "⚠️ 넣지 못한 캐릭터"] + [f"• {s}" for s in result.skipped]
    return truncate("\n".join(lines), 2000)
