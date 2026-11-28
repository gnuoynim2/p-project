"""
aT 전국 공영도매시장 정산정보 수집기 (v0.1) - 도매시장 거래량(반입량)

진단 결과: 요청주소 .../katSale/trades, 가락시장 = 110001(서울가락), 하루 약 5천 행,
2019년은 있고 2016년은 없음, 한 페이지 1,000행. 물량은 unit_tot_qty(unit_nm 단위의 총량).

  1) python collect_katsale.py probe
       - 한 페이지에 몇 행까지 되는지(1000/5000/10000) 시험
       - 데이터가 시작되는 날짜를 찾음 (호출 50~80번)
  2) 팀원 5명이 나눠 받기 (기간을 5등분, 각자 자기 키로)
       python collect_katsale.py collect --member 1      ← 1번 담당
       python collect_katsale.py collect --member 2      ← 2번 담당 … 5번까지
       - 기간 기본값 2018-01-01 ~ 어제. 모두 같은 --start/--end 를 써야 겹치지 않음
       - 결과 파일 이름에 담당 번호가 붙음 (katsale_items_m1.csv 등) → 담당자에게 모으기
       - 날짜마다 모든 페이지를 받아서 바로 합친 뒤 저장 (원본 행은 저장 안 함)
       - 중간에 끊기거나 하루 한도(10,000회)에 걸리면 멈추고, 다시 실행하면 이어서 받음
       (--member 없이 실행하면 혼자 전체 기간을 받음)
  3) python collect_katsale.py merge
       - data/ 폴더에 모은 katsale_items_m*.csv, katsale_origin_m*.csv 를 합쳐서
         katsale_items.csv, katsale_origin.csv 로 저장 (겹치는 날짜는 하나만)

결과 (data/ 폴더)
  katsale_items.csv   날짜·시장·품목(대/중/소분류)·단위별  qty(물량 합계), amount(금액 합계), price_per_unit(금액/물량), n_rows
  katsale_origin.csv  날짜·시장·품목(중분류)·산지 시도·단위별  qty, amount   ← 주산지가 바뀌는 흐름 보기용
  katsale_done.txt    받은 날짜 기록
※ 등급(특·상·보통)과 법인은 합쳐서 하나로 만든다. 물량 단위가 kg이 아닌 품목(개·접·단 등)은 unit 열로 구분됨
"""
import argparse
import csv
import os
import sys
import time
from collections import defaultdict
from datetime import date, timedelta
from pathlib import Path

import requests
from dotenv import load_dotenv

HERE = Path(__file__).resolve().parent
load_dotenv(HERE / ".env")
KEY = os.getenv("DATA_GO_KR_KEY", "")
URL = "https://apis.data.go.kr/B552845/katSale/trades"
DATA = HERE / "data"
ITEMS, ORIGIN, DONE = DATA / "katsale_items.csv", DATA / "katsale_origin.csv", DATA / "katsale_done.txt"
ITEM_F = ["date", "market", "lcls_cd", "lcls", "mcls_cd", "mcls", "scls_cd", "scls", "unit", "qty", "amount", "price_per_unit", "n_rows"]
ORIG_F = ["date", "market", "lcls", "mcls_cd", "mcls", "origin_sido", "unit", "qty", "amount"]
CALLS = 0


def fetch(day, market, rows, page):
    global CALLS
    params = {"serviceKey": KEY, "returnType": "json", "pageNo": page, "numOfRows": rows,
              "cond[trd_clcln_ymd::EQ]": day, "cond[whsl_mrkt_cd::EQ]": market}
    for attempt in range(4):
        try:
            r = requests.get(URL, params=params, timeout=90)
            CALLS += 1
            js = r.json()
            body = js["response"]["body"]
            items = body.get("items") or {}
            items = items.get("item", []) if isinstance(items, dict) else items
            if isinstance(items, dict):
                items = [items]
            return items, int(body.get("totalCount") or 0)
        except (requests.RequestException, ValueError, KeyError, TypeError) as e:
            if attempt == 3:
                raise RuntimeError(f"{e} / 응답: {r.text[:200] if 'r' in locals() else ''}")
            time.sleep(3 * (attempt + 1))


def f(x):
    try:
        return float(str(x).replace(",", ""))
    except ValueError:
        return 0.0


