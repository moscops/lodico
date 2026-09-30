"""자동 일정 제안 (README 4.2 매칭).

1. 캐릭터별 골드 레이드 중 이번 주 일정에 아직 없는 것 = 수요(demand)
2. 이번 주 남은 시간을 1시간 단위 시작 시각 후보로 나눈다
3. 인원이 많은 레이드부터(같으면 골드 많은 순), 후보 시각마다 '가능 시간 안이고 그 시간에 다른 일정이 없는' 공대원을 센다
4. 파티를 채울 수 있는 시각 중 가능 인원이 가장 많은 시각(동률이면 이른 시각)을 골라 파티를 짠다
5. 더 이상 파티를 못 채울 때까지 반복, 다음 레이드로

고정 파티 중 시간 미정인 줄은 먼저 파티 단위로 배치한다: 파티원 모두 되는 시각에 레이드를 이어서 배치.
고정 파티가 맡은 (캐릭터, 레이드)는 일반 매칭에서 뺀다.

제약: 한 공대원은 같은 시간에 캐릭터 하나, 한 일정엔 공대원당 캐릭터 하나, 같은 캐릭터는 같은 레이드 주 1회,
파티(4명)마다 서폿 최대 1명 · 딜러 최대 3명 → 꽉 찬 파티는 반드시 서폿 1명.
가능 시간을 등록하지 않은 공대원은 언제 되는지 모르므로 제안에서 빠진다.
"""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from .config import Config, Raid
from .db import Availability, Character, Database
from .services import homework_plan, party_count, session_end, week_bounds
from .timeutil import KST, from_epoch, to_epoch

SLOT_STEP = timedelta(hours=1)


@dataclass
class Proposal:
    raid: Raid
    start: datetime
    characters: list[Character]
    party_raid_id: int | None = None   # 고정 파티 줄에서 나온 제안이면
    party_number: int | None = None

    @property
    def end(self) -> datetime:
        return self.start + timedelta(minutes=self.raid.duration_minutes)


@dataclass
class PlanResult:
    proposals: list[Proposal] = field(default_factory=list)
    # raid_key → 제안에 못 들어간 캐릭터 (인원 부족/시간 안 맞음)
    leftovers: dict[str, list[Character]] = field(default_factory=dict)
    # 골드 레이드가 남았는데 가능 시간이 없어 제안에서 빠진 공대원
    no_availability: list[str] = field(default_factory=list)
    # 파티원 모두 되는 시간을 못 찾은 고정 파티 번호
    party_unplaced: list[int] = field(default_factory=list)


def available_between(slots: list[Availability], start: datetime, end: datetime) -> bool:
    """[start, end) 전체가 어떤 요일 가능 시간 하나에 들어가는지 (자정 넘김 포함)."""
    start, end = start.astimezone(KST), end.astimezone(KST)
    for base in (start.date(), start.date() - timedelta(days=1)):
        day0 = datetime.combine(base, datetime.min.time(), KST)
        for a in slots:
            if a.weekday != base.weekday():
                continue
            if day0 + timedelta(minutes=a.start_min) <= start and end <= day0 + timedelta(minutes=a.end_min):
                return True
    return False


def _overlaps(intervals: list[tuple[datetime, datetime]], start: datetime, end: datetime) -> bool:
    return any(s < end and start < e for s, e in intervals)


def _pick_party(
    cfg: Config, raid: Raid, per_member: dict[int, list[Character]], load: dict[int, int], names: dict[int, str]
) -> list[Character]:
    """공대원당 1캐릭, 파티(4명)마다 서폿 최대 1 · 딜러 최대 3. 남은 숙제가 많은 공대원 먼저."""
    n = party_count(raid)
    order = sorted(per_member, key=lambda m: (-load[m], names[m]))
    supports: list[Character] = []
    dealers: list[Character] = []
    used: set[int] = set()
    for m in order:
        sup = next((c for c in per_member[m] if cfg.is_support(c.cls)), None)
        if sup and len(supports) < n:
            supports.append(sup)
            used.add(m)
    for m in order:
        if m in used:
            continue
        dealer = next((c for c in per_member[m] if not cfg.is_support(c.cls)), None)
        if dealer and len(dealers) < 3 * n:
            dealers.append(dealer)
            used.add(m)
    return supports + dealers


