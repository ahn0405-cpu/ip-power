"""수집기 스모크 테스트 — 네트워크·키 없이 '라이브 경로'를 실제로 실행해 본다.

왜 필요한가: MOCK 빌드는 _mock_collect 만 타고, 매일 도는 뉴스 실행은
collect_offices 만 탄다 → OPS 를 쓰는 _live_collect 는 주 1회 특허 워크플로에서만
실행돼, 거기 있는 오류(예: 미정의 변수)가 월요일에야 드러난다. py_compile 로는
NameError 를 못 잡으므로, OPS 응답을 흉내 내는 스텁을 끼워 두 경로를 다 돌려본다.

사용: python selftest.py   (의존성 없음. 실패하면 exit 1)
"""
from __future__ import annotations

import sys
from datetime import datetime, timedelta

import patent_config as cfg
import patent_source as ps

FAILS: list[str] = []


def check(cond: bool, msg: str) -> None:
    print(("  ok   " if cond else "  FAIL ") + msg)
    if not cond:
        FAILS.append(msg)


# ── OPS 응답 스텁 (값이 {"$": ...} 로 감싸인 실제 구조를 그대로 흉내) ──
def _doc(number: str, date: str, cpc: str) -> dict:
    return {"exchange-document": {"bibliographic-data": {
        "publication-reference": {"document-id": [{
            "@document-id-type": "docdb",
            "country": {"$": number[:2]}, "doc-number": {"$": number[2:-2]},
            "kind": {"$": number[-2:]}, "date": {"$": date}}]},
        "invention-title": [{"@lang": "en", "$": "Stub power apparatus"}],
        "parties": {"applicants": {"applicant": [
            {"@data-format": "original",
             "applicant-name": {"name": {"$": "STUB CO LTD"}}}]}},
        "patent-classifications": {"patent-classification": [{
            "section": {"$": cpc[0]}, "class": {"$": cpc[1:3]},
            "subclass": {"$": cpc[3]}, "main-group": {"$": cpc[4:]}}]},
    }}}


class Stub:
    """_search 를 대신한다. 호출마다 다른 공개번호를 줘 중복 제거에 다 걸리지 않게."""

    def __init__(self, total: int = 7, per_call: int = 2):
        self.total, self.per_call, self.calls = total, per_call, 0

    def __call__(self, token, cql, start, end, timeout=None):
        self.calls += 1
        check_range = start <= end
        if not check_range:                       # '51-50' 같은 역전 범위 방지 확인
            raise AssertionError(f"잘못된 OPS 범위: {start}-{end}")
        docs = [_doc(f"US{9000000 + self.calls * 10 + i}A1", "20260701", "H02J3")
                for i in range(self.per_call)]
        return ({"ops:world-patent-data": {"ops:biblio-search": {
            "@total-result-count": str(self.total),
            "ops:search-result": {"exchange-documents": docs}}}}, self.total)


def _kipris_page(rows: list[tuple[int, str, str]], total: int) -> str:
    body = "".join(
        f"<item><applicantName>{who}</applicantName>"
        f"<applicationDate>20240701</applicationDate>"
        f"<applicationNumber>10202400{i:05d}</applicationNumber>"
        f"<astrtCont>초록 {i}</astrtCont><inventionTitle>제목 {i}</inventionTitle>"
        f"<ipcNumber>{ipc}</ipcNumber><openDate>20260115</openDate>"
        f"<openNumber>102026{i:07d}</openNumber>"
        f"<registerStatus>공개</registerStatus></item>"
        for i, who, ipc in rows)
    return ('<?xml version="1.0" encoding="UTF-8"?><response><header>'
            "<resultCode>00</resultCode><resultMsg>NORMAL SERVICE.</resultMsg>"
            f"</header><body><items>{body}</items>"
            f"<count><totalCount>{total}</totalCount></count></body></response>")


def _favicon_checks(sr) -> None:
    """탭 아이콘. 여기서 틀리면 오류 없이 조용히 아이콘만 사라진다.

    특히 data URI 는 HTML 속성 안에 그대로 들어가므로, 따옴표나 꺾쇠가 살아
    있으면 속성이 그 자리에서 끊긴다 — 페이지는 멀쩡히 뜨고 아이콘만 안 나온다.
    """
    import struct
    import tempfile
    from pathlib import Path

    import favicon as fv

    svg = fv.svg()
    check(svg.startswith("<svg") and svg.rstrip().endswith("</svg>"),
          "SVG 가 온전히 닫힌다")
    check('width="32"' in svg and 'viewBox="0 0 32 32"' in svg,
          "SVG 에 고유 크기와 viewBox 가 다 있다")
    check(f"#{fv.BG[0]:02X}{fv.BG[1]:02X}{fv.BG[2]:02X}" in svg,
          "타일 색이 사이트 강조색(--accent)과 같다")
    import re as _re2
    pts = _re2.search(r'points="([^"]+)"', svg)
    pairs = pts.group(1).split() if pts else []
    check(len(pairs) == len(fv.BOLT)
          and all(len(p.split(",")) == 2 for p in pairs),
          f"번개 꼭짓점이 {len(fv.BOLT)}개 다 들어간다 (읽은 값 {len(pairs)}개)")
    # 벡터와 래스터가 같은 좌표를 쓰는지. 갈라지면 탭과 바로가기의 모양이 달라진다.
    fit = fv._fit(32)
    check(all(abs(float(p.split(",")[0]) - fit[i][0]) < 0.01
              and abs(float(p.split(",")[1]) - fit[i][1]) < 0.01
              for i, p in enumerate(pairs)),
          "SVG 좌표가 래스터와 같은 _fit() 에서 나온다")

    uri = fv.data_uri()
    check(uri.startswith("data:image/svg+xml,"), "data URI 형식이 맞다")
    bad = [c for c in '"<>#' if c in uri]
    check(not bad,
          f"data URI 에 속성을 깨뜨리는 문자가 없다 (발견: {bad or '없음'})")

    # PNG 서명과 실제 크기
    for size in (16, 32, 180):
        data = fv.png(size)
        w, h = struct.unpack(">II", data[16:24])
        check(data[:8] == b"\x89PNG\r\n\x1a\n" and (w, h) == (size, size),
              f"PNG {size}px 가 유효하다 ({w}x{h})")

    # ICO 헤더: reserved=0, type=1, 그리고 담은 장수
    blob = fv.ico()
    res, typ, cnt = struct.unpack("<HHH", blob[:6])
    check((res, typ) == (0, 1) and cnt == 2,
          f"ICO 헤더가 맞다 (type={typ}, 장수={cnt})")
    # 각 항목이 가리키는 자리에 진짜 PNG 가 있는지 — 오프셋을 잘못 쌓으면
    # 파일 크기는 그럴듯한데 브라우저가 못 읽는다.
    okpng = True
    for i in range(cnt):
        off, = struct.unpack("<I", blob[6 + 16 * i + 12: 6 + 16 * i + 16])
        n, = struct.unpack("<I", blob[6 + 16 * i + 8: 6 + 16 * i + 12])
        if blob[off:off + 8] != b"\x89PNG\r\n\x1a\n" or off + n > len(blob):
            okpng = False
    check(okpng, "ICO 안의 각 항목이 실제 PNG 를 가리킨다")

    with tempfile.TemporaryDirectory() as td:
        fv.write(Path(td))
        made = sorted(p.name for p in Path(td).iterdir())
        check(made == ["apple-touch-icon.png", "favicon.ico", "favicon.svg"],
              f"아이콘 파일 세 개를 쓴다 ({', '.join(made)})")

    # 페이지가 실제로 그걸 가리키는지. favicon.ico 는 링크가 없어도 브라우저가
    # 자동으로 찾는 자리라, 파일만 있으면 404 는 사라진다.
    page = sr._PAGE
    check('rel="icon"' in page and "__FAVICON__" in page,
          "head 에 기본 아이콘 링크가 있다")
    check('href="favicon.ico"' in page, "head 에 ico 대비책이 있다")
    check('rel="apple-touch-icon"' in page, "head 에 iOS 바로가기 아이콘이 있다")


def _lazy_checks(sr) -> None:
    """목록 분할과, 분할해도 집계가 흔들리지 않는지.

    이 기능의 위험은 '느려진다'가 아니라 **숫자가 조용히 작아진다**는 것이다.
    집계를 자르기 전 전체로 계산하는 순서가 한 번만 어긋나도 화면의 모든 수가
    부분집합 기준이 되는데, 그건 눈으로 봐서는 알 수 없다.
    """
    import json
    import tempfile
    from pathlib import Path

    items = [{"title": f"제목 {i}", "url": f"u{i}", "number": f"N{i}",
              "pub_date": f"2026-08-{(i % 28) + 1:02d}"} for i in range(50)]
    with tempfile.TemporaryDirectory() as td:
        d = Path(td)
        sec = {"items": list(items), "totals": {"한국전력공사": 999}}
        rest = sr._split_items(sec, 10, "patents", d)

        check(rest == 40, f"나머지를 갈라낸다 (뒤 {rest}건)")
        check(len(sec["items"]) == 10, f"인라인은 상한만큼만 ({len(sec['items'])}건)")
        check(sec["total"] == 50, f"total 은 자르기 전 전체 ({sec['total']})")
        check(sec["totals"] == {"한국전력공사": 999},
              "집계는 분할이 건드리지 않는다")

        f = d / sr.FEED_SUBDIR / "patents-rest.json"
        check(f.exists(), "나머지 파일이 만들어진다")
        tail = json.loads(f.read_text(encoding="utf-8"))
        check(len(tail) == 40, f"나머지 파일에 뒤가 다 있다 ({len(tail)}건)")

        # 합치면 원본과 정확히 같아야 한다 — 겹치거나 빠지면 목록이 조용히 틀어진다.
        merged = {x["number"] for x in sec["items"]} | {x["number"] for x in tail}
        check(merged == {x["number"] for x in items},
              f"인라인 + 나머지 = 원본 (합 {len(merged)}건, 중복·누락 없음)")
        check(not ({x["number"] for x in sec["items"]}
                   & {x["number"] for x in tail}),
              "인라인과 나머지가 겹치지 않는다")

        # 최신순으로 갈라야 첫 화면이 최근 것으로 찬다.
        newest = sorted((x["pub_date"] for x in items), reverse=True)[:10]
        check(sorted((x["pub_date"] for x in sec["items"]), reverse=True) == newest,
              "인라인에 남는 것은 가장 최근 건들이다")

        # 상한보다 적으면 그냥 다 인라인 — 쓸데없는 요청을 만들지 않는다.
        small = {"items": items[:5]}
        check(sr._split_items(small, 10, "news", d) == 0
              and "rest" not in small and small["total"] == 5,
              "상한보다 적으면 나누지 않는다")

        # 탈출구: 통째로 인라인(로컬 검증·오프라인 배포).
        saved = sr.INLINE_ALL
        try:
            sr.INLINE_ALL = True
            whole = {"items": list(items)}
            check(sr._split_items(whole, 10, "patents", d) == 0
                  and len(whole["items"]) == 50 and "rest" not in whole,
                  "NEWS_INLINE_ALL 이면 나누지 않는다")
        finally:
            sr.INLINE_ALL = saved

    js = sr._JS
    check("hydrate()" in js, "첫 그림 뒤 나머지를 받는 호출이 있다")
    check("FEED[t].total" in js or "FEED[t] && FEED[t].total" in js,
          "건수는 total 로 읽는다 (items.length 는 로딩 중 작다)")
    check("_shareCache = null" in js,
          "다 받은 뒤 항목 기반 캐시를 버린다 (안 버리면 최근분으로 계산한 값이 남는다)")
    # 국적을 모르는 출원인을 화면에서 빼기만 하면 '🇺🇸8곳' 으로 보여 미국 기업이
    # 여덟 곳뿐인 것으로 읽힌다(실측: 5,403곳 중 국적을 아는 곳이 1,186곳뿐).
    # 모르는 것은 모른다고 두되, 모른다는 사실도 화면에 남아야 한다.
    # 수집 축이 OPS(출원인 목록) → KIPRIS(분야+기간 전수) 로 바뀌었다. 화면 문구가
    # 옛 방식 그대로면 읽는 사람은 아직 65곳만 보는 줄 안다 — 기관 서비스에서
    # 사실과 다른 설명은 그 자체가 결함이다.
    check("EPO OPS" not in js,
          "화면 문구에 EPO OPS 가 남아 있지 않다 (지금 쓰는 것은 KIPRISplus)")
    check("KIPRISplus" in js, "푸터가 실제 출처(KIPRISplus)를 밝힌다")
    # 국기 — 윈도우에는 국기 이모지 글꼴이 없어 지역표시자 두 글자가 그대로 찍힌다
    # (실측: 화면이 'US8KR1143CN11JP11EU13' 이 됐다). 표시 직전에 우리가 그린
    # 것으로 바꾸므로, 국기가 나오는 자리는 전부 flg() 를 거쳐야 한다.
    check("function flg(" in js, "국기를 그려 주는 헬퍼가 있다")
    check("0x1F1E6" in js,
          "지역표시자에서 나라 코드를 되돌린다 (표를 따로 두지 않는다)")
    for sym in ("f-KR", "f-US", "f-CN", "f-JP", "f-EU"):
        check('id="' + sym + '"' in sr._PAGE, f"{sym} 국기를 그려 둔다")
    # 이모지가 flg() 를 거치지 않고 그대로 붙는 자리가 남아 있으면 안 된다.
    import re as _re3
    # 변수 이름을 열거하면 새로 생긴 이름을 놓친다 — 옛 목록에 'g.flag'·'t.flag'
    # 가 없어서 국기 세 자리가 오래 빠져 있었다(국내 공개 패널 머리, 분야별 경쟁
    # 구도의 출원인 칩 둘). 이름을 세지 말고 **모양**으로 본다.
    # .emoji 는 다르게 다룬다 — 분야 이모지(⚡🏠🚗)도 같은 이름을 쓰는데 그쪽은
    # 국기가 아니라 그대로 찍혀도 된다. 국기를 담는 객체(지역 rg·특허청 off)에서만 본다.
    raw = (_re3.findall(r"\+\s*\(?[A-Za-z_$][\w$]*\.(?:aFlag|flag)\s*\|\|", js)
           + _re3.findall(r"\+\s*\(?(?:rg|off)\.emoji\s*\|\|", js))
    check(not raw, f"국기를 flg() 없이 그대로 붙이는 자리가 없다 (발견 {len(raw)}곳)")
    # 반대쪽 실수도 있다 — flg() 가 돌려준 SVG 를 문자열에 담아 두었다가 나중에
    # esc() 로 흘려보내면 '<svg class="fl" …>' 가 글자 그대로 화면에 찍힌다
    # (실측: 거래·지원 '국내 권리 N곳' 칩과 판정 문장 양쪽에서 났다).
    # 그리는 자리에서 바로 부르면 안전하다. 위험한 것은 '데이터에 담아 두는' 자리다
    # — 담아 둔 문자열은 나중에 esc() 를 지나며 마크업이 글자로 찍힌다. 거래·지원의
    # '국내 권리 N곳' 이 실제로 그랬다. 그 자리를 이름·국기 따로 담게 고쳤고,
    # 되돌아가면 여기서 걸린다.
    # 담아 두는 값이 **국기 코드**여야 한다. 문자열을 정확히 박아 두면 옆의 다른
    # 이유(건수 세기)로 모양이 바뀔 때 같이 깨진다 — 실제로 한 번 그랬다.
    # 그래서 '무엇을 담는가' 만 본다.
    check("{flag:it.aFlag" in js.replace(" ", ""),
          "국내 권리 목록은 국기 '코드' 를 담는다 (그린 SVG 가 아니라)")
    check("s.add(flg(" not in js and "add(flg(" not in js,
          "국내 권리 목록에 flg() 결과(SVG)를 담아 두지 않는다")
    check("EPO" not in js and "전력 CPC" not in js,
          "홈 타일 설명에도 EPO·전력 CPC 라는 옛 근거가 남아 있지 않다")
    check("전 세계 공개" not in js,
          "'전 세계 공개' 라고 하지 않는다 (국내 + 미국·유럽·일본·중국 넷이다)")
    check("칸의 수는 표본 건수" not in js and "출원인마다 수집 상한이 있어" not in js,
          "매트릭스 안내가 '칸은 표본' 이라고 하지 않는다 (지금은 전수라 실제 건수다)")
    check("규모를 실제 총계로 되돌린" not in js,
          "'총계로 되돌린' 이라는 OPS 시절 보정 이야기가 남아 있지 않다")
    check("곳을 매주 조회해" not in js,
          "'주요 출원인 N곳을 조회' 라는 옛 설명이 남아 있지 않다")
    check("안에서의 분포입니다" not in js
          or "추적 중인 주요 출원인" not in js,
          "'추적 중인 N곳 안에서의 분포' 라는 옛 단서가 남아 있지 않다")

    check("function regionSplit(" in js, "국적 미상 출원인 수를 따로 센다")
    check("국적미상 " in js, "KPI 에 '국적미상 N' 을 함께 보인다")
    # 글자를 그대로 고정하지 않는다 — 문구를 다듬을 때마다 이 검사가 깨진다.
    # 지켜야 할 것은 '빠진 곳의 수를 밝히는가' 이지 어떤 낱말을 쓰는가가 아니다.
    _rs = js[js.find("const rankSub ="):]
    _rs = _rs[:_rs.find(";\n", 10)]
    check("unknown.toLocaleString()" in _rs
          and ("제외" in _rs or "넣지 않" in _rs),
          "국적별 랭킹이 '빠진 곳이 몇인지'를 밝힌다")
    check("서지상세를\\n    + '출원인당 한 번씩" in js
          or "출원인당 한 번씩 조회해 채웁니다" in js.replace("'\n    + '", ""),
          "국적을 어떻게 채우는지 화면에서 밝힌다")
    # 두 부류를 갈라 말해야 한다. 중국·일본 공보에는 국적 칸이 원본부터 비어 있어
    # (실측) 그 출원인들은 영영 안 채워진다 — '채우는 중' 으로만 말하면 오지 않을
    # 것을 기다리게 만든다.
    check("원본부터 비어 있어" in js and "originsBlocked" in js,
          "채울 수 없는 몫을 따로 밝힌다")
    check("uniq.toLocaleString()" in js,
          "출원인 수도 천 단위로 끊는다 (옆의 미상 수와 표기가 어긋나지 않게)")

    for name, guard in (("공급자 표", "FULL ? supplierHTML"),
                        ("경쟁 구도", "if(!FULL) return '<div class=\"sec\" id=\"sec-analysis\">"),
                        ("통계 뷰", "FULL ? renderStats")):
        check(guard in js, f"{name}는 다 받기 전에는 그리지 않는다")


def _docno_checks(sr) -> None:
    """바깥 링크에 넘길 표준 문헌번호(_docno).

    조용한 실패다 — 번호를 한 글자 틀려도 페이지는 멀쩡히 뜨고 링크만 빈손으로
    간다. 실제로 국내 공개번호(1020260127780)를 그대로 넘기고 있었고, 아무도
    그걸 열어 보지 않아 오래 남아 있었다. 표기 여섯 갈래를 실제 값으로 고정한다.
    """
    print("\n· 바깥 링크용 문헌번호")
    cases = [
        # (저장된 값, office, ltrtno, 기대값, 설명)
        ({"number": "KR20260088580A", "office": "KR"}, "KR20260088580A",
         "이미 표준형이면 손대지 않는다 (OPS 시절 항목)"),
        ({"number": "1020260127780", "office": "KR"}, "KR20260127780A",
         "국내 13자리에서 문서종류 '10' 을 떼고 KR·A 를 붙인다"),
        ({"number": "US20260221300", "office": "US", "ltrtno": "202600221300A1"},
         "US20260221300A1", "공개국 접두를 살리고 종류코드를 ltrtno 에서 가져온다"),
        ({"number": "CN122158202", "office": "CN", "ltrtno": "202610335327A0"},
         "CN122158202A", "KIPRIS 안에서만 쓰는 'A0' 은 바깥에서 'A' 로 보낸다"),
        ({"number": "JP38528513|2026528513", "office": "JP",
          "ltrtno": "202600528513A0"}, "JP2026528513A",
         "일본은 파이프 뒤(국제표기) 쪽을 쓴다"),
        ({"number": "EP04793971", "office": "EP", "ltrtno": "000004793971A1"},
         "EP4793971A1", "유럽은 앞의 0 을 뗀다"),
        ({"number": "WO2026142708A1", "office": "WO"}, "WO2026142708A1",
         "PCT 도 표준형 그대로"),
        ({"number": "이상한번호", "office": "KR"}, "이상한번호",
         "모르는 모양은 손대지 않는다 (틀린 번호를 지어내지 않는다)"),
        ({"number": "", "office": "KR"}, "", "번호가 없으면 빈 값"),
    ]
    for item, want, why in cases:
        got = sr._docno(item)
        check(got == want, f"{why} — {item.get('number') or '(빈 값)'} → {got}")


