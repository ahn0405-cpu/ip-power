"""전력 특허 수집 — KIPRISplus 해외특허(미국·유럽·일본·중국).

국내(patent_source_kipris)와 **같은 항목 스키마**를 돌려주므로 목록에 그대로 섞인다.
다만 API 는 국내와 계열이 완전히 다르다. 하나라도 섞으면 조용히 깨지므로 여기
전부 적어 둔다 — 전부 러너 실측이다(2026-08-25).

                국내(kipo-api)              해외(openapi/rest)
  기준 경로     /kipo-api/kipi              /openapi/rest
  서비스        patUtiModInfoSearchSevice   ForeignPatentAdvencedSearchService
  키 질의       ServiceKey                  accessKey
  분류 파라미터 ipcNumber                   ipc
  항목 태그     <item>                      <searchResult>
  성공 표시     resultCode=00               resultCode 가 **빈 값**
  총 건수       totalCount                  totalSearchCount
  쪽넘김        pageNo(쪽) + numOfRows      currentPage(시작 위치) + docsCount

주의해야 할 것 셋
--------------------------------------------------
1) 서비스 이름의 오타가 진짜다 — ForeignPatent**Advenced**SearchService.
   철자를 고친 Advanced 는 '경로 없음'(포털 HTML)이 온다. 실측으로 확인했다.

2) 성공 판정이 국내와 **정반대**다. 해외는 정상일 때 resultCode 가 비어 있고,
   실패해야 채워진다(대상국 누락 → 11 No Mandatory Request Parameters Error).
   국내 규칙(00 이 아니면 오류)을 그대로 쓰면 정상 응답을 전부 실패로 읽는다.

3) currentPage 는 **쪽 번호가 아니라 '몇 번째 건부터'** 다. 같은 질의로 1·2·3 을
   받아 보니 2쪽의 첫 건이 1쪽의 두 번째, 3쪽의 첫 건이 1쪽의 세 번째였다.
   쪽으로 알고 docsCount=50 씩 넘기면 각 쪽이 49건씩 겹치면서 뒤쪽 자료에는
   영원히 닿지 못한다. 그래서 시작 위치를 docsCount 만큼 더해 넘긴다.

못 하는 것(정직하게 남긴다)
--------------------------------------------------
· CPC 검색이 없다. cpc 파라미터를 넣으면 오류가 아니라 **무시**된다(Y04S 를 넣었더니
  월마트 쇼핑공간 특허가 왔다). 서지상세에 cpcInfo 칸은 있으나 표본에서 비어 있다.
  → 해외도 국내와 같이 IPC 로만 분류한다.
· 목록에 출원인 **국적**이 없다. 큐레이션 별칭에 걸리는 곳(Siemens·Toyota…)은
  국적을 알지만, 나머지는 모른다. 공개국(countryCode)으로 대신 채우면 'US 에 낸
  일본 회사'가 미국 기업으로 둔갑하므로 비워 둔다 — 모르는 것은 모른다고 둔다.
  (서지상세에 applicantCountry 가 있지만 건마다 한 번씩 더 불러야 해 쓰지 않는다.)

키는 GitHub Secret(KIPRIS_KEY) → 환경변수로만 받는다. 파일에 적지 않는다.
"""
from __future__ import annotations

import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime

import kipris_rate
import patent_config as cfg
import patent_source_kipris as kr      # 분류·별칭·날짜 도우미를 함께 쓴다


# ── 요청 ─────────────────────────────────────────────────────────
def _url(params: dict) -> str:
    q = dict(params)
    q[cfg.FOREIGN_KEYPARAM] = cfg.KIPRIS_KEY
    return (f"{cfg.FOREIGN_BASE}/{cfg.FOREIGN_SERVICE}/{cfg.FOREIGN_OP}?"
            + urllib.parse.urlencode(q))


