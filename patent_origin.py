"""해외 출원인의 **국적**을 서지상세로 채운다.

왜 따로 있나
--------------------------------------------------------------------
해외 검색(ForeignPatentAdvencedSearchService)의 응답에는 출원인 국적 칸이 없다.
그래서 큐레이션 별칭표에 이름이 걸리는 곳(Siemens·Toyota…)만 국적이 붙고 나머지는
비어 있었다 — 실측(2026-08-25) 출원인 5,471곳 가운데 4,256곳이 미상이고, 미상이
등장한 건수는 중국 4,542 · 일본 2,039 · 미국 1,042 · 유럽 838 이었다.

공개국(countryCode)으로 대신 채우는 것은 하지 않는다. 실제로 갈린다:
  US 공개 202600213551A1 → Panasonic … Co., Ltd. / applicantCountry **JP**
  US 공개 202600221299A1 → Tsinghua University    / applicantCountry **CN**
공개국으로 채웠으면 둘 다 미국 기업이 됐을 것이다.

**서지상세**(ForeignPatentBibliographicService/bibliographicInfo)에는
applicantCountry 가 있다(위 두 건이 그 실측이다). 다만 건당 1요청이고 응답이
무겁다(초록·청구항까지 실려 온다). 그래서 세 가지로 비용을 눌렀다.

  1) **출원인당 1회**만 부른다. 국적은 출원인의 성질이지 문서의 성질이 아니므로,
     그 출원인의 문서 하나만 보면 된다. 16,265건이 아니라 4,256곳이 대상이다.
  2) 한 실행에 상한(ORIGIN_PER_RUN)을 두고 stats.json 에 **누적**한다. 매일
     조금씩 채워 며칠에 걸쳐 메운다 — OPS 시절 공개국 집계와 같은 방식이다.
  3) 몇 갈래로 나눠 동시에 부른다. 하나가 느리다고 줄 전체가 서지 않게.

주의
--------------------------------------------------------------------
· 조회 열쇠는 **ltrtno**(문헌번호, 예: 202600213551A1)다. 공개번호로는 빈 결과가
  온다(실측). 항목에 ltrtno 를 저장해 두고 있다(수집기 참조). OPS 시절 항목에는
  없으므로 그런 것은 건너뛴다.
· 성공/실패 판정이 국내와 반대다 — 해외는 정상일 때 resultCode 가 **비어 있다**.
· 번호 표기가 맞지 않는 문헌이 섞여 있으면 매 실행 같은 것을 다시 두드리며 상한을
  써 버린다 → 실패 횟수를 세어 ORIGIN_MAX_TRY 를 넘으면 그만둔다.

키는 GitHub Secret(KIPRIS_KEY) → 환경변수로만 받는다. 파일에 적지 않는다.
"""
from __future__ import annotations

import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor

import kipris_rate
import patent_config as cfg


def _url(lit: str, office: str) -> str:
    q = {"literatureNumber": lit, "countryCode": office,
         cfg.ORIGIN_KEYPARAM: cfg.KIPRIS_KEY}
    return (f"{cfg.ORIGIN_BASE}/{cfg.ORIGIN_SERVICE}/{cfg.ORIGIN_OP}?"
            + urllib.parse.urlencode(q))


def _fetch(lit: str, office: str) -> tuple[str | None, str]:
    """(국적 코드 또는 None, 실패 사유). 성공이면 사유는 빈 문자열.

    사유를 돌려주는 이유: 첫 실행에서 800곳 중 579곳이 실패했는데 전부 None 으로
    뭉개 놓아 '왜'를 가릴 수 없었다. 타임아웃인지, 서버가 오류 코드를 준 것인지,
    응답은 왔는데 국적 칸이 빈 것인지에 따라 다음 수가 정반대다(재시도 / 요청 교정
    / 포기). 실패를 세는 것과 실패를 아는 것은 다르다.
    """
    # 어떤 예외도 밖으로 내보내지 않는다. 이 보강은 '있으면 좋은' 것이라 한 건이
    # 이상하다고 매일 도는 빌드를 죽이면 안 된다. http.client 쪽 예외는 OSError
    # 계열이 아니라 따로 잡히지도 않는다 → 통째로 삼키고 사유만 남긴다.
    try:
        kipris_rate.acquire()   # 초당 천장(모든 KIPRIS 호출이 지난다)
        req = urllib.request.Request(_url(lit, office),
                                     headers={"Accept": "application/xml"})
        with urllib.request.urlopen(req, timeout=cfg.ORIGIN_TIMEOUT) as r:
            body = r.read()
    except TimeoutError:
        return None, "시간초과"
    except Exception as e:                       # noqa: BLE001 — 사유만 남긴다
        return None, f"연결:{type(e).__name__}"
    try:
        root = ET.fromstring(body)
    except Exception:                            # noqa: BLE001
        return None, "본문파싱"
    # 해외는 정상일 때 resultCode 가 비어 있다(국내와 반대) — 채워져 있으면 실패다.
    code = (root.findtext(".//resultCode") or "").strip()
    if code:
        return None, f"코드{code}"
    cc = (root.findtext(".//applicantInfo/applicantCountry") or "").strip()
    if cc:
        return cc.upper(), ""
    # 응답은 정상인데 국적 칸이 비었다. 문헌 자체가 없어서 빈 것인지(출원인 이름도
    # 없다) 국적만 빠진 것인지 갈라 둔다 — 앞의 것은 번호 표기 문제이고 뒤의 것은
    # 그 문헌에 원래 없는 것이라 다시 두드려도 소용없다.
    has_name = (root.findtext(".//applicantInfo/applicantName") or "").strip()
    return None, "국적칸없음" if has_name else "빈응답"


