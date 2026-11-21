"""
여행지 추천 AI 에이전트 시험 스크립트 (v0.3: 이어서 묻기, 붐빌 때 진행분 유지, 속도 개선)

AI(Gemini)가 질문을 보고 두 도구 중 필요한 것을 스스로 골라 쓴 뒤,
API 명세(POST /api/recommend 응답) 모양의 JSON으로 답한다.

  도구 1. search_places   : 수집한 TourAPI 관광지 CSV에서 후보 찾기
  도구 2. search_blog     : 네이버 블로그(API HUB)에서 최근 후기 찾기

준비
  1) data/.env 에 아래 키가 있어야 한다
       GEMINI_API_KEY=...
       NAVER_CLIENT_ID=...
       NAVER_CLIENT_SECRET=...
       (선택) GEMINI_MODEL=gemini-3.8-flash
  2) data/output/places.csv, places_detail.csv 가 있어야 한다 (collect_tourapi.py 결과)
  3) pip install -r ai/requirements.txt

실행 (p-project 폴더에서)
  python ai/agent_test.py "강원도에서 커플끼리 가기 좋은 여행지 추천해줘"
  python ai/agent_test.py --batch ai/test_questions.txt     # 여러 질문을 한 번에, 결과는 ai/results/ 에 저장
  python ai/agent_test.py --chat                            # 대화 모드: 앞의 질문과 답을 기억하고 이어서 묻기
"""

import csv
import json
import os
import re
import sys
import time
from datetime import datetime
from pathlib import Path

import requests
from dotenv import load_dotenv
from google import genai
from google.genai import errors as genai_errors
from google.genai import types

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / "data" / ".env")

GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-3.8-flash")
MAX_TOOL_CALLS = 6     # 질문 하나에 AI가 도구를 부를 수 있는 최대 횟수
# 생각 수준: 낮을수록 빠르다. 추천 품질이 떨어지면 medium 으로 올리고, 빈 값이면 모델 기본값
THINKING_LEVEL = os.getenv("GEMINI_THINKING_LEVEL", "low").strip().lower() or None
NO_THINKING_MODELS = set()   # 생각 수준 설정을 거부한 모델 (자동으로 채워짐)
MAX_HISTORY_TURNS = 5  # 대화 모드에서 기억할 최근 질문·답 쌍의 수 (너무 길면 느려지고 비용이 커진다)
BUSY_WAITS = [3, 8]    # 서버가 붐빌 때 같은 모델로 다시 시도하기 전 기다리는 시간(초)
# 기본 모델이 붐빌 때 차례로 시도할 예비 모델 (쉼표로 구분)
FALLBACK_MODELS = [m.strip() for m in os.getenv(
    "GEMINI_FALLBACK_MODELS", "gemini-3.6-flash,gemini-3.5-flash-lite,gemini-3.1-flash-lite").split(",") if m.strip()]
NAVER_ID = os.getenv("NAVER_CLIENT_ID")
NAVER_SECRET = os.getenv("NAVER_CLIENT_SECRET")

PLACES_CSV = ROOT / "data" / "output" / "places.csv"
DETAIL_CSV = ROOT / "data" / "output" / "places_detail.csv"
RESULTS_DIR = ROOT / "ai" / "results"

NAVER_BLOG_URL = "https://naverapihub.apigw.ntruss.com/search/v1/blog"


# ---------------------------------------------------------------- 데이터 불러오기
def load_places():
    """관광지 목록 + 설명글을 합쳐서 메모리에 올린다."""
    if not PLACES_CSV.exists():
        sys.exit(f"{PLACES_CSV} 가 없습니다. 먼저 data/collect_tourapi.py list 를 실행하세요.")

    overviews = {}
    if DETAIL_CSV.exists():
        with open(DETAIL_CSV, encoding="utf-8-sig") as f:
            for r in csv.DictReader(f):
                overviews[r["contentid"]] = clean_text(r.get("overview", ""))

    places = {}
    with open(PLACES_CSV, encoding="utf-8-sig") as f:
        for r in csv.DictReader(f):
            places[r["contentid"]] = {
                "id": r["contentid"],
                "name": r["title"],
                "address": r.get("addr1", ""),
                "lat": to_float(r.get("mapy")),   # TourAPI: mapy = 위도
                "lng": to_float(r.get("mapx")),   # TourAPI: mapx = 경도
                "image": r.get("firstimage") or None,
                "overview": overviews.get(r["contentid"], ""),
            }
    return places


