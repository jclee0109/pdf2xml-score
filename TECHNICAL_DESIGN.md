# PDF Score → MusicXML: Technical Design

> 작성일: 2026-04-22  
> v2: 외부 OMR 의존성 제거, 자체 파이프라인으로 전환

---

## 1. 설계 원칙

외부 OMR 엔진(Audiveris, ChatGPT Plugin 등)에 의존하지 않는다.  
구조 추출부터 MusicXML 생성까지 전 단계를 우리가 소유한다.

**왜?**
- OMR 엔진은 코드 심볼을 전혀 못 읽음 (스파이크에서 확인, 0%)
- 구조 정확도도 도구마다 편차가 크고 제어 불가능
- 장기적으로 품질 개선 루프를 우리가 돌려야 함

**핵심 발견 (스파이크 기반)**
- Claude 멀티모달은 코드값 자체는 잘 읽음 (Page 1: 100%)
- 실패 원인의 44%는 마디번호 shift → 구조를 먼저 잡으면 해결 가능
- 조성 컨텍스트를 주입하면 조성 변화 구간 오인식 제거 가능

---

## 2. 전체 파이프라인

```
PDF
 │
 ▼ [Render]
 │   300dpi PNG, 페이지별
 │
 ▼ [Pass 1] 구조 분석  ────────────────── per page
 │   악기 목록, 시스템 레이아웃
 │   마디번호 앵커, 조표/박자표
 │   → ScoreLayout
 │
 ├─▶ [Pass 2a] 코드 심볼 추출  ─────────── per system
 │     Piano 영역 crop + 구조 컨텍스트 주입
 │     코드명, beat position, confidence
 │     → RawChordList
 │
 ├─▶ [Pass 2b] 음표 추출  ───────────────── per instrument × per system
 │     악기별 파트 crop + 클레프/조표/박자 컨텍스트
 │     pitch, duration, voice, articulation
 │     → RawNoteList
 │
 ▼ [Pass 3] 음악 이론 검증
 │   코드: 다이아토닉 분류, 도약 검사, 저신뢰도 재평가
 │   음표: 마디 내 총 duration 합산 검증, 피치 범위 검사
 │   → ValidatedScore
 │
 ▼ [Build] MusicXML 생성
 │   ScoreLayout + ValidatedScore → MusicXML 4.0
 │   스키마 검증
 │   → Draft MusicXML
 │
 ▼ [Review UI]
 │   needs_review 항목만 표시
 │   원본 PDF 나란히
 │   → Final MusicXML
```

Pass 2a, 2b는 병렬 실행 가능 (독립적).

---

## 3. Pass 1 — 구조 분석

### 목적
이후 모든 패스의 좌표 앵커와 음악 컨텍스트를 확정한다.

### 두 단계로 나눔

**Step 1-A: 악기 목록 + 파트 레이아웃**
첫 페이지에만 실행. 악기명, 파트 수, 각 파트의 y 위치 비율을 읽는다.

```
프롬프트:
"이 악보의 첫 페이지야.
왼쪽 끝에 적힌 악기 이름을 모두 읽어줘.
위에서 아래 순서대로, 각 악기의 보표가 전체 높이에서 몇 % 위치에 있는지도 알려줘.

JSON:
{
  'parts': [
    {'name': 'Piccolo', 'y_ratio': 0.08},
    {'name': 'Flute', 'y_ratio': 0.13},
    ...
  ]
}"
```

**Step 1-B: 페이지별 시스템 구조**
모든 페이지에 실행. 음표/코드는 읽지 않음.

```
프롬프트:
"이 악보 페이지에서 구조 정보만 추출해줘. 음표와 코드는 무시해.

1. 시스템 수 (가로 줄 수)
2. 각 시스템의 첫 마디 번호 (시스템 왼쪽 끝에 인쇄된 숫자)
3. 조표 변화: 몇 번 마디에서 변하는지, 바뀐 조성 (Concert pitch 기준)
4. 박자표 변화 (있으면)

JSON:
{
  'systems': [
    {'start_measure': 1, 'key': 'G major', 'time': '4/4', 'y_top': 0.12, 'y_bottom': 0.45},
    {'start_measure': 26, 'key': 'G major', 'time': '4/4', 'y_top': 0.50, 'y_bottom': 0.85}
  ]
}"
```

### 출력 — ScoreLayout

