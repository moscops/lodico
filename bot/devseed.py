"""테스트용 더미 공대원을 DB 에 넣고 지우는 개발 도구.

디스코드 계정 없이 매칭·`/공대일정`·`/일정제안` 을 시험해 볼 수 있다. 더미의 discord_id 는 실제 유저가
가질 수 없는 큰 값이라 진짜 공대원과 섞이지 않고, `remove` 로 더미만 깔끔히 지워진다.

    python -m bot.devseed add --guild <서버ID> [--count 8]
    python -m bot.devseed list --guild <서버ID>
    python -m bot.devseed remove --guild <서버ID>

봇이 켜져 있어도 실행할 수 있다 (SQLite 는 짧은 쓰기를 동시에 받는다).
"""
from __future__ import annotations

import argparse
import random
from pathlib import Path

from dotenv import load_dotenv

from .config import ROOT, env
from .db import Database

DUMMY_BASE = 9_000_000_000_000_000_000  # 디스코드 ID(약 1.5e18) 보다 훨씬 큰 값. SQLite 정수 한도(9.22e18) 안쪽
DEALERS = ["블레이드", "소서리스", "워로드", "슬레이어", "건슬링어", "리퍼", "기상술사"]
SUPPORTS = ["바드", "홀리나이트", "도화가"]


def add_dummies(db: Database, guild_id: int, count: int) -> list[str]:
    """더미 공대원 count 명 (캐릭터 2~3개, 4일치 저녁~밤 가능 시간). 이미 있으면 덮어쓴다."""
    lines = []
    for i in range(1, count + 1):
        rng = random.Random(i)  # 같은 번호는 항상 같은 데이터
        member = db.upsert_member(guild_id, DUMMY_BASE + i, f"더미{i}")
        n_chars = rng.choice([2, 3])
        for j in range(1, n_chars + 1):
            # 3명 중 1명꼴로 서폿을 첫 캐릭터로
            cls = rng.choice(SUPPORTS) if (j == 1 and i % 3 == 1) else rng.choice(DEALERS)
            level = round(rng.uniform(1700, 1745) - (j - 1) * rng.uniform(3, 12), 2)
            db.upsert_character(member.id, f"더미{i}-{j}", cls, level)
        days = rng.sample(range(7), 4)
        for d in days:
            start = rng.choice([19, 20, 21]) * 60
            db.set_availability(member.id, d, start, start + rng.choice([3, 4, 5]) * 60)
        lines.append(f"더미{i}: 캐릭터 {n_chars}개, 가능 요일 {sorted(days)}")
    return lines


def remove_dummies(db: Database, guild_id: int) -> int:
    """더미 공대원과 그들의 캐릭터·가능 시간·불가·일정 참가 기록을 지운다 (일정 자체는 남는다)."""
    cur = db.conn.execute(
        "DELETE FROM members WHERE guild_id = ? AND discord_id >= ?", (guild_id, DUMMY_BASE)
    )
    db.conn.commit()
    return cur.rowcount


def list_dummies(db: Database, guild_id: int) -> list[str]:
    rows = db.conn.execute(
        "SELECT m.display_name, COUNT(c.id) AS n FROM members m LEFT JOIN characters c ON c.member_id = m.id "
        "WHERE m.guild_id = ? AND m.discord_id >= ? GROUP BY m.id ORDER BY m.discord_id",
        (guild_id, DUMMY_BASE),
    ).fetchall()
    return [f"{r['display_name']} (캐릭터 {r['n']}개)" for r in rows]


def main() -> None:
    load_dotenv(ROOT / ".env")
    parser = argparse.ArgumentParser(description="테스트용 더미 공대원 관리")
    parser.add_argument("action", choices=["add", "remove", "list"])
    parser.add_argument("--guild", type=int, required=True, help="디스코드 서버 ID")
    parser.add_argument("--count", type=int, default=8, help="add 할 더미 수 (기본 8)")
    args = parser.parse_args()

    db = Database(Path(env("DB_PATH", str(ROOT / "data" / "bot.db"))))
    if args.action == "add":
        print("\n".join(add_dummies(db, args.guild, args.count)))
        print(f"\n더미 {args.count}명을 넣었어요. /공대일정, /가능현황, /일정제안 으로 확인하세요.")
    elif args.action == "remove":
        print(f"더미 {remove_dummies(db, args.guild)}명을 지웠어요.")
    else:
        print("\n".join(list_dummies(db, args.guild)) or "더미가 없어요.")


if __name__ == "__main__":
    main()
