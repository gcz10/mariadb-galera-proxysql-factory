# Makefile — stabilny interfejs operatora.
# Komendy dodawane INKREMENTALNIE wraz z działającym feature.

.PHONY: help cluster-discover cluster-validate cluster-deploy cluster-bootstrap cluster-health

CLUSTER ?= example-cluster
ANSIBLE_OPTS ?=

help:  ## Pokaż dostępne komendy
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | awk 'BEGIN{FS=":.*?## "}{printf "  %-28s %s\n", $$1, $$2}'

cluster-discover:  ## F0 Discovery — zbierz fakty z hostów (read-only)
	ansible-playbook playbooks/f0_discovery.yml -i clusters/$(CLUSTER)/inventory.yml $(ANSIBLE_OPTS)

cluster-validate:  ## Waliduj konfigurację klastra (schema + preflight)
	python3 tests/validation/validate-cluster-schema.py clusters/$(CLUSTER)/cluster.yml
	ansible-playbook playbooks/f2_preflight.yml -i clusters/$(CLUSTER)/inventory.yml $(ANSIBLE_OPTS)

cluster-deploy:  ## F2+F3 — instaluj pakiety + konfiguruj (idempotentny converge)
	ansible-playbook playbooks/f2_install.yml -i clusters/$(CLUSTER)/inventory.yml $(ANSIBLE_OPTS)
	ansible-playbook playbooks/site.yml -i clusters/$(CLUSTER)/inventory.yml $(ANSIBLE_OPTS)

cluster-bootstrap:  ## F4 — initial bootstrap (JEDEN węzeł, wymaga confirm=yes)
	ansible-playbook playbooks/bootstrap.yml -i clusters/$(CLUSTER)/inventory.yml -e confirm=yes -l gnode1 $(ANSIBLE_OPTS)

cluster-health:  ## Weryfikuj cluster status (wsrep)
	ansible-playbook playbooks/f2_preflight.yml -i clusters/$(CLUSTER)/inventory.yml $(ANSIBLE_OPTS) --check
	@echo "Galera status per node:"
	@ansible galera -i clusters/$(CLUSTER)/inventory.yml -m shell -a "mariadb --socket=/var/lib/mysql/mysql.sock -N -B -e \"SHOW STATUS WHERE Variable_name IN ('wsrep_cluster_status','wsrep_cluster_size','wsrep_connected','wsrep_ready','wsrep_local_state')\"" $(ANSIBLE_OPTS)
