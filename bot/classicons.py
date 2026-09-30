"""직업 아이콘 이모지. 봇 전용(애플리케이션) 이모지로 올려 두고, 메시지에는 '<:이름:id>' 로 쓴다.

이모지가 없으면(업로드 실패 등) 빈 문자열을 돌려주고, 렌더링은 예전처럼 직업 이름 글자로 대신한다.
"""
from __future__ import annotations

import logging
from typing import Any

from .config import ROOT

log = logging.getLogger("raidbot")

ICON_DIR = ROOT / "assets" / "classes"

# 이모지 이름은 영문/숫자/_ 만 가능해서 한글 직업명(=이미지 파일 이름)을 영문 이름으로 바꾼다.
EMOJI_NAMES = {
    "디스트로이어": "cls_destroyer", "워로드": "cls_warlord", "버서커": "cls_berserker",
    "홀리나이트": "cls_paladin", "슬레이어": "cls_slayer", "발키리": "cls_valkyrie",
    "가디언나이트": "cls_guardianknight",
    "바드": "cls_bard", "서머너": "cls_summoner", "아르카나": "cls_arcana", "소서리스": "cls_sorceress",
    "배틀마스터": "cls_battlemaster", "인파이터": "cls_infighter", "기공사": "cls_soulmaster",
    "창술사": "cls_lancemaster", "스트라이커": "cls_striker", "브레이커": "cls_breaker",
    "데빌헌터": "cls_devilhunter", "블래스터": "cls_blaster", "호크아이": "cls_hawkeye",
    "스카우터": "cls_scouter", "건슬링어": "cls_gunslinger",
    "블레이드": "cls_blade", "데모닉": "cls_demonic", "리퍼": "cls_reaper", "소울이터": "cls_souleater",
    "도화가": "cls_artist", "기상술사": "cls_aeromancer", "환수사": "cls_wildsoul", "차원술사": "cls_dimension",
}

# 파티 표(코드블록)처럼 이모지를 못 쓰는 곳에서 직업 대신 적는 두 글자 약칭 (공대에서 흔히 쓰는 말)
CLASS_SHORT = {
    "디스트로이어": "디트", "워로드": "워로", "버서커": "버서", "홀리나이트": "홀나", "슬레이어": "슬레",
    "발키리": "발키", "가디언나이트": "가나", "바드": "바드", "서머너": "서머", "아르카나": "알카",
    "소서리스": "소서", "배틀마스터": "배마", "인파이터": "인파", "기공사": "기공", "창술사": "창술",
    "스트라이커": "스커", "브레이커": "브커", "데빌헌터": "데헌", "블래스터": "블래", "호크아이": "호크",
    "스카우터": "스카", "건슬링어": "건슬", "블레이드": "블레", "데모닉": "데모", "리퍼": "리퍼",
    "소울이터": "소이", "도화가": "도화", "기상술사": "기상", "환수사": "환수", "차원술사": "차원",
}

# 시트·입력에서 줄임말로 적힌 직업 (config.toml 의 support_classes 줄임말과 같은 것들)
ALIASES = {"홀나": "홀리나이트", "도화": "도화가", "발키": "발키리", "블레": "블레이드", "알카": "아르카나",
           "기상": "기상술사", "인파": "인파이터"}

# 골드 아이콘도 같은 방식으로 올린다 (직업 이름과 겹치지 않는 키로 _icons 에 함께 보관)
GOLD_KEY = "@골드"
GOLD_EMOJI = "lodico_gold"
GOLD_PATH = ROOT / "assets" / "gold.png"

# 아이템 레벨용 색 숫자(딜러 빨강 r / 서폿 초록 g)와 역할 아이콘. 디스코드는 글자 색을 못 입혀서 숫자를 이모지로 만든다.
# 파일 이름(assets/levels/<stem>.png) → 이모지 이름: digit_r7 → lv_r7, role_dealer → role_dealer
LEVEL_DIR = ROOT / "assets" / "levels"
LEVEL_STEMS = [f"digit_{tone}{d}" for tone in "rg" for d in (*"0123456789", "dot")] + ["role_dealer", "role_support"]
ROLE_FALLBACK = {"role_dealer": "⚔️", "role_support": "➕"}


