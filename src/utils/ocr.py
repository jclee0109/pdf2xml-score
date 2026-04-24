"""OCR 유틸 — pytesseract 래퍼"""
from __future__ import annotations

import re
import logging

import pytesseract
from PIL import Image

log = logging.getLogger(__name__)

# 악기 이름 → 클레프 매핑 키워드
_BASS_KEYWORDS  = {"bass", "bassoon", "trombone", "tuba", "contrabass", "cello", "baritone"}
_ALTO_KEYWORDS  = {"viola"}
_TENOR_KEYWORDS = {"tenor"}


def _infer_clef(name: str) -> str:
    lower = name.lower()
    if any(k in lower for k in _BASS_KEYWORDS):
        return "bass"
    if any(k in lower for k in _ALTO_KEYWORDS):
        return "alto"
    if any(k in lower for k in _TENOR_KEYWORDS):
        return "tenor"
    return "treble"


def extract_instrument_names(img: Image.Image, margin_width: int = 160) -> list[dict]:
    """악보 첫 페이지 왼쪽 여백에서 악기명 목록 추출.

    Returns: [{"name": str, "clef": str}, ...]
    """
    cropped = img.crop((0, 0, margin_width, img.height))
    # 업스케일링으로 OCR 정확도 향상
    scale = 2
    w, h = cropped.size
    cropped = cropped.resize((w * scale, h * scale), Image.LANCZOS)

    text = pytesseract.image_to_string(cropped, config="--psm 6 --oem 3")
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]

    parts = []
    seen: set[str] = set()
    for line in lines:
        # 숫자·특수문자만 있는 라인 제거
        if re.fullmatch(r'[\d\W]+', line):
            continue
        name = line
        if name in seen:
            continue
        seen.add(name)

        # Piano/Organ/Harp → treble + bass 분리
        lower = name.lower()
        if any(k in lower for k in ("piano", "organ", "harp", "keyboard")):
            treble_name = f"{name} treble"
            bass_name   = f"{name} bass"
            if treble_name not in seen:
                parts.append({"name": treble_name, "clef": "treble"})
                seen.add(treble_name)
            if bass_name not in seen:
                parts.append({"name": bass_name, "clef": "bass"})
                seen.add(bass_name)
        else:
            parts.append({"name": name, "clef": _infer_clef(name)})

    log.debug(f"OCR 악기명: {[p['name'] for p in parts]}")
    return parts


def extract_text_region(img: Image.Image, bbox: tuple[int, int, int, int],
                        psm: int = 7) -> str:
    """임의 영역에서 텍스트 한 줄 추출."""
    cropped = img.crop(bbox)
    w, h = cropped.size
    if w < 10 or h < 5:
        return ""
    cropped = cropped.resize((w * 2, h * 2), Image.LANCZOS)
    text = pytesseract.image_to_string(cropped, config=f"--psm {psm} --oem 3")
    return text.strip()


# ── 코드 심볼 정규화 ──────────────────────────────────────────────────────────

_CHORD_CLEANUP = [
    (r'(?<=[A-Ga-g])b(?=[^a-z]|$)', '♭'),   # Gb → G♭  (단어 끝 또는 비알파벳 앞)
    (r'#',  '♯'),
    (r'maj7|M7|Maj7', 'maj7'),
    (r'[Mm]in|[Mm]i(?=[^n])',  'm'),         # min/mi → m
    (r'dim7', 'dim7'),
    (r'aug',  'aug'),
    (r'sus2', 'sus2'),
    (r'sus4', 'sus4'),
    (r'\s+', ''),                             # 공백 제거
]


def normalize_chord(text: str) -> str:
    """OCR로 읽은 코드 심볼 텍스트를 정규화."""
    for pattern, repl in _CHORD_CLEANUP:
        text = re.sub(pattern, repl, text)
    return text.strip()


def extract_chord_symbols(img: Image.Image, chord_strip_height: int = 45) -> list[tuple[int, str]]:
    """크롭된 피아노 보표 이미지에서 코드 심볼 추출.

    Args:
        img: crop_part_range()로 얻은 Piano 보표 영역
        chord_strip_height: 코드 심볼이 위치한 상단 픽셀 높이

    Returns: [(x_center_px, chord_text), ...]  x 기준으로 정렬
    """
    strip = img.crop((0, 0, img.width, min(chord_strip_height, img.height)))
    w, h = strip.size
    strip = strip.resize((w * 2, h * 2), Image.LANCZOS)

    data = pytesseract.image_to_data(strip, config="--psm 6 --oem 3",
                                     output_type=pytesseract.Output.DICT)
    results: list[tuple[int, str]] = []
    for i, word in enumerate(data["text"]):
        word = word.strip()
        if not word:
            continue
        conf = int(data["conf"][i])
        if conf < 10:
            continue
        # 코드 심볼 패턴: A-G로 시작
        if not re.match(r'^[A-G]', word):
            continue
        x_center = data["left"][i] + data["width"][i] // 2
        x_center //= 2  # 2× 스케일 보정
        normalized = normalize_chord(word)
        results.append((x_center, normalized))

    results.sort(key=lambda t: t[0])
    return results
