#!/usr/bin/env bash
# aces-console 0.1.0-4 on the rehearsal VM: a real golden run (demo, --lane none: computed on this VM only, nothing sent
# to the rig) and real CAD actions (inspect, render) started through the console's own API, as the page does.
set -u
U=http://127.0.0.1:8070
H=(-H "Content-Type: application/json" -H "X-ACES-Intent: human" -H "Origin: $U")
pass=0; fail=0
ok() { echo "PASS $*"; pass=$((pass+1)); }
bad() { echo "FAIL $*"; fail=$((fail+1)); }
J() { python3 -c "import json,sys; d=json.load(sys.stdin); print($1)"; }
wait_job() {
  for i in $(seq 1 400); do
    s=$(curl -s "$U/api/job/$1" | J 'd["data"]["status"]')
    [ "$s" != "running" ] && { echo "$s"; return; }
    sleep 1
  done
  echo "timeout"
}

echo "== install 0.1.0-4"
yes | sudo pacman -Sy aces-console 2>&1 | tail -2
pacman -Q aces-console | grep -qx "aces-console 0.1.0-4" && ok "aces-console 0.1.0-4 installed" || bad "aces-console 0.1.0-3 installed"
aces-console stop >/dev/null 2>&1; aces-console start 2>&1 | tail -1
curl -s $U/api/health | J '"modules " + ",".join(k for k,v in d["data"]["modules"].items() if v=="installed")'
n0=$(wc -l < ~/.local/state/aces/ledger.jsonl)
runs0=$(ls ~/Work/golden-demo/golden | wc -l)

echo "== a golden run from the console (demo root-mount, lane none)"
r=$(curl -s -X POST $U/api/golden/run "${H[@]}" -d '{"demo":"root-mount","confirm":"golden-demo","options":{"lane":"none"},"note":"started from the aces-console API in the 0.1.0-4 acceptance"}')
job=$(echo "$r" | J 'd["data"]["job"]')
echo "job $job"
st=$(wait_job "$job")
curl -s "$U/api/job/$job" > /tmp/golden-job.json
J '"status=%s exit=%s run=%s outcome=%s grade=%s duration=%ss lines=%s" % (d["data"]["status"], d["data"]["exit"], d["data"]["result_run_id"], d["data"]["result_outcome"], d["data"]["result_grade"], d["data"]["duration_s"], d["data"]["lines"])' < /tmp/golden-job.json
[ "$st" = "done" ] && ok "golden run job done" || bad "golden run job: $st"
runs1=$(ls ~/Work/golden-demo/golden | wc -l)
[ "$runs1" -gt "$runs0" ] && ok "a new run directory under golden-demo/golden ($runs0 -> $runs1)" || bad "no new run directory"
rid=$(J 'd["data"]["result_run_id"]' < /tmp/golden-job.json)
curl -s "$U/api/golden/runs?project=golden-demo" | J '"listed by the console: " + str(any(r["run_id"]=="'"$rid"'" for r in d["data"]["runs"]))'
aces golden verify ~/Work/golden-demo/golden/$rid --json --no-model > /tmp/verify.json 2>/dev/null; echo "aces golden verify exit $?"

echo "== CAD actions from the console (cad-vm, latest run)"
for body in '{"project":"cad-vm","action":"inspect","run":"latest"}' '{"project":"cad-vm","action":"render","run":"latest","options":{"size":"small"}}'; do
  r=$(curl -s -X POST $U/api/cad/action "${H[@]}" -d "$body")
  job=$(echo "$r" | J 'd["data"]["job"]')
  st=$(wait_job "$job")
  curl -s "$U/api/job/$job" | J '"%s: status=%s exit=%s ok=%s" % (d["data"]["kind"], d["data"]["status"], d["data"]["exit"], (d["data"]["result"] or {}).get("ok"))'
  [ "$st" = "done" ] && ok "$(echo "$body" | J 'd["action"]') job done" || bad "$(echo "$body" | J 'd["action"]') job: $st"
done

echo "== ledger"
n1=$(wc -l < ~/.local/state/aces/ledger.jsonl)
tail -n $((n1-n0)) ~/.local/state/aces/ledger.jsonl | python3 -c "import json,sys; print([json.loads(l)['kind'] for l in sys.stdin])"
aces ledger verify >/dev/null && ok "aces ledger verify ($n0 -> $n1 entries)" || bad "aces ledger verify"
echo "RESULT: $pass passed, $fail failed"
