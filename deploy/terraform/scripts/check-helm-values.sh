#!/usr/bin/env bash
# Render the GKE and EKS modules' Helm values with sample inputs, then lint and template the real
# chart with them, so drift between modules/helm-values, modules/aws-helm-values and
# deploy/helm/satchel fails CI.
# Needs terraform, helm and (optionally) kubeconform on PATH.
set -euo pipefail

here="$(cd "$(dirname "$0")/.." && pwd)"
chart="$here/../helm/satchel"
work="$(mktemp -d)"
trap 'rm -rf "$work"' EXIT

mkdir -p "$work/tests" "$work/modules"
for m in helm-values aws-helm-values; do
  cp -R "$here/tests/$m" "$work/tests/$m"
  cp -R "$here/modules/$m" "$work/modules/$m"
done

render() { # <test dir> <output name> <file>
  terraform -chdir="$work/tests/$1" output -raw "$2" > "$work/$3"
}

for m in helm-values aws-helm-values; do
  terraform -chdir="$work/tests/$m" init -backend=false -input=false >/dev/null
  terraform -chdir="$work/tests/$m" apply -auto-approve -input=false >/dev/null
done
render helm-values values_yaml gke.yaml
render aws-helm-values values_yaml_csi eks-csi.yaml
render aws-helm-values values_yaml_kubernetes eks-kubernetes.yaml

for v in gke eks-csi eks-kubernetes; do
  echo "--- rendered values: $v"
  cat "$work/$v.yaml"
  helm lint --strict "$chart" -f "$work/$v.yaml"
  helm template satchel "$chart" --namespace satchel -f "$work/$v.yaml" > "$work/rendered-$v.yaml"
  if command -v kubeconform >/dev/null 2>&1; then
    kubeconform -strict -ignore-missing-schemas -summary "$work/rendered-$v.yaml"
  else
    echo "kubeconform not found; skipping"
  fi
done
echo "helm values check OK"