# 출원인 국적이 실려 오는 공개국. 실측(2026-08-25, 800곳 조회):
#   실패 738곳 전부가 '국적칸없음' 이고, 실패한 공개국은 CN 620 · JP 118 뿐이었다.
#   US·EP 는 한 건도 실패하지 않았다.
# 즉 미국·유럽 공보에는 applicantCountry 가 있고 중국·일본 공보에는 그 칸이 비어
# 있다. 재시도로 풀리는 문제가 아니다 → 같은 출원인의 문서가 여럿이면 US·EP 것을
# 골라 두드리고, 그런 문서가 아예 없는 곳은 처음부터 부르지 않는다(요청이 곧 비용).
ORIGIN_OFFICES = ("US", "EP")


def targets(weeks: dict[str, dict], store: dict) -> list[tuple[str, str, str, int]]:
    """채울 대상 (출원인, 문헌번호, 공개국, 건수). 건수 많은 곳부터.

    건수 순으로 하는 이유: 화면에서 먼저 눈에 띄는 곳부터 맞아 간다. 며칠에 걸쳐
    채우는 동안에도 상위 랭킹과 매트릭스는 빨리 정확해진다.
    """
    have = store.get("origins") or {}
    tried = store.get("originTry") or {}
    seen: dict[str, list] = {}          # 이름 → [문헌번호, 공개국, 건수]
    for wk in sorted(weeks):
        for p in weeks[wk].get("patents", []):
            name = (p.get("applicant") or "").strip()
            if not name or p.get("country"):        # 이미 국적을 아는 항목
                continue
            if name in have or tried.get(name, 0) >= cfg.ORIGIN_MAX_TRY:
                continue
            lit, office = p.get("ltrtno"), p.get("office")
            if not lit or not office:               # OPS 시절 항목 — 열쇠가 없다
                continue
            row = seen.get(name)
            if row is None:
                seen[name] = [lit, office, 1]
                continue
            row[2] += 1
            # 국적이 실려 오는 공보를 만나면 그것으로 갈아 끼운다. 같은 출원인이
            # 중국에도 미국에도 냈다면 미국 문서를 봐야 국적을 얻는다.
            if row[1] not in ORIGIN_OFFICES and office in ORIGIN_OFFICES:
                row[0], row[1] = lit, office
    out = [(n, v[0], v[1], v[2]) for n, v in seen.items()
           if v[1] in ORIGIN_OFFICES]
    out.sort(key=lambda t: (-t[3], t[0]))
    # 이 경로로는 국적을 얻을 수 없는 곳(중국·일본 공보에만 나오는 출원인)의 수도
    # 같이 돌려준다. 조용히 빼 두면 '언젠가 다 찬다'고 잘못 알게 된다.
    skipped = sum(1 for v in seen.values() if v[1] not in ORIGIN_OFFICES)
    return out, skipped


# ── 국내 공보 쪽 ─────────────────────────────────────────────────
# 같은 문제, 다른 원인이다. 해외는 응답에 국적 칸이 없어서 비었고, 국내는 칸이
# 있는데 수집기가 보지 않고 **전부 KR 로 적어** 왔다(별칭표에 없으면 KR 이 기본값).
# 그래서 여기는 '빈 것을 채우는' 것이 아니라 '틀린 것을 고치는' 보강이다.
def _url_kr(app_no: str) -> str:
    q = {"applicationNumber": app_no, cfg.ORIGIN_KR_KEYPARAM: cfg.KIPRIS_KEY}
    return (f"{cfg.ORIGIN_KR_BASE}/{cfg.ORIGIN_KR_SERVICE}/{cfg.ORIGIN_KR_OP}?"
            + urllib.parse.urlencode(q))


