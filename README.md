# GMUC Tennis Reservation Scheduler

GMUC는 광명도시공사(GMUC) 테니스 코트를 예약하기 위한 자동화 도구입니다.

## 프로그램 수정 후 루틴

1. oneclick_docker.sh로 이미지 말기
2. push_to_ecr.sh로 이미지 ecr에 올리기
3. 2의 이미지로 ecr에서 새 개정 만들기
4. 3의 개정을 TASK_DEF로 setup_lambda_scheduler.sh 실행하되, 새 개정으로 변경하기

## 실행 모드

### 1. GUI 모드 (로컬)
데스크톱 앱에서 직접 로그인하고 예약을 관리합니다.

### 2. CLI 모드 (로컬/Docker)
캡챠 OCR을 통한 자동 로그인과 예약을 수행합니다.

### 3. AWS 클라우드 모드
Lambda + EventBridge + Fargate를 통한 완전 자동화된 예약 시스템입니다.

---

## 주요 구성 요소

| 파일 | 설명 |
|------|------|
| `gui.py` | PySide6 기반 GUI. 로그인용 웹뷰, 예약 등록/삭제 UI, 로그 뷰어, 스케줄러 제어 |
| `cli.py` | CLI 엔트리포인트. 캡챠 OCR 자동 로그인 + 스케줄러 실행 |
| `scheduler.py` | 백그라운드 스케줄러. 예약 정보를 관리하고 실행 시각에 `reserv.py` 호출 |
| `reserv.py` | 6단계 예약 HTTP 흐름을 수행하는 클라이언트 |
| `schedule_runner.py` | AWS Lambda 핸들러. Fargate 태스크 스케줄링 |
| `ocr/reader.py` | Tesseract 기반 캡챠 OCR |
| `alerts.py` | Telegram 알림 전송 |

---

## 아키텍처

### 로컬 실행
```
사용자 → cli.py → 캡챠 OCR 로그인 → scheduler.py → reserv.py → GMUC 서버
```

### AWS 클라우드 실행
```
EventBridge (매월 15일/말일 09:30 KST)
    ↓
Lambda (schedule_runner.lambda_handler)
    ├─ Parameter Store에서 users/schedule 로드
    └─ ECS run_task 호출
        ↓
Fargate (cli.py)
    ├─ exec_at 30분 전부터 로그인 시도
    ├─ 캡챠 OCR 자동 로그인
    └─ exec_at 시간에 예약 실행
```

---

## 설치

### 로컬 환경

```bash
python -m venv .venv
source .venv/bin/activate  # Windows: .venv\Scripts\activate
pip install -r requirements.txt
```

### Docker

```bash
docker build -t gmuc .
```

### AWS ECR 배포

```bash
# ECR 로그인
REGION=ap-northeast-2 ACCOUNT_ID=123456789012 make ecr-login

# 멀티 아키텍처 빌드 및 푸시
REGION=ap-northeast-2 ACCOUNT_ID=123456789012 make cross-push
```

---

## 사용법

### 1. GUI 모드

```bash
python gui.py
```

1. 접근 코드 입력 (HMAC 기반)
2. "로그인 창 열기"로 GMUC 사이트에서 직접 로그인
3. 예약 추가 후 "실행 시작"

### 2. CLI 모드

```bash
python cli.py \
  --id 사용자ID \
  --password 비밀번호 \
  --reservation "20250118,08:00,10:00,1,2025-01-18 10:00:00" \
  --reservation "20250118,10:00,12:00,2,2025-01-18 10:00:00"
```

#### CLI 옵션

| 옵션 | 설명 |
|------|------|
| `--id` | 로그인 ID |
| `--password` | 로그인 비밀번호 |
| `--cookie` | 이미 확보한 쿠키 (로그인 생략) |
| `--reservation` | 예약 정보: `YYYYMMDD,HH:MM,HH:MM,court_no[,exec_at][,time_base]` |
| `--exec-at` | 예약 실행 시각 기본값 (YYYY-MM-DD HH:MM:SS) |
| `--time-base` | 모든 예약에 적용할 time base (미지정 시 자동 계산) |
| `--tg-token` | Telegram Bot 토큰 |
| `--tg-chat-id` | Telegram chat_id |
| `--debug-http` | HTTP 요청/응답 디버그 출력 |

#### 예약 형식
```
YYYYMMDD,HH:MM,HH:MM,court_no[,exec_at][,time_base]

예시:
20250118,08:00,10:00,1                           # 기본
20250118,08:00,10:00,1,2025-01-18 10:00:00       # exec_at 지정
20250118,08:00,10:00,1,2025-01-18 10:00:00,69    # time_base도 지정
```

