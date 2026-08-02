# Contributing

This repository holds crawl **recipes** (`recipes/<name>/recipe.yaml`, one
config-driven extraction recipe per careers-site page family) and
browser-playbook **skills** (`skills/<name>/SKILL.md`). Every addition must be
referenced from `manifest.json` (`schema_version: "1"`) so the loader can
discover it. A recipe describes selectors and JSONPath field extractors for a
single page family; a skill describes reusable browser actions shared across
recipes. To contribute, fork the repo, drop your file under `recipes/` or
`skills/`, add its stable id to `manifest.json`, run `make verify` from the
parent project to confirm nothing regressed, then open a pull request
describing the site family you covered and the seed URL you validated against.
