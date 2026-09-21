"""전력 이슈 특허 아카이브 — 설정 (출원인 × 분야).

수집 축: 큐레이션한 주요 출원인 × 전력 CPC.
표시 축: 출원인을 **국적(미국·한국·중국·일본·유럽)별로 묶어** 매트릭스를 나눠 본다
        → "각 나라 주요 기업이 어느 분야에 무엇을 출원하나".
        ※ 이 그룹축은 '출원인 국적'이며, 특허가 실제로 공개된 특허청(office)과는
          다르다(한 기업이 US·EP·WO 등 여러 곳에 공개). office 는 카드에 표시한다.

수집은 EPO OPS 공식 API 를 쓴다(프로브로 실측 확인):
  - pa="<출원인>" and pd within "<기간>" and (cpc=...) → 출원인당 1~2요청.
  - 응답에 CPC 가 있어 분야를 코드로 분류한다(제목 키워드 추정보다 정확).
  - 중/일/유럽 특허도 영문 서지로 정규화돼 돌아온다.

경로/모드는 news_config 를 재사용해 뉴스와 같은 사이트에 함께 배포된다.
"""
from __future__ import annotations

import os
import re

import news_config as ncfg

# 사이트 안에서 특허 원자료(JSON)가 담기는 하위 폴더
PATENT_DATA_SUBDIR = "data/patents"

# ── 지역(그룹) 정의 — 매트릭스를 이 순서로 나눠 표시 ──────────────
REGIONS = ["US", "KR", "CN", "JP", "EU"]
REGION_LABEL = {
    "US": ("🇺🇸", "미국"), "KR": ("🇰🇷", "한국"), "CN": ("🇨🇳", "중국"),
    "JP": ("🇯🇵", "일본"), "EU": ("🇪🇺", "유럽"),
}
# (구)코드 호환: site_render 가 참조하던 이름 유지
COUNTRIES = REGIONS
COUNTRY_LABEL = REGION_LABEL

# 서지상세가 주는 applicantCountry 는 실제 나라 코드(DE·DK·TW·CA…)다. 화면의
# 국적 축은 다섯 갈래(미국·한국·중국·일본·유럽)로 잡혀 있고, 큐레이션 별칭도
# 이미 그렇게 쓰고 있었다 — Vestas 는 국기 🇩🇰 에 지역 EU 다. 같은 규칙으로 접는다.
# 유럽 밖의 나라(대만·캐나다·인도…)는 접지 않고 그 코드 그대로 둔다. 다섯 갈래에
# 없다고 '미상'으로 만들면 아는 것을 모른다고 하는 셈이다.
EUROPE = {
    "AT", "BE", "BG", "CH", "CY", "CZ", "DE", "DK", "EE", "ES", "FI", "FR",
    "GB", "GR", "HR", "HU", "IE", "IS", "IT", "LI", "LT", "LU", "LV", "MC",
    "MK", "MT", "NL", "NO", "PL", "PT", "RO", "RS", "SE", "SI", "SK", "SM",
    "TR", "UA",
}


def region_of(cc: str) -> str:
    """나라 코드 → 화면의 국적 축 값. 빈 값은 빈 값 그대로(모르는 것은 모른다)."""
    cc = (cc or "").strip().upper()
    if not cc:
        return ""
    if cc in REGION_LABEL:
        return cc
    return "EU" if cc in EUROPE else cc


def flag_of(cc: str) -> str:
    """나라 코드 → 국기 이모지. 표에 없는 나라도 지역표시자로 만들어 준다.

    화면의 flg() 가 지역표시자 쌍을 받으면 우리가 그린 국기로 바꾸고, 안 그린
    나라는 두 글자 배지로 보인다 — 그러라고 만든 장치라 여기서 표를 늘리지 않는다.
    """
    cc = (cc or "").strip().upper()
    if cc in REGION_LABEL:
        return REGION_LABEL[cc][0]
    if len(cc) == 2 and cc.isalpha():
        return "".join(chr(0x1F1E6 + ord(c) - 65) for c in cc)
    return ""

# ── 공개 특허청(시장) ─────────────────────────────────────────────
# "어느 시장에 출원했나" 축. 출원인 국적(REGIONS)과는 다른 개념이며, 같은 발명이
# 여러 특허청에 공개되므로 특허청별 합계는 출원인 총계를 넘을 수 있다.
# OPS CQL: pn any "<코드>" (프로브로 실측 확인)
OFFICES = [
    {"code": "US", "emoji": "🇺🇸", "name": "미국"},
    {"code": "KR", "emoji": "🇰🇷", "name": "한국"},
    {"code": "CN", "emoji": "🇨🇳", "name": "중국"},
    {"code": "JP", "emoji": "🇯🇵", "name": "일본"},
    {"code": "EP", "emoji": "🇪🇺", "name": "유럽(EPO)"},
    {"code": "WO", "emoji": "🌐", "name": "PCT 국제"},
]

# ── 수집(EPO OPS) ────────────────────────────────────────────────
# 출원인마다 '최근 N일 발행 + 전력 CPC' 특허를 한 번에 받아 CPC로 분야를 분류한다.
# (무키 시절의 출원인×분야 개별 조회가 아니라 출원인당 1~2요청 → 회전 불필요)
OPS_KEY = os.getenv("OPS_KEY", "")
OPS_SECRET = os.getenv("OPS_SECRET", "")
# 출원인당 최대 수집. 50 → 30 으로 낮췄다. 실측(8/3) 상 쿼터에 걸리기 전까지
# 24곳밖에 못 돌았는데, 상한이 낮으면 같은 쿼터로 더 많은 출원인에 닿는다
# (24곳 × 50 ≈ 40곳 × 30). 표본이 작아지면 분야 구성비 추정이 거칠어지지만,
# 규모는 표본이 아니라 OPS 총계에서 오므로(경쟁 구도 지표) 영향이 제한적이다.
PER_APPLICANT_LIMIT = int(os.getenv("PATENT_PER_APPLICANT", "30"))
LOOKBACK_DAYS = int(os.getenv("PATENT_LOOKBACK_DAYS", "90"))        # 최근 N일 발행분
REQUEST_TIMEOUT = int(os.getenv("PATENT_TIMEOUT", "40"))
REQUEST_DELAY = float(os.getenv("PATENT_REQ_DELAY", "0.4"))         # OPS 쿼터 배려
MOCK_MODE = os.getenv("PATENT_MOCK", ncfg.MOCK_MODE)                # auto | on | off
# 주간 수집의 시작점 회전 폭(출원인 수). 목록을 늘 같은 순서로 돌면 쿼터(403)가
# 나는 지점이 매주 같아 뒤쪽이 영구히 굶는다 — 실측: 8/3 실행은 39번(중국 끝)에서
# 끊겨 일본·유럽 25곳이 통째로 빠졌고, 세 주 내내 같은 자리에서 끊겼다. 한 실행이
# 소화하는 양(24~28곳)만큼 매주 시작점을 옮기면 3주에 한 바퀴가 돈다.
COLLECT_ROTATE = int(os.getenv("PATENT_COLLECT_ROTATE", "24"))
# 출원인×특허청 정확 집계(공개국별 랭킹). 전 출원인을 한 번에 돌리면 OPS 무료 쿼터를
# 넘어 403 이 나고 본 수집까지 망친다(실측) → **매일 뉴스 실행에 얹어** 일부만 돌리고
# data/patents/stats.json 에 병합해 누적한다. 8곳/일이면 31곳을 4일에 한 바퀴 돈다
# (요청량 = 8 × (1 총계 + 6 특허청) = 56/일).
OFFICE_COUNTS = os.getenv("PATENT_OFFICE_COUNTS", "on") != "off"
OFFICE_BATCH = int(os.getenv("PATENT_OFFICE_BATCH", "11"))  # 한 실행에서 처리할 출원인 수
# (출원인이 40→65곳으로 늘며 8곳/일이면 한 바퀴에 8일이 걸린다. 11곳/일이면 6일.)
# 집계는 '있으면 좋은' 부가 정보다. OPS 가 느려도 매일 도는 뉴스 배포를 붙잡으면 안 되므로
# 전체 소요에 상한을 두고, 넘으면 그 자리에서 접고 다음 날 이어서 채운다.
OFFICE_BUDGET = float(os.getenv("PATENT_OFFICE_BUDGET", "180"))   # 초
OFFICE_TIMEOUT = int(os.getenv("PATENT_OFFICE_TIMEOUT", "15"))    # 집계 요청 1건 타임아웃(초)
# 해외 출원인의 '국내(KR) 공개' 전용 수집.
# 본 수집은 출원인당 상한(PER_APPLICANT_LIMIT) 안에서 최신순이라 KR 공개가 뒤로 밀려난다 — 실측으로
# CATL 은 집계상 KR 60건인데 표본에는 0건이었다. 해외 출원인이 한국에 낸 건 그들이
# 한국 시장에서 지킬 값어치가 있다고 본 기술이라 따로 한 번 더 훑는다(pn any "KR").
KR_FOCUS = os.getenv("PATENT_KR_FOCUS", "on") != "off"
KR_LIMIT = int(os.getenv("PATENT_KR_LIMIT", "15"))          # 해외 출원인당 최대 수집
KR_BUDGET = float(os.getenv("PATENT_KR_BUDGET", "240"))     # 초, 넘으면 접고 다음 주에

