# floci-gke

Satchel on GKE against the [floci-gcp](https://github.com/floci-io/floci-gcp) emulator. floci runs
the cluster as a real k3s container, so the `helm_release` really installs the chart: migration
hook (bootstrap + upgrade), first-workspace Job, web Deployment, worker CronJob. The Auth Proxy,
Gateway, Certificate Manager and Cloud NAT are turned off. **Not for real GCP.**

Two-step apply, as in production (the Kubernetes and Helm providers are configured from the
cluster), plus importing the local image into k3s:

```
deploy/floci/up.sh
terraform init
terraform apply -target=module.foundation -target=terraform_data.cloudsql_shim \
  -target=module.gke.google_container_cluster.this
../../../floci/k3s-load-image.sh satchel satchel:floci
ip=$(docker inspect -f '{{(index .NetworkSettings.Networks "floci-satchel").IPAddress}}' floci-satchel-gcp)
terraform apply -var storage_emulator_host=http://$ip:4588
kubectl config set-cluster floci --server=https://$(terraform output -raw cluster_endpoint) --insecure-skip-tls-verify=true
../../../floci/empty-bucket.sh floci-local-satchel-results floci-local-satchel-audit && terraform destroy
deploy/floci/down.sh
```

Details and the list of emulator gaps: [docs/deploy/floci.md](../../../../docs/deploy/floci.md).
