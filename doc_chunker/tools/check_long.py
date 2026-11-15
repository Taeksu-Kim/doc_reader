import json, re, sys
from collections import Counter
for path in sys.argv[1:]:
    units = [json.loads(l) for l in open(path, encoding="utf-8")]
    marks, eq = Counter(), Counter()
    wrong_chapter, table_parts, bad_header, bad_rows = 0, 0, 0, 0
    for u in units:
        for c, s in re.findall(r"MARK-(\d\d)-(\d\d)", u["text"]):
            marks[(c, s)] += 1
            if not any(f"제{int(c)}장" in h.replace(" ", "") for h in u["heading_path"]) and f"제{int(c)}장" not in u["text"].replace(" ", ""):
                wrong_chapter += 1
        eq.update(re.findall(r"\| (EQ-\d{4}) \|", u["text"]))
        for tbl in re.findall(r"((?:^\|.*\|\n?)+)", u["text"], flags=re.M):
            rows = [r for r in tbl.strip().split("\n") if not r.startswith("|---")]
            table_parts += 1
            head = rows[0]
            if ("EQ-" in tbl or "점검 " in tbl) and not ("설비번호" in head or "항목" in head):
                bad_header += 1
            widths = {r.count("|") for r in rows}
            bad_rows += len(widths) > 1
    toks = [u["tokens"] for u in units]
    print(f"{path.split('/')[-1]}: 단위 {len(units)}, 토큰 최대 {max(toks)} / 평균 {sum(toks)//len(toks)}")
    print(f"  표식 160개 중 발견 {len(marks)}, 중복 {sum(1 for v in marks.values() if v > 1)}, 장 불일치 {wrong_chapter}")
    print(f"  부록 설비 500행 중 발견 {len(eq)}, 중복 {sum(1 for v in eq.values() if v > 1)}")
    print(f"  표 조각 {table_parts}, 머리행 없는 조각 {bad_header}, 열 수 어긋난 조각 {bad_rows}")
    print(f"  첫 단위 머리: {units[0]['text'].splitlines()[0][:110]}")
    print(f"  중간 단위 머리: {units[len(units)//2]['text'].splitlines()[0][:110]}")
    print(f"  문서 정보 붙은 단위: {sum('문서 정보:' in u['text'] for u in units)}")
