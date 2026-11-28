"""
KAMIS 전 품목 일별 도·소매 가격 수집기 (v0.1)

3단계로 실행한다.

  1) python collect_kamis.py codes
     품목·품종·등급 코드표를 자동으로 만든다 → data/kamis_codes.csv
     "일별 부류별 가격" API로 최근 1년의 매달 15일을 부류(식량작물·채소·특용작물·과일·축산·수산)별,
     소매·도매별로 조회해서, 그동안 나온 모든 품목·품종 조합을 모은다.
     (배추 봄·고랭지·가을·월동처럼 계절마다 바뀌는 품종까지 잡기 위해 여러 날짜를 본다)
     호출 수: 12개월 × 6부류 × 2 = 최대 144회
     → 만들어진 코드표에서 필요 없는 줄은 지워도 된다. 수집은 남은 줄만 한다.

  2) python collect_kamis.py test
     코드표 첫 줄 품목으로 1년치를 한 번에 받아 보고, 며칠치가 왔는지 알려준다.
     1년치가 다 오면 기본값(--chunk 365)으로, 일부만 오면 --chunk 값을 줄여서 수집한다.

  3) python collect_kamis.py collect --start 2016-01-01
     코드표의 각 줄마다 기간을 나눠 "일별 품목별 가격" API로 매일 가격을 받는다 → data/kamis_daily.csv
     이미 받은 조각은 data/kamis_done.txt 에 기록해 다시 받지 않는다 (끊겨도 다시 실행하면 이어서 받음).
     호출 수: 코드표 줄 수 × 기간 조각 수 (예: 200줄 × 11년 = 약 2,200회)

옵션
  --max-calls 1000   이번 실행에서 최대 호출 수 (하루 한도에 맞춰 나눠 받을 때)
  --chunk 365        한 번에 받는 기간(일)
  codes --all-ranks  상품·중품 등 모든 등급 포함 (기본은 상품만 → 호출 수 절반)

결과 (data/ 폴더, GitHub에 올리지 않음)
  data/kamis_codes.csv   cls, category, item_code, item_name, kind_code, kind_name, rank_code, rank, unit, seen_dates
  data/kamis_daily.csv   date, cls, category, item_code, item_name, kind_code, kind_name, rank_code, rank, unit, county, price
"""

import argparse
import csv
import os
import sys
import time
from datetime import date, timedelta
from pathlib import Path

import requests
from dotenv import load_dotenv

HERE = Path(__file__).resolve().parent
load_dotenv(HERE / ".env")
KEY, CID = os.getenv("KAMIS_CERT_KEY", ""), os.getenv("KAMIS_CERT_ID", "")
URL = "http://www.kamis.or.kr/service/price/xml.do"
DATA = HERE / "data"
CODES = DATA / "kamis_codes.csv"
DAILY = DATA / "kamis_daily.csv"
DONE = DATA / "kamis_done.txt"

CATEGORIES = {"100": "식량작물", "200": "채소류", "300": "특용작물", "400": "과일류", "500": "축산물", "600": "수산물"}
CLS = {"01": "소매", "02": "도매"}
CODE_FIELDS = ["cls", "category", "item_code", "item_name", "kind_code", "kind_name", "rank_code", "rank", "unit", "seen_dates"]
DAILY_FIELDS = ["date", "cls", "category", "item_code", "item_name", "kind_code", "kind_name",
                "rank_code", "rank", "unit", "county", "price"]
SLEEP = 0.4          # 호출 한도가 공개돼 있지 않아 천천히 부른다
CALLS = 0


class TooManyFailures(Exception):
    pass