def _quad_checks(sr) -> None:
    """분야 지도(quadChartHTML)를 실제 값 분포로 그려 보고 눈으로 볼 것을 대신 센다.

    왜 필요한가: 축 범위를 코드에 고정해 두었는데, 수집 방식이 바뀌면서 값의
    자릿수가 통째로 달라졌다. y 축은 실질 경쟁자 2~12곳 기준이었는데 실측이
    10~239곳이 되어 여덟 중 일곱이 바닥에 눌어붙었고, 등급 경계(5곳/8곳)도 같은
    시절 값이라 여덟 분야가 전부 한 등급으로 나와 색이 아무것도 구분하지 못했다.
    둘 다 '그려 놓고 보면' 바로 보이지만 코드만 읽으면 안 보인다 → 실제 분포를
    넣고 (ⓐ 점이 축에 퍼지는가 ⓑ 등급이 갈리는가 ⓒ 원이 판 안에 있는가
    ⓓ 이름표가 겹치지 않는가) 를 센다.
    """
    import json
    import re as _re
    import shutil
    import subprocess
    import tempfile

    js = sr._JS
    check("constNEF=[8,256]" in js.replace(" ", ""),
          "y 축 고정 범위가 실측 분포(10~239곳)를 담는다")
    if not shutil.which("node"):
        check(True, "(node 없음 — 분야 지도 실행 검사는 건너뛴다)")
        return

    cs, ce = js.find("const CONC_MID"), js.find("function concentration")
    # 읽는 법(quadGuideHTML)까지 함께 떼어낸다 — 차트가 그 함수를 부른다.
    qs, qe = js.find("function quadGuideHTML"), js.find("const STAOWN_HEAD")
    check(cs >= 0 < ce - cs and qs >= 0 < qe - qs, "분야 지도 블록을 떼어낼 수 있다")
    if not (0 <= cs < ce and 0 <= qs < qe):
        return

    # 2026-08-25 실측값(전수 수집 뒤). 축·등급이 이 분포를 못 담으면 실패한다.
    rows = [("계량·스마트그리드", 10.0, 216, 0.37, 1.00),
            ("원전·SMR", 42.0, 382, 0.20, 1.04),
            ("재생에너지·저장", 43.0, 8013, 0.23, 1.00),
            ("전력수급·수요관리", 43.2, 2217, 0.18, 0.86),
            ("전력설비·기기", 52.8, 461, 0.18, 1.12),
            ("송·변전·전력망", 62.9, 1679, 0.18, 1.36),
            ("데이터센터·무정전전원", 106.6, 249, 0.11, 0.95),
            ("전력반도체·전력변환", 239.3, 1658, 0.06, 0.23)]
    # 이름표가 몰리는 경우. 실질 경쟁자 수가 거의 같은 분야가 나란히 서면 위·아래
    # 자리를 서로 뺏는다 — 실제로 재생에너지(43.0곳)와 원전(42.0곳)이 그랬다.
    # 네 곳까지는 오른쪽·왼쪽·위·아래 네 자리로 덮이므로 여섯 곳으로 민다.
    crowd = [(f"긴이름분야{i}", 43.0 + i * 0.1, 1200, 0.2, 0.98 + i * 0.02)
             for i in range(6)]

    def render(rs):
        data = [{"r": {"cat": {"name": n}, "neff": ne, "tot": t, "cr3": c, "n": 50},
                 "ratio": ra} for n, ne, t, c, ra in rs]
        prog = (js[cs:ce] + "\n" + js[qs:qe]
                + "\nfunction esc(s){return String(s).replace(/[&<>\"]/g,"
                  "c=>({'&':'&amp;','<':'&lt;','>':'&gt;','\"':'&quot;'}[c]));}\n"
                + "const FEED={trade:{quadLead:'앞말',"
                  "quadGuide:[['가로','설명1'],['세로','설명2']]}};\n"
                + f"const ROWS={json.dumps(data, ensure_ascii=False)};\n"
                + "ROWS.forEach(d=>d.lv=concLevel(d.r.neff));\n"
                  "console.log(JSON.stringify({html:quadChartHTML(ROWS),"
                  "lv:ROWS.map(d=>d.lv),pad:QPAD,w:QW,h:QH}));")
        prog = prog.replace("\\\\", "\\")
        with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False,
                                         encoding="utf-8") as f:
            f.write(prog)
            path = f.name
        try:
            out = subprocess.run(["node", path], capture_output=True, text=True,
                                 timeout=30)
            if out.returncode != 0:
                check(False, f"분야 지도 JS 가 실행된다 ({out.stderr.strip()[:160]})")
                return None
            return json.loads(out.stdout)
        finally:
            try:
                __import__("os").unlink(path)
            except OSError:
                pass

    res = render(rows)
    if res is None:
        return

    check(res["html"].count("<dt>") == 2 and "이 그림 읽는 법" in res["html"],
          "그림 아래에 읽는 법이 함께 나온다")
    check(len(set(res["lv"])) >= 2,
          "집중도 등급이 실측 분포에서 갈린다 "
          f"(받은 등급 {sorted(set(res['lv']))})")

    pad, x0, y0 = res["pad"], res["pad"]["l"], res["pad"]["t"]
    x1, y1 = res["w"] - pad["r"], res["h"] - pad["b"]
    dots = [tuple(map(float, m)) for m in _re.findall(
        r'<circle class="dot" cx="([\d.]+)" cy="([\d.]+)" r="([\d.]+)"', res["html"])]
    check(len(dots) == len(rows), f"분야 수만큼 원이 그려진다 ({len(dots)}/{len(rows)})")
    ys = [c[1] for c in dots]
    span = (max(ys) - min(ys)) / (y1 - y0) if dots else 0
    check(span >= 0.55,
          f"점이 y 축에 퍼진다 (세로로 판의 {span:.0%} 를 쓴다, 기준 55% 이상)")
    check(all(x0 <= cx - r and cx + r <= x1 and y0 <= cy - r and cy + r <= y1
              for cx, cy, r in dots),
          "원이 그림판 밖으로 삐져나가지 않는다 (사분면 설명 글자를 덮었었다)")

    # 이름표 겹침. 글자 폭은 그리는 쪽과 같은 어림(한글 10.5 / 그 밖 6)으로 잰다.
    def overlaps(html):
        labs = _re.findall(
            r'<text class="ql" x="([\d.]+)" y="([\d.]+)" text-anchor="(\w+)">([^<]+)</text>',
            html)
        boxes = []
        for sx, sy, anc, txt in labs:
            w = sum(10.5 if ord(ch) > 0x1100 else 6 for ch in txt)
            lx = float(sx) if anc == "start" else float(sx) - (w if anc == "end" else w / 2)
            boxes.append((lx, float(sy) - 10, w, 13))
        return labs, [(labs[i][3], labs[j][3])
                      for i in range(len(boxes)) for j in range(i + 1, len(boxes))
                      if boxes[i][0] < boxes[j][0] + boxes[j][2]
                      and boxes[j][0] < boxes[i][0] + boxes[i][2]
                      and boxes[i][1] < boxes[j][1] + boxes[j][3]
                      and boxes[j][1] < boxes[i][1] + boxes[i][3]]

    labs, over = overlaps(res["html"])
    check(len(labs) == len(rows) and not over,
          f"이름표가 서로 겹치지 않는다 ({'겹침: ' + str(over[:2]) if over else '겹침 없음'})")

    res2 = render(crowd)
    if res2 is None:
        return
    labs2, over2 = overlaps(res2["html"])
    check(len(labs2) == len(crowd) and not over2,
          "실질 경쟁자 수가 거의 같은 분야가 몰려도 이름표가 겹치지 않는다 "
          f"({'겹침: ' + str(over2[:2]) if over2 else '겹침 없음'})")


def _subs_checks(sr) -> None:
    """세부 기술 쏠림(subsRows/subsBlockHTML)을 실제로 실행해 본다.

    여기서 틀리기 쉬운 것은 '퍼센트의 분모'다. 한 건에 IPC 가 여러 개 붙어 있어
    전부 세면 한 건이 여러 번 계수돼 합이 100%를 넘는데, 화면은 그래도 100% 폭
    막대를 그린다 — 눈으로는 멀쩡하고 숫자만 틀린다. 그래서 ⓐ 첫 코드만 세는가
    ⓑ 네 자리로 자르는가 ⓒ 네 칸의 합이 정확히 100인가 를 실행해서 센다.
    이름표는 줄마다 색이 다른 기술을 가리키므로 ⓓ 줄 안에서 색과 이름이 묶여
    있는지도 본다(패널 하나짜리 범례로는 묶이지 않는다).
    """
    import json
    import re as _re
    import shutil
    import subprocess
    import tempfile

    import ip_guide

    # 실측(2026-08-25, 누적 16,265건)으로 여덟 분야의 1~3위에 실제로 오르는 코드.
    # 이 중 하나라도 이름이 없으면 화면에 코드가 그대로 나온다 → 사람이 채워야 한다.
    seen = ["B60L", "B60R", "F03D", "G01R", "G06F", "G06Q", "G21C", "G21D",
            "H01F", "H01H", "H01M", "H02B", "H02G", "H02H", "H02J", "H02M", "H02S"]
    missing = [c for c in seen if c not in ip_guide.IPC_NAMES]
    check(not missing, f"화면에 실제로 오르는 IPC 에 한국어 이름이 다 있다 "
                       f"({'빠짐: ' + ','.join(missing) if missing else '17종 모두'})")

    js = sr._JS
    if not shutil.which("node"):
        check(True, "(node 없음 — 세부 기술 실행 검사는 건너뛴다)")
        return
    s, e = js.find("const SUBS_MIN"), js.find("// 국유판매기술")
    check(s >= 0 < e - s, "세부 기술 블록을 떼어낼 수 있다")
    if not 0 <= s < e:
        return

    # 쏠림이 **덜한** 분야를 목록 앞에 둔다 — 순서가 이미 맞아 있으면 정렬을
    # 지워도 검사가 통과해 버린다(실제로 그랬다).
    CATS = [{"key": "b", "emoji": "🅱️", "name": "다라분야"},
            {"key": "a", "emoji": "🅰️", "name": "가나분야"},
            {"key": "c", "emoji": "🆑", "name": "작은분야"}]
    # 두 번째 코드는 전부 Y02E10 이다 — 첫 코드만 센다면 Y02E 는 어디에도 안 나온다.
    def mk(cat, code, n):
        return [{"category": cat, "cpc": [code, "Y02E10"]} for _ in range(n)]
    items = (mk("a", "H01M10/052", 60) + mk("a", "F03D7/02", 25)
             + mk("a", "H02S40", 10) + mk("a", "G06F1", 5)
             + mk("b", "H02J3/38", 40) + mk("b", "G06Q50", 30)
             + mk("b", "X99Z1", 20) + mk("b", "H02M1", 10)      # 이름표에 없는 코드
             + mk("c", "H02G1", 10))                            # SUBS_MIN 미만
    names = {k: v for k, v in ip_guide.IPC_NAMES.items()
             if k in ("H01M", "F03D", "H02S", "H02J", "G06Q", "G06F", "H02M", "H02G")}
    prog = (js[s:e]
            + "\nfunction esc(s){return String(s).replace(/[&<>\"]/g,"
              "c=>({'&':'&amp;','<':'&lt;','>':'&gt;','\"':'&quot;'}[c]));}\n"
            + "function loadingNote(w){return '<p>'+w+' 로딩</p>';}\n"
            + "let FULL=true;\n"
            + "const FEED={patents:{categories:"
            + json.dumps(CATS, ensure_ascii=False) + ",items:"
            + json.dumps(items, ensure_ascii=False) + "},trade:{ipcNames:"
            + json.dumps(names, ensure_ascii=False)
            + ",subsLead:'앞말',subsNote:'각주'}};\n"
            + "const R=subsRows();\n"
            + "const H=R.map(r=>subsBlockHTML(r, FEED.trade)).join('');\n"
            + "console.log(JSON.stringify({rows:R,html:H,"
              "empty:subsBlockHTML(null, FEED.trade)}));")
    prog = prog.replace("\\\\", "\\")
    with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False,
                                     encoding="utf-8") as f:
        f.write(prog)
        path = f.name
    try:
        out = subprocess.run(["node", path], capture_output=True, text=True, timeout=30)
        if out.returncode != 0:
            check(False, f"세부 기술 JS 가 실행된다 ({out.stderr.strip()[:200]})")
            return
        res = json.loads(out.stdout)
    finally:
        try:
            __import__("os").unlink(path)
        except OSError:
            pass

    rows = res["rows"]
    check([r["cat"]["key"] for r in rows] == ["a", "b"],
          "건수가 적은 분야는 빼고, 1위에 많이 몰린 분야가 위로 온다 "
          f"(받은 순서 {[r['cat']['key'] for r in rows]})")
    a = rows[0]
    check(a["n"] == 100 and [t["code"] for t in a["top"]] == ["H01M", "F03D", "H02S"],
          f"IPC 를 앞 네 자리로 자르고 큰 것부터 센다 (받은 값 "
          f"{[t['code'] for t in a['top']]}, 분모 {a['n']})")
    check("Y02E" not in json.dumps(rows) and a["kinds"] == 4,
          f"한 건에서 대표(첫) 코드 하나만 센다 (갈래 {a['kinds']}종, Y02E 안 나옴)")
    for r in rows:
        tot = sum(t["share"] for t in r["top"]) + r["rest"]["share"]
        check(abs(tot - 1.0) < 1e-9,
              f"{r['cat']['name']}: 세 갈래 + 나머지 = 100% (받은 값 {tot * 100:.4f}%)")
    check(abs(a["rest"]["share"] - 0.05) < 1e-9 and a["rest"]["v"] == 5,
          f"나머지는 전체에서 상위 셋을 뺀 값이다 (받은 값 {a['rest']['v']}건)")

    html = res["html"]
    # 'X99Z 가 html 안에 있다' 로는 부족하다 — 이름 자리가 비어도 작은 코드칩
    # 때문에 통과한다. 이름 자리에 무엇이 들어갔는지를 본다.
    check('풍력 발전<span class="sbc mono">F03D</span>' in html
          and '<b>20%</b>X99Z</span>' in html,
          "코드에 한국어 이름을 붙이고, 이름이 없는 코드는 코드가 이름 자리에 선다")
    widths = [float(w) for w in _re.findall(r'width:([\d.]+)%', html)]
    check(len(widths) == 8 and all(abs(sum(widths[i:i + 4]) - 100) < 0.25
                                   for i in (0, 4)),
          f"막대 네 칸의 폭 합이 100%다 (받은 값 {widths})")
    # 그 줄의 요점(1위 이름과 몫)은 막대 **안**에 있어야 한다. 아래 칩 줄로만
    # 내려가면 여덟 줄이 전부 비슷해 보이고 요점이 문장 끝 작은 숫자로 묻힌다.
    inbar = _re.findall(r'<i title="[^"]*" style="width:[\d.]+%;background:[^"]+">'
                        r'<b>([^<]+)</b>(?:<span[^>]*>[^<]*</span>)?'
                        r'<em>(\d+)%</em></i>', html)
    check(inbar == [("전지·연료전지", "60"), ("전력 공급·계통", "40")],
          f"1위 이름과 몫이 막대 안에 들어간다 (받은 값 {inbar})")
    swatches = _re.findall(
        r'<span class="sbn[^"]*"><i style="background:([^"]+)"', html)
    check(swatches == ["var(--q3)", "var(--q2)", "var(--q1)"] * 2,
          f"칩에 그 줄의 순위 색이 붙는다 (받은 값 {swatches})")
    # 칸 안에 이름을 넣은 1위도 칩을 **버리지 않는다** — 좁은 화면에서는 칸 안
    # 글자가 잘려(실측 430px) 칩이 대신 나서야 하는데, 글자 폭은 그리기 전에
    # 알 수 없다. 서버가 둘 다 내보내고 CSS 가 화면 폭으로 하나를 고른다.
    check(html.count('<span class="sbn inb">') == 2,
          "칸 안에 이름을 넣은 1위도 칩을 남겨 둔다 (좁은 화면에서 칩이 대신한다)")
    # 퍼센트가 이름보다 앞이다 — 칩 끝에 붙여 두면 숫자를 찾으려고 이름을 다 읽어야 했다.
    check(_re.search(r'</i><b>\d+%</b>[^<]', html) is not None,
          "칩은 퍼센트를 이름보다 앞에 둔다")
    # 분야 카드가 축이다 — 세부 기술은 그 안의 한 토막이라 이름을 달고 들어간다.
    check(res["html"].count('<div class="blk">무엇을 내고 있나') == 2
          and "서로 다른 4갈래" in res["html"],
          "분야 카드 안에 이름 붙은 토막으로 들어간다")
    # 클래스 이름 충돌. 이 스타일시트는 한 파일에 다 들어 있어서, 새로 지은 이름이
    # 이미 있는 전역 클래스와 겹치면 조용히 남의 스타일을 뒤집어쓴다. 두 번 겪었다 —
    # 전역 .more 때문에 공급자 칩이 줄 가운데로 밀렸고, 이번에는 전역
    # .lead{display:flex;flex-direction:column} 때문에 칩 안이 세 줄로 갈렸다.
    # 화면으로만 보면 '왜 줄바꿈이 되지?' 로 보여 원인이 안 잡힌다.
    # 규칙: 한 이름에 **전역 규칙(.x{)과 스코프 규칙(… .x{)이 둘 다** 있으면 충돌이다.
    css = sr._CSS
    clash = [c for c in ("inb", "blk", "blkd", "subrow", "sbn", "sbc")
             if _re.search(r"(?:^|[,}])\s*\." + c + r"(?=[\s,{:])", css, _re.M)
             and _re.search(r"[\w\]\)]\s+\." + c + r"(?=[\s,{:.])", css)]
    check(not clash,
          "새 클래스 이름이 이미 있는 전역 클래스와 겹치지 않는다 "
          + (f"(겹침: {clash})" if clash else "(겹침 없음)"))
    # 같은 이름 충돌이 아니라 **세기·순서** 로 지는 경우도 있다. 칸 안 이름과
    # 겹치는 칩을 '.shns .inb{display:none}' 으로 숨겼는데, 뒤에 오는
    # '.shns .sbn{display:inline-flex}' 가 세기가 같아 이겨서 둘 다 보였다.
    # 숨기는 쪽이 .sbn 을 함께 물고 있어야 한다.
    flat = _re.sub(r"\s+", "", css)
    check(".shns.sbn.inb,.shns.shn.inb{display:none}" in flat,
          "칸 안 이름과 겹치는 칩은 .sbn 규칙보다 센 선택자로 숨긴다")
    check(res["empty"] == "",
          "그 분야에 셀 것이 없으면 토막을 통째로 뺀다 (빈 막대를 그리지 않는다)")

    # 국내 권리 줄. 여기서 한 번 크게 물렸다 — 출원인 국적을 바로잡자 이 목록이
    # 8곳에서 132곳이 되어 카드가 이름 목록으로 덮였고, '서로 다른 출원인' 목록이라
    # 1건짜리(필립모리스, 전자담배 배터리)가 100건짜리(CATL) 옆에 같은 크기로 섰다.
    ks, ke = js.find("const KR_HEAD="), js.find("const STAOWN_HEAD=")
    check(ks >= 0 < ke - ks, "국내 권리 줄 블록을 떼어낼 수 있다")
    if 0 <= ks < ke:
        kr = [{"name": f"많은곳{i}", "flag": "🇨🇳", "n": 100 - i} for i in range(30)]
        prog2 = (js[ks:ke]
                 + "\nfunction esc(s){return String(s).replace(/[&<>\"]/g,"
                   "c=>({'&':'&amp;','<':'&lt;','>':'&gt;','\"':'&quot;'}[c]));}\n"
                 + "function flg(f){return '<svg class=\"fl\"></svg>';}\n"
                 + f"const KR={json.dumps(kr, ensure_ascii=False)};\n"
                 + "console.log(JSON.stringify({many:krLineHTML(KR),"
                   "few:krLineHTML(KR.slice(0,2)),none:krLineHTML([])}));")
        prog2 = prog2.replace("\\\\", "\\")
        with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False,
                                         encoding="utf-8") as f:
            f.write(prog2)
            path2 = f.name
        try:
            out2 = subprocess.run(["node", path2], capture_output=True, text=True,
                                  timeout=30)
            if out2.returncode != 0:
                check(False, f"국내 권리 JS 가 실행된다 ({out2.stderr.strip()[:200]})")
                return
            r2 = json.loads(out2.stdout)
        finally:
            try:
                __import__("os").unlink(path2)
            except OSError:
                pass
        shown = _re.findall(r'<svg class="fl"></svg> ([^<]+)<span class="krn">',
                            r2["many"])
        check(len(shown) == 3 and shown == ["많은곳0", "많은곳1", "많은곳2"],
              f"이름은 건수 많은 세 곳까지만 세운다 (받은 값 {shown})")
        check("국내 권리 30곳" in r2["many"] and "외 27곳" in r2["many"],
              "곳 수는 줄이지 않고 그대로 밝힌다 (30곳 · 외 27곳)")
        check(r2["many"].count("많은곳") <= 3 + 25 + 25,
              "나머지 이름은 툴팁으로 돌리고 본문에 늘어놓지 않는다")
        check('<span class="krmore"' not in r2["few"]
              and r2["few"].count('class="krn"') == 2,
              "세 곳 이하면 '외 N곳' 을 붙이지 않는다")
        check(r2["none"] == "", "국내 권리가 없는 분야는 줄을 통째로 뺀다")


