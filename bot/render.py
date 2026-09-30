"""메시지 텍스트 생성. 디스코드 객체 없이 문자열만 만든다."""
from __future__ import annotations

import re
import unicodedata
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from itertools import groupby
from typing import TYPE_CHECKING
from urllib.parse import quote

from .classicons import char_head, class_icon, class_short, gold_mark, level_text, levelup_mark, role_icon
from .config import Config, Raid
from .db import Availability, Character, Session, Unavailable
from .services import (
    GoldSummary, Homework, Overview, SessionView, composition_warnings, gold_summary, hourly_counts,
    party_count,
)
from .timeutil import KST, WEEKDAYS, default_end, fmt_date, fmt_dt, fmt_minutes, fmt_range, from_epoch

if TYPE_CHECKING:
    from .roster import SyncResult


LOPEC_URL = "https://lopec.kr/character/specPoint/"


def lopec_url(name: str) -> str:
    return LOPEC_URL + quote(name)


def character_line(c: Character, with_owner: bool = False) -> str:
    owner = f" — {c.display_name}" if with_owner and c.display_name else ""
    return f"{char_head(c.cls, f'**{c.name}**')} {c.item_level:g}{owner}"


def gold(n: int) -> str:
    return f"{n:,}G"


def gold_summary_text(s: GoldSummary) -> str:
    """클골 합계와 더보기를 전부 했을 때 남는 골드."""
    return (
        f"{gold_mark()} 주간 클골 **{gold(s.gold)}** (거래 {gold(s.tradable)} · 귀속 {gold(s.bound)})\n"
        f"➕ 더보기 전부: **−{gold(s.more_cost)}** → 남는 골드 **{gold(s.after)}** "
        f"(거래 {gold(s.tradable_after)} · 귀속 {gold(s.bound_after)})\n"
        "-# 더보기 비용은 귀속 골드가 있는 레이드는 귀속에서, 없으면 거래 가능 골드에서 빠져요"
    )


def character_plan_text(chars: list[Character], plan: dict[int, list[Raid] | Homework]) -> str:
    """캐릭터별 숙제(구간, 레이드, 클골/더보기). chars 는 아이템 레벨 내림차순."""
    lines = []
    all_gold: list[Raid] = []
    for c in chars:
        hw = plan.get(c.id)
        raids = hw.gold if isinstance(hw, Homework) else (hw or [])
        extra = hw.extra if isinstance(hw, Homework) else []
        tier = ""
        if isinstance(hw, Homework) and hw.tier:
            tier = f" `{hw.tier}{'' if hw.explicit else ' 자동'}`"
        if raids:
            all_gold += raids
            s = gold_summary(raids)
            lines.append(f"{gold_mark()} {character_line(c)}{tier} · {gold(s.gold)} (더보기 −{s.more_cost / 1000:g}k)")
            names = [f"{r.name} {r.gold / 1000:g}k" for r in raids] + [f"{r.name} (골드X)" for r in extra]
            lines.append("-# 　" + " · ".join(names))
        else:
            lines.append(f"▫️ {character_line(c)}")
    if all_gold:
        lines.append("\n" + gold_summary_text(gold_summary(all_gold)))
    return "\n".join(lines)


def raid_table_text(cfg: Config, today: date | None = None) -> str:
    lines = []
    for r in sorted((r for r in cfg.raids.values() if not r.event), key=lambda r: (-r.min_item_level, -r.gold)):
        bound = f" (귀속 {r.bound_gold / 1000:g}k)" if r.bound_gold else ""
        mark = " ✏️" if r.overridden else ""
        more = f" · 더보기 −{r.more_cost / 1000:g}k → {(r.gold - r.more_cost) / 1000:g}k" if r.more_cost else ""
        lines.append(
            f"`{r.min_item_level:g}` **{r.name}** {r.party_size}인 · {gold(r.gold)}{bound}{more}{mark}"
        )
    if cfg.tiers:
        lines.append("\n**📋 숙제 구간** (`/숙제구간` 으로 캐릭터별 지정, ⭐ = 레벨로 자동 배정)")
        for t in sorted(cfg.tiers.values(), key=lambda t: (-t.min_level, t.label)):
            raids = " · ".join(cfg.raids[k].name for k in t.raids)
            extra = f" + {cfg.raids[t.no_gold].name}(골드X)" if t.no_gold else ""
            s = gold_summary([cfg.raids[k] for k in t.raids])
            lines.append(f"{'⭐' if t.auto else '　'}`{t.label}` {raids}{extra} · {s.gold / 1000:g}k")
    events = [r for r in cfg.raids.values() if r.event and (today is None or r.is_active(today))]
    if events:
        lines.append("\n**🎉 진행 중인 이벤트 레이드**")
        for r in sorted(events, key=lambda r: (r.ends_on or date.max, r.name)):
            until = f" · ~{fmt_date(r.ends_on)}까지" if r.ends_on else ""
            lines.append(f"`{r.min_item_level:g}` **{r.name}** {r.party_size}인{until}")
    return "\n".join(lines)


def session_title(cfg: Config, s: Session) -> str:
    raid = cfg.raid(s.raid_key)
    name = raid.name if raid else s.raid_key
    return f"#{s.id} {fmt_dt(from_epoch(s.start_at))} {name}"


def session_label(v: SessionView) -> str:
    """자동완성 등 한 줄 요약용 (100자 제한)."""
    name = v.raid.name if v.raid else v.session.raid_key
    return f"#{v.session.id} {fmt_dt(from_epoch(v.session.start_at))} {name} ({v.filled})"[:100]


def participants_block(chars: list[Character], mention: bool = False) -> str:
    if not chars:
        return "_아직 참가자 없음_"
    lines = []
    for c in chars:
        who = f"<@{c.discord_id}>" if mention and c.discord_id else (c.display_name or "?")
        if class_icon(c.cls):
            lines.append(f"• {who} — {char_head(c.cls, c.name)} ({c.item_level:g})")
        else:
            lines.append(f"• {who} — {c.name} ({c.cls} {c.item_level:g})")
    return "\n".join(lines)


PARTY_MEMO_RE = re.compile(r"파티 \d+")


