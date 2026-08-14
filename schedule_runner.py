#!/usr/bin/env python3
"""
Generate GMUC reservation tasks for Fargate.

동작 요약
- 오늘 날짜(KST)를 기준으로 매월 15일/말일에 실행한다고 가정한다.
- 15일 실행: 같은 달 16일~말일 사이의 토/일요일 슬롯을 생성.
- 말일 실행: 다음 달 1~15일 사이의 토/일요일 슬롯을 생성.
- 슬롯: 3~10월 -> 06-08, 08-10, 10-12 / 11~2월 -> 07-09, 09-11
- 코트: 1, 2
- Parameter Store의 /gmuc/users, /gmuc/schedule로 설정 관리.
- user.json의 계정에 배정하며, 같은 날짜에 같은 계정은 피하려 시도(불가 시 재사용).
- Lambda 핸들러 또는 CLI로 실행 가능.

Lambda 환경변수:
- GMUC_REGION: AWS 리전 (예: ap-northeast-2)
- GMUC_CLUSTER: ECS 클러스터 이름
- GMUC_TASK_DEF: 태스크 정의 (예: gmuc:1)
- GMUC_SUBNETS: 서브넷 ID들 (콤마 구분)
- GMUC_SECURITY_GROUPS: 보안 그룹 ID들 (콤마 구분)
- GMUC_CONTAINER_NAME: 컨테이너 이름 (기본값: gmuc)
- GMUC_ASSIGN_PUBLIC_IP: ENABLED 또는 DISABLED (기본값: ENABLED)
- SSM_USERS_PARAM: 사용자 정보 Parameter Store 키 (기본값: /gmuc/users)
- SSM_SCHEDULE_PARAM: 스케줄 오버라이드 Parameter Store 키 (기본값: /gmuc/schedule)
"""

from __future__ import annotations

import argparse
import json
import os
import shlex
import subprocess
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from typing import Any, Dict, Iterable, List, Optional, Sequence
from calendar import monthrange

try:
    import boto3
    from botocore.exceptions import ClientError
except ImportError:
    boto3 = None  # type: ignore
    ClientError = Exception  # type: ignore

try:
    from zoneinfo import ZoneInfo
except Exception:  # pragma: no cover
    ZoneInfo = None  # type: ignore

KST = ZoneInfo("Asia/Seoul") if ZoneInfo else None
WINTER_MONTHS = {11, 12, 1, 2}

# Parameter Store 기본 키
DEFAULT_SSM_USERS_PARAM = "/gmuc/users"
DEFAULT_SSM_SCHEDULE_PARAM = "/gmuc/schedule"


@dataclass(frozen=True)
class Reservation:
    reserv_date: str  # YYYYMMDD
    start: str        # HH:MM
    end: str          # HH:MM
    court: int

    def key(self) -> tuple:
        return (self.reserv_date, self.start, self.end, self.court)


@dataclass
class User:
    user_id: str
    password: str


@dataclass
class AWSConfig:
    region: str
    cluster: str
    task_def: str
    subnets: List[str]
    security_groups: List[str]
    container_name: str = "gmuc"
    assign_public_ip: str = "ENABLED"  # or DISABLED


def load_users(path: str) -> List[User]:
    """파일에서 사용자 목록 로드 (CLI용)"""
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    return _parse_users(data)


def _parse_users(data: List[Dict[str, Any]]) -> List[User]:
    """JSON 데이터에서 User 객체 리스트 생성"""
    users: List[User] = []
    for entry in data:
        if "id" not in entry or "password" not in entry:
            raise ValueError(f"user entry missing id/password: {entry}")
        users.append(User(user_id=entry["id"], password=entry["password"]))
    if not users:
        raise ValueError("user list is empty")
    return users


def load_users_from_ssm(param_name: str, region: Optional[str] = None) -> List[User]:
    """Parameter Store (SecureString)에서 사용자 목록 로드"""
    if not boto3:
        raise RuntimeError("boto3가 필요합니다: pip install boto3")
    ssm = boto3.client("ssm", region_name=region) if region else boto3.client("ssm")
    try:
        resp = ssm.get_parameter(Name=param_name, WithDecryption=True)
        data = json.loads(resp["Parameter"]["Value"])
        return _parse_users(data)
    except ClientError as e:
        raise RuntimeError(f"Parameter Store에서 {param_name} 로드 실패: {e}") from e


def load_schedule_overrides(path: Optional[str]) -> tuple[List[Reservation], List[Reservation]]:
    """파일에서 스케줄 오버라이드 로드 (CLI용)"""
    if not path or not os.path.exists(path):
        return [], []
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    return _parse_schedule_overrides(data)


