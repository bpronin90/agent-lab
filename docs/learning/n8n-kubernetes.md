# Experiment 2: n8n triggers agent Kubernetes Jobs

n8n starts the demo agent as a Kubernetes Job on a throwaway k3d cluster. It then
polls the Job until it finishes, reads the agent's JSON run record from the pod
log, and reports the outcome. It uses the mock provider only, with no schedule
and no permanent infrastructure.

```text
 n8n container (Docker, 127.0.0.1:5678)                k3d cluster "agent-lab-n8n"
 ┌──────────────────────────────────────┐  private   ┌───────────────────────────────────┐
 │ "n8n + k8s experiment" (Manual)      │  Docker    │ kube-apiserver :6443              │
 │   Scenarios ──► Run agent Job (each) │  network   │   authn: ServiceAccount token     │
 │ "Run k8s Job" (sub-workflow)         │ ─────────► │   authz: Role n8n-job-runner      │
 │   Validate ► Build manifest ► POST   │  HTTPS +   │   admission: ValidatingAdmission- │
 │   ► Wait 2s ► GET status ◄─┐ loop    │  cluster   │     Policy + Pod Security         │
 │   ► (gave up? DELETE) ► list pods    │  CA        │ Job ► Pod ► agent-lab:dev         │
 │   ► GET pod log ► Build report       │            │   stdout = RunRecord JSON         │
 └──────────────────────────────────────┘            └───────────────────────────────────┘
```

| File | Role |
| --- | --- |
| `k8s/n8n/rbac.yaml` | `n8n-job-runner` ServiceAccount and a namespace Role: create/get/delete Jobs, list pods, read pod logs. Nothing else. |
| `k8s/n8n/admission-policy.yaml` | Limits what that ServiceAccount's Jobs may contain: image, `args = [run, --, prompt]`, no `command`, lifecycle hooks or probes, an env allowlist, no secrets, no SA token, not privileged, a bounded deadline, one pod, and resource limits at most 500m CPU and 256Mi memory. |
| `k8s/n8n/namespace-psa.yaml` | Pod Security `restricted` on `agent-lab`. |
| `n8n/agent-lab-run-k8s-job.json` | Sub-workflow that does the work for one prompt. |
| `n8n/agent-lab-n8n-k8s-experiment.json` | Manual-trigger workflow that runs the scenarios through the sub-workflow. |

The workflow files reference two n8n credentials by ID, and neither credential is
committed. One is a Bearer token and the other is the cluster CA. Both are created
locally in step 4.

## Reproduce

You need Docker, the `agent-lab:dev` image build, and a running n8n container
(here `fantasy-n8n`, n8n 2.45). Nothing below touches other workflows, credentials,
or volumes.

```bash
# 1. Pinned, checksum-verified tools in a temp dir (no sudo).
WORK=$(mktemp -d); export PATH=$WORK:$PATH KUBECONFIG=$WORK/kubeconfig; umask 077
curl -fsSL -o $WORK/k3d https://github.com/k3d-io/k3d/releases/download/v5.8.3/k3d-linux-amd64
echo "dbaa79a76ace7f4ca230a1ff41dc7d8a5036a8ad0309e9c54f9bf3836dbe853e  $WORK/k3d" | sha256sum -c
curl -fsSL -o $WORK/kubectl https://dl.k8s.io/release/v1.31.4/bin/linux/amd64/kubectl
echo "$(curl -fsSL https://dl.k8s.io/release/v1.31.4/bin/linux/amd64/kubectl.sha256)  $WORK/kubectl" | sha256sum -c
chmod +x $WORK/k3d $WORK/kubectl

# 2. Cluster with the API on loopback only, then the image and manifests.
docker build --build-arg GIT_SHA=$(git rev-parse HEAD) -t agent-lab:dev .
k3d cluster create agent-lab-n8n --agents 0 --no-lb --api-port 127.0.0.1:6550 --wait
k3d image import agent-lab:dev -c agent-lab-n8n
kubectl apply -k k8s/ && kubectl apply -f k8s/n8n/

# 3. Let n8n reach the API by name over the cluster's private Docker network.
docker network connect k3d-agent-lab-n8n fantasy-n8n

# 4. Short-lived token and cluster CA, as n8n credentials. The plaintext file is deleted at once.
kubectl -n agent-lab create token n8n-job-runner --duration=4h > $WORK/token
kubectl config view --raw -o jsonpath='{.clusters[0].cluster.certificate-authority-data}' | base64 -d > $WORK/ca.crt
python3 - "$WORK" <<'EOF'
import json, sys, pathlib
w = pathlib.Path(sys.argv[1])
(w / "creds.json").write_text(json.dumps([
  {"id": "AgLabK8sTokenCr1", "name": "Agent Lab k8s token (n8n-job-runner)", "type": "httpBearerAuth",
   "data": {"token": (w / "token").read_text().strip()}},
  {"id": "AgLabK8sCACert01", "name": "Agent Lab k3d cluster CA", "type": "httpSslAuth",
   "data": {"ca": (w / "ca.crt").read_text()}}]))
EOF
docker cp $WORK/creds.json fantasy-n8n:/tmp/creds.json
docker exec fantasy-n8n sh -c 'n8n import:credentials --input=/tmp/creds.json; rm -f /tmp/creds.json'
rm -f $WORK/creds.json $WORK/token

# 5. Import both workflows. Publish the sub-workflow: n8n 2.x runs only the published
#    version of a sub-workflow outside the editor. Its only trigger is "called by
#    another workflow", so publishing it schedules nothing.
for f in n8n/*.json; do docker cp $f fantasy-n8n:/tmp/wf.json
  docker exec fantasy-n8n sh -c 'n8n import:workflow --input=/tmp/wf.json; rm -f /tmp/wf.json'; done
docker exec fantasy-n8n n8n publish:workflow --id=AgLabK8sJobRun01

# 6. Run it, either in the editor (open "Agent Lab — n8n + k8s experiment",
#    click Execute workflow) or from the CLI. The CLI needs its own task-broker
#    port because the server already holds 5679.
docker exec -e N8N_RUNNERS_BROKER_PORT=5690 fantasy-n8n \
  n8n execute --id=AgLabK8sExperim1 --rawOutput
kubectl -n agent-lab get jobs -L agent-lab/scenario,agent-lab/n8n-execution
```

