# Convenience targets for the local test stack.
#
# Pick which GeoServer to talk to with GS=2 (default) or GS=3:
#
#   make up            &&  make check         # 2.28 on :8080
#   make up-gs3 GS=3   &&  make check GS=3    # 3.0  on :8081
#
# Or point anywhere with URL=https://host/geoserver.

# Load .env so the driver settings in it (COG_RANGE_READER, the *_FROM_GEOSERVER
# hostnames, credentials) actually reach the driver.  Compose reads .env by
# itself; make does not, so without this they are silently ignored.
# Caveat: values are taken literally -- no inline `# comments` after a value.
ifneq (,$(wildcard .env))
include .env
export
endif

GS       ?= 2
GS2_URL  ?= http://localhost:8080/geoserver
GS3_URL  ?= http://localhost:8081/geoserver
URL      ?= $(if $(filter 3,$(GS)),$(GS3_URL),$(GS2_URL))

# Which single mosaic `make mosaic` builds: local, remote or upload.
WHICH    ?= remote

GEOSERVER_USER     ?= admin
GEOSERVER_PASSWORD ?= geoserver

S3_PORT   ?= 4566
S3_BUCKET ?= mosaic-tiles

COMPOSE   = docker compose
# One extras set everywhere, so uv does not re-sync the venv between targets.
UV        = uv run --extra dev --extra examples

.PHONY: help install test fixtures up up-gs3 up-all down clean-volumes logs ps \
        seed-s3 verify-s3 check driver mosaic inspect clean-gs integration smoke ui

help:  ## Show this help
	@grep -E '^[a-zA-Z0-9_-]+:.*?## .*$$' $(MAKEFILE_LIST) \
	  | awk 'BEGIN{FS=":.*?## "}{printf "  \033[36m%-14s\033[0m %s\n", $$1, $$2}'
	@printf '\nTarget server: \033[33m%s\033[0m  (override with GS=3 or URL=...)\n' "$(URL)"

install:  ## Install the package with dev + examples extras
	uv sync --extra dev --extra examples

test:  ## Run the unit tests (no stack required)
	$(UV) pytest -m 'not integration'

# --- stack -------------------------------------------------------------------

fixtures:  ## Generate the demo COG granules into fixtures/tiles
	$(UV) python examples/make_fixtures.py

up: fixtures  ## Start PostGIS, LocalStack and GeoServer 2.28 (:8080)
	$(COMPOSE) --profile gs2 up -d
	@echo "GeoServer 2.28 starting on $(GS2_URL) -- first boot installs plugins, give it a few minutes"
	@echo "Watch it come up:  make logs   |   then:  make smoke"

up-gs3: fixtures  ## Start PostGIS, LocalStack and GeoServer 3.0 (:8081)
	$(COMPOSE) --profile gs3 up -d
	@echo "GeoServer 3.0 starting on $(GS3_URL)"
	@echo "Then:  make smoke GS=3"

up-all: fixtures  ## Start both GeoServer versions side by side
	$(COMPOSE) --profile all up -d

ps:  ## Show container status and health
	$(COMPOSE) --profile all ps

logs:  ## Tail the stack logs
	$(COMPOSE) --profile all logs -f

down:  ## Stop the stack, keeping volumes
	$(COMPOSE) --profile all down

clean-volumes:  ## Stop the stack and delete all data (catalogs, database, S3)
	$(COMPOSE) --profile all down -v

seed-s3:  ## Re-upload fixtures to LocalStack after regenerating them
	@ls fixtures/tiles/*.tif >/dev/null 2>&1 || { echo "No granules -- run 'make fixtures' first"; exit 1; }
	$(COMPOSE) exec localstack bash /etc/localstack/init/ready.d/01-seed-s3.sh

verify-s3:  ## Check LocalStack serves granules anonymously with range requests
	@granule=$$(ls fixtures/tiles/*.tif 2>/dev/null | head -1 | xargs -r basename); \
	if [ -z "$$granule" ]; then echo "No fixtures; run: make fixtures"; exit 1; fi; \
	code=$$(curl -s -o /dev/null -w '%{http_code}' -r 0-1023 \
	        http://localhost:$(S3_PORT)/$(S3_BUCKET)/$$granule); \
	echo "range GET $$granule -> HTTP $$code"; \
	case "$$code" in \
	  206) echo "  OK: anonymous range requests work, the COG HTTP reader will too";; \
	  200) echo "  WARN: range ignored, whole files will be fetched (COG gains lost)";; \
	  403) echo "  FAIL: bucket is not publicly readable -- re-run: make seed-s3";; \
	  *)   echo "  FAIL: is LocalStack up? make ps";; \
	esac

# --- driving the client ------------------------------------------------------

ui:  ## Print the GeoServer web UI URL and login
	@echo "GeoServer web UI (note the /geoserver context path -- the bare host:port 404s):"
	@echo "    $(URL)/web"
	@echo "  login: $(GEOSERVER_USER) / $(GEOSERVER_PASSWORD)"
	@ip=$$(ip -4 addr show eth0 2>/dev/null | awk '/inet /{print substr($$2,1,index($$2,"/")-1)}'); \
	 if [ -n "$$ip" ]; then \
	   echo "  if localhost does not resolve from your browser, try:"; \
	   echo "    $$(echo $(URL) | sed "s#//localhost#//$$ip#")/web"; \
	 fi
	@printf '  reachable now: '; curl -s -o /dev/null -w 'HTTP %{http_code}\n' $(URL)/web || echo "no answer -- make ps"

check:  ## Report the server version and which COG plugins installed
	$(UV) python examples/driver.py --url $(URL) check

driver:  ## Build all three demo mosaics (local, remote, upload)
	$(UV) python examples/driver.py --url $(URL) --replace all

mosaic:  ## Build one mosaic: make mosaic WHICH=remote|local|upload
	$(UV) python examples/driver.py --url $(URL) --replace $(WHICH)

inspect:  ## Show the live state of the demo mosaics
	$(UV) python examples/driver.py --url $(URL) inspect

clean-gs:  ## Delete the demo workspace from the target server
	$(UV) python examples/driver.py --url $(URL) clean

integration:  ## Run the integration tests against a running stack
	GEOSERVER_URL=$(URL) $(UV) pytest -m integration -v

smoke: check driver inspect  ## check + build everything + report (the usual run)
