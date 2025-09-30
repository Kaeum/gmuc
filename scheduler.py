#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
server (백엔드 역할)
- Reservation 객체 생성/보관
- 실행 시각이 되면 각 예약에 대해 reserv.py(요청 클라이언트) 실행
  (항상 Reservation의 값을 명시적 인자로 전달)
- timeCode, courtCode 해석 규칙 적용:
  * timeCode: 06:00-08:00 기준. Base는 예약일(YYYYMMDD)의 월 기준으로 산출:
      - 10월일 때 base=69, 이후 한 달 증가할 때마다 +8 (년도 넘김 포함)
      - 파라미터로 base가 주어지면 해당 값을 우선 사용
    각 2시간 블록마다 +1 증가
  * courtCode: 코트번호 N -> TC + N을 3자리 0패딩 (예: 1 -> TC001)
"""
import os
import sys
import re
import time
import queue
import threading
import io
import contextlib
from dataclasses import dataclass, field
from datetime import datetime
from typing import Callable, List, Optional

# reserv.py는 이제 모듈로 import 하여 직접 호출합니다.


@dataclass
class Reservation:
    cookie: str
    reservDate: str      # "YYYYMMDD"
    fromTime: str        # "HH:MM"
    toTime: str          # "HH:MM"
    timeCode: str        # "TM061" 형태
    courtNo: int
    courtCode: str       # "TCxxx"
    exec_at: datetime
    timeBase: Optional[int] = None  # 사용된 TIME_CODE base (명시 또는 자동 계산)
    id: int = field(default_factory=lambda: int(time.time() * 1000))

    def to_env(self) -> dict:
        """ reserv.sh에 주입할 환경변수 딕셔너리 """
        return {
            "COOKIE": self.cookie,
            "reservDate": self.reservDate,
            "fromTime": self.fromTime,
            "toTime": self.toTime,
            "timeCode": self.timeCode,
            "courtNo": str(self.courtNo),
            "courtCode": self.courtCode,
        }


def _compute_timecode_base(reserv_date: str, base_override: Optional[int]) -> int:
    """TIME_CODE base 계산
    - override가 있으면 그대로 사용
    - 없으면 예약일(YYYYMMDD)의 월 기준으로 '해당 사이클의 10월' 대비 경과 개월 * 8을 69에 더함
      예: 10월→69, 11월→77, 12월→85, 다음 해 1월→93 ...
    """
    if base_override is not None:
        return int(base_override)
    m = re.match(r"^(\d{4})(\d{2})(\d{2})$", reserv_date)
    if not m:
        raise ValueError(f"reservDate 형식 오류: {reserv_date}")
    year = int(m.group(1))
    month = int(m.group(2))
    # 기준 10월: 같은 해 10월 또는 이전 해 10월(월이 1~9면 이전 해 10월을 기준)
    base_year = year if month >= 10 else year - 1
    months_since_oct = (year * 12 + month) - (base_year * 12 + 10)
    return 69 + 8 * months_since_oct


def derive_time_code(from_time: str, to_time: str, reserv_date: str, base_override: Optional[int] = None) -> str:
    """
    2시간 블록: 06:00-08:00가 base 의 첫 슬롯, 이후 2시간마다 +1.
    base는 _compute_timecode_base에 따름.
    """
    # ex: "06:00"
    m = re.match(r"^(\d{2}):(\d{2})$", from_time)
    if not m:
        raise ValueError(f"fromTime 형식 오류: {from_time}")
    start_h = int(m.group(1))
    start_m = int(m.group(2))
    if start_m != 0:
        raise ValueError("분 단위는 00만 허용합니다(2시간 블록 가정).")

    base = _compute_timecode_base(reserv_date, base_override)
    if start_h < 6 or start_h > 20 or (start_h - 6) % 2 != 0:
        # 06~20 사이, 2시간 간격만 허용
        raise ValueError(f"허용되지 않는 시작 시간: {from_time}")
    idx = base + ((start_h - 6) // 2)
    return f"TM0{idx}"


def derive_court_code(court_no: int) -> str:
    return f"TC{court_no:03d}"


class ReservationManager:
    def __init__(self, log_callback: Optional[Callable[[str], None]] = None):
        self.cookie: Optional[str] = None
        self._reservations: List[Reservation] = []
        self._lock = threading.Lock()
        self._running = False
        self._worker: Optional[threading.Thread] = None
        self._log_cb = log_callback or (lambda msg: None)
        self._exec_queue: "queue.Queue[Reservation]" = queue.Queue()

    # ----- API -----
    def set_cookie(self, cookie: str):
        self.cookie = cookie
        self._log(f"쿠키 설정: {cookie}")

    def create_reservation(self, reservDate: str, fromTime: str, toTime: str,
                           courtNo: int, exec_at: datetime,
                           timeBaseOverride: Optional[int] = None) -> Reservation:
        if not self.cookie:
            raise RuntimeError("쿠키가 설정되지 않음. 먼저 set_cookie 호출 필요.")
        timeCode = derive_time_code(fromTime, toTime, reservDate, timeBaseOverride)
        timeBase = _compute_timecode_base(reservDate, timeBaseOverride)
        courtCode = derive_court_code(courtNo)
        r = Reservation(
            cookie=self.cookie,
            reservDate=reservDate,
            fromTime=fromTime,
            toTime=toTime,
            timeCode=timeCode,
            courtNo=courtNo,
            courtCode=courtCode,
            exec_at=exec_at,
            timeBase=timeBase,
        )
        with self._lock:
            self._reservations.append(r)
        self._log(f"Reservation 생성: {r}")
        return r

    def start(self):
        if self._running:
            return
        self._running = True
        self._worker = threading.Thread(target=self._run_loop, daemon=True)
        self._worker.start()
        self._log("스케줄러 시작")

    def cancel_reservation(self, reservation_id: int) -> bool:
        """예약 취소: 대기 목록 및 실행 큐에서 제거 시도.

        반환: 하나 이상에서 제거되면 True
        """
        removed = False
        with self._lock:
            remain: List[Reservation] = []
            for r in self._reservations:
                if r.id == reservation_id:
                    removed = True
                else:
                    remain.append(r)
            self._reservations = remain

        # 실행 큐에서 제거(가능한 범위에서 non-blocking으로 재구성)
        tmp: List[Reservation] = []
        try:
            while True:
                item = self._exec_queue.get_nowait()
                if item.id == reservation_id:
                    removed = True
                    # drop it
                else:
                    tmp.append(item)
        except queue.Empty:
            pass
        finally:
            for item in tmp:
                self._exec_queue.put(item)

        if removed:
            self._log(f"Reservation 취소: id={reservation_id}")
        return removed

    # ----- 내부 -----
    def _run_loop(self):
        """ 실행시각이 지난 예약을 큐에 넣고, 큐에서 하나씩 꺼내 순차 실행 """
        self._log("스케줄러 루프 가동")
        while self._running:
            now = datetime.now()
            due_list: List[Reservation] = []
            with self._lock:
                remain: List[Reservation] = []
                for r in self._reservations:
                    if r.exec_at <= now:
                        due_list.append(r)
                    else:
                        remain.append(r)
                self._reservations = remain
            # 큐에 due 등록
            for r in due_list:
                self._exec_queue.put(r)
                self._log(f"실행 대기열 추가: id={r.id} @ {r.exec_at} "
                          f"({r.reservDate} {r.fromTime}-{r.toTime} court {r.courtNo})")

            # 큐 처리(순차)
            try:
                job = self._exec_queue.get(timeout=0.5)
            except queue.Empty:
                time.sleep(0.3)
                continue
            try:
                self._execute(job)
            except Exception as e:
                self._log(f"[ERROR] 실행 실패 id={job.id}: {e}")
            finally:
                self._exec_queue.task_done()

    def _execute(self, r: Reservation):
        self._log(
            f"실행 시작 id={r.id}: {r.reservDate} {r.fromTime}-{r.toTime} "
            f"court {r.courtNo} (timeCode={r.timeCode}, base={r.timeBase}, courtCode={r.courtCode})"
        )

        # Reservation의 값을 명시적 인자로 전달하여 실행
        rc, out = self._run_script_with_args(r)
        if rc == 0:
            self._log(f"실행 완료(id={r.id})\n{out.strip()}")
        else:
            self._log(f"[ERROR] 실행 실패(id={r.id}) rc={rc}\n{out}")

    def _run_script_with_args(self, r: Reservation) -> tuple[int, str]:
        """Reservation 값을 인자로 하여 reserv.run_reservation을 직접 호출"""
        try:
            import reserv
        except Exception as e:
            return 1, f"reserv 모듈 import 실패: {e}"

        buf = io.StringIO()
        try:
            with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
                rc = reserv.run_reservation(
                    cookie=r.cookie,
                    reserv_date=r.reservDate,
                    time_code=r.timeCode,
                    from_time=r.fromTime,
                    to_time=r.toTime,
                    court_code=r.courtCode,
                    court_no=r.courtNo,
                    # 기타 옵션은 reserv.run_reservation의 기본값 사용
                )
        except Exception as e:
            return 1, f"reserv.run_reservation 실행 오류: {e}"

        return int(rc), buf.getvalue()

    def _log(self, msg: str):
        self._log_cb(str(msg))