def _supplier_checks(sr) -> None:
    """공급자 표(_supKind/suppliers)를 실제로 실행해 본다.

    이 부분은 파이썬 문자열 안의 JS 라 py_compile 이 봐 주지 않는다. 실제로
    한 번에 네 가지가 났다 — 선언 없는 SUP_COMPANY(페이지 전체가 죽었다),
    민간 시험연구소가 출연연으로 분류, 전역 .more 와 클래스 이름 충돌로 칩이
    줄 가운데로 밀림, 칩 숫자와 눌렀을 때 열리는 목록 건수 불일치. 화면으로만
    확인하면 다음에 또 난다.
    """
    import json
    import re as _re
    import shutil
    import subprocess
    import tempfile

    js = sr._JS
    # 1) 소스 수준 — node 가 없어도 도는 최소한의 방어선.
    check("const SUP_COMPANY" in js,
          "SUP_COMPANY 가 선언돼 있다 (없으면 페이지 전체가 죽는다)")
    check("class=\"cta more\"" not in js,
          "공급자 칩이 전역 '더 보기'(.more) 클래스를 쓰지 않는다")
    check("$('#guide').addEventListener('keydown'" in js
          or "$('#guide').onkeydown" in js,
          "공급자 칩에 키보드 활성화가 달려 있다")
    # 클릭 위임은 #results 가 아니라 #guide 여야 한다 (표가 #guide 안에 있다).
    gi = js.find("$('#guide').onclick")
    ri = js.find("$('#results').addEventListener('click'")
    sp = js.find("closest('[data-sup]')")
    check(gi >= 0 and sp > gi and (ri < 0 or not (ri < sp < js.find("$('#reset')"))),
          "[data-sup] 클릭 위임이 #guide 에 달려 있다")

    if not shutil.which("node"):
        check(True, "(node 없음 — JS 실행 검사는 건너뛴다)")
        return

    # 2) 실행 수준 — 함수를 그대로 떼어내 node 에서 돌린다.
    start = js.find("const SUP_KINDS")
    end = js.find("const SUP_TOP")
    check(start >= 0 and end > start, "공급자 블록을 소스에서 떼어낼 수 있다")
    if start < 0 or end <= start:
        return
    block = js[start:end]

    items = [
        # 대표 출원인 + 공동출원. 한국원자력연구원은 세 번째 자리에 있다.
        {"aName": "서울대학교산학협력단|한국과학기술원|한국원자력연구원",
         "aCountry": "KR", "category": "nuclear", "number": "P1"},
        {"aName": "한국원자력연구원", "aCountry": "KR",
         "category": "nuclear", "number": "P2"},
        {"aName": "주식회사 스탠더드시험연구소", "aCountry": "KR",
         "category": "nuclear", "number": "P3"},   # 민간 — 빠져야 한다
        {"aName": "주식회사동일기술공사", "aCountry": "KR",
         "category": "nuclear", "number": "P4"},   # 민간 — 빠져야 한다
        {"aName": "한국전력공사", "aCountry": "KR",
         "category": "nuclear", "number": "P5"},
        {"aName": "Siemens AG", "aCountry": "DE",
         "category": "nuclear", "number": "P6"},   # 해외 — 빠져야 한다
    ]
    prog = (block + "\nconst FEED={patents:{categories:[{key:'nuclear',"
            "name:'원전·SMR',emoji:'x'}]}};\n"
            f"const ITEMS={json.dumps(items, ensure_ascii=False)};\n"
            "const rows=suppliers(ITEMS);\n"
            "console.log(JSON.stringify(rows.map(r=>({cat:r.cat.key,n:r.n,"
            "total:r.total,orgs:r.orgs.map(o=>[o.name,o.cnt,o.kind.label])}))));")
    # site_render 의 JS 는 파이썬 문자열이라 정규식 이스케이프가 '\\s' 로 들어 있다.
    prog = prog.replace("\\\\", "\\")
    with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False,
                                     encoding="utf-8") as f:
        f.write(prog)
        path = f.name
    try:
        out = subprocess.run(["node", path], capture_output=True, text=True,
                             timeout=30)
        if out.returncode != 0:
            check(False, f"공급자 JS 가 실행된다 ({out.stderr.strip()[:160]})")
            return
        rows = json.loads(out.stdout)
    finally:
        try:
            __import__("os").unlink(path)
        except OSError:
            pass

    check(len(rows) == 1 and rows[0]["cat"] == "nuclear",
          "분야 한 줄이 나온다")
    orgs = {o[0]: (o[1], o[2]) for o in rows[0]["orgs"]}
    check("한국원자력연구원" in orgs and orgs["한국원자력연구원"][0] == 2,
          "공동출원에서 두 번째·세 번째 출원인도 세어진다 "
          f"(한국원자력연구원 {orgs.get('한국원자력연구원', ('?',))[0]}건)")
    check("주식회사 스탠더드시험연구소" not in orgs,
          "'주식회사 …시험연구소'는 출연연으로 분류되지 않는다")
    check("주식회사동일기술공사" not in orgs,
          "'주식회사…공사'는 공공기관으로 분류되지 않는다")
    check("Siemens AG" not in orgs, "해외 출원인은 빠진다")
    check(orgs.get("한국전력공사", (0, ""))[1] == "공공기관",
          "한국전력공사는 공공기관이다")
    check(orgs.get("서울대학교산학협력단", (0, ""))[1] == "대학",
          "산학협력단은 대학이다")
    # 합계는 '건수'다. 기관별 건수를 더하면 P1 이 세 번 세져 6이 된다.
    check(rows[0]["total"] == 3,
          f"분야 합계가 건수다 (공동출원 중복 없이 3건, 받은 값 {rows[0]['total']})")


def _fg_page(rows: list[tuple[str, str, str]], total: int) -> str:
    """해외 응답 흉내. 성공이면 resultCode 가 **비어 있다**(실측)."""
    body = "".join(
        f"<searchResult><ltrtno>{lit}</ltrtno><ipc>{ipc}</ipc>"
        f"<applicationNo>{lit[:8]}</applicationNo><registerNo></registerNo>"
        f"<publishrNo>{lit}</publishrNo><countryCode>{cc}</countryCode>"
        f"<applicationDate>20250301</applicationDate>"
        f"<openDate>20260715</openDate><registerDate></registerDate>"
        f"<applicant>Panasonic Holdings|Nichicon</applicant>"
        f"<inventors>A|B</inventors><openNumber>{lit}</openNumber>"
        f"<inventionName>POWER {lit}</inventionName></searchResult>"
        for lit, ipc, cc in rows)
    return ('<?xml version="1.0" encoding="UTF-8"?><response><header>'
            "<resultCode></resultCode><resultMsg></resultMsg></header>"
            f"<body><items>{body}<colString>US</colString>"
            f"<totalSearchCount>{total}</totalSearchCount>"
            "</items></body></response>")


def _wf_block(src, key: str, indent: int) -> list[str]:
    """워크플로 YAML 에서 `key:` 아래 블록의 줄들을 준다 (주석·빈 줄은 뺀다).

    PyYAML 을 쓰지 않는다. 이 자리에서 import 하던 yaml 이 러너에 없어
    selftest 가 죽었고, selftest 를 도는 세 워크플로(일간 뉴스·주간 특허·
    재빌드)가 2026-08-28~29 연속 실패해 배포가 48시간 멈췄다. 검사 하나에
    필요한 모듈이 그날 수집을 통째로 멈추게 해서는 안 된다 — 워크플로는
    아무것도 설치하지 않고 표준 라이브러리만 쓴다는 전제로 서 있다.

    주석을 먼저 걷어낸다. 이 파일은 'paths 필터를 두지 않는다', 'claude/**'
    같은 말을 주석에 길게 적어 두고 있어서, 글자만 찾으면 주석에 속는다.

    블록형(`key:` 뒤가 비고 아래로 들여쓴 꼴)만 읽는다. 인라인형
    (`branches: [claude/**]`)이면 빈 목록이 나오고 부르는 쪽 검사가 FAIL 로
    드러난다 — 모양이 바뀐 것을 조용히 통과시키지 않는다.
    """
    lines = src.splitlines() if isinstance(src, str) else list(src)
    out: list[str] = []
    inside = False
    for raw in lines:
        line = raw.split("#", 1)[0].rstrip()
        if not line.strip():
            continue
        cur = len(line) - len(line.lstrip())
        if inside:
            if cur <= indent:
                break
            out.append(line)
        elif cur == indent and line.strip() == key + ":":
            inside = True
    return out


