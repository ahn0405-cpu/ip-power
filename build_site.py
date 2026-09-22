"""전력 이슈 아카이브 — 사이트 빌드 (뉴스 + 특허).

수집기는 유형별로 분리돼 있고, 어느 쪽을 돌리든 항상 두 데이터(뉴스·특허)를
모두 불러와 다시 저장하고 전체 사이트를 재생성한다 → 어떤 워크플로가 돌아도
다른 탭의 데이터가 유실되지 않는다.

사용법:
  python build_site.py                 # (로컬) 뉴스+특허 둘 다 수집 후 빌드
  python build_site.py --collect news  # 뉴스 + 특허 공개국 집계 일부 (매일 워크플로)
  python build_site.py --collect patents  # 특허 목록만 수집 (매주 워크플로)
  python build_site.py --collect offices  # 특허 공개국 집계만 (수동 보충)
  python build_site.py --collect origins  # 해외 출원인 국적만 (수동 보충)
  python build_site.py --collect none  # 수집 없이 기존 데이터로 재빌드만

환경변수:
  NEWS_SITE_DIR (기본 site) / NEWS_PREV_DIR (이전 아카이브, Actions=gh-pages 체크아웃)
  NEWS_MOCK / PATENT_MOCK : auto | on | off
"""
from __future__ import annotations

import gzip
import json
import os
import sys
from datetime import datetime
from pathlib import Path

import news_config as ncfg
import news_source
import news_archive
import patent_archive
import patent_source
import brief_archive
import ip_guide
import kipris_rate
import site_render

# 특허 수집 백엔드를 고른다. 두 모듈이 같은 계약(collect / collect_offices)을
# 지키므로 site_render 는 어느 쪽이 돌았는지 알 필요가 없다.
#   kipris — KIPRISplus 국내 공보 (기본)
#   ops    — EPO OPS. 8/09·8/16·8/23 세 주 연속 401(Client credentials are
#            invalid)로 실패했고 특허 데이터가 8/03 에서 멈췄다. 자격이 되살아나면
#            PATENT_BACKEND=ops 로 되돌릴 수 있게 남겨 둔다.
if os.getenv("PATENT_BACKEND", "kipris").lower() == "ops":
    patent_backend = patent_source
else:
    import patent_source_kipris as patent_backend


def _load_brief(name: str = "brief.json") -> dict | None:
    """저장소에 커밋된 서술형 브리핑을 읽는다(뉴스=brief.json 매일, 특허=patent_brief.json
    매주). 반자동(사람이 갱신·커밋)이라 수집·빌드 자동화와 분리돼 있고, 파일이 없거나
    깨지면 조용히 건너뛴다(수집·렌더는 정상)."""
    f = Path(__file__).resolve().parent / name
    if not f.exists():
        return None
    try:
        obj = json.loads(f.read_text(encoding="utf-8"))
        return obj or None
    except Exception as e:
        print(f"[경고] {name} 읽기 실패(무시): {e}")
        return None


def _collect_arg() -> str:
    argv = sys.argv[1:]
    if "--collect" in argv:
        i = argv.index("--collect")
        if i + 1 < len(argv):
            return argv[i + 1].lower()
    return "both"


