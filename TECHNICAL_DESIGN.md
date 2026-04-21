# PDF Score → MusicXML: Technical Design

> 작성일: 2026-04-22  
> 스파이크 결과 기반 설계 (바람의노래 풀스코어 테스트)

---

## 1. 설계 배경

### 스파이크에서 확인한 것

| 도구 | 마디 구조 | 코드 심볼 | 비고 |
|------|----------|----------|------|
| ACE Studio | 완전 붕괴 (133→1138) | 0% | 사용 불가 |
| ChatGPT Plugin | 정확 (133마디, 악기명 일치) | 0% | 구조만 가능 |
| Claude 단독 (Page 1) | — | 95% (코드값 기준 100%) | 마디번호 shift 문제 |
| Claude 단독 (Page 4-6) | — | 0% (shift + 조성 미감지) | 밀도 높은 페이지 실패 |

### 핵심 발견

1. **코드 자체는 잘 읽힌다** — 마디번호 없이 코드값만 비교하면 Page 1-3은 70-100%
2. **마디번호 shift가 최대 적** — 오인식의 44%가 번호 밀림. 코드 자체는 맞음
3. **조성 컨텍스트 없이 읽으면 조성 변화 구간에서 붕괴** (Page 6: 10%)
4. **기존 도구는 Pass 3 (음악 이론 검증)을 전혀 하지 않음** — 차별점

### 문제 분리

```
구조 레이어 (geometric)   : 마디번호, 조표, 박자표, 시스템 위치 → 명시적으로 인쇄됨
내용 레이어 (semantic)    : 코드 심볼, 가사, 표현기호 → 컨텍스트가 있어야 정확
```

단일 패스로 두 레이어를 동시에 처리하면 항상 실패한다.

---

## 2. 전체 파이프라인

```
PDF
 │
 ▼ [Render] 페이지별 고해상도 이미지 (300dpi)
 │
 ▼ [Pass 1] 구조 추출 (per page)
 │   ─ 시스템 경계 (y좌표)
 │   ─ 각 시스템의 시작 마디번호
 │   ─ 조표 변화 위치 + 조성 (Concert pitch 기준)
 │   ─ 박자표
 │   → ScoreStructure
 │
 ▼ [Pass 2] 코드 추출 (per system, 컨텍스트 주입)
 │   ─ 시스템 crop 이미지 + "마디 N, 키 X" 프롬프트
 │   ─ 코드명 + beat position + confidence score
 │   → RawChordList
 │
 ▼ [Pass 3] 음악 이론 검증
 │   ─ 키에서 벗어난 코드 분류 (diatonic / borrowed / out-of-key)
 │   ─ voice leading 도약 검사
 │   ─ 앞뒤 컨텍스트로 모호한 코드 재평가
 │   → ValidatedChordList (with confidence + flags)
 │
 ▼ [Merge] OMR 구조 + 검증된 코드 병합
 │   ─ ChatGPT Plugin / Audiveris 구조 MusicXML 사용
 │   ─ 코드 심볼 삽입
 │   → Draft MusicXML
 │
 ▼ [Review UI]
 │   ─ low confidence 항목만 표시
 │   ─ 원본 PDF 나란히 제공
 │   → Human-corrected MusicXML
 │
 ▼ MusicXML 4.0 스키마 검증 → Export
```

---

## 3. Pass 1 — 구조 추출

### 목적
마디번호와 조성을 먼저 확정해서 Pass 2의 앵커로 사용한다.

### 입력
- 페이지 이미지 (300dpi PNG)

### 프롬프트 설계

```
이 악보 이미지에서 구조 정보만 추출해줘. 음표나 코드는 보지 않아도 돼.

1. 시스템(가로 줄) 수
2. 각 시스템 첫 마디 번호 (시스템 왼쪽 끝 숫자)
3. 조표 변화: 어느 시스템, 어느 마디에서 조표가 바뀌는지, 바뀐 후 조성
4. 박자표 변화 (있으면)

JSON으로만 응답:
{
  "systems": [
    {"index": 0, "start_measure": 1, "key": "G major", "time": "4/4"},
    {"index": 1, "start_measure": 26, "key": "G major", "time": "4/4"}
  ]
}
```

### 출력 — ScoreStructure

```python
@dataclass
class SystemInfo:
    index: int
    start_measure: int
    end_measure: int        # Pass 2에서 채워짐
    key: str                # "G major", "F minor" 등 concert pitch
    time_signature: str     # "4/4"
    y_top: float            # crop 좌표 (선택)
    y_bottom: float

@dataclass
class ScoreStructure:
    page: int
    systems: list[SystemInfo]
```

### 실패 처리
- 마디번호를 못 읽으면 이전 페이지 마지막 번호 + 1 추정
- 조성을 못 읽으면 이전 시스템 조성 유지
- 불확실한 경우 `confidence` 필드로 표시, 검수 UI 플래그

