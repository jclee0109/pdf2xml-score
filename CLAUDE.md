# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## 프로젝트 개요

PDF 악보에서 MusicXML을 추출하는 파이프라인. 외부 OMR 엔진에 의존하지 않고 전 단계를 직접 소유한다.
- Pass 1/2a: pytesseract OCR + OpenCV (API 불필요)
- Pass 2b: oemer OMR (로컬 ONNX, API 불필요)
- Pass 2c: 성악 가사 추출
- Pass 3: 음악 이론 검증
- `src/utils/llm.py`는 존재하지만 현재 어떤 pass에서도 호출하지 않는다. API 키 불필요.

## 실행 방법

```bash
# venv 활성화 (항상 필요)
source .venv/bin/activate

# 파이프라인 전체 실행 (PDF → MusicXML)
python -c "
import logging, sys
logging.basicConfig(level=logging.INFO, stream=sys.stdout)
sys.path.insert(0, '.')
from src.pipeline.runner import run_sprint1
run_sprint1('your_score.pdf', 'output_dir')
"

# 기존 JSON 결과에서 MusicXML만 재생성 (pass1_layout.json + pass2a_chords.json 필요)
python -c "
from src.pipeline.runner import run_sprint1_from_files
run_sprint1_from_files('output_dir')
"

# 검수 UI
streamlit run review_ui.py
```

## 파이프라인 아키텍처

```
PDF → [Render 300dpi] → pages/*.png
                               │
              ┌────────────────┼─────────────────┐
         [Pass 1]          [Pass 2a]          [Pass 2b]
       OCR+OpenCV         OCR 코드             oemer OMR
       ScoreLayout       RawChord[]           RawNote[]
              └────────────────┼─────────────────┘
                          [Pass 3]
                        음악 이론 검증
                      ValidatedChord[]
                       ValidatedNote[]
                          [Build]
                        output.musicxml
```

Pass 2a, 2b, 2c는 독립적이며 병렬 실행 가능.

## 핵심 데이터 모델 (`src/models/`)

- `ScoreLayout`: `parts: list[PartInfo]` + `systems: list[SystemInfo]` — 전체 파이프라인의 좌표 앵커
- `SystemInfo`: `y_top_px/y_bottom_px` (픽셀 절대값) + `active_parts` (이 시스템에 실제 보표가 있는 파트 ID 목록)
- `active_parts` 순서가 crop 인덱스 기준 — `PartInfo.order`(전체 순서)와 다르다. 쉬는 파트가 생략되면 인덱스가 달라지므로 반드시 `system.active_parts.index(pid)`를 써야 한다.
- `RawNote.pitch`: 악보에 적힌 written pitch 그대로. 이조 변환은 `TRANSPOSITION_TABLE`로 파이프라인 코드에서 처리.

## 주요 구현 세부사항

**파트 crop 방식** (`src/utils/render.py`): 시스템 `y_top~y_bottom`을 `active_parts` 수로 균등 분할. 정확한 보표 경계가 없으므로 근사값이다.

**oemer 캐시** (`src/utils/omr.py`): `output/.oemer_cache/`에 `.pkl` + `.musicxml` + `.conf.json` 사이드카 저장. 첫 실행은 ONNX 추론으로 수 분, 이후 캐시 히트 시 즉시 완료.

**oemer 한계**: 피아노 단일 보표에 최적화. 오케스트라 악보(다수 파트, 복잡한 조표)에서는 음표 추출 정확도가 크게 낮아진다.

**Pass 1 레이아웃 감지** (`src/utils/staff_detect.py`): 시스템 y좌표 감지 신뢰도 높음, 마디번호 OCR 신뢰도 높음, 조표/박자표 감지 낮음(best-effort).

**confidence**: `RawNote.confidence`는 oemer seg_net의 notehead channel 확률 평균. monkey-patch로 캡처 (`_patch_oemer_for_probs`).

## 파일 구조

```
src/
├── models/
│   ├── score.py        # PartInfo, SystemInfo, ScoreLayout, RawNote, RawChord 등
│   └── chord.py        # ChordSymbol, 음악 이론 유틸
├── pipeline/
│   ├── runner.py       # run_sprint1() / run_sprint1_from_files() 진입점
│   ├── pass1.py        # 구조 분석 (OCR + OpenCV)
│   ├── pass2a.py       # 코드 심볼 추출 (OCR)
│   ├── pass2b.py       # 음표 추출 (oemer)
│   ├── pass2c.py       # 가사 추출
│   ├── pass3.py        # 음악 이론 검증 → ValidatedChord/Note
│   └── build.py        # MusicXML 4.0 생성
└── utils/
    ├── staff_detect.py # OpenCV 기반 시스템/마디 감지
    ├── ocr.py          # pytesseract 래퍼 (악기명, 코드 심볼)
    ├── omr.py          # oemer 래퍼 + confidence 캡처
    ├── render.py       # PDF 렌더링, 파트 crop
    └── llm.py          # Anthropic/Gemini 래퍼 (현재 미사용)
review_ui.py            # Streamlit 검수 UI
```

## 버그 수정 원칙

버그를 수정할 때는 코드를 바꾸기 전에 반드시 확인할 것:

1. **하드코딩 여부**: 특정 값(문자열 리스트, 매직 넘버)을 조건으로 사용하는가? 대신 원인을 제거하거나 더 신뢰할 수 있는 소스를 우선 사용하는 방식으로 해결한다.
2. **일반성 훼손 여부**: 이 수정이 다른 입력(다른 악보, 다른 박자표, 다른 조성)에서도 올바르게 동작하는가? "지금 테스트한 케이스"만 통과하는 fix는 fix가 아니다.
3. **근본 원인 vs 증상**: 잘못된 출력을 후처리로 필터링하지 말고, 잘못된 출력이 생기는 이유를 없애라.

예시 (잘못된 fix):
```python
if sys.time_signature in ("4/4", "7/4", "1/4"):  # 하드코딩된 "나쁜 값" 목록
    sys.time_signature = detected_time
```

예시 (올바른 fix):
```python
# Audiveris가 감지 성공 시 무조건 신뢰 — 7/4 등 실제 박자표도 보존
if detected_time:
    sys.time_signature = detected_time
```

## Skill routing

When the user's request matches an available skill, ALWAYS invoke it using the Skill
tool as your FIRST action. Do NOT answer directly, do NOT use other tools first.

Key routing rules:
- Bugs, errors, "why is this broken" → invoke investigate
- Ship, deploy, push, create PR → invoke ship
- QA, test the site, find bugs → invoke qa
- Code review, check my diff → invoke review
- Weekly retro → invoke retro
- Architecture review → invoke plan-eng-review
- Save progress, checkpoint, resume → invoke context-save
