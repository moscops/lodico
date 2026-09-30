import unittest

from bot import classicons
from bot.classicons import EMOJI_NAMES, ICON_DIR, char_head, class_icon, set_icons


class ClassIconTest(unittest.TestCase):
    def tearDown(self):
        set_icons({})

    def test_every_emoji_name_is_valid_for_discord(self):
        for name in EMOJI_NAMES.values():
            self.assertRegex(name, r"^[A-Za-z0-9_]{2,32}$")
        self.assertEqual(len(set(EMOJI_NAMES.values())), len(EMOJI_NAMES))

    def test_every_class_has_an_image_file(self):
        for cls in EMOJI_NAMES:
            self.assertTrue((ICON_DIR / f"{cls}.png").exists(), cls)

    def test_no_stray_image_files(self):
        files = {p.stem for p in ICON_DIR.glob("*.png")}
        self.assertEqual(files, set(EMOJI_NAMES))

    def test_icons_fit_discord_limit(self):
        for path in ICON_DIR.glob("*.png"):
            self.assertLess(path.stat().st_size, 256 * 1024, path.name)

    def test_icons_are_colorless_white(self):
        import struct
        import zlib

        def first_opaque_pixel(path):
            data, pos, idat = path.read_bytes(), 8, b""
            while pos < len(data):
                length, kind = struct.unpack(">I4s", data[pos:pos + 8])
                if kind == b"IHDR":
                    width = struct.unpack(">I", data[pos + 8:pos + 12])[0]
                elif kind == b"IDAT":
                    idat += data[pos + 8:pos + 8 + length]
                pos += 12 + length
            raw = zlib.decompress(idat)
            stride = 1 + width * 4
            for i in range(0, len(raw), stride):
                row = raw[i + 1:i + stride]  # 필터 0 으로 저장한 PNG
                for x in range(0, len(row), 4):
                    if row[x + 3] > 200:
                        return tuple(row[x:x + 3])

        for cls in EMOJI_NAMES:
            self.assertEqual(first_opaque_pixel(ICON_DIR / f"{cls}.png"), (255, 255, 255), cls)

    def test_class_icon_is_empty_until_icons_are_loaded(self):
        self.assertEqual(class_icon("워로드"), "")

    def test_class_icon_returns_emoji_markup(self):
        set_icons({"워로드": "<:cls_warlord:123>"})
        self.assertEqual(class_icon("워로드"), "<:cls_warlord:123>")

    def test_class_icon_resolves_aliases(self):
        set_icons({"홀리나이트": "<:cls_paladin:1>"})
        self.assertEqual(class_icon("홀나"), "<:cls_paladin:1>")

    def test_unknown_class_has_no_icon(self):
        set_icons({"워로드": "<:cls_warlord:123>"})
        self.assertEqual(class_icon("없는직업"), "")
        self.assertEqual(class_icon(""), "")

    def test_char_head_puts_icon_before_name(self):
        set_icons({"워로드": "<:cls_warlord:123>"})
        self.assertEqual(char_head("워로드", "**로워붕**"), "<:cls_warlord:123> **로워붕**")

    def test_char_head_falls_back_to_class_text(self):
        self.assertEqual(char_head("워로드", "**로워붕**"), "**로워붕** 워로드")

    def test_gold_image_exists(self):
        self.assertTrue(classicons.GOLD_PATH.exists())
        self.assertLess(classicons.GOLD_PATH.stat().st_size, 256 * 1024)

    def test_gold_mark_falls_back_to_emoji(self):
        self.assertEqual(classicons.gold_mark(), "💰")

    def test_gold_mark_uses_uploaded_icon(self):
        set_icons({classicons.GOLD_KEY: "<:lodico_gold:9>"})
        self.assertEqual(classicons.gold_mark(), "<:lodico_gold:9>")

    def test_set_icons_replaces_previous_icons(self):
        set_icons({"워로드": "<:a:1>"})
        set_icons({"바드": "<:b:2>"})
        self.assertEqual(class_icon("워로드"), "")
        self.assertEqual(class_icon("바드"), "<:b:2>")


