"""이벤트 레이드, 날짜별 가능 명단, /내일정, 고정 일정 메시지."""
import unittest
from datetime import date, datetime
from unittest import mock

import discord

from bot import board as board_mod
from bot.board import BoardManager
from bot.config import ROOT, add_event_raids, load_config
from bot.db import Database
from bot.render import lopec_url, my_schedule_text, weekly_availability_text
from bot.services import SessionView, build_overview, gold_raids
from bot.timeutil import KST, to_epoch

CFG = load_config(ROOT / "config.toml")
G = 1


def kst(*args) -> datetime:
    return datetime(*args, tzinfo=KST)


class EventRaidTest(unittest.TestCase):
    def test_event_raid_from_db(self):
        db = Database(":memory:")
        key = db.add_event_raid("할로윈 레이드", 8, 1600, 60, "2026-10-31", 1)
        cfg = add_event_raids(CFG, db.list_event_raids())
        raid = cfg.raids[key]
        self.assertEqual(raid.name, "[이벤트] 할로윈 레이드")
        self.assertTrue(raid.event)
        self.assertTrue(raid.is_active(date(2026, 10, 31)))
        self.assertFalse(raid.is_active(date(2026, 11, 1)))
        # 골드 레이드 추천에 끼지 않는다
        m = db.upsert_member(G, 1, "a")
        c, _ = db.upsert_character(m.id, "x", "바드", 1600)
        self.assertNotIn(key, [r.key for r in gold_raids(cfg, c)])
        # 종료하면 어제 날짜로
        self.assertTrue(db.set_event_raid_end(key, "2026-10-01"))
        cfg = add_event_raids(CFG, db.list_event_raids())
        self.assertFalse(cfg.raids[key].is_active(date(2026, 10, 2)))
        self.assertNotEqual(db.add_event_raid("두번째", 4, 0, 30, None, 1), key)

    def test_event_raid_in_config(self):
        import tempfile
        from pathlib import Path
        text = (ROOT / "config.toml").read_text(encoding="utf-8") + (
            '\n[[raids]]\nkey = "ev_test"\nname = "테스트 이벤트"\nparty_size = 4\nevent = true\nend_date = "2026-10-20"\n'
        )
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "config.toml"
            p.write_text(text, encoding="utf-8")
            cfg = load_config(p)
        r = cfg.raids["ev_test"]
        self.assertEqual((r.name, r.event, r.ends_on), ("[이벤트] 테스트 이벤트", True, date(2026, 10, 20)))


class ScheduleViewsTest(unittest.TestCase):
    def setUp(self):
        self.db = Database(":memory:")
        self.a = self.db.upsert_member(G, 100, "철수")
        self.b = self.db.upsert_member(G, 200, "영희")
        self.c = self.db.upsert_member(G, 300, "민수")
        self.a1, _ = self.db.upsert_character(self.a.id, "철수바드", "바드", 1745)
        self.b1, _ = self.db.upsert_character(self.b.id, "영희소서", "소서리스", 1745)
        self.now = kst(2026, 9, 30, 12)

    def test_weekly_availability_by_date(self):
        self.db.set_availability(self.a.id, 4, 21 * 60, 26 * 60)       # 금 21~02
        self.db.set_availability(self.b.id, 4, 20 * 60 + 30, 24 * 60)  # 금 20:30~24
        names = {m.id: m.display_name for m in self.db.list_members(G)}
        text = weekly_availability_text(self.db.list_guild_availability(G), names, kst(2026, 9, 30, 6))
        lines = text.splitlines()
        self.assertTrue(lines[0].startswith("**9/30(수)**"))  # 주간 시작(수)부터
        fri = lines.index("**10/2(금)** · 2명")
        self.assertEqual(lines[fri + 1], "　영희 `20:30~00` · 철수 `21~02`")  # 시작 시각 순
        self.assertIn("가능 시간 미등록: 민수", text)
        only = weekly_availability_text(self.db.list_guild_availability(G), names, kst(2026, 9, 30, 6), 4)
        self.assertTrue(only.startswith("**10/2(금)**"))
        self.assertNotIn("9/30", only)

    def test_my_schedule_shows_party_and_remaining(self):
        s = self.db.create_session(G, "serka_nightmare", to_epoch(kst(2026, 10, 2, 21)), 1, "숙련")
        self.db.add_slot(s.id, self.a1.id)
        self.db.add_slot(s.id, self.b1.id)
        ov = build_overview(self.db, CFG, G, self.now)
        views = [SessionView(s, CFG.raid(s.raid_key), self.db.session_characters(s.id))]
        row = next(m for m in ov.members if m.member_id == self.a.id)
        text = my_schedule_text(views, self.a.id, [(r.character, r.remaining) for r in row.characters], self.now)
        self.assertIn("🗓️ **10/2(금) 21:00 세르카 나이트메어** `2/4`", text)
        self.assertIn("내 캐릭터: **철수바드**", text)
        self.assertIn("함께: 영희(영희소서)", text)
        self.assertIn("철수바드: 종막 하드, 지평의 성당 2단계", text)

    def test_lopec_url_encodes_korean(self):
        self.assertEqual(lopec_url("달빛바드"), "https://lopec.kr/character/specPoint/%EB%8B%AC%EB%B9%9B%EB%B0%94%EB%93%9C")


