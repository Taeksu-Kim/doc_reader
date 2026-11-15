# doc_chunker — 프로젝트 규칙

공통 규칙은 상위 `../CLAUDE.md`를 따른다. 이 파일은 doc_chunker 전용.

- **여러 에이전트가 쓰는 컴포넌트다.** 공개 API(`chunk`, `parse_and_chunk`, `Unit` 필드, `count=`)를 바꾸면 쓰는 쪽(doc_writer)이
  깨진다. 바꿀 때는 doc_writer 테스트까지 돌린다.
- 의존은 doc_parser 한 방향뿐. 읽기 결함은 doc_parser에서 고친다(여기서 우회하지 않는다).
- LLM·네트워크를 넣지 않는다 (같은 입력 → 같은 단위).
- 분할 규칙을 바꾸면 긴 문서 회귀(`tools/README.md`)를 돌린다.
- 화면·서버는 doc_reader에 있다. 여기 `service.py`는 그 서버의 미리보기 데스크 — 바꾸면 doc_reader `tests/test_chunk.py`.
- 설치: `pip install -e . --no-deps` (doc_parser는 먼저 editable 설치)