# ── 주요 출원인(큐레이션) ────────────────────────────────────────
# name: 표시명 / region: 그룹(미국·한국·중국·일본·유럽) / flag: 행 국기(국적)
# q   : OPS pa= 검색어. 문자열 하나 또는 여러 표기의 목록(목록이면 OR 로 묶어 조회).
# 편집 가능: 지역별로 자유롭게 추가/삭제(OPS 는 출원인당 1~2요청이라 여유 있음).
# ※ ✔ 는 실수집으로 결과가 확인된 곳.
# ※ 2026-07-27 프로브로 전 출원인 검색어를 실측 확인했다. Dynapower·산일전기·제룡전기는
#   '검색어가 틀린 것이 아니라'(각각 45·56·56건이 색인에 있음) 최근 90일 전력 CPC 공개가
#   0건이라 안 잡힌다 → 그대로 두고, 공개가 생기면 자동으로 들어온다.
# KIPRIS 는 한글 법인명을 준다('주식회사 엘지에너지솔루션', '도요타 지도샤(주)').
# 영문 별칭과 음절이 달라(엘지 ≠ LG) 정규화로는 못 붙는다 — 첫 실전 수집에서
# 같은 회사가 둘로 갈렸다(LG에너지솔루션 2264 + 주식회사 엘지에너지솔루션 502).
# 그래서 q 에 한글 표기를 함께 적는다(한국수력원자력·산일전기에 이미 쓰던 방식).
APPLICANTS = [
    # 🇺🇸 미국
    {"name": "General Electric", "region": "US", "flag": "🇺🇸", "q": "General Electric"},      # ✔
    {"name": "GE Vernova", "region": "US", "flag": "🇺🇸", "q": "GE Vernova"},                  # ✔
    {"name": "Eaton", "region": "US", "flag": "🇺🇸", "q": "Eaton"},                            # ✔
    {"name": "Caterpillar", "region": "US", "flag": "🇺🇸", "q": "Caterpillar"},
    {"name": "Dynapower", "region": "US", "flag": "🇺🇸", "q": "Dynapower"},   # ✔ 검색어 확인(최근 공개 0)
    {"name": "Westinghouse", "region": "US", "flag": "🇺🇸", "q": "Westinghouse Electric"},
    {"name": "Kairos Power", "region": "US", "flag": "🇺🇸", "q": "Kairos Power"},
    {"name": "NuScale", "region": "US", "flag": "🇺🇸", "q": "NuScale"},
    {"name": "TerraPower", "region": "US", "flag": "🇺🇸", "q": "TerraPower"},
    {"name": "Vertiv", "region": "US", "flag": "🇺🇸", "q": "Vertiv"},   # 데이터센터 전원
    # 🇰🇷 한국
    {"name": "한국전력공사", "region": "KR", "flag": "🇰🇷", "q": "Korea Electric Power"},          # ✔
    {"name": "한국전력기술", "region": "KR", "flag": "🇰🇷", "q": "KEPCO Engineering Construction"},
    # 영문 표기가 "KOREA HYDRO & NUCLEAR POWER" 라 '&' 때문에 어구가 끊길 수 있어
    # 앞부분만 쓰고, 한글 원표기도 함께 묶는다(산일전기 사례 — 한글로만 잡히는 문서가 있다).
    {"name": "한국수력원자력", "region": "KR", "flag": "🇰🇷",
     "q": ["Korea Hydro", "한국수력원자력"]},
    {"name": "HD현대일렉트릭", "region": "KR", "flag": "🇰🇷", "q": "Hyundai Electric"},            # ✔
    {"name": "효성중공업", "region": "KR", "flag": "🇰🇷", "q": "Hyosung Heavy Industries"},       # ✔
    # 2022년 두산중공업 → 두산에너빌리티로 사명 변경. 변경 전 출원이 옛 이름으로 남아 둘 다 조회.
    {"name": "두산에너빌리티", "region": "KR", "flag": "🇰🇷",
     "q": ["Doosan Enerbility", "Doosan Heavy Industries"]},
    {"name": "LS일렉트릭", "region": "KR", "flag": "🇰🇷", "q": "LS Electric"},                    # ✔
    {"name": "삼성전자", "region": "KR", "flag": "🇰🇷",
     "q": ["Samsung Electronics", "삼성전자"]},              # ✔
    {"name": "LG에너지솔루션", "region": "KR", "flag": "🇰🇷",
     "q": ["LG Energy Solution", "엘지에너지솔루션", "LG에너지솔루션"]},
    {"name": "삼성SDI", "region": "KR", "flag": "🇰🇷",
     "q": ["Samsung SDI", "삼성에스디아이", "삼성SDI"]},
    {"name": "SK온", "region": "KR", "flag": "🇰🇷", "q": ["SK On", "에스케이온"]},
    {"name": "일진전기", "region": "KR", "flag": "🇰🇷", "q": "Iljin Electric"},
    {"name": "대한전선", "region": "KR", "flag": "🇰🇷", "q": "Taihan"},
    # 해저·초고압 케이블. 대한전선만 있고 빠져 있었다(정규화 별칭 표에는 등록돼 있었음).
    {"name": "LS전선", "region": "KR", "flag": "🇰🇷", "q": ["LS Cable", "엘에스전선"]},
    {"name": "한화솔루션", "region": "KR", "flag": "🇰🇷", "q": ["Hanwha Solutions", "한화솔루션"]},
    {"name": "씨에스윈드", "region": "KR", "flag": "🇰🇷", "q": ["CS Wind", "씨에스윈드"]},
    {"name": "산일전기", "region": "KR", "flag": "🇰🇷",
     "q": ["Sanil Electric", "산일전기"]},          # ✔ 검색어 확인(최근 공개 0)
    {"name": "제룡전기", "region": "KR", "flag": "🇰🇷",
     "q": ["Jeryong", "제룡전기"]},                 # ✔ 검색어 확인(최근 공개 0)
    {"name": "그리드위즈", "region": "KR", "flag": "🇰🇷", "q": "Gridwiz"},
    # 🇨🇳 중국
    {"name": "State Grid", "region": "CN", "flag": "🇨🇳", "q": "State Grid Corporation of China"},  # ✔
    {"name": "Huawei", "region": "CN", "flag": "🇨🇳", "q": ["Huawei", "화웨이"]},                          # ✔
    {"name": "CATL", "region": "CN", "flag": "🇨🇳",
     "q": ["Contemporary Amperex Technology", "엠퍼렉스"]},   # ✔
    {"name": "CNNC", "region": "CN", "flag": "🇨🇳", "q": "China National Nuclear"},   # 중국핵공업집단
    {"name": "CGN", "region": "CN", "flag": "🇨🇳", "q": "China General Nuclear"},     # 중국광핵집단
    {"name": "Sungrow", "region": "CN", "flag": "🇨🇳", "q": "Sungrow"},               # 인버터·ESS PCS
    {"name": "TBEA", "region": "CN", "flag": "🇨🇳", "q": "TBEA"},                     # 변압기
    {"name": "Goldwind", "region": "CN", "flag": "🇨🇳", "q": "Goldwind"},
    {"name": "Ming Yang", "region": "CN", "flag": "🇨🇳", "q": "Mingyang"},
    {"name": "Envision", "region": "CN", "flag": "🇨🇳", "q": "Envision Energy"},
    {"name": "BYD", "region": "CN", "flag": "🇨🇳", "q": ["BYD", "비와이디"]},
    # 🇯🇵 일본
    {"name": "Hitachi Energy", "region": "JP", "flag": "🇯🇵", "q": "Hitachi Energy"},          # ✔
    # 히타치제작소(Hitachi, Ltd.). 그냥 "Hitachi" 로 두면 위 Hitachi Energy 까지 걸려
    # 총계가 부풀려지므로 어구로 묶어 분리한다(목록 중복은 공개번호 dedup 이 막지만
    # stats 의 총계는 출원인별 독립 질의라 겹치면 그대로 부푼다).
    {"name": "Hitachi", "region": "JP", "flag": "🇯🇵",
     "q": ["Hitachi Ltd", "日立製作所"]},
    {"name": "Mitsubishi Electric", "region": "JP", "flag": "🇯🇵",
     "q": ["Mitsubishi Electric", "미쓰비시덴키", "미쓰비시 전기",
           "三菱電機"]},
    {"name": "Toshiba", "region": "JP", "flag": "🇯🇵",
     "q": ["Toshiba", "도시바", "東芝"]},
    {"name": "Panasonic", "region": "JP", "flag": "🇯🇵",
     "q": ["Panasonic", "파나소닉", "パナソニック"]},
    {"name": "Kyocera", "region": "JP", "flag": "🇯🇵",
     "q": ["Kyocera", "京セラ"]},
    {"name": "Toyota", "region": "JP", "flag": "🇯🇵",
     "q": ["Toyota", "도요타", "トヨタ"]},
    {"name": "Sumitomo Electric", "region": "JP", "flag": "🇯🇵",
     "q": ["Sumitomo Electric", "스미토모", "住友電気"]},
    {"name": "Furukawa Electric", "region": "JP", "flag": "🇯🇵", "q": "Furukawa Electric"},
    {"name": "Fuji Electric", "region": "JP", "flag": "🇯🇵",
     "q": ["Fuji Electric", "富士電機"]},   # 전력반도체·인버터
    {"name": "Meidensha", "region": "JP", "flag": "🇯🇵", "q": "Meidensha"},
    # 🇪🇺 유럽
    # Siemens 계열은 구체적인 쪽을 먼저 둔다. 목록에서 앞선 항목이 공개번호 dedup 으로
    # 먼저 가져가게 하려는 것.
    # 총계는 출원인별 독립 질의라 dedup 이 듣지 않는다 → 넓은 q="Siemens" 로는 Energy·
    # Gamesa 가 그대로 합산됐다(실측: Siemens 183 · Energy 53 · Gamesa 75). 총계가
    # 겹치면 분야별 경쟁 구도의 지분이 부풀려지므로 모회사 법인명으로 좁힌다.
    # 표기가 문서마다 흔들려(AKTIENGESELLSCHAFT / AG) 둘을 OR 로 묶었다.
    {"name": "Siemens Energy", "region": "EU", "flag": "🇩🇪", "q": "Siemens Energy"},
    {"name": "Siemens Gamesa", "region": "EU", "flag": "🇪🇸", "q": "Siemens Gamesa"},
    # seq: '앞 항목보다 뒤에 와야 한다'는 표시. 주간 수집이 시작점을 회전시키므로
    # (_collect_order) 회전이 이 사이를 끊고 들어오지 못하게 막는다.
    {"name": "Siemens", "region": "EU", "flag": "🇩🇪", "seq": True,
     "q": ["Siemens Aktiengesellschaft", "Siemens AG"]},                                      # ✔
    {"name": "ABB", "region": "EU", "flag": "🇨🇭", "q": ["ABB", "에이비비"]},                                # ✔
    {"name": "Schneider Electric", "region": "EU", "flag": "🇫🇷",
     "q": ["Schneider Electric", "슈나이더"]},  # ✔
    {"name": "Bosch", "region": "EU", "flag": "🇩🇪", "q": ["Robert Bosch", "보쉬"]},
    {"name": "Vestas", "region": "EU", "flag": "🇩🇰",
     "q": ["Vestas Wind Systems", "베스타스"]},
    {"name": "Nordex", "region": "EU", "flag": "🇩🇪", "q": "Nordex"},
    {"name": "Prysmian", "region": "EU", "flag": "🇮🇹", "q": "Prysmian"},     # 해저·초고압 케이블
    {"name": "Nexans", "region": "EU", "flag": "🇫🇷", "q": "Nexans"},
    {"name": "NKT", "region": "EU", "flag": "🇩🇰", "q": "NKT"},               # HVDC 케이블
    {"name": "Framatome", "region": "EU", "flag": "🇫🇷", "q": "Framatome"},
    {"name": "Rolls-Royce SMR", "region": "EU", "flag": "🇬🇧", "q": "Rolls-Royce SMR"},
    {"name": "Legrand", "region": "EU", "flag": "🇫🇷", "q": "Legrand"},
]