def level_emoji_name(stem: str) -> str:
    return stem.replace("digit_", "lv_", 1)

_icons: dict[str, str] = {}


def set_icons(icons: dict[str, str]) -> None:
    """직업명 → 이모지 표기. 통째로 교체한다."""
    global _icons
    _icons = dict(icons)


def class_icon(cls: str) -> str:
    return _icons.get(ALIASES.get(cls, cls), "")


def role_icon(is_support: bool) -> str:
    """딜러 = 빨강 검, 서폿 = 초록 +. 못 올렸으면 기본 이모지."""
    stem = "role_support" if is_support else "role_dealer"
    return _icons.get(stem, ROLE_FALLBACK[stem])


def level_text(level: float, is_support: bool) -> str:
    """아이템 레벨(소수점 버림)을 딜러 빨강 / 서폿 초록 숫자 이모지로. 하나라도 없으면 일반 숫자."""
    text = str(int(level))
    tone = "g" if is_support else "r"
    icons = [_icons.get(f"digit_{tone}{ch}") for ch in text]
    return "".join(icons) if all(icons) else text


def class_short(cls: str) -> str:
    """두 글자 약칭. 목록에 없는 직업은 앞 두 글자."""
    return CLASS_SHORT.get(ALIASES.get(cls, cls), cls[:2])


LEVELUP_KEY = "@레벨업"
LEVELUP_EMOJI = "lodico_levelup"
LEVELUP_PATH = ROOT / "assets" / "levelup3.png"


def levelup_mark() -> str:
    """레벨 상승 아이콘(/레벨갱신). 못 올렸으면 ⬆️."""
    return _icons.get(LEVELUP_KEY, "⬆️")


def gold_mark() -> str:
    """골드 아이콘. 못 올렸으면 💰."""
    return _icons.get(GOLD_KEY, "💰")


def char_head(cls: str, name: str) -> str:
    """캐릭터 이름 앞부분: 아이콘이 있으면 '아이콘 이름', 없으면 '이름 직업'."""
    icon = class_icon(cls)
    return f"{icon} {name}" if icon else f"{name} {cls}"


async def load_class_icons(bot: Any) -> None:
    """봇의 애플리케이션 이모지를 조회해서 없는 직업 아이콘만 assets/classes 에서 올린다. 실패해도 봇은 계속 동작."""
    try:
        existing = {e.name: e for e in await bot.fetch_application_emojis()}
    except Exception:
        log.exception("직업 아이콘: 이모지 목록을 못 가져왔어요. 직업 이름 글자로 표시합니다.")
        return

    wanted = [(cls, name, ICON_DIR / f"{cls}.png") for cls, name in EMOJI_NAMES.items()]
    wanted.append((GOLD_KEY, GOLD_EMOJI, GOLD_PATH))
    wanted.append((LEVELUP_KEY, LEVELUP_EMOJI, LEVELUP_PATH))
    wanted += [(stem, level_emoji_name(stem), LEVEL_DIR / f"{stem}.png") for stem in LEVEL_STEMS]
    icons: dict[str, str] = {}
    for key, name, path in wanted:
        emoji = existing.get(name)
        if emoji is None:
            if not path.exists():
                log.warning("아이콘: %s 이미지가 없어요 (%s)", key, path)
                continue
            try:
                emoji = await bot.create_application_emoji(name=name, image=path.read_bytes())
            except Exception:
                log.exception("아이콘: %s 업로드 실패", key)
                continue
            log.info("아이콘 업로드: %s", name)
        icons[key] = str(emoji)
    set_icons(icons)
    log.info("아이콘 %d/%d개 준비", len(icons), len(wanted))
