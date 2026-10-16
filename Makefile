.PHONY: run run-gpu run-local prepare-model stop logs smoke smoke-live package
run run-gpu run-local prepare-model stop logs smoke smoke-live package:
	$(MAKE) -C track_2b $@
