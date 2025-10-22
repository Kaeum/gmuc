# GMUC Tennis Reservation Scheduler

GMUC는 군포시민체육 광명운동장(GMUC) 테니스 코트를 예약하기 위한 데스크톱 자동화 도구입니다.  
사용자는 내장된 웹뷰에서 직접 로그인해 세션 쿠키를 확보하고, 원하는 예약 일시와 코트를 등록한 뒤 스케줄러를 실행하여 지정한 순간에 예약 요청을 자동으로 전송할 수 있습니다.

## 주요 구성 요소
- `gui.py`: PySide6 기반 GUI. 로그인용 웹뷰, 예약 등록/삭제 UI, 로그 뷰어, 스케줄러 제어를 제공합니다.
- `scheduler.py`: 백그라운드 스케줄러. 예약 정보를 관리하고 실행 시각이 도래하면 `reserv.py`를 호출합니다.
- `reserv.py`: `requests`를 사용해 GMUC 예약 시스템의 6단계 HTTP 흐름을 자동 수행하는 클라이언트.
- `GMUC.spec`: PyInstaller 번들링 설정. macOS 앱 번들을 포함한 패키징에 사용됩니다.

## 동작 흐름
1. **접근 코드 확인** – 앱 실행 시 매월 변경되는 HMAC 코드(비밀 키와 `YYYYMM` 기준)를 3회 이내에 입력해야 GUI가 열린다.
2. **사용자 로그인** – “로그인 창 열기” 버튼으로 GMUC 예약 사이트를 내장 브라우저에 띄워 직접 로그인하면 `JSESSIONID` 쿠키가 자동 감지되어 저장된다.
3. **예약 등록** – 날짜, 2시간 단위의 시간 블록, 코트 번호, (선택) time base, 실행 시각을 지정해 여러 건의 예약을 큐에 추가할 수 있다. (11~2월 동절기는 07:00~21:00, 그 외 달은 06:00~22:00 슬롯이 자동 제안된다.)
4. **스케줄링** – “실행 시작” 버튼을 누르면 백그라운드 스레드가 활성화되어 각 예약의 실행 시간이 되면 순차적으로 `reserv.run_reservation()`을 호출한다.
5. **예약 실행** – `reserv.py`는 각 단계의 응답을 로그로 남기면서 최대 5회까지 재시도하며, 최종 errCode가 `0`이면 성공으로 간주한다.

## 설치 요구 사항
- Python 3.10 이상 권장
- 필수 패키지: `PySide6` (WebEngine 포함), `requests`
- 추가 도구(선택): `pyinstaller` – 독립 실행 파일을 만들 때 사용

```bash
python -m venv .venv
source .venv/bin/activate  # Windows: .venv\Scripts\activate
pip install PySide6 requests
# 번들링이 필요하다면
pip install pyinstaller
```

## 실행 방법
1. 환경 변수 `QTWEBENGINE_DISABLE_SANDBOX=1`이 자동으로 설정되므로 별도 조치 없이 `python gui.py`를 실행할 수 있습니다.
2. 프로그램이 시작되면 이번 달의 접근 코드를 입력합니다. (코드는 `APP_SECRET`과 `YYYYMM`을 SHA-256 HMAC 한 값을 소문자 hex로 입력.)
3. GUI에서 로그인 → 예약 추가 → “실행 시작” 순으로 작업하십시오.
4. 로그 패널에 스케줄러 및 HTTP 단계 로그가 실시간으로 표시됩니다.

## 예약 옵션 참고
- **Time Base**: 빈칸이면 예약일 기준으로 10월 `base=69`에서 이후 각 월의 슬롯 수(8 또는 7)를 누적한 값을 사용합니다. 특정 값을 지정하려면 숫자를 직접 입력하세요.
- **timeCode 계산**: 월별 운영 시간표에 맞는 2시간 블록을 선택해야 하며, `TM0XX` 형태로 변환됩니다. (동절기 07:00~21:00, 비동절기 06:00~22:00)
- **courtCode 계산**: 코트 번호 `N`은 `TC{N:03d}`로 매핑됩니다.
- **재시도**: `reserv.py`는 최대 5회까지 자동 재시도하며, 네트워크 오류나 `errCode != 0` 응답 시 재시도 로그를 남깁니다.

## 번들링(선택 사항)
PyInstaller를 사용해 독립 실행 파일 또는 macOS 앱 번들을 만들 수 있습니다.

```bash
pyinstaller GMUC.spec
```

결과물은 `dist/GMUC` (실행 파일)과 `dist/GMUC.app`에 생성되며, `build/` 폴더에는 중간 산출물이 저장됩니다.

## 커스터마이징 힌트
- **APP_SECRET 변경**: `gui.py`의 `APP_SECRET` 상수를 수정해 접근 코드 비밀 키를 교체할 수 있습니다.
- **로그 처리**: GUI 로그는 Qt 시그널을 통해 전달되므로, 필요 시 `LogBridge`를 확장하거나 별도 파일 로깅을 추가할 수 있습니다.
- **예약 파라미터**: `reserv.run_reservation` 함수의 기본 인자(`deal_type`, `adult_cnt` 등)는 호출부에서 원하는 값으로 오버라이드 가능합니다.
