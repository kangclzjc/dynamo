# Shared PodGroup for Prefill and Decode Components

## Overview

The shared PodGroup feature allows prefill and decode components to share a single Volcano PodGroup instead of creating separate PodGroups for each component. This can improve resource utilization and scheduling efficiency in disaggregated serving scenarios.

## How it Works

When the `nvidia.com/shared-podgroup: "true"` annotation is set on a DynamoComponentDeployment:

1. **Component Detection**: The system automatically detects if the component is a prefill or decode component by checking if the component name contains "prefill" or "decode" (case-insensitive).

2. **Shared PodGroup Name**: Instead of creating individual PodGroups for each component, a shared PodGroup is created with a name derived from the base component name.

3. **Combined Resource Requirements**: The shared PodGroup's `MinMember` is calculated by summing the actual `lws-size` values of all components that share the same PodGroup (prefill + decode + any other related components).

4. **Component Labeling**: Pods are labeled with `component-type: prefill` or `component-type: decode` to distinguish between them within the shared PodGroup.

## Configuration

### Enable Shared PodGroup

Add the following annotation to your DynamoComponentDeployment:

```yaml
metadata:
  annotations:
    nvidia.com/shared-podgroup: "true"
```

### Example Configuration

```yaml
apiVersion: v1alpha1.nvidia.com
kind: DynamoComponentDeployment
metadata:
  name: vllm-prefill-worker
  namespace: default
  annotations:
    nvidia.com/deployment-type: "leader-worker"
    nvidia.com/lws-size: "4"
    nvidia.com/shared-podgroup: "true"  # Enable shared pod group
spec:
  dynamoComponent: "vllm-prefill-worker:latest"
  dynamoTag: "VllmPrefillWorker"
  replicas: 2
  resources:
    limits:
      gpu: "1"
      cpu: "10"
      memory: "20Gi"
---
apiVersion: v1alpha1.nvidia.com
kind: DynamoComponentDeployment
metadata:
  name: vllm-decode-worker
  namespace: default
  annotations:
    nvidia.com/deployment-type: "leader-worker"
    nvidia.com/lws-size: "4"
    nvidia.com/shared-podgroup: "true"  # Enable shared pod group
spec:
  dynamoComponent: "vllm-decode-worker:latest"
  dynamoTag: "VllmDecodeWorker"
  replicas: 2
  resources:
    limits:
      gpu: "1"
      cpu: "10"
      memory: "20Gi"
```

## Generated Resources

With shared PodGroup enabled, the following resources will be created:

### PodGroup
- **Name**: `{base-component-name}-shared` (e.g., `vllm-shared`)
- **MinMember**: Sum of all `lws-size` values for components sharing the PodGroup (e.g., 10 for prefill lws-size=6 + decode lws-size=4)
- **Labels**: 
  - `shared-podgroup: "true"`
  - `component-type: "prefill"` or `component-type: "decode"`
  - `base-component: "{base-component-name}"`

### LeaderWorkerSets
- **Prefill**: `{base-component-name}-shared-prefill-{instance-id}`
- **Decode**: `{base-component-name}-shared-decode-{instance-id}`

## Component Name Mapping

The system automatically maps component names to shared PodGroup names:

| Component Name | Base Component | Shared PodGroup Name |
|----------------|----------------|---------------------|
| `vllm-prefill-worker` | `vllm` | `vllm-shared` |
| `vllm-decode-worker` | `vllm` | `vllm-shared` |
| `prefill-worker` | `worker` | `worker-shared` |
| `decode-worker` | `worker` | `worker-shared` |

## Benefits

1. **Improved Resource Utilization**: Shared PodGroups can better utilize cluster resources by allowing Volcano to schedule prefill and decode pods together.

2. **Reduced Scheduling Overhead**: Fewer PodGroups mean less scheduling complexity.

3. **Better Resource Allocation**: Volcano can make better decisions about resource allocation when it sees the combined requirements of both components.

4. **Co-location**: Prefill and decode pods are more likely to be scheduled on the same nodes, reducing network latency.

## Limitations

1. **Component Name Requirements**: The feature only works for components whose names contain "prefill" or "decode".

2. **Dynamic Size Calculation**: The system automatically calculates the total `MinMember` by summing the actual `lws-size` values of all components sharing the same PodGroup.

3. **Shared Cleanup**: When using shared PodGroups, individual PodGroups are not deleted during cleanup to avoid affecting other components.

## Troubleshooting

### Check PodGroup Status

```bash
kubectl get podgroups -n <namespace>
```

### Check Component Labels

```bash
kubectl get pods -n <namespace> -l shared-podgroup=true
```

### Verify Component Types

```bash
kubectl get pods -n <namespace> -l component-type=prefill
kubectl get pods -n <namespace> -l component-type=decode
```

### Check Base Component Labels

```bash
kubectl get pods -n <namespace> -l base-component=vllm
```

## Advanced Configuration

### Custom PodGroup Sizes

The PodGroup size is automatically calculated by summing the `lws-size` values of all components sharing the same PodGroup. This calculation is performed dynamically by the `calculateTotalLwsSizeForSharedPodGroup` function, which:

1. Finds all DynamoComponentDeployments in the same namespace
2. Identifies those with `nvidia.com/shared-podgroup: "true"`
3. Checks if they are prefill/decode components with the same base name
4. Sums their individual `lws-size` values
5. Uses the total as the PodGroup's `MinMember`

### Multiple Component Types

The system can be extended to support other component types beyond prefill and decode by modifying the `IsPrefillOrDecodeComponent` function.

### PodGroup Cleanup Strategy

For shared PodGroups, you might want to implement a more sophisticated cleanup strategy that considers whether other components are still using the shared PodGroup. 