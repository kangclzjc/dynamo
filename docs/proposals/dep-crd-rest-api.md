<!--
SPDX-FileCopyrightText: Copyright (c) 2025-2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
SPDX-License-Identifier: Apache-2.0
-->

# DEP: Native REST APIs for Dynamo CRD Lifecycle Management (Dynadmin)

| Field           | Value                                                            |
| --------------- | ---------------------------------------------------------------- |
| **Area**        | external-api / k8s (cross-ref: dgdr)                             |
| **Status**      | Draft                                                            |
| **Authors**     | Kang Zhang (@kangclzjc), Anish Maddipoti (amaddipoti@nvidia.com) |
| **Created**     | 2026-06-05                                                       |
| **Reference UI/UX**  | Appendix A (non-normative) |

## Summary

Dynamo's Kubernetes operator exposes its deployment surface — `DynamoGraphDeployment`
(DGD), `DynamoGraphDeploymentRequest` (DGDR), `DynamoModel` (DM), and
`DynamoComponentDeployment` (DCD) — **only** through the Kubernetes API (`kubectl apply`,
client-go, Helm). There is no language-agnostic, OpenAPI-documented, auth-friendly HTTP
surface for *creating, reading, updating, deleting, and observing* these resources.

This DEP proposes **Dynadmin** — the Dynamo control-plane API: a thin, stateless HTTP service
that fronts the existing CRDs with a versioned, OpenAPI-described contract, plus a **high-level
`deploy` endpoint** that translates a simplified, UI-friendly configuration into a fully-formed
DGD or DGDR custom resource. A reference **"Deploy a model" UI** in the NVIDIA visual style
(Appendix A) demonstrates the API and the intent → CR mapping end-to-end.

This is a proposal in the spirit of a Kubernetes KEP: it covers the motivation, user stories,
scope, and design. Detailed implementation (full endpoint schemas, error model, the served
OpenAPI document) is left to a follow-up design doc / the implementation PR. Appendix A is a
non-normative reference design for the UI and the field → CR mapping.

## Motivation

### Today: every consumer re-implements the same Kubernetes glue

The operator is 100% Kubernetes-API native. Verified against
[`deploy/operator/cmd/main.go`](../../deploy/operator/cmd/main.go), the operator process
runs only a webhook server, a metrics server, and health probes — **there is no management
HTTP/REST listener**. Anyone building a product *on top* of Dynamo — an internal deploy
portal, a multi-tenant inference-as-a-service platform, a CI/CD pipeline, a thin CLI — must:

1. **Embed client-go (or a kube client in their language).** Non-Go ecosystems (TypeScript
   front-ends, Python automation, Java services) have no first-class path and fall back to
   shelling out to `kubectl` or hand-rolled API calls.
2. **Own a kubeconfig / broad in-cluster RBAC** with create/patch/delete rights on
   `nvidia.com` resources, and re-solve multi-tenancy (namespace scoping, per-tenant quota,
   audit) themselves.
3. **Re-derive the intent → CR translation.** Turning "deploy Llama-3.1-70B on H200, vLLM,
   disaggregated, TTFT ≤ 300 ms" into a valid DGD/DGDR — choosing `components`, the
   `type: prefill`/`type: decode` split, worker `args` for TP/PP/dtype, and the
   `hardware`/`workload`/`sla` blocks — is non-trivial domain knowledge that currently
   lives only in tribal examples ([`examples/`](../../examples/), `recipes/`).
4. **Poll `kubectl get -o json`** and parse `status.conditions` / `status.phase` /
   `status.profilingPhase` by hand to drive a UI.

### Evidence: duplicated translation drifts from the schema

Because the intent → CR translation is copied into each consumer, every copy drifts from the
CRD as the schema evolves — and it does evolve. The deployment API has already moved from
`nvidia.com/v1alpha1` (a `spec.services:` **map**) to the now-served `nvidia.com/v1beta1`
(a `spec.components:` **list**), changed how GPUs are requested (`resources.limits."nvidia.com/gpu"`) and constrains GPU types
to a controlled `GPUSKUType` enum. Any hand-written client not kept in lockstep silently emits
stale or invalid manifests — the old `services` map, a wrong GPU resource key, or a GPU SKU
the CRD does not accept.

