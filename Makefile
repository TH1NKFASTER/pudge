PYTHON ?= python3
BATCHES ?= 4

.PHONY: install-dev test test-batches lint bump release build-release clean

install-dev:
	$(PYTHON) -m pip install -e ".[dev,sync]"

test:
	@runtime=$$(mktemp -d "$${TMPDIR:-/tmp}/pudge-pytest-runtime.XXXXXX"); \
	trap 'rm -rf "$$runtime"' EXIT; \
	PUDGE_HOME="$$runtime/home" PUDGE_RUNTIME_LOG_PATH="$$runtime/runtime.log" $(PYTHON) -m pytest -q

test-batches:
	@runtime=$$(mktemp -d "$${TMPDIR:-/tmp}/pudge-pytest-runtime.XXXXXX"); \
	trap 'rm -rf "$$runtime"' EXIT; \
	i=0; while [ $$i -lt $(BATCHES) ]; do \
		echo "== Test batch $$((i+1))/$(BATCHES) =="; \
		PUDGE_HOME="$$runtime/home" PUDGE_RUNTIME_LOG_PATH="$$runtime/runtime.log" $(PYTHON) scripts/run_test_batch.py --batch $$i --batches $(BATCHES) || exit $$?; \
		i=$$((i+1)); \
	done

bump:
	@test -n "$(VERSION)" || (echo "Usage: make bump VERSION=0.6.69" && exit 2)
	$(PYTHON) scripts/bump_version.py $(VERSION)

lint:
	$(PYTHON) -m ruff check --select E9,F pudge scripts
	$(PYTHON) -m ruff check --select E9,F63,F7 tests
	find pudge/web -type f -name '*.js' -print0 | xargs -0 -n1 node --check

release:
	@test -n "$(VERSION)" || (echo "Usage: make release VERSION=0.7.21" && exit 2)
	$(PYTHON) scripts/release.py "$(VERSION)" --python "$(PYTHON)"

build-release:
	./build_release.sh

clean:
	rm -rf build dist *.egg-info .pytest_cache .ruff_cache
	find pudge tests scripts -type d -name __pycache__ -prune -exec rm -rf {} +
