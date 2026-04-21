# PDF Score → MusicXML: Technical Design

> 작성일: 2026-04-22  
> v4: 리뷰 반영 — Piano treble/bass 구분, 클레프 수집, active_parts 변환, 반복기호/리허설마크, 다이어그램 수정, JSON 실패 처리

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
 ├─▶ [Pass 2b] 음표 추출  ───────────────── per system × Tier 묶음 호출
 │     Tier 단위 crop + 클레프/조표/박자 컨텍스트
 │     pitch(written), duration, voice
 │     → RawNoteList (이조 변환은 파이프라인 코드에서)
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

### JSON 파싱 실패 처리

모든 Claude 호출 응답에 공통 적용:

```python
def parse_response(raw: str, context: str) -> dict | None:
    # 1차: JSON 블록 추출 시도
    match = re.search(r'```json\s*([\s\S]*?)```|(\[[\s\S]*\]|\{[\s\S]*\})', raw)
    if match:
        try:
            return json.loads(match.group(1) or match.group(2))
        except json.JSONDecodeError:
            pass
    # 2차: "JSON만 다시 출력해줘" 재요청 (1회)
    retry = claude_call(f"이전 응답을 JSON 형식으로만 다시 출력해줘. 설명 없이.\n\n{raw}")
    try:
        return json.loads(retry)
    except json.JSONDecodeError:
        # 최종 실패 → 해당 단위 전체 needs_review 마킹
        log.warning(f"JSON parse failed: {context}")
        return None
```

---

## 3. Pass 1 — 구조 분석

### 목적
이후 모든 패스의 좌표 앵커와 음악 컨텍스트를 확정한다.

### 두 단계로 나눔

**Step 1-A: 악기 목록 + 파트 순서**
첫 페이지에만 실행. 악기명과 위에서 아래 순서만 읽는다.  
y 좌표는 읽지 않는다 — 페이지마다 쉬는 악기 파트가 생략되거나 보표 간격이 달라지므로 절대/비율 좌표는 신뢰할 수 없다.

```
프롬프트:
"이 악보의 첫 페이지야.
왼쪽 끝에 적힌 악기 이름을 모두 읽어줘.
위에서 아래 순서대로, 각 악기의 클레프(treble/bass/alto/tenor)도 함께 알려줘.
Piano처럼 treble + bass 두 단이 있는 악기는 두 줄로 나눠서 표기해.

JSON:
{
  'parts': [
    {'name': 'Piccolo',      'order': 0, 'clef': 'treble'},
    {'name': 'Flute',        'order': 1, 'clef': 'treble'},
    {'name': 'Piano treble', 'order': 14, 'clef': 'treble'},
    {'name': 'Piano bass',   'order': 15, 'clef': 'bass'},
    ...
  ]
}"
```

파이프라인에서 이름 기반으로 ID를 자동 부여한다:
```python
parts = [PartInfo(id=f"P{i}", name=p["name"], order=p["order"], clef=p["clef"],
                  transposition_semitones=TRANSPOSITION_TABLE.get(p["name"], 0))
         for i, p in enumerate(response["parts"])]
name_to_id = {p.name: p.id for p in parts}
```

**Step 1-B: 페이지별 시스템 구조**
모든 페이지에 실행. 음표/코드는 읽지 않음.  
y 좌표는 시스템 단위(가로 줄 전체)만 받는다 — 악기별 내부 위치는 "시스템 안에서 위에서 N번째 파트"로 처리한다.

```
프롬프트:
"이 악보 페이지에서 구조 정보만 추출해줘. 음표와 코드는 무시해.

1. 시스템 수 (가로 줄 수)
2. 각 시스템의 첫 마디 번호 (시스템 왼쪽 끝에 인쇄된 숫자)
3. 조표 변화: 몇 번 마디에서 변하는지, 바뀐 조성 (Concert pitch 기준)
4. 박자표 변화 (있으면)
5. 각 시스템에 포함된 악기 목록 (이 시스템에서 쉬어서 생략된 파트가 있으면 제외)
6. 리허설 마크: 박스/원 안에 A, B, C 등 문자가 있는 마디 번호
7. 반복 기호: repeat barline(시작/끝), 1st/2nd ending(볼타 브라켓) 위치

JSON:
{
  'systems': [
    {
      'start_measure': 1,
      'key': 'G major',
      'time': '4/4',
      'y_top_px': 120,
      'y_bottom_px': 450,
      'active_parts': ['Piccolo', 'Flute', 'Piano treble', 'Piano bass', ...],
      'rehearsal_marks': [{'measure': 1, 'label': 'A'}],
      'repeat_barlines': [{'measure': 5, 'type': 'start'}, {'measure': 21, 'type': 'end'}],
      'volta_brackets': [{'start_measure': 20, 'end_measure': 21, 'number': 1}]
    }
  ]
}"
```

