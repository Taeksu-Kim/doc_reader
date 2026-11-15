# doc_index — 프로젝트 규칙

공통 규칙은 상위 `../CLAUDE.md`를 따른다. 이 파일은 doc_index 전용.

- 여러 에이전트가 쓰는 컴포넌트. 공개 API(`Index.build/search/keyword/vector/hybrid`, `Hit`, `files_of`, `OpenAICompatEmbedder`)를
  바꾸면 쓰는 쪽 테스트까지 돌린다.
- 의존: doc_chunker → doc_parser 한 방향. 읽기·자르기 결함은 그쪽에서 고친다.
- 검색 방식(토큰화·결합·가중치)을 바꾸면 `tools/`의 정확도 측정을 다시 돌려 README 표와 비교한다. 숫자는 측정일과 함께 갱신.
- 테스트에서 가짜로 바꾸는 것은 임베딩 서버(네트워크)뿐.
- 화면·서버는 doc_reader에 있다. 여기 `service.py`는 그 서버의 자료실 데스크 — 바꾸면 doc_reader `tests/test_library.py`.
- 설치: `pip install -e . --no-deps`