---

## 4. Pass 2 — 코드 추출

### 목적
Pass 1의 구조 컨텍스트를 주입해서 정확한 코드 심볼을 추출한다.

### 입력
- 시스템 단위 crop 이미지
- "이 줄은 마디 N부터 시작하고, 조성은 X" 컨텍스트

### 크로핑 전략

**Option A — Claude에게 시스템 경계 물어보기** (구현 쉬움)
```
Pass 1에서 y좌표를 받아서 PIL로 crop
```

**Option B — 악보 구조 기반 고정 crop** (더 빠름)
```
풀스코어는 Piano 파트가 항상 중간에 있음
→ Piano staff + 바로 위 코드 심볼 영역만 crop
→ 전체 오케스트라 파트를 볼 필요 없음
```

Option B가 더 빠르고 노이즈가 적다. Piano 파트의 y 위치는 Pass 1에서 한 번만 확인하면 됨.

### 프롬프트 설계

```
이 악보 이미지는 마디 {start_measure}부터 {end_measure}까지야.
현재 조성은 {key}이고 박자는 {time_sig}야.

Piano 파트 위에 적힌 코드 심볼을 모두 추출해줘.
각 코드가 몇 번 마디에 있는지 왼쪽의 숫자를 보고 확인해줘.

JSON으로만 응답:
[
  {"measure": 26, "beat": 1, "chord": "G/B", "confidence": 0.95},
  {"measure": 27, "beat": 1, "chord": "Dsus4", "confidence": 0.90}
]

확신이 낮은 경우 confidence를 낮게 줘 (0.0~1.0).
```

### 출력 — RawChordList

```python
@dataclass
class RawChord:
    measure: int
    beat: float             # 1.0, 2.0, 2.5 등
    chord_text: str         # "Gmaj7", "D/F#", "Ebsus4" 등 원문 그대로
    confidence: float       # 0.0 ~ 1.0
    source_page: int
    source_system: int
```

---

## 5. Pass 3 — 음악 이론 검증

### 목적
읽은 코드가 음악적으로 말이 되는지 검사해서 오인식을 잡아낸다. 기존 어떤 도구도 이 단계가 없다.

### 검증 규칙

#### Rule 1 — 다이아토닉 분류
```
감지된 키(예: G major) 기준으로 각 코드를 분류:
- diatonic     : 해당 키의 코드 (G, Am, Bm, C, D, Em, F#dim)
- borrowed     : 패럴렐 마이너 등에서 빌린 코드 (Eb, Ab/C 등)
- secondary    : 세컨더리 도미넌트 (A7→D, B7→Em 등)
- chromatic    : 완전히 벗어남 → confidence 하향 + 플래그
```

→ chromatic 판정 코드는 "조표 변화 미감지 가능성" 플래그

#### Rule 2 — 근음 도약 검사
```
연속 코드의 근음 거리 계산:
- 반음 0-5: 정상적인 진행
- 반음 6-7: 흔하지 않음, 낮은 confidence
- 반음 8+: 매우 드묾 → 마디번호 shift 가능성 플래그
```

→ 근음이 7도 이상 도약하면 "마디번호가 밀린 것 아닌가?" 의심

#### Rule 3 — 컨텍스트 재평가
confidence < 0.7 인 코드에 대해:
```
"앞 코드는 {prev}, 뒤 코드는 {next}, 키는 {key}야.
이 이미지의 코드가 '{ambiguous}'로 읽혔는데, 맞나? 
틀리다면 가장 가능성 있는 코드는?"
```

→ 2차 Claude 호출로 재평가, 결과가 다르면 두 후보 모두 검수 UI에 제시

### 출력 — ValidatedChordList

```python
@dataclass
class ValidatedChord:
    measure: int
    beat: float
    chord_text: str
    normalized: ChordSymbol      # 구조화된 코드 객체
    confidence: float
    flags: list[str]             # "chromatic", "large_leap", "low_confidence" 등
    alternatives: list[str]      # 재평가 후보들
    needs_review: bool           # True이면 검수 UI에 표시
```

```python
@dataclass
class ChordSymbol:
    root: str           # "G", "F#", "Bb"
    quality: str        # "major", "minor", "dominant", "major-seventh" 등
    bass: str | None    # "B", "D#" 등 슬래시 코드
    extensions: list    # ["add9", "sus4"] 등
```

---

## 6. 병합 — OMR 구조 + 코드 심볼

### 전략
ChatGPT Plugin이 구조(마디수, 악기명)를 정확히 잡는 걸 확인했다.  
이 MusicXML을 뼈대로 쓰고 코드 심볼만 삽입한다.