def clean_text(s):
    s = re.sub(r"<[^>]+>", " ", s or "")
    s = s.replace("&quot;", '"').replace("&amp;", "&").replace("&lt;", "<").replace("&gt;", ">")
    return re.sub(r"\s+", " ", s).strip()


def to_float(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


PLACES = {}
USED_TOOLS = []   # AI가 이번 질문에서 쓴 도구 기록 (응답의 usedTools)
TOOL_CACHE = {}   # 블로그 검색 결과 저장. 모델이 바뀌어도 같은 검색을 다시 하지 않는다


# ---------------------------------------------------------------- 도구 1: 관광지 검색
def search_places(region: str = "", keywords: str = "", limit: int = 8) -> dict:
    """한국관광공사 TourAPI에서 수집한 전국 관광지 데이터에서 후보를 찾는다.

    Args:
        region: 지역 이름. 주소에 포함된 글자로 찾는다. 예: "강원", "강릉", "부산 해운대". 상관없으면 빈 문자열.
        keywords: 관광지 이름이나 설명글에서 찾을 단어들, 띄어쓰기로 구분. 예: "바다 해변 카페". 없으면 빈 문자열.
        limit: 돌려받을 최대 개수 (1~15).

    Returns:
        조건에 맞는 관광지 목록. 각 항목은 id, name, address, overview(설명글 일부)를 가진다.
    """
    USED_TOOLS.append("tourDb")
    limit = max(1, min(int(limit or 8), 15))
    key = ("places", (region or "").strip(), (keywords or "").strip(), limit)
    if key in TOOL_CACHE:
        log(f"  [도구] search_places(region={region!r}, keywords={keywords!r}) → 이전 결과 재사용")
        return TOOL_CACHE[key]
    words = [w for w in (keywords or "").split() if w]
    region_words = [w for w in (region or "").split() if w]

    scored = []
    for p in PLACES.values():
        if region_words and not all(w in p["address"] for w in region_words):
            continue
        text = p["name"] + " " + p["overview"]
        hits = sum(text.count(w) for w in words)
        if words and hits == 0:
            continue
        # 설명글이 있는 곳을 우선, 키워드가 많이 나온 곳을 우선
        scored.append((hits + (2 if p["overview"] else 0), p))

    scored.sort(key=lambda x: x[0], reverse=True)
    results = [{
        "id": p["id"],
        "name": p["name"],
        "address": p["address"],
        "overview": p["overview"][:200] or "(설명글 아직 없음)",
    } for _, p in scored[:limit]]
    log(f"  [도구] search_places(region={region!r}, keywords={keywords!r}) → {len(results)}곳")
    if not results:
        out = {"count": 0, "places": [], "hint": "결과 없음. 더 일반적인 단어(예: 바다, 공원, 박물관)로 바꾸거나 키워드를 비워 보세요."}
    else:
        out = {"count": len(results), "places": results}
    TOOL_CACHE[key] = out
    return out


# ---------------------------------------------------------------- 도구 2: 블로그 검색
def search_blog(query: str, count: int = 5) -> dict:
    """네이버 블로그에서 최신 글을 검색한다. 관광지의 최근 후기, 분위기, 운영 여부, 방문 팁을 확인할 때 쓴다.

    Args:
        query: 검색어. 관광지 이름을 넣고 필요하면 "후기", "아이랑", "데이트" 같은 단어를 붙인다. 예: "안목해변 커피거리 데이트 후기"
        count: 가져올 글 수 (1~10).

    Returns:
        블로그 글 목록. 각 항목은 title, link, summary, date를 가진다.
    """
    USED_TOOLS.append("webSearch")
    count = max(1, min(int(count or 5), 10))
    key = ("blog", query.strip(), count)
    if key in TOOL_CACHE:   # 앞 모델이 이미 찾은 검색이면 다시 부르지 않는다
        log(f"  [도구] search_blog({query!r}) → 이전 결과 재사용")
        return TOOL_CACHE[key]
    try:
        r = requests.get(
            NAVER_BLOG_URL,
            headers={"X-NCP-APIGW-API-KEY-ID": NAVER_ID, "X-NCP-APIGW-API-KEY": NAVER_SECRET},
            params={"query": query, "display": count, "sort": "sim", "format": "json"},
            timeout=10,
        )
        r.raise_for_status()
        items = r.json().get("items", [])
    except Exception as e:
        log(f"  [도구] search_blog({query!r}) 실패: {e}")
        return {"error": f"블로그 검색 실패: {e}", "posts": []}

    posts = [{
        "title": clean_text(it.get("title")),
        "link": it.get("link"),
        "summary": clean_text(it.get("description"))[:200],
        "date": it.get("postdate"),
    } for it in items]
    log(f"  [도구] search_blog({query!r}) → {len(posts)}건")
    result = {"count": len(posts), "posts": posts}
    TOOL_CACHE[key] = result
    return result


# ---------------------------------------------------------------- AI 지시문
SYSTEM_PROMPT = """너는 한국 국내 여행지를 추천하는 여행 비서다.

규칙
1. 추천하는 관광지는 반드시 search_places 결과에 나온 곳이어야 한다. 결과에 없는 장소를 지어내지 않는다.
2. 사용자의 조건(지역, 동행, 아이 나이, 예산, 날짜)을 먼저 파악하고, search_places를 부른다.
   "커플", "데이트", "가족"처럼 설명글에 잘 안 나오는 말 대신 바다, 해변, 호수, 공원, 박물관, 체험처럼 장소의 특징을 키워드로 쓴다.
   search_places는 최대 2번까지만 부른다. 첫 검색은 여러 키워드를 한 번에 넣어 넓게 찾는다.
3. 최종 후보 2~3곳을 정한 뒤, 최근 후기나 분위기, 아이와 가도 되는지, 운영 여부처럼 설명글만으로 판단하기 어려운 점이 있으면
   그 후보들에 대해서만 search_blog를 부른다. 한 장소에 한 번, 모두 합쳐 최대 3번.
4. 2~4곳을 추천하고, 각 장소의 추천 이유를 사용자의 조건과 연결해서 한 문장으로 쓴다.
5. 블로그를 참고한 장소는 sources에 그 글의 link를 넣는다. 참고하지 않았으면 빈 목록.
6. 조건을 더 좁히면 좋을 때 followUp에 짧은 질문 하나와, suggestions에 선택지 2~4개를 쓴다. 필요 없으면 null과 빈 목록.
7. 이전 대화가 있으면 앞에서 말한 조건(지역, 동행 등)을 이어받는다. 사용자가 이전 followUp에 답한 것이면 그 답을 조건에 더한다.
   "거기", "아까 그곳"처럼 앞에서 추천한 장소를 가리키면 그 장소의 id를 그대로 쓴다.
   새 장소를 추천할 때는 다시 search_places로 찾는다. 이전 답에 나온 장소만 다시 다룰 때는 search_places를 생략해도 된다.

마지막 답은 아래 JSON 하나만 출력한다. 설명 문장이나 코드 블록 표시는 붙이지 않는다.
{
  "answer": "사용자 조건을 반영한 답변 1~2문장",
  "city": "추천 도시 또는 지역",
  "places": [
    {"id": "search_places에서 받은 id", "name": "이름", "reason": "추천 이유", "sources": ["블로그 링크"]}
  ],
  "followUp": "되묻는 질문 또는 null",
  "suggestions": ["선택지1", "선택지2"]
}"""


# ---------------------------------------------------------------- 실행
def log(msg):
    print(msg, file=sys.stderr)


def parse_json(text):
    """AI가 코드 블록(```json)으로 감싸서 줄 때도 읽을 수 있게 한다."""
    t = (text or "").strip()
    t = re.sub(r"^```(?:json)?\s*|\s*```$", "", t)
    start, end = t.find("{"), t.rfind("}")
    if start == -1 or end == -1:
        raise ValueError("JSON을 찾지 못함")
    return json.loads(t[start:end + 1])


TOOLS = {"search_places": search_places, "search_blog": search_blog}


class ModelUnavailable(Exception):
    """이 모델로는 더 진행할 수 없음 → 다음 모델로 넘어가라는 신호"""
    def __init__(self, code, reason):
        super().__init__(f"{code} {reason}")
        self.code = code


def make_config(model, final=False):
    cfg = dict(
        system_instruction=SYSTEM_PROMPT,
        tools=[search_places, search_blog],
        automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True),  # 도구 실행은 우리가 직접
        temperature=0.3,
    )
    if final:   # 도구를 너무 많이 썼으면 더 못 부르게 하고 답만 받는다
        cfg["tool_config"] = types.ToolConfig(
            function_calling_config=types.FunctionCallingConfig(mode=types.FunctionCallingConfigMode.NONE))
    if THINKING_LEVEL and model not in NO_THINKING_MODELS:
        cfg["thinking_config"] = types.ThinkingConfig(thinking_level=THINKING_LEVEL)
    return types.GenerateContentConfig(**cfg)


