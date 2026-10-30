# Kubernetes concepts used in Agent Lab

### Cluster, node, control plane
A **cluster** is a set of machines (**nodes**) that run containers. The **control plane**
(API server, scheduler, controllers) decides what runs where. You talk to the API server with `kubectl`.

### K3s and k3d
- **K3s** is a lightweight, CNCF-certified Kubernetes distribution packaged as a single binary.
  It suits one small machine, such as a home server.
- **k3d** runs K3s *inside Docker containers*. A cluster appears in seconds and disappears just as fast,
  which makes it ideal for learning and for CI (`k3d-smoke.yml`). Manifests that work on k3d work on K3s.

### Manifests and declarative state
The YAML files in `k8s/` describe a **desired state**. `kubectl apply` sends them to the API server, and
controllers keep working until the actual state matches. You declare *what* you want, not *how* to get there.

### Namespace (`namespace.yaml`)
A namespace is a named partition of the cluster. Everything in this project lives in `agent-lab`,
so `kubectl delete namespace agent-lab` removes all of it.

### Pod
The smallest unit Kubernetes schedules: one or more containers that share network and storage.
You rarely create Pods directly. Controllers such as Jobs create them for you.

### Job and CronJob (`cronjob.yaml`)
- A **Job** runs Pods until they *complete*. That fits an agent run, which is batch work that exits.
  A **Deployment** is the controller for long-running servers; we don't need one yet.
- A **CronJob** creates a Job on a schedule. Settings used here:
  - `suspend: true`: the schedule is off until you turn it on.
  - `concurrencyPolicy: Forbid`: never two runs at once.
  - `backoffLimit: 1`: retry once.
  - `activeDeadlineSeconds: 300`: kill after 5 minutes.
  - `ttlSecondsAfterFinished`: clean up old Jobs.
- `kubectl create job X --from=cronjob/agent-lab-demo` triggers a run right away.

### ConfigMap and Secret
- A **ConfigMap** holds non-secret settings. `envFrom` turns each key into an environment variable.
- A **Secret** holds sensitive values. Secrets are only base64-*encoded*, not encrypted, so restrict
  who can read them and never commit them. `secretRef.optional: true` lets the Pod start without the
  Secret, in mock mode.

### PersistentVolumeClaim and StorageClass (`pvc.yaml`)
A **PVC** requests storage. The **StorageClass** `local-path`, built into K3s, satisfies the request
with a directory on the node. Pods mount the claim, so `runs.jsonl` survives from one Pod to the next.

### Resource requests and limits
- `requests` is what the scheduler reserves for the container.
- `limits` is the hard ceiling. A container that goes over its memory limit is killed (OOMKilled).
  CPU above the limit is throttled.

### securityContext
This is the Kubernetes version of the Docker hardening flags:
- `runAsNonRoot` and `runAsUser: 10001` run the container as an unprivileged user.
- `readOnlyRootFilesystem` makes the root filesystem read-only.
- `capabilities.drop: [ALL]` removes Linux capabilities.
- `allowPrivilegeEscalation: false` blocks privilege escalation.
- `seccompProfile: RuntimeDefault` applies the runtime's default system-call filter.

`automountServiceAccountToken: false` means the Pod gets no credentials for the Kubernetes API. It doesn't need any.

### imagePullPolicy and image import
`IfNotPresent` uses the image already on the node. `k3d image import` copies your local image
into the node, so no registry is needed.

### Kustomize (`kustomization.yaml`)
Kustomize, built into `kubectl`, lists the files to apply together: `kubectl apply -k k8s/`.
Later you can add *overlays*, for example `k8s/overlays/home`, that patch values without copying files.
The example Secret is deliberately left out of the list.
