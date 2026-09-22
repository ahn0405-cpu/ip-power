"""KIPRIS 초당 호출 수의 **천장**. 모든 KIPRIS 호출이 이 문을 지난다.

왜 필요한가. KIPRISplus 의 제약은 일일 총량이 아니라 초당 호출 수이고
(2026-09-01 부터 100회/초), 넘기면 권한이 사라진다 — 되돌리는 데 사람 손이
필요한 종류의 사고다.

워커 수를 줄이는 것으로는 못 막는다. 실제 초당 호출 수는

    초당 호출 수 = 워커 수 ÷ 응답시간

이고, 응답시간은 우리 것이 아니라 KIPRIS 것이다. 워커 20 · 건당 5초면 4회/초
지만, 같은 코드가 건당 0.2초면 100회/초, 0.1초면 200회/초가 된다. 코드를 한 줄도
안 고쳤는데 상대 서버가 빨라졌다는 이유만으로 한도를 넘는다.

더 나쁜 것은 **실패가 빠르다**는 점이다. 서버가 막히기 시작하면 오류는 즉시
돌아오고, 그러면 풀은 곧바로 다음 요청을 던진다 — 가장 두드리면 안 될 때 가장
빨리 두드리는 되먹임이다. 403 이 나기 시작한 순간 속도가 치솟는다.

그래서 응답시간·워커 수·성공 여부와 무관하게 천장을 못으로 박는다. 호출 직전에
acquire() 를 부르면, 프로세스 전체를 통틀어 초당 KIPRIS_RPS 회를 넘지 않는다.

── 천장만으로는 모자란다: 차단기 ──────────────────────────────────
천장은 **얼마나 빨리** 두드릴지만 정한다. **언제 그만둘지**는 정하지 않는다.

실제로 벌어진 일(2026-09-22 KIPRIS 답신으로 확인): 초당 호출 초과로 이 ID 가
차단됐다가 자정에 자동 해제됐다. 정황은 로그에 남아 있다 — 9/14·9/21 주간 실행
모두 해외 호출이 전부 resultCode=30 으로 즉시 실패했고, 그 실패는 기다림이 없어
워커 20개가 쉬지 않고 다음 요청을 던졌다. 그런데 모든 예외를 삼키는 구조라
빌드는 '성공' 으로 끝났다. 아무도 몰랐다.

천장을 박은 지금도 이 부분은 그대로다. 차단당한 뒤에도 남은 13,000번을 75회/초로
꼬박 3분간 더 두드린다 — 이미 문이 닫힌 곳을, 가장 두드리면 안 될 때.

그래서 문에 차단기를 단다. 끊는 기준은 둘이다.
  (가) 한도 초과를 **명시**하는 응답(resultCode 22, HTTP 429 …) → 한 번에 끊는다.
  (나) 성공 없이 연속 실패가 KIPRIS_FAIL_STREAK 회 → 끊는다. 이유가 무엇이든
       '빠른 실패가 줄지어 오는 상태' 자체가 위험하기 때문이다. 원인을 몰라도
       끊을 수 있어야 한다 — 원인을 알 때쯤이면 이미 늦다.

중요한 구분: **응답이 비어 있는 것은 실패가 아니다.** 국적 칸이 없거나 검색 결과가
0건인 것은 서비스가 멀쩡히 답한 것이다(실측: 정상 실행에서도 800곳 중 738곳이
'국적칸없음' 이었다). 이것을 실패로 세면 멀쩡한 실행에서 차단기가 내려가 보강이
통째로 죽는다. 차단기가 재는 것은 **서비스의 건강**이지 자료의 수확량이 아니다.
"""
from __future__ import annotations

import threading
import time

import patent_config as cfg


class Pacer:
    """호출 시각을 일정 간격으로 벌려 주는 문(gate).

    토큰 버킷을 쓰지 않는 이유: 버킷은 쌓아 둔 토큰을 한꺼번에 쓸 수 있어 **순간
    폭주**를 허용한다. 우리가 막으려는 것이 바로 그 순간 폭주다. 그래서 '다음에
    허용되는 시각' 하나만 들고, 호출마다 그 시각을 간격만큼 밀어 둔다 — 몰아치기가
    구조적으로 불가능하다.
    """

    def __init__(self, rps: float) -> None:
        self._gap = 1.0 / rps if rps > 0 else 0.0
        self._lock = threading.Lock()
        self._next = 0.0

    def acquire(self) -> float:
        """차례가 될 때까지 기다린다. 반환: 실제로 기다린 초(시험용)."""
        if self._gap <= 0:                      # 0 이하면 끈 것으로 본다
            return 0.0
        # 자리를 잡는 동안만 잠근다. sleep 은 잠금 **밖**에서 한다 —
        # 안에서 자면 스레드들이 한 줄로 서서 병렬이 통째로 사라진다.
        with self._lock:
            now = time.monotonic()
            when = max(now, self._next)
            self._next = when + self._gap
        delay = when - time.monotonic()
        if delay > 0:
            time.sleep(delay)
        return max(0.0, delay)