# ── 분야(기술 카테고리) — CPC 분류 기반 ────────────────────────────
# EPO OPS 는 CPC 를 주므로, 제목 키워드 대신 CPC 로 검색·분류한다(정확도↑).
#   cpc   : OPS CQL 검색에 쓸 CPC 접두(여러 개면 OR)
#   match : 응답 CPC 를 분야로 되분류할 때 쓰는 접두(우선순위는 CATEGORIES 순서)
# 전력 분야 CPC 요약:
#   H02J 전력 공급/분배 계통   H02M 전력 변환   H01L 반도체(전력소자)
#   H01H 개폐기·차단기        H02B 배전반      H01F 변압기
#   G21 원자력                H02S 태양광      H01M 전지(저장)
#   G01R 계측(전력량계)       Y04S 스마트그리드 ICT   Y02E 에너지 감축기술
# ipc: KIPRISplus 국내 공보 검색에 쓸 IPC 접두(여러 개면 분야당 여러 번 조회).
#   실측(8/25): KIPRISplus 국내 항목별검색에 cpcNumber 파라미터가 **없다**
#   (cpcNumber=Y04S → INVALID_REQUEST_PARAMETER). ipcNumber 만 받는다.
#   그래서 CPC 전용 코드는 IPC 로 갈아 끼워야 한다:
#     Y04S(스마트그리드 ICT) → IPC 에 없음. 원격 감시·제어는 H02J13,
#       전력량 계측은 G01R21/G01R22 가 같은 자리를 덮는다.
#     Y02E(에너지 감축) → 애초에 우리 분야 정의에는 쓰지 않았다.
#   나머지 일곱 분야는 CPC 와 IPC 가 같은 코드라 그대로 쓴다.
# ── 분야 = CPC Y04S (스마트그리드) ────────────────────────────────
# 우리가 여덟 분야를 손으로 정해 두었었다. 그러면 '왜 이 여덟인가'를 늘 우리가
# 변호해야 하고, 기관 문서·국제 통계와 대조도 안 된다. 이미 있는 국제 표준을 쓴다.
#
# Y04S = "SYSTEMS INTEGRATING TECHNOLOGIES RELATED TO POWER NETWORK OPERATION,
#         COMMUNICATION OR INFORMATION TECHNOLOGIES FOR IMPROVING THE ELECTRICAL
#         POWER GENERATION, TRANSMISSION, DISTRIBUTION, MANAGEMENT OR USAGE,
#         i.e. SMART GRIDS"  (CPC 2026.08)
# 발전원의 종류가 아니라 **계통에 ICT 를 붙인 것**에 부여된다. 그래서 태양광·풍력
# 발전기 자체(H02S·F03D)도, 배터리 소재(H01M4·H01M50)도 여기 자리가 없다 —
# 의도한 결과다(실측: 재생에너지·저장 8,540건 중 H01M 이 5,290건이고 그 절반이
# 전극·부품이었다. 계통이 아니라 이차전지 산업이다).
#
# 원전만 예외로 남긴다. Y04S 에는 원전 자리가 **없다** — 실측으로 확인했다:
#   첫 IPC 가 G21 인 항목 285건 중 Y 코드를 받은 42건이 전부 Y02E30 이고 Y04S 는 0건.
#   Y04S 를 받은 47건의 정체는 H02J 36 · G06Q 7 · G06F 2 · H02H 1 뿐이다.
# 그래서 원전은 Y04S 가 아니라 **Y02E 30(원자력)** 을 근거로 따로 세운다.
#
# 이름은 옮긴 것이다. 공식 표제(en)를 함께 담아 화면 툴팁에 붙인다 — 옮긴 말이
# 표준을 대체하는 것처럼 보이면 안 된다.
#
# ipc: 수집 질의에 쓸 IPC 접두. **Y04S 로는 모을 수 없다**(실측):
#   국내 항목별검색에 cpcNumber 없음 · 해외는 cpc= 무시(지게차 특허가 온다) ·
#   epc= 는 색인이 비어 0건 · 해외 서지상세 cpcInfo/cpcCd 도 빈값(3건 표본).
#   그래서 IPC 로 모으고, 실제 Y04S 코드가 붙어 있으면 그것을 우선한다(match).
CATEGORIES = [
    # 원전이 **맨 앞이다.** 이 목록은 순서가 곧 우선순위라(먼저 걸리는 분야가
    # 이긴다), 뒤에 두면 원전 특허가 다른 코드에 먼저 걸려 밀려난다 — 실측 7건이
    # 그랬다: '소형모듈형 원자로 방사능 방재'(G21D3/04)가 G06Q50/06 에 걸려
    # 시장·거래로, '원자력 발전소 순환수 펌프'(G21D3/06)가 H02J9 에 걸려
    # 수용가로 갔다. 원전은 따로 관리하기로 한 분야라 그렇게 새면 안 된다.
    # 앞으로 옮겨도 다른 분야는 그 7건 말고 움직이지 않는다(실측으로 확인).
    # Y04S 밖이라는 것은 화면에도 밝힌다(근거는 Y02E 30).
    {"key": "nuclear", "emoji": "☢️", "name": "원전·SMR",
     "en": "Nuclear fission reactors (Y02E 30/30) — outside the Y04S scheme",
     "outside": True,
     "cpc": ["Y02E30", "G21C", "G21D"], "ipc": ["G21C", "G21D"],
     "match": ["Y02E30", "G21"]},
    {"key": "y04s10", "emoji": "⚡", "name": "발전·송배전 지원",
     "en": "Systems supporting electrical power generation, transmission or distribution",
     "cpc": ["Y04S10"],
     # 10/50 계통 운영 · 10/14 저장 · 10/16 변전소 · 10/18 개폐 · 10/20 보호 ·
     # 10/22 무효전력 · 10/30 상태감시 · 10/12 분산전원 감시
     "ipc": ["H02J3", "H02J13", "H02J1", "H02J15", "H02G", "H01F27",
             "H02B", "H01H33", "H02H", "G01R31"],
     "match": ["Y04S10", "H02J3", "H02J13", "H02J1", "H02J15",
               "H02G", "H01F27", "H02B", "H01H33", "H02H", "G01R31"]},
    {"key": "y04s20", "emoji": "🏠", "name": "수용가·배전 말단",
     "en": ("Management or operation of end-user stationary applications or the "
            "last stages of power distribution"),
     "cpc": ["Y04S20"],
     # 20/12 수용가 저장·UPS · 20/30 스마트미터링 · 20/222 수요반응
     # H02J7 을 통째로 넣지 않는다 — 기기 배터리 충전 회로(휴대기기·공구)까지
     # 딸려 온다(실측 118건). 20/12 에 맞는 것은 예비전원(H02J 7/34)과 태양광
     # 연계 충전(H02J 7/35)이다. 코드를 자르지 않게 해 둔 것이 여기서 값을 한다.
     "ipc": ["H02J9", "G01R21", "G01R22", "G01D4", "H02J7/34", "H02J7/35"],
     "match": ["Y04S20", "H02J9", "G01R21", "G01R22", "G01D4",
               "H02J7/34", "H02J7/35"]},
    {"key": "y04s30", "emoji": "🚗", "name": "수송(전기차)",
     "en": "Systems supporting specific end-user applications in the sector of transportation",
     "cpc": ["Y04S30"],
     # 30/00 은 '수송 부문의 **최종 수용가 응용**' 이다 — 차량 자체가 아니라 차량이
     # 계통과 주고받는 자리다. 그래서 B60L 을 통째로 넣지 않고 세 자리만 쓴다:
     #   B60L 53  충전 방법·회로          (30/12 원격·협조 충전)
     #   B60L 55  **차량→계통 역송(V2G)**  (30/10 상호운용의 본진)
     #   B60L 58  배터리 감시·제어         (셋 중 가장 느슨하다 — 넘치면 여기부터 뺀다)
     # B60L 전체를 넣었더니 구동·제동회생까지 들어와 미국·중국에서만 수천 건이 되고,
     # 이 분야만 상한에 걸려 '전수가 아닌 표본' 이 된다. 한 표 안에서 성격이 다른
     # 수치가 섞이는 것이 이 프로젝트가 계속 피해 온 오차다.
     # B60M(전차선)·H02J50(무선전력)도 뺐다 — 전자는 철도 급전이라 수용가 응용이
     # 아니고, 후자는 휴대기기 무선충전까지 걸린다. 되돌리려면 여기에 다시 넣으면 된다.
     "ipc": ["B60L53", "B60L55", "B60L58"],
     "match": ["Y04S30", "B60L53", "B60L55", "B60L58"]},
    {"key": "y04s40", "emoji": "🌐", "name": "통신·정보기술",
     "en": ("Systems for electrical power generation, transmission, distribution or "
            "end-user application management characterised by the use of communication "
            "or information technologies"),
     "cpc": ["Y04S40"],
     # 좁게 잡는다. 이 갈래의 IPC 짝(H04L12 통신망 · H04W4 무선 · G06F 연산)은
     # 범용 대분류라 그대로 조회하면 전력과 무관한 것이 쏟아진다 — 전력선반송
     # (H04B3/54)처럼 전력에 붙박인 코드만 쓰고, 나머지는 실제 Y04S40 코드가
     # 붙어 있을 때만 걸린다. 그래서 이 갈래는 원래 얇다(실측 16건).
     "ipc": ["H04B3/54"],
     "match": ["Y04S40", "H04B3/54"]},
    {"key": "y04s50", "emoji": "💱", "name": "시장·거래",
     "en": ("Market activities related to the operation of systems integrating "
            "technologies related to power network operation or related to "
            "communication or information technologies"),
     "cpc": ["Y04S50"],
     # G06Q50/06 = 전력·가스·수도 사업. G06Q 전체나 G06Q30(상거래)은 범용이라 안 쓴다.
     "ipc": ["G06Q50/06"],
     "match": ["Y04S50", "G06Q50/06"]},
]

