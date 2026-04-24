"""
Pass 2b 스파이크 — Piano 음표 추출 정확도 측정

모드:
  python spike_pass2b.py          → Claude API 호출 (ANTHROPIC_API_KEY 필요)
  python spike_pass2b.py --mock   → 하모나이즈 MusicXML 데이터로 평가 파이프라인 검증
"""
import argparse
import base64
import io
import json
import re
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

import anthropic
from PIL import Image

# ── 설정 ──────────────────────────────────────────────────────────────────────

OUTPUT_DIR  = Path("output")
LAYOUT_JSON = OUTPUT_DIR / "pass1_layout.json"
PAGE_IMG    = OUTPUT_DIR / "page-1.png"
RESULT_JSON = OUTPUT_DIR / "spike_pass2b_result.json"
CROP_PNG    = OUTPUT_DIR / "spike_piano_crop.png"

REF_MUSICXML = Path("[하모나이즈] 바람의노래 (R).musicxml")
SPIKE_MEASURES = 4
MODEL = "claude-opus-4-7"

# ── 레퍼런스 (하모나이즈 MusicXML에서 추출, m1~4) ───────────────────────────────
# 하모나이즈 Piano P18: staff 1 = treble, staff 2 = bass
REFERENCE = {
    1: {
        "treble": [
            {"beat": 1.0, "pitch": "D5",  "duration": "half", "dots": 1},
            {"beat": 1.0, "pitch": "G5",  "duration": "half", "dots": 1},
        ],
        "bass": [
            {"beat": 1.0, "pitch": "G3",  "duration": "half", "dots": 1},
            {"beat": 1.0, "pitch": "D4",  "duration": "half", "dots": 1},
            {"beat": 1.0, "pitch": "G4",  "duration": "half", "dots": 1},
        ],
    },
    2: {
        "treble": [
            {"beat": 1.0, "pitch": "A4",  "duration": "half", "dots": 1},
            {"beat": 1.0, "pitch": "F#5", "duration": "half", "dots": 1},
        ],
        "bass": [
            {"beat": 1.0, "pitch": "G3",  "duration": "half", "dots": 1},
            {"beat": 1.0, "pitch": "F#4", "duration": "half", "dots": 1},
        ],
    },
    3: {
        "treble": [
            {"beat": 1.0, "pitch": "A4",  "duration": "half", "dots": 1},
            {"beat": 1.0, "pitch": "B4",  "duration": "half", "dots": 1},
            {"beat": 1.0, "pitch": "E5",  "duration": "half", "dots": 1},
        ],
        "bass": [
            {"beat": 1.0, "pitch": "G3",  "duration": "half", "dots": 1},
            {"beat": 1.0, "pitch": "E4",  "duration": "half", "dots": 1},
        ],
    },
    4: {
        "treble": [
            {"beat": 1.0, "pitch": "A4",  "duration": "half", "dots": 1},
            {"beat": 1.0, "pitch": "F#5", "duration": "half", "dots": 1},
        ],
        "bass": [
            {"beat": 1.0, "pitch": "G3",  "duration": "half", "dots": 1},
            {"beat": 1.0, "pitch": "F#4", "duration": "half", "dots": 1},
        ],
    },
}

# ── crop ──────────────────────────────────────────────────────────────────────

def crop_piano(page_img_path: Path, layout: dict) -> Image.Image:
    s = layout["systems"][0]
    active = s["active_parts"]
    treble_idx = active.index("Piano treble")
    bass_idx   = active.index("Piano bass")

    system_h = s["y_bottom_px"] - s["y_top_px"]
    n = len(active)
    part_h = system_h / n
    y_top = int(s["y_top_px"] + part_h * treble_idx - 20)
    y_bot = int(s["y_top_px"] + part_h * (bass_idx + 1) + 10)

    img = Image.open(page_img_path)
    return img.crop((0, y_top, img.width, y_bot))


# ── mock 데이터: 하모나이즈 MusicXML → Claude 응답 포맷 ─────────────────────────