class RenderTest(unittest.TestCase):
    WARLORD = "<:cls_warlord:1>"

    def setUp(self):
        from bot.config import ROOT, load_config
        from bot.db import Character

        self.cfg = load_config(ROOT / "config.toml")
        self.warlord = Character(1, 1, "로워붕", "워로드", 1700, discord_id=5, display_name="로디코")
        self.bard = Character(2, 1, "로바드", "바드", 1690, discord_id=5, display_name="로디코")

    def tearDown(self):
        set_icons({})

    def test_character_line_uses_icon(self):
        from bot.render import character_line

        set_icons({"워로드": self.WARLORD})
        self.assertEqual(character_line(self.warlord), f"{self.WARLORD} **로워붕** 1700")

    def test_character_line_keeps_class_text_without_icon(self):
        from bot.render import character_line

        self.assertEqual(character_line(self.warlord), "**로워붕** 워로드 1700")

    def test_participants_block_uses_icon(self):
        from bot.render import participants_block

        set_icons({"워로드": self.WARLORD})
        self.assertEqual(participants_block([self.warlord]), f"• 로디코 — {self.WARLORD} 로워붕 (1700)")

    def test_participants_block_keeps_class_text_without_icon(self):
        from bot.render import participants_block

        self.assertEqual(participants_block([self.warlord]), "• 로디코 — 로워붕 (워로드 1700)")

    def test_gold_summary_uses_gold_icon(self):
        from bot.render import gold_summary_text
        from bot.services import GoldSummary

        summary = GoldSummary(gold=87500, bound=27500, more_cost=0, bound_after=27500, tradable_after=60000)
        self.assertTrue(gold_summary_text(summary).startswith("💰 주간 클골"))
        set_icons({classicons.GOLD_KEY: "<:lodico_gold:9>"})
        self.assertTrue(gold_summary_text(summary).startswith("<:lodico_gold:9> 주간 클골"))

    def test_member_label_uses_icon(self):
        from bot.render import _member_label

        set_icons({"바드": "<:cls_bard:2>"})
        self.assertEqual(_member_label(self.bard), "로디코(<:cls_bard:2> 로바드)")

    def test_member_label_has_no_support_shield(self):
        from bot.render import _member_label

        self.assertEqual(_member_label(self.bard), "로디코(로바드)")
        self.assertEqual(_member_label(self.warlord), "로디코(로워붕)")

    def test_remaining_gold_raids_list_uses_icons(self):
        from bot.render import overview_text
        from bot.services import build_overview
        from bot.timeutil import KST
        from datetime import datetime
        from bot.db import Database

        db = Database(":memory:")
        member = db.upsert_member(1, 100, "로디코")
        db.upsert_character(member.id, "로워붕", "워로드", 1730)
        set_icons({"워로드": self.WARLORD})
        ov = build_overview(db, self.cfg, 1, datetime(2026, 9, 30, 12, tzinfo=KST))
        text = overview_text(self.cfg, ov, datetime(2026, 9, 30, 12, tzinfo=KST))
        self.assertIn(f"로디코({self.WARLORD} 로워붕)", text)


class _Emoji:
    def __init__(self, name, id_):
        self.name, self.id = name, id_

    def __str__(self):
        return f"<:{self.name}:{self.id}>"


class LoadIconsTest(unittest.IsolatedAsyncioTestCase):
    def tearDown(self):
        set_icons({})

    async def test_uploads_only_missing_emojis(self):
        class FakeBot:
            def __init__(self):
                self.created = []

            async def fetch_application_emojis(self):
                return [_Emoji("cls_warlord", 7)]

            async def create_application_emoji(self, *, name, image):
                self.created.append(name)
                return _Emoji(name, 100 + len(self.created))

        bot = FakeBot()
        await classicons.load_class_icons(bot)

        self.assertNotIn("cls_warlord", bot.created)
        self.assertIn(classicons.GOLD_EMOJI, bot.created)
        # 직업 29개(워로드는 이미 있음) + 골드 1개 + 색 숫자·역할 아이콘
        self.assertEqual(len(bot.created), len(EMOJI_NAMES) + len(classicons.LEVEL_STEMS))
        self.assertEqual(class_icon("워로드"), "<:cls_warlord:7>")
        self.assertTrue(classicons.gold_mark().startswith("<:lodico_gold:"))
        self.assertTrue(class_icon("바드").startswith("<:cls_"))

    async def test_upload_failure_leaves_class_without_icon(self):
        class FakeBot:
            async def fetch_application_emojis(self):
                return []

            async def create_application_emoji(self, *, name, image):
                raise RuntimeError("boom")

        await classicons.load_class_icons(FakeBot())
        self.assertEqual(class_icon("워로드"), "")

    async def test_fetch_failure_does_not_raise(self):
        class FakeBot:
            async def fetch_application_emojis(self):
                raise RuntimeError("boom")

        await classicons.load_class_icons(FakeBot())
        self.assertEqual(class_icon("워로드"), "")


if __name__ == "__main__":
    unittest.main()
