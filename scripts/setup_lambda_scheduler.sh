#!/bin/bash
# GMUC Lambda Scheduler 설정 스크립트
#
# 사전 조건:
# 1. AWS CLI 설정 완료
# 2. 적절한 IAM 권한 (Lambda, EventBridge, SSM, ECS)
#
# 사용법:
#   REGION=ap-northeast-2 \
#   CLUSTER=my-cluster \
#   TASK_DEF=gmuc:1 \
#   SUBNETS=subnet-xxx,subnet-yyy \
#   SECURITY_GROUPS=sg-xxx \
#   ./scripts/setup_lambda_scheduler.sh

set -euo pipefail

# 필수 환경변수 확인
: "${REGION:?REGION 환경변수가 필요합니다}"
: "${CLUSTER:?CLUSTER 환경변수가 필요합니다}"
: "${TASK_DEF:?TASK_DEF 환경변수가 필요합니다}"
: "${SUBNETS:?SUBNETS 환경변수가 필요합니다}"
: "${SECURITY_GROUPS:?SECURITY_GROUPS 환경변수가 필요합니다}"

# 옵션 환경변수
LAMBDA_NAME="${LAMBDA_NAME:-gmuc-scheduler}"
LAMBDA_ROLE_NAME="${LAMBDA_ROLE_NAME:-gmuc-scheduler-role}"
CONTAINER_NAME="${CONTAINER_NAME:-gmuc}"
SSM_USERS_PARAM="${SSM_USERS_PARAM:-/gmuc/users}"
SSM_SCHEDULE_PARAM="${SSM_SCHEDULE_PARAM:-/gmuc/schedule}"
TELEGRAM_TOKEN="${TELEGRAM_TOKEN:-6104901453:AAH5RBa-vT5JDB0zb7r9MGz54RIpZamOGW0}"
TELEGRAM_CHAT_ID="${TELEGRAM_CHAT_ID:-5998176515}"

ACCOUNT_ID=$(aws sts get-caller-identity --query Account --output text)

echo "=== GMUC Lambda Scheduler 설정 ==="
echo "Region: $REGION"
echo "Account: $ACCOUNT_ID"
echo "Lambda: $LAMBDA_NAME"
echo ""

# 1. IAM Role 생성
echo "[1/6] IAM Role 생성..."
cat > /tmp/trust-policy.json << 'EOF'
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Effect": "Allow",
      "Principal": {"Service": "lambda.amazonaws.com"},
      "Action": "sts:AssumeRole"
    }
  ]
}
EOF

aws iam create-role \
  --role-name "$LAMBDA_ROLE_NAME" \
  --assume-role-policy-document file:///tmp/trust-policy.json \
  --region "$REGION" 2>/dev/null || echo "  (Role이 이미 존재합니다)"

# IAM Policy 생성
cat > /tmp/lambda-policy.json << EOF
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Effect": "Allow",
      "Action": [
        "logs:CreateLogGroup",
        "logs:CreateLogStream",
        "logs:PutLogEvents"
      ],
      "Resource": "arn:aws:logs:*:*:*"
    },
    {
      "Effect": "Allow",
      "Action": [
        "ssm:GetParameter"
      ],
      "Resource": [
        "arn:aws:ssm:${REGION}:${ACCOUNT_ID}:parameter${SSM_USERS_PARAM}",
        "arn:aws:ssm:${REGION}:${ACCOUNT_ID}:parameter${SSM_SCHEDULE_PARAM}"
      ]
    },
    {
      "Effect": "Allow",
      "Action": [
        "ecs:RunTask"
      ],
      "Resource": "arn:aws:ecs:${REGION}:${ACCOUNT_ID}:task-definition/${TASK_DEF%:*}:*"
    },
    {
      "Effect": "Allow",
      "Action": [
        "iam:PassRole"
      ],
      "Resource": "*",
      "Condition": {
        "StringLike": {
          "iam:PassedToService": "ecs-tasks.amazonaws.com"
        }
      }
    }
  ]
}
EOF

POLICY_NAME="${LAMBDA_NAME}-policy"
aws iam put-role-policy \
  --role-name "$LAMBDA_ROLE_NAME" \
  --policy-name "$POLICY_NAME" \
  --policy-document file:///tmp/lambda-policy.json \
  --region "$REGION"

ROLE_ARN="arn:aws:iam::${ACCOUNT_ID}:role/${LAMBDA_ROLE_NAME}"
echo "  Role ARN: $ROLE_ARN"

# Role이 사용 가능해질 때까지 대기
echo "  Role 전파 대기 (10초)..."
sleep 10

# 2. Lambda 패키지 생성
echo "[2/6] Lambda 패키지 생성..."
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"

cd "$PROJECT_DIR"
rm -f /tmp/gmuc-lambda.zip
zip -j /tmp/gmuc-lambda.zip schedule_runner.py

# 3. Lambda 함수 생성/업데이트
echo "[3/6] Lambda 함수 생성..."

# 환경변수를 JSON 형식으로 빌드
ENV_JSON=$(cat <<EOF
{
  "Variables": {
    "GMUC_REGION": "${REGION}",
    "GMUC_CLUSTER": "${CLUSTER}",
    "GMUC_TASK_DEF": "${TASK_DEF}",
    "GMUC_SUBNETS": "${SUBNETS}",
    "GMUC_SECURITY_GROUPS": "${SECURITY_GROUPS}",
    "GMUC_CONTAINER_NAME": "${CONTAINER_NAME}",
    "SSM_USERS_PARAM": "${SSM_USERS_PARAM}",
    "SSM_SCHEDULE_PARAM": "${SSM_SCHEDULE_PARAM}"
  }
}
EOF
)