def _parse_schedule_overrides(data: Dict[str, Any]) -> tuple[List[Reservation], List[Reservation]]:
    """JSON 데이터에서 add/remove 리스트 생성"""
    add_list = [
        Reservation(
            reserv_date=item["date"],
            start=item["start"],
            end=item["end"],
            court=int(item["court"]),
        )
        for item in data.get("add", [])
    ]
    remove_list = [
        Reservation(
            reserv_date=item["date"],
            start=item["start"],
            end=item["end"],
            court=int(item["court"]),
        )
        for item in data.get("remove", [])
    ]
    return add_list, remove_list


def load_schedule_overrides_from_ssm(param_name: str, region: Optional[str] = None) -> tuple[List[Reservation], List[Reservation]]:
    """Parameter Store에서 스케줄 오버라이드 로드"""
    if not boto3:
        raise RuntimeError("boto3가 필요합니다: pip install boto3")
    ssm = boto3.client("ssm", region_name=region) if region else boto3.client("ssm")
    try:
        resp = ssm.get_parameter(Name=param_name, WithDecryption=False)
        data = json.loads(resp["Parameter"]["Value"])
        return _parse_schedule_overrides(data)
    except ssm.exceptions.ParameterNotFound:
        # 스케줄 오버라이드가 없으면 빈 리스트 반환
        return [], []
    except ClientError as e:
        raise RuntimeError(f"Parameter Store에서 {param_name} 로드 실패: {e}") from e


def is_weekend(dt: date) -> bool:
    return dt.weekday() >= 5  # 5=Sat, 6=Sun


def month_slots(month: int) -> List[tuple[str, str]]:
    if month in WINTER_MONTHS:
        return [("07:00", "09:00"), ("09:00", "11:00")]
    return [("06:00", "08:00"), ("08:00", "10:00")]


def iter_dates(start: date, end: date) -> Iterable[date]:
    cur = start
    while cur <= end:
        yield cur
        cur += timedelta(days=1)


def generate_default_reservations(today: date) -> List[Reservation]:
    """
    today가 15일이면 현재 달 16~말일 주말, 오늘이 말일이면 다음 달 1~15 주말 생성.
    그 외 날짜엔 빈 리스트를 반환.
    """
    year = today.year
    month = today.month
    # 오늘이 15일인지 말일인지 판단
    next_day = today + timedelta(days=1)
    is_last = next_day.month != month

    targets: List[Reservation] = []
    if today.day == 15:
        start_dt = date(year, month, 16)
        last_dom = monthrange(year, month)[1]
        end_dt = date(year, month, last_dom)
    elif is_last:
        # 다음 달 1~15
        nm_year = year + (1 if month == 12 else 0)
        nm_month = 1 if month == 12 else month + 1
        start_dt = date(nm_year, nm_month, 1)
        end_dt = date(nm_year, nm_month, 15)
    else:
        return []

    for dt in iter_dates(start_dt, end_dt):
        if not is_weekend(dt):
            continue
        slots = month_slots(dt.month)
        for start, end in slots:
            for court in (1, 2):
                targets.append(
                    Reservation(
                        reserv_date=dt.strftime("%Y%m%d"),
                        start=start,
                        end=end,
                        court=court,
                    )
                )
    return targets


def apply_overrides(base: List[Reservation], add_list: List[Reservation], remove_list: List[Reservation]) -> List[Reservation]:
    result = {r.key(): r for r in base}
    for r in add_list:
        result[r.key()] = r
    for r in remove_list:
        result.pop(r.key(), None)
    return sorted(result.values(), key=lambda r: (r.reserv_date, r.start, r.court))


def assign_users(reservations: Sequence[Reservation], users: Sequence[User]) -> dict[str, List[Reservation]]:
    """
    같은 날짜에 동일 계정 중복을 피하려 시도. 불가하면 순환 재사용.
    반환: user_id -> 예약 리스트
    """
    if not users:
        raise ValueError("no users available")
    assignments: dict[str, List[Reservation]] = defaultdict(list)
    date_used: dict[str, set[str]] = defaultdict(set)
    user_ids = [u.user_id for u in users]
    users_by_id = {u.user_id: u for u in users}
    next_idx = 0

    for r in reservations:
        chosen: Optional[str] = None
        n = len(users)
        for i in range(n):
            cand = user_ids[(next_idx + i) % n]
            if cand not in date_used[r.reserv_date]:
                chosen = cand
                next_idx = (next_idx + i + 1) % n
                break
        if chosen is None:
            # 모두 사용 중이면 그냥 next_idx를 사용
            chosen = user_ids[next_idx]
            next_idx = (next_idx + 1) % n

        assignments[chosen].append(r)
        date_used[r.reserv_date].add(chosen)

    # keep order per user
    for k in assignments:
        assignments[k] = sorted(assignments[k], key=lambda r: (r.reserv_date, r.start, r.court))
    return assignments


