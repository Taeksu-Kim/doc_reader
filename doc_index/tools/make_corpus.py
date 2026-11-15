"""Synthetic company drive for retrieval tests: the 12 누리센스 target files + ~2,000 distractors.

Distractors are deliberately close to the targets (hard negatives): other quarters' results and
preliminary versions, older meeting notes that still say "NS-500 출시 2027년 3월", travel rules
v1.0 / v1.5, other vendors' housing quotes, other customers' pilot schedules, other years' hiring
plans... plus ordinary filler (meeting notes, notices, customer visits, FAQs, purchase orders).

    python tools/make_corpus.py --targets ../doc_writer/samples/nurisense --out out/corpus [--seed 42]

Same seed -> same corpus. All companies, people and numbers are fictional.
"""

from __future__ import annotations

import argparse
import random
import shutil
from datetime import date, timedelta
from pathlib import Path

PEOPLE = ["박도윤", "오세훈", "김라온", "이서준", "한지수", "정민아", "윤태호", "최하린", "강도현", "임서윤", "조민재", "송유나"]
TEAMS = ["경영지원팀", "국내영업1팀", "국내영업2팀", "해외영업팀", "HW팀", "펌웨어팀", "플랫폼개발팀", "고객성공팀", "생산품질팀"]
CUSTOMERS = ["동해화학", "한결자동차부품", "세진식품", "대원제지", "미래바이오팜", "그린냉동", "한빛유업", "서해정밀", "태성철강",
             "우진물산", "청림제약", "대한펄프", "삼정에너지", "가온전자", "새봄식품", "명성화학", "보람기계", "하나수산"]
VENDORS = ["대성정밀", "한울사출", "세원몰드", "동진테크", "미래금형", "광명플라스틱", "신영전자", "태광배터리", "에이스PCB", "케이엠통신"]
PRODUCTS = ["NS-200", "NS-300", "NuriWatch", "LoRa 게이트웨이"]
TOPICS = ["배터리 교체 주기", "통신 음영 대응", "대시보드 개선", "알림 규칙 설정", "설치 일정 조율", "연간 점검 계약",
          "센서 브래킷 규격", "데이터 보관 기간", "엑셀 내보내기", "방폭 인증 일정", "견적 재요청", "납기 지연"]


def d(rng: random.Random, start: date, end: date) -> date:
    return start + timedelta(days=rng.randint(0, (end - start).days))


def money(rng: random.Random, lo: int, hi: int) -> int:
    return rng.randint(lo, hi) // 5 * 5


# ---------------------------------------------------------------- hard negatives

def quarterly(rng, out: Path) -> None:
    for y in (2024, 2025, 2026):
        for q in (1, 2, 3, 4):
            if (y, q) > (2026, 2) and not (y, q) == (2026, 3):
                continue
            for kind in ("확정", "잠정"):
                if (y, q) == (2026, 3) and kind == "확정":
                    continue                       # the target file is the 2026 Q3 final report
                rows = {p: [money(rng, 80, 700) for _ in range(3)] for p in PRODUCTS}
                tot = sum(sum(v) for v in rows.values())
                months = [(q - 1) * 3 + k for k in (1, 2, 3)]
                body = "\n".join(f"| {p} | " + " | ".join(str(x) for x in v) + f" | {sum(v):,} |" for p, v in rows.items())
                (out / f"실적_{y}_{q}분기_{kind}.txt").write_text(
                    f"{y}년 {q}분기 영업실적 보고 ({kind})\n작성: 경영지원팀 재무파트\n\n"
                    f"1. 요약\n- {q}분기 매출: {tot:,}백만원\n- 분기 목표 대비 달성률 {rng.uniform(85, 108):.1f}%\n\n"
                    f"2. 제품별·월별 매출 (단위: 백만원)\n| 제품 | {months[0]}월 | {months[1]}월 | {months[2]}월 | 합계 |\n"
                    f"|---|---|---|---|---|\n{body}\n\n3. 코멘트\n- {rng.choice(CUSTOMERS)} 증설 물량 반영.\n",
                    encoding="utf-8")
    for m in range(1, 11):
        (out / f"월간실적_2026-{m:02d}.txt").write_text(
            f"2026년 {m}월 월간 실적 속보\n작성: 경영지원팀\n\n월 매출 {money(rng, 1200, 1700):,}백만원 (잠정, 월말 정산 전)\n"
            f"- 구독 매출 {money(rng, 550, 680)}백만원\n- 하드웨어 {money(rng, 500, 900)}백만원\n", encoding="utf-8")


