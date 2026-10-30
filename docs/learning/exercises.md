# Hands-on exercises

Run these yourself, in order. Each one lists a command, what to look for, and a question to answer.
Prerequisites: Docker, plus `kubectl` and `k3d` for Part C
(`curl -s https://raw.githubusercontent.com/k3d-io/k3d/main/install.sh | bash`).

## Part A — the runtime (no Docker)
1. Install from the lockfiles (see README, Quick start), then `pytest`: every test passes offline.
2. `agent-lab run "What is (2+3)*4?"`: find `tool_calls` and `requests` in the output.
   *Why are there 2 requests?* The first produced the tool call. The second read the result and produced the answer.
3. `AGENT_LAB_MAX_REQUESTS=1 agent-lab run "What is 2+2?"`: the status is `failed` with `UsageLimitExceeded`.
   This is the bounded loop at work.
4. Optional: with a Gemini key, run `AGENT_LAB_PROVIDER=gemini agent-lab run "What's 15% of 240, and what is k3s?"`
   Compare the tool calls the real model chose with the mock's choices.

## Part B — Docker
1. `docker build -t agent-lab:dev .`, then `docker images agent-lab`. Note the size.
   Then `docker history agent-lab:dev` to see the layers.
2. Edit `src/agent_lab/agents/demo.py` (for example, a note's text) and rebuild.
   *Which steps were `CACHED`?*
3. `docker run --rm agent-lab:dev healthcheck`: CMD is overridden.
   `docker run --rm --entrypoint id agent-lab:dev` shows that the UID is 10001, not root.
4. `docker run --rm --network none agent-lab:dev`: the mock still works without a network.
5. `docker compose run --rm agent run "What is 9*9?"` twice, then
   `docker compose run --rm --entrypoint cat agent /data/runs/runs.jsonl`: two lines.
   The named volume persisted them. `docker volume ls` lists it. `docker compose down -v` deletes it.
6. `docker run --rm --entrypoint sh agent-lab:dev -c 'touch /x'` succeeds (the container root filesystem is writable),
   while `docker compose run --rm --entrypoint sh agent -c 'touch /x'` fails because Compose sets `read_only`.
7. `docker compose --profile test run --rm test`: tests run inside a container against your bind-mounted source.

## Part C — Kubernetes with k3d
1. `k3d cluster create lab`, then `kubectl get nodes`: one node, which is a Docker container
   (`docker ps` shows it).
2. `k3d image import agent-lab:dev -c lab`
3. `kubectl apply -k k8s/`, then `kubectl -n agent-lab get cronjob,pvc,configmap`.
   *Why is the PVC `Pending`?* local-path binds storage only when a Pod first uses it (`WaitForFirstConsumer`).
4. `kubectl -n agent-lab create job run1 --from=cronjob/agent-lab-demo`, then
   `kubectl -n agent-lab get pods -w` until the Pod shows `Completed`.
   Then `kubectl -n agent-lab logs job/run1`.
5. `kubectl -n agent-lab get pvc`: now it is `Bound`. Run a second job (`run2`), then inspect the shared storage:
   `kubectl -n agent-lab run peek --rm -it --image=busybox --restart=Never --overrides='{"spec":{"volumes":[{"name":"r","persistentVolumeClaim":{"claimName":"agent-lab-runs"}}],"containers":[{"name":"peek","image":"busybox","command":["cat","/r/runs.jsonl"],"volumeMounts":[{"name":"r","mountPath":"/r"}]}]}}'`
6. Change `AGENT_LAB_MAX_REQUESTS` to `"1"` in `k8s/configmap.yaml`, re-apply, run `run3`, and read the logs.
   *Did the Job show Complete or Failed?* The CLI exits 1, so the Pod fails and the Job retries once (`backoffLimit`).
   Look at `kubectl -n agent-lab describe job run3`. Revert the change afterwards.
7. Set a tiny memory limit (`16Mi`), re-apply, and run a job. Use `kubectl describe pod` to find `OOMKilled`. Revert.
8. Enable the schedule: `kubectl -n agent-lab patch cronjob agent-lab-demo -p '{"spec":{"suspend":false,"schedule":"*/2 * * * *"}}'`.
   Watch Jobs appear with `kubectl -n agent-lab get jobs -w`, then suspend it again.
9. Optional Gemini: `kubectl -n agent-lab create secret generic agent-lab-secrets --from-literal=GEMINI_API_KEY=...`,
   set `AGENT_LAB_PROVIDER: "gemini"` in the ConfigMap, re-apply, and run a job.
10. Clean up: `kubectl delete namespace agent-lab && k3d cluster delete lab`.

## Part D — CI
1. Read `.github/workflows/k3d-smoke.yml` and map each step to Part C.
2. Run `Agent run` manually from the Actions tab with provider `mock`, then download the artifact.
3. Explain why a scheduled run shows as *skipped* until `AGENT_LAB_SCHEDULE_ENABLED=true` is set.
