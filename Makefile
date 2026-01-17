DOCKER ?= docker

# Usage:
#   REGION=ap-northeast-2 ACCOUNT_ID=005592596386 make cross-push
#   IMAGE를 직접 지정하려면: IMAGE=123456789012.dkr.ecr.ap-northeast-2.amazonaws.com/gmuc:tag make cross-push

.PHONY: cross-push ecr-login

IMAGE ?= $(ACCOUNT_ID).dkr.ecr.$(REGION).amazonaws.com/gmuc:latest

cross-push:
	@if [ -z "$(REGION)" ] || [ -z "$(ACCOUNT_ID)" ]; then \
		echo "REGION과 ACCOUNT_ID 환경변수를 설정하세요. 예: REGION=ap-northeast-2 ACCOUNT_ID=123456789012 make cross-push"; \
		exit 1; \
	fi
	$(DOCKER) run --privileged --rm tonistiigi/binfmt --install all
	$(DOCKER) buildx create --use --name gmucbuilder 2>/dev/null || $(DOCKER) buildx use gmucbuilder
	$(DOCKER) buildx build --platform linux/amd64,linux/arm64 -t "$(IMAGE)" --push .

ecr-login:
	@if [ -z "$(REGION)" ] || [ -z "$(ACCOUNT_ID)" ]; then \
		echo "REGION과 ACCOUNT_ID 환경변수를 설정하세요. 예: REGION=ap-northeast-2 ACCOUNT_ID=123456789012 make ecr-login"; \
		exit 1; \
	fi
	aws ecr get-login-password --region $(REGION) | $(DOCKER) login --username AWS --password-stdin $(ACCOUNT_ID).dkr.ecr.$(REGION).amazonaws.com