def _fetch_kr(app_no: str) -> tuple[str | None, str]:
    """(국적 코드 또는 None, 실패 사유). 해외 _fetch 와 같은 규약."""
    try:
        kipris_rate.acquire()   # 초당 천장(모든 KIPRIS 호출이 지난다)
        req = urllib.request.Request(_url_kr(app_no),
                                     headers={"Accept": "application/xml"})
        with urllib.request.urlopen(req, timeout=cfg.ORIGIN_TIMEOUT) as r:
            body = r.read()
    except TimeoutError:
        return None, "시간초과"
    except Exception as e:                       # noqa: BLE001 — 사유만 남긴다
        return None, f"연결:{type(e).__name__}"
    try:
        root = ET.fromstring(body)
    except Exception:                            # noqa: BLE001
        return None, "본문파싱"
    # 국내는 정상이 resultCode=00 이다(해외와 반대).
    code = (root.findtext(".//resultCode") or "").strip()
    if code and code != "00":
        return None, f"코드{code}"
    # **반드시 applicantInfo 안에서만** 읽는다. 응답에는 agentInfo 에도 country 가
    # 있고 대리인은 거의 언제나 한국 특허법인이라, 스코프 없이 .//country 로 읽으면
    # 외국 출원인까지 전부 '대한민국' 으로 돌아온다.
    ko = ""
    for ap in root.iterfind(".//applicantInfoArray/applicantInfo"):
        ko = (ap.findtext("country") or "").strip()
        if ko:
            break                                # 대표(첫) 출원인의 국적
    if not ko:
        has = root.findtext(".//applicantInfoArray/applicantInfo/name")
        return None, "국적칸없음" if (has or "").strip() else "빈응답"
    cc = cfg.COUNTRY_KO.get(ko)
    # 표에 없는 나라 이름은 추측하지 않는다. 사유에 이름을 실어 로그로 올린다 —
    # 그걸 보고 사람이 COUNTRY_KO 에 한 줄 더하면 다음 실행에서 채워진다.
    return (cc, "") if cc else (None, f"이름모르는나라:{ko}")


def targets_kr(weeks: dict[str, dict], store: dict) -> list[tuple[str, str, int]]:
    """채울 대상 (출원인, 출원번호, 건수). 건수 많은 곳부터.

    열쇠는 **출원번호**(filing_no)다 — 공개번호로는 서지상세가 조회되지 않는다.
    실측: 국내 공보 4,967건 중 97.0%에 출원번호가 있고, 출원인 1,182곳 가운데
    1,174곳을 두드릴 수 있다.
    """
    have = store.get("origins") or {}
    tried = store.get("originTry") or {}
    seen: dict[str, list] = {}
    for wk in sorted(weeks):
        for p in weeks[wk].get("patents", []):
            if (p.get("office") or "") != "KR":
                continue
            name = (p.get("applicant") or "").strip()
            if not name or name in have:
                continue
            if tried.get(name, 0) >= cfg.ORIGIN_MAX_TRY:
                continue
            no = (p.get("filing_no") or "").strip()
            if not no:
                continue
            row = seen.get(name)
            if row is None:
                seen[name] = [no, 1]
            else:
                row[1] += 1
    out = [(n, v[0], v[1]) for n, v in seen.items()]
    out.sort(key=lambda t: (-t[2], t[0]))
    return out


def collect_kr(weeks: dict[str, dict], store: dict) -> tuple[dict, dict]:
    """국내 공보 출원인의 국적을 확인한다. 반환 규약은 collect 와 같다."""
    if not cfg.ORIGIN_KR or cfg.ORIGIN_KR_PER_RUN <= 0:
        return {}, {}
    if not cfg.KIPRIS_KEY:
        return {}, {}
    todo = targets_kr(weeks, store)
    rest = max(0, len(todo) - cfg.ORIGIN_KR_PER_RUN)
    todo = todo[:cfg.ORIGIN_KR_PER_RUN]
    if not todo:
        print("  국내 출원인 국적 확인: 확인할 곳이 없습니다")
        return {}, {}

    with ThreadPoolExecutor(max_workers=max(1, cfg.ORIGIN_WORKERS)) as pool:
        results = list(pool.map(
            lambda t: (t[0], *_fetch_kr(t[1])), todo))

    got: dict[str, str] = {}
    fail: dict[str, int] = {}
    prev = store.get("originTry") or {}
    why_cnt: dict[str, int] = {}
    for name, cc, why in results:
        if cc:
            got[name] = cc
        else:
            fail[name] = (cfg.ORIGIN_MAX_TRY if why == "국적칸없음"
                          else prev.get(name, 0) + 1)
            why_cnt[why] = why_cnt.get(why, 0) + 1
    foreign = {n: c for n, c in got.items() if c != "KR"}
    print(f"  국내 출원인 국적 확인: {len(todo)}곳 조회 → {len(got)}곳 확인"
          f" · 실패 {len(fail)}곳 · 남은 곳 {rest:,}")
    if got:
        by_cc: dict[str, int] = {}
        for cc in got.values():
            by_cc[cc] = by_cc.get(cc, 0) + 1
        print("    확인된 국적: " + " ".join(
            f"{k} {v}" for k, v in sorted(by_cc.items(), key=lambda x: -x[1])[:8]))
        # 이 보강의 값어치는 이 줄이다 — 국내 기업으로 잡혀 있던 외국 출원인이
        # 몇 곳이었는지. 0 이면 애초에 고칠 것이 없었다는 뜻이다.
        print(f"    국내로 잘못 잡혀 있던 외국 출원인 {len(foreign):,}곳")
    if why_cnt:
        print("    실패 사유: " + " ".join(
            f"{k} {v}" for k, v in sorted(why_cnt.items(), key=lambda x: -x[1])[:6]))
    return got, fail


