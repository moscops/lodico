"""디스코드와 무관한 규칙/집계 로직. 테스트는 이 모듈을 대상으로 한다."""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, time, timedelta

from .config import Config, Raid
from .db import Availability, Character, Database, Session
from .timeutil import KST, WEEKDAYS, from_epoch, week_start, to_epoch


class RuleError(Exception):
    """사용자에게 그대로 보여줄 규칙 위반 메시지."""


def week_bounds(cfg: Config, at: datetime) -> tuple[datetime, datetime]:
    start = week_start(at, cfg.reset_weekday, cfg.reset_hour)
    return start, start + timedelta(days=7)


PERIODS = ["이번주", "오늘", *WEEKDAYS]


def period_range(cfg: Config, now: datetime, period: str) -> tuple[datetime, datetime, bool]:
    """(시작, 끝, 하루 단위인지). 요일('금')은 이번 주간(리셋~리셋) 안의 그 요일 하루."""
    week_s, week_e = week_bounds(cfg, now)
    if period == "이번주":
        return week_s, week_e, False
    if period == "오늘":
        day = now.astimezone(KST).date()
    elif period in WEEKDAYS:
        day = week_s.date() + timedelta(days=(WEEKDAYS.index(period) - week_s.weekday()) % 7)
    else:
        raise ValueError(f"알 수 없는 기간: {period}")
    start = datetime.combine(day, time(0), KST)
    return start, start + timedelta(days=1), True


# ---- 골드 계획 -----------------------------------------------------------

def _gold_rank(r: Raid) -> tuple:
    # 골드 많은 순 → 거래 가능 골드 많은 순 → 높은 난이도 순
    return (r.gold, r.tradable_gold, r.min_item_level)


def gold_earner_ids(cfg: Config, chars: list[Character]) -> set[int]:
    """공대원(원정대)별 아이템 레벨 상위 N 캐릭터 = 골드 획득 캐릭터."""
    by_member: dict[int, list[Character]] = defaultdict(list)
    for c in chars:
        by_member[c.member_id].append(c)
    ids: set[int] = set()
    for cs in by_member.values():
        cs = sorted(cs, key=lambda c: (-c.item_level, c.name))
        ids.update(c.id for c in cs[: cfg.gold_characters])
    return ids


def gold_raids(cfg: Config, character: Character) -> list[Raid]:
    """이 캐릭터가 골드를 받을 레이드: 레이드(group)마다 가장 좋은 난이도를 고른 뒤 상위 N개."""
    best: dict[str, Raid] = {}
    for r in cfg.raids.values():
        if r.event or r.gold <= 0 or character.item_level < r.min_item_level:
            continue
        cur = best.get(r.group)
        if cur is None or _gold_rank(r) > _gold_rank(cur):
            best[r.group] = r
    return sorted(best.values(), key=_gold_rank, reverse=True)[: cfg.gold_raids_per_character]


@dataclass(frozen=True)
class Homework:
    """캐릭터의 주간 숙제."""
    tier: str | None          # 적용된 숙제 구간 (구간표가 없으면 None)
    explicit: bool            # 캐릭터에 직접 지정한 구간인지 (False 면 레벨로 자동)
    gold: list[Raid]          # 골드 받는 레이드
    extra: list[Raid]         # 골드 없이 추가로 도는 레이드

    @property
    def all(self) -> list[Raid]:
        return self.gold + self.extra


def resolve_tier(cfg: Config, character: Character):
    """(구간, 직접 지정 여부). 지정한 구간이 없어졌거나 레벨이 모자라면 자동 구간으로."""
    if character.tier and character.tier in cfg.tiers:
        tier = cfg.tiers[character.tier]
        if tier.min_level <= character.item_level:
            return tier, True
    return cfg.auto_tier(character.item_level), False


