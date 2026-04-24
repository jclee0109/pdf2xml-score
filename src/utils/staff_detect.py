"""staff_detect.py — Classical CV 기반 악보 레이아웃 감지

신뢰도:
  - 시스템 y 좌표: 높음 (수평 투영법)
  - 마디 시작 번호: 높음 (OCR)
  - 조표/박자표: 낮음 (best-effort, 실패 시 기본값 반환)
"""
from __future__ import annotations

import re
import logging

import cv2
import numpy as np
import pytesseract
from PIL import Image

log = logging.getLogger(__name__)

# 조성 테이블
_SHARPS_KEY = {
    0: "C major", 1: "G major", 2: "D major", 3: "A major",
    4: "E major", 5: "B major", 6: "F# major", 7: "C# major",
}
_FLATS_KEY = {
    1: "F major", 2: "Bb major", 3: "Eb major", 4: "Ab major",
    5: "Db major", 6: "Gb major", 7: "Cb major",
}


def _to_gray(img: Image.Image) -> np.ndarray:
    return np.array(img.convert("L"))


# ── 1. 보표 라인 y 좌표 ────────────────────────────────────────────────────────

def find_staff_line_ys(gray: np.ndarray, threshold: float = 0.40) -> list[int]:
    """수평 투영법으로 보표 라인의 y 좌표 목록 반환."""
    row_density = (gray < 128).sum(axis=1) / gray.shape[1]
    in_line = False
    start_y = 0
    staff_ys: list[int] = []
    for y, val in enumerate(row_density > threshold):
        if val and not in_line:
            in_line = True
            start_y = y
        elif not val and in_line:
            in_line = False
            staff_ys.append((start_y + y) // 2)
    return staff_ys


# ── 2. 시스템 경계 감지 ───────────────────────────────────────────────────────

def _find_system_barline_x(gray: np.ndarray) -> tuple[int, list[tuple[int, int]]]:
    """
    초기 시스템 바라인(수직선) x 좌표와 각 시스템 y 범위 반환.

    악보 시스템의 왼쪽 바라인은 시스템 내 모든 보표를 관통하는
    연속 수직선이다. 이를 이용해 시스템 개수와 y 범위를 신뢰성 있게 감지.
    """
    h, w = gray.shape
    MIN_SEG_HEIGHT = h * 0.30  # 시스템 최소 높이: 페이지 높이의 30%

    best_x = -1
    best_segs: list[tuple[int, int]] = []
    best_total = 0

    # 페이지 왼쪽 40%를 스캔 (악기명 + 보표 시작 영역)
    for x in range(w // 12, w * 2 // 5):
        col = gray[:, x]
        is_black = col < 100

        segs: list[tuple[int, int]] = []
        in_s = False
        s_start = 0
        for y, v in enumerate(is_black):
            if v and not in_s:
                in_s = True
                s_start = y
            elif not v and in_s:
                in_s = False
                if y - s_start >= MIN_SEG_HEIGHT:
                    segs.append((s_start, y))
        if in_s and (h - s_start) >= MIN_SEG_HEIGHT:
            segs.append((s_start, h))

        if not segs:
            continue

        total = sum(e - s for s, e in segs)
        if total > best_total:
            best_total = total
            best_x = x
            best_segs = segs

    return best_x, best_segs


def detect_staff_systems(img: Image.Image) -> list[dict]:
    """
    페이지에서 시스템(보표 묶음) y 경계 반환.

    Returns: [{"y_top": int, "y_bottom": int}, ...]

    알고리즘: 시스템 초기 바라인(수직 실선)을 감지해 시스템 y 범위 결정.
    수직 바라인이 없으면 수평 투영법으로 폴백.
    """
    gray = _to_gray(img)
    h = gray.shape[0]

    barline_x, segs = _find_system_barline_x(gray)

    if segs:
        PADDING = 30
        return [
            {
                "y_top":    max(0, y_start - PADDING),
                "y_bottom": min(h, y_end + PADDING),
            }
            for y_start, y_end in segs
        ]

    # 폴백: 수평 투영법 (단순 악보용)
    log.warning("수직 바라인 미감지 — 수평 투영 폴백")
    staff_ys = find_staff_line_ys(gray)
    if not staff_ys:
        margin = h // 20
        return [{"y_top": margin, "y_bottom": h - margin}]

    return [{"y_top": max(0, staff_ys[0] - 30), "y_bottom": min(h, staff_ys[-1] + 30)}]


# ── 3. 마디 시작 번호 ─────────────────────────────────────────────────────────

def detect_measure_start(img: Image.Image, system_y_top: int) -> int:
    """
    시스템 y_top 위 영역에서 첫 마디 번호를 OCR.
    반환: 마디 번호 (int), 실패 시 1.

    마디 번호는 보표 상단에서 약 10-80px 위에 인쇄됨.
    """
    gray = _to_gray(img)
    h, w = gray.shape

    # 마디 번호 위치: y_top 직전 60px (헤더 영역 제외)
    y1 = max(0, system_y_top - 60)
    y2 = min(h, system_y_top + 15)
    if y2 <= y1:
        return 1

    x1 = w // 10   # 악기 이름 일부 포함해도 OK — 숫자만 추출
    x2 = w // 2

    crop = gray[y1:y2, x1:x2]
    pil = Image.fromarray(crop)
    pil = pil.resize((pil.width * 3, pil.height * 3), Image.LANCZOS)

    text = pytesseract.image_to_string(pil, config="--psm 6 --oem 3")
    # 2자리 이상 숫자 우선 (마디번호는 보통 2-3자리)
    numbers = re.findall(r"\d+", text)
    multi = [n for n in numbers if len(n) >= 2]
    candidates = multi if multi else numbers
    if candidates:
        return int(candidates[0])
    return 1


# ── 4. 보표 x 시작 위치 추정 ──────────────────────────────────────────────────

def _find_staff_x_start(gray: np.ndarray) -> int:
    """
    페이지에서 보표가 시작되는 x 좌표 추정.
    (악기 이름 오른쪽 끝 = 보표 왼쪽 경계)
    """
    # 전체 페이지 열 밀도
    col_density = (gray < 128).sum(axis=0) / gray.shape[0]

    # 페이지 너비의 1/5에서 1/2 사이에서 밀도가 급격히 오르는 지점 탐색
    w = gray.shape[1]
    search_range = range(w // 8, w // 3)

    # 보표 라인은 매우 긴 수평선 → 열 밀도가 갑자기 높아지는 곳
    for x in search_range:
        if col_density[x] > 0.10:  # 페이지 높이의 10% 이상이 검은 픽셀
            return max(0, x - 5)

    return w // 5  # 폴백: 페이지 너비의 1/5


# ── 5. 조표 감지 ──────────────────────────────────────────────────────────────

def detect_key_signature(
    img: Image.Image,
    staff_ys: list[int],
    x_staff_start: int,
) -> str:
    """
    첫 보표 직후 조표 영역에서 샤프/플랫 수를 세어 조성 반환.
    실패 시 "C major".
    """
    gray = _to_gray(img)

    if len(staff_ys) < 5:
        return "C major"

    y_top = staff_ys[0]
    y_bot = staff_ys[4]
    staff_height = y_bot - y_top

    # 보표 라인 제거
    cleaned = gray.copy()
    for sy in staff_ys[:5]:
        cleaned[sy - 2:sy + 3, :] = 255

    # 조표 영역: 클레프 이후 ~100px, 보표 위아래 margin 포함
    margin = staff_height // 3
    x1 = x_staff_start + 50   # 클레프 width ~50px
    x2 = x_staff_start + 170  # 클레프 + 조표 영역
    y1 = max(0, y_top - margin)
    y2 = min(gray.shape[0], y_bot + margin)

    if x2 >= gray.shape[1] or x2 <= x1:
        return "C major"

    region = cleaned[y1:y2, x1:x2]
    _, binary = cv2.threshold(region, 200, 255, cv2.THRESH_BINARY_INV)

    n_labels, _, stats, _ = cv2.connectedComponentsWithStats(binary)

    # 샤프/플랫 크기 범위 (보표 간격 기준)
    space = max((y_bot - y_top) // 4, 5)
    min_h = space
    max_h = staff_height + space * 2

    accidentals = []
    for i in range(1, n_labels):
        area = stats[i, cv2.CC_STAT_AREA]
        comp_h = stats[i, cv2.CC_STAT_HEIGHT]
        comp_w = stats[i, cv2.CC_STAT_WIDTH]
        if area < 20:
            continue
        if not (min_h <= comp_h <= max_h):
            continue
        aspect = comp_h / max(comp_w, 1)
        if aspect < 0.8:
            continue  # 너무 납작한 건 보표 라인 잔여
        accidentals.append((comp_h, comp_w, area))

    n = len(accidentals)
    log.debug(f"조표 accidental 감지: {n}개")

    if n == 0:
        return "C major"

    # 샤프 vs 플랫 판별: 샤프는 가로세로비 1~2, 플랫은 2~4
    avg_aspect = sum(h / max(w, 1) for h, w, _ in accidentals) / n
    is_sharp = avg_aspect < 2.5

    if is_sharp:
        return _SHARPS_KEY.get(min(n, 7), f"C major")
    else:
        return _FLATS_KEY.get(min(n, 7), "C major")


# ── 6. 박자표 감지 ────────────────────────────────────────────────────────────

def detect_time_signature(
    img: Image.Image,
    staff_ys: list[int],
    x_staff_start: int,
    prev_time: str = "4/4",
) -> str:
    """
    첫 보표 영역에서 박자표 추출.

    전략:
    1. 보표 라인 제거 후 박자표 영역 OCR
    2. 연결 요소로 숫자 감지
    실패 시 prev_time 반환 (이전 페이지 박자 유지).
    """
    gray = _to_gray(img)

    if len(staff_ys) < 5:
        return prev_time

    first5 = staff_ys[:5]
    y_top = first5[0]
    y_bot = first5[4]
    staff_height = y_bot - y_top

    # 보표 라인 제거
    cleaned = gray.copy()
    for sy in first5:
        cleaned[sy - 2:sy + 3, :] = 255

    # 박자표 영역: 클레프+조표 이후 ~80px
    x1 = x_staff_start + 110
    x2 = min(gray.shape[1], x_staff_start + 220)
    margin = staff_height // 2
    y1 = max(0, y_top - margin)
    y2 = min(gray.shape[0], y_bot + margin)

    region = cleaned[y1:y2, x1:x2]
    _, binary = cv2.threshold(region, 200, 255, cv2.THRESH_BINARY_INV)

    n_labels, _, stats, centroids = cv2.connectedComponentsWithStats(binary)

    # 박자표 숫자: 상단(분자)과 하단(분모) 각각 하나씩
    digits: list[tuple[int, int, int]] = []  # (y_center, area, label)
    for i in range(1, n_labels):
        area = stats[i, cv2.CC_STAT_AREA]
        comp_h = stats[i, cv2.CC_STAT_HEIGHT]
        comp_w = stats[i, cv2.CC_STAT_WIDTH]
        y_center = int(centroids[i][1]) + y1
        if area < 100 or comp_h < staff_height * 0.25:
            continue
        digits.append((y_center, area, i))

    if len(digits) < 2:
        # 단일 숫자 또는 감지 실패 → OCR 폴백
        pil = Image.fromarray(region)
        pil_big = pil.resize((pil.width * 4, pil.height * 4), Image.LANCZOS)
        text = pytesseract.image_to_string(
            pil_big, config="--psm 6 --oem 3 -c tessedit_char_whitelist=0123456789"
        )
        nums = re.findall(r"\d+", text)
        if len(nums) >= 2:
            return f"{nums[0]}/{nums[1]}"
        log.debug(f"박자표 감지 실패, 이전 값 유지: {prev_time}")
        return prev_time

    # 두 숫자: y_center 기준으로 위=분자, 아래=분모
    digits.sort(key=lambda t: t[0])
    top_label = digits[0][2]
    bot_label = digits[-1][2]

    def _ocr_component(label: int) -> str:
        mask = np.zeros_like(binary)
        mask[binary == 0] = 255  # HACK: use region directly
        x_comp = stats[label, cv2.CC_STAT_LEFT]
        y_comp = stats[label, cv2.CC_STAT_TOP]
        w_comp = stats[label, cv2.CC_STAT_WIDTH]
        h_comp = stats[label, cv2.CC_STAT_HEIGHT]
        sub = region[y_comp:y_comp + h_comp, x_comp:x_comp + w_comp]
        pil = Image.fromarray(sub)
        pil = pil.resize((max(pil.width * 6, 30), max(pil.height * 6, 30)), Image.LANCZOS)
        text = pytesseract.image_to_string(
            pil, config="--psm 8 --oem 3 -c tessedit_char_whitelist=0123456789"
        )
        return text.strip()

    top_num = _ocr_component(top_label)
    bot_num = _ocr_component(bot_label)

    if top_num and bot_num and top_num.isdigit() and bot_num.isdigit():
        return f"{top_num}/{bot_num}"

    log.debug(f"박자표 OCR 실패 (top={top_num!r}, bot={bot_num!r}), 이전 값 유지: {prev_time}")
    return prev_time


# ── 공개 API ──────────────────────────────────────────────────────────────────

def analyze_page(
    img: Image.Image,
    prev_key: str = "C major",
    prev_time: str = "4/4",
    default_measure: int | None = None,
) -> dict:
    """
    단일 페이지 전체 분석.

    Returns:
        {
            "systems": [{"y_top": int, "y_bottom": int}],
            "start_measure": int,
            "key": str,
            "time": str,
            "staff_x_start": int,
        }
    """
    gray = _to_gray(img)
    staff_ys = find_staff_line_ys(gray)
    systems = detect_staff_systems(img)

    if not systems:
        return {
            "systems": [], "start_measure": 1,
            "key": prev_key, "time": prev_time, "staff_x_start": 0,
        }

    x_start = _find_staff_x_start(gray)

    # 마디 번호: 첫 시스템 위쪽 영역
    if default_measure is not None:
        start_measure = default_measure
    else:
        start_measure = detect_measure_start(img, systems[0]["y_top"])

    # 조표 / 박자표: 첫 번째 보표 기준
    key = detect_key_signature(img, staff_ys, x_start)
    if key == "C major" and prev_key != "C major":
        key = prev_key  # 감지 실패 → 이전 페이지 값 유지

    time_sig = detect_time_signature(img, staff_ys, x_start, prev_time)

    return {
        "systems": systems,
        "start_measure": start_measure,
        "key": key,
        "time": time_sig,
        "staff_x_start": x_start,
    }