def _origin_checks() -> None:
    """출원인 국적 보강(patent_origin). 요청은 흉내 내고 고르는 규칙만 실측한다.

    지키는 것 넷:
      · 국적을 이미 아는 항목은 다시 두드리지 않는다 (요청이 곧 비용이다)
      · 출원인당 한 번만 두드린다 (국적은 출원인의 성질이지 문서의 성질이 아니다)
      · 건수 많은 곳부터 (며칠에 걸쳐 채우는 동안 화면이 빨리 정확해진다)
      · 실패가 쌓인 곳은 그만둔다 (번호 표기가 안 맞는 문헌이 상한을 다 먹는다)
    그리고 성공/실패 판정이 국내와 뒤집혀 있다 — resultCode 가 **비어야** 성공이다.
    """
    print("\n· 출원인 국적 보강")
    import patent_archive as pa
    import patent_config as pcfg
    import patent_origin as po

    def pat(name, lit, office, country=""):
        return {"applicant": name, "ltrtno": lit, "office": office,
                "country": country, "number": lit}

    weeks = {"2026-08-24": {"week": "2026-08-24", "patents": [
        pat("US Maker", "L1", "US"),
        pat("US Maker", "L2", "US"),               # 같은 곳 — 한 번만
        pat("US Maker", "L3", "US"),
        pat("CN University", "L4", "CN"),          # 중국 공보뿐 — 국적 칸이 없다
        pat("CN University", "L5", "CN"),
        pat("JP Corp", "L6", "JP"),                # 일본 공보뿐 — 마찬가지
        pat("Mixed Filer", "L8", "CN"),            # 중국에도 미국에도 냈다
        pat("Mixed Filer", "L9", "US"),            #   → 미국 문서를 골라야 한다
        pat("Siemens", "L5", "EP", "EU"),          # 이미 안다 — 건너뛴다
        pat("No Key Co", "", "US"),                # ltrtno 가 없다(OPS 시절)
        pat("Tried Out", "L6", "US"),              # 실패가 쌓였다
    ]}}
    store = {"origins": {}, "originTry": {"Tried Out": pcfg.ORIGIN_MAX_TRY}}
    got, skipped = po.targets(weeks, store)
    names = [t[0] for t in got]
    check(names == ["US Maker", "Mixed Filer"],
          f"두드릴 곳을 제대로 고른다 (받은 목록 {names})")
    check(got and got[0][3] == 3 and got[0][1] == "L1",
          "출원인당 한 번만 두드리고 건수 많은 곳을 앞에 둔다")
    # 국적이 실려 오는 공보는 미국·유럽뿐이다(실측). 같은 출원인이 중국에도
    # 미국에도 냈다면 미국 문서를 봐야 국적을 얻는다.
    mixed = [t for t in got if t[0] == "Mixed Filer"][0]
    check(mixed[2] == "US" and mixed[1] == "L9",
          f"국적이 실려 오는 공보(US·EP) 쪽 문헌을 고른다 (받은 값 {mixed[1:3]})")
    check(skipped == 2,
          f"이 경로로 닿지 않는 곳(중국·일본 공보만 있는 출원인)을 센다 (받은 값 {skipped})")
    check("Siemens" not in names, "이미 아는 국적은 다시 두드리지 않는다")
    check("No Key Co" not in names, "문헌번호가 없는 옛 항목은 건너뛴다")
    check("Tried Out" not in names, "실패가 쌓인 곳은 그만 두드린다")

    # 응답 파싱 — 성공은 resultCode 가 비어 있다(국내와 반대).
    ok = ('<response><header><resultCode></resultCode></header><body><items>'
          '<bibliographicInfo><applicantInfo><applicantName>Tsinghua University'
          '</applicantName><applicantCountry>CN</applicantCountry></applicantInfo>'
          '</bibliographicInfo></items></body></response>')
    bad = ('<response><header><resultCode>11</resultCode>'
           '<resultMsg>No Mandatory</resultMsg></header><body></body></response>')
    import io
    import urllib.request as ur

    class _R:
        def __init__(self, b): self.b = b
        def read(self): return self.b.encode()
        def __enter__(self): return self
        def __exit__(self, *a): return False

    seen = []
    orig_open, orig_key = ur.urlopen, pcfg.KIPRIS_KEY
    pcfg.KIPRIS_KEY = "x"
    try:
        ur.urlopen = lambda req, timeout=None: (seen.append(req.full_url), _R(ok))[1]
        check(po._fetch("L1", "CN") == ("CN", ""), "정상 응답에서 출원인 국적을 뽑는다")
        check(seen and "literatureNumber=L1" in seen[0]
              and "countryCode=CN" in seen[0]
              and pcfg.ORIGIN_KEYPARAM + "=" in seen[0],
              "문헌번호·공개국·키 질의를 제대로 싣는다")
        ur.urlopen = lambda req, timeout=None: _R(bad)
        check(po._fetch("L1", "CN") == (None, "코드11"),
              "resultCode 가 **채워져** 있으면 실패로 읽고 사유를 남긴다 (국내와 반대다)")

        def boom(req, timeout=None):
            raise TimeoutError("timed out")
        ur.urlopen = boom
        check(po._fetch("L1", "CN") == (None, "시간초과"),
              "타임아웃은 삼키고 사유를 남긴다")
    finally:
        ur.urlopen, pcfg.KIPRIS_KEY = orig_open, orig_key
    del io

    # 병합 — 통째로 대입하면 지난 실행이 채운 것이 지워진다.
    st = {"totals": {}, "offices": {}, "updated": {},
          "origins": {"A": "JP"}, "originTry": {"B": 1}}
    pa.merge_stats(st, {"origins": {"B": "CN"}, "originTry": {"C": 1}}, "2026-08-25")
    check(st["origins"] == {"A": "JP", "B": "CN"},
          f"국적은 병합된다 (받은 값 {st['origins']})")
    check("B" not in st["originTry"] and st["originTry"].get("C") == 1,
          "채워진 곳의 실패 기록은 지운다")

    # 나라 코드 접기 — 화면의 국적 축은 다섯 갈래인데 서지상세는 실제 나라를 준다.
    check(pcfg.region_of("DK") == "EU" and pcfg.region_of("DE") == "EU",
          "유럽 나라는 유럽으로 접는다")
    check(pcfg.region_of("TW") == "TW" and pcfg.region_of("CA") == "CA",
          "유럽 밖의 나라는 접지 않는다 (모른다고 하지 않는다)")
    check(pcfg.region_of("") == "", "빈 값은 빈 값 그대로 둔다")
    check(pcfg.flag_of("DK") == "\U0001F1E9\U0001F1F0",
          "표에 없는 나라도 국기(지역표시자)를 만들어 준다")
    check(pcfg.flag_of("KR") == "🇰🇷", "표에 있는 나라는 그대로 쓴다")

    # ── 국내 공보 쪽. 고치는 오류의 성격이 반대다 ────────────────────
    # 해외는 '비어 있는 것을 채우는' 일이고 국내는 '틀리게 적힌 것을 고치는' 일이다
    # (수집기가 별칭표에 없는 출원인을 전부 KR 로 적는다). 그래서 대상을 고를 때
    # country 가 이미 차 있다고 건너뛰면 안 되고, 그리는 쪽도 덮어써야 한다.
    print("\n· 국내 출원인 국적 확인")

    def kpat(name, no, office="KR", country="KR"):
        return {"applicant": name, "filing_no": no, "office": office,
                "country": country, "number": no}

    kweeks = {"2026-08-24": {"week": "2026-08-24", "patents": [
        kpat("컨템포러리 엠퍼렉스", "A1"), kpat("컨템포러리 엠퍼렉스", "A2"),
        kpat("한국전력공사", "B1"),
        kpat("이미 확인한 곳", "C1"),
        kpat("실패 쌓인 곳", "D1"),
        kpat("번호 없는 곳", ""),
        kpat("해외 항목", "E1", office="US", country=""),   # 국내 공보가 아니다
    ]}}
    kstore = {"origins": {"이미 확인한 곳": "JP"},
              "originTry": {"실패 쌓인 곳": pcfg.ORIGIN_MAX_TRY}}
    kt = po.targets_kr(kweeks, kstore)
    knames = [t[0] for t in kt]
    check(knames == ["컨템포러리 엠퍼렉스", "한국전력공사"],
          f"국내 공보만 골라 출원인당 한 번, 건수 많은 곳부터 (받은 목록 {knames})")
    check(kt and kt[0][1] == "A1" and kt[0][2] == 2,
          "조회 열쇠는 출원번호다 (공개번호로는 서지상세가 안 열린다)")
    check("이미 확인한 곳" not in knames and "실패 쌓인 곳" not in knames
          and "번호 없는 곳" not in knames and "해외 항목" not in knames,
          "이미 확인한 곳·실패가 쌓인 곳·번호 없는 곳·해외 항목은 뺀다")

    # 응답 파싱. 함정은 agentInfo 다 — 대리인은 거의 언제나 한국 특허법인이라,
    # 스코프 없이 .//country 를 읽으면 외국 출원인이 전부 KR 로 돌아온다.
    kok = ('<response><header><resultCode>00</resultCode></header><body><item>'
           '<applicantInfoArray><applicantInfo><name>컨템포러리 엠퍼렉스</name>'
           '<country>중국</country></applicantInfo></applicantInfoArray>'
           '<agentInfoArray><agentInfo><name>특허법인</name>'
           '<country>대한민국</country></agentInfo></agentInfoArray>'
           '</item></body></response>')
    knone = kok.replace("<country>중국</country>", "<country> </country>")
    kodd = kok.replace("<country>중국</country>", "<country>바나나공화국</country>")
    kbad = ('<response><header><resultCode>10</resultCode></header>'
            '<body></body></response>')
    kseen = []
    orig_open, orig_key = ur.urlopen, pcfg.KIPRIS_KEY
    pcfg.KIPRIS_KEY = "x"
    try:
        ur.urlopen = lambda req, timeout=None: (kseen.append(req.full_url), _R(kok))[1]
        check(po._fetch_kr("A1") == ("CN", ""),
              "출원인의 국적을 읽는다 — 대리인(대한민국)이 아니라")
        check(kseen and "applicationNumber=A1" in kseen[0]
              and pcfg.ORIGIN_KR_KEYPARAM + "=" in kseen[0],
              "출원번호와 키 질의를 제대로 싣는다")
        ur.urlopen = lambda req, timeout=None: _R(kbad)
        check(po._fetch_kr("A1") == (None, "코드10"),
              "resultCode 가 00 이 아니면 실패로 읽는다 (해외와 반대다)")
        ur.urlopen = lambda req, timeout=None: _R(knone)
        check(po._fetch_kr("A1") == (None, "국적칸없음"),
              "이름은 있는데 국적만 비면 그 문헌에 원래 없는 것으로 센다")
        ur.urlopen = lambda req, timeout=None: _R(kodd)
        check(po._fetch_kr("A1") == (None, "이름모르는나라:바나나공화국"),
              "표에 없는 나라 이름은 추측하지 않고 이름을 사유에 실어 올린다")
    finally:
        ur.urlopen, pcfg.KIPRIS_KEY = orig_open, orig_key
    check(pcfg.COUNTRY_KO.get("중국") == "CN"
          and pcfg.COUNTRY_KO.get("대한민국") == "KR",
          "한국어 나라 이름을 코드로 옮긴다 (서지상세가 코드를 주지 않는다)")

    # ── 2000 시리즈 색인 코드 ────────────────────────────────────────
    # 부가 태그이지 그 특허의 성격을 규정하는 코드가 아니다 → 담아 두되 분야를
    # 정하는 데는 쓰지 않는다. 기준은 메인그룹 **2000 이상**이다. '100 이상' 으로
    # 재면 C07D 307/00·C09J 141/00 같은 진짜 메인그룹이 딸려 온다(실측 59건 오판).
    print("\n· 2000 시리즈 색인 코드")
    import patent_source_kipris as _ks
    yes = ["H02J2300/28", "H01M2300", "F05B2270/337", "H02J 2101/20", "H01H2033"]
    no = ["H02J3/28", "C07D307/00", "C09J141/00", "H01M50/242", "Y04S10/14",
          "G01R21/133", "H02J101/20"]
    wrong = ([c for c in yes if not _ks.is_index_code(c)]
             + [c for c in no if _ks.is_index_code(c)])
    check(not wrong, "색인 코드를 진짜 메인그룹과 가른다 "
                     + (f"(틀림: {wrong})" if wrong else "(12종 모두)"))
    # 송·변전의 match 가 'H02J' 라서, H02J 코드가 색인 코드뿐인 항목이 그리로
    # 끌려간다(실측 1건). 진짜 분류 코드가 있으면 그쪽이 이겨야 한다.
    # 'H02J3000/10' 은 색인 코드이면서 y04s10 의 match 접두 'H02J3' 으로 시작한다
    # — 거르지 않으면 부가 태그가 분야를 끌고 간다.
    check(_ks._classify(["H02J3000/10", "G06Q50/06"], "etc") == "y04s50",
          "분야는 진짜 분류 코드로 정한다 (색인 코드가 끌고 가지 않는다)")
    # 색인 코드밖에 없으면 그거라도 보긴 하지만, 접두가 메인그룹까지 딱 떨어지면
    # 경계에서 끊긴다 — 'H02J3' 은 'H02J3000' 을 잡지 않는다. 서브클래스 접두
    # ('H02G')는 원래 번호를 안 보므로 그때는 색인 코드로도 분야가 정해진다.
    check(_ks._classify(["H02J3000/10"], "etc") == "etc",
          "메인그룹 접두는 색인 코드의 번호를 먹지 않는다 (H02J3 ≠ H02J3000)")
    check(_ks._classify(["H02G2200/10"], "etc") == "y04s10",
          "서브클래스 접두는 색인 코드밖에 없어도 분야를 정한다 (H02G = 케이블)")

    # ── 분야 = CPC Y04S ─────────────────────────────────────────────
    # 스마트그리드는 '계통에 ICT 를 붙인 것' 이다. 배터리를 **만드는** 기술은
    # 범위 밖이고, 배터리를 **계통에 붙여 운영하는** 기술이 범위 안이다.
    # 그 경계가 접두 하나 차이로 갈리는 자리들을 못 박아 둔다.
    print("\n· 분야 = CPC Y04S")
    IN = [
        ("H02J3/28", "y04s10", "저장을 이용한 계통 부하평준화 (10/14)"),
        ("H02J15/00", "y04s10", "전기 외 형태 저장 — 장주기 ESS 자리"),
        ("H02J13/00", "y04s10", "계통 원격 감시·제어 (10/50)"),
        ("G01R31/00", "y04s10", "계통 상태감시 (10/30)"),
        ("H02B1/00", "y04s10", "변전소·배전반 (10/16)"),
        ("G01R21/133", "y04s20", "스마트미터링 (20/30)"),
        ("H02J9/06", "y04s20", "수용가 비상전원·UPS (20/12)"),
        ("H02J7/34", "y04s20", "예비전원 (20/12)"),
        ("B60L53/00", "y04s30", "전기차 충전 (30/12)"),
        ("B60L55/00", "y04s30", "차량→계통 역송(V2G) — 30/10 의 본진"),
        ("B60L58/12", "y04s30", "차량 배터리 감시·제어"),
        ("G06Q50/06", "y04s50", "전력 거래·에너지 서비스 (50/10·50/16)"),
        ("G21C1/00", "nuclear", "원전 — Y04S 밖이라 Y02E 30 근거로 따로 둔다"),
    ]
    bad = [(c, want, got) for c, want, _ in IN
           if (got := pcfg.classify([c], "")) != want]
    check(not bad, "Y04S 갈래에 제자리로 들어간다 "
                   + (f"(틀림: {bad})" if bad else f"({len(IN)}종 모두)"))
    OUT = [("H01M4/62", "배터리 전극 — 만드는 기술이지 계통이 아니다"),
           ("H01M50/242", "전지 부품"),
           ("H02M7/48", "전력 변환(전력반도체)"),
           ("F03D7/02", "풍력 발전기 자체"),
           ("H02S40/32", "태양광 발전기 자체"),
           ("H02J7/12", "기기 배터리 충전 회로 — H02J7 을 통째로 넣으면 딸려 온다"),
           ("G06Q50/10", "전력이 아닌 서비스업"),
           ("G06Q10/06", "경영 관리"),
           ("B60L15/20", "차량 구동 제어 — 계통과 주고받는 자리가 아니다"),
           ("B60L7/10", "제동 회생 — 차량 안에서 끝난다")]
    leak = [(c, pcfg.classify([c], "")) for c, _ in OUT if pcfg.classify([c], "")]
    check(not leak, "스마트그리드가 아닌 것은 들어오지 않는다 "
                    + (f"(샌 것: {leak})" if leak else f"({len(OUT)}종 모두 범위 밖)"))
    # 목록 순서가 곧 우선순위다. 원전이 뒤에 있으면 원전 특허가 다른 코드에 먼저
    # 걸려 밀려난다(실측 7건: G21D3/04 가 G06Q50/06 에 걸려 시장·거래로 갔다).
    check(pcfg.CATEGORIES[0]["key"] == "nuclear",
          "원전이 목록 맨 앞이다 (따로 관리하기로 한 분야가 다른 코드에 안 밀리게)")
    check(pcfg.classify(["G06Q50/06", "G21D3/04"], "") == "nuclear"
          and pcfg.classify(["H02J9/062", "G21D3/06"], "") == "nuclear",
          "원전 코드가 있으면 원전으로 간다 (다른 코드가 먼저 와도)")
    check(any(c.get("outside") for c in pcfg.CATEGORIES),
          "Y04S 밖인 분야(원전)는 그 사실을 데이터에 달고 있다")
    check(all(c.get("en") for c in pcfg.CATEGORIES),
          "분야마다 공식 영문 표제를 함께 담는다 (옮긴 말이 표준을 대체하지 않게)")
    # 화면까지 실려 가야 뜻이 있다. 피드에 빠지면 카드에 코드도 표제도 안 붙는다.
    import site_render as _sr
    js = _sr._JS
    check('r.cat.cpc' in js and 'r.cat.en' in js and 'r.cat.outside' in js,
          "카드가 표준 코드·공식 표제·'Y04S 밖' 을 실제로 그린다")

    # 접두는 코드 경계에서 끊겨야 한다. 'H02J1'(직류 계통)이 'H02J101'(색인 코드)을
    # 먹던 자리 — 실측 29건이 그렇게 분야가 정해져 있었다.
    EDGE = [
        # (코드, 접두, 걸려야 하나, 왜)
        ("H02J1/10", "H02J1", True, "메인그룹 그대로"),
        ("H02J1", "H02J1", True, "번호만 있고 서브그룹이 없어도"),
        ("H02J101/24", "H02J1", False, "IPC 색인 코드 — 번호가 새어 들던 자리"),
        ("H02J103/30", "H02J1", False, "같은 색인 계열"),
        ("H02J13/00", "H02J1", False, "13 은 1 이 아니다"),
        ("H02J7/345", "H02J7/34", True, "서브그룹의 하위 갈래는 살린다"),
        ("H02G3/00", "H02G", True, "서브클래스 접두는 숫자가 바로 붙는다"),
        ("B60L55/00", "B60L55", True, "V2G"),
        ("B60L5/00", "B60L55", False, "5 는 55 가 아니다"),
        ("Y04S10/14", "Y04S10", True, "Y 섹션도 같은 규칙"),
    ]
    wrong = [(c, p, want) for c, p, want, _ in EDGE
             if pcfg.code_matches(c, p) is not want]
    check(not wrong, "접두는 코드 경계에서 끊긴다 (색인 코드로 번호가 새지 않게) "
                     + (f"(틀림: {wrong})" if wrong else f"({len(EDGE)}종 모두)"))
    check(pcfg.classify(["H02J101/24"], "") == "",
          "색인 코드 하나뿐이면 분야가 정해지지 않는다 (근거 없이 발전으로 밀려나던 29건)")

    # ── Y04S 10/00 안의 하위 갈래 ────────────────────────────────
    # 발전·송배전 지원 하나가 수집분의 70%다. 그 안이 안 보이면 장주기 ESS 가
    # 어디쯤인지 말할 수 없다. 갈래는 CPC 2026.08 Y04S 10/00 의 하위 그룹 그대로다.
    print("\n· Y04S 10/00 안의 하위 갈래")
    SUB = [
        # 같은 G01R31 이 셋으로 갈린다 — 이 순서가 무너지면 4,504건이 한 칸에 뭉친다
        ("G01R31/08", "10/52", "선로 고장 위치 — 정전·고장 관리"),
        ("G01R31/392", "10/14", "전지 상태 진단 — 에너지 저장"),
        ("G01R31/12", "10/30", "절연 파괴 시험 — 상태 감시 (나머지 G01R31)"),
        ("H02J15/00", "10/14", "전기 외 형태 저장 — 장주기 ESS 자리"),
        ("H02J3/32", "10/14", "전지의 계통 연계"),
        ("H02J3/28", "10/14", "저장을 이용한 부하평준화"),
        ("H02J3/38", "10/12", "분산전원 병입"),
        ("H02J3/16", "10/22", "무효전력 보상"),
        ("H01H33/00", "10/18", "고압 개폐기"),
        ("H02H3/00", "10/20", "보호계전"),
        ("H01F27/00", "10/16", "변압기 — 변전소"),
        ("H02J13/00", "10/50", "계통 원격 감시·제어"),
        ("H02J3/00", "10/50", "H02J3 의 나머지는 계통 운영"),
        ("H02G3/00", "10/00", "케이블 부설 — Y04S 10 에 대응 하위가 없다"),
    ]
    miss = [(c, want, got) for c, want, _ in SUB
            if (got := pcfg.subgroup_of([c], "y04s10")) != want]
    check(not miss, "Y04S 10 안에서 제 갈래로 간다 "
                    + (f"(틀림: {miss})" if miss else f"({len(SUB)}종 모두)"))
    # 좁은 갈래가 넓은 갈래보다 위에 있어야 한다. 순서만 무너져도 통과하던
    # 검사가 되지 않게, 순서에 기대는 자리를 직접 못 박는다.
    order = [s["code"] for s in pcfg.SUBGROUPS["y04s10"]]
    check(order.index("10/52") < order.index("10/30")
          and order.index("10/14") < order.index("10/30")
          and order.index("10/12") < order.index("10/50")
          and order[-1] == "10/00",
          "좁은 갈래가 위에 온다 (G01R31·H02J3 이 한 칸에 뭉치지 않게)")
    check(pcfg.subgroup_of(["H02J9/06"], "y04s20") == "",
          "하위 갈래를 두지 않은 분야는 빈 값을 준다 (없는 갈래를 지어내지 않게)")
    check(all(s.get("en") for s in pcfg.SUBGROUPS["y04s10"]),
          "하위 갈래도 CPC 공식 표제를 함께 담는다")
    # 화면에 싣는 cpc 는 앞 세 개로 자른다. 하위 갈래를 브라우저에서 다시 계산하면
    # 네 번째 코드로 갈리는 건을 놓치므로, 서버가 전체 목록으로 정해 실어 보낸다.
    four = ["H02G3/00", "G06F18/20", "G06N3/08", "H02J15/00"]
    check(pcfg.subgroup_of(four, "y04s10") == "10/14",
          "네 번째 이후 코드로 갈리는 건도 잡는다 (피드의 cpc 는 세 개로 잘려 있다)")
    check("subgBlockHTML(SUBG[r.cat.key]" in js,
          "카드가 하위 갈래 막대를 실제로 그린다")
    check("it.sub" in js and "FEED.patents.subgroups" in js,
          "화면이 서버가 정한 하위 갈래(it.sub)를 쓴다 (다시 계산하지 않는다)")

    # ── 매트릭스는 상위 몇 곳까지만 그린다 ─────────────────────────
    # 국내 공보를 전수로 받기 시작하며 이 표가 5,026행 158,071px 이 됐다.
    # 상한이 빠지면 그 상태로 조용히 되돌아가므로 세 자리를 다 못 박는다:
    # 상한이 걸려 있나 · 넘치는 만큼 펼침 버튼이 나오나 · 버튼이 실제로 붙나.
    check("regionMatrixHTML(list, {total:true, top:MTX_TOP})" in js,
          "통계 탭 매트릭스에 상위 N 상한이 걸려 있다 (5,026행을 그대로 그리지 않게)")
    check("mtxOpen" in js and "data-mtx" in js and "'[data-mtx]'" in js,
          "펼침 버튼과 그 처리기가 함께 있다 (버튼만 있고 안 눌리는 상태를 막는다)")
    # 버튼 문구에 남은 수가 들어가야 한다. '더 보기' 만으로는 30곳이 남았는지
    # 3,000곳이 남았는지 몰라 누를지를 고를 수 없다.
    check("more.toLocaleString()" in js and "곳 펼치기" in js,
          "펼침 버튼이 남은 곳 수를 숫자로 말한다")

    # ── 뉴스 ↔ 특허 분야 잇기 ──────────────────────────────────────
    # 두 축은 다른 것을 잰다(뉴스=무슨 일이 벌어지나, 특허=어떤 기술에 권리가
    # 걸렸나). 합치지 않고 겹치는 자리만 잇는데, 한쪽 분류가 바뀌면 이 표가
    # 조용히 죽는다 — 실제로 특허 분야를 Y04S 로 다시 세우자 여섯 분야가 전부
    # '뉴스 짝 없음' 으로 나왔다(오류 없이). 양쪽 키가 살아 있는지 못 박는다.
    print("\n· 뉴스 ↔ 특허 분야 잇기")
    import ip_guide as _ig
    import news_config as _nc
    pat_keys = {c["key"] for c in pcfg.CATEGORIES}
    news_keys = {c["key"] for c in _nc.CATEGORIES}
    bad_l = sorted(set(_ig.FIELD_NEWS) - pat_keys)
    check(not bad_l, "왼쪽 키가 전부 실재하는 특허 분야다 "
                     + (f"(없는 분야: {bad_l})" if bad_l else f"({len(_ig.FIELD_NEWS)}개)"))
    bad_r = sorted({k for v in _ig.FIELD_NEWS.values() for k in v} - news_keys)
    check(not bad_r, "오른쪽 키가 전부 실재하는 뉴스 분류다 "
                     + (f"(없는 분류: {bad_r})" if bad_r else ""))
    check(set(_ig.FIELD_MAP) <= pat_keys,
          "단서(FIELD_MAP)도 특허 분야 키를 쓴다")
    check(len(_ig.FIELD_NEWS) >= 2,
          f"실제로 이어지는 분야가 있다 ({len(_ig.FIELD_NEWS)}개 — 0개면 표가 죽은 것이다)")
    # 두 축이 조용히 갈라지지 않게 한다. 특허 분야를 Y04S 로 다시 세웠을 때
    # 뉴스 쪽은 그대로였고, 수송(1,547건)·통신(51건)이 '뉴스 짝 없음' 으로 남았다
    # — 실측하니 뉴스 1,760건 중 전기차·충전 기사는 3건뿐이었고 그마저 데이터센터
    # 기사가 '전기차' 를 스쳐 언급한 것이었다. 뉴스는 검색어로만 모으므로,
    # 검색어가 없으면 안 모이고 안 모이면 없는 셈이 된다.
    # 분야를 새로 만들 때 이 검사가 '뉴스 쪽은 어떻게 할 것이냐' 를 묻는다.
    orphan = sorted(pat_keys - set(_ig.FIELD_NEWS))
    check(not orphan,
          "모든 특허 분야에 대응하는 뉴스 분류가 있다 "
          + (f"(짝이 없는 분야: {orphan} — 뉴스 검색어를 만들거나, "
             f"짝이 없다는 판단을 여기 적어야 한다)" if orphan
             else f"({len(pat_keys)}개 모두)"))
    # 새로 만든 분류는 검색어가 있어야 한다. 검색어가 비면 카테고리만 있고
    # 기사가 영원히 0건인 칸이 생긴다.
    noq = [c["key"] for c in _nc.CATEGORIES if not c.get("queries")]
    check(not noq, f"모든 뉴스 분류에 검색어가 있다 (빈 것: {noq})")
    # 비중과 배율은 다른 수다. ratio 를 비중으로 적어 원전이 '뉴스 비중 121%' 로
    # 나왔다 — 비중은 100%를 넘을 수 없으니 읽는 사람에게는 그냥 틀린 수다.
    check("d.news.share" in js and "'배'" in js,
          "비중은 share 로, 변화는 '배' 로 따로 적는다 (121% 가 다시 나오지 않게)")
    check("catShareSeries(d.newsKeys" in js,
          "흐름 그래프도 이어진 뉴스 분류를 전부 합쳐 그린다")
    # 세 번째 상태. 짝이 없는 것과 짝은 있는데 아직 안 모은 것은 다르다 —
    # 새 분류를 만든 직후 '0%' 로 찍으면 '뉴스가 이 기술을 안 다룬다' 로 읽힌다.
    check("fresh: paired && rows.length===0" in js
          and "수집 시작 전" in js and "d.fresh" in js,
          "짝은 있는데 아직 0건인 상태를 따로 알린다 ('0%' 로 찍지 않는다)")

    # ── 해외 출원인의 국내 공개: 회사별 / 기술별 ──────────────────
    # 같은 자료인데 묻는 것이 둘이다. 회사별은 '이 회사가 한국에 무엇을 걸어
    # 뒀나', 기술별은 '이 기술에 누가 한국에 권리를 걸고 있나' 다. 기술별이
    # 없을 때는 뒤쪽 물음에 답하려면 회사 블록을 전부 훑으며 분야칩을 눈으로
    # 세야 했다 — 출원인이 수십 곳이라 사실상 불가능했다.
    print("\n· 국내 공개 — 회사별 / 기술별")
    import json, re as _re, subprocess, tempfile
    check("data-kr=\"ap\"" in js and "data-kr=\"cat\"" in js
          and "'[data-kr]'" in js,
          "두 보기 버튼과 그 처리기가 함께 있다 (버튼만 있고 안 눌리는 상태를 막는다)")
    # 읽기만 봐서는 안 된다 — setItem 을 지워도 getItem 줄에 이름이 남아
    # 통과한다(변이시험에서 실제로 통과했다). 읽기와 쓰기를 따로 본다.
    check("getItem('pnp_krMode')" in js
          and "setItem('pnp_krMode'" in js,
          "고른 보기를 쓰고 또 읽는다 (새로 고칠 때마다 회사별로 돌아가지 않게)")
    # 묶은 기준은 블록 머리에 이미 있다 → 항목에는 **다른 축**이 붙어야 한다.
    # 기술별인데 항목에도 분야명을 붙이면 같은 말을 두 번 하고, 정작 누가 낸
    # 건인지는 끝내 안 보인다.
    # 나머지는 글자를 맞춰 보는 대신 **실제로 돌려서** 본다. 문자열 대조로는
    # 정렬을 0 으로 곱해 무력화해도 그 글자는 그대로 남아 통과한다(변이시험에서
    # 실제로 통과했다).
    ks2 = js.find("const KR_MODES")
    ke2 = js.find("// ── 분야별 경쟁 구도")
    check(0 <= ks2 < ke2, "국내 공개 패널 블록을 떼어낼 수 있다")
    if 0 <= ks2 < ke2:
        items = (
            # 큰 곳 3건 · 작은 곳 1건 — 기술별 블록 안 순서를 가른다
            [{"office": "KR", "aCountry": "JP", "aName": "큰곳", "aFlag": "🇯🇵",
              "category": "y04s10", "title": f"큰{i}", "number": f"N{i}", "url": ""}
             for i in range(3)]
            + [{"office": "KR", "aCountry": "CN", "aName": "작은곳", "aFlag": "🇨🇳",
                "category": "y04s10", "title": "작은0", "number": "M0", "url": ""}]
            # 분야를 모르는 건 — 기술별에서 빠져야 한다
            + [{"office": "KR", "aCountry": "US", "aName": "미상곳", "aFlag": "🇺🇸",
                "category": "", "title": "분야없음", "number": "X0", "url": ""}]
            # 국내 출원인·해외 공개는 애초에 이 패널에 들어오지 않는다
            + [{"office": "KR", "aCountry": "KR", "aName": "국내곳", "aFlag": "🇰🇷",
                "category": "y04s10", "title": "국내건", "number": "K0", "url": ""},
               {"office": "US", "aCountry": "JP", "aName": "큰곳", "aFlag": "🇯🇵",
                "category": "y04s10", "title": "해외공개", "number": "U0", "url": ""}]
        )
        feed = {"patents": {"categories": [
            {"key": "y04s10", "emoji": "⚡", "name": "발전·송배전 지원"}], "krLimit": 15}}
        # 이 블록은 첫 줄에서 localStorage 를 읽는다(브라우저에만 있다).
        # 저장해 둔 값을 돌려주게 해서, 초기값이 실제로 그걸 따르는지도 본다 —
        # 글자만 맞춰 보면 그 줄을 무력화해도 이름이 남아 통과한다.
        prog3 = ("const localStorage={getItem(){return 'cat';},setItem(){}};\n"
                 + js[ks2:ke2]
                 + "\nfunction esc(s){return String(s).replace(/[&<>\"]/g,"
                   "c=>({'&':'&amp;','<':'&lt;','>':'&gt;','\"':'&quot;'}[c]));}\n"
                   "function flg(f){return '<svg class=\"fl\"></svg>';}\n"
                   "function safeUrl(u){return u||'#';}\n"
                 + f"const FEED={json.dumps(feed, ensure_ascii=False)};\n"
                 + f"const IT={json.dumps(items, ensure_ascii=False)};\n"
                 # 화면 상태. 검색·필터가 걸리면 분석 첫 줄의 말이 달라져야 한다.
                 + ("const state={q:'', cats:new Set(), countries:new Set(),"
                    " newonly:false, period:'all', source:'', savedOnly:false,"
                    " unreadOnly:false};\n")
                 + ("const boot=krMode;\n"
                    "krMode='ap'; const ap=krEntryHTML(IT);\n"
                    "krMode='cat'; const cat=krEntryHTML(IT);\n"
                    "krMode='ap'; const wide=krEntryHTML(IT);\n"
                    "state.q='x'; const narrow=krEntryHTML(IT);\n"
                    "state.q='';\n"
                    "console.log(JSON.stringify({boot, ap, cat, wide, narrow,"
                    " none:krEntryHTML([])}));"))
        prog3 = prog3.replace("\\\\", "\\")
        with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False,
                                         encoding="utf-8") as f:
            f.write(prog3)
            path3 = f.name
        try:
            out3 = subprocess.run(["node", path3], capture_output=True, text=True,
                                  timeout=30)
            ok3 = out3.returncode == 0
            check(ok3, "국내 공개 패널 JS 가 실행된다"
                       + ("" if ok3 else f" ({out3.stderr.strip()[:200]})"))
            r3 = json.loads(out3.stdout) if ok3 else None
        finally:
            try:
                __import__("os").unlink(path3)
            except OSError:
                pass
        if r3:
            heads = lambda h: _re.findall(r'<div class="kap">(.*?)</div>', h)
            check(r3["boot"] == "cat",
                  f"저장해 둔 보기로 시작한다 (받은 값 {r3['boot']!r})")
            # 통계 탭의 이 패널은 걸러진 목록 위에서도 다시 그려진다. 그림이 여기
            # 남아 있으면 걸러 낸 몇 건을 아카이브 전체인 양 말하게 되므로, 아예
            # 그리지 않는 것이 맞다 — 그림은 필터가 없는 홈에만 있다.
            check('"kalead"' not in r3["wide"] and '"kfrow"' not in r3["wide"],
                  "통계 탭 패널이 실제로 그림 없이 그려진다 (문자열이 아니라 결과로 확인)")
            # 이 패널은 '해외 출원인이 국내에 공개한 것' 만 담는다.
            check("국내곳" not in r3["ap"] and "해외공개" not in r3["ap"],
                  "국내 출원인과 해외 공개분은 애초에 들어오지 않는다")
            # 묶은 기준은 블록 머리에 있으니 항목에는 **다른 축**이 붙어야 한다.
            first_cat_item = _re.search(r'<ul class="klist">(.*?)</li>', r3["cat"])
            check(bool(first_cat_item) and "큰곳" in first_cat_item.group(1),
                  "기술별 항목에는 출원인이 붙는다 (분야명을 두 번 적지 않는다)")
            first_ap_item = _re.search(r'<ul class="klist">(.*?)</li>', r3["ap"])
            check(bool(first_ap_item)
                  and "발전·송배전 지원" in first_ap_item.group(1),
                  "회사별 항목에는 분야가 붙는다")
            # 분야를 모르는 건은 기술별에서 빠진다 — 이름 없는 블록이 생기지 않게.
            check("분야없음" in r3["ap"] and "분야없음" not in r3["cat"],
                  "분야를 모르는 건은 회사별에는 남고 기술별에서만 빠진다")
            # 블록 안 순서: 많이 낸 곳이 먼저. 0 을 곱해 무력화하면 여기서 걸린다.
            body = r3["cat"][r3["cat"].find('<ul class="klist">'):]
            check(body.find("큰곳") < body.find("작은곳"),
                  "기술별 블록 안은 그 분야에 많이 낸 곳부터 세운다")
            # 블록 머리에 건수와 출원인 수가 함께 선다.
            check(any("2곳" in h for h in heads(r3["cat"])),
                  "기술별 블록 머리가 그 분야의 출원인 수를 말한다")
            check(r3["none"] == "", "담을 것이 없으면 패널을 통째로 뺀다")
    # 패널 하나만 갈아 끼워야 한다. 통계 전체를 다시 그리면 위에서 펼쳐 둔
    # 국적 묶음이 그대로 있어도 스크롤이 튄다.
    check("querySelector('.krpanel')" in js,
          "보기를 바꿀 때 그 패널만 갈아 끼운다")

    # ── 이슈 흐름의 화살표는 비중 변화다 ──────────────────────────
    # 여기서 크게 틀리고 있었다. 수집은 아카이브 전체와 중복을 제거하므로
    # 아카이브가 커질수록 '새 기사' 로 잡히는 수가 구조적으로 줄어든다 —
    # 실측(2026-08-26)에서 여덟 분야가 예외 없이 ▼ 였고 그중 둘은 **실제로는
    # 늘어난** 분야였다(원전 화면 '135건 ▼160' · 실제 비중 1.12배 증가).
    # 방향이 정해진 오차라 특히 나쁘다.
    print("\n· 이슈 흐름 화살표")
    ins = js[js.find("function insightsHTML"):]
    ins = ins[:ins.find("\n}")]
    check("r.delta" not in ins,
          "화살표를 건수 차이(delta)로 정하지 않는다")
    check("r.ratio" in ins and "'배'" in ins,
          "비중 배율로 정하고 '배' 로 적는다 (건수와 헷갈리지 않게)")
    # 낱말을 그대로 고정하지 않는다(문구를 다듬을 때마다 깨진다). 지켜야 할 것은
    # '비중이라고 말하고, 건수가 아님을 못 박는가' 다.
    check("비중" in ins and _re.search(r"건수[^']{0,6}아닙니다", ins),
          "부제가 화살표의 뜻을 밝힌다 (비중이며 건수가 아니다)")
    # 이전 기간에 없던 분류는 배율이 정의되지 않는다(ratio=null). 새로 만든
    # 분류가 여기 해당하는데, 그때 화살표를 그리면 없던 수를 지어내는 셈이다.
    check("r.ratio==null" in ins and "새 분류" in ins,
          "이전 기간에 없던 분류는 배율 대신 '새 분류' 라고 적는다")

    # ── 분야 설명은 데이터에서 만든다 ─────────────────────────────
    # '전력 8대 분야(IPC)' 라고 손으로 적어 둔 문구가, 분야를 Y04S 여섯으로 갈아
    # 끼운 뒤에도 그대로 남아 있었다 — 화면이 없는 분야 여덟 개를 말하고 있었다.
    # 수를 적지 말고 목록을 세게 한다.
    print("\n· 분야 설명")
    # 주석에도 같은 말이 있다 — 그 내력을 적어 둔 것이라 지우면 안 된다.
    # 그러니 **주석을 걷어낸 뒤 따옴표 안의 문자열만** 본다. 주석 안의 따옴표까지
    # 세면 설명을 써 둔 것 때문에 검사가 실패한다(실제로 그랬다).
    # 오늘만 다섯 번째로 겪는 자리다 — 글자를 찾을 때는 어디서 찾는지가 먼저다.
    _nc = _re.sub(r"^\s*//.*$", "", js, flags=_re.M)
    _lit = _re.findall(r"'([^'\n]*)'", _nc)
    stale = [t for t in _lit if "8대 분야" in t or "여덟 분야" in t]
    check(not stale,
          "화면 문구에 분야 수를 손으로 적지 않는다 (Y04S 로 바꾼 뒤 6개가 됐다) "
          + (f"(남은 것: {stale})" if stale else ""))
    check("catScopeText" in js and "cs.length+'대 분야'" in js,
          "분야 수를 실제 목록에서 센다")
    # 설명이 **함수 안에서** 두 가지를 다 말해야 한다. 밖 어딘가에 그 글자가
    # 있으면 통과하던 검사라, 원전 대목을 지워도 조용했다(변이시험에서 통과했다).
    cn = js[js.find("function catScopeNote"):]
    cn = cn[:cn.find("\n}")]
    check("CPC Y04S" in cn and "Y02E 30" in cn and "c.outside" in js,
          "설명이 Y04S 갈래와 원전을 따로 더한 이유를 함께 말한다")
    check("+catScopeNote()+" in js,
          "설명을 실제로 그린다 (정의만 두지 않는다)")

    # ── 홈 첫 화면: 오늘의 한 줄 · 오늘의 짝 · 30일 흐름 ──────────
    # 실측(1280x800) 첫 화면에 그림이 하나도 없었고 브리핑은 3px 만 걸쳤다 —
    # '오늘 무슨 일인가' 를 알려면 스크롤해야 했다.
    print("\n· 홈 첫 화면")
    css2 = _re.sub(r"\s+", "", _sr._CSS)
    # ① 머리글만 올린다. 본문을 옮기면 같은 글이 두 곳에 생긴다.
    check("todayLineHTML" in js and "+ todayLineHTML" not in js.replace("const td=todayLineHTML();", ""),
          "오늘의 한 줄을 그린다")
    check("const td=todayLineHTML(); if(td) parts.push(td);" in js,
          "홈이 오늘의 한 줄을 **부른다** (정의만 두지 않는다)")
    check('id="sec-brief"' in js and 'id="sec-pbrief"' in js,
          "내려갈 자리(브리핑 카드)에 앵커가 있다")
    # 위임은 그 요소가 든 컨테이너에 달아야 한다. [data-jump] 가 #guide 에만
    # 있어서 홈의 줄은 눌러도 아무 일이 없었다(실측).
    home_h = js[js.find("$('#home').onclick"):]
    home_h = home_h[:home_h.find("$('#results')")] or home_h[:4000]
    # 주석에도 같은 글자가 들어 있다 → **부르는 모양**으로 본다. 글자만 찾으면
    # 코드를 지워도 주석이 남아 통과한다(변이시험에서 실제로 통과했다).
    check("closest('[data-jump]')" in home_h,
          "홈 컨테이너에도 [data-jump] 처리기가 있다 (#guide 에만 있어 안 눌리던 자리)")
    check("classList.contains('collapsed')" in home_h,
          "접힌 카드로 내려가면 펴 준다 (내려갔는데 접혀 있으면 헛걸음이다)")

    # ② 뉴스와 특허를 잇는 다리. 전에는 '오늘의 짝' 패널이 이 일을 했는데,
    # 그 패널이 보여 준 특허 6건 중 3건이 바로 아래 '이번 주 공개 특허' 와
    # 겹쳤고(실측) 왼쪽 절반(오른 분야)은 결론 한 줄이 이미 말하고 있었다.
    # 295px 짜리 패널 대신 링크 하나로 남긴다 — 다만 **다리 자체는 있어야 한다**.
    check("pairHTML" not in js and "pairRows" not in js,
          "겹치던 '오늘의 짝' 패널은 없앴다")
    check("data-catpat" in js and "l2go" in js,
          "결론 한 줄이 그 분야 특허로 가는 다리를 남긴다")
    check("'[data-catpat]'" in js,
          "그 다리를 누르면 실제로 움직인다 (버튼만 있고 안 눌리는 상태를 막는다)")

    # ③ 흐름은 건수가 아니라 비중이어야 위 화살표와 같은 것을 말한다.
    ins2 = js[js.find("function insightsHTML"):]
    ins2 = ins2[:ins2.find("\n}")]
    check("sparkShare(catShareSeries([r.key], TREND_DAYS))" in ins2,
          "흐름 선은 비중으로 그린다 (건수는 아카이브가 커질수록 구조적으로 준다)")
    check("TREND_DAYS" in js and "비중" in ins2
          and _re.search(r"건수[^']{0,6}아닙니다", ins2),
          "부제가 무엇을 재는지 밝힌다 (비중이지 건수가 아니다)")
    # 패널 둘을 하나로 접었다. 옛 키워드 칩은 '▲' 옆에 **생건수 차이**를 찍어
    # '▲-178' 처럼 부호가 스스로 모순인 표시를 내고 있었다(원전 146건/7일 vs
    # 324건/21일 — 하루평균으로는 늘고 총량으로는 줄었다). 되살아나지 않게 막는다.
    # 글자를 통째로 박아 두면 따옴표만 달라도 빠져나간다(변이시험에서 통과했다).
    # 그 **식** 자체를 막는다 — count-prev 는 이 버그 말고 쓸 데가 없다.
    check("k.count-k.prev" not in js.replace(" ", ""),
          "키워드에 생건수 차이를 붙이지 않는다 ('▲-178' 이 다시 나오지 않게)")
    check("catKeywords" in js and "KW_MIN_SHARE" in js,
          "키워드를 분야에 붙인다 (한 분야에 모이는 말만)")
    check("c[top]/hit.length < KW_MIN_SHARE" in js,
          "흩어지는 일반어는 어느 분야의 말도 아니다 ('전력' 24%·'AI' 44%)")
    # 1위가 뚜렷할 때만 '지금은 X 다' 라고 말한다. 20%:19% 인데 같은 문장을 쓰면
    # 그건 거짓말이 된다 → 격차를 보고 말이 바뀌어야 한다.
    check("LEAD_GAP" in js and "gap >= LEAD_GAP" in js
          and "함께 이슈입니다" in js and "뚜렷한 쏠림이 없습니다" in js,
          "격차가 좁으면 결론 문장이 바뀐다 (단정하지 않는다)")
    check("leadLineHTML(ct, cmp)" in ins2 and "trendChartHTML(ct)" in ins2,
          "결론 한 줄과 30일 그래프를 실제로 그린다")
    # comparable=false 는 '비교할 이전 기간이 아직 얇다' 는 뜻이다. 표는 그 값을
    # 보는데 결론 한 줄만 안 봐서, '증감은 표시하지 않습니다' 라고 적힌 화면에서
    # 이 줄만 '▲1.1배' 를 말하고 있었다.
    ld = js[js.find("function leadLineHTML"):]
    ld = ld[:ld.find("\n}")]
    check("(!cmp||r.ratio==null)" in ld and "const up=cmp?" in ld and "const dn=cmp?" in ld,
          "비교 기간이 얇으면 결론 한 줄도 배율을 말하지 않는다")
    # 머리글이 부른 분야를 아래에서 또 부르면 같은 말을 두 번 한다
    # (1위가 식는 날 '지금은 원전입니다 ▼0.8배 … 식은 곳 원전 0.8배').
    check("named.indexOf(r) < 0" in ld and "named.push(a, b)" in ld,
          "머리글이 부른 분야는 '오른 곳·식은 곳' 에서 뺀다 (한쪽만 빼던 비대칭)")
    check("_josaOnly(b.name" in ld,
          "조사를 받침으로 고른다 (_josa 는 낱말까지 붙여 돌려준다)")
    # 그래프의 세로축은 비중이다 — 일별 기사 수가 23~180건으로 널뛰어 건수로
    # 그리면 그 널뜀이 곡선을 다 먹는다.
    tc = js[js.find("function trendChartHTML"):]
    tc = tc[:tc.find("\nfunction ")]
    check("catShareSeries([r.key], TREND_DAYS)" in tc,
          "그래프도 비중으로 그린다")
    check("top.slice(0,4)" in tc or "rows.slice(0,4)" in tc,
          "선은 넷까지만 — 그 이상은 서로 구분되는 색을 못 뽑는다")
    check("TCOL" in js and js.count("'#2F6FB5'") >= 1,
          "선 색은 검사기로 맞춘 조합을 쓴다")
    # 선 끝 이름이 겹치면 둘 다 못 읽는다(실측: 15%와 13% 곡선이 붙었다).
    check("LBL_GAP" in tc and "L.y = lab[k-1].y + LBL_GAP" in tc,
          "선 끝 이름이 겹치면 밀어낸다")
    # 오른쪽 여백이 붙박이면 조금만 긴 이름이 상위 넷에 들 때 잘린다
    # (실측: '반도체 클러스터·메가프로젝트' 가 730/660 으로 나갔다).
    check("const padR = Math.max(pad.r" in tc and "(r.name||'').length" in tc,
          "선 끝 이름 길이에 맞춰 오른쪽 여백을 잡는다")
    # 같은 이름의 규칙을 **같은 층에** 둘 두면 세기가 같아 순서로 진다 — 전에
    # 물린 자리다(.shns .inb 가 뒤에 오는 .shns .sbn 에 졌다). 미디어 쿼리 안의
    # 재정의는 다른 이야기라 세지 않는다 → 중괄호 깊이로 가른다.
    def _top_level(sel: str) -> int:
        n, depth, i = 0, 0, 0
        while i < len(css2):
            c = css2[i]
            if c == "{":
                depth += 1
            elif c == "}":
                depth -= 1
            elif css2.startswith(sel, i) and depth == 0:
                n += 1
            i += 1
        return n
    tr = _top_level(".trend.row{")
    check(tr == 1,
          f"'.trend .row' 을 같은 층에 두 번 적지 않는다 (둘이면 순서로 진다 — 받은 값 {tr})")
    check("minmax(120px,1fr)62px74pxminmax(0,1.1fr)" in css2,
          "이름·흐름·수치·말 네 칸이 같은 격자에 선다")

    # ── 홈은 요약, 경쟁·활용 탭은 상세 ─────────────────────────────────
    # 분야 카드 여섯 장이 1,900px 이라 홈 높이의 절반을 먹고 있었다. 홈은 '지금
    # 무슨 일이 벌어지나' 를 한눈에 보이는 자리라 카드가 너무 무겁다 → 지도와
    # 한 줄 요약만 남기고, 상세는 '누구와 부딪히나' 를 묻는 경쟁·활용 탭으로 보낸다.
    print("\n· 홈은 요약, 경쟁·활용 탭은 상세")
    # 부르는 것만 보면 그 분기가 죽어도 통과한다(변이시험에서 통과했다) —
    # where==='trade' 를 false 로 바꾸면 경쟁·활용 탭이 홈 판본을 그리는데도 검사가
    # 조용했다. 분기가 **있고 그 안에서 body 를 낸다**는 것까지 본다.
    check("tradeSectionHTML('trade')" in js, "경쟁·활용 탭이 'trade' 로 부른다")
    ts = js[js.find("function tradeSectionHTML"):]
    ts = ts[:ts.find("\n}")]
    br = ts[ts.find("if(where === 'trade')"):]
    br = br[:br.find("return '<div class=\"homepanel\"")]
    check("if(where === 'trade')" in ts and "+ body" in br,
          "거래 분기가 분야 카드(body)를 낸다 (분기가 죽으면 홈 판본이 나온다)")
    # 정의만 보면 안 된다 — 'function catSummaryHTML(rows){' 자체가 그 글자를
    # 담고 있어서, 부르는 줄을 지워도 통과한다(변이시험에서 실제로 통과했다).
    check("+ catSummaryHTML(rows)" in js and "class=\"crow2" in js,
          "홈이 한 줄 요약을 **부른다**")
    # 같은 카드를 두 곳에서 그리면 둘 중 하나는 반드시 뒤처진다.
    home_part = js[js.find("return '<div class=\"homepanel\" id=\"sec-analysis\">"):]
    home_part = home_part[:home_part.find("\n}")]
    check("body" not in home_part.replace("bodyx", ""),
          "홈 패널은 분야 카드(body)를 그리지 않는다 (같은 표를 두 곳에서 그리지 않게)")
    check("data-catgo" in js and "'[data-catgo]'" in js,
          "요약 줄을 누르면 경쟁·활용 탭의 그 분야로 간다 (버튼만 있고 안 눌리는 상태를 막는다)")
    check('.trow[data-cat="' in js and 'data-cat="\'+esc(r.cat.key)' in js,
          "카드에 분야 키가 달려 있어 찾아갈 수 있다")
    check("classList.add('hit')" in js and ".trow.hit{" in css2,
          "찾아간 카드를 잠깐 표시한다 (탭만 바뀌면 무엇을 골랐는지 놓친다)")
    # 추정 총계는 실수다. 그대로 찍으면 '8,282.095' 처럼 쉼표와 소수점이 섞인다.
    check("Math.round(r.tot).toLocaleString()" in js,
          "요약 줄의 추정 규모를 반올림해 찍는다")
    # 특허 브리핑 본문은 접는다(실측 780px — 그것 하나가 홈 한 화면을 먹었다).
    # 접는 쪽은 서술, 펴 두는 쪽은 결론이라 접어도 무슨 일이 있었는지는 읽힌다.
    pb = js[js.find("function patentBriefHomeHTML"):]
    pb = pb[:pb.find("\n}")]
    check("<details class=\"pbfull\"" in pb and "bpoints" in pb
          and pb.find("bpoints") < pb.find("pbfull"),
          "홈 특허 브리핑은 짚은 점을 펴 두고 긴 본문만 접는다")

    # ── 홈의 매트릭스는 축약판이다 ─────────────────────────────────
    # 경쟁·활용 탭 판본(상위 10곳 + 펼치기)을 그대로 홈에 놓으면 홈이 다시 길어진다.
    # 정의만 보면 안 된다 — 부르는 줄이 있어야 화면에 뜬다.
    # 부르기만 해서는 모자란다 — 부른 값이 홈 마크업에 **들어가야** 뜬다.
    # (변이시험: 'FULL ?' 를 'false ?' 로 바꿔도 '부른다' 만 보면 통과했다.)
    check("const mx=FULL ? matrixHomeHTML(FEED.patents.items" in js
          and "+mx+" in js,
          "홈이 매트릭스 축약판을 부르고 그 값을 화면에 넣는다")
    mh = js[js.find("function matrixHomeHTML"):]
    mh = mh[:mh.find("\n}")]
    check("top:MTX_HOME" in mh and "const MTX_HOME = 3;" in js and "fixed:true" in mh,
          "홈 매트릭스는 국적마다 상위 세 곳만 낸다")
    # 상위 N 만 자르는 것으로는 모자랐다. 실측(전체 자료)에서 홈 매트릭스에 펼치기
    # 단추 여섯 개가 그대로 나왔고('나머지 3,162곳 펼치기'), 앞서 목업 자료로만
    # 봤을 때는 항목이 넷뿐이라 단추가 안 나와서 놓쳤다.
    _rs = js[js.find("function _rgSecHTML("):]
    _rs = _rs[:_rs.find("\n}")]
    check("&& !opts.fixed)" in _rs and "!opts.fixed && mtxOpen.has" in _rs,
          "축약판은 펼치기 단추를 내지 않고 남이 펼쳐 둔 상태도 따르지 않는다")
    check('data-jump2="guide:sec-matrix"' in mh,
          "홈 축약판에서 전체 판본으로 가는 길이 있다")
    # 탭을 건너뛰는 이동은 data-jump 로는 안 된다(같은 화면 안에서만 움직인다).
    check("'[data-jump2]'" in js and "gotoTab(v[0], v[2]? {view:v[2]} : undefined)" in js,
          "'전체 보기' 가 탭과 **보기**까지 건너뛴다 (통계는 탭만으로 닿지 않는다)")
    # #home 에만 달면 홈 밖에 놓은 다리는 눌러도 아무 일이 없다(실측한 적 있다).
    _j2 = js[max(0, js.find("[data-jump2]") - 400):]
    check("document.addEventListener('click'" in js
          and js.find("document.addEventListener('click'") < js.find("const j2=e.target.closest"),
          "탭 건너뛰기 다리가 문서 전체에 달려 있다 (홈 밖에서도 눌린다)")

    # ── 통계 탭 차례: 요약을 먼저, 목록을 나중에 ───────────────────
    # 국내 공개는 개별 건 **목록**이고 위의 둘은 전체를 요약한 집계다.
    # 목록이 먼저 오면 요약 둘을 보기까지 한참 내려가야 한다.
    st = js[js.find("function renderStats("):]
    st = st[:st.find("\nfunction ", 10)]
    i_cmp, i_rank, i_kr = (st.find("🧭 분야별 경쟁 구도"),
                           st.find("🏆 출원인 랭킹"), st.find("+ krEntryHTML(list)"))
    check(min(i_cmp, i_rank, i_kr) > 0 and i_cmp < i_rank < i_kr,
          "통계 탭이 경쟁 구도 → 출원인 랭킹 → 국내 공개 차례로 선다")

    # ── 좁은 화면에서 본문이 가로로 넘치지 않는다 ──────────────────
    # 통계 탭이 430px 화면에서 본문 536px 이었다(배포본에서도 그랬다). 원인은
    # 세 가지가 겹친 것이고, 셋 다 '줄어들 수 없어서' 생긴다:
    #   · 그리드 칸의 기본 min-width:auto → 표의 최소폭까지 패널이 늘어난다.
    #     .pmxwrap 에 overflow-x:auto 가 있어도 조상이 안 줄면 스크롤 상자가
    #     아예 만들어지지 않아 아무 일도 하지 않는다.
    #   · 검색칸이 제 최소폭을 고집해 오른쪽 묶음을 화면 밖으로 민다.
    #   · minmax(300px,…) 는 화면이 300px 보다 좁아도 칸을 못 줄인다.

    check(".stats>.panel{min-width:0}" in css2,
          "통계 패널이 줄어들 수 있다 (표의 최소폭이 화면을 밀어내지 않게)")
    check(".searchrow{" in css2 and "flex-wrap:wrap}" in
          css2[css2.find(".searchrow{"):css2.find(".searchrow{") + 120],
          "검색 줄이 좁은 화면에서 접힌다")
    # 검색칸과 그 안의 input 둘 다 줄어들 수 있어야 한다. input 은 기본 최소폭이
    # 있어 flex:1 만으로는 안 줄고, 그 최소폭이 그대로 바깥까지 밀어낸다.
    def _rule(sel):
        i = css2.find(sel)
        return css2[i:css2.find("}", i)] if i >= 0 else ""
    # 공백을 지운 CSS 라 '.search input{' 은 '.searchinput{' 이 된다.
    check("min-width:0" in _rule(".search{")
          and "min-width:0" in _rule(".searchinput{"),
          "검색칸과 그 입력칸 둘 다 줄어들 수 있다")
    check("minmax(min(300px,100%),1fr)" in css2,
          "국내 공개 칸이 화면보다 넓어지지 않는다 (320px 에서 넘치던 자리)")

    # ── 탭 이름과 그것을 가리키는 문구가 어긋나지 않는다 ─────────────
    # 이 탭은 '거래·지원' 이었다가 '경쟁·활용' 이 됐다(화면 절반이 경쟁 구도인데
    # 이름은 거래만 말하고 있었다). 이름을 바꾸면 **그 탭을 가리키는 문구**도 같이
    # 바뀌어야 한다 — 안 그러면 '거래 탭에 있습니다' 라고 안내해 놓고 그런 이름의
    # 탭이 없는 상태가 된다. 한쪽만 고치는 실수를 기계가 잡는다.
    _page = _sr._PAGE
    _m = _re.search(r'data-tab="guide">([^<]+)<', _page)
    check(bool(_m), "경쟁·활용 탭 버튼이 있다")
    if _m:
        _tab = _m.group(1)
        _core = _tab.split(' ', 1)[-1].strip()       # 이모지를 뗀 이름
        check("거래" not in _tab, f"탭 이름에 옛 이름이 남지 않았다 (받은 값 {_tab!r})")
        # 화면 문자열(주석 제외)에서 이 탭을 가리키는 말이 새 이름을 쓰는가.
        _body = "\n".join(l for l in js.split("\n") if not l.strip().startswith("//"))
        _refs = _re.findall(r"[가-힣·]* 탭", _body)
        _bad = sorted({r for r in _refs
                       if r.strip() not in ("뉴스 탭", "특허 탭", _core + " 탭")})
        check(not _bad, f"탭을 가리키는 문구가 실제 탭 이름을 쓴다 (어긋난 것: {_bad})")
    # 스크린리더가 읽는 이름도 함께 맞춘다(화면과 다른 이름을 읽으면 더 헷갈린다).
    check('aria-label="특허 경쟁 구도·활용 안내"' in _page,
          "탭 영역의 aria-label 이 새 이름을 따른다")

    # ── 해외 국내공개: 나열 위에 읽는 값을 올린다 ────────────────────
    kr = js[js.find("function krEntryHTML("):]
    kr = kr[:kr.find("\nfunction ", 10)]
    # 그림은 홈에만, 목록은 통계 탭에만. 같은 그림을 두 곳에 그리면 둘 중 하나는
    # 반드시 뒤처진다(홈 분야 카드에서 이미 겪었다).
    check("krAnalysisHTML" not in kr,
          "통계 탭의 국내 공개 패널은 그림을 그리지 않는다 (홈으로 갔다)")
    khome = js[js.find("function krHomeHTML("):]
    khome = khome[:khome.find("\n}")]
    check("krAnalysisHTML(rows, krAll, cm)" in khome and "homepanel" in khome,
          "홈 판본이 그림을 그린다")
    check("const kr=FULL ? krHomeHTML(FEED.patents.items" in js
          and "+ (kr? '<div class=\"homemx\">'+kr+'</div>' : '')" in js,
          "홈이 그 판본을 부르고 그 값을 화면에 넣는다")
    # 홈은 걸러진 목록이 아니라 **전체**를 센다 — filtered() 를 넘기면 검색 상태에
    # 따라 홈의 수치가 흔들린다.
    check("krHomeHTML(filtered())" not in js,
          "홈은 걸러진 목록이 아니라 전체에서 센다")
    # 목록이 맨 아래에 오려면 매트릭스 뒤에 놓여야 한다.
    check(js.find("const mx=FULL ? matrixHomeHTML") < js.find("const kr=FULL ? krHomeHTML"),
          "해외 국내공개가 홈 맨 아래에 온다")
    ka = js[js.find("function krAnalysisHTML("):]
    ka = ka[:ka.find("\nfunction ", 10)]
    # 분모가 핵심이다. 해외 건수만 세면 44건이 큰지 작은지 알 길이 없다 —
    # 그 분야 국내 공개분 **전체**로 나눠야 '밀도' 가 나온다.
    check("krAll.filter(it=>it.category===c.key).length" in ka
          and "rows.filter(it=>it.category===c.key).length" in ka,
          "분야별 해외 비중의 분모가 그 분야 국내 공개분 전체다")
    check("const krAll=list.filter(it=>it.office==='KR')" in kr,
          "분모는 같은 창 안의 KR 공개분에서 센다")
    # 0~100% 자로 그리면 최댓값 23% 인 화면에서 다섯 줄이 전부 토막이 된다.
    kb = js[js.find("function krBarRow("):]
    kb = kb[:kb.find("\n}")]
    check("max>0? Math.round(num*100/den*100/max)" in kb and "width:'+w+'%" in kb,
          "막대는 그 묶음의 최댓값에 맞춰 그린다 (0~100% 자로는 차이가 안 읽힌다)")
    check("<b>'+pct+'%</b>" in kb and "num+'/'+den+'건" in kb,
          "막대가 정규화된 만큼 %와 원래 건수를 줄마다 직접 적는다")
    check("fldMax=fld.length? fld[0].pct*100" in ka and "rgMax=rows.length? Math.max(" in ka,
          "두 그림이 각자의 최댓값을 쓴다 (분모가 달라 자를 같이 쓸 수 없다)")
    check("f.den<KR_THIN? '표본 적음'" in ka and "const KR_THIN" in js,
          "분모가 얇은 줄은 지우지 않고 '표본 적음' 을 붙인다")
    # 줄마다 grid 를 따로 만들면 이름 길이에 따라 막대 자리가 달라져 견줄 수가 없다.
    check(".kfrow{display:contents}" in css2 and ".kfrows{display:grid" in css2
          and "'<div class=\"kfrows\">'+fldRows" in ka
          and "'<div class=\"kfrows\">'+rgRows" in ka,
          "한 그림의 막대가 같은 자를 쓴다 (줄마다 grid 를 따로 두지 않는다)")
    # 화면 폭으로 재면 두 칸이 나란한 820px 을 놓친다 — 칸은 349px 뿐이었다.
    check("@container(max-width:420px)" in css2
          and "container-type:inline-size" in _rule(".kacol{"),
          "이름 칸이 좁아지면 제 줄로 올라간다 (화면 폭이 아니라 칸 폭으로 잰다)")
    # details 를 접었을 때 안을 숨기는 것은 UA 규칙이라 작성자 규칙에 진다.
    check("display:none" in _rule(".kchips{") and ".ksolo[open]>.kchips{display:flex}" in css2,
          "접힌 칩 묶음이 실제로 숨는다 (display:flex 를 그냥 주면 접어도 보인다)")
    check("display:none" in _rule(".krmore>.krwrap{")
          and ".krmore[open]>.krwrap{display:grid" in css2,
          "접힌 나머지 블록이 실제로 숨는다")
    check("order.slice(0, KR_BLOCKS)" in kr and "const KR_BLOCKS" in js
          and "order.slice(KR_BLOCKS)" in kr,
          "블록은 많이 낸 곳부터 몇 곳만 펴 둔다 (쉰 곳을 다 펴면 320px 에서 14,000px 였다)")
    check("g.items.length===1" in kr and "solo.length>=5" in kr,
          "1건뿐인 곳은 칩으로 모은다 (163곳 중 112곳이 그랬다)")

    # 한 dict 리터럴에 같은 키를 두 번 적으면 파이썬은 **조용히 뒤엣것만** 남긴다.
    # FIELD_NEWS 를 "news" 로 넣었다가 먼저 있던 "news"(해석 문구)에 덮여, 화면이
    # 오류 없이 짝을 전부 잃었다. 사람 눈으로는 안 보이는 종류의 실수라 기계가 본다.
    import ast as _ast
    dups = []
    for path in ("site_render.py", "ip_guide.py", "patent_config.py"):
        for node in _ast.walk(_ast.parse(open(path, encoding="utf-8").read())):
            if not isinstance(node, _ast.Dict):
                continue
            names = [k.value for k in node.keys
                     if isinstance(k, _ast.Constant) and isinstance(k.value, str)]
            for n in names:
                if names.count(n) > 1 and (path, n) not in dups:
                    dups.append((path, n))
    check(not dups, "한 dict 안에 같은 키를 두 번 적지 않는다 "
                    + (f"(덮인 키: {dups})" if dups else "(3개 파일)"))

    # ── 브리핑이 무엇을 정리한 것인지 밝힌다 ─────────────────────
    # 브리핑은 어제 하루치를 아침에 정리한다(수집이 Routine 뒤에 돌기 때문이고,
    # 아침의 오늘치는 몇 시간 분량뿐이라 그 편이 고르다). 전에는 하루 차이에
    # 아무 표시가 없어 '오늘의 브리핑' 인데 날짜는 어제인 채로 어긋나 보였다.
    # 글자만 맞춰 보지 않고 **그려서** 확인한다.
    _bh = js[js.find("function briefHTML()"):]
    _bh = _bh[:_bh.find("\nfunction ", 10)]
    _prog = (
        "const localStorage={getItem(){return '0';},setItem(){}};\n"
        "let briefCollapsed=false;\n"
        "function esc(s){return String(s);}\n"
        "function latestNewsDate(){return FEED.news.perDay.slice(-1)[0].x;}\n"
        + _bh + "\n"
        "function run(bd, last){ FEED={brief:{date:bd, headline:'H'},"
        " news:{perDay:[{x:last}]}}; return briefHTML(); }\n"
        "let FEED;\n"
        "console.log(JSON.stringify({same:run('2026-08-27','2026-08-27'),"
        " one:run('2026-08-26','2026-08-27'), three:run('2026-08-24','2026-08-27')}));")
    with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False,
                                     encoding="utf-8") as f:
        f.write(_prog); _bp = f.name
    try:
        _out = subprocess.run(["node", _bp], capture_output=True, text=True, timeout=30)
        _ok = _out.returncode == 0
        check(_ok, "브리핑 머리글 JS 가 실행된다"
                   + ("" if _ok else f" ({_out.stderr.strip()[:200]})"))
        _r = json.loads(_out.stdout) if _ok else None
    finally:
        try:
            __import__("os").unlink(_bp)
        except OSError:
            pass
    if _r:
        check("어제 하루치" in _r["one"],
              "하루 차이면 '어제 하루치' 라고 밝힌다 (정상인데 어긋나 보이던 자리)")
        check("어제 하루치" not in _r["same"] and "일 전 작성" not in _r["same"],
              "같은 날이면 군더더기를 붙이지 않는다")
        check("3일 전 작성" in _r["three"] and "어제 하루치" not in _r["three"],
              "이틀 이상 벌어지면 그때는 경고한다")

    # ── 예약 시각 ─────────────────────────────────────────────────
    # 정시(분 0)는 전 세계가 몰려 GitHub 이 지연시키거나 그 실행을 **버린다**.
    # 실측: "0 23" 이던 한 달 동안 한 번도 정시에 못 떴고(23:22~23:45),
    # 2026-08-27 아침에는 34회 연속 성공 뒤 처음으로 누락됐다.
    #
    # 그리고 수집은 브리핑 Routine(23:03 UTC)보다 **뒤**여야 한다 — Routine 이
    # 배포본에서 '최신 뉴스 날짜'를 찾아 그날치를 정리하므로, 수집이 먼저 끝나면
    # 브리핑이 조용히 당일치로 바뀐다(지금 의도는 어제 하루치다).
    # 중계가 수집을 부르는 것은 브리핑이 바뀐 것과 무관해야 한다. 조건을 걸면
    # 순환이 된다 — 수집이 돌아야 새 뉴스가 생기고, 새 뉴스가 있어야 브리핑이
    # 바뀌고, 브리핑이 바뀌어야 수집을 부른다. 한 곳이 멈추면 스스로 못 푼다.
    _relay = open(".github/workflows/relay-brief.yml", encoding="utf-8").read()
    _call = _relay[_relay.find("- name: 배포 워크플로 호출"):]
    check("gh workflow run daily-power-news.yml" in _call
          and "if:" not in _call,
          "중계는 브리핑이 그대로여도 수집을 부른다 (조건을 걸면 순환이 된다)")
    # 조건을 없애도 paths 필터가 남아 있으면 소용이 없다 — 브리핑 파일이 안 바뀐
    # 푸시는 애초에 이 워크플로를 깨우지 못해, 고리는 그대로 남는다.
    _push = _wf_block(_wf_block(_relay, "on", 0), "push", 2)
    check(bool(_push) and not any(l.lstrip().startswith("paths:") for l in _push),
          "중계는 푸시가 있었다는 사실만으로 돈다 (paths 로 거르면 고리가 남는다)")
    # 브랜치는 좁히지 않는다. Routine 세션이 어느 이름으로 밀지 우리가 못 정한다 —
    # 프롬프트는 claude/happy-bohr 를 쓰라고 하지만 세션의 outcome 브랜치는
    # claude/bold-hamilton 이고 원격에 그 접미사 판본들이 실제로 쌓여 있다.
    # 좁혔다가 다른 이름으로 밀면 중계가 통째로 안 깨어난다.
    _br = [l.lstrip()[1:].strip().strip("'\"")
           for l in _wf_block(_push, "branches", 4) if l.lstrip().startswith("- ")]
    check(_br == ["claude/**"],
          f"중계는 claude/** 를 다 받는다 (좁히면 Routine 브랜치를 놓친다 — 받은 값 {_br})")

    # ── 저장소는 표준 라이브러리 밖으로 나가지 않는다 ────────────────
    # 이번 고장의 본체다. selftest 가 import 하던 yaml 이 러너에 없어
    # 세 워크플로가 나흘치 통째로 실패했다. 워크플로는 pip install 을 하지
    # 않으므로, 검사용이든 수집용이든 밖의 모듈을 하나 들이면 그날로 수집이
    # 멈춘다. try 로 감싼 것은 없어도 도는 것이라 봐준다(news_config 의
    # dotenv 가 그렇다).
    import glob as _glob
    for _p in sorted(_glob.glob("*.py")):
        _tree = _ast.parse(open(_p, encoding="utf-8").read())
        _guarded = {id(_s) for _n in _ast.walk(_tree)
                    if isinstance(_n, _ast.Try) for _s in _ast.walk(_n)}
        _mods: set[str] = set()
        for _n in _ast.walk(_tree):
            if id(_n) in _guarded:
                continue
            if isinstance(_n, _ast.Import):
                _mods |= {a.name.split(".")[0] for a in _n.names}
            elif isinstance(_n, _ast.ImportFrom) and not _n.level and _n.module:
                _mods.add(_n.module.split(".")[0])
        _ext = sorted(_mods - set(__import__("sys").stdlib_module_names)
                      - {q[:-3] for q in _glob.glob("*.py")})
        check(not _ext,
              f"{_p}: 표준 라이브러리와 저장소 모듈만 쓴다 (밖의 것: {_ext})")

    ROUTINE_MIN = 23 * 60 + 5          # 브리핑 Routine 이 뜨는 시각(UTC) 뒤
    for wf in sorted(_glob.glob(".github/workflows/*.yml")):
        for line in open(wf, encoding="utf-8"):
            m = _re.search(r'^\s*-\s*cron:\s*"(\S+)\s+(\S+)\s', line)
            if not m:
                continue
            mi, hh = m.group(1), m.group(2)
            name = wf.split("/")[-1]
            check(mi != "0",
                  f"{name}: 예약이 정시가 아니다 (분={mi} — 정시는 누락된다)")
            if mi.isdigit() and hh.isdigit():
                check(int(hh) * 60 + int(mi) >= ROUTINE_MIN,
                      f"{name}: 수집이 브리핑 Routine 뒤에 온다 "
                      f"({hh}:{mi} UTC — 앞서면 브리핑이 당일치로 바뀐다)")