# ---------------------------------------------------------------- 호출
def call(params):
    """KAMIS 호출 한 번. 네트워크 오류는 3번까지 다시 시도한다."""
    global CALLS
    base = {"p_cert_key": KEY, "p_cert_id": CID, "p_returntype": "json"}
    for attempt in range(4):
        try:
            r = requests.get(URL, params={**base, **params}, timeout=30)
            CALLS += 1
            time.sleep(SLEEP)
            return r.json()
        except (requests.RequestException, ValueError) as e:
            if attempt == 3:
                raise RuntimeError(f"응답을 읽지 못함: {e}")
            time.sleep(2 * (attempt + 1))


def unpack(resp):
    """KAMIS 응답에서 (행 목록, 결과 코드)를 꺼낸다.
    정상: {"data": {"error_code": "000", "item": [...]}}  /  데이터 없음 등: {"data": ["001"]}"""
    data = resp.get("data") if isinstance(resp, dict) else None
    if isinstance(data, dict):
        items = data.get("item") or []
        return ([items] if isinstance(items, dict) else items), str(data.get("error_code", "000"))
    if isinstance(data, list) and data:
        return [], str(data[0])
    return [], "unknown"


def num(s):
    s = str(s or "").replace(",", "").strip()
    try:
        return float(s)
    except ValueError:
        return None      # "-" 등 값 없음


# ---------------------------------------------------------------- 1) 코드표
def cmd_codes(a):
    """최근 1년 매달 15일 × 부류 × 소매/도매로 '일별 부류별 가격'을 불러 품목·품종·등급 조합을 모은다."""
    today = date.today()
    dates = []
    for m in range(12):
        y, mo = today.year, today.month - m
        while mo <= 0:
            mo += 12
            y -= 1
        d = date(y, mo, 15)
        if d >= today:
            d = today - timedelta(days=1)
        dates.append(d)
    combos = {}
    for d in dates:
        for cls in CLS:
            for cat in CATEGORIES:
                rows, code = unpack(call({
                    "action": "dailyPriceByCategoryList", "p_product_cls_code": cls,
                    "p_item_category_code": cat, "p_country_code": "", "p_regday": d.isoformat(),
                    "p_convert_kg_yn": "N",
                }))
                for r in rows:
                    k = (cls, cat, str(r.get("item_code", "")), str(r.get("kind_code", "")), str(r.get("rank_code", "")))
                    if not k[2]:
                        continue
                    c = combos.setdefault(k, {
                        "cls": CLS[cls], "category": CATEGORIES[cat], "item_code": k[2], "item_name": r.get("item_name", ""),
                        "kind_code": k[3], "kind_name": r.get("kind_name", ""), "rank_code": k[4],
                        "rank": r.get("rank", ""), "unit": r.get("unit", ""), "seen_dates": set(),
                    })
                    c["seen_dates"].add(d.strftime("%Y-%m"))
                if code not in ("000", "001"):
                    print(f"  {d} {CLS[cls]} {CATEGORIES[cat]}: 결과 코드 {code}")
        print(f"  {d} 조회 완료 · 지금까지 조합 {len(combos)}개 · 호출 {CALLS}회")

    rows = list(combos.values())
    if not a.all_ranks:   # 품목·품종마다 '상품' 등급만 남긴다 (상품이 없으면 그 품목의 등급을 모두 남김)
        has_top = {(r["cls"], r["item_code"], r["kind_code"]) for r in rows if "상품" in str(r["rank"])}
        rows = [r for r in rows if "상품" in str(r["rank"]) or (r["cls"], r["item_code"], r["kind_code"]) not in has_top]
    rows.sort(key=lambda r: (r["cls"], r["category"], r["item_code"], r["kind_code"], r["rank_code"]))
    DATA.mkdir(exist_ok=True)
    with open(CODES, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=CODE_FIELDS)
        w.writeheader()
        for r in rows:
            w.writerow({**r, "seen_dates": " ".join(sorted(r["seen_dates"]))})
    print(f"\n코드표 {len(rows)}줄 저장: {CODES}")
    print("필요 없는 줄(예: 수산물)은 지워도 됩니다. 다음: python collect_kamis.py test")