def _get(params: dict, timeout: int | None = None) -> ET.Element:
    kipris_rate.acquire()   # 초당 천장(모든 KIPRIS 호출이 지난다)
    req = urllib.request.Request(_url(params), headers={
        "User-Agent": "ip-power/1.0", "Accept": "application/xml"})
    try:
        with urllib.request.urlopen(
                req, timeout=timeout or cfg.REQUEST_TIMEOUT) as r:
            raw = r.read()
    except urllib.error.HTTPError as e:
        kipris_rate.fail(f"HTTP {e.code}")
        raise RuntimeError(f"KIPRIS 해외 HTTP {e.code}") from None
    except Exception as e:                       # noqa: BLE001 — 세고 그대로 던진다
        kipris_rate.fail(f"연결:{type(e).__name__}")
        raise
    try:
        root = ET.fromstring(raw)
    except ET.ParseError:
        head = raw[:120].decode("utf-8", "replace").replace("\n", " ")
        kipris_rate.fail("본문파싱")
        raise RuntimeError(
            f"XML 이 아닌 응답(경로가 틀렸을 수 있다): {head}") from None
    # 여기가 국내와 뒤집힌 자리다 — 채워져 있으면 오류다(위 주석 2 참고).
    code = (root.findtext(".//resultCode") or "").strip()
    if code:
        msg = (root.findtext(".//resultMsg") or "").strip()
        kipris_rate.fail(f"코드{code}")
        raise RuntimeError(f"KIPRIS 해외 resultCode={code} {msg}")
    kipris_rate.ok()        # 0건이어도 서비스는 정상 응답이다
    return root


def _total(root: ET.Element) -> int:
    try:
        return int((root.findtext(".//totalSearchCount") or "0").strip() or 0)
    except ValueError:
        return 0


# ── 응답 파싱 ────────────────────────────────────────────────────
def _normalize(node: ET.Element, cat_key: str) -> dict | None:
    """<searchResult> 한 건 → 국내와 같은 스키마."""
    lit = kr._text(node, "ltrtno")
    num = (kr._text(node, "openNumber") or kr._text(node, "publishrNo")
           or kr._text(node, "registerNo") or lit)
    if not num:
        return None
    office = kr._text(node, "countryCode") or "?"
    # 공개번호는 나라마다 체계가 달라 국내 건과 겹칠 수 있다 → 나라를 앞에 붙여
    # 아카이브 전체에서 유일한 열쇠로 만든다(중복 제거의 기준이 된다).
    number = f"{office}{num}"
    ipcs = kr._ipcs(kr._text(node, "ipc"))
    raw_name = kr._text(node, "applicant")
    firsts = kr._split_applicants(raw_name)
    lead = firsts[0] if firsts else raw_name
    name, region, flag = _identify_foreign(lead)
    return {
        "number": number,
        "title": kr._text(node, "inventionName") or number,
        "assignee": raw_name,
        "inventor": kr._text(node, "inventors"),
        "pub_date": (kr._date(kr._text(node, "openDate"))
                     or kr._date(kr._text(node, "registerDate"))),
        "filing_date": kr._date(kr._text(node, "applicationDate")),
        # 목록 응답에는 초록이 없다(국내의 astrtCont 자리가 비어 있다).
        # 서지상세에는 있지만 건마다 한 번씩 더 불러야 해 여기서는 넣지 않는다.
        "snippet": "",
        "office": office,
        "filing_no": kr._text(node, "applicationNo"),
        "cpc": ipcs[:10],   # 국내와 같은 상한(patent_source_kipris.IPC_KEEP)
        "category": kr._classify(ipcs, cat_key),
        "applicant": name,
        "country": region,
        "flag": flag,
        # 문헌번호로 여는 것이 가장 정확하다(공개번호는 나라마다 표기가 다르다).
        "url": "https://patents.google.com/?q="
               + urllib.parse.quote(lit or num) + "&hl=ko",
        "registerStatus": "등록" if kr._text(node, "registerNo") else "공개",
        # 해외 서지상세(CPC·패밀리·청구항)는 이 형식으로만 조회된다 — 나중에
        # 보강할 때 다시 만들지 않도록 들고 있는다.
        "ltrtno": lit,
    }