### 3. Docker 실행

```bash
docker run --rm gmuc \
  --id 사용자ID \
  --password 비밀번호 \
  --reservation "20250118,08:00,10:00,1,2025-01-18 10:00:00"
```

---

## AWS 클라우드 배포

### 개요

Lambda가 매월 15일과 말일에 자동으로 실행되어 Fargate 태스크를 시작합니다.

- **매월 15일**: 16일~말일 주말 예약 생성
- **매월 말일**: 다음 달 1일~15일 주말 예약 생성

### 1. Lambda 및 EventBridge 설정

```bash
REGION=ap-northeast-2 \
CLUSTER=your-cluster-name \
TASK_DEF=gmuc:1 \
SUBNETS=subnet-xxx,subnet-yyy \
SECURITY_GROUPS=sg-xxx \
TELEGRAM_TOKEN=your-bot-token \
TELEGRAM_CHAT_ID=your-chat-id \
./scripts/setup_lambda_scheduler.sh
```

이 스크립트가 자동으로 수행하는 작업:
- IAM Role/Policy 생성
- Lambda 함수 배포 (`schedule_runner.lambda_handler`)
- EventBridge 규칙 생성 (매월 15일, 말일 09:30 KST)

### 2. Parameter Store 설정

사용자 정보와 스케줄 오버라이드를 Parameter Store에 저장합니다.

#### 사용자 정보 (SecureString)

```bash
aws ssm put-parameter \
  --name '/gmuc/users' \
  --type SecureString \
  --value '[
    {"id": "user1", "password": "pass1"},
    {"id": "user2", "password": "pass2"}
  ]' \
  --region ap-northeast-2
  
aws ssm put-parameter \                                                                                                                                                            
    --name '/gmuc/users' \                                                                             
    --type SecureString \                                                                                                                                                            
    --value "file://user.json" \                                                                                                                                                     
    --overwrite \        
    --region ap-northeast-2
```

#### 스케줄 오버라이드 (선택사항)

```bash
aws ssm put-parameter \
  --name '/gmuc/schedule' \
  --type String \
  --value '{
    "add": [
      {"date": "20250120", "start": "08:00", "end": "10:00", "court": 3}
    ],
    "remove": [
      {"date": "20250118", "start": "06:00", "end": "08:00", "court": 1}
    ]
  }' \
  --region ap-northeast-2
  
aws ssm put-parameter \                   
    --name '/gmuc/schedule' \            
    --type String \
    --value "file://schedule.json" \                                                                                                                                                 
    --overwrite \
    --region ap-northeast-2  
```

### 3. Lambda 환경변수

| 환경변수 | 설명 | 예시 |
|----------|------|------|
| `GMUC_REGION` | AWS 리전 | `ap-northeast-2` |
| `GMUC_CLUSTER` | ECS 클러스터 이름 | `my-cluster` |
| `GMUC_TASK_DEF` | 태스크 정의 | `gmuc:8` |
| `GMUC_SUBNETS` | 서브넷 ID (콤마 구분) | `subnet-xxx,subnet-yyy` |
| `GMUC_SECURITY_GROUPS` | 보안 그룹 ID (콤마 구분) | `sg-xxx` |
| `GMUC_CONTAINER_NAME` | 컨테이너 이름 | `gmuc` (기본값) |
| `GMUC_ASSIGN_PUBLIC_IP` | 퍼블릭 IP 할당 | `ENABLED` (기본값) |
| `SSM_USERS_PARAM` | 사용자 정보 파라미터 키 | `/gmuc/users` (기본값) |
| `SSM_SCHEDULE_PARAM` | 스케줄 파라미터 키 | `/gmuc/schedule` (기본값) |
| `TELEGRAM_TOKEN` | Telegram Bot 토큰 | (선택) |
| `TELEGRAM_CHAT_ID` | Telegram chat_id | (선택) |

### 4. Lambda 테스트

```bash
# 실제 실행 (Fargate 태스크 시작)
aws lambda invoke \
  --function-name gmuc-scheduler \
  --payload '{"run_date":"2025-01-15"}' \
  --cli-binary-format raw-in-base64-out \
  --region ap-northeast-2 \
  /tmp/output.json && cat /tmp/output.json
```

### 5. EventBridge 규칙

| 규칙 이름 | cron 표현식 | 설명 |
|-----------|-------------|------|
| `gmuc-scheduler-15th` | `cron(30 0 15 * ? *)` | 매월 15일 09:30 KST |
| `gmuc-scheduler-last` | `cron(30 0 L * ? *)` | 매월 말일 09:30 KST |