# Telegram 설정이 있으면 추가
if [ -n "$TELEGRAM_TOKEN" ] && [ -n "$TELEGRAM_CHAT_ID" ]; then
  ENV_JSON=$(echo "$ENV_JSON" | jq --arg token "$TELEGRAM_TOKEN" --arg chat "$TELEGRAM_CHAT_ID" \
    '.Variables.TELEGRAM_TOKEN = $token | .Variables.TELEGRAM_CHAT_ID = $chat')
fi

if aws lambda get-function --function-name "$LAMBDA_NAME" --region "$REGION" 2>/dev/null; then
  echo "  Lambda 함수 업데이트..."
  aws lambda update-function-code \
    --function-name "$LAMBDA_NAME" \
    --zip-file fileb:///tmp/gmuc-lambda.zip \
    --region "$REGION" > /dev/null

  # 코드 업데이트 완료 대기
  echo "  코드 업데이트 완료 대기..."
  aws lambda wait function-updated \
    --function-name "$LAMBDA_NAME" \
    --region "$REGION"

  aws lambda update-function-configuration \
    --function-name "$LAMBDA_NAME" \
    --environment "$ENV_JSON" \
    --timeout 60 \
    --region "$REGION" > /dev/null
else
  echo "  Lambda 함수 생성..."
  aws lambda create-function \
    --function-name "$LAMBDA_NAME" \
    --runtime python3.11 \
    --role "$ROLE_ARN" \
    --handler "schedule_runner.lambda_handler" \
    --zip-file fileb:///tmp/gmuc-lambda.zip \
    --timeout 60 \
    --environment "$ENV_JSON" \
    --region "$REGION" > /dev/null
fi

LAMBDA_ARN="arn:aws:lambda:${REGION}:${ACCOUNT_ID}:function:${LAMBDA_NAME}"
echo "  Lambda ARN: $LAMBDA_ARN"

# 4. EventBridge 규칙 생성 - 매월 15일
echo "[4/6] EventBridge 규칙 생성 (매월 15일 09:30 KST)..."
RULE_NAME_15="${LAMBDA_NAME}-15th"
# KST 09:30 = UTC 00:30
aws events put-rule \
  --name "$RULE_NAME_15" \
  --schedule-expression "cron(30 0 15 * ? *)" \
  --state ENABLED \
  --region "$REGION" > /dev/null

aws lambda add-permission \
  --function-name "$LAMBDA_NAME" \
  --statement-id "${RULE_NAME_15}-invoke" \
  --action lambda:InvokeFunction \
  --principal events.amazonaws.com \
  --source-arn "arn:aws:events:${REGION}:${ACCOUNT_ID}:rule/${RULE_NAME_15}" \
  --region "$REGION" 2>/dev/null || true

aws events put-targets \
  --rule "$RULE_NAME_15" \
  --targets "Id=1,Arn=${LAMBDA_ARN}" \
  --region "$REGION" > /dev/null

echo "  규칙 생성됨: $RULE_NAME_15"

# 5. EventBridge 규칙 생성 - 매월 말일
echo "[5/6] EventBridge 규칙 생성 (매월 말일 09:30 KST)..."
RULE_NAME_LAST="${LAMBDA_NAME}-last"
# L = 월의 마지막 날
aws events put-rule \
  --name "$RULE_NAME_LAST" \
  --schedule-expression "cron(30 0 L * ? *)" \
  --state ENABLED \
  --region "$REGION" > /dev/null

aws lambda add-permission \
  --function-name "$LAMBDA_NAME" \
  --statement-id "${RULE_NAME_LAST}-invoke" \
  --action lambda:InvokeFunction \
  --principal events.amazonaws.com \
  --source-arn "arn:aws:events:${REGION}:${ACCOUNT_ID}:rule/${RULE_NAME_LAST}" \
  --region "$REGION" 2>/dev/null || true

aws events put-targets \
  --rule "$RULE_NAME_LAST" \
  --targets "Id=1,Arn=${LAMBDA_ARN}" \
  --region "$REGION" > /dev/null

echo "  규칙 생성됨: $RULE_NAME_LAST"

# 6. Parameter Store 초기값 안내
echo "[6/6] Parameter Store 설정..."
echo ""
echo "다음 명령으로 Parameter Store에 데이터를 저장하세요:"
echo ""
echo "# 사용자 정보 (SecureString)"
echo "aws ssm put-parameter \\"
echo "  --name '$SSM_USERS_PARAM' \\"
echo "  --type SecureString \\"
echo "  --value '[{\"id\":\"user1\",\"password\":\"pass1\"},{\"id\":\"user2\",\"password\":\"pass2\"}]' \\"
echo "  --region $REGION"
echo ""
echo "# 스케줄 오버라이드 (선택사항)"
echo "aws ssm put-parameter \\"
echo "  --name '$SSM_SCHEDULE_PARAM' \\"
echo "  --type String \\"
echo "  --value '{\"add\":[],\"remove\":[]}' \\"
echo "  --region $REGION"
echo ""
echo "=== 설정 완료 ==="
echo ""
echo "테스트 실행:"
echo "aws lambda invoke \\"
echo "  --function-name $LAMBDA_NAME \\"
echo "  --payload '{\"run_date\":\"2025-01-15\"}' \\"
echo "  --region $REGION \\"
echo "  /tmp/lambda-output.json && cat /tmp/lambda-output.json"