def read_codes():
    if not CODES.exists():
        sys.exit("코드표가 없습니다. 먼저 python collect_kamis.py codes 를 실행하세요.")
    with open(CODES, encoding="utf-8-sig") as f:
        return list(csv.DictReader(f))


# ---------------------------------------------------------------- 2), 3) 기간별 매일 가격
def period(code, start, end, all_regions=False):
    """'일별 품목별 가격'으로 한 품목·품종·등급의 기간 내 매일 가격.
    KAMIS는 지역별(서울·부산 등) 행과 '평균', 비교용 '평년' 행을 함께 준다.
    기본은 전국 '평균' 행만 남기고, '평년'(여러 해 평균 참고값)과 요청 기간 밖 날짜는 버린다."""
    cls_code = "01" if code["cls"] == "소매" else "02"
    cat_code = next(k for k, v in CATEGORIES.items() if v == code["category"])
    rows, rc = unpack(call({
        "action": "periodProductList", "p_productclscode": cls_code,
        "p_startday": start.isoformat(), "p_endday": end.isoformat(),
        "p_itemcategorycode": cat_code, "p_itemcode": code["item_code"], "p_kindcode": code["kind_code"],
        "p_productrankcode": code["rank_code"], "p_countrycode": "", "p_convert_kg_yn": "N",
    }))
    out = []
    for r in rows:
        try:   # 날짜: yyyy(연도) + regday("MM/DD")
            m, d = str(r.get("regday", "")).split("/")
            day = date(int(r.get("yyyy")), int(m), int(d))
        except (ValueError, TypeError):
            continue    # 날짜가 없는 요약 행
        county = str(r.get("countyname", "")).strip()
        if county == "평년" or not (start <= day <= end):
            continue
        if not all_regions and county != "평균":
            continue
        out.append({
            "date": day.isoformat(), "cls": code["cls"], "category": code["category"],
            "item_code": code["item_code"], "item_name": code["item_name"],
            "kind_code": code["kind_code"], "kind_name": code["kind_name"],
            "rank_code": code["rank_code"], "rank": code["rank"], "unit": code["unit"],
            "county": county, "price": num(r.get("price")),
        })
    return out, rc


def cmd_test(a):
    codes = read_codes()
    c = codes[0]
    end = date.today() - timedelta(days=1)
    start = end - timedelta(days=364)
    rows, rc = period(c, start, end, all_regions=True)
    days = sorted({r["date"] for r in rows if r["county"] == "평균"})
    counties = sorted({r["county"] for r in rows})
    print(f"시험 품목: {c['cls']} {c['item_name']}/{c['kind_name']} ({c['rank']}) · 요청 기간 {start} ~ {end}")
    print(f"결과 코드 {rc} · 요청 기간 안의 전국 평균 날짜 {len(days)}일 ({days[0] if days else '-'} ~ {days[-1] if days else '-'})")
    print(f"지역 구분(평년 제외): {counties[:8]}  → 수집할 때는 기본으로 '평균'만 저장")
    if days and days[0] > (start + timedelta(days=45)).isoformat():
        print("→ 요청한 1년 중 앞부분이 빠졌습니다. 기간 제한이 있을 수 있어요. --chunk 90 또는 30으로 수집하세요.")
    elif days:
        print("→ 1년치가 한 번에 옵니다. 기본값으로 수집하면 됩니다: python collect_kamis.py collect --start 2016-01-01")
    else:
        print("→ 데이터가 없습니다. 다른 품목으로 시험하려면 코드표 첫 줄을 바꿔 보세요.")


