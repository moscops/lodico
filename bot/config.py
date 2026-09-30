"""설정 로딩: 비밀값은 .env, 게임 규칙 값은 config.toml.

레이드 골드/입장 레벨은 패치로 자주 바뀌므로 두 가지 방법으로 갱신한다.
1. config.toml 을 고친 뒤 `/레이드 새로고침` (재시작 불필요)
2. `/레이드 수정` 으로 디스코드에서 바로 덮어쓰기 (DB 에 저장, config.toml 보다 우선)
"""
from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field, replace
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# /레이드 수정 으로 덮어쓸 수 있는 필드
OVERRIDABLE = ("gold", "bound_gold", "min_item_level", "party_size", "more_cost")
EVENT_PREFIX = "[이벤트] "


@dataclass(frozen=True)
class Raid:
    key: str
    group: str
    name: str
    party_size: int
    min_item_level: float
    duration_minutes: int
    gold: int = 0
    bound_gold: int = 0
    more_cost: int = 0  # 전 관문 더보기 비용 합
    overridden: bool = False  # /레이드 수정 으로 바뀐 값인지
    # 기간 한정 이벤트 레이드. 골드 레이드 추천/자동 제안에서 빠지고, ends_on 이 지나면 목록에서 숨는다
    event: bool = False
    ends_on: date | None = None

    @property
    def tradable_gold(self) -> int:
        return self.gold - self.bound_gold

    def is_active(self, today: date) -> bool:
        return self.ends_on is None or today <= self.ends_on


def make_event_raid(
    key: str, name: str, party_size: int, min_item_level: float, duration_minutes: int, ends_on: date | None
) -> Raid:
    name = name if name.startswith(EVENT_PREFIX) else EVENT_PREFIX + name
    return Raid(key=key, group=key, name=name, party_size=party_size, min_item_level=min_item_level,
                duration_minutes=duration_minutes, event=True, ends_on=ends_on)


@dataclass(frozen=True)
class Tier:
    """숙제 구간: 이 구간 캐릭터가 매주 도는 레이드 묶음."""
    label: str
    min_level: float
    raids: tuple[str, ...]        # 골드 받는 레이드 key
    no_gold: str | None = None    # 골드 없이 추가로 도는 레이드 key
    auto: bool = False            # 구간 미지정 캐릭터에 레벨로 자동 배정할지


@dataclass(frozen=True)
class Config:
    reset_weekday: int
    reset_hour: int
    morning_time: str
    reminder_minutes: int
    default_start: str = "21:00"
    gold_characters: int = 6
    gold_raids_per_character: int = 3
    data_updated: str = ""
    support_classes: tuple[str, ...] = ("바드", "홀리나이트", "도화가", "발키리")
    sync_interval_hours: float = 6
    auto_add_new: bool = True
    raids: dict[str, Raid] = field(default_factory=dict)
    tiers: dict[str, Tier] = field(default_factory=dict)

    def raid(self, key: str) -> Raid | None:
        return self.raids.get(key)

    def is_support(self, cls: str) -> bool:
        return cls.replace(" ", "") in self.support_classes

    def auto_tier(self, item_level: float) -> Tier | None:
        """구간을 안 정한 캐릭터의 기본 구간: auto 구간 중 레벨이 되는 가장 높은 것."""
        candidates = [t for t in self.tiers.values() if t.auto and t.min_level <= item_level]
        return max(candidates, key=lambda t: t.min_level, default=None)