def cmd_probe(a):
    day = "2026-11-27"
    print("[한 페이지 최대 행 수]")
    best = 1000
    for n in (1000, 5000, 10000):
        try:
            items, total = fetch(day, a.market, n, 1)
            print(f"  numOfRows={n} → {len(items)}행 (하루 {total}행)")
            if len(items) >= min(n, total):
                best = n
        except Exception as e:
            print(f"  numOfRows={n} → 실패: {e}")
    print(f"  → collect 할 때 --rows {best} 를 쓰세요")

    def has_data(d0):      # d0부터 7일 중 하루라도 데이터가 있으면 True (휴장일 대비)
        for i in range(7):
            _, total = fetch((d0 + timedelta(days=i)).isoformat(), a.market, 1, 1)
            if total:
                return True
        return False

    print("\n[데이터 시작 날짜 찾기]")
    lo, hi = date(2016, 1, 1), date(2019, 11, 29)      # lo엔 없고 hi엔 있음(진단 결과)
    if has_data(lo):
        print("  2016-01-01 부터 있어요 → --start 2016-01-01")
        return
    while (hi - lo).days > 7:
        mid = lo + (hi - lo) / 2
        ok = has_data(mid)
        print(f"  {mid} 주변: {'있음' if ok else '없음'}")
        lo, hi = (lo, mid) if ok else (mid, hi)
    print(f"  → 대략 {hi} 무렵부터 있어요. collect --start {hi.replace(day=1)} 로 받으면 됩니다 (호출 {CALLS}회 사용)")


def split(days, member, of):
    """날짜 목록을 of 등분해서 member 번째(1부터) 묶음을 돌려준다. 앞 묶음부터 하루씩 더 가짐."""
    size, extra = divmod(len(days), of)
    lo = (member - 1) * size + min(member - 1, extra)
    hi = lo + size + (1 if member <= extra else 0)
    return days[lo:hi]


