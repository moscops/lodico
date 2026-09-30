"""tools/level_icons.html 을 브라우저로 렌더링한 DOM 출력(--dump-dom)에서 이미지를 꺼내 assets/levels/ 에 저장한다.

실행: python tools/save_level_icons.py <dom 출력 파일>
"""
from __future__ import annotations

import base64
import html
import json
import re
import sys
from pathlib import Path

OUT_DIR = Path(__file__).resolve().parent.parent / "assets" / "levels"
DATA_PREFIX = "data:image/png;base64,"


def main(dom_path: str) -> None:
    dom = Path(dom_path).read_text(encoding="utf-8")
    matches = re.findall(r'<pre id="out">(.*?)</pre>', dom, re.S)
    if not matches:
        raise SystemExit("DOM 에서 <pre id=\"out\"> 을 못 찾았어요.")
    images = json.loads(html.unescape(matches[-1]))
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    for name, url in images.items():
        if not url.startswith(DATA_PREFIX):
            raise SystemExit(f"{name}: PNG data URL 이 아니에요.")
        (OUT_DIR / f"{name}.png").write_bytes(base64.b64decode(url[len(DATA_PREFIX):]))
    print(f"{len(images)}개 저장: {OUT_DIR}")


if __name__ == "__main__":
    if len(sys.argv) != 2:
        raise SystemExit(__doc__)
    main(sys.argv[1])