This is exactly the cost a native API removes: the translation belongs in **one**
authoritative place that tracks the CRD, not copied into every consumer.

### Goals

- A **versioned REST contract** (`/api/v1/…`) for the full lifecycle of Dynamo CRDs.
- A **canonical, server-side high-level `deploy` translation** so consumers stop
  re-implementing it.
- **First-class status/observability** (conditions, phase, replica counts, events).
- A **machine-readable OpenAPI 3.x spec** served by the API.
- **AuthN/AuthZ that maps cleanly onto Kubernetes RBAC** with namespace/tenant scoping,
  without distributing kubeconfigs.
- A **reference UI/UX design** demonstrating the deploy form and the CR mapping.

### Non-goals

- Replacing the Kubernetes API or the operator's reconcile loop — this is a *facade*, not a
  new control plane. The operator remains the source of truth.
- Replacing the inference data plane (OpenAI-compatible `/v1/chat/completions` via the
  Inference Gateway / frontend) — see [`docs/kubernetes/inference-gateway.md`](../kubernetes/inference-gateway.md).
  This DEP is about the **management/control** plane only.
- Building a billing/usage/identity product — that is what downstream platforms layer on top.

## Proposal

Introduce **Dynadmin**, a stateless control-plane REST service that fronts the Dynamo CRDs with
ordinary CRUD plus a high-level `deploy` endpoint that owns the intent → CR translation. Clients
(web UIs, CLIs, CI, SaaS platforms) speak HTTPS/JSON and authenticate with a bearer token;
Dynadmin authorizes every call through Kubernetes RBAC and applies changes to the cluster, while
the operator continues to reconcile exactly as it does today.

### User Stories

- **Story 1 — Self-service "Deploy a model" portal.** A platform team runs an internal portal.
  A user fills the deploy form (model, engine, hardware, SLA targets…) and the portal POSTs it
  to Dynadmin's `deploy` endpoint, which returns the rendered DGD/DGDR and creates it — no
  client-go, no hand-written CR templating. (See the reference UI in Appendix A.)
- **Story 2 — Multi-tenant inference service.** A SaaS gives each tenant a namespace. Tenants
  create, list, update, and delete *their own* deployments through Dynadmin under their own
  OIDC identity; Kubernetes RBAC is enforced per request and no kubeconfig is ever handed out.
- **Story 3 — CI / CLI / non-Go automation.** A CI pipeline (Python / TypeScript / bash +
  `curl`) creates a deployment and polls its status over REST. In pull requests it calls
  `dryRun=client` to render and review the resulting manifest **without a cluster**.
- **Story 4 — Observe and scale.** An ops dashboard reads a deployment's status (phase,
  conditions, replica counts) and triggers a `scale` action on a worker component — all through
  Dynadmin, without direct cluster access.

### Scope

**In scope (v1):**

- CRUD + status read for DGD and DGDR; CRUD for DM (model / LoRA registry); read-only for DCD;
  read-only + a `scale` action for scaling adapters.
- The high-level `deploy` translation (flat config → DGD or DGDR) with a two-level dry-run.
- Status, events, a `scale` action, and YAML/manifest export; a served OpenAPI 3.x document.
- AuthN/AuthZ mapped onto Kubernetes RBAC (user impersonation), with namespace/tenant scoping.

**Deferred / out of scope:**

- The advanced CR surface (`epp`, raw planner config, `topologyConstraint`, checkpoints,
  GMS/failover) — reachable via raw CRUD as an escape hatch, not modeled in the high-level form
  for v1.
- Streaming status (Server-Sent Events) and the aggregated-apiserver topology — Phase 2.
- Everything under Non-goals (kube-API replacement, the data plane, billing/identity).

## Design Details

### Architecture