def build_command(user: User, assigned: List[Reservation], exec_at: str, aws_cfg: AWSConfig) -> List[str]:
    cmd = [
        "aws",
        "ecs",
        "run-task",
        "--region",
        aws_cfg.region,
        "--cluster",
        aws_cfg.cluster,
        "--launch-type",
        "FARGATE",
        "--task-definition",
        aws_cfg.task_def,
        "--network-configuration",
        f"awsvpcConfiguration={{subnets=[{','.join(aws_cfg.subnets)}],securityGroups=[{','.join(aws_cfg.security_groups)}],assignPublicIp={aws_cfg.assign_public_ip}}}",
    ]
    # build container override command list
    inner_cmd: List[str] = [
        "--id",
        user.user_id,
        "--password",
        user.password,
        "--exec-at",
        exec_at,
    ]
    for r in assigned:
        inner_cmd.extend(
            [
                "--reservation",
                f"{r.reserv_date},{r.start},{r.end},{r.court}",
                "--tg-token",
                "6104901453:AAH5RBa-vT5JDB0zb7r9MGz54RIpZamOGW0",
                "--tg-chat-id",
                "5998176515"
            ]
        )
    overrides = {
        "containerOverrides": [
            {
                "name": aws_cfg.container_name,
                "command": inner_cmd,
            }
        ]
    }
    cmd += ["--overrides", json.dumps(overrides)]
    return cmd


def print_commands(commands: List[List[str]]):
    for cmd in commands:
        print(" ".join(shlex.quote(x) for x in cmd) + " >/dev/null")


def execute_commands(commands: List[List[str]]):
    """CLI에서 aws ecs run-task 명령 실행"""
    for cmd in commands:
        print(f"[exec] {' '.join(shlex.quote(x) for x in cmd)}")
        subprocess.run(cmd, check=True)


def run_ecs_task(user: User, assigned: List[Reservation], exec_at: str, aws_cfg: AWSConfig) -> Dict[str, Any]:
    """boto3로 ECS Fargate 태스크 실행 (Lambda용)"""
    if not boto3:
        raise RuntimeError("boto3가 필요합니다: pip install boto3")

    ecs = boto3.client("ecs", region_name=aws_cfg.region)

    inner_cmd: List[str] = [
        "--id", user.user_id,
        "--password", user.password,
        "--exec-at", exec_at,
    ]
    for r in assigned:
        inner_cmd.extend([
            "--reservation",
            f"{r.reserv_date},{r.start},{r.end},{r.court}",
        ])
    # Telegram 알림 설정은 환경변수에서 가져오거나 Parameter Store에서 관리
    tg_token = os.environ.get("TELEGRAM_TOKEN", "")
    tg_chat_id = os.environ.get("TELEGRAM_CHAT_ID", "")
    if tg_token and tg_chat_id:
        inner_cmd.extend(["--tg-token", tg_token, "--tg-chat-id", tg_chat_id])

    response = ecs.run_task(
        cluster=aws_cfg.cluster,
        launchType="FARGATE",
        taskDefinition=aws_cfg.task_def,
        networkConfiguration={
            "awsvpcConfiguration": {
                "subnets": aws_cfg.subnets,
                "securityGroups": aws_cfg.security_groups,
                "assignPublicIp": aws_cfg.assign_public_ip,
            }
        },
        overrides={
            "containerOverrides": [
                {
                    "name": aws_cfg.container_name,
                    "command": inner_cmd,
                }
            ]
        },
    )
    return response


def compute_exec_at(now_kst: datetime) -> str:
    run_date = now_kst.date()
    next_day = run_date + timedelta(days=1)
    exec_time = time(10, 0, 0) if run_date.day == 15 else time(9, 59, 1)
    if next_day.month != run_date.month:
        exec_time = time(9, 59, 1)
    exec_dt = datetime.combine(run_date, exec_time, tzinfo=KST) if KST else datetime.combine(run_date, exec_time)
    return exec_dt.strftime("%Y-%m-%d %H:%M:%S")


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="GMUC Fargate scheduler")
    p.add_argument("--users", default="user.json", help="user.json path")
    p.add_argument("--schedule", default="schedule.json", help="schedule overrides file (optional)")
    p.add_argument("--run-date", help="YYYY-MM-DD 형식으로 스케줄 기준 날짜를 강제 (미지정 시 오늘)")
    p.add_argument("--region", required=True, help="AWS region, e.g., ap-northeast-2")
    p.add_argument("--cluster", required=True, help="ECS cluster name")
    p.add_argument("--task-def", required=True, help="Task definition name:revision")
    p.add_argument("--container-name", default="gmuc", help="Container name in task definition")
    p.add_argument("--subnet", action="append", required=True, help="Subnet ID (repeatable)")
    p.add_argument("--security-group", action="append", required=True, help="Security group ID (repeatable)")
    p.add_argument("--assign-public-ip", default="ENABLED", choices=["ENABLED", "DISABLED"], help="Fargate assignPublicIp")
    p.add_argument("--execute", action="store_true", help="Actually run aws ecs run-task (default: print commands)")
    return p.parse_args()


