# Evernorth audit dashboard.
#   make start   build the image and run the dashboard (one command)
#   make stop    stop it and remove every Docker resource this project created
#   make logs    follow the dashboard logs

PROJECT := evernorth-audit
PORT    ?= 8501
OPEN    ?= 1
URL     := http://localhost:$(PORT)
COMPOSE := PORT=$(PORT) docker compose -p $(PROJECT)

# Files sealed in data/manifest.csv. The manifest is written only when it is missing, so it records the
# pack as delivered; any later edit to these files turns the dashboard's integrity banner amber.
# After dropping in a new export, delete data/manifest.csv and run make start to seal the new pack.
PACK := uniswap_mm_platform_position_rollover.csv uniswap_mm_platform_daily_notional_and_fees.csv \
        uniswap_mm_platform_mint_audit_log.csv twap_tca_1.csv wintermute_otc_historical_records.csv \
        option_outcomes.csv dev_queries.md
SHA256  := $(shell command -v sha256sum >/dev/null 2>&1 && echo sha256sum || echo "shasum -a 256")

.DEFAULT_GOAL := help
.PHONY: help start stop logs check-docker manifest

help:
	@echo "make start   build and run the dashboard on $(URL)  (PORT=xxxx to change, OPEN=0 to skip the browser)"
	@echo "make stop    stop it and remove this project's containers, images, volumes and networks"
	@echo "make logs    follow the dashboard logs"

check-docker:
	@docker info >/dev/null 2>&1 || { echo "Docker is not running. Start Docker Desktop and try again."; exit 1; }

manifest:
	@cd data && for f in $(PACK); do test -f "$$f" || { echo "Missing data/$$f"; exit 1; }; done
	@if [ -f data/manifest.csv ]; then \
	  echo "Manifest: using existing data/manifest.csv (delete it to re-seal after a new export)"; \
	else \
	  cd data && $(SHA256) $(PACK) > manifest.csv && echo "Manifest: sealed $(words $(PACK)) files into data/manifest.csv"; \
	fi

start: check-docker manifest
	$(COMPOSE) up -d --build --wait
	@echo ""
	@echo "Dashboard running at $(URL)"
	@[ "$(OPEN)" = 1 ] && command -v open >/dev/null 2>&1 && open $(URL) || true

stop: check-docker
	$(COMPOSE) down --rmi all --volumes --remove-orphans
	@# leftover untagged images from earlier builds of this project
	@docker image prune -f --filter "label=project=$(PROJECT)" >/dev/null
	@echo "Stopped. All Docker resources for $(PROJECT) removed."

logs: check-docker
	$(COMPOSE) logs -f dashboard
