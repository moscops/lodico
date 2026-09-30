"""공식 API 로 받은 원정대 정보를 DB 에 반영. 네트워크 없이 테스트할 수 있게 분리했다."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta

from .config import Config, Raid
from .db import Database
from .lostark_api import ApiCharacter
from .services import gold_plan
from .timeutil import fmt_date, from_epoch, now_kst, to_epoch


@dataclass
class CharacterChange:
    name: str
    cls: str
    old_level: float | None  # None 이면 새로 추가된 캐릭터
    new_level: float
    gold_before: list[Raid]
    gold_after: list[Raid]
    removed_from: list[str] = field(default_factory=list)  # 레벨이 올라 안 가게 된 레이드라 일정에서 뺀 것 ('10/3(토) 종막 하드')

    @property
    def gold_changed(self) -> bool:
        return [r.key for r in self.gold_before] != [r.key for r in self.gold_after]


@dataclass
class SyncResult:
    changes: list[CharacterChange] = field(default_factory=list)
    # DB 에는 있는데 API 원정대 목록에 없는 캐릭터 (이름 변경/삭제 가능성)
    missing: list[str] = field(default_factory=list)

    @property
    def gold_changes(self) -> list[CharacterChange]:
        return [c for c in self.changes if c.gold_changed]


def min_raid_level(cfg: Config) -> float:
    return min((r.min_item_level for r in cfg.raids.values()), default=0)


def apply_roster(
    db: Database, cfg: Config, member_id: int, roster: list[ApiCharacter], min_level: float, add_new: bool = True,
    now: datetime | None = None,
) -> SyncResult:
    """등록된 캐릭터의 직업/레벨을 갱신하고, add_new 면 min_level 이상 새 캐릭터를 추가한다.

    레벨이 올라 더 이상 골드로 안 가게 된 레이드는, 아직 시작 전인 그 레이드 일정에서 그 캐릭터를 뺀다
    (공대장이 새 레이드 일정에 다시 배정하도록)."""
    now = now or now_kst()
    before = db.list_characters(member_id)
    plan0 = gold_plan(cfg, before)
    plan_before = {c.name: plan0[c.id] for c in before}
    old_levels = {c.name: c.item_level for c in before}
    by_name = {c.name: c for c in before}

    touched: set[str] = set()
    for a in roster:
        if a.item_level <= 0:
            continue
        c = by_name.get(a.name)
        if c is not None:
            if (c.item_level, c.cls) != (a.item_level, a.cls):
                db.update_character_info(c.id, a.cls or c.cls, a.item_level)
                touched.add(a.name)
        elif add_new and a.item_level >= min_level:
            db.upsert_character(member_id, a.name, a.cls, a.item_level)
            touched.add(a.name)

    # 레벨이 올라 더 높은 기본 구간에 들어갈 수 있게 됐으면 고정해 둔 구간을 풀어 자동으로 따라가게 한다.
    # (같은 레벨대의 변형 구간 _2/_3 은 최소 레벨이 같아 그대로 둔다)
    roster_names = {a.name for a in roster}
    released: set[str] = set()
    for c in db.list_characters(member_id):
        tier = cfg.tiers.get(c.tier) if c.tier else None
        auto = cfg.auto_tier(c.item_level)
        if c.name in roster_names and tier and auto and auto.min_level > tier.min_level:
            db.set_character_tier(c.id, None)
            released.add(c.name)

    after = db.list_characters(member_id)
    plan_after = gold_plan(cfg, after)
    result = SyncResult()
    for c in after:
        gb, ga = plan_before.get(c.name, []), plan_after[c.id]
        if c.name in touched or [r.key for r in gb] != [r.key for r in ga]:
            change = CharacterChange(c.name, c.cls, old_levels.get(c.name), c.item_level, gb, ga)
            old = old_levels.get(c.name)
            if old is not None and (c.item_level > old or c.name in released):
                dropped = {r.key for r in gb} - {r.key for r in ga}
                change.removed_from = _leave_sessions(db, cfg, c, dropped, now)
                change.removed_from += [
                    f"고정 파티 {n} {cfg.raid(k).name if cfg.raid(k) else k}"
                    for n, k in db.remove_character_from_party_raids(c.id, dropped)
                ]
            result.changes.append(change)
    result.missing = [c.name for c in before if c.name not in roster_names]
    return result


def _leave_sessions(db: Database, cfg: Config, c, raid_keys: set[str], now: datetime) -> list[str]:
    """캐릭터 c 를 raid_keys 레이드의 시작 전 일정에서 뺀다. 뺀 일정을 '10/3(토) 종막 하드' 꼴로 돌려준다."""
    if not raid_keys:
        return []
    row = db.conn.execute("SELECT guild_id FROM members WHERE id = ?", (c.member_id,)).fetchone()
    if row is None:
        return []
    left = []
    for s in db.list_sessions(row["guild_id"], to_epoch(now), to_epoch(now + timedelta(days=400))):
        if s.raid_key in raid_keys and any(x.id == c.id for x in db.session_characters(s.id)):
            db.remove_slot(s.id, c.id)
            raid = cfg.raid(s.raid_key)
            left.append(f"{fmt_date(from_epoch(s.start_at).date())} {raid.name if raid else s.raid_key}")
    return left
