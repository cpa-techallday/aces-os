#!/usr/bin/env bash
# aces-console acceptance on the Omarchy rehearsal VM (run as aces over ssh). Read-only towards the rig: no switch,
# no auto-switch, no red-team VM, no simlab write tool, no Mission Control write. Prints one PASS/FAIL line per check.
set -u
pass=0; fail=0
ok()   { echo "PASS $*"; pass=$((pass+1)); }
bad()  { echo "FAIL $*"; fail=$((fail+1)); }
check() { local name=$1; shift; if "$@"; then ok "$name"; else bad "$name"; fi; }
J() { python3 -c "import json,sys; d=json.load(sys.stdin); $1"; }
U=http://127.0.0.1:8070
export U
export -f J

echo "== 1 install (replacement of aces-consoles chosen at pacman's prompt)"
yes | sudo pacman -Sy aces-base aces-console aces-omarchy aces-os 2>&1 | tail -6
check "aces-console 0.1.0-2 installed" bash -c 'pacman -Q aces-console | grep -qx "aces-console 0.1.0-2"'
check "aces-consoles removed (pacman -Qq has no such name)" bash -c '! pacman -Qq | grep -qx aces-consoles'
check "aces-base 0.1.0-4" bash -c 'pacman -Q aces-base | grep -qx "aces-base 0.1.0-4"'
check "aces-os 0.1.0-7 depends on aces-console" bash -c 'pacman -Qi aces-os | grep -q "aces-console"'
check "one launcher entry: /usr/share/applications/aces.desktop" test -f /usr/share/applications/aces.desktop
check "no GRID CONTROL / SIMLAB / MLflow launcher entries left" bash -c '! ls /usr/share/applications | grep -Eqi "grid-control|simlab|mlflow"'
check "static tree installed" test -f /usr/share/aces/console/static/index.html
check "[console] table in /etc/aces/aces.toml" grep -q '^\[console\]' /etc/aces/aces.toml

echo "== 2 start and health"
n_ledger0=$(wc -l < ~/.local/state/aces/ledger.jsonl)
aces-console stop >/dev/null 2>&1 || true
aces-console start 2>&1 | tail -2
curl -s $U/api/health > /tmp/h.json
check "health answers with all six modules installed" bash -c "J 'm=d[\"data\"][\"modules\"]; assert all(v==\"installed\" for v in m.values()) and len(m)==6, m' < /tmp/h.json"
check "served from the package (static_source config)" bash -c "J 'assert d[\"data\"][\"static_source\"]==\"config\"' < /tmp/h.json"
check "binds 127.0.0.1:8070 only" bash -c 'ss -ltn | grep -q "127.0.0.1:8070" && ! ss -ltn | grep -Eq "(0.0.0.0|\*|\[::\]):8070"'
check "server.log and server.pid under ~/.local/state/aces/console" bash -c 'test -f ~/.local/state/aces/console/server.pid && test -f ~/.local/state/aces/console/server.log'

