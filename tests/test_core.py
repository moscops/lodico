import unittest
from datetime import date, datetime, time

from bot.config import ROOT, apply_overrides, load_config
from bot.db import Database
from bot.render import heatmap_text, member_view_chunks, morning_text, overview_text
from bot.services import (
    RuleError, SessionView, build_overview, check_join, gold_earner_ids, gold_plan, gold_raids,
    hourly_counts, is_available,
)
from bot.timeutil import KST, ParseError, parse_date, parse_time, to_epoch, week_start

CFG = load_config(ROOT / "config.toml")
G = 1  # guild id


def kst(*args) -> datetime:
    return datetime(*args, tzinfo=KST)


class TimeTest(unittest.TestCase):
    def test_week_start_wednesday_reset(self):
        # 2026-09-30 은 수요일
        self.assertEqual(week_start(kst(2026, 9, 30, 6, 0)), kst(2026, 9, 30, 6))
        self.assertEqual(week_start(kst(2026, 9, 30, 5, 59)), kst(2026, 9, 23, 6))
        self.assertEqual(week_start(kst(2026, 10, 4, 23)), kst(2026, 9, 30, 6))
        self.assertEqual(week_start(kst(2026, 10, 7, 5)), kst(2026, 9, 30, 6))

    def test_parse_date(self):
        today = date(2026, 9, 29)  # 화
        self.assertEqual(parse_date("10/3", today), date(2026, 10, 3))
        self.assertEqual(parse_date("2026-10-03", today), date(2026, 10, 3))
        self.assertEqual(parse_date("10월 3일", today), date(2026, 10, 3))
        self.assertEqual(parse_date("내일", today), date(2026, 9, 30))
        self.assertEqual(parse_date("화", today), today)
        self.assertEqual(parse_date("금요일", today), date(2026, 10, 2))
        self.assertEqual(parse_date("월", today), date(2026, 10, 5))
        self.assertEqual(parse_date("1/5", date(2026, 12, 20)), date(2027, 1, 5))
        with self.assertRaises(ParseError):
            parse_date("2/30", today)
        with self.assertRaises(ParseError):
            parse_date("아무때나", today)

    def test_parse_time(self):
        self.assertEqual(parse_time("21:00"), time(21))
        self.assertEqual(parse_time("21"), time(21))
        self.assertEqual(parse_time("2130"), time(21, 30))
        self.assertEqual(parse_time("21시"), time(21))
        self.assertEqual(parse_time("21시 30분"), time(21, 30))
        self.assertEqual(parse_time("9시반"), time(9, 30))
        self.assertEqual(parse_time("오후 9시"), time(21))
        with self.assertRaises(ParseError):
            parse_time("25:00")


class Fixture(unittest.TestCase):
    def setUp(self):
        self.db = Database(":memory:")
        self.now = kst(2026, 9, 30, 12)  # 수요일 낮, 새 주간
        self.a = self.db.upsert_member(G, 100, "철수")
        self.b = self.db.upsert_member(G, 200, "영희")
        self.a1, _ = self.db.upsert_character(self.a.id, "철수바드", "바드", 1685)
        self.a2, _ = self.db.upsert_character(self.a.id, "철수버서", "버서커", 1665)
        self.b1, _ = self.db.upsert_character(self.b.id, "영희소서", "소서리스", 1690)

    def session(self, raid: str, dt: datetime):
        return self.db.create_session(G, raid, to_epoch(dt), 1, None)


