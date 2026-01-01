#!/usr/bin/env python3
"""
GMUC CLI
- Performs login via HTTP (JSESSIONID fetch -> captcha OCR -> login POST).
- Builds reservations (similar to GUI inputs) and hands them to the scheduler.

Notes:
- Login success detection is left as TODO; the loop will retry up to --max-login-attempts
  but does not yet parse the response to confirm success/failure.
"""

from __future__ import annotations

import argparse
import base64
import getpass
import re
import random
import sys
import time
import urllib.parse
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import List, Optional, Tuple

import requests

import scheduler
from ocr.reader import extract_code_from_base64_png


BASE_URL = "https://reserve.gmuc.co.kr"
UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36"
)


def _build_session() -> requests.Session:
    s = requests.Session()
    s.headers.update(
        {
            "User-Agent": UA,
            "accept-language": "ko-KR,ko;q=0.9,en-US;q=0.8,en;q=0.7",
            "accept-encoding": "gzip, deflate, br, zstd",
            "connection": "keep-alive",
        }
    )
    return s


def _get_jsessionid_from_response(resp: requests.Response) -> Optional[str]:
    cookie = resp.cookies.get("JSESSIONID")
    if cookie:
        return cookie
    set_cookie = resp.headers.get("Set-Cookie", "")
    m = re.search(r"JSESSIONID=([^;]+)", set_cookie)
    if m:
        return m.group(1)
    return None


def _normalize_cookie(jsessionid: str) -> str:
    value = jsessionid.strip()
    if value.startswith("JSESSIONID="):
        return value
    return f"JSESSIONID={value}"


def fetch_initial_jsessionid(session: requests.Session, base_url: str = BASE_URL) -> str:
    resp = session.get(base_url)
    jsid = _get_jsessionid_from_response(resp)
    if not jsid:
        raise RuntimeError("초기 JSESSIONID를 획득하지 못했습니다.")
    return jsid


def request_login_page(session: requests.Session, jsessionid: str, base_url: str = BASE_URL) -> None:
    headers = {
        "accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8,application/signed-exchange;v=b3;q=0.7",
        "sec-fetch-dest": "document",
        "sec-fetch-mode": "navigate",
        "sec-fetch-site": "same-origin",
        "sec-fetch-user": "?1",
        "upgrade-insecure-requests": "1",
        "referer": f"{base_url}/",
        "cookie": _normalize_cookie(jsessionid),
    }
    session.get(f"{base_url}/user/login/login.do", headers=headers)


def fetch_captcha_text(session: requests.Session, jsessionid: str, base_url: str = BASE_URL, use_cache_buster: bool = False) -> Tuple[str, str]:
    """Return (captcha_text, base64_png). Adds ?ran=... when use_cache_buster=True to bypass cache."""
    headers = {
        "accept": "image/avif,image/webp,image/apng,image/svg+xml,image/*,*/*;q=0.8",
        "sec-fetch-dest": "image",
        "sec-fetch-mode": "no-cors",
        "sec-fetch-site": "same-origin",
        "referer": f"{base_url}/user/login/login.do",
        "cookie": _normalize_cookie(jsessionid),
    }
    url = f"{base_url}/captcha"
    if use_cache_buster:
        url = f"{url}?ran={random.random()}"
    resp = session.get(url, headers=headers)
    resp.raise_for_status()

    b64_data = base64.b64encode(resp.content).decode("ascii")
    text = extract_code_from_base64_png(b64_data)
    return text, b64_data


def encode_password_for_login(password: str) -> str:
    """
    requests가 form-urlencode를 수행하므로 비밀번호는 원문 그대로 둔다.
    사전 인코딩하면 % 기호가 다시 인코딩되어 잘못 전달될 수 있다.
    """
    return password


