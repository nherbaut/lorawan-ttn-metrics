# Build and publish the lorawan-ttn-metrics exporter image.
#
#   make build          # local image for the host architecture (docker load)
#   make run            # run it locally, needs TTN_API_KEY in the environment
#   make push           # multi-arch (amd64 + arm64) build, pushed to ghcr.io
#
# `make push` is what the GitHub Actions workflow runs. Pushing from a
# workstation needs `make login` first (GHCR_TOKEN = PAT with write:packages).

REGISTRY   ?= ghcr.io
IMAGE_NAME ?= lorawan-ttn-metrics
# Owner: from the git remote if present, otherwise the GitHub account below.
OWNER      ?= $(shell git remote get-url origin 2>/dev/null | sed -nE 's#.*github\.com[:/]([^/]+)/.*#\1#p' | tr 'A-Z' 'a-z')
ifeq ($(strip $(OWNER)),)
OWNER      := nherbaut
endif
IMAGE      ?= $(REGISTRY)/$(OWNER)/$(IMAGE_NAME)
VERSION    ?= $(shell git describe --tags --always --dirty 2>/dev/null || echo dev)
# Additional tags pushed alongside $(VERSION); empty to push only $(VERSION).
EXTRA_TAGS ?= latest
PLATFORMS  ?= linux/amd64,linux/arm64
BUILDER    ?= ttn-multiarch
SOURCE_URL ?= https://github.com/$(OWNER)/$(IMAGE_NAME)
METRICS_PORT ?= 9877

TAG_ARGS   := -t $(IMAGE):$(VERSION) $(foreach t,$(EXTRA_TAGS),-t $(IMAGE):$(t))
BUILD_ARGS := --build-arg VERSION=$(VERSION) --build-arg SOURCE_URL=$(SOURCE_URL)

.PHONY: help build run multiarch push builder login inspect clean

help:
	@echo "Image:     $(IMAGE):$(VERSION) (+ $(or $(EXTRA_TAGS),no extra tags))"
	@echo "Platforms: $(PLATFORMS)"
	@echo "Targets:   build run multiarch push login inspect clean"

build:
	docker build $(BUILD_ARGS) $(TAG_ARGS) .

run:
	@test -n "$$TTN_API_KEY" || { echo "TTN_API_KEY is not set" >&2; exit 1; }
	docker run --rm -it -e TTN_API_KEY -e TTN_GATEWAY_ID -p $(METRICS_PORT):9877 $(IMAGE):$(VERSION)

builder:
	@docker buildx inspect $(BUILDER) >/dev/null 2>&1 || \
		docker buildx create --name $(BUILDER) --driver docker-container --bootstrap >/dev/null
	@echo "buildx builder: $(BUILDER)"

MULTIARCH = docker buildx build --builder $(BUILDER) --platform $(PLATFORMS) \
	$(BUILD_ARGS) $(TAG_ARGS) --provenance=false

# Multi-arch build without publishing (used for pull requests).
multiarch: builder
	$(MULTIARCH) .

push: builder
	$(MULTIARCH) --push .

login:
	@test -n "$$GHCR_TOKEN" || { echo "GHCR_TOKEN is not set" >&2; exit 1; }
	@echo "$$GHCR_TOKEN" | docker login $(REGISTRY) -u $(OWNER) --password-stdin

inspect:
	docker buildx imagetools inspect $(IMAGE):$(VERSION)

clean:
	-docker image rm $(IMAGE):$(VERSION) $(foreach t,$(EXTRA_TAGS),$(IMAGE):$(t))