def homework(cfg: Config, character: Character, earns_gold: bool = True) -> Homework:
    if not cfg.tiers:  # 구간표가 없으면 골드 많은 순 상위 N개
        return Homework(None, False, gold_raids(cfg, character) if earns_gold else [], [])
    tier, explicit = resolve_tier(cfg, character)
    if not earns_gold:
        return Homework(tier.label if tier else None, explicit, [], [])
    if tier is None:  # 구간표 아래 레벨: 골드 많은 순 상위 N개
        return Homework(None, False, gold_raids(cfg, character), [])
    ok = lambda k: k in cfg.raids and cfg.raids[k].min_item_level <= character.item_level  # noqa: E731
    gold = [cfg.raids[k] for k in tier.raids if ok(k)]
    extra = [cfg.raids[tier.no_gold]] if tier.no_gold and ok(tier.no_gold) else []
    return Homework(tier.label, explicit, gold, extra)


def homework_plan(cfg: Config, chars: list[Character]) -> dict[int, Homework]:
    """character_id → 숙제. 골드 획득 캐릭터(원정대 상위 N)가 아니면 레이드 없음."""
    earners = gold_earner_ids(cfg, chars)
    return {c.id: homework(cfg, c, c.id in earners) for c in chars}


def gold_plan(cfg: Config, chars: list[Character]) -> dict[int, list[Raid]]:
    """character_id → 골드 레이드 목록. 골드 획득 캐릭터가 아니면 빈 목록."""
    return {cid: hw.gold for cid, hw in homework_plan(cfg, chars).items()}


@dataclass(frozen=True)
class GoldSummary:
    gold: int
    bound: int
    more_cost: int
    bound_after: int      # 더보기 전부 한 뒤 남는 귀속 골드
    tradable_after: int   # 더보기 전부 한 뒤 남는 거래 가능 골드

    @property
    def tradable(self) -> int:
        return self.gold - self.bound

    @property
    def after(self) -> int:
        return self.bound_after + self.tradable_after


def gold_summary(raids: list[Raid]) -> GoldSummary:
    """더보기 비용은 귀속 골드가 있는 레이드는 귀속에서, 없는 레이드는 거래 가능 골드에서 빠진다."""
    gold = bound = more = bound_after = tradable_after = 0
    for r in raids:
        gold += r.gold
        bound += r.bound_gold
        more += r.more_cost
        if r.bound_gold:
            bound_after += r.bound_gold - r.more_cost
            tradable_after += r.gold - r.bound_gold
        else:
            tradable_after += r.gold - r.more_cost
    return GoldSummary(gold, bound, more, bound_after, tradable_after)


# ---- 파티 편성 (4명 단위, 서폿 1 + 딜러 3) --------------------------------

