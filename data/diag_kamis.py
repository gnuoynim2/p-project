"""KAMIS가 옛날 기간을 주는지 확인하는 진단용 스크립트 (호출 5번).
collect_kamis.py와 같은 폴더에서:  python diag_kamis.py
"""
from collections import Counter
from collect_kamis import call, unpack, read_codes, CATEGORIES

codes = read_codes()
c = next((x for x in codes if x["cls"] == "소매" and x["item_name"] == "배추"), codes[0])
print(f"시험 품목: {c['cls']} {c['item_name']}/{c['kind_name']} ({c['rank']}) "
      f"item={c['item_code']} kind={c['kind_code']} rank={c['rank_code']}")

cls_code = "01" if c["cls"] == "소매" else "02"
cat_code = next(k for k, v in CATEGORIES.items() if v == c["category"])

for start, end in [("2016-01-01", "2016-12-31"), ("2020-01-01", "2020-12-31"),
                   ("2024-01-01", "2024-12-31"), ("2025-06-01", "2025-06-30")]:
    for kind in [c["kind_code"], ""]:          # 품종 코드를 넣었을 때 / 비웠을 때
        if kind == "" and start != "2020-01-01":
            continue
        rows, rc = unpack(call({
            "action": "periodProductList", "p_productclscode": cls_code,
            "p_startday": start, "p_endday": end,
            "p_itemcategorycode": cat_code, "p_itemcode": c["item_code"], "p_kindcode": kind,
            "p_productrankcode": c["rank_code"], "p_countrycode": "", "p_convert_kg_yn": "N",
        }))
        avg = [r for r in rows if str(r.get("countyname", "")).strip() == "평균"]
        years = Counter(str(r.get("yyyy")) for r in avg)
        days = sorted(f"{r.get('yyyy')}-{str(r.get('regday')).replace('/', '-')}" for r in avg)
        print(f"\n요청 {start}~{end} (kind={kind or '비움'}) → 결과 코드 {rc}, 전체 {len(rows)}행, 평균 {len(avg)}행")
        print(f"  평균 행의 연도 분포: {dict(years)}")
        if days:
            print(f"  평균 행 날짜 범위: {days[0]} ~ {days[-1]}")
        if rows:
            print(f"  첫 행 예시: {rows[0]}")