To try your own prompt, edit the `Scenarios` Code node. A scenario must be
`success`, `limit`, or `timeout`. The prompt can be any text up to 500 characters.

## Cleanup

```bash
k3d cluster delete agent-lab-n8n                    # Jobs, pods, PVC, RBAC, and the token's signing key
docker network disconnect k3d-agent-lab-n8n fantasy-n8n 2>/dev/null || true  # k3d removes the network
docker exec fantasy-n8n n8n unpublish:workflow --id=AgLabK8sJobRun01
# The token is useless once the cluster is gone. Delete the two "Agent Lab k8s"
# credentials, and both "Agent Lab —" workflows if you no longer want them, in the n8n UI.
rm -rf "$WORK"
```

## Results (local run)

| Scenario | What n8n reported | Kubernetes | Agent run record |
| --- | --- | --- | --- |
| `success` | `SUCCESS`, answer `(12+30)*2 = 84; lookup_note: …` | `Complete`, `CompletionsReached`, exit 0 | `status=succeeded`, tools `calculate`, `lookup_note` |
| `limit` (`AGENT_LAB_MAX_REQUESTS=1`) | `FAILED (agent error_class=limit)` | `Failed`, `BackoffLimitExceeded`, exit 1 | `UsageLimitExceeded: … request_limit of 1` |
| `timeout` (`activeDeadlineSeconds: 1`) | `TIMEOUT (Kubernetes activeDeadlineSeconds)` | `Failed`, `DeadlineExceeded`, pod deleted | none; the pod and its log were gone |
| hostile prompt `--agent=evil; $(rm -rf /) \`id\` what is 2+2` | `SUCCESS`, answer `2+2 = 4` | `Complete` | prompt stored verbatim, nothing executed |
| scenario `cluster-admin` | `REJECTED by n8n before Kubernetes` | no Job created | — |
| n8n poll deadline cut to 3 s (temporary edit) | `TIMEOUT (n8n stopped waiting and deleted the Job)` | Job deleted by n8n | — |

RBAC and admission checks: as `n8n-job-runner`, `kubectl auth can-i` allows
create/get/delete Jobs, list pods, and get pod logs. It denies listing Jobs,
patching Jobs, creating pods, `pods/exec`, reading secrets or ConfigMaps, any
other namespace, nodes, and ClusterRoleBindings. Creating a Job as that account is
denied when the Job uses another image, sets a `command`, adds a lifecycle hook or
probe (both can exec arbitrary commands), omits the `--` argument, has extra env or a
`secretRef`, mounts a `hostPath`, has no deadline, mounts an SA token, sets
`privileged`, asks for more than one pod (`parallelism`/`completions`), or omits or
exceeds the resource limits.

## What each piece teaches

**n8n triggers.** A trigger node starts an execution: Manual, Webhook, Schedule,
"When executed by another workflow", and so on. Here a Manual trigger builds the
items, and the Execute Workflow node (mode *each*) starts one sub-execution per
item. Each Job therefore gets its own n8n execution, and its ID is written onto the
Job as the `agent-lab/n8n-execution` label so you can trace either direction. In
n8n 2.x, a sub-workflow called outside the editor, for example from the CLI,
another trigger, or production, runs only its *published* version. The CLI also
runs in `cli` mode, so it hits this rule while an editor run uses the draft. A
production webhook trigger would be the next step, but it means publishing a
workflow with an external entry point, so it is left out here.

**Controlling Jobs from outside.** A Job is just an API object. "Run the agent"
is `POST /apis/batch/v1/namespaces/agent-lab/jobs` with a manifest that uses
`generateName`, and the API server returns the name it chose. "Check it" is a
`GET` on that name, reading `status.conditions[type=Complete|Failed]` and its
`reason`. "Stop it" is a `DELETE` with `propagationPolicy=Background`, which also
deletes the pod. The result comes from the pod log (`GET …/pods/<pod>/log`),
because the agent prints its `RunRecord` as JSON on stdout. `kubectl` makes these
same calls.

