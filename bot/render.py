"""메시지 텍스트 생성. 디스코드 객체 없이 문자열만 만든다."""
from __future__ import annotations

import re
from collections import defaultdict
from datetime import date, datetime, timedelta
from itertools import groupby
from typing import TYPE_CHECKING
from urllib.parse import quote

from .config import Config, Raid
from .db import Availability, Character, Session, Unavailable
from .services import (
    GoldSummary, Homework, Overview, SessionView, composition_warnings, gold_summary, hourly_counts,
    party_count,
)
from .timeutil import KST, WEEKDAYS, fmt_date, fmt_dt, fmt_range, from_epoch

if TYPE_CHECKING:
    from .roster import SyncResult


LOPEC_URL = "https://lopec.kr/character/specPoint/"
# parties.PALETTE 의 이모지와 같은 순서 (순환 import 를 피하려고 여기 둔다)
PARTY_EMOJI = ["🟪", "🟦", "🟧", "🟩", "🟨", "🟥", "🟫", "⚫"]


def lopec_url(name: str) -> str:
    return LOPEC_URL + quote(name)


def character_line(c: Character, with_owner: bool = False) -> str:
    owner = f" — {c.display_name}" if with_owner and c.display_name else ""
    return f"**{c.name}** {c.cls} {c.item_level:g}{owner}"


def gold(n: int) -> str:
    return f"{n:,}G"


