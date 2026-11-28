"""aT 전국 공영도매시장 정산정보 진단 (호출 10번 안팎)
collect_kamis.py와 같은 폴더에서:  python diag_katsale.py
       요청주소를 직접 줄 때:     python diag_katsale.py --url https://apis.data.go.kr/B552845/katSale/...

.env 에 공공데이터포털 '일반 인증키(Decoding)'를 DATA_GO_KR_KEY=... 로 넣어 두세요.
확인하는 것: 요청주소가 맞는지, 2016년 날짜가 오는지, 하루 몇 행인지, 응답 필드 이름(물량 단위 등)
"""
import argparse
import json
import os
from pathlib import Path

import requests
from dotenv import load_dotenv

HERE = Path(__file__).resolve().parent
load_dotenv(HERE / ".env")
KEY = os.getenv("DATA_GO_KR_KEY", "")
BASE = "https://apis.data.go.kr/B552845/katSale"
CANDIDATES = [BASE, BASE + "/trades", BASE + "/getTrades", BASE + "/list"]
MARKET = "110001"     # 포털 예시 값. 가락시장인지 응답의 시장 이름으로 확인


def get(url, day, rows=1000, page=1):
    params = {"serviceKey": KEY, "returnType": "json", "pageNo": page, "numOfRows": rows,
              "cond[trd_clcln_ymd::EQ]": day, "cond[whsl_mrkt_cd::EQ]": MARKET}
    r = requests.get(url, params=params, timeout=60)
    try:
        return r.status_code, r.json(), None
    except ValueError:
        return r.status_code, None, r.text[:300]


def find_items(obj):
    """응답 구조를 몰라서, 가장 긴 리스트(=데이터 행)와 totalCount를 찾아낸다."""
    best, total = [], None
    stack = [obj]
    while stack:
        x = stack.pop()
        if isinstance(x, dict):
            for k, v in x.items():
                if k.lower() in ("totalcount", "total_count") and total is None:
                    total = v
                stack.append(v)
        elif isinstance(x, list):
            if len(x) > len(best) and all(isinstance(i, dict) for i in x):
                best = x
            stack.extend(x)
    return best, total


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", help="포털 '요청주소'(End Point + 오퍼레이션)")
    a = ap.parse_args()
    if not KEY:
        raise SystemExit(".env 에 DATA_GO_KR_KEY=공공데이터포털 인증키(Decoding) 를 넣어 주세요.")

    # 1) 요청주소 찾기 (최근 날짜로)
    url = None
    for u in ([a.url] if a.url else CANDIDATES):
        code, js, raw = get(u, "2026-11-27", rows=5)
        items, total = find_items(js) if js else ([], None)
        print(f"[주소 시험] {u} → HTTP {code}, 행 {len(items)}, totalCount {total}")
        if raw:
            print(f"   응답 앞부분: {raw}")
        if items:
            url = u
            break
        if js:
            print(f"   응답 앞부분: {json.dumps(js, ensure_ascii=False)[:300]}")
    if not url:
        print("\n데이터가 오는 주소를 못 찾았어요. 포털 '마이페이지 > 활용신청 상세'의 요청주소를 --url 로 넣어 다시 실행해 주세요.")
        return

    # 2) 응답 필드
    code, js, _ = get(url, "2026-11-27", rows=3)
    items, total = find_items(js)
    print(f"\n[필드] 최근일 2026-11-27 · 이 시장 하루 전체 {total}행")
    for k, v in items[0].items():
        print(f"   {k} = {v}")

    # 3) 과거 날짜
    for day in ["2016-11-25", "2019-11-29", "2022-11-25", "2024-11-29"]:
        code, js, raw = get(url, day, rows=3)
        items, total = find_items(js) if js else ([], None)
        print(f"\n[과거] {day} → HTTP {code}, totalCount {total}")
        if items:
            print("   첫 행:", json.dumps(items[0], ensure_ascii=False)[:300])
        elif raw or js:
            print("   응답 앞부분:", raw or json.dumps(js, ensure_ascii=False)[:300])

    # 4) 한 페이지 최대 행 수
    code, js, _ = get(url, "2026-11-27", rows=1000)
    items, total = find_items(js) if js else ([], None)
    print(f"\n[페이지] numOfRows=1000 요청 → 실제 {len(items)}행 (하루 {total}행이면 약 {(-(-int(total or 0)//max(len(items),1)))}페이지)")


if __name__ == "__main__":
    main()