def build_mock_from_harmonize() -> dict:
    tree = ET.parse(REF_MUSICXML)
    root = tree.getroot()

    result = {"Piano treble": {}, "Piano bass": {}}
    for part in root.findall("part"):
        if part.get("id") != "P18":
            continue
        divisions = 1
        for m in list(part.findall("measure"))[:SPIKE_MEASURES]:
            num = m.get("number")
            div_el = m.find(".//divisions")
            if div_el is not None:
                divisions = int(div_el.text)

            beat_pos  = {1: 1.0, 2: 1.0}
            last_beat = {1: 1.0, 2: 1.0}
            treble_notes, bass_notes = [], []

            for note in m.findall("note"):
                pitch_el = note.find("pitch")
                dur_el   = note.find("duration")
                type_el  = note.find("type")
                dots     = len(note.findall("dot"))
                voice_el = note.find("voice")
                staff_el = note.find("staff")
                chord_el = note.find("chord")
                tie_els  = note.findall("tie")

                staff     = int(staff_el.text) if staff_el is not None else 1
                dur_ticks = int(dur_el.text)   if dur_el   is not None else 0

                if chord_el is None:
                    last_beat[staff] = beat_pos[staff]
                    beat_pos[staff] += dur_ticks / divisions
                beat_val = last_beat[staff]

                if pitch_el is not None:
                    step   = pitch_el.find("step").text
                    octave = pitch_el.find("octave").text
                    alter  = pitch_el.find("alter")
                    acc    = ("#" if alter is not None and float(alter.text) > 0
                              else "b" if alter is not None and float(alter.text) < 0
                              else "")
                    pitch_str = f"{step}{acc}{octave}"
                else:
                    pitch_str = "rest"

                nd = {
                    "beat":       round(beat_val, 2),
                    "pitch":      pitch_str,
                    "duration":   type_el.text if type_el is not None else "quarter",
                    "dots":       dots,
                    "voice":      int(voice_el.text) if voice_el is not None else 1,
                    "tie_start":  any(t.get("type") == "start" for t in tie_els),
                    "tie_end":    any(t.get("type") == "stop"  for t in tie_els),
                    "confidence": 1.0,
                }
                (treble_notes if staff == 1 else bass_notes).append(nd)

            result["Piano treble"][num] = treble_notes
            result["Piano bass"][num]   = bass_notes

    return result


# ── Claude 호출 ───────────────────────────────────────────────────────────────

PROMPT = """이 이미지는 오케스트라 악보의 Piano 파트야. 위쪽이 treble, 아래쪽이 bass.
마디 1~4에 해당해. 조성: G major, 박자: 4/4

Piano treble과 Piano bass 각각의 음표를 모두 추출해줘.
- pitch: 음이름+옥타브 (예: G4, F#5, rest)
- duration: whole, half, quarter, eighth, 16th 중 하나
- dots: 점음표 수 (0, 1, 2)
- beat: 마디 내 박 위치 (1.0, 2.0, 3.0, 4.0 등)
- voice: 1 또는 2 (여러 성부가 있는 경우)
- tie_start/tie_end: 붙임줄 여부

반드시 JSON만 응답해:
{
  "Piano treble": {
    "1": [{"beat": 1.0, "pitch": "G4", "duration": "half", "dots": 1, "voice": 1, "tie_start": false, "tie_end": false, "confidence": 0.9}],
    "2": [...]
  },
  "Piano bass": {
    "1": [...],
    "2": [...]
  }
}"""


def call_claude(img: Image.Image) -> dict | None:
    def img_b64(i: Image.Image) -> str:
        buf = io.BytesIO()
        i.save(buf, format="PNG")
        return base64.standard_b64encode(buf.getvalue()).decode()

    client = anthropic.Anthropic()
    response = client.messages.create(
        model=MODEL,
        max_tokens=4096,
        messages=[{
            "role": "user",
            "content": [
                {"type": "image",
                 "source": {"type": "base64", "media_type": "image/png", "data": img_b64(img)}},
                {"type": "text", "text": PROMPT},
            ],
        }],
    )
    raw = response.content[0].text
    match = re.search(r'```json\s*([\s\S]*?)```|(\{[\s\S]*\})', raw)
    if match:
        try:
            return json.loads(match.group(1) or match.group(2))
        except json.JSONDecodeError:
            pass
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        print("JSON 파싱 실패. 원문:")
        print(raw[:500])
        return None


# ── 정확도 평가 ───────────────────────────────────────────────────────────────

def normalize_pitch(pitch: str) -> str:
    enharmonic = {"Db": "C#", "Eb": "D#", "Gb": "F#", "Ab": "G#", "Bb": "A#"}
    if len(pitch) >= 3 and pitch[1] in ("#", "b"):
        root = enharmonic.get(pitch[:2], pitch[:2])
        return root + pitch[2:]
    return pitch


