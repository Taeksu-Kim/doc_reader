# doc_reader — 읽기 에이전트

문서를 **읽고**(doc_parser) → 검색 단위로 **자르고**(doc_chunker) → 자료 폴더에서 **찾는**(doc_index) 에이전트.
문서를 쓰는 [doc_writer](https://github.com/Taeksu-Kim/doc_writer)의 짝으로, 라이터가 쓸 근거를 리더가 읽고 찾아 줍니다.

같은 기능을 세 가지로 씁니다 (`../reference/AGENT_PRODUCT.md`의 공통 구조):

| 쓰임 | 어떻게 |
|---|---|
| **import** | `from doc_parser import parse_file` · `from doc_chunker import chunk` · `from doc_index import Index` — 부품은 따로 설치·사용 가능 |
| **화면** | `python -m doc_reader serve` → http://127.0.0.1:8767 — 자료실 검색 · 자료 관리 · 읽기 검수 · 청크 미리보기 |
| **A2A** | 같은 서버의 에이전트 카드 하나(`/.well-known/agent-card.json`), `POST /a2a` |

## 구성

```
doc_reader/
  doc_reader/    서버 하나: 화면 3개 + A2A 창구 (app.py · read.py · chunk.py · library.py · static/)
  doc_parser/    읽기: 파일 → 제목·본문·표·그림 블록(쪽·위치), 선택 레이아웃·OCR      README 참조
  doc_chunker/   자르기: 블록 → 구조 단위(머리·문서 정보 포함), 큰 표는 머리 행 반복      README 참조
  doc_index/     찾기: 키워드(한국어 2글자 BM25) + 벡터 + 혼합, SQLite 한 파일, 증분      README 참조
  tests/         서버 E2E (영역별 + 에이전트 전체)
  tools/         브라우저 점검 (check_read · check_chunk · check_library)
```

의존 방향: doc_index → doc_chunker → doc_parser, 서버(doc_reader)는 셋 위에. 각 부품의 상태(작업 대기열·SQLite)는
그 부품의 `service.py`에 있고, 서버는 화면·API·A2A만 얹습니다.

## 설치·실행

```bash
PY=/home/ubuntu/miniconda3/envs/stock-dataset/bin/python
R=/mnt/d/workspace/side_projects/doc_reader
# 부품은 compat 방식으로: 저장소 루트의 같은 이름 폴더(doc_parser/)가 패키지를 가리지 않게
$PY -m pip install --no-deps --config-settings editable_mode=compat -e $R/doc_parser -e $R/doc_chunker -e $R/doc_index
$PY -m pip install --no-deps -e $R                      # fastapi·uvicorn·python-multipart 필요 (stock-dataset엔 있음)

$PY -m doc_reader serve        # http://127.0.0.1:8767 (127.0.0.1 고정), 데이터 doc_reader/data/{library,read,chunk}
  # --model-url  레이아웃·OCR (기본 http://<WSL IP>:8090 = doc_parser/tools/ocr.sh) — 꺼져 있으면 그 옵션만 잠김
  # --embed-url  임베딩 (기본 http://<WSL IP>:8011/v1) — 꺼져 있으면 키워드로만 찾음
```

## 화면 (한 서버, 공통 상단 바)

| 주소 | 화면 | 하는 일 |
|---|---|---|
| `/library/#/search` (첫 화면) | 자료실 검색 | 혼합·키워드·의미 검색, 결과마다 파일·절·쪽·찾은 방식·같은 계열 경고, 근거 전체, **읽기 검수에서 보기**(그 쪽으로) |
| `/library/#/library` | 자료 관리 | 자료 폴더 추가·빼기, 증분 색인 진행·기록, 파일별 상태(건너뜀 이유·중복) |
| `/read/` | 읽기 검수 | 문서를 놓으면 읽어 원본 페이지 위에 블록 자리를 겹쳐 대조, 종류·경고 거르기, 다시 읽기(레이아웃·OCR), 내려받기 |
| `/chunk/` | 청크 미리보기 | 상한(토큰)으로 잘라 단위 지도·단위 목록, 상한 바꿔 비교, JSONL |

자료실의 "읽기 검수에서 보기"는 같은 서버 안에서 그 파일을 읽기 데스크에 넣고(같은 파일·같은 방식은 다시 읽지 않음)
`/read/#/docs/<id>?p=N`으로 이동합니다.

## A2A (1.0, JSON-RPC)

에이전트 하나(`doc_reader`)에 기능 4개. 메시지는 `data.skill`로 고르거나, 없으면 내용으로 정합니다:
data `{folder}` → 폴더 추가 · 파일 + `max_tokens` → 자르기 · 파일 → 읽기 · 글만 → 검색.

| skill | 입력 | 결과 (artifacts) | reviewUrl |
|---|---|---|---|
| `parse-document` | file `{raw, filename}` · `{url: file:///…}` · data `{path}`, 옵션 `{layout, ocr}` | markdown · blocks · figures | `/read/#/docs/<id>` |
| `chunk-document` | 위와 같은 파일 + data `{max_tokens}` | units | `/chunk/#/p/<id>` |
| `search-documents` | text = 찾을 말, data `{mode, k}` | hits | `/library/#/search?q=…` |
| `add-folder` | data `{folder}` (기본: 색인 끝까지 기다림) | build | `/library/#/library` |

`GetTask`·`ListTasks`·`CancelTask`는 세 데스크를 함께 봅니다(작업 id로 주인을 찾음, 목록은 최신순). 스트리밍·푸시 알림은 없음.
A2A 배관(`doc_parser.a2a`, 도메인 코드 없음)은 doc_writer·meal_log도 씁니다.

## 검증

```bash
cd /mnt/d/workspace/side_projects/doc_reader
$PY -m pytest -q                              # 서버 E2E 11개 (읽기 3 · 자르기 3 · 찾기 4 · 에이전트 전체 1), 모델·임베딩만 가짜
(cd doc_parser && $PY -m pytest -q)           # 부품 E2E: 12 · (doc_chunker) 3 · (doc_index) 3
$PY tools/check_read.py [PDF]                 # 실제 Chromium: 읽기 40 · 자르기 24 · 찾기 38개 점검, 폭 3종·다크
$PY tools/check_chunk.py
$PY tools/check_library.py
```

2026-11-15: 위 전부 통과, doc_writer 59개·meal_log 85개 테스트도 통과(둘 다 `doc_parser.a2a`·`doc_index`를 씀).
부품별 측정(OCR 실측·검색 정확도·긴 문서 회귀)은 각 부품 README.

## 경로

원래 `side_projects/doc_parser` 등에 있던 부품을 2026-11-15 이 저장소로 묶었습니다. 예전 경로
(`side_projects/doc_parser` · `doc_chunker` · `doc_index`)는 이 폴더를 가리키는 링크로 남겨 둡니다(WSL 안에서만 동작).
이전 저장소(Taeksu-Kim/doc_parser · doc_chunker · doc_index)는 보관 처리.