```
            ┌────────────────────────┐        HTTPS / JSON (OpenAPI 3.x)
   Web UI ──┤                        │◄───────────────────────────── CLI / CI / SaaS
            │        Dynadmin        │
  (React)   │   (stateless facade)   │  AuthN: bearer/OIDC   AuthZ: K8s RBAC (impersonation)
            └───────────┬────────────┘
                        │ kube-apiserver REST (served versions, Server-Side Apply)
                        ▼
                ┌───────────────┐   watch / reconcile  ┌──────────────────┐
                │  kube-apiserver│◄───────────────────►│ Dynamo Operator  │
                │  (CRDs + conv. │                      │ (controllers)    │
                │   webhook)     │                      └──────────────────┘
                └───────────────┘
```

**Dynadmin** is **stateless**: it holds no database, derives all state from the Kubernetes API,
and scales horizontally. It is the single place where the intent → CR translation lives. The
operator's reconcile loop is unchanged and remains the source of truth.

### API surface

Dynadmin fronts the CRDs with namespace-scoped REST resources (`/api/v1/namespaces/{ns}/…`),
targeting each CRD's **served** version (the conversion webhook bridges to the storage version):

| Resource             | CRD (kind)                              | Served apiVersion         | Verbs        |
| -------------------- | --------------------------------------- | ------------------------- | ------------ |
| Deployments          | `DynamoGraphDeployment`                 | `nvidia.com/v1beta1`      | CRUD         |
| Deployment Requests  | `DynamoGraphDeploymentRequest`          | `nvidia.com/v1beta1`      | CRUD         |
| Models               | `DynamoModel`                           | **`nvidia.com/v1alpha1`** | CRUD (registry: LoRA/adapter + endpoint discovery — **not** a deploy target) |
| Components           | `DynamoComponentDeployment`             | `nvidia.com/v1beta1`      | read-only    |
| Scaling Adapters     | `DynamoGraphDeploymentScalingAdapter`   | `nvidia.com/v1beta1`      | read-only (scale via the deployments `scale` action) |

Each resource gets the usual collection/item routes plus a few subresources:

```
GET  | POST                 /api/v1/namespaces/{ns}/deployments
GET  | PUT | PATCH | DELETE  /api/v1/namespaces/{ns}/deployments/{name}
GET                          /api/v1/namespaces/{ns}/deployments/{name}/{status | events | manifest}
POST                         /api/v1/namespaces/{ns}/deployments/{name}/scale
POST                         /api/v1/namespaces/{ns}/deploy            # high-level translate + create
```

The headline is the high-level **`deploy`** endpoint: it accepts a flat, UI-friendly
configuration and emits a complete **DGD** (direct) or **DGDR** (SLA / auto-optimized) — the
single, canonical home for the intent → CR translation consumers re-implement today. It supports
a two-level dry-run: **`dryRun=client`** (render-only — translate and return the manifest, no
cluster contact, so it works with no cluster at all) and **`dryRun=server`** (a Kubernetes
`dryRun=All` apply for real admission/CEL validation without persisting; this relies on the
operator's admission webhooks being dry-run-safe, which they are today).

A few design points fall out of this:

- **One translation, tested.** The intent → CR translation lives only in Dynadmin and is
  unit-tested against the served CRD schema, so it cannot drift the way per-platform copies do.
- **Validation up front.** Enum fields (GPU SKU, backend, search strategy, component type) are
  checked against the CRD's own enums and returned as structured per-field errors (RFC 7807
  style) before anything reaches the cluster.
- **Scale safely.** The `scale` action drives a component's scaling adapter when present; if a
  planner owns the replicas and there is no adapter, it refuses (`409`) rather than issuing a
  `replicas` patch the planner would immediately revert.
- **Disaggregation correctness.** When the form requests prefill/decode separation, the
  translation emits a working disaggregated graph for the target engine — including the engine's
  prefill/decode launch flag and the mandatory KV-transfer connector config — tracking the
  engine's current convention (see Appendix A).
- **OpenAPI.** Full request/response schemas, the complete route list, and the error model are
  published in the served OpenAPI 3.x document and the follow-up design doc.

### Authentication & authorization

