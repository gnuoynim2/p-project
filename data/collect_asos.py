"""
기상청 지상(종관, ASOS) 일자료 수집기 (v0.1)

공공데이터포털 '기상청_지상(종관, ASOS) 일자료 조회서비스' 활용 승인 후 사용.
키는 정산정보와 같은 공공데이터포털 키(.env 의 DATA_GO_KR_KEY)를 그대로 쓴다.

  1) python collect_asos.py probe
       관측소 번호 → 이름이 맞는지, 2006년 자료가 오는지, 한 번에 며칠까지 오는지 확인 (호출 30~40번)
  2) python collect_asos.py collect
       data/asos_stations.csv 의 관측소 전부, 2006-01-01 ~ 어제 → data/asos_daily.csv
       관측소당 999일씩 나눠 받음 (약 8번). 끊기면 다시 실행하면 이어서 받음

관측소 목록은 처음 실행할 때 data/asos_stations.csv 로 만들어진다. 엑셀로 열어 줄을 더하거나 지워도 된다.

저장하는 항목 (기상청 필드 이름 그대로)
  avgTa 평균기온  minTa 최저기온  maxTa 최고기온  minTg 최저초상온도(서리)
  sumRn 일강수량  hr1MaxRn 1시간 최다강수  sumSsHr 일조시간  sumGsr 일사량
  ddMes 일 최심신적설  avgRhm 평균습도  avgWs 평균풍속  maxWs 최대풍속  maxInsWs 최대순간풍속
  ※ 비가 안 온 날은 sumRn 이 빈칸으로 온다 → 가공할 때 0으로 바꾼다 (받을 때는 그대로 둠)
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
KEY = os.getenv("DATA_GO_KR_KEY", "")
URL = "https://apis.data.go.kr/1360000/AsosDalyInfoService/getWthrDataList"
DATA = HERE / "data"
STATIONS = DATA / "asos_stations.csv"
OUT = DATA / "asos_daily.csv"
DONE = DATA / "asos_done.txt"
FIELDS = ["avgTa", "minTa", "maxTa", "minTg", "sumRn", "hr1MaxRn", "sumSsHr", "sumGsr",
          "ddMes", "avgRhm", "avgWs", "maxWs", "maxInsWs"]
OUT_F = ["date", "stn_id", "stn_name"] + FIELDS
CALLS = 0

# 주산지 중심 초안 (번호는 probe 로 이름을 꼭 확인할 것)
DEFAULT_STATIONS = [
    ("108", "서울", "기준점(소비지)"),
    ("100", "대관령", "고랭지 배추·무·감자"),
    ("216", "태백", "고랭지 배추·무"),
    ("101", "춘천", "강원 내륙"),
    ("105", "강릉", "강원 영동"),
    ("114", "원주", "강원 남부"),
    ("119", "수원", "경기 채소"),
    ("203", "이천", "경기 쌀·채소"),
    ("131", "청주", "충북"),
    ("221", "제천", "충북 북부"),
    ("232", "천안", "충남 시설채소"),
    ("236", "부여", "충남 시설채소(수박·토마토)"),
    ("129", "서산", "충남 서해안(마늘·생강)"),
    ("146", "전주", "전북"),
    ("245", "정읍", "전북 서부"),
    ("156", "광주", "전남 내륙"),
    ("165", "목포", "무안·신안 양파·마늘·배추"),
    ("261", "해남", "가을·월동 배추"),
    ("262", "고흥", "전남 남해안"),
    ("174", "순천", "전남 동부 시설채소"),
    ("136", "안동", "경북 북부(고추·사과)"),
    ("278", "의성", "경북 마늘"),
    ("281", "영천", "경북 남부(마늘·과일)"),
    ("143", "대구", "경북 중부"),
    ("192", "진주", "경남 시설채소(풋고추·애호박)"),
    ("288", "밀양", "경남 창녕·밀양 양파·마늘"),
    ("155", "창원", "경남 남부"),
    ("184", "제주", "제주 북부(월동무·당근·양배추)"),
    ("189", "서귀포", "제주 남부(감귤·월동채소)"),
    ("188", "성산", "제주 동부(당근·무)"),
]


def ensure_stations():
    DATA.mkdir(exist_ok=True)
    if not STATIONS.exists():
        with open(STATIONS, "w", newline="", encoding="utf-8-sig") as f:
            w = csv.writer(f)
            w.writerow(["stn_id", "name", "note"])
            w.writerows(DEFAULT_STATIONS)
        print(f"관측소 목록 {len(DEFAULT_STATIONS)}곳 생성: {STATIONS} (엑셀로 고쳐도 됨)")
    with open(STATIONS, encoding="utf-8-sig") as f:
        return [r for r in csv.DictReader(f) if r.get("stn_id")]


def fetch(stn, start, end, rows=999, page=1):
    global CALLS
    params = {"serviceKey": KEY, "pageNo": page, "numOfRows": rows, "dataType": "JSON",
              "dataCd": "ASOS", "dateCd": "DAY", "startDt": start.strftime("%Y%m%d"),
              "endDt": end.strftime("%Y%m%d"), "stnIds": stn}
    for attempt in range(4):
        r = None
        try:
            r = requests.get(URL, params=params, timeout=60)
            CALLS += 1
            js = r.json()
            head = js["response"]["header"]
            if head.get("resultCode") not in ("00", "0"):
                return [], 0, f"{head.get('resultCode')} {head.get('resultMsg')}"
            body = js["response"]["body"]
            items = (body.get("items") or {}).get("item", [])
            if isinstance(items, dict):
                items = [items]
            return items, int(body.get("totalCount") or 0), None
        except (requests.RequestException, ValueError, KeyError, TypeError) as e:
            if attempt == 3:
                txt = r.text[:200] if r is not None else ""
                return [], 0, f"응답 오류: {e} {txt}"
            time.sleep(3 * (attempt + 1))


def cmd_probe(a):
    st = ensure_stations()
    y = date.today() - timedelta(days=1)
    print("[관측소 번호 → 이름 확인] (최근 3일)")
    bad = []
    for s in st:
        items, _, err = fetch(s["stn_id"], y - timedelta(days=2), y, rows=3)
        got = items[0].get("stnNm") if items else None
        ok = got and (got == s["name"] or s["name"] in got or got in s["name"])
        print(f"  {s['stn_id']:>4} {s['name']:<6} → {got or err or '자료 없음'} {'' if ok else '  ← 확인 필요'}")
        if not ok:
            bad.append(s["stn_id"])
    print("\n[2006년 자료와 한 번에 받을 수 있는 날 수] 서울 108")
    items, total, err = fetch("108", date(2006, 1, 1), date(2008, 9, 25), rows=999)
    print(f"  2006-01-01 ~ 2008-09-25 (999일) 요청 → {len(items)}행 / totalCount {total} {err or ''}")
    if items:
        print(f"  첫 행 날짜 {items[0].get('tm')}, 마지막 {items[-1].get('tm')}")
        print("  첫 행 일부:", {k: items[0].get(k) for k in ["tm", "stnNm"] + FIELDS})
    print(f"\n호출 {CALLS}회. " + (f"이름이 다른 관측소 {bad} 는 asos_stations.csv 에서 번호를 고치거나 지우세요." if bad else "관측소 번호 모두 정상."))


def cmd_collect(a):
    st = ensure_stations()
    start, end = date.fromisoformat(a.start), date.fromisoformat(a.end)
    chunks, s = [], start
    while s <= end:
        e = min(s + timedelta(days=a.chunk - 1), end)
        chunks.append((s, e))
        s = e + timedelta(days=1)
    done = set(DONE.read_text(encoding="utf-8").split("\n")) if DONE.exists() else set()
    todo = [(x, c) for x in st for c in chunks if f"{x['stn_id']}|{c[0]}|{c[1]}" not in done]
    print(f"관측소 {len(st)}곳 × 기간 {len(chunks)}조각 · 남은 호출 약 {len(todo)}회")
    new = not OUT.exists()
    fails = 0
    with open(OUT, "a", newline="", encoding="utf-8-sig") as f, open(DONE, "a", encoding="utf-8") as dlog:
        w = csv.DictWriter(f, fieldnames=OUT_F)
        if new:
            w.writeheader()
        last = None
        for x, (s, e) in todo:
            items, total, err = fetch(x["stn_id"], s, e, rows=a.chunk)
            if err and "NO_DATA" not in err and not err.startswith("03"):
                fails += 1
                print(f"  실패 {x['name']} {s}~{e}: {err}")
                if fails >= 5:
                    print("\n연속 5번 실패해서 멈췄습니다. 다시 실행하면 이어서 받습니다.")
                    break
                continue
            fails = 0
            if total > len(items):            # 한 번에 다 안 왔으면 나머지 페이지
                for p in range(2, -(-total // a.chunk) + 1):
                    more, _, _ = fetch(x["stn_id"], s, e, rows=a.chunk, page=p)
                    items += more
            for it in items:
                d = str(it.get("tm", ""))[:10]
                if not d or not (s.isoformat() <= d <= e.isoformat()):
                    continue                  # 요청 기간 밖 날짜는 버림 (KAMIS 때 교훈)
                w.writerow({"date": d, "stn_id": x["stn_id"], "stn_name": it.get("stnNm", x["name"]),
                            **{k: it.get(k, "") for k in FIELDS}})
            f.flush()
            dlog.write(f"{x['stn_id']}|{s}|{e}\n")
            dlog.flush()
            if last != x["stn_id"]:
                print(f"  {x['stn_id']} {x['name']} 받는 중 · 누적 호출 {CALLS}회")
                last = x["stn_id"]
    print(f"\n끝. 호출 {CALLS}회 · 결과 {OUT}")


def main():
    ap = argparse.ArgumentParser(description="기상청 ASOS 일자료 수집")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("probe")
    c = sub.add_parser("collect")
    c.add_argument("--start", default="2006-01-01")
    c.add_argument("--end", default=(date.today() - timedelta(days=1)).isoformat())
    c.add_argument("--chunk", type=int, default=999, help="한 번에 요청하는 날 수 (최대 999)")
    a = ap.parse_args()
    if not KEY:
        sys.exit(".env 에 DATA_GO_KR_KEY 를 넣어 주세요.")
    {"probe": cmd_probe, "collect": cmd_collect}[a.cmd](a)


if __name__ == "__main__":
    main()
