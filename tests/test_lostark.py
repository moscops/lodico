import sqlite3
import tempfile
import time
import unittest
from pathlib import Path

from aiohttp import web
from aiohttp.test_utils import TestServer

from bot import lostark_api
from bot.config import ROOT, load_config
from bot.db import Database
from bot.lostark_api import ApiCharacter, ApiError, LostArkClient, parse_item_level
from bot.render import sync_result_text
from bot.roster import apply_roster, min_raid_level

CFG = load_config(ROOT / "config.toml")


def api(name, level, cls="바드"):
    return ApiCharacter(name=name, server="루페온", cls=cls, item_level=level)


class ParseTest(unittest.TestCase):
    def test_item_level(self):
        self.assertEqual(parse_item_level("1,720.83"), 1720.83)
        self.assertEqual(parse_item_level("1,640.00"), 1640.0)
        self.assertEqual(parse_item_level(None), 0.0)


class ApplyRosterTest(unittest.TestCase):
    def setUp(self):
        self.db = Database(":memory:")
        self.m = self.db.upsert_member(1, 100, "철수")
        self.db.upsert_character(self.m.id, "본캐", "바드", 1725)
        self.db.upsert_character(self.m.id, "부캐", "워로드", 1700)

    def levels(self):
        return {c.name: c.item_level for c in self.db.list_characters(self.m.id)}

    def test_first_import_adds_only_above_min_level(self):
        db = Database(":memory:")
        m = db.upsert_member(1, 1, "a")
        roster = [api("A", 1740), api("B", 1650), api("C", 1500)]
        result = apply_roster(db, CFG, m.id, roster, min_raid_level(CFG))
        self.assertEqual(sorted(c.name for c in db.list_characters(m.id)), ["A", "B"])
        self.assertTrue(all(c.old_level is None for c in result.changes))

    def test_level_up_changes_gold_plan(self):
        roster = [api("본캐", 1731), api("부캐", 1700, "워로드")]
        result = apply_roster(self.db, CFG, self.m.id, roster, 1640)
        self.assertEqual(self.levels()["본캐"], 1731)
        self.assertEqual([c.name for c in result.changes], ["본캐"])
        change = result.changes[0]
        self.assertEqual((change.old_level, change.new_level), (1725, 1731))
        self.assertTrue(change.gold_changed)  # 1730 을 넘어 종막 하드/세르카 하드 입장 가능
        self.assertIn("종막 하드", [r.name for r in change.gold_after])
        self.assertIn("골드 레이드 변경", sync_result_text(result))

    def test_stale_pinned_tier_is_released_when_higher_auto_tier_opens(self):
        # 1730+ 로 고정해 둔 캐릭터의 레벨이 1750 이 되면 고정을 풀어 1750+ 를 따라간다 (레벨이 같아도 갱신 때 검사)
        char = self.db.list_characters(self.m.id)[0]
        self.db.update_character_info(char.id, char.cls, 1750)
        self.db.set_character_tier(char.id, "1730+")
        apply_roster(self.db, CFG, self.m.id, [api(char.name, 1750)], 1640)
        self.assertIsNone(next(c for c in self.db.list_characters(self.m.id) if c.id == char.id).tier)

    def test_same_level_variant_tier_is_kept(self):
        char = self.db.list_characters(self.m.id)[0]
        self.db.update_character_info(char.id, char.cls, 1750)
        self.db.set_character_tier(char.id, "1750+_3")
        apply_roster(self.db, CFG, self.m.id, [api(char.name, 1750)], 1640)
        self.assertEqual(next(c for c in self.db.list_characters(self.m.id) if c.id == char.id).tier, "1750+_3")

    def test_level_up_removes_character_from_dropped_raid_sessions(self):
        # 1725(1720+: 종막 노말·지평의 성당 2단계·4막 하드) → 1731(1730+: 종막 하드…)이 되면 종막 노말은 안 간다
        from datetime import datetime
        from bot.timeutil import KST, to_epoch
        char = next(c for c in self.db.list_characters(self.m.id) if c.name == "본캐")
        soon = to_epoch(datetime(2026, 10, 3, 21, tzinfo=KST))
        past = to_epoch(datetime(2026, 9, 1, 21, tzinfo=KST))
        s_normal = self.db.create_session(1, "final_normal", soon, 1, "")
        s_past = self.db.create_session(1, "final_normal", past, 1, "")
        s_keep = self.db.create_session(1, "cathedral_2", soon, 1, "")
        for s in (s_normal, s_past, s_keep):
            self.db.add_slot(s.id, char.id)
        now = datetime(2026, 9, 30, 12, tzinfo=KST)
        result = apply_roster(self.db, CFG, self.m.id, [api("본캐", 1731), api("부캐", 1700, "워로드")], 1640, now=now)
        left = lambda s: [c.name for c in self.db.session_characters(s.id)]
        self.assertEqual(left(s_normal), [])            # 종막 노말 (시작 전) → 빠짐
        self.assertEqual(left(s_past), ["본캐"])         # 이미 지난 일정은 그대로
        self.assertEqual(left(s_keep), ["본캐"])         # 계속 가는 레이드는 그대로
        change = next(c for c in result.changes if c.name == "본캐")
        self.assertEqual(change.removed_from, ["10/3(토) 종막 노말"])
        self.assertIn("일정에서 빠짐", sync_result_text(result))

    def test_level_up_removes_character_from_fixed_party_raid(self):
        char = next(c for c in self.db.list_characters(self.m.id) if c.name == "본캐")
        other = next(c for c in self.db.list_characters(self.m.id) if c.name == "부캐")
        self.db.replace_parties(1, [{"number": 1, "label": None, "raids": [
            {"raid_key": "final_normal", "weekday": 5, "start_min": 21 * 60, "character_ids": [char.id, other.id]},
            {"raid_key": "cathedral_2", "weekday": 5, "start_min": 22 * 60, "character_ids": [char.id]},
        ]}])
        result = apply_roster(self.db, CFG, self.m.id, [api("본캐", 1731), api("부캐", 1700, "워로드")], 1640)
        raids = {r.raid_key: [c.name for c in r.characters] for r in self.db.list_parties(1)[0].raids}
        self.assertEqual(raids["final_normal"], ["부캐"])   # 종막 노말 줄에서만 빠짐
        self.assertEqual(raids["cathedral_2"], ["본캐"])
        change = next(c for c in result.changes if c.name == "본캐")
        self.assertIn("고정 파티 1 종막 노말", change.removed_from)

    def test_no_change(self):
        roster = [api("본캐", 1725), api("부캐", 1700, "워로드")]
        result = apply_roster(self.db, CFG, self.m.id, roster, 1640)
        self.assertEqual(result.changes, [])
        self.assertEqual(sync_result_text(result), "")

    def test_new_character_pushes_one_out_of_gold_six(self):
        for i in range(4):
            self.db.upsert_character(self.m.id, f"캐{i}", "바드", 1680 - i)
        # 현재 6캐릭: 본캐 1725, 부캐 1700, 캐0~3 (1680~1677). 새 캐릭 1690 → 캐3 가 골드에서 빠짐
        roster = [api(c.name, c.item_level, c.cls) for c in self.db.list_characters(self.m.id)]
        roster.append(api("신캐", 1690))
        result = apply_roster(self.db, CFG, self.m.id, roster, 1640)
        by_name = {c.name: c for c in result.changes}
        self.assertIsNone(by_name["신캐"].old_level)
        self.assertEqual(by_name["캐3"].gold_after, [])
        self.assertIn("골드 획득 해제", sync_result_text(result))

    def test_auto_add_off_and_missing(self):
        roster = [api("본캐", 1725), api("신캐", 1690)]
        result = apply_roster(self.db, CFG, self.m.id, roster, 1640, add_new=False)
        self.assertNotIn("신캐", self.levels())
        self.assertEqual(result.missing, ["부캐"])

    def test_zero_level_from_api_is_ignored(self):
        result = apply_roster(self.db, CFG, self.m.id, [api("본캐", 0)], 1640)
        self.assertEqual(self.levels()["본캐"], 1725)
        self.assertEqual(result.changes, [])