Authentication is a bearer token (a Kubernetes ServiceAccount token or an OIDC id-token); the
service holds no identity store and **issues no kubeconfigs**. Authorization is delegated to
**Kubernetes RBAC**: the recommended model is **user impersonation** — the gateway acts as the
end user, so RBAC and the Kubernetes audit log stay authoritative — with token-forwarding as an
in-cluster opt-in. Tenants map to namespace(s) via OIDC claims and `RoleBinding`s; quota is
delegated to Kubernetes `ResourceQuota`. Secrets (HuggingFace token, image-pull) are referenced
**by name** and never carried in request bodies. The full impersonation vs token-forwarding
trade-off and multi-tenancy detail are deferred to the follow-up design doc.

### Deployment topology — alternatives & recommendation

| Criterion                         | (a) Standalone gateway        | (b) Aggregated API server        | (c) Operator HTTP sidecar     |
| --------------------------------- | ----------------------------- | -------------------------------- | ----------------------------- |
| Implementation effort             | **Low** (HTTP + kube client)  | High (apiserver-builder, etcd registry, APIService registration) | Low-medium |
| K8s-nativeness (kubectl/RBAC/audit)| Medium (RBAC via impersonation)| **High** (native verbs/RBAC/audit) | Medium |
| Independent scaling               | **Yes** (HPA the gateway)     | Coupled to apiserver SLOs        | **No** (scales the controller) |
| Release coupling                  | **Decoupled** from operator   | Coupled to k8s apiserver matrix  | **Tight** to operator release |
| Blast radius                      | **Isolated** from reconcile   | A flaky APIService can wedge cluster-wide discovery | **High** — can crash reconcile |
| Local / CI render-only mode       | **Easy** (no cluster)         | Hard                             | Hard |

**Recommendation & phasing:**

- **Phase 1 (now): (a) standalone stateless gateway** calling the kube-apiserver. Ships
  fastest, isolates blast radius from the reconcile loop (the operator runs only
  webhook/metrics/health listeners today — adding request-serving there is rejected), runs
  render-only in CI, and scales independently. SSE/watch streaming is deferred to Phase 2.
- **Phase 2 (optional): (b) aggregated API server** under a reserved `deploy.nvidia.com` group,
  if/when first-class `kubectl` verbs and native audit become hard requirements.
- **(c) is rejected**: conflating control-loop and request-serving concerns enlarges the
  operator's blast radius for no infrastructure savings.

### Reference UI

A reference single-page **"Deploy a model"** UI in the NVIDIA visual style demonstrates the API
and the intent → CR mapping end-to-end: a mode toggle (DGDR vs DGD), engine / hardware /
parallelism / scaling / SLA / disaggregation / advanced sections, and a live YAML preview. The
design tokens, page wireframe, field-visibility-by-mode, and the full **form-field → CR mapping
table** are in Appendix A. (Proposed design; not implemented in this DEP.)

![Reference "Deploy a model" form (illustrative prototype)](images/dynadmin-deploy-form.png)

*Illustrative prototype of the "Deploy a model" form. The proposed design tracks the canonical
CRD schema (see Appendix A): a `GPUSKUType`-driven GPU dropdown, the NVIDIA accent, and the
v1beta1 `components` mapping.*

## Risks / Open Questions

1. **Schema drift (the very problem this solves).** A hand-written translation can itself drift
   from the CRD as the schema evolves. How is it kept in lockstep — generated from the CRD OpenAPI in `config/crd/bases/`,
   or a contract test that round-trips against the operator's validation webhook?
2. **Storage-version churn.** Storage versions differ today (DGDR=v1beta1; DGD/DCD/DM=v1alpha1)
   and will flip later. Does the `/api/v1` contract truly insulate clients when the storage
   version changes and the conversion webhook is in the path?
3. **Engine-specific args.** `--max-num-seqs`, `--is-prefill-worker`, `--dtype` are vLLM-isms;
   SGLang/TRT-LLM differ. The server owns a per-backend flag map — who maintains it as engines
   evolve?
4. **Overlap with DGDR's existing intent layer.** DGDR is already a deploy-by-intent API. Is the
   DGD path of `/deploy` re-deriving what the profiler does (hand-picked TP/PP), and is that the
   right trade-off?
5. **Impersonation privilege.** A gateway SA with broad `impersonate` is a high-value target;
   reviewers will want `resourceName` scoping, NetworkPolicy, and audit review.