# ── KIPRISplus (국내 공보) ────────────────────────────────────────
# 명세서 실측(8/25). OPS 와 결정적으로 다른 점: **출원인 없이 분야+기간만으로**
# 조회된다. OPS 는 pa= 가 필수라 큐레이션한 65곳 안에서만 볼 수 있었는데, 여기서는
# 그 분야에 실제로 출원하는 국내 주체가 전부 나온다(대학 산학협력단·중소기업 포함)
# → 오래 막혀 있던 '공급자 축'이 목록 없이 풀린다.
KIPRIS_KEY = os.getenv("KIPRIS_KEY", "")
KIPRIS_BASE = os.getenv("KIPRIS_BASE", "http://plus.kipris.or.kr/kipo-api/kipi")
KIPRIS_SERVICE = os.getenv("KIPRIS_SERVICE", "patUtiModInfoSearchSevice")
KIPRIS_KEYPARAM = os.getenv("KIPRIS_KEYPARAM", "ServiceKey")
KIPRIS_ROWS = int(os.getenv("KIPRIS_ROWS", "100"))       # 한 요청에 받을 건수
# IPC 접두당 상한. OPS 때와 달리 '표본'이 아니라 **모집단 전수**를 받는 것이
# 목표다 (그래야 출원인 총계를 세기만 하면 되고, 표본 편향이 아예 생기지 않는다).
# 실측: H02M × 최근 90일 = 595건. 2천이면 넉넉하다고 봤는데 실전에서 재생에너지·
# 저장이 잘렸다 — 그 분야에 배터리(H01M10)가 들어 있고, 배터리는 다른 어떤 전력
# 접두보다 출원이 많다. 잘리면 그 분야의 출원인 총계만 전수가 아니게 되어 한 표
# 안에서 성격이 다른 수치가 섞이므로, 배터리가 들어와도 남는 값으로 올린다.
# 접두 하나가 5천을 넘으면 다시 잘리고, 그때는 로그가 어느 분야인지 말해 준다.
KIPRIS_PER_CAT = int(os.getenv("KIPRIS_PER_CAT", "5000"))
KIPRIS_DELAY = float(os.getenv("KIPRIS_DELAY", "0.3"))

