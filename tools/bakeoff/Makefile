# This directory has no targets of its own. The real Makefile is at the repo
# root; forward everything there so `make <target>` works from inside the
# bake-off directory too (its venv lives here, so shells often end up here).
ROOT := $(shell git rev-parse --show-toplevel)

.DEFAULT_GOAL := help

%:
	@echo "→ running from $(ROOT)"
	@$(MAKE) --no-print-directory -C $(ROOT) $@

help:
	@$(MAKE) --no-print-directory -C $(ROOT) help
