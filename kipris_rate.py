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


_PACER = Pacer(cfg.KIPRIS_RPS)


def acquire() -> float:
    """KIPRIS 를 부르기 직전에 호출한다."""
    return _PACER.acquire()