def _tail_lines(cfg: Config, ov: Overview) -> list[str]:
    """일정 아래에 붙는 '골드 레이드 남은 캐릭터'와 '일정 없는 공대원' (하루 보기에서는 없음)."""
    lines: list[str] = []
    if ov.single_day:
        return lines
    if ov.unassigned_by_raid:
        lines.append("### 골드 레이드 남은 캐릭터")
        limit = 30 if len(ov.unassigned_by_raid) == 1 else 6
        for key, chars in ov.unassigned_by_raid.items():
            raid = cfg.raids[key]
            names = ", ".join(_member_label(c) for c in chars[:limit])
            more = f" 외 {len(chars) - limit}" if len(chars) > limit else ""
            lines.append(f"• {raid.name} **{len(chars)}**: {names}{more}")
    if ov.idle_members:
        lines.append("### 이번 주 일정 없는 공대원")
        lines.append(", ".join(ov.idle_members))
    return lines


def overview_text(cfg: Config, ov: Overview, now: datetime | None = None) -> str:
    """일정별 보기. ✅ 끝난 일정(취소선) · ❌ 미완료. 예정 일정은 상태 아이콘 없이 표시한다."""
    now = now or datetime.now(KST)
    lines: list[str] = []
    if not ov.sessions:
        lines.append("_등록된 일정이 없어요. 공대장이 `/일정등록` 으로 추가할 수 있어요._")

    by_day = groupby(ov.sessions, key=lambda v: from_epoch(v.session.start_at).date())
    for day, views in by_day:
        lines.append(f"### {fmt_date(day)}")
        for v in views:
            s = v.session
            name = v.raid.name if v.raid else s.raid_key
            # 파티 일정의 메모는 "파티 N" 이 대부분이라 생략한다
            memo = f" · {s.memo}" if s.memo and not (s.id in ov.party_of and PARTY_MEMO_RE.fullmatch(s.memo)) else ""
            if session_failed(v):
                lines.append(f"❌ ~~{from_epoch(s.start_at):%H:%M} {name}~~ `{v.filled}` #{s.id} **미완료**")
                continue
            if session_done(v, now):
                lines.append(f"✅ ~~{from_epoch(s.start_at):%H:%M} {name}~~ `{v.filled}` #{s.id}")
                continue
            lines.append(f"**{from_epoch(s.start_at):%H:%M} {name}** `{v.filled}` #{s.id}{memo}")
            lines += party_lines(cfg, v)

    lines += _tail_lines(cfg, ov)

    text = truncate("\n".join(lines), 4000)
    return text + "\n```" if text.count("```") % 2 else text


def session_failed(v: SessionView) -> bool:
    """수동으로 '미완료(못 깸)' 표시한 일정. 그 레이드는 다시 잡아야 하는 숙제로 돌아간다."""
    return v.session.cleared == 0


def session_done(v: SessionView, now: datetime) -> bool:
    """완료 여부. 수동 표시(/일정완료)가 있으면 그 값, 없으면 일정 끝 시각이 지났을 때 완료로 본다."""
    if v.session.cleared is not None:
        return bool(v.session.cleared)
    minutes = v.raid.duration_minutes if v.raid else 60
    return from_epoch(v.session.start_at) + timedelta(minutes=minutes) <= now


def member_view_chunks(
    ov: Overview, viewer_id: int | None = None, limit: int = 3900, now: datetime | None = None,
    cfg: Config | None = None,
) -> list[str]:
    """공대원별 → 캐릭터별 보기. 임베드 한도에 맞춰 여러 덩어리로 나눈다. 보는 사람 묶음이 맨 위.

    끝난 일정은 ✅ + 취소선, 예정은 🗓️. 아직 안 잡힌 숙제는 개수만 (캐릭터 줄 끝 '미정 N').
    """
    now = now or datetime.now(KST)
    day_view = ov.single_day
    members = sorted(ov.members, key=lambda m: m.discord_id != viewer_id)  # 안정 정렬: 나머지는 이름순 유지
    if not members:
        empty = "_이 날 레이드가 있는 캐릭터가 없어요._" if day_view else "_이번 주 일정도, 남은 골드 레이드도 없어요._"
        return [empty]

    blocks = []
    for m in members:
        me = " (나)" if m.discord_id == viewer_id else ""
        done = sum(1 for row in m.characters for v in row.sessions if session_done(v, now))
        failed = sum(1 for row in m.characters for v in row.sessions if session_failed(v))
        planned = sum(1 for row in m.characters for v in row.sessions) - done - failed
        left = sum(len(row.remaining) for row in m.characters)
        count = "" if day_view else f" · ✅ {done} · 🗓️ {planned} · 미정 {left}" + (f" · ❌ {failed}" if failed else "")
        lines = [f"### {m.name}{me}{count}"]
        for row in m.characters:
            c = row.character
            pending = f" · 미정 {len(row.remaining)}" if row.remaining and not day_view else ""
            # 캐릭터 이름을 누르면 로펙 페이지가 열린다
            lines.append(f"{char_head(c.cls, f'**[{c.name}]({lopec_url(c.name)})**')} {c.item_level:g}{pending}")
            for v in row.sessions:
                name = v.raid.name if v.raid else v.session.raid_key
                when = f"{from_epoch(v.session.start_at):%H:%M}" if day_view else fmt_dt(from_epoch(v.session.start_at))
                if session_failed(v):
                    lines.append(f"　❌ ~~{when} {name}~~ 미완료")
                elif session_done(v, now):
                    lines.append(f"　✅ ~~{when} {name}~~")
                else:
                    mine = next((x for x in v.characters if x.id == c.id), None)
                    which = f" · {mine.party_no}파티" if mine and mine.party_no and party_count(v.raid) > 1 else ""
                    warn = " ⚠️" if cfg and composition_warnings(cfg, v.raid, v.characters) else ""
                    lines.append(f"　🗓️ {when} {name} `{v.filled}` #{v.session.id}{which}{warn}")
        blocks.append("\n".join(lines))

    chunks, current = [], ""
    for block in blocks:
        block = truncate(block, limit)
        if current and len(current) + len(block) + 1 > limit:
            chunks.append(current)
            current = ""
        current = f"{current}\n{block}" if current else block
    chunks.append(current)
    return chunks


def _member_label(c: Character) -> str:
    """'공대원(아이콘 캐릭터)'. 서폿/딜러는 아이콘 색(초록/빨강)으로 구분한다."""
    name = f"{class_icon(c.cls)} {c.name}" if class_icon(c.cls) else c.name
    return f"{c.display_name}({name})"