class Blocked(RuntimeError):
    """차단(또는 그 직전)으로 보고 이번 실행의 남은 KIPRIS 호출을 끊었다."""


# 한도 초과를 명시하는 신호. 이것이 보이면 연속 실패를 셀 것도 없이 끊는다.
#  · resultCode 22 = 요청 한도 초과 (probe_kipris.CODE_MEANS 참고)
#  · HTTP 429 = Too Many Requests
# 문구는 대문자로 맞춰 비교한다. 여기 없는 표현은 (나) 연속 실패로 걸린다.
_HARD_MARKS = ("코드22", "RESULTCODE=22", "HTTP 429", "TOO MANY REQUEST",
               "요청 한도", "REQUEST LIMIT", "EXCEED")


class Breaker:
    """빠른 실패가 줄지어 오면 남은 호출을 끊는다.

    연속 실패만 센다(누적이 아니라). 성공이 하나라도 섞이면 0 으로 되돌린다 —
    드문드문 실패하는 것은 그냥 그 문헌의 사정이고, 줄줄이 실패하는 것만이
    '상대가 문을 닫았다' 는 신호이기 때문이다.
    """

    def __init__(self, streak: int) -> None:
        self._limit = max(0, streak)
        self._lock = threading.Lock()
        self._streak = 0
        self._tripped = False
        self._why = ""
        self._ok = 0
        self._fail = 0
        self._skipped = 0          # 차단기가 내려간 뒤 안 보낸 호출 수

    # ── 문 ──────────────────────────────────────────────────────
    def check(self) -> None:
        with self._lock:
            if self._tripped:
                self._skipped += 1
                why = self._why
            else:
                return
        raise Blocked(why)

    # ── 결과 보고 ───────────────────────────────────────────────
    def ok(self) -> None:
        """서비스가 정상으로 답했다. 자료가 비어 있어도 여기다."""
        with self._lock:
            self._ok += 1
            self._streak = 0

    def fail(self, tag: str) -> None:
        """서비스에 닿지 못했거나 오류 코드를 받았다."""
        up = str(tag).upper()
        hard = any(m in up for m in _HARD_MARKS)
        with self._lock:
            self._fail += 1
            self._streak += 1
            if self._tripped:
                return
            if hard:
                self._why = f"한도 초과 응답({tag})"
            elif self._limit and self._streak >= self._limit:
                self._why = f"성공 없이 연속 {self._streak}회 실패(마지막: {tag})"
            else:
                return
            self._tripped = True
            why = self._why
        # 잠금 밖에서 한 번만 크게 남긴다. 조용히 끊으면 이번에도 아무도 모른다.
        print(f"\n  ⛔ KIPRIS 호출을 끊습니다 — {why}")
        print("     이번 실행의 남은 KIPRIS 호출은 보내지 않습니다."
              " (차단 상태에서 계속 두드리면 차단이 길어집니다)\n")

    # ── 실행 끝 보고용 ──────────────────────────────────────────
    def status(self) -> dict:
        with self._lock:
            return {"tripped": self._tripped, "why": self._why,
                    "ok": self._ok, "fail": self._fail,
                    "skipped": self._skipped}


_PACER = Pacer(cfg.KIPRIS_RPS)
_BREAKER = Breaker(cfg.KIPRIS_FAIL_STREAK)


def acquire() -> float:
    """KIPRIS 를 부르기 직전에 호출한다. 차단기가 내려갔으면 Blocked 를 던진다."""
    _BREAKER.check()
    return _PACER.acquire()


def ok() -> None:
    """호출 성공(서비스가 정상 응답). 자료가 비어도 성공이다."""
    _BREAKER.ok()


def fail(tag: str) -> None:
    """호출 실패(연결 실패·시간초과·오류 코드). 자료 없음은 여기가 아니다."""
    _BREAKER.fail(tag)


def status() -> dict:
    """실행 끝에 한 번 읽어 로그에 남긴다."""
    return _BREAKER.status()


def summary() -> str:
    """사람이 읽을 한 줄. 정상이면 빈 문자열."""
    s = _BREAKER.status()
    if not s["tripped"]:
        return ""
    return (f"⛔ KIPRIS 차단기 작동 — {s['why']} · "
            f"성공 {s['ok']:,} · 실패 {s['fail']:,} · 보내지 않음 {s['skipped']:,}")


def reset(streak: int | None = None, rps: float | None = None) -> None:
    """시험용. 실행 중에는 쓰지 않는다."""
    global _PACER, _BREAKER
    _BREAKER = Breaker(cfg.KIPRIS_FAIL_STREAK if streak is None else streak)
    if rps is not None:
        _PACER = Pacer(rps)
