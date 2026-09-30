"""공대 일정 일괄 수정의 규칙 (디스코드와 무관해서 테스트할 수 있다).

권한: 서버 관리 권한자·공대장(leader=True)은 모든 일정, 그 외에는 내 캐릭터가 참가한 일정만.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timedelta

from .config import Config
from .db import Character, Database, Session
from .services import RuleError, SessionView, add_to_session, week_bounds
from .timeutil import KST, fmt_dt, from_epoch, parse_date, parse_time, to_epoch

MAX_LISTED = 25          # 드롭다운 한도
LOOKAHEAD_DAYS = 45

# /일정등록 에서 시각을 비웠을 때 메모에 붙인 표시. 시각을 정하면 자동으로 걷어 낸다
TIME_TBD_RE = re.compile(r"^⏱️ 시간 미정 \(임시 [^)]*\)( · )?")


@dataclass
class EditRequest:
    date_text: str = ""       # 비우면 날짜 유지
    time_text: str = ""       # 비우면 시각 유지
    shift_minutes: int = 0    # 날짜·시각을 적용한 뒤 이만큼 민다 (음수 가능)
    memo: str = ""            # 비우면 유지, '-' 면 삭제

    @property
    def empty(self) -> bool:
        return not (self.date_text.strip() or self.time_text.strip() or self.shift_minutes or self.memo.strip())


def parse_shift(text: str) -> int:
    """'+60', '-30', '90', '' → 분."""
    s = text.strip().replace(" ", "")
    if not s:
        return 0
    if not re.fullmatch(r"[+-]?\d{1,5}", s):
        raise RuleError(f"시간 밀기 '{text}' 는 분 단위 숫자로 적어 주세요. 예: +60, -30")
    return int(s)


def can_edit(chars: list[Character], member_id: int | None, leader: bool) -> bool:
    return leader or (member_id is not None and any(c.member_id == member_id for c in chars))


def editable_sessions(
    db: Database, cfg: Config, guild_id: int, now: datetime, member_id: int | None, leader: bool
) -> list[SessionView]:
    """이번 주(리셋 이후) 지난 일정과 앞으로 45일 안의 일정 중 고칠 수 있는 것, 시간순.
    지난 일정은 완료/미완료 표시용이다 (시각·메모는 시작 전 일정만 고칠 수 있다)."""
    since = week_bounds(cfg, now)[0]
    end = now + timedelta(days=LOOKAHEAD_DAYS)
    out = []
    for s in db.list_sessions(guild_id, to_epoch(since), to_epoch(end)):
        chars = db.session_characters(s.id)
        if can_edit(chars, member_id, leader):
            out.append(SessionView(s, cfg.raid(s.raid_key), chars))
    return out


def _load(db: Database, session_id: int, guild_id: int) -> Session:
    s = db.get_session(session_id)
    if s is None or s.guild_id != guild_id:
        raise RuleError(f"#{session_id} 일정을 찾을 수 없어요.")
    if s.status != "scheduled":
        raise RuleError(f"#{session_id} 은 취소된 일정이에요.")
    return s


def compute_new_start(old: datetime, req: EditRequest, now: datetime) -> datetime | None:
    """시각 변경이 없으면 None."""
    if not (req.date_text.strip() or req.time_text.strip() or req.shift_minutes):
        return None
    day = parse_date(req.date_text, now.date()) if req.date_text.strip() else old.date()
    at = parse_time(req.time_text) if req.time_text.strip() else old.timetz().replace(tzinfo=None)
    new = datetime.combine(day, at, KST) + timedelta(minutes=req.shift_minutes)
    if new <= now:
        raise RuleError(f"{fmt_dt(new)} 은 이미 지난 시각이에요.")
    return new


def apply_edit(
    db: Database, cfg: Config, guild_id: int, session_id: int, req: EditRequest,
    member_id: int | None, leader: bool, now: datetime,
) -> list[str]:
    """일정 하나에 수정을 적용하고 바뀐 내용 줄을 돌려준다. 권한/규칙 위반이면 RuleError."""
    if req.empty:
        raise RuleError("바꿀 항목(날짜/시각/밀기/메모)을 하나 이상 입력해 주세요.")
    s = _load(db, session_id, guild_id)
    if s.start_at <= to_epoch(now):
        raise RuleError(f"#{s.id} 은 이미 시작한 일정이라 고칠 수 없어요.")
    if not can_edit(db.session_characters(s.id), member_id, leader):
        raise RuleError(f"#{s.id} 은 내가 참가한 일정이 아니라 고칠 수 없어요. (공대장·서버 관리자는 모든 일정을 고칠 수 있어요)")

    changes = []
    old = from_epoch(s.start_at)
    new = compute_new_start(old, req, now)
    memo_text = req.memo.strip()
    if new is not None and new != old:
        db.update_session_time(s.id, to_epoch(new))
        changes.append(f"🕘 {fmt_dt(old)} → **{fmt_dt(new)}**")
        # 시각을 정했으니 '시간 미정' 표시는 걷어 낸다 (메모를 따로 고치지 않았을 때만)
        if not memo_text and s.memo and TIME_TBD_RE.match(s.memo):
            db.update_session_memo(s.id, TIME_TBD_RE.sub("", s.memo).strip() or None)
    if memo_text:
        memo = None if memo_text == "-" else memo_text[:100]
        db.update_session_memo(s.id, memo)
        changes.append(f"📝 메모: {memo or '(삭제)'}")
    return changes


def leave_mine(db: Database, cfg: Config, guild_id: int, session_id: int, member_id: int, now: datetime) -> str:
    s = _load(db, session_id, guild_id)
    if s.start_at <= to_epoch(now):
        raise RuleError(f"#{s.id} 은 이미 시작한 일정이에요.")
    mine = [c for c in db.session_characters(s.id) if c.member_id == member_id]
    if not mine:
        raise RuleError(f"#{s.id} 에는 내 캐릭터가 없어요.")
    for c in mine:
        db.remove_slot(s.id, c.id)
    return f"➖ {', '.join(c.name for c in mine)} 빠짐"


def swap_mine(
    db: Database, cfg: Config, guild_id: int, session_id: int, new_char: Character, now: datetime
) -> str:
    """내 캐릭터를 다른 캐릭터로 교체. 규칙(입장 레벨·주 1회·서폿 등)에 걸리면 원래대로 되돌린다."""
    s = _load(db, session_id, guild_id)
    if s.start_at <= to_epoch(now):
        raise RuleError(f"#{s.id} 은 이미 시작한 일정이에요.")
    mine = [c for c in db.session_characters(s.id) if c.member_id == new_char.member_id]
    if not mine:
        raise RuleError(f"#{s.id} 에는 내 캐릭터가 없어요.")
    if [c.id for c in mine] == [new_char.id]:
        raise RuleError(f"#{s.id} 에는 이미 {new_char.name} 으로 참가 중이에요.")
    for c in mine:
        db.remove_slot(s.id, c.id)
    try:
        add_to_session(db, cfg, s, new_char, now, strict=True)
    except RuleError:
        for c in mine:  # 되돌리기: 규칙을 다시 검사하지 않고 원래 파티 번호 그대로 복구
            db.add_slot(s.id, c.id, c.party_no)
        raise
    return f"🔁 {', '.join(c.name for c in mine)} → **{new_char.name}**"


def mark_cleared(
    db: Database, guild_id: int, session_id: int, value: int | None, member_id: int | None, leader: bool, now: datetime
) -> str:
    """완료(1) / 미완료(0) / 자동(None, 끝 시각 기준) 표시. 시작 전 일정은 완료·미완료를 표시할 수 없다."""
    s = _load(db, session_id, guild_id)
    if not can_edit(db.session_characters(s.id), member_id, leader):
        raise RuleError(f"#{s.id} 은 내가 참가한 일정이 아니라 표시할 수 없어요. (공대장·서버 관리자는 모든 일정 가능)")
    if value is not None and s.start_at > to_epoch(now):
        raise RuleError(f"#{s.id} 은 아직 시작 전이라 완료/미완료를 표시할 수 없어요.")
    db.set_cleared(s.id, value)
    return {1: "✅ 완료로 표시", 0: "❌ 미완료로 표시 (다시 잡아야 할 숙제로 돌아가요)", None: "↩️ 자동(끝 시각 기준)으로 되돌림"}[value]


def session_line(v: SessionView, now: datetime | None = None) -> str:
    name = v.raid.name if v.raid else v.session.raid_key
    if v.session.cleared == 0:
        icon = "❌ "
    elif v.session.cleared == 1 or (now and v.session.start_at <= to_epoch(now) and v.session.cleared is None
                                    and from_epoch(v.session.start_at) + timedelta(minutes=v.raid.duration_minutes if v.raid else 60) <= now):
        icon = "✅ "
    else:
        icon = ""
    return f"{icon}#{v.session.id} {fmt_dt(from_epoch(v.session.start_at))} {name} {v.filled}"