def perform_login_request(
    session: requests.Session,
    jsessionid: str,
    user_id: str,
    encoded_password: str,
    captcha_text: str,
    base_url: str = BASE_URL,
    login_fail_cnt: str = "",
) -> requests.Response:
    headers = {
        "accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8,application/signed-exchange;v=b3;q=0.7",
        "cache-control": "no-cache",
        "pragma": "no-cache",
        "content-type": "application/x-www-form-urlencoded",
        "origin": base_url,
        "referer": f"{base_url}/user/login/login.do",
        "sec-fetch-dest": "document",
        "sec-fetch-mode": "navigate",
        "sec-fetch-site": "same-origin",
        "sec-fetch-user": "?1",
        "upgrade-insecure-requests": "1",
        "sec-ch-ua": '"Not=A?Brand";v="24", "Chromium";v="140"',
        "sec-ch-ua-mobile": "?0",
        "sec-ch-ua-platform": '"macOS"',
        "cookie": _normalize_cookie(jsessionid),
    }
    data = {
        "LOGIN_FAIL_CNT": login_fail_cnt,
        "menuFlag": "",
        "txtID": user_id,
        "txtPass": encoded_password,
        "txtCaptcha": captcha_text,
    }
    resp = session.post(
        f"{base_url}/user/login/loginProc.do",
        headers=headers,
        data=data,
        allow_redirects=False,  # 성공 시 302 Location을 직접 관찰하기 위해 리다이렉트 따라가지 않음
    )
    return resp


def _extract_login_fail_cnt(resp_text: str) -> Optional[str]:
    m = re.search(r'name=["\']LOGIN_FAIL_CNT["\']\s+value=["\']?([^"\'>\s]*)', resp_text, re.IGNORECASE)
    if m:
        return m.group(1)
    return None


def _extract_alert_messages(resp_text: str) -> List[str]:
    """login 페이지에 포함된 alert(\"...\") 메시지 추출."""
    return re.findall(r'alert\(["\']([^"\']+)["\']\)', resp_text or "", re.IGNORECASE)


def _decide_login_success(resp: requests.Response) -> Optional[bool]:
    """
    휴리스틱:
    - 30x 리다이렉트이고 Location이 main.do를 향하면 성공
    - 응답이 비어 있으면 성공으로 간주 (사이트 특성)
    - 로그인 페이지 HTML 단서(예: title, captcha_image, txtID) 포함 시 실패
    - 나머지는 성공으로 간주
    """
    if resp is not None and resp.status_code in (301, 302, 303, 307, 308):
        loc = (resp.headers.get("Location") or "").lower()
        if "main.do" in loc or "/main" in loc:
            return True
        if "login" in loc:
            return False

    resp_text = resp.text if resp is not None else ""
    stripped = (resp_text or "").strip()
    if stripped == "":
        return True
    lower = stripped.lower()
    fail_cnt = _extract_login_fail_cnt(resp_text)
    if fail_cnt and fail_cnt not in ("", "0"):
        return False
    for msg in _extract_alert_messages(resp_text):
        if "비밀번호 변경" in msg or "본인 인증" in msg or "홈페이지" in msg:
            return False
    if "<html" in lower or "captcha_image" in lower or "txtid" in lower or "광명도시공사" in resp_text:
        return False
    if "login_fail_cnt" in lower or "보안문자가 정확하지 않습니다" in resp_text:
        return False
    return True


def _mask_secret(value: str) -> str:
    if value is None:
        return ""
    if len(value) <= 2:
        return "*" * len(value)
    return value[0] + "*" * (len(value) - 2) + value[-1]


def _debug_dump_http(resp: requests.Response, label: str = "login"):
    try:
        req = resp.request
    except Exception:
        req = None

    print(f"[debug] ===== {label} request =====")
    if req:
        print(f"[debug] {req.method} {req.url}")
        for k, v in req.headers.items():
            print(f"[debug] req.hdr {k}: {v}")
        body = req.body
        if body:
            try:
                body_str = body.decode() if isinstance(body, (bytes, bytearray)) else str(body)
                parsed = urllib.parse.parse_qsl(body_str, keep_blank_values=True)
                for k, v in parsed:
                    if k.lower() == "txtpass":
                        v_disp = _mask_secret(v)
                    else:
                        v_disp = v
                    print(f"[debug] req.body {k}: {v_disp}")
            except Exception as e:
                print(f"[debug] req.body raw (decode error {e}): {body!r}")
    else:
        print("[debug] (no request object)")

    print(f"[debug] ===== {label} response =====")
    print(f"[debug] status: {resp.status_code}")
    print(f"[debug] resp.hdr Location: {resp.headers.get('Location')}")
    print(f"[debug] resp.hdr Set-Cookie: {resp.headers.get('Set-Cookie')}")
    fail_cnt = _extract_login_fail_cnt(resp.text or "")
    if fail_cnt is not None:
        print(f"[debug] parsed LOGIN_FAIL_CNT from body: '{fail_cnt}'")
    alerts = _extract_alert_messages(resp.text or "")
    if alerts:
        print(f"[debug] found alert messages: {alerts}")
    snippet = (resp.text or "")[:600]
    print(f"[debug] resp.text (first 600 chars):\n{snippet}")


