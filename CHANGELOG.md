# Changelog

## 2026-04-25 — 파이프라인 완성 + 검수 UI v2

### 배경

PDF 악보 → MusicXML 변환 파이프라인의 미완성 부분을 마무리하고, AI 인식 결과를 사람이 빠르게 검수·수정할 수 있는 UI를 구축했다.

---

### 1. 6/8 박자 whole-rest duration 버그 수정

**커밋:** `9419119`  
**PR:** `feature/fix-duration-6-8` → `main`

**문제:** oemer(OMR 엔진)는 박자표와 관계없이 전마디 쉼표를 항상 `whole` (4분음표 4박)로 출력한다. 악보 전체가 6/8 박자(3박/마디)인 경우, 매 마디마다 4.0 ≠ 3.0 불일치가 발생했다. Rule 4 경고가 **1724개** 쏟아졌고, MusicXML에 잘못된 duration이 기록됐다.

**수정:**
- `build.py`: 마디 내 음표가 전부 쉼표면 `_build_rest(time_sig)`로 교체 → 6/8는 12틱, 4/4는 16틱 등 박자표 기반 정확한 duration 출력
- `pass3.py` Rule 4: 쉼표만 있는 마디(전마디 쉼표 기호)는 duration 검사 스킵

**결과:** Rule 4 경고 **1724개 → 16개** (남은 16개는 실제 oemer 인식 오류)

---

### 2. Pass 2c 가사 추출 + MusicXML 구조 검증

**커밋:** `41ded6e`  
**PR:** `feature/pipeline-complete` → `feature/fix-duration-6-8`

#### 2-1. Pass 2c — 가사(Lyrics) 추출

파이프라인에 성악 파트 가사 추출 단계를 추가했다. 이전까지 가사는 전혀 지원되지 않았다.

| 파일 | 변경 내용 |
|------|---------|
| `src/models/score.py` | `RawLyric` 데이터클래스 추가, `PASS2C_DONE` 상태, `ScoreDocument.raw_lyrics` 필드 |
| `src/utils/ocr.py` | `extract_lyrics_from_stave()` — 수평 투영법으로 보표 하단 y 감지 후 tesseract `kor+eng` OCR |
| `src/pipeline/pass2c.py` | 신규 파일. 성악 파트 이름 패턴 자동 감지(`vocal`, `voice`, `soprano`, `보컬`, `노래` 등) → 시스템별 가사 크롭 → x 좌표 → `(마디, 박자)` 변환 |
| `src/pipeline/build.py` | `<lyric>` MusicXML 요소 생성, beat 0.5 이내 가장 가까운 음표에 자동 매칭 |
| `src/pipeline/runner.py` | Pass 2c 호출, `pass2c_lyrics.json` 저장 |
| `review_ui.py` | `rebuild_musicxml`에 가사 통과 |

#### 2-2. MusicXML 구조 검증

export 전 기본 구조를 자동으로 검증한다.

- `runner.py`: `validate_musicxml()` 추가
  - `score-partwise` 루트 확인
  - `part-list` ↔ `part` 요소 수 일치 확인
  - 빈 마디(음표·attributes 모두 없음) 감지
- 검증 오류는 WARNING 로그로 기록, 정상이면 `"MusicXML 구조 검증 통과"` INFO

---

### 3. Tier 2-4 음표 추출 + Rule 4 마디 정규화

**커밋:** `ad801c1`  
**PR:** `feature/tier24-rule4` → `feature/pipeline-complete`

#### 3-1. Tier 2-4 단일 보표 oemer 추출

기존 Pass 2b는 Piano(Tier 1, 2단 보표)만 oemer로 처리했다. 현악·관악·금관 파트는 전쉼표만 출력됐다.

| 파일 | 변경 내용 |
|------|---------|
| `src/utils/omr.py` | `_parse_mxl_single()` — 단일 보표 oemer MusicXML 파서 |
| `src/utils/omr.py` | `extract_notes_oemer_single()` — 단일 보표 oemer 래퍼 (캐시 지원, Piano 크롭과 키 충돌 없음) |
| `src/pipeline/pass2b.py` | `_extract_single_part_worker()` — subprocess-safe Tier 2-4 전용 worker |
| `src/pipeline/pass2b.py` | `_tier_parts()` 헬퍼, `run_pass2b(tiers=[1,2,3,4])` 호출 시 전체 파트 병렬 추출 |

**활성화:** `run_pass2b(tiers=[1,2,3,4])`. 기본값(`tiers=None`)은 Tier 1(Piano)만 처리한다.

#### 3-2. Rule 4 마디 정규화 (build.py)

oemer나 LLM이 잘못 인식한 음표(박자 초과·미달)가 MusicXML에 그대로 들어가면 MuseScore 임포트 오류가 발생한다. 빌드 단계에서 자동 정규화한다.

- **overflow(초과):** voice당 누적 틱 추적 → 마디 총 틱 초과하는 음표는 클리핑, 나머지를 typeless rest로 보충
- **underflow(미달):** voice 종료 후 남은 틱을 typeless rest로 채움

**검증:**
```
P0  m70: duration 합 = 12틱 (6/8 정확) ✓
P14 m18: voice 1 = 12틱, voice 2 = 12틱 ✓
```

---

