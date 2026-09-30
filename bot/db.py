"""SQLite 저장소. 공대 규모(수십 명)에서는 동기 sqlite3 로 충분하다."""
from __future__ import annotations

import re
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS members (
    id           INTEGER PRIMARY KEY,
    guild_id     INTEGER NOT NULL,
    discord_id   INTEGER NOT NULL,
    display_name TEXT NOT NULL,
    roster_name  TEXT,                     -- 공식 API 연동용 대표 캐릭터 이름
    synced_at    INTEGER,                  -- 마지막 API 동기화 (unix epoch)
    UNIQUE (guild_id, discord_id)
);

CREATE TABLE IF NOT EXISTS characters (
    id          INTEGER PRIMARY KEY,
    member_id   INTEGER NOT NULL REFERENCES members(id) ON DELETE CASCADE,
    name        TEXT NOT NULL,
    class       TEXT NOT NULL,
    item_level  REAL NOT NULL,
    tier        TEXT,                      -- 숙제 구간 (config.toml [[tiers]] label). NULL 이면 레벨로 자동
    UNIQUE (member_id, name)
);

-- 요일별 기본 가능 시간. weekday 0=월. end_min 이 1440 을 넘으면 자정 넘김.
CREATE TABLE IF NOT EXISTS availability (
    id        INTEGER PRIMARY KEY,
    member_id INTEGER NOT NULL REFERENCES members(id) ON DELETE CASCADE,
    weekday   INTEGER NOT NULL CHECK (weekday BETWEEN 0 AND 6),
    start_min INTEGER NOT NULL,
    end_min   INTEGER NOT NULL,
    UNIQUE (member_id, weekday)
);