def login_with_captcha(
    user_id: str,
    password: str,
    base_url: str = BASE_URL,
    debug_http: bool = False,
    login_fail_cnt: str = "",
) -> Tuple[str, requests.Session, str, bool]:
    """
    로그인 성공 시까지 캡챠 OCR을 반복합니다. Returns (cookie_string, session, last_response_text, login_ok).
    """
    session = _build_session()
    jsessionid = fetch_initial_jsessionid(session, base_url)
    print(f"[login] 초기 JSESSIONID 획득: {jsessionid}")

    request_login_page(session, jsessionid, base_url)

    encoded_pw = encode_password_for_login(password)
    last_resp_text = ""
    login_ok = False

    attempt = 1
    while True:
        print(f"[login] 시도 {attempt} - 캡챠 요청 및 OCR")
        captcha_text, _ = fetch_captcha_text(session, jsessionid, base_url, use_cache_buster=True)
        print(f"[login] OCR 추출 캡챠: {captcha_text}")

        if not captcha_text or len(captcha_text) != 5:
            print("[login] 캡챠 결과가 5자가 아님. 새 캡챠로 즉시 재시도.")
            attempt += 1
            time.sleep(0.3)
            continue

        resp = perform_login_request(
            session,
            jsessionid,
            user_id,
            encoded_pw,
            captcha_text,
            base_url,
            login_fail_cnt=login_fail_cnt,
        )
        last_resp_text = resp.text
        set_cookie_hdr = resp.headers.get("Set-Cookie")
        latest_jsid = session.cookies.get("JSESSIONID")
        print(f"[debug] login Set-Cookie 헤더: {set_cookie_hdr}")
        print(f"[debug] 세션 쿠키 JSESSIONID: {latest_jsid}")
        if debug_http:
            _debug_dump_http(resp, label=f"attempt_{attempt}")

        verdict = _decide_login_success(resp)
        if verdict is True:
            print("[login] 성공으로 판단")
            login_ok = True
            break
        if verdict is False:
            print("[login] 실패로 판단. 새로운 캡챠로 즉시 재시도.")
        else:
            print("[login] 성공/실패 판단 불가. 즉시 재시도.")

        time.sleep(0.3)
        attempt += 1

    # 최신 JSESSIONID 다시 확인 (변경될 수 있음)
    jsessionid_latest = session.cookies.get("JSESSIONID") or jsessionid
    cookie_str = _normalize_cookie(jsessionid_latest)
    print(f"[login] 최종 쿠키: {cookie_str}")
    if not login_ok:
        print("[login][warn] 로그인에 성공하지 못했습니다. 응답을 참고하세요.")
    return cookie_str, session, last_resp_text, login_ok


def _parse_datetime(text: str) -> datetime:
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S"):
        try:
            return datetime.strptime(text, fmt)
        except ValueError:
            continue
    raise ValueError(f"지원하지 않는 날짜/시간 형식: {text}")


@dataclass
class ReservationSpec:
    reserv_date: str
    from_time: str
    to_time: str
    court_no: int
    exec_at: datetime
    time_base: Optional[int] = None


def parse_reservation_arg(arg: str, default_exec_at: datetime) -> ReservationSpec:
    """
    Parse reservation string: YYYYMMDD,HH:MM,HH:MM,court_no[,exec_at][,time_base]
    exec_at format: YYYY-MM-DD HH:MM:SS or YYYY-MM-DDTHH:MM:SS
    """
    parts = [p.strip() for p in arg.split(",") if p.strip()]
    if len(parts) < 4:
        raise ValueError("예약 인자는 'YYYYMMDD,HH:MM,HH:MM,court_no[,exec_at][,time_base]' 형식이어야 합니다.")

    reserv_date, from_time, to_time, court_raw = parts[:4]
    exec_at = default_exec_at
    time_base: Optional[int] = None

    if len(parts) >= 5:
        exec_at = _parse_datetime(parts[4])
    if len(parts) >= 6:
        time_base = int(parts[5])

    return ReservationSpec(
        reserv_date=reserv_date,
        from_time=from_time,
        to_time=to_time,
        court_no=int(court_raw),
        exec_at=exec_at,
        time_base=time_base,
    )