def cmd_collect(a):
    global ITEMS, ORIGIN, DONE
    start, end = date.fromisoformat(a.start), date.fromisoformat(a.end)
    days = [start + timedelta(days=i) for i in range((end - start).days + 1)]
    if a.member:
        if not 1 <= a.member <= a.of:
            sys.exit(f"--member 는 1~{a.of} 사이여야 해요.")
        days = split(days, a.member, a.of)
        tag = f"_m{a.member}"
        ITEMS, ORIGIN, DONE = (DATA / f"katsale_items{tag}.csv", DATA / f"katsale_origin{tag}.csv",
                               DATA / f"katsale_done{tag}.txt")
        print(f"[{a.member}번 담당 / {a.of}명] {days[0]} ~ {days[-1]} ({len(days)}일)")
    done = set(DONE.read_text(encoding="utf-8").split("\n")) if DONE.exists() else set()
    days = [d for d in days if f"{a.market}|{d}" not in done]
    print(f"남은 날짜 {len(days)}일 · 시장 {a.market} · 한 페이지 {a.rows}행 (하루 한도 10,000회, 걸리면 멈추고 내일 이어서)")
    DATA.mkdir(exist_ok=True)
    new_i, new_o = not ITEMS.exists(), not ORIGIN.exists()
    fails = 0
    with open(ITEMS, "a", newline="", encoding="utf-8-sig") as fi, \
         open(ORIGIN, "a", newline="", encoding="utf-8-sig") as fo, \
         open(DONE, "a", encoding="utf-8") as dlog:
        wi, wo = csv.DictWriter(fi, fieldnames=ITEM_F), csv.DictWriter(fo, fieldnames=ORIG_F)
        if new_i:
            wi.writeheader()
        if new_o:
            wo.writeheader()
        for k, d in enumerate(days, 1):
            if CALLS >= a.max_calls:
                print(f"\n이번 최대 호출 수({a.max_calls}) 도달. 다시 실행하면 이어서 받습니다.")
                break
            try:
                rows, total = fetch(d.isoformat(), a.market, a.rows, 1)
                pages = -(-total // a.rows) if total else 1
                for p in range(2, pages + 1):
                    more, _ = fetch(d.isoformat(), a.market, a.rows, p)
                    rows += more
                if total and len(rows) < total:
                    raise RuntimeError(f"행 수 부족 {len(rows)}/{total}")
            except Exception as e:
                fails += 1
                print(f"  실패 {d}: {e}")
                if fails >= 5:
                    print("\n연속 5번 실패해서 멈췄습니다. 하루 한도(10,000회)일 수 있어요. 내일 다시 실행하면 이어집니다.")
                    break
                continue
            fails = 0
            it, og = defaultdict(lambda: [0.0, 0.0, 0]), defaultdict(lambda: [0.0, 0.0])
            for r in rows:
                q, amt = f(r.get("unit_tot_qty")), f(r.get("totprc"))
                unit = r.get("unit_nm", "")
                ki = (r.get("gds_lclsf_cd"), r.get("gds_lclsf_nm"), r.get("gds_mclsf_cd"), r.get("gds_mclsf_nm"),
                      r.get("gds_sclsf_cd"), r.get("gds_sclsf_nm"), unit)
                it[ki][0] += q
                it[ki][1] += amt
                it[ki][2] += 1
                sido = str(r.get("plor_nm") or "").split(" ")[0] or "미상"
                ko = (r.get("gds_lclsf_nm"), r.get("gds_mclsf_cd"), r.get("gds_mclsf_nm"), sido, unit)
                og[ko][0] += q
                og[ko][1] += amt
            for (lc, ln, mc, mn, sc, sn, u), (q, amt, n) in it.items():
                wi.writerow({"date": d, "market": a.market, "lcls_cd": lc, "lcls": ln, "mcls_cd": mc, "mcls": mn,
                             "scls_cd": sc, "scls": sn, "unit": u, "qty": round(q, 1), "amount": round(amt),
                             "price_per_unit": round(amt / q, 1) if q else "", "n_rows": n})
            for (ln, mc, mn, s, u), (q, amt) in og.items():
                wo.writerow({"date": d, "market": a.market, "lcls": ln, "mcls_cd": mc, "mcls": mn,
                             "origin_sido": s, "unit": u, "qty": round(q, 1), "amount": round(amt)})
            fi.flush(); fo.flush()
            dlog.write(f"{a.market}|{d}\n"); dlog.flush()
            if k % 20 == 0 or k == len(days):
                print(f"  [{k}/{len(days)}] {d} · 그날 {total}행 · 누적 호출 {CALLS}회")
    print(f"\n끝. 호출 {CALLS}회 · 결과 {ITEMS}, {ORIGIN}")


def cmd_merge(a):
    import glob
    for kind, out, keys in (("items", DATA / "katsale_items.csv", ["date", "market", "scls_cd", "mcls_cd", "lcls_cd", "unit"]),
                            ("origin", DATA / "katsale_origin.csv", ["date", "market", "mcls_cd", "lcls", "origin_sido", "unit"])):
        files = sorted(glob.glob(str(DATA / f"katsale_{kind}_m*.csv")))
        if not files:
            print(f"{kind}: 합칠 파일(katsale_{kind}_m*.csv)이 data/ 에 없어요.")
            continue
        seen, n_in, n_out = set(), 0, 0
        fields = ITEM_F if kind == "items" else ORIG_F
        with open(out, "w", newline="", encoding="utf-8-sig") as fw:
            w = csv.DictWriter(fw, fieldnames=fields)
            w.writeheader()
            for fn in files:
                with open(fn, encoding="utf-8-sig") as fr:
                    for r in csv.DictReader(fr):
                        n_in += 1
                        k = tuple(r.get(x) for x in keys)
                        if k in seen:
                            continue
                        seen.add(k)
                        w.writerow({x: r.get(x) for x in fields})
                        n_out += 1
        days = sorted({k[0] for k in seen})
        print(f"{kind}: 파일 {len(files)}개 · {n_in:,}행 → 중복 빼고 {n_out:,}행 · {days[0]} ~ {days[-1]} ({len(days)}일 데이터) → {out}")


def main():
    ap = argparse.ArgumentParser(description="aT 도매시장 정산정보(거래량) 수집")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("probe")
    p.add_argument("--market", default="110001")
    c = sub.add_parser("collect")
    c.add_argument("--start", default="2018-01-01")
    c.add_argument("--end", default=(date.today() - timedelta(days=1)).isoformat())
    c.add_argument("--market", default="110001", help="110001 = 서울가락")
    c.add_argument("--rows", type=int, default=1000, help="한 페이지 행 수 (probe 결과)")
    c.add_argument("--max-calls", type=int, default=9800)
    c.add_argument("--member", type=int, help="담당 번호 (1~5)")
    c.add_argument("--of", type=int, default=5, help="나눠 받는 인원 수")
    sub.add_parser("merge", help="담당별 파일 합치기")
    a = ap.parse_args()
    if a.cmd == "merge":
        return cmd_merge(a)
    if not KEY:
        sys.exit(".env 에 DATA_GO_KR_KEY 를 넣어 주세요.")
    {"probe": cmd_probe, "collect": cmd_collect}[a.cmd](a)


if __name__ == "__main__":
    main()