-- /불가 로 등록한 예외: 이 구간은 평소 가능 시간이어도 안 됨 (지난 뒤에는 의미 없음)
CREATE TABLE IF NOT EXISTS unavailable (
    id        INTEGER PRIMARY KEY,
    member_id INTEGER NOT NULL REFERENCES members(id) ON DELETE CASCADE,
    start_at  INTEGER NOT NULL,            -- unix epoch (초)
    end_at    INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_unavailable_member ON unavailable (member_id, start_at);

CREATE TABLE IF NOT EXISTS sessions (
    id         INTEGER PRIMARY KEY,
    guild_id   INTEGER NOT NULL,
    raid_key   TEXT NOT NULL,
    start_at   INTEGER NOT NULL,           -- unix epoch (초)
    status     TEXT NOT NULL DEFAULT 'scheduled' CHECK (status IN ('scheduled', 'cancelled')),
    memo       TEXT,
    created_by INTEGER NOT NULL,           -- discord user id
    reminded   INTEGER NOT NULL DEFAULT 0,
    party_raid_id INTEGER,                 -- 고정 파티에서 만들어진 일정이면 그 파티 레이드 줄
    cleared    INTEGER                     -- 수동 표시: 1 = 완료, 0 = 미완료(못 깸), NULL = 일정 끝 시각으로 자동 판단
);
CREATE INDEX IF NOT EXISTS idx_sessions_guild_start ON sessions (guild_id, start_at);

CREATE TABLE IF NOT EXISTS session_slots (
    session_id   INTEGER NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    character_id INTEGER NOT NULL REFERENCES characters(id) ON DELETE CASCADE,
    party_no     INTEGER,                  -- 레이드 안의 파티 (4명 단위, 1부터)
    PRIMARY KEY (session_id, character_id)
);

-- /레이드 수정 으로 덮어쓴 레이드 값 (config.toml 보다 우선). NULL 이면 config 값 사용
CREATE TABLE IF NOT EXISTS raid_overrides (
    raid_key       TEXT PRIMARY KEY,
    gold           INTEGER,
    bound_gold     INTEGER,
    min_item_level REAL,
    party_size     INTEGER,
    more_cost      INTEGER,
    updated_by     INTEGER NOT NULL,
    updated_at     INTEGER NOT NULL
);

-- 고정 파티: 번호별로 레이드 줄 여러 개. 줄마다 요일/시각(선택)과 참가 캐릭터
CREATE TABLE IF NOT EXISTS parties (
    id       INTEGER PRIMARY KEY,
    guild_id INTEGER NOT NULL,
    number   INTEGER NOT NULL,
    label    TEXT,                         -- 표시용 (예: 1770+)
    UNIQUE (guild_id, number)
);
CREATE TABLE IF NOT EXISTS party_raids (
    id        INTEGER PRIMARY KEY,
    party_id  INTEGER NOT NULL REFERENCES parties(id) ON DELETE CASCADE,
    position  INTEGER NOT NULL,
    raid_key  TEXT NOT NULL,
    weekday   INTEGER,                     -- 0=월. NULL 이면 시간 미정 (자동 제안 대상)
    start_min INTEGER
);
CREATE TABLE IF NOT EXISTS party_raid_members (
    party_raid_id INTEGER NOT NULL REFERENCES party_raids(id) ON DELETE CASCADE,
    character_id  INTEGER NOT NULL REFERENCES characters(id) ON DELETE CASCADE,
    PRIMARY KEY (party_raid_id, character_id)
);

-- 디스코드에서 추가한 기간 한정 이벤트 레이드
CREATE TABLE IF NOT EXISTS event_raids (
    key              TEXT PRIMARY KEY,
    name             TEXT NOT NULL,
    party_size       INTEGER NOT NULL,
    min_item_level   REAL NOT NULL,
    duration_minutes INTEGER NOT NULL,
    ends_on          TEXT,                 -- YYYY-MM-DD, 이 날까지 표시
    created_by       INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS guild_settings (
    guild_id          INTEGER PRIMARY KEY,
    notify_channel_id INTEGER,
    morning_time      TEXT,
    reminder_minutes  INTEGER,
    leader_role_id    INTEGER,
    last_morning_date TEXT,
    board_message_id  INTEGER,             -- 알림 채널에 고정한 '이번 주 공대 일정' 메시지
    board_week        TEXT,                -- 그 메시지가 보여 주는 주간 시작일 (YYYY-MM-DD)
    parties_week      TEXT,                -- 고정 파티 일정을 마지막으로 만든 주간 시작일
    eve_week          TEXT                 -- 리셋 전날 요약을 마지막으로 보낸 주간 시작일
);
"""


@dataclass
class Member:
    id: int
    guild_id: int
    discord_id: int
    display_name: str
    roster_name: str | None = None
    synced_at: int | None = None


@dataclass
class Character:
    id: int
    member_id: int
    name: str
    cls: str
    item_level: float
    tier: str | None = None
    party_no: int | None = None  # 일정 참가자로 조회했을 때 그 일정 안의 파티
    # 조인해서 채우는 값
    discord_id: int | None = None
    display_name: str | None = None


@dataclass
class Session:
    id: int
    guild_id: int
    raid_key: str
    start_at: int
    status: str
    memo: str | None
    created_by: int
    reminded: bool
    party_raid_id: int | None = None
    cleared: int | None = None


@dataclass
class PartyRaid:
    id: int
    party_number: int
    raid_key: str
    weekday: int | None
    start_min: int | None
    characters: list[Character]

    @property
    def timed(self) -> bool:
        return self.weekday is not None and self.start_min is not None


@dataclass
class Party:
    id: int
    number: int
    label: str | None
    raids: list[PartyRaid]


@dataclass
class Availability:
    member_id: int
    weekday: int
    start_min: int
    end_min: int


@dataclass
class Unavailable:
    id: int
    member_id: int
    start_at: int
    end_at: int


@dataclass
class GuildSettings:
    guild_id: int
    notify_channel_id: int | None
    morning_time: str | None
    reminder_minutes: int | None
    leader_role_id: int | None
    last_morning_date: str | None
    board_message_id: int | None = None
    board_week: str | None = None
    parties_week: str | None = None
    eve_week: str | None = None


def _character(row: sqlite3.Row) -> Character:
    keys = row.keys()
    return Character(
        id=row["id"],
        member_id=row["member_id"],
        name=row["name"],
        cls=row["class"],
        item_level=row["item_level"],
        tier=row["tier"] if "tier" in keys else None,
        party_no=row["party_no"] if "party_no" in keys else None,
        discord_id=row["discord_id"] if "discord_id" in keys else None,
        display_name=row["display_name"] if "display_name" in keys else None,
    )


def _session(row: sqlite3.Row) -> Session:
    return Session(
        id=row["id"],
        guild_id=row["guild_id"],
        raid_key=row["raid_key"],
        start_at=row["start_at"],
        status=row["status"],
        memo=row["memo"],
        created_by=row["created_by"],
        reminded=bool(row["reminded"]),
        party_raid_id=row["party_raid_id"] if "party_raid_id" in row.keys() else None,
        cleared=row["cleared"] if "cleared" in row.keys() else None,
    )


_CHAR_JOIN = """
SELECT c.*, m.discord_id, m.display_name
FROM characters c JOIN members m ON m.id = c.member_id
"""


class Database:
    def __init__(self, path: str | Path = ":memory:"):
        self.conn = sqlite3.connect(str(path))
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")
        self.conn.executescript(SCHEMA)
        self._migrate()
        self.conn.commit()
        # 일정/참가자가 바뀌면 guild_id 로 호출된다 (고정 일정 메시지 갱신용)
        self.on_change: Callable[[int], None] | None = None

    def _changed(self, guild_id: int | None = None, session_id: int | None = None) -> None:
        if self.on_change is None:
            return
        if guild_id is None and session_id is not None:
            row = self.conn.execute("SELECT guild_id FROM sessions WHERE id = ?", (session_id,)).fetchone()
            guild_id = row["guild_id"] if row else None
        if guild_id is not None:
            self.on_change(guild_id)

    MIGRATIONS = {
        "members": (("roster_name", "TEXT"), ("synced_at", "INTEGER")),
        "characters": (("tier", "TEXT"),),
        "raid_overrides": (("more_cost", "INTEGER"),),
        "guild_settings": (("board_message_id", "INTEGER"), ("board_week", "TEXT"), ("parties_week", "TEXT"), ("eve_week", "TEXT")),
        "sessions": (("party_raid_id", "INTEGER"), ("cleared", "INTEGER")),
        "session_slots": (("party_no", "INTEGER"),),
    }

    def _migrate(self) -> None:
        """이전 버전 DB 에 새 컬럼을 추가한다."""
        for table, columns in self.MIGRATIONS.items():
            cols = {r["name"] for r in self.conn.execute(f"PRAGMA table_info({table})")}
            for col, typ in columns:
                if col not in cols:
                    self.conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {typ}")

    def close(self) -> None:
        self.conn.close()

    # ---- members -------------------------------------------------------
    def upsert_member(self, guild_id: int, discord_id: int, display_name: str) -> Member:
        self.conn.execute(
            """INSERT INTO members (guild_id, discord_id, display_name) VALUES (?, ?, ?)
               ON CONFLICT (guild_id, discord_id) DO UPDATE SET display_name = excluded.display_name""",
            (guild_id, discord_id, display_name),
        )
        self.conn.commit()
        row = self.conn.execute(
            "SELECT * FROM members WHERE guild_id = ? AND discord_id = ?", (guild_id, discord_id)
        ).fetchone()
        return Member(**dict(row))

    def get_member(self, guild_id: int, discord_id: int) -> Member | None:
        row = self.conn.execute(
            "SELECT * FROM members WHERE guild_id = ? AND discord_id = ?", (guild_id, discord_id)
        ).fetchone()
        return Member(**dict(row)) if row else None

    def list_members(self, guild_id: int) -> list[Member]:
        rows = self.conn.execute(
            "SELECT * FROM members WHERE guild_id = ? ORDER BY display_name", (guild_id,)
        ).fetchall()
        return [Member(**dict(r)) for r in rows]

    def set_roster(self, member_id: int, roster_name: str | None) -> None:
        self.conn.execute("UPDATE members SET roster_name = ? WHERE id = ?", (roster_name, member_id))
        self.conn.commit()

    def mark_synced(self, member_id: int, at: int) -> None:
        self.conn.execute("UPDATE members SET synced_at = ? WHERE id = ?", (at, member_id))
        self.conn.commit()

    def list_linked_members(self, guild_id: int | None = None) -> list[Member]:
        sql = "SELECT * FROM members WHERE roster_name IS NOT NULL"
        args: tuple = ()
        if guild_id is not None:
            sql += " AND guild_id = ?"
            args = (guild_id,)
        rows = self.conn.execute(sql + " ORDER BY synced_at IS NOT NULL, synced_at", args).fetchall()
        return [Member(**dict(r)) for r in rows]

    # ---- characters ----------------------------------------------------
    def upsert_character(
        self, member_id: int, name: str, cls: str, item_level: float
    ) -> tuple[Character, bool]:
        """(캐릭터, 새로 만들었는지) 를 반환."""
        existing = self.conn.execute(
            "SELECT id FROM characters WHERE member_id = ? AND name = ?", (member_id, name)
        ).fetchone()
        self.conn.execute(
            """INSERT INTO characters (member_id, name, class, item_level) VALUES (?, ?, ?, ?)
               ON CONFLICT (member_id, name) DO UPDATE SET
                 class = excluded.class, item_level = excluded.item_level""",
            (member_id, name, cls, item_level),
        )
        self.conn.commit()
        row = self.conn.execute(
            _CHAR_JOIN + "WHERE c.member_id = ? AND c.name = ?", (member_id, name)
        ).fetchone()
        return _character(row), existing is None

    def set_character_tier(self, character_id: int, tier: str | None) -> None:
        self.conn.execute("UPDATE characters SET tier = ? WHERE id = ?", (tier, character_id))
        self.conn.commit()

    def update_character_info(self, character_id: int, cls: str, item_level: float) -> None:
        self.conn.execute(
            "UPDATE characters SET class = ?, item_level = ? WHERE id = ?", (cls, item_level, character_id)
        )
        self.conn.commit()

    def delete_character(self, member_id: int, name: str) -> bool:
        cur = self.conn.execute(
            "DELETE FROM characters WHERE member_id = ? AND name = ?", (member_id, name)
        )
        self.conn.commit()
        if cur.rowcount:  # 참가 중이던 일정에서도 빠지므로
            row = self.conn.execute("SELECT guild_id FROM members WHERE id = ?", (member_id,)).fetchone()
            self._changed(guild_id=row["guild_id"] if row else None)
        return cur.rowcount > 0

    def get_character(self, character_id: int) -> Character | None:
        row = self.conn.execute(_CHAR_JOIN + "WHERE c.id = ?", (character_id,)).fetchone()
        return _character(row) if row else None

    def list_characters(self, member_id: int) -> list[Character]:
        rows = self.conn.execute(
            _CHAR_JOIN + "WHERE c.member_id = ? ORDER BY c.item_level DESC, c.name", (member_id,)
        ).fetchall()
        return [_character(r) for r in rows]

    def list_guild_characters(self, guild_id: int) -> list[Character]:
        rows = self.conn.execute(
            _CHAR_JOIN + "WHERE m.guild_id = ? ORDER BY m.display_name, c.item_level DESC",
            (guild_id,),
        ).fetchall()
        return [_character(r) for r in rows]

    # ---- availability --------------------------------------------------
    def set_availability(self, member_id: int, weekday: int, start_min: int, end_min: int) -> None:
        if not (0 <= start_min < 1440 and start_min < end_min <= start_min + 1440):
            raise ValueError("잘못된 시간 범위")
        self.conn.execute(
            """INSERT INTO availability (member_id, weekday, start_min, end_min) VALUES (?, ?, ?, ?)
               ON CONFLICT (member_id, weekday) DO UPDATE SET
                 start_min = excluded.start_min, end_min = excluded.end_min""",
            (member_id, weekday, start_min, end_min),
        )
        self.conn.commit()

    def clear_availability(self, member_id: int, weekday: int) -> None:
        self.conn.execute(
            "DELETE FROM availability WHERE member_id = ? AND weekday = ?", (member_id, weekday)
        )
        self.conn.commit()

    def list_availability(self, member_id: int) -> list[Availability]:
        rows = self.conn.execute(
            "SELECT member_id, weekday, start_min, end_min FROM availability "
            "WHERE member_id = ? ORDER BY weekday",
            (member_id,),
        ).fetchall()
        return [Availability(**dict(r)) for r in rows]

    def list_guild_availability(self, guild_id: int) -> list[Availability]:
        rows = self.conn.execute(
            """SELECT a.member_id, a.weekday, a.start_min, a.end_min
               FROM availability a JOIN members m ON m.id = a.member_id
               WHERE m.guild_id = ?""",
            (guild_id,),
        ).fetchall()
        return [Availability(**dict(r)) for r in rows]

    # ---- unavailable (주간 예외) ----------------------------------------
    def add_unavailable(self, member_id: int, start_at: int, end_at: int) -> Unavailable:
        if start_at >= end_at:
            raise ValueError("잘못된 시간 범위")
        cur = self.conn.execute(
            "INSERT INTO unavailable (member_id, start_at, end_at) VALUES (?, ?, ?)", (member_id, start_at, end_at)
        )
        self.conn.commit()
        return Unavailable(cur.lastrowid, member_id, start_at, end_at)

    def list_unavailable(self, member_id: int, since: int = 0) -> list[Unavailable]:
        rows = self.conn.execute(
            "SELECT id, member_id, start_at, end_at FROM unavailable "
            "WHERE member_id = ? AND end_at > ? ORDER BY start_at",
            (member_id, since),
        ).fetchall()
        return [Unavailable(**dict(r)) for r in rows]

    def list_guild_unavailable(self, guild_id: int, since: int = 0) -> list[Unavailable]:
        rows = self.conn.execute(
            """SELECT u.id, u.member_id, u.start_at, u.end_at
               FROM unavailable u JOIN members m ON m.id = u.member_id
               WHERE m.guild_id = ? AND u.end_at > ? ORDER BY u.start_at""",
            (guild_id, since),
        ).fetchall()
        return [Unavailable(**dict(r)) for r in rows]

    def delete_unavailable(self, member_id: int, unavailable_id: int) -> bool:
        cur = self.conn.execute(
            "DELETE FROM unavailable WHERE id = ? AND member_id = ?", (unavailable_id, member_id)
        )
        self.conn.commit()
        return cur.rowcount > 0

    def purge_unavailable(self, before: int) -> int:
        cur = self.conn.execute("DELETE FROM unavailable WHERE end_at <= ?", (before,))
        self.conn.commit()
        return cur.rowcount

    # ---- sessions ------------------------------------------------------
    def create_session(
        self, guild_id: int, raid_key: str, start_at: int, created_by: int, memo: str | None,
        party_raid_id: int | None = None,
    ) -> Session:
        cur = self.conn.execute(
            "INSERT INTO sessions (guild_id, raid_key, start_at, memo, created_by, party_raid_id) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (guild_id, raid_key, start_at, memo, created_by, party_raid_id),
        )
        self.conn.commit()
        self._changed(guild_id=guild_id)
        return self.get_session(cur.lastrowid)  # type: ignore[return-value]

    def find_session(self, guild_id: int, raid_key: str, start_at: int) -> Session | None:
        row = self.conn.execute(
            "SELECT * FROM sessions WHERE guild_id = ? AND raid_key = ? AND start_at = ? AND status = 'scheduled'",
            (guild_id, raid_key, start_at),
        ).fetchone()
        return _session(row) if row else None

    def get_session(self, session_id: int) -> Session | None:
        row = self.conn.execute("SELECT * FROM sessions WHERE id = ?", (session_id,)).fetchone()
        return _session(row) if row else None

    def list_sessions(
        self, guild_id: int, start: int, end: int, include_cancelled: bool = False
    ) -> list[Session]:
        sql = "SELECT * FROM sessions WHERE guild_id = ? AND start_at >= ? AND start_at < ?"
        if not include_cancelled:
            sql += " AND status = 'scheduled'"
        rows = self.conn.execute(sql + " ORDER BY start_at, id", (guild_id, start, end)).fetchall()
        return [_session(r) for r in rows]

    def list_all_scheduled_between(self, start: int, end: int) -> list[Session]:
        rows = self.conn.execute(
            "SELECT * FROM sessions WHERE status = 'scheduled' AND start_at >= ? AND start_at < ? "
            "ORDER BY start_at",
            (start, end),
        ).fetchall()
        return [_session(r) for r in rows]

    def update_session_time(self, session_id: int, start_at: int) -> None:
        # 시간이 바뀌면 리마인더를 다시 보낼 수 있게 초기화
        self.conn.execute(
            "UPDATE sessions SET start_at = ?, reminded = 0 WHERE id = ?", (start_at, session_id)
        )
        self.conn.commit()
        self._changed(session_id=session_id)

    def update_session_memo(self, session_id: int, memo: str | None) -> None:
        self.conn.execute("UPDATE sessions SET memo = ? WHERE id = ?", (memo, session_id))
        self.conn.commit()
        self._changed(session_id=session_id)

    def find_unlinked_party_session(self, guild_id: int, raid_key: str, party_number: int, start: int, end: int) -> Session | None:
        """파티 연결 없이 만든 예전 일정 중 메모가 '파티 N' 이고 레이드가 같은 것 (같은 주간)."""
        row = self.conn.execute(
            "SELECT * FROM sessions WHERE guild_id = ? AND raid_key = ? AND party_raid_id IS NULL AND memo = ? "
            "AND status = 'scheduled' AND start_at >= ? AND start_at < ? ORDER BY start_at LIMIT 1",
            (guild_id, raid_key, f"파티 {party_number}", start, end),
        ).fetchone()
        return _session(row) if row else None

    def link_party_raid(self, session_id: int, party_raid_id: int) -> None:
        self.conn.execute("UPDATE sessions SET party_raid_id = ? WHERE id = ?", (party_raid_id, session_id))
        self.conn.commit()
        self._changed(session_id=session_id)

    def set_cleared(self, session_id: int, cleared: int | None) -> None:
        """1 = 완료, 0 = 미완료, None = 자동(끝 시각 기준)."""
        self.conn.execute("UPDATE sessions SET cleared = ? WHERE id = ?", (cleared, session_id))
        self.conn.commit()
        self._changed(session_id=session_id)

    def cancel_session(self, session_id: int) -> None:
        self.conn.execute("UPDATE sessions SET status = 'cancelled' WHERE id = ?", (session_id,))
        self.conn.commit()
        self._changed(session_id=session_id)

    def mark_reminded(self, session_id: int) -> None:
        self.conn.execute("UPDATE sessions SET reminded = 1 WHERE id = ?", (session_id,))
        self.conn.commit()

    def add_slot(self, session_id: int, character_id: int, party_no: int | None = None) -> None:
        self.conn.execute(
            "INSERT INTO session_slots (session_id, character_id, party_no) VALUES (?, ?, ?)",
            (session_id, character_id, party_no),
        )
        self.conn.commit()
        self._changed(session_id=session_id)

    def remove_slot(self, session_id: int, character_id: int) -> bool:
        cur = self.conn.execute(
            "DELETE FROM session_slots WHERE session_id = ? AND character_id = ?",
            (session_id, character_id),
        )
        self.conn.commit()
        if cur.rowcount:
            self._changed(session_id=session_id)
        return cur.rowcount > 0

    def remove_character_from_party_raids(self, character_id: int, raid_keys: set[str]) -> list[tuple[int, str]]:
        """고정 파티의 해당 레이드 줄에서 캐릭터를 뺀다. 뺀 (파티 번호, 레이드 key) 목록을 돌려준다."""
        if not raid_keys:
            return []
        marks = ",".join("?" * len(raid_keys))
        rows = self.conn.execute(
            f"SELECT pr.id, p.number, pr.raid_key FROM party_raid_members pm "
            f"JOIN party_raids pr ON pr.id = pm.party_raid_id JOIN parties p ON p.id = pr.party_id "
            f"WHERE pm.character_id = ? AND pr.raid_key IN ({marks}) ORDER BY p.number, pr.position",
            (character_id, *raid_keys),
        ).fetchall()
        for r in rows:
            self.conn.execute(
                "DELETE FROM party_raid_members WHERE party_raid_id = ? AND character_id = ?", (r["id"], character_id)
            )
        self.conn.commit()
        return [(r["number"], r["raid_key"]) for r in rows]

    def session_characters(self, session_id: int) -> list[Character]:
        rows = self.conn.execute(
            "SELECT c.*, m.discord_id, m.display_name, s.party_no FROM characters c "
            "JOIN members m ON m.id = c.member_id "
            "JOIN session_slots s ON s.character_id = c.id WHERE s.session_id = ? "
            "ORDER BY s.party_no, m.display_name, c.name",
            (session_id,),
        ).fetchall()
        return [_character(r) for r in rows]

    # ---- raid overrides ------------------------------------------------
    def get_raid_overrides(self) -> dict[str, dict]:
        rows = self.conn.execute("SELECT * FROM raid_overrides").fetchall()
        return {r["raid_key"]: dict(r) for r in rows}

    def set_raid_override(self, raid_key: str, updated_by: int, updated_at: int, **values: object) -> None:
        allowed = {"gold", "bound_gold", "min_item_level", "party_size", "more_cost"}
        if not values or not set(values) <= allowed:
            raise ValueError(f"알 수 없는 레이드 필드: {set(values) - allowed}")
        self.conn.execute(
            "INSERT OR IGNORE INTO raid_overrides (raid_key, updated_by, updated_at) VALUES (?, ?, ?)",
            (raid_key, updated_by, updated_at),
        )
        cols = ", ".join(f"{k} = ?" for k in values)
        self.conn.execute(
            f"UPDATE raid_overrides SET {cols}, updated_by = ?, updated_at = ? WHERE raid_key = ?",
            (*values.values(), updated_by, updated_at, raid_key),
        )
        self.conn.commit()

    def clear_raid_override(self, raid_key: str) -> bool:
        cur = self.conn.execute("DELETE FROM raid_overrides WHERE raid_key = ?", (raid_key,))
        self.conn.commit()
        return cur.rowcount > 0

    # ---- parties ---------------------------------------------------------
    def replace_parties(self, guild_id: int, parties: list[dict]) -> None:
        """이 서버의 고정 파티를 통째로 바꾼다 (시트가 원본).

        parties: [{"number", "label", "raids": [{"raid_key", "weekday", "start_min", "character_ids"}]}]
        이미 만들어진 일정은 남기되 파티 연결만 끊는다.
        """
        old = [r["id"] for r in self.conn.execute(
            "SELECT pr.id FROM party_raids pr JOIN parties p ON p.id = pr.party_id WHERE p.guild_id = ?", (guild_id,)
        )]
        if old:
            marks = ",".join("?" * len(old))
            self.conn.execute(f"UPDATE sessions SET party_raid_id = NULL WHERE party_raid_id IN ({marks})", old)
        self.conn.execute("DELETE FROM parties WHERE guild_id = ?", (guild_id,))
        for p in parties:
            pid = self.conn.execute(
                "INSERT INTO parties (guild_id, number, label) VALUES (?, ?, ?)", (guild_id, p["number"], p.get("label"))
            ).lastrowid
            for pos, r in enumerate(p["raids"]):
                rid = self.conn.execute(
                    "INSERT INTO party_raids (party_id, position, raid_key, weekday, start_min) VALUES (?, ?, ?, ?, ?)",
                    (pid, pos, r["raid_key"], r.get("weekday"), r.get("start_min")),
                ).lastrowid
                self.conn.executemany(
                    "INSERT OR IGNORE INTO party_raid_members (party_raid_id, character_id) VALUES (?, ?)",
                    [(rid, cid) for cid in r["character_ids"]],
                )
        self.conn.commit()

    def list_parties(self, guild_id: int) -> list[Party]:
        parties = [
            Party(r["id"], r["number"], r["label"], [])
            for r in self.conn.execute(
                "SELECT * FROM parties WHERE guild_id = ? ORDER BY number", (guild_id,)
            )
        ]
        for p in parties:
            for r in self.conn.execute(
                "SELECT * FROM party_raids WHERE party_id = ? ORDER BY position", (p.id,)
            ).fetchall():
                chars = [
                    _character(c) for c in self.conn.execute(
                        _CHAR_JOIN + "JOIN party_raid_members pm ON pm.character_id = c.id "
                        "WHERE pm.party_raid_id = ? ORDER BY m.display_name", (r["id"],)
                    )
                ]
                p.raids.append(PartyRaid(r["id"], p.number, r["raid_key"], r["weekday"], r["start_min"], chars))
        return parties

    def party_numbers_by_session(self, guild_id: int) -> dict[int, int]:
        rows = self.conn.execute(
            """SELECT s.id, p.number FROM sessions s
               JOIN party_raids pr ON pr.id = s.party_raid_id JOIN parties p ON p.id = pr.party_id
               WHERE s.guild_id = ?""", (guild_id,)
        )
        result = {r["id"]: r["number"] for r in rows}
        # 파티 연결 없이 만든 예전 일정도 메모가 '파티 N' 이면 같은 파티로 본다
        memo_rows = self.conn.execute(
            "SELECT id, memo FROM sessions WHERE guild_id = ? AND party_raid_id IS NULL AND memo LIKE '파티 %'",
            (guild_id,),
        )
        for r in memo_rows:
            m = re.match(r"파티 (\d+)", r["memo"])
            if m:
                result[r["id"]] = int(m.group(1))
        return result

    def find_party_session(self, party_raid_id: int, start: int, end: int) -> Session | None:
        row = self.conn.execute(
            "SELECT * FROM sessions WHERE party_raid_id = ? AND status = 'scheduled' AND start_at >= ? AND start_at < ?",
            (party_raid_id, start, end),
        ).fetchone()
        return _session(row) if row else None

    # ---- event raids ----------------------------------------------------
    def list_event_raids(self) -> list[dict]:
        return [dict(r) for r in self.conn.execute("SELECT * FROM event_raids ORDER BY key")]

    def add_event_raid(
        self, name: str, party_size: int, min_item_level: float, duration_minutes: int,
        ends_on: str | None, created_by: int,
    ) -> str:
        n = self.conn.execute("SELECT COUNT(*) FROM event_raids").fetchone()[0] + 1
        while self.conn.execute("SELECT 1 FROM event_raids WHERE key = ?", (f"event_{n}",)).fetchone():
            n += 1
        key = f"event_{n}"
        self.conn.execute(
            "INSERT INTO event_raids (key, name, party_size, min_item_level, duration_minutes, ends_on, created_by) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (key, name, party_size, min_item_level, duration_minutes, ends_on, created_by),
        )
        self.conn.commit()
        return key

    def set_event_raid_end(self, key: str, ends_on: str) -> bool:
        cur = self.conn.execute("UPDATE event_raids SET ends_on = ? WHERE key = ?", (ends_on, key))
        self.conn.commit()
        return cur.rowcount > 0

    # ---- settings ------------------------------------------------------
    def get_settings(self, guild_id: int) -> GuildSettings:
        row = self.conn.execute(
            "SELECT * FROM guild_settings WHERE guild_id = ?", (guild_id,)
        ).fetchone()
        if row:
            return GuildSettings(**dict(row))
        return GuildSettings(guild_id, None, None, None, None, None)

    def list_settings(self) -> list[GuildSettings]:
        rows = self.conn.execute("SELECT * FROM guild_settings").fetchall()
        return [GuildSettings(**dict(r)) for r in rows]

    def update_settings(self, guild_id: int, **values: object) -> None:
        allowed = {
            "notify_channel_id", "morning_time", "reminder_minutes", "leader_role_id", "last_morning_date",
            "board_message_id", "board_week", "parties_week", "eve_week",
        }
        if not values or not set(values) <= allowed:
            raise ValueError(f"알 수 없는 설정: {set(values) - allowed}")
        self.conn.execute("INSERT OR IGNORE INTO guild_settings (guild_id) VALUES (?)", (guild_id,))
        cols = ", ".join(f"{k} = ?" for k in values)
        self.conn.execute(
            f"UPDATE guild_settings SET {cols} WHERE guild_id = ?", (*values.values(), guild_id)
        )
        self.conn.commit()