def load_config(path: Path | None = None) -> Config:
    path = path or ROOT / "config.toml"
    with open(path, "rb") as f:
        data = tomllib.load(f)
    game = data.get("game", {})
    api = data.get("lostark_api", {})
    defaults = data.get("defaults", {})
    default_duration = int(defaults.get("raid_duration_minutes", 60))

    raids: dict[str, Raid] = {}
    for r in data.get("raids", []):
        if r["key"] in raids:
            raise ValueError(f"config.toml: 레이드 key 중복 '{r['key']}'")
        if r["party_size"] not in (4, 8, 16):
            raise ValueError(f"config.toml: {r['key']} party_size 는 4/8/16 중 하나여야 합니다")
        if r.get("bound_gold", 0) > r.get("gold", 0):
            raise ValueError(f"config.toml: {r['key']} bound_gold 가 gold 보다 큽니다")
        if r.get("event"):
            end = r.get("end_date")
            raids[r["key"]] = make_event_raid(
                r["key"], r["name"], int(r["party_size"]), float(r.get("min_item_level", 0)),
                int(r.get("duration_minutes", default_duration)),
                date.fromisoformat(str(end)) if end else None,
            )
            continue
        raids[r["key"]] = Raid(
            key=r["key"],
            group=r.get("group", r["key"]),
            name=r["name"],
            party_size=int(r["party_size"]),
            min_item_level=float(r.get("min_item_level", 0)),
            duration_minutes=int(r.get("duration_minutes", default_duration)),
            gold=int(r.get("gold", 0)),
            bound_gold=int(r.get("bound_gold", 0)),
            more_cost=int(r.get("more_cost", 0)),
        )

    tiers: dict[str, Tier] = {}
    for t in data.get("tiers", []):
        label = str(t["label"])
        if label in tiers:
            raise ValueError(f"config.toml: 숙제 구간 '{label}' 중복")
        keys = tuple(t.get("raids", []))
        no_gold = t.get("no_gold")
        for k in (*keys, *([no_gold] if no_gold else [])):
            if k not in raids:
                raise ValueError(f"config.toml: 숙제 구간 '{label}' 의 레이드 '{k}' 가 raids 에 없습니다")
            if raids[k].min_item_level > float(t["min_level"]):
                raise ValueError(
                    f"config.toml: 숙제 구간 '{label}'(min_level {t['min_level']}) 에 "
                    f"입장 레벨이 더 높은 '{k}'({raids[k].min_item_level:g}) 가 있습니다"
                )
        tiers[label] = Tier(label, float(t["min_level"]), keys, no_gold, bool(t.get("auto", False)))

    return Config(
        reset_weekday=int(game.get("reset_weekday", 2)),
        reset_hour=int(game.get("reset_hour", 6)),
        morning_time=str(defaults.get("morning_time", "09:00")),
        reminder_minutes=int(defaults.get("reminder_minutes", 30)),
        default_start=str(defaults.get("default_start", "21:00")),
        gold_characters=int(game.get("gold_characters", 6)),
        gold_raids_per_character=int(game.get("gold_raids_per_character", 3)),
        data_updated=str(game.get("data_updated", "")),
        support_classes=tuple(game.get("support_classes", ["바드", "홀리나이트", "도화가", "발키리"])),
        sync_interval_hours=float(api.get("sync_interval_hours", 6)),
        auto_add_new=bool(api.get("auto_add_new", True)),
        raids=raids,
        tiers=tiers,
    )


def apply_overrides(cfg: Config, overrides: dict[str, dict[str, float | int]]) -> Config:
    """DB 에 저장된 레이드 값 덮어쓰기를 적용한 새 Config. 없어진 레이드의 덮어쓰기는 무시."""
    raids = dict(cfg.raids)
    for key, values in overrides.items():
        if key not in raids:
            continue
        changes = {k: v for k, v in values.items() if k in OVERRIDABLE and v is not None}
        if changes:
            raids[key] = replace(raids[key], **changes, overridden=True)
    return replace(cfg, raids=raids)


def add_event_raids(cfg: Config, rows: list[dict]) -> Config:
    """디스코드에서 추가한 이벤트 레이드(DB)를 합친 새 Config."""
    raids = dict(cfg.raids)
    for r in rows:
        raids[r["key"]] = make_event_raid(
            r["key"], r["name"], int(r["party_size"]), float(r["min_item_level"]), int(r["duration_minutes"]),
            date.fromisoformat(r["ends_on"]) if r["ends_on"] else None,
        )
    return replace(cfg, raids=raids)


def env(name: str, default: str | None = None) -> str | None:
    value = os.getenv(name, default)
    return value if value else default
