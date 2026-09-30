"""직업 아이콘 원본(assets/classes_src, 1600px)을 디스코드 이모지 한도(256KB)에 맞게 160px 흰색 PNG 로 줄여 assets/classes 에 만든다.

서폿/딜러 구분은 색이 아니라 파티 표의 아이템 레벨 색(초록/빨강)으로 한다.
표준 라이브러리만 쓴다.   실행: python tools/make_class_icons.py
"""
from __future__ import annotations

import struct
import sys
import zlib
from multiprocessing import Pool
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from bot.classicons import EMOJI_NAMES  # noqa: E402

SRC_DIR = ROOT / "assets" / "classes_src"
OUT_DIR = ROOT / "assets" / "classes"
SIZE = 160
ICON_COLOR = (255, 255, 255)
SAMPLE_STEP = 2  # 한 칸(10x10)에서 2픽셀 간격으로 평균


def read_alpha(path: Path) -> tuple[int, int, list[bytes]]:
    """RGBA 8비트 PNG 에서 알파 채널만 줄 단위로 읽는다."""
    data = path.read_bytes()
    pos, idat = 8, b""
    while pos < len(data):
        length, kind = struct.unpack(">I4s", data[pos:pos + 8])
        chunk = data[pos + 8:pos + 8 + length]
        pos += 12 + length
        if kind == b"IHDR":
            width, height, depth, color = struct.unpack(">IIBB", chunk[:10])
            if (depth, color) != (8, 6):
                raise ValueError(f"{path.name}: RGBA 8비트 PNG 만 지원해요")
        elif kind == b"IDAT":
            idat += chunk
    raw = zlib.decompress(idat)
    stride, bpp = width * 4, 4
    prev = bytearray(stride)
    alphas, offset = [], 0
    for _ in range(height):
        kind, line = raw[offset], bytearray(raw[offset + 1:offset + 1 + stride])
        offset += 1 + stride
        if kind == 1:
            for i in range(bpp, stride):
                line[i] = (line[i] + line[i - bpp]) & 255
        elif kind == 2:
            line = bytearray((a + b) & 255 for a, b in zip(line, prev))
        elif kind == 3:
            for i in range(stride):
                left = line[i - bpp] if i >= bpp else 0
                line[i] = (line[i] + ((left + prev[i]) >> 1)) & 255
        elif kind == 4:
            for i in range(stride):
                a = line[i - bpp] if i >= bpp else 0
                b, c = prev[i], prev[i - bpp] if i >= bpp else 0
                pa, pb, pc = abs(b - c), abs(a - c), abs(a + b - 2 * c)
                pred = a if pa <= pb and pa <= pc else (b if pb <= pc else c)
                line[i] = (line[i] + pred) & 255
        alphas.append(bytes(line[3::4]))
        prev = line
    return width, height, alphas


def shrink(width: int, height: int, alphas: list[bytes]) -> list[list[int]]:
    """알파를 SIZE x SIZE 로 평균내어 줄인다."""
    step_x, step_y = width // SIZE, height // SIZE
    out = []
    for ty in range(SIZE):
        row = []
        for tx in range(SIZE):
            total = count = 0
            for dy in range(0, step_y, SAMPLE_STEP):
                line = alphas[ty * step_y + dy]
                for dx in range(0, step_x, SAMPLE_STEP):
                    total += line[tx * step_x + dx]
                    count += 1
            row.append(total // count)
        out.append(row)
    return out


def encode_png(pixels: list[list[int]], color: tuple[int, int, int]) -> bytes:
    """단색 + 알파 RGBA PNG."""
    rgb = bytes(color)
    raw = b"".join(b"\x00" + b"".join(rgb + bytes([a]) for a in row) for row in pixels)

    def chunk(kind: bytes, body: bytes) -> bytes:
        return struct.pack(">I", len(body)) + kind + body + struct.pack(">I", zlib.crc32(kind + body))

    header = struct.pack(">IIBBBBB", SIZE, SIZE, 8, 6, 0, 0, 0)
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", header) + chunk(b"IDAT", zlib.compress(raw, 9)) + chunk(b"IEND", b"")


def make_icon(cls: str) -> str:
    width, height, alphas = read_alpha(SRC_DIR / f"{cls}.png")
    png = encode_png(shrink(width, height, alphas), ICON_COLOR)
    (OUT_DIR / f"{cls}.png").write_bytes(png)
    return f"{cls}: {len(png) // 1024}KB"


def main() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    with Pool() as pool:
        for line in pool.map(make_icon, list(EMOJI_NAMES)):
            print(line)


if __name__ == "__main__":
    main()