echo "== 3 live reads from the VM (rig over the LAN, records from disk)"
r() { curl -s "$U$1"; }
check "Fabric: rig status with a mode" bash -c "r() { curl -s \"$U\$1\"; }; r /api/rig/status | J 'assert d[\"ok\"] and d[\"data\"][\"mode\"], d'"
check "Agents: Mission Control agents (token or read proxy)" bash -c "curl -s $U/api/mc/agents | J 'assert d[\"data\"][\"n\"]>0 and d[\"data\"][\"via\"] in (\"token\",\"proxy\"), d.get(\"meta\")'"
check "Agents: Mission Control runs" bash -c "curl -s $U/api/mc/runs | J 'assert d[\"data\"][\"n\"]>0'"
check "Agents: missions" bash -c "curl -s $U/api/mc/missions | J 'assert d[\"data\"][\"n\"]>0'"
check "Agents: chat sessions" bash -c "curl -s $U/api/mc/sessions | J 'assert d[\"data\"][\"n\"]>0'"
check "Pipeline: simlab-mcp tools (17)" bash -c "curl -s $U/api/sim/tools | J 'assert d[\"data\"][\"n\"]==17, d[\"data\"][\"n\"]'"
check "Pipeline: SIMLAB experiments" bash -c "curl -s $U/api/sim/experiments | J 'assert d[\"data\"] is not None, d[\"meta\"]'"
check "Pipeline: MLflow experiments" bash -c "curl -s $U/api/mlflow/experiments | J 'assert d[\"data\"][\"n\"]>0, d[\"meta\"]'"
check "Solvers: registry" bash -c "curl -s $U/api/sim/registry | J 'assert d[\"data\"][\"n\"]>0'"
check "Projects: the VM's three projects" bash -c "curl -s $U/api/projects | J 'n=[p[\"name\"] for p in d[\"data\"][\"projects\"]]; assert {\"cad-vm\",\"cad-gold\",\"golden-demo\"} <= set(n), n'"
check "Objects: cad-vm runs" bash -c "curl -s '$U/api/cad/runs?project=cad-vm' | J 'assert len(d[\"data\"][\"runs\"])>=16'"
check "Evidence: golden runs" bash -c "curl -s $U/api/golden/runs | J 'assert len(d[\"data\"][\"runs\"])>=4'"
check "Evidence: desktop ledger verifies" bash -c "curl -s $U/api/ledger/verify | J 'assert d[\"data\"][\"ok\"] is True, d[\"data\"]'"
check "Ops: decisions, red team, model staging" bash -c "for p in /api/rig/decisions /api/rig/redteam /api/rig/downloads; do curl -s $U\$p | J 'assert d[\"ok\"]' || exit 1; done"
check "Ops: GRID CONTROL / SIMLAB / MLflow listed, not openable" bash -c "curl -s $U/api/system/links | J 'l={x[\"id\"]:x for x in d[\"data\"][\"links\"]}; assert all(l[k][\"replaced_by\"] and not l[k][\"openable\"] for k in (\"grid_control\",\"simlab_console\",\"mlflow\"))'"

echo "== 4 human-only writes"
code() { curl -s -o /dev/null -w "%{http_code}" -X POST "$U$1" -H "Content-Type: application/json" "${@:2}" -d '{}'; }
check "POST without the human headers is 403 (sim tool)" bash -c "[ \$(curl -s -o /dev/null -w '%{http_code}' -X POST $U/api/sim/tool/sim_list_experiments -H 'Content-Type: application/json' -d '{}') = 403 ]"
check "POST from another origin is 403 (auto-switch)" bash -c "[ \$(curl -s -o /dev/null -w '%{http_code}' -X POST $U/api/rig/auto -H 'Content-Type: application/json' -H 'X-ACES-Intent: human' -H 'Origin: http://evil.example' -d '{\"enabled\":true,\"confirm\":\"enable auto-switch\"}') = 403 ]"
check "a write tool without its typed name is refused (400)" bash -c "[ \$(curl -s -o /dev/null -w '%{http_code}' -X POST $U/api/sim/tool/sim_cancel -H 'Content-Type: application/json' -H 'X-ACES-Intent: human' -H 'Origin: $U' -d '{\"arguments\":{\"experiment_id\":\"none\"}}') = 400 ]"
check "a read tool runs (sim_whoami)" bash -c "curl -s -X POST $U/api/sim/tool/sim_whoami -H 'Content-Type: application/json' -H 'X-ACES-Intent: human' -H 'Origin: $U' -d '{\"arguments\":{}}' | J 'assert d[\"ok\"], d'"
n_ledger1=$(wc -l < ~/.local/state/aces/ledger.jsonl)
check "no ledger entry from refused or read actions ($n_ledger0 -> $n_ledger1)" [ "$n_ledger0" = "$n_ledger1" ]
check "aces ledger verify" bash -c 'aces ledger verify >/dev/null'

echo "RESULT: $pass passed, $fail failed"
