# doc_parser — 프로젝트 규칙

공통 규칙은 상위 `../CLAUDE.md`를 따른다. 이 파일은 doc_parser 전용.

- **여러 에이전트가 쓰는 컴포넌트다.** 공개 API(`parse_file`, `register_parser`, `Block`/`Loc`/`ParsedDoc` 필드)를 바꾸면
  쓰는 쪽(doc_chunker, doc_writer)이 깨진다. 바꿀 때는 그쪽 테스트까지 돌린다.
- 하는 일은 "읽기"까지. 자르기·색인·LLM은 넣지 않는다 (같은 파일 → 같은 블록).
  예외는 opt-in 모델 두 가지(OCR `ocr=`/`--ocr-url`, 레이아웃 `layout=`/`--layout-url`, 2026-11-14 사용자 요청):
  기본 경로는 모델을 절대 부르지 않고,
  모델은 별도 env의 HTTP 서버(`tools/ocr_server.py`) 뒤에만 둔다. paddle 등 모델 의존성을 pyproject에 넣지 않는다.
  OCR을 고치면 README "OCR 옵션"의 실측(원본 vs 래스터 스캔본, 3회 동일)을 다시 돈다.
- 다른 프로젝트에 의존하지 않는다. 시험 파일은 테스트 안에서 각 형식 라이브러리로 만든다.
- 파서 결함을 고치면 `../doc_chunker/tools/README.md`의 긴 문서 회귀도 돌린다 (표식·표 행 누락/중복·머리 행 없는 조각 0).
- 설치: `pip install -e .` (stock-dataset 환경에 editable 설치됨)
- 화면·서버는 doc_reader에 있다(`../doc_reader/`, `../CLAUDE.md`). 여기 `service.py`는 그 서버의 읽기 데스크, `a2a.py`는 도메인 코드 없는
  공용 A2A 배관(doc_reader·doc_writer·meal_log가 import — 바꾸면 그쪽 테스트도). 웹 의존성은 `[app]` extra(fastapi)에만.