6. **Scale semantics (resolved in Design Details).** When no scaling adapter exists and a planner
   owns the component, the `scale` action refuses (`409`) rather than racing; the open follow-up
   is how to detect "a planner owns this component" reliably.
7. **Lossy coverage.** The form covers frontend/worker/prefill/decode but not `epp`, raw planner
   config (`spec.features.planner`), `topologyConstraint`, `compilationCache`, or GMS/failover.
   Is a lossy subset acceptable, with an escape hatch to raw CRUD for advanced cases?
8. **SSE fan-out / resume (Phase 2).** Per-object watches don't scale; the Phase-2 design uses a
   shared informer/relay with `resourceVersion` resume — to be validated under load.
9. **`DynamoModel` confusion.** DM is a registry/LoRA resource, not a deploy target. The UI must
   not conflate "register a model" with "deploy a model."
10. **Ownership.** Where does the reference service live (operator module vs separate module),
    who owns its release cadence, and does it import the operator's Go types or stay on
    `unstructured` to avoid a compile-time coupling?

## Alternate Solutions

- **Status quo (client-go / kubectl / Helm).** Works for Go and ops users; fails the non-Go,
  web-UI, and multi-tenant SaaS cases and forces translation duplication.
- **Generic Kubernetes dashboards** (Headlamp, Lens, k8s-dashboard). Show raw CRs but have no
  Dynamo domain model, no "deploy a model" intent layer, and no SLA/profiling workflow.
- **Per-platform bridges.** Each platform re-implements a client-side translator plus a kube
  client. Proven but duplicated per platform and prone to schema drift — exactly the cost this
  DEP removes.
- **GAIE / Inference Gateway.** Solves the *data* plane (OpenAI-compatible inference), not CRD
  lifecycle management.

## References

- DGD types: [`deploy/operator/api/v1beta1/dynamographdeployment_types.go`](../../deploy/operator/api/v1beta1/dynamographdeployment_types.go)
- DGDR types: [`deploy/operator/api/v1beta1/dynamographdeploymentrequest_types.go`](../../deploy/operator/api/v1beta1/dynamographdeploymentrequest_types.go)
- Component shared spec + `ComponentType` enum: [`deploy/operator/api/v1beta1/dynamocomponentdeployment_types.go`](../../deploy/operator/api/v1beta1/dynamocomponentdeployment_types.go), [`deploy/operator/api/v1beta1/common.go`](../../deploy/operator/api/v1beta1/common.go)
- DynamoModel (v1alpha1-only): [`deploy/operator/api/v1alpha1/dynamo_model_types.go`](../../deploy/operator/api/v1alpha1/dynamo_model_types.go)
- Scaling adapter (scale subresource): [`deploy/operator/api/v1beta1/dynamographdeploymentscalingadapter_types.go`](../../deploy/operator/api/v1beta1/dynamographdeploymentscalingadapter_types.go)
- Generated CRD OpenAPI: [`deploy/operator/config/crd/bases/`](../../deploy/operator/config/crd/bases/)
- CRD API reference docs: [`docs/kubernetes/api-reference.md`](../kubernetes/api-reference.md)
- Inference Gateway (data plane, for contrast): [`docs/kubernetes/inference-gateway.md`](../kubernetes/inference-gateway.md)
- Canonical v1beta1 example (worker args, GPU limits, HF/imagePullSecret patterns): [`examples/global_planner/v1beta1/global-planner-vllm-test.yaml`](../../examples/global_planner/v1beta1/global-planner-vllm-test.yaml)
- DEP issue template (Area options): [`.github/ISSUE_TEMPLATE/dep.yml`](../../.github/ISSUE_TEMPLATE/dep.yml)

---

## Appendix A — Reference UI/UX (non-normative)

> This appendix is **non-normative**: a reference design for the "Deploy a model" UI and the
> form-field → CR mapping that the high-level `deploy` endpoint produces. The normative
> proposal is above.

A reference single-page "Deploy a model" app demonstrates the API in the **NVIDIA visual
style**. (Proposed design; not implemented in this DEP.)

