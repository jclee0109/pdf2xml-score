"""
검수 UI — needs_review 코드 심볼을 원본 이미지와 함께 확인/수정

실행: streamlit run review_ui.py
"""
import json
import logging
from pathlib import Path

import streamlit as st
from PIL import Image, ImageDraw

logging.disable(logging.CRITICAL)

from src.pipeline.pass1 import layout_from_json
from src.pipeline.pass2a import chords_from_json
from src.pipeline.pass3 import validate_chords, ValidatedChord
from src.models.score import SystemInfo
from src.utils.render import crop_part_range

# ── 경로 ──────────────────────────────────────────────────────────────────────

OUTPUT_DIR      = Path("output")
LAYOUT_PATH     = OUTPUT_DIR / "pass1_layout.json"
CHORDS_PATH     = OUTPUT_DIR / "pass2a_chords.json"
CORRECTIONS_PATH = OUTPUT_DIR / "corrections.json"


# ── 데이터 로드 ───────────────────────────────────────────────────────────────

@st.cache_data
def load_data():
    layout    = layout_from_json(LAYOUT_PATH)
    chords    = chords_from_json(CHORDS_PATH)
    validated = validate_chords(chords, layout)
    return layout, validated


@st.cache_data
def load_page_image(page_num: int) -> Image.Image:
    return Image.open(OUTPUT_DIR / f"page-{page_num}.png").convert("RGB")


def load_corrections() -> dict[str, str]:
    if CORRECTIONS_PATH.exists():
        return json.loads(CORRECTIONS_PATH.read_text())
    return {}


def save_corrections(corrections: dict[str, str]) -> None:
    CORRECTIONS_PATH.write_text(json.dumps(corrections, indent=2, ensure_ascii=False))


# ── 이미지 crop ───────────────────────────────────────────────────────────────

def crop_piano_for_system(page_img: Image.Image, system: SystemInfo) -> Image.Image:
    active = system.active_parts
    try:
        treble_idx = active.index("Piano treble")
        bass_idx   = active.index("Piano bass")
    except ValueError:
        # active_parts가 ID인 경우 fallback: 아래쪽 1/4 영역
        h = page_img.height
        return page_img.crop((0, int(h * 0.6), page_img.width, int(h * 0.85)))

    return crop_part_range(
        page_img,
        system.y_top_px, system.y_bottom_px,
        treble_idx, bass_idx, len(active),
        extra_top=40, extra_bottom=15,
    )