def party_count(raid: Raid | None) -> int:
    return max(1, (raid.party_size if raid else 4) // 4)


def choose_party(cfg: Config, raid: Raid, members: list[Character], character: Character, strict: bool) -> int:
    """새 캐릭터가 들어갈 파티 번호. strict 면 서폿 규칙을 어기는 자리는 RuleError.

    서폿 → 서폿 없는 파티, 딜러 → 딜러 3명 미만 파티 (서폿이 있는 쪽, 인원 적은 쪽 우선).
    strict 가 아니면 규칙을 못 지켜도 빈자리에 넣는다 (수동 편성: 대신 경고가 뜬다).
    """
    n = party_count(raid)
    size = [0] * (n + 1)
    sup = [0] * (n + 1)
    for c in members:
        p = c.party_no if c.party_no and c.party_no <= n else None
        if p:
            size[p] += 1
            sup[p] += cfg.is_support(c.cls)
    open_parties = [p for p in range(1, n + 1) if size[p] < 4]
    if not open_parties:
        raise RuleError("정원이 찼어요.")
    if cfg.is_support(character.cls):
        good = [p for p in open_parties if sup[p] == 0]
        why = "모든 파티에 이미 서폿이 있어요 (파티마다 서폿 1명)."
    else:
        good = [p for p in open_parties if size[p] - sup[p] < 3]
        why = "딜러 자리가 없어요. 남은 자리는 서폿 자리예요 (파티마다 딜러 최대 3명)."
    if good:
        return min(good, key=lambda p: (-sup[p] if not cfg.is_support(character.cls) else 0, size[p], p))
    if strict:
        raise RuleError(why)
    return min(open_parties, key=lambda p: (size[p], p))


def composition_warnings(cfg: Config, raid: Raid | None, members: list[Character]) -> list[str]:
    """꽉 찬 파티에 서폿이 없거나, 한 파티에 서폿이 2명 이상이면 경고. (딜러 3 + 빈자리는 괜찮음)"""
    n = party_count(raid)
    out = []
    for p in range(1, n + 1):
        party = [c for c in members if c.party_no == p]
        sups = sum(cfg.is_support(c.cls) for c in party)
        label = f"{p}파티 " if n > 1 else ""
        if len(party) >= 4 and sups == 0:
            out.append(f"{label}서폿 없음 (딜러 {len(party)}명)")
        elif sups >= 2:
            out.append(f"{label}서폿 {sups}명")
    return out


def add_to_session(
    db: Database, cfg: Config, session: Session, character: Character, now: datetime, strict: bool
) -> tuple[int, list[str]]:
    """규칙 검사 → 파티 배정 → 추가. (파티 번호, 경고) 반환. 규칙 위반이면 RuleError."""
    warnings = check_join(db, cfg, session, character, now)
    raid = cfg.raid(session.raid_key)
    members = db.session_characters(session.id)
    party = choose_party(cfg, raid, members, character, strict)
    db.add_slot(session.id, character.id, party)
    after = composition_warnings(cfg, raid, db.session_characters(session.id))
    return party, warnings + [f"⚠️ {w}" for w in after]


def supports_first(cfg: Config, chars: list[Character]) -> list[Character]:
    """여러 명을 한꺼번에 넣을 때 서폿부터 넣어야 파티마다 한 명씩 흩어진다."""
    return sorted(chars, key=lambda c: not cfg.is_support(c.cls))


def session_end(cfg: Config, session: Session) -> int:
    raid = cfg.raid(session.raid_key)
    return session.start_at + (raid.duration_minutes if raid else 60) * 60


def check_join(
    db: Database, cfg: Config, session: Session, character: Character, now: datetime
) -> list[str]:
    """참가 가능하면 경고 목록(비어 있을 수 있음)을 반환하고, 불가능하면 RuleError."""
    raid = cfg.raid(session.raid_key)
    if raid is None:
        raise RuleError("설정에서 삭제된 레이드라 참가할 수 없어요.")
    if session.status != "scheduled":
        raise RuleError("취소된 일정이에요.")
    if session.start_at <= to_epoch(now):
        raise RuleError("이미 시작한 일정이에요.")
    if character.item_level < raid.min_item_level:
        raise RuleError(
            f"{character.name}({character.item_level:g})은 {raid.name} 입장 레벨 "
            f"{raid.min_item_level:g} 미만이에요."
        )

    slots = db.session_characters(session.id)
    if any(c.id == character.id for c in slots):
        raise RuleError(f"{character.name}은 이미 이 일정에 참가 중이에요.")
    if any(c.member_id == character.member_id for c in slots):
        mine = next(c for c in slots if c.member_id == character.member_id)
        raise RuleError(f"이 일정에는 이미 {mine.name}(으)로 참가 중이에요. 한 일정엔 캐릭터 하나만.")
    if len(slots) >= raid.party_size:
        raise RuleError(f"정원이 찼어요 ({len(slots)}/{raid.party_size}).")

    # 같은 캐릭터는 같은 레이드를 (난이도 불문) 주 1회만
    overlap_warnings: list[str] = []
    ws, we = week_bounds(cfg, from_epoch(session.start_at))
    for other in db.list_sessions(session.guild_id, to_epoch(ws), to_epoch(we)):
        if other.id == session.id:
            continue
        other_chars = db.session_characters(other.id)
        other_raid = cfg.raid(other.raid_key)
        same_group = other_raid.group == raid.group if other_raid else other.raid_key == raid.key
        if same_group and other.cleared != 0 and any(c.id == character.id for c in other_chars):
            raise RuleError(
                f"{character.name}은 이번 주 {other_raid.name if other_raid else other.raid_key} "
                f"일정(#{other.id})에 이미 배정돼 있어요. 같은 레이드는 난이도 상관없이 주 1회예요."
            )
        # 같은 시간대 다른 일정에 이미 참가 중이어도 막지 않고 경고만 한다
        overlaps = other.start_at < session_end(cfg, session) and session.start_at < session_end(cfg, other)
        if overlaps and any(c.member_id == character.member_id for c in other_chars):
            overlap_warnings.append(
                f"같은 시간대 일정(#{other.id} {other_raid.name if other_raid else other.raid_key})에도 "
                "참가 중이에요."
            )

    warnings: list[str] = list(overlap_warnings)
    hw = homework_plan(cfg, db.list_characters(character.member_id)).get(character.id)
    plan = hw.all if hw else []
    if plan and raid not in plan:
        same = next((r for r in plan if r.group == raid.group), None)
        if same:
            warnings.append(f"골드 기준 추천 난이도는 {same.name}이에요.")
        else:
            warnings.append(f"{character.name}의 숙제 레이드가 아니에요 (숙제: {', '.join(r.name for r in plan)}).")
    if not is_available(db.list_availability(character.member_id), from_epoch(session.start_at)):
        warnings.append("등록한 가능 시간대 밖의 일정이에요.")
    end = session_end(cfg, session)
    if any(u.start_at < end and session.start_at < u.end_at for u in db.list_unavailable(character.member_id)):
        warnings.append("`/불가` 로 등록한 시간과 겹쳐요.")
    return warnings


def is_available(slots: list[Availability], at: datetime) -> bool:
    """가능 시간을 등록하지 않은 사람은 판단 불가로 보고 True."""
    if not slots:
        return True
    at = at.astimezone(KST)
    minute = at.hour * 60 + at.minute
    for a in slots:
        if a.weekday == at.weekday() and a.start_min <= minute < a.end_min:
            return True
        # 전날 범위가 자정을 넘긴 경우
        if a.weekday == (at.weekday() - 1) % 7 and a.start_min <= minute + 1440 < a.end_min:
            return True
    return False


# ---- 가능 시간 집계 ------------------------------------------------------

def hourly_counts(avail: list[Availability]) -> dict[int, dict[int, set[int]]]:
    """weekday → hour(0~47, 24 이상은 익일) → 가능한 member_id 집합.

    한 시간 전체가 범위에 들어가야 가능으로 센다.
    """
    result: dict[int, dict[int, set[int]]] = defaultdict(lambda: defaultdict(set))
    for a in avail:
        for hour in range(a.start_min // 60, (a.end_min + 59) // 60):
            if a.start_min <= hour * 60 and (hour + 1) * 60 <= a.end_min:
                result[a.weekday][hour].add(a.member_id)
    return result


# ---- /공대일정 집계 ------------------------------------------------------

@dataclass
class SessionView:
    session: Session
    raid: Raid | None
    characters: list[Character]

    @property
    def filled(self) -> str:
        size = self.raid.party_size if self.raid else "?"
        return f"{len(self.characters)}/{size}"


@dataclass
class CharacterRow:
    character: Character
    sessions: list[SessionView]      # 기간 안에 이 캐릭터가 들어간 일정 (시간순)
    remaining: list[Raid]            # 골드 레이드 중 이번 주 일정에 아직 없는 것
    gold_raids: list[Raid]           # 이 캐릭터의 골드 레이드 전체 (레이드 필터 적용)


@dataclass
class MemberRow:
    member_id: int
    discord_id: int | None
    name: str
    characters: list[CharacterRow]

    @property
    def assigned_count(self) -> int:
        return sum(len(r.gold_raids) - len(r.remaining) for r in self.characters)

    @property
    def gold_count(self) -> int:
        return sum(len(r.gold_raids) for r in self.characters)


@dataclass
class Overview:
    start: datetime
    end: datetime
    sessions: list[SessionView]
    # raid_key → 이 레이드가 골드 레이드인데 이번 주 일정에 아직 없는 캐릭터
    unassigned_by_raid: dict[str, list[Character]] = field(default_factory=dict)
    # 캐릭터는 있는데 이번 주 어떤 일정에도 없는 공대원 이름
    idle_members: list[str] = field(default_factory=list)
    # 공대원별 → 캐릭터별 일정/남은 골드 레이드 (공대원 이름순)
    members: list[MemberRow] = field(default_factory=list)
    # 하루 단위 보기(오늘/요일)인지. 하루 보기에서는 남은 골드 레이드 요약을 생략한다
    single_day: bool = False
    # session_id → 고정 파티 번호 (파티 색 표시용)
    party_of: dict[int, int] = field(default_factory=dict)


def build_overview(
    db: Database,
    cfg: Config,
    guild_id: int,
    now: datetime,
    period: str = "이번주",
    raid_key: str | None = None,
) -> Overview:
    week_s, week_e = week_bounds(cfg, now)
    start, end, single_day = period_range(cfg, now, period)

    # 미배정 집계는 기간/레이드 필터와 무관하게 주간 전체 일정 기준
    week_views = [
        SessionView(sess, cfg.raid(sess.raid_key), db.session_characters(sess.id))
        for sess in db.list_sessions(guild_id, to_epoch(week_s), to_epoch(week_e))
    ]
    if (start, end) == (week_s, week_e):
        shown = week_views
    else:
        shown = [
            SessionView(sess, cfg.raid(sess.raid_key), db.session_characters(sess.id))
            for sess in db.list_sessions(guild_id, to_epoch(start), to_epoch(end))
        ]
    if raid_key:
        shown = [v for v in shown if v.session.raid_key == raid_key]

    assigned: dict[str, set[int]] = defaultdict(set)  # group → character ids
    busy_members: set[int] = set()
    for v in week_views:
        group = v.raid.group if v.raid else v.session.raid_key
        for c in v.characters:
            busy_members.add(c.member_id)
            if v.session.cleared != 0:  # 미완료 표시한 일정은 아직 못 깬 숙제로 돌려놓는다
                assigned[group].add(c.id)

    characters = db.list_guild_characters(guild_id)
    plan = {cid: hw.all for cid, hw in homework_plan(cfg, characters).items()}
    only_group = cfg.raids[raid_key].group if raid_key and raid_key in cfg.raids else None
    unassigned: dict[str, list[Character]] = {}
    for c in characters:
        for raid in plan[c.id]:
            if only_group and raid.group != only_group:
                continue
            if c.id not in assigned[raid.group]:
                unassigned.setdefault(raid.key, []).append(c)
    # config 순서(보통 높은 레이드부터)로 정렬
    order = list(cfg.raids)
    unassigned = dict(sorted(unassigned.items(), key=lambda kv: order.index(kv[0])))

    idle = sorted({c.display_name or "?" for c in characters if c.member_id not in busy_members})

    # 공대원별 묶음. 오늘 보기는 오늘 일정이 있는 캐릭터만, 이번 주 보기는 남은 골드 레이드까지.
    members: list[MemberRow] = []
    for c in characters:  # 공대원 이름 → 아이템 레벨 내림차순으로 정렬돼 있음
        sessions = [v for v in shown if any(x.id == c.id for x in v.characters)]
        gold = [r for r in plan[c.id] if not only_group or r.group == only_group]
        remaining = [r for r in gold if c.id not in assigned[r.group]]
        if not sessions and (single_day or not remaining):
            continue
        if not members or members[-1].member_id != c.member_id:
            members.append(MemberRow(c.member_id, c.discord_id, c.display_name or "?", []))
        members[-1].characters.append(CharacterRow(c, sessions, remaining, gold))

    return Overview(start, end, shown, unassigned, idle, members, single_day, db.party_numbers_by_session(guild_id))
