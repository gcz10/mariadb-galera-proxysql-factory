# Makefile — stabilny interfejs operatora.
# Komendy dodawane INKREMENTALNIE wraz z działającym feature.
# Komendy poniżej są ZAAWANSOWANE — wymagają hostów i gotowych playbooków.
# Działają dopiero od odpowiedniego feature.

.PHONY: help cluster-discover cluster-validate

CLUSTER ?= example-cluster
ANSIBLE_OPTS ?=

help:  ## Pokaż dostępne komendy
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | awk 'BEGIN{FS=":.*?## "}{printf "  %-28s %s\n", $$1, $$2}'

# F0 — przygotowany, wymaga hostów (BLK-1, BLK-2)
cluster-discover:  ## F0 Discovery — zbierz fakty z hostów (read-only)
	ansible-playbook playbooks/f0_discovery.yml -i clusters/$(CLUSTER)/inventory.yml $(ANSIBLE_OPTS)

# F1 — do zaimplementowania
cluster-validate:  ## Waliduj konfigurację klastra (schema + preflight, check mode)
	@echo "TODO F1: walidacja schema + preflight"