def meetings(rng, out: Path) -> None:
    day = date(2025, 1, 6)
    while day <= date(2026, 11, 10):
        if day in (date(2026, 10, 6), date(2026, 10, 20)):
            day += timedelta(days=7)
            continue                               # the two target meetings
        agenda = rng.sample(TOPICS, 3)
        lines = [f"주간 경영회의 회의록\n일시: {day.isoformat()}(월) 09:00~10:00\n참석: " + ", ".join(rng.sample(PEOPLE, 5)) + "\n"]
        for k, a in enumerate(agenda, 1):
            lines.append(f"[안건 {k}] {a}\n- {rng.choice(PEOPLE)}: {rng.choice(CUSTOMERS)} 관련 {a} 현황 공유.\n- 결정: 다음 회의까지 정리.\n")
        if date(2026, 9, 1) <= day < date(2026, 10, 20):
            lines.append("[안건 4] NS-500 기획\n- 김라온: 기획 착수. 출시 목표 2027년 3월, 목표 단가 62만원 검토 중.\n")
        if date(2026, 10, 27) <= day:
            lines.append("[안건 4] NS-500 단가\n- 한지수: 재무 시뮬레이션 진행 중 (11/14 완료 예정).\n")
        if date(2026, 7, 1) <= day < date(2026, 10, 1) and rng.random() < 0.5:
            lines.append(f"[안건 5] 3분기 실적 중간 점검\n- 오세훈: 누적 매출 {money(rng, 2500, 3000):,}백만원, 목표 대비 소폭 미달.\n")
        (out / f"주간회의록_{day.isoformat()}.txt").write_text("\n".join(lines), encoding="utf-8")
        day += timedelta(days=7)


def rules(rng, out: Path) -> None:
    travel = {"v1.0": ("2021-03-01", 20000, 80000, 70000, 200, 40),
              "v1.5": ("2023-01-01", 22000, 90000, 75000, 220, 45)}
    for v, (eff, per, metro, local, km, usd) in travel.items():
        (out / f"출장비규정_{v}.txt").write_text(
            f"국내외 출장비 지급 규정 {v}\n시행 {eff}\n\n제1조 (목적) 출장 경비 지급 기준을 정한다.\n"
            f"제2조 (국내 출장 일비) 1일 {per:,}원.\n제3조 (국내 숙박비) 수도권·광역시 {metro:,}원, 그 외 {local:,}원.\n"
            f"제4조 (교통비) 자가용 km당 {km}원.\n제5조 (해외 출장 일비) 1일 USD {usd}.\n", encoding="utf-8")
    (out / "출장비_FAQ_2024.txt").write_text(
        "출장비 자주 묻는 질문 (2024년판, 규정 v1.5 기준)\n\nQ. 당일 출장도 일비가 나오나요?\nA. 50% 지급합니다.\n"
        "Q. KTX 특실을 탈 수 있나요?\nA. 원칙적으로 일반실입니다.\n", encoding="utf-8")
    others = ["법인카드 사용", "경조사 지원", "재택근무", "정보보안", "구매 승인", "교육비 지원", "차량 운행", "복리후생"]
    for name in others:
        for v in ("v1.0", "v1.1", "v2.0"):
            arts = "\n".join(f"제{k}조 ({rng.choice(['적용', '절차', '한도', '승인', '정산', '예외'])}) "
                             f"{name} 관련 세부 기준을 {rng.choice(['정한다', '따른다', '적용한다'])}. 한도 {money(rng, 10, 500)}만원."
                             for k in range(1, rng.randint(5, 9)))
            (out / f"{name}규정_{v}.txt").write_text(f"{name} 규정 {v}\n시행 {d(rng, date(2021, 1, 1), date(2026, 6, 1))}\n\n{arts}\n",
                                                    encoding="utf-8")


def quotes(rng, out: Path) -> None:
    items = ["NS-300 하우징 금형", "NS-200 브래킷 금형", "배터리팩", "메인 PCB", "LTE-M 모듈", "게이트웨이 케이스", "포장 박스", "방폭 하우징"]
    for k in range(120):
        v, item = rng.choice(VENDORS), rng.choice(items)
        if v == "대성정밀" and "NS-500" in item:
            continue
        mold = money(rng, 800, 3500) * 10000
        dt = d(rng, date(2024, 1, 1), date(2026, 11, 1))
        (out / f"견적서_{v}_{item.replace(' ', '')}_{dt.isoformat()}_{k}.txt").write_text(
            f"견 적 서\n견적번호: {v[:2].upper()}-{dt.year}-{dt.month:02d}{dt.day:02d}-{k:02d}\n견적일자: {dt}\n"
            f"수신: (주)누리센스 {rng.choice(PEOPLE)}\n발신: {v}(주) 영업부 (가상 업체)\n\n건명: {item} 제작\n"
            f"| 품목 | 수량 | 금액(원) |\n|---|---|---|\n| {item} | 1식 | {mold:,} |\n| 초도품 검사 | 1식 | {money(rng, 50, 120) * 10000:,} |\n"
            f"조건: 납기 발주 후 {rng.randint(4, 12)}주, 유효기간 {rng.choice([30, 60, 90])}일\n", encoding="utf-8")