# ── 초당 호출 수의 천장 ───────────────────────────────────────────────
# KIPRISplus 의 제약은 일일 총량이 아니라 **초당 호출 수**이고(2026-09-01 부터
# 100회/초), 넘기면 권한이 사라진다. 워커 수를 조절하는 것으로는 못 막는다 —
# 실제 속도는 '워커 수 ÷ 응답시간' 이고 응답시간은 우리 것이 아니기 때문이다.
# 자세한 사정은 kipris_rate.py 머리글에 적어 두었다.
#
# 30 은 한도의 30% 다. 넉넉히 두는 이유: (1) 순차 수집과 병렬 보강이 한 실행
# 안에서 겹칠 수 있고, (2) 초 경계에서 두 초가 붙어 보일 수 있으며, (3) 넘겼을
# 때의 대가가 '느려짐' 이 아니라 '권한 상실' 이라 비대칭이다.
# 최대 건수(해외 국적 4,000 + 국내 4,000 + CPC 5,000 = 13,000)를 다 써도
# 13,000 ÷ 30 ≈ 7분이라 매일 실행에 얹을 만하다.
KIPRIS_RPS = float(os.getenv("KIPRIS_RPS", "30"))

# ── 해외특허 (계열이 완전히 다르다 — patent_source_foreign 주석 참고) ──
# 전부 러너 실측(2026-08-25). 이름의 오타 'Advenced' 가 진짜다 — 철자를 고치면
# 경로 없음이 온다. 키 질의도 국내(ServiceKey)와 달리 accessKey 다.
FOREIGN = os.getenv("KIPRIS_FOREIGN", "on").lower() not in ("0", "off", "false")
FOREIGN_BASE = os.getenv("FOREIGN_BASE", "http://plus.kipris.or.kr/openapi/rest")
FOREIGN_SERVICE = os.getenv("FOREIGN_SERVICE",
                            "ForeignPatentAdvencedSearchService")
