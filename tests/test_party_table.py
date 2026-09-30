import re
import unittest
from datetime import datetime

from bot.classicons import CLASS_SHORT, EMOJI_NAMES, class_short
from bot.config import ROOT, load_config
from bot.db import Character, Database, Session
from bot.render import COL_WIDTH, _width, overview_text, party_lines
from bot.services import SessionView, build_overview
from bot.timeutil import KST, to_epoch

CFG = load_config(ROOT / "config.toml")
ANSI = re.compile(r"\x1b\[[0-9;]*m")
RED, GREEN = "\x1b[31m", "\x1b[32m"


def plain(text: str) -> str:
    return ANSI.sub("", text)


def make(idx: int, cls: str, name: str | None = None, member: str | None = None,
         level: float = 1745, party: int | None = None) -> Character:
    return Character(idx, idx, name or f"캐릭{idx}", cls, level, party_no=party,
                     discord_id=idx, display_name=member or f"공대원{idx}")


def view(raid_key: str, chars: list[Character]) -> SessionView:
    session = Session(1, 1, raid_key, 0, "open", None, 0, False)
    return SessionView(session, CFG.raid(raid_key), chars)


def table_rows(lines: list[str]) -> list[str]:
    assert lines[0] == "```ansi"
    return lines[1:lines.index("```")]


class ClassShortTest(unittest.TestCase):
    def test_every_class_has_a_short_label(self):
        self.assertEqual(set(CLASS_SHORT), set(EMOJI_NAMES))
        for label in CLASS_SHORT.values():
            self.assertLessEqual(len(label), 2)

    def test_alias_and_unknown_class(self):
        self.assertEqual(class_short("홀나"), "홀나")
        self.assertEqual(class_short("워로드"), "워로")
        self.assertEqual(class_short("새직업"), "새직")


class PartyTableTest(unittest.TestCase):
    def eight(self, **override) -> list[Character]:
        chars = [make(i, "블레이드", party=1 if i < 5 else 2) for i in range(1, 9)]
        chars[3] = make(4, "바드", party=1)
        chars[7] = make(8, "홀리나이트", party=2)
        for key, char in override.items():
            chars[int(key[1:]) - 1] = char
        return chars

    def test_two_parties_side_by_side(self):
        rows = table_rows(party_lines(CFG, view("final_hard", self.eight())))
        header, body = plain(rows[0]), [plain(r) for r in rows[1:]]
        self.assertTrue(header.startswith("1파티"))
        self.assertIn("2파티", header)
        self.assertEqual(len(body), 4)
        for row in body:
            left, right = row.split(" | ")
            self.assertEqual(_width(left), COL_WIDTH)
            self.assertTrue(right)

    def test_first_party_left_second_party_right(self):
        chars = self.eight(c1=make(1, "워로드", "왼쪽캐", "왼사람", party=1),
                           c5=make(5, "워로드", "오른캐", "오른사람", party=2))
        body = [plain(r) for r in table_rows(party_lines(CFG, view("final_hard", chars)))[1:]]
        left, right = body[0].split(" | ")
        self.assertIn("왼쪽캐", left)
        self.assertIn("오른캐", right)

    def test_row_format_class_name_member_level(self):
        chars = self.eight(c1=make(1, "브레이커", "풍월문", "이동현", 1784, party=1))
        first = plain(table_rows(party_lines(CFG, view("final_hard", chars)))[1]).split(" | ")[0]
        self.assertEqual(first.split(), ["브커", "풍월문(이동현)", "1784"])

    def test_only_item_level_is_colored_red_for_dealer_green_for_support(self):
        chars = self.eight(c1=make(1, "브레이커", "풍월문", "이동현", 1784, party=1))
        rows = table_rows(party_lines(CFG, view("final_hard", chars)))
        dealer = rows[1].split(" | ")[0]
        self.assertTrue(dealer.endswith(f"{RED}1784\x1b[0m"))
        self.assertEqual(dealer.count("\x1b["), 2)  # 레벨 앞 색, 레벨 뒤 초기화만
        bard = rows[4].split(" | ")[0]
        self.assertIn(f"{GREEN}1745\x1b[0m", bard)

    def test_very_long_names_keep_second_column_aligned(self):
        chars = self.eight(
            c1=make(1, "워로드", "아주아주아주긴캐릭터이름입니다", "굉장히긴공대원이름", party=1),
            c6=make(6, "워로드", "짧", "김", party=2),
        )
        body = [plain(r) for r in table_rows(party_lines(CFG, view("final_hard", chars)))[1:]]
        self.assertEqual({_width(row.split(" | ")[0]) for row in body}, {COL_WIDTH})
        self.assertIn("…", body[0])
        self.assertLessEqual(max(_width(row) for row in body), COL_WIDTH * 2 + 3)

    def test_decimal_item_levels_do_not_break_alignment(self):
        chars = self.eight(c1=make(1, "워로드", level=1745.83, party=1))
        body = [plain(r) for r in table_rows(party_lines(CFG, view("final_hard", chars)))[1:]]
        self.assertEqual({_width(row.split(" | ")[0]) for row in body}, {COL_WIDTH})
        self.assertIn("1745.83", body[0])

    def test_four_person_raid_is_single_column_without_header(self):
        chars = [make(i, "블레이드", party=1) for i in range(1, 4)]
        rows = table_rows(party_lines(CFG, view("serka_nightmare", chars)))
        self.assertEqual(len(rows), 3)
        self.assertTrue(all(" | " not in r and "파티" not in plain(r) for r in rows))

    def test_members_without_party_are_listed_under_own_header(self):
        chars = self.eight()
        chars[6] = make(7, "블레이드", party=None)
        rows = [plain(r) for r in table_rows(party_lines(CFG, view("final_hard", chars)))]
        self.assertTrue(any(r.startswith("파티 미정") for r in rows))

    def test_empty_session_has_no_lines(self):
        self.assertEqual(party_lines(CFG, view("final_hard", [])), [])

    def test_warnings_stay_outside_the_code_block(self):
        chars = [make(i, "블레이드", party=1 if i < 5 else 2) for i in range(1, 9)]  # 서폿 없음
        lines = party_lines(CFG, view("final_hard", chars))
        self.assertTrue(lines[lines.index("```") + 1].startswith("-# "))

    def test_overview_text_closes_code_fence_even_when_truncated(self):
        db = Database(":memory:")
        now = datetime(2026, 9, 30, 12, tzinfo=KST)
        for i in range(40):
            member = db.upsert_member(1, 900 + i, f"공대원{i}")
            char, _ = db.upsert_character(member.id, f"긴이름의캐릭터{i}", "워로드", 1745)
            when = datetime(2026, 10, 1 + i % 5, 10 + i % 12, tzinfo=KST)
            session = db.create_session(1, "final_hard", to_epoch(when), 1, None)
            db.add_slot(session.id, char.id)
        text = overview_text(CFG, build_overview(db, CFG, 1, now), now)
        self.assertEqual(text.count("```") % 2, 0)


if __name__ == "__main__":
    unittest.main()