class FakeMessage:
    _next = 1000

    def __init__(self, embed):
        FakeMessage._next += 1
        self.id = FakeMessage._next
        self.embed = embed
        self.pinned = False
        self.deleted = False

    async def edit(self, embed):
        self.embed = embed

    async def pin(self, reason=None):
        self.pinned = True

    async def unpin(self, reason=None):
        self.pinned = False

    async def delete(self):
        self.deleted = True


class FakeChannel:
    def __init__(self):
        self.messages: dict[int, FakeMessage] = {}

    async def send(self, embed):
        m = FakeMessage(embed)
        self.messages[m.id] = m
        return m

    async def fetch_message(self, mid):
        m = self.messages.get(mid)
        if m is None or m.deleted:
            raise discord.NotFound(mock.Mock(status=404), "unknown message")
        return m


class BoardTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.db = Database(":memory:")
        self.channel = FakeChannel()
        self.db.update_settings(G, notify_channel_id=555)
        bot = mock.Mock()
        bot.db, bot.cfg = self.db, CFG
        bot.get_channel = lambda cid: self.channel if cid == 555 else None
        self.board = BoardManager(bot)
        self.now = kst(2026, 9, 30, 12)

    async def refresh(self, now):
        with mock.patch.object(board_mod, "now_kst", return_value=now):
            await self.board.refresh(G)

    async def test_create_pin_edit_and_weekly_rollover(self):
        await self.refresh(self.now)
        self.assertEqual(self.channel.messages, {})  # 일정이 없으면 만들지 않는다

        s = self.db.create_session(G, "final_hard", to_epoch(kst(2026, 10, 2, 21)), 1, None)
        await self.refresh(self.now)
        (msg,) = self.channel.messages.values()
        self.assertTrue(msg.pinned)
        self.assertIn("종막 하드", msg.embed.description)
        self.assertEqual(self.db.get_settings(G).board_week, "2026-09-30")

        self.db.update_session_memo(s.id, "숙련")
        await self.refresh(self.now)
        self.assertEqual(len(self.channel.messages), 1)  # 새로 보내지 않고 수정
        self.assertIn("숙련", msg.embed.description)

        # 다음 주간으로 넘어가면 지난주 메시지 고정 해제 + 삭제, 새 주 일정이 없으니 새 메시지도 없음
        next_week = kst(2026, 10, 7, 7)
        with mock.patch.object(board_mod, "now_kst", return_value=next_week):
            await self.board.check_rollover(G)
        self.assertTrue(msg.deleted)
        self.assertFalse(msg.pinned)
        self.assertIsNone(self.db.get_settings(G).board_message_id)

        # 새 주 일정이 생기면 새 고정 메시지
        self.db.create_session(G, "final_hard", to_epoch(kst(2026, 10, 9, 21)), 1, None)
        await self.refresh(next_week)
        live = [m for m in self.channel.messages.values() if not m.deleted]
        self.assertEqual(len(live), 1)
        self.assertTrue(live[0].pinned)
        self.assertEqual(self.db.get_settings(G).board_week, "2026-10-07")

    async def test_recreates_when_message_was_deleted_by_someone(self):
        self.db.create_session(G, "final_hard", to_epoch(kst(2026, 10, 2, 21)), 1, None)
        await self.refresh(self.now)
        first = next(iter(self.channel.messages.values()))
        first.deleted = True
        await self.refresh(self.now)
        live = [m for m in self.channel.messages.values() if not m.deleted]
        self.assertEqual(len(live), 1)
        self.assertNotEqual(live[0].id, first.id)

    async def test_db_changes_trigger_request(self):
        calls = []
        self.db.on_change = calls.append
        s = self.db.create_session(G, "final_hard", to_epoch(kst(2026, 10, 2, 21)), 1, None)
        m = self.db.upsert_member(G, 9, "x")
        c, _ = self.db.upsert_character(m.id, "c", "바드", 1740)
        self.db.add_slot(s.id, c.id)
        self.db.remove_slot(s.id, c.id)
        self.db.cancel_session(s.id)
        self.assertEqual(calls, [G, G, G, G])


