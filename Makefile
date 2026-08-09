PYTHON ?= python3
BATCHES ?= 4

.PHONY: install-dev test test-batches lint bump release clean

install-dev:
	$(PYTHON) -m pip install -e ".[dev,sync]"

test:
	$(PYTHON) -m pytest -q

test-batches:
	@i=0; while [ $$i -lt $(BATCHES) ]; do \
		echo "== Test batch $$((i+1))/$(BATCHES) =="; \
		$(PYTHON) scripts/run_test_batch.py --batch $$i --batches $(BATCHES) || exit $$?; \
		i=$$((i+1)); \
	done

bump:
	@test -n "$(VERSION)" || (echo "Usage: make bump VERSION=0.6.69" && exit 2)
	$(PYTHON) scripts/bump_version.py $(VERSION)

lint:
	$(PYTHON) -m ruff check anime_mpv tests scripts

release:
	./build_release.sh

clean:
	rm -rf build dist *.egg-info .pytest_cache .ruff_cache
	find anime_mpv tests scripts -type d -name __pycache__ -prune -exec rm -rf {} +
