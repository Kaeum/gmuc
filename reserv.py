#!/usr/bin/env python3
"""
reserv.py — 6단계 예약 HTTP 클라이언트 (모듈 버전)

용도
- GUI/스케줄러에서 import 하여 함수(run_reservation)를 직접 호출합니다.
- 표준출력으로 단계별 응답을 그대로 출력하므로, 호출측에서 stdout 캡처가 가능합니다.

주의
- requests 라이브러리가 필요합니다: pip install requests
"""
from __future__ import annotations

from typing import Dict
import json
import time

import requests


BASE_DEFAULT = "https://reserve.gmuc.co.kr"
UA_DEFAULT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/140.0.0.0 Safari/537.36"
)
CONNECT_TIMEOUT_DEFAULT = 5.0
READ_TIMEOUT_DEFAULT: float | None = None
STEP_RETRY_COUNT_DEFAULT = 1


def build_session(base: str, cookie: str, referer: str | None = None, ua: str | None = None) -> requests.Session:
    s = requests.Session()
    s.headers.update(
        {
            "User-Agent": ua or UA_DEFAULT,
            "cookie": cookie,
            "caller_id": "GMFMC_AJAX",
            "content-type": "application/x-www-form-urlencoded; charset=UTF-8",
            "x-requested-with": "XMLHttpRequest",
            "origin": base,
            "referer": referer or f"{base}/user/tennis/tennisReservation.do?menu=d&menuFlag=T",
        }
    )
    return s


def print_step(title: str):
    print(title)


def print_sep():
    print("\n----------------------------------------\n")


def post_text(
    s: requests.Session,
    url: str,
    data: Dict[str, str],
    timeout: float | tuple[float, float | None] | None = 120,
) -> str:
    r = s.post(url, data=data, timeout=timeout)
    if not r.encoding:
        r.encoding = "utf-8"
    return r.text


def _post_step_text(
    s: requests.Session,
    url: str,
    data: Dict[str, str],
    *,
    step_name: str,
    timeout: float | tuple[float, float | None] | None,
    retry_count: int,
) -> str:
    total_attempts = max(1, retry_count + 1)
    for attempt in range(1, total_attempts + 1):
        started = time.monotonic()
        try:
            resp = s.post(url, data=data, timeout=timeout)
            elapsed = time.monotonic() - started
            if not resp.encoding:
                resp.encoding = "utf-8"
            print(
                f"[HTTP] {step_name} status={resp.status_code} "
                f"elapsed={elapsed:.2f}s attempt={attempt}/{total_attempts}"
            )
            return resp.text
        except (requests.Timeout, requests.ConnectionError) as e:
            elapsed = time.monotonic() - started
            will_retry = attempt < total_attempts
            print(
                f"[WARN] {step_name} {type(e).__name__} elapsed={elapsed:.2f}s "
                f"attempt={attempt}/{total_attempts}"
            )
            if not will_retry:
                raise
            backoff = min(3.0, 0.5 * attempt)
            print(f"[HTTP][retry] {step_name} {backoff:.1f}s 후 재시도")
            time.sleep(backoff)

    raise RuntimeError(f"{step_name} 요청이 비정상 종료되었습니다.")


def _is_success_from_step6(text: str) -> tuple[bool, str | None, str | None]:
    """Parse step6 response text and decide success by errCode.

    Returns (is_success, err_code, err_msg).
    """
    try:
        data = json.loads(text)
        if isinstance(data, str):
            # Some APIs may return a JSON string; try second parse
            data = json.loads(data)
        if not isinstance(data, dict):
            return (False, None, None)
        err = data.get("errCode")
        err_msg = (
            data.get("errMsg")
            or data.get("errMessage")
            or data.get("msg")
            or data.get("message")
        )
        return (
            str(err) == "0",
            None if err is None else str(err),
            None if err_msg is None else str(err_msg),
        )
    except Exception:
        return (False, None, None)