**Authentication and RBAC.** n8n authenticates with a ServiceAccount token from
`kubectl create token --duration=4h`. It is bound to that account, expires on its
own, and is stored only in n8n's encrypted credential store. It verifies the server
against the cluster CA rather than skipping TLS. Authorization is a namespaced
`Role`, not a ClusterRole. But `create jobs` effectively means "run any pod", so
RBAC alone is not enough. The `ValidatingAdmissionPolicy` limits *what* the account
can create, and Pod Security `restricted` is a second layer at pod creation.
One thing we saw: a privileged Job got past Pod Security, which checks pods and not
Jobs, and then failed at pod creation with `FailedCreate` events. The policy rule
now rejects it at Job creation. A policy change also took about a second to take
effect.

**Untrusted input.** The scenario chooses from a fixed table in the Validate node,
and n8n never passes through an image, env, or deadline. The prompt is
length-checked, rejected if it contains control characters, and placed in a single
argv element after `--`. No shell ever runs it. The `--` matters: before it,
`agent-lab run "--help"` printed the CLI help, because argparse read the prompt as
a flag. The admission policy requires the `--` too, so a modified workflow cannot
drop it.

**Polling versus events.** This workflow polls every 2 s, which is simple, stateless,
and needs only `get`. The Kubernetes alternative is a watch
(`GET …/jobs?watch=1&fieldSelector=metadata.name=<job>`), a long-lived streaming
response that pushes each change. A watch needs `list`/`watch` permissions and a
client that holds a connection open, which n8n's HTTP node doesn't do. The
event-driven version flips the direction instead: something in the cluster (a
controller, or the Job itself on exit) calls an n8n webhook. That needs n8n to be
reachable from the cluster and the webhook to be authenticated. Polling also has
two deadlines: Kubernetes `activeDeadlineSeconds` kills the Job, and n8n's own
90 s limit stops waiting and deletes it. Either one alone leaves a gap.

**How errors propagate.** The chain is: agent `error_class` → CLI exit code (1
limit, 2 config, 3 transient) → container `terminated.exitCode` → pod phase →
Job condition `Failed` with `reason` (`BackoffLimitExceeded`, `DeadlineExceeded`) →
n8n report `outcome`. `backoffLimit: 0` is deliberate. The agent's runtime already
retries transient errors inside one usage budget, so a Kubernetes retry would spend
a second budget. Input rejected by n8n never reaches Kubernetes. It shows up as a
per-item error because the Execute Workflow node uses *continue on error*. (With
node version 1.2, that setting mangled the outputs. Version 1.3 handles them
correctly.)

**The timeout gap.** A Job killed by its deadline leaves **no run record**. The pod
is deleted, so its log and the RunRecord it never printed are gone. The runtime's
`AGENT_LAB_DEADLINE_SECONDS` (120 s) is meant to fire first and record
`error_class=limit`. Keep it shorter than `activeDeadlineSeconds`, and treat the
Kubernetes deadline as a backstop only. n8n is the system of record for this case.

## Moving it to the cloud

Locally, n8n and the cluster share a Docker network on one laptop. In the cloud:

- **The API endpoint.** It is no longer a private Docker hostname. Either run n8n
  *inside* the cluster, where it uses the in-cluster service and a projected,
  auto-rotating ServiceAccount token with no long-lived secret, or keep the API on
  a private network or VPN. Never open it to the internet for n8n.
- **Credentials.** Hand-minted 4-hour tokens don't work for an always-on system.
  In-cluster n8n gets rotated tokens automatically. An external n8n would need
  workload identity or a token refresher, plus an n8n encryption key kept in a
  secret manager.
- **Images.** These come from a registry, pinned by digest. The admission policy
  should pin `registry/agent-lab@sha256:…` instead of `agent-lab:dev`.
- **Results.** Logs disappear with pods (TTL, deadline, node loss). The agent should
  write its RunRecord to durable storage (the proposed `agent_lab` Supabase schema),
  and n8n should read it from there instead of from logs.
- **Events.** Once n8n is reachable inside the cluster, a callback to an
  authenticated webhook can replace polling.

**Recommendation (no laptop, zero cost).** Use one free-tier VM, such as the Oracle
Cloud Always Free Arm instance, running K3s, with n8n deployed *into* the same
cluster. Give it a persistent volume, a namespace, and the same Role and policy, and
have n8n use its in-cluster ServiceAccount. Expose nothing publicly: reach the n8n
UI through Tailscale or an SSH tunnel, and keep the Kubernetes API on the
tailnet/localhost only. This keeps the model from this experiment: Jobs, RBAC,
admission policy, polling. Only the network path and the token source change. Each
part is an owner decision and needs its own issue: provisioning, registry, secrets,
and any schedule. GitHub Actions (`agent-run.yml`) remains the simpler option for
scheduled agent runs with no n8n.