**Design tokens** (consistent with NVIDIA branding):

```
accent (NVIDIA green) #76b900   accent hover #69a600   accent bg rgba(118,185,0,0.08)
sidebar #1a1a1a   content bg #f7f8f9   surface #ffffff   border #e0e2e6
text #1a1a1a / #5a5e66 / #8c9099   radius 6px / 10px   font Inter, system-ui
```

**Form field visibility by mode:**

| Section        | Field                                          | DGDR | DGD |
| -------------- | ---------------------------------------------- | :--: | :-: |
| Mode           | Auto-Optimized (DGDR) vs Direct Deploy (DGD)    | ✓ | ✓ |
| Identity       | name                                            | ✓ | ✓ |
| Engine         | backend (vLLM/SGLang/TRT-LLM) + image override  | ✓ | ✓ |
| Hardware       | GPU type (16-value enum), GPUs/replica          | ✓ | ✓ |
| Hardware       | GPUs/node                                       | ✓ | advisory |
| Parallelism    | TP, PP                                          | profiler-derived* | ✓ |
| Scaling        | replicas (DGD) / min,max (DGDR, planner-opaque) | ✓ | ✓ |
| SLA + workload | TTFT, ITL, E2E, ISL, OSL, searchStrategy, autoApply | ✓ | — |
| Disaggregation | enable + prefill/decode replicas                | advisory | ✓ |
| Advanced       | maxBatch, maxSeq, dtype, routerMode, extraArgs, envVars | limited | ✓ |

\* In DGDR mode the profiler normally chooses TP/PP; expose them only as optional `overrides`.

**Wireframe:**

```
┌──────────────┬───────────────────────────────────────┬───────────────────────────┐
│  ▆ DYNAMO    │  Deploy a model                        │  Live preview (YAML)      │
│ (#1a1a1a)    │  Configure an NVIDIA Dynamo deployment.│ ┌───────────────────────┐ │
│              │  ┌─ Mode ─────────────────────────┐    │ │apiVersion: nvidia.com │ │
│ ▸ Models     │  │ [🔬 Auto-Optimized (DGDR)]      │    │ │  /v1beta1             │ │
│ ▸ Deployments│  │ [⚡ Direct Deploy (DGD)   ]      │    │ │kind: DynamoGraph...   │ │
│ ▸ Requests   │  └─────────────────────────────────┘    │ │metadata:              │ │
│ ▸ Components │  Name   [ llama3-70b-prod           ]   │ │  name: llama3-70b-... │ │
│ ▸ Models(DM) │  Engine ( vLLM ) ( SGLang ) ( TRT-LLM ) │ │spec:                  │ │
│              │  Hardware  GPU [h200_sxm▾] /rep[4]/node[8] │ │  model: meta-llama/..│ │
│              │  Parallelism  TP[1|2|4|8]  PP[1|2|4]    │ │  backend: vllm        │ │
│              │  Scaling   min[1]  max[4]               │ │  hardware:            │ │
│              │  ── SLA & Workload (DGDR only) ──       │ │    gpuSku: h200_sxm   │ │
│              │   TTFT[300] ITL[20] ISL[4000] OSL[1000] │ │    numGpusPerNode: 8  │ │
│              │   Strategy (⚡ rapid)                    │ │  workload: {isl:4000} │ │
│              │  ── Disaggregated serving ──            │ │  sla: {ttft:300}      │ │
│              │   [x] prefill/decode   P[2]  D[2]       │ │  autoApply: true      │ │
│              │  ▸ Advanced (dtype, maxBatch, maxSeq…)  │ └───────────────────────┘ │
│              │              [ Cancel ]  [⚡ Deploy ]    │   (updates as you type)   │
└──────────────┴───────────────────────────────────────┴───────────────────────────┘
```

The accent color (`#76b900`) marks the active mode card, selected engine, selected TP/PP
chips, and the Deploy button (hover `#69a600`). The GPU dropdown is driven by the canonical
`GPUSKUType` enum (not a hardcoded list), eliminating the `a10g` drift.

**Form-field → CR mapping** (the heart of the translation; worker `args` are vLLM-canonical —
SGLang/TRT-LLM use different flag names, which the server keys off `backend`):

