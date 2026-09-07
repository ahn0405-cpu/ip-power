"""정적 사이트 렌더링 — 인터랙티브 단일 페이지 리더(SPA).

아카이브(뉴스·특허)를 index.html 한 장에 담고, 브라우저에서 통합검색·다중필터·
정렬·가벼운 개요(스탯 타일 + 스파크라인)를 수행한다. 백엔드 없음.
라이트/다크 자동, 필터 상태는 URL 해시에 반영(공유 가능).

원자료(data/*.json, data/patents/*.json)는 build_site 가 계속 저장한다(아카이브 누적용).
이 모듈은 그 데이터를 하나로 합쳐 SPA 로 렌더한다.

── 지연 로딩 ────────────────────────────────────────────────────
예전에는 아카이브 전체를 인라인했다. 실측 5,164건에서 전송량이 986 KB(gzip)가
됐고, 그중 데이터가 4,631 KB·코드가 147 KB였다 — 즉 무게는 전부 목록이다.
그리고 이건 기준선 문제가 아니라 **증가** 문제다. 아카이브는 매주 쌓이므로
자르기로는 언젠가 반드시 진다.

그래서 목록(items)만 둘로 나눈다:
  · 최근 INLINE_* 건 → index.html 에 그대로 (첫 화면은 여기서 다 그려진다)
  · 나머지          → data/feed/{news,patents}-rest.json, 그린 뒤 받아서 붙인다
집계(총계·주별 추이·공개일 범위 등)는 **나누기 전 전체**로 서버에서 계산해
인라인한다. 그래야 덜 받은 상태에서도 숫자가 틀리지 않는다.

그래도 항목을 직접 세는 화면(거래·지원의 경쟁 구도·공급자 표, 목록 건수)은
다 받기 전까지 잠깐 작은 값을 보게 된다. 그 화면들은 로딩이 끝날 때까지
'불러오는 중'으로 두고 숫자를 내보내지 않는다 — 틀린 수를 잠깐 보여주는 것이
늦게 보여주는 것보다 나쁘다.

file:// 로 열면 fetch 가 CORS 에 막힌다. 그때는 받은 만큼만 쓰고 그 사실을
화면에 밝힌다. 통째로 인라인해 예전처럼 쓰려면 NEWS_INLINE_ALL=1 로 빌드한다
(로컬 검증·오프라인 배포용 탈출구).
"""
from __future__ import annotations

import html
import json
import os
import re
import urllib.parse
from datetime import timezone, timedelta
from pathlib import Path

import news_config as ncfg
import patent_config as pcfg
import insights as _insights
import ip_guide
import favicon as _favicon

KST = timezone(timedelta(hours=9))

# ── 인라인 분량 ──────────────────────────────────────────────────
# 시간(최근 N일)이 아니라 **건수**로 자른다. 시간 기준은 전송량을 묶어 주지 못한다
# — 뉴스가 몰린 주 하나가 그대로 페이로드가 된다. 건수로 자르면 아카이브가
# 아무리 자라도 첫 화면 전송량이 고정된다. 그게 이 작업의 목적이다.
# 실측(5,164건 기준): 뉴스 400 + 특허 1,200 → 986 KB → 265 KB.
INLINE_NEWS = int(os.getenv("NEWS_INLINE_NEWS", "400"))
INLINE_PATENTS = int(os.getenv("NEWS_INLINE_PATENTS", "1200"))
INLINE_ALL = os.getenv("NEWS_INLINE_ALL", "").lower() in ("1", "true", "yes", "on")
FEED_SUBDIR = "data/feed"

# ── 출원인(assignee) 정규화 ───────────────────────────────────────
# 목적: "삼성전자"/"삼성전자주식회사"/"Samsung Electronics Co., Ltd." 를 한 항목으로.
# 접미사(주식회사·Co.,Ltd. 등)를 떼고, 한/영 이름은 별칭 표로 대표명에 병합한다.
_SUFFIX_KEYS = ["주식회사", "유한회사", "coltd", "co", "ltd", "limited", "inc",
                "corp", "corporation", "llc", "gmbh", "company", "plc", "sa",
                "nv", "ag", "holdings", "kk", "ep", "lp"]

# (대표명, 국적 ISO2, [별칭 키...])
_ALIAS_RAW = [
    ("삼성전자", "KR", ["삼성전자", "samsungelectronics", "samsungelec"]),
    ("삼성SDI", "KR", ["삼성sdi", "samsungsdi"]),
    ("삼성전기", "KR", ["삼성전기", "samsungelectromechanics"]),
    ("삼성디스플레이", "KR", ["삼성디스플레이", "samsungdisplay"]),
    ("SK하이닉스", "KR", ["sk하이닉스", "skhynix"]),
    ("SK온", "KR", ["sk온", "skon"]),
    ("LG에너지솔루션", "KR", ["lg에너지솔루션", "lgenergysolution"]),
    ("LG전자", "KR", ["lg전자", "lgelectronics"]),
    ("LG화학", "KR", ["lg화학", "lgchem"]),
    ("LG디스플레이", "KR", ["lg디스플레이", "lgdisplay"]),
    ("현대자동차", "KR", ["현대자동차", "hyundaimotor", "hyundaimotorcompany"]),
    ("기아", "KR", ["기아", "기아자동차", "kia", "kiamotors"]),
    ("현대모비스", "KR", ["현대모비스", "hyundaimobis"]),
    ("현대일렉트릭", "KR", ["현대일렉트릭", "hyundaielectric"]),
    ("한국전력공사", "KR", ["한국전력공사", "한국전력", "kepco", "koreaelectricpower"]),
    ("한국수력원자력", "KR", ["한국수력원자력", "khnp", "koreahydronuclearpower"]),
    ("한국전기연구원", "KR", ["한국전기연구원", "keri"]),
    ("한국에너지기술연구원", "KR", ["한국에너지기술연구원", "kier"]),
    ("한국전자통신연구원", "KR", ["한국전자통신연구원", "etri"]),
    ("LS일렉트릭", "KR", ["ls일렉트릭", "lselectric"]),
    ("LS전선", "KR", ["ls전선", "lscable", "lscns"]),
    ("효성중공업", "KR", ["효성중공업", "hyosungheavyindustries"]),
    ("두산에너빌리티", "KR", ["두산에너빌리티", "doosanenerbility", "두산중공업", "doosanheavyindustries"]),
    ("포스코", "KR", ["포스코", "posco", "포스코홀딩스"]),
    ("한화솔루션", "KR", ["한화솔루션", "hanwhasolutions"]),
    ("Qualcomm", "US", ["qualcomm"]),
    ("Intel", "US", ["intel"]),
    ("Micron", "US", ["micron", "microntechnology"]),
    ("Applied Materials", "US", ["appliedmaterials"]),
    ("General Electric", "US", ["generalelectric"]),
    ("Tesla", "US", ["tesla"]),
    ("Google", "US", ["google"]),
    ("Apple", "US", ["apple"]),
    ("Westinghouse", "US", ["westinghouse", "westinghouseelectric"]),
    ("TSMC", "TW", ["tsmc", "taiwansemiconductormanufacturing"]),
    ("Siemens", "DE", ["siemens"]),
    ("Bosch", "DE", ["bosch", "robertbosch"]),
    ("Panasonic", "JP", ["panasonic"]),
    ("Toyota", "JP", ["toyota", "toyotamotor"]),
    ("Sony", "JP", ["sony"]),
    ("CATL", "CN", ["catl", "contemporaryamperextechnology"]),
    ("BYD", "CN", ["byd"]),
]
_ALIASES = {a: canon for canon, _co, al in _ALIAS_RAW for a in al}
_CANON_CO = {canon: co for canon, co, _al in _ALIAS_RAW}
_HANGUL = re.compile(r"[가-힣]")


def _assignee_country(canon: str, original: str) -> str:
    """출원인 국적 추정(ISO2). 큐레이션 매핑 우선, 없으면 한글 포함 시 KR, 그 외 미상('')."""
    if canon in _CANON_CO:
        return _CANON_CO[canon]
    if _HANGUL.search(original or ""):
        return "KR"
    return ""


def _akey(s: str) -> str:
    return re.sub(r"[\s.,()·\-_/]+", "", (s or "").lower())


# 검색어가 넓으면 자회사 문서까지 딸려 온다. 실측(8/5): 'Siemens' 로 담긴 54건 중
# 36건이 Siemens Energy·Gamesa 문서였다. OPS 총계는 출원인별 독립 질의라 손쓸 수
# 없지만, **목록은 원문 출원인명이 정답을 갖고 있다** → 더 구체적인 출원인의 검색
# 어구가 원문에 들어 있으면 그쪽으로 옮긴다(어구가 길수록 구체적이다). 수집을 다시
# 하지 않아도 이미 쌓인 데이터가 이 경로에서 교정된다.
_SPECIFIC = sorted(
    ((t.lower(), a["name"], a["region"], a["flag"])
     for a in pcfg.APPLICANTS
     for t in ([a["q"]] if isinstance(a["q"], str) else a["q"])),
    key=lambda x: -len(x[0]))


def _reassign(raw: str, cur: str):
    """원문 출원인명이 더 구체적인 출원인을 가리키면 (이름, 지역, 국기). 아니면 None.

    어느 어구도 안 맞으면 손대지 않는다 — 한글 표기처럼 검색어와 형태가 다른
    경우까지 억지로 옮기면 오히려 틀린다.
    """
    r = (raw or "").lower()
    if not r:
        return None
    for t, name, region, flag in _SPECIFIC:
        if t in r:
            return None if name == cur else (name, region, flag)
    return None


def _plain(s: str) -> str:
    """남은 HTML 실체참조를 푼다 — **표시 직전에** 한다.

    KIPRIS 는 실체참조를 두 번 감싸 줘서 XML 파서가 한 번 푼 뒤에도 '&apos;' 가
    글자로 남는다. 수집기(patent_source_kipris._unescape)에서 이미 풀지만, 그건
    **새로 들어오는 항목에만** 걸린다 — 아카이브는 누적이라 이미 저장된 것은
    그대로다(실측: 고친 뒤에도 저장분 430건에 XI&apos;AN 이 남아 있었다).
    저장소를 건드려 고치는 대신 그리기 직전에 풀면 옛 자료까지 한 번에 맞는다.

    두 번까지만 푼다 — 이름에 '&amp;' 라는 글자가 진짜로 들어 있는 경우까지
    먹어 치우면 안 된다.
    """
    s = s or ""
    for _ in range(2):
        if "&" not in s:
            break
        once = html.unescape(s)
        if once == s:
            break
        s = once
    return s


_STD_NO = re.compile(r"^[A-Z]{2}\d+[A-Z]\d?$")


def _docno(p: dict) -> str:
    """바깥 창구(구글 특허·Espacenet)가 알아듣는 표준 문헌번호를 만든다.

    왜 필요한가: 카드의 링크가 저장된 번호를 그대로 질의로 넘기고 있었는데, 그
    번호가 창구마다 통하는 표기가 아니었다. 국내 공개번호 '1020260127780' 을
    넣으면 구글은 아무것도 못 찾는다 — 사용자가 준 예제가 그 답이었다.
      https://patents.google.com/patent/KR20250160754A/en?oq=KR20250160754
    'oq'(원래 질의)가 KR20250160754 다. 즉 **표준 표기로 검색하면** 구글이 알아서
    문헌 페이지로 보낸다.

    실측(2026-08-25, 러너에서 열어 봄)
      · 2025년 공개 KR20250160754A → 열림
      · 우리 아카이브의 최근 90일 공개분은 표기가 맞아도 전부 404 다.
        EPO OPS 가 준 표준번호(US12687116B1·WO2026142708A1·EP4778833A2 …)까지
        404 였으니 표기 문제가 아니라 **구글이 아직 수록하지 않은 것**이다.
      → 그래서 문헌 직통(/patent/…)이 아니라 검색(?q=)으로 보낸다. 직통은 없는
        문헌이면 404 지만 검색은 '결과 없음' 이라 덜 나쁘고, 수록되는 순간
        같은 링크가 바로 문헌으로 이어진다.

    표기 규칙(저장된 값에서 만든다 — 아카이브가 누적이라 그리기 직전에 고친다):
      이미 표준형(KR20260088580A)  그대로 — OPS 시절 항목이 그렇다
      국내 13자리 1020260127780    앞의 문서종류 '10' 을 떼고 KR + A
      공개국이 앞에 붙은 것         US20260221300 → US20260221300 + 종류코드
      일본의 파이프 JP38…|2026…    파이프 뒤(국제표기) 쪽을 쓴다
      유럽의 앞 0  EP04793971      0 을 떼어 EP4793971
    종류코드는 ltrtno 끝 두 글자에서 가져온다(US 는 A1, EP 는 A1 …). 'A0' 은
    KIPRIS 안에서만 쓰는 표기라 바깥에서는 'A' 로 보낸다.
    """
    raw = (p.get("number") or "").strip()
    if not raw:
        return ""
    if _STD_NO.match(raw):
        return raw
    office = (p.get("office") or "").strip().upper()
    lit = (p.get("ltrtno") or "").strip()
    kind = ""
    m = re.search(r"([A-Z]\d?)$", lit)
    if m:
        kind = "A" if m.group(1) == "A0" else m.group(1)
    body = raw
    if "|" in body:                      # 일본: 자국번호|국제표기
        body = body.split("|")[-1]
    if body.startswith(office) and office:
        body = body[len(office):]
    elif re.fullmatch(r"\d{13}", body):  # 국내 공개번호: 10 + 연도 + 일련번호
        body, office = body[2:], "KR"
    body = body.lstrip("0") or body
    if not office or not body.isdigit():
        return raw                        # 모르는 모양은 손대지 않는다
    return f"{office}{body}{kind or 'A'}"


def _lead(name: str) -> str:
    """공동출원인('|' 로 이어진 이름)에서 대표(첫) 출원인만 남긴다 — 표시 직전에.

    수집기(patent_source_kipris._split_applicants)가 이미 쪼개지만, _plain 과 같은
    이유로 그것만으로는 모자란다: 아카이브가 누적이라 쪼개기 전에 저장된 항목은
    그대로 남는다. 실측으로 252건이 남아 있었고, 그중 하나가
    '애빌린 크리스천 유니버시티|조지아 테크 리서치 코포레이션|더 텍사스 에이 앤드
    엠 유니버시티 시스템|보드 오브 리전츠, 더 유니버시티 오브 텍사스 시스템'
    이었다. 이런 이름 하나가 매트릭스의 이름 칸을 통째로 늘려 표를 오른쪽으로
    밀어낸다. 게다가 그 조합 자체가 하나의 '출원인'으로 잡혀 랭킹도 어긋난다.

    공동출원인 전체가 필요한 곳(공급자 표의 참여 집계)은 원문 assignee 를 쓴다 —
    여기서 버리는 것이 아니라 대표만 골라 두는 것이다.
    """
    return (name or "").split("|")[0].strip() or (name or "").strip()


def _canon_assignee(name: str) -> str:
    s = (name or "").strip().strip(",.")
    if not s:
        return "(출원인 미상)"
    key = _akey(s)
    changed = True
    while changed:                       # 끝의 법인 접미사 반복 제거
        changed = False
        for suf in _SUFFIX_KEYS:
            if len(key) > len(suf) + 1 and key.endswith(suf):
                key = key[:-len(suf)]
                changed = True
    if key in _ALIASES:
        return _ALIASES[key]
    # 별칭에 없으면 원문에서 꼬리 접미사만 떼어 표시
    disp = re.sub(r"[,\s]*(주식회사|유한회사|\(주\)|㈜|Co\.?\s*,?\s*Ltd\.?|Co\.?|Ltd\.?|"
                  r"Inc\.?|Corp\.?(oration)?|L\.?L\.?C\.?|GmbH|Company|PLC|Holdings|"
                  r"S\.?A\.?|N\.?V\.?|A\.?G\.?)\s*$", "", s, flags=re.I).strip().strip(",.")
    return disp or s

SITE_TITLE = "IP-Power 플랫폼"
# 태그라인은 사이트가 실제로 하는 일까지만 적는다. 뉴스·특허를 모으는 데서
# 시작했지만 지금은 분야별 경쟁 구도와 거래 창구 안내까지 있어, '한자리에 모은다'
# 만으로는 절반만 말하는 셈이다. '거래를 돕는다' 로 넘어가지는 않는다 — 우리는
# 알선·중개를 하지 않고 판단 재료와 창구를 알려줄 뿐이다.
SITE_TAGLINE = "전력 뉴스와 특허를 매일 모아 — 기술 동향부터 경쟁 구도·활용까지"
SITE_ORG = "지식재산처 전기통신심사국 전기심사과"
# 헤더에서는 CI 에 '지식재산처' 가 이미 들어 있으므로 소속 부서만 덧붙인다.
SITE_DEPT = "전기통신심사국 전기심사과"
# 호스팅 주소가 기관 도메인이 아니어서, 이용자가 주소만으로는 공식 서비스인지 확인할 수
# 없다. 기관 사이트(스마트전력 연구회)에서 이 사이트로 링크를 걸고, 여기서도 그 페이지로
# 되걸어 양방향으로 확인되게 한다(같은 CI 를 붙인 사칭 사이트와 구별되는 지점).
SITE_CLUB = "스마트전력 연구회"
SITE_ORG_URL = ("https://www.moip.go.kr/club/front/main/index/"
                "mainIndex.do?clubId=display")

# 사이트 자기 주소. canonical·og:url·사이트맵이 같은 값을 가리켜야 검색엔진이
# 하나의 문서로 본다(gh-pages 는 / 와 /index.html 두 주소로 같은 내용을 준다).
def _default_site_url() -> str:
    """gh-pages 주소를 저장소 이름에서 만든다.

    이름을 손으로 적어 두면 저장소 이름을 바꾸는 날 canonical·사이트맵이 조용히
    없는 주소를 가리킨다(검색엔진은 그걸 정본으로 믿는다). Actions 는 언제나
    GITHUB_REPOSITORY 를 주므로 거기서 유도하면 이름이 바뀌어도 따라온다.
    Actions 밖(로컬 빌드)에서는 그 값이 없으니 지금 주소를 그대로 쓴다.
    """
    slug = os.getenv("GITHUB_REPOSITORY", "")
    if "/" in slug:
        owner, repo = slug.split("/", 1)
        # <owner>.github.io 저장소는 경로 없이 뿌리에 붙는다(그 한 경우만 다르다).
        if repo.lower() == f"{owner.lower()}.github.io":
            return f"https://{owner.lower()}.github.io/"
        return f"https://{owner.lower()}.github.io/{repo}/"
    return "https://ahn0405-cpu.github.io/power-news-patents-archive/"


SITE_URL = os.getenv("NEWS_SITE_URL") or _default_site_url()
# Google Search Console 소유 확인. 방식이 둘이고 콘솔에서 고른 쪽과 맞아야 한다.
#   HTML 태그  → head 의 meta(아래 토큰)
#   HTML 파일  → google<토큰>.html 파일을 사이트 루트에 두고, 그 안에
#                "google-site-verification: <파일명>" 한 줄만 적는다.
# 태그 쪽만 넣어 두고 콘솔에서 파일 방식을 고르면 '확인 파일을 찾을 수 없습니다' 가
# 난다(실측) → 파일명을 넣어 두면 둘 다 만족한다.
GSV_TOKEN = os.getenv("NEWS_GSV", "fgZguZl7oY-esN40KhbQKh6Os7yuky7QoGN-fQZLuIc")
GSV_FILE = os.getenv("NEWS_GSV_FILE", "")   # 예: google1a2b3c4d5e6f.html

# 운영 기관 CI. assets/ci.svg|png|jpg 중 먼저 발견되는 파일을 빌드 시점에 data URI 로
# 인라인한다(사이트가 index.html 한 장으로 자족하는 구조라 외부 파일을 두지 않는다).
# 파일이 없으면 로고 없이 기관명만 나가므로 빌드가 깨지지 않는다.
_CI_MIME = {".svg": "image/svg+xml", ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg"}


def _ci_markup() -> str:
    import base64
    for name in ("ci.svg", "ci.png", "ci.jpg", "ci.jpeg"):
        p = Path(__file__).resolve().parent / "assets" / name
        if not p.is_file():
            continue
        src = "data:%s;base64,%s" % (_CI_MIME[p.suffix.lower()],
                                     base64.b64encode(p.read_bytes()).decode())
        return '<img class="ci" src="%s" alt="%s" width="150">' % (src, _esc(SITE_ORG))
    return ""


def _squash(s: str) -> str:
    return re.sub(r"[^0-9a-z가-힣]+", "", (s or "").lower())


def _echoes_title(summary: str, title: str) -> bool:
    """요약이 제목을 사실상 되풀이하는가(구글 뉴스 description 이 흔히 그렇다)."""
    st, tt = _squash(summary), _squash(title)
    return bool(tt) and (st.startswith(tt) or tt.startswith(st))


def _news_feed(news_days: dict[str, dict]) -> dict:
    items = []
    per_day: dict[str, int] = {}
    for date in sorted(news_days):
        arts = news_days[date].get("articles", [])
        per_day[date] = len(arts)
        mock = bool(news_days[date].get("mock"))
        for a in arts:
            # 페이로드 절약: RSS 요약은 제목을 그대로 되풀이하는 경우가 많다 →
            # 제목과 실질적으로 같으면 싣지 않는다(카드에서 어차피 중복 표시).
            title = a.get("title", "")
            summary = (a.get("summary") or "").strip()
            it = {
                "title": title, "url": a.get("url", ""),
                "source": a.get("source", ""), "published": a.get("published"),
                "category": a.get("category", "etc"), "date": date,
            }
            if summary and not _echoes_title(summary, title):
                it["summary"] = summary
            if mock:
                it["mock"] = True
            items.append(it)
    return {
        "categories": [{"key": c["key"], "emoji": c["emoji"], "name": c["name"]}
                       for c in ncfg.CATEGORIES],
        "perDay": [{"x": d, "y": per_day[d]} for d in sorted(per_day)],
        "items": items,
    }


TOTAL_FIX_MIN = 10      # 이보다 표본이 적으면 구성비가 튀어 총계를 손대지 않는다
TOTAL_FIX_SHARE = 0.10  # 이 비율 이상 섞였을 때만 (몇 건은 표본 오차와 구분되지 않는다)


def _cat_of(p: dict) -> str:
    """그리는 시점에 분야를 다시 정한다.

    아카이브는 누적이라, 저장된 category 는 **그때의 분야 체계**로 정해진 값이다.
    분야를 Y04S 로 갈아엎으면 옛 항목의 'renew'·'grid' 는 어느 분야에도 없어
    화면에서 통째로 사라진다. 저장 파일을 고치는 대신 그릴 때 다시 나눈다 —
    실체참조·공동출원인·출원인 국적 때와 같은 자리다(되돌리기 쉽고 손실이 없다).

    코드가 아무 데도 안 걸리면, **지금 체계에 있는 키일 때만** 저장된 분야를 쓴다.
    그 항목은 그 분야의 IPC 로 조회해서 들어온 것이라 근거가 있다(수집기도 같은
    규칙으로 fallback 한다). 지금 체계에 없는 옛 키('renew'·'grid'…)면 빈 값이다 —
    옛 이름을 화면에 흘려보내느니 세지 않는 편이 정직하다.
    """
    got = pcfg.classify(p.get("cpc") or [], "")
    if got:
        return got
    old = p.get("category") or ""
    return old if old in pcfg.CATEGORY_BY_KEY else ""


def _patent_feed(patent_weeks: dict[str, dict], stats: dict | None = None) -> dict:
    items = []
    per_week: dict[str, int] = {}
    # 넓은 검색어가 자회사까지 쓸어 온 정도를 센다: 그 이름으로 조회해 담긴 문서 수(q_all)
    # 대비 실제로 그 회사 것이었던 수(q_own). OPS 총계는 이 비율만큼 부풀려져 있다.
    q_all: dict[str, int] = {}
    q_own: dict[str, int] = {}
    origins: dict[str, str] = (stats or {}).get("origins") or {}
    for wk in sorted(patent_weeks):
        pats = patent_weeks[wk].get("patents", [])
        per_week[wk] = len(pats)
        week_mock = bool(patent_weeks[wk].get("mock"))
        for p in pats:
            # 출원인은 수집 시 명시(우리가 조회한 주체) → 그 값을 우선 사용.
            # 옛 데이터(applicant 없음)는 이름 정규화로 대체(하위호환).
            # 실체참조는 별칭을 맞추기 **전에** 푼다 — 'XI&apos;AN' 상태로는
            # 별칭·정규화가 어긋나고, 화면에도 그대로 새어 나간다.
            ap = _lead(_plain(p.get("applicant") or ""))
            aname = ap or _canon_assignee(_plain(p.get("assignee", "")))
            region = p.get("country", "") if ap else \
                _assignee_country(_canon_assignee(p.get("assignee", "")), p.get("assignee", ""))
            # 서지상세로 따로 확인해 둔 국적(stats.origins)이 있으면 그리기 직전에
            # 붙인다 — 저장된 항목을 고치지 않으므로 옛 자료도 한 번에 맞아진다.
            # 비어 있을 때만 채우는 게 아니라 **덮어쓴다**. 두 경로가 서로 다른
            # 종류의 오류를 고치기 때문이다:
            #   해외 공보 — 응답에 국적 칸이 없어 그냥 비어 있다(채우기)
            #   국내 공보 — 수집기가 별칭표에 없는 출원인을 전부 KR 로 적었다(고치기).
            #     한국에 공개한 CATL·BYD·테슬라가 국내 기업으로 잡혀 있었고,
            #     '비면 채운다' 규칙으로는 이미 KR 이 적혀 있어 영영 안 고쳐진다.
            # origins 는 서지상세에서 읽은 값이고 region 은 수집기의 기본값이라,
            # 둘이 다르면 origins 쪽이 근거가 있다.
            if aname:
                cc = origins.get(aname) or ""
                if cc:
                    region = pcfg.region_of(cc)
                    p = dict(p, flag=pcfg.flag_of(cc))
            flag = p.get("flag") or pcfg.flag_of(region)
            # 페이로드 절약: 원문 assignee 는 정규화명(aName)과 다를 때만, snippet 은
            # 있을 때만 담는다. cpc 는 카드에 분류 근거로 보여주므로 상위 3개만.
            raw = _plain(p.get("assignee", ""))
            fix = _reassign(raw, aname)     # 자회사 문서를 모회사 밑에 두지 않는다
            if ap:
                q_all[ap] = q_all.get(ap, 0) + 1
                if not fix:
                    q_own[ap] = q_own.get(ap, 0) + 1
            if fix:
                aname, region, flag = fix
            # 바깥 창구로 넘길 표준 문헌번호. 화면에 보이는 번호는 저장된 표기를
            # 그대로 두고(국내는 13자리가 관행), 링크만 표준형으로 보낸다.
            std = _docno(p)
            num = p.get("number", "")
            # 일본 공보는 자국번호와 국제표기가 파이프로 이어져 저장돼 있다
            # (JP38528513|2026528513). 읽는 사람에게 파이프는 아무 뜻이 없으므로
            # 화면에도 표준 표기를 보인다 — 다른 나라는 저장된 표기가 곧 관행이다.
            if "|" in num and std:
                num = std
            it = {
                "title": _plain(p.get("title", "")),
                "url": ("https://patents.google.com/?q="
                        + urllib.parse.quote(std) + "&hl=ko") if std
                       else p.get("url", ""),
                "number": num,
                "aName": aname, "aCountry": region, "aFlag": flag,
                "office": p.get("office", ""),
                "pub_date": p.get("pub_date"),
                "category": _cat_of(p), "country": region,
                "week": wk,
            }
            if std and std != num:          # 같으면 싣지 않는다(페이로드 절약)
                it["nq"] = std
            if raw and raw != aname:
                it["assignee"] = raw
            if p.get("snippet"):
                it["summary"] = p["snippet"]
            if p.get("cpc"):
                it["cpc"] = p["cpc"][:3]
            # 하위 갈래는 **여기서** 정한다. 화면에 싣는 cpc 는 앞 세 개로 자른
            # 것이라, 브라우저가 다시 계산하면 네 번째 이후 코드로 갈리는 건을
            # 놓친다. 코드가 하나도 없으면 붙이지 않는다(없는 것을 지어내지 않게).
            sub = pcfg.subgroup_of(p.get("cpc") or [], it["category"]) \
                if p.get("cpc") else ""
            if sub:
                it["sub"] = sub
            # 항목별 mock 우선(없으면 주 단위 — 옛 데이터 하위호환)
            if p.get("mock", week_mock):
                it["mock"] = True
            items.append(it)
    # 출원인별 '실제 전체 건수'(OPS @total-result-count). 저장 목록은 상한까지의
    # 표본이지만 이 값은 상한과 무관해, 랭킹·규모 비교를 왜곡 없이 할 수 있다.
    # 지금은 stats.json(주별 버킷과 분리해 매일 누적)이 정본이고, 주별 버킷 읽기는
    # 예전 데이터 하위호환용이다 → 먼저 주별을 깔고 stats 로 덮는다.
    totals: dict[str, int] = {}
    offices: dict[str, dict] = {}
    for wk in sorted(patent_weeks):                 # 최신 주 값이 이기도록 오름차순
        for k, v in (patent_weeks[wk].get("totals") or {}).items():
            totals[k] = v
        for k, v in (patent_weeks[wk].get("offices") or {}).items():
            offices[k] = v
    totals.update((stats or {}).get("totals") or {})
    for k, v in ((stats or {}).get("offices") or {}).items():
        offices[k] = v

    # 총계 보정. 총계는 출원인별 독립 질의라 공개번호 dedup 이 듣지 않는다 →
    # 넓은 검색어는 자회사 문서까지 세어 규모를 부풀린다. 검색어를 모회사 법인명으로
    # 좁혀 봤지만 OPS 총계는 183 그대로였다(8/5 실측) — 이름으로는 갈라지지 않는다.
    # 대신 표본이 알려 준 구성비로 총계를 깎는다. Siemens 표본 54건 중 제 것은
    # 18건이었으므로 183 × 18/54 ≈ 61. (Energy 53 · Gamesa 75 를 뺀 55 와도 가깝다.)
    # 표본이 너무 작으면 비율이 튀므로 일정 수 이상일 때만 손댄다.
    adjusted: dict[str, dict] = {}
    for name, n_all in q_all.items():
        n_own = q_own.get(name, 0)
        if n_all < TOTAL_FIX_MIN or name not in totals:
            continue
        # 몇 건 섞인 정도는 손대지 않는다 — 표본 오차와 구분이 안 된다.
        if (n_all - n_own) / n_all < TOTAL_FIX_SHARE:
            continue
        raw_total = totals[name]
        totals[name] = max(n_own, round(raw_total * n_own / n_all))
        adjusted[name] = {"raw": raw_total, "kept": n_own, "of": n_all}

    # 국적을 '아직 못 채운 곳' 과 '원본에 없어 못 채우는 곳' 을 나눠 센다.
    # 실측(2026-08-25): 출원인 국적은 **미국·유럽 공보에만** 실려 있다. 중국 공보
    # 응답 전문을 받아 보니 태그는 있는데 값이 빈칸이었다(주소도 발명자 국적도
    # 마찬가지). 이 수를 세어 두지 않으면 화면이 '매일 채우는 중, N곳 남음' 이라고
    # 말하게 되는데, 그중 상당수는 영영 채워지지 않는다 — 거짓말이 된다.
    per_applicant: dict[str, set] = {}
    for it in items:
        if it.get("aCountry") or not it.get("aName"):
            continue
        per_applicant.setdefault(it["aName"], set()).add(it.get("office") or "")
    origins_blocked = sum(1 for o in per_applicant.values()
                          if not (o & {"US", "EP"}))

    # 실제 '공개일' 범위 — 아카이브에 담긴 특허가 어느 기간 공개분인지 알려준다.
    # (주별 버킷 키는 '수집한 주'라서 공개 기간과 다르다 → 혼동 방지용으로 따로 계산)
    pubs = sorted(p["pub_date"] for p in items if p.get("pub_date"))
    return {
        # en(공식 영문 표제)·outside(Y04S 밖) 를 함께 싣는다. 분야 이름은 우리가
        # 옮긴 말이라, 표준 표제를 곁에 두지 않으면 옮긴 말이 표준인 것처럼 읽힌다.
        "categories": [{"key": c["key"], "emoji": c["emoji"], "name": c["name"],
                        "en": c.get("en", ""), "cpc": (c.get("cpc") or [""])[0],
                        **({"outside": True} if c.get("outside") else {})}
                       for c in pcfg.CATEGORIES],
        # 분야 안의 하위 갈래(지금은 Y04S 10/00 만). 순서가 CPC 표의 순서가 아니라
        # 우선순위 순서라, 화면은 이 목록이 아니라 건수로 정렬한다.
        "subgroups": {k: [{"code": s["code"], "name": s["name"], "en": s["en"],
                           **({"note": s["note"]} if s.get("note") else {})}
                          for s in v] for k, v in pcfg.SUBGROUPS.items()},
        "countries": [{"code": k, "emoji": v[0], "name": v[1]}
                      for k, v in pcfg.COUNTRY_LABEL.items()],
        "perWeek": [{"x": w, "y": per_week[w]} for w in sorted(per_week)],
        "pubRange": {"from": pubs[0], "to": pubs[-1]} if pubs else None,
        "totals": totals,
        "totalsAdjusted": adjusted,   # 표본 구성비로 깎은 총계(화면에서 근거를 밝힌다)
        "officeCounts": offices,
        # 서지상세로 국적을 채운 출원인 수. 화면에서 '몇 곳까지 확인됐는지'를
        # 밝히는 데 쓴다 — 며칠에 걸쳐 채우는 동안 진행 상황이 보여야 한다.
        "originsFilled": len(origins),
        # 중국·일본 공보에만 나오는 출원인 — 원본에 국적 칸이 비어 있어 못 채운다.
        "originsBlocked": origins_blocked,
        "offices": [{"code": o["code"], "emoji": o["emoji"], "name": o["name"]}
                    for o in pcfg.OFFICES],
        # 카드·안내문의 "최근 N일" 문구가 설정과 어긋나지 않게 값으로 넘긴다.
        "lookbackDays": pcfg.LOOKBACK_DAYS,
        "krLimit": pcfg.KR_LIMIT,
        "perApplicantLimit": pcfg.PER_APPLICANT_LIMIT,   # 표본 상한(랭킹의 '이상' 표기용)
        "applicants": len(pcfg.APPLICANTS),
        # 카드마다 붙는 조회 창구(Espacenet·Google Patents 등). 공개번호만 있으면
        # 되므로 항목별로 URL 을 저장하지 않고 템플릿만 한 번 내려보낸다.
        "links": ip_guide.links(),
        "items": items,
    }


def _split_items(sec: dict, keep: int, name: str, site_dir: Path) -> int:
    """items 를 '최근 keep 건'(인라인)과 나머지(별도 파일)로 가른다. 반환: 뒷부분 건수.

    최신순으로 갈라야 첫 화면이 최근 것으로 채워진다. 정렬 키는 항목마다 다르므로
    (뉴스=published/date, 특허=pub_date/week) 있는 것을 순서대로 쓴다.

    집계는 이미 **가르기 전 전체**로 계산돼 sec 안에 들어 있다 → 여기서 손대지
    않는다. 여기서 나누는 것은 목록뿐이다.
    """
    items = sec.get("items") or []
    sec["total"] = len(items)          # 화면이 '전체 몇 건인지'는 늘 알아야 한다
    if INLINE_ALL or len(items) <= keep:
        return 0

    def when(it):
        return (it.get("published") or it.get("pub_date")
                or it.get("date") or it.get("week") or "")

    ordered = sorted(items, key=when, reverse=True)
    head, tail = ordered[:keep], ordered[keep:]
    sec["items"] = head
    d = site_dir / FEED_SUBDIR
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{name}-rest.json").write_text(
        json.dumps(tail, ensure_ascii=False, separators=(",", ":")),
        encoding="utf-8")
    sec["rest"] = {"url": f"{FEED_SUBDIR}/{name}-rest.json", "count": len(tail)}
    return len(tail)


