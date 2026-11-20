"""
TourAPI 4.0 (KorService2) 전국 관광지 수집 스크립트

1단계: areaBasedList2 로 전국 관광지 목록 수집     -> output/places.csv
2단계: detailCommon2 로 관광지별 개요(overview) 수집 -> output/places_detail.csv

사용법
  1) 이 폴더에 .env 파일을 만들고 아래 한 줄을 넣는다 (Decoding 키 사용)
       TOUR_API_KEY=발급받은_일반인증키_Decoding
  2) pip install requests python-dotenv
  3) python collect_tourapi.py list      # 1단계: 목록 수집
     python collect_tourapi.py detail    # 2단계: 개요 수집 (하루 한도 걸리면 다음날 다시 실행하면 이어서 진행)

주의: 개발계정은 오퍼레이션별 하루 1,000건 제한이 있다.
      2단계는 관광지 1곳당 1번 호출하므로 하루에 약 1,000곳씩만 수집된다.
"""

import csv
import os
import sys
import time

import requests
from dotenv import load_dotenv

load_dotenv()

BASE_URL = "https://apis.data.go.kr/B551011/KorService2"
API_KEY = os.getenv("TOUR_API_KEY")

CONTENT_TYPE_ID = 12     # 12=관광지 (14=문화시설, 15=축제, 28=레포츠, 39=음식점)
ROWS_PER_PAGE = 100
DAILY_LIMIT = 1000       # 개발계정 오퍼레이션별 일일 호출 한도

OUT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "output")
LIST_CSV = os.path.join(OUT_DIR, "places.csv")
DETAIL_CSV = os.path.join(OUT_DIR, "places_detail.csv")

LIST_FIELDS = [
    "contentid", "contenttypeid", "title", "addr1", "addr2", "zipcode", "tel",
    "mapx", "mapy", "firstimage", "firstimage2",
    "lDongRegnCd", "lDongSignguCd", "lclsSystm1", "lclsSystm2", "lclsSystm3",
    "createdtime", "modifiedtime",
]
DETAIL_FIELDS = ["contentid", "title", "overview", "homepage"]


class QuotaExceeded(Exception):
    pass


def call_api(operation, **params):
    """TourAPI 호출 후 item 리스트와 전체 건수를 돌려준다."""
    query = {
        "serviceKey": API_KEY,
        "MobileOS": "ETC",
        "MobileApp": "TravelRecommend",
        "_type": "json",
        **params,
    }
    resp = requests.get(f"{BASE_URL}/{operation}", params=query, timeout=20)

    # 인증키 오류, 호출 한도 초과 등은 json 요청이어도 XML로 온다
    try:
        data = resp.json()
    except ValueError:
        text = resp.text
        if "LIMITED_NUMBER_OF_SERVICE_REQUESTS_EXCEEDS" in text:
            raise QuotaExceeded("오늘 호출 한도(1,000건)를 다 썼습니다. 내일 다시 실행하세요.")
        if "SERVICE_KEY_IS_NOT_REGISTERED" in text:
            raise SystemExit("인증키가 등록되지 않았습니다. Decoding 키인지, 발급 후 30분이 지났는지 확인하세요.")
        raise SystemExit(f"API 오류 응답:\n{text[:500]}")

    header = data["response"]["header"]
    if header.get("resultCode") != "0000":
        raise SystemExit(f"API 오류: {header}")

    body = data["response"]["body"]
    items = body.get("items")
    if not items:                       # 결과가 없으면 items가 "" 로 온다
        return [], body.get("totalCount", 0)
    item = items["item"]
    if isinstance(item, dict):          # 결과가 1건이면 리스트가 아니라 dict로 온다
        item = [item]
    return item, body.get("totalCount", 0)


def collect_list():
    """1단계: 전국 관광지 목록 수집"""
    os.makedirs(OUT_DIR, exist_ok=True)
    rows, page = [], 1
    while True:
        items, total = call_api(
            "areaBasedList2",
            contentTypeId=CONTENT_TYPE_ID,
            numOfRows=ROWS_PER_PAGE,
            pageNo=page,
            arrange="C",
        )
        rows.extend(items)
        print(f"  {page}페이지: {len(rows)}/{total}건")
        if not items or len(rows) >= total:
            break
        page += 1
        time.sleep(0.2)

    with open(LIST_CSV, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=LIST_FIELDS, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)
    print(f"완료: {len(rows)}건 -> {LIST_CSV}")


def collect_detail():
    """2단계: 관광지별 개요 수집 (이미 받은 건 건너뛰고 이어서 진행)"""
    if not os.path.exists(LIST_CSV):
        raise SystemExit("먼저 `python collect_tourapi.py list` 를 실행하세요.")

    with open(LIST_CSV, encoding="utf-8-sig") as f:
        places = list(csv.DictReader(f))

    done = set()
    if os.path.exists(DETAIL_CSV):
        with open(DETAIL_CSV, encoding="utf-8-sig") as f:
            done = {r["contentid"] for r in csv.DictReader(f)}

    todo = [p for p in places if p["contentid"] not in done]
    print(f"전체 {len(places)}건 중 {len(done)}건 완료, 남은 {len(todo)}건")

    is_new = not os.path.exists(DETAIL_CSV)
    with open(DETAIL_CSV, "a", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=DETAIL_FIELDS, extrasaction="ignore")
        if is_new:
            w.writeheader()
        count = 0
        try:
            for p in todo[:DAILY_LIMIT]:
                items, _ = call_api("detailCommon2", contentId=p["contentid"])
                if items:
                    w.writerow(items[0])
                    f.flush()
                count += 1
                if count % 50 == 0:
                    print(f"  {count}건 수집")
                time.sleep(0.1)
        except QuotaExceeded as e:
            print(e)
    print(f"이번 실행에서 {count}건 수집 -> {DETAIL_CSV}")


if __name__ == "__main__":
    if not API_KEY:
        raise SystemExit(".env 파일에 TOUR_API_KEY 를 넣어주세요.")
    mode = sys.argv[1] if len(sys.argv) > 1 else "list"
    if mode == "list":
        collect_list()
    elif mode == "detail":
        collect_detail()
    else:
        print("사용법: python collect_tourapi.py [list|detail]")
