"""공식 API 로 받은 원정대 정보를 DB 에 반영. 네트워크 없이 테스트할 수 있게 분리했다."""
from __future__ import annotations

from dataclasses import dataclass, field

from .config import Config, Raid
from .db import Database
from .lostark_api import ApiCharacter
from .services import gold_plan


@dataclass
class CharacterChange:
    name: str
    cls: str
    old_level: float | None  # None 이면 새로 추가된 캐릭터
    new_level: float
    gold_before: list[Raid]
    gold_after: list[Raid]

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
    db: Database, cfg: Config, member_id: int, roster: list[ApiCharacter], min_level: float, add_new: bool = True
) -> SyncResult:
    """등록된 캐릭터의 직업/레벨을 갱신하고, add_new 면 min_level 이상 새 캐릭터를 추가한다."""
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

    after = db.list_characters(member_id)
    plan_after = gold_plan(cfg, after)
    result = SyncResult()
    for c in after:
        gb, ga = plan_before.get(c.name, []), plan_after[c.id]
        if c.name in touched or [r.key for r in gb] != [r.key for r in ga]:
            result.changes.append(
                CharacterChange(c.name, c.cls, old_levels.get(c.name), c.item_level, gb, ga)
            )
    roster_names = {a.name for a in roster}
    result.missing = [c.name for c in before if c.name not in roster_names]
    return result
