.PHONY: install test test-rules check-configs dashboard up down logs query

PROM_IMAGE ?= prom/prometheus:v3.2.1
AM_IMAGE   ?= prom/alertmanager:v0.28.1
OTEL_IMAGE ?= otel/opentelemetry-collector-contrib:0.120.0

install:
	pip install -e ".[dev]"

test:
	pytest -q

# Unit-test the alert rules with promtool (also run in CI).
test-rules:
	docker run --rm -v $(PWD)/deploy/prometheus:/p -w /p --entrypoint promtool $(PROM_IMAGE) check rules rules/recording.yml rules/alerts.yml
	docker run --rm -v $(PWD)/deploy/prometheus:/p -w /p/tests --entrypoint promtool $(PROM_IMAGE) test rules alerts_test.yml

check-configs: test-rules
	docker run --rm -v $(PWD)/deploy/alertmanager:/a --entrypoint amtool $(AM_IMAGE) check-config /a/alertmanager.yml
	docker run --rm -v $(PWD)/deploy/otel-collector:/c $(OTEL_IMAGE) validate --config=/c/config.yaml
	docker compose config -q

dashboard:
	python scripts/build_dashboard.py

up:
	docker compose up -d --build

down:
	docker compose down

logs:
	docker compose logs -f agent mcp-server

query:
	curl -s localhost:8080/v1/query -H 'content-type: application/json' \
	  -d '{"query":"Give me a short summary of Apple Inc. and its stock performance over the last 30 days."}' | python -m json.tool