def main():
    parser = argparse.ArgumentParser(description="GMUC CLI (captcha OCR + scheduler)")
    parser.add_argument("--id", dest="user_id", help="로그인 ID (미지정 시 프롬프트)")
    parser.add_argument("--password", dest="password", help="로그인 비밀번호 (미지정 시 프롬프트/숨김 입력)")
    parser.add_argument("--cookie", help="이미 확보한 쿠키(JSESSIONID=...)가 있으면 로그인 생략")
    parser.add_argument("--login-fail-cnt", dest="login_fail_cnt", default="", help="LOGIN_FAIL_CNT 값(기본 빈 문자열, 필요 시 1 등으로 지정)")
    parser.add_argument(
        "--reservation",
        action="append",
        nargs="+",
        dest="reservations",
        help="예약 정보. 형식: YYYYMMDD,HH:MM,HH:MM,court_no[,exec_at][,time_base] (공백 포함 시 따옴표로 묶거나 그대로 입력하면 자동 결합)",
    )
    parser.add_argument(
        "--exec-at",
        dest="default_exec_at",
        help="예약 실행 시각 기본값 (YYYY-MM-DD HH:MM:SS). 개별 예약에서 재정의 가능.",
    )
    parser.add_argument(
        "--time-base",
        dest="override_time_base",
        type=int,
        help="모든 예약에 적용할 time base. 미지정 시 자동 계산.",
    )
    parser.add_argument("--base-url", dest="base_url", default=BASE_URL, help="기본 요청 대상 URL")
    parser.add_argument("--debug-http", action="store_true", help="로그인 요청/응답 헤더와 응답 텍스트 일부를 출력")

    args = parser.parse_args()

    default_exec_at = datetime.now()
    if args.default_exec_at:
        default_exec_at = _parse_datetime(args.default_exec_at)

    if not args.reservations:
        sys.exit("최소 1개 이상의 --reservation 인자가 필요합니다.")

    # args.reservations is List[List[str]] because of nargs="+"
    raw_entries = [" ".join(parts) for parts in args.reservations]

    specs: List[ReservationSpec] = []
    for raw in raw_entries:
        spec = parse_reservation_arg(raw, default_exec_at)
        if args.override_time_base is not None:
            spec.time_base = args.override_time_base
        specs.append(spec)

    earliest_exec = min(spec.exec_at for spec in specs)
    prelogin_start = earliest_exec - timedelta(minutes=15)

    cookie_str = args.cookie
    session = None
    if not cookie_str:
        user_id = args.user_id or input("ID: ").strip()
        password = args.password or getpass.getpass("Password: ")
        if not user_id or not password:
            sys.exit("ID와 Password가 모두 필요합니다.")

        now = datetime.now()
        if now < prelogin_start:
            wait_sec = (prelogin_start - now).total_seconds()
            print(f"[login] execAt 15분 전까지 {wait_sec/60:.1f}분 대기 후 로그인 시도 시작")
            time.sleep(wait_sec)

        cookie_str, session, last_resp, login_ok = login_with_captcha(
            user_id,
            password,
            args.base_url,
            debug_http=args.debug_http,
            login_fail_cnt=args.login_fail_cnt,
        )
        if last_resp and "<html" in last_resp:
            print("[login][debug] 마지막 응답 본문 첫 400자:\n" + last_resp[:400])
        if not login_ok:
            sys.exit("[login] 로그인 성공하지 못했습니다. 종료합니다.")
    else:
        print("[info] 제공된 쿠키를 사용합니다 (로그인 스킵)")

    manager = scheduler.ReservationManager(log_callback=lambda msg: print(f"[scheduler] {msg}"))
    manager.set_cookie(cookie_str)

    for spec in specs:
        r = manager.create_reservation(
            reservDate=spec.reserv_date,
            fromTime=spec.from_time,
            toTime=spec.to_time,
            courtNo=spec.court_no,
            exec_at=spec.exec_at,
            timeBaseOverride=spec.time_base,
        )
        print(
            f"[enqueue] id={r.id} {r.reservDate} {r.fromTime}-{r.toTime} "
            f"court {r.courtNo} timeCode={r.timeCode} base={r.timeBase}"
        )

    manager.start()
    print("[info] 스케줄러 가동. Ctrl+C로 종료할 수 있습니다.")

    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        print("\n[info] 종료 요청을 받았습니다. 백그라운드 스레드는 데몬으로 자동 종료됩니다.")


if __name__ == "__main__":
    main()