class MigrationTest(unittest.TestCase):
    def test_old_db_gets_new_columns(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "old.db"
            conn = sqlite3.connect(path)
            conn.execute(
                "CREATE TABLE members (id INTEGER PRIMARY KEY, guild_id INTEGER NOT NULL, "
                "discord_id INTEGER NOT NULL, display_name TEXT NOT NULL, UNIQUE (guild_id, discord_id))"
            )
            conn.execute("INSERT INTO members (guild_id, discord_id, display_name) VALUES (1, 2, 'x')")
            conn.commit()
            conn.close()

            db = Database(path)
            m = db.get_member(1, 2)
            self.assertIsNone(m.roster_name)
            db.set_roster(m.id, "본캐")
            db.mark_synced(m.id, 123)
            linked = db.list_linked_members(1)
            self.assertEqual((linked[0].roster_name, linked[0].synced_at), ("본캐", 123))
            db.close()


class ClientTest(unittest.IsolatedAsyncioTestCase):
    """로컬 가짜 서버로 HTTP 처리(인증 헤더, 429 재시도, 오류 코드)를 확인한다."""

    async def asyncSetUp(self):
        self.calls = 0
        self.mode = "ok"

        async def siblings(request: web.Request):
            self.calls += 1
            self.auth = request.headers.get("authorization")
            if self.mode == "429" and self.calls == 1:
                return web.Response(status=429, headers={"X-RateLimit-Reset": str(int(time.time()))})
            if self.mode == "401":
                return web.Response(status=401)
            if self.mode == "503":
                return web.Response(status=503)
            if request.match_info["name"] == "없는캐릭":
                return web.Response(text="null", content_type="application/json")
            return web.json_response([
                {"ServerName": "루페온", "CharacterName": request.match_info["name"], "CharacterLevel": 70,
                 "CharacterClassName": "바드", "ItemAvgLevel": "1,720.83"},
                {"ServerName": "루페온", "CharacterName": "부캐", "CharacterLevel": 70,
                 "CharacterClassName": "워로드", "ItemAvgLevel": "1,680.00"},
            ])

        app = web.Application()
        app.router.add_get("/characters/{name}/siblings", siblings)
        self.server = TestServer(app)
        await self.server.start_server()
        self._base, self._interval = lostark_api.BASE_URL, lostark_api.MIN_INTERVAL
        lostark_api.BASE_URL = str(self.server.make_url("")).rstrip("/")
        lostark_api.MIN_INTERVAL = 0
        self.client = LostArkClient("test-key")

    async def asyncTearDown(self):
        await self.client.close()
        await self.server.close()
        lostark_api.BASE_URL, lostark_api.MIN_INTERVAL = self._base, self._interval

    async def test_siblings_parses_korean_name_and_level(self):
        chars = await self.client.siblings("달빛바드")
        self.assertEqual(self.auth, "bearer test-key")
        self.assertEqual(chars[0], ApiCharacter("달빛바드", "루페온", "바드", 1720.83))
        self.assertEqual(chars[1].item_level, 1680.0)

    async def test_not_found_returns_empty(self):
        self.assertEqual(await self.client.siblings("없는캐릭"), [])

    async def test_429_waits_and_retries_once(self):
        self.mode = "429"
        chars = await self.client.siblings("달빛바드")
        self.assertEqual(self.calls, 2)
        self.assertEqual(len(chars), 2)

    async def test_fatal_errors(self):
        for mode, text in (("401", "API 키"), ("503", "점검")):
            self.mode = mode
            with self.assertRaisesRegex(ApiError, text) as ctx:
                await self.client.siblings("달빛바드")
            self.assertTrue(ctx.exception.fatal)


if __name__ == "__main__":
    unittest.main()
