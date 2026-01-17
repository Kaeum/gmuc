#!/usr/bin/env python3
"""
Generate GMUC reservation tasks for Fargate.

동작 요약
- 오늘 날짜(KST)를 기준으로 매월 15일/말일에 실행한다고 가정한다.
- 15일 실행: 같은 달 16일~말일 사이의 토/일요일 슬롯을 생성.
- 말일 실행: 다음 달 1~15일 사이의 토/일요일 슬롯을 생성.
- 슬롯: 3~10월 -> 06-08, 08-10, 10-12 / 11~2월 -> 07-09, 09-11
- 코트: 1, 2
- schedule.json의 add/remove로 기본 스케줄 가감.
- user.json의 계정에 배정하며, 같은 날짜에 같은 계정은 피하려 시도(불가 시 재사용).
- 최종적으로 aws ecs run-task 명령을 출력하거나(--execute 시 실제 실행) containerOverrides에 예약/계정 정보를 삽입.
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
from typing import Iterable, List, Optional, Sequence
from calendar import monthrange

try:
    from zoneinfo import ZoneInfo
except Exception:  # pragma: no cover
    ZoneInfo = None  # type: ignore

KST = ZoneInfo("Asia/Seoul") if ZoneInfo else None
WINTER_MONTHS = {11, 12, 1, 2}


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
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    users: List[User] = []
    for entry in data:
        if "id" not in entry or "password" not in entry:
            raise ValueError(f"user.json entry missing id/password: {entry}")
        users.append(User(user_id=entry["id"], password=entry["password"]))
    if not users:
        raise ValueError("user list is empty")
    return users


def load_schedule_overrides(path: Optional[str]) -> tuple[List[Reservation], List[Reservation]]:
    if not path or not os.path.exists(path):
        return [], []
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
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


def is_weekend(dt: date) -> bool:
    return dt.weekday() >= 5  # 5=Sat, 6=Sun


def month_slots(month: int) -> List[tuple[str, str]]:
    if month in WINTER_MONTHS:
        return [("07:00", "09:00"), ("09:00", "11:00")]
    return [("06:00", "08:00"), ("08:00", "10:00"), ("10:00", "12:00")]


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
    for cmd in commands:
        print(f"[exec] {' '.join(shlex.quote(x) for x in cmd)}")
        subprocess.run(cmd, check=True)


def compute_exec_at(now_kst: datetime) -> str:
    exec_dt = datetime.combine(now_kst.date(), time(10, 0), tzinfo=KST) if KST else datetime.combine(now_kst.date(), time(10, 0))
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


if __name__ == "__main__":
    main()
