"""Generate the PNG app icons (no image libraries needed). Run: python tools/make_icons.py"""
import struct
import zlib
from pathlib import Path

OUT = Path(__file__).resolve().parent.parent / "app" / "static" / "icons"
TEAL, WHITE, LIGHT, AMBER = (15, 118, 110), (255, 255, 255), (153, 213, 207), (245, 158, 11)


def rrect(x, y, x0, y0, x1, y1, r):
    if not (x0 <= x <= x1 and y0 <= y <= y1):
        return False
    cx = min(max(x, x0 + r), x1 - r)
    cy = min(max(y, y0 + r), y1 - r)
    return (x - cx) ** 2 + (y - cy) ** 2 <= r * r


def seg(px, py, ax, ay, bx, by, w):
    dx, dy = bx - ax, by - ay
    t = max(0, min(1, ((px - ax) * dx + (py - ay) * dy) / (dx * dx + dy * dy)))
    return (px - ax - t * dx) ** 2 + (py - ay - t * dy) ** 2 <= (w / 2) ** 2


def pixel(u, v, maskable):
    """u, v in 0..64 icon space."""
    if maskable:  # full-bleed background, content scaled into the safe zone
        u, v = 32 + (u - 32) / 0.78, 32 + (v - 32) / 0.78
    elif not rrect(u, v, 0, 0, 64, 64, 14):
        return None
    col = TEAL
    if rrect(u, v, 15, 11, 45, 49, 4):
        col = WHITE
        if rrect(u, v, 20, 18, 40, 21, 1.5):
            col = TEAL
        elif rrect(u, v, 20, 25, 36, 28, 1.5) or rrect(u, v, 20, 31, 32, 34, 1.5):
            col = LIGHT
    d2 = (u - 44) ** 2 + (v - 44) ** 2
    if d2 <= 12.5 ** 2:
        col = TEAL
    if d2 <= 9.5 ** 2:
        col = AMBER
        if seg(u, v, 44, 38, 44, 44.5, 2.6) or seg(u, v, 44, 44.5, 48, 47, 2.6):
            col = WHITE
    return col


def render(size, maskable=False, ss=3):
    rows = []
    for y in range(size):
        row = bytearray([0])
        for x in range(size):
            acc = [0, 0, 0, 0]
            for sy in range(ss):
                for sx in range(ss):
                    u = (x + (sx + 0.5) / ss) * 64 / size
                    v = (y + (sy + 0.5) / ss) * 64 / size
                    c = pixel(u, v, maskable)
                    if c:
                        acc[0] += c[0]; acc[1] += c[1]; acc[2] += c[2]; acc[3] += 255
            n = ss * ss
            a = acc[3] / n
            if a:
                row += bytes([round(acc[0] * 255 / acc[3]), round(acc[1] * 255 / acc[3]), round(acc[2] * 255 / acc[3]), round(a)])
            else:
                row += bytes(4)
        rows.append(bytes(row))
    raw = b"".join(rows)

    def chunk(tag, data):
        return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)

    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", size, size, 8, 6, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw, 9)) + chunk(b"IEND", b""))


if __name__ == "__main__":
    OUT.mkdir(parents=True, exist_ok=True)
    for name, size, mask in (("icon-180.png", 180, True), ("icon-192.png", 192, False),
                             ("icon-512.png", 512, False), ("icon-maskable-512.png", 512, True)):
        (OUT / name).write_bytes(render(size, mask))
        print("wrote", name)