def cmd_collect(a):
    codes = read_codes()
    start, end = date.fromisoformat(a.start), date.fromisoformat(a.end)
    done = set(DONE.read_text(encoding="utf-8").split("\n")) if DONE.exists() else set()
    new = not DAILY.exists()
    fails, total_rows = 0, 0
    chunks = []
    s = start
    while s <= end:
        e = min(s + timedelta(days=a.chunk - 1), end)
        chunks.append((s, e))
        s = e + timedelta(days=1)
    todo = sum(1 for c in codes for s, e in chunks
               if f"{c['cls']}|{c['item_code']}|{c['kind_code']}|{c['rank_code']}|{s}|{e}" not in done)
    print(f"코드표 {len(codes)}줄 × 기간 조각 {len(chunks)}개 · 남은 호출 약 {todo}회 (이번 실행 최대 {a.max_calls}회)")

    DATA.mkdir(exist_ok=True)
    with open(DAILY, "a", newline="", encoding="utf-8-sig") as f, open(DONE, "a", encoding="utf-8") as dlog:
        w = csv.DictWriter(f, fieldnames=DAILY_FIELDS)
        if new:
            w.writeheader()
        try:
            for i, c in enumerate(codes, 1):
                got = 0
                for s, e in chunks:
                    tag = f"{c['cls']}|{c['item_code']}|{c['kind_code']}|{c['rank_code']}|{s}|{e}"
                    if tag in done:
                        continue
                    if CALLS >= a.max_calls:
                        print(f"\n이번 실행 최대 호출 수({a.max_calls})에 도달. 다시 실행하면 이어서 받습니다.")
                        return
                    try:
                        rows, rc = period(c, s, e, a.all_regions)
                    except Exception as ex:
                        fails += 1
                        print(f"  실패: {c['item_name']}/{c['kind_name']} {s}~{e} - {ex}")
                        if fails >= 5:
                            raise TooManyFailures
                        continue
                    if rc in ("000", "001"):          # 정상 또는 데이터 없음 → 완료로 기록
                        w.writerows(rows)
                        f.flush()
                        dlog.write(tag + "\n")
                        dlog.flush()
                        fails, got = 0, got + len(rows)
                    else:                              # 인증 오류·한도 초과 등
                        fails += 1
                        print(f"  {c['item_name']}/{c['kind_name']} {s}~{e}: 결과 코드 {rc}")
                        if fails >= 5:
                            raise TooManyFailures
                total_rows += got
                print(f"  [{i}/{len(codes)}] {c['cls']} {c['item_name']}/{c['kind_name']} ({c['rank']}): {got:,}행 · 누적 호출 {CALLS}회")
        except TooManyFailures:
            print("\n연속 5번 실패해서 멈췄습니다. 하루 한도 초과일 수 있어요. 내일 다시 실행하면 이어서 받습니다.")
    print(f"\n끝. 이번에 {total_rows:,}행 · 호출 {CALLS}회 · 결과 {DAILY}")


def main():
    ap = argparse.ArgumentParser(description="KAMIS 전 품목 일별 도·소매 가격 수집")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p1 = sub.add_parser("codes", help="품목·품종·등급 코드표 만들기")
    p1.add_argument("--all-ranks", action="store_true", help="상품 외 등급도 포함")
    sub.add_parser("test", help="1년치가 한 번에 오는지 시험")
    p3 = sub.add_parser("collect", help="매일 가격 수집")
    p3.add_argument("--start", default="2016-01-01")
    p3.add_argument("--end", default=(date.today() - timedelta(days=1)).isoformat())
    p3.add_argument("--chunk", type=int, default=365, help="한 번에 받는 기간(일)")
    p3.add_argument("--max-calls", type=int, default=100000, help="이번 실행 최대 호출 수")
    p3.add_argument("--all-regions", action="store_true", help="전국 평균 외 지역별 가격도 저장")
    a = ap.parse_args()

    if not (KEY and CID):
        sys.exit(".env 에 KAMIS_CERT_KEY 와 KAMIS_CERT_ID 를 넣어 주세요 (.env.example 참고).")
    {"codes": cmd_codes, "test": cmd_test, "collect": cmd_collect}[a.cmd](a)


if __name__ == "__main__":
    main()
