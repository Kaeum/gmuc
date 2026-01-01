DOCKER ?= docker
IMAGE  ?= gmuc:latest

.PHONY: docker-build docker-run docker-shell docker-install docker-oneclick

docker-build:
	$(DOCKER) build -t $(IMAGE) .

# 사용 예:
# make docker-run ARGS='--id $(ID) --password $(PW) --exec-at "2025-12-27 22:30:00" --reservation "20251231,07:00,09:00,4"'
docker-run:
	@if [ -z "$$ARGS" ]; then echo "ARGS 환경변수에 cli.py 인자를 넣어주세요."; exit 1; fi
	$(DOCKER) run --rm $(IMAGE) $(ARGS)

# 컨테이너 내부 셸 진입
docker-shell:
	$(DOCKER) run --rm -it --entrypoint /bin/bash $(IMAGE)

# Ubuntu/Debian에서 Docker 설치 자동화 (sudo 필요)
docker-install:
	scripts/oneclick_docker.sh --install-only

# Docker 설치(필요시) + 빌드만 수행
docker-oneclick:
	scripts/oneclick_docker.sh