def _foreign_checks() -> None:
    """해외 수집기. 국내와 규칙이 뒤집힌 자리가 많아 회귀 검사가 특히 중요하다."""
    import xml.etree.ElementTree as ET
    from datetime import datetime

    import patent_source_foreign as fg
    import patent_source_kipris as kr

    print("\n[해외 백엔드]")
    orig_get, orig_key = fg._get, cfg.KIPRIS_KEY
    orig_cap, orig_rows = cfg.FOREIGN_PER_CAT, cfg.FOREIGN_ROWS
    cfg.KIPRIS_KEY = "TEST"
    calls: list[dict] = []
    try:
        # 이름·키 질의·파라미터가 국내와 섞이지 않았는지 (섞이면 조용히 빈 결과다)
        url = fg._url({"ipc": "H02M", "currentPage": "1"})
        check("ForeignPatentAdvencedSearchService" in url,
              "서비스 이름의 오타(Advenced)를 그대로 쓴다")
        check("ForeignPatentAdvancedSearchService" not in url,
              "철자를 고친 이름(Advanced)을 쓰지 않는다 — 그 경로는 없다")
        check("accessKey=" in url and "ServiceKey=" not in url,
              "키 질의 이름이 accessKey 다 (국내는 ServiceKey)")
        check("ipc=" in url and "ipcNumber=" not in url,
              "분류 파라미터가 ipc 다 (국내는 ipcNumber)")
        check("/openapi/rest/" in url, "기준 경로가 openapi/rest 다")

        # 성공 판정이 국내와 뒤집혀 있다 — 여기가 틀리면 정상 응답이 전부 오류가 된다
        ok = ET.fromstring(_fg_page([("202600213551A1", "H02M1/10", "US")], 1))
        check(fg._total(ok) == 1, "totalSearchCount 를 읽는다 (국내는 totalCount)")
        bad = ('<?xml version="1.0"?><response><header><resultCode>11'
               "</resultCode><resultMsg>No Mandatory Request Parameters Error"
               "</resultMsg></header></response>")

        import urllib.request
        saved_open = urllib.request.urlopen

        class _R:
            def __init__(self, s): self.s = s
            def read(self): return self.s.encode()
            def __enter__(self): return self
            def __exit__(self, *a): return False

        urllib.request.urlopen = lambda *a, **k: _R(bad)
        try:
            fg._get({"ipc": "H02M"})
            check(False, "resultCode 가 채워져 오면 오류로 본다")
        except RuntimeError as e:
            check("11" in str(e), f"resultCode 가 채워져 오면 오류로 본다 ({e})")
        finally:
            urllib.request.urlopen = saved_open

        urllib.request.urlopen = lambda *a, **k: _R(
            _fg_page([("202600213551A1", "H02M1/10", "US")], 1))
        try:
            fg._get({"ipc": "H02M"})
            check(True, "resultCode 가 비어 오면 정상으로 본다 (국내와 정반대)")
        except Exception as e:
            check(False, f"resultCode 가 비어 오면 정상으로 본다 ({e})")
        finally:
            urllib.request.urlopen = saved_open

        # ── 쪽넘김: currentPage 는 '시작 위치'다 ──────────────────────
        # 쪽 번호로 넘기면 각 쪽이 겹치면서 뒤쪽 자료에 영원히 닿지 못한다.
        cfg.FOREIGN_ROWS, cfg.FOREIGN_PER_CAT = 50, 500
        universe = [(f"20260021{i:04d}A1", "H02M1/10", "US") for i in range(120)]

        def fake(params, timeout=None):
            calls.append(dict(params))
            start = int(params["currentPage"])
            n = int(params["docsCount"])
            return ET.fromstring(
                _fg_page(universe[start - 1:start - 1 + n], len(universe)))
        fg._get = fake

        cat = {"key": "mega", "name": "전력반도체", "emoji": "x",
               "ipc": ["H02M"], "cpc": ["H02M"]}
        items, total, capped = fg._sweep(cat, "20260601~20260825")
        starts = [int(c["currentPage"]) for c in calls]
        check(starts[:3] == [1, 51, 101],
              f"시작 위치를 쪽 크기만큼 더해 넘긴다 (보낸 값 {starts[:3]})")
        check(len(items) == len(universe),
              f"120건을 하나도 빠뜨리지 않고 받는다 (받은 값 {len(items)}건)")
        check(len({i['number'] for i in items}) == len(items),
              "받은 목록에 중복이 없다")
        check(total == len(universe), f"전체 건수를 읽는다 ({total})")
        check(not capped, "상한에 걸리지 않았다")

        # 항목 스키마가 국내와 같아야 목록에 그대로 섞인다
        need = {"number", "title", "assignee", "pub_date", "office", "cpc",
                "category", "applicant", "country", "flag", "url"}
        check(not (need - set(items[0])),
              f"항목 스키마가 국내와 같다 (누락 {need - set(items[0]) or '없음'})")
        check(items[0]["number"].startswith("US"),
              f"공개번호 앞에 나라를 붙인다 ({items[0]['number']})")
        check(items[0]["office"] == "US", "office 가 공개국이다")

        # 모르는 해외 출원인을 한국으로 떨어뜨리면 '국내 공급자' 표가 오염된다
        n, r, f = fg._identify_foreign("Nichicon Corporation")
        check(r == "" and f == "",
              f"큐레이션에 없는 해외 출원인의 국적은 비운다 (받은 값 {r!r})")
        n2, r2, _ = fg._identify_foreign("Siemens Energy AG")
        check(r2 and r2 != "KR",
              f"큐레이션에 있으면 그 국적을 쓴다 ({n2} → {r2})")

        # 일본 공보는 회사 이름이 일본어로 온다. 영문 별칭만 두면 같은 회사가
        # 둘로 갈린다 — 실측에서 Toyota 가 영문 245 + 일본어 222 로 쪼개져
        # 랭킹이 절반만 반영됐다(국내에서 겪은 '엘지 ≠ LG' 와 같은 문제).
        for jp, want in (("トヨタ自動車株式会社", "Toyota"),
                         ("株式会社東芝", "Toshiba"),
                         ("パナソニックIPマネジメント株式会社", "Panasonic"),
                         ("三菱電機株式会社", "Mitsubishi Electric"),
                         ("株式会社日立製作所", "Hitachi")):
            got = kr._identify(jp)[0]
            check(got == want, f"일본어 표기가 붙는다: {jp} → {got}")

        # KIPRIS 는 실체참조를 두 번 감싼다. 한 번만 풀면 화면에 글자로 남는다.
        check(kr._unescape("XI&apos;AN JIAOTONG") == "XI'AN JIAOTONG",
              "남은 &apos; 를 푼다")
        check(kr._unescape("Fitch &amp;#x26; Flannery") == "Fitch & Flannery",
              "두 번 감싼 &#x26; 도 푼다")
        check(kr._unescape("정상 이름 & 회사") == "정상 이름 & 회사",
              "이미 정상인 글자는 건드리지 않는다")

        # 수집기에서 푸는 것만으로는 부족하다 — 아카이브는 누적이라 이미 저장된
        # 항목은 그대로다(실측: 고친 뒤에도 저장분 430건에 XI&apos;AN 이 남았다).
        # 그래서 그리기 직전에 한 번 더 푼다.
        import site_render as _sr
        check(_sr._plain("XI&apos;AN JIAOTONG") == "XI'AN JIAOTONG",
              "화면에 그리기 직전에도 실체참조를 푼다 (옛 저장분까지 고쳐진다)")
        check(_sr._plain("Vorwerk &amp; Co.") == "Vorwerk & Co.",
              "&amp; 도 화면에서 풀린다")
        check('_plain(p.get("applicant")' in _sr.__doc__ or
              '_plain' in open("site_render.py", encoding="utf-8").read()
              .split("def _patent_feed")[1][:3000],
              "_patent_feed 가 이름을 그리기 전에 _plain 을 거친다")

        # 국내 수집이 살아 있는데 해외가 죽으면, 국내까지 잃으면 안 된다
        import patent_source_kipris as ks
        saved_fg = fg.collect
        fg.collect = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("해외 다운"))
        try:
            kept = [{"number": "KR1", "office": "KR", "applicant": "한국전력공사"}]
            out, st = ks._add_foreign(datetime(2026, 8, 25), kept,
                                      {"totals": {"한국전력공사": 1}})
            check(out == kept and "foreignError" in st,
                  "해외가 실패해도 국내 결과를 지키고 그 사실을 남긴다")
        finally:
            fg.collect = saved_fg

        # 공개국 축이 화면이 읽는 모양으로 나오는지 — 모양이 다르면 오류 없이
        # 표만 빈다(officeCounts[출원인][특허청]).
        saved_fg2 = fg.collect
        fg.collect = lambda *a, **k: ([{
            "number": "US1", "office": "US", "applicant": "Siemens"}], {})
        try:
            out, st = ks._add_foreign(
                datetime(2026, 8, 25),
                [{"number": "KR1", "office": "KR", "applicant": "한국전력공사"}],
                {"totals": {}})
            off = st.get("offices") or {}
            check(off.get("한국전력공사", {}).get("KR") == 1
                  and off.get("Siemens", {}).get("US") == 1,
                  f"공개국 집계가 [출원인][특허청] 모양이다 ({off})")
            check(st.get("replaceOffices") is True,
                  "공개국 집계도 전수라 갈아 끼우게 표시한다")
            import patent_archive as pa
            store = {"totals": {}, "offices": {"Siemens": {"WO": 183}},
                     "updated": {}}
            pa.merge_stats(store, {"totals": {}, "offices": off,
                                   "replaceTotals": True,
                                   "replaceOffices": True})
            check("WO" not in store["offices"].get("Siemens", {}),
                  "옛 공개국 수치(OPS 시절)가 실제로 버려진다")
        finally:
            fg.collect = saved_fg
    finally:
        fg._get, cfg.KIPRIS_KEY = orig_get, orig_key
        cfg.FOREIGN_PER_CAT, cfg.FOREIGN_ROWS = orig_cap, orig_rows


