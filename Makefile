PYTHON ?= python3
BATCHES ?= 4

.PHONY: install-dev test test-batches lint bump release clean

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
	$(PYTHON) -m ruff check --select E9,F63,F7 pudge tests scripts
	$(PYTHON) -m ruff check --select E9,F pudge/audiobooks.py pudge/database.py pudge/light_novels.py pudge/manga.py pudge/manga_ocr_worker.py pudge/metadata_cache.py pudge/reading_audio_alignment.py pudge/web_app.py pudge/subtitles pudge/backup.py tests/test_p0_p1_features.py tests/test_ui_integration.py tests/test_v072_reading_experience.py
	node --check pudge/web/settings.js
	node --check pudge/web/media.js
	node --check pudge/web/reading_tools.js
	node --check pudge/web/manga_reader_v2.js

release:
	./build_release.sh

clean:
	rm -rf build dist *.egg-info .pytest_cache .ruff_cache
	find pudge tests scripts -type d -name __pycache__ -prune -exec rm -rf {} +