def render_all(site_dir: Path, news_days: dict[str, dict],
               patent_weeks: dict[str, dict], generated: str,
               briefs: list[dict] | None = None,
               stats: dict | None = None,
               pbriefs: list[dict] | None = None,
               staown: dict | None = None) -> Path:
    site_dir = Path(site_dir)
    site_dir.mkdir(parents=True, exist_ok=True)

    briefs = briefs or []
    pbriefs = pbriefs or []
    feed = {
        "generated": generated,
        "title": SITE_TITLE, "tagline": SITE_TAGLINE, "org": SITE_ORG,
        "orgUrl": SITE_ORG_URL, "club": SITE_CLUB,
        "brief": briefs[0] if briefs else None,   # 최신(홈 상단)
        "briefs": briefs,                          # 최신순 전체(타임라인)
        "patentBrief": pbriefs[0] if pbriefs else None,   # 최신(특허 탭 상단)
        "patentBriefs": pbriefs,                           # 최신순 전체
        "insights": _insights.build(news_days, patent_weeks),
        "news": _news_feed(news_days),
        "patents": _patent_feed(patent_weeks, stats),
        # 거래·지원 탭(수집과 무관한 사람 관리 상수 → ip_guide.py 에서만 고친다)
        "guide": ip_guide.guide(),        # 주소가 확인된 항목만(뼈대는 코드에만 남는다)
        "guideNote": ip_guide.NOTE,
        "staown": staown or None,
        "staownNote": ip_guide.STAOWN_NOTE,
        # newsMap 이지 news 가 아니다 — 이 dict 에는 이미 "news"(READ_NEWS,
        # 해석 문구)가 있어서 처음에 "news" 로 넣었더니 뒤에 오는 쪽이 이기며
        # 통째로 덮였다. 화면에서는 짝이 하나도 안 잡혀 여섯 분야가 전부
        # '뉴스 짝 없음' 으로 나왔다(오류는 안 났다).
        "trade": {"map": ip_guide.FIELD_MAP, "newsMap": ip_guide.FIELD_NEWS,
                  "unpaired": ip_guide.UNPAIRED,
                  "conc": ip_guide.READ_CONC, "news": ip_guide.READ_NEWS,
                  "kr": ip_guide.READ_KR, "note": ip_guide.TRADE_NOTE,
                  "quadLead": ip_guide.QUAD_GUIDE_LEAD,
                  "quadGuide": ip_guide.QUAD_GUIDE,
                  "ipcNames": ip_guide.IPC_NAMES,
                  "subsLead": ip_guide.SUBS_LEAD,
                  "subsNote": ip_guide.SUBS_NOTE,
                  "concShort": ip_guide.READ_SHORT,
                  "newsShort": ip_guide.NEWS_SHORT,
                  "gen": ip_guide.READ_GEN, "genKr": ip_guide.READ_GEN_KR,
                  "genNews": ip_guide.READ_GEN_NEWS,
                  "genDom": ip_guide.READ_GEN_DOM},
    }
    # 집계가 다 들어간 뒤에 목록만 가른다 — 순서가 바뀌면 집계가 부분집합으로
    # 계산돼 화면의 모든 수가 조용히 작아진다.
    rest_n = (_split_items(feed["news"], INLINE_NEWS, "news", site_dir)
              + _split_items(feed["patents"], INLINE_PATENTS, "patents", site_dir))
    if rest_n:
        print(f"       지연 로딩: 최근 {len(feed['news']['items'])}+"
              f"{len(feed['patents']['items'])}건 인라인 · 나머지 {rest_n:,}건은 "
              f"{FEED_SUBDIR}/ 에서 받아 붙임")

    payload = json.dumps(feed, ensure_ascii=False).replace("</", "<\\/")

    html = _PAGE.replace("__TITLE__", _esc(SITE_TITLE)) \
               .replace("__TAGLINE__", _esc(SITE_TAGLINE)) \
               .replace("__ORG__", _esc(SITE_ORG)) \
               .replace("__DEPT__", _esc(SITE_DEPT)) \
               .replace("__ORGURL__", _esc(SITE_ORG_URL)) \
               .replace("__CLUB__", _esc(SITE_CLUB)) \
               .replace("__CI__", _ci_markup()) \
               .replace("__GSV__", _esc(GSV_TOKEN)) \
               .replace("__SITEURL__", _esc(SITE_URL)) \
               .replace("__FAVICON__", _favicon.data_uri()) \
               .replace("__CSS__", _CSS) \
               .replace("__JS__", _JS) \
               .replace("__FEED__", payload)
    if not GSV_TOKEN:
        html = re.sub(r'\n<meta name="google-site-verification"[^>]*>', "", html)
    (site_dir / "index.html").write_text(html, encoding="utf-8")
    _favicon.write(site_dir)

    # 검색엔진용 최소 파일 두 개. 사이트가 index.html 한 장이라 sitemap 도 한 줄이다.
    # robots.txt 가 없으면 크롤러가 사이트맵 위치를 알 방법이 없다.
    # HTML 파일 방식 소유 확인. 파일명이 곧 내용이라 형식이 단순하다.
    if GSV_FILE:
        name = GSV_FILE if GSV_FILE.endswith(".html") else GSV_FILE + ".html"
        (site_dir / name).write_text(
            f"google-site-verification: {name}\n", encoding="utf-8")

    (site_dir / "robots.txt").write_text(
        "User-agent: *\nAllow: /\nSitemap: " + SITE_URL.rstrip("/") + "/sitemap.xml\n",
        encoding="utf-8")
    (site_dir / "sitemap.xml").write_text(
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n'
        f"  <url><loc>{_esc(SITE_URL)}</loc>"
        f"<lastmod>{generated[:10]}</lastmod>"
        "<changefreq>daily</changefreq></url>\n"
        "</urlset>\n", encoding="utf-8")

    # 이전 구조(patents.html)로 들어오는 링크 호환 → 앱의 특허 탭으로 이동
    (site_dir / "patents.html").write_text(_REDIRECT, encoding="utf-8")
    return site_dir / "index.html"


def _esc(s: str) -> str:
    import html as _h
    return _h.escape(s or "", quote=True)


_REDIRECT = ('<!doctype html><meta charset="utf-8">'
             '<meta http-equiv="refresh" content="0; url=index.html#tab=patents">'
             '<link rel="canonical" href="index.html#tab=patents">'
             '<script>location.replace("index.html#tab=patents")</script>'
             '<p>특허 탭으로 이동합니다… <a href="index.html#tab=patents">여기</a></p>')


_CSS = """
:root{
  --bg:#F4F5F3; --card:#FFFFFF; --ink:#16181C; --muted:#6A6E76;
  --line:#E2E4E0; --accent:#E8A33D; --accent2:#3A6FB0; --chipbg:#FFFFFF;
  --shadow:0 1px 2px rgba(0,0,0,.05); --spark:#E8A33D;
}
@media (prefers-color-scheme:dark){
  :root{
    --bg:#0F1114; --card:#181B20; --ink:#E8EAED; --muted:#9AA0A8;
    --line:#262A31; --accent:#F0B65A; --accent2:#6FA0DC; --chipbg:#1E2127;
    --shadow:0 1px 2px rgba(0,0,0,.3); --spark:#F0B65A;
  }
}
*{box-sizing:border-box}
[hidden]{display:none!important}
body{margin:0;background:var(--bg);color:var(--ink);
  font-family:-apple-system,BlinkMacSystemFont,"Segoe UI","Noto Sans KR",sans-serif;
  line-height:1.55;-webkit-font-smoothing:antialiased}
a{color:inherit}
.mono{font-variant-numeric:tabular-nums;font-family:ui-monospace,"SFMono-Regular",Menlo,monospace}
.wrap{max-width:1600px;margin:0 auto;padding:22px 32px 72px}
/* 상세(뉴스·특허) 목록은 가독성을 위해 읽기 폭을 가운데 정렬로 제한. 홈/통계는 전체 폭. */
.readcol{max-width:1120px}
.mast{border-bottom:3px solid var(--ink);padding-bottom:14px;margin-bottom:0;
  display:flex;align-items:flex-end;justify-content:space-between;gap:18px;flex-wrap:wrap}
.mast .masttext{min-width:0}
/* 운영 기관: 서비스명과 경쟁하지 않게 오른쪽 끝에 작게 */
.mast .org{display:flex;flex-direction:column;align-items:flex-end;gap:5px;flex-shrink:0;
  text-decoration:none;color:inherit}
.mast .org:hover .orgname{color:var(--accent2)}
.mast .org:focus-visible{outline:2px solid var(--accent);outline-offset:3px;border-radius:6px}
.mast .org .ci{height:38px;width:auto;max-width:220px;display:block}
.mast .org .orgname{color:var(--muted);font-size:11.5px;font-weight:600;white-space:nowrap}
.mast .org .orgclub{color:var(--accent2);font-size:11px;font-weight:700;white-space:nowrap}
.mast .org:hover .orgclub{text-decoration:underline}
/* CI 는 흰 바탕에 쓰도록 만들어진 원색 로고다. 어두운 배경에 그대로 얹지 않고
   흰 판을 깔아 규정대로 흰 바탕 위에 놓이게 한다(라이트 모드 배경은 이미 거의 흰색). */
@media (prefers-color-scheme:dark){
  .mast .org .ci{background:#fff;padding:5px 8px;border-radius:6px;height:48px}
}
@media (max-width:640px){ .mast .org{align-items:flex-start} .mast .org .orgname{white-space:normal} }
.mast h1{font-size:24px;font-weight:800;letter-spacing:-.02em;margin:0 0 3px}
/* 제목 자체가 홈으로 가는 버튼. 글자처럼 보이게 버튼 기본 스타일을 지운다 */
.mast h1 .brand{font:inherit;color:inherit;background:none;border:0;padding:0;cursor:pointer;
  display:flex;gap:9px;align-items:center;text-align:left}
.mast h1 .brand:hover{color:var(--accent2)}
.mast h1 .brand:focus-visible{outline:2px solid var(--accent);outline-offset:3px;border-radius:4px}
.mast h1 .bolt{color:var(--accent)}
.mast .tag{color:var(--muted);font-size:13px;margin:0}
.tabs{display:flex;gap:6px;margin:0 0 16px;border-bottom:1px solid var(--line)}
.tabs button{padding:11px 18px;font:inherit;font-weight:700;font-size:14.5px;color:var(--muted);
  background:none;border:0;border-bottom:3px solid transparent;margin-bottom:-1px;cursor:pointer}
.tabs button[aria-selected="true"]{color:var(--ink);border-bottom-color:var(--accent)}
.tabs button:hover{color:var(--ink)}
/* 좁은 화면에서 탭이 넷이면 기본 여백으로는 글자가 두 줄로 접힌다 → 여백만 줄여
   한 줄을 유지한다(줄바꿈 금지는 걸지 않는다. 글자가 커지면 접히는 게 낫다) */
@media (max-width:560px){ .tabs{gap:2px} .tabs button{padding:11px 10px;font-size:13.5px} }
/* 개요 */
.overview{display:grid;grid-template-columns:repeat(4,minmax(0,1fr)) 1.6fr;gap:12px;margin:2px 0 18px}
.tile{background:var(--card);border:1px solid var(--line);border-radius:9px;padding:11px 13px;box-shadow:var(--shadow)}
.tile .k{font-size:10.5px;letter-spacing:.05em;text-transform:uppercase;color:var(--muted)}
.tile .v{font-size:22px;font-weight:800;margin-top:3px}
.tile .v small{font-size:12px;font-weight:600;color:var(--muted)}
.tile.spark{grid-column:span 1}
.sparkwrap{display:flex;flex-direction:column;justify-content:space-between}
.sparkwrap svg{width:100%;height:38px;display:block;margin-top:4px}
.sparkwrap .k b{color:var(--accent)}
/* 서술형 브리핑 */
.brief{background:var(--card);border:1px solid var(--line);border-left:4px solid var(--accent);
  border-radius:11px;padding:16px 18px 14px;box-shadow:var(--shadow);margin:2px 0 14px}
.brief .bhead{display:flex;align-items:baseline;gap:9px;flex-wrap:wrap;margin:0 0 3px}
.brief .btag{font-size:10.5px;font-weight:800;letter-spacing:.03em;color:var(--accent);
  border:1px solid var(--accent);border-radius:999px;padding:2px 8px;white-space:nowrap}
.brief .bdate{color:var(--muted);font-size:11.5px;font-variant-numeric:tabular-nums}
.brief .bstale{color:#b06a1d;font-size:11px;font-weight:700}
@media (prefers-color-scheme:dark){ .brief .bstale{color:var(--accent)} }
.brief h2{font-size:17px;font-weight:800;letter-spacing:-.01em;line-height:1.4;margin:2px 0 9px}
.brief .bbody{font-size:13.5px;line-height:1.75;color:var(--ink);margin:0}
.brief .bbody p{margin:0 0 7px}
.brief .bbody p:last-child{margin-bottom:0}
.brief .bpoints{display:grid;grid-template-columns:repeat(3,1fr);gap:9px;margin:12px 0 2px}
.brief .pt{background:var(--bg);border:1px solid var(--line);border-radius:9px;padding:9px 11px}
.brief .pt .pl{font-size:12px;font-weight:800;display:flex;align-items:center;gap:5px;margin-bottom:3px}
.brief .pt .px{font-size:11.5px;color:var(--muted);line-height:1.5}
.brief .bfoot{color:var(--muted);font-size:11px;margin-top:11px;padding-top:9px;border-top:1px solid var(--line);
  display:flex;gap:8px;flex-wrap:wrap;align-items:center}
.brief .bfoot .sep{opacity:.5}
.brief .btoggle{margin-left:auto;background:none;border:0;color:var(--accent2);font:inherit;font-size:11px;cursor:pointer}
.brief.collapsed .bbody,.brief.collapsed .bpoints{display:none}
@media (max-width:820px){ .brief .bpoints{grid-template-columns:1fr} }
/* 특허 카드 하단의 조회 창구 링크. 제목 링크보다 약하게 보여 시선을 뺏지 않게 한다 */
.card .xlinks{display:flex;gap:6px;flex-wrap:wrap;margin-top:9px}
.card .xl{font-size:11px;font-weight:700;color:var(--muted);text-decoration:none;
  border:1px solid var(--line);border-radius:999px;padding:3px 9px;background:var(--bg);white-space:nowrap}
.card .xl:hover{color:var(--accent2);border-color:var(--accent2)}
/* 분야 지도(뉴스 비중 변화 × 권리 집중도). 색은 집중도 3단계의 단일 색상 램프다 —
   집중을 붉게, 분산을 푸르게 칠하면 기관이 분야에 좋고 나쁨을 매기는 것처럼 읽힌다.
   같은 파랑의 밝기 차이로만 정도를 보이고, 뜻은 글자(집중/중간/분산)가 진다.
   (밝은 화면은 진할수록 집중, 어두운 화면은 밝을수록 집중 — 표면 대비 기준.) */
:root{ --q1:#8FB2D6; --q2:#3A6FB0; --q3:#1D4470; }
@media (prefers-color-scheme:dark){ :root{ --q1:#33639C; --q2:#659ACF; --q3:#AFCFEE; } }
.qwrap{overflow-x:auto}
.qchart{display:block;width:100%;min-width:480px;height:auto}
.qchart .ax{stroke:var(--line);stroke-width:1}
.qchart .qg{stroke:var(--line);stroke-width:1;stroke-dasharray:2 5;opacity:.55}
.qchart .qa.qm{fill:var(--ink);opacity:.75}
.qchart .qoff{fill:none;stroke:var(--muted);stroke-width:1.2;stroke-dasharray:3 3;opacity:.7}
.qchart .qz{fill:var(--muted);font-size:9.5px;font-weight:700;opacity:.65}
.qchart .qa{fill:var(--muted);font-size:10px;font-weight:700}
.qchart .ql{fill:var(--ink);font-size:10.5px;font-weight:700}
.qchart .dot{stroke:var(--card);stroke-width:2}
.qchart .hit{fill:transparent;cursor:default}
.qlegend{display:flex;gap:14px;flex-wrap:wrap;align-items:center;margin:8px 2px 0;
  font-size:11px;font-weight:700;color:var(--muted)}
.qlegend i{display:inline-block;width:10px;height:10px;border-radius:50%;margin-right:5px;vertical-align:-1px}
.qlegend .s1 i{background:var(--q1)} .qlegend .s2 i{background:var(--q2)} .qlegend .s3 i{background:var(--q3)}
.qlegend .soff i{background:none;border:1.2px dashed var(--muted)}
/* 읽는 법. 그림과 표 사이에 끼므로 배경 없이 왼쪽 선 하나로만 묶는다 — 카드가
   하나 더 생기면 표(진짜 내용)와 위계가 헷갈린다. */
.qguide{margin:10px 2px 0;border-left:2px solid var(--line);padding:2px 0 2px 12px}
.qguide>summary{font-size:12px;font-weight:800;color:var(--muted);cursor:pointer;
  list-style:none;display:flex;align-items:center;gap:6px}
.qguide>summary::-webkit-details-marker{display:none}
.qguide>summary::before{content:'▾';font-size:10px;transition:transform .15s}
.qguide:not([open])>summary::before{transform:rotate(-90deg)}
.qguide .qgl{margin:8px 0 0;font-size:12px;font-weight:700;color:var(--ink)}
.qguide dl{margin:6px 0 0;display:grid;grid-template-columns:auto 1fr;gap:5px 12px;
  font-size:12px;line-height:1.6}
.qguide dt{font-weight:800;color:var(--ink);white-space:nowrap}
.qguide dd{margin:0;color:var(--muted);font-weight:600;word-break:keep-all}
@media (max-width:560px){
  .qguide dl{grid-template-columns:1fr;gap:2px}
  .qguide dd{margin:0 0 6px}
}
/* 분야별 상세 = 위 그림의 표 대응물(값을 그림에만 두지 않는다).
   ④ 왼쪽 색 띠로 집중도 단계를 표시해 스크롤 스캔이 되게 한다 */
.trow{border:1px solid var(--line);border-left:3px solid var(--line);border-radius:11px;
  background:var(--card);padding:12px 14px;margin-top:10px}
/* 홈에서 눌러 온 카드를 잠깐 표시한다. 표시가 없으면 탭이 바뀌고 스크롤만
   움직여서 '내가 고른 것이 어느 것인지' 를 놓친다. */
.trow.hit{box-shadow:0 0 0 2px var(--accent2)}
.trow.lv-hi{border-left-color:var(--q3)} .trow.lv-mid{border-left-color:var(--q2)}
.trow.lv-lo{border-left-color:var(--q1)}
.trow .th{display:flex;align-items:center;gap:8px;flex-wrap:wrap;font-size:14px;font-weight:800}
.trow .tcpc{font-size:10.5px;font-weight:700;color:var(--muted);cursor:help;
  border:1px solid var(--line);border-radius:5px;padding:1px 5px;margin-left:7px;
  letter-spacing:.02em;white-space:nowrap}
.trow .tout{font-size:10px;font-weight:800;color:var(--accent);cursor:help;
  border:1px solid var(--accent);border-radius:999px;padding:1px 7px;margin-left:6px;
  white-space:nowrap}
.trow .thp{font-size:13px;color:var(--muted);font-variant-numeric:tabular-nums}
.trow .tb{margin-left:auto;display:flex;gap:6px;flex-wrap:wrap;font-size:11px;font-weight:700}
.trow .tb span{border:1px solid var(--line);border-radius:999px;padding:2px 8px;color:var(--muted);
  font-variant-numeric:tabular-nums;display:inline-flex;align-items:center;gap:6px}
/* 최근 14일 비중 추이. 값은 배지 숫자가 지고, 선은 오르내림만 보인다 */
.spk{width:64px;height:18px;flex:none;overflow:visible}
.spk path{fill:none;stroke:var(--accent2);stroke-width:2;stroke-linejoin:round;stroke-linecap:round}
.spk circle{fill:var(--accent2)}
.trow .tr{margin:8px 0 0;font-size:12px;line-height:1.6;color:var(--muted);font-weight:700;
  word-break:keep-all;cursor:help}
.trow .tcaveat{margin:7px 0 0;font-size:11px;line-height:1.6;color:var(--muted);opacity:.85}
/* ① 지분 막대 — 칸 사이는 테두리가 아니라 배경색 틈(2px)으로 가른다 */
.shbar{display:flex;gap:2px;height:9px;margin:10px 0 6px;border-radius:999px;overflow:hidden}
.shbar i{display:block;height:100%}
.shbar i:first-child{border-radius:999px 0 0 999px}
.shbar i:last-child{border-radius:0 999px 999px 0}
.shbar .rest{background:var(--line)}
.shns{display:flex;gap:10px;flex-wrap:wrap;font-size:11.5px;font-weight:700;color:var(--ink)}
.shns .shn b{color:var(--accent2);margin-left:4px;font-variant-numeric:tabular-nums}
.shns .shn.rest{color:var(--muted)} .shns .shn.rest b{color:var(--muted)}
/* 세부 기술 쏠림 — 지분 막대와 같은 부품을 쓰되, 이 패널에서는 막대가 주인공이라
   조금 두껍게 하고 이름표에 색 점을 붙인다(색이 줄마다 다른 기술을 가리키므로
   패널 하나짜리 범례로는 묶이지 않는다). */
/* 카드 안의 토막 이름. 붙이지 않았을 때는 막대 두 개가 잇달아 나와 둘 다 '지분'
   처럼 보였다 — 하나는 출원인 지분이고 하나는 기술 갈래 비중이다. */
.blk{display:flex;align-items:baseline;gap:8px;margin:13px 0 0;
  font-size:11px;font-weight:800;letter-spacing:.03em;color:var(--muted)}
.blk .blkd{margin-left:auto;font-weight:600;letter-spacing:0;
  font-variant-numeric:tabular-nums;white-space:nowrap}
.subrow{margin-top:2px}
/* 좁은 화면에서 분야 이름이 낱말 가운데서 잘렸다('전력수급·수 / 요관리') — 머리글이
   한 줄에 묶여 있어서다. 줄바꿈을 허용하고 건수를 통째로 아랫줄로 내린다. */
.subrow .subh{display:flex;flex-wrap:wrap;align-items:baseline;gap:3px 8px;
  font-size:13px;font-weight:700;word-break:keep-all}
.subrow .subd{margin-left:auto;font-size:11px;font-weight:600;color:var(--muted);
  white-space:nowrap;font-variant-numeric:tabular-nums}
/* 카드 안 두 막대는 **같은 높이**다. 처음에는 기술 막대만 24px 로 키웠는데(칸 안에
   이름을 넣느라), 출원인 막대가 9px 로 남아 기술 쪽이 더 중요해 보였다 — 이 카드의
   머리 물음은 '누가' 쪽이다. 칸 안 글자는 진한 파랑 위 흰색이라 대비가 충분하다. */
.trow .shbar{height:22px;margin:7px 0 6px;border-radius:6px;align-items:stretch}
.trow .shbar i:first-child{border-radius:6px 0 0 6px}
.trow .shbar i:last-child{border-radius:0 6px 6px 0}
.trow .shbar i{cursor:help;display:flex;align-items:center;gap:7px;
  overflow:hidden;white-space:nowrap;padding:0 9px}
.trow .shbar i b{color:#fff;font-size:11.5px;font-weight:700}
.trow .shbar i .sbc{color:#fff;opacity:.6;margin-left:-2px}
.trow .shbar i .fl{flex:none}
.trow .shbar i em{color:#fff;font-style:normal;font-size:11.5px;font-weight:800;
  opacity:.82;font-variant-numeric:tabular-nums}
/* 칸 안 이름과 그 칩은 같은 것을 두 번 말한다 → 넓을 때는 칸 안이, 좁을 때는
   칩이 맡는다. 칸 폭은 퍼센트라 화면이 좁아지면 그대로 줄어드는데 글자는 안 줄어
   잘린다(실측 430px). 서버는 둘 다 내보내고 여기서 하나를 고른다. */
.shns .sbn.inb, .shns .shn.inb{display:none}   /* .shns .sbn 보다 세게 —
   같은 세기면 뒤에 오는 .shns .sbn{display:inline-flex} 가 이겨 둘 다 보였다 */
@media (max-width:700px){
  .trow .shbar i b, .trow .shbar i em,
  .trow .shbar i .sbc, .trow .shbar i .fl{display:none}
  .shns .sbn.inb{display:inline-flex} .shns .shn.inb{display:block}
}
/* 하위 갈래 막대. 위 막대와 달리 칸이 열한 개라 얇은 칸이 여럿 생긴다 →
   칸 사이를 1px 벌려 경계를 보이고, 좁은 칸에서 글자가 넘치지 않게 붙인다.
   ('무엇을 내고 있나' 는 네 칸뿐이라 이 처리가 필요 없다) */
.trow .sgbar i{padding:0 6px;box-shadow:1px 0 0 var(--card) inset}
.trow .sgbar i:last-child{box-shadow:none}
/* 칩이 열한 개면 한 줄에 안 들어간다 → 줄바꿈을 허용하고 간격을 넓힌다.
   .shns 는 원래 세 칩짜리라 줄바꿈을 안 쓴다. */
.shns.sgns{display:flex;flex-wrap:wrap;gap:4px 14px;row-gap:5px}
/* 칩 안에서 줄이 갈리면 안 된다 — 좁은 화면에서 '■ 59%' / '전기 계측' / 'G01R' 이
   세 줄로 흩어져 무엇이 무엇의 값인지 읽히지 않았다(실측 430px). */
.shns .sbn{display:inline-flex;align-items:baseline;gap:5px;color:var(--muted);
  font-weight:600;white-space:nowrap;max-width:100%}
.shns .sbn>i{width:8px;height:8px;border-radius:2px;flex:none;align-self:center}
.shns .sbn .sbc{font-size:10px;font-weight:600;color:var(--muted);opacity:.75}
/* 숫자를 이름보다 먼저 놓는다 — 퍼센트를 훑어 내려가는 것이 이 줄의 쓸모다 */
.shns .sbn b{color:var(--ink);font-weight:800;font-variant-numeric:tabular-nums;
  min-width:2.4em;text-align:right}
.shns .sbn.rest b{color:var(--muted)}
.shns .sbn.rest>i{background:var(--line)}
@media (max-width:560px){ .subrow .subd{margin-left:0;flex-basis:100%} }
/* ③ 국내 권리는 '누가 갖고 있나' 와 성격이 다른 신호 → 모양을 달리한다 */
.tkr{margin:9px 0 0;padding:7px 10px;border-radius:8px;background:var(--bg);
  border:1px solid var(--line);border-left:3px solid var(--accent);
  font-size:11.5px;line-height:1.6;color:var(--muted);word-break:keep-all}
.tkr b{color:var(--ink);font-weight:800}
.tkr .krn{color:var(--muted);font-weight:600;font-size:11px;margin-left:4px;
  font-variant-numeric:tabular-nums}
.tkr .krmore{color:var(--muted);font-weight:700;cursor:help;
  border-bottom:1px dotted var(--muted)}
.trow .nonews{color:var(--muted);opacity:.8}
.golink{font:inherit;font-size:12px;font-weight:700;color:var(--accent2);background:none;
  border:0;padding:0;cursor:pointer;text-decoration:underline}
/* 절 바로가기 — 서브탭 대신. 내용을 숨기지 않고 이동만 빠르게 한다 */
.jump{display:flex;gap:6px;flex-wrap:wrap;margin:0 0 4px}
.jump button{font:inherit;font-size:12px;font-weight:700;color:var(--muted);cursor:pointer;
  background:var(--card);border:1px solid var(--line);border-radius:999px;padding:5px 13px}
.jump button:hover{color:var(--accent2);border-color:var(--accent2)}
/* 국유특허 목록 — 건수가 적어 표 대신 줄 목록으로 */
.stmore{margin-top:6px;font:inherit;font-size:12px;font-weight:700;color:var(--accent2);
  background:none;border:1px dashed var(--line);border-radius:9px;padding:8px;cursor:pointer;width:100%}
.stmore:hover{border-color:var(--accent2)}
.stlist{display:flex;flex-direction:column;gap:2px}
.strow{border:1px solid var(--line);border-radius:9px;background:var(--card);padding:9px 12px}
.strow .stt{display:flex;align-items:baseline;gap:7px;font-size:13.5px;font-weight:700;flex-wrap:wrap}
.strow .stt a{color:inherit;text-decoration:none;word-break:keep-all}
.strow .stt a:hover{color:var(--accent2)}
.strow .stfree,.strow .stpay{font-size:10.5px;font-weight:800;border-radius:999px;
  padding:2px 7px;flex:none;border:1px solid var(--line)}
.strow .stfree{color:#15803d;border-color:#15803d}
.strow .stpay{color:var(--muted)}
@media (prefers-color-scheme:dark){ .strow .stfree{color:#4ade80;border-color:#4ade80} }
.strow .stm{font-size:11.5px;color:var(--muted);margin-top:3px;font-weight:600}
/* 거래·지원 안내 */
#guide .gdesc{color:var(--muted);font-size:12.5px;line-height:1.7;margin:0 2px 10px}
#guide .glist{display:grid;grid-template-columns:repeat(auto-fit,minmax(280px,1fr));gap:12px}
/* 항목이 하나뿐인 묶음에서 카드가 화면 폭 전체로 늘어나지 않게 상한을 둔다
   (2열일 때의 칸 너비와 비슷해 묶음끼리 크기가 들쭉날쭉해 보이지 않는다) */
#guide .gcard{display:block;text-decoration:none;color:inherit;background:var(--card);
  border:1px solid var(--line);border-radius:11px;padding:13px 15px;max-width:640px}
#guide .gcard:hover{border-color:var(--accent2)}
#guide .gname{font-size:14px;font-weight:800;display:flex;align-items:baseline;gap:7px;flex-wrap:wrap}
#guide .gorg{font-size:11px;font-weight:700;color:var(--muted)}
#guide .garr{margin-left:auto;color:var(--accent2);font-size:12px}
#guide .gwhat{margin:6px 0 0;font-size:12.5px;line-height:1.65;color:var(--muted);word-break:keep-all}
#guide .gnote{margin:22px 2px 0;padding-top:12px;border-top:1px solid var(--line);
  color:var(--muted);font-size:11.5px;line-height:1.7;word-break:keep-all}
/* 홈(대시보드) */
.homemode .controls,.homemode .resline,.homemode #overview,.homemode #results,.homemode #scope,
.homemode #more,.homemode #viewToggle{display:none!important}
.home{display:flex;flex-direction:column;gap:16px}
/* 섹션 구분(트렌드 인사이트 / 뉴스 / 특허). 한글이라 대문자 변환은 쓰지 않는다 */
.home .sec{font-size:13px;font-weight:800;color:var(--ink);letter-spacing:-.01em;
  margin:20px 2px 8px;padding-bottom:6px;border-bottom:2px solid var(--line)}
.home .sec:first-child{margin-top:4px}
.homekpi{display:grid;grid-template-columns:repeat(4,1fr);gap:12px}
.homekpi .tile .v{font-size:21px;line-height:1.25}
.homekpi .tile small{display:block;font-size:11.5px;font-weight:600;color:var(--muted);margin-top:3px}
.homekpi .rgs{display:flex;gap:7px;flex-wrap:wrap;font-variant-numeric:tabular-nums}
.homekpi .rgc{font-size:12px;font-weight:700;color:var(--ink);
  display:inline-flex;align-items:center;gap:3px;margin-right:6px}
/* 국적을 모르는 곳 — 국기 칩과 나란히 서지만 나라가 아니므로 점선으로 구분한다 */
.homekpi .rgc.unk{color:var(--muted);font-weight:600;
  border:1px dashed var(--line);border-radius:999px;padding:0 6px}
.homekpi .topap{font-size:15.5px;font-weight:800;display:block;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.tile .k{display:flex;align-items:center;gap:4px}
.tile .tq{font-size:11px;color:var(--muted);cursor:help;font-weight:700}
.tile .tq:hover{color:var(--accent2)}
.kpinote{color:var(--muted);font-size:11.5px;line-height:1.6;margin:9px 2px 0}
/* 오른쪽 '지난 브리핑'은 날짜+제목만 있으면 되므로 고정 폭으로 묶고, 남는 폭은 전부
   왼쪽 매트릭스에 준다(분야 이름이 길어져 넓을수록 가로 스크롤 없이 다 보인다). */
.homebot{display:grid;grid-template-columns:minmax(0,1fr) 320px;gap:14px;align-items:start}
.homebot.single{grid-template-columns:1fr}
.homepanel{background:var(--card);border:1px solid var(--line);border-radius:11px;padding:15px 16px;box-shadow:var(--shadow);min-width:0}
.homepanel h3{font-size:14px;font-weight:700;margin:0 0 3px;display:flex;align-items:center;gap:7px}
.homepanel .sub{color:var(--muted);font-size:12px;margin:0 0 12px}
.homepanel .morelink{margin-left:auto;font-size:11.5px;font-weight:600;color:var(--accent2);cursor:pointer}
.timeline{display:flex;flex-direction:column}
.timeline .tl{border-left:2px solid var(--line);padding:0 0 14px 15px;position:relative}
.timeline .tl:last-child{padding-bottom:2px}
.timeline .tl::before{content:"";position:absolute;left:-5px;top:4px;width:8px;height:8px;border-radius:50%;background:var(--accent)}
.timeline .tld{font-size:11px;color:var(--muted);font-variant-numeric:tabular-nums}
.timeline .tlh{font-size:13px;font-weight:700;margin:2px 0 3px;cursor:pointer;line-height:1.4}
.timeline .tlh:hover{color:var(--accent2)}
.timeline .tlb{font-size:12px;color:var(--muted);line-height:1.65;display:none}
.timeline .tl.open .tlb{display:block}
.homemx{margin-top:14px}   /* 브리핑 아래 분야별 경쟁 구도 */
/* 홈 특허 섹션 = 브리핑 전문(접기 없음) */
.pbhome .pbw{font-size:11px;font-weight:600;color:var(--muted);margin-left:2px}
.pbhome .pbh{font-size:16px;font-weight:800;letter-spacing:-.01em;line-height:1.45;margin:4px 0 11px}
.pbhome .bbody{font-size:13px;line-height:1.75}
.pbhome .bbody p{margin:0 0 7px}
.pbhome .bsec{margin:0 0 12px}
.pbhome .bsl{font-size:12px;font-weight:800;color:var(--accent2);margin:0 0 5px;
  padding-bottom:4px;border-bottom:1px solid var(--line)}
.pbhome .bpoints{display:grid;grid-template-columns:repeat(3,1fr);gap:9px;margin-top:11px}
.pbhome .pt{background:var(--bg);border:1px solid var(--line);border-radius:9px;padding:9px 11px}
.pbhome .pt .pl{font-size:12px;font-weight:800;display:flex;align-items:center;gap:5px;margin-bottom:3px}
.pbhome .pt .px{font-size:11.5px;color:var(--muted);line-height:1.5}
.pbhome .pbfoot{color:var(--muted);font-size:11px;margin:11px 0 0;padding-top:9px;
  border-top:1px solid var(--line)}
@media (max-width:1100px){ .pbhome .bpoints{grid-template-columns:1fr} }
/* 해외 출원인의 국내 공개 */
/* minmax(300px,…) 는 화면이 300px 보다 좁아도 칸을 300px 밑으로 못 줄인다
   → 320px 화면에서 칸이 화면 밖으로 나갔다. min() 으로 '300px 또는 100% 중
   작은 쪽' 을 최소폭으로 준다. */
/* 홈 한 줄 요약. 여섯 줄이 같은 격자에 서야 눈이 세로로 훑을 수 있다 —
   줄마다 폭이 다르면 규모 막대끼리 견주는 일이 불가능해진다. */
/* 오늘의 한 줄. KPI 바로 아래/* 오늘의 한 줄. KPI 바로 아래, 첫 화면 안에 든다. 머리글만 보이고 누르면
   본문이 있는 패널로 내려간다 — 같은 글을 두 곳에 두지 않는다. */
.today{display:flex;flex-direction:column;gap:7px;margin:14px 0 4px}
.tdl{display:flex;align-items:flex-start;gap:10px;width:100%;text-align:left;
  font:inherit;cursor:pointer;background:var(--card);border:1px solid var(--line);
  border-left:3px solid var(--accent);border-radius:10px;padding:11px 13px}
.tdl:hover{border-color:var(--accent2);border-left-color:var(--accent)}
.tdl .tdi{font-size:15px;line-height:1.45;flex:none}
.tdl .tdt{font-size:13.5px;line-height:1.55;color:var(--ink);flex:1;min-width:0}
.tdl .tdt b{color:var(--accent2);font-weight:800;margin-right:5px;white-space:nowrap}
.tdl .tdw{font-size:11px;color:var(--muted);font-weight:600;white-space:nowrap;
  flex:none;padding-top:2px}
@media (max-width:700px){ .tdl .tdw{display:none} }
.csum{display:flex;flex-direction:column;gap:2px;margin-top:12px}
.crow2{display:grid;grid-template-columns:minmax(150px,1.15fr) minmax(60px,1fr) 58px 92px 62px auto;
  align-items:center;gap:10px;width:100%;text-align:left;font:inherit;cursor:pointer;
  background:none;border:0;border-left:3px solid var(--line);border-radius:0 8px 8px 0;
  padding:8px 10px}
.crow2:hover{background:var(--bg)}
.crow2.lv-hi{border-left-color:var(--q3)} .crow2.lv-mid{border-left-color:var(--q2)}
.crow2.lv-lo{border-left-color:var(--q1)}
.crow2 .cnm{font-size:13px;font-weight:700;color:var(--ink);display:flex;align-items:center;
  gap:6px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.crow2 .cbar2{height:9px;background:var(--line);border-radius:5px;overflow:hidden}
.crow2 .cbar2 i{display:block;height:100%;background:var(--q2);border-radius:5px}
.crow2 .cval{font-size:12px;font-weight:700;color:var(--muted);text-align:right;
  font-variant-numeric:tabular-nums}
.crow2 .cspk{display:flex;align-items:center;gap:5px;font-size:11.5px;color:var(--muted)}
.crow2 .cspk b{color:var(--ink);font-weight:800;font-variant-numeric:tabular-nums}
.crow2 .cspk.none{font-size:10.5px;opacity:.7}
.crow2 .ckr{font-size:11.5px;font-weight:700;color:var(--muted);white-space:nowrap}
.crow2 .cgo{font-size:11px;font-weight:700;color:var(--accent2);white-space:nowrap;opacity:0}
.crow2:hover .cgo,.crow2:focus-visible .cgo{opacity:1}
@media (max-width:820px){
  .crow2{grid-template-columns:1fr auto;gap:5px 9px;padding:9px 10px}
  .crow2 .cnm{grid-column:1 / -1}
  .crow2 .cbar2{grid-column:1} .crow2 .cval{grid-column:2}
  .crow2 .cspk{grid-column:1} .crow2 .ckr{grid-column:2;text-align:right}
  .crow2 .cgo{display:none}
}
.krwrap{display:grid;grid-template-columns:repeat(auto-fill,minmax(min(300px,100%),1fr));gap:14px}
.krow{min-width:0}
.kap{font-size:13px;font-weight:800;display:flex;align-items:center;gap:6px;
  padding-bottom:5px;margin-bottom:7px;border-bottom:1px solid var(--line)}
.kap .kcnt{margin-left:auto;font-size:11px;font-weight:700;color:var(--muted)}
.klist{list-style:none;margin:0;padding:0;display:flex;flex-direction:column;gap:7px}
.klist li{font-size:12.5px;line-height:1.5}
.klist a{color:var(--ink);text-decoration:none}
.klist a:hover{color:var(--accent2);text-decoration:underline}
.klist .kc{display:inline-block;margin-left:6px;font-size:10.5px;font-weight:700;color:var(--muted);
  border:1px solid var(--line);border-radius:5px;padding:1px 5px;white-space:nowrap}
.klist .kn{display:block;font-size:10.5px;color:var(--muted);margin-top:1px}
.klist .kmore{color:var(--muted);font-size:11.5px}
/* 해외 국내공개 — 목록 위의 분석 */
.kana{margin:0 0 4px}
.kalead{margin:0 0 12px;font-size:13.5px;line-height:1.65}
.kacols{display:grid;grid-template-columns:repeat(auto-fit,minmax(min(280px,100%),1fr));gap:16px 22px}
.kah{font-size:12px;font-weight:800;color:var(--accent2);letter-spacing:.02em;
  margin:0 0 5px;padding-bottom:4px;border-bottom:1px solid var(--line)}
.kanote{margin:0 0 9px;font-size:11.5px;line-height:1.6;color:var(--muted)}
/* 줄마다 따로 grid 를 만들면 이름 길이에 따라 막대 자리가 줄마다 달라진다 —
   같은 그림 안에서 자가 다른 셈이라 견줄 수가 없다(실측 270·262·277·248px).
   바깥에 한 번만 grid 를 두고 줄은 display:contents 로 흘려보내면 세 칸이
   모든 줄에서 같은 폭으로 선다. */
.kfrows{display:grid;grid-template-columns:minmax(0,1fr) minmax(70px,1.3fr) auto;
  align-items:center;gap:6px 9px;font-size:12.5px}
.kfrow{display:contents}
.kfn{min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
/* 막대는 얇게, 데이터 끝만 둥글게, 바닥(0)에 붙여 둔다 */
.kfb{height:9px;border-radius:3px;background:var(--line);overflow:hidden}
.kfb i{display:block;height:100%;background:var(--accent2);border-radius:0 3px 3px 0}
.kfv{display:flex;align-items:baseline;gap:6px;white-space:nowrap;font-size:11.5px}
/* 좁은 자리에서는 이름 칸이 세 글자로 잘려('원전·…') 어느 막대인지 알 수 없었다.
   이름을 제 줄로 올리고 아래에 막대와 숫자를 둔다 — 자는 그대로 공유한다.
   화면 폭이 아니라 **칸 폭**으로 판단한다. 두 칸이 나란히 서면 화면이 820px 라도
   칸은 349px 뿐이라, 화면 기준(560px)으로는 그 경우를 못 잡았다(실측 세 줄이 잘렸다). */
.kacol{min-width:0;container-type:inline-size}
@container (max-width:420px){
  .kfrows{grid-template-columns:minmax(0,1fr) auto;gap:0 9px}
  .kfn{grid-column:1 / -1;white-space:normal;padding-top:9px}
  .kfb{grid-column:1;margin-top:3px} .kfv{grid-column:2;margin-top:3px}
}
.kfv b{font-size:12.5px}
.kfv span{color:var(--muted)}
.kfw{font-size:10px;border:1px solid var(--line);border-radius:5px;padding:0 4px}
.kafoot{margin:13px 0 0;padding-top:11px;border-top:1px solid var(--line);
  font-size:12.5px;line-height:1.65;color:var(--muted)}
.kafoot b{color:var(--ink)}
.kalist{margin-top:18px}
.kalist .kah{margin-bottom:10px;display:flex;flex-wrap:wrap;align-items:baseline;gap:4px 10px}
.kasub{font-size:11.5px;font-weight:600;color:var(--muted);letter-spacing:0}
.ksolo{margin-top:14px;border-top:1px solid var(--line);padding-top:11px}
.ksolo>summary{cursor:pointer;font-size:12.5px;font-weight:800;list-style:none}
.ksolo>summary::-webkit-details-marker{display:none}
.ksolo>summary::before{content:'▸ ';color:var(--muted)}
.ksolo[open]>summary::before{content:'▾ '}
.ksolo>summary .kcnt{margin-left:5px;font-size:11px;font-weight:700;color:var(--muted)}
/* details 를 접었을 때 안을 숨기는 것은 UA 규칙이라 작성자 규칙에 진다.
   .kchips 에 display:flex 를 그냥 주면 접어도 칩이 그대로 보인다(실측). */
.kchips{display:none;flex-wrap:wrap;gap:5px;margin-top:9px}
.ksolo[open]>.kchips{display:flex}
.krmore>.krwrap{display:none}
.krmore[open]>.krwrap{display:grid;margin-top:12px}
.kchip{font-size:11.5px;color:var(--ink);text-decoration:none;background:var(--chipbg);
  border:1px solid var(--line);border-radius:6px;padding:2px 7px;max-width:100%;
  overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.kchip:hover{color:var(--accent2);border-color:var(--accent2)}
/* 국내/해외 소제목 구분 */
.brief .bsec{margin:0 0 13px}
.brief .bsec:last-child{margin-bottom:0}
.brief .bsl{font-size:12px;font-weight:800;color:var(--accent2);letter-spacing:.02em;
  margin:0 0 5px;padding-bottom:4px;border-bottom:1px solid var(--line)}
/* 긴 본문은 접는다. 여는 장치는 '지난 특허 브리핑'(.pbpast)과 같은 모양으로
   둔다 — 한 패널 안에 서로 다르게 생긴 접기 두 개가 있으면 다른 일을 하는
   것처럼 읽힌다. */
.pbfull{margin-top:12px;border-top:1px solid var(--line);padding-top:10px}
.pbfull summary{font-size:12px;font-weight:700;color:var(--accent2);cursor:pointer}
.pbfull .bbody{margin-top:9px}
.pbpast{margin-top:12px;border-top:1px solid var(--line);padding-top:10px}
.pbpast summary{font-size:12px;font-weight:700;color:var(--muted);cursor:pointer}
.pbpast summary:hover{color:var(--ink)}
.pbpast .timeline{margin-top:10px}
@media (max-width:1100px){ .homebot{grid-template-columns:1fr} }
@media (max-width:720px){ .homekpi{grid-template-columns:repeat(2,1fr)} }
/* 트렌드 인사이트 바 */
/* 패널 둘(키워드·이슈 흐름)을 하나로 접은 뒤로 이 격자는 한 칸이다. 예전의
   여러 칸 규칙을 그대로 두면 접은 패널이 절반 폭에 갇혀, 그래프의 선 이름과
   분야 이름이 잘린다(실측 460px). */
.insights{display:grid;grid-template-columns:1fr;gap:12px;margin:2px 0 16px}
.insights .ipanel{background:var(--card);border:1px solid var(--line);border-radius:10px;
  padding:12px 14px;box-shadow:var(--shadow);min-width:0}
.insights h3{font-size:12px;font-weight:800;letter-spacing:.01em;margin:0 0 2px;display:flex;
  align-items:center;gap:6px}
.insights .isub{color:var(--muted);font-size:11px;margin:0 0 10px}
.trend{display:flex;flex-direction:column;gap:7px}
/* 뉴스 트렌드 — 결론 한 줄. 계산해서 말하는 문장이라 브리핑(사람이 쓴 사건
   서술)과 겹치지 않는다. 격차가 좁으면 문구 자체가 바뀐다(leadLineHTML). */
.lead2{margin:2px 0 12px}
.lead2 .l2h{font-size:16px;font-weight:800;letter-spacing:-.01em;line-height:1.5}
.lead2 .l2h b{color:var(--accent2)}
.lead2 .l2h em{font-style:normal;font-size:13px;font-weight:700;color:var(--muted);
  margin-left:8px;white-space:nowrap}
.lead2 .l2s{display:flex;flex-wrap:wrap;gap:6px 14px;margin-top:6px;
  font-size:11.5px;font-weight:600;color:var(--muted)}
.lead2 .l2s .lu{color:var(--q2)}
/* 뉴스와 특허를 잇는 다리. 옆의 '함께 오른 곳' 문구와 같은 줄에 서되, 누를 수
   있다는 것이 보여야 한다. */
.lead2 .l2go{font:inherit;font-size:11.5px;font-weight:700;color:var(--accent2);
  background:none;border:0;padding:0;cursor:pointer;white-space:nowrap}
.lead2 .l2go:hover{text-decoration:underline}
/* 매트릭스가 경쟁·활용 탭으로 왔다. 거기 .panel 격자가 없으므로 표가 줄어들 수 있게
   직접 잡아 준다 — 안 그러면 표의 최소폭이 본문을 화면 밖으로 민다(통계 탭에서
   같은 자리를 이미 한 번 물렸다). */
.mtxwrap{min-width:0}
.mtxwrap .rgsec, .mtxwrap .pmxwrap{min-width:0}
/* 30일 그래프. 세로축은 비중이다 — 일별 기사 수가 23~180건으로 널뛰어 건수로
   그리면 그 널뜀이 곡선을 다 먹는다(실측). 좁으면 제 상자에서 가로로 넘긴다. */
.tcwrap{overflow-x:auto;margin:0 0 10px}
.tchart{width:100%;min-width:520px;height:auto;display:block}
.tchart .tl{fill:none;stroke-width:2;stroke-linejoin:round;stroke-linecap:round}
.tchart .tg{stroke:var(--line);stroke-width:1}
.tchart .tgl{font-size:9.5px;fill:var(--muted);font-weight:600}
.tchart .tll{font-size:10.5px;font-weight:700}
/* 분야 줄 — 이름 · 흐름 · 수치 · 그 분야를 끄는 말. */
.trend .row .kws{display:flex;flex-wrap:wrap;gap:4px;justify-content:flex-end;min-width:0}
.kw2{font-size:10.5px;font-weight:600;color:var(--muted);background:var(--bg);
  border:1px solid var(--line);border-radius:999px;padding:1px 7px;cursor:pointer;
  white-space:nowrap}
.kw2:hover{color:var(--accent2);border-color:var(--accent2)}
/* 이름 · 30일 흐름 · 수치 세 칸. 같은 격자에 서야 세로로 훑을 수 있다 —
   칸 폭이 줄마다 다르면 선끼리 견줄 수 없다. */
.trend .row{display:grid;grid-template-columns:minmax(120px,1fr) 62px 74px minmax(0,1.1fr);
  align-items:center;gap:9px;font-size:12.5px;
  cursor:pointer;border-radius:6px;padding:3px 4px;margin:0 -4px}
.trend .row:hover{background:var(--bg)}
.trend .row .sp{display:flex;align-items:center;justify-content:flex-end;min-width:0}
.trend .row .sp .spk{opacity:.85}
.trend .row:hover .sp .spk{opacity:1}
/* 아주 좁으면 흐름 칸을 접는다 — 68px 아래로는 선이 뜻을 잃는다. */
@media (max-width:820px){
  /* 좁으면 말 칸을 줄 아래로 내린다 — 네 칸을 다 세우면 이름이 잘린다. */
  .trend .row{grid-template-columns:1fr 62px 74px;gap:6px 9px}
  .trend .row .kws{grid-column:1 / -1;justify-content:flex-start}
}
@media (max-width:520px){
  /* 아주 좁으면 흐름 칸을 접는다 — 62px 아래로는 선이 뜻을 잃는다. */
  .trend .row{grid-template-columns:1fr auto}
  .trend .row .sp{display:none}
}
.trend .nm{white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.trend .d{font-variant-numeric:tabular-nums;font-weight:700;font-size:12px;white-space:nowrap}
.trend .d .n{color:var(--muted);font-weight:600}
.trend .up{color:var(--accent)}.trend .dn{color:var(--muted)}.trend .fl{color:var(--muted)}
.ppick{display:flex;flex-direction:column;gap:2px}
.ppick .pk{display:flex;gap:8px;align-items:flex-start;text-decoration:none;color:var(--ink);
  padding:6px 4px;border-radius:6px;margin:0 -4px;border-bottom:1px solid var(--line)}
.ppick .pk:last-child{border-bottom:0}
.ppick .pk:hover{background:var(--bg)}
.ppick .pf{font-size:13px;line-height:1.5;flex:none}
.ppick .pt2{font-size:12.5px;line-height:1.45;display:-webkit-box;-webkit-line-clamp:2;
  -webkit-box-orient:vertical;overflow:hidden}
.ppick .pk:hover .pt2{color:var(--accent2)}
.ppick .who{color:var(--muted);font-size:11px;margin-left:6px;white-space:nowrap}
.iempty{color:var(--muted);font-size:12px}
@media (max-width:820px){ .insights{grid-template-columns:1fr} }
/* 컨트롤 */
.controls{display:flex;flex-direction:column;gap:10px;margin:0 0 16px;
  position:sticky;top:0;z-index:5;background:var(--bg);padding-top:8px}
/* 줄바꿈을 허용하고 칸이 줄어들 수 있게 해 둔다. 안 그러면 좁은 화면에서
   검색칸이 제 최소폭을 고집해 오른쪽 정렬·토글 묶음을 화면 밖으로 밀어낸다
   (실측 360px: 본문이 395px 이 되어 페이지 전체에 가로 스크롤이 걸렸다).
   input 은 기본 최소폭이 있어 flex:1 만으로는 안 줄어든다 → min-width:0 을 준다. */
.searchrow{display:flex;gap:8px;align-items:center;flex-wrap:wrap}
.search{flex:1 1 220px;min-width:0;display:flex;align-items:center;gap:8px;background:var(--card);
  border:1px solid var(--line);border-radius:9px;padding:9px 13px}
.search input{flex:1;min-width:0;border:0;background:none;color:var(--ink);font:inherit;font-size:15px;outline:none}
.search .ico{color:var(--muted)}
.selects{display:flex;gap:8px;flex-wrap:wrap}
.selects select,.selects button.toggle{font:inherit;font-size:13px;color:var(--ink);
  background:var(--chipbg);border:1px solid var(--line);border-radius:8px;padding:7px 11px;cursor:pointer}
.chips{display:flex;gap:7px;flex-wrap:wrap}
.chips .f{font-size:12.5px;padding:5px 11px;border:1px solid var(--line);border-radius:999px;
  background:var(--chipbg);color:var(--ink);cursor:pointer;user-select:none;font:inherit;transition:all .12s}
.chips .f[aria-pressed="true"]{background:var(--ink);color:var(--bg);border-color:var(--ink)}
.chips .f .n{color:var(--muted);margin-left:5px}
.chips .f[aria-pressed="true"] .n{color:var(--bg);opacity:.7}
.chips .f.co[aria-pressed="true"]{background:var(--accent2);border-color:var(--accent2);color:#fff}
.resline{display:flex;justify-content:space-between;align-items:center;color:var(--muted);
  font-size:12.5px;margin:2px 2px 12px}
.resline .reset{color:var(--accent2);cursor:pointer;background:none;border:0;font:inherit;font-size:12.5px}
/* 결과 카드 */
.card{background:var(--card);border:1px solid var(--line);border-radius:9px;padding:13px 15px;
  margin-bottom:9px;box-shadow:var(--shadow);transition:border-color .12s,transform .12s;position:relative}
.card:hover{border-color:var(--accent);transform:translateY(-1px)}
.card .t{font-size:15.5px;font-weight:650;line-height:1.4;margin:0 0 5px;text-decoration:none;display:block}
.card .t:hover{color:var(--accent2)}
.card .meta{color:var(--muted);font-size:12.5px;display:flex;gap:8px;flex-wrap:wrap;align-items:center}
.card .meta .src{color:var(--ink);font-weight:600}
.card .meta .num{font-family:ui-monospace,Menlo,monospace;font-size:11.5px}
.card .meta .off{border:1px solid var(--line);border-radius:4px;padding:0 5px;font-size:11px}
.card .meta .cpc{font-family:ui-monospace,Menlo,monospace;font-size:11px;color:var(--accent2)}
.card .sum{color:var(--muted);font-size:13px;margin-top:6px;
  display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;overflow:hidden}
.card .tag{display:inline-flex;align-items:center;gap:4px;font-size:11px;color:var(--muted);
  border:1px solid var(--line);border-radius:999px;padding:1px 8px;margin-left:auto}
.card.isnew{border-left:3px solid var(--accent)}
.mockflag{background:var(--accent);color:#1a1a1a;font-size:10.5px;font-weight:700;padding:1px 7px;border-radius:999px}
.empty{color:var(--muted);font-size:14px;padding:40px 0;text-align:center;border:1px dashed var(--line);border-radius:9px}
.scope,.scopewrap{color:var(--muted);font-size:12px;line-height:1.6;margin:0 2px 12px;background:var(--card);border:1px solid var(--line);border-radius:8px;padding:8px 11px}
.scope b{color:var(--ink)}
.stats .panel .scope{margin:0 0 12px}
.more{display:block;margin:14px auto 0;font:inherit;font-size:13px;font-weight:600;color:var(--ink);
  background:var(--card);border:1px solid var(--line);border-radius:8px;padding:9px 18px;cursor:pointer}
.selects .toggle[aria-pressed="true"]{background:var(--ink);color:var(--bg);border-color:var(--ink)}
/* 저장 별표 / 읽음 / 검색어 하이라이트 */
.card{padding-right:38px}
.card .star{position:absolute;top:10px;right:10px;background:none;border:0;cursor:pointer;
  font-size:17px;line-height:1;color:var(--muted);padding:2px;transition:color .12s}
.card .star:hover{color:var(--accent)}
.card .star.on{color:var(--accent)}
.card.isread{opacity:.5}
.card.isread .t{color:var(--muted)}
.card mark{background:rgba(232,163,61,.38);color:inherit;border-radius:2px;padding:0 1px}
/* 날짜 그룹 헤더 */
.dgroup{font-size:13.5px;font-weight:800;color:var(--ink);margin:18px 2px 9px;display:flex;
  align-items:baseline;gap:8px;border-bottom:1px solid var(--line);padding-bottom:5px}
.dgroup:first-child{margin-top:2px}
.dgroup .d{font-weight:500;color:var(--muted);font-size:11.5px;font-variant-numeric:tabular-nums}
.dgroup .n{margin-left:auto;font-weight:600;color:var(--muted);font-size:11.5px}
.sparkwrap svg rect{cursor:pointer}
.sparkwrap svg rect.sel{fill:var(--accent2)}
/* 맨 위로 버튼 */
.totop{position:fixed;right:18px;bottom:18px;z-index:20;width:44px;height:44px;border-radius:50%;
  border:1px solid var(--line);background:var(--card);color:var(--ink);font-size:19px;cursor:pointer;
  box-shadow:0 3px 10px rgba(0,0,0,.18)}
.totop:hover{border-color:var(--accent)}
.foot{color:var(--muted);font-size:12px;border-top:1px solid var(--line);padding-top:14px;margin-top:32px;line-height:1.7}
.foot a{color:var(--accent2)}
/* 목록/통계 토글 */
.viewseg{display:none;gap:0;margin:0 0 14px;border:1px solid var(--line);border-radius:9px;
  overflow:hidden;width:max-content}
.viewseg.on{display:inline-flex}
.viewseg button{font:inherit;font-size:13px;font-weight:600;color:var(--muted);background:var(--card);
  border:0;padding:8px 16px;cursor:pointer}
.viewseg button[aria-pressed="true"]{background:var(--ink);color:var(--bg)}
/* 통계 뷰 */
.stats{display:grid;grid-template-columns:1fr 1fr;gap:18px}
/* 그리드 칸은 기본이 min-width:auto 라 **안에 든 것의 최소폭까지** 늘어난다.
   매트릭스 표는 칸 이름이 nowrap 이라 최소폭이 화면보다 넓고, 그래서 패널이
   화면 밖으로 밀려 본문 전체에 가로 스크롤이 걸렸다(실측 430px 화면에서 본문
   536px). .pmxwrap 에 overflow-x:auto 를 걸어 뒀지만, 조상이 줄어들 수 없으면
   스크롤 상자가 만들어지지 않아 아무 일도 하지 않는다.
   칸이 줄어들 수 있게 해 두면 표는 제 상자 안에서 가로로 넘어간다. */
.stats>.panel{min-width:0}
.stats .rgsec, .stats .pmxwrap{min-width:0}
.stats .panel{background:var(--card);border:1px solid var(--line);border-radius:11px;padding:16px 17px;box-shadow:var(--shadow)}
.stats .panel.wide{grid-column:1 / -1}
.stats h3{font-size:14px;font-weight:700;margin:0 0 4px;display:flex;align-items:center;gap:7px}
.rankseg{margin-left:auto;display:inline-flex;border:1px solid var(--line);border-radius:7px;overflow:hidden}
.rankseg button{font:inherit;font-size:11.5px;font-weight:600;color:var(--muted);background:var(--card);border:0;padding:4px 10px;cursor:pointer}
.rankseg button[aria-pressed="true"]{background:var(--ink);color:var(--bg)}
.rgrank{margin-bottom:13px}
.rgrank:last-child{margin-bottom:0}
.rgrank .rghead{font-size:12px;margin-bottom:5px}
.stats .sub{color:var(--muted);font-size:12px;margin:0 0 13px}
.lead{display:flex;flex-direction:column;gap:9px}
.lead .row{display:grid;grid-template-columns:170px 1fr auto;align-items:center;gap:10px}
.lead .nm{font-size:13px;font-weight:600;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.lead .nm .rk{color:var(--muted);font-weight:700;margin-right:6px;font-variant-numeric:tabular-nums}
.lead .bar{height:15px;background:var(--accent);border-radius:0 4px 4px 0;min-width:2px}
.lead .bar.cap{background:repeating-linear-gradient(135deg,var(--accent) 0 7px,var(--line) 7px 10px)}
.lead .val .plus{color:var(--muted);font-weight:700;margin-left:1px}
.lead .val{font-size:12.5px;font-weight:700;font-variant-numeric:tabular-nums;white-space:nowrap}
.lead .val .co{color:var(--muted);font-weight:500;font-size:11.5px;margin-left:5px}
.pmxwrap{overflow-x:auto}
.pmx{border-collapse:separate;border-spacing:2px;font-size:12.5px;min-width:100%}
.pmx th{font-weight:600;color:var(--muted);text-align:center;padding:3px 4px;font-size:13px;white-space:nowrap}
.pmx th.cnr{text-align:left;font-size:11px;font-weight:600}
/* 분야 머리글: 아이콘 대신 이름. <wbr> 위치(가운뎃점)에서만 접힌다 */
.pmx th.cth{font-size:10.5px;font-weight:600;line-height:1.25;white-space:normal;word-break:keep-all;
  max-width:80px;vertical-align:bottom}
.pmx th.cth .seg{white-space:nowrap}
/* 이름 칸은 폭을 묶는다. 출원인 이름은 길이가 정해져 있지 않아서(실측 216자짜리
   중국 연구소, 그리고 공동출원인이 통째로 붙은 이름) 한 줄이 표 전체를 오른쪽으로
   밀어낸다 — 숫자 칸이 화면 밖으로 나가면 표를 볼 수가 없다. 잘린 이름 전체는
   title 로 준다(칸에 이미 title 이 있으므로 이름 칸에도 붙인다). */
.pmx td.lab{text-align:left;white-space:nowrap;font-weight:600;padding-right:8px;font-size:12px;
  max-width:15em;overflow:hidden;text-overflow:ellipsis}
@media (max-width:560px){ .pmx td.lab{max-width:9.5em} }
.pmx td.c{text-align:center;border-radius:5px;font-variant-numeric:tabular-nums;min-width:34px;
  padding:5px 4px;color:var(--muted);background:var(--bg)}
.pmx td.c.has{color:var(--ink);cursor:pointer}
.pmx td.c.has:hover{outline:2px solid var(--accent)}
.pmx td.c.tot{font-weight:800;color:var(--ink);background:transparent}
.rgsec{margin-bottom:14px}
.rgsec:last-child{margin-bottom:0}
.rghead{font-size:12.5px;font-weight:700;margin:0 0 5px;padding-bottom:3px;border-bottom:1px solid var(--line);
  display:flex;align-items:baseline;gap:6px}
.rghead .rgn{margin-left:auto;font-size:11px;font-weight:600;color:var(--muted)}
/* 국적 묶음 펼치기 — 국유특허의 '더 보기'(.stmore)와 같은 모양으로 둔다.
   한 화면에 두 종류의 펼침 버튼이 다르게 생기면 서로 다른 일을 하는 것처럼 읽힌다. */
.rgsec .rgmore{margin-top:6px;font:inherit;font-size:12px;font-weight:700;
  color:var(--accent2);background:none;border:1px dashed var(--line);
  border-radius:9px;padding:8px;cursor:pointer;width:100%}
.rgsec .rgmore:hover{border-color:var(--accent2)}
.catlead{display:flex;flex-direction:column;gap:11px}
.catlead .crow{display:flex;flex-direction:column;gap:5px}
.catlead .clab{font-size:12.5px;font-weight:700}
.catlead .ctops{display:flex;flex-wrap:wrap;gap:5px}
.catlead .cta{font-size:11.5px;border:1px solid var(--line);border-radius:999px;padding:2px 9px;
  display:inline-flex;align-items:center;gap:5px;background:var(--bg)}
.catlead .cta .ctn{color:var(--accent2);font-weight:700;font-variant-numeric:tabular-nums;font-size:10.5px}
/* 분야별 집중도(상위 3곳 점유율). 막대 하나로 분야끼리 눈으로 비교되게 한다 */
.catlead .clab{display:flex;align-items:center;gap:8px}
.catlead .conc{display:flex;align-items:center;gap:7px;margin-left:auto;font-size:11px;font-weight:700}
.catlead .cbar{width:96px;height:6px;border-radius:999px;background:var(--line);overflow:hidden;flex:none}
/* 막대 색은 분야 지도(.qchart)와 같은 단일 색상 램프를 쓴다 — 같은 변수를 두 화면에서
   다른 색 언어로 칠하면 읽는 사람이 둘을 다른 지표로 오해한다. 뜻은 글자가 지므로
   글자는 색 램프가 아니라 본문 색을 입는다(연한 단계는 글자로 쓰기엔 대비가 모자란다). */
.catlead .cbar i{display:block;height:100%;border-radius:999px;background:var(--muted)}
.catlead .conc.hi .cbar i{background:var(--q3)}
.catlead .conc.mid .cbar i{background:var(--q2)}
.catlead .conc.lo .cbar i{background:var(--q1)}
.catlead .cpct{font-variant-numeric:tabular-nums;color:var(--ink)}
.catlead .clv{color:var(--ink)}
.catlead .cn{color:var(--muted);font-weight:600}
.catlead .conc.na{color:var(--muted)}
@media (max-width:560px){ .catlead .conc{margin-left:0;width:100%} .catlead .clab{flex-wrap:wrap} }
/* 분야별 국내 공급자. 경쟁 구도 표와 같은 뼈대(.catlead)를 쓰되, 칩이 누를 수 있는
   것임을 커서·테두리로만 알린다. 색은 새로 만들지 않는다. */
.catlead.sup .cta.sup{cursor:pointer;background:var(--card)}
.catlead.sup .cta.sup:hover{border-color:var(--accent2)}
.catlead.sup .cta.sup:focus-visible{outline:2px solid var(--accent);outline-offset:2px}
.catlead.sup .cta.supmore{color:var(--muted);border-style:dashed}
.catlead.sup .cbar i{background:var(--q2)}
/* 지연 로딩 표시. 눈에 띄되 숫자를 가리지 않는 크기 — 이건 경고가 아니라 상태다. */
.hyd{font-size:11px;font-weight:600;color:var(--muted);margin-left:6px}
.statkpi{display:flex;gap:20px;flex-wrap:wrap;margin-bottom:4px}
.statkpi .k{color:var(--muted);font-size:11.5px}
/* 우리가 그린 국기. 글자 옆에 서므로 글자 크기를 따라가고, 밑선을 글자에 맞춘다.
   가는 테두리는 흰 바탕 국기(일본·한국)가 밝은 배경에 묻히지 않게 하는 것이다. */
.fl{width:1.15em;height:.86em;vertical-align:-.08em;border-radius:1.5px;
  box-shadow:0 0 0 .5px rgba(0,0,0,.22);flex:none}
/* 우리가 안 그린 나라 — 두 글자로 보인다(윈도우가 하던 것과 같지만 의도된 모양). */
.fl.code{display:inline-block;width:auto;height:auto;font-size:.72em;font-weight:700;
  letter-spacing:.02em;padding:0 3px;border-radius:3px;box-shadow:none;
  border:1px solid var(--line);color:var(--muted);vertical-align:.05em}
/* '국적미상 N' — 국기 칩과 나란히 서지만 나라가 아니므로 점선 테두리로 구분한다.
   숨기지 않는 것이 요점이라 눈에는 띄되, 국기들보다 앞서 읽히면 안 된다. */
.statkpi .unk{border:1px dashed var(--line);border-radius:999px;padding:0 6px;
  margin-left:4px;cursor:help;white-space:nowrap}
.statkpi .v{font-size:19px;font-weight:800}
.unknown{color:var(--muted);font-size:12px;margin-top:8px}
.homehint{color:var(--muted);font-size:12.5px;margin:2px 0 0}
@media (max-width:820px){
.stats{grid-template-columns:1fr}
.lead .row{grid-template-columns:120px 1fr auto}
  .overview{grid-template-columns:repeat(2,1fr)}
  .tile.spark{grid-column:1 / -1}
  .controls{position:static}
}
"""