def _kipris_checks() -> None:
    """KIPRIS 백엔드의 라이브 경로를 네트워크 없이 실제로 실행한다.

    py_compile 로는 못 잡는 것들을 잡으려는 것이다 — OPS 때 _live_collect 의
    NameError 가 월요일 실행에서야 드러난 적이 있다. 여기서 확인하는 것은
    '수집이 도는가'가 아니라 **site_render 가 기대하는 계약을 지키는가**다.
    """
    import xml.etree.ElementTree as ET
    from datetime import datetime
    import patent_source_kipris as ks

    import urllib.request

    print("\n[KIPRIS 백엔드]")
    orig_get, orig_key = ks._get, cfg.KIPRIS_KEY
    orig_lim, orig_open = cfg.KIPRIS_CPC_LIMIT, urllib.request.urlopen
    # 이 검사는 **국내 경로**를 본다. collect() 는 이제 해외까지 붙이므로 꺼 둔다 —
    # 켜 두면 해외 요청이 이 검사의 스텁으로 새어 들어와 CPC 호출 수가 어긋난다
    # (실제로 5건이어야 할 것이 22건으로 찍혔다). 해외는 _foreign_checks 가 본다.
    orig_fg = cfg.FOREIGN
    cfg.FOREIGN = False
    cfg.KIPRIS_KEY = "TEST"
    calls: list[dict] = []
    cpc_calls: list[str] = []
    try:
        # CPC 보강은 urlopen 을 직접 쓴다 → 여기서도 막지 않으면 스모크 테스트가
        # 네트워크를 탄다(러너에서 수집 전에 도는 검사라 절대 나가면 안 된다).
        cfg.KIPRIS_CPC_LIMIT = 5
        _CPC = ('<?xml version="1.0"?><response><body><items><patentCpcInfo>'
                "<CooperativepatentclassificationNumber>Y04S 10/50"
                "</CooperativepatentclassificationNumber></patentCpcInfo>"
                "</items></body></response>")

        class _CpcResp:
            def read(self): return _CPC.encode()
            def __enter__(self): return self
            def __exit__(self, *a): return False

        def _fake_open(req, *a, **k):
            cpc_calls.append(getattr(req, "full_url", str(req)))
            return _CpcResp()
        urllib.request.urlopen = _fake_open

        def fake(op, params, timeout=None):
            calls.append(dict(params, _op=op))
            who = ["한국전력공사", "인천대학교 산학협력단", "주식회사 현대케피코"]
            n = len(calls)
            return ET.fromstring(_kipris_page(
                [(n * 10 + k, who[k], params["ipcNumber"]) for k in range(3)], 42))
        ks._get = fake

        items, mock, stats = ks.collect(datetime(2026, 8, 25))
        check(not mock, "라이브 경로로 돈다 (MOCK 폴백이 아니다)")
        check(bool(items), f"수집 결과가 있다 ({len(items)}건)")

        need = {"number", "title", "assignee", "pub_date", "office", "cpc",
                "category", "applicant", "country", "flag", "url"}
        missing = need - set(items[0])
        check(not missing, f"항목 스키마가 site_render 계약을 지킨다 (누락 {missing or '없음'})")

        keys = {c["key"] for c in cfg.CATEGORIES}
        check(all(i["category"] in keys for i in items),
              "모든 항목이 8대 분야 중 하나로 분류된다")
        check(all(i["office"] == "KR" for i in items), "국내 공보이므로 office 는 전부 KR")
        nums = [i["number"] for i in items]
        check(len(nums) == len(set(nums)), "공개번호가 중복되지 않는다")
        check(all(i["url"].startswith("https://") for i in items),
              "카드 링크가 전부 https 다")
        check(len({i["url"] for i in items}) == len(items),
              "url 이 항목마다 다르다 (읽음 상태의 키라 겹치면 한꺼번에 토글된다)")

        # 총계는 따로 조회하지 않고 모은 것을 센다 — 그 약속이 지켜지는지 본다.
        check(sum(stats["totals"].values()) == len(items),
              "출원인 총계의 합 = 수집 건수 (표본과 전수가 어긋나지 않는다)")
        # 전수를 가져오므로 집계는 병합이 아니라 대체여야 한다. 병합하면 OPS 시절
        # 값(전 세계·CPC 기준)이 남아 단위가 다른 수치와 한 표에 섞인다(첫 실전
        # 실행에서 실제로 그랬다 — Siemens 183 같은 옛 수치가 그대로 남았다).
        check(stats.get("replaceTotals") is True,
              "집계를 대체로 표시한다 (옛 OPS 수치가 섞이지 않는다)")
        import patent_archive as pa
        store = {"totals": {"Siemens": 183}, "updated": {"Siemens": "2026-08-03"},
                 "offices": {}}
        pa.merge_stats(store, stats, "2026-08-25")
        check("Siemens" not in store["totals"] or store["totals"]["Siemens"] != 183,
              "merge_stats 가 옛 값을 실제로 버린다")

        # 공동출원('|')과 한글 법인명이 같은 회사를 둘로 가르면 랭킹이 거짓이 된다.
        check(ks._split_applicants("현대자동차주식회사|기아 주식회사")
              == ["현대자동차주식회사", "기아 주식회사"],
              "공동출원인을 '|' 로 나눈다")
        for raw, want in (("주식회사 엘지에너지솔루션", "LG에너지솔루션"),
                          ("삼성에스디아이 주식회사", "삼성SDI"),
                          ("도요타 지도샤(주)", "Toyota"),
                          ("컨템포러리 엠퍼렉스 테크놀로지 씨오., 리미티드", "CATL")):
            check(ks._identify(raw)[0] == want,
                  f"한글 법인명이 붙는다: {raw} → {want}")
        check(all(p.get("openDate") for p in calls),
              "모든 질의에 공개일 범위가 들어간다 (기간 없이 전수를 긁지 않는다)")
        check(all("~" in p["openDate"] for p in calls),
              "공개일 범위 표기가 'YYYYMMDD~YYYYMMDD' 다 (실측 형식)")
        check(not any("cpcNumber" in p for p in calls),
              "cpcNumber 를 보내지 않는다 (이 API 에 없는 파라미터 — 실측)")

        # CPC 보강: 검색으로는 못 잡는 Y04S 를 출원번호로 되받아 분류에 반영한다.
        check(len(cpc_calls) == cfg.KIPRIS_CPC_LIMIT,
              f"CPC 보강이 상한만큼만 돈다 ({len(cpc_calls)}건)")
        check(all("patentCpcInfo" in u and "applicationNumber=" in u
                  for u in cpc_calls),
              "CPC 는 출원번호로 조회한다 (공개번호가 아니다)")
        check(all(cfg.KIPRIS_CPC_KEYPARAM + "=" in u for u in cpc_calls),
              f"CPC 조회의 키 질의 이름이 {cfg.KIPRIS_CPC_KEYPARAM} 다 (계열이 다르다)")
        enriched = [i for i in items if any(c.startswith("Y04S") for c in i["cpc"])]
        check(bool(enriched), f"보강된 건에 CPC 가 들어간다 ({len(enriched)}건)")
        check(all(i["category"].startswith("y04s") for i in enriched),
              "Y04S 가 붙으면 그 Y04S 분야로 분류된다 (IPC 로는 못 잡는 코드)")

        # 국내 전용이라 특허청 축은 만들지 않는다. 빈 dict 이어야 기존 stats 가
        # 덮이지 않는다(merge_stats 계약).
        check(ks.collect_offices(datetime(2026, 8, 25)) == {},
              "collect_offices 는 빈 dict (기존 공개국 집계를 덮지 않는다)")

        # 경로가 틀리면 KIPRIS 는 HTTP 200 + 포털 HTML 을 준다. 그때 XML 파싱이
        # 깨지는데, 사람이 읽을 수 있는 오류로 바뀌는지 확인한다(실측으로 물렸던 함정).
        ks._get = orig_get
        class _Fake:
            def read(self): return b"<!doctype html><html>\xed\x8e\x98\xec\x9d\xb4\xec\xa7\x80"
            def __enter__(self): return self
            def __exit__(self, *a): return False
        saved_open = urllib.request.urlopen
        urllib.request.urlopen = lambda *a, **k: _Fake()
        try:
            ks._get("getAdvancedSearch", {"ipcNumber": "H02M"})
            check(False, "포털 HTML 응답이 오류로 드러난다")
        except RuntimeError as e:
            check("XML" in str(e), f"포털 HTML 응답이 오류로 드러난다 ({e})")
        finally:
            urllib.request.urlopen = saved_open

        # 접두당 상한 판정. 예전에는 '접두별 totalCount 의 합 > 상한' 으로 짐작해
        # 두 방향으로 다 틀릴 수 있었다. 두 경우를 실제로 돌려 확인한다.
        saved_cap = cfg.KIPRIS_PER_CAT
        try:
            cfg.KIPRIS_PER_CAT = 6
            three = {"ipc": ["A", "B", "C"], "cpc": ["A"], "key": "renew",
                     "name": "테스트", "emoji": "x"}

            # ① 접두마다 2건씩(합 6, 상한과 같음) — 아무것도 잘리지 않았다.
            seq = [0]
            def small(op, params, timeout=None):
                seq[0] += 1
                base = seq[0] * 100
                return ET.fromstring(_kipris_page(
                    [(base + k, "한국전력공사", params["ipcNumber"])
                     for k in range(2)], 900))     # totalCount 는 크게 900
            ks._get = small
            _, _, hit = ks._sweep_category(three, "20260101~20260301")
            check(not hit,
                  "접두별 합계가 커도 실제로 안 잘렸으면 상한 경고가 없다"
                  " (옛 방식이면 거짓 경고)")

            # ② 한 접두가 상한까지 차오른다 — 잘렸다.
            seq2 = [0]
            def big(op, params, timeout=None):
                seq2[0] += 1
                base = seq2[0] * 1000
                return ET.fromstring(_kipris_page(
                    [(base + k, "한국전력공사", params["ipcNumber"])
                     for k in range(cfg.KIPRIS_ROWS)], 900))
            ks._get = big
            _, _, hit2 = ks._sweep_category(three, "20260101~20260301")
            check(hit2, "접두 하나가 상한에 닿으면 잘린 것으로 보고한다")
        finally:
            cfg.KIPRIS_PER_CAT = saved_cap
            ks._get = orig_get
    finally:
        ks._get, cfg.KIPRIS_KEY = orig_get, orig_key
        cfg.KIPRIS_CPC_LIMIT = orig_lim
        cfg.FOREIGN = orig_fg
        urllib.request.urlopen = orig_open