def main():
    args = parse_args()
    now_kst = datetime.now(tz=KST) if KST else datetime.now()
    if args.run_date:
        try:
            forced_date = datetime.strptime(args.run_date, "%Y-%m-%d").date()
        except ValueError:
            raise SystemExit(f"--run-date 형식이 잘못되었습니다: {args.run_date} (예: 2025-12-15)")
        now_kst = datetime.combine(forced_date, time(now_kst.hour, now_kst.minute, now_kst.second), tzinfo=KST) if KST else datetime.combine(forced_date, time(now_kst.hour, now_kst.minute, now_kst.second))

    users = load_users(args.users)
    add_list, remove_list = load_schedule_overrides(args.schedule)

    base_reservations = generate_default_reservations(now_kst.date())
    reservations = apply_overrides(base_reservations, add_list, remove_list)
    if not reservations:
        print("[info] 생성된 예약이 없습니다 (오늘이 15일/말일이 아니거나 주말 없음).")
        return

    user_map = {u.user_id: u for u in users}
    assignments_by_userid = assign_users(reservations, users)

    aws_cfg = AWSConfig(
        region=args.region,
        cluster=args.cluster,
        task_def=args.task_def,
        subnets=args.subnet,
        security_groups=args.security_group,
        container_name=args.container_name,
        assign_public_ip=args.assign_public_ip,
    )
    exec_at = compute_exec_at(now_kst)

    commands: List[List[str]] = []
    for uid, items in assignments_by_userid.items():
        user = user_map[uid]
        commands.append(build_command(user, items, exec_at, aws_cfg))

    if args.execute:
        execute_commands(commands)
    else:
        print_commands(commands)