| Form field            | DGDR (`nvidia.com/v1beta1`)                         | DGD (`nvidia.com/v1beta1`)                                          |
| --------------------- | --------------------------------------------------- | ------------------------------------------------------------------ |
| name                  | `metadata.name`                                     | `metadata.name`                                                    |
| model                 | `spec.model`                                        | worker arg `--model <v>`                                           |
| backend               | `spec.backend` {auto,sglang,trtllm,vllm}            | `spec.backendFramework` {sglang,vllm,trtllm}                       |
| image                 | `spec.image`                                        | worker `podTemplate.spec.containers[name=main].image`              |
| gpuType               | `spec.hardware.gpuSku`                              | node selector / affinity (not a DGD spec field)                    |
| gpusPerReplica        | — (DGDR sizing is profiler-decided)                 | worker `…containers[main].resources.limits."nvidia.com/gpu"`       |
| gpusPerNode           | `spec.hardware.numGpusPerNode`                      | informs `multinode.nodeCount` when gpusPerReplica > gpusPerNode    |
| totalGpus             | `spec.hardware.totalGpus` (whole-deployment budget) | — (sum of component GPU limits)                                    |
| TP / PP               | optional `spec.overrides` (profiler derives)        | worker args `--tensor-parallel-size` / `--pipeline-parallel-size`  |
| replicas              | — (profiler/planner-decided)                        | `spec.components[i].replicas`                                      |
| replicasMin / Max     | opaque passthrough to `spec.features.planner` (not schema-validated — escape hatch) | autoscaling via DGDSA/HPA, not a DGD spec field |
| TTFT / ITL / E2E      | `spec.sla.{ttft,itl,e2eLatency}` (ms)               | —                                                                  |
| ISL / OSL             | `spec.workload.{isl,osl}`                           | —                                                                  |
| searchStrategy        | `spec.searchStrategy` {rapid,thorough}              | —                                                                  |
| autoApply             | `spec.autoApply`                                    | —                                                                  |
| **disaggEnabled=true**| advisory (profiler decides split)                   | emits a prefill + a decode component (`type: prefill`/`decode`), each with own `replicas` + GPU limits; each worker gets `--disaggregation-mode prefill\|decode` **and** the required `--kv-transfer-config '{"kv_connector":"NixlConnector",...}'` for prefill↔decode KV transfer |
| disaggEnabled=false   | —                                                   | single `{name:VllmWorker,type:worker}`                             |
| (always, DGD)         | —                                                   | also `{name:Frontend,type:frontend,replicas:frontendReplicas}`, `command:[python3,-m,dynamo.frontend]`, args incl. `--router-mode` |
| dtype                 | override only                                       | worker arg `--dtype <v>`                                           |
| maxBatchSize          | override only                                       | worker arg `--max-num-seqs <v>` (vLLM)                             |
| maxSeqLen             | override only                                       | worker arg `--max-model-len <v>` (vLLM)                            |
| routerMode            | —                                                   | Frontend arg `--router-mode <random\|kv\|round-robin>`             |
| extraArgs (k/v)       | `spec.overrides`                                    | appended to worker container `args`                                |
| envVars (k/v)         | —                                                   | `spec.env[]` (graph-level) or per-component `podTemplate…env[]`    |
| HF token (Secret name)| worker `envFrom.secretRef.name` (via overrides)     | container `envFrom[].secretRef.name`                               |
| imagePullSecret name  | overrides                                           | `podTemplate.spec.imagePullSecrets[].name`                         |

The valid component `type` values (`common.go`) are: `frontend, worker, prefill, decode,
planner, epp`. Disaggregation is the subtle part the server must own: the launch flags vary
across in-repo examples (`--disaggregation-mode prefill|decode` is the dominant convention;
`--is-prefill-worker`/`--is-decode-worker` is an alternative), and **both halves require a
`--kv-transfer-config` (NixlConnector) block** — omitting it yields a DGD that does not
actually disaggregate. Centralizing this engine-specific knowledge against the served v1beta1
`components` schema is precisely the value a single translation provides.
