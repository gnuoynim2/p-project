# KAMIS 전 품목 일별 도·소매 가격 수집

## 준비
```
pip install -r requirements.txt
copy .env.example .env      # 메모장으로 열어 KAMIS_CERT_KEY, KAMIS_CERT_ID 입력
```
`.env`와 `data/`는 GitHub에 올라가지 않는다(`.gitignore`).

## 실행 순서
| 단계 | 명령 | 하는 일 | 호출 수 |
|---|---|---|---|
| 1 | `python collect_kamis.py codes` | 최근 1년 매달 15일을 부류·소매/도매별로 조회해 **품목·품종·등급 코드표** 생성 (`data/kamis_codes.csv`). 계절 품종(배추 봄·고랭지·가을·월동)까지 잡힘. 기본은 '상품' 등급만 | 최대 144회 |
| 2 | `python collect_kamis.py test` | 코드표 첫 줄로 **1년치가 한 번에 오는지** 시험 | 1회 |
| 3 | `python collect_kamis.py collect --start 2016-01-01` | 코드표의 모든 줄에 대해 **매일 가격** 수집 (`data/kamis_daily.csv`) | 코드표 줄 수 × 연도 수 |

- 코드표에서 필요 없는 줄(예: 수산물, 축산물)을 지우면 그만큼 호출이 줄어든다.
- 2단계에서 "앞부분이 빠졌다"고 나오면 `collect --chunk 90`(또는 30)으로 기간을 짧게 나눈다.
- 하루 한도가 걱정되면 `collect --max-calls 1000`처럼 나눠서 실행한다. 받은 조각은 `data/kamis_done.txt`에 기록되어, 다시 실행하면 이어서 받는다.
- 연속 5번 실패하면(인증 오류, 한도 초과 등) 멈춘다.

## 결과 열
`data/kamis_daily.csv`: date, cls(소매/도매), category, item_code, item_name, kind_code, kind_name, rank_code, rank, unit, county, price
- `county`가 "평균"인 줄이 전국 평균 가격이다.
- 가격은 원 단위 숫자이고, 값이 없는 날은 빈칸이다.
- 소매·도매는 단위가 다르다(예: 배추 소매 1포기, 도매 10kg). 비교할 때는 단위를 맞춘다.