---

## 로컬 테스트 (dry-run)

AWS에 배포하기 전에 로컬에서 Lambda 로직을 테스트할 수 있습니다.

```bash
cd /path/to/gmuc

python3 -c "
from schedule_runner import lambda_handler
import json

result = lambda_handler({
    'dry_run': True,
    'run_date': '2025-01-15'
}, None)

print(json.dumps(result, indent=2, ensure_ascii=False))
"
```

### dry-run 옵션

| 옵션 | 설명 |
|------|------|
| `dry_run: true` | AWS 호출 없이 테스트 |
| `run_date: "2025-01-15"` | 15일 기준 (16~말일 주말 생성) |
| `run_date: "2025-01-31"` | 말일 기준 (다음달 1~15일 주말 생성) |
| `users_file: "user.json"` | 로컬 사용자 파일 경로 |
| `schedule_file: "schedule.json"` | 로컬 스케줄 파일 경로 |

### 출력 예시

```
[dry_run] User: user1 (6개 예약)
  exec_at: 2025-01-15 10:00:00
  - 20250118 07:00-09:00 court 1
  - 20250119 09:00-11:00 court 1
  ...

{
  "statusCode": 200,
  "body": "2개 태스크 (dry_run) 시작 요청 완료",
  "tasks_started": 2,
  "dry_run": true,
  ...
}
```

---

## 예약 시간대

### 동절기 (11월~2월)
| 시작 | 종료 |
|------|------|
| 07:00 | 09:00 |
| 09:00 | 11:00 |
| 11:00 | 13:00 |
| 13:00 | 15:00 |
| 15:00 | 17:00 |
| 17:00 | 19:00 |
| 19:00 | 21:00 |

### 비동절기 (3월~10월)
| 시작 | 종료 |
|------|------|
| 06:00 | 08:00 |
| 08:00 | 10:00 |
| 10:00 | 12:00 |
| 12:00 | 14:00 |
| 14:00 | 16:00 |
| 16:00 | 18:00 |
| 18:00 | 20:00 |
| 20:00 | 22:00 |

---

## 예약 코드 계산

### timeCode
- 월별 운영 시간표의 첫 슬롯 TIME_CODE를 base로 사용
- 이후 2시간 블록마다 +1 증가
- 형식: `TM{base + slot_index:03d}` (예: `TM069`)

### courtCode
- 코트 번호 N → `TC{N:03d}` (예: 1번 코트 → `TC001`)

---

## 파일 구조

```
gmuc/
├── cli.py                  # CLI 엔트리포인트
├── gui.py                  # GUI 앱
├── scheduler.py            # 예약 스케줄러
├── reserv.py               # 6단계 예약 HTTP 클라이언트
├── schedule_runner.py      # Lambda 핸들러
├── alerts.py               # Telegram 알림
├── ocr/
│   └── reader.py           # 캡챠 OCR
├── scripts/
│   ├── setup_lambda_scheduler.sh  # Lambda/EventBridge 설정
│   ├── install_make_and_docker.sh
│   └── push_to_ecr.sh
├── Dockerfile              # Docker 이미지 빌드
├── Makefile                # ECR 배포
├── requirements.txt        # Python 의존성
├── user.json               # 사용자 정보 (로컬용)
├── schedule.json           # 스케줄 오버라이드 (로컬용)
└── GMUC.spec               # PyInstaller 설정
```

---

## 트러블슈팅

### 캡챠 OCR 실패
- Tesseract가 설치되어 있는지 확인: `tesseract --version`
- Docker 이미지에는 Tesseract가 포함되어 있음

### 로그인 실패
- `--debug-http` 옵션으로 요청/응답 확인
- Telegram 알림 설정 시 exec_at 10분 전부터 경고 발송

### Lambda 타임아웃
- 기본 타임아웃: 60초
- Fargate 태스크 시작만 하므로 충분함

### 예약 요청 타임아웃
- `reserv.py`의 HTTP 타임아웃: 무기한 (`timeout=None`)
- 서버 응답이 느릴 때 대기

---

## 커스터마이징

### 예약 파라미터 변경
`reserv.run_reservation()` 함수의 기본값:
- `deal_type`: `"CARD"` (결제 방식)
- `adult_cnt`: `4` (성인 인원)
- `use_type_cd`: `"002"` (연습이용)

### 로그 처리
- GUI: Qt 시그널을 통해 로그 패널에 표시
- CLI: stdout으로 출력
- Lambda: CloudWatch Logs에 기록

### Telegram 알림
- 로그인 실패 경고 (exec_at 10분 전)
- 환경변수 또는 CLI 옵션으로 설정
