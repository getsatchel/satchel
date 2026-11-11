{{- define "satchel.name" -}}
{{- default .Chart.Name .Values.nameOverride | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{- define "satchel.fullname" -}}
{{- if .Values.fullnameOverride -}}
{{- .Values.fullnameOverride | trunc 63 | trimSuffix "-" -}}
{{- else -}}
{{- $name := default .Chart.Name .Values.nameOverride -}}
{{- if contains $name .Release.Name -}}
{{- .Release.Name | trunc 63 | trimSuffix "-" -}}
{{- else -}}
{{- printf "%s-%s" .Release.Name $name | trunc 63 | trimSuffix "-" -}}
{{- end -}}
{{- end -}}
{{- end -}}

{{- define "satchel.labels" -}}
helm.sh/chart: {{ printf "%s-%s" .Chart.Name .Chart.Version | replace "+" "_" | trunc 63 | trimSuffix "-" }}
{{ include "satchel.selectorLabels" . }}
app.kubernetes.io/version: {{ .Values.image.tag | default .Chart.AppVersion | quote }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
{{- end -}}

{{- define "satchel.selectorLabels" -}}
app.kubernetes.io/name: {{ include "satchel.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end -}}

{{- define "satchel.serviceAccountName" -}}
{{- if .Values.serviceAccount.create -}}
{{- default (include "satchel.fullname" .) .Values.serviceAccount.name -}}
{{- else -}}
{{- default "default" .Values.serviceAccount.name -}}
{{- end -}}
{{- end -}}

{{/* Service account of the migrate Job: its own when serviceAccount.migration.create, else the runtime one. */}}
{{- define "satchel.migrationServiceAccountName" -}}
{{- if .Values.serviceAccount.migration.create -}}
{{- default (printf "%s-migrate" (include "satchel.fullname" .)) .Values.serviceAccount.migration.name -}}
{{- else -}}
{{- include "satchel.serviceAccountName" . -}}
{{- end -}}
{{- end -}}

{{- define "satchel.image" -}}
{{- printf "%s:%s" .Values.image.repository (default .Chart.AppVersion .Values.image.tag) -}}
{{- end -}}

{{/* Hook annotations for resources the pre-install migration Job depends on. Usage: include "satchel.preHook" (dict "weight" "-10") */}}
{{- define "satchel.preHook" -}}
"helm.sh/hook": pre-install,pre-upgrade
"helm.sh/hook-weight": {{ .weight | quote }}
"helm.sh/hook-delete-policy": {{ .policy | default "before-hook-creation" }}
{{- end -}}

{{/* "true" when resultDir is an object store URI (gs:// or s3://), shared by every replica. */}}
{{- define "satchel.objectResultDir" -}}
{{- if or (hasPrefix "gs://" .Values.resultDir) (hasPrefix "s3://" .Values.resultDir) -}}true{{- end -}}
{{- end -}}

{{/* CSI driver for secretManager: the GKE-managed driver, or the upstream driver with the AWS provider (ASCP). */}}
{{- define "satchel.csiDriver" -}}
{{- if eq .Values.secretManager.provider "aws" -}}secrets-store.csi.k8s.io{{- else -}}secrets-store-gke.csi.k8s.io{{- end -}}
{{- end -}}

{{- define "satchel.devSecretName" -}}{{ include "satchel.fullname" . }}-secrets{{- end -}}
{{- define "satchel.devPgHost" -}}{{ include "satchel.fullname" . }}-postgres{{- end -}}

{{/* Name of the Secret holding runtime keys. */}}
{{- define "satchel.secretName" -}}
{{- if .Values.secretManager.enabled -}}{{ include "satchel.fullname" . }}-sm
{{- else if or .Values.secrets.create .Values.devPostgres.enabled -}}{{ include "satchel.devSecretName" . }}
{{- else -}}{{ .Values.existingSecret }}
{{- end -}}
{{- end -}}

{{- define "satchel.bootstrapEnabled" -}}
{{- if or .Values.bootstrap.enabled .Values.devPostgres.enabled -}}true{{- end -}}
{{- end -}}

{{/* Name of the Secret holding bootstrap keys. */}}
{{- define "satchel.bootstrapSecretName" -}}
{{- if .Values.devPostgres.enabled -}}{{ include "satchel.devSecretName" . }}
{{- else if .Values.secretManager.enabled -}}{{ include "satchel.fullname" . }}-sm-bootstrap
{{- else -}}{{ .Values.bootstrap.existingSecret }}
{{- end -}}
{{- end -}}

{{/* One secretKeyRef env var. dict: name, secret, optional */}}
{{- define "satchel.secretEnv" -}}
- name: {{ .name }}
  valueFrom:
    secretKeyRef:
      name: {{ .secret }}
      key: {{ .name }}
      {{- if .optional }}
      optional: true
      {{- end }}
{{- end -}}

{{/* Runtime secret env, selected by role: web | worker | migration */}}
{{- define "satchel.runtimeSecretEnv" -}}
{{- $s := include "satchel.secretName" .root -}}
{{- if eq .role "migration" }}
{{- $m := $s -}}
{{- if and .root.Values.secretManager.enabled (hasKey .root.Values.secretManager.migrationSecrets "SATCHEL_MIGRATION_DATABASE_URL") -}}
{{- $m = printf "%s-sm-migrate" (include "satchel.fullname" .root) -}}
{{- end }}
{{ include "satchel.secretEnv" (dict "name" "SATCHEL_MIGRATION_DATABASE_URL" "secret" $m) }}
{{- else }}
{{ include "satchel.secretEnv" (dict "name" "SATCHEL_DATABASE_URL" "secret" $s) }}
{{ include "satchel.secretEnv" (dict "name" "SATCHEL_ADMIN_DATABASE_URL" "secret" $s) }}
{{ include "satchel.secretEnv" (dict "name" "SATCHEL_OIDC_CLIENT_SECRET" "secret" $s "optional" true) }}
{{ include "satchel.secretEnv" (dict "name" "SATCHEL_CHAT_API_KEY" "secret" $s "optional" true) }}
{{ include "satchel.secretEnv" (dict "name" "SATCHEL_ENCRYPTION_KEY" "secret" $s "optional" true) }}
{{- end }}
{{- end -}}

{{/* Cloud SQL proxy native sidecar (an initContainer). */}}
{{- define "satchel.cloudSqlProxy" -}}
{{- with .Values.cloudSqlProxy }}
- name: cloud-sql-proxy
  image: {{ .image }}
  restartPolicy: Always
  args:
    - "--address=127.0.0.1"
    - "--port={{ .port }}"
    - "--structured-logs"
    - "--health-check"
    - "--http-port=9091"
    - "--exit-zero-on-sigterm"
    {{- if .privateIp }}
    - "--private-ip"
    {{- end }}
    {{- if .autoIamAuthn }}
    - "--auto-iam-authn"
    {{- end }}
    - {{ .instanceConnectionName | quote }}
  startupProbe:
    httpGet: {path: /startup, port: 9091}
    periodSeconds: 1
    failureThreshold: 60
  readinessProbe:
    httpGet: {path: /readiness, port: 9091}
    periodSeconds: 10
  securityContext:
    allowPrivilegeEscalation: false
    readOnlyRootFilesystem: true
    runAsNonRoot: true
    runAsUser: 10001
    capabilities: {drop: [ALL]}
    seccompProfile: {type: RuntimeDefault}
  resources:
    {{- toYaml .resources | nindent 4 }}
{{- end }}
{{- end -}}

{{- define "satchel.containerSecurityContext" -}}
allowPrivilegeEscalation: false
readOnlyRootFilesystem: true
runAsNonRoot: true
runAsUser: 10001
runAsGroup: 10001
capabilities:
  drop: [ALL]
seccompProfile:
  type: RuntimeDefault
{{- end -}}

{{- define "satchel.podSecurityContext" -}}
runAsNonRoot: true
runAsUser: 10001
runAsGroup: 10001
fsGroup: 10001
seccompProfile:
  type: RuntimeDefault
{{- end -}}

{{/* Common container env (ConfigMap) */}}
{{- define "satchel.envFrom" -}}
- configMapRef:
    name: {{ include "satchel.fullname" . }}
{{- end -}}

{{/* Shared volumes. dict: root, results, sm, smBootstrap (bools) */}}
{{- define "satchel.volumes" -}}
- name: tmp
  emptyDir: {}
{{- if and .results (not (include "satchel.objectResultDir" .root)) }}
- name: results
  emptyDir: {}
{{- end }}
{{- if and .sm .root.Values.secretManager.enabled }}
- name: sm
  csi:
    driver: {{ include "satchel.csiDriver" .root }}
    readOnly: true
    volumeAttributes:
      secretProviderClass: {{ include "satchel.fullname" .root }}-sm
{{- end }}
{{- if and .smMigrate .root.Values.secretManager.enabled .root.Values.secretManager.migrationSecrets }}
- name: sm-migrate
  csi:
    driver: {{ include "satchel.csiDriver" .root }}
    readOnly: true
    volumeAttributes:
      secretProviderClass: {{ include "satchel.fullname" .root }}-sm-migrate
{{- end }}
{{- if and .smBootstrap .root.Values.secretManager.enabled .root.Values.secretManager.bootstrapSecrets }}
- name: sm-bootstrap
  csi:
    driver: {{ include "satchel.csiDriver" .root }}
    readOnly: true
    volumeAttributes:
      secretProviderClass: {{ include "satchel.fullname" .root }}-sm-bootstrap
{{- end }}
{{- end -}}

{{- define "satchel.volumeMounts" -}}
- name: tmp
  mountPath: /tmp
{{- if and .results (not (include "satchel.objectResultDir" .root)) }}
- name: results
  mountPath: {{ .root.Values.resultDir }}
{{- end }}
{{- if and .sm .root.Values.secretManager.enabled }}
- name: sm
  mountPath: /mnt/secrets
  readOnly: true
{{- end }}
{{- if and .smMigrate .root.Values.secretManager.enabled .root.Values.secretManager.migrationSecrets }}
- name: sm-migrate
  mountPath: /mnt/secrets-migrate
  readOnly: true
{{- end }}
{{- if and .smBootstrap .root.Values.secretManager.enabled .root.Values.secretManager.bootstrapSecrets }}
- name: sm-bootstrap
  mountPath: /mnt/secrets-bootstrap
  readOnly: true
{{- end }}
{{- end -}}

{{/* Validation. Rendered from configmap.yaml so lint/template fail fast. */}}
{{- define "satchel.validate" -}}
{{- $multi := or (gt (int .Values.web.replicaCount) 1) .Values.autoscaling.enabled -}}
{{- if and $multi (not (include "satchel.objectResultDir" .)) -}}
{{- fail "resultDir must be gs://bucket/prefix or s3://bucket/prefix when web.replicaCount > 1 or autoscaling.enabled (get_result is read back on any replica)" -}}
{{- end -}}
{{- $sources := len (compact (list (and .Values.existingSecret "x") (and .Values.secrets.create "x") (and .Values.secretManager.enabled "x") (and .Values.devPostgres.enabled "x"))) -}}
{{- if eq $sources 0 -}}
{{- fail "no secret source: set existingSecret, secrets.create (dev), secretManager.enabled or devPostgres.enabled" -}}
{{- end -}}
{{- if and .Values.secretManager.enabled (hasKey .Values.secretManager.secrets "SATCHEL_MIGRATION_DATABASE_URL") (hasKey .Values.secretManager.migrationSecrets "SATCHEL_MIGRATION_DATABASE_URL") -}}
{{- fail "SATCHEL_MIGRATION_DATABASE_URL belongs in secretManager.secrets or secretManager.migrationSecrets, not both" -}}
{{- end -}}
{{- if and .Values.secretManager.enabled (not .Values.secretManager.secrets) -}}
{{- fail "secretManager.enabled requires secretManager.secrets" -}}
{{- end -}}
{{- if and .Values.bootstrap.enabled (not .Values.devPostgres.enabled) (not .Values.secretManager.enabled) (not .Values.bootstrap.existingSecret) -}}
{{- fail "bootstrap.enabled requires bootstrap.existingSecret (or secretManager.bootstrapSecrets)" -}}
{{- end -}}
{{- if and .Values.cloudSqlProxy.enabled .Values.aws.enabled -}}
{{- fail "cloudSqlProxy is for Cloud SQL; on AWS point the DB URLs at RDS directly" -}}
{{- end -}}
{{- if and .Values.gke.enabled .Values.aws.enabled -}}
{{- fail "gke.enabled and aws.enabled are mutually exclusive" -}}
{{- end -}}
{{- if and .Values.cloudSqlProxy.enabled (not .Values.cloudSqlProxy.instanceConnectionName) -}}
{{- fail "cloudSqlProxy.enabled requires cloudSqlProxy.instanceConnectionName" -}}
{{- end -}}
{{- if and .Values.firstWorkspace.enabled (not (and .Values.firstWorkspace.slug .Values.firstWorkspace.adminEmail)) -}}
{{- fail "firstWorkspace.enabled requires slug and adminEmail" -}}
{{- end -}}
{{- if and (ne .Values.exposure.type "none") (not .Values.exposure.host) -}}
{{- fail "exposure.host is required when exposure.type is gateway or ingress" -}}
{{- end -}}
{{- if and (eq .Values.exposure.type "gateway") (not .Values.gateway.create) (not .Values.gateway.parentRefs) -}}
{{- fail "exposure.type=gateway needs gateway.parentRefs or gateway.create" -}}
{{- end -}}
{{- if lt (int .Values.exposure.timeoutSec) 300 -}}
{{- fail "exposure.timeoutSec must be >= 300 (MCP SSE)" -}}
{{- end -}}
{{- end -}}