_PAGE = """<!doctype html><html lang="ko"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>__TITLE__</title>
<meta name="description" content="__TAGLINE__">
<!-- Google Search Console 소유 확인. 검색 노출 자체를 만들어 주지는 않고,
     '이 사이트가 내 것' 임을 확인해 색인 상태를 볼 수 있게 하는 표식이다. -->
<meta name="google-site-verification" content="__GSV__">
<meta name="robots" content="index,follow">
<link rel="canonical" href="__SITEURL__">
<meta property="og:type" content="website">
<meta property="og:title" content="__TITLE__">
<meta property="og:description" content="__TAGLINE__">
<meta property="og:url" content="__SITEURL__">
<meta property="og:locale" content="ko_KR">
<!-- 탭 아이콘. 기본은 data: 로 박아 요청이 없고 file:// 로 열어도 뜬다.
     favicon.ico 는 링크가 없어도 브라우저가 자동으로 찾는 자리라 같이 둔다
     (없으면 404 가 남고, SVG 를 못 읽는 옛 클라이언트의 대비책이기도 하다). -->
<link rel="icon" type="image/svg+xml" href="__FAVICON__">
<link rel="alternate icon" href="favicon.ico" sizes="16x16 32x32">
<link rel="apple-touch-icon" href="apple-touch-icon.png">
<style>__CSS__</style></head>
<body>
<!-- 국기 — 이모지로 두면 윈도우에서 안 보인다.
     윈도우의 시스템 이모지 글꼴에는 국기가 없어서, 지역표시자 두 글자를 그대로
     찍는다: 🇰🇷 → 'KR', 🇺🇸 → 'US'. 실제 화면이 'US8KR1143CN11JP11EU13' 이 됐다.
     글꼴 문제라 CSS 로는 못 고치고, 국기가 든 웹폰트를 불러오는 것도 답이 아니다
     (수 MB짜리 이모지 글꼴 하나가 우리가 줄여 놓은 전송량을 통째로 되돌린다).
     그래서 직접 그린다. 16px 에서 나라가 구분되는 최소한만 담았다 —
     별 개수·괘 같은 세부는 그 크기에서 어차피 보이지 않는다. -->
<svg width="0" height="0" style="position:absolute" aria-hidden="true" focusable="false">
  <symbol id="f-KR" viewBox="0 0 12 9"><rect width="12" height="9" fill="#fff"/>
    <circle cx="6" cy="4.5" r="2" fill="#CD2E3A"/>
    <path d="M4 4.5a2 2 0 004 0 1 1 0 00-2 0 1 1 0 01-2 0z" fill="#0047A0"/>
    <g fill="#111"><rect x="1.1" y="1.5" width="2" height=".5"/>
      <rect x="8.9" y="1.5" width="2" height=".5"/>
      <rect x="1.1" y="7" width="2" height=".5"/>
      <rect x="8.9" y="7" width="2" height=".5"/></g></symbol>
  <symbol id="f-US" viewBox="0 0 12 9"><rect width="12" height="9" fill="#fff"/>
    <g fill="#B22234"><rect width="12" height="1"/><rect y="2" width="12" height="1"/>
      <rect y="4" width="12" height="1"/><rect y="6" width="12" height="1"/>
      <rect y="8" width="12" height="1"/></g>
    <rect width="5.5" height="5" fill="#3C3B6E"/></symbol>
  <symbol id="f-CN" viewBox="0 0 12 9"><rect width="12" height="9" fill="#DE2910"/>
    <circle cx="2.6" cy="2.6" r="1.2" fill="#FFDE00"/>
    <g fill="#FFDE00"><circle cx="5.1" cy="1.2" r=".45"/><circle cx="6.1" cy="2.4" r=".45"/>
      <circle cx="5.9" cy="4" r=".45"/><circle cx="4.7" cy="5" r=".45"/></g></symbol>
  <symbol id="f-JP" viewBox="0 0 12 9"><rect width="12" height="9" fill="#fff"/>
    <circle cx="6" cy="4.5" r="2.4" fill="#BC002D"/></symbol>
  <symbol id="f-EU" viewBox="0 0 12 9"><rect width="12" height="9" fill="#039"/>
    <g fill="#FC0"><circle cx="6" cy="1.9" r=".5"/><circle cx="8.2" cy="2.8" r=".5"/>
      <circle cx="9.1" cy="4.5" r=".5"/><circle cx="8.2" cy="6.2" r=".5"/>
      <circle cx="6" cy="7.1" r=".5"/><circle cx="3.8" cy="6.2" r=".5"/>
      <circle cx="2.9" cy="4.5" r=".5"/><circle cx="3.8" cy="2.8" r=".5"/></g></symbol>
</svg>
<div class="wrap">
  <header class="mast">
    <div class="masttext">
      <h1><button type="button" id="brand" class="brand" aria-label="__TITLE__ — 홈으로"><span class="bolt">⚡</span> __TITLE__</button></h1>
      <p class="tag">__TAGLINE__</p>
    </div>
    <a class="org" href="__ORGURL__" target="_blank" rel="noopener"
       title="__CLUB__ 페이지로 이동 — __ORG__ 운영">__CI__<span class="orgname">__DEPT__</span><span
       class="orgclub">__CLUB__ ↗</span></a>
  </header>
  <nav class="tabs" role="tablist" aria-label="보기 전환">
    <button role="tab" id="tab-home" aria-selected="true" data-tab="home">🏠 홈</button>
    <button role="tab" id="tab-news" aria-selected="false" data-tab="news">📰 뉴스</button>
    <button role="tab" id="tab-patents" aria-selected="false" data-tab="patents">📄 특허</button>
    <button role="tab" id="tab-guide" aria-selected="false" data-tab="guide">🤝 경쟁·활용</button>
  </nav>
  <section class="home" id="home" aria-label="대시보드" hidden></section>
  <section class="home" id="guide" aria-label="특허 경쟁 구도·활용 안내" hidden></section>
  <div class="viewseg" id="viewToggle" role="group" aria-label="특허 보기 방식">
    <button data-view="list" aria-pressed="true">목록</button>
    <button data-view="stats" aria-pressed="false">📊 통계</button>
  </div>
  <section class="overview" id="overview" aria-label="개요"></section>
  <div class="controls">
    <div class="searchrow">
      <label class="search">
        <span class="ico" aria-hidden="true">🔍</span>
        <input id="q" type="search" placeholder="제목·요약·출처·출원인·공개번호 검색" aria-label="검색">
      </label>
      <div class="selects">
        <select id="sort" aria-label="정렬">
          <option value="new">최신순</option>
          <option value="old">오래된순</option>
        </select>
        <select id="source" aria-label="출처 필터" hidden></select>
        <button class="toggle" id="newonly" aria-pressed="false" title="지난 방문 이후 새 항목만">✨ 새 항목</button>
        <button class="toggle" id="savedonly" aria-pressed="false" title="저장한 기사만">⭐ 저장</button>
        <button class="toggle" id="unreadonly" aria-pressed="false" title="안 읽은 기사만">👁 안읽음</button>
      </div>
    </div>
    <div class="chips" id="periodBar" aria-label="기간 필터" hidden></div>
    <div class="chips" id="countryChips" aria-label="국가 필터" hidden></div>
    <div class="chips" id="catChips" aria-label="카테고리 필터"></div>
  </div>
  <div class="resline">
    <span id="resCount" aria-live="polite"></span>
    <button class="reset" id="reset" hidden>필터 초기화</button>
  </div>
  <p class="scopewrap" id="scope" hidden></p>
  <main id="results"></main>
  <button class="more" id="more" hidden>더 보기</button>
  <footer class="foot" id="foot"></footer>
</div>
<button id="toTop" class="totop" aria-label="맨 위로" title="맨 위로 (스크롤)" hidden>↑</button>
<script id="feed" type="application/json">__FEED__</script>
<script>__JS__</script>
</body></html>"""