def _site_url_checks() -> None:
    """저장소 이름이 바뀌어도 canonical·사이트맵이 따라오는가.

    이름을 손으로 적어 두면 이름을 바꾸는 날 조용히 없는 주소를 가리키게 된다 —
    화면은 멀쩡한데 검색엔진만 잘못된 정본을 믿는, 눈에 안 띄는 종류의 고장이다.
    """
    import importlib
    import os as _os

    import site_render as _sr

    print("\n· 사이트 주소")
    keep = (_os.environ.get("GITHUB_REPOSITORY"), _os.environ.get("NEWS_SITE_URL"))
    try:
        for slug, want in [
            ("ahn0405-cpu/ippower", "https://ahn0405-cpu.github.io/ippower/"),
            ("ahn0405-cpu/power-news-patents-archive",
             "https://ahn0405-cpu.github.io/power-news-patents-archive/"),
            # <owner>.github.io 저장소만 경로 없이 뿌리에 붙는다.
            ("ahn0405-cpu/ahn0405-cpu.github.io", "https://ahn0405-cpu.github.io/"),
        ]:
            _os.environ["GITHUB_REPOSITORY"] = slug
            _os.environ.pop("NEWS_SITE_URL", None)
            m = importlib.reload(_sr)
            check(m.SITE_URL == want,
                  f"저장소 이름을 따라간다: {slug} → {m.SITE_URL}")
        # 사람이 정한 값이 있으면 그것이 이긴다(다른 도메인에 얹을 때 쓴다).
        _os.environ["NEWS_SITE_URL"] = "https://example.org/x/"
        m = importlib.reload(_sr)
        check(m.SITE_URL == "https://example.org/x/",
              "NEWS_SITE_URL 을 주면 그것이 이긴다")
        # 이름을 코드에 손으로 적어 두지 않는다(옛 이름이 남으면 이 검사가 잡는다).
        src = open("site_render.py", encoding="utf-8").read()
        body = "\n".join(l for l in src.split("\n") if not l.strip().startswith("#"))
        n = body.count("power-news-patents-archive")
        check(n <= 1,
              f"저장소 이름을 코드에 거듭 적지 않는다 (폴백 1곳만 — 받은 값 {n})")
    finally:
        for k, v in zip(("GITHUB_REPOSITORY", "NEWS_SITE_URL"), keep):
            if v is None:
                _os.environ.pop(k, None)
            else:
                _os.environ[k] = v
        importlib.reload(_sr)


