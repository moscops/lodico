import unittest
from datetime import datetime

from bot.classicons import LEVEL_DIR, LEVEL_STEMS, level_emoji_name, level_text, role_icon, set_icons
from bot.config import ROOT, load_config
from bot.db import Database
from bot.render import FIELD_TEXT_CELLS, ZWSP, _width, overview_cards, pack_by_limits
from bot.services import build_overview
from bot.timeutil import KST, to_epoch

CFG = load_config(ROOT / "config.toml")
NOW = datetime(2026, 9, 30, 12, tzinfo=KST)
ALL_ICONS = {f"digit_{t}{d}": f"<:lv_{t}{d}:1>" for t in "rg" for d in "0123456789"}
ALL_ICONS |= {"role_dealer": "<:role_dealer:2>", "role_support": "<:role_support:3>"}


class LevelIconTest(unittest.TestCase):
    def tearDown(self):
        set_icons({})

    def test_every_level_image_exists_and_names_are_valid(self):
        for stem in LEVEL_STEMS:
            self.assertTrue((LEVEL_DIR / f"{stem}.png").exists(), stem)
            self.assertRegex(level_emoji_name(stem), r"^[A-Za-z0-9_]{2,32}$")
        self.assertEqual(len({level_emoji_name(s) for s in LEVEL_STEMS}), len(LEVEL_STEMS))

    def test_level_text_falls_back_to_plain_number(self):
        self.assertEqual(level_text(1784.83, False), "1784")

    def test_level_text_uses_red_digits_for_dealer_and_green_for_support(self):
        set_icons(ALL_ICONS)
        self.assertEqual(level_text(1784, False), "<:lv_r1:1><:lv_r7:1><:lv_r8:1><:lv_r4:1>")
        self.assertEqual(level_text(1721.9, True), "<:lv_g1:1><:lv_g7:1><:lv_g2:1><:lv_g1:1>")

    def test_level_text_is_plain_when_a_digit_icon_is_missing(self):
        set_icons({k: v for k, v in ALL_ICONS.items() if k != "digit_r8"})
        self.assertEqual(level_text(1784, False), "1784")

    def test_role_icon_and_fallback(self):
        self.assertEqual(role_icon(True), "➕")
        self.assertEqual(role_icon(False), "⚔️")
        set_icons(ALL_ICONS)
        self.assertEqual(role_icon(True), "<:role_support:3>")
        self.assertEqual(role_icon(False), "<:role_dealer:2>")


class Fixture(unittest.TestCase):
    def setUp(self):
        self.db = Database(":memory:")
        self.n = 0
        set_icons({**ALL_ICONS, "브레이커": "<:cls_breaker:4>", "바드": "<:cls_bard:5>"})

    def tearDown(self):
        set_icons({})

    def join(self, session, cls, name, member, level, party):
        self.n += 1
        m = self.db.upsert_member(1, 100 + self.n, member)
        c, _ = self.db.upsert_character(m.id, name, cls, level)
        self.db.add_slot(session.id, c.id, party)
        return c

    def session(self, raid, day=1, hour=21):
        return self.db.create_session(1, raid, to_epoch(datetime(2026, 10, day, hour, tzinfo=KST)), 1, None)

    def cards(self):
        return overview_cards(CFG, build_overview(self.db, CFG, 1, NOW), NOW)