def main() -> None:
    what = _collect_arg()
    now = datetime.now(site_render.KST)
    today = now.strftime("%Y-%m-%d")
    generated = now.strftime("%Y-%m-%d %H:%M KST")

    source_dir = ncfg.PREV_DIR or ncfg.SITE_DIR
    news_days = news_archive.load_days(source_dir)
    patent_weeks = patent_archive.load_weeks(source_dir)
    # 출원인 집계(총계·공개국별)는 주별 버킷과 분리해 누적한다 → 매일 조금씩 갱신 가능.
    pstats_store = patent_archive.load_stats(source_dir)
    seeded = patent_archive.seed_stats(pstats_store, patent_weeks)
    if seeded:
        print(f"  (예전 주별 버킷의 집계 {seeded}곳을 stats.json 으로 이전)")

    # ── 특허 아카이브 초기화 (일회성, 명시적) ──
    # 분야 체계를 CPC Y04S 로 갈아엎으면 옛 아카이브를 이어 쓸 수 없다. 옛것은
    # 우리가 손으로 정한 여덟 분야에 맞춘 IPC 로 모은 것이라 Y04S 갈래별 커버리지가
    # **고르지 않다** — H02J15·G01R31·H04B3/54 는 아예 조회한 적이 없다. 고르지 않은
    # 옛것과 고른 새것을 섞으면 분야끼리 비교가 방향성 있게 왜곡된다(부분집합으로
    # 세면 소수가 다 가진 것처럼 보이던 것과 같은 종류의 오차다).
    #
    # 되돌릴 수 없다. gh-pages 는 매 실행 단일 커밋으로 강제 푸시라 이력이 없고,
    # 남는 사본은 Actions 아티팩트(14일)뿐이다 → 환경변수로 **한 번만** 켠다.
    # 뉴스는 건드리지 않는다(분야 체계와 무관하다).
    if os.getenv("PATENT_RESET", "").strip().lower() in ("1", "on", "true", "yes"):
        print(f"  ⚠️ 특허 아카이브 초기화: {len(patent_weeks)}주 "
              f"{sum(len(w.get('patents', [])) for w in patent_weeks.values()):,}건 "
              f"· 집계 {len(pstats_store.get('totals', {})):,}곳을 버리고 새로 시작합니다 "
              f"(PATENT_RESET). 뉴스는 그대로 둡니다.")
        patent_weeks = {}
        pstats_store = {"totals": {}, "offices": {}, "updated": {},
                        "origins": {}, "originTry": {}}
    print(f"기존 아카이브: 뉴스 {len(news_days)}일 · 특허 {len(patent_weeks)}주 "
          f"· 집계 {len(pstats_store.get('totals', {}))}곳 ({source_dir})")

    # ── 뉴스 수집 ──
    if what in ("news", "both"):
        print(f"{'[MOCK] ' if ncfg.is_mock() else ''}뉴스 수집 → {today}")
        # 오늘 수집이 통째로 막혀도 **사이트는 짓는다**. 아카이브(수십 일치)가
        # 그대로 있는데 빌드를 죽이면, 수집과 무관한 변경(문구 수정 같은 것)까지
        # 소스가 잠깐 흔들렸다는 이유로 배포가 막힌다 — 실제로 2026-09-04 에
        # 구글 뉴스가 11개 카테고리 전부 503 을 내면서 그렇게 됐다.
        #
        # 다만 두 가지를 지킨다:
        #  · MOCK 으로 메우지 않는다. NEWS_MOCK=off 는 '샘플을 아카이브에 섞지
        #    말라' 는 뜻이고, 그 뜻은 실패했을 때야말로 지켜야 한다.
        #  · merge_today 를 부르지 않는다. 빈 목록으로 부르면 '기사 0건인 날' 이
        #    아카이브에 생겨 30일 추이가 오염된다(그날 뉴스가 없었던 것처럼 보인다).
        # 그래서 그냥 건너뛴다 — 화면의 '최근 수집일' 이 어제로 남아 사실을 말한다.
        try:
            fresh, mock = news_source.collect(now)
        except news_source.CollectFailed as e:
            print(f"⚠️ 뉴스 수집 실패 — 기존 아카이브로 사이트를 짓습니다: {e}")
        else:
            _, added = news_archive.merge_today(news_days, today, fresh, mock)
            print(f"  뉴스 신규 {added}건 (수집 {len(fresh)}{' MOCK' if mock else ''})")

    # ── 특허 수집 (주 단위) ──
    if what in ("patents", "both"):
        wk = patent_archive.week_start(now)
        print(f"{'[MOCK] ' if patent_backend.cfg.is_mock() else ''}특허 수집 "
              f"→ {wk} 주 · {patent_backend.__name__}")
        pfresh, pmock, pstats = patent_backend.collect(now)
        _, padded = patent_archive.merge_week(patent_weeks, wk, pfresh, pmock)
        n = patent_archive.merge_stats(pstats_store, pstats, today)
        print(f"  특허 신규 {padded}건 (수집 {len(pfresh)}{' MOCK' if pmock else ''})"
              f" · 집계 갱신 {n}곳")

    # ── 출원인×공개 특허청 정확 집계 (매일 일부만 회전) ──
    # 전 출원인을 한 번에 돌리면 OPS 쿼터에 걸리므로, 매일 도는 뉴스 실행에 얹어
    # 날짜 기준으로 일부만 갱신한다(PATENT_OFFICE_BATCH, 65곳 ≈ 6일 한 바퀴). 결과는 병합만 하고 덮어쓰지 않는다.
    if what in ("news", "offices", "both"):
        n = patent_archive.merge_stats(pstats_store, patent_backend.collect_offices(now), today)
        if n:
            print(f"  공개국 집계 갱신 {n}곳 "
                  f"(누적 {len(pstats_store.get('offices', {}))}곳)")

    # ── 해외 출원인 국적 보강 (매일 일부만) ──
    # 해외 검색 응답에는 출원인 국적이 없어 4천여 곳이 '미상'으로 남는다. 서지상세에는
    # 있지만 건당 1요청이라 한 번에 다 못 받는다 → 출원인당 1회, 하루 상한만큼,
    # stats 에 누적. 공개국 집계와 같은 자리에서 같은 방식으로 돈다.
    if what in ("news", "origins", "offices", "both"):
        import patent_origin
        got, fail = patent_origin.collect(patent_weeks, pstats_store)
        if got or fail:
            patent_archive.merge_stats(
                pstats_store, {"origins": got, "originTry": fail}, today)
            print(f"    누적 확인 {len(pstats_store.get('origins', {})):,}곳")

    # ── 저장 + 전체 사이트 재생성 ──
    news_archive.save(ncfg.SITE_DIR, news_days, generated)
    patent_archive.save(ncfg.SITE_DIR, patent_weeks, generated)
    patent_archive.save_stats(ncfg.SITE_DIR, pstats_store)

    # 서술형 브리핑: 이전 아카이브 로드 → 현재 brief.json 병합(날짜별 누적) → 저장.
    briefs = brief_archive.load_briefs(source_dir)
    current = _load_brief()
    brief_archive.merge(briefs, current)
    brief_archive.save(ncfg.SITE_DIR, briefs)
    brief_list = brief_archive.sorted_list(briefs)
    if brief_list:
        print(f"  브리핑: {len(brief_list)}개 (최신 {brief_list[0].get('date','?')})")

    # 특허 브리핑(주 1회): 같은 방식이되 키가 수집 주(week)다.
    pbriefs = brief_archive.load_briefs(source_dir, brief_archive.PATENT_BRIEF_SUBDIR, "week")
    brief_archive.merge(pbriefs, _load_brief("patent_brief.json"), "week")
    brief_archive.save(ncfg.SITE_DIR, pbriefs, brief_archive.PATENT_BRIEF_SUBDIR)
    pbrief_list = brief_archive.sorted_list(pbriefs)
    if pbrief_list:
        print(f"  특허 브리핑: {len(pbrief_list)}개 (최신 {pbrief_list[0].get('week','?')})")

    # 거래·지원 안내에서 주소를 아직 못 채운 항목은 화면에 나오지 않는다 → 로그로
    # 남기지 않으면 뼈대만 만들어 두고 잊게 된다.
    todo = ip_guide.pending()
    if todo:
        print(f"  거래·지원 안내: 주소 미확인 {len(todo)}곳(미표시) — " + ", ".join(todo))

    # 국유판매기술: 국내에서 받아 커밋한 파일(자동 수집 불가 — apis.data.go.kr 이
    # 해외 IP 를 막는다). 없으면 조용히 건너뛴다.
    staown = ip_guide.staown_power(_load_brief("staown.json"))
    if staown:
        print(f"  국유판매기술: 무상 {len(staown['free'])}건 · "
              f"유상 {len(staown['pay'])}건 (수집 {staown.get('generated','?')})")

    index = site_render.render_all(ncfg.SITE_DIR, news_days, patent_weeks,
                                   generated, briefs=brief_list, stats=pstats_store,
                                   pbriefs=pbrief_list, staown=staown)

    nt = sum(len(d.get("articles", [])) for d in news_days.values())
    pt = sum(len(w.get("patents", [])) for w in patent_weeks.values())
    print(f"\n완료 → {index}")
    print(f"       뉴스 {len(news_days)}일/{nt}건 · 특허 {len(patent_weeks)}주/{pt}건")
    # 차단기가 내려갔으면 끝에서 한 번 더 크게 말한다. 수집 중의 경고는 수천 줄
    # 로그에 묻히고, 빌드는 성공으로 끝나 아무도 모른다 — 9/14·9/21 이 그랬다.
    _kipris_note = kipris_rate.summary()
    if _kipris_note:
        print(f"       {_kipris_note}")
        print("       ↑ 이번 특허 수집은 불완전합니다. KIPRIS 상태를 확인하세요.")
    _report_payload(index)