def plans(rng, out: Path) -> None:
    specs = [("NS-300 저온 배터리팩", ["v0.1", "v0.2"]), ("NuriWatch 알림 개선", ["v0.3", "v1.0"]),
             ("NS-200 리뉴얼", ["v0.1", "v0.2", "v0.3"]), ("LoRa 게이트웨이 2세대", ["v0.1"])]
    for name, vers in specs:
        for v in vers:
            (out / f"기획서_{name.replace(' ', '')}_{v}.txt").write_text(
                f"{name} 기획서 {v}\n작성: 제품개발본부 / {d(rng, date(2025, 1, 1), date(2026, 10, 1))}\n\n"
                f"1. 배경\n- 고객 요구: {rng.choice(TOPICS)}.\n2. 목표\n- 출시 목표 {rng.choice(['2026년 12월', '2027년 2분기', '2027년 하반기'])}\n"
                f"- 목표 단가 {money(rng, 10, 60)}만원\n3. 개발비\n- {rng.uniform(0.5, 4):.1f}억원\n", encoding="utf-8")
    (out / "NS-500_기획착수_보고_2026-09-01.txt").write_text(
        "NS-500 기획 착수 보고\n작성: 김라온 / 2026-09-01\n\n엣지 AI 진동분석 센서 기획을 시작한다.\n"
        "- 출시 목표 2027년 3월\n- 목표 단가 62만원 (NS-200의 1.6배)\n- 상세는 기획서 v0.3에서 확정\n", encoding="utf-8")


def vocs(rng, out: Path) -> None:
    for y in (2024, 2025, 2026):
        for q in (1, 2, 3, 4):
            if (y, q) >= (2026, 3):
                continue
            rows = "\n".join(f"| {k} | {d(rng, date(y, q * 3 - 2, 1), date(y, q * 3, 28))} | {rng.choice(CUSTOMERS)} | "
                             f"{rng.choice(PRODUCTS)} | {rng.choice(['배터리', '통신', '앱', '설치'])} | {rng.choice(TOPICS)} | "
                             f"{rng.choice(['해결', '조치중'])} |" for k in range(1, rng.randint(8, 16)))
            (out / f"고객VOC_{y}Q{q}.txt").write_text(
                f"고객 VOC 정리 — {y}년 {q}분기\n작성: 고객성공팀\n\n| No | 접수일 | 고객사 | 제품 | 유형 | 내용 | 상태 |\n"
                f"|---|---|---|---|---|---|---|\n{rows}\n", encoding="utf-8")


def schedules(rng, out: Path) -> None:
    for c in CUSTOMERS:
        if c == "동해화학":
            continue
        start = d(rng, date(2025, 3, 1), date(2026, 8, 1))
        rows = "\n".join(f"| {k} | {task} | {start + timedelta(days=20 * k)} | {start + timedelta(days=20 * k + 18)} | "
                         f"{rng.choice([0, 30, 70, 100])}% | {rng.choice(['완료', '진행중', '대기', '연기'])} |"
                         for k, task in enumerate(["착수·현장 조사", "센서 설치", "데이터 수집", "효과 측정·보고"], 1))
        (out / f"{c}_설치프로젝트_일정표.txt").write_text(
            f"{c} 센서 도입 프로젝트 — 일정표\nPM: {rng.choice(PEOPLE)}\n계약금액 {rng.uniform(0.3, 2.5):.1f}억원\n\n"
            f"| WBS | 작업 | 시작 | 종료 | 진척률 | 상태 |\n|---|---|---|---|---|---|\n{rows}\n", encoding="utf-8")


def hr(rng, out: Path) -> None:
    for y in (2025, 2026):
        rows = "\n".join(f"| {t} | {rng.randint(1, 4)} | {rng.choice(['1분기', '2분기', '하반기'])} |" for t in rng.sample(TEAMS, 5))
        (out / f"{y}_채용계획_확정.txt").write_text(
            f"{y}년 인력 충원 계획 (확정)\n작성: 경영지원팀\n\n| 조직 | 인원 | 입사 목표 |\n|---|---|---|\n{rows}\n"
            f"추가 인건비 약 {rng.uniform(5, 12):.1f}억원\n", encoding="utf-8")
    for k in range(40):
        (out / f"인사발령_{k:03d}.txt").write_text(
            f"인사 발령 공지\n발령일: {d(rng, date(2024, 1, 1), date(2026, 11, 1))}\n\n- {rng.choice(PEOPLE)}: "
            f"{rng.choice(TEAMS)} → {rng.choice(TEAMS)}\n", encoding="utf-8")