class JoinRuleTest(Fixture):
    def test_join_ok(self):
        s = self.session("act1_hard", kst(2026, 10, 1, 21))
        self.assertEqual(check_join(self.db, CFG, s, self.a1, self.now), [])

    def test_item_level_too_low(self):
        s = self.session("act1_hard", kst(2026, 10, 1, 21))
        with self.assertRaisesRegex(RuleError, "입장 레벨"):
            check_join(self.db, CFG, s, self.a2, self.now)

    def test_one_character_per_member_per_session(self):
        s = self.session("act1_normal", kst(2026, 10, 1, 21))
        self.db.add_slot(s.id, self.a1.id)
        with self.assertRaisesRegex(RuleError, "캐릭터 하나만"):
            check_join(self.db, CFG, s, self.a2, self.now)

    def test_same_character_same_raid_once_per_week(self):
        s1 = self.session("act1_hard", kst(2026, 10, 1, 21))
        s2 = self.session("act1_hard", kst(2026, 10, 3, 21))
        self.db.add_slot(s1.id, self.a1.id)
        with self.assertRaisesRegex(RuleError, "이미 배정"):
            check_join(self.db, CFG, s2, self.a1, self.now)
        # 다음 주간이면 가능
        s3 = self.session("act1_hard", kst(2026, 10, 7, 21))
        check_join(self.db, CFG, s3, self.a1, self.now)

    def test_member_time_overlap(self):
        s1 = self.session("act1_hard", kst(2026, 10, 1, 21))
        s2 = self.session("act1_normal", kst(2026, 10, 1, 21, 30))
        self.db.add_slot(s1.id, self.a1.id)
        # 같은 시간대 다른 일정에 참가 중이어도 막지 않고 경고만
        warnings = check_join(self.db, CFG, s2, self.a2, self.now)
        self.assertTrue(any("같은 시간대" in w for w in warnings))
        # 겹치지 않는 일정에는 그 경고가 없다
        s3 = self.session("act1_normal", kst(2026, 10, 1, 22))
        self.assertFalse(any("같은 시간대" in w for w in check_join(self.db, CFG, s3, self.a2, self.now)))

    def test_party_full(self):
        s = self.session("act1_hard", kst(2026, 10, 1, 21))
        for i in range(8):
            m = self.db.upsert_member(G, 1000 + i, f"m{i}")
            c, _ = self.db.upsert_character(m.id, f"c{i}", "바드", 1700)
            self.db.add_slot(s.id, c.id)
        with self.assertRaisesRegex(RuleError, "정원"):
            check_join(self.db, CFG, s, self.b1, self.now)

    def test_past_and_cancelled(self):
        s = self.session("act1_hard", kst(2026, 9, 30, 11))
        with self.assertRaisesRegex(RuleError, "시작한"):
            check_join(self.db, CFG, s, self.a1, self.now)
        s2 = self.session("act1_hard", kst(2026, 10, 1, 21))
        self.db.cancel_session(s2.id)
        with self.assertRaisesRegex(RuleError, "취소"):
            check_join(self.db, CFG, self.db.get_session(s2.id), self.a1, self.now)

    def test_outside_availability_is_warning(self):
        self.db.set_availability(self.a.id, 3, 20 * 60, 24 * 60)  # 목 20~24
        s = self.session("act1_hard", kst(2026, 10, 1, 18))  # 목 18시
        self.assertEqual(len(check_join(self.db, CFG, s, self.a1, self.now)), 1)


class AvailabilityTest(Fixture):
    def test_cross_midnight(self):
        self.db.set_availability(self.a.id, 4, 22 * 60, 26 * 60)  # 금 22 ~ 토 02
        slots = self.db.list_availability(self.a.id)
        self.assertTrue(is_available(slots, kst(2026, 10, 2, 23)))   # 금 23시
        self.assertTrue(is_available(slots, kst(2026, 10, 3, 1)))    # 토 01시
        self.assertFalse(is_available(slots, kst(2026, 10, 3, 2)))   # 토 02시
        self.assertFalse(is_available(slots, kst(2026, 10, 2, 21)))

    def test_hourly_counts_and_heatmap(self):
        self.db.set_availability(self.a.id, 0, 20 * 60, 23 * 60)
        self.db.set_availability(self.b.id, 0, 21 * 60 + 30, 25 * 60)
        counts = hourly_counts(self.db.list_guild_availability(G))
        self.assertEqual(len(counts[0][20]), 1)
        self.assertEqual(len(counts[0][21]), 1)  # 영희는 21:30부터라 21시는 불완전
        self.assertEqual(len(counts[0][22]), 2)
        self.assertEqual(len(counts[0][24]), 1)
        text = heatmap_text(self.db.list_guild_availability(G), 2)
        self.assertIn("월", text)

    def test_set_replaces_same_weekday(self):
        self.db.set_availability(self.a.id, 0, 20 * 60, 23 * 60)
        self.db.set_availability(self.a.id, 0, 19 * 60, 22 * 60)
        self.assertEqual(len(self.db.list_availability(self.a.id)), 1)
        with self.assertRaises(ValueError):
            self.db.set_availability(self.a.id, 1, 22 * 60, 21 * 60)


