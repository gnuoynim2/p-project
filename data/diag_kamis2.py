"""'날짜 하나 지정 → 그날 부류 전체 가격' API가 옛날 날짜를 주는지 확인 (호출 4번).
collect_kamis.py와 같은 폴더에서:  python diag_kamis2.py
"""
from collect_kamis import call, unpack

for day in ["2016-11-25", "2020-11-27", "2024-11-29", "2026-05-15"]:
    rows, rc = unpack(call({
        "action": "dailyPriceByCategoryList", "p_product_cls_code": "01",   # 소매
        "p_item_category_code": "200", "p_country_code": "", "p_regday": day,  # 채소류
        "p_convert_kg_yn": "N",
    }))
    print(f"\n요청 날짜 {day} → 결과 코드 {rc}, {len(rows)}행")
    배추 = [r for r in rows if r.get("item_name") == "배추"]
    for r in (배추 or rows)[:2]:
        print("  ", {k: r.get(k) for k in ["item_name", "kind_name", "rank", "unit", "day1", "dpr1", "day2", "dpr2", "day6", "dpr6"]})