def call_gemini(client, model, contents, final=False):
    """Gemini 한 번 호출. 붐비면(5xx) 같은 내용으로 이 단계만 다시 시도한다.
    → 앞에서 끝낸 도구 검색은 버리지 않는다."""
    for attempt in range(len(BUSY_WAITS) + 1):
        t = time.time()
        try:
            r = client.models.generate_content(model=model, contents=contents, config=make_config(model, final))
            log(f"  [Gemini] {model} 응답 {time.time() - t:.1f}초")
            return r
        except genai_errors.APIError as e:
            if e.code == 400 and THINKING_LEVEL and model not in NO_THINKING_MODELS:
                log(f"  {model} 은(는) 생각 수준 설정을 지원하지 않아 빼고 다시 시도")
                NO_THINKING_MODELS.add(model)
                continue
            if e.code in (500, 503) and attempt < len(BUSY_WAITS):
                wait = BUSY_WAITS[attempt]
                log(f"  {model} 서버 붐빔({e.code}), {wait}초 뒤 이 단계만 다시 시도")
                time.sleep(wait)
                continue
            if e.code in (404, 429, 500, 503):
                raise ModelUnavailable(e.code, {404: "쓸 수 없는 모델", 429: "사용량 한도"}.get(e.code, "계속 붐빔"))
            raise
    raise ModelUnavailable(503, "계속 붐빔")


