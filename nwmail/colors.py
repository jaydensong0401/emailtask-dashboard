"""프로젝트 색: 직접 고른 색(#rrggbb)을 라이트 · 다크 테마에 맞게 밝기만 맞춘다.

팔레트 9색은 대시보드 CSS(--s1~--s9)에 테마별로 들어 있다. 직접 고른 색은 OKLCH 밝기만 테마 범위 안으로
옮기고 색상 · 채도는 그대로 둔다 (색역 밖으로 나가면 채도만 줄인다). 대시보드 생성과 서버 응답이 같이 쓴다.
"""
from __future__ import annotations

import math
import re

HEX = re.compile(r"^#[0-9a-f]{6}$")
# OKLCH 밝기: 밝은 바탕 · 어두운 바탕에서 머리줄 막대 · 네모가 잘 보이는 범위 (팔레트 9색이 이 안에 있다)
BANDS = {"light": (0.45, 0.75), "dark": (0.55, 0.75)}


def _lin(c: float) -> float:
    return c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4


def _enc(c: float) -> float:
    c = min(1.0, max(0.0, c))
    return 12.92 * c if c <= 0.0031308 else 1.055 * c ** (1 / 2.4) - 0.055


def _oklab(r: float, g: float, b: float) -> tuple[float, float, float]:
    """선형 sRGB → OKLab."""
    l_, m_, s_ = (math.copysign(abs(v) ** (1 / 3), v) for v in (
        0.4122214708 * r + 0.5363325363 * g + 0.0514459929 * b,
        0.2119034982 * r + 0.6806995451 * g + 0.1073969566 * b,
        0.0883024619 * r + 0.2817188376 * g + 0.6299787005 * b))
    return (0.2104542553 * l_ + 0.7936177850 * m_ - 0.0040720468 * s_,
            1.9779984951 * l_ - 2.4285922050 * m_ + 0.4505937099 * s_,
            0.0259040371 * l_ + 0.7827717662 * m_ - 0.8086757660 * s_)


def _linear_rgb(L: float, a: float, b: float) -> tuple[float, float, float]:
    """OKLab → 선형 sRGB (색역 밖이면 0~1 을 벗어난 값)."""
    l_ = L + 0.3963377774 * a + 0.2158037573 * b
    m_ = L - 0.1055613458 * a - 0.0638541728 * b
    s_ = L - 0.0894841775 * a - 1.2914855480 * b
    l, m, s = l_ ** 3, m_ ** 3, s_ ** 3
    return (4.0767416621 * l - 3.3077115913 * m + 0.2309699292 * s,
            -1.2684380046 * l + 2.6097574011 * m - 0.3413193965 * s,
            -0.0041960863 * l - 0.7034186147 * m + 1.7076147010 * s)


def lightness(hex_: str) -> float:
    """#rrggbb 의 OKLCH 밝기 (0~1)."""
    return _oklab(*(_lin(int(hex_[i:i + 2], 16) / 255) for i in (1, 3, 5)))[0]


def tone(hex_: str, mode: str) -> str:
    """#rrggbb 를 그 테마(light | dark) 밝기 범위 안으로. 이미 안에 있으면 그대로."""
    hex_ = hex_.lower()
    L, a, b = _oklab(*(_lin(int(hex_[i:i + 2], 16) / 255) for i in (1, 3, 5)))
    lo, hi = BANDS[mode]
    if lo <= L <= hi:
        return hex_
    L = min(hi, max(lo, L))
    chroma, hue = math.hypot(a, b), math.atan2(b, a)
    for _ in range(60):                              # 색역 밖이면 채도만 줄인다
        rgb = _linear_rgb(L, chroma * math.cos(hue), chroma * math.sin(hue))
        if all(-1e-4 <= c <= 1 + 1e-4 for c in rgb):
            break
        chroma *= 0.9
    return "#" + "".join(f"{round(_enc(c) * 255):02x}" for c in rgb)


def tones(hex_: str) -> dict[str, str]:
    """테마별 값 {light, dark}."""
    return {mode: tone(hex_, mode) for mode in BANDS}
