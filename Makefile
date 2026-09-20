PYTHON ?= $(if $(wildcard .venv-test/bin/python),.venv-test/bin/python,python3)
BATCHES ?= 4

.PHONY: install-dev test test-batches lint quality bump release build-release clean

install-dev:
	$(PYTHON) -m pip install -e ".[dev,sync]"

test:
	@runtime=$$(mktemp -d "$${TMPDIR:-/tmp}/pudge-pytest-runtime.XXXXXX"); \
	trap 'rm -rf "$$runtime"' EXIT; \
	PUDGE_HOME="$$runtime/home" PUDGE_RUNTIME_LOG_PATH="$$runtime/runtime.log" $(PYTHON) -m pytest -q

test-batches:
	@results=$${PUDGE_TEST_RESULTS_DIR:-"$${TMPDIR:-/tmp}/pudge-test-batches.$$$$"}; \
	mkdir -p "$$results"; \
	status=0; i=0; while [ $$i -lt $(BATCHES) ]; do \
		echo "== Test batch $$((i+1))/$(BATCHES) =="; \
		$(PYTHON) scripts/run_test_batch.py --batch $$i --batches $(BATCHES) --results-dir "$$results/batch-$$i" || status=$$?; \
		i=$$((i+1)); \
	done; \
	echo "Test results: $$results"; \
	exit $$status

bump:
	@test -n "$(VERSION)" || (echo "Usage: make bump VERSION=0.6.69" && exit 2)
	$(PYTHON) scripts/bump_version.py $(VERSION)

lint:
	$(PYTHON) -m ruff check --select E9,F pudge scripts
	$(PYTHON) -m ruff check --select E9,F63,F7 tests
	$(PYTHON) scripts/sync_config_example.py --check
	find pudge/web -type f -name '*.js' -print0 | xargs -0 -n1 node --check

quality: lint
	$(PYTHON) -m ruff check --select BLE001 pudge/task_supervisor.py pudge/identity.py pudge/cache_registry.py pudge/diagnostics.py pudge/safe_mode.py pudge/web_state.py pudge/web_controllers.py pudge/manager_services.py pudge/providers/base.py
	$(PYTHON) -m ruff format --check pudge/alignment_replay.py pudge/cache_registry.py pudge/diagnostics.py pudge/identity.py pudge/manager_services.py pudge/providers/base.py pudge/safe_mode.py pudge/secrets_store.py pudge/subtitles/pipeline.py pudge/task_supervisor.py pudge/web_controllers.py pudge/web_state.py scripts/check_release_metadata.py scripts/sync_config_example.py scripts/torrent_backtest.py tests/test_audit_properties.py tests/test_audit_v0722_stabilization.py tests/test_performance_budgets.py
	$(PYTHON) -m mypy --follow-imports=skip pudge/task_supervisor.py pudge/identity.py pudge/cache_registry.py pudge/diagnostics.py pudge/safe_mode.py pudge/web_state.py pudge/providers/base.py
	$(PYTHON) scripts/check_release_metadata.py

release:
	@test -n "$(VERSION)" || (echo "Usage: make release VERSION=0.7.23" && exit 2)
	$(PYTHON) scripts/release.py "$(VERSION)" --python "$(PYTHON)"

build-release:
	./build_release.sh

clean:
	rm -rf build dist *.egg-info .pytest_cache .ruff_cache
	find pudge tests scripts -type d -name __pycache__ -prune -exec rm -rf {} +

PUDGE_BENCH_DATA ?= $(HOME)/Downloads/pudge-bench-data
PUDGE_RUNTIME_PYTHON ?= $(HOME)/.local/share/pudge/venv/bin/python
JMANGA_REPO ?= $(PUDGE_BENCH_DATA)/JMangaBench_Mixed-repo
JMANGA_BENCHMARK ?= $(PUDGE_BENCH_DATA)/JMangaBench_Mixed
MANGA109_ROOT ?= $(PUDGE_BENCH_DATA)/Manga109-v2026
BENCH_TAG ?= $(shell date +%Y%m%d-%H%M%S)
BENCH_OUT ?= $(PUDGE_BENCH_DATA)/results/$(BENCH_TAG)

.PHONY: benchmark-setup benchmark-jmanga benchmark-manga109 benchmark-compare

benchmark-setup:
	bash scripts/setup_manga_ocr_benchmark.command

benchmark-jmanga:
	@mkdir -p "$(BENCH_OUT)"
	PYTHONPATH="$(CURDIR)" "$(PUDGE_RUNTIME_PYTHON)" -m pudge.manga_benchmark_cli jmanga-predict \
		--benchmark "$(JMANGA_BENCHMARK)" \
		--output "$(BENCH_OUT)/jmanga-predictions.jsonl"
	cd "$(JMANGA_REPO)" && uv run python -m jmangabench validate-predictions \
		--benchmark "$(JMANGA_BENCHMARK)" \
		--predictions "$(BENCH_OUT)/jmanga-predictions.jsonl"
	cd "$(JMANGA_REPO)" && uv run python -m jmangabench evaluate \
		--benchmark "$(JMANGA_BENCHMARK)" \
		--predictions "$(BENCH_OUT)/jmanga-predictions.jsonl" \
		--output "$(BENCH_OUT)/jmanga-evaluation"
	PYTHONPATH="$(CURDIR)" "$(PUDGE_RUNTIME_PYTHON)" -m pudge.manga_benchmark_cli jmanga-wrap-report \
		--report "$(BENCH_OUT)/jmanga-evaluation/report.json" \
		--predictions "$(BENCH_OUT)/jmanga-predictions.jsonl" \
		--benchmark-root "$(JMANGA_BENCHMARK)" \
		--output "$(BENCH_OUT)/jmanga-report.json"
	@echo "JMangaBench result: $(BENCH_OUT)/jmanga-report.json"

benchmark-manga109:
	@mkdir -p "$(BENCH_OUT)"
	PYTHONPATH="$(CURDIR)" "$(PUDGE_RUNTIME_PYTHON)" -m pudge.manga_benchmark_cli manga109 \
		--images-root "$(MANGA109_ROOT)/images" \
		--annotations-root "$(MANGA109_ROOT)/annotations" \
		--output-dir "$(BENCH_OUT)/manga109"
	@echo "Manga109 result: $(BENCH_OUT)/manga109/report.json"

benchmark-compare:
	@test -n "$(OLD)" -a -n "$(NEW)" || (echo "Usage: make benchmark-compare OLD=/path/old.json NEW=/path/new.json [OUT=/path/compare.json]" && exit 2)
	PYTHONPATH="$(CURDIR)" $(PYTHON) -m pudge.manga_benchmark_cli compare "$(OLD)" "$(NEW)" $(if $(OUT),--output "$(OUT)",)