def run_agent(client, model, question, history=None):
    """에이전트 반복: Gemini 호출 → 도구 요청이 있으면 실행해서 결과를 돌려줌 → 답이 나올 때까지."""
    contents = list(history or []) + [types.Content(role="user", parts=[types.Part(text=question)])]
    tool_calls = 0
    while True:
        final = tool_calls >= MAX_TOOL_CALLS
        r = call_gemini(client, model, contents, final=final)
        calls = r.function_calls or []
        if not calls or final:
            return r.text
        contents.append(r.candidates[0].content)   # 모델의 도구 요청(생각 서명 포함)을 그대로 기록
        parts = []
        for fc in calls:
            tool_calls += 1
            fn = TOOLS.get(fc.name)
            try:
                out = fn(**(fc.args or {})) if fn else {"error": f"없는 도구: {fc.name}"}
            except Exception as e:
                out = {"error": f"도구 실행 실패: {e}"}
            parts.append(types.Part.from_function_response(name=fc.name, response=out))
        contents.append(types.Content(role="user", parts=parts))


def recommend(client, question, history=None):
    """모델이 계속 붐비거나 한도(429)·사용 불가(404)면 예비 모델로 넘어간다.
    넘어가면 처음부터 다시 하지만, 이미 한 검색은 TOOL_CACHE에서 바로 꺼내 쓴다."""
    t0 = time.time()
    last_error = None
    text = None
    model = None
    for model in [GEMINI_MODEL] + FALLBACK_MODELS:
        USED_TOOLS.clear()
        try:
            text = run_agent(client, model, question, history)
            break
        except ModelUnavailable as e:
            last_error = e
            log(f"  {model} 사용 불가({e}), 다음 모델로 넘어갑니다")
    if text is None:
        raise last_error
    elapsed = round(time.time() - t0, 1)

    try:
        result = parse_json(text)
    except Exception as e:
        return {"error": f"AI 답을 JSON으로 읽지 못함: {e}", "raw": text,
                "usedTools": sorted(set(USED_TOOLS)), "seconds": elapsed}

    # 환각 검사: AI가 준 id가 실제 데이터에 있는지 확인하고, 좌표·사진은 데이터에서 채운다
    for p in result.get("places", []):
        real = PLACES.get(str(p.get("id")))
        p["verified"] = real is not None
        if real:
            p["name"] = real["name"]
            p["lat"], p["lng"], p["image"] = real["lat"], real["lng"], real["image"]

    result["usedTools"] = sorted(set(USED_TOOLS))
    result["model"] = model
    result["seconds"] = elapsed
    return result