# 전체 아카이브를 index.html 에 인라인하는 구조라 페이지 용량이 계속 자란다.
# 항목별 필드를 다듬는 최적화는 실측상 의미가 없었다 — 중복 필드(특허 url·country·
# aFlag·week)를 다 걷어내도 gzip 기준 3.6% 뿐이다. 반복이 많은 데이터는 압축이 이미
# 처리한다. 실제로 관리해야 하는 건 총량이라, 매 빌드마다 압축 전송량을 찍고 임계를
# 넘으면 경고한다. 넘어가면 최근 구간만 인라인하고 과거분은 따로 받는 구조를 볼 시점이다.
PAYLOAD_WARN_KB = int(os.getenv("NEWS_PAYLOAD_WARN_KB", "700"))


def _report_payload(index_path) -> None:
    try:
        raw = Path(index_path).read_bytes()
    except Exception:
        return
    gz = len(gzip.compress(raw, 6))
    print(f"       페이지 {len(raw)/1024:,.0f} KB · 압축 전송 약 {gz/1024:,.0f} KB")
    if gz / 1024 > PAYLOAD_WARN_KB:
        # 분리 로딩은 이미 들어가 있다(site_render.INLINE_*). 그런데도 넘었다면
        # 남은 원인은 둘 중 하나다 → 아무거나 줄이라고 하면 엉뚱한 데를 깎는다.
        print(f"       ⚠️ 압축 전송량이 기준({PAYLOAD_WARN_KB} KB)을 넘었습니다 — "
              "분리 로딩은 이미 켜져 있으니 (1) NEWS_INLINE_ALL 로 빌드했는지, "
              "(2) 인라인 분량(NEWS_INLINE_NEWS·NEWS_INLINE_PATENTS)이 커졌는지 "
              "보세요.")


if __name__ == "__main__":
    main()
