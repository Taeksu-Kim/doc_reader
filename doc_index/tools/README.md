# 검색 정확도 측정 (재현)

```bash
PY=/home/ubuntu/miniconda3/envs/stock-dataset/bin/python
IP=$(hostname -I | awk '{print $1}')
$PY tools/make_corpus.py --targets ../doc_writer/samples/nurisense --out out/corpus            # 1,968개 (seed 42)
$PY -m doc_index build out/corpus --db out/idx.sqlite --embed-url http://$IP:8011/v1         # CPU 임베딩 약 9분
$PY tools/rewrite_queries.py --queries tools/queries_nurisense.json --out out/rewrites.json \
    --llm-url http://$IP:8002/v1 --model qwen27b                                              # LLM 검색어 (선택)
$PY tools/eval_retrieval.py --db out/idx.sqlite --queries tools/queries_nurisense.json \
    --embed-url http://$IP:8011/v1 [--rewrites out/rewrites.json] [--detail]
```

- `queries_nurisense.json`: 질의, 필요한 파일(`need`), 함정 파일(`trap`: 구버전·잠정본·옛 일정)
- 2026-11-13 결과는 `../README.md`의 표. LLM 검색어는 vLLM `qwen27b`(Qwen3.8-27B-INT4, thinking off, temperature 0)
