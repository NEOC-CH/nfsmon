# nfsmon has no build step; these targets are the project's checks.
# .RECIPEPREFIX avoids tab-vs-space mishaps in recipes (GNU make >= 3.82).
.RECIPEPREFIX = >
PY ?= python3

.PHONY: check compile test

check: compile test

compile:
> $(PY) -m py_compile nfsmon.py

test:
> $(PY) -m unittest discover -s tests -v
