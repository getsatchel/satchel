# floci-cloud-run

Satchel on Cloud Run against the [floci-gcp](https://github.com/floci-io/floci-gcp) emulator: the
`foundation` and `cloud-run` modules with every provider endpoint pointed at
`http://localhost:4588`, project `floci-local`, a dummy access token and
`runtime_profile = "emulator"`. **Not for real GCP**: secret values go into the service spec.

```
deploy/floci/up.sh                     # --cloudrun-mock for control plane only
terraform init
terraform apply -var deploy_service=false
../../../floci/run-job.sh satchel-migrate
../../../floci/run-job.sh satchel-migrate '["-c","satchel workspace create demo --name Demo --admin-email admin@example.com"]'
terraform apply
curl "$(terraform output -raw public_url)/readyz"
../../../floci/empty-bucket.sh floci-local-satchel-results floci-local-satchel-audit && terraform destroy
deploy/floci/down.sh
```

Needs the image `satchel:floci` (`docker build -f deploy/docker/Dockerfile --build-arg BAKE_EMBEDDINGS=false -t satchel:floci .`)
and Docker on the machine running Terraform (`cloudsql-shim.sh`). What is turned off and why:
[docs/deploy/floci.md](../../../../docs/deploy/floci.md). `scripts/floci-e2e.sh` runs all of it with assertions.