if __name__ == "__main__":
    unittest.main()


class TierAndMoreCostTest(unittest.TestCase):
    def setUp(self):
        self.db = Database(":memory:")
        self.m = self.db.upsert_member(G, 1, "나래")

    def char(self, name, level, tier=None):
        c, _ = self.db.upsert_character(self.m.id, name, "바드", level)
        if tier:
            self.db.set_character_tier(c.id, tier)
        return next(x for x in self.db.list_characters(self.m.id) if x.id == c.id)

    def test_auto_and_explicit_tiers(self):
        from bot.services import homework
        names = lambda hw: [r.name for r in hw.gold]  # noqa: E731
        hw = homework(CFG, self.char("a", 1782))
        self.assertEqual((hw.tier, hw.explicit), ("1780+", False))
        self.assertEqual(names(hw), ["벨가르딘 나이트메어", "지평의 성당 3단계", "세르카 나이트메어"])
        hw = homework(CFG, self.char("b", 1782, "1780+_2"))
        self.assertEqual((hw.tier, hw.explicit), ("1780+_2", True))
        self.assertEqual(names(hw)[0], "벨가르딘 하드")
        # 골드X 레이드가 있는 구간
        hw = homework(CFG, self.char("c", 1735, "1730+_2"))
        self.assertEqual(names(hw), ["종막 하드", "세르카 하드", "4막 하드"])
        self.assertEqual([r.name for r in hw.extra], ["지평의 성당 2단계"])
        # 레벨이 모자라는 구간을 지정하면 자동 구간으로
        hw = homework(CFG, self.char("d", 1745, "1780+"))
        self.assertEqual((hw.tier, hw.explicit), ("1730+", False))
        # 구간표 아래 레벨은 골드 순 상위 3개
        hw = homework(CFG, self.char("e", 1685))
        self.assertIsNone(hw.tier)
        self.assertEqual(len(hw.gold), 3)

    def test_more_cost_summary_matches_sheet_rule(self):
        from bot.services import gold_summary
        r = CFG.raids
        # 세르카 노말: 귀속 16k 에서 더보기 10,240 차감 / 세르카 나메: 거래 가능에서 17,280 차감 / 성당 3단계: 전부 귀속
        s = gold_summary([r["serka_normal"], r["serka_nightmare"], r["cathedral_3"]])
        self.assertEqual(s.gold, 32000 + 54000 + 50000)
        self.assertEqual(s.more_cost, 10240 + 17280 + 16000)
        self.assertEqual(s.bound_after, (16000 - 10240) + (50000 - 16000))
        self.assertEqual(s.tradable_after, 16000 + (54000 - 17280))
        self.assertEqual(s.after, s.gold - s.more_cost)

    def test_config_tiers_loaded(self):
        self.assertEqual(len(CFG.tiers), 14)
        self.assertEqual(CFG.auto_tier(1755).label, "1750+")
        self.assertEqual(CFG.auto_tier(1741).label, "1730+")
        self.assertIsNone(CFG.auto_tier(1700))
        self.assertEqual(CFG.raids["serka_nightmare"].more_cost, 17280)