FOREIGN_OP = os.getenv("FOREIGN_OP", "advancedSearch")
FOREIGN_KEYPARAM = os.getenv("FOREIGN_KEYPARAM", "accessKey")
# 대상국. 쉼표로 한 번에 여러 개를 줄 수 있다(US,EP,JP,CN 실측). 비우면 필수값
# 오류(11)가 난다. KR 은 넣지 않는다 — 국내는 국내 API 로 전수를 받고 있어
# 여기서 또 받으면 같은 특허가 두 벌로 들어온다.
FOREIGN_COUNTRIES = [s for s in os.getenv(
    "FOREIGN_COUNTRIES", "US,EP,JP,CN").split(",") if s.strip()]
# 한 요청에 받을 건수. docsCount=50 이 먹는 것을 확인했다(기본은 30).
FOREIGN_ROWS = int(os.getenv("FOREIGN_ROWS", "50"))
FOREIGN_PER_CAT = int(os.getenv("FOREIGN_PER_CAT", "3000"))

# ── 해외 출원인 국적 보강 ────────────────────────────────────────
# 해외 **검색** 응답에는 출원인 국적이 없다. 그래서 큐레이션 별칭에 걸리는 곳만
# 국적이 붙고 나머지는 비어 있었다 — 실측 출원인 5,471곳 중 4,256곳이 미상이고,
# 그 대부분이 중국 대학·국유기업이다. 공개국으로 대신 채우면 'US 에 낸 일본
# 회사'가 미국 기업이 되므로 그것은 하지 않는다.
#
# **서지상세**에는 applicantCountry 가 있다(실측 2026-08-25):
#   US 공개 202600213551A1 → Panasonic … / JP
#   US 공개 202600221299A1 → Tsinghua University / CN
# 공개국과 출원인 국적이 실제로 갈리는 것까지 확인됐다.
#
# 다만 건당 1요청이고 응답이 무겁다(청구항·초록까지 실려 온다). 그래서
#   · **출원인당 1회**만 부른다. 국적은 출원인의 성질이지 문서의 성질이 아니다.
#   · 한 실행에 상한을 두고 stats.json 에 누적한다(OPS 시절 offices 와 같은 방식).
#   · 몇 갈래로 나눠 동시에 부른다. 하나가 느리다고 줄 전체가 서지는 않게.
ORIGIN = os.getenv("KIPRIS_ORIGIN", "on").lower() not in ("0", "off", "false")
ORIGIN_BASE = os.getenv("ORIGIN_BASE", "http://plus.kipris.or.kr/openapi/rest")
ORIGIN_SERVICE = os.getenv("ORIGIN_SERVICE", "ForeignPatentBibliographicService")
ORIGIN_OP = os.getenv("ORIGIN_OP", "bibliographicInfo")
ORIGIN_KEYPARAM = os.getenv("ORIGIN_KEYPARAM", "accessKey")
# 실측: 건당 약 5초. 여섯 갈래 동시면 800곳에 10분 남짓이고, 4천여 곳이 엿새면
# 다 찬다. 매일 실행에 얹기에 그 정도가 한계다.
# 상한을 대폭 올렸다. KIPRISplus 의 제약은 **일일 총량이 아니라 초당 호출 수**다
# (공지 실측: 75회/초, 2026-09-01 부터 100회/초). 지금까지 워커 6 × 건당 약 5초라
# 1.2 req/s — 한도의 1.6% 만 쓰면서 보강을 며칠에 나눠 하고 있었다. 워커를 20 으로
# 올려도 약 4 req/s 로 한도의 5% 다. 그래서 한 실행에서 전수를 끝낸다.
ORIGIN_PER_RUN = int(os.getenv("ORIGIN_PER_RUN", "4000"))  # 0 이면 끔
ORIGIN_WORKERS = int(os.getenv("ORIGIN_WORKERS", "20"))
ORIGIN_TIMEOUT = int(os.getenv("ORIGIN_TIMEOUT", "20"))
# 몇 번 연달아 실패하면 그만 시도한다. 번호 표기가 안 맞는 문헌이 섞여 있으면
# 매 실행 같은 것을 다시 두드리며 상한을 다 써 버린다.
ORIGIN_MAX_TRY = int(os.getenv("ORIGIN_MAX_TRY", "3"))

# ── 국내 공보 출원인 국적 ─────────────────────────────────────────
# 국내 수집기(patent_source_kipris._identify)는 별칭표에 없는 출원인을 **전부 KR**
# 로 적는다. 한국에 공개한 외국 기업이 그대로 국내 기업이 된다. 실측(8/25, 누적
# 16,265건): 이름에 외국 법인 표기가 든 것만 세어도 국내공보·KR표시 4,905건 중
# 461건(9.4%)·출원인 219곳이고, '🇰🇷 국내 N%' 배지가 분야마다 0.3~5.9%p 부풀어
# 있었다. 이름만 보고 가르는 것은 하지 않는다(포스코홀딩스는 '홀딩스' 때문에
# 외국으로 잡힌다) — 해외 쪽에서 공개국 추정을 실측으로 반박하고 버린 것과 같은 이유다.
#
# 국내 서지상세에는 그 값이 실제로 있다(8/25 실측, CATL 출원 1020267024585):
#   <applicantInfoArray><applicantInfo><country>중국</country> …
#   <agentInfoArray><agentInfo><country>대한민국</country> …   ← 대리인. 함정이다
# 대리인은 거의 언제나 한국 특허법인이라, 스코프 없이 <country> 를 읽으면 전부
# KR 로 돌아온다. 반드시 applicantInfo 안에서만 읽는다.
#
# 값이 ISO 코드가 아니라 **한국어 나라 이름**이라 표가 필요하다. 표에 없는 이름은
# 추측하지 않고 사유로 남겨 로그에 찍는다 — 그걸 보고 사람이 한 줄 더한다.
ORIGIN_KR = os.getenv("KIPRIS_ORIGIN_KR", "on").lower() not in ("0", "off", "false")
ORIGIN_KR_BASE = os.getenv("ORIGIN_KR_BASE", KIPRIS_BASE)
ORIGIN_KR_SERVICE = os.getenv("ORIGIN_KR_SERVICE", KIPRIS_SERVICE)
ORIGIN_KR_OP = os.getenv("ORIGIN_KR_OP", "getBibliographyDetailInfoSearch")
ORIGIN_KR_KEYPARAM = os.getenv("ORIGIN_KR_KEYPARAM", KIPRIS_KEYPARAM)
# 대상은 국내 출원인 1,174곳(실측)이라 해외(4천여 곳)보다 훨씬 작다 — 이틀이면 찬다.
ORIGIN_KR_PER_RUN = int(os.getenv("ORIGIN_KR_PER_RUN", "4000"))  # 0 이면 끔