```python
@dataclass
class PartInfo:
    id: str                  # "P1", "P2" 등
    name: str                # "Piccolo", "Piano" 등
    y_ratio: float           # 전체 페이지 높이 대비 위치 (0.0~1.0)
    clef: str                # "treble", "bass", "alto", "tenor"
    transposition: int       # concert pitch 대비 반음 수 (Bb클라리넷: -2)

@dataclass
class SystemInfo:
    page: int
    system_index: int
    start_measure: int
    end_measure: int         # 다음 시스템 start - 1
    key: str                 # "G major", "Ab minor" 등
    time_signature: str      # "4/4", "3/8" 등
    y_top: float
    y_bottom: float

@dataclass
class ScoreLayout:
    parts: list[PartInfo]
    systems: list[SystemInfo]
    total_measures: int
```

### 실패 처리
- 마디번호 미인식 → 이전 시스템 끝 + 1로 추정, 플래그
- 조성 미인식 → 이전 시스템 조성 유지
- 악기 y위치 불확실 → confidence < 0.8이면 Step 1-A 재실행 (다른 crop으로)

---

## 4. Pass 2a — 코드 심볼 추출

### 목적
Piano 파트 위의 코드 심볼을 정확하게 추출한다.

### 크로핑 전략
Pass 1에서 Piano 파트의 y_ratio를 알고 있음 → Piano staff + 위 여백(코드 심볼 영역)만 crop.

```
Piano y_ratio = 0.62 (예시)
crop: y = [페이지높이 × (0.62 - 0.08), 페이지높이 × (0.62 + 0.05)]
      x = [전체 너비]
```

### 프롬프트

```
이 이미지는 악보의 마디 {start}~{end}에 해당하는 Piano 파트야.
현재 조성: {key}, 박자: {time_sig}

보표 위에 적힌 코드 심볼을 모두 추출해줘.
각 코드의 마디 번호는 이미지 왼쪽 숫자를 기준으로 확인해.

JSON:
[
  {"measure": 1, "beat": 1.0, "chord": "G", "confidence": 0.98},
  {"measure": 2, "beat": 1.0, "chord": "Gmaj7", "confidence": 0.95}
]

읽기 어려운 경우 confidence를 낮게 (< 0.7) 표시해.
```

### 출력

```python
@dataclass
class RawChord:
    measure: int
    beat: float
    chord_text: str          # 원문 그대로 ("Gmaj7", "D/F#")
    confidence: float
    source_page: int
    source_system: int
```

---

## 5. Pass 2b — 음표 추출

### 목적
각 악기 파트의 음표를 추출한다.  
이 단계가 외부 OMR을 완전히 대체한다.

### 난이도 현실 인식

음표 추출은 코드 심볼보다 훨씬 어렵다.

| 항목 | 코드 심볼 | 음표 |
|------|---------|------|
| 정보 표현 | 텍스트 | 위치(피치) + 형태(길이) |
| 모호성 | 낮음 | 높음 (임시표, 붙임줄, 점음표) |
| 컨텍스트 의존 | 조성 | 조성 + 클레프 + 전 마디 상태 |
| 예상 초기 정확도 | 85%+ | 60-75% (검수 필수) |

→ 음표 추출의 목표는 "완벽"이 아니라 "검수 가능한 초안"

### 악기 우선순위

MVP에서 전체 오케스트라 모든 악기를 완벽하게 읽는 건 비현실적.  
우선순위를 정한다:

```
Tier 1 (MVP): Piano (treble + bass), Melody 파트 1개
Tier 2:       현악 (Violin I, II, Viola, Cello, Bass)
Tier 3:       관악 (Flute, Oboe, Clarinet, Bassoon)
Tier 4:       금관 + 타악
```

실사용자 입장에서 Piano + Melody가 있으면 일단 쓸 수 있음.

### 크로핑

Pass 1의 y_ratio를 사용해서 악기별로 개별 crop.

```
Violin I y_ratio = 0.78
crop: y = [페이지높이 × 0.76, 페이지높이 × 0.82]
```

### 프롬프트

```
이 이미지는 {instrument} 파트의 마디 {start}~{end}야.
클레프: {clef}, 조성: {key} (Concert pitch: {concert_key}), 박자: {time_sig}
이조악기인 경우 표기음과 concert pitch의 차이: {transposition}반음

이 파트의 모든 음표를 추출해줘.
쉼표도 포함. 붙임줄(tie)로 연결된 음표는 tie: true로 표시.

JSON:
[
  {
    "measure": 1,
    "beat": 1.0,
    "pitch": "G4",        // concert pitch 기준, 쉼표는 "rest"
    "duration": "quarter",
    "dots": 0,
    "tie_start": false,
    "tie_end": false,
    "voice": 1,
    "confidence": 0.9
  }
]

duration 종류: whole, half, quarter, eighth, 16th, 32nd
읽기 어려운 경우 confidence 낮게.
```