def run_reservation(
    *,
    cookie: str,
    reserv_date: str,
    time_code: str,
    from_time: str,
    to_time: str,
    court_code: str,
    court_no: int,
    base: str = BASE_DEFAULT,
    ua: str = UA_DEFAULT,
    referer: str | None = None,
    menu_id: str = "Resv",
    use_type_cd: str = "002",
    use_type_nm: str = "연습이용",
    adult_cnt: int = 4,
    youth_cnt: int = 0,
    oldman_cnt: int = 0,
    gcard_cnt: int = 0,
    mchild_cnt: int = 0,
    use_light: str = "N",
    deal_type: str = "CARD",
    connect_timeout: float = CONNECT_TIMEOUT_DEFAULT,
    read_timeout: float | None = READ_TIMEOUT_DEFAULT,
    retry_count: int = STEP_RETRY_COUNT_DEFAULT,
) -> int:
    """6단계 예약 흐름 수행. 성공 시 0, 실패 시 비0 반환.

    호출자는 stdout/stderr를 캡처하여 GUI 로그로 표시할 수 있습니다.
    """
    if not cookie:
        print("[ERROR] cookie가 필요합니다 (예: JSESSIONID=XXXX)")
        return 2
    if not reserv_date:
        print("[ERROR] reserv_date 필요 (YYYYMMDD)")
        return 2
    if connect_timeout <= 0:
        print("[ERROR] connect_timeout은 0보다 커야 합니다.")
        return 2
    if read_timeout is not None and read_timeout <= 0:
        print("[ERROR] read_timeout은 None 또는 0보다 큰 값이어야 합니다.")
        return 2

    retry_count = max(0, int(retry_count))
    base = base.rstrip("/")
    s = build_session(base, cookie, referer=referer, ua=ua)
    timeout = (float(connect_timeout), None if read_timeout is None else float(read_timeout))
    read_label = "None" if read_timeout is None else f"{float(read_timeout):g}s"

    print_step(f"== 예약 시도 ==")
    print(f"[HTTP] timeout(connect={float(connect_timeout):g}s, read={read_label}), retries(step1~5)={retry_count}")
    print_sep()

    try:
        # 1) 날짜 가능 여부 체크
        print_step("== 1) 날짜 가능 여부 체크 ==")
        _ = _post_step_text(
            s,
            f"{base}/user/tennis/tennisReservDayCheck.do",
            {"reservDate": reserv_date},
            step_name="step1/day-check",
            timeout=timeout,
            retry_count=retry_count,
        )
        print_sep()

        # 2) 시간대 선택 검증
        print_step("== 2) 시간대 선택 검증 ==")
        if not (time_code and from_time and to_time):
            print("[WARN] time_code/from_time/to_time 중 일부가 비어 있음")
        _ = _post_step_text(
            s,
            f"{base}/user/tennis/tennisReservNext0Check.do",
            {
                "timeCode": time_code or "",
                "fromTime": from_time or "",
                "toTime": to_time or "",
                "menuId": menu_id,
            },
            step_name="step2/time-check",
            timeout=timeout,
            retry_count=retry_count,
        )
        print_sep()

        # 3) 코트 선택 검증
        print_step("== 3) 코트 선택 검증 ==")
        if not (court_code and court_no is not None):
            print("[WARN] court_code/court_no 중 일부가 비어 있음")
        _ = _post_step_text(
            s,
            f"{base}/user/tennis/tennisReservNext1Check.do",
            {
                "courtCode": court_code or "",
                "courtNo": str(court_no) if court_no is not None else "",
                "menuId": menu_id,
            },
            step_name="step3/court-check",
            timeout=timeout,
            retry_count=retry_count,
        )
        print_sep()

        # 4) 이용유형 선택
        print_step("== 4) 이용유형 선택 ==")
        _ = _post_step_text(
            s,
            f"{base}/user/tennis/tennisReservNext2Check.do",
            {
                "useTypeCd": use_type_cd,
                "useTypeNm": use_type_nm,
                "menuId": menu_id,
            },
            step_name="step4/use-type",
            timeout=timeout,
            retry_count=retry_count,
        )
        print_sep()

        # 5) 인원/옵션 입력
        print_step("== 5) 인원/옵션 입력 ==")
        _ = _post_step_text(
            s,
            f"{base}/user/tennis/tennisReservNext3Check.do",
            {
                "adultCnt": str(adult_cnt),
                "youthCnt": str(youth_cnt),
                "oldManCnt": str(oldman_cnt),
                "gCardCnt": str(gcard_cnt),
                "mChildCnt": str(mchild_cnt),
                "useLightYn": use_light,
                "menuId": menu_id,
            },
            step_name="step5/options",
            timeout=timeout,
            retry_count=retry_count,
        )
        print_sep()

        # 6) 결제수단 결정
        print_step(f"== 6) 결제수단 -> ({deal_type}) ==")
        txt6 = _post_step_text(
            s,
            f"{base}/user/tennis/tennisReservNext4Check.do",
            {"deal_type": deal_type, "menuId": menu_id},
            step_name="step6/finalize",
            timeout=timeout,
            retry_count=0,
        )
        print_sep()

        ok, err, err_msg = _is_success_from_step6(txt6)
        if ok:
            print("완료. 각 단계 응답을 확인해 주세요.")
            return 0
        else:
            details: list[str] = []
            if err is not None:
                details.append(f"errCode={err}")
            if err_msg:
                details.append(f"errMsg={err_msg}")
            if not details:
                details.append("응답 파싱 실패")
            print(f"[FAIL] 최종 단계 실패 ({', '.join(details)}).")
            snippet = " ".join((txt6 or "").split())
            if snippet:
                print(f"[FAIL][step6] response={snippet[:240]}")
            return 1

    except requests.RequestException as e:
        print(f"[ERROR] 요청 중 오류: {e}")
        return 1