def propose(
    db: Database,
    cfg: Config,
    guild_id: int,
    now: datetime,
    raid_key: str | None = None,
    min_fill: int | None = None,
    lead: timedelta = timedelta(hours=1),
    max_proposals: int = 20,
) -> PlanResult:
    """min_fill: 파티 최소 인원 (기본 = 정원). 정원보다 작게 주면 빈자리가 있는 일정도 제안한다."""
    week_s, week_e = week_bounds(cfg, now)
    chars = db.list_guild_characters(guild_id)
    plan = {cid: hw.all for cid, hw in homework_plan(cfg, chars).items()}
    names = {c.member_id: c.display_name or "?" for c in chars}

    # 이미 잡힌 이번 주 일정: 레이드별 배정 캐릭터, 공대원별 바쁜 시간
    assigned: dict[str, set[int]] = defaultdict(set)
    busy: dict[int, list[tuple[datetime, datetime]]] = defaultdict(list)
    for s in db.list_sessions(guild_id, to_epoch(week_s), to_epoch(week_e)):
        raid = cfg.raid(s.raid_key)
        group = raid.group if raid else s.raid_key
        interval = (from_epoch(s.start_at), from_epoch(session_end(cfg, s)))
        for c in db.session_characters(s.id):
            if s.cleared != 0:  # 미완료 표시한 일정의 레이드는 다시 잡을 수 있다
                assigned[group].add(c.id)
            busy[c.member_id].append(interval)

    # /불가 로 막아 둔 시간도 바쁜 시간으로 취급
    for u in db.list_guild_unavailable(guild_id, to_epoch(week_s)):
        busy[u.member_id].append((from_epoch(u.start_at), from_epoch(u.end_at)))

    only_group = cfg.raids[raid_key].group if raid_key and raid_key in cfg.raids else None
    parties = db.list_parties(guild_id)
    covered = {
        (c.id, cfg.raids[r.raid_key].group)
        for p in parties for r in p.raids if r.raid_key in cfg.raids for c in r.characters
    }
    demand: dict[str, list[Character]] = defaultdict(list)
    for c in chars:
        for r in plan[c.id]:
            if (only_group is None or r.group == only_group) and c.id not in assigned[r.group] \
                    and (c.id, r.group) not in covered:
                demand[r.key].append(c)

    avail = {m: db.list_availability(m) for m in {c.member_id for c in chars}}
    result = PlanResult()
    result.no_availability = sorted({names[c.member_id] for cs in demand.values() for c in cs if not avail[c.member_id]})
    # 남은 수요가 많은 공대원을 먼저 태운다 (그래야 전원이 골고루 소화)
    load = defaultdict(int)
    for cs in demand.values():
        for c in cs:
            load[c.member_id] += 1

    first = max(now + lead, week_s)
    first = first.replace(minute=0, second=0, microsecond=0) + (SLOT_STEP if first.minute or first.second else timedelta())
    # 큰 파티부터: 8인은 8명이 동시에 비어야 해서 맞출 시간이 적다. 4인이 먼저 좋은 시간을 차지하면 못 잡는다.
    # 같은 인원이면 골드 많은 레이드부터.
    order = sorted(demand, key=lambda k: (-cfg.raids[k].party_size, -cfg.raids[k].gold, k))

    # ---- 고정 파티 (시간 미정 줄) ----
    for party in parties:
        rows = []
        for r in party.raids:
            raid = cfg.raid(r.raid_key)
            if raid is None or r.timed or (only_group and raid.group != only_group):
                continue
            if db.find_party_session(r.id, to_epoch(week_s), to_epoch(week_e)):
                continue
            members = [c for c in r.characters if c.id not in assigned[raid.group]]
            if members:
                rows.append((r, raid, members))
        if not rows or len(result.proposals) >= max_proposals:
            continue
        total = sum((timedelta(minutes=raid.duration_minutes) for _, raid, _ in rows), timedelta())
        placed = None
        start = first
        while placed is None and start + total <= week_e:
            t, ok = start, True
            for _, raid, members in rows:
                end = t + timedelta(minutes=raid.duration_minutes)
                if any(_overlaps(busy[c.member_id], t, end) or not available_between(avail.get(c.member_id, []), t, end)
                       for c in members):
                    ok = False
                    break
                t = end
            if ok:
                placed = start
            start += SLOT_STEP
        if placed is None:
            result.party_unplaced.append(party.number)
            continue
        t = placed
        for r, raid, members in rows:
            end = t + timedelta(minutes=raid.duration_minutes)
            result.proposals.append(Proposal(raid, t, members, r.id, party.number))
            for c in members:
                busy[c.member_id].append((t, end))
                assigned[raid.group].add(c.id)
            t = end

    for key in order:
        raid = cfg.raids[key]
        need = min(min_fill or raid.party_size, raid.party_size)
        duration = timedelta(minutes=raid.duration_minutes)
        pending = [c for c in demand[key] if avail[c.member_id]]

        while pending and len(result.proposals) < max_proposals:
            best: tuple[int, datetime, list[Character]] | None = None
            start = first
            while start + duration <= week_e:
                end = start + duration
                per_member: dict[int, list[Character]] = defaultdict(list)
                for c in pending:
                    m = c.member_id
                    if _overlaps(busy[m], start, end) or not available_between(avail[m], start, end):
                        continue
                    per_member[m].append(c)
                party = _pick_party(cfg, raid, per_member, load, names)
                if len(party) >= need and (best is None or len(party) > best[0]):
                    best = (len(party), start, party)
                start += SLOT_STEP
            if best is None:
                break

            _, start, party = best
            result.proposals.append(Proposal(raid, start, party))
            for c in party:
                pending.remove(c)
                load[c.member_id] -= 1
                busy[c.member_id].append((start, start + duration))

        left = [c for c in demand[key] if c in pending or not avail[c.member_id]]
        if left:
            result.leftovers[key] = left

    result.proposals.sort(key=lambda p: (p.start, p.raid.name))
    return result