def competitors(rng, out: Path) -> None:
    for dt in ("2025-03", "2025-09", "2026-01"):
        (out / f"경쟁사동향_{dt}.txt").write_text(
            f"경쟁사 동향 메모 ({dt})\n작성: 영업본부 전략파트\n\n- 센트릭스(가상): 진동센서 가격 {rng.randint(30, 40)}만원대.\n"
            f"- 파이오닉스(가상): 가스 감지 신제품 예고.\n", encoding="utf-8")


# ---------------------------------------------------------------- filler

def filler(rng, out: Path, n: int) -> None:
    kinds = ["고객미팅", "공지", "교육자료", "FAQ", "설치가이드", "품질월보", "발주서", "주간업무보고"]
    for k in range(n):
        kind = rng.choice(kinds)
        c, p, t = rng.choice(CUSTOMERS), rng.choice(PRODUCTS), rng.choice(TOPICS)
        dt = d(rng, date(2024, 1, 1), date(2026, 11, 10))
        body = {
            "고객미팅": f"{c} 방문 미팅 노트\n일시: {dt}\n참석: {', '.join(rng.sample(PEOPLE, 2))}\n\n- {p} {t} 논의.\n- 고객 요청: {rng.choice(TOPICS)}.\n- 후속: 견적 송부.",
            "공지": f"[공지] {rng.choice(['사내 행사', '시스템 점검', '보안 교육', '건강검진', '사무실 이전'])} 안내\n게시일: {dt}\n\n{rng.choice(TEAMS)}에서 안내드립니다. 일정 확인 바랍니다.",
            "교육자료": f"{p} 제품 교육 자료\n작성: {rng.choice(TEAMS)}\n\n1. 제품 개요\n- {p}는 설비 상태를 측정한다.\n2. {t}\n- 현장에서 확인할 사항을 정리한다.",
            "FAQ": f"{p} 자주 묻는 질문\n\nQ. {t}은 어떻게 하나요?\nA. 고객성공팀에 문의하세요. 평균 처리 {rng.randint(1, 5)}일.",
            "설치가이드": f"{p} 설치 가이드 rev.{rng.randint(1, 9)}\n\n1. 준비물\n- 브래킷, 앵커 볼트\n2. 설치 위치\n- {rng.choice(['모터', '펌프', '팬', '탱크'])} 하우징 상단",
            "품질월보": f"생산품질 월보 {dt.year}-{dt.month:02d}\n작성: 생산품질팀\n\n- {p} 출하 {rng.randint(500, 3000)}대, 불량률 {rng.uniform(0.1, 1.5):.2f}%\n- 주요 불량: {rng.choice(['납땜', '방수', '배터리 접촉', '외관'])}",
            "발주서": f"발 주 서\n발주번호: PO-{dt.year}-{k:05d}\n발주일: {dt}\n공급사: {rng.choice(VENDORS)}\n\n| 품목 | 수량 |\n|---|---|\n| {rng.choice(['PCB', '배터리', '케이스', '안테나'])} | {rng.randint(100, 5000)} |",
            "주간업무보고": f"{rng.choice(TEAMS)} 주간 업무 보고 ({dt})\n\n- {c} {t} 대응\n- {p} 관련 문의 {rng.randint(1, 20)}건 처리\n- 다음 주: {rng.choice(TOPICS)}",
        }[kind]
        (out / f"{kind}_{dt.isoformat()}_{k:05d}.txt").write_text(body + "\n", encoding="utf-8")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--targets", required=True, help="정답 문서 폴더 (doc_writer/samples/nurisense)")
    ap.add_argument("--out", required=True)
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--filler", type=int, default=1600)
    args = ap.parse_args()
    out = Path(args.out)
    if out.exists():
        shutil.rmtree(out)
    out.mkdir(parents=True)
    rng = random.Random(args.seed)
    for f in sorted(Path(args.targets).glob("*.txt")):
        shutil.copy(f, out / f.name)
    for fn in (quarterly, meetings, rules, quotes, plans, vocs, schedules, hr, competitors):
        fn(rng, out)
    filler(rng, out, args.filler)
    print(f"{len(list(out.iterdir()))} files -> {out}")


if __name__ == "__main__":
    main()