y 좌표는 픽셀 절대값으로 받는다. 비율은 페이지 크기가 바뀌면 쓸 수 없다.  
`active_parts`는 악기 이름으로 받고, 파이프라인에서 `name_to_id`로 ID 변환한다:
```python
system.active_parts = [name_to_id[n] for n in raw["active_parts"] if n in name_to_id]
```

### 출력 — ScoreLayout

```python
@dataclass
class PartInfo:
    id: str                  # "P1", "P2" 등
    name: str                # "Piccolo", "Piano" 등
    order: int               # 위에서 아래 순서 (0-based)
    clef: str                # "treble", "bass", "alto", "tenor"
    transposition_semitones: int  # written→concert pitch 반음 수. 코드로 처리, Claude 미사용
                                  # Bb악기: -2, F악기: -7, Eb악기: -9, Concert: 0

# 이조악기 테이블 (결정론적, Claude에게 맡기지 않음)
TRANSPOSITION_TABLE = {
    "Clarinet in Bb": -2, "Trumpet in Bb": -2,
    "Horn in F": -7,
    "Clarinet in Eb": 3,
    "Piccolo": 12,
    "Contrabass": -12,
}

@dataclass
class RehearsalMark:
    measure: int
    label: str               # "A", "B", "C" 등

@dataclass
class RepeatBarline:
    measure: int
    type: str                # "start" | "end" | "end-start"

@dataclass
class VoltaBracket:
    start_measure: int
    end_measure: int
    number: int              # 1 or 2

@dataclass
class SystemInfo:
    page: int
    system_index: int
    start_measure: int
    end_measure: int         # 다음 시스템 start - 1
    key: str                 # concert pitch 기준 "G major", "Ab minor"
    time_signature: str      # "4/4", "3/8"
    y_top_px: int            # 픽셀 절대값
    y_bottom_px: int
    active_parts: list[str]  # 이 시스템에 실제 보표가 있는 파트 id 목록
    rehearsal_marks: list[RehearsalMark] = field(default_factory=list)
    repeat_barlines: list[RepeatBarline] = field(default_factory=list)
    volta_brackets: list[VoltaBracket] = field(default_factory=list)

@dataclass
class ScoreLayout:
    parts: list[PartInfo]
    systems: list[SystemInfo]
    total_measures: int
```

### Piano treble/bass 처리

Piano는 두 단이 항상 붙어 있으므로, Step 1-A에서 `Piano treble`과 `Piano bass`를 별도 파트로 등록한다. `active_parts`에서도 두 파트가 연속해서 나타나는 것이 보장된다.

crop 시 Piano 영역 전체(treble + bass 합산)를 한 번 crop한 뒤, Pass 2a/2b 프롬프트에서 "위쪽이 treble, 아래쪽이 bass"로 구분해서 읽도록 한다:

```python
def crop_piano_both(page_img, system, treble_order, bass_order) -> Image:
    # treble 시작부터 bass 끝까지 한 번에 crop
    system_h = system.y_bottom_px - system.y_top_px
    n = len(system.active_parts)
    part_h = system_h / n
    y_top = system.y_top_px + part_h * treble_order - 20  # 코드 심볼 여백
    y_bot = system.y_top_px + part_h * (bass_order + 1) + 5
    return page_img.crop((0, y_top, page_img.width, y_bot))
```

### 실패 처리
- 마디번호 미인식 → 이전 시스템 끝 + 1로 추정, 플래그
- 조성 미인식 → 이전 시스템 조성 유지
- `name_to_id` 매핑 실패 (이름 불일치) → fuzzy match 후 로그, 실패 시 해당 파트 스킵 + 플래그

---

## 4. Pass 2a — 코드 심볼 추출

### 목적
Piano 파트 위의 코드 심볼을 정확하게 추출한다.

### 크로핑 전략
Pass 1에서 시스템의 `y_top_px`, `y_bottom_px`를 알고 있고, Piano가 시스템 내 몇 번째 파트인지도 안다.  
시스템 높이를 `active_parts` 수로 균등 분할해서 Piano 위치를 추정한다.

```python
def crop_piano(page_img, system: SystemInfo, part_order: int) -> Image:
    system_h = system.y_bottom_px - system.y_top_px
    n_parts = len(system.active_parts)
    part_h = system_h / n_parts
    
    piano_y_top = system.y_top_px + part_h * part_order - 20   # 코드 심볼 여백 포함
    piano_y_bot = system.y_top_px + part_h * (part_order + 1) + 5
    return page_img.crop((0, piano_y_top, page_img.width, piano_y_bot))
```

파트가 균등 분할되지 않을 수 있으므로 초기 crop 실패 시 ±10% 여백으로 재시도.

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

### ⚠️ Sprint 2 시작 전 스파이크 필수
Pass 2b는 아직 정확도 데이터가 없다. Sprint 1 완료 후, Piano 1마디로 스파이크를 먼저 실행하고 정확도 60% 이상일 때만 Sprint 2 진행.

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
→ **Sprint 1 완료 후 CEO에게 확인 필요: "Tier 1만으로 쓸 수 있나요?"**

