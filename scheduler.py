#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
server (백엔드 역할)
- Reservation 객체 생성/보관
- 실행 시각이 되면 각 예약에 대해 reserv.py(요청 클라이언트) 실행
  (항상 Reservation의 값을 명시적 인자로 전달)
- timeCode, courtCode 해석 규칙 적용:
  * timeCode: 월별 운영 시간표(동절기 07~21, 비동절기 06~22) 슬롯을 따르며,
    base 값은 tennisReservDayCheck 응답의 timeCurrentInfo 첫 TIME_CODE를 기준으로 계산.
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
import json
from dataclasses import dataclass, field
from datetime import datetime
from typing import Callable, List, Optional, Tuple

# reserv.py는 이제 모듈로 import 하여 직접 호출합니다.


WINTER_MONTHS = {11, 12, 1, 2}
TIME_INFO_PATTERN = re.compile(
    r"var\s+timeCurrentInfo\s*=\s*([\"'])\s*(.*?)\s*\1",
    re.IGNORECASE | re.DOTALL,
)
TIME_CODE_VALUE_PATTERN = re.compile(
    r"TIME_CODE\s*[:=]\s*['\"]?\s*([A-Za-z0-9]+)",
    re.IGNORECASE,
)


def _extract_time_code_from_response(html: str) -> str:
    match = TIME_INFO_PATTERN.search(html)
    if not match:
        raise ValueError("timeCurrentInfo 구문을 찾을 수 없습니다.")
    payload = match.group(2).strip()
    if not payload:
        raise ValueError("timeCurrentInfo 데이터가 비어 있습니다.")
    try:
        data = json.loads(payload)
    except json.JSONDecodeError as e:
        alt = TIME_CODE_VALUE_PATTERN.search(payload)
        if alt:
            return alt.group(1)
        raise ValueError("timeCurrentInfo JSON 파싱 실패") from e
    if not isinstance(data, list) or not data:
        raise ValueError("예약 가능한 시간이 없습니다.")
    first = data[0]
    if not isinstance(first, dict):
        raise ValueError("timeCurrentInfo 형식이 올바르지 않습니다.")
    time_code = first.get("TIME_CODE") or first.get("time_code")
    if not time_code:
        raise ValueError("TIME_CODE 필드를 찾지 못했습니다.")
    return str(time_code)


def fetch_time_base_from_server(reserv_date: str, cookie: str, base_url: Optional[str] = None) -> int:
    """tennisReservDayCheck 응답에서 첫 TIME_CODE의 숫자 부분을 base로 반환."""
    if not cookie:
        raise ValueError("쿠키가 필요합니다.")
    try:
        import reserv
    except Exception as e:
        raise RuntimeError(f"reserv 모듈 로드 실패: {e}") from e

    base = (base_url or reserv.BASE_DEFAULT).rstrip("/")
    session = reserv.build_session(base, cookie)
    html = reserv.post_text(
        session,
        f"{base}/user/tennis/tennisReservDayCheck.do",
        {"reservDate": reserv_date},
    )
    time_code = _extract_time_code_from_response(html)
    m = re.search(r"(\d+)$", time_code)
    if not m:
        raise ValueError(f"TIME_CODE 형식이 올바르지 않습니다: {time_code}")
    return int(m.group(1))


def _time_slots_for_month(year: int, month: int) -> List[Tuple[str, str]]:
    """Return ordered 2-hour slots for given month, accounting for winter schedule."""
    if month in WINTER_MONTHS:
        start_hour = 7
        end_hour = 21  # exclusive upper bound for range
    else:
        start_hour = 6
        end_hour = 22
    slots: List[Tuple[str, str]] = []
    for hour in range(start_hour, end_hour, 2):
        slots.append((f"{hour:02d}:00", f"{hour + 2:02d}:00"))
    return slots


def get_time_slots_for_reserv_date(reserv_date: str) -> List[Tuple[str, str]]:
    """Expose slots to other modules (GUI) based on 예약일."""
    m = re.match(r"^(\d{4})(\d{2})(\d{2})$", reserv_date)
    if not m:
        raise ValueError(f"reservDate 형식 오류: {reserv_date}")
    year = int(m.group(1))
    month = int(m.group(2))
    return _time_slots_for_month(year, month)


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


def _compute_timecode_base(reserv_date: str, base_override: Optional[int], cookie: Optional[str]) -> int:
    """TIME_CODE base 계산
    - override가 있으면 그대로 사용
    - 없으면 tennisReservDayCheck 응답의 첫 TIME_CODE 숫자를 사용
    """
    if base_override is not None:
        return int(base_override)
    if not cookie:
        raise ValueError("Time Base 자동 계산을 위해 쿠키가 필요합니다.")
    return fetch_time_base_from_server(reserv_date, cookie)


def derive_time_code(from_time: str, to_time: str, reserv_date: str, time_base: int) -> str:
    """
    2시간 블록: 월별 운영 시간에 따라 첫 슬롯이 다르며, 이후 2시간마다 +1.
    주어진 time_base는 해당 날짜의 첫 TIME_CODE 숫자 값.
    """
    slots = get_time_slots_for_reserv_date(reserv_date)
    slot_map = {start: end for start, end in slots}
    if from_time not in slot_map:
        allowed = ", ".join(f"{start}-{end}" for start, end in slots)
        raise ValueError(f"허용되지 않는 시작 시간: {from_time} (허용 범위: {allowed})")
    expected_end = slot_map[from_time]
    if to_time != expected_end:
        raise ValueError(f"종료 시간이 시작 시간과 맞지 않습니다: {from_time}->{to_time} (기대값 {expected_end})")
    slot_index = [start for start, _ in slots].index(from_time)
    idx = time_base + slot_index
    # TM 코드는 3자리 0패딩이 원칙 (예: 61 -> TM061, 100 -> TM100)
    return f"TM{idx:03d}"


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
        self._active_job = False

    # ----- API -----
    def set_cookie(self, cookie: str):
        self.cookie = cookie
        self._log(f"쿠키 설정: {cookie}")

    def create_reservation(self, reservDate: str, fromTime: str, toTime: str,
                           courtNo: int, exec_at: datetime,
                           timeBaseOverride: Optional[int] = None) -> Reservation:
        if not self.cookie:
            raise RuntimeError("쿠키가 설정되지 않음. 먼저 set_cookie 호출 필요.")
        timeBase = _compute_timecode_base(reservDate, timeBaseOverride, self.cookie)
        timeCode = derive_time_code(fromTime, toTime, reservDate, timeBase)
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

    def stop(self):
        """스케줄러 루프를 종료"""
        self._running = False
        if self._worker and self._worker.is_alive():
            self._worker.join(timeout=2)

    def is_idle(self) -> bool:
        """예약/큐/실행 중 작업이 모두 없는지 확인"""
        with self._lock:
            pending = len(self._reservations)
        queue_empty = self._exec_queue.empty()
        return (pending == 0) and queue_empty and (not self._active_job)

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
                self._active_job = True
                self._execute(job)
            except Exception as e:
                self._log(f"[ERROR] 실행 실패 id={job.id}: {e}")
            finally:
                self._active_job = False
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