class OverviewTest(Fixture):
    def test_overview_counts_and_unassigned(self):
        s = self.session("act1_hard", kst(2026, 10, 1, 21))
        self.db.add_slot(s.id, self.a1.id)
        self.session("act1_normal", kst(2026, 10, 8, 21))  # 다음 주간 → 제외
        ov = build_overview(self.db, CFG, G, self.now)
        self.assertEqual(len(ov.sessions), 1)
        self.assertEqual(ov.sessions[0].filled, "1/8")
        self.assertEqual([c.name for c in ov.unassigned_by_raid["act1_hard"]], ["영희소서"])
        self.assertEqual(ov.idle_members, ["영희"])
        text = overview_text(CFG, ov)
        self.assertIn("1막 하드", text)
        self.assertIn("1/8", text)

    def test_today_and_raid_filter(self):
        self.session("act1_hard", kst(2026, 9, 30, 21))
        self.session("act1_normal", kst(2026, 9, 30, 22))
        self.session("act1_hard", kst(2026, 10, 1, 21))
        self.assertEqual(len(build_overview(self.db, CFG, G, self.now, "오늘").sessions), 2)
        ov = build_overview(self.db, CFG, G, self.now, "이번주", "act1_hard")
        self.assertEqual(len(ov.sessions), 2)
        # 같은 레이드(1막)의 다른 난이도가 골드 레이드인 캐릭터(철수버서 → 1막 노말)도 함께 나온다
        self.assertEqual(set(ov.unassigned_by_raid), {"act1_hard", "act1_normal"})

    def test_member_view_groups_characters_by_member(self):
        s1 = self.session("act1_hard", kst(2026, 10, 1, 21))
        s2 = self.session("act1_normal", kst(2026, 10, 2, 21))
        self.db.add_slot(s1.id, self.a1.id)
        self.db.add_slot(s2.id, self.a2.id)
        ov = build_overview(self.db, CFG, G, self.now)
        chulsu = next(m for m in ov.members if m.name == "철수")
        self.assertEqual([r.character.name for r in chulsu.characters], ["철수바드", "철수버서"])
        self.assertEqual([v.session.id for v in chulsu.characters[0].sessions], [s1.id])
        # 철수바드의 골드 레이드 중 1막 하드는 잡혔으니 남은 것에서 빠진다
        self.assertNotIn("act1_hard", [r.key for r in chulsu.characters[0].remaining])

        text = member_view_chunks(ov, viewer_id=200)[0]
        self.assertTrue(text.startswith("### 영희 (나)"))  # 보는 사람이 맨 위
        self.assertLess(text.index("철수바드"), text.index("철수버서"))
        self.assertNotIn("남음", text)
        self.assertIn("· 미정 ", text)
        # 일정이 끝나면 ✅ + 취소선
        after = member_view_chunks(ov, viewer_id=200, now=kst(2026, 10, 1, 23))[0]
        self.assertIn("✅ ~~10/1(목) 21:00 1막 하드~~", after)
        self.assertIn("🗓️ 10/2(금) 21:00 1막 노말", after)
        self.assertIn("### 철수 · ✅ 1 · 🗓️ 1", after)

    def test_weekday_period(self):
        # 이번 주간(9/30 수 06시 ~ 10/7 수 06시)의 금요일 = 10/2
        self.session("act1_hard", kst(2026, 10, 2, 21))
        s_other = self.session("act1_normal", kst(2026, 10, 1, 21))
        self.db.add_slot(s_other.id, self.a1.id)
        ov = build_overview(self.db, CFG, G, self.now, "금")
        self.assertTrue(ov.single_day)
        self.assertEqual(ov.start, kst(2026, 10, 2))
        self.assertEqual(len(ov.sessions), 1)
        # 하루 보기는 그 날 일정이 있는 캐릭터만 (목요일 일정만 있는 철수바드는 없음)
        self.assertEqual(ov.members, [])
        self.assertIn("없어요", member_view_chunks(ov)[0])
        # 월요일은 주간 안의 다음 주 월요일(10/5)
        self.assertEqual(build_overview(self.db, CFG, G, self.now, "월").start, kst(2026, 10, 5))

    def test_member_view_splits_long_output(self):
        for i in range(40):
            m = self.db.upsert_member(G, 5000 + i, f"공대원{i:02d}")
            for j in range(6):
                self.db.upsert_character(m.id, f"캐릭{i:02d}_{j}", "바드", 1745 - j)
        chunks = member_view_chunks(build_overview(self.db, CFG, G, self.now))
        self.assertGreater(len(chunks), 1)
        self.assertTrue(all(len(c) <= 3900 for c in chunks))

    def test_deleting_character_removes_slot(self):
        s = self.session("act1_hard", kst(2026, 10, 1, 21))
        self.db.add_slot(s.id, self.a1.id)
        self.db.delete_character(self.a.id, "철수바드")
        self.assertEqual(self.db.session_characters(s.id), [])

    def test_morning_text_mentions(self):
        s = self.session("act1_hard", kst(2026, 9, 30, 21))
        self.db.add_slot(s.id, self.b1.id)
        v = SessionView(s, CFG.raid(s.raid_key), self.db.session_characters(s.id))
        text = morning_text(CFG, self.now, [v])
        self.assertIn("<@200>", text)
        self.assertIn("21:00", text)


