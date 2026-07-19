PYTHON ?= python3
HOST ?= 127.0.0.1
PORT ?= 8765
BUNDLED_NODE_MODULES ?=

.PHONY: setup link-node-modules build-vision run scan

setup:
	$(PYTHON) -m venv .venv
	.venv/bin/python -m pip install -e .
	$(MAKE) link-node-modules
	$(MAKE) build-vision

link-node-modules:
	@if test -e node_modules; then true; \
	elif test -n "$(BUNDLED_NODE_MODULES)"; then ln -s "$(BUNDLED_NODE_MODULES)" node_modules; \
	else echo "未配置 BUNDLED_NODE_MODULES；Excel 导出需要 DOCREVIEW_NODE_MODULES"; fi

build-vision:
	mkdir -p .runtime
	mkdir -p .runtime/clang-module-cache
	clang -O2 -fobjc-arc -fmodules-cache-path=.runtime/clang-module-cache \
		-framework Foundation -framework AppKit -framework Vision \
		tools/vision_ocr.m -o .runtime/vision_ocr

run: build-vision link-node-modules
	PYTHONPATH=src $(PYTHON) -m docreview serve --host $(HOST) --port $(PORT)

scan: build-vision link-node-modules
	PYTHONPATH=src $(PYTHON) -m docreview scan --source datas --keywords-file keywords.txt