# 파티 표(ANSI 코드블록). 코드블록 안에는 커스텀 이모지를 못 쓰므로 직업은 두 글자 약칭으로 적고,
# 아이템 레벨만 색을 입힌다 (딜러 빨강 / 서폿 초록). 색은 PC·웹 디스코드에서만 보인다.
COL_WIDTH = 27       # 한 열의 폭(칸). 한글·전각은 2칸
COL_GAP = " | "
LABEL_WIDTH = 4      # 직업 약칭 (두 글자 = 4칸)
MEMBER_MAX = 6       # 괄호 안 공대원 이름 최대 폭
ANSI_RESET, ANSI_BOLD = "\u001b[0m", "\u001b[1m"
ANSI_DEALER, ANSI_SUPPORT = "\u001b[31m", "\u001b[32m"


def _width(text: str) -> int:
    return sum(2 if unicodedata.east_asian_width(ch) in "WF" else 1 for ch in text)


def _pad(text: str, width: int) -> str:
    return text + " " * max(0, width - _width(text))


def _fit(text: str, limit: int) -> str:
    """표시 폭이 limit 칸을 넘으면 …로 줄인다."""
    if _width(text) <= limit:
        return text
    out, used = "", 0
    for ch in text:
        w = _width(ch)
        if used + w > limit - 1:
            break
        out, used = out + ch, used + w
    return out + "…"


def _party_cell(cfg: Config, c: Character, level_width: int) -> str:
    """'브커 풍월문(이동현) 1784' 한 칸 (폭 COL_WIDTH 고정). 길면 이름을 …로 줄여 열이 밀리지 않게 한다."""
    name_width = COL_WIDTH - LABEL_WIDTH - 1 - 1 - level_width
    member = _fit(c.display_name or "?", MEMBER_MAX)
    char = _fit(c.name, max(2, name_width - _width(member) - 2))
    level = f"{c.item_level:g}".rjust(level_width)
    color = ANSI_SUPPORT if cfg.is_support(c.cls) else ANSI_DEALER
    label = _pad(class_short(c.cls), LABEL_WIDTH)
    return f"{label} {_pad(f'{char}({member})', name_width)} {color}{level}{ANSI_RESET}"


def _party_header(text: str) -> str:
    return f"{ANSI_BOLD}{_pad(text, COL_WIDTH)}{ANSI_RESET}"


def _party_rows(cells: list[list[str]], headers: list[str]) -> list[str]:
    """열(파티)들을 나란히 놓는다. 빈 칸은 공백으로 채워 오른쪽 열 위치를 맞춘다."""
    rows = [COL_GAP.join(_party_header(h) for h in headers).rstrip()]
    blank = " " * COL_WIDTH
    for i in range(max(len(col) for col in cells)):
        row = COL_GAP.join(col[i] if i < len(col) else blank for col in cells)
        rows.append(row.rstrip())
    return rows


# ── 일정별 보기(임베드 카드) ─────────────────────────────────────────────────────
# 일정 하나 = 임베드 하나. 제목은 일정 정보, 필드는 1파티(왼쪽)·2파티(오른쪽). 필드 한 칸이 좁아서
# 멤버마다 두 줄로 쓴다: ① 직업 아이콘 캐릭명(공대원)  ② 역할 아이콘 + 색 숫자 레벨.
ZWSP = "\u200b"           # 디스코드 필드는 이름·값이 비면 안 돼서 빈 칸용으로 쓴다
FIELD_TEXT_CELLS = 16    # 첫 줄 '캐릭명(공대원)' 최대 폭(칸). 한글 8글자쯤
MEMBER_CELLS = 6         # 그중 괄호 안 공대원 이름 최대 폭
EMBED_CHARS_PER_MESSAGE = 5800   # 디스코드 한도는 6000자 (임베드 글자 합계)
EMBEDS_PER_MESSAGE = 10


@dataclass(frozen=True)
class SessionCard:
    title: str
    description: str
    fields: tuple[tuple[str, str, bool], ...]  # (이름, 값, 나란히 놓기)


@dataclass(frozen=True)
class OverviewCards:
    notice: str                       # 일정이 하나도 없을 때 안내
    past: str                         # 끝난·미완료 일정 요약 (없으면 "")
    sessions: tuple[SessionCard, ...]  # 앞으로 할 일정
    tail: str                         # 골드 레이드 남은 캐릭터 · 일정 없는 공대원 (없으면 "")


def _member_head(c: Character) -> str:
    """'직업아이콘 캐릭명(공대원)'. 이름이 길면 …로 줄여 두 열의 모양이 같게 한다. 아이콘이 없으면 직업 약칭."""
    member = _fit(c.display_name or "?", MEMBER_CELLS)
    char = _fit(c.name, max(4, FIELD_TEXT_CELLS - _width(member) - 2))
    return f"{class_icon(c.cls) or class_short(c.cls)} {char}({member})"


def _member_level(cfg: Config, c: Character) -> str:
    support = cfg.is_support(c.cls)
    return f"{role_icon(support)}{level_text(c.item_level, support)}"


def _column_value(cfg: Config, chars: list[Character]) -> str:
    """좁은 칸용: 멤버마다 두 줄."""
    if not chars:
        return ZWSP
    value = "\n".join(f"{_member_head(c)}\n{_member_level(cfg, c)}" for c in chars)
    return truncate(value, 1024)


def _wide_value(cfg: Config, chars: list[Character]) -> str:
    """넓은 칸(한 줄 전체)용: 멤버마다 한 줄."""
    return truncate("\n".join(f"{_member_head(c)} {_member_level(cfg, c)}" for c in chars), 1024)


def _party_fields(cfg: Config, v: SessionView) -> list[tuple[str, str, bool]]:
    n = party_count(v.raid)
    if n == 1:
        return [("참가자", _wide_value(cfg, v.characters), False)]
    fields: list[tuple[str, str, bool]] = []
    for first in range(1, n + 1, 2):
        pair = [p for p in (first, first + 1) if p <= n]
        members = {p: [c for c in v.characters if c.party_no == p] for p in pair}
        if not any(members.values()):
            continue
        fields += [(f"{p}파티", _column_value(cfg, members[p]), True) for p in pair]
        if len(pair) == 2:
            fields.append((ZWSP, ZWSP, True))  # 한 줄에 3칸까지라 빈 칸으로 다음 파티를 아래 줄로 보낸다
    rest = [c for c in v.characters if not c.party_no or c.party_no > n]
    if rest:
        fields.append(("파티 미정", _wide_value(cfg, rest), False))
    return fields