### API 호출 방식

악기별 개별 호출은 너무 많다 (25악기 × 16시스템 = 400 calls).  
**시스템 단위로 Tier 묶어서 호출한다:**

```
호출 1: 이 시스템의 Piano treble + bass (2 파트 동시)
호출 2: 이 시스템의 Melody 파트 (1 파트)
---
Tier 1 기준: 16시스템 × 2 calls = 32 calls (전곡 기준)
```

### 크로핑

Pass 2a와 동일한 방식 (시스템 y_top/y_bottom + 파트 순서로 균등 분할).

### 프롬프트

```
이 이미지는 마디 {start}~{end}에 해당하는 악보야.
{instruments} 파트들의 음표를 추출해줘.

조성: {key} (concert pitch 기준), 박자: {time_sig}
클레프: {clef_map}  (예: "Piano treble: treble, Piano bass: bass")

중요: 음표는 악보에 적힌 그대로의 음이름(written pitch)으로 출력해.
이조 변환은 하지 않아도 돼.

쉼표 포함. 붙임줄(tie)은 tie_start/tie_end로 표시.

JSON:
{
  "Piano treble": [
    {"measure": 1, "beat": 1.0, "pitch": "G4", "duration": "quarter",
     "dots": 0, "tie_start": false, "tie_end": false, "voice": 1, "confidence": 0.9}
  ],
  "Piano bass": [...]
}

duration 종류: whole, half, quarter, eighth, 16th, 32nd
읽기 어려우면 confidence 낮게.
```

이조 변환은 파이프라인 코드에서 `TRANSPOSITION_TABLE`로 처리한다.

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

**Rule 6 — 음표-코드 일관성 (Sprint 4 이후)**
```
비화성음(passing tone, suspension 등)이 일반적이어서 오탐 비율이 높음.
MVP에서는 구현하지 않는다.
향후: 강박(beat 1)의 melody 음이 코드 구성음에 전혀 없을 때만 플래그.
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
        # needs_review=True인 항목은 XML 주석으로 마킹 (<!-- REVIEW: ... -->)

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
2. Pass 1 구현 + ScoreLayout 검증 (마디번호, 조성, 픽셀 좌표)
3. Piano crop + Pass 2a (코드 추출, 컨텍스트 주입)
4. 정확도 재측정 목표: 코드 85%+
5. **[스파이크] Pass 2b 예비 테스트 — Piano 1마디 음표 추출 정확도 측정**
6. **[CEO 검증] "코드 심볼만 있는 MusicXML이면 쓸 수 있나요?"**  
   → Yes: Sprint 2 진행 / No 또는 Tier 1으로 충분: 방향 재검토

### Sprint 2 — Pass 2b (음표 추출, Tier 1) — 스파이크 통과 시
1. Piano treble/bass 음표 추출 (시스템 단위 그룹 호출)
2. Melody 파트 1개 음표 추출
3. 이조 변환 파이프라인 코드 구현 (TRANSPOSITION_TABLE 기반)
4. Pass 3-B (duration 합산 검증, 피치 범위 검사) 구현

### Sprint 3 — Pass 3-A 완성 + MusicXML 빌더
1. Pass 3-A (코드 이론 검증) 완성
2. MusicXML 빌더 + lxml 스키마 검증
3. needs_review XML 주석 마킹

### Sprint 4 — 검수 UI + Tier 2 + Rule 6
1. 검수 UI (needs_review 항목 + 원본 PDF 나란히)
2. CEO와 실전 테스트 (전곡 end-to-end)
3. Tier 2 악기 (현악) 추가
4. Rule 6 (음표-코드 일관성, 강박 기준) 구현

---

## 12. 오픈 퀘스천

| # | 질문 | 영향 | 해결 시점 |
|---|------|------|---------|
| 1 | 균등 분할 crop이 실제로 파트 위치를 잡아내는가? | Pass 2a/2b crop 정확도 | Sprint 1 |
| 2 | Pass 2b 스파이크: 음표 추출 정확도 60% 달성 가능한가? | Sprint 2 진행 여부 | Sprint 1 말 |
| 3 | Tier 1만으로 CEO가 실제로 쓸 수 있는 파일이 나오는가? | Sprint 2 이후 방향 | Sprint 1 말 CEO 검증 |
| 4 | 2차 Claude 호출 (Rule 3 재평가) 비용이 허용 가능한가? | 과금 구조 | Sprint 3 |
| 5 | 시스템 단위 그룹 호출이 개별 호출보다 정확도가 낮아지는가? | Pass 2b 호출 방식 | Sprint 2 |

---

*연관 문서: `lead_sheet_cleanup_mvp_prd.md`, `juchan-main-design-20260420-151735.md`*