class GoldTest(Fixture):
    def names(self, raids):
        return [r.name for r in raids]

    def test_top3_picks_best_difficulty_per_raid(self):
        c, _ = self.db.upsert_character(self.a.id, "고렙", "바드", 1745)
        # 세르카는 나메(54k), 종막은 하드(48k), 성당은 2단계(40k). 같은 레이드 다른 난이도는 중복 안 됨
        self.assertEqual(self.names(gold_raids(CFG, c)), ["세르카 나이트메어", "종막 하드", "지평의 성당 2단계"])

    def test_tie_prefers_tradable_gold(self):
        c, _ = self.db.upsert_character(self.a.id, "벨가", "바드", 1750)
        # 벨가르딘 노말과 성당 3단계 모두 50k → 거래 가능 골드가 있는 벨가르딘 우선
        self.assertEqual(
            self.names(gold_raids(CFG, c)), ["세르카 나이트메어", "벨가르딘 노말", "지평의 성당 3단계"]
        )

    def test_only_top6_characters_per_member_earn_gold(self):
        m = self.db.upsert_member(G, 300, "부캐부자")
        chars = [self.db.upsert_character(m.id, f"캐{i}", "바드", 1700 - i)[0] for i in range(8)]
        earners = gold_earner_ids(CFG, self.db.list_characters(m.id))
        self.assertEqual(earners, {c.id for c in chars[:6]})
        plan = gold_plan(CFG, self.db.list_characters(m.id))
        self.assertEqual(plan[chars[7].id], [])
        self.assertEqual(len(plan[chars[0].id]), 3)

    def test_join_warns_when_not_gold_raid(self):
        s = self.session("prologue", kst(2026, 10, 1, 21))
        warnings = check_join(self.db, CFG, s, self.a1, self.now)
        self.assertTrue(any("숙제 레이드가 아니에요" in w for w in warnings))
        s2 = self.session("act1_normal", kst(2026, 10, 2, 21))
        warnings = check_join(self.db, CFG, s2, self.a1, self.now)
        self.assertTrue(any("추천 난이도는 1막 하드" in w for w in warnings))

    def test_same_raid_other_difficulty_blocked_same_week(self):
        s1 = self.session("act1_hard", kst(2026, 10, 1, 21))
        s2 = self.session("act1_normal", kst(2026, 10, 3, 21))
        self.db.add_slot(s1.id, self.a1.id)
        with self.assertRaisesRegex(RuleError, "난이도 상관없이"):
            check_join(self.db, CFG, s2, self.a1, self.now)


