# doc_reader — 프로젝트 규칙

공통 규칙은 상위 `../CLAUDE.md`. 부품별 규칙은 `doc_parser/CLAUDE.md` · `doc_chunker/CLAUDE.md` · `doc_index/CLAUDE.md`.

- **서버는 하나**(`python -m doc_reader serve`, 8767): 화면 3개(/library · /read · /chunk)와 A2A 에이전트 하나. 부품에 서버·화면을 다시 만들지 않는다.
- 층: 부품의 `service.py`(상태·작업) ← `doc_reader/{read,chunk,library}.py`(그 영역의 API·A2A 작업 보기) ← `doc_reader/app.py`(묶기·A2A 창구·영역 간 연결).
  영역 간 연결은 서버 안에서 함수로(예: 자료실 → 읽기 검수 `open_doc`). HTTP로 자기 자신을 부르지 않는다.
- 화면은 `doc_reader/static/{library,read,chunk}.html`. API 경로는 상대 경로(`api/...`), 상단 바·색은 세 화면 같게(공통 CSS 블록).
  화면을 바꾸면 해당 `tools/check_*.py`, 서버를 바꾸면 `tests/`를 돌린다.
- A2A: 카드는 `app.py`의 `CARD`(기능은 각 영역 모듈의 `SKILL(S)`), 메시지 배정 규칙은 `ReaderTasks._area`. 기능을 추가하면
  `tests/test_reader.py`의 배정·목록 검사에 넣는다. A2A 배관은 `doc_parser.a2a`(doc_writer·meal_log도 씀 — 바꾸면 그쪽 테스트도).
- 부품 설치는 compat 방식(`--config-settings editable_mode=compat`): 루트의 `doc_parser/` 폴더가 패키지를 가리는 문제 때문 (README).
- 의존: doc_index → doc_chunker → doc_parser 한 방향, 부품은 doc_reader를 import하지 않는다.