def gold_summary_text(s: GoldSummary) -> str:
    """클골 합계와 더보기를 전부 했을 때 남는 골드."""
    return (
        f"💰 주간 클골 **{gold(s.gold)}** (거래 {gold(s.tradable)} · 귀속 {gold(s.bound)})\n"
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
            lines.append(f"💰 {character_line(c)}{tier} · {gold(s.gold)} (더보기 −{s.more_cost / 1000:g}k)")
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
        lines.append(f"• {who} — {c.name} ({c.cls} {c.item_level:g})")
    return "\n".join(lines)


PARTY_MEMO_RE = re.compile(r"파티 \d+")


def party_tag(number: int | None) -> str:
    """고정 파티 일정은 색 이모지 + P번호, 아닌 일정은 ▫️ — 줄마다 같은 자리에 와서 눈으로 훑기 쉽다."""
    if not number:
        return "▫️ "
    return f"{PARTY_EMOJI[(number - 1) % len(PARTY_EMOJI)]}P{number} "


def overview_text(cfg: Config, ov: Overview, now: datetime | None = None) -> str:
    """일정별 보기. ✅ 끝난 일정(취소선) · 🟢 정원 참 · ⏳ 모집 중."""
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
            full = v.raid and len(v.characters) >= v.raid.party_size
            tag = party_tag(ov.party_of.get(s.id))
            # 파티 일정의 메모는 '파티 N' 이 대부분이라 색·번호 표시와 겹치니 생략한다
            memo = f" · {s.memo}" if s.memo and not (s.id in ov.party_of and PARTY_MEMO_RE.fullmatch(s.memo)) else ""
            if session_failed(v):
                lines.append(f"❌ {tag}~~{from_epoch(s.start_at):%H:%M} {name}~~ `{v.filled}` #{s.id} **미완료**")
                continue
            if session_done(v, now):
                lines.append(f"✅ {tag}~~{from_epoch(s.start_at):%H:%M} {name}~~ `{v.filled}` #{s.id}")
                continue
            mark = "🟢" if full else "⏳"
            lines.append(f"{mark} {tag}**{from_epoch(s.start_at):%H:%M} {name}** `{v.filled}` #{s.id}{memo}")
            lines += party_lines(cfg, v)

    if not ov.single_day:
        if ov.unassigned_by_raid:
            lines.append("### 골드 레이드 남은 캐릭터")
            limit = 30 if len(ov.unassigned_by_raid) == 1 else 6
            for key, chars in ov.unassigned_by_raid.items():
                raid = cfg.raids[key]
                names = ", ".join(f"{c.display_name}({c.name})" for c in chars[:limit])
                more = f" 외 {len(chars) - limit}" if len(chars) > limit else ""
                lines.append(f"• {raid.name} **{len(chars)}**: {names}{more}")
        if ov.idle_members:
            lines.append("### 이번 주 일정 없는 공대원")
            lines.append(", ".join(ov.idle_members))

    return truncate("\n".join(lines), 4000)


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
            lines.append(f"**[{c.name}]({lopec_url(c.name)})** {c.cls} {c.item_level:g}{pending}")
            for v in row.sessions:
                name = v.raid.name if v.raid else v.session.raid_key
                when = f"{from_epoch(v.session.start_at):%H:%M}" if day_view else fmt_dt(from_epoch(v.session.start_at))
                if session_failed(v):
                    lines.append(f"　❌ ~~{when} {name}~~ 미완료")
                elif session_done(v, now):
                    lines.append(f"　✅ ~~{when} {name}~~")
                else:
                    party = ov.party_of.get(v.session.id)
                    tag = party_tag(party) if party else ""
                    mine = next((x for x in v.characters if x.id == c.id), None)
                    which = f" · {mine.party_no}파티" if mine and mine.party_no and party_count(v.raid) > 1 else ""
                    warn = " ⚠️" if cfg and composition_warnings(cfg, v.raid, v.characters) else ""
                    lines.append(f"　🗓️ {tag}{when} {name} `{v.filled}` #{v.session.id}{which}{warn}")
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


def _member_label(cfg: Config, c: Character) -> str:
    mark = "🛡️" if cfg.is_support(c.cls) else ""
    return f"{c.display_name}({c.name}{mark})"


def party_lines(cfg: Config, v: SessionView) -> list[str]:
    """참가자를 파티(4명)별로. 8인 이상이면 '1파티: …' 로 나누고, 서폿 구성 경고를 붙인다. 🛡️ = 서폿"""
    if not v.characters:
        return []
    n = party_count(v.raid)
    out = []
    if n == 1:
        out.append("　" + ", ".join(_member_label(cfg, c) for c in v.characters))
    else:
        for p in range(1, n + 1):
            party = [c for c in v.characters if c.party_no == p]
            if party:
                out.append(f"　{p}파티: " + ", ".join(_member_label(cfg, c) for c in party))
        rest = [c for c in v.characters if not c.party_no or c.party_no > n]
        if rest:
            out.append("　파티 미정: " + ", ".join(_member_label(cfg, c) for c in rest))
    for w in composition_warnings(cfg, v.raid, v.characters):
        out.append(f"-# 　⚠️ {w}")
    return out


def availability_text(slots: list[Availability]) -> str:
    if not slots:
        return "_등록된 가능 시간이 없어요._"
    by_day = {a.weekday: a for a in slots}
    return "\n".join(
        f"`{WEEKDAYS[d]}` {fmt_range(by_day[d].start_min, by_day[d].end_min) if d in by_day else '—'}"
        for d in range(7)
    )


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


def my_schedule_text(
    views: list[SessionView], member_id: int, remaining: list[tuple[Character, list[Raid]]], now: datetime
) -> str:
    """내가 들어간 일정(함께하는 공대원 포함)과 아직 안 잡힌 골드 레이드."""
    lines = []
    if not views:
        lines.append("_잡힌 일정이 없어요._")
    for v in views:
        s = v.session
        start = from_epoch(s.start_at)
        name = v.raid.name if v.raid else s.raid_key
        mine = [c.name for c in v.characters if c.member_id == member_id]
        others = [f"{c.display_name}({c.name})" for c in v.characters if c.member_id != member_id]
        memo = f" · {s.memo}" if s.memo else ""
        if session_failed(v):
            lines.append(f"❌ ~~{fmt_dt(start)} {name}~~ 미완료 — {', '.join(mine)}")
            continue
        if session_done(v, now):
            lines.append(f"✅ ~~{fmt_dt(start)} {name}~~ — {', '.join(mine)}")
            continue
        lines.append(f"🗓️ **{fmt_dt(start)} {name}** `{v.filled}` #{s.id}{memo} — 내 캐릭터: **{', '.join(mine)}**")
        lines.append(f"-# 　함께: {', '.join(others) if others else '아직 없음'}")
    if remaining:
        lines.append("\n**📋 아직 안 잡힌 숙제**")
        for c, raids in remaining:
            lines.append(f"{c.name}: " + ", ".join(r.name for r in raids))
    return truncate("\n".join(lines), 4000)


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
        if c.old_level is None:
            head = f"🆕 {prefix}**{c.name}** {c.cls} {c.new_level:g} 추가"
        elif c.old_level != c.new_level:
            arrow = "⬆️" if c.new_level > c.old_level else "⬇️"
            head = f"{arrow} {prefix}**{c.name}** {c.old_level:g} → **{c.new_level:g}**"
        else:
            head = f"🔁 {prefix}**{c.name}** {c.new_level:g}"
        if c.gold_changed and not c.gold_after:
            head += "\n-# 　💰 골드 획득 해제 (원정대 아이템 레벨 상위 캐릭터에서 밀려남)"
        elif c.gold_changed:
            added = [r.name for r in c.gold_after if r not in c.gold_before]
            removed = [r.name for r in c.gold_before if r not in c.gold_after]
            parts = [f"+{n}" for n in added] + [f"−{n}" for n in removed]
            head += f"\n-# 　💰 골드 레이드 변경: {' '.join(parts) or '순서 변경'}"
        lines.append(head)
    if result.missing:
        lines.append(
            f"-# ❔ 원정대에서 찾지 못한 캐릭터: {', '.join(result.missing)} "
            "(닉네임 변경/삭제라면 `/캐릭터삭제`)"
        )
    return "\n".join(lines)