```python
def merge(structure_xml: str, chords: ValidatedChordList) -> str:
    # structure_xml: ChatGPT plugin 출력 (마디 구조 정확)
    # chords: Pass 3 통과한 코드 목록
    
    # 각 코드를 해당 마디에 <harmony> 태그로 삽입
    # needs_review=True인 항목은 <harmony> + comment 태그
    # MusicXML 4.0 스키마 검증
```

### 구조 소스 우선순위
1. ChatGPT Plugin MusicXML (현재 가장 구조가 좋음)
2. Audiveris (로컬 실행, 구조 정확도 벤치마크 필요)
3. 자체 구조 파싱 (장기)

---

## 7. 내부 데이터 모델

```python
# 전체 파이프라인을 흐르는 중간 표현
@dataclass
class ScoreDocument:
    id: str
    source_pdf: str
    pages: int
    status: PipelineStatus      # pending | processing | review | done

@dataclass
class PipelineResult:
    document_id: str
    structure: list[ScoreStructure]     # Pass 1 결과
    raw_chords: list[RawChord]          # Pass 2 결과
    validated_chords: list[ValidatedChord]  # Pass 3 결과
    review_items: list[ValidatedChord]  # needs_review=True만 필터
    musicxml_draft: str                 # 병합 결과

class PipelineStatus(Enum):
    PENDING = "pending"
    PASS1_DONE = "pass1_done"
    PASS2_DONE = "pass2_done"
    PASS3_DONE = "pass3_done"
    AWAITING_REVIEW = "awaiting_review"
    DONE = "done"
```

---

## 8. 정확도 목표 (수정)

스파이크 결과 기준 현실적 목표:

| Pass | 개선 대상 | 기대 정확도 |
|------|---------|-----------|
| Pass 1 (구조) | 마디번호 shift 44% → 0% | 마디번호 95%+ |
| Pass 2 (컨텍스트 주입) | 조성 오인식 제거 | 코드값 85%+ |
| Pass 3 (이론 검증) | 잔여 오인식 필터링 | confidence 기반 검수 최소화 |
| 목표 | 검수 필요 항목 | 전체의 15% 이하 |

검수 UI의 목표: A4 1페이지 기준 플래그 항목 15개 이하, 검수 5분 이내

---

## 9. 기술 스택

| 역할 | 선택 | 이유 |
|------|------|------|
| PDF → 이미지 | `pdftoppm` (poppler) | 설치됨, 300dpi 안정 |
| 이미지 처리 | `Pillow` | crop, resize |
| AI 인식 | Claude claude-opus-4-7 (multimodal) | 스파이크에서 코드값 인식 확인 |
| OMR 구조 | ChatGPT Plugin 또는 Audiveris | 마디 구조 정확성 확인 |
| MusicXML 파싱/생성 | `music21` 또는 직접 XML | 스키마 검증 포함 |
| 음악 이론 검증 | 자체 구현 (Python) | 키→스케일→다이아토닉 체크 |
| 서버 | FastAPI | 비동기 파이프라인 처리 |
| 프론트엔드 | React | 검수 UI |

---

## 10. 구현 순서

### Sprint 1 — Pass 1 + Pass 2 (코어 파이프라인)
1. PDF → 300dpi 이미지 변환
2. Pass 1 프롬프트 구현 + ScoreStructure 파싱
3. 시스템 crop (Piano 영역)
4. Pass 2 프롬프트 + 컨텍스트 주입
5. 정확도 재측정 (바람의노래 기준, 목표 85%+)

### Sprint 2 — Pass 3 (이론 검증)
1. ChordSymbol 파서 (텍스트 → 구조화)
2. 다이아토닉 분류기
3. 도약 검사
4. 저신뢰도 코드 재평가 (2차 Claude 호출)

### Sprint 3 — 병합 + 검수 UI
1. OMR MusicXML + ValidatedChordList 병합
2. MusicXML 4.0 스키마 검증
3. 검수 UI (플래그 항목 리스트 + 원본 PDF 나란히)
4. CEO와 실전 테스트

---

## 11. 오픈 퀘스천

| # | 질문 | 영향 |
|---|------|------|
| 1 | Pass 1에서 시스템 y좌표를 Claude가 안정적으로 잡아주는가? | crop 전략 결정 |
| 2 | Piano-only crop vs 전체 시스템 crop 중 어느 쪽이 코드 인식에 더 좋은가? | Pass 2 구현 |
| 3 | Audiveris가 ChatGPT Plugin보다 구조 정확도가 높은가? | OMR 소스 결정 |
| 4 | Pass 3 재평가에서 2차 Claude 호출이 실제로 accuracy를 올리는가? | 비용 vs 효과 |
| 5 | 리드시트 (단일 파트)는 파이프라인이 더 단순해지는가? | MVP 범위 재검토 |

---

*연관 문서: `lead_sheet_cleanup_mvp_prd.md`, `juchan-main-design-20260420-151735.md`*
