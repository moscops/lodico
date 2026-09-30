"""KST 시간 처리와 사용자 입력(날짜/시각) 파싱.

한국은 서머타임이 없으므로 고정 +9 오프셋을 쓴다 (Windows에 tzdata 설치 불필요).
"""
from __future__ import annotations

import re
from datetime import date, datetime, time, timedelta, timezone

KST = timezone(timedelta(hours=9), "KST")
WEEKDAYS = ["월", "화", "수", "목", "금", "토", "일"]


def now_kst() -> datetime:
    return datetime.now(KST)


def to_epoch(dt: datetime) -> int:
    return int(dt.timestamp())


def from_epoch(ts: int) -> datetime:
    return datetime.fromtimestamp(ts, KST)


def week_start(dt: datetime, reset_weekday: int = 2, reset_hour: int = 6) -> datetime:
    """dt 가 속한 주간(리셋 ~ 다음 리셋)의 시작 시각."""
    dt = dt.astimezone(KST)
    days_back = (dt.weekday() - reset_weekday) % 7
    start = datetime.combine(dt.date() - timedelta(days=days_back), time(reset_hour), KST)
    if start > dt:
        start -= timedelta(days=7)
    return start


class ParseError(ValueError):
    pass


def parse_date(text: str, today: date) -> date:
    """'오늘', '내일', '모레', '금', '10/3', '10-03', '10.3', '2026-10-03' 를 받는다.

    요일만 주면 오늘 포함 가장 가까운 그 요일. 연도가 없으면 올해로 보되
    반년 넘게 지난 날짜면 내년으로 본다 (12월에 '1/5' 입력하는 경우).
    """
    s = text.strip().replace(" ", "")
    if s in ("오늘",):
        return today
    if s in ("내일",):
        return today + timedelta(days=1)
    if s in ("모레",):
        return today + timedelta(days=2)

    day = s.removesuffix("요일")
    if day in WEEKDAYS:
        return today + timedelta(days=(WEEKDAYS.index(day) - today.weekday()) % 7)

    m = re.fullmatch(r"(\d{4})[-./](\d{1,2})[-./](\d{1,2})", s)
    if m:
        y, mo, d = map(int, m.groups())
        return _make_date(y, mo, d, text)

    m = re.fullmatch(r"(\d{1,2})[-./](\d{1,2})", s) or re.fullmatch(r"(\d{1,2})월(\d{1,2})일?", s)
    if m:
        mo, d = map(int, m.groups())
        result = _make_date(today.year, mo, d, text)
        if (today - result).days > 183:
            result = _make_date(today.year + 1, mo, d, text)
        return result

    raise ParseError(f"날짜 '{text}' 를 이해하지 못했어요. 예: 10/3, 2026-10-03, 오늘, 내일, 금")


def _make_date(y: int, mo: int, d: int, original: str) -> date:
    try:
        return date(y, mo, d)
    except ValueError:
        raise ParseError(f"'{original}' 는 존재하지 않는 날짜예요.") from None


def parse_time(text: str) -> time:
    """'21:00', '21:30', '21', '2130', '21시', '21시30분', '오후 9시' 를 받는다."""
    s = text.strip().replace(" ", "")
    pm = am = False
    if s.startswith("오후"):
        pm, s = True, s[2:]
    elif s.startswith("오전"):
        am, s = True, s[2:]

    m = (
        re.fullmatch(r"(\d{1,2}):(\d{2})", s)
        or re.fullmatch(r"(\d{1,2})시(?:(\d{1,2})분?|반)?", s)
        or re.fullmatch(r"(\d{2})(\d{2})", s)
        or re.fullmatch(r"(\d{1,2})()", s)
    )
    if not m:
        raise ParseError(f"시각 '{text}' 를 이해하지 못했어요. 예: 21:00, 21시, 21시30분")
    hour = int(m.group(1))
    minute = 30 if s.endswith("반") else int(m.group(2) or 0)
    if pm and hour < 12:
        hour += 12
    if am and hour == 12:
        hour = 0
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        raise ParseError(f"시각 '{text}' 가 범위를 벗어났어요 (00:00~23:59).")
    return time(hour, minute)


def parse_hhmm(text: str) -> time:
    m = re.fullmatch(r"(\d{1,2}):(\d{2})", text.strip())
    if not m or not (0 <= int(m.group(1)) <= 23 and 0 <= int(m.group(2)) <= 59):
        raise ParseError(f"'{text}' 는 HH:MM 형식이 아니에요. 예: 09:00")
    return time(int(m.group(1)), int(m.group(2)))


OPEN_END = 26 * 60  # 가능 시간의 끝을 안 정하면 익일 02:00 까지로 본다


def default_end(start: int) -> int:
    """끝 시각 미정일 때 저장할 값. 새벽 시작이라 24시간을 넘기면 그날 자정까지."""
    return OPEN_END if OPEN_END <= start + 1440 else 24 * 60


def parse_time_range(text: str) -> tuple[int, int]:
    """'21-02', '21~', '20:30~24', '21시-2시' → (시작 분, 끝 분). 끝이 시작보다 이르면 익일."""
    s = text.strip().replace(" ", "")
    parts = re.split(r"[-~]", s, maxsplit=1)
    start_t = parse_time(parts[0])
    start = start_t.hour * 60 + start_t.minute
    if len(parts) == 1 or not parts[1]:
        return start, default_end(start)
    end_text = parts[1].removeprefix("익일")
    if end_text in ("24", "24:00", "24시"):
        end = 24 * 60
    else:
        end_t = parse_time(end_text)
        end = end_t.hour * 60 + end_t.minute
    if end <= start:
        end += 24 * 60
    if end - start > 24 * 60:
        raise ParseError(f"'{text}' 는 24시간을 넘어요.")
    return start, end


def fmt_minutes(total: int) -> str:
    """하루 시작 기준 분 → 'HH:MM'. 24시 이후는 '익일 HH:MM'."""
    h, m = divmod(total, 60)
    if h >= 24:
        return f"익일 {h - 24:02d}:{m:02d}"
    return f"{h:02d}:{m:02d}"


def fmt_range(start_min: int, end_min: int) -> str:
    return f"{fmt_minutes(start_min)}~{fmt_minutes(end_min)}"


def fmt_date(d: date) -> str:
    return f"{d.month}/{d.day}({WEEKDAYS[d.weekday()]})"


def fmt_dt(dt: datetime) -> str:
    return f"{fmt_date(dt.date())} {dt:%H:%M}"
