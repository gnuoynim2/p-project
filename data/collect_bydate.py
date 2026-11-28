"""
KAMIS 날짜별 수집기 (v0.2) - 과거 10년치용

'일별 품목별 가격'(periodProductList)은 요청 기간을 무시하고 최근 1년만 줘서 과거를 받을 수 없었다.
대신 '일별 부류별 가격'(dailyPriceByCategoryList)은 날짜 하나를 주면 그날 부류 전체 가격을 주고,
2016년 같은 옛날 날짜도 된다(진단으로 확인). 응답에 '당일'과 '1일전' 가격이 같이 오므로
화·목·금만 요청해도 월~금 5일이 모두 채워진다.

  python collect_bydate.py                         # 2016-01-01 ~ 어제, 채소류·특용작물, 소매·도매
  python collect_bydate.py --max-calls 1000        # 이번 실행 호출 수 제한 (다시 실행하면 이어서)
  python collect_bydate.py --categories 100,200,300,400   # 부류 바꾸기 (100 식량 200 채소 300 특용 400 과일 500 축산 600 수산)

호출 수(기본값): 약 570주 × 3일 × 2부류 × 소매·도매 = 약 6,800회
결과: data/kamis_bydate.csv (재실행 시 겹치는 날짜가 생길 수 있음 → 가공 단계에서 중복 제거)
안전장치: 받은 날짜가 요청 날짜와 10일 넘게 다르면 '날짜 불일치'로 세고, 5번 연속이면 멈춘다.
"""
import argparse
import csv
import re
import sys
from datetime import date, timedelta

import collect_kamis as K

OUT = K.DATA / "kamis_bydate.csv"
DONE = K.DATA / "kamis_bydate_done.txt"
FIELDS = ["date", "cls", "category", "item_code", "item_name", "kind_code", "kind_name",
          "rank_code", "rank", "unit", "price", "req_date"]
LABEL = re.compile(r"\((\d{1,2})/(\d{1,2})\)")


def label_date(label, req):
    """'당일 (11/25)' → 날짜. 연도는 요청 날짜 기준, 1월에 12월 날짜가 오면 전년도."""
    m = LABEL.search(str(label or ""))
    if not m:
        return None
    mo, d = int(m.group(1)), int(m.group(2))
    try:
        x = date(req.year, mo, d)
    except ValueError:
        return None
    if x > req + timedelta(days=1):
        x = date(req.year - 1, mo, d)
    return x


def fetch(cls, cat, req):
    rows, rc = K.unpack(K.call({
        "action": "dailyPriceByCategoryList", "p_product_cls_code": cls,
        "p_item_category_code": cat, "p_country_code": "", "p_regday": req.isoformat(),
        "p_convert_kg_yn": "N",
    }))
    out, first_day = [], None
    for r in rows:
        if not str(r.get("item_code", "")).strip():
            continue
        for lab, val in (("day1", "dpr1"), ("day2", "dpr2")):      # 당일, 1일전
            d = label_date(r.get(lab), req)
            p = K.num(r.get(val))
            if lab == "day1" and d and first_day is None:
                first_day = d
            if d is None or p is None:
                continue
            out.append({
                "date": d.isoformat(), "cls": K.CLS[cls], "category": K.CATEGORIES[cat],
                "item_code": r.get("item_code"), "item_name": r.get("item_name"),
                "kind_code": r.get("kind_code"), "kind_name": r.get("kind_name"),
                "rank_code": r.get("rank_code"), "rank": r.get("rank"), "unit": r.get("unit"),
                "price": p, "req_date": req.isoformat(),
            })
    return out, rc, first_day


def main():
    ap = argparse.ArgumentParser(description="KAMIS 날짜별 과거 가격 수집")
    ap.add_argument("--start", default="2016-01-01")
    ap.add_argument("--end", default=(date.today() - timedelta(days=1)).isoformat())
    ap.add_argument("--categories", default="200,300", help="부류 코드, 쉼표로 구분")
    ap.add_argument("--weekdays", default="1,3,4", help="요청 요일 (0=월 … 4=금), 기본 화·목·금")
    ap.add_argument("--max-calls", type=int, default=100000)
    a = ap.parse_args()
    if not (K.KEY and K.CID):
        sys.exit(".env 에 KAMIS_CERT_KEY 와 KAMIS_CERT_ID 를 넣어 주세요.")

    cats = [c.strip() for c in a.categories.split(",") if c.strip()]
    wds = {int(x) for x in a.weekdays.split(",")}
    start, end = date.fromisoformat(a.start), date.fromisoformat(a.end)
    days = [start + timedelta(days=i) for i in range((end - start).days + 1)]
    days = [d for d in days if d.weekday() in wds]
    jobs = [(d, cls, cat) for d in days for cls in K.CLS for cat in cats]
    done = set(DONE.read_text(encoding="utf-8").split("\n")) if DONE.exists() else set()
    jobs = [j for j in jobs if f"{j[1]}|{j[2]}|{j[0]}" not in done]
    print(f"요청 날짜 {len(days)}일 × 소매·도매 × 부류 {len(cats)}개 · 남은 호출 {len(jobs)}회 (이번 최대 {a.max_calls}회)")

    K.DATA.mkdir(exist_ok=True)
    new = not OUT.exists()
    fails = mismatch = total = 0
    with open(OUT, "a", newline="", encoding="utf-8-sig") as f, open(DONE, "a", encoding="utf-8") as dlog:
        w = csv.DictWriter(f, fieldnames=FIELDS)
        if new:
            w.writeheader()
        for i, (d, cls, cat) in enumerate(jobs, 1):
            if K.CALLS >= a.max_calls:
                print(f"\n이번 최대 호출 수({a.max_calls}) 도달. 다시 실행하면 이어서 받습니다.")
                break
            try:
                rows, rc, first = fetch(cls, cat, d)
            except Exception as ex:
                fails += 1
                print(f"  실패 {d} {K.CLS[cls]} {K.CATEGORIES[cat]}: {ex}")
                if fails >= 5:
                    print("\n연속 5번 실패해서 멈췄습니다. 하루 한도일 수 있어요. 내일 다시 실행하면 이어집니다.")
                    break
                continue
            if rc not in ("000", "001"):
                fails += 1
                print(f"  {d} {K.CLS[cls]} {K.CATEGORIES[cat]}: 결과 코드 {rc}")
                if fails >= 5:
                    print("\n연속 5번 실패해서 멈췄습니다. 하루 한도일 수 있어요. 내일 다시 실행하면 이어집니다.")
                    break
                continue
            fails = 0
            if rc == "000" and first and (first > d or (d - first).days > 10):
                mismatch += 1
                print(f"  날짜 불일치: {d} 요청 → {first} 데이터 ({mismatch}번 연속)")
                if mismatch >= 5:
                    print("\n요청한 날짜와 다른 데이터가 계속 와서 멈췄습니다. 결과를 알려주세요.")
                    break
                continue
            mismatch = 0
            w.writerows(rows)
            f.flush()
            dlog.write(f"{cls}|{cat}|{d}\n")
            dlog.flush()
            total += len(rows)
            if i % 50 == 0 or i == len(jobs):
                print(f"  [{i}/{len(jobs)}] {d} 까지 · 누적 {total:,}행 · 호출 {K.CALLS}회")
    print(f"\n끝. 이번에 {total:,}행 · 호출 {K.CALLS}회 · 결과 {OUT}")


if __name__ == "__main__":
    main()