def pitch_match(p1: str, p2: str) -> bool:
    if p1 == "rest" or p2 == "rest":
        return p1 == p2
    return normalize_pitch(p1) == normalize_pitch(p2)


def evaluate(extracted: dict, reference: dict) -> dict:
    results = {"by_measure": {}}
    total_ref = total_pitch = total_full = 0

    for m_num in range(1, SPIKE_MEASURES + 1):
        m_str  = str(m_num)
        ref_m  = reference.get(m_num, {})

        for staff in ("treble", "bass"):
            ref_notes = ref_m.get(staff, [])
            ext_notes = extracted.get(f"Piano {staff}", {}).get(m_str, [])

            ref_pitches = sorted(n["pitch"] for n in ref_notes)
            ext_pitches = sorted(n["pitch"] for n in ext_notes)

            matched_pitch = sum(
                1 for rp in ref_pitches
                if any(pitch_match(rp, ep) for ep in ext_pitches)
            )
            matched_full = 0
            for rn in ref_notes:
                for en in ext_notes:
                    if (pitch_match(rn["pitch"], en["pitch"])
                            and rn["duration"] == en.get("duration")
                            and rn["dots"] == en.get("dots", 0)):
                        matched_full += 1
                        break

            total_ref   += len(ref_pitches)
            total_pitch += matched_pitch
            total_full  += matched_full

            results["by_measure"][f"m{m_num}_{staff}"] = {
                "ref_count":    len(ref_pitches),
                "ext_count":    len(ext_pitches),
                "pitch_correct": matched_pitch,
                "full_correct":  matched_full,
                "ref_pitches":   ref_pitches,
                "ext_pitches":   ext_pitches,
            }

    results["totals"] = {
        "ref_notes":       total_ref,
        "pitch_accuracy":  round(total_pitch / total_ref * 100, 1) if total_ref else 0,
        "full_accuracy":   round(total_full  / total_ref * 100, 1) if total_ref else 0,
    }
    return results


# ── 메인 ──────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--mock", action="store_true",
                        help="하모나이즈 MusicXML 데이터로 평가 파이프라인 검증 (API 불필요)")
    args = parser.parse_args()

    print("=== Pass 2b Spike: Piano 음표 추출 ===\n")

    layout = json.loads(LAYOUT_JSON.read_text())
    crop = crop_piano(PAGE_IMG, layout)
    crop.save(CROP_PNG)
    print(f"Crop: {crop.size[0]}x{crop.size[1]}px → {CROP_PNG}")

    if args.mock:
        print("모드: MOCK (하모나이즈 MusicXML → Claude 응답 포맷 변환)\n")
        extracted = build_mock_from_harmonize()
    else:
        print(f"모드: Claude API ({MODEL})\n")
        extracted = call_claude(crop)
        if extracted is None:
            print("추출 실패")
            sys.exit(1)

    results = evaluate(extracted, REFERENCE)
    totals  = results["totals"]

    print("=== 정확도 결과 ===")
    print(f"레퍼런스 음표 수: {totals['ref_notes']}")
    print(f"피치 정확도:                    {totals['pitch_accuracy']}%")
    print(f"완전 일치 (pitch+duration+dots): {totals['full_accuracy']}%")
    print()
    print("마디별 상세:")
    for key, r in results["by_measure"].items():
        ok = r["pitch_correct"] == r["ref_count"]
        status = "✓" if ok else "✗"
        print(f"  {status} {key}: ref={r['ref_pitches']}")
        if not ok:
            print(f"      ext={r['ext_pitches']}")

    threshold = 60.0
    passed = totals["pitch_accuracy"] >= threshold
    label  = "PASS ✓" if passed else "FAIL ✗"
    print(f"\n판정: {label}  (기준 {threshold}%, 실제 {totals['pitch_accuracy']}%)")
    if args.mock:
        if passed:
            print("→ 평가 파이프라인 정상. API 호출 시 이 정확도 기준 적용 가능.")
        else:
            print("→ 레퍼런스/평가 로직 재검토 필요.")
    else:
        if passed:
            print("→ Sprint 2 (Pass 2b 구현) 진행 가능")
        else:
            print("→ 프롬프트/크롭 전략 재검토 필요")

    RESULT_JSON.write_text(json.dumps(
        {"mode": "mock" if args.mock else "api",
         "extracted": extracted,
         "evaluation": results},
        indent=2, ensure_ascii=False,
    ))
    print(f"\n결과 저장: {RESULT_JSON}")


if __name__ == "__main__":
    main()