_JS = r"""
const FEED = JSON.parse(document.getElementById('feed').textContent);
const PAGE = 60;

// ── 지연 로딩 ───────────────────────────────────────────────────
// 첫 화면은 인라인된 최근분으로 바로 그리고, 나머지 목록은 받아서 붙인 뒤 다시
// 그린다. 집계는 서버에서 전체로 계산돼 인라인이라 처음부터 정확하다 — 여기서
// 채워지는 것은 '목록'뿐이다.
//
// FULL 이 false 인 동안에는 항목을 직접 세는 화면을 내보내지 않는다. 틀린 수를
// 잠깐 보여주는 것이 늦게 보여주는 것보다 나쁘다(공급자 87곳이 20곳으로 보였다가
// 채워지면, 먼저 본 사람은 20곳으로 기억한다).
let FULL = !(FEED.news.rest || FEED.patents.rest);
let HYDFAIL = '';
const count = t => (FEED[t] && FEED[t].total != null)
  ? FEED[t].total : ((FEED[t] && FEED[t].items || []).length);

// 한 번 실패하면 화면이 계속 반쪽으로 남는다. 잠깐 끊긴 것과 못 받는 것은
// 다르므로 한 번은 다시 시도한다(file:// 처럼 구조적으로 막힌 경우는 두 번 다
// 같은 이유로 실패하니 손해가 없다).
function _grab(url, retry){
  return fetch(url, {cache:'no-cache'})
    .then(r => { if(!r.ok) throw new Error('HTTP '+r.status); return r.json(); })
    .catch(e => { if(!retry) throw e;
      return new Promise(ok=>setTimeout(ok, 1500)).then(()=>_grab(url, 0)); });
}

function hydrate(){
  if(FULL) return;
  const jobs = ['news','patents'].filter(t=>FEED[t].rest).map(t =>
    _grab(FEED[t].rest.url, 1)
      .then(rows => { FEED[t].items = FEED[t].items.concat(rows); }));
  Promise.all(jobs).then(()=>{
    FULL = true; HYDFAIL = '';
    ['news','patents'].forEach(t=>{ delete FEED[t].rest; });
    // 항목에서 만든 캐시는 전부 버린다. 지연 로딩 전에는 items 가 바뀌는 일이
    // 없어서 한 번 만들고 끝이었지만, 이제는 남겨 두면 화면이 '다 받았다'고
    // 하면서 최근분으로만 계산한 값을 계속 보여준다.
    _shareCache = null;
    renderChips(); render();
  }).catch(e => {
    // file:// 로 열면 fetch 가 CORS 에 막힌다. 그때도 화면은 살아 있어야 하되,
    // 최근분만 보고 있다는 사실을 숨기면 안 된다.
    HYDFAIL = String(e && e.message || e);
    FULL = false; render();
  });
}

// 받침 유무로 조사를 고른다. 한글 음절은 0xAC00 부터 28개씩 한 묶음이고 그 안에서
// 종성이 0이면 받침이 없다. '기관 목록을 / 집중도를' 처럼 문구가 자연스러워야
// 안내가 기계가 뱉은 것처럼 읽히지 않는다.
function _batchim(w){
  const c = (w||'').charCodeAt((w||'').length-1);
  return c>=0xAC00 && c<=0xD7A3 ? ((c-0xAC00)%28)!==0 : false;
}
// 낱말에 조사를 붙여 돌려준다.
function _josa(w, withBatchim, without){
  return w + (_batchim(w)? withBatchim : without);
}
// 조사만 돌려준다 — 낱말이 이미 태그 안에 있어 뒤에 조사만 이어 붙일 때 쓴다.
// (_josa 를 그대로 쓰면 낱말이 한 번 더 찍힌다 — 실제로 그렇게 찍혔다.)
function _josaOnly(w, withBatchim, without){
  return _batchim(w)? withBatchim : without;
}

// 못 받은 이유를 짐작해서 쓰면 안 된다. file:// 은 브라우저가 구조적으로 막는
// 것이라 새로 고쳐도 소용없고, 네트워크 실패는 새로 고치면 된다 — 안내가 반대면
// 읽는 사람이 헛수고를 한다. 프로토콜로 갈라 말한다.
function hydWhy(){
  return location.protocol === 'file:'
    ? '이 페이지를 file:// 로 열면 브라우저가 데이터 요청을 막습니다 — '
      + '주소로 접속하면 정상 표시됩니다.'
    : '잠시 뒤 새로 고치면 다시 시도합니다.';
}

// 다 받기 전에는 '이 화면은 아직 전체가 아니다'를 항상 같은 문구로 알린다.
function loadingNote(what){
  if(FULL) return '';
  // .gnote 가 아니라 .gdesc 를 쓴다 — gnote 는 절 끝에 붙는 각주라 border-top 이
  // 있어서, 머리글 바로 밑에 놓으면 정체 모를 구분선과 빈 칸이 생긴다(실측).
  if(HYDFAIL) return '<p class="gdesc">아카이브 전체를 불러오지 못해 '
    + _josa(what,'을','를') + ' 표시하지 않습니다. ' + hydWhy() + '</p>';
  return '<p class="gdesc">아카이브를 불러오는 중입니다 — '
    + _josa(what,'은','는') + ' 전체를 받은 뒤 표시됩니다.</p>';
}
const $ = s => document.querySelector(s);
const esc = s => (s==null?'':String(s)).replace(/[&<>"]/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
// 링크 주소는 http(s) 만 허용한다. 제목·요약은 esc 로 막히지만 href 는 esc 를 통과해도
// javascript: 스킴이 그대로 남아 클릭 한 번으로 실행된다(RSS·OPS 응답은 외부 입력이다).
const safeUrl = u => { const s=String(u==null?'':u).trim(); return /^https?:\/\//i.test(s) ? s : ''; };

// ── 국기 ────────────────────────────────────────────────────────
// 자료에는 국기가 이모지로 들어 있다(🇰🇷). 그런데 윈도우는 국기 이모지를
// 그리지 않고 지역표시자 두 글자를 그대로 찍는다 — 화면이
// 'US8KR1143CN11JP11EU13' 이 됐다(실측). 그래서 표시 직전에 우리가 그린 것으로
// 바꾼다. 자료는 그대로 두므로 저장 형식도 옛 아카이브도 건드리지 않는다.
//
// 이모지에서 나라 코드를 되돌리는 방법: 국기 이모지는 지역표시자(U+1F1E6~FF)
// 두 개이고, 각각이 A~Z 한 글자에 대응한다. 표를 따로 두지 않아도 되고,
// 우리가 안 그린 나라도 두 글자 배지로 알아볼 수 있게 된다.
const FLAG_DRAWN = {KR:1, US:1, CN:1, JP:1, EU:1};
function flg(e){
  const cps = [...String(e==null?'':e)];
  const RI = c => { const v=c.codePointAt(0); return v>=0x1F1E6 && v<=0x1F1FF; };
  if(cps.length!==2 || !cps.every(RI)) return String(e==null?'':e);  // 🌐 등은 그대로
  const code = cps.map(c=>String.fromCharCode(c.codePointAt(0)-0x1F1E6+65)).join('');
  return FLAG_DRAWN[code]
    ? '<svg class="fl" viewBox="0 0 12 9" role="img" aria-label="'+code+'"><use href="#f-'+code+'"/></svg>'
    : '<span class="fl code">'+code+'</span>';
}
const LS_KEY = 'pnp_lastVisit';
const lastVisit = Number(localStorage.getItem(LS_KEY) || 0);


const LS_SAVE='pnp_saved', LS_READ='pnp_read';
let saved = new Set(JSON.parse(localStorage.getItem(LS_SAVE)||'[]'));
let read  = new Set(JSON.parse(localStorage.getItem(LS_READ)||'[]'));
let briefCollapsed = localStorage.getItem('pnp_briefClosed')==='1';
function persist(){ localStorage.setItem(LS_SAVE,JSON.stringify([...saved])); localStorage.setItem(LS_READ,JSON.stringify([...read])); }

const state = { tab:'home', view:'list', q:'', cats:new Set(), countries:new Set(),
  sort:'new', newonly:false, period:'all', source:'', savedOnly:false, unreadOnly:false, limit:PAGE };

function catMap(tab){ const m={}; FEED[tab].categories.forEach(c=>m[c.key]=c); return m; }
function itemTime(it){ const d = it.published || it.pub_date || it.date || it.week || ''; const t = Date.parse(d); return isNaN(t)?0:t; }
function isNew(it){ return lastVisit>0 && itemTime(it) > lastVisit; }

function latestNewsDate(){ const p=FEED.news.perDay; return p.length? p[p.length-1].x : ''; }
function shiftDate(d, delta){ const t=new Date(d+'T00:00:00'); if(isNaN(t)) return d;
  t.setDate(t.getDate()+delta); const p=n=>String(n).padStart(2,'0');
  return t.getFullYear()+'-'+p(t.getMonth()+1)+'-'+p(t.getDate()); }
function dayLabel(d){ const L=latestNewsDate(); if(d===L) return '오늘'; if(d===shiftDate(L,-1)) return '어제'; return d; }
function weekday(d){ const t=new Date(d+'T00:00:00'); return isNaN(t)?'':'일월화수목금토'[t.getDay()]; }
function inPeriod(it){
  if(state.period==='all') return true;
  if(state.tab==='news'){
    const L=latestNewsDate();
    if(state.period==='today') return it.date===L;
    if(state.period==='7d')  return it.date >= shiftDate(L,-6);
    if(state.period==='30d') return it.date >= shiftDate(L,-29);
    return it.date===state.period;          // 특정일(스파크라인 클릭)
  }
  return it.week===state.period;            // 특허: 특정 주(스파크라인 클릭)
}
function hl(text){
  const e=esc(text);
  const terms=state.q.toLowerCase().split(/\s+/).filter(Boolean)
    .map(t=>t.replace(/[.*+?^${}()|[\]\\]/g,'\\$&'));
  if(!terms.length) return e;
  try{ return e.replace(new RegExp('('+terms.join('|')+')','gi'),'<mark>$1</mark>'); }catch(_){ return e; }
}

function filtered(){
  const f = FEED[state.tab];
  const terms = state.q.toLowerCase().split(/\s+/).filter(Boolean);
  let out = f.items.filter(it=>{
    if(state.cats.size && !state.cats.has(it.category)) return false;
    if(state.tab==='patents' && state.countries.size && !state.countries.has(it.country)) return false;
    if(!inPeriod(it)) return false;
    if(state.tab==='news' && state.source && it.source!==state.source) return false;
    if(state.savedOnly && !saved.has(it.url)) return false;
    if(state.unreadOnly && read.has(it.url)) return false;
    if(state.newonly && !isNew(it)) return false;
    if(terms.length){
      const hay = (it.title+' '+(it.summary||'')+' '+(it.source||'')+' '+(it.assignee||'')+' '+(it.aName||'')+' '+(it.number||'')).toLowerCase();
      if(!terms.every(t=>hay.includes(t))) return false;
    }
    return true;
  });
  out.sort((a,b)=> state.sort==='new' ? itemTime(b)-itemTime(a) : itemTime(a)-itemTime(b));
  return out;
}

function fmtDate(iso){ if(!iso) return ''; const t=Date.parse(iso); if(isNaN(t)) return esc(iso);
  const d=new Date(t); const p=n=>String(n).padStart(2,'0');
  return d.getFullYear()+'-'+p(d.getMonth()+1)+'-'+p(d.getDate())+' '+p(d.getHours())+':'+p(d.getMinutes()); }
function fmtDay(iso){ if(!iso) return ''; const t=Date.parse(iso); if(isNaN(t)) return esc(iso);
  const d=new Date(t); const p=n=>String(n).padStart(2,'0'); return (d.getMonth()+1)+'-'+p(d.getDate()); }

function card(it, cm){
  const c = cm[it.category] || {emoji:'',name:it.category};
  const nw = isNew(it);
  const bits = [];
  if(state.tab==='news'){
    if(it.source) bits.push('<span class="src">'+esc(it.source)+'</span>');
    if(it.published) bits.push('<span class="mono">'+fmtDate(it.published)+'</span>');
  } else {
    // 출원인은 매트릭스·랭킹과 같은 정규화명(aName)으로 통일. 원문이 다르면 툴팁으로.
    if(it.aName) bits.push('<span class="src" title="'+esc(it.assignee||it.aName)+'">'
      + flg(it.aFlag) + ' ' + esc(it.aName)+'</span>');
    if(it.office) bits.push('<span class="off" title="공개 특허청">'+esc(it.office)+' 공보</span>');
    if(it.number) bits.push('<span class="num">'+esc(it.number)+'</span>');
    if(it.cpc && it.cpc.length) bits.push('<span class="cpc" title="CPC 분류(분야 판정 근거)">'
      + esc(it.cpc.join(' ')) + '</span>');
    if(it.pub_date) bits.push('<span class="mono">공개 '+esc(it.pub_date)+'</span>');
  }
  const mock = it.mock ? ' <span class="mockflag">샘플</span>' : '';
  const meta = bits.join(' <span aria-hidden="true">·</span> ');
  const sum = it.summary ? '<div class="sum">'+hl(it.summary)+'</div>' : '';
  const isS = saved.has(it.url), isR = read.has(it.url);
  return '<article class="card'+(nw?' isnew':'')+(isR?' isread':'')+'">'
    + '<button class="star'+(isS?' on':'')+'" data-save="'+esc(it.url)+'" aria-label="저장" title="저장">'+(isS?'★':'☆')+'</button>'
    + '<a class="t" href="'+esc(safeUrl(it.url))+'" target="_blank" rel="noopener" data-read="'+esc(it.url)+'">'+hl(it.title||'(제목 없음)')+'</a>'
    + '<div class="meta">'+meta+mock+'<span class="tag">'+(c.emoji||'')+' '+esc(c.name)+'</span></div>'
    + sum + (state.tab==='patents' ? patLinks(it) : '') + '</article>';
}

// 특허 카드 → 조회 창구. 공개번호 하나로 여러 곳을 열 수 있어 URL 은 저장하지 않고
// FEED.patents.links 의 템플릿({n})에 끼워 넣는다. office 가 지정된 링크는 그
// 특허청 공보에만 붙는다(예: KIPRIS 는 KR 공보에만).
function patLinks(it){
  const L = (FEED.patents.links)||[];
  if(!L.length || !it.number) return '';
  // 화면에는 저장된 표기를 그대로 보이되(국내는 13자리가 관행이다) 바깥 창구에는
  // 표준 문헌번호로 넘긴다. 둘이 다를 때만 nq 가 실려 온다 — 자세한 것은
  // site_render._docno 주석 참조.
  const n = encodeURIComponent(it.nq || it.number);
  const out = L.filter(l=> !l.office || l.office===it.office).map(l=>
    '<a class="xl" href="'+esc(safeUrl(String(l.url||'').replace('{n}', n)))+'"'
    + ' target="_blank" rel="noopener" title="'+esc(l.tip||'')+'">'+esc(l.label)+' ↗</a>');
  return out.length ? '<div class="xlinks">'+out.join('')+'</div>' : '';
}

function sparkline(series){
  if(!series.length) return '';
  const pts = series.slice(-40);
  const max = Math.max(1, ...pts.map(p=>p.y));
  const n = pts.length, gap = 2, W = 320, H = 38;
  const bw = Math.max(1, (W-(n-1)*gap)/n);
  let bars = '';
  pts.forEach((p,i)=>{
    const h = Math.max(1, Math.round(p.y/max*H));
    const x = i*(bw+gap), y = H-h;
    const sel = state.period===p.x ? ' class="sel"' : '';
    bars += '<rect'+sel+' data-x="'+esc(p.x)+'" x="'+x.toFixed(1)+'" y="'+y+'" width="'+bw.toFixed(1)+'" height="'+h
      +'" rx="1.5" fill="var(--spark)"><title>'+esc(p.x)+' · '+p.y+'건 (클릭해 필터)</title></rect>';
  });
  return '<svg viewBox="0 0 '+W+' '+H+'" preserveAspectRatio="none" role="img" aria-label="기간별 건수 추이(막대 클릭 시 그 기간만)">'+bars+'</svg>';
}

function briefHTML(){
  const b = FEED.brief;   // 최신 브리핑(홈 상단)
  if(!b || !(b.headline || (b.body&&b.body.length))) return '';
  // 이 브리핑은 **어제 하루치**를 아침에 정리한다. 오늘치는 아침 시점에 몇 시간
  // 분량뿐이라(실측 32건, 하루가 닫히면 26~72건) 하루가 온전히 지난 뒤 정리하는
  // 편이 고르다. 그래서 기준일이 어제인 것은 **정상**이고, 이틀 이상 벌어졌을
  // 때만 경고다. 전에는 하루 차이에 아무 말도 없어서, 화면은 '오늘의 브리핑'
  // 이라 하고 날짜는 어제인 채로 이유 없이 어긋나 보였다.
  let cover='', stale='';
  const L=latestNewsDate();
  if(b.date && L){ const dd=Math.round((Date.parse(L)-Date.parse(b.date))/86400000);
    if(dd===1) cover=' (어제 하루치)';
    else if(dd>=2) stale='<span class="bstale">· '+dd+'일 전 작성</span>'; }
  const body=(b.body||[]).map(p=>'<p>'+esc(p)+'</p>').join('');
  const pts=(b.points||[]).map(p=>'<div class="pt"><div class="pl">'+esc(p.emoji||'')+' '+esc(p.label||'')
    +'</div><div class="px">'+esc(p.text||'')+'</div></div>').join('');
  const foot=[];
  if(b.author) foot.push('✍️ '+esc(b.author)+(b.mode?' · '+esc(b.mode):''));
  if(b.basis) foot.push('<span class="sep">·</span> '+esc(b.basis));
  if(b.note) foot.push('<span class="sep">·</span> '+esc(b.note));
  return '<div class="brief'+(briefCollapsed?' collapsed':'')+'" id="sec-brief">'
    + '<div class="bhead"><span class="btag">🧭 오늘의 브리핑</span>'
    + (b.date?'<span class="bdate">'+esc(b.date)+' 뉴스'+cover+'</span>':'') + stale
    + '<button class="btoggle" id="briefToggle">'+(briefCollapsed?'펼치기 ▾':'접기 ▴')+'</button></div>'
    + (b.headline?'<h2>'+esc(b.headline)+'</h2>':'')
    + '<div class="bbody">'+body+'</div>'
    + (pts?'<div class="bpoints">'+pts+'</div>':'')
    + (foot.length?'<div class="bfoot">'+foot.join(' ')+'</div>':'')
    + '</div>';   // ← .brief 닫기. 없으면 홈의 후속 블록이 전부 이 카드 안에 중첩된다.
}

// ── 트렌드 인사이트: 결론 한 줄 · 30일 그래프 · 통합 표 ──────────────
//
// 전에는 패널 둘이 나란히 있었다(요즘 뜨는 키워드 · 이슈 흐름). 둘 다 **목록**
// 이라 '그래서 지금 뭐가 이슈냐' 를 읽는 사람이 머릿속에서 합쳐야 했고, 게다가
// 서로 따로 놀았다 — '원전 146' 칩과 '원전·SMR 137건 ▲1.1배' 줄이 같은 것을
// 두 번 말하는데 화면에서는 이어져 있지 않았다.
//
// 키워드를 분야에 붙일 수 있는지 실측했다(최근 7일 510건): 원전 90% · 전기요금
// 95% · 해상풍력 100% · 전력망 65% · 데이터센터 63% 가 한 분야로 모인다.
// 일반어만 흩어진다(전력 24% · AI 44%) → 그런 말은 대표 키워드에서 뺀다.
const KW_MIN_SHARE = 0.55;   // 한 분야에 이만큼은 모여야 그 분야의 말이라 할 수 있다
const KW_PER_CAT = 3;        // 분야마다 대표 키워드 몇 개
const LEAD_GAP = 1.35;       // 1위가 2위의 이 배 이상이어야 '지금은 X 다' 라고 말한다
const TCHART = {w:660, h:158, pad:{l:30, r:96, t:12, b:22}};
// 상위 네 분야의 선. 눈으로 고르지 않고 검사기를 돌려 맞춘 조합이다 —
// 밝은 화면·어두운 화면 양쪽에서 명도대·채도·색각이상 시 인접색 구분·대비를
// 모두 통과한다. 넷을 넘기지 않는 이유도 같다(그 이상은 구분되게 못 뽑는다).
const TCOL = ['#2F6FB5', '#D1562C', '#1F8A6D', '#A24BA0'];

// 분야별 대표 키워드. 그 말이 실제로 그 분야 기사에서 나왔을 때만 붙인다.
function catKeywords(){
  const D=(FEED.insights.window||{}).recentDays||7;
  const days=new Set([...new Set((FEED.news.items||[]).map(i=>i.date))].sort().slice(-D));
  const recent=(FEED.news.items||[]).filter(i=>days.has(i.date));
  const out={};
  (FEED.insights.trending||[]).forEach(k=>{
    const w=k.term; if(!w) return;
    const hit=recent.filter(a=>(a.title||'').indexOf(w)>=0);
    if(!hit.length) return;
    const c={}; hit.forEach(a=>{ c[a.category]=(c[a.category]||0)+1; });
    const top=Object.keys(c).sort((x,y)=>c[y]-c[x])[0];
    // 한 분야에 충분히 모이지 않는 일반어는 어느 분야의 말도 아니다.
    if(c[top]/hit.length < KW_MIN_SHARE) return;
    (out[top]||(out[top]=[])).push({term:w, n:c[top]});
  });
  Object.keys(out).forEach(k=>{ out[k]=out[k].sort((a,b)=>b.n-a.n).slice(0,KW_PER_CAT); });
  return out;
}

// 결론 한 줄. **격차를 보고 말을 바꾼다** — 1위가 뚜렷할 때만 '지금은 X 다' 라고
// 말한다. 어느 날 1·2위가 20%:19% 인데도 같은 문장을 쓰면 그건 거짓말이 된다.
// cmp 를 반드시 받는다. insights 의 comparable 은 '비교할 이전 기간이 아직
// 얇다' 는 뜻이고, 그때는 배율을 말하면 안 된다 — 아래 표는 그 값을 보는데
// 이 줄만 안 봐서, '증감은 표시하지 않습니다' 라고 적힌 화면에서 이 줄만
// '▲1.1배' 를 말하고 있었다(실측). 비중은 그대로 말한다(그건 지금 값이다).
function leadLineHTML(rows, cmp){
  if(!rows.length) return '';
  const a=rows[0], b=rows[1];
  const gap = b && b.share>0 ? a.share/b.share : 99;
  const pct=v=>Math.round(v*100)+'%';
  const mul=r=>(!cmp||r.ratio==null)? '' : (r.ratio>=1.10? ' ▲'+r.ratio.toFixed(1)+'배'
                                  : r.ratio<=0.90? ' ▼'+r.ratio.toFixed(1)+'배' : '');
  let head, sub;
  // named — 머리글이 이미 이름을 부른 분야들. 아래 '함께 오른 곳 / 식은 곳' 에서
  // 뺀다. 안 빼면 같은 분야를 두 번 말한다: 1위가 식는 날이면 '지금은 원전입니다
  // ▼0.8배 … 식은 곳 원전 0.8배' 가 되어 스스로 겹친다(실측으로 재현했다).
  // 전에는 '오른 곳' 만 1위를 빼고 '식은 곳' 은 안 뺐다 — 한쪽만 고친 비대칭이었다.
  const named = [];
  if(gap >= LEAD_GAP){
    head = a.emoji+' 지금은 <b>'+esc(a.name)+'</b> 입니다';
    sub  = '최근 뉴스의 '+pct(a.share)+''+mul(a);
    named.push(a);
  } else if(b){
    // 조사는 받침으로 고른다 — '가·나 이 함께' 는 틀리고 '나' 는 받침이 없어 '가' 다.
    head = a.emoji+b.emoji+' <b>'+esc(a.name)+'·'+esc(b.name)+'</b>'
         + _josaOnly(b.name,'이','가')+' 함께 이슈입니다';
    sub  = pct(a.share)+mul(a)+' · '+pct(b.share)+mul(b)+' — 1·2위가 가깝습니다';
    named.push(a, b);
  } else {
    head = '뚜렷한 쏠림이 없습니다'; sub = '';
  }
  const other = r => named.indexOf(r) < 0;
  const up=cmp? rows.filter(r=>r.ratio!=null && r.ratio>=1.10 && other(r)).slice(0,2) : [];
  const dn=cmp? rows.filter(r=>r.ratio!=null && r.ratio<=0.90 && other(r)).slice(0,2) : [];
  const side=[];
  if(up.length) side.push('<span class="lu">함께 오른 곳 '
    + up.map(r=>r.emoji+esc(r.name)+' '+r.ratio.toFixed(1)+'배').join(' · ')+'</span>');
  if(dn.length) side.push('<span class="ld">식은 곳 '
    + dn.map(r=>r.emoji+esc(r.name)+' '+r.ratio.toFixed(1)+'배').join(' · ')+'</span>');
  // 뉴스와 특허를 잇는 다리. 전에는 '오늘의 짝' 패널이 이 일을 했는데, 그 패널이
  // 보여 주던 특허 6건 중 3건이 바로 아래 '이번 주 공개 특허' 와 겹쳤고(실측)
  // 왼쪽 절반(오른 분야)은 이 줄이 이미 말하고 있었다 → 295px 짜리 패널 대신
  // 링크 하나로 남긴다.
  const T=FEED.trade||{}, NEWSMAP=T.newsMap||{};
  const back={}; Object.keys(NEWSMAP).forEach(pk=>
    (NEWSMAP[pk]||[]).forEach(nk=>{ back[nk]=pk; }));
  const cm={}; (FEED.patents.categories||[]).forEach(c=>cm[c.key]=c);
  const pk=back[a.key], pc=pk? cm[pk] : null;
  const bridge = pc
    ? '<button type="button" class="l2go" data-catpat="'+esc(pk)+'">'
      + pc.emoji+' '+esc(pc.name)+' 특허 보기 →</button>' : '';
  return '<div class="lead2"><div class="l2h">'+head
    + (sub? '<em>'+esc(sub)+'</em>':'')+'</div>'
    + ((side.length||bridge)? '<div class="l2s">'+side.join('')+bridge+'</div>':'')+'</div>';
}

// 30일 그래프. 세로축은 **비중**이다 — 일별 기사 수가 23~180건으로 널뛰어서
// 건수로 그리면 그 널뜀이 곡선을 다 먹는다(실측). 상위 네 분야만 선으로 긋고
// 이름을 선 끝에 직접 단다(범례를 따로 두면 눈이 왕복한다).
function trendChartHTML(rows){
  const top=rows.slice(0,4);
  if(top.length<2) return '';
  const days=[...new Set((FEED.news.items||[]).map(i=>i.date))].sort().slice(-TREND_DAYS);
  if(days.length<7) return '';
  const series=top.map(r=>catShareSeries([r.key], TREND_DAYS));
  const {w,h,pad}=TCHART;
  // 오른쪽 여백은 **이름 길이에 맞춰 잡는다.** 붙박이 96px 로 뒀더니 지금 이름들이
  // 657/660 으로 3px 만 남기고 겨우 들어가 있었고, 조금만 긴 이름이 상위 넷에
  // 들면 그대로 잘렸다(실측: '반도체 클러스터·메가프로젝트' 730/660).
  // SVG 는 글자 폭을 그리기 전에 못 재므로 어림한다 — 한글은 글자 크기(10.5px)를
  // 거의 그대로 먹고, 앞의 이모지와 사이 여백까지 더한다. 넉넉하게 잡아도
  // 손해는 그림이 조금 좁아지는 것뿐이다.
  const nameW = Math.max.apply(null, top.map(r=>(r.name||'').length)) * 10.5 + 30;
  const padR = Math.max(pad.r, Math.min(nameW, w * 0.42));
  const x0=pad.l, x1=w-padR, y0=pad.t, y1=h-pad.b;
  const max=Math.max(0.1, Math.max.apply(null, series.map(s=>Math.max.apply(null, s))));
  const px=i=>x0+(i/Math.max(1,days.length-1))*(x1-x0);
  const py=v=>y1-(v/max)*(y1-y0);
  let g='<svg class="tchart" viewBox="0 0 '+w+' '+h+'" role="img" aria-label="'
    + esc('최근 '+TREND_DAYS+'일 분야별 뉴스 비중 흐름: '
          + top.map(r=>r.name).join(', '))+'">';
  // 눈금 — 없으면 위아래 간격이 무슨 뜻인지 안 읽힌다.
  [0, max/2, max].forEach(v=>{
    g+='<line class="tg" x1="'+x0+'" y1="'+py(v).toFixed(1)+'" x2="'+x1
      + '" y2="'+py(v).toFixed(1)+'"/>'
      + '<text class="tgl" x="'+(x0-6)+'" y="'+(py(v)+3.5).toFixed(1)
      + '" text-anchor="end">'+Math.round(v*100)+'%</text>';
  });
  series.forEach((s,i)=>{
    const d=s.map((v,j)=>(j?'L':'M')+px(j).toFixed(1)+' '+py(v).toFixed(1)).join(' ');
    g+='<path class="tl" d="'+d+'" stroke="'+TCOL[i]+'"/>'
      + '<circle class="td" cx="'+px(s.length-1).toFixed(1)+'" cy="'+py(s[s.length-1]).toFixed(1)
      + '" r="3" fill="'+TCOL[i]+'"/>';
  });
  // 선 끝 이름은 **서로 밀어낸다**. 그냥 마지막 값 자리에 두면 곡선이 가까울 때
  // 글자가 겹쳐 둘 다 못 읽는다(실측: 송·변전 15%와 재생에너지 13%가 붙었다).
  // 위에서부터 훑으며 최소 간격을 못 지키면 아래로 민다. 선은 제자리에 있고
  // 이름만 옮기므로, 어느 이름이 어느 선인지는 색과 y 순서가 지킨다.
  const LBL_GAP = 13;
  const lab = series.map((s,i)=>({i, y:py(s[s.length-1])}))
    .sort((a,b)=>a.y-b.y);
  lab.forEach((L,k)=>{ if(k && L.y - lab[k-1].y < LBL_GAP) L.y = lab[k-1].y + LBL_GAP; });
  lab.forEach(L=>{
    g+='<text class="tll" x="'+(x1+7)+'" y="'+(L.y+3.5).toFixed(1)
      + '" fill="'+TCOL[L.i]+'">'+esc(top[L.i].emoji+' '+top[L.i].name)+'</text>';
  });
  g+='<text class="tgl" x="'+x0+'" y="'+(h-5)+'">'+esc(days[0])+'</text>'
   + '<text class="tgl" x="'+x1+'" y="'+(h-5)+'" text-anchor="end">'
   + esc(days[days.length-1])+'</text></svg>';
  return '<div class="tcwrap">'+g+'</div>';
}

function insightsHTML(){
  const ins = FEED.insights;
  if(!ins || !ins.asOf) return '';
  const w = ins.window || {recentDays:7, recentWeeks:4};

  // 비교할 '이전' 구간에 기사가 하나도 없으면(아카이브를 막 시작한 때) 증감을 숨긴다.
  // 안 그러면 prev 가 전부 0 이라 모든 항목이 전량 급증한 것처럼 보인다.
  const cmp = ins.comparable !== false;

  // 키워드 칩은 분야 줄 안으로 들어갔다(catKeywords). 여기 있던 옛 칩은 지운다 —
  // 거기 '▲' 옆의 수가 **생건수 차이**라, 하루평균으로는 늘었는데 총량으로는 준
  // 말들이 '▲-178' 처럼 찍히고 있었다(원전 146건/7일 vs 324건/21일). 부호가
  // 스스로 모순인 표시였다. 어제 이슈 흐름에서 고친 것과 같은 종류다.

  // 이슈 흐름 (카테고리 최근 vs 이전)
  // 화살표는 **건수 차이가 아니라 비중 변화**를 가리킨다.
  //
  // 여기서 크게 틀리고 있었다. 수집은 아카이브 전체와 중복을 제거하므로,
  // 아카이브가 커질수록 '새 기사'로 잡히는 수가 구조적으로 줄어든다 — 실측
  // (2026-08-26)에서 여덟 분야가 예외 없이 ▼ 였고, 그중 둘은 **실제로는
  // 늘어난** 분야였다:
  //     원전·SMR      화면 '135건 ▼160'  ·  실제 비중 26.7%, 1.12배 증가
  //     송·변전·전력망  화면 ' 79건 ▼72'   ·  실제 비중 15.6%, 1.28배 증가
  // 방향이 정해진 오차라 특히 나쁘다. insights 는 이미 이 이유로 share·ratio 를
  // 함께 계산해 두었는데(그 판단이 주석으로도 적혀 있다), 화면만 옛 수치를
  // 그리고 있었다.
  //
  // 건수는 그대로 둔다 — '얼마나 많이 나왔나' 는 사실이고 크기를 읽는 데 쓴다.
  // 바뀌는 것은 그 옆의 화살표뿐이다.
  //
  // ③ 30일 흐름. 아카이브가 35일 쌓였는데 홈에는 추이 그림이 없었다 — 이 패널이
  // 숫자 목록이라 '지금 얼마인가' 만 말하고 '어떻게 여기까지 왔나' 는 못 말한다.
  // 분야마다 한 줄씩 30일 비중 스파크라인을 단다(소형 다중). 한 그림에 열한 줄을
  // 겹쳐 그리지 않는 이유: 그러려면 색이 열한 개 필요한데 그만큼을 서로 구분되게
  // 뽑을 수 없다(색각 이상까지 보면 더 그렇다). 줄을 나누면 색이 아예 필요 없고,
  // 이름이 그림 바로 옆에 붙어 범례도 필요 없다.
  const KW = catKeywords();
  // 열한 분야를 다 세운다. 여덟 줄로 자르면 새로 연 세 분류가 영영 안 보인다.
  const ct = (ins.catTrend||[]);
  const ctHtml = ct.length ? ct.map(r=>{
    // 이전 기간에 그 분야가 아예 없었으면 배율이 정의되지 않는다(ratio=null).
    // 새로 만든 분류가 여기 해당한다 — 배율 대신 '새 분류' 라고 적는다.
    const fresh = cmp && r.ratio==null;
    const v = r.ratio;
    const cls = (!cmp||v==null) ? 'fl' : v>=1.10 ? 'up' : v<=0.90 ? 'dn' : 'fl';
    const sym = (!cmp||v==null) ? '' : v>=1.10 ? '▲' : v<=0.90 ? '▼' : '–';
    const dd = fresh ? ' 새 분류'
      : (!cmp||v==null) ? ''
      : (' '+sym+(sym==='–' ? '' : v.toFixed(1)+'배'));
    const tip = esc(r.name)+' 필터 — 최근 '+w.recentDays+'일 '+r.recent+'건, '
      + '뉴스에서 차지한 비중 '+Math.round((r.share||0)*100)+'%'
      + (fresh ? ' (이전 기간에는 없던 분류입니다)'
         : (cmp && v!=null) ? ' (이전 기간 대비 '+v.toFixed(2)+'배)' : '');
    // 스파크라인은 '그 분야가 뉴스에서 차지한 비중' 의 30일 흐름이다. 건수가
    // 아니라 비중이라야 위 화살표와 같은 것을 말한다(건수는 아카이브가 커질수록
    // 구조적으로 줄어든다 — 바로 위 주석의 그 이유다).
    const spk = sparkShare(catShareSeries([r.key], TREND_DAYS));
    // 그 분야를 끌고 있는 말. 칩을 누르면 검색으로 간다(옛 키워드 패널이 하던 일).
    // 숫자는 붙이지 않는다 — 방향은 바로 옆 분야 화살표가 이미 말한다.
    const kws = (KW[r.key]||[]).map(k=>
      '<span class="kw2" data-kw="'+esc(k.term)+'" title="'
      + esc(k.term+' — 최근 '+w.recentDays+'일 이 분야 기사 '+k.n+'건의 제목에 나옵니다')
      + '">'+esc(k.term)+'</span>').join('');
    return '<div class="row" data-cat="'+esc(r.key)+'" title="'+tip+'"><div class="nm">'
      +r.emoji+' '+esc(r.name)+'</div>'
      +'<div class="sp">'+spk+'</div>'
      +'<div class="d">'+Math.round((r.share||0)*100)+'<span class="n">%</span>'
      +'<span class="'+cls+'">'+esc(dd)+'</span></div>'
      +'<div class="kws">'+kws+'</div></div>';
  }).join('') : '<span class="iempty">–</span>';

  // 부제가 무엇을 재는지 말해야 한다. '이전 대비' 만 적어 두면 옆의 수와 견주는
  // 것으로 읽힌다 — 실제로 재는 것은 그 분야가 뉴스에서 차지한 **비중**이다.
  const sub = cmp
    ? '최근 '+w.recentDays+'일 각 분야의 <b>비중</b>과 이전 대비 배율입니다'
      + '(건수가 아닙니다). 선은 '+TREND_DAYS+'일 흐름, 오른쪽은 그 분야를 끄는 말 '
      + '· 분야를 누르면 필터, 말을 누르면 검색.'
    : '최근 '+w.recentDays+'일 뉴스에서 각 분야가 차지한 비중입니다 '
      + '(비교할 이전 기간이 아직 쌓이지 않아 증감은 표시하지 않습니다).';
  // 패널 둘을 하나로 접는다. 전에는 키워드와 이슈 흐름이 나란히 있었는데, 둘 다
  // 목록인 데다 서로 이어져 있지 않아 '그래서 지금 뭐가 이슈냐' 를 읽는 사람이
  // 머릿속에서 합쳐야 했다.
  return '<div class="insights"><div class="ipanel wide">'
    + '<h3>📈 뉴스 트렌드'
    + '<span class="morelink" data-go="news">뉴스 탭 →</span></h3>'
    + leadLineHTML(ct, cmp)
    + trendChartHTML(ct)
    + '<p class="isub">'+sub+'</p>'
    + '<div class="trend">'+ctHtml+'</div></div></div>';
}

// 이번 주 공개 특허 — 인사이트에 있던 것을 특허 섹션으로 옮겼다(질적 노출, 건수 아님).
// 홈 특허 섹션의 출원인×분야 요약(지역별 상위 3곳). 특허 탭 통계 뷰에는 현재 필터가
// 반영된 전체 매트릭스가 있고, 여기 것은 브리핑 서술을 수치로 받쳐 주는 용도다.

function patentPickPanelHTML(){
  const picks = patentPicks(8);
  if(!picks.length) return '';
  const pkHtml = picks.map(p=>{
    // 국기는 매트릭스와 같은 출원인 국적(aFlag)으로 통일 — 같은 화면에서 달라 보이지 않게.
    const who = p.aName ? '<span class="who">'+esc(p.aName)+'</span>' : '';
    return '<a class="pk" href="'+esc(safeUrl(p.url))+'" target="_blank" rel="noopener" title="'+esc(p.title)+'">'
      +'<span class="pf">'+(p.aFlag? flg(p.aFlag) : '📄')+'</span>'
      +'<span class="pt2">'+esc(p.title||'(제목 없음)')+who+'</span></a>';
  }).join('');
  return '<div class="homepanel"><h3>📄 이번 주 공개 특허</h3>'
    + '<p class="sub">최근 공개분 일부 · 무엇을 누가 출원했는지(건수 아님) · 누르면 원문.</p>'
    + '<div class="ppick">'+pkHtml+'</div></div>';
}

// 홈의 특허 브리핑. 머리글과 분야별 짚은 점은 펴 두고, **긴 본문은 접는다** —
// 실측 780px 이라 그것 하나가 홈 한 화면을 통째로 먹었다. 접는 쪽은 서술이고
// 펴 두는 쪽은 결론이라, 접어도 '무슨 일이 있었나' 는 그대로 읽힌다.
function patentBriefHomeHTML(){
  const b=FEED.patentBrief;
  if(!b || !(b.headline || (b.sections&&b.sections.length) || (b.body&&b.body.length))) return '';
  const body = (b.sections&&b.sections.length)
    ? b.sections.map(s=>'<div class="bsec"><div class="bsl">'+esc(s.label||'')+'</div>'
        + (s.paras||[]).map(p=>'<p>'+esc(p)+'</p>').join('') + '</div>').join('')
    : (b.body||[]).map(p=>'<p>'+esc(p)+'</p>').join('');
  const pts=(b.points||[]).map(p=>'<div class="pt"><div class="pl">'+esc(p.emoji||'')+' '+esc(p.label||'')
    +'</div><div class="px">'+esc(p.text||'')+'</div></div>').join('');
  return '<div class="homepanel pbhome" id="sec-pbrief"><h3>🔬 이번 주 특허 브리핑'
    + (b.week? '<span class="pbw">'+esc(b.week)+' 수집분</span>':'')
    + '<span class="morelink" data-go="patents">특허 탭 →</span></h3>'
    + (b.headline?'<h4 class="pbh">'+esc(b.headline)+'</h4>':'')
    + (pts?'<div class="bpoints">'+pts+'</div>':'')
    + (body? '<details class="pbfull"><summary>전문 읽기</summary>'
           + '<div class="bbody">'+body+'</div></details>' : '')
    + pastPatentBriefsHTML()
    + (b.basis?'<p class="pbfoot">'+esc(b.basis)+'</p>':'')
    + '</div>';
}

// 지난 특허 브리핑 — 특허 탭 배너를 없애면서 이리로 옮겼다(접어 둔다).
function pastPatentBriefsHTML(){
  const past=(FEED.patentBriefs||[]).slice(1);
  if(!past.length) return '';
  return '<details class="pbpast"><summary>지난 특허 브리핑 '+past.length+'건</summary>'
    + '<div class="timeline">'
    + past.map(x=>{
        const ps=(x.sections&&x.sections.length)? x.sections.flatMap(s=>s.paras||[]) : (x.body||[]);
        return '<div class="tl"><div class="tld">'+esc(x.week||'')+' 수집</div>'
          + '<div class="tlh">'+esc(x.headline||'(제목 없음)')+'</div>'
          + '<div class="tlb">'+ps.slice(0,2).map(p=>'<p>'+esc(p)+'</p>').join('')+'</div></div>';
      }).join('')
    + '</div></details>';
}

// 분야 설명을 **데이터에서** 만든다. 손으로 '전력 8대 분야(IPC)' 라고 적어 둔
// 문구가 분야를 Y04S 여섯으로 갈아 끼운 뒤에도 그대로 남아, 화면이 없는 분야
// 여덟 개를 말하고 있었다. 수를 세지 말고 목록을 세게 한다.
function catScopeText(){
  const cs=(FEED.patents.categories||[]);
  if(!cs.length) return '전력 분야';
  const inY=cs.filter(c=>!c.outside), out=cs.filter(c=>c.outside);
  return '전력 '+cs.length+'대 분야';
}
// 그 분야가 무엇인지 한 문장. '8대 분야' 같은 말만 두면 무엇을 세는지 모른다.
function catScopeNote(){
  const cs=(FEED.patents.categories||[]);
  if(!cs.length) return '';
  const inY=cs.filter(c=>!c.outside), out=cs.filter(c=>c.outside);
  // 결론부터 말한다: '6대 분야가 무엇인가' 가 물음이지, Y04S 의 정의가 물음이 아니다.
  return '전력 '+cs.length+'대 분야 = <b>CPC Y04S</b>(스마트그리드) '+inY.length+'개 갈래'
    + (out.length? ' + <b>'+out.map(c=>esc(c.name)).join('·')+'</b>' : '')+'입니다. '
    + inY.map(c=>esc(c.name)+'('+esc(c.cpc||'')+')').join(' · ') + '.'
    + (out.length? ' 원전은 Y04S 에 자리가 없어 Y02E 30 을 근거로 더했습니다.' : '')
    + ' 조회는 각 갈래에 대응하는 IPC 로 합니다(Y04S 는 CPC 전용이라 검색에 안 잡힙니다).';
}

function kpiHTML(){
  const n=FEED.news, p=FEED.patents;
  const nL = n.perDay.length? n.perDay[n.perDay.length-1] : {x:'-',y:0};
  // 특허 요약 지표(출원인 수·국적 내역·최다 출원인)는 통계 탭까지 안 들어가도 보이게 홈에.
  const ranked = p.items.length? _rankApplicants(p.items) : [];
  const regCnt={}; ranked.forEach(r=>{ regCnt[r.region]=(regCnt[r.region]||0)+1; });
  const split = regionSplit(ranked), unknown = split.unknown;
  // 통계 탭과 같은 이유로 여기서도 '모른다'를 숨기지 않는다(renderStats 주석 참고).
  const regChips = p.countries.map(rg=>regCnt[rg.code]
      ? '<span class="rgc">'+flg(rg.emoji)+regCnt[rg.code]+'</span>' : '').filter(Boolean).join('')
    + (split.other? '<span class="rgc">🌐'+split.other.toLocaleString()+'</span>' : '')
    + (unknown? '<span class="rgc unk">미상 '+unknown.toLocaleString()+'</span>' : '');
  const top = ranked[0];
  const lookback = p.lookbackDays||90;
  return '<div class="homekpi">'
    + tile('📰 뉴스 누적', n.items.length.toLocaleString()
        + '<small>건 · 최근 '+esc(nL.x)+' '+nL.y+'건</small>',
        '수집을 시작한 뒤 쌓인 전체 기사 수입니다. Google 뉴스 RSS 를 매일 조회해 새 기사만 더합니다.')
    + tile('📄 특허 누적', p.items.length.toLocaleString()
        + (p.pubRange? '<small>건 · 공개 '+esc(p.pubRange.from)+' ~ '+esc(p.pubRange.to)+'</small>':'건'),
        '아카이브에 저장된 특허 문헌 수입니다. 매주 최근 '+lookback+'일 공개분을 조회해 새 것만 더하며, '
        + '출원인당 저장 상한이 있어 대형 출원인은 전수가 아니라 표본입니다.')
    + tile('🏢 분석 출원인', (ranked.length||0).toLocaleString()
        + '<small class="rgs">'+regChips+'</small>',
        '최근 '+lookback+'일 공개분에 문헌이 있는 출원인이 '
        + (ranked.length||0).toLocaleString()+'곳입니다. 국기 옆 숫자는 국적별 출원인 수이고, '
        + '🌐 는 다섯 갈래 밖의 나라입니다. '
        + (unknown? originNote(unknown) : '국적은 모두 확인됐습니다.'))
    + tile('🏆 최다 출원인', top
        ? '<span class="topap">'+flg(top.flag)+' '+esc(top.name)+'</span><small>'
          + top.total.toLocaleString()+'건 · 최근 '+lookback+'일 국내·해외 공개</small>'
        : '—',
        '산출 근거: 최근 '+lookback+'일 사이 '+catScopeText()+'로 공개된 문헌이 가장 많은 곳입니다. '
        + '지식재산처 KIPRISplus 로 국내 공보와 해외(미국·유럽·일본·중국) 공보를 조회하며, 출원인을 '
        + '미리 정해 두지 않고 분야와 기간에 맞는 것을 모두 담습니다. 다만 관청마다 수록 범위·시점이 '
        + '달라 최근 공개분에는 시차가 있을 수 있고, 출원인 이름 표기가 다르면 따로 세어질 수 있습니다 '
        + '(같은 회사의 한글·영문·일본어 표기는 맞춰 두었습니다). 같은 발명이 여러 나라에 공개되면 '
        + '각각 세므로 특허 패밀리 수가 아니라 공개 문헌 수이고, 조회 조건이 전력 IPC 라 배터리처럼 '
        + '그 코드에 출원이 몰리는 분야가 크게 잡힙니다. 기업의 기술력이나 시장 점유율을 뜻하지 않습니다.')
    + '</div>'
    // 툴팁은 모바일에서 뜨지 않는다 → 특허 수치의 산출 근거는 한 줄로도 항상 보이게.
    // 출원인 범위(40곳)는 바로 옆 '분석 출원인' 타일과 ⓘ 툴팁에 있어 여기선 뺀다.
    + '<p class="kpinote"><b>국내 + 해외(미국·유럽·일본·중국) 공보</b>의 최근 '
    + lookback+'일 공개분입니다(KIPRISplus 기준, '+catScopeText()+'). '
    + '같은 발명이 여러 나라에 공개되면 각각 세는 <b>공개 문헌 수</b>이며 패밀리 수가 아닙니다.</p>'
    + '<p class="kpinote">'+catScopeNote()+'</p>';
}



// 브리핑은 하루 한 건씩 쌓인다 → 다 깔면 옆 브리핑 카드를 계속 밀어낸다(실측:
// 8건에 764px 로 이미 브리핑 카드 535px 보다 컸다. 한 달이면 2,500px).
// 최근 것만 깔고 나머지는 펼쳐 본다. 특허 브리핑 쪽은 이미 <details> 로 접혀 있다.
const TL_HEAD=5;
let tlAll=false;
function timelineHTML(){
  const past=(FEED.briefs||[]).slice(1);   // 최신은 위에 크게 노출, 나머지를 타임라인으로
  const shown = tlAll? past : past.slice(0, TL_HEAD);
  const item=b=>{
    const body=(b.body||[]).slice(0,2).map(p=>'<p>'+esc(p)+'</p>').join('');
    return '<div class="tl"><div class="tld">'+esc(b.date||'')+'</div>'
      + '<div class="tlh">'+esc(b.headline||'(제목 없음)')+'</div>'
      + '<div class="tlb">'+body+'</div></div>';
  };
  const more = past.length>TL_HEAD
    ? '<button type="button" class="stmore" data-more="tl">'
      + (tlAll? '접기' : '더 보기 ('+(past.length-TL_HEAD)+'건 남음)')+'</button>'
    : '';
  const inner = past.length ? shown.map(item).join('')
    : '<p class="homehint">지난 브리핑이 쌓이면 여기 타임라인으로 보여요(매주 갱신).</p>';
  return '<div class="homepanel" id="tlpanel"><h3>🗓️ 지난 브리핑</h3>'
    + '<p class="sub">제목을 누르면 요지가 펼쳐집니다.</p>'
    + '<div class="timeline">'+inner+'</div>'+more+'</div>';
}

// 홈 구성: 아카이브 현황(숫자) → 트렌드 인사이트(계산) → 뉴스 → 특허.
// 주제별로 묶어 읽히게 한다. 예전에는 뉴스 브리핑과 특허 브리핑 사이에 KPI·인사이트가
// 끼어 있어 어디까지가 뉴스 얘기인지 흐렸다.
// ① 오늘의 한 줄 — 스크롤 전에 '오늘 무슨 일이 있었나' 가 끝나게 한다.
//
// 뉴스·특허 브리핑의 headline 은 이미 데이터에 있는데, 각각 964px·857px 짜리
// 패널 **안에** 묻혀 있었다. 실측(1280×800): 첫 화면에 브리핑이 3px 걸쳤다 —
// 방문자는 스크롤해야 오늘 무슨 일인지 알 수 있었다.
//
// 두 줄만 위로 올린다. 본문을 옮기는 게 아니라 **머리글만** 올리고, 누르면 그
// 패널로 내려간다(같은 글을 두 곳에 두지 않는다).
function todayLineHTML(){
  const rows=[];
  const nb=FEED.brief, pb=FEED.patentBrief;
  const one=(icon, label, head, when, target)=>
    '<button type="button" class="tdl" data-jump="'+esc(target)+'">'
    + '<span class="tdi">'+icon+'</span>'
    + '<span class="tdt"><b>'+esc(label)+'</b>'+esc(head)+'</span>'
    + (when? '<span class="tdw">'+esc(when)+'</span>' : '')
    + '</button>';
  if(nb && nb.headline)
    rows.push(one('🧭','오늘의 브리핑 ', nb.headline, nb.date? nb.date+' 기준':'', 'sec-brief'));
  if(pb && pb.headline)
    rows.push(one('🔬','이번 주 특허 ', pb.headline, pb.week? pb.week+' 수집':'', 'sec-pbrief'));
  if(!rows.length) return '';
  return '<div class="today">'+rows.join('')+'</div>';
}

function renderHome(){
  const parts=[];
  parts.push(kpiHTML());
  const td=todayLineHTML(); if(td) parts.push(td);
  const ih=insightsHTML(); if(ih) parts.push('<div class="sec">🔎 트렌드 인사이트</div>'+ih);

  // 📰 뉴스 — 브리핑 전문 + 지난 브리핑 타임라인.
  // 지난 브리핑이 아직 없으면 2열 배치가 절반을 빈칸으로 남긴다 → 그땐 1열로.
  const bh=briefHTML();
  if(bh){
    const hasPast=(FEED.briefs||[]).length>1;
    parts.push('<div class="sec">📰 뉴스</div>'
      + '<div class="homebot'+(hasPast?'':' single')+'">'
      + bh + (hasPast? timelineHTML() : '') + '</div>');
  }

  // 📄 특허 — 브리핑 전문 + 이번 주 공개 특허를 두 칸으로, 그 아래 분야별 경쟁 구도를
  // 전체 폭으로. 브리핑이 서술한 내용을 바로 아래 수치가 받아 이어서 읽힌다.
  // 세부 기술은 패널을 따로 두지 않는다 — 같은 여덟 분야를 두 패널이 각각 한 번씩
  // 나열하면 한 분야의 그림을 맞추려고 위아래를 오가게 된다. 분야 카드 안에 넣었다.
  // 분야별 경쟁 구도(지도·요약·카드)는 통째로 경쟁·활용 탭에 있다. 홈은 '오늘 무슨
  // 일인가' 를 말하는 자리고, 분야를 하나씩 견주는 일은 '누구와 부딪히나' 를 묻는
  // 자리에서 필요하다 — 거기 창구가 바로 아래 있다.
  const pb=patentBriefHomeHTML(), pk=patentPickPanelHTML();
  const mx=FULL ? matrixHomeHTML(FEED.patents.items||[]) : '';
  // 해외 국내공개는 **맨 아래**다. 앞의 것들이 '우리 쪽에 무엇이 있나' 를 말하고
  // 이것은 '밖에서 무엇이 들어와 있나' 를 말한다 — 안을 다 보고 밖을 보는 차례다.
  const kr=FULL ? krHomeHTML(FEED.patents.items||[]) : '';
  if(pb||pk||mx||kr){
    parts.push('<div class="sec">📄 특허</div>'
      + ((pb||pk)? '<div class="homebot'+((pb&&pk)?'':' single')+'">'+(pb||'')+(pk||'')+'</div>' : '')
      + (mx? '<div class="homemx">'+mx+'</div>' : '')
      + (kr? '<div class="homemx">'+kr+'</div>' : ''));
  }
  $('#home').innerHTML = parts.join('');
}

function latestPatentWeek(){ const p=FEED.patents.perWeek; return p.length? p[p.length-1].x : ''; }
function patentPicks(n){
  const items = FEED.patents.items.slice();
  const w = latestPatentWeek();
  let pool = items.filter(it=>it.week===w);
  if(pool.length < n) pool = items;                 // 최신 주가 빈약하면 전체에서
  pool.sort((a,b)=> (Date.parse(b.pub_date||b.week)||0)-(Date.parse(a.pub_date||a.week)||0));
  return pool.slice(0, n);
}

// 특허가 '어느 기간 공개분'인지 한 줄로. 주별 버킷은 수집한 주라서 공개일과 다르다.

function patentScopeHTML(){
  const f=FEED.patents, r=f.pubRange;
  if(!r) return '';
  return '<p class="scope">📄 <b>공개일 '+esc(r.from)+' ~ '+esc(r.to)+'</b> 특허 '
    // 수집 축이 바뀌었다(출원인 목록 → 분야+기간 전수). 문구가 옛 방식 그대로면
    // 읽는 사람은 아직 65곳만 보는 줄 안다.
    + count('patents').toLocaleString()+'건 · <b>분야(IPC)+기간</b>으로 조회해 '
    + '조건에 맞는 것을 모두 담습니다(1회 범위: 최근 '
    + (f.lookbackDays||90)+'일, 국내 + 미국·유럽·일본·중국). '
    + '새로 공개된 것만 누적.</p>';
}

function renderOverview(){
  const f = FEED[state.tab];
  const total = f.items.length;
  const series = state.tab==='news' ? f.perDay : f.perWeek;
  const periods = series.length;
  const latest = series.length ? series[series.length-1] : {x:'-',y:0};
  const newCount = f.items.filter(isNew).length;
  const news = state.tab==='news';
  const ov = $('#overview');
  // 특허는 '최근 주(수집한 주)' 대신 공개일 범위를 보여준다 — 그쪽이 사용자가 궁금한 값.
  const thirdTile = news
    ? tile('최근 일', latest.y+'<small>건 · '+esc(latest.x)+'</small>')
    : tile('공개일 범위', (f.pubRange
        ? '<small>'+esc(f.pubRange.from)+' ~ '+esc(f.pubRange.to)+'</small>' : '—'));
  ov.innerHTML =
    tile('누적 '+(news?'기사':'특허'), total.toLocaleString())
    + tile('수집 '+(news?'일':'주'), periods)
    + thirdTile
    + (lastVisit
        ? tile('✨ 새 항목', newCount + '<small>지난 방문 이후</small>')
        : tile(news? '최근 7일' : '최근 4주',
            f_recentCount(f, news) + '<small>건</small>'))
    + (periods>1
        ? '<div class="tile spark sparkwrap"><div class="k">'+(news?'일별':'주별')+' 수집 추이 <b>('+periods+')</b></div>'
          + sparkline(series) + '</div>'
        : '');
}
// 최근 구간 합계(뉴스=7일, 특허=4주) — '오늘 열람' 자리를 채울 실질 지표
function f_recentCount(f, news){
  const series = news? f.perDay : f.perWeek;
  return series.slice(news? -7 : -4).reduce((a,b)=>a+b.y,0).toLocaleString();
}
// tip 을 주면 라벨 옆에 ⓘ 를 달고 마우스를 올렸을 때 산출 근거를 보여준다.
function tile(k,v,tip){
  const q = tip? '<span class="tq" title="'+esc(tip)+'">ⓘ</span>' : '';
  return '<div class="tile"><div class="k">'+k+q+'</div><div class="v mono">'+v+'</div></div>';
}

function renderChips(){
  if(state.tab==='home'||state.tab==='guide'){ $('#catChips').innerHTML=''; const cc=$('#countryChips'); cc.hidden=true; cc.innerHTML=''; return; }
  const f = FEED[state.tab], cm = {};
  f.items.forEach(it=> cm[it.category]=(cm[it.category]||0)+1);
  $('#catChips').innerHTML = f.categories.filter(c=>cm[c.key]).map(c=>
    '<button class="f" data-cat="'+c.key+'" aria-pressed="'+state.cats.has(c.key)+'">'
    + c.emoji+' '+esc(c.name)+'<span class="n">'+(cm[c.key]||0)+'</span></button>').join('');
  const cc = $('#countryChips');
  if(state.tab==='patents'){
    cc.hidden = false;
    const cnt = {}; f.items.forEach(it=> cnt[it.country]=(cnt[it.country]||0)+1);
    cc.innerHTML = f.countries.filter(c=>cnt[c.code]).map(c=>
      '<button class="f co" data-country="'+c.code+'" aria-pressed="'+state.countries.has(c.code)+'">'
      + c.emoji+' '+esc(c.name)+'<span class="n">'+(cnt[c.code]||0)+'</span></button>').join('');
  } else { cc.hidden = true; cc.innerHTML=''; }
}

function renderPeriodBar(){
  const pb=$('#periodBar');
  if(state.tab==='patents'){
    // 특허 탭엔 기간 프리셋이 없지만, 스파크라인 클릭으로 특정 주가 걸릴 수 있다 →
    // 해제할 수 있게 그때만 칩을 보여준다.
    if(state.period==='all'){ pb.hidden=true; pb.innerHTML=''; return; }
    pb.hidden=false;
    pb.innerHTML='<button class="f" data-period="all">전체 기간</button>'
      + '<button class="f" data-period="'+esc(state.period)+'" aria-pressed="true">📅 '
      + esc(state.period)+' 주 ✕</button>';
    return;
  }
  if(state.tab!=='news'){ pb.hidden=true; pb.innerHTML=''; return; }
  pb.hidden=false;
  const opts=[['all','전체'],['today','오늘'],['7d','최근 7일'],['30d','최근 30일']];
  let html=opts.map(o=>'<button class="f" data-period="'+o[0]+'" aria-pressed="'+(state.period===o[0])+'">'+o[1]+'</button>').join('');
  if(state.period!=='all' && !opts.some(o=>o[0]===state.period))
    html+='<button class="f" data-period="'+esc(state.period)+'" aria-pressed="true">📅 '+esc(state.period)+' ✕</button>';
  pb.innerHTML=html;
}
function syncSearchPlaceholder(){
  const q=$('#q'); if(!q) return;
  q.placeholder = state.tab==='patents'
    ? '특허 제목·출원인·공개번호·CPC 검색'
    : '뉴스 제목·요약·언론사 검색';
}

function renderSource(){
  const sel=$('#source');
  if(state.tab!=='news'){ sel.hidden=true; return; }
  sel.hidden=false;
  if(!sel.dataset.built){
    const srcs=[...new Set(FEED.news.items.map(n=>n.source).filter(Boolean))].sort((a,b)=>a.localeCompare(b));
    sel.innerHTML='<option value="">전체 출처</option>'+srcs.map(s=>'<option value="'+esc(s)+'">'+esc(s)+'</option>').join('');
    sel.dataset.built='1';
  }
  sel.value=state.source;
}

// ── 거래 판단 참고: 뉴스 비중 변화 × 권리 집중도 ──────────────────────
// 두 축을 겹쳐 봐야 답이 나온다. 관심이 느는데 권리가 소수에 몰렸으면 회피설계·
// 라이선스가 먼저고, 관심이 느는데 권리가 흩어져 있으면 자체 출원 여지가 있다.
// 예측은 하지 않는다 — 지금 어떤 모양인지까지만 보인다(FEED.trade.note).
function tradeRows(){
  const T=FEED.trade||{}, MAP=T.map||{};
  const conc=concentration(FEED.patents.items||[]);
  const ct={}; (FEED.insights.catTrend||[]).forEach(c=>ct[c.key]=c);
  // 해외 출원인이 국내(KR)에 공개한 건 — 국내에서 실제로 부딪힐 수 있는 권리다.
  // 국기와 이름은 따로 담는다. 예전엔 flg(국기)+이름 을 한 문자열로 붙여 뒀는데,
  // 국기를 이모지에서 직접 그린 SVG 로 바꾼 뒤 이 문자열이 esc() 를 지나며
  // '<svg class="fl" …>' 가 글자 그대로 화면에 찍혔다(칩과 판정 문장 양쪽에서).
  // 그리는 시점에 flg() 를 부르고 이름만 esc() 한다.
  // 건수까지 센다. 예전에는 이름만 모아 가나다순으로 늘어놓았는데, 국내 공보
  // 출원인의 국적을 서지상세로 바로잡은 뒤 이 목록이 8곳에서 **132곳**이 됐다
  // (전에는 대부분 KR 로 잘못 적혀 있어 아예 들어오지 않았다). 132개 이름을
  // 가나다순으로 늘어놓으면 아무것도 읽히지 않는다 → 많이 낸 곳부터 세운다.
  const kr={};
  (FEED.patents.items||[]).forEach(it=>{ if(it.office!=='KR'||it.aCountry==='KR') return;
    const m=kr[it.category]||(kr[it.category]=new Map());
    const e=m.get(it.aName);
    if(e) e.n++; else m.set(it.aName, {flag:it.aFlag, n:1}); });
  const cmp=FEED.insights.comparable;
  // 분야는 전부 싣는다. 뉴스 쪽에 짝이 없는 분야(계량·스마트그리드)는 뉴스 칸만
  // 비우고 권리 구조는 그대로 보인다 — 한전·State Grid·LS일렉트릭이 있는 분야라
  // '뉴스 분류가 없다' 는 이유로 통째로 빼면 화면이 사실보다 좁아진다.
  // 한 특허 분야가 여러 뉴스 분류에 걸린다(Y04S 10 = 송·변전 + 재생에너지 +
  // 전력설비). catTrend 의 share·prevShare 는 **같은 분모**를 쓰는 비중이라
  // 그대로 더할 수 있다 — 배율은 더한 비중끼리 나눈다.
  const NEWSMAP=T.newsMap||{};
  return conc.map(r=>{
    const keys = NEWSMAP[r.cat.key]||[];
    const paired = keys.length>0;
    const rows = keys.map(k=>ct[k]).filter(Boolean);
    const share = rows.reduce((s,c)=>s+(c.share||0), 0);
    const prev  = rows.reduce((s,c)=>s+(c.prevShare||0), 0);
    const c = rows.length? {share, prevShare:prev,
                            recent: rows.reduce((s,x)=>s+(x.recent||0),0)} : null;
    const ratio = (paired && prev>0) ? share/prev : null;
    const dir = (!cmp||ratio==null) ? 'flat'
      : ratio>=1.10 ? 'up' : ratio<=0.90 ? 'down' : 'flat';
    const lv = r.n<CONC_MIN ? null : concLevel(r.neff);
    // 짝이 **있는데 아직 0건**인 상태를 따로 알린다. 뉴스 분류를 새로 만들면
    // 다음 수집 전까지는 기사가 없는데, 그대로 '0%' 로 찍으면 '뉴스가 이 기술을
    // 안 다룬다' 로 읽힌다 — 사실은 '우리가 아직 안 모았다' 다.
    return {r, news:c||null, ratio, dir, lv, paired, newsKeys:keys,
            fresh: paired && rows.length===0,
            kr:[...(kr[r.cat.key]||new Map())].map(([name,v])=>({name, flag:v.flag, n:v.n}))
                 .sort((a,b)=> b.n-a.n || a.name.localeCompare(b.name)),
            note: MAP[r.cat.key]||''};
  });
}

// 그림 읽는 법. 축 이름과 범례만으로는 '실질 경쟁자 수'가 무엇인지 알 수 없다.
// 접어 두되 첫 화면에서는 펼쳐 둔다 — 접어 놓으면 아무도 열지 않고, 늘 펼쳐 두면
// 이미 아는 사람에게는 표가 그만큼 밀린다. 문구는 ip_guide 에 있다(한 곳에서 고친다).
function quadGuideHTML(){
  const T=FEED.trade||{}, G=T.quadGuide||[];
  if(!G.length) return '';
  return '<details class="qguide" open><summary>이 그림 읽는 법</summary>'
    + '<p class="qgl">'+esc(T.quadLead||'')+'</p><dl>'
    + G.map(g=>'<dt>'+esc(g[0])+'</dt><dd>'+esc(g[1])+'</dd>').join('')
    + '</dl></details>';
}

// 분야 지도. x=뉴스 비중 배율(log2, 가운데가 '변화 없음'), y=실질 경쟁자 수(위가 집중),
// 원 크기=그 분야 추정 규모. 값은 전부 아래 표에도 있다(그림에만 두지 않는다).
// 두 축 모두 범위를 고정해 둔다 — 주마다 축이 움직이면 지난주 그림과 겹쳐 볼 수 없다.
// 다만 고정값은 데이터를 보고 정해야 한다. 옛 값(y=2~12곳 선형, x=±1.6옥타브)은
// 큐레이션 65곳 시절에 맞춘 것이라, 전수 수집 뒤 실측하니 y 는 여덟 중 일곱이 바닥에
// 눌어붙고(실제 10~239곳) x 는 여섯이 가운데 5분의 1 폭에 뭉쳤다.
//   y : 10곳과 239곳을 한 화면에 두려면 로그다. 8~256곳(5옥타브) 고정.
//   x : 주 단위 비중 변화는 대개 ±40% 안이다 → ±0.6옥타브(0.66~1.5배) 고정.
// 범위를 벗어난 분야는 가장자리에 붙이고 점선 테두리로 '축 밖' 임을 밝힌다(값은
// 툴팁과 아래 표에 그대로 있다).
// 사분면 설명은 그림판 '밖'(위·아래 여백)에 둔다. 안쪽 네 귀퉁이에 두었더니 축
// 끝에 붙은 분야의 원이 글자를 덮어 '관심 ↓ · 권리 분산 — 관망' 이 반쯤 잘렸다.
const QW=680, QH=356, QPAD={l:48,r:18,t:32,b:56}, QCLAMP=0.6;
const NEF=[8,256], NEG=[16,128];
function quadChartHTML(rows){
  const pts=rows.filter(d=>d.lv && d.ratio!=null);
  if(pts.length<3) return '';
  const x0=QPAD.l, x1=QW-QPAD.r, y0=QPAD.t, y1=QH-QPAD.b;
  const lg=v=>Math.max(-QCLAMP, Math.min(QCLAMP, Math.log2(v)));
  const px=v=>x0+(lg(v)+QCLAMP)/(QCLAMP*2)*(x1-x0);
  const ny0=Math.log2(NEF[0]), nyr=Math.log2(NEF[1])-ny0;
  const py=v=>y0+(Math.log2(Math.max(NEF[0],Math.min(NEF[1],v)))-ny0)/nyr*(y1-y0);
  const maxTot=Math.max(...pts.map(d=>d.r.tot));
  const pr=t=>9+Math.sqrt(t/maxTot)*13;
  const col=lv=>lv==='hi'?'var(--q3)':lv==='mid'?'var(--q2)':'var(--q1)';
  const xm=px(1), ym=py(CONC_MID);
  let s='<svg class="qchart" viewBox="0 0 '+QW+' '+QH+'" role="img" '
    + 'aria-label="분야별 뉴스 비중 변화와 권리 집중도 지도">';
  // 사분면 안내(실선 헤어라인)와 로그 눈금선(점선, 더 옅게). 로그 축은 눈금이 없으면
  // 위아래 간격이 무슨 뜻인지 읽히지 않는다.
  s+='<line class="ax" x1="'+xm+'" y1="'+y0+'" x2="'+xm+'" y2="'+y1+'"/>'
   + NEG.map(v=>'<line class="qg" x1="'+x0+'" y1="'+py(v).toFixed(1)+'" x2="'+x1
       + '" y2="'+py(v).toFixed(1)+'"/>').join('')
   + '<line class="ax" x1="'+x0+'" y1="'+ym+'" x2="'+x1+'" y2="'+ym+'"/>'
   + '<line class="ax" x1="'+x0+'" y1="'+y1+'" x2="'+x1+'" y2="'+y1+'"/>';
  const zone=(tx,ty,t,anc)=>'<text class="qz" x="'+tx+'" y="'+ty+'" text-anchor="'+anc+'">'+t+'</text>';
  s+=zone(x0,y0-9,'관심 ↓ · 권리 집중 — 성숙·고착','start')
   + zone(x1,y0-9,'관심 ↑ · 권리 집중 — 회피설계·라이선스 먼저','end')
   + zone(x0,y1+18,'관심 ↓ · 권리 분산 — 관망','start')
   + zone(x1,y1+18,'관심 ↑ · 권리 분산 — 자체 출원 여지','end');
  // 축 이름·눈금. y 는 숫자 눈금만 왼쪽 여백에 두고, 축이 무엇인지는 세로 캡션이 진다
  // ('집중'·'분산' 두 글자만으로는 값이 몇인지 알 수 없었다).
  const ytick=(v,cls)=>'<text class="qa'+(cls?' '+cls:'')+'" x="'+(x0-6)+'" y="'
    + (py(v)+3.5).toFixed(1)+'" text-anchor="end">'+v+'</text>';
  s+='<text class="qa" x="'+((x0+x1)/2)+'" y="'+(QH-8)+'" text-anchor="middle">'
   + '← 뉴스 비중 줄어듦   |   늘어남 →</text>'
   + '<text class="qa" transform="translate(11,'+((y0+y1)/2)+') rotate(-90)" '
   + 'text-anchor="middle">실질 경쟁자 수(곳) — 위가 집중</text>'
   + ytick(NEF[0]) + NEG.map(v=>ytick(v)).join('') + ytick(NEF[1]) + ytick(CONC_MID,'qm');
  // 라벨은 원마다 붙는다(값이 아니라 이름이므로 전부 붙어도 된다). 다만 점이 몰리면
  // 라벨이 옆 원을 덮는다 → 오른쪽·왼쪽·위·아래 순으로 비어 있는 자리를 찾아 놓고,
  // 넷 다 막히면 원 위쪽에 얹는다. 글자 폭은 한글 기준으로 어림한다(측정 API 없이 그린다).
  // 축 밖으로 벗어난 분야는 원이 여백으로 반쯤 튀어나가지 않도록 반지름만큼 들여
  // 붙인다(눈금 숫자를 덮는다). 벗어났다는 사실은 점선 테두리로 밝힌다.
  const nodes=pts.map(d=>{
    const r=pr(d.r.tot);
    const out = Math.abs(Math.log2(d.ratio))>QCLAMP || d.r.neff<NEF[0] || d.r.neff>NEF[1];
    return {d, cx:Math.max(x0+r, Math.min(x1-r, px(d.ratio))),
            cy:Math.max(y0+r, Math.min(y1-r, py(d.r.neff))), r,
            out, t:d.r.cat.name};
  });
  const tw=t=>{ let w=0; for(const ch of t) w += ch.charCodeAt(0)>0x1100 ? 10.5 : 6; return w; };
  const hitCircle=(bx,by,bw,bh,c)=>{
    const nx=Math.max(bx,Math.min(c.cx,bx+bw)), ny=Math.max(by,Math.min(c.cy,by+bh));
    return (nx-c.cx)**2 + (ny-c.cy)**2 < (c.r+2)**2; };
  const hitBox=(a,b)=> a.x < b.x+b.w && b.x < a.x+a.w && a.y < b.y+b.h && b.y < a.y+a.h;
  const placed=[];
  nodes.forEach(n=>{
    const w=tw(n.t), h=13;
    const cand=[
      {x:n.cx+n.r+6,      y:n.cy-h/2, anchor:'start'},
      {x:n.cx-n.r-6-w,    y:n.cy-h/2, anchor:'end'},
      {x:n.cx-w/2,        y:n.cy-n.r-6-h, anchor:'middle'},
      {x:n.cx-w/2,        y:n.cy+n.r+6, anchor:'middle'},
      // 넷 다 막히는 경우가 실제로 나온다 — 실질 경쟁자 수가 거의 같은 두 분야가
      // (재생에너지 43.0곳, 원전 42.0곳) 나란히 서면 위쪽 자리를 서로 뺏는다.
      // 예전엔 그때 그냥 '위' 로 겹쳐 찍어 두 이름이 붙어 버렸다 → 한 줄씩 더 물려 본다.
      {x:n.cx-w/2,        y:n.cy-n.r-6-h*2-4, anchor:'middle'},
      {x:n.cx-w/2,        y:n.cy+n.r+6+h+4, anchor:'middle'},
      {x:n.cx+n.r+6,      y:n.cy-h/2-h-4, anchor:'start'},
      {x:n.cx-n.r-6-w,    y:n.cy-h/2-h-4, anchor:'end'},
    ];
    let pick=cand.find(c=> c.x>=x0-40 && c.x+w<=x1+16 && c.y>=y0 && c.y+h<=y1
      && !nodes.some(o=> o!==n && hitCircle(c.x,c.y,w,h,o))
      && !placed.some(p=> hitBox({x:c.x,y:c.y,w,h}, p))) || cand[2];
    placed.push({x:pick.x, y:pick.y, w, h});
    n.lx = pick.anchor==='start'? pick.x : pick.anchor==='end'? pick.x+w : pick.x+w/2;
    n.ly = pick.y+h-3; n.anchor=pick.anchor;
  });
  nodes.forEach(n=>{
    const d=n.d;
    const tip=d.r.cat.name+' — 뉴스 비중 '+Math.round(d.ratio*100)+'%(이전=100), '
      + '실질 경쟁자 '+d.r.neff.toFixed(1)+'곳, 상위 3곳 '+Math.round(d.r.cr3*100)+'%'
      + (n.out? ' (축 범위를 벗어나 가장자리에 표시)' : '');
    s+='<g><title>'+esc(tip)+'</title>'
      + (n.out? '<circle class="qoff" cx="'+n.cx.toFixed(1)+'" cy="'+n.cy.toFixed(1)
          + '" r="'+(n.r+3.5).toFixed(1)+'"/>' : '')
      + '<circle class="dot" cx="'+n.cx.toFixed(1)+'" cy="'+n.cy.toFixed(1)+'" r="'+n.r.toFixed(1)
      + '" fill="'+col(d.lv)+'"/>'
      + '<circle class="hit" cx="'+n.cx.toFixed(1)+'" cy="'+n.cy.toFixed(1)+'" r="'+Math.max(n.r,13)+'"/>'
      + '<text class="ql" x="'+n.lx.toFixed(1)+'" y="'+n.ly.toFixed(1)+'" text-anchor="'
      + n.anchor+'">'+esc(n.t)+'</text></g>';
  });
  return '<div class="qwrap">'+s+'</svg></div>'
    + '<div class="qlegend"><span>권리 집중도(분야끼리 견준 상대 등급)</span>'
    + '<span class="s3"><i></i>'+CONC_NAME.hi+'</span>'
    + '<span class="s2"><i></i>'+CONC_NAME.mid+'</span>'
    + '<span class="s1"><i></i>'+CONC_NAME.lo+'</span>'
    + '<span>원 크기 = 그 분야 추정 공개 규모</span>'
    + (nodes.some(n=>n.out)? '<span class="soff"><i></i>축 범위 밖 — 가장자리에 붙였습니다</span>' : '')
    + '</div>'
    + quadGuideHTML();
}

// ② 뉴스 비중 추이. '비중 98%' 는 한 시점의 값이라 늘고 있는지 줄고 있는지가
// 안 보인다 → 분야별 일자 비중을 작은 선으로 붙인다. 새 수집 없이 지금 피드
// (기사마다 date·category)로 계산된다. 세로축은 그 분야의 최대치 기준이라
// 분야끼리 높이를 비교하는 용도가 아니다 — 오르내림만 본다.
// 국내 권리 줄. 이름은 **많이 낸 세 곳까지만** 세운다.
// 전에는 전부 늘어놓았다. 국내 공보 출원인의 국적을 서지상세로 바로잡자
// 재생에너지·저장이 8곳에서 **132곳**이 됐고(전에는 대부분 KR 로 잘못 적혀 있어
// 아예 들어오지 않았다), 카드 한 장이 이름 목록으로 덮였다. 게다가 그 목록은
// '서로 다른 출원인' 이라 1건짜리와 100건짜리가 같은 크기로 놓여, 필립모리스
// (전자담배 배터리 1건)가 CATL 옆에 나란히 섰다. 곳 수는 머리에 그대로 밝히고,
// 이름은 건수 순 세 곳만, 나머지는 툴팁으로 돌린다.
const KR_HEAD=3;
function krLineHTML(kr){
  if(!kr || !kr.length) return '';
  const top=kr.slice(0, KR_HEAD), more=kr.length-top.length;
  return '<div class="tkr"><b>🇰🇷 국내 권리 '+kr.length+'곳</b> '
    + top.map(k=>flg(k.flag)+' '+esc(k.name)
        + '<span class="krn">'+k.n+'건</span>').join(' <span aria-hidden="true">·</span> ')
    + (more>0? ' <span class="krmore" title="'
        + esc(kr.slice(KR_HEAD, KR_HEAD+25).map(k=>k.name+' '+k.n+'건').join(' · ')
              + (kr.length>KR_HEAD+25? ' … 외 '+(kr.length-KR_HEAD-25)+'곳' : ''))
        + '">외 '+more+'곳</span>' : '')
    + '</div>';
}
const STAOWN_HEAD=6;        // 처음에 보일 국유특허 건수
let staownAll=false;
let _shareCache=null;
// 뉴스 분류 **여러 개**의 비중 합. 한 특허 분야가 여러 뉴스 분류에 걸리므로
// 키 하나만 받으면 Y04S 10 의 흐름이 송·변전 한 갈래로만 그려진다.
function catShareSeries(keys, days){
  keys = Array.isArray(keys)? keys : (keys? [keys] : []);
  if(!keys.length) return [];
  if(!_shareCache){
    const byDay={}, tot={};
    (FEED.news.items||[]).forEach(it=>{ const d=it.date; if(!d) return;
      tot[d]=(tot[d]||0)+1;
      (byDay[d]||(byDay[d]={}))[it.category]=((byDay[d]||{})[it.category]||0)+1; });
    _shareCache={byDay, tot, days:Object.keys(tot).sort()};
  }
  const ds=_shareCache.days.slice(-days);
  return ds.map(d=>{ const t=_shareCache.tot[d]||0; if(!t) return 0;
    const m=_shareCache.byDay[d]||{};
    return keys.reduce((s,k)=>s+(m[k]||0), 0)/t; });
}
function sparkShare(vals){
  if(vals.length<3) return '';
  const W=64, H=18, max=Math.max(...vals, 0.01);
  const pt=(v,i)=>[ (i/(vals.length-1))*(W-3)+1.5, H-1.5-(v/max)*(H-3) ];
  const pts=vals.map(pt);
  const d=pts.map((p,i)=>(i?'L':'M')+p[0].toFixed(1)+' '+p[1].toFixed(1)).join(' ');
  const last=pts[pts.length-1];
  return '<svg class="spk" viewBox="0 0 '+W+' '+H+'" aria-hidden="true">'
    + '<path d="'+d+'"/><circle cx="'+last[0].toFixed(1)+'" cy="'+last[1].toFixed(1)
    + '" r="2"/></svg>';
}

// 홈의 한 줄 요약. 분야마다 [규모 막대 · 뉴스 비중 30일 흐름 · 국내 지분] 만
// 남기고, 상세(누가 갖고 있나 / 무엇을 내고 있나 / Y04S 갈래 / 국내 권리)는
// 경쟁·활용 탭으로 보낸다.
//
// 왜 나누나: 이 카드 여섯 장이 1,900px 이라 홈 높이의 절반을 차지하고 있었다.
// 홈은 '지금 무슨 일이 벌어지나' 를 한눈에 보여야 하는 자리인데, 거기서 분야
// 하나하나의 출원인 지분까지 읽게 만들면 첫 화면이 끝나기 전에 스크롤이 시작된다.
// 상세는 '누구와 부딪히나' 를 묻는 자리 — 곧 경쟁·활용 탭 — 에서 필요하다.
function catSummaryHTML(rows){
  const maxTot=Math.max.apply(null, rows.map(d=>d.r.tot||0)) || 1;
  return '<div class="csum">' + rows.map(d=>{
    const r=d.r;
    const w=Math.max(2, (r.tot/maxTot)*100);
    const code = r.cat.cpc
      ? '<span class="tcpc mono"'+(r.cat.en? ' title="'+esc(r.cat.en)+'"' : '')
        + '>'+esc(r.cat.cpc)+'</span>' : '';
    // 뉴스 흐름은 '짝이 있는 분야만' 그린다. 짝이 없는데 빈 자리를 두면 값이
    // 0인 것처럼 읽힌다 → 그 자리에 이유를 적는다.
    const spark = !d.paired
      ? '<span class="cspk none" title="이 분야에 대응하는 뉴스 분류가 없습니다">뉴스 짝 없음</span>'
      : d.fresh
      ? '<span class="cspk none" title="'+esc('뉴스 분류를 새로 만든 자리입니다 — 다음 수집부터 채워집니다')
        + '">수집 시작 전</span>'
      : '<span class="cspk">'+sparkShare(catShareSeries(d.newsKeys||[], 14))
        + '<b>'+Math.round((d.news&&d.news.share||0)*100)+'%</b></span>';
    return '<button type="button" class="crow2 lv-'+(d.lv||'na')+'" data-catgo="'
      + esc(r.cat.key)+'" title="'+esc(r.cat.name+' 자세히 보기 — 누가 갖고 있나·무엇을 내고 있나')+'">'
      + '<span class="cnm">'+r.cat.emoji+' '+esc(r.cat.name)+code+'</span>'
      + '<span class="cbar2"><i style="width:'+w.toFixed(1)+'%"></i></span>'
      // tot 는 표본 구성비로 되돌린 **추정** 총계라 실수다. 그대로 찍으면
      // '8,282.095' 처럼 천 단위 쉼표와 소수점이 한 줄에 섞여 읽히지 않는다.
      + '<span class="cval mono" title="'+esc('추정 공개 규모 — 표본 구성비로 되돌린 값입니다')
      + '">'+Math.round(r.tot).toLocaleString()+'</span>'
      + spark
      + '<span class="ckr" title="이 분야 권리 중 국내 출원인 몫">🇰🇷 '
      + Math.round(r.krShare*100)+'%</span>'
      + '<span class="cgo">자세히 →</span></button>';
  }).join('') + '</div>';
}

function tradeSectionHTML(where){
  const T=FEED.trade; if(!T) return '';
  // 이 표는 분야별 권리 집중도(CR3)를 항목에서 직접 센다 → 다 받기 전에는
  // 내보내지 않는다. 부분집합으로 세면 집중도가 실제보다 높게 나온다
  // (적게 볼수록 소수가 다 가진 것처럼 보인다) — 방향이 정해진 오차라 특히 나쁘다.
  if(!FULL) return '<div class="sec" id="sec-analysis">🧭 분야별 경쟁 구도</div>'
    + loadingNote('집중도');
  const rows=tradeRows(); if(!rows.length) return '';
  const cmp=FEED.insights.comparable;
  const ADJ=FEED.patents.totalsAdjusted||{};
  // 분야별 세부 기술은 따로 패널을 두지 않고 여기 카드 안으로 들어온다(축은 분야다).
  const SUB={}; subsRows().forEach(s=>SUB[s.cat.key]=s);
  const SUBG=subgRows();
  const body=rows.map(d=>{
    const r=d.r, pct=Math.round(r.cr3*100);
    // ① 지분 막대 — '상위 3곳 77%' 라고 쓰기보다 그 77% 를 보이게 한다. 위아래로
    //   쌓이면 어느 분야가 몰려 있는지 스크롤만으로 잡힌다. 색은 그 분야의 집중도
    //   단계(파랑 램프) 하나로 통일하고, 칸 사이는 2px 배경색 틈으로 나눈다.
    const col=d.lv==='hi'?'var(--q3)':d.lv==='mid'?'var(--q2)':'var(--q1)';
    const segs=r.top.map(t=>t.v/r.tot);
    const rest=Math.max(0, 1-segs.reduce((a,b)=>a+b,0));
    // 1위 몫이 넉넉하면 이름을 칸 안에 넣는다 — 기술 막대와 같은 규칙이다. 다만
    // 출원인은 1위가 12% 인 분야도 흔해(재생에너지=LG에너지솔루션 12%) 대개는
    // 빈 막대로 남고, 이름은 아래 칩이 진다. 그래도 두 막대의 높이는 같아야 한다.
    const inbar1 = segs.length>0 && segs[0]>=0.25;
    const bar='<div class="shbar" role="img" aria-label="상위 3곳이 '+pct+'%">'
      + segs.map((s,i)=>'<i style="width:'+(s*100).toFixed(1)+'%;background:'+col+'">'
          + (i===0 && inbar1
             ? flg(r.top[0].flag)+'<b>'+esc(r.top[0].name)+'</b>'
               + '<em>'+Math.round(s*100)+'%</em>' : '')
          + '</i>').join('')
      + (rest>0.005? '<i class="rest" style="width:'+(rest*100).toFixed(1)+'%"></i>':'')
      + '</div>';
    // 1위 칩은 칸 안에 이름을 넣었어도 **버리지 않고** .lead 로 실어 둔다. 몫이
    // 같아도 화면이 좁으면 칸 안 글자가 잘린다(실측 430px: '한국전력ㄱ' 에서
    // 끊겼다). 글자 폭은 그리기 전에 알 수 없으므로 둘 다 내보내고 CSS 가 고른다.
    const names=r.top.map((t,i)=>{
      const a=ADJ[t.name];
      return '<span class="shn'+(i===0 && inbar1? ' inb':'')+'"'
             +(a?' title="총계 보정: '+esc(a.raw+'건 → 표본 '+a.of
             +'건 중 제 것 '+a.kept+'건 비율')+'"':'')+'>'
        + flg(t.flag)+' '+esc(t.name)+(a?'*':'')
        + '<b>'+Math.round(t.v/r.tot*100)+'%</b></span>'; }).join('')
      + (rest>0.005? '<span class="shn rest">나머지<b>'+Math.round(rest*100)+'%</b></span>':'');
    // ③ 국내 공개는 '누가 갖고 있나' 와 성격이 다르다 — 한국에서 실제로 부딪히는
    //   권리라는 경고다. 모양을 달리하고, 없는 분야에서는 아예 나오지 않게 한다.
    const krc=krLineHTML(d.kr);
    // ④ 판정 문장. 어느 분야에나 붙는 일반론("권리가 소수에 몰려 있습니다")은
    //   일곱 번 반복되면 배경이 된다 → 그 분야의 실제 수치와 회사 이름을 넣어
    //   만든다. 이름은 손으로 적지 않고 이 행의 데이터에서 뽑는다(순위가 바뀌면
    //   문장도 같이 바뀐다). 짧은 꼬리표는 툴팁으로 돌려 위계를 준다.
    const names3=r.top.map(t=>t.name).join('·');
    const fill=s=>String(s||'')
      .replace('{neff}', r.neff.toFixed(1)).replace('{n}', r.n)
      .replace('{top3}', names3).replace('{top1}', (r.top[0]||{}).name||'')
      .replace('{krn}', d.kr.length)
      .replace('{krs}', d.kr.slice(0, KR_HEAD).map(k=>k.name).join('·'))
      .replace('{ratio}', d.ratio!=null? Math.round(d.ratio*100) : '')
      .replace('{krshare}', Math.round(r.krShare*100))
      .replace('{domn}', r.krN).replace('{domtop}', r.krTop||'');
    const hasNews = d.paired && cmp && d.ratio!=null && !d.fresh;
    const dom = r.krShare<0.05 ? 'none' : (r.krShare<0.20 ? 'low' : '');
    const gen=[ d.lv? fill((T.gen||{})[d.lv]) : '',
                dom? fill((T.genDom||{})[dom]) : '',
                d.kr.length? fill(T.genKr) : '',
                hasNews? fill((T.genNews||{})[d.dir]) : '' ].filter(Boolean).join(' ');
    const short=[(T.concShort||{})[d.lv]||'',
                 hasNews? (T.newsShort||{})[d.dir]||'' : '',
                 d.kr.length? '국내 권리 '+d.kr.length+'곳' : ''].filter(Boolean)
                .join(' · ');
    // 배지는 이미 이스케이프한 HTML 로 담는다 — 스파크라인(SVG)이 섞이기 때문.
    const badges=[];
    if(d.lv) badges.push(esc('실질 '+r.neff.toFixed(1)+'곳 / '+r.n+'곳'));
    // 예전에는 '뉴스 비중 '+ratio*100+'%' 였다. ratio 는 비중이 아니라 **이전
    // 기간 대비 배율**이라 원전 카드가 '뉴스 비중 121%' 로 나왔다 — 비중은
    // 100%를 넘을 수 없으니 읽는 사람에게는 그냥 틀린 수다. 둘을 갈라 적는다:
    // 비중은 지금 이 분야가 뉴스에서 차지한 몫, 배율은 그 몫의 변화.
    else if(d.fresh) badges.push(esc('뉴스 수집 시작 전'));
    if(hasNews){
      const chg = d.ratio>=1.10 ? ' ▲'+d.ratio.toFixed(1)+'배'
                : d.ratio<=0.90 ? ' ▼'+d.ratio.toFixed(1)+'배' : '';
      badges.push(esc('뉴스 비중 '+Math.round((d.news.share||0)*100)+'%'+chg)
        + sparkShare(catShareSeries(d.newsKeys||[], 14)));
    }
    else if(d.paired===false) badges.push(esc('뉴스 짝 없음'));
    // 국내 지분 — 이 분야에 국내 협상 상대가 있는지. 없으면 도입 말고는 길이 없다.
    badges.push(esc('🇰🇷 국내 '+Math.round(r.krShare*100)+'%'));
    // 카드 하나가 분야 하나다. 안은 두 토막 — 누가 갖고 있나 / 무엇을 내고 있나.
    // 토막에 이름을 붙이는 이유: 붙이지 않았을 때는 막대 두 개가 잇달아 나와
    // 둘 다 '지분' 처럼 보였다(하나는 출원인 지분, 하나는 기술 갈래 비중이다).
    // 분야 이름 옆에 표준 코드를 단다. 이 분야들은 우리가 정한 것이 아니라 CPC
    // Y04S(스마트그리드)가 정한 것이고, 이름은 우리가 옮긴 말이다 — 코드와 공식
    // 표제를 곁에 두지 않으면 옮긴 말이 표준인 것처럼 읽힌다.
    // 원전은 Y04S 밖이라 그 사실을 배지로 밝힌다(근거는 Y02E 30).
    const code = r.cat.cpc
      ? '<span class="tcpc mono"'+(r.cat.en? ' title="'+esc(r.cat.en)+'"' : '')
        + '>'+esc(r.cat.cpc)+'</span>'
      : '';
    const outside = r.cat.outside
      ? '<span class="tout" title="Y04S 는 계통에 ICT 를 붙인 것에만 부여됩니다 — '
        + '원전은 그 체계에 자리가 없어, 기관 소관에 따라 Y02E 30(원자력)을 근거로 '
        + '따로 둡니다.">Y04S 밖</span>' : '';
    return '<div class="trow lv-'+(d.lv||'na')+'" data-cat="'+esc(r.cat.key)+'"><div class="th">'
      + r.cat.emoji+' '+esc(r.cat.name) + code + outside
      + '<div class="tb">'+badges.map(b=>'<span>'+b+'</span>').join('')+'</div></div>'
      + '<div class="blk">누가 갖고 있나<span class="blkd">'
      + r.n.toLocaleString()+'곳 중 상위 3곳이 '+pct+'%</span></div>'
      + bar + '<div class="shns">'+names+'</div>' + krc
      + subsBlockHTML(SUB[r.cat.key], T)
      + subgBlockHTML(SUBG[r.cat.key], r.cat)
      + '<p class="tr" title="'+esc(short)+'">'+esc(gen)+'</p>'
      + (d.note? '<p class="tcaveat">※ '+esc(d.note)+'</p>' : '')
      + '</div>';
  }).join('');
  // 홈에 놓는다 — 경쟁·활용 탭에도 같은 표를 두면 약한 판본이 하나 더 생긴다(전에
  // 홈의 '분야별 경쟁 구도' 가 이 표의 축소판이었다). 이 표는 '거래' 이전에
  // '지금 이 분야가 어떻게 생겼나' 를 말하므로 홈이 제자리다.
  // 홈에는 지도와 한 줄 요약까지, 경쟁·활용 탭에는 분야 카드 여섯 장.
  // 분야 분석은 **한자리에** 모은다. 전에는 지도·요약이 홈에, 카드가 여기,
  // 매트릭스가 통계 탭에 흩어져 있어 한 분야의 그림을 맞추려면 탭을 오갔다.
  // 순서는 넓은 것에서 좁은 것으로: 분야끼리 견주기(지도) → 한 줄 요약 →
  // 분야마다 누가·무엇을(카드) → 기업 하나하나(매트릭스). 그다음이 창구다.
  if(where === 'trade')
    return '<div class="sec" id="sec-analysis">🧭 분야별 경쟁 구도</div>'
      + '<p class="gdesc">지도로 분야끼리 견주고, 그 아래에서 분야마다 '
      + '<b>누가</b>(출원인) <b>무엇을</b>(세부 기술) 내는지 봅니다. '
      + '맨 아래 매트릭스는 기업별로 어느 분야에 내는지입니다.'
      + (cmp? '' : ' (이전 기간 자료가 아직 부족해 뉴스 변화는 표시하지 않습니다.)')
      + '</p>'
      + quadChartHTML(rows)
      + catSummaryHTML(rows)
      + body
      + '<p class="tcaveat" style="margin-top:12px">' + esc(T.unpaired) + '</p>'
      + (T.subsNote? '<p class="gnote">' + esc(T.subsNote) + '</p>' : '')
      + '<p class="gnote">' + esc(T.note) + '</p>'
      + matrixSectionHTML(FEED.patents.items||[]);
  return '<div class="homepanel" id="sec-analysis"><h3>🧭 분야별 경쟁 구도'
    + '<span class="morelink" data-go="patents-stats">특허 통계 전체 →</span></h3>'
    + '<p class="sub">분야끼리 견주는 지도입니다. 그 아래 한 줄씩은 <b>규모</b>와 '
    + '<b>최근 2주 뉴스 비중</b>, <b>국내 출원인 몫</b>입니다. 줄을 누르면 경쟁·활용 탭에서 '
    + '그 분야를 누가 갖고 있는지까지 볼 수 있습니다.'
    + (cmp? '' : ' (이전 기간 자료가 아직 부족해 뉴스 변화는 표시하지 않습니다.)')
    + '</p>'
    + quadChartHTML(rows)
    + catSummaryHTML(rows)
    + '<p class="tcaveat" style="margin-top:10px">' + esc(T.unpaired) + '</p></div>';
}

// ── 분야 안을 들여다보면: 세부 기술 쏠림 ──────────────────────────────
// 위 '분야별 경쟁 구도' 는 **누가** 갖고 있는지를 말한다. 그런데 같은 분야 안에서
// 출원이 **무엇에** 몰려 있는지는 거기서 통째로 뭉개진다 — 재생에너지·저장이
// 8,540건이라는 말만으로는 그중 62%가 배터리라는 사실이 보이지 않는다.
// 새 수집 없이 지금 피드로 계산된다(항목마다 IPC 코드가 이미 실려 있다).
//
// 대표 코드는 첫 IPC 의 앞 네 자리(서브클래스)다. 한 건에 여러 코드가 붙지만
// 전부 세면 한 건이 여러 번 계수돼 합이 100%를 넘고, '몇 퍼센트' 라는 말이
// 뜻을 잃는다 → 첫 코드 하나만 센다. 실측(누적 16,265건): 코드가 아예 없는 항목 0건,
// 첫 코드가 Y(CPC 전용 태깅) 인 항목 1건 — 대표 코드로 쓰기에 무리가 없다.
// 흐름 그림이 볼 날수. 아카이브가 35일이라 30일이면 거의 전부를 쓰면서도
// 초반의 들쭉날쭉한 며칠(수집을 막 시작한 때)은 비켜 간다.
const TREND_DAYS = 30;
const SUBS_MIN = 30;        // 이 아래로는 비율이 한두 건에 휘둘린다
function subsRows(){
  const cats=FEED.patents.categories||[];
  const by={};
  (FEED.patents.items||[]).forEach(it=>{
    const c=(it.cpc||[])[0]; if(!c) return;
    const k=String(c).slice(0,4).toUpperCase();
    if(k.length<4) return;
    const m=by[it.category]||(by[it.category]={});
    m[k]=(m[k]||0)+1;
  });
  return cats.map(c=>{
    const m=by[c.key]; if(!m) return null;
    const all=Object.entries(m).sort((a,b)=> b[1]-a[1] || a[0].localeCompare(b[0]));
    const n=all.reduce((s,x)=>s+x[1],0);
    if(n<SUBS_MIN) return null;
    const top=all.slice(0,3).map(([code,v])=>({code, v, share:v/n}));
    const restV=n-top.reduce((s,t)=>s+t.v,0);
    return {cat:c, n, kinds:all.length, top, rest:{v:restV, share:restV/n}};
  }).filter(Boolean).sort((a,b)=> b.top[0].share-a.top[0].share);
}

// 한 분야의 '무엇을' 토막. 패널을 따로 두지 않고 분야 카드 안에 들어간다 —
// 여덟 분야를 두 패널이 각각 한 번씩 나열하면, 한 분야의 그림을 맞추려고
// 위아래를 오가게 된다. 축은 분야이고, '누가'와 '무엇을'은 그 아래 두 토막이다.
function subsBlockHTML(r, T){
  if(!r) return '';
  const NM=(T||{}).ipcNames||{};
  const nameOf=c=> NM[c] || c;                 // 툴팁·대체텍스트용(글자만)
  const COL=['var(--q3)','var(--q2)','var(--q1)'];
  // 1위 이름을 막대 **안**에 넣는다. 처음에는 네 갈래를 전부 같은 크기의 칩으로
  // 막대 아래에 늘어놓았는데, 그 줄의 요점(‘73%가 한 갈래’)이 칩 네 개짜리
  // 문장 끝의 작은 숫자로 묻혀 여덟 줄이 전부 비슷해 보였다. 요점은 막대가
  // 지고, 아래 줄은 2·3위와 나머지만 작게 받는다.
  // 안에 넣을지는 데이터로 정한다(글자 폭을 재지 않는다) — 1위 몫이 30% 아래면
  // 좁은 화면에서 이름이 잘리므로 그때는 아래 줄이 1위까지 맡는다.
  const inbar = r.top[0].share >= 0.30;
  const segs=r.top.map((t,i)=>{
    const tip=nameOf(t.code)+' ('+t.code+') — '+r.cat.name+' '+r.n.toLocaleString()
      +'건 중 '+t.v.toLocaleString()+'건, '+Math.round(t.share*100)+'%';
    const lab = (i===0 && inbar)
      ? '<b>'+esc(nameOf(t.code))+'</b>'
        + (NM[t.code] ? '<span class="sbc mono">'+esc(t.code)+'</span>' : '')
        + '<em>'+Math.round(t.share*100)+'%</em>' : '';
    return '<i title="'+esc(tip)+'" style="width:'+(t.share*100).toFixed(1)
      + '%;background:'+COL[i]+'">'+lab+'</i>';
  }).join('');
  const rest=r.rest.share>0.005
    ? '<i class="rest" title="'+esc('그 밖의 갈래 '+(r.kinds-r.top.length)
        +'종 합계 — '+r.rest.v.toLocaleString()+'건, '+Math.round(r.rest.share*100)+'%')
      + '" style="width:'+(r.rest.share*100).toFixed(1)+'%"></i>' : '';
  // 아래 줄. 순위를 앞에 두고 숫자를 이름보다 먼저 읽히게 한다 — 칩 끝에
  // 붙여 두었을 때는 퍼센트를 찾으려면 이름을 다 읽어야 했다.
  // 이름이 없는 코드는 코드가 이름 자리에 선다. 그때 뒤의 작은 코드칩까지
  // 붙이면 'X99Z X99Z' 가 되므로 붙이지 않는다.
  // lead=true 면 칸 안에도 같은 이름이 있다는 뜻이다. 지우지 않고 표시만 해 둔다 —
  // 좁은 화면에서는 칸 안 글자가 잘려서 이 칩이 대신 나서야 한다(CSS 가 고른다).
  const chip=(t,i,dup)=>'<span class="sbn'+(dup?' inb':'')+'">'
    + '<i style="background:'+COL[i]+'"></i>'
    + '<b>'+Math.round(t.share*100)+'%</b>'+esc(nameOf(t.code))
    + (NM[t.code] ? '<span class="sbc mono">'+esc(t.code)+'</span>' : '')
    + '</span>';
  const names=r.top.map((t,i)=> chip(t, i, i===0 && inbar)).join('')
    + (r.rest.share>0.005
       ? '<span class="sbn rest"><i></i><b>'+Math.round(r.rest.share*100)+'%</b>나머지</span>'
       : '');
  return '<div class="subrow"><div class="blk">무엇을 내고 있나'
    + '<span class="blkd">'+r.n.toLocaleString()+'건 · 서로 다른 '
    + r.kinds+'갈래</span></div>'
    + '<div class="shbar" role="img" aria-label="'
    + esc(r.cat.name+' 상위 세 갈래: '
          + r.top.map(t=>nameOf(t.code)+' '+Math.round(t.share*100)+'%').join(', '))
    + '">'+segs+rest+'</div><div class="shns">'+names+'</div></div>';
}

// ── 분야 안의 하위 갈래 (지금은 Y04S 10/00 만) ─────────────────────────
// 발전·송배전 지원 하나가 수집분의 70%다. 그 안에서 장주기 ESS 가 어디쯤인지
// 위의 '무엇을 내고 있나' 로는 안 보인다 — 그 막대의 축은 IPC 서브클래스라
// G01R31(전기적 특성 시험) 한 칸에 선로 고장 위치·전지 상태 진단·절연 시험이
// 함께 들어간다. 여기서는 축을 CPC Y04S 하위 그룹으로 바꾼다.
//
// 위 막대와 달리 **상위 3개로 자르지 않고 전부 그린다**. 갈래가 열한 개뿐이고,
// 자르면 이 토막을 만든 이유(작은 갈래가 어디 있나)가 사라진다. 대신 이름은
// 3% 넘는 갈래에만 붙인다 — 얇은 칸에 글자를 넣으면 겹친다.
const SUBG_NAME_MIN = 0.03;
function subgRows(){
  const map=(FEED.patents.subgroups)||{};
  const by={};
  (FEED.patents.items||[]).forEach(it=>{
    if(!it.sub || !map[it.category]) return;
    const m=by[it.category]||(by[it.category]={});
    m[it.sub]=(m[it.sub]||0)+1;
  });
  const out={};
  Object.keys(by).forEach(k=>{
    const n=Object.values(by[k]).reduce((s,v)=>s+v,0);
    if(!n) return;
    out[k]={n, parts: map[k].map(s=>({...s, v: by[k][s.code]||0,
                                      share:(by[k][s.code]||0)/n}))
                    .filter(p=>p.v>0)
                    .sort((a,b)=> b.v-a.v || a.code.localeCompare(b.code))};
  });
  return out;
}

function subgBlockHTML(r, cat){
  if(!r || !r.parts.length) return '';
  // 큰 갈래 셋만 뚜렷한 색을 쓰고 나머지는 옅은 회색 계단이다. 열한 칸에 전부
  // 다른 색을 주면 색이 순위가 아니라 이름표처럼 읽힌다.
  const COL=['var(--q3)','var(--q2)','var(--q1)'];
  const col=i=> i<3 ? COL[i] : 'color-mix(in srgb, var(--fg) '+(16-Math.min(i,9))+'%, transparent)';
  const segs=r.parts.map((p,i)=>{
    const tip=p.name+' (Y04S '+p.code+') — '+r.n.toLocaleString()+'건 중 '
      + p.v.toLocaleString()+'건, '+Math.round(p.share*100)+'%'
      + (p.note? '\n'+p.note : '') + (p.en? '\n'+p.en : '');
    // 이름은 **색이 진한 앞 세 칸에만** 넣는다. 처음에는 3% 넘는 칸 전부에
    // 넣었는데, 네 번째부터는 배경이 옅어 흰 글자가 그대로 묻혔다(보이지 않는
    // 글자를 그리고 있었다). 나머지는 아래 칩 줄이 전부 맡는다.
    const lab = (i<3 && p.share>=SUBG_NAME_MIN)
      ? '<b>'+esc(p.name)+'</b><em>'+Math.round(p.share*100)+'%</em>' : '';
    return '<i title="'+esc(tip)+'" style="width:'+(p.share*100).toFixed(1)
      + '%;background:'+col(i)+'">'+lab+'</i>';
  }).join('');
  const tipOf=p=> [p.name+' (Y04S '+p.code+')', p.note, p.en].filter(Boolean).join('\n');
  const chips=r.parts.map((p,i)=>'<span class="sbn">'
    + '<i style="background:'+col(i)+'"></i>'
    + '<b>'+Math.round(p.share*100)+'%</b>'+esc(p.name)
    + '<span class="sbc mono" title="'+esc(tipOf(p))+'">'+esc(p.code)+'</span>'
    + '</span>').join('');
  return '<div class="subrow"><div class="blk">Y04S 어느 갈래인가'
    + '<span class="blkd">'+r.n.toLocaleString()+'건 · CPC 하위 그룹 '
    + r.parts.length+'개</span></div>'
    + '<div class="shbar sgbar" role="img" aria-label="'
    + esc(cat.name+' 하위 갈래: '
          + r.parts.map(p=>p.name+' '+Math.round(p.share*100)+'%').join(', '))
    + '">'+segs+'</div><div class="shns sgns">'+chips+'</div></div>';
}

// 국유판매기술 — 권리자가 국가라 창구가 분명하고, 무상은 비용 없이 실시할 수 있다.
// 건수가 적어(전력 분야는 수십 건 규모) 표 하나로 충분하다. 규모를 부풀리지 않는다.
function staownHTML(){
  const S=FEED.staown; if(!S) return '';
  const row=(it, free)=>{
    // outNo 는 출원번호다(공개번호가 아니라 정확히 매칭되는 링크를 만들 수 없다)
    // → 검색으로 연결하고, 번호 자체도 보여 KIPRIS 에 직접 넣을 수 있게 한다.
    const q='https://patents.google.com/?q='+encodeURIComponent(it.no||it.title)+'&hl=ko';
    return '<div class="strow"><div class="stt">'
      + (free? '<span class="stfree">무상</span>' : '<span class="stpay">유상</span>')
      + '<a href="'+esc(safeUrl(q))+'" target="_blank" rel="noopener">'+esc(it.title)+' ↗</a></div>'
      + '<div class="stm">'+esc(it.org||'')
      + (it.type? ' <span aria-hidden="true">·</span> '+esc(it.type) : '')
      + (it.no? ' <span class="mono">'+esc(it.no)+'</span>' : '')
      + '</div></div>';
  };
  // 이 목록은 훑어보는 것보다 '있다는 걸 아는 것' 이 중요하다 → 기본은 몇 건만
  // 보이고 나머지는 펼쳐 본다(16건을 다 깔면 화면의 4분의 1을 먹는다).
  const all = (S.free||[]).map(i=>[i,true]).concat((S.pay||[]).map(i=>[i,false]));
  if(!all.length) return '';
  const shown = staownAll? all : all.slice(0, STAOWN_HEAD);
  const rows = shown.map(x=>row(x[0], x[1])).join('')
    + (all.length>shown.length
        ? '<button type="button" class="stmore" data-more="staown">더 보기 ('
          + (all.length-shown.length) + '건 남음)</button>'
        : (staownAll && all.length>STAOWN_HEAD
            ? '<button type="button" class="stmore" data-more="staown">접기</button>' : ''));
  return '<div class="sec" id="sec-staown">🏛️ 국유특허 — 전력 관련</div>'
    + '<p class="gdesc">'+esc(FEED.staownNote||'')+'</p>'
    + '<div class="stlist">'+rows+'</div>'
    + '<p class="tcaveat">무상 '+(S.free||[]).length+'건 · 유상 '+(S.pay||[]).length
    + '건 · 공공데이터포털 「지식재산처 특허기술거래 국유판매기술정보」'
    + (S.generated? ' · 받은 때 '+esc(S.generated) : '')
    + '. 제목에 전력 설비·계통 표현이 있는 것만 추렸습니다.</p>';
}

// 거래·지원: 수집 데이터와 무관한 안내 화면. 검색·필터가 필요 없어 홈과 같은
// 'homemode'(컨트롤 숨김)로 그린다.
// 이 탭은 셋이 순서를 이룬다: 분석(어디를 볼까) → 매물(뭘 받을 수 있나) →
// 창구(어디로 가나). 서브탭으로 끊으면 그 흐름이 깨지고, 안 누른 절은 있는 줄도
// 모르게 된다 → 내용은 한 화면에 두고 이동만 빠르게 하는 바로가기를 얹는다.
// (길이 실측: 이 탭 4,618px < 특허 탭 8,138px — 나눠야 할 만큼 길지는 않다.)
function renderGuide(){
  const G = FEED.guide||[];
  // 분석 표는 홈에 있다. 여기서는 그리로 보내기만 한다(같은 표를 두 번 그리면
  // 둘 중 하나는 반드시 뒤처진다).
  // 분야 카드는 **여기가 제자리다**. 홈에는 지도와 한 줄 요약만 두고(홈은 '지금
  // 무슨 일이 벌어지나' 를 한눈에 보이는 자리라 카드 여섯 장 1,900px 이 너무
  // 무겁다), 상대를 고르는 상세는 창구 바로 위에 온다.
  const trade = FULL ? tradeSectionHTML('trade')
    : '<div class="sec" id="sec-analysis">🧭 분야별 경쟁 구도</div>' + loadingNote('집중도');
  // 분석 다음에 '누구한테 가면 되나'가 온다. 매물(국유판매기술)·창구보다 앞이다 —
  // 상대를 정하고 나서야 매물과 창구가 뜻을 갖는다.
  // 공급자 표는 항목을 직접 센다 → 다 받기 전에는 숫자를 내보내지 않는다.
  // 자리는 남겨 둔다(섹션이 통째로 사라졌다 나타나면 화면이 튀고, 바로가기도
  // 가리킬 데가 없어진다).
  const supply = FULL ? supplierHTML(FEED.patents.items||[])
    : '<div class="sec" id="sec-supply">🎓 분야별 국내 공급자</div>'
      + loadingNote('기관 목록');
  const staown = staownHTML();
  let desks = G.map((g,i)=>
    '<div class="sec"'+(i===0? ' id="sec-desks"':'')+'>'
    + esc(g.emoji||'')+' '+esc(g.name)+'</div>'
    + (g.desc? '<p class="gdesc">'+esc(g.desc)+'</p>' : '')
    + '<div class="glist">' + (g.items||[]).map(it=>
        '<a class="gcard" href="'+esc(safeUrl(it.url))+'" target="_blank" rel="noopener">'
        + '<div class="gname">'+esc(it.name)
        + (it.org? '<span class="gorg">'+esc(it.org)+'</span>' : '')
        + '<span class="garr" aria-hidden="true">↗</span></div>'
        + '<p class="gwhat">'+esc(it.what||'')+'</p></a>').join('')
    + '</div>').join('');
  if(desks && FEED.guideNote) desks += '<p class="gnote">'+esc(FEED.guideNote)+'</p>';
  const jumps=[];
  if(trade)  jumps.push(['sec-analysis','🧭 분석']);
  if(supply) jumps.push(['sec-supply','🎓 공급자']);
  if(staown) jumps.push(['sec-staown','🏛️ 매물']);
  if(desks)  jumps.push(['sec-desks','🏢 창구']);
  const nav = jumps.length>1
    ? '<div class="jump">'+jumps.map(j=>'<button type="button" data-jump="'+j[0]+'">'
      + esc(j[1])+'</button>').join('')+'</div>' : '';
  $('#guide').innerHTML = nav + trade + supply + staown + desks;
}

function render(){
  const home = state.tab==='home', guide = state.tab==='guide';
  document.querySelector('.wrap').classList.toggle('homemode', home||guide);
  $('#home').hidden = !home;
  $('#guide').hidden = !guide;
  if(guide){ renderGuide(); updateViewToggle(); syncHash(); return; }
  if(home){ renderHome(); updateViewToggle(); syncHash(); return; }
  renderOverview();
  renderPeriodBar(); renderSource(); syncSearchPlaceholder();
  const list = filtered();
  const active = state.q || state.cats.size || state.countries.size || state.newonly
    || state.period!=='all' || state.source || state.savedOnly || state.unreadOnly;
  // '전체'는 FEED.*.total 을 쓴다(지연 로딩 중에도 정확). 왼쪽의 걸린 건수는
  // 아직 받는 중이면 최근분에서만 센 값이라, 세는 중임을 옆에 밝힌다.
  $('#resCount').innerHTML = '<b>'+list.length.toLocaleString()+'</b>건'
    + (active? ' <span style="opacity:.7">/ 전체 '+count(state.tab).toLocaleString()+'</span>' : '')
    + (FULL? '' : ' <span class="hyd">'+(HYDFAIL? '최근분만' : '불러오는 중…')+'</span>');
  // 특허 탭엔 '어느 기간 공개분인지'를 항상 명시(주별 버킷=수집 주와 혼동 방지)
  $('#scope').innerHTML = state.tab==='patents' ? patentScopeHTML() : '';
  $('#scope').hidden = state.tab!=='patents';
  $('#reset').hidden = !active;
  const isStats = state.tab==='patents' && state.view==='stats';
  $('#results').classList.toggle('readcol', !isStats);   // 목록=읽기폭, 통계=전체폭
  if(isStats){
    // 통계는 통째로 '전체를 센 결과'다 — 부분집합으로 그리면 랭킹 순서까지 바뀐다.
    $('#results').innerHTML = FULL ? renderStats(list)
      : '<div class="empty">'+(HYDFAIL
          ? '아카이브 전체를 불러오지 못해 통계를 표시할 수 없습니다. ' + hydWhy()
          : '아카이브를 불러오는 중입니다 — 통계는 전체를 받은 뒤 표시됩니다.')
        + '</div>';
    $('#more').hidden = true;
    syncHash(); return;
  }
  const cm = catMap(state.tab);
  const shown = list.slice(0, state.limit);
  if(!shown.length){
    $('#results').innerHTML = '<div class="empty">조건에 맞는 항목이 없습니다.</div>';
  } else if(state.tab==='news'){
    // 날짜별 그룹 헤더
    const byDay={}; const order=[];
    shown.forEach(it=>{ const d=it.date||'?'; if(!(d in byDay)){byDay[d]=[]; order.push(d);} byDay[d].push(it); });
    $('#results').innerHTML = order.map(d=>{
      const lbl=dayLabel(d), wd=weekday(d);
      const dateSpan = (lbl!==d? '<span class="d">'+esc(d)+'</span>':'') + (wd? '<span class="d">('+wd+')</span>':'');
      return '<div class="dgroup">'+esc(lbl)+dateSpan
        + '<span class="n">'+byDay[d].length+'건</span></div>'
        + byDay[d].map(it=>card(it,cm)).join('');
    }).join('');
  } else {
    $('#results').innerHTML = shown.map(it=>card(it,cm)).join('');
  }
  $('#more').hidden = list.length <= state.limit;
  $('#more').textContent = '더 보기 ('+(list.length-state.limit)+'개 남음)';
  syncHash();
}

// '국적을 모른다' 와 '국적 축 다섯 갈래 밖이다' 는 다르다. 서지상세로 국적을
// 채우기 시작하면서 대만·캐나다·인도 같은 나라가 실제로 들어온다 — 그것까지 미상에
// 넣으면 아는 것을 모른다고 말하는 셈이다. 세는 자리가 둘이라 여기서 한 번만 센다.
function regionSplit(ranked){
  const known = new Set((FEED.patents.countries||[]).map(c=>c.code));
  let unknown=0, other=0;
  (ranked||[]).forEach(r=>{ if(!r.region) unknown++; else if(!known.has(r.region)) other++; });
  return {unknown:unknown, other:other};
}
// 국적을 어떻게 알아내고 있는지 — 두 자리(홈 타일·통계 탭)에서 같은 말을 해야 한다.
// 두 부류를 반드시 갈라 말한다. 하나는 아직 안 채운 것이고, 하나는 원본에 값이
// 없어 채울 수 없는 것이다. 뭉뚱그려 '채우는 중' 이라고 하면 영영 안 오는 것을
// 기다리게 만든다.
function originNote(unknown){
  const done = (FEED.patents.originsFilled||0);
  const blocked = (FEED.patents.originsBlocked||0);
  const left = Math.max(0, unknown - blocked);
  let s = '해외 공보 목록에는 출원인 국적 칸이 없어, 문헌마다 있는 서지상세를 '
    + '출원인당 한 번씩 조회해 채웁니다';
  s += done? ' — 지금까지 '+done.toLocaleString()+'곳을 확인했습니다.'
           : ' (아직 시작 단계입니다).';
  if(blocked)
    s += ' 다만 출원인 국적이 실려 있는 것은 미국·유럽 공보뿐입니다. 중국·일본 '
      + '공보에는 그 칸이 원본부터 비어 있어(응답 전문을 받아 확인했습니다) '
      + blocked.toLocaleString()+'곳은 이 방법으로 채울 수 없습니다'
      + (left? ' (나머지 '+left.toLocaleString()+'곳은 채우는 중입니다).' : '.');
  s += ' 공개국으로 대신 채우지는 않습니다 — 미국에만 공개한 출원인 99곳을 실제로 '
    + '대조해 보니 미국 기업은 34%뿐이었고, 유럽은 57%였습니다. 셋 중 둘이 틀리는 '
    + '추정이라 쓰지 않습니다.';
  return s;
}

// 출원인 집계(표본 내 건수 + 분야 그리드), 건수 내림차순
function _rankApplicants(list){
  const T=FEED.patents.totals||{};
  const byA={};
  list.forEach(it=>{ const nm=it.aName||'(미상)';
    const o=byA[nm]||(byA[nm]={cnt:0, flag:it.aFlag||'', region:it.aCountry||'', grid:{}});
    o.cnt++; o.grid[it.category]=(o.grid[it.category]||0)+1; if(!o.flag)o.flag=it.aFlag||''; });
  const CAP = FEED.patents.perApplicantLimit || 0;
  return Object.keys(byA).map(nm=>{
    const o=Object.assign({name:nm}, byA[nm]);
    // total: OPS 가 알려준 실제 전체 건수(있으면). cnt: 저장된 표본 수.
    o.exact = T[nm] != null;                 // 집계가 아직 안 돈 출원인은 false
    o.total = o.exact ? T[nm] : o.cnt;
    // 실제 건수를 모르는데 표본이 상한까지 찼으면 그 수는 '최소값'이다 →
    // 정확한 값처럼 보이지 않게 표본 표시(사선 막대 + '+')를 붙인다.
    o.sampled = o.exact ? (o.total > o.cnt) : (CAP > 0 && o.cnt >= CAP);
    return o;
  }).sort((a,b)=> b.total-a.total || a.name.localeCompare(b.name));
}
// 한 지역(부분집합)의 출원인×분야 표. opts.total → 합계 열
function matrixTableHTML(ranked, opts){
  const cats=FEED.patents.categories; opts=opts||{};
  if(!ranked.length) return '';
  let maxCell=1; ranked.forEach(r=>cats.forEach(c=>{const v=r.grid[c.key]||0; if(v>maxCell)maxCell=v;}));
  // 분야는 아이콘 대신 이름으로. 가운뎃점(·) 단위로 조각을 내어 조각 안에서는 절대 줄이 바뀌지
  // 않게 하고(.seg{nowrap}), 조각 사이 <wbr> 에서만 접히게 한다. 그냥 두면 좁은 폭에서 '·' 가
  // 혼자 한 줄을 차지한다. '·' 는 앞 조각에 붙여 '데이터센터·' / '무정전전원' 으로 접히게 한다.
  const catHead=c=>{ const parts=c.name.split('·');
    return parts.map((s,i)=>'<span class="seg">'+esc(s)+(i<parts.length-1?'·':'')+'</span>').join('<wbr>'); };
  const head='<tr><th class="cnr"></th>'+cats.map(c=>'<th class="cth" title="'+esc(c.name)+'">'
      +catHead(c)+'</th>').join('')
    +(opts.total?'<th>합계</th>':'')+'</tr>';
  const body=ranked.map(r=>{
    const cells=cats.map(c=>{ const v=r.grid[c.key]||0; const a=v?(0.14+v/maxCell*0.78).toFixed(2):0;
      const st=v?('background:rgba(58,111,176,'+a+');color:'+(v/maxCell>0.55?'#fff':'inherit')):'';
      const attr=v?(' class="c has" data-ap="'+esc(r.name)+'" data-cat="'+c.key+'" title="'+esc(r.name)+' · '+esc(c.name)+' '+v+'건"'):' class="c"';
      return '<td'+attr+' style="'+st+'">'+(v||'·')+'</td>'; }).join('');
    return '<tr><td class="lab" title="'+esc(r.name)+'">'+flg(r.flag)+' '+esc(r.name)+'</td>'+cells
      +(opts.total?'<td class="c tot">'+r.cnt+'</td>':'')+'</tr>';
  }).join('');
  return '<div class="pmxwrap"><table class="pmx"><thead>'+head+'</thead><tbody>'+body+'</tbody></table></div>';
}
// 출원인 국적(지역)별로 나눈 매트릭스. opts.top 이 있으면 지역마다 상위 N 출원인만(홈 요약용).
// 국적 묶음마다 몇 곳까지 펼쳐 뒀는지. 기본은 상위 MTX_TOP 곳.
// 처음에는 전부 그렸는데, 국내 공보를 전수로 받기 시작하면서 이 표가 5,026행
// 158,071px 이 됐다 — 한 화면에 담기지 않는 정도가 아니라 스크롤로도 끝에
// 닿기 어렵고, 그리는 것만으로 탭 전환이 느려진다. 상위 열 곳이면 '이 나라는
// 누가 어느 분야에 내나' 라는 이 표의 물음에는 답이 되고, 나머지는 펼쳐서 본다.
const MTX_TOP = 10;
const mtxOpen = new Set();

function _rgSecHTML(rg, list, opts){
  const known=new Set(FEED.patents.countries.map(c=>c.code));
  const sub=rg.code? list.filter(it=>it.aCountry===rg.code)
                   : list.filter(it=>!known.has(it.aCountry));
  if(!sub.length) return '';
  const all=_rankApplicants(sub);
  const cap=opts.top||0;
  // opts.fixed 는 '늘어나지 않는 축약판' 이다(홈). 펼치기 단추를 내지 않고,
  // 경쟁·활용 탭에서 펼쳐 둔 상태(mtxOpen)도 따르지 않는다 — 따르면 홈이 남의 조작
  // 때문에 수천 행으로 늘어난다. 홈은 언제 봐도 같은 높이여야 한다.
  const open=!opts.fixed && mtxOpen.has(rg.code);
  const ranked=(cap && !open)? all.slice(0, cap) : all;
  const more=all.length-ranked.length;
  // 버튼은 '몇 곳이 더 있는지'를 숫자로 말한다. '더 보기' 만으로는 30곳이
  // 남았는지 3,000곳이 남았는지 몰라 누를지 말지를 고를 수 없다.
  const btn = (cap && all.length>cap && !opts.fixed)
    ? '<button class="rgmore" data-mtx="'+esc(rg.code)+'" aria-expanded="'+open+'">'
      + (open? '상위 '+cap+'곳만 보기'
             : '나머지 '+more.toLocaleString()+'곳 펼치기')+'</button>'
    : '';
  return '<div class="rgsec" data-rgsec="'+esc(rg.code)+'"><div class="rghead">'
    + flg(rg.emoji)+' <b>'+esc(rg.name)+'</b>'
    + ' <span class="rgn">'+sub.length+'건 · 출원인 '+all.length.toLocaleString()
    + ((cap && !open && more>0)? ' (상위 '+ranked.length+')':'') + '</span></div>'
    + matrixTableHTML(ranked, opts) + btn + '</div>';
}

function _mtxGroups(list){
  const known=new Set(FEED.patents.countries.map(c=>c.code));
  const groups=FEED.patents.countries.slice();
  // 알 수 없는 지역(옛 데이터 등)은 버리지 않고 '기타'로 모아 KPI 합계와 어긋나지 않게 한다.
  if(list.some(it=>!known.has(it.aCountry))) groups.push({code:'', emoji:'🏳️', name:'기타'});
  return groups;
}

// 출원인 × 분야 매트릭스. 분야 분석의 가장 좁은 눈금이라 분야별 경쟁 구도의
// 맨 아래에 온다(넓은 것 → 좁은 것). 전에는 통계 탭에 혼자 있어서, 카드에서 본
// 분야를 기업 단위로 확인하려면 탭을 옮겨야 했다.
// 홈에 놓는 축약판. 국적 묶음마다 **상위 세 곳**만 보이고 펼치지 않는다 —
// 홈은 '한눈에' 가 전부인 자리라, 경쟁·활용 탭의 상위 10곳 + 펼치기를 그대로 가져오면
// 홈이 다시 길어진다. 전체는 경쟁·활용 탭에 그대로 있고 링크로 잇는다.
const MTX_HOME = 3;
function matrixHomeHTML(list){
  if(!list.length) return '';
  return '<div class="homepanel"><h3>🧩 출원인 × 분야 매트릭스'
    + '<span class="morelink" data-jump2="guide:sec-matrix">전체 보기 →</span></h3>'
    + '<p class="sub">국적별로 <b>가장 많이 낸 세 곳</b>이 어느 분야에 내는지입니다. '
    + '칸을 누르면 해당 특허로 이동합니다(최근 '
    + (FEED.patents.lookbackDays||90)+'일 실제 건수). 전체는 경쟁·활용 탭에.</p>'
    + '<div class="mtxwrap">'
    + regionMatrixHTML(list, {total:true, top:MTX_HOME, fixed:true}) + '</div></div>';
}

function matrixSectionHTML(list){
  if(!list.length) return '';
  return '<div class="sec" id="sec-matrix">🧩 출원인 × 분야 매트릭스</div>'
    + '<p class="gdesc">출원인을 국적별로 묶어, 각 기업이 <b>어느 분야에</b> 최근 특허를 '
    + '냈는지 봅니다. 칸을 누르면 그 출원인·분야 특허로 이동합니다. '
    + '<b>가로로</b>(이 기업이 어느 분야에) 읽으세요 — '
    + '세로(이 분야를 누가 나눠 갖나)는 위 분야 카드가 말합니다.<br>'
    // 칸의 수는 표본이 아니라 실제 건수다(기간 안 모집단을 통째로 받는다).
    // 옛 안내를 그대로 두면 맞는 수를 틀린 수로 의심하게 만든다.
    + '※ 최근 '+(FEED.patents.lookbackDays||90)
    + '일 공개분 건수이며 보유 특허 전체가 아닙니다. '
    + '공개 특허청은 이와 별개로 각 카드에 표시됩니다.</p>'
    + '<div class="mtxwrap">'
    + regionMatrixHTML(list, {total:true, top:MTX_TOP}) + '</div>';
}

function regionMatrixHTML(list, opts){
  opts = opts||{};
  const html=_mtxGroups(list).map(rg=>_rgSecHTML(rg, list, opts))
    .filter(Boolean).join('');
  return html || '<p class="sub" style="margin:0">아직 수집된 특허가 없습니다.</p>';
}

// 랭킹 보기 방식: 'region'(출원인 국적별) | 'office'(공개 특허청=시장별) | 'all'(통합)
const RANK_MODES = ['region','office','all'];
let rankMode = RANK_MODES.includes(localStorage.getItem('pnp_rankMode'))
  ? localStorage.getItem('pnp_rankMode') : 'region';

function rankRowsHTML(rows, maxOverride){
  const maxA = maxOverride || (rows[0] ? rows[0].total : 1) || 1;
  return rows.map((r,i)=>{ const w=Math.max(2,r.total/maxA*100);
    const tip = r.exact ? (r.sampled? ' — 실제 '+r.total+'건 중 '+r.cnt+'건 저장':'')
      : (r.sampled? ' — 수집 상한까지 저장돼 실제는 '+r.cnt+'건 이상(정확 집계 대기)':'');
    return '<div class="row"><div class="nm" title="'+esc(r.name)+tip+'">'
      + '<span class="rk">'+(i+1)+'</span>'+flg(r.flag)+' '+esc(r.name)+'</div>'
      + '<div class="bar'+(r.sampled?' cap':'')+'" style="width:'+w.toFixed(1)+'%"></div>'
      + '<div class="val">'+r.total+(r.exact?'':(r.sampled?'+':''))+'</div></div>'; }).join('');
}

// 국적별 다출원 기업 — "미국에서 1등, 한국에서 1등" 을 한 화면에서 비교
function regionRankHTML(ranked){
  const out = FEED.patents.countries.map(rg=>{
    const sub = ranked.filter(r=>r.region===rg.code);
    if(!sub.length) return '';
    return '<div class="rgrank"><div class="rghead">'+flg(rg.emoji)+' <b>'+esc(rg.name)+'</b>'
      + ' <span class="rgn">'+sub.length+'곳</span></div>'
      + rankRowsHTML(sub.slice(0,5)) + '</div>';
  }).filter(Boolean).join('');
  return out || '<span class="unknown">—</span>';
}

// 공개 특허청(시장)별 다출원 기업 — "미국 시장에 누가 많이 내나"(국적 무관).
// officeCounts[출원인][특허청] = 실제 건수(OPS count 쿼리). 없으면 표본으로 근사.
function officeRankHTML(ranked){
  const OC = FEED.patents.officeCounts || {};
  const exact = Object.keys(OC).length > 0;
  const byName = {}; ranked.forEach(r=>byName[r.name]=r);
  const fallback = {};      // 표본 기반 근사(정확 집계가 없을 때)
  if(!exact){
    FEED.patents.items.forEach(it=>{
      const o=fallback[it.aName]||(fallback[it.aName]={});
      if(it.office) o[it.office]=(o[it.office]||0)+1; });
  }
  const src = exact? OC : fallback;
  const out = (FEED.patents.offices||[]).map(off=>{
    const rows = Object.keys(src).map(nm=>{
      const n = src[nm][off.code] || 0;
      const base = byName[nm] || {flag:'', name:nm};
      return n? {name:nm, flag:base.flag, total:n, cnt:n, sampled:false} : null;
    }).filter(Boolean).sort((a,b)=> b.total-a.total || a.name.localeCompare(b.name));
    if(!rows.length) return '';
    return '<div class="rgrank"><div class="rghead">'+flg(off.emoji)+' <b>'+esc(off.name)+'</b>'
      + ' <span class="rgn">'+rows.length+'곳</span></div>'
      + rankRowsHTML(rows.slice(0,5)) + '</div>';
  }).filter(Boolean).join('');
  return out || '<span class="unknown">—</span>';
}

// 해외 출원인이 한국에 공개한 특허 — 그들이 국내 시장에서 지킬 값어치가 있다고 본 기술.
// 국내 업계 입장에선 '누가 무엇을 들고 들어왔나'가 가장 실용적인 신호다.
// 이 표를 두 가지로 볼 수 있게 한다. 같은 자료인데 묻는 것이 다르다:
//   회사별 — "이 회사가 한국에 무엇을 걸어 뒀나" (누구와 부딪히나)
//   기술별 — "이 기술에 누가 한국에 권리를 걸고 있나" (이 분야에 들어가도 되나)
// 기술별이 없을 때는 뒤쪽 물음에 답하려면 회사 블록을 전부 훑으며 분야칩을
// 눈으로 세야 했다 — 출원인이 수십 곳이라 사실상 불가능했다.
const KR_MODES = ['ap','cat'];
let krMode = KR_MODES.includes(localStorage.getItem('pnp_krMode'))
  ? localStorage.getItem('pnp_krMode') : 'ap';
const KR_SHOW = 12;          // 블록마다 보일 건수
const KR_BLOCKS = 12;      // 펴 두는 블록 수(나머지는 접는다)

function _krItemHTML(it, mode, cm){
  // 묶은 기준은 블록 머리에 이미 있으니 항목에는 **다른 축**을 붙인다.
  // 회사별로 묶었으면 그 건이 무슨 기술인지를, 기술별로 묶었으면 누가 낸 건지를.
  const tag = mode==='cat'
    ? '<span class="kc">'+flg(it.aFlag)+' '+esc(it.aName||'')+'</span>'
    : (cm[it.category]? '<span class="kc">'+esc(cm[it.category].name)+'</span>' : '');
  return '<li><a href="'+esc(safeUrl(it.url))+'" target="_blank" rel="noopener">'
    + esc(it.title)+'</a>'+tag
    + '<span class="kn mono">'+esc(it.number||'')+'</span></li>';
}

// 목록 위에 올리는 **읽는 값**. 나열만 있으면 70건을 다 훑어야 겨우 감이 오는데,
// 정작 궁금한 것은 셋뿐이다: 얼마나 들어와 있나 · 어느 분야에 · 어디서.
//
// 분모를 국내 공보 전체(같은 창 안의 KR 공개분)로 잡는 것이 핵심이다. 해외 건수만
// 세면 '44건'이 큰지 작은지 알 길이 없다 — 발전·송배전은 226건 중 44건(19%)이고
// 원전은 24건 중 9건(37%)이라, 건수로는 발전이 다섯 배지만 **밀도는 원전이 두 배**다.
// 국내 업계가 알고 싶은 것은 뒤쪽이다.
const KR_THIN = 20;    // 이보다 적은 분모의 비율은 표시하되 '표본 적음'을 붙인다

// 막대 길이는 **그 묶음의 최댓값**에 맞춘다. 0~100% 자로 그리면 최댓값이 23% 인
// 화면에서 다섯 줄이 전부 왼쪽 끝의 짤막한 토막이 되어 23·16·11 의 차이가 안 읽힌다.
// 대신 절대값을 잃지 않도록 %와 원래 건수를 줄마다 직접 적는다 — 막대는 견주는
// 일만 하고, 크기는 숫자가 말한다.
function krBarRow(name, num, den, max, note){
  const pct = den? Math.round(num*100/den) : 0;
  const w = max>0? Math.round(num*100/den*100/max) : 0;
  return '<div class="kfrow"><div class="kfn">'+name+'</div>'
    + '<div class="kfb"><i style="width:'+w+'%"></i></div>'
    + '<div class="kfv"><b>'+pct+'%</b><span class="mono">'+num+'/'+den+'건</span>'
    + (note? '<span class="kfw">'+note+'</span>' : '')+'</div></div>';
}

function krAnalysisHTML(rows, krAll, cm){
  // ① 분야별 해외 비중 — 비중 큰 순. 분모가 얇은 줄은 지우지 않고 표시만 한다
  //    (지우면 '그 분야엔 해외가 없다' 로 잘못 읽힌다).
  const fld=[];
  (FEED.patents.categories||[]).forEach(c=>{
    const den=krAll.filter(it=>it.category===c.key).length;
    if(!den) return;
    const num=rows.filter(it=>it.category===c.key).length;
    fld.push({c, num, den, pct:num/den});
  });
  fld.sort((a,b)=> b.pct-a.pct || b.den-a.den);
  const fldMax=fld.length? fld[0].pct*100 : 0;
  const fldRows=fld.map(f=>krBarRow(
    f.c.emoji+' '+esc(f.c.name), f.num, f.den, fldMax,
    f.den<KR_THIN? '표본 적음' : '')).join('');

  // ② 어디서 왔나 — 국적별 건수. 같은 자리에 같은 모양으로 둔다(눈이 덜 헤맨다).
  // 이름을 아는 나라만 제 줄을 갖는다. 나머지(싱가포르·캐나다처럼 한두 건씩)는
  // '기타' 로 묶는다 — 한 건짜리 줄이 늘어서면 위의 큰 셋이 안 보인다.
  const known={}; (FEED.patents.countries||[]).forEach(c=>{ known[c.code]={name:c.name, emoji:c.emoji}; });
  const rg={}; let etc=0;
  rows.forEach(it=>{ const k=it.aCountry||'';
    if(!known[k] || k==='KR'){ etc++; return; }
    const g=rg[k]||(rg[k]={n:0, flag:known[k].emoji});
    g.n++; });
  const rgSort=Object.entries(rg).sort((a,b)=>b[1].n-a[1].n);
  const rgMax=rows.length? Math.max(etc, ...rgSort.map(e=>e[1].n))*100/rows.length : 0;
  const rgRows=rgSort
    .map(([k,v])=>krBarRow(flg(v.flag)+' '+esc(known[k].name), v.n, rows.length, rgMax, '')).join('')
    + (etc? krBarRow('🌐 기타', etc, rows.length, rgMax, '') : '');

  // ③ 넓게 왔나 좁게 왔나 — 한 곳이 몰아 낸 것과 여러 곳이 한 건씩 낸 것은
  //    같은 70건이라도 뜻이 정반대다(협상 상대가 하나냐 쉰이냐).
  const cnt={}; rows.forEach(it=>cnt[it.aName]=(cnt[it.aName]||0)+1);
  const firms=Object.keys(cnt).length;
  const ones=Object.values(cnt).filter(v=>v===1).length;
  const top=Object.entries(cnt).sort((a,b)=>b[1]-a[1])[0];
  const share=rows.length? Math.round(ones*100/firms) : 0;

  const head=fld[0];
  // 이 칸은 홈에만 있고 홈에는 검색·필터가 없다 — 그래서 '최근 90일' 이라고 단정해도
  // 된다. 걸러진 목록 위에 다시 놓게 되면 그때는 무엇을 센 값인지 밝혀야 한다.
  const lead='최근 '+(FEED.patents.lookbackDays||90)+'일 국내 공개 <b>'
    + krAll.length.toLocaleString()+'건</b> 가운데 <b>'+rows.length.toLocaleString()+'건</b>('
    + (krAll.length? Math.round(rows.length*100/krAll.length):0)+'%)이 해외 출원인입니다.'
    + (head? ' 분야로는 <b>'+esc(head.c.name)+'</b>의 해외 비중이 '
        + Math.round(head.pct*100)+'%로 가장 높습니다.' : '');

  return '<div class="kana">'
    + '<p class="kalead">'+lead+'</p>'
    + '<div class="kacols">'
      + '<div class="kacol"><div class="kah">분야별 해외 비중</div>'
        + '<p class="kanote">그 분야 국내 공개분 가운데 해외 출원인이 낸 몫입니다. '
        + '건수가 아니라 <b>밀도</b>라, 적게 공개되는 분야도 비중이 높으면 드러납니다. '
        + '막대는 <b>최댓값 기준</b>(견주기용) — 크기는 옆 숫자로.</p>'
        + '<div class="kfrows">'+fldRows+'</div></div>'
      + '<div class="kacol"><div class="kah">어디서 왔나</div>'
        + '<p class="kanote">해외 '+rows.length.toLocaleString()+'건의 출원인 국적 구성입니다. '
        + '막대는 위와 같은 방식입니다.</p>'
        + '<div class="kfrows">'+rgRows+'</div></div>'
    + '</div>'
    + '<p class="kafoot">출원인 <b>'+firms.toLocaleString()+'곳</b> · 그중 <b>'
      + ones.toLocaleString()+'곳('+share+'%)</b>이 1건뿐입니다'
      + (top && top[1]>1? ' (가장 많은 곳은 '+esc(top[0])+' '+top[1]+'건)' : '')+'. '
      + (share>=60
         ? '몇 곳이 몰아 낸 것이 아니라 여러 곳이 <b>넓고 얕게</b> 들어와 있습니다 — '
           + '부딪힐 상대가 하나로 좁혀지지 않습니다.'
         : '소수의 출원인에 <b>몰려</b> 있습니다 — 상대가 좁아 개별 대응이 가능합니다.')
    + '</p></div>';
}

// 홈에 놓는 그림만의 판본. 목록은 통계 탭에 그대로 두고 **읽는 값만** 올린다 —
// 홈에서 궁금한 것은 '얼마나·어디에·어디서' 이고, 개별 292건은 찾아갈 일이 있을 때
// 보는 것이라 자리가 다르다. 홈에는 검색·필터가 없어 언제나 전체를 센다.
function krHomeHTML(list){
  const rows=list.filter(it=>it.office==='KR' && it.aCountry!=='KR');
  if(!rows.length) return '';
  const krAll=list.filter(it=>it.office==='KR');
  const cm={}; FEED.patents.categories.forEach(c=>cm[c.key]=c);
  return '<div class="homepanel"><h3>🇰🇷 해외 출원인의 국내 공개'
    + '<span class="morelink" data-jump2="patents:sec-krlist:stats">건별로 보기 →</span></h3>'
    + '<p class="sub">해외 출원인이 <b>한국에 공개</b>한 특허입니다. 여러 관할 구역 가운데 '
    + '한국이 포함됐다는 점에서, 해당 기술의 국내 권리화를 함께 고려한 것으로 볼 수 있습니다.</p>'
    + krAnalysisHTML(rows, krAll, cm)+'</div>';
}

function krEntryHTML(list){
  const rows=list.filter(it=>it.office==='KR' && it.aCountry!=='KR');
  if(!rows.length) return '';
  const krAll=list.filter(it=>it.office==='KR');
  const cm={}; FEED.patents.categories.forEach(c=>cm[c.key]=c);
  const mode=krMode;
  const by={};
  rows.forEach(it=>{
    const k = mode==='cat' ? (it.category||'') : it.aName;
    if(mode==='cat' && !cm[k]) return;          // 분야를 모르는 건은 기술별에서 뺀다
    const g = by[k]||(by[k]={items:[], names:new Set()});
    g.items.push(it); g.names.add(it.aName);
    if(mode!=='cat') g.flag=it.aFlag;
  });
  let order=Object.entries(by)
    .sort((a,b)=>b[1].items.length-a[1].items.length||a[0].localeCompare(b[0]));
  // 회사별로 묶으면 블록이 출원인 수만큼 생긴다 — 실측 163곳 가운데 112곳(69%)이
  // 1건뿐이라, 여러 건 낸 쉰 곳이 한 건짜리 백열두 곳에 묻혔다. 1건짜리는 이름만
  // 칩으로 모아 접어 두고(정보는 안 버린다) 블록은 여러 건 낸 곳에만 준다.
  let solo=[];
  if(mode!=='cat'){
    solo=order.filter(([,g])=>g.items.length===1).map(([k,g])=>({name:k, flag:g.flag, it:g.items[0]}));
    if(solo.length>=5) order=order.filter(([,g])=>g.items.length>1);
    else solo=[];
  }
  const blockHTML=([k,g])=>{
    // 기술별 블록 안에서는 많이 낸 곳부터 세운다. 그냥 두면 열두 줄이 우연히
    // 잡히는 순서라, 그 분야에서 누가 큰지가 안 보인다.
    let items=g.items;
    if(mode==='cat'){
      const cnt={}; items.forEach(it=>cnt[it.aName]=(cnt[it.aName]||0)+1);
      items=items.slice().sort((x,y)=> cnt[y.aName]-cnt[x.aName]
        || (x.aName||'').localeCompare(y.aName||''));
    }
    const lis=items.slice(0,KR_SHOW).map(it=>_krItemHTML(it, mode, cm)).join('');
    const more=items.length>KR_SHOW
      ? '<li class="kmore">… 외 '+(items.length-KR_SHOW).toLocaleString()+'건</li>':'';
    const head = mode==='cat'
      ? (cm[k].emoji+' '+esc(cm[k].name)
         + '<span class="kcnt">'+g.items.length.toLocaleString()+'건 · '
         + g.names.size.toLocaleString()+'곳</span>')
      : (flg(g.flag)+' '+esc(k)
         + '<span class="kcnt">'+g.items.length.toLocaleString()+'건</span>');
    return '<div class="krow"><div class="kap">'+head+'</div>'
      + '<ul class="klist">'+lis+more+'</ul></div>';
  };
  // 여러 건 낸 곳만 남겨도 쉰 곳이다 — 320px 화면에서 이 칸 하나가 14,000px 였다.
  // 많이 낸 곳부터 KR_BLOCKS 곳을 펴 두고 나머지는 접는다(기술별은 대여섯 개라 그냥 둔다).
  const blocks=order.slice(0, KR_BLOCKS).map(blockHTML).join('');
  const rest=order.slice(KR_BLOCKS);
  const restHTML=rest.length
    ? '<details class="ksolo krmore"><summary>'
      + (mode==='cat'? '나머지 분야 ' : '나머지 ')+rest.length.toLocaleString()+'곳 · '
      + rest.reduce((n,e)=>n+e[1].items.length,0).toLocaleString()+'건'
      + ' <span class="kcnt">펼쳐 보기</span></summary>'
      + '<div class="krwrap">'+rest.map(blockHTML).join('')+'</div></details>'
    : '';
  const seg='<span class="rankseg">'
    + '<button data-kr="ap" aria-pressed="'+(mode==='ap')+'" '
    + 'title="이 회사가 한국에 무엇을 걸어 뒀나">회사별</button>'
    + '<button data-kr="cat" aria-pressed="'+(mode==='cat')+'" '
    + 'title="이 기술에 누가 한국에 권리를 걸고 있나">기술별</button></span>';
  const lead = mode==='cat'
    ? '<b>기술별</b> — 이 기술에 <b>누가</b> 한국에 권리를 걸고 있는지 봅니다. '
      + '블록 안은 그 분야에 많이 낸 곳부터입니다.'
    : '<b>회사별</b> — 이 회사가 한국에 <b>무엇을</b> 걸어 뒀는지 봅니다.';
  // 칩으로 줄여도 112곳이면 여전히 벽이다. 접어 두고 세어만 준다 — 펼치면 다 있다.
  const soloHTML = solo.length
    ? '<details class="ksolo"><summary>1건씩 낸 곳 '
      + solo.length.toLocaleString()+'곳 <span class="kcnt">펼쳐 보기</span></summary>'
      + '<div class="kchips">'+solo.map(s=>
          '<a class="kchip" href="'+esc(safeUrl(s.it.url))+'" target="_blank" rel="noopener" '
          + 'title="'+esc(s.it.title)+'">'+flg(s.flag)+' '+esc(s.name)+'</a>').join('')
      + '</div></details>'
    : '';
  // 그림 둘(분야별 해외 비중·어디서 왔나)은 **홈 맨 아래**로 갔다. 여기는 개별 건을
  // 찾아보는 자리라 목록만 둔다 — 같은 그림을 두 곳에 그리지 않는다.
  return '<div class="panel wide krpanel" id="sec-krlist">'
    + '<h3>🇰🇷 해외 출원인의 국내 공개'+seg+'</h3>'
    // '해외 출원인이 한국에 공개한…' 두 문장은 홈 패널에 글자 그대로 또 있다.
    // 같은 설명을 두 곳에서 읽게 되므로 여기서는 뺀다(홈 쪽만 남긴다).
    + '<p class="sub">'+lead+' 제목을 누르면 원문으로 이동합니다. '
    + '<span class="morelink" data-jump2="home:">분야·국적 요약은 홈에서 →</span>'
    + '<br>※ 매주 출원인별로 국내 공개분을 조회해 모읍니다(출원인당 최대 '
    + (FEED.patents.krLimit||15)+'건). 쿼터에 걸리면 다음 주에 이어 채웁니다.</p>'
    + '<div class="krwrap">'+blocks+'</div>'+restHTML+soloHTML+'</div>';
}

// ── 분야별 경쟁 구도 ───────────────────────────────────────────────
// 표본을 그대로 세면 안 된다. 수집은 출원인당 상한(PER_APPLICANT_LIMIT)이 있어
// 큰 기업일수록 잘려 나가고, 잘린 곳들이 표본의 대부분을 차지한다. 실측하면
// LG에너지솔루션은 실제 2,264건인데 표본엔 52건뿐이었다 — 이 상태로 세면
// 재생에너지 분야가 '분산'(31%)으로 나오지만, 규모를 되돌리면 '소수 집중'(74%)이다.
//
// 그래서 두 값을 나눠 쓴다:
//   분야 구성비 → 표본에서 (그 출원인이 어느 분야에 내는지의 비율은 표본으로도 보인다)
//   규모        → OPS 가 알려준 실제 총계 (상한과 무관하게 정확하다)
//   추정 건수 = 실제 총계 × (그 분야 표본 / 그 출원인 표본 전체)
//
// 지표는 둘이다.
//   CR3   : 상위 3곳의 몫. 한눈에 읽히지만 4위 이하의 모양을 못 본다.
//   실질 N: 1/HHI(지분 제곱합의 역수). 규모 차이를 반영한 '실질 경쟁자 수'로,
//           출원인이 35곳이어도 셋이 다 가져가면 4곳 수준으로 나온다.
const CONC_MIN = 4;          // 등장 출원인이 이보다 적으면 CR3 가 항상 100% 라 무의미
// 집중도 등급의 경계. 처음엔 실질 5곳/8곳이었는데, 그때 표본은 미리 골라 둔 대기업
// 65곳뿐이라 실질 경쟁자 수가 한 자릿수로 나왔다. 지금은 분야+기간 전수(출원인
// 5천여 곳)라 실측이 10~239곳이다 → 옛 경계로는 여덟 분야가 전부 한 칸에 들어가
// 색도 축도 아무것도 구분하지 못했다(실측으로 확인). 그래서 지금 분포에 맞춰 다시
// 잡는다. 반독점 HHI 같은 절대 기준이 아니라 '여기 여덟 분야를 서로 견준' 상대
// 등급이라는 점을 화면에도 적는다.
const CONC_MID = 50, CONC_WIDE = 100;
const CONC_NAME = {hi:'상위권 쏠림', mid:'중간', lo:'고르게 분산'};
function concLevel(ne){ return ne<CONC_MID ? 'hi' : ne<CONC_WIDE ? 'mid' : 'lo'; }
const CONC_REL = '등급(상위권 쏠림·중간·고르게 분산)은 절대 기준이 아니라 이 아카이브의 '
  + '이 분야들을 서로 견준 상대 값입니다.';
function concentration(list){
  const ranked = _rankApplicants(list).filter(r=>r.name!=='(미상)');
  return (FEED.patents.categories||[]).map(c=>{
    const sh=[];
    ranked.forEach(r=>{ const g=r.grid[c.key]||0;
      if(!g || !r.cnt) return;
      sh.push({name:r.name, flag:r.flag, region:r.region,
               v:r.total*g/r.cnt, raw:g}); });
    if(!sh.length) return null;
    sh.sort((a,b)=> b.v-a.v || a.name.localeCompare(b.name));
    const tot = sh.reduce((s,x)=>s+x.v,0);
    // 보정 전(표본 그대로) 값도 같이 낸다 — 툴팁에서 보정 폭을 밝히기 위해서다.
    const rawSorted = sh.map(x=>x.raw).sort((a,b)=>b-a);
    const rawTot = rawSorted.reduce((s,v)=>s+v,0);
    return {cat:c, n:sh.length, tot,
      cr3: tot? sh.slice(0,3).reduce((s,x)=>s+x.v,0)/tot : 0,
      neff: tot? 1/sh.reduce((s,x)=>s+(x.v/tot)*(x.v/tot),0) : 0,
      rawCr3: rawTot? rawSorted.slice(0,3).reduce((s,v)=>s+v,0)/rawTot : 0,
      // 분야 × 출원인 국적 — 매트릭스에만 있고 집중도로는 뭉개지는 축이다.
      // 국내 지분이 낮은 분야는 국내에서 협상 상대를 찾기 어렵다는 뜻이라,
      // 거래로 보면 '도입이냐 자체 개발이냐' 를 가르는 값이다.
      krShare: tot? sh.filter(x=>x.region==='KR').reduce((s,x)=>s+x.v,0)/tot : 0,
      krN: sh.filter(x=>x.region==='KR').length,
      krTop: (sh.filter(x=>x.region==='KR')[0]||{}).name || '',
      top: sh.slice(0,3)};
  }).filter(Boolean).sort((a,b)=> b.cr3-a.cr3);
}

// 상위 보유자 칩. 수치는 건수가 아니라 분야 내 지분(%)이다 — 추정치를 건수로 쓰면
// 없는 정밀도를 있는 것처럼 보이게 한다. 총계를 깎아 쓴 출원인은 * 를 달고 툴팁에
// 근거를 적는다(수치를 조용히 고쳐 두지 않는다).
function concChip(t, tot){
  const a=(FEED.patents.totalsAdjusted||{})[t.name];
  const tip = a ? (t.name+' — 검색어가 넓어 자회사 문서까지 조회됐습니다. 표본 '+a.of
    +'건 중 실제로 이 회사 것이었던 '+a.kept+'건의 비율로, 총계를 '+a.raw
    +'건에서 깎아 썼습니다.') : '';
  return '<span class="cta"'+(tip? ' title="'+esc(tip)+'"' : '')+'>'
    + flg(t.flag) + ' ' + esc(t.name) + (a? '<span class="ctn">*</span>' : '')
    + '<span class="ctn">'+Math.round(t.v/tot*100)+'%</span></span>';
}

function concRowsHTML(rows){
  return rows.map(r=>{
    let metric;
    if(r.n < CONC_MIN){
      metric = '<span class="conc na" title="이 분야에 등장한 출원인이 '+r.n
        + '곳뿐이라 집중도를 계산하지 않습니다(3곳 이하면 상위 3곳 몫이 항상 100%)">'
        + '출원인 '+r.n+'곳</span>';
    } else {
      const pct = Math.round(r.cr3*100), ne = r.neff;
      const key = concLevel(ne), lv = [CONC_NAME[key], key];
      const tip = '상위 3곳이 이 분야의 '+pct+'%를 차지합니다. 등장 출원인은 '+r.n
        + '곳이지만 규모 차이를 반영하면 실질 경쟁자는 '+ne.toFixed(1)+'곳 수준입니다. '
        + '표본을 그대로 세면 '+Math.round(r.rawCr3*100)+'%지만, 출원인당 수집 상한 때문에 '
        + '큰 기업이 잘려 있어 실제 공개 총계로 규모를 되돌려 계산한 값입니다. ' + CONC_REL;
      metric = '<span class="conc '+lv[1]+'" title="'+esc(tip)+'">'
        + '<span class="cbar"><i style="width:'+pct+'%"></i></span>'
        + '<span class="cpct mono">'+pct+'%</span><span class="clv">'+lv[0]+'</span>'
        + '<span class="cn">실질 '+ne.toFixed(1)+'곳 / '+r.n+'곳</span></span>';
    }
    // 칩의 수치는 건수가 아니라 분야 내 지분(%)이다 — 추정치라 건수로 쓰면
    // 없는 정밀도를 있는 것처럼 보이게 한다.
    const chips = r.top.map(t=>concChip(t, r.tot)).join('');
    return '<div class="crow"><div class="clab">'+r.cat.emoji+' '+esc(r.cat.name)+metric+'</div>'
      + '<div class="ctops">'+(chips||'<span class="unknown">—</span>')+'</div></div>';
  }).join('');
}

// ── 분야별 국내 공급자 ────────────────────────────────────────────────
// 왜 필요한가: 이 사이트가 아직 답하지 못한 질문이 "그래서 누구한테 가면 되나"
// 였다. 경쟁 구도는 '누가 이 분야를 쥐고 있나'를 보여 주지만 그 상위는 대부분
// 대기업이라, 중소기업에게는 협상 상대가 아니라 회피 대상이다.
//
// 실제로 기술을 내놓는 쪽은 대학 산학협력단·출연연·공공기관이다. 기술이전
// 전담조직이 있어 거래가 제도로 굴러가고 실시 조건도 공개돼 있다. OPS 를 쓰던
// 때는 이들이 표본에 아예 없었다(큐레이션한 대기업 65곳만 봤다). KIPRIS 는
// 분야+기간만으로 조회돼 모집단이 통째로 들어오므로 이제 이름과 건수를 그대로
// 셀 수 있다 — 실측 첫 주 대학·연구기관 85곳 313건.
const SUP_KINDS = [
  ['대학', /산학협력단|대학교|대학(?!원생)|UNIV/i, '🎓'],
  ['출연연·연구기관', /연구원|연구소|연구재단|RESEARCH|INSTITUTE|KIST|ETRI/i, '🔬'],
  ['공공기관', /공사$|공사\\s|공단|진흥원|한국전력|KEPCO/i, '🏛'],
];
// 위 낱말들은 민간 회사 이름에도 그대로 들어간다 — '주식회사동일기술공사'는
// 공기업이 아니고 '주식회사 스탠더드시험연구소'는 출연연이 아니다. 법인격 표기가
// 붙어 있으면 민간이므로, 어느 갈래든 여기서 뺀다(공공기관에만 걸었더니 민간
// 시험연구소가 🔬 로 올라왔다 — 실측).
const SUP_COMPANY = /주식회사|㈜|\\(주\\)|유한회사|\\bCO\\.|\\bLTD\\b|\\bINC\\b/i;
function _supKind(name){
  const nm = name||'';
  if(SUP_COMPANY.test(nm)) return null;
  for(var i=0;i<SUP_KINDS.length;i++){
    if(!SUP_KINDS[i][1].test(nm)) continue;
    return {label:SUP_KINDS[i][0], icon:SUP_KINDS[i][2]};
  }
  return null;
}

// 분야별로 '공급자 성격의 국내 출원인'만 모은다. 대기업을 섞지 않는 것이 요점이다.
function suppliers(list){
  const cats = FEED.patents.categories||[];
  const byCat = {}, filedIn = {};
  (list||[]).forEach(it=>{
    // 공동출원인 전체는 원문(assignee)에만 남아 있다. aName 은 대표(첫) 출원인
    // 하나로 정리된 이름이다 — 수집기가 쪼개고 화면도 표시 직전에 한 번 더 쪼갠다.
    // 여기서 aName 을 쓰면 공동출원의 둘째·셋째가 통째로 빠진다.
    const raw = it.assignee || it.aName || ''; if(!raw || raw==='(미상)') return;
    if((it.aCountry||'') !== 'KR') return;          // 국내 주체만
    // 공동출원은 '|' 로 이어져 있다. 대표(첫) 출원인만 세면 칩의 숫자와 칩을
    // 눌렀을 때 열리는 목록이 어긋난다 — 목록 검색은 이름이 어느 자리에 있든
    // 걸리기 때문이다(한국원자력연구원 칩 11 vs 목록 12, 실측). 참여를 세는 쪽이
    // '이 기관에 말을 걸 수 있는 건'이라는 이 표의 뜻에도 맞다.
    const parts = raw.indexOf('|')>=0 ? raw.split('|') : [raw];
    const seen = {};
    parts.forEach(p0=>{
      const nm = (p0||'').trim(); if(!nm || seen[nm]) return; seen[nm]=1;
      const kind = _supKind(nm); if(!kind) return;  // 대학·출연연·공공만
      const m = byCat[it.category] || (byCat[it.category] = {});
      const o = m[nm] || (m[nm] = {name:nm, kind:kind, cnt:0});
      o.cnt++;
      // 분야 합계는 건수여야 한다. 기관별 건수를 더하면 공동출원이 두 번 세진다.
      (filedIn[it.category] || (filedIn[it.category] = new Set()))
        .add(it.id || it.number || it.url || (it.title+'|'+(it.date||'')));
    });
  });
  return cats.map(c=>{
    const m = byCat[c.key]; if(!m) return null;
    const orgs = Object.keys(m).map(k=>m[k])
      .sort((a,b)=> b.cnt-a.cnt || a.name.localeCompare(b.name));
    return {cat:c, orgs:orgs, n:orgs.length,
            total:(filedIn[c.key]||{size:0}).size};
  }).filter(Boolean).sort((a,b)=> b.n-a.n || b.total-a.total);
}

const SUP_TOP = 6;          // 한 분야에 보여 줄 기관 수(나머지는 '외 N곳')
function supplierHTML(list){
  const rows = suppliers(list);
  if(!rows.length) return '';
  const orgAll = new Set();
  rows.forEach(r=>r.orgs.forEach(o=>orgAll.add(o.name)));
  const tot = rows.reduce((s,r)=>s+r.total,0);
  // 막대는 분야끼리 '기관 수'를 눈으로 견주기 위한 것이다. 단일 계열이라 범례가
  // 필요 없고, 색은 옆 표(.catlead)와 같은 단일 램프를 쓴다 — 한 화면에서 색
  // 언어를 둘로 쓰면 서로 다른 지표로 오해된다. 뜻은 글자가 지므로 글자는
  // 색 램프가 아니라 본문 색을 입는다.
  const maxN = Math.max.apply(null, rows.map(r=>r.n));
  const body = rows.map(r=>{
    const chips = r.orgs.slice(0, SUP_TOP).map(o=>
      '<span class="cta sup" role="button" tabindex="0" data-sup="'+esc(o.name)
      + '" data-supcat="'+esc(r.cat.key)+'" title="'+esc(o.name+' — 이 분야 '+o.cnt
      + '건. 누르면 특허 탭에서 이 기관의 해당 분야 공개 건을 봅니다.')+'">'
      + o.kind.icon+' '+esc(o.name)+'<span class="ctn">'+o.cnt+'</span></span>').join('');
    const more = r.orgs.length > SUP_TOP
      // 클래스 이름을 'more' 로 쓰면 안 된다 — 전역 '더 보기' 버튼(.more)이
      // display:block; margin:14px auto 로 잡혀 있어 칩이 줄 가운데로 밀려난다(실측).
      ? '<span class="cta supmore">외 '+(r.orgs.length-SUP_TOP)+'곳</span>' : '';
    // 성격별 구성. 한전 한 곳이 분야 건수를 대부분 차지하는 일이 잦아(송·변전 198건),
    // 총계만 보이면 '한전 목록'처럼 읽힌다 → 대학·출연연이 몇 곳인지 함께 밝힌다.
    const mix = {};
    r.orgs.forEach(o=>{ mix[o.kind.label]=(mix[o.kind.label]||0)+1; });
    const mixTxt = ['대학','출연연·연구기관','공공기관']
      .filter(k=>mix[k]).map(k=>k.replace('출연연·연구기관','연구기관')+' '+mix[k]).join(' · ');
    const metric = '<span class="conc lo" title="'+esc('이 분야에 최근 공개가 있는 국내 '
      + '대학·출연연·공공기관이 '+r.n+'곳, 합계 '+r.total+'건입니다. 구성: '+mixTxt)+'">'
      + '<span class="cbar"><i style="width:'+Math.round(r.n/maxN*100)+'%"></i></span>'
      + '<span class="cn">'+mixTxt+' · '+r.total+'건</span></span>';
    return '<div class="crow"><div class="clab">'+r.cat.emoji+' '+esc(r.cat.name)+metric
      + '</div><div class="ctops">'+chips+more+'</div></div>';
  }).join('');
  return '<div class="sec" id="sec-supply">🎓 분야별 국내 공급자</div>'
    + '<p class="gdesc">이 분야에 최근 공개가 있는 <b>대학 산학협력단·출연연·공공기관</b>입니다. '
    + '기술이전 전담조직이 있어 먼저 두드릴 상대입니다(대기업은 협상 상대라기보다 '
    + '회피 대상이라 뺐습니다). 기관 이름을 누르면 그 분야 공개 건이 열립니다.</p>'
    + '<div class="catlead sup">'+body+'</div>'
    + '<p class="gnote">' + rows.length + '개 분야 · 기관 ' + orgAll.size + '곳 · ' + tot + '건'
    + '(최근 ' + (FEED.patents.lookbackDays||90) + '일 국내 공개분, 보유 특허 전체 아님). '
    + '목록에 있다고 이전 의사가 있다는 뜻은 아닙니다 — 문의는 아래 창구로.</p>';
}

function renderStats(list){
  if(!list.length) return '<div class="empty">조건에 맞는 특허가 없습니다.</div>';
  const cats=FEED.patents.categories, regions=FEED.patents.countries;
  const ranked=_rankApplicants(list);
  const uniq=ranked.length, topA=ranked[0];
  const regCnt={}; ranked.forEach(r=>{ regCnt[r.region]=(regCnt[r.region]||0)+1; });
  // 국적을 모르는 출원인을 세어 함께 밝힌다.
  //
  // 해외 목록에는 출원인 국적이 없다. 큐레이션 목록에 있는 곳(Siemens·Toyota…)은
  // 알지만 나머지는 모르고, 공개국으로 대신 채우면 'US 에 낸 일본 회사'가 미국
  // 기업이 되므로 비워 둔다. 그런데 비워 둔 것을 화면에서 빼기만 하면 '🇺🇸8곳'
  // 처럼 보여, 미국 기업이 여덟 곳뿐인 것으로 읽힌다 — 실제로 그렇게 보였다
  // (전체 5,403곳 중 국적을 아는 곳이 1,186곳뿐이었다).
  // 모르는 것은 모른다고 두되, **모른다는 사실도 화면에 남긴다**.
  const split = regionSplit(ranked), unknown = split.unknown;
  const known = uniq - unknown;
  const regChips=regions.map(rg=>regCnt[rg.code]?(flg(rg.emoji)+regCnt[rg.code]):'').filter(Boolean).join(' ')
    + (split.other? ' <span title="'+esc('국적 축 다섯 갈래(미국·한국·중국·일본·유럽) '
        + '밖의 나라입니다. 아래 매트릭스의 [기타] 묶음에서 나라별로 볼 수 있습니다.')
        + '">🌐'+split.other.toLocaleString()+'</span>' : '')
    + (unknown? ' <span class="unk" title="'
        + esc(originNote(unknown)
              + ' 국적별 랭킹에는 국적을 아는 ' + known.toLocaleString() + '곳만 들어갑니다.')
        + '">국적미상 '+unknown.toLocaleString()+'</span>' : '');
  const catLeadRows = concRowsHTML(concentration(list));
  // 랭킹(전 지역 통합). 수집 상한에 걸린 곳은 실제 건수가 그 이상이라 '50+' 로 표기하고
  // 막대도 구분한다 — 상한 동점끼리 순위를 매기면 정렬 우연을 실력처럼 보여주게 된다.
  const nSampled=ranked.filter(r=>r.sampled).length;
  const leadRows = rankMode==='region' ? regionRankHTML(ranked)
    : rankMode==='office' ? officeRankHTML(ranked)
    : rankRowsHTML(ranked.slice(0,15));
  const exactOffice = Object.keys(FEED.patents.officeCounts||{}).length>0;
  const rankSub = rankMode==='region'
      ? '<b>출원인 국적별</b> — 그 나라 기업 중 다출원 순서(지역별 상위 5) · 실제 공개 건수 기준.'
        + (nSampled? ' 사선 막대 '+nSampled+'곳은 목록에 표본만 저장돼 있습니다.' : '')
        // 이 표는 국적을 아는 곳만 담는다. 그 사실을 적지 않으면 빠진 곳이
        // '해당 나라에 없는 것'으로 읽힌다.
        + (unknown? ' 국적 미확인 '+unknown.toLocaleString()
            + '곳은 제외했습니다(매일 채우는 중) — 전체는 [전체]·[공개국별]로 보세요.' : '')
    : rankMode==='office'
      ? '<b>공개 특허청(시장)별</b> — 그 특허청에 많이 공개한 기업(국적 무관, 상위 5). '
        + (exactOffice? '실제 공개 건수 기준.' : '표본 기반 근사치(다음 수집부터 정확).')
        + ' 같은 발명이 여러 나라에 공개되므로 특허청별 합계는 출원인 총계를 넘을 수 있습니다.'
      : '전 지역 통합 상위 15 · 실제 공개 건수 기준(수집 상한과 무관).';

  return '<div class="stats">'
    + '<div class="panel wide"><div class="statkpi">'
      + '<div><div class="k">분석 출원인</div><div class="v mono">'+uniq.toLocaleString()
        + ' <span style="font-size:12px;color:var(--muted)">'+regChips+'</span></div></div>'
      + '<div><div class="k">수집 특허(표본)</div><div class="v mono">'+list.length.toLocaleString()+'</div></div>'
      + '<div><div class="k">최다 출원인</div><div class="v">'+flg(topA.flag)+' '+esc(topA.name)
        + ' <span style="font-size:14px;color:var(--muted)" class="mono">'+topA.total+'건</span></div></div>'
      + '</div></div>'
    + '<div class="panel"><h3>🧭 분야별 경쟁 구도</h3>'
      + '<p class="sub">각 분야를 <b>몇 곳이 나눠 갖고 있는지</b>입니다. 막대는 <b>상위 3곳의 몫</b>, '
      + '‘실질 N곳’은 규모 차이를 반영한 경쟁자 수입니다. 칩의 %는 그 분야 안 지분입니다.<br>'
      + '<b>※ 시장 점유율이 아닙니다</b> — 최근 '+(FEED.patents.lookbackDays||90)
      + '일 공개분의 분포이며 보유 특허 전체가 아닙니다.</p>'
      + '<div class="catlead">'+catLeadRows+'</div></div>'
    + '<div class="panel"><h3>🏆 출원인 랭킹'
      + '<span class="rankseg">'
      + '<button data-rank="region" aria-pressed="'+(rankMode==='region')+'" title="그 나라 기업 중 다출원">출원인 국적별</button>'
      + '<button data-rank="office" aria-pressed="'+(rankMode==='office')+'" title="그 특허청에 많이 낸 기업(국적 무관)">공개국별</button>'
      + '<button data-rank="all" aria-pressed="'+(rankMode==='all')+'">전체</button></span></h3>'
      + '<p class="sub">'+rankSub+'</p>'
      + '<div class="lead">'+leadRows+'</div></div>'
    // 국내 공개는 **목록**이라 아래로 내린다. 위의 둘은 전체를 요약한 집계고,
    // 요약을 먼저 보고 개별 건으로 내려가는 것이 읽는 차례다.
    + krEntryHTML(list)
    + '</div>';
}

function updateViewToggle(){
  const vt = $('#viewToggle');
  vt.classList.toggle('on', state.tab==='patents');
  vt.querySelectorAll('button').forEach(b=>b.setAttribute('aria-pressed', b.dataset.view===state.view));
}

function syncHash(){
  const p = new URLSearchParams();
  p.set('tab', state.tab);
  if(state.q) p.set('q', state.q);
  if(state.cats.size) p.set('cat', [...state.cats].join(','));
  if(state.countries.size) p.set('co', [...state.countries].join(','));
  if(state.sort!=='new') p.set('sort', state.sort);
  if(state.newonly) p.set('new','1');
  if(state.period!=='all') p.set('period', state.period);
  if(state.source) p.set('src', state.source);
  if(state.savedOnly) p.set('saved','1');
  if(state.unreadOnly) p.set('unread','1');
  if(state.tab==='patents' && state.view==='stats') p.set('view','stats');
  const h = p.toString();
  history.replaceState(null,'', h? '#'+h : location.pathname);
}
function loadHash(){
  const p = new URLSearchParams(location.hash.replace(/^#/,''));
  const t=p.get('tab'); if(t==='news'||t==='patents'||t==='home'||t==='guide') state.tab=t;
  if(state.tab==='patents' && p.get('view')==='stats') state.view='stats';
  state.q = p.get('q')||'';
  state.sort = p.get('sort')==='old'?'old':'new';
  state.newonly = p.get('new')==='1';
  state.period = p.get('period')||'all';
  state.source = p.get('src')||'';
  state.savedOnly = p.get('saved')==='1';
  state.unreadOnly = p.get('unread')==='1';
  if(p.get('cat')) p.get('cat').split(',').forEach(c=>state.cats.add(c));
  if(p.get('co')) p.get('co').split(',').forEach(c=>state.countries.add(c));
}

function syncTabsUI(){ document.querySelectorAll('.tabs button')
  .forEach(b=>b.setAttribute('aria-selected', b.dataset.tab===state.tab)); }

// 홈에서 특정 탭으로 이동하며 필터를 적용(키워드→검색, 카테고리·매트릭스→필터)
function gotoTab(t, opts){ opts=opts||{};
  state.tab=t; state.view=opts.view||'list';
  resetFilters();
  state.q=opts.q||''; if(opts.cat) state.cats.add(opts.cat);
  const q=$('#q'); if(q) q.value=state.q;
  syncTabsUI(); updateViewToggle(); renderChips(); render();
  window.scrollTo({top:0, behavior:'smooth'});
}

function resetFilters(){
  state.q=''; state.cats.clear(); state.countries.clear();
  state.period='all'; state.source=''; state.limit=PAGE;
  state.newonly=false; state.savedOnly=false; state.unreadOnly=false;
  const q=$('#q'); if(q) q.value='';
  ['newonly','savedonly','unreadonly'].forEach(id=>{
    const b=$('#'+id); if(b) b.setAttribute('aria-pressed','false'); });
}

function setTab(t){
  if(state.tab===t) return;
  // 탭을 바꾸면 필터를 모두 초기화한다. 특히 홈은 검색창이 숨겨져 있어
  // 남아있는 검색어/토글을 사용자가 인지·해제할 방법이 없다.
  state.tab=t; state.view='list';
  resetFilters();
  syncTabsUI();
  updateViewToggle(); renderChips(); render();
}

function wire(){
  document.querySelectorAll('.tabs button').forEach(b=> b.onclick=()=>setTab(b.dataset.tab));
  // 상단 제목 → 홈. 이미 홈이면 setTab 이 그냥 빠져나오므로 맨 위로 올려주기만 한다.
  $('#brand').onclick = ()=>{ setTab('home'); window.scrollTo({top:0, behavior:'smooth'}); };
  $('#viewToggle').onclick = e=>{ const b=e.target.closest('[data-view]'); if(!b) return;
    state.view=b.dataset.view; updateViewToggle(); render(); };
  let deb; $('#q').oninput = e=>{ clearTimeout(deb); deb=setTimeout(()=>{ state.q=e.target.value.trim(); state.limit=PAGE; render(); },140); };
  $('#sort').onchange = e=>{ state.sort=e.target.value; render(); };
  $('#newonly').onclick = e=>{ state.newonly=!state.newonly; e.currentTarget.setAttribute('aria-pressed',state.newonly); state.limit=PAGE; render(); };
  $('#catChips').onclick = e=>{ const b=e.target.closest('[data-cat]'); if(!b) return;
    const k=b.dataset.cat; state.cats.has(k)?state.cats.delete(k):state.cats.add(k);
    b.setAttribute('aria-pressed',state.cats.has(k)); state.limit=PAGE; render(); };
  $('#countryChips').onclick = e=>{ const b=e.target.closest('[data-country]'); if(!b) return;
    const k=b.dataset.country; state.countries.has(k)?state.countries.delete(k):state.countries.add(k);
    b.setAttribute('aria-pressed',state.countries.has(k)); state.limit=PAGE; render(); };
  $('#more').onclick = ()=>{ state.limit+=PAGE; render(); };
  $('#savedonly').onclick = e=>{ state.savedOnly=!state.savedOnly; e.currentTarget.setAttribute('aria-pressed',state.savedOnly); state.limit=PAGE; render(); };
  $('#unreadonly').onclick = e=>{ state.unreadOnly=!state.unreadOnly; e.currentTarget.setAttribute('aria-pressed',state.unreadOnly); state.limit=PAGE; render(); };
  $('#source').onchange = e=>{ state.source=e.target.value; state.limit=PAGE; render(); };
  $('#periodBar').onclick = e=>{ const b=e.target.closest('[data-period]'); if(!b) return;
    const p=b.dataset.period; state.period=(p===state.period && p!=='all')?'all':p; state.limit=PAGE; render(); };
  $('#overview').onclick = e=>{ const r=e.target.closest('rect[data-x]'); if(!r) return;
    const x=r.getAttribute('data-x'); state.period=(state.period===x?'all':x); state.limit=PAGE; render(); };
  $('#guide').onclick = e=>{
    if(e.target.closest('[data-gohome]')){
      setTab('home');
      const t=document.getElementById('sec-analysis');
      if(t) t.scrollIntoView({behavior:'smooth', block:'start'});
      return; }
    const j=e.target.closest('[data-jump]');
    if(j){ const t=document.getElementById(j.getAttribute('data-jump'));
      if(t) t.scrollIntoView({behavior:'smooth', block:'start'}); return; }
    // 공급자 칩 → 특허 탭에서 그 기관의 해당 분야 공개 건으로 좁힌다.
    // 이 절은 반드시 #guide 에 달아야 한다 — 공급자 표는 #results 가 아니라
    // #guide 안에 있어서, #results 위임에 달았더니 클릭이 먹지 않았다(실측).
    const sp=e.target.closest('[data-sup]');
    if(sp){ gotoTab('patents', {q: sp.getAttribute('data-sup'),
                                cat: sp.getAttribute('data-supcat')}); return; }
    // 국유특허 더 보기/접기 — 접을 때는 그 자리로 돌려놔야 화면이 튀지 않는다.
    if(e.target.closest('[data-more="staown"]')){
      const back=staownAll;
      staownAll=!staownAll; renderGuide();
      if(back){ const t=document.getElementById('sec-staown');
        if(t) t.scrollIntoView({block:'start'}); }
      return; }
  };
  // '전체 보기 →' 같은 탭 건너뛰기 다리. data-jump 는 같은 화면 안에서만 움직이므로
  // 탭 이름을 함께 싣는다 — '탭:앵커' 또는 '탭:앵커:보기'(특허 탭의 통계 보기처럼
  // 탭만으로는 닿지 않는 자리가 있다). 앵커가 비면 탭 맨 위로 간다.
  //
  // **문서 전체**에 단다. 전에는 #home 에만 달려 있어서 홈 밖에 놓은 다리는 눌러도
  // 아무 일이 없었다 — 바로 위 data-jump 주석이 같은 실수를 이미 적어 두고 있다.
  document.addEventListener('click', e=>{
    const j2=e.target.closest && e.target.closest('[data-jump2]');
    if(!j2) return;
    const v=(j2.getAttribute('data-jump2')||'').split(':');
    gotoTab(v[0], v[2]? {view:v[2]} : undefined);
    if(!v[1]) return;
    setTimeout(()=>{ const t=document.getElementById(v[1]);
      if(t) t.scrollIntoView({behavior:'smooth', block:'start'}); }, 60);
  });
  // 공급자 칩은 role=button·tabindex=0 이라 키보드로도 눌려야 한다.
  $('#guide').addEventListener('keydown', e=>{
    if(e.key!=='Enter' && e.key!==' ') return;
    const sp=e.target.closest && e.target.closest('[data-sup]');
    if(!sp) return;
    e.preventDefault(); sp.click();
  });
  $('#home').onclick = e=>{
    // '오늘의 한 줄' → 그 브리핑 패널로 내려간다.
    // 위임은 **그 요소가 들어 있는 컨테이너**에 달아야 한다. [data-jump] 처리기가
    // #guide 에만 있어서, 홈에 올린 줄은 눌러도 아무 일이 없었다(실측) —
    // 바로 아래 data-sup 주석이 같은 교훈을 이미 적어 두고 있었다.
    const tj=e.target.closest('[data-jump]');
    if(tj){ const t=document.getElementById(tj.getAttribute('data-jump'));
      if(t){ // 접혀 있으면 펴 준다 — 내려갔는데 접힌 카드만 보이면 헛걸음이다.
        if(t.classList.contains('collapsed')){ briefCollapsed=false;
          localStorage.setItem('pnp_briefClosed','0'); renderHome(); }
        const t2=document.getElementById(tj.getAttribute('data-jump'));
        (t2||t).scrollIntoView({behavior:'smooth', block:'start'}); }
      return; }
    // 브리핑 접기/펼치기
    if(e.target.closest('#briefToggle')){ briefCollapsed=!briefCollapsed;
      localStorage.setItem('pnp_briefClosed', briefCollapsed?'1':'0');
      // 홈 전체가 아니라 브리핑 카드만 교체 → 펼쳐둔 '지난 브리핑' 타임라인이 유지된다.
      const cur=$('#home').querySelector('.brief');
      if(cur){ const tmp=document.createElement('div'); tmp.innerHTML=briefHTML();
        const next=tmp.firstElementChild; if(next) cur.replaceWith(next); }
      else renderHome();
      return; }
    // 지난 브리핑 더 보기/접기 — 홈 전체가 아니라 그 패널만 갈아 끼운다(브리핑
    // 카드나 특허 브리핑을 펼쳐 둔 상태가 날아가지 않게). 패널을 새로 그리면 그
    // 안에서 펼쳐 둔 항목은 닫히므로, 날짜를 기억했다가 되살린다.
    // 홈 요약 줄 → 경쟁·활용 탭의 그 분야 카드로. 홈에서 고르고 거기서 상대를 본다.
    // '전체 보기' → 다른 탭의 특정 자리로. data-jump 는 같은 화면 안에서만
    // 움직이므로 탭을 건너뛰려면 탭 이름을 함께 실어야 한다('탭:앵커').
    // 결론 한 줄의 다리 → 특허 탭에서 그 분야만 본다.
    const cp=e.target.closest('[data-catpat]');
    if(cp){ gotoTab('patents', {cat: cp.getAttribute('data-catpat')}); return; }
    const cg=e.target.closest('[data-catgo]');
    if(cg){ const k=cg.getAttribute('data-catgo');
      // 요약줄과 카드가 이제 **같은 탭**에 있다. 그래도 gotoTab 을 부르는 이유:
      // 이 처리기는 홈에도 남아 있고(다른 곳에서 부를 수 있다), 이미 그 탭이면
      // gotoTab 이 아무 일도 하지 않는다.
      gotoTab('guide');
      setTimeout(()=>{ const t=document.querySelector('.trow[data-cat="'+k+'"]');
        (t||document.getElementById('sec-analysis'))
          .scrollIntoView({behavior:'smooth', block:'start'});
        if(t){ t.classList.add('hit'); setTimeout(()=>t.classList.remove('hit'), 1600); }
      }, 60);
      return; }
    if(e.target.closest('[data-more="tl"]')){
      tlAll=!tlAll;
      const cur=document.getElementById('tlpanel');
      if(cur){
        const open=new Set([...cur.querySelectorAll('.tl.open .tld')].map(x=>x.textContent));
        const tmp=document.createElement('div'); tmp.innerHTML=timelineHTML();
        const next=tmp.firstElementChild;
        if(next){
          next.querySelectorAll('.tl').forEach(el=>{
            const d=el.querySelector('.tld');
            if(d && open.has(d.textContent)) el.classList.add('open'); });
          cur.replaceWith(next);
        }
      } else renderHome();
      return; }
    // 지난 브리핑 타임라인 펼치기
    const tl=e.target.closest('.tl'); if(tl && !e.target.closest('[data-go]')){ tl.classList.toggle('open'); return; }
    // 키워드 → 뉴스 탭에서 검색
    const kw=e.target.closest('[data-kw]'); if(kw){ gotoTab('news', {q:kw.getAttribute('data-kw')}); return; }
    // 특허 통계 전체 보기
    // data-go="patents-stats" → 통계 뷰, "patents" → 목록 뷰(브리핑 전문이 그 위에 있다)
    const go=e.target.closest('[data-go]');
    if(go){ gotoTab('patents', go.getAttribute('data-go')==='patents-stats'?{view:'stats'}:{}); return; }
    // 매트릭스 칸 → 특허 탭에서 그 출원인·분야
    const mc=e.target.closest('.pmx td.has[data-ap]');
    if(mc){ gotoTab('patents', {q:mc.getAttribute('data-ap'), cat:mc.getAttribute('data-cat')}); return; }
    // 이슈 흐름 행 → 뉴스 탭 카테고리 필터
    const row=e.target.closest('.trend [data-cat]');
    if(row){ gotoTab('news', {cat:row.getAttribute('data-cat')}); return; }
  };
  $('#results').addEventListener('click', e=>{
    // 랭킹 보기 토글(국적별 / 전체)
    const rb=e.target.closest('[data-rank]');
    if(rb){ rankMode=rb.getAttribute('data-rank');
      localStorage.setItem('pnp_rankMode', rankMode); render(); return; }
    // 국내 공개 보기 방식(회사별/기술별) — 패널 하나만 갈아 끼운다.
    // 통계 전체를 다시 그리면 위에서 펼쳐 둔 국적 묶음이 그대로 있어도 스크롤이 튄다.
    const kb=e.target.closest('[data-kr]');
    if(kb){ krMode=kb.getAttribute('data-kr');
      localStorage.setItem('pnp_krMode', krMode);
      const cur=document.querySelector('.krpanel');
      if(cur){ const tmp=document.createElement('div');
        tmp.innerHTML=krEntryHTML(filtered());
        const next=tmp.firstElementChild; if(next) cur.replaceWith(next); }
      else render();
      return; }
    // 국적 묶음 펼치기/접기 — 통계 패널 전체가 아니라 그 묶음만 갈아 끼운다.
    // 전체를 다시 그리면 다른 묶음의 펼침 상태는 살아남아도 스크롤 위치가 튄다.
    const mb=e.target.closest('[data-mtx]');
    if(mb){
      const code=mb.getAttribute('data-mtx');
      mtxOpen.has(code)? mtxOpen.delete(code) : mtxOpen.add(code);
      const sec=mb.closest('.rgsec');
      const rg=_mtxGroups(filtered()).find(g=>g.code===code);
      if(sec && rg){
        const tmp=document.createElement('div');
        tmp.innerHTML=_rgSecHTML(rg, filtered(), {total:true, top:MTX_TOP});
        const next=tmp.firstElementChild;
        // 접을 때는 그 자리로 돌려놔야 화면이 튀지 않는다(국유특허와 같은 처리).
        if(next){ const shrink=!mtxOpen.has(code); sec.replaceWith(next);
          if(shrink) next.scrollIntoView({block:'start'}); }
      }
      return; }
    // 매트릭스 칸 클릭 → 그 출원인·분야로 좁혀 목록 보기
    const mc=e.target.closest('.pmx td.has[data-ap]');
    if(mc){ state.q=mc.getAttribute('data-ap'); $('#q').value=state.q;
      state.cats=new Set([mc.getAttribute('data-cat')]); state.view='list';
      state.limit=PAGE; updateViewToggle(); renderChips(); render();
      $('#results').scrollIntoView({behavior:'smooth',block:'start'}); return; }
    const sb=e.target.closest('[data-save]');
    if(sb){ e.preventDefault(); const u=sb.getAttribute('data-save');
      saved.has(u)?saved.delete(u):saved.add(u); persist(); render(); return; }
    const a=e.target.closest('a.t[data-read]');
    if(a){ const u=a.getAttribute('data-read'); if(!read.has(u)){ read.add(u); persist();
      const cd=a.closest('.card'); if(cd) cd.classList.add('isread'); } }
  });
  $('#reset').onclick = ()=>{ state.q=''; state.cats.clear(); state.countries.clear(); state.newonly=false;
    state.period='all'; state.source=''; state.savedOnly=false; state.unreadOnly=false;
    state.limit=PAGE; $('#q').value='';
    ['newonly','savedonly','unreadonly'].forEach(id=>$('#'+id).setAttribute('aria-pressed','false'));
    document.querySelectorAll('.chips .f').forEach(b=>b.setAttribute('aria-pressed','false')); render(); };
  const toTop=$('#toTop');
  addEventListener('scroll', ()=>{ toTop.hidden = scrollY < 500; }, {passive:true});
  toTop.onclick = ()=> scrollTo({top:0, behavior:'smooth'});
  addEventListener('keydown', e=>{
    const tag=(document.activeElement&&document.activeElement.tagName)||'';
    if(e.key==='/' && !/^(INPUT|TEXTAREA|SELECT)$/.test(tag)){ e.preventDefault(); $('#q').focus(); }
    else if(e.key==='Escape' && document.activeElement===$('#q')){ $('#q').blur(); }
  });
}

// 건수는 FEED.*.total 을 쓴다 — items 는 지연 로딩 중이면 최근분뿐이라,
// items.length 로 적으면 '아카이브가 줄었다'로 읽힌다.
$('#foot').innerHTML = '뉴스: Google 뉴스 RSS(매일 수집) · 특허: KIPRISplus 공식 API에서 '
  + catScopeText()+'로 매주 수집 — 국내 공보와 해외(미국·유럽·일본·중국) 공보 '
  + '(1회 조회 범위 = 최근 '
  + (FEED.patents.lookbackDays||90) + '일 공개분, 새로 공개된 것만 누적). '
  + '제목·요약·링크는 원문으로 연결됩니다. 본 사이트는 이슈 아카이브용이며 특정 투자·정책 판단을 권유하지 않습니다.'
  + '<br>최종 갱신 <b class="mono">'+esc(FEED.generated)+'</b> · 뉴스 '+count('news')
  + '건 · 특허 '+count('patents')+'건'
  // 기관 홈페이지로 되걸어 공식 서비스임을 확인할 수 있게 한다(주소가 기관 도메인이 아니다).
  + (FEED.org? '<br>운영 <b>'+esc(FEED.org)+'</b>'
      + (FEED.orgUrl? ' · <a href="'+esc(safeUrl(FEED.orgUrl))+'" target="_blank" rel="noopener">'
          + esc(FEED.club || '기관 홈페이지') + '</a>' : '')
      : '');

loadHash();
$('#q').value = state.q;
$('#sort').value = state.sort;
$('#newonly').setAttribute('aria-pressed', state.newonly);
$('#savedonly').setAttribute('aria-pressed', state.savedOnly);
$('#unreadonly').setAttribute('aria-pressed', state.unreadOnly);
document.querySelectorAll('.tabs button').forEach(b=>b.setAttribute('aria-selected', b.dataset.tab===state.tab));
updateViewToggle(); renderChips(); wire(); render();
// 첫 화면을 그린 **뒤에** 나머지를 받는다. 순서가 바뀌면 지연 로딩의 뜻이 없다.
hydrate();
localStorage.setItem(LS_KEY, Date.now());
"""