class SupportCompositionTest(unittest.TestCase):
    def setUp(self):
        from bot.services import add_to_session
        self.add = add_to_session
        self.db = Database(":memory:")
        self.now = kst(2026, 9, 30, 12)

    def char(self, i, cls):
        m = self.db.upsert_member(G, 500 + i, f"m{i}")
        c, _ = self.db.upsert_character(m.id, f"c{i}", cls, 1745)
        return c

    def test_self_join_blocks_fourth_dealer_and_second_support(self):
        from bot.services import RuleError
        s = self.db.create_session(G, "serka_nightmare", to_epoch(kst(2026, 10, 2, 21)), 1, None)
        for i in range(3):
            self.add(self.db, CFG, s, self.char(i, "블레이드"), self.now, strict=True)
        with self.assertRaisesRegex(RuleError, "딜러 자리가 없어요"):
            self.add(self.db, CFG, s, self.char(3, "데빌헌터"), self.now, strict=True)
        self.add(self.db, CFG, s, self.char(4, "바드"), self.now, strict=True)  # 서폿은 들어간다
        s2 = self.db.create_session(G, "serka_hard", to_epoch(kst(2026, 10, 3, 21)), 1, None)
        self.add(self.db, CFG, s2, self.char(5, "도화가"), self.now, strict=True)
        with self.assertRaisesRegex(RuleError, "서폿이 있어요"):
            self.add(self.db, CFG, s2, self.char(6, "홀나"), self.now, strict=True)  # 줄임말도 서폿

    def test_leader_can_place_four_dealers_but_gets_warning(self):
        from bot.render import party_lines
        from bot.services import composition_warnings
        s = self.db.create_session(G, "serka_nightmare", to_epoch(kst(2026, 10, 2, 21)), 1, None)
        warns = []
        for i in range(4):
            _, warns = self.add(self.db, CFG, s, self.char(i, "블레이드"), self.now, strict=False)
        self.assertIn("⚠️ 서폿 없음 (딜러 4명)", warns)
        members = self.db.session_characters(s.id)
        self.assertEqual(composition_warnings(CFG, CFG.raid(s.raid_key), members), ["서폿 없음 (딜러 4명)"])
        v = SessionView(s, CFG.raid(s.raid_key), members)
        self.assertIn("-# ⚠️ 서폿 없음 (딜러 4명)", party_lines(CFG, v))

    def test_eight_person_raid_splits_supports_into_two_parties(self):
        from bot.render import party_lines
        from bot.services import composition_warnings, supports_first
        s = self.db.create_session(G, "final_hard", to_epoch(kst(2026, 10, 2, 21)), 1, None)
        chars = [self.char(i, "블레이드") for i in range(6)] + [self.char(6, "바드"), self.char(7, "발키리")]
        for c in supports_first(CFG, chars):
            self.add(self.db, CFG, s, c, self.now, strict=True)
        members = self.db.session_characters(s.id)
        for p in (1, 2):
            party = [c for c in members if c.party_no == p]
            self.assertEqual(len(party), 4)
            self.assertEqual(sum(CFG.is_support(c.cls) for c in party), 1)
        self.assertEqual(composition_warnings(CFG, CFG.raid(s.raid_key), members), [])
        lines = party_lines(CFG, SessionView(s, CFG.raid(s.raid_key), members))
        self.assertEqual(lines[0], "```ansi")
        self.assertTrue("1파티" in lines[1] and "2파티" in lines[1])
        self.assertLess(lines[1].index("1파티"), lines[1].index("2파티"))
        self.assertNotIn("🛡️", "\n".join(lines))

    def test_three_dealers_and_empty_slot_is_fine(self):
        from bot.services import composition_warnings
        s = self.db.create_session(G, "serka_nightmare", to_epoch(kst(2026, 10, 2, 21)), 1, None)
        for i in range(3):
            self.add(self.db, CFG, s, self.char(i, "블레이드"), self.now, strict=True)
        self.assertEqual(composition_warnings(CFG, CFG.raid(s.raid_key), self.db.session_characters(s.id)), [])


class BlockedInAvailabilityTextTest(unittest.TestCase):
    def test_blocked_marks_shown_per_date(self):
        from bot.db import Database, Unavailable
        from bot.timeutil import KST
        from datetime import datetime
        db = Database(":memory:")
        m = db.upsert_member(G, 1, "철수")
        db.set_availability(m.id, 4, 21 * 60, 24 * 60)
        names = {m.id: "철수"}
        k = lambda *a: int(datetime(*a, tzinfo=KST).timestamp())
        blocked = [Unavailable(1, m.id, k(2026, 10, 2, 20), k(2026, 10, 2, 23)),
                   Unavailable(2, m.id, k(2026, 10, 3), k(2026, 10, 4))]
        text = weekly_availability_text(db.list_guild_availability(G), names, kst(2026, 9, 30, 6), blocked=blocked)
        self.assertIn("🚫 철수 `20~23`", text)
        self.assertIn("🚫 철수 `하루 종일`", text)
        self.assertNotIn("🚫", weekly_availability_text(db.list_guild_availability(G), names, kst(2026, 9, 30, 6)))
