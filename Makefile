.PHONY: run run-gpu run-local prepare-model stop logs smoke smoke-live
run run-gpu run-local prepare-model stop logs smoke smoke-live:
	$(MAKE) -C track_2b $@
