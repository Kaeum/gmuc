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

from typing import Dict, Optional
import json

import requests


BASE_DEFAULT = "https://reserve.gmuc.co.kr"
UA_DEFAULT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/140.0.0.0 Safari/537.36"
)


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


def post_text(s: requests.Session, url: str, data: Dict[str, str]) -> str:
    r = s.post(url, data=data, timeout=15)
    if not r.encoding:
        r.encoding = "utf-8"
    return r.text


def _is_success_from_step6(text: str) -> tuple[bool, str | None]:
    """Parse step6 response text and decide success by errCode.

    Returns (is_success, err_code).
    """
    try:
        data = json.loads(text)
        err = data.get("errCode") if isinstance(data, dict) else None
        if err is None and isinstance(data, str):
            # Some APIs may return a JSON string; try second parse
            data2 = json.loads(data)
            if isinstance(data2, dict):
                err = data2.get("errCode")
        return (str(err) == "0", None if err is None else str(err))
    except Exception:
        return (False, None)


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
    max_retries: int = 5,
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

    base = base.rstrip("/")
    s = build_session(base, cookie, referer=referer, ua=ua)

    max_retries = max(1, min(5, int(max_retries)))

    last_err_code: str | None = None

    for attempt in range(1, max_retries + 1):
        try:
            print_step(f"== 예약 시도 {attempt}/{max_retries} ==")
            print_sep()

            # 1) 날짜 가능 여부 체크
            print_step("== 1) 날짜 가능 여부 체크 ==")
            txt = post_text(
                s,
                f"{base}/user/tennis/tennisReservDayCheck.do",
                {"reservDate": reserv_date},
            )
            print(txt)
            print_sep()

            # 2) 시간대 선택 검증
            print_step("== 2) 시간대 선택 검증 ==")
            if not (time_code and from_time and to_time):
                print("[WARN] time_code/from_time/to_time 중 일부가 비어 있음")
            txt = post_text(
                s,
                f"{base}/user/tennis/tennisReservNext0Check.do",
                {
                    "timeCode": time_code or "",
                    "fromTime": from_time or "",
                    "toTime": to_time or "",
                    "menuId": menu_id,
                },
            )
            print(txt)
            print_sep()

            # 3) 코트 선택 검증
            print_step("== 3) 코트 선택 검증 ==")
            if not (court_code and court_no is not None):
                print("[WARN] court_code/court_no 중 일부가 비어 있음")
            txt = post_text(
                s,
                f"{base}/user/tennis/tennisReservNext1Check.do",
                {
                    "courtCode": court_code or "",
                    "courtNo": str(court_no) if court_no is not None else "",
                    "menuId": menu_id,
                },
            )
            print(txt)
            print_sep()

            # 4) 이용유형 선택
            print_step("== 4) 이용유형 선택 ==")
            txt = post_text(
                s,
                f"{base}/user/tennis/tennisReservNext2Check.do",
                {
                    "useTypeCd": use_type_cd,
                    "useTypeNm": use_type_nm,
                    "menuId": menu_id,
                },
            )
            print(txt)
            print_sep()

            # 5) 인원/옵션 입력
            print_step("== 5) 인원/옵션 입력 ==")
            txt = post_text(
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
            )
            print(txt)
            print_sep()

            # 6) 결제수단 결정
            print_step(f"== 6) 결제수단 -> ({deal_type}) ==")
            txt6 = post_text(
                s,
                f"{base}/user/tennis/tennisReservNext4Check.do",
                {"deal_type": deal_type, "menuId": menu_id},
            )
            print(txt6)
            print_sep()

            ok, err = _is_success_from_step6(txt6)
            last_err_code = err
            if ok:
                print("완료. 각 단계 응답을 확인해 주세요.")
                return 0

            # 실패 시 재시도 안내
            if attempt < max_retries:
                msg_err = f"errCode={err}" if err is not None else "응답 파싱 실패"
                print(f"[INFO] 최종 단계 실패({msg_err}). 재시도합니다... ({attempt}/{max_retries})")
                print_sep()
                continue
            else:
                break

        except requests.RequestException as e:
            # 네트워크/HTTP 오류는 재시도
            if attempt < max_retries:
                print(f"[WARN] 요청 중 오류: {e}. 재시도합니다... ({attempt}/{max_retries})")
                print_sep()
                continue
            else:
                print(f"[ERROR] 요청 중 오류: {e}")
                break

    # 모든 재시도 실패
    if last_err_code is not None:
        print(f"[FAIL] 모든 시도 실패 (최종 errCode={last_err_code}).")
    else:
        print("[FAIL] 모든 시도 실패 (성공 응답을 확인하지 못함).")
    return 1