def collect(weeks: dict[str, dict], store: dict) -> tuple[dict, dict]:
    """국적을 채운다. 반환 ({이름: 국적코드}, {이름: 실패횟수}).

    반환값은 stats 에 **병합**된다(덮어쓰지 않는다) — 한 실행이 일부만 채우는
    설계라서, 통째로 대입하면 지난 실행이 채운 것이 지워진다.
    """
    if not cfg.KIPRIS_KEY:
        print("  출원인 국적 보강: 키가 없어 건너뜁니다")
        return {}, {}
    # 국내 쪽을 먼저 돌린다. 대상이 훨씬 작고(1,174곳 대 4천여 곳) '빈 것을 채우는'
    # 것이 아니라 '틀린 것을 고치는' 일이라, 화면의 오류가 하루라도 빨리 걷힌다.
    got_kr, fail_kr = collect_kr(weeks, store)
    if not cfg.ORIGIN or cfg.ORIGIN_PER_RUN <= 0:
        print("  출원인 국적 보강(해외): 꺼져 있음")
        return got_kr, fail_kr

    todo, skipped = targets(weeks, store)
    rest = max(0, len(todo) - cfg.ORIGIN_PER_RUN)
    todo = todo[:cfg.ORIGIN_PER_RUN]
    if not todo:
        print(f"  출원인 국적 보강: 채울 곳이 없습니다"
              f"{f' (이 경로로 닿지 않는 곳 {skipped:,})' if skipped else ''}")
        return got_kr, fail_kr          # 국내 쪽 결과를 흘리지 않는다

    def one(t):
        name, lit, office, _ = t
        cc, why = _fetch(lit, office)
        return name, office, cc, why

    with ThreadPoolExecutor(max_workers=max(1, cfg.ORIGIN_WORKERS)) as pool:
        results = list(pool.map(one, todo))

    got: dict[str, str] = {}
    fail: dict[str, int] = {}
    prev = store.get("originTry") or {}
    why_cnt: dict[str, int] = {}
    fail_off: dict[str, int] = {}
    for name, office, cc, why in results:
        if cc:
            got[name] = cc
        else:
            # '국적칸없음' 은 그 문헌에 원래 없는 것이라 다시 두드려도 소용없다
            # → 재시도 한도를 바로 채워 다음 실행이 상한을 여기에 쓰지 않게 한다.
            fail[name] = (cfg.ORIGIN_MAX_TRY if why == "국적칸없음"
                          else prev.get(name, 0) + 1)
            why_cnt[why] = why_cnt.get(why, 0) + 1
            fail_off[office] = fail_off.get(office, 0) + 1
    by_cc: dict[str, int] = {}
    for cc in got.values():
        by_cc[cc] = by_cc.get(cc, 0) + 1
    top = " ".join(f"{k} {v}" for k, v in
                   sorted(by_cc.items(), key=lambda x: -x[1])[:6])
    _tally = lambda d: " ".join(  # noqa: E731 — 로그 한 줄용
        f"{k} {v}" for k, v in sorted(d.items(), key=lambda x: -x[1])[:6])
    print(f"  출원인 국적 보강: {len(todo)}곳 조회 → {len(got)}곳 확인"
          f" · 실패 {len(fail)}곳 · 남은 곳 {rest:,}"
          + (f" · 이 경로로 닿지 않는 곳 {skipped:,}" if skipped else ""))
    if top:
        print(f"    확인된 국적: {top}")
    if why_cnt:
        print(f"    실패 사유: {_tally(why_cnt)}")
        print(f"    실패한 공개국: {_tally(fail_off)}")
    # 두 경로의 결과를 합쳐 한 번에 돌려준다(stats 에는 병합으로 들어간다).
    # 이름 공간이 겹치지 않는다 — 국내 공보의 출원인은 한글 음차 표기이고
    # 해외 공보는 영문이라 같은 회사도 다른 열쇠가 된다.
    return {**got_kr, **got}, {**fail_kr, **fail}