def _session_title(ov: Overview, v: SessionView) -> str:
    s = v.session
    name = v.raid.name if v.raid else s.raid_key
    return f"{fmt_dt(from_epoch(s.start_at))} {name} · {v.filled} · #{s.id}"


def _session_notes(cfg: Config, ov: Overview, v: SessionView) -> str:
    s = v.session
    notes = []
    # 파티 일정의 메모는 "파티 N" 이 대부분이라 생략한다
    if s.memo and not (s.id in ov.party_of and PARTY_MEMO_RE.fullmatch(s.memo)):
        notes.append(s.memo)
    notes += [f"⚠️ {w}" for w in composition_warnings(cfg, v.raid, v.characters)]
    return "\n".join(notes)


def overview_cards(cfg: Config, ov: Overview, now: datetime | None = None) -> OverviewCards:
    """일정별 보기를 임베드로 만들 재료. 디스코드 객체 없이 문자열만 돌려준다."""
    now = now or datetime.now(KST)
    past, upcoming = [], []
    for v in ov.sessions:
        s = v.session
        name = v.raid.name if v.raid else s.raid_key
        when = fmt_dt(from_epoch(s.start_at))
        if session_failed(v):
            past.append(f"❌ ~~{when} {name}~~ `{v.filled}` #{s.id} **미완료**")
        elif session_done(v, now):
            past.append(f"✅ ~~{when} {name}~~ `{v.filled}` #{s.id}")
        else:
            upcoming.append(SessionCard(
                title=_session_title(ov, v),
                description=_session_notes(cfg, ov, v),
                fields=tuple(_party_fields(cfg, v)) if v.characters else (),
            ))
    notice = "" if ov.sessions else "_등록된 일정이 없어요. 공대장이 `/일정등록` 으로 추가할 수 있어요._"
    return OverviewCards(notice, "\n".join(past), tuple(upcoming), "\n".join(_tail_lines(cfg, ov)))


def pack_by_limits(sizes: list[int], max_chars: int = EMBED_CHARS_PER_MESSAGE, max_items: int = EMBEDS_PER_MESSAGE) -> list[list[int]]:
    """임베드를 메시지에 나눠 담는다 (메시지당 글자 합계·개수 한도). 순서를 지키고, 혼자 한도를 넘는 것은 단독 메시지."""
    groups: list[list[int]] = []
    used = 0
    for i, size in enumerate(sizes):
        if groups and used + size <= max_chars and len(groups[-1]) < max_items:
            groups[-1].append(i)
            used += size
        else:
            groups.append([i])
            used = size
    return groups


def party_lines(cfg: Config, v: SessionView) -> list[str]:
    """참가자를 파티(4명)별로. 8인 이상이면 1파티는 왼쪽, 2파티는 오른쪽 (3·4파티는 그 아래 줄). 서폿 구성 경고를 붙인다."""
    if not v.characters:
        return []
    level_width = max(len(f"{c.item_level:g}") for c in v.characters)
    cell = lambda c: _party_cell(cfg, c, level_width)  # noqa: E731
    n = party_count(v.raid)
    rows: list[str] = []
    if n == 1:
        rows = [cell(c) for c in v.characters]
    else:
        parties = {p: [cell(c) for c in v.characters if c.party_no == p] for p in range(1, n + 1)}
        for p in range(1, n + 1, 2):
            group = [p, p + 1] if p + 1 <= n else [p]
            if any(parties[q] for q in group):
                rows += _party_rows([parties[q] for q in group], [f"{q}파티" for q in group])
        rest = [cell(c) for c in v.characters if not c.party_no or c.party_no > n]
        if rest:
            rows += _party_rows([rest], ["파티 미정"])
    out = ["```ansi", *rows, "```"]
    for w in composition_warnings(cfg, v.raid, v.characters):
        out.append(f"-# ⚠️ {w}")
    return out


def availability_text(slots: list[Availability], week_start: datetime) -> str:
    """이번 주(리셋~리셋) 날짜별로 내 가능 시간을 코드블록으로. 날짜는 주가 바뀌면 같이 바뀌고, 시간대만 색을 입힌다."""
    by_day = {a.weekday: a for a in slots}
    rows = []
    for i in range(7):
        d = (week_start + timedelta(days=i)).date()
        a = by_day.get(d.weekday())
        label = _pad(f"{fmt_date(d)}:", 9)
        rows.append(f"{label} {ANSI_TIME}{fmt_range(a.start_min, a.end_min)}{ANSI_RESET}" if a else label.rstrip())
    return "```ansi\n" + "\n".join(rows) + "\n```"


def _short_time(total: int) -> str:
    h, m = divmod(total % 1440, 60)
    return f"{h:02d}" if m == 0 else f"{h:02d}:{m:02d}"