COUNTRY_KO = {
    "대한민국": "KR", "한국": "KR",
    "미국": "US", "중국": "CN", "일본": "JP", "대만": "TW", "홍콩": "HK",
    "독일": "DE", "프랑스": "FR", "영국": "GB", "네덜란드": "NL", "스위스": "CH",
    "스웨덴": "SE", "덴마크": "DK", "핀란드": "FI", "노르웨이": "NO",
    "이탈리아": "IT", "스페인": "ES", "오스트리아": "AT", "벨기에": "BE",
    "아일랜드": "IE", "룩셈부르크": "LU", "폴란드": "PL", "체코": "CZ",
    "헝가리": "HU", "포르투갈": "PT", "그리스": "GR", "터키": "TR",
    "튀르키예": "TR", "러시아": "RU", "우크라이나": "UA",
    "캐나다": "CA", "멕시코": "MX", "브라질": "BR", "칠레": "CL",
    "이스라엘": "IL", "인도": "IN", "싱가포르": "SG", "말레이시아": "MY",
    "태국": "TH", "베트남": "VN", "인도네시아": "ID", "필리핀": "PH",
    "호주": "AU", "오스트레일리아": "AU", "뉴질랜드": "NZ",
    "남아프리카공화국": "ZA", "사우디아라비아": "SA",
    "아랍에미리트": "AE", "아랍에미리트연합": "AE",
    # 첫 실행(600곳)에서 표에 없어 걸린 이름. 로그가 이름을 실어 올려 준 것을
    # 그대로 옮겨 적었다 — 추측한 것이 아니다.
    "세이쉘": "SC", "세이셸": "SC",
    # 아직 안 걸렸지만 특허 출원인 주소로 흔한 곳들(조세피난처 법인·유럽 소국·
    # 신흥국). 미리 채워 두면 로그를 한 번 덜 오간다.
    "케이맨제도": "KY", "버진아일랜드": "VG", "버뮤다": "BM", "파나마": "PA",
    "리히텐슈타인": "LI", "모나코": "MC", "몰타": "MT", "키프로스": "CY",
    "아이슬란드": "IS", "슬로베니아": "SI", "슬로바키아": "SK",
    "루마니아": "RO", "불가리아": "BG", "크로아티아": "HR", "세르비아": "RS",
    "에스토니아": "EE", "라트비아": "LV", "리투아니아": "LT", "벨라루스": "BY",
    "카자흐스탄": "KZ", "우즈베키스탄": "UZ", "몽골": "MN", "마카오": "MO",
    "아르헨티나": "AR", "콜롬비아": "CO", "페루": "PE", "우루과이": "UY",
    "이집트": "EG", "나이지리아": "NG", "케냐": "KE", "모로코": "MA",
    "카타르": "QA", "쿠웨이트": "KW", "이란": "IR", "요르단": "JO",
    "파키스탄": "PK", "방글라데시": "BD", "스리랑카": "LK", "네팔": "NP",
    "미얀마": "MM", "캄보디아": "KH", "라오스": "LA", "브루나이": "BN",
}

# ── CPC 보강 ─────────────────────────────────────────────────────
# 검색은 IPC 로만 되지만(cpcNumber 파라미터 없음), **출원번호를 주면 그 특허의
# CPC 를 받을 수 있다**(명세서 실측):
#   http://plus.kipris.or.kr/openapi/rest/patUtiModInfoSearchSevice/patentCpcInfo
#     ?applicationNumber=1020060118886&accessKey=…
# 같은 서비스가 /openapi/rest 에도 있고 거기서는 키 이름이 accessKey 다.
#
# 이걸로 Y04S 처럼 CPC 에만 있는 코드를 되찾아 8대 분야 분류를 원래 정의대로
# 맞출 수 있다. 다만 건당 1요청이라 전수 보강은 비싸다 → 상한을 두고, 분류가
# 바뀔 여지가 큰 것부터 채운다(무엇을 몇 건 보강했는지는 로그에 남긴다).
KIPRIS_CPC_BASE = os.getenv("KIPRIS_CPC_BASE",
                            "http://plus.kipris.or.kr/openapi/rest")
KIPRIS_CPC_KEYPARAM = os.getenv("KIPRIS_CPC_KEYPARAM", "accessKey")
KIPRIS_CPC_LIMIT = int(os.getenv("KIPRIS_CPC_LIMIT", "5000"))  # 0 이면 끔

_MAIN_GROUP = re.compile(r"^[A-H][0-9]{2}[A-Z]([0-9]+)")
# 'H02J1'·'Y04S10' 처럼 메인그룹 번호까지 딱 떨어지는 접두. Y 섹션도 쓰므로 [A-Z].
_MAIN_GROUP_PREF = re.compile(r"[A-Z][0-9]{2}[A-Z][0-9]+")


def is_index_code(code: str) -> bool:
    """'2000 시리즈' 색인 코드인가.

    CPC/IPC 는 주분류 외에 **부가 태그**를 둔다. 상위 분류의 계층을 거울처럼
    비추면서 소재·용도·환경 특성을 짚어 주는 코드로, 메인그룹 번호가 2000 이상이다
    (H02J 2300/28 저장, H01M 2300 전해질, F05B 2270 풍력 제어…). 검색 정밀도를 위한
    심사관 참고용이지 **그 특허의 성격을 규정하는 코드가 아니다** → 분야를 정하는
    데 쓰지 않는다.

    메인그룹이 100 을 넘는다고 색인 코드인 것은 아니다 — C07D 307/00, C09J 141/00
    처럼 진짜 메인그룹도 세 자리를 쓴다(처음에 그렇게 재서 59건을 잘못 골랐다).
    """
    m = _MAIN_GROUP.match((code or "").replace(" ", ""))
    return bool(m) and int(m.group(1).split("/")[0]) >= 2000


def code_matches(code: str, pref: str) -> bool:
    """분류 코드가 접두에 걸리는가 — **코드 경계를 지켜서**.

    글자만 앞에서 맞춰 보면 메인그룹 자리에서 번호가 새어 든다:
    'H02J101/24'.startswith('H02J1') 이 참이라, 직류 계통(H02J 1/00)을 찾는 접두가
    IPC 색인 코드 H02J 101/00(전원의 성질)까지 끌고 왔다. 실측 29건이 그렇게
    분야가 정해져 있었다.

    그래서 접두가 어디서 끝나느냐로 나눈다.
      · 메인그룹까지 딱 떨어지는 접두('H02J1' = 서브클래스+번호) → 뒤에 '/' 가
        오거나 끝나야 한다. H02J 1/10 은 잡고 H02J 101/24 는 놓아준다.
      · 그보다 짧은 접두('G21' 섹션+클래스, 'H02G' 서브클래스) → 뒤는 무엇이든
        온다. 여기서 끊으면 G21C 1/00 까지 놓쳐 버린다.
      · 그보다 긴 접두('H02J7/34' 서브그룹) → 숫자가 더 와도 된다. IPC 에서
        7/345 는 7/34 의 **하위 갈래**라 잘라 내면 자식들을 잃는다.
    """
    c = (code or "").replace(" ", "")
    p = (pref or "").replace(" ", "")
    if not p or not c.startswith(p):
        return False
    if not _MAIN_GROUP_PREF.fullmatch(p):
        return True
    rest = c[len(p):]
    return rest == "" or rest[0] == "/"


