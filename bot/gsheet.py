"""구글 시트 내보내기 (README 4.5). 원본은 봇 DB 이고 시트는 읽기용 — 내보낼 때마다 탭을 통째로 덮어쓴다.

`build_export` 는 표(2차원 리스트)만 만들고, 실제 쓰기는 `write_export` (gspread, 서비스 계정) 가 한다.
그래서 구글에 접속하지 않고도 내용을 테스트할 수 있다.
"""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime

from .config import Config
from .db import Database
from .render import session_done, session_failed
from .services import SessionView, build_overview, week_bounds
from .timeutil import WEEKDAYS, from_epoch, to_epoch

TAB_SESSIONS = "이번 주 일정"
TAB_CLEARS = "클리어 현황"
TAB_STATS = "통계"

Table = list[list[str]]


class ExportError(Exception):
    """사용자에게 그대로 보여줄 내보내기 실패 사유."""


@dataclass
class ExportConfig:
    credentials_file: str
    spreadsheet_id: str


def _status(v: SessionView, now: datetime) -> str:
    if session_failed(v):
        return "❌ 미완료"
    return "✅ 완료" if session_done(v, now) else "🗓️ 예정"


def build_export(db: Database, cfg: Config, guild_id: int, now: datetime) -> dict[str, Table]:
    ov = build_overview(db, cfg, guild_id, now)
    week_s, _ = week_bounds(cfg, now)

    sessions: Table = [["날짜", "요일", "시각", "레이드", "인원", "상태", "참가 캐릭터", "메모", "#"]]
    for v in sorted(ov.sessions, key=lambda v: v.session.start_at):
        start = from_epoch(v.session.start_at)
        chars = ", ".join(f"{c.display_name}({c.name})" for c in v.characters)
        sessions.append([
            start.strftime("%Y-%m-%d"), WEEKDAYS[start.weekday()], start.strftime("%H:%M"),
            v.raid.name if v.raid else v.session.raid_key, v.filled, _status(v, now), chars,
            v.session.memo or "", str(v.session.id),
        ])

    clears: Table = [["공대원", "캐릭터", "직업", "아이템 레벨", "레이드", "상태", "일정"]]
    counts_of: dict[int, list[int]] = {}   # member_id → [전체, 완료, 예정, 미정]
    names: dict[int, str] = {}
    for m in ov.members:
        names[m.member_id] = m.name
        counts = counts_of.setdefault(m.member_id, [0, 0, 0, 0])
        for row in m.characters:
            by_group = {v.raid.group: v for v in row.sessions if v.raid}
            for r in row.gold_raids:
                v = by_group.get(r.group)
                counts[0] += 1
                if v is None:
                    status, when = "❔ 미정", ""
                    counts[3] += 1
                else:
                    when = from_epoch(v.session.start_at).strftime("%m/%d %H:%M")
                    status = _status(v, now)
                    if session_failed(v):
                        counts[3] += 1   # 못 깬 레이드는 다시 잡아야 하므로 미정으로 센다
                    else:
                        counts[1 if session_done(v, now) else 2] += 1
                clears.append([m.name, row.character.name, row.character.cls, f"{row.character.item_level:g}",
                               r.name, status, when])

    # 누적 참가: 이미 끝난 모든 일정에서 공대원별로 센다 (한 일정에 공대원당 캐릭터 하나)
    attended: dict[int, int] = defaultdict(int)
    for s in db.list_sessions(guild_id, 0, to_epoch(now)):
        chars = db.session_characters(s.id)
        if not session_done(SessionView(s, cfg.raid(s.raid_key), chars), now):
            continue
        for c in chars:
            attended[c.member_id] += 1
            names.setdefault(c.member_id, c.display_name or "?")

    stats: Table = [["공대원", "이번 주 숙제", "완료", "예정", "미정", "완료율", "누적 참가 일정"]]
    for mid in sorted(names, key=lambda k: names[k]):
        total, done, planned, left = counts_of.get(mid, [0, 0, 0, 0])
        rate = f"{done / total:.0%}" if total else "-"
        stats.append([names[mid], str(total), str(done), str(planned), str(left), rate, str(attended.get(mid, 0))])
    stats.append([])
    stats.append([f"주간 {week_s:%Y-%m-%d} 시작 · 갱신 {now:%Y-%m-%d %H:%M} KST · 원본은 디스코드 봇 (이 시트는 읽기용)"])
    return {TAB_SESSIONS: sessions, TAB_CLEARS: clears, TAB_STATS: stats}


def load_export_config(credentials_file: str | None, spreadsheet_id: str | None) -> ExportConfig | None:
    if not credentials_file or not spreadsheet_id:
        return None
    return ExportConfig(credentials_file, spreadsheet_id)


def write_export(conf: ExportConfig, tables: dict[str, Table]) -> str:
    """탭을 만들거나 비우고 새 내용을 쓴다 (동기 함수 — asyncio.to_thread 로 부를 것). 시트 URL 을 돌려준다."""
    import gspread
    from gspread.exceptions import APIError, SpreadsheetNotFound

    try:
        client = gspread.service_account(filename=conf.credentials_file)
        book = client.open_by_key(conf.spreadsheet_id)
        existing = {ws.title: ws for ws in book.worksheets()}
        for title, rows in tables.items():
            ws = existing.get(title) or book.add_worksheet(title, rows=max(len(rows) + 20, 100), cols=12)
            ws.clear()
            ws.update(range_name="A1", values=rows)
            ws.format("A1:Z1", {"textFormat": {"bold": True}})
    except FileNotFoundError:
        raise ExportError(f"서비스 계정 키 파일을 찾을 수 없어요: {conf.credentials_file}") from None
    except SpreadsheetNotFound:
        raise ExportError(
            "시트를 찾을 수 없어요. 시트 ID 와, 시트를 서비스 계정 이메일에 '편집자'로 공유했는지 확인해 주세요."
        ) from None
    except APIError as e:
        raise ExportError(f"구글 API 오류: {e}") from None
    return book.url