def _blocked_on(day: date, blocked: list[Unavailable]) -> list[tuple[int, str]]:
    """그 날짜에 걸치는 불가 예외 → (member_id, '하루 종일' 또는 '20~24')."""
    day0 = datetime.combine(day, datetime.min.time(), KST)
    day1 = day0 + timedelta(days=1)
    out = []
    for u in blocked:
        start, end = max(from_epoch(u.start_at), day0), min(from_epoch(u.end_at), day1)
        if start >= end:
            continue
        if (start, end) == (day0, day1):
            out.append((u.member_id, "하루 종일"))
        else:
            hours = lambda dt: _short_time(int((dt - day0).total_seconds() // 60)) if dt != day1 else "24"
            out.append((u.member_id, f"{hours(start)}~{hours(end)}"))
    return out


def weekly_availability_text(
    avail: list[Availability], names: dict[int, str], week_start: datetime, only_weekday: int | None = None,
    blocked: list[Unavailable] | None = None,
) -> str:
    """주간(리셋일부터 7일) 날짜별로 누가 몇 시에 되는지. 21~02 처럼 끝이 작으면 익일."""
    by_day: dict[int, list[Availability]] = defaultdict(list)
    for a in avail:
        by_day[a.weekday].append(a)
    lines = []
    for i in range(7):
        d = (week_start + timedelta(days=i)).date()
        wd = d.weekday()
        if only_weekday is not None and wd != only_weekday:
            continue
        slots = sorted(by_day[wd], key=lambda a: (a.start_min, a.end_min, names.get(a.member_id, "")))
        lines.append(f"**{fmt_date(d)}** · {len(slots)}명")
        if slots:
            lines.append("　" + " · ".join(
                f"{names.get(a.member_id, '?')} `{_short_time(a.start_min)}~{_short_time(a.end_min)}`" for a in slots
            ))
        else:
            lines.append("-# 　가능한 사람 없음")
        off = _blocked_on(d, blocked or [])
        if off:
            lines.append("　🚫 " + " · ".join(f"{names.get(m, '?')} `{t}`" for m, t in sorted(off, key=lambda x: names.get(x[0], ''))))
    registered = {a.member_id for a in avail}
    missing = sorted(n for m, n in names.items() if m not in registered)
    if missing:
        lines.append(f"\n-# 가능 시간 미등록: {', '.join(missing)}")
    return truncate("\n".join(lines), 4000)


LIST_WIDTH = 56  # 날짜 목록 코드블록의 한 줄 폭(칸). PC 임베드 한 줄에 들어가는 최대치에 가깝게 잡았다. 넘으면 다음 줄로 넘기고 첫 이름 자리에 맞춘다


def _wrap_names(label: str, items: list[tuple[str, str]]) -> list[str]:
    """'10/3(토): 이름, 이름,' 을 LIST_WIDTH 안에서 줄바꿈한다. items = (색 없는 이름, 색 입힌 이름).
    이어지는 줄은 날짜 뒤 첫 이름과 같은 칸에서 시작한다."""
    indent = " " * (_width(label) + 1)
    lines: list[str] = []
    current, current_width = "", 0
    for i, (plain, painted) in enumerate(items):
        comma = "," if i < len(items) - 1 else ""
        piece_width = _width(plain) + len(comma)
        gap = 1 if current else 0
        if current and len(indent) + current_width + gap + piece_width > LIST_WIDTH:
            lines.append((label + " " if not lines else indent) + current)
            current, current_width, gap = "", 0, 0
        current += (" " if gap else "") + painted + comma
        current_width += gap + piece_width
    if not items:
        return [label.rstrip()]
    lines.append((label + " " if not lines else indent) + current)
    return lines


def _member_colors(names: dict[int, str]) -> dict[int, str]:
    """공대원(member id)마다 고정 색. 번호순으로 NAME_COLORS 를 돌아가며 정한다."""
    return {m: NAME_COLORS[i % len(NAME_COLORS)] for i, m in enumerate(sorted(names))}


def team_characters_text(
    chars_by_day: dict[date, list[Character]], names: dict[int, str], week_start: datetime,
) -> str:
    """/공대시간대 이름:캐릭터 — 이번 주 날짜별로 그날 레이드 일정에 들어간 캐릭터 이름을 코드블록으로.
    색은 공대원(원정대)마다 같아서, 같은 사람의 캐릭터는 같은 색이다."""
    color_of = _member_colors(names)
    rows = []
    for i in range(7):
        d = (week_start + timedelta(days=i)).date()
        chars = sorted(chars_by_day.get(d, []), key=lambda c: (names.get(c.member_id, ""), c.name))
        label = _pad(f"{fmt_date(d)}:", 10)
        items = [(c.name, color_of.get(c.member_id, "") + c.name + ANSI_RESET) for c in chars]
        rows += _wrap_names(label, items)
    return "```ansi\n" + "\n".join(rows) + "\n```"


def team_availability_text(
    avail: list[Availability], names: dict[int, str], week_start: datetime, blocked: list[Unavailable] | None = None,
) -> str:
    """/공대시간대: 이번 주(리셋~리셋) 날짜별로 가능한 공대원 이름을 코드블록으로. 이름마다 고정 색.

    '10/1(목): 이동현, 더미1'. 색은 공대원(번호순)마다 NAME_COLORS 를 돌아가며 정해서 어느 날짜에서나 같다.
    /불가 예외는 날짜 아래 🚫 줄로 붙인다."""
    color_of = _member_colors(names)

    def paint(member_id: int) -> str:
        return f"{color_of.get(member_id, '')}{names.get(member_id, '?')}{ANSI_RESET}"

    by_day: dict[int, list[Availability]] = defaultdict(list)
    for a in avail:
        by_day[a.weekday].append(a)
    rows = []
    for i in range(7):
        d = (week_start + timedelta(days=i)).date()
        slots = sorted(by_day[d.weekday()], key=lambda a: names.get(a.member_id, ""))
        label = _pad(f"{fmt_date(d)}:", 10)
        rows += _wrap_names(label, [(names.get(a.member_id, "?"), paint(a.member_id)) for a in slots])
        off = _blocked_on(d, blocked or [])
        if off:
            rows.append(_pad("", 10) + " 🚫 " + ", ".join(f"{paint(m)}({t})" for m, t in sorted(off, key=lambda x: names.get(x[0], ""))))
    text = "```ansi\n" + "\n".join(rows) + "\n```"
    registered = {a.member_id for a in avail}
    missing = sorted(n for m, n in names.items() if m not in registered)
    if missing:
        text += f"\n-# 가능 시간 미등록: {', '.join(missing)}"
    return text


ANSI_TIME = "\u001b[1;32m"  # 내 가능 시간대 표시 색 (굵은 초록)
# 공대원 이름 색: 빨강·초록·노랑·파랑·분홍·청록·흰색 (디스코드 ansi 에서 쓸 수 있는 색). 8명째부터는 색이 다시 돌아온다
NAME_COLORS = [f"\u001b[{n}m" for n in (31, 32, 33, 34, 35, 36, 37)]
ANSI_GRAY, ANSI_WHITE = "\u001b[30m", "\u001b[1;37m"  # 굵게: 디스코드의 '흰색'이 기본 글자색과 비슷해 보여서 더 또렷하게
ANSI_ORANGE, ANSI_PURPLE, ANSI_BLUE, ANSI_CYAN = "\u001b[33m", "\u001b[35m", "\u001b[34m", "\u001b[36m"  # 디스코드 ansi 에는 주황이 없어 노랑(갈색빛 주황)으로 쓴다
# 난이도 색: 노말 파랑 · 하드 주황 · 나이트메어 보라 · 익스트림 청록. 지평의 성당 1·2·3단계는 노말·하드·나이트메어와 같은 색
DIFFICULTY_COLORS = {
    "노말": ANSI_BLUE, "하드": ANSI_ORANGE, "나이트메어": ANSI_PURPLE, "익스트림": ANSI_CYAN,
    "1단계": ANSI_BLUE, "2단계": ANSI_ORANGE, "3단계": ANSI_PURPLE,
}


def _my_char_head(cfg: Config, c: Character) -> str:
    """'직업아이콘 캐릭명 아이템레벨 딜러/서폿아이콘' (코드블록 밖이라 아이콘이 보인다. 글자 색은 없음)."""
    return f"{char_head(c.cls, c.name)} {c.item_level:g} {role_icon(cfg.is_support(c.cls))}"


def _raid_ansi(name: str) -> str:
    """레이드 이름은 흰색, 끝의 난이도(노말·하드·나이트메어…)만 난이도 색으로."""
    base, _, last = name.rpartition(" ")
    color = DIFFICULTY_COLORS.get(last)
    if not base or color is None:
        return f"{ANSI_WHITE}{name}{ANSI_RESET}"
    return f"{ANSI_WHITE}{base}{ANSI_RESET} {color}{last}{ANSI_RESET}"


def _my_raid_line(name: str, when: str = "", level: float | None = None, mark: str = "") -> str:
    """코드블록 안의 레이드 줄. 난이도만 색이 있고 나머지(시각·입장 레벨 포함)는 흰색."""
    detail = " · ".join(x for x in (when, f"{level:g}" if level else "") if x)
    tail = f" {ANSI_WHITE}· {detail}{ANSI_RESET}" if detail else ""
    return f"{mark}{_raid_ansi(name)}{tail}"


def _my_fields(cfg: Config, title: str, entries: list[tuple[Character, list[str]]]) -> list[tuple[str, str, bool]]:
    """캐릭터마다 필드 하나(값 = 아이콘·이름·레벨 + 색 코드블록)를 두 칸씩 나란히 놓는다.

    날짜(title)는 첫 필드의 이름으로 합쳐서 날짜와 캐릭터 사이에 빈 줄이 생기지 않게 한다."""
    fields: list[tuple[str, str, bool]] = []
    for i, (c, raid_lines) in enumerate(entries):
        value = f"{_my_char_head(cfg, c)}\n```ansi\n" + "\n".join(raid_lines) + "\n```"
        fields.append((title if i == 0 else ZWSP, value, True))
        if i % 2 == 1:
            fields.append((ZWSP, ZWSP, True))  # 한 줄에 3칸까지라 빈 칸으로 다음 캐릭터를 아래 줄로 보낸다
    if len(entries) % 2 == 1:
        # 마지막 줄이 캐릭터 1명뿐이면 빈 칸 둘로 줄을 채운다. 안 채우면 다음 날짜 필드가 같은 줄 오른쪽으로 올라온다
        fields += [(ZWSP, ZWSP, True)] * 2
    return fields


def _session_line(v: SessionView, now: datetime) -> str:
    """코드블록 안의 레이드 한 줄. 끝난 일정은 [완료], 미완료 표시는 [미완료]."""
    name = v.raid.name if v.raid else v.session.raid_key
    if session_failed(v):
        return _my_raid_line(name, mark=f"{ANSI_DEALER}[미완료]{ANSI_RESET} ")
    if session_done(v, now):
        return _my_raid_line(name, mark=f"{ANSI_GRAY}[완료]{ANSI_RESET} ")
    return _my_raid_line(name, level=v.raid.min_item_level if v.raid else None)


def chunk_groups(
    groups: list[list[tuple[str, str, bool]]], max_fields: int = 24, max_chars: int = 4500
) -> list[list[tuple[str, str, bool]]]:
    """일정 묶음(필드 여러 개)을 임베드에 담는다. 임베드 하나에 필드 25개·글자 6000자 한도가 있어서,
    한 묶음은 쪼개지 않고 통째로 다음 임베드로 넘긴다 (묶음 하나가 한도를 넘으면 혼자 한 임베드)."""
    chunks: list[list[tuple[str, str, bool]]] = []
    used = 0
    for group in groups:
        size = sum(len(n) + len(v) for n, v, _ in group)
        if chunks and len(chunks[-1]) + len(group) <= max_fields and used + size <= max_chars:
            chunks[-1] += group
            used += size
        else:
            chunks.append(list(group))
            used = size
    return chunks


def _header_line(v: SessionView, now: datetime) -> str:
    """레이드 헤더 코드블록의 한 줄: '벨가르딘 나이트메어 1780' (난이도만 색). 끝난 일정은 [완료], 미완료 표시는 [미완료]."""
    name = v.raid.name if v.raid else v.session.raid_key
    level = f" {ANSI_WHITE}{v.raid.min_item_level:g}{ANSI_RESET}" if v.raid else ""
    mark = ""
    if session_failed(v):
        mark = f"{ANSI_DEALER}[미완료]{ANSI_RESET} "
    elif session_done(v, now):
        mark = f"{ANSI_GRAY}[완료]{ANSI_RESET} "
    return f"{mark}{_raid_ansi(name)}{level}"


def _plain_head(c: Character) -> str:
    """'직업아이콘 캐릭명' (공대원 이름은 넣지 않는다). 아이콘이 없으면 직업 약칭."""
    return f"{class_icon(c.cls) or class_short(c.cls)} {c.name}"   # 이름은 줄여 쓰지 않는다


def _level_plain(cfg: Config, c: Character) -> str:
    """'소수점까지 일반 숫자 + 딜러/서폿 아이콘(오른쪽)'. 숫자에는 색을 넣지 않는다."""
    return f"{c.item_level:g} {role_icon(cfg.is_support(c.cls))}"


def _session_groups(views: list[SessionView], party_of: dict[int, int] | None = None) -> list[list[SessionView]]:
    """같은 날 일정을 묶는다. 같은 고정 파티의 일정은 8인·4인 구분 없이 한 묶음이다.
    파티 연결이 없는 일정은, 4인 레이드를 같은 파티원이 함께 가면 한 묶음이고 그 밖에는 일정마다 한 묶음이다."""
    party_of = party_of or {}
    groups: list[list[SessionView]] = []
    by_party: dict[int, list[SessionView]] = {}
    by_members: dict[frozenset[int], list[SessionView]] = {}
    for v in views:
        number = party_of.get(v.session.id)
        if number:
            if number in by_party:
                by_party[number].append(v)
                continue
            by_party[number] = group = [v]
        elif party_count(v.raid) == 1 and v.characters:
            key = frozenset(c.id for c in v.characters)
            if key in by_members:
                by_members[key].append(v)
                continue
            by_members[key] = group = [v]
        else:
            group = [v]
        groups.append(group)
    return groups


MEMBER_LINE_PREFIX = "### "  # 캐릭터 줄을 키우는 제목 서식 (디스코드에서 글자 크기를 바꾸는 유일한 방법). 글자가 그대로 보이면 ""로 끈다


def _member_line(cfg: Config, c: Character) -> str:
    """'직업아이콘 캐릭명 1788.33 딜러/서폿아이콘' — 이름 오른쪽에 일반 숫자 레벨, 그 오른쪽에 역할 아이콘. 줄 크기를 키운다."""
    return f"{MEMBER_LINE_PREFIX}{_plain_head(c)} {_level_plain(cfg, c)}"


BLANK_CELL = (ZWSP, ZWSP, True)  # 한 줄(칸 3개)에서 2개만 쓰고 줄을 끝내기 위한 빈 칸


def _party_columns(cfg: Config, group: list[SessionView]) -> tuple[int, list[tuple[str, list[Character]]]]:
    """묶음의 파티원을 파티별로. 가장 큰(파티가 많은) 레이드의 파티 배정을 따르고, 그 레이드에 없는 캐릭터는 자기 일정의 배정을 따른다.
    각 파티에서 서폿은 맨 아래로 보낸다. (파티 수, [(칸 이름, 캐릭터들)])"""
    main = max(group, key=lambda v: party_count(v.raid))
    n = party_count(main.raid)
    assigned = {c.id: c.party_no for c in main.characters}
    people: dict[int, Character] = {}
    for v in group:
        for c in v.characters:
            people.setdefault(c.id, c)
            assigned.setdefault(c.id, c.party_no)
    columns: list[tuple[str, list[Character]]] = []
    for p in range(1, n + 1):
        chars = [c for c in people.values() if assigned.get(c.id) == p]
        columns.append((f"{p}파티", chars))
    columns.append(("파티 미정", [c for c in people.values() if not assigned.get(c.id) or assigned[c.id] > n]))
    out = []
    for name, chars in columns:
        if chars:
            # 서폿은 아래로 (나머지는 원래 순서 유지: 공대원 이름 → 캐릭터 이름)
            chars = sorted(chars, key=lambda c: (cfg.is_support(c.cls), c.display_name or "", c.name))
            out.append((name, chars))
    return n, out


def _group_fields(cfg: Config, group: list[SessionView], title: str, now: datetime) -> list[tuple[str, str, bool]]:
    """일정 묶음 하나: 레이드 이름·난이도·입장 레벨을 한 코드블록에 한 줄씩, 그 아래에 1파티(왼쪽)·2파티(오른쪽).
    8인 레이드가 없는 묶음(4인만)은 파티 구분 없이 헤더 아래에 파티원을 놓는다."""
    ordered = sorted(group, key=lambda v: (v.session.start_at, v.session.id))
    header = "```ansi\n" + "\n".join(_header_line(v, now) for v in ordered) + "\n```"
    n, columns = _party_columns(cfg, group)
    if n == 1:
        members = "\n".join(_member_line(cfg, c) for _, chars in columns for c in chars)
        return [(title, truncate(header + ("\n" + members if members else ""), 1024), False)]
    fields: list[tuple[str, str, bool]] = [(title, header, False)]
    for i in range(0, len(columns), 2):
        row = columns[i:i + 2]
        fields += [(name, truncate("\n".join(_member_line(cfg, c) for c in chars), 1024), True) for name, chars in row]
        fields += [BLANK_CELL] * (3 - len(row))   # 한 줄에 최대 2개, 나머지 칸은 비워 다음 줄로 보낸다
    return fields


def raid_schedule_groups(
    cfg: Config, views: list[SessionView], now: datetime, party_of: dict[int, int] | None = None,
) -> tuple[str, list[list[tuple[str, str, bool]]]]:
    """/레이드일정 재료: (안내 문구, 묶음별 필드 목록). 날짜마다 그날 일정을 같은 고정 파티끼리 묶고,
    묶음마다 레이드 목록 코드블록 아래에 1파티(왼쪽)·2파티(오른쪽)를 놓는다. 날짜는 그날 첫 묶음 필드 이름에 붙인다.
    party_of 는 일정 번호 → 고정 파티 번호 (services.Overview.party_of)."""
    days: dict[date, list[SessionView]] = {}
    for v in sorted(views, key=lambda v: v.session.start_at):
        days.setdefault(from_epoch(v.session.start_at).date(), []).append(v)
    out: list[list[tuple[str, str, bool]]] = []
    for day, day_views in days.items():
        for k, group in enumerate(_session_groups(day_views, party_of)):
            out.append(_group_fields(cfg, group, fmt_date(day) if k == 0 else ZWSP, now))
    return ("" if days else "_잡힌 일정이 없어요._"), out


def my_schedule_fields(
    cfg: Config, views: list[SessionView], member_id: int, remaining: list[tuple[Character, list[Raid]]], now: datetime
) -> tuple[str, list[tuple[str, str, bool]]]:
    """내 일정 임베드 재료: (안내 문구, 필드들). 날짜마다 캐릭터 필드(하루 2명이면 좌우로), 끝에 '그 외 숙제'."""
    sections: list[list[tuple[str, str, bool]]] = []
    days: dict[date, list[SessionView]] = {}
    for v in sorted(views, key=lambda v: v.session.start_at):
        days.setdefault(from_epoch(v.session.start_at).date(), []).append(v)
    for day, day_views in days.items():
        rows: dict[int, tuple[Character, list[str]]] = {}
        for v in day_views:
            line = _session_line(v, now)
            for c in v.characters:
                if c.member_id == member_id:
                    rows.setdefault(c.id, (c, []))[1].append(line)
        sections.append(_my_fields(cfg, fmt_date(day), list(rows.values())))
    if remaining:
        entries = [(c, [_my_raid_line(r.name, level=r.min_item_level) for r in raids]) for c, raids in remaining]
        sections.append(_my_fields(cfg, "그 외 숙제", entries))
    fields = [f for section in sections for f in section]
    return ("" if days else "_잡힌 일정이 없어요._"), fields[:25]


def heatmap_text(avail: list[Availability], total_members: int) -> str:
    """요일×시간 가능 인원 표 (코드블록)."""
    counts = hourly_counts(avail)
    hours = sorted({h for day in counts.values() for h, ms in day.items() if ms})
    if not hours:
        return "_아직 가능 시간을 등록한 공대원이 없어요._"
    lo, hi = hours[0], hours[-1]
    cols = list(range(lo, hi + 1))
    header = "   " + "".join(f"{h % 24:>3}" for h in cols)
    rows = [header]
    for d in range(7):
        cells = "".join(f"{len(counts[d][h]) or '·':>3}" for h in cols)
        rows.append(f"{WEEKDAYS[d]} {cells}")
    note = f"숫자 = 그 1시간 전체가 가능한 인원 (등록 {total_members}명). 24시 이후는 익일 새벽."
    return "```\n" + "\n".join(rows) + "\n```\n" + note


def day_detail_text(avail: list[Availability], weekday: int, names: dict[int, str]) -> str:
    counts = hourly_counts(avail)[weekday]
    lines = []
    for h in sorted(counts):
        if counts[h]:
            who = ", ".join(sorted(names.get(m, "?") for m in counts[h]))
            label = f"익일 {h - 24:02d}" if h >= 24 else f"{h:02d}"
            lines.append(f"`{label}시` **{len(counts[h])}** {who}")
    return "\n".join(lines) or f"_{WEEKDAYS[weekday]}요일에 가능한 사람이 없어요._"


def morning_text(cfg: Config, today: datetime, views: list[SessionView]) -> str:
    head = f"☀️ **{fmt_date(today.date())} 오늘의 레이드**"
    if not views:
        return head + "\n오늘은 잡힌 일정이 없어요."
    parts = [head]
    for v in views:
        s = v.session
        name = v.raid.name if v.raid else s.raid_key
        parts.append(f"\n**{from_epoch(s.start_at):%H:%M} {name}** `{v.filled}` #{s.id}")
        parts.append(participants_block(v.characters, mention=True))
    return truncate("\n".join(parts), 2000)


def reset_eve_text(cfg: Config, ov: Overview, now: datetime) -> str | None:
    """리셋 전날 요약: 이번 주 안에 못 끝낸 골드 레이드(예정 🗓️ / 아직 안 잡힘 ❔)가 있는 공대원. 없으면 None."""
    lines = []
    for m in ov.members:
        parts = []
        for row in m.characters:
            done = {v.raid.group for v in row.sessions if v.raid and session_done(v, now)}
            planned = {v.raid.group for v in row.sessions if v.raid and not session_done(v, now) and not session_failed(v)}
            todo = []
            for r in row.gold_raids:
                if r.group in done:
                    continue
                todo.append(f"{r.name}{'🗓️' if r.group in planned else '❔'}")
            if todo:
                parts.append(f"{row.character.name}: {', '.join(todo)}")
        if parts:
            who = f"<@{m.discord_id}>" if m.discord_id else m.name
            lines.append(f"{who}\n" + "\n".join(f"-# 　{p}" for p in parts))
    if not lines:
        return None
    reset = f"{WEEKDAYS[cfg.reset_weekday]}요일 {cfg.reset_hour:02d}:00"
    head = f"🔔 **내일 {reset} 리셋!** 아직 못 끝낸 골드 레이드예요. (🗓️ 예정 · ❔ 아직 안 잡힘)"
    return truncate(head + "\n" + "\n".join(lines), 2000)


def reminder_text(cfg: Config, v: SessionView, minutes_left: int) -> str:
    s = v.session
    name = v.raid.name if v.raid else s.raid_key
    return truncate(
        f"⏰ **{minutes_left}분 뒤 {name}** ({from_epoch(s.start_at):%H:%M}) `{v.filled}` #{s.id}\n"
        + participants_block(v.characters, mention=True),
        2000,
    )


def truncate(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[: limit - 20].rsplit("\n", 1)[0] + "\n…(생략)"


def sync_result_text(result: "SyncResult", owner: str | None = None) -> str:
    """원정대 동기화 결과. owner 를 주면 줄 앞에 공대원 이름을 붙인다."""
    prefix = f"{owner} · " if owner else ""
    lines = []
    for c in result.changes:
        who = f"{prefix}{char_head(c.cls, f'**{c.name}**')}"  # 이름 왼쪽은 직업 아이콘
        if c.old_level is None:
            head = f"{who} {c.new_level:g} 추가 🆕"
        elif c.old_level != c.new_level:
            arrow = levelup_mark() if c.new_level > c.old_level else "⬇️"
            head = f"{who} {c.old_level:g} → **{c.new_level:g}** {arrow}"
        else:
            head = f"{who} {c.new_level:g} 🔁"
        if c.gold_changed and not c.gold_after:
            head += f"\n-# 　{gold_mark()} 골드 획득 해제 (원정대 아이템 레벨 상위 캐릭터에서 밀려남)"
        elif c.gold_changed:
            added = [r.name for r in c.gold_after if r not in c.gold_before]
            removed = [r.name for r in c.gold_before if r not in c.gold_after]
            parts = [f"+{n}" for n in added] + [f"−{n}" for n in removed]
            head += f"\n-# 　{gold_mark()} 골드 레이드 변경: {' '.join(parts) or '순서 변경'}"
        if c.removed_from:
            head += f"\n-# 　🚪 일정에서 빠짐 (더 이상 안 가는 레이드): {', '.join(c.removed_from)} — 새 일정에 다시 참가시켜 주세요"
        lines.append(head)
    if result.missing:
        lines.append(
            f"-# ❔ 원정대에서 찾지 못한 캐릭터: {', '.join(result.missing)} "
            "(닉네임 변경/삭제라면 `/캐릭터삭제`)"
        )
    return "\n".join(lines)