class SessionCardTest(Fixture):
    def eight_person(self):
        s = self.session("final_hard")
        self.join(s, "브레이커", "풍월문", "이동현", 1784, 1)
        self.join(s, "바드", "로바드", "철수", 1721, 1)
        self.join(s, "브레이커", "민수딜", "민수", 1700, 2)
        return s

    def test_title_has_date_time_raid_filled_and_id(self):
        s = self.eight_person()
        card = self.cards().sessions[0]
        self.assertIn("10/1(목) 21:00", card.title)
        self.assertIn("3/8", card.title)
        self.assertTrue(card.title.endswith(f"#{s.id}"))

    def test_party_one_is_left_and_party_two_is_right_as_inline_fields(self):
        self.eight_person()
        fields = self.cards().sessions[0].fields
        self.assertEqual([f[0] for f in fields[:2]], ["1파티", "2파티"])
        self.assertTrue(all(f[2] for f in fields[:2]))
        self.assertEqual(fields[2], (ZWSP, ZWSP, True))  # 세 번째 칸은 비워 3·4파티가 다음 줄로 가게

    def test_member_takes_two_lines_class_icon_name_then_role_icon_and_colored_level(self):
        self.eight_person()
        left = self.cards().sessions[0].fields[0][1].split("\n")
        self.assertEqual(left[0], "<:cls_breaker:4> 풍월문(이동현)")
        self.assertEqual(left[1], "<:role_dealer:2><:lv_r1:1><:lv_r7:1><:lv_r8:1><:lv_r4:1>")
        self.assertEqual(left[3], "<:role_support:3><:lv_g1:1><:lv_g7:1><:lv_g2:1><:lv_g1:1>")  # 서폿은 초록

    def test_long_names_are_shortened_so_both_columns_stay_the_same_shape(self):
        s = self.session("final_hard")
        self.join(s, "브레이커", "아주아주아주긴캐릭터이름입니다", "굉장히긴공대원이름", 1745, 1)
        self.join(s, "브레이커", "짧", "김", 1745, 2)
        for _, value, _ in self.cards().sessions[0].fields[:2]:
            head = value.split("\n")[0].split(" ", 1)[1]
            self.assertLessEqual(_width(head), FIELD_TEXT_CELLS)
            self.assertEqual(len(value.split("\n")), 2)

    def test_four_person_raid_uses_one_full_width_field_with_one_line_rows(self):
        s = self.session("serka_nightmare")
        self.join(s, "브레이커", "풍월문", "이동현", 1784, 1)
        (field,) = self.cards().sessions[0].fields
        self.assertFalse(field[2])
        self.assertEqual(field[1], "<:cls_breaker:4> 풍월문(이동현) <:role_dealer:2><:lv_r1:1><:lv_r7:1><:lv_r8:1><:lv_r4:1>")

    def test_members_without_party_get_their_own_field(self):
        s = self.session("final_hard")
        self.join(s, "브레이커", "풍월문", "이동현", 1784, None)
        names = [f[0] for f in self.cards().sessions[0].fields]
        self.assertIn("파티 미정", names)

    def test_field_values_fit_discord_limit_even_with_many_members(self):
        s = self.session("final_hard")
        for i in range(4):
            self.join(s, "바드", f"긴캐릭터{i}", f"공대원{i}", 1745, 1)
            self.join(s, "브레이커", f"긴캐릭터{i}", f"공대원{i}", 1745, 2)
        for _, value, _ in self.cards().sessions[0].fields:
            self.assertLessEqual(len(value), 1024)

    def test_warnings_go_to_description(self):
        s = self.session("final_hard")
        for i in range(4):
            self.join(s, "브레이커", f"딜{i}", f"공대원{i}", 1745, 1)
        self.assertIn("⚠️", self.cards().sessions[0].description)

    def test_status_icons_are_gone_from_upcoming_sessions(self):
        self.eight_person()
        card = self.cards().sessions[0]
        self.assertNotIn("⏳", card.title)
        self.assertNotIn("🟢", card.title)

    def test_empty_week_has_a_notice(self):
        self.assertIn("등록된 일정이 없어요", self.cards().notice)

    def test_remaining_gold_raids_are_in_the_tail(self):
        m = self.db.upsert_member(1, 900, "로디코")
        self.db.upsert_character(m.id, "로워붕", "브레이커", 1730)
        self.assertIn("골드 레이드 남은 캐릭터", self.cards().tail)


class PackTest(unittest.TestCase):
    def test_groups_respect_char_and_count_limits(self):
        self.assertEqual(pack_by_limits([2000, 2000, 2000, 2000], max_chars=5800, max_items=10), [[0, 1], [2, 3]])
        self.assertEqual(pack_by_limits([1] * 12, max_chars=5800, max_items=10), [list(range(10)), [10, 11]])

    def test_oversized_item_gets_its_own_group(self):
        self.assertEqual(pack_by_limits([100, 7000, 100], max_chars=5800, max_items=10), [[0], [1], [2]])

    def test_empty(self):
        self.assertEqual(pack_by_limits([], 5800, 10), [])


if __name__ == "__main__":
    unittest.main()