def highlight_chord_in_image(img: Image.Image, measure_ratio: float) -> Image.Image:
    """마디 위치 추정해서 반투명 하이라이트 표시."""
    img = img.copy()
    draw = ImageDraw.Draw(img, "RGBA")
    x = int(img.width * measure_ratio)
    w = max(60, img.width // 20)
    draw.rectangle(
        [max(0, x - w // 2), 0, min(img.width, x + w // 2), img.height],
        fill=(255, 255, 0, 60),
        outline=(255, 200, 0, 180),
        width=3,
    )
    return img


def estimate_measure_x_ratio(measure: int, system: SystemInfo) -> float:
    """마디 번호 → 시스템 내 x 위치 비율 (균등 추정)."""
    total = system.end_measure - system.start_measure + 1
    idx   = measure - system.start_measure
    return (idx + 0.5) / total


# ── 파이프라인 재실행 ─────────────────────────────────────────────────────────

def rebuild_musicxml(corrections: dict[str, str]) -> str:
    from src.pipeline.pass2b import notes_from_json
    from src.pipeline.pass2c import lyrics_from_json
    from src.pipeline.pass3 import validate_notes
    from src.pipeline.build import build_musicxml
    from src.models.chord import parse_chord_text
    from src.pipeline.pass3 import ValidatedChord as VC

    layout    = layout_from_json(LAYOUT_PATH)
    chords    = chords_from_json(CHORDS_PATH)
    validated = validate_chords(chords, layout)

    # 수정 적용
    corrected = []
    for v in validated:
        if str(v.measure) in corrections and corrections[str(v.measure)].strip():
            new_text   = corrections[str(v.measure)].strip()
            normalized = parse_chord_text(new_text)
            corrected.append(VC(
                measure=v.measure, beat=v.beat,
                chord_text=new_text, normalized=normalized,
                confidence=1.0, flags=["corrected"], needs_review=False,
            ))
        else:
            corrected.append(v)

    notes_path  = OUTPUT_DIR / "pass2b_notes.json"
    lyrics_path = OUTPUT_DIR / "pass2c_lyrics.json"
    raw_notes   = notes_from_json(notes_path)   if notes_path.exists()  else []
    raw_lyrics  = lyrics_from_json(lyrics_path) if lyrics_path.exists() else []
    raw_notes   = validate_notes(raw_notes, layout)

    xml_bytes = build_musicxml(layout, corrected, raw_notes, raw_lyrics or None)
    out_path  = OUTPUT_DIR / "output.musicxml"
    out_path.write_bytes(xml_bytes)
    return str(out_path)


# ── UI ────────────────────────────────────────────────────────────────────────

def main():
    st.set_page_config(page_title="코드 심볼 검수", layout="wide")
    st.title("🎵 코드 심볼 검수")

    layout, validated = load_data()
    needs_review = [v for v in validated if v.needs_review]
    corrections  = load_corrections()

    # 사이드바 요약
    with st.sidebar:
        st.header("요약")
        st.metric("전체 코드", len(validated))
        st.metric("검수 필요", len(needs_review))
        st.metric("수정 완료", len(corrections))
        st.divider()

        if st.button("✅ MusicXML 재생성", type="primary", use_container_width=True):
            with st.spinner("빌드 중..."):
                out = rebuild_musicxml(corrections)
            st.success(f"저장: {out}")
            st.cache_data.clear()

        if corrections:
            st.divider()
            st.subheader("저장된 수정")
            for m, c in sorted(corrections.items(), key=lambda x: int(x[0])):
                orig = next((v.chord_text for v in validated if v.measure == int(m)), "?")
                st.write(f"m{m}: `{orig}` → `{c}`")

    if not needs_review:
        st.success("검수 항목이 없습니다. 모든 코드가 자동 검증 통과.")
        return

    st.info(f"아래 {len(needs_review)}개 항목을 확인해주세요.")

    for vc in needs_review:
        sys = next(
            (s for s in layout.systems if s.start_measure <= vc.measure <= s.end_measure),
            None,
        )
        if sys is None:
            continue

        flag_labels = {
            "chromatic":     "🔴 비다이아토닉",
            "large_leap":    "🟡 근음 도약 큼",
            "low_confidence": "🟠 저신뢰도",
            "corrected":     "✅ 수정됨",
        }
        flag_str = " ".join(flag_labels.get(f, f) for f in vc.flags)

        with st.expander(
            f"마디 {vc.measure} — **{vc.chord_text}** | conf={vc.confidence:.2f} | {flag_str}",
            expanded=True,
        ):
            col_img, col_ctrl = st.columns([3, 1])

            with col_img:
                page_img = load_page_image(sys.page)
                crop     = crop_piano_for_system(page_img, sys)
                ratio    = estimate_measure_x_ratio(vc.measure, sys)
                crop_hl  = highlight_chord_in_image(crop, ratio)
                st.image(crop_hl, caption=f"페이지 {sys.page} | 시스템 m{sys.start_measure}~{sys.end_measure}", use_container_width=True)

            with col_ctrl:
                st.markdown(f"**원본:** `{vc.chord_text}`")
                st.markdown(f"**조성:** {sys.key}")
                st.markdown(f"**마디:** {vc.measure}")
                st.markdown(f"**신뢰도:** {vc.confidence:.0%}")
                st.markdown("---")

                current = corrections.get(str(vc.measure), "")
                new_val = st.text_input(
                    "수정 (빈칸=원본 유지)",
                    value=current,
                    key=f"input_{vc.measure}",
                    placeholder=vc.chord_text,
                )

                if st.button("저장", key=f"save_{vc.measure}"):
                    if new_val.strip():
                        corrections[str(vc.measure)] = new_val.strip()
                    else:
                        corrections.pop(str(vc.measure), None)
                    save_corrections(corrections)
                    st.success("저장됨")
                    st.rerun()

    st.divider()
    st.caption("수정 후 사이드바의 'MusicXML 재생성' 버튼을 누르면 output.musicxml에 반영됩니다.")


if __name__ == "__main__":
    main()