### 출력

```python
@dataclass
class RawNote:
    measure: int
    beat: float
    pitch: str               # "G4", "F#3", "rest"
    duration: str            # "quarter", "eighth" 등
    dots: int                # 0, 1, 2
    tie_start: bool
    tie_end: bool
    voice: int               # 1 or 2
    confidence: float
    part_id: str
    source_system: int
```

---

## 6. Pass 3 — 음악 이론 검증

### 목적
Pass 2a, 2b 결과를 음악적 규칙으로 검사해서 오류를 걸러낸다.  
기존 어떤 도구도 이 단계가 없다 — 우리의 핵심 차별점.

### 3-A: 코드 검증

**Rule 1 — 다이아토닉 분류**
```python
def classify_chord(chord: ChordSymbol, key: str) -> str:
    # 반환: "diatonic" | "borrowed" | "secondary_dominant" | "chromatic"
    # chromatic → confidence 하향, 조표 변화 가능성 플래그
```

**Rule 2 — 근음 도약 검사**
```
연속 코드 사이 근음 거리 (반음):
0–5   → 정상
6–7   → confidence 소폭 하향
8+    → 마디번호 shift 가능성 플래그 + 재확인 요청
```

**Rule 3 — 저신뢰도 재평가 (2차 Claude 호출)**
```
대상: confidence < 0.7
프롬프트: "앞 코드 {prev}, 뒤 코드 {next}, 키 {key}. 
          이 코드가 '{chord}'로 읽혔는데 맞나? 
          다른 가능성이 있다면?"
→ 결과가 다르면 두 후보 모두 검수 UI에 제시
```

### 3-B: 음표 검증

**Rule 4 — 마디 내 duration 합산**
```
마디 내 음표 duration 합 ≠ 박자표 × 1마디 분량
→ 음표 누락/중복 플래그
예: 4/4인데 5박치 음표가 있으면 즉시 플래그
```

**Rule 5 — 피치 범위 검사**
```
악기별 정상 음역 테이블:
예) Violin I: G3–A7
    Piano treble: A0–C8
범위를 벗어난 음 → confidence 하향 + 플래그
```

**Rule 6 — 음표-코드 일관성**
```
각 마디의 코드와 그 마디 멜로디 음들이 코드 톤을 포함하는가?
멜로디가 코드 톤을 전혀 포함 안 하면 → 둘 중 하나가 틀릴 가능성
(강박 음이 코드 톤이 아닌 경우 플래그)
```

### 출력 — ValidatedScore

```python
@dataclass
class ValidatedChord:
    measure: int
    beat: float
    chord_text: str
    normalized: ChordSymbol
    confidence: float
    flags: list[str]          # "chromatic", "large_leap", "low_confidence"
    alternatives: list[str]
    needs_review: bool

@dataclass
class ValidatedNote:
    measure: int
    beat: float
    pitch: str
    duration: str
    dots: int
    tie_start: bool
    tie_end: bool
    voice: int
    confidence: float
    flags: list[str]          # "out_of_range", "duration_mismatch"
    needs_review: bool
    part_id: str

@dataclass
class ValidatedScore:
    layout: ScoreLayout
    chords: list[ValidatedChord]
    notes: dict[str, list[ValidatedNote]]   # part_id → notes
    review_count: int
```

---

## 7. MusicXML 빌더

외부 OMR 뼈대 없이 ValidatedScore 전체에서 MusicXML을 직접 생성한다.

### 구조

```python
class MusicXMLBuilder:
    def build(self, score: ValidatedScore) -> str:
        root = self._build_header(score.layout)
        for part in score.layout.parts:
            part_el = self._build_part(part, score)
            root.append(part_el)
        self._validate_schema(root)       # MusicXML 4.0 XSD 검증
        return ET.tostring(root)

    def _build_part(self, part, score):
        notes = score.notes.get(part.id, [])
        chords = score.chords if part.name == "Piano" else []
        # 마디별로 <measure> 생성
        # needs_review=True인 항목은 <notations><technical><footnote> 태그로 마킹

    def _validate_schema(self, root):
        # musicxml 4.0 xsd로 lxml 검증
        # 실패 시 어느 마디가 문제인지 구체적으로 보고
```