def remember(history, question, result):
    """대화 기록에 이번 질문과 답을 더한다. 좌표·사진처럼 AI에게 필요 없는 값은 빼서 짧게 저장한다."""
    brief = {
        "answer": result.get("answer"),
        "city": result.get("city"),
        "places": [{"id": p.get("id"), "name": p.get("name"), "reason": p.get("reason")}
                   for p in result.get("places", [])],
        "followUp": result.get("followUp"),
        "suggestions": result.get("suggestions", []),
    }
    history.append(types.Content(role="user", parts=[types.Part(text=question)]))
    history.append(types.Content(role="model", parts=[types.Part(text=json.dumps(brief, ensure_ascii=False))]))
    del history[:-MAX_HISTORY_TURNS * 2]   # 오래된 대화는 버린다


def chat_loop(client):
    """대화 모드. 빈 줄이나 exit 를 입력하면 끝난다.
    직전 답에 선택지(suggestions)가 있으면 번호만 입력해도 그 선택지를 고른 것으로 본다."""
    history = []
    suggestions = []
    log("대화 모드입니다. 끝내려면 exit 를 입력하세요. 선택지는 번호로 고를 수 있어요.")
    while True:
        try:
            q = input("\n질문: ").strip()
        except (EOFError, KeyboardInterrupt):
            break
        if not q or q.lower() in ("exit", "quit", "q"):
            break
        if q.isdigit() and 1 <= int(q) <= len(suggestions):
            q = suggestions[int(q) - 1]
            log(f"  → 선택: {q}")
        try:
            result = recommend(client, q, history)
        except (ModelUnavailable, genai_errors.APIError) as e:
            log(f"  모든 모델을 쓰지 못했습니다. 잠시 뒤 다시 질문해 주세요. ({e})")
            continue
        print(json.dumps(result, ensure_ascii=False, indent=2))
        if "error" in result:
            continue
        remember(history, q, result)
        suggestions = result.get("suggestions") or []
        if result.get("followUp"):
            log(f"\nAI: {result['followUp']}")
            for i, s_ in enumerate(suggestions, 1):
                log(f"  {i}. {s_}")


def main():
    if not GEMINI_API_KEY:
        sys.exit("data/.env 에 GEMINI_API_KEY 가 없습니다.")
    if not (NAVER_ID and NAVER_SECRET):
        log("경고: NAVER_CLIENT_ID / NAVER_CLIENT_SECRET 가 없어 블로그 검색이 실패합니다.")

    global PLACES
    PLACES = load_places()
    with_overview = sum(1 for p in PLACES.values() if p["overview"])
    log(f"관광지 {len(PLACES)}곳 불러옴 (설명글 있는 곳 {with_overview}곳) · 모델 {GEMINI_MODEL}")

    client = genai.Client(api_key=GEMINI_API_KEY)
    args = sys.argv[1:]

    if args[:1] == ["--batch"]:
        questions = [q.strip() for q in Path(args[1]).read_text(encoding="utf-8").splitlines()
                     if q.strip() and not q.startswith("#")]
        RESULTS_DIR.mkdir(exist_ok=True)
        out = RESULTS_DIR / f"{datetime.now():%Y%m%d-%H%M}_{GEMINI_MODEL}.jsonl"
        with open(out, "w", encoding="utf-8") as f:
            for i, q in enumerate(questions, 1):
                log(f"\n[{i}/{len(questions)}] {q}")
                try:
                    r = recommend(client, q)
                except Exception as e:
                    r = {"error": str(e)}
                    log(f"  오류: {e}")
                f.write(json.dumps({"question": q, "result": r}, ensure_ascii=False) + "\n")
                time.sleep(4)   # 무료 사용량의 분당 제한을 피하려고 잠깐 쉰다
        log(f"\n결과 저장: {out}")
        return

    if args[:1] == ["--chat"]:
        chat_loop(client)
        return

    question = " ".join(args) or input("질문: ")
    try:
        result = recommend(client, question)
    except Exception as e:
        if isinstance(e, (ModelUnavailable, genai_errors.APIError)):
            tried = ", ".join([GEMINI_MODEL] + FALLBACK_MODELS)
            sys.exit(f"시도한 모델을 모두 쓰지 못했습니다 ({tried}).\n"
                     f"몇 분 뒤에 다시 하거나, data/.env 의 GEMINI_MODEL / GEMINI_FALLBACK_MODELS 를 바꿔 보세요.\n"
                     f"마지막 오류: {e}")
        raise
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