def lambda_handler(event: Dict[str, Any], context: Any) -> Dict[str, Any]:
    """
    AWS Lambda 핸들러.

    환경변수에서 설정을 읽고, Parameter Store에서 users/schedule을 로드하여
    ECS Fargate 태스크를 실행합니다.

    event 옵션:
    - run_date: "2025-01-15" - 스케줄 생성 기준 날짜
    - dry_run: true - 로컬 파일 사용, ECS 호출 없이 테스트
    - users_file: "user.json" - dry_run 시 사용자 파일 경로
    - schedule_file: "schedule.json" - dry_run 시 스케줄 파일 경로
    """
    event = event or {}
    dry_run = event.get("dry_run", False)

    # 환경변수에서 AWS 설정 로드
    region = os.environ.get("GMUC_REGION") or os.environ.get("AWS_REGION", "ap-northeast-2")
    cluster = os.environ.get("GMUC_CLUSTER", "dry-run-cluster")
    task_def = os.environ.get("GMUC_TASK_DEF", "dry-run-task:1")
    subnets = os.environ.get("GMUC_SUBNETS", "subnet-dry-run").split(",")
    security_groups = os.environ.get("GMUC_SECURITY_GROUPS", "sg-dry-run").split(",")
    container_name = os.environ.get("GMUC_CONTAINER_NAME", "gmuc")
    assign_public_ip = os.environ.get("GMUC_ASSIGN_PUBLIC_IP", "ENABLED")

    ssm_users_param = os.environ.get("SSM_USERS_PARAM", DEFAULT_SSM_USERS_PARAM)
    ssm_schedule_param = os.environ.get("SSM_SCHEDULE_PARAM", DEFAULT_SSM_SCHEDULE_PARAM)

    # 필수 환경변수 검증 (dry_run 모드에서는 스킵)
    if not dry_run:
        missing = []
        if not cluster or cluster == "dry-run-cluster":
            missing.append("GMUC_CLUSTER")
        if not task_def or task_def == "dry-run-task:1":
            missing.append("GMUC_TASK_DEF")
        if not subnets or subnets == ["subnet-dry-run"]:
            missing.append("GMUC_SUBNETS")
        if not security_groups or security_groups == ["sg-dry-run"]:
            missing.append("GMUC_SECURITY_GROUPS")
        if missing:
            return {"statusCode": 400, "body": f"필수 환경변수 누락: {', '.join(missing)}"}

    # 현재 시간 (KST)
    now_kst = datetime.now(tz=KST) if KST else datetime.now()

    # event에서 run_date 오버라이드
    if event.get("run_date"):
        try:
            forced_date = datetime.strptime(event["run_date"], "%Y-%m-%d").date()
            now_kst = datetime.combine(forced_date, time(now_kst.hour, now_kst.minute, now_kst.second), tzinfo=KST) if KST else datetime.combine(forced_date, time(now_kst.hour, now_kst.minute, now_kst.second))
        except ValueError:
            return {"statusCode": 400, "body": f"run_date 형식 오류: {event['run_date']}"}

    # users, schedule 로드
    if dry_run:
        # dry_run: 로컬 파일에서 로드
        users_file = event.get("users_file", "user.json")
        schedule_file = event.get("schedule_file", "schedule.json")
        try:
            users = load_users(users_file)
        except FileNotFoundError:
            # 파일이 없으면 더미 사용자 생성
            users = [User(user_id="test_user1", password="***"), User(user_id="test_user2", password="***")]
            print(f"[dry_run] {users_file} 없음, 더미 사용자 사용")
        except Exception as e:
            return {"statusCode": 500, "body": f"users 로드 실패: {e}"}

        try:
            add_list, remove_list = load_schedule_overrides(schedule_file)
        except Exception as e:
            add_list, remove_list = [], []
            print(f"[dry_run] {schedule_file} 로드 실패, 오버라이드 없이 진행: {e}")
    else:
        # 실제 실행: Parameter Store에서 로드
        try:
            users = load_users_from_ssm(ssm_users_param, region)
        except Exception as e:
            return {"statusCode": 500, "body": f"users 로드 실패: {e}"}

        try:
            add_list, remove_list = load_schedule_overrides_from_ssm(ssm_schedule_param, region)
        except Exception as e:
            return {"statusCode": 500, "body": f"schedule 로드 실패: {e}"}

    # 예약 생성
    base_reservations = generate_default_reservations(now_kst.date())
    reservations = apply_overrides(base_reservations, add_list, remove_list)

    if not reservations:
        return {
            "statusCode": 200,
            "body": "생성된 예약이 없습니다 (오늘이 15일/말일이 아니거나 주말 없음).",
            "tasks_started": 0,
            "dry_run": dry_run,
        }

    # 사용자에게 예약 배정
    user_map = {u.user_id: u for u in users}
    assignments = assign_users(reservations, users)

    aws_cfg = AWSConfig(
        region=region,
        cluster=cluster,
        task_def=task_def,
        subnets=[s.strip() for s in subnets if s.strip()],
        security_groups=[s.strip() for s in security_groups if s.strip()],
        container_name=container_name,
        assign_public_ip=assign_public_ip,
    )
    exec_at = compute_exec_at(now_kst)

    # ECS 태스크 실행 (또는 dry_run 시 출력만)
    results = []
    for uid, items in assignments.items():
        user = user_map[uid]
        if dry_run:
            # dry_run: 명령어 출력
            print(f"\n[dry_run] User: {uid} ({len(items)}개 예약)")
            print(f"  exec_at: {exec_at}")
            for r in items:
                print(f"  - {r.reserv_date} {r.start}-{r.end} court {r.court}")
            results.append({
                "user_id": uid,
                "reservations": len(items),
                "reservation_details": [
                    {"date": r.reserv_date, "start": r.start, "end": r.end, "court": r.court}
                    for r in items
                ],
                "status": "dry_run",
            })
        else:
            try:
                resp = run_ecs_task(user, items, exec_at, aws_cfg)
                task_arns = [t.get("taskArn", "") for t in resp.get("tasks", [])]
                results.append({
                    "user_id": uid,
                    "reservations": len(items),
                    "task_arns": task_arns,
                    "status": "started",
                })
            except Exception as e:
                results.append({
                    "user_id": uid,
                    "reservations": len(items),
                    "status": "failed",
                    "error": str(e),
                })

    started_count = len([r for r in results if r["status"] in ("started", "dry_run")])
    return {
        "statusCode": 200,
        "body": f"{len(results)}개 태스크 {'(dry_run) ' if dry_run else ''}시작 요청 완료",
        "tasks_started": started_count,
        "exec_at": exec_at,
        "dry_run": dry_run,
        "results": results,
    }


if __name__ == "__main__":
    main()
