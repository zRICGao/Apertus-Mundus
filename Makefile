.PHONY: run run-gpu run-local run-remote run-remote-gpu prepare-model stop logs smoke smoke-live package
run run-gpu run-local run-remote run-remote-gpu prepare-model stop logs smoke smoke-live package:
	$(MAKE) -C track_2b $@
