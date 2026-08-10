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
	$(PYTHON) -m ruff check --select E9,F63,F7 pudge tests scripts
	$(PYTHON) -m ruff check --select E9,F pudge/audiobooks.py pudge/manga.py pudge/subtitles pudge/backup.py tests/test_p0_p1_features.py tests/test_ui_integration.py
	node --check pudge/web/settings.js
	node --check pudge/web/media.js

release:
	./build_release.sh

clean:
	rm -rf build dist *.egg-info .pytest_cache .ruff_cache
	find pudge tests scripts -type d -name __pycache__ -prune -exec rm -rf {} +