class RaidOverrideTest(unittest.TestCase):
    def test_override_changes_values_and_plan(self):
        db = Database(":memory:")
        db.set_raid_override("final_hard", 1, 0, gold=10000)
        cfg = apply_overrides(CFG, db.get_raid_overrides())
        self.assertEqual(cfg.raids["final_hard"].gold, 10000)
        self.assertTrue(cfg.raids["final_hard"].overridden)
        self.assertEqual(cfg.raids["final_hard"].min_item_level, 1730)  # 안 건드린 값 유지
        self.assertEqual(CFG.raids["final_hard"].gold, 48000)          # 원본 불변

        m = db.upsert_member(G, 1, "a")
        c, _ = db.upsert_character(m.id, "x", "바드", 1735)
        self.assertNotIn("종막 하드", [r.name for r in gold_raids(cfg, c)])

        db.set_raid_override("final_hard", 1, 0, min_item_level=1735)
        cfg = apply_overrides(CFG, db.get_raid_overrides())
        self.assertEqual((cfg.raids["final_hard"].gold, cfg.raids["final_hard"].min_item_level), (10000, 1735))

        self.assertTrue(db.clear_raid_override("final_hard"))
        self.assertEqual(apply_overrides(CFG, db.get_raid_overrides()).raids["final_hard"].gold, 48000)

    def test_unknown_raid_override_ignored(self):
        db = Database(":memory:")
        db.set_raid_override("deleted_raid", 1, 0, gold=1)
        self.assertEqual(apply_overrides(CFG, db.get_raid_overrides()).raids, CFG.raids)

    def test_config_groups_are_consistent(self):
        for r in CFG.raids.values():
            self.assertLessEqual(r.bound_gold, r.gold, r.key)
            self.assertGreater(r.gold, 0, r.key)
        # 난이도가 여러 개인 레이드는 같은 group 으로 묶여 있어야 한다
        self.assertEqual({r.group for r in CFG.raids.values() if r.name.startswith("세르카")}, {"serka"})


class SettingsTest(unittest.TestCase):
    def test_settings_roundtrip(self):
        db = Database(":memory:")
        self.assertIsNone(db.get_settings(G).notify_channel_id)
        db.update_settings(G, notify_channel_id=5, morning_time="08:30")
        db.update_settings(G, reminder_minutes=0)
        s = db.get_settings(G)
        self.assertEqual((s.notify_channel_id, s.morning_time, s.reminder_minutes), (5, "08:30", 0))
        with self.assertRaises(ValueError):
            db.update_settings(G, bogus=1)


if __name__ == "__main__":
    unittest.main()


class OpenEndTest(unittest.TestCase):
    def test_default_end(self):
        from bot.timeutil import default_end
        self.assertEqual(default_end(21 * 60), 26 * 60)  # 21:00~ → 익일 02:00
        self.assertEqual(default_end(2 * 60), 26 * 60)   # 딱 24시간
        self.assertEqual(default_end(1 * 60), 24 * 60)   # 24시간 넘으면 자정까지
        db = Database(":memory:")
        m = db.upsert_member(G, 1, "a")
        for start in range(0, 24 * 60, 60):
            db.set_availability(m.id, 0, start, default_end(start))  # 모든 시작 시각에서 유효
