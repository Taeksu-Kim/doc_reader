# 긴 문서 회귀 검사 (수동)

파서를 고친 뒤 실행. 215쪽 가상 매뉴얼(장 20 · 절 160 · 장마다 60행 표 · 500행 부록 표)로 표식·표 행·머리 행을 센다.

```bash
PY=/home/ubuntu/miniconda3/envs/stock-dataset/bin/python
mkdir -p out/longdoc
$PY tools/make_longdoc.py out/longdoc/manual.docx
# Word(Windows)로 PDF 두 종(책갈피 有/無) — 선택
cp tools/export_pdf.ps1 out/longdoc/ && powershell.exe -NoProfile -ExecutionPolicy Bypass \
  -File 'D:\workspace\side_projects\doc_chunker\out\longdoc\export_pdf.ps1' -dir 'D:\workspace\side_projects\doc_chunker\out\longdoc'
for f in manual.docx manual_bookmarks.pdf manual_plain.pdf; do
  $PY -m doc_chunker out/longdoc/$f --out out/longdoc/${f%.*}_${f##*.}.jsonl; done
$PY tools/check_long.py out/longdoc/*.jsonl
```

기대값 (2026-11-13): 세 파일 모두 표식 160/160·중복 0·장 불일치 0, 부록 500/500·중복 0, 표 조각 25·머리 행 없는 조각 0.