def _news_outage_checks() -> None:
    """뉴스 소스가 통째로 죽은 날, 사이트는 그래도 지어지는가.

    2026-09-04 에 구글 뉴스가 11개 카테고리 전부 503 을 내면서 빌드가 죽었다.
    아카이브 43일치가 멀쩡히 있는데도 site/ 가 만들어지지 않아, 그날 올린 문구
    수정까지 배포되지 못했다. 수집 실패가 배포 실패로 번지지 않아야 한다.
    """
    import news_archive
    import news_source

    print("\n· 뉴스 소스가 죽은 날")

    # 전량 실패는 전용 예외로 온다 — 진짜 버그(TypeError 등)와 섞이면 부르는 쪽이
    # 둘을 갈라 다룰 수 없다.
    check(issubclass(news_source.CollectFailed, RuntimeError),
          "전량 실패에 전용 예외가 있다 (버그와 갈라 다루려면 필요하다)")

    orig = news_source._fetch_rss
    try:
        def _dead(_q):
            raise OSError("HTTP Error 503: Service Unavailable")
        news_source._fetch_rss = _dead
        raised = None
        try:
            news_source._live_collect()
        except Exception as e:                                  # noqa: BLE001
            raised = e
        check(isinstance(raised, news_source.CollectFailed),
              f"모두 503 이면 CollectFailed 를 올린다 (받은 값 {type(raised).__name__})")
    finally:
        news_source._fetch_rss = orig

    # 부르는 쪽이 그 예외만 골라 잡고, **merge 를 건너뛴다**. 빈 목록으로 merge 하면
    # '기사 0건인 날' 이 아카이브에 생겨 30일 추이가 그날만 푹 꺼진다.
    src = open("build_site.py", encoding="utf-8").read()
    blk = src[src.find("if what in (\"news\", \"both\")"):]
    blk = blk[:blk.find("# ── 특허 수집")]
    # 주석을 걷어내고 본다 — 위 주석이 merge_today 를 언급해서, 날것으로 위치를
    # 비교하면 주석 쪽이 먼저 잡혀 엉뚱하게 판정된다(실제로 그렇게 틀렸다).
    code = "\n".join(l for l in blk.split("\n") if not l.strip().startswith("#"))
    check("except news_source.CollectFailed" in code,
          "빌드가 그 예외만 골라 잡는다 (다른 예외는 그대로 터져야 한다)")
    check("else:" in code
          and code.find("except news_source.CollectFailed") < code.find("merge_today"),
          "수집이 실패하면 merge 를 건너뛴다 (빈 날짜를 만들지 않는다)")
    check("_mock_collect" not in code and "MOCK 으로 메우지" in blk,
          "실패했다고 MOCK 으로 메우지 않는다 (NEWS_MOCK=off 의 뜻이다)")

    # 실제로 빈 목록을 merge 하면 어떻게 되는지 — 위 규칙이 왜 필요한지를 못 박는다.
    days = {"2026-09-03": {"date": "2026-09-03", "articles": [{"title": "t", "url": "u"}]}}
    news_archive.merge_today(days, "2026-09-04", [], False)
    check("2026-09-04" in days and not days["2026-09-04"]["articles"],
          "빈 목록으로 merge 하면 실제로 빈 날짜가 생긴다 (그래서 건너뛰어야 한다)")


def main() -> int:
    today = datetime(2026, 7, 27)
    orig = (ps._search, ps._get_token, cfg.OPS_KEY, cfg.OPS_SECRET, cfg.REQUEST_DELAY)
    ps._get_token = lambda: "stub-token"
    cfg.OPS_KEY, cfg.OPS_SECRET, cfg.REQUEST_DELAY = "k", "s", 0.0
    try:
        print("· _live_collect (주간 특허 수집 경로)")
        stub = Stub()
        ps._search = stub
        items, stats = ps._live_collect(today)
        check(bool(items), f"특허를 수집한다 ({len(items)}건)")
        check(isinstance(stats.get("totals"), dict) and bool(stats["totals"]),
              f"출원인 총계를 담는다 ({len(stats.get('totals', {}))}곳)")
        check(all(i.get("category") and i.get("number") for i in items),
              "모든 항목에 분야·공개번호가 있다")

        print("· 국내(KR) 공개 추가 수집 (해외 출원인 한정)")
        # KR 전용 질의(pn any "KR")가 해외 출원인에게만 나가는지, 그 결과가 목록에
        # 더해지는지 확인한다. 스텁은 질의 문자열을 기록만 하고 문서를 돌려준다.
        seen_cql: list[str] = []

        class KrStub(Stub):
            def __call__(self, token, cql, start, end, timeout=None):
                seen_cql.append(cql)
                return super().__call__(token, cql, start, end, timeout)

        ps._search = KrStub(total=3, per_call=3)
        seen, collected = set(), []
        n = ps._collect_kr("stub-token", today, seen, collected)
        check(bool(seen_cql) and all('pn any "KR"' in q for q in seen_cql),
              "모든 질의가 KR 공개로 한정된다")
        kr_names = {a["name"] for a in cfg.APPLICANTS if a["region"] == "KR"}
        foreign = {a["name"] for a in cfg.APPLICANTS if a["region"] != "KR"}
        kr_terms = [a["q"] for a in cfg.APPLICANTS if a["region"] == "KR"]
        kr_terms = [t for q in kr_terms for t in ([q] if isinstance(q, str) else q)]
        check(not any(f'pa="{t}"' in q for q in seen_cql for t in kr_terms),
              f"국내 출원인({len(kr_names)}곳)에게는 질의하지 않는다")
        check(n > 0 and len(collected) == n, f"국내 공개를 목록에 더한다 ({n}건)")
        check(all(i.get("applicant") in foreign for i in collected),
              "더해진 항목은 전부 해외 출원인 것이다")
        per_ap: dict[str, int] = {}
        for i in collected:
            per_ap[i["applicant"]] = per_ap.get(i["applicant"], 0) + 1
        check(max(per_ap.values()) <= cfg.KR_LIMIT,
              f"출원인당 상한({cfg.KR_LIMIT})을 넘지 않는다")

        print("· 주간 수집 시작점 회전 (뒤쪽 출원인이 영구히 굶지 않는다)")
        n = len(cfg.APPLICANTS)
        starts, weeks = set(), [datetime(2026, 1, 5) + timedelta(weeks=w)
                                for w in range(12)]
        ok_seq = True
        for d in weeks:
            o = ps._collect_order(d)
            check_len = len(o) == n and {a["name"] for a in o} == \
                {a["name"] for a in cfg.APPLICANTS}
            if not check_len:
                ok_seq = False
                break
            starts.add(o[0]["name"])
            # seq 로 묶인 항목은 앞 항목보다 뒤에 있어야 한다
            pos = {a["name"]: i for i, a in enumerate(o)}
            for i, a in enumerate(cfg.APPLICANTS):
                if a.get("seq") and i > 0:
                    if pos[a["name"]] < pos[cfg.APPLICANTS[i - 1]["name"]]:
                        ok_seq = False
        check(check_len, "회전해도 출원인이 빠지거나 늘지 않는다")
        check(len(starts) >= 3, f"주마다 시작점이 바뀐다 (12주에 {len(starts)}가지)")
        check(ok_seq, "dedup 우선순위 묶음(seq)이 회전에 끊기지 않는다")
        # 한 실행이 소화하는 만큼(COLLECT_ROTATE) 씩 밀리면 몇 주 안에 전원이 선두권에 든다
        covered, span = set(), max(1, cfg.COLLECT_ROTATE)
        for d in weeks[:4]:
            covered |= {a["name"] for a in ps._collect_order(d)[:span]}
        check(len(covered) >= min(n, span * 3),
              f"4주 안에 {len(covered)}/{n}곳이 앞 {span}순위 안에 든다")

        print("· OPS 오류 사유 노출 (401 이 왜 났는지 로그에 남는다)")
        import io as _io
        import urllib.error as _ue

        class _Err(_ue.HTTPError):
            def __init__(self, code, body):
                super().__init__("u", code, "x", {}, _io.BytesIO(body.encode()))

        # 이 파일은 앞에서 _get_token·_search 를 스텁으로 바꿔 놓았다 → 진짜 함수를
        # 잠시 되돌려 놓고 검사한다(안 그러면 스텁을 시험하는 꼴이 된다).
        _orig_open = ps.urllib.request.urlopen
        _stub_search, _stub_token = ps._search, ps._get_token
        ps._search, ps._get_token = orig[0], orig[1]
        try:
            ps.urllib.request.urlopen = lambda *a, **k: (_ for _ in ()).throw(
                _Err(401, "<error><code>CLIENT.InvalidCredentials</code></error>"))
            try:
                ps._get_token()
                msg = ""
            except Exception as e:
                msg = str(e)
            check("CLIENT.InvalidCredentials" in msg,
                  "토큰 실패 시 EPO 가 준 사유가 메시지에 담긴다")
            ps.urllib.request.urlopen = lambda *a, **k: (_ for _ in ()).throw(
                _Err(403, "<fault><code>SERVER.QuotaPerHour</code></fault>"))
            try:
                ps._search("t", "q", 1, 25)
                msg2 = ""
            except Exception as e:
                msg2 = str(e)
                # 사유를 붙이면서 '403' 을 잃으면 쿼터 판정이 깨져 계속 두드리게 된다.
                check(ps._is_quota(e), "사유를 붙여도 쿼터(403) 판정이 유지된다")
            check("QuotaPerHour" in msg2, "검색 실패 사유도 메시지에 담긴다")
        finally:
            ps.urllib.request.urlopen = _orig_open
            ps._search, ps._get_token = _stub_search, _stub_token

        print("· 출원인 질의어 겹침 (총계 부풀림 방지)")
        # 목록은 공개번호 dedup 이 막아 주지만, 총계(stats)는 출원인별 독립 질의라
        # 막을 방법이 없다. 한 출원인의 검색 어구가 다른 출원인의 이름·어구에 통째로
        # 들어 있으면 그 회사 문서까지 세어 총계가 부풀려진다 — 실측으로 q="Siemens"
        # 가 Siemens Energy(53)·Gamesa(75) 를 삼켜 183 이 나왔다. 분야별 경쟁 구도의
        # 지분이 그만큼 틀어지므로 설정 단계에서 막는다.
        def _qs(a):
            q = a["q"]
            return [q] if isinstance(q, str) else list(q)

        overlaps = []
        for a in cfg.APPLICANTS:
            for b in cfg.APPLICANTS:
                if a is b:
                    continue
                for x in _qs(a):
                    for y in _qs(b) + [b["name"]]:
                        if x.lower() != y.lower() and x.lower() in y.lower():
                            overlaps.append(f'{a["name"]} q="{x}" ⊂ {b["name"]}("{y}")')
        for o in overlaps:
            print("    " + o)
        check(not overlaps,
              f"어떤 질의어도 다른 출원인을 삼키지 않는다 ({len(cfg.APPLICANTS)}곳)")

        print("· collect_offices (매일 공개국 집계 경로)")
        ps._search = Stub(total=5, per_call=1)
        st = ps.collect_offices(today)
        check(bool(st.get("totals")), f"총계 갱신 ({len(st.get('totals', {}))}곳)")
        check(bool(st.get("offices")), f"공개국 집계 갱신 ({len(st.get('offices', {}))}곳)")
        check(len(st.get("totals", {})) <= cfg.OFFICE_BATCH,
              f"하루 배치 상한({cfg.OFFICE_BATCH})을 넘지 않는다")

        print("· 집계 병합 (부분 실행이 기존 값을 지우지 않는다)")
        import patent_archive as pa
        store = {"totals": {"기존": 1}, "offices": {"기존": {"KR": 1}}, "updated": {}}
        pa.merge_stats(store, st, "2026-07-27")
        check(store["totals"].get("기존") == 1, "이전 출원인 값이 남는다")
        check(len(store["totals"]) > 1, "새 값이 더해진다")

        print("· 뉴스 아카이브 중복 판정·MOCK 표시")
        import news_archive as na
        # URL 동일 판정: 제목이 완전히 달라도 같은 기사면 다시 담기지 않아야 한다.
        days = {"2026-07-28": {"date": "2026-07-28", "articles": [
            {"title": "제목 A", "url": "https://ex.com/same"}]}}
        _, n = na.merge_today(days, "2026-07-29", [
            {"title": "완전히 딴판인 제목 B 입니다", "url": "https://ex.com/same"}], False)
        check(n == 0, "같은 URL 기사는 제목이 달라도 다시 담지 않는다")
        _, n = na.merge_today(days, "2026-07-29", [
            {"title": "아주 새로운 기사 제목 하나", "url": "https://ex.com/new"}], False)
        check(n == 1, "새 기사는 정상적으로 담긴다")
        # 하루 안에 라이브와 MOCK 이 섞여도 실데이터에 샘플 표시가 붙으면 안 된다.
        d2 = {}
        na.merge_today(d2, "2026-07-29", [{"title": "실데이터 기사", "url": "u1"}], False)
        na.merge_today(d2, "2026-07-29", [{"title": "샘플 기사", "url": "u2"}], True)
        arts = d2["2026-07-29"]["articles"]
        check(not arts[0].get("mock") and arts[1].get("mock") is True,
              "MOCK 표시는 항목별로 남는다")

        print("· 링크 스킴 제한 (javascript: 차단)")
        import re as _re
        import site_render as sr
        hrefs = _re.findall(r"href=\"'\+esc\((\w+)\(", sr._JS)
        raw = _re.findall(r"href=\"'\+esc\((?!safeUrl)", sr._JS)
        check("const safeUrl" in sr._JS, "safeUrl 헬퍼가 있다")
        check(bool(hrefs) and not raw,
              f"모든 링크 href 가 safeUrl 을 거친다 ({len(hrefs)}곳)")

        print("· 분야별 국내 공급자 (site_render 안의 JS)")
        _supplier_checks(sr)

        _docno_checks(sr)

        print("· 분야 지도 (축·등급·이름표 배치)")
        _quad_checks(sr)

        print("· 세부 기술 쏠림 (분모·대표 코드·이름표)")
        _subs_checks(sr)

        print("· 지연 로딩 (목록 분할)")
        _lazy_checks(sr)

        print("· 탭 아이콘(파비콘)")
        _favicon_checks(sr)

        print("· 거래·지원 안내 데이터 (사람이 관리하는 상수)")
        import ip_guide as ig
        ln = ig.links()
        check(bool(ln) and all(l.get("label") and l.get("url") for l in ln),
              f"카드 링크에 이름·URL 이 다 있다 ({len(ln)}종)")
        check(all("{n}" in l["url"] for l in ln),
              "카드 링크 URL 에 공개번호 자리({n})가 있다")
        allitems = [i for g in ig.GUIDE for i in g["items"]]
        shown = [i for g in ig.guide() for i in g["items"]]
        check(bool(ig.guide()) and bool(shown),
              f"안내 항목이 있다 ({len(ig.guide())}묶음 {len(shown)}곳 표시"
              f" · 주소 미확인 {len(ig.pending())}곳)")
        check(all(i.get("name") and i.get("org") and i.get("what") for i in allitems),
              "뼈대 항목도 이름·기관·설명은 다 갖춘다")
        # 주소를 못 채운 항목이 화면으로 새면 안 된다 — 확인 안 된 링크를 기관
        # 사이트에 올리지 않으려고 url 유무로 거르는 구조다.
        check(all(i.get("url") for i in shown), "표시되는 항목은 전부 주소가 있다")
        check(len(shown) + len(ig.pending()) == len(allitems),
              "표시 + 미확인 = 전체 (조용히 사라지는 항목이 없다)")
        urls = [l["url"] for l in ln] + [i["url"] for i in shown]
        check(all(u.startswith("https://") for u in urls),
              f"모든 링크가 https 다 ({len(urls)}개)")
    finally:
        ps._search, ps._get_token = orig[0], orig[1]
        cfg.OPS_KEY, cfg.OPS_SECRET, cfg.REQUEST_DELAY = orig[2], orig[3], orig[4]

    _kipris_checks()
    _foreign_checks()
    _origin_checks()
    _news_outage_checks()
    _site_url_checks()

    print(f"\n{'실패 ' + str(len(FAILS)) + '건' if FAILS else '전부 통과'}")
    return 1 if FAILS else 0


if __name__ == "__main__":
    sys.exit(main())