### 검수 마킹
`needs_review=True`인 요소는 MusicXML 주석으로 마킹 → 검수 UI에서 하이라이트.

```xml
<!-- REVIEW: confidence=0.45, flags=chromatic,large_leap -->
<harmony>
  <root><root-step>F</root-step></root>
  <kind>minor</kind>
</harmony>
```

---

## 8. 데이터 모델 요약

```python
# 파이프라인 상태
class PipelineStatus(Enum):
    PENDING       = "pending"
    RENDERING     = "rendering"
    PASS1_DONE    = "pass1_done"
    PASS2_DONE    = "pass2_done"
    PASS3_DONE    = "pass3_done"
    BUILDING      = "building"
    REVIEW        = "awaiting_review"
    DONE          = "done"

@dataclass
class ScoreDocument:
    id: str
    source_pdf: str
    pages: int
    status: PipelineStatus
    layout: ScoreLayout | None
    validated_score: ValidatedScore | None
    musicxml_draft: str | None
    review_count: int          # needs_review 항목 수
```

---

## 9. 기술 스택

| 역할 | 선택 | 이유 |
|------|------|------|
| PDF → 이미지 | `pdftoppm` (poppler) | 300dpi 안정, 설치됨 |
| 이미지 crop/resize | `Pillow` | 경량 |
| AI 인식 (Pass 1/2/3) | Claude claude-opus-4-7 multimodal | 스파이크 검증 |
| MusicXML 생성 | 직접 XML (`lxml`) | 스키마 검증 포함 |
| 스키마 검증 | MusicXML 4.0 XSD + `lxml` | export 안정성 |
| 음악 이론 검증 | 자체 구현 Python | 키→스케일→다이아토닉 |
| 서버 | FastAPI | 비동기 파이프라인 |
| 프론트엔드 | React | 검수 UI |

---

## 10. 정확도 목표

| 항목 | 현재 (단일 패스) | 목표 (3-pass) |
|------|----------------|--------------|
| 마디번호 정확도 | ~56% (shift 발생) | 95%+ |
| 코드 심볼 (컨텍스트 있음) | 95% (Page 1) | 85% 전체 평균 |
| 음표 (Tier 1: Piano/Melody) | 미측정 | 70%+ |
| 검수 필요 항목 비율 | — | 전체의 15% 이하 |
| 검수 시간 목표 | — | A4 1페이지 5분 이내 |

---

## 11. 구현 순서

### Sprint 1 — Pass 1 + Pass 2a (코드 파이프라인)
1. 300dpi 렌더링
2. Pass 1 (구조 분석) 구현 + ScoreLayout 검증
3. Piano crop + Pass 2a (코드 추출)
4. 정확도 재측정 목표: 코드 85%+

### Sprint 2 — Pass 2b (음표 추출, Tier 1)
1. Piano treble/bass 음표 추출
2. Melody 파트 1개 음표 추출
3. Pass 3-B (duration 합산, 피치 범위 검사) 구현

### Sprint 3 — Pass 3 완성 + MusicXML 빌더
1. Pass 3-A (코드 이론 검증) 완성
2. Rule 6 (음표-코드 일관성) 구현
3. MusicXML 빌더 + 스키마 검증

### Sprint 4 — 검수 UI + 실전 테스트
1. 검수 UI (needs_review 항목 + 원본 PDF)
2. CEO와 실전 테스트
3. Tier 2 악기 (현악) 추가

---

## 12. 오픈 퀘스천

| # | 질문 | 영향 |
|---|------|------|
| 1 | Pass 1 y_ratio가 페이지마다 안정적으로 나오는가? | crop 정확도 |
| 2 | 음표 추출에서 이조악기(Bb클라리넷 등) concert pitch 변환을 Claude에게 맡길 것인가, 파이프라인에서 처리할 것인가? | Pass 2b 설계 |
| 3 | 음표-코드 일관성 Rule 6이 실제로 오류를 잡아내는가? | Pass 3 비용 vs 효과 |
| 4 | Tier 1만으로 CEO가 실제로 쓸 수 있는 파일이 나오는가? | Sprint 우선순위 |
| 5 | 2차 Claude 호출 (Rule 3 재평가) 비용이 허용 가능한가? | 과금 구조 |

---

*연관 문서: `lead_sheet_cleanup_mvp_prd.md`, `juchan-main-design-20260420-151735.md`*