def _identify_foreign(name: str) -> tuple[str, str, str]:
    """(표시명, 국적, 국기). 모르면 **비워 둔다** — KR 로 떨어뜨리면 안 된다.

    kr._identify 는 못 찾은 이름을 한국으로 본다(국내 공보라 그게 맞다). 해외
    목록에 그대로 쓰면 Panasonic 이 한국 기업이 되고, 거래·지원 탭의 '국내 공급자'
    표에까지 섞여 들어간다. 그 표는 aCountry==='KR' 로 거르기 때문이다.
    """
    raw = (name or "").strip()
    if not raw:
        return "미상", "", ""
    low, canon = raw.lower(), kr._canon(raw)
    for token, ap in kr._ALIAS:
        if not token:
            continue
        if token in low or kr._canon(token) and kr._canon(token) in canon:
            return ap["name"], ap["region"], ap["flag"]
    return raw, "", ""


# ── 수집 ─────────────────────────────────────────────────────────
def _sweep(cat: dict, window: str) -> tuple[list[dict], int, bool]:
    """한 분야를 훑는다. (항목, 조건에 맞는 전체 건수, 상한에 걸렸는지)."""
    got: dict[str, dict] = {}
    total = 0
    countries = ",".join(cfg.FOREIGN_COUNTRIES)
    capped = False
    for pref in cat.get("ipc") or cat["cpc"]:
        start, taken = 1, 0
        while taken < cfg.FOREIGN_PER_CAT:
            params = {
                "ipc": pref,
                "openDate": window,
                "collectionValues": countries,
                # 시작 위치다. 쪽 번호가 아니다(모듈 주석 3 참고).
                "currentPage": str(start),
                "docsCount": str(cfg.FOREIGN_ROWS),
            }
            try:
                root = _get(params)
            except Exception as e:
                print(f"  ! [해외 {cat['name']}/{pref}] {e}")
                break
            if start == 1:
                total += _total(root)
            rows = root.findall(".//searchResult")
            if not rows:
                break
            for node in rows:
                one = _normalize(node, cat["key"])
                if one and one["number"] not in got:
                    got[one["number"]] = one
                    taken += 1
            if len(rows) < cfg.FOREIGN_ROWS:
                break
            start += cfg.FOREIGN_ROWS      # ← 다음 '시작 위치'
            if cfg.KIPRIS_DELAY:
                time.sleep(cfg.KIPRIS_DELAY)
        if taken >= cfg.FOREIGN_PER_CAT:
            capped = True
    return list(got.values()), total, capped


def collect(today: datetime) -> tuple[list[dict], dict]:
    """(목록, 집계). 국내 수집기가 이 결과를 자기 것과 합친다."""
    if not cfg.KIPRIS_KEY:
        raise RuntimeError("KIPRIS 키 없음(KIPRIS_KEY)")
    window = kr._window(today)
    print(f"KIPRIS 해외 공보 수집 — 공개일 {window} · "
          f"대상국 {'·'.join(cfg.FOREIGN_COUNTRIES)}")
    collected: list[dict] = []
    seen: set[str] = set()
    cat_totals: dict[str, int] = {}
    capped: list[str] = []

    for cat in cfg.CATEGORIES:
        items, total, hit = _sweep(cat, window)
        cat_totals[cat["key"]] = total
        if hit:
            capped.append(cat["name"])
        added = 0
        for it in items:
            if it["number"] in seen:
                continue
            seen.add(it["number"])
            collected.append(it)
            added += 1
        print(f"  · {cat['emoji']} {cat['name']}: {added}건 수집 "
              f"(조건에 맞는 전체 {total}건)")
        if cfg.KIPRIS_DELAY:
            time.sleep(cfg.KIPRIS_DELAY)

    if capped:
        print(f"  ⚠️ 해외 상한({cfg.FOREIGN_PER_CAT}건)에 걸린 분야: "
              f"{', '.join(capped)} — 이 분야는 전수가 아니다")

    # 공개국(특허청)별 건수. 국내 소스는 전부 KR 이라 만들 수 없던 축이다.
    offices: dict[str, int] = {}
    for it in collected:
        offices[it["office"]] = offices.get(it["office"], 0) + 1
    if offices:
        print("  공개국별: " + " · ".join(
            f"{k} {v}" for k, v in sorted(offices.items(), key=lambda x: -x[1])))
    print(f"  해외 합계 {len(collected)}건")
    return collected, {"categoryTotals": cat_totals, "truncated": capped,
                       "officeCounts": offices}