# ── Y04S 10/00 안의 하위 갈래 ────────────────────────────────────
# 발전·송배전 지원 한 분야가 수집분의 약 70%다. 그 안에서 장주기 ESS 가 어디쯤인지
# 보이지 않아, CPC 가 이미 나눠 둔 하위 그룹으로 한 번 더 쪼갠다. 갈래도 이름도
# 우리가 만든 것이 아니다 — CPC 2026.08 Y04S 10/00 의 하위 그룹 그대로다(en 은
# 그 원문 표제, name 은 우리가 옮긴 말).
#
# 순서가 곧 우선순위다. 먼저 걸리는 갈래가 이긴다 → **좁은 것을 위에** 둔다.
# G01R31(전기적 특성 시험)이 특히 그렇다: 같은 서브클래스 안에
#   /08·/11·/50~/58  선로 고장 위치 → 10/52 정전·고장 관리
#   /36~/39x         전지 상태 진단 → 10/14 에너지 저장
#   나머지            절연·회로 시험 → 10/30 상태 감시
# 가 섞여 있어, G01R31 을 통째로 한 갈래에 넣으면 그 분야의 4,504건이 한 칸에
# 뭉친다. 좁은 접두를 위에 두어야 갈라진다.
SUBGROUPS = {
    "y04s10": [
        {"code": "10/52", "name": "정전·고장 관리",
         "en": "Outage or fault management, e.g. fault detection or location",
         "ipc": ["G01R31/02", "G01R31/08", "G01R31/11", "G01R31/14", "G01R31/34",
                 "G01R31/50", "G01R31/52", "G01R31/54", "G01R31/58", "H02H7/26"]},
        {"code": "10/14", "name": "에너지 저장",
         "en": "Energy storage units",
         # 장주기만 세지 않는다. 계통에 붙은 저장(H02J15·H02J3/28·3/32)과
         # 전지 상태 진단(G01R31/36~/39x, SoC·SoH)을 함께 담는다 — 실측 1,413건
         # 중 진단 쪽이 더 많고, 거기에는 전기차·기기용도 섞여 있다.
         "note": "계통에 붙은 저장장치와 전지 상태 진단(SoC·SoH)을 함께 셉니다"
                 " — 장주기 ESS 만 추린 수치가 아닙니다.",
         "ipc": ["H02J15", "H02J3/28", "H02J3/32",
                 "G01R31/36", "G01R31/37", "G01R31/38", "G01R31/39",
                 "H01M10/42", "H01M10/44", "H01M10/48"]},
        {"code": "10/12", "name": "분산전원 연계",
         "en": "Monitoring or controlling equipment for energy generation units,"
               " e.g. distributed energy generation [DER] or load-side generation",
         "ipc": ["H02J3/38", "H02J3/40", "H02J3/46", "H02J3/48", "H02J3/50"]},
        {"code": "10/22", "name": "무효전력·FACTS",
         "en": "Flexible AC transmission systems [FACTS] or power factor or"
               " reactive power compensating or correcting units",
         "ipc": ["H02J3/12", "H02J3/16", "H02J3/18"]},
        {"code": "10/18", "name": "개폐·차단",
         "en": "using switches, relays or circuit breakers,"
               " e.g. intelligent electronic devices [IED]",
         "ipc": ["H01H", "G01R31/327", "G01R31/333"]},
        {"code": "10/20", "name": "보호계전",
         "en": "using protection elements, arrangements or systems",
         "ipc": ["H02H"]},
        {"code": "10/16", "name": "변전소·변압",
         "en": "Electric power substations",
         "ipc": ["H02B", "H01F27", "H01F30", "H01F38"]},
        {"code": "10/30", "name": "상태 감시·진단",
         "en": "State monitoring, e.g. fault, temperature monitoring,"
               " insulator monitoring, corona discharge",
         "ipc": ["G01R31", "G01R1", "G01R15", "G01R19", "G01R27", "G01R29",
                 "G01R35", "G01D21", "G01K", "G01H"]},
        {"code": "10/40", "name": "정보 표시",
         "en": "Display of information, e.g. of data or controls",
         "ipc": ["G09G", "G06F3/048"]},
        {"code": "10/50", "name": "계통 운영·제어",
         "en": "Systems or methods supporting the power network operation or"
               " management, involving a certain degree of interaction with the"
               " load-side end user applications",
         "ipc": ["H02J13", "H02J3", "H02J1", "H02J4", "H02J11", "G05B",
                 "G06Q50/06"]},
        # 어디에도 안 걸리는 나머지. 실측 691건(10.5%)이고 거의 전부 H02G —
        # 전선·케이블 부설과 접속함이다. Y04S 10 에 대응하는 하위 그룹이 아예
        # 없어서 남는 것이라, '기타' 가 아니라 그 사실을 이름에 적는다.
        {"code": "10/00", "name": "배선·케이블",
         "en": "Systems supporting electrical power generation, transmission or"
               " distribution",
         # 이름을 짧게 두는 이유: 막대 칸 안에 들어가는 글자다. 처음에
         # '배선·케이블 (하위 갈래 없음)' 으로 뒀더니 102px 칸에서 잘렸다.
         # 사연은 툴팁으로 옮긴다 — 코드가 '10/00'(하위 없는 상위 그룹) 인 것
         # 자체가 이미 신호이기도 하다.
         "note": "Y04S 10 에 대응하는 하위 그룹이 없어 상위 그룹 직속으로 둡니다"
                 " — 전선 부설·가공선·케이블 접속함 등.",
         "ipc": []},
    ],
}
SUBGROUP_LAST = {k: v[-1]["code"] for k, v in SUBGROUPS.items()}


def subgroup_of(codes: list[str], cat_key: str) -> str:
    """분류 코드 목록 → 그 분야 안의 하위 갈래 코드. 없으면 빈 문자열."""
    subs = SUBGROUPS.get(cat_key)
    if not subs:
        return ""
    real = [c for c in codes if not is_index_code(c)] or list(codes)
    for sub in subs:
        for pref in sub["ipc"]:
            if any(code_matches(c, pref) for c in real):
                return sub["code"]
    return SUBGROUP_LAST[cat_key]


def classify(codes: list[str], fallback: str = "") -> str:
    """분류 코드 목록 → 분야 키(CATEGORIES 순서 = 우선순위).

    수집할 때도 그릴 때도 같은 함수를 쓴다. 그래야 아카이브에 저장된 옛 분야 키와
    무관하게, 그리는 시점에 지금 규칙으로 다시 나눌 수 있다(저장분을 건드리지 않고
    분야 체계를 바꿀 수 있는 이유가 이것이다).

    색인 코드는 빼고 본다. 진짜 분류 코드가 하나도 없을 때만 색인 코드라도 본다 —
    아무 근거 없이 fallback 으로 흘리는 것보다 낫다.
    """
    real = [c for c in codes if not is_index_code(c)] or list(codes)
    for cat in CATEGORIES:
        for pref in cat["match"]:
            if any(code_matches(c, pref) for c in real):
                return cat["key"]
    return fallback


CATEGORY_BY_KEY = {c["key"]: c for c in CATEGORIES}
APPLICANT_BY_NAME = {a["name"]: a for a in APPLICANTS}


def is_mock() -> bool:
    return MOCK_MODE == "on"


def force_live() -> bool:
    return MOCK_MODE == "off"