### 4. 검수 UI v2 — 형광팬 하이라이트 + 음표·코드 편집기

**커밋:** `42477b5`  
**PR:** `feature/review-ui-v2` → `feature/tier24-rule4`

`review_ui.py`를 전면 재작성. 기존 UI는 코드 심볼 수정만 가능했다.

#### 신뢰도 기반 형광팬 하이라이트

마디별 confidence 점수를 계산하고 원본 악보 이미지 위에 반투명 색상으로 오버레이한다.

| 색상 | 기준 | 의미 |
|------|------|------|
| 🔴 적색 | `conf < 50%` | 검수 필요 — Rule 4 플래그, parse 실패 등 |
| 🟡 황색 | `50% ≤ conf < 75%` | 주의 — 낮은 신뢰도 |
| 🟢 연녹 | `conf ≥ 75%` | 정상 |

신뢰도 결정 요소:
- 마디 내 음표의 최소 confidence
- 코드 심볼 `needs_review` 여부
- Rule 4 박자 불일치 플래그 (→ conf 0.40으로 강제)

#### 악보 개요 스트립

각 시스템(페이지 행)을 형광팬이 칠해진 이미지로 표시한다. 마디 수만큼의 버튼이 아래에 배치되고, 클릭하면 상세 패널이 열린다.

```
[시스템 이미지 — 형광팬 오버레이]
[m1 🟢] [m2 🟢] ... [m7 🔴] [m8 🟡] ...
```

#### 마디 상세 패널

마디 클릭 시 페이지 상단에 표시:
- 마디 크롭 이미지 (형광팬 포함) + 신뢰도 게이지 바
- 경고 플래그 목록
- 코드 심볼 편집 (인라인)
- 음표 편집 폼

#### 음표 편집 폼

파트별로 전체 음표를 한 번에 수정한다. `st.form`으로 일괄 저장.

| 필드 | 설명 |
|------|------|
| 박자 | float, 0.25 단위 step |
| 음높이 | text input (`G4`, `F#5`, `rest` 등) |
| 음길이 | selectbox (whole / half / quarter / eighth / 16th / 32nd) |
| 점 | 0–2 |
| 성부 | 1–4 |
| 삭제 | 체크박스 |

하단 "➕ 새 음표 추가" 폼으로 마디에 음표를 추가할 수 있다.

#### 수정 저장 구조

`output/corrections.json`:
```json
{
  "chords": { "133": "Am7" },
  "notes": {
    "P13-70": [
      { "beat": 1.0, "pitch": "D5", "duration": "half", "dots": 1, "voice": 1 }
    ]
  }
}
```

"MusicXML 재생성" 클릭 시 corrections를 반영해 `output.musicxml`을 재빌드한다.

---

### 파이프라인 전체 구조 (현재)

```
PDF
 │
 ▼ Pass 1 ── pytesseract + OpenCV
 │  악기명 OCR, 시스템 y좌표, 조표/박자표, 마디번호
 │  → pass1_layout.json
 │
 ▼ Pass 2a ── pytesseract + regex
 │  코드 심볼 OCR (Piano 보표 상단 스트립)
 │  → pass2a_chords.json
 │
 ▼ Pass 2b ── oemer (신경망 OMR)
 │  Tier 1: Piano treble + bass (2단 크롭, 병렬)
 │  Tier 2-4: 현악/관악/금관 (단일 보표 크롭, 병렬, opt-in)
 │  → pass2b_notes.json
 │
 ▼ Pass 2c ── pytesseract kor+eng
 │  성악 파트 감지 → 보표 하단 가사 OCR → beat 추정
 │  → pass2c_lyrics.json
 │
 ▼ Pass 3 ── 음악이론 규칙
 │  Rule 1: 다이아토닉 분류
 │  Rule 2: 근음 도약 검사
 │  Rule 3: 저신뢰도 플래그
 │  Rule 4: 마디 duration 합산 검증
 │
 ▼ Build ── lxml MusicXML 생성
 │  코드·음표·가사 조립, 마디 정규화 (overflow clip + underflow fill)
 │  → output.musicxml
 │
 ▼ 구조 검증 (validate_musicxml)
 │  part-list ↔ part 수, 빈 마디 확인
 │
 ▼ output.musicxml
   MuseScore / Dorico에서 직접 열기
```

---

### 남은 작업

| 항목 | 우선순위 | 비고 |
|------|---------|------|
| 실제 PDF end-to-end 테스트 | 🔴 High | CEO 악보로 처음부터 실행 미완 |
| 웹 앱 (PDF 업로드 → 검수 → 다운로드) | 🔴 High | 현재 CLI + Streamlit dev 툴만 |
| Claude 멀티모달 코드 보정 레이어 | 🟡 Medium | 현재 pytesseract only, Claude API 미연결 |
| 검수 UI — 조표/마디구조 검수 | 🟡 Medium | 코드·음표만 커버, 조표 수정 없음 |
| MusicXML 4.0 XSD 스키마 검증 | 🟡 Medium | 현재 구조 체크만, 정식 XSD 미적용 |
| 사용자 검증 (인터뷰 3-5명 추가) | 🟡 Medium | 현재 확인된 사용자 1명 |
| Tier 2-4 실제 정확도 측정 | 🟢 Low | oemer 단일 보표 성능 미검증 |
