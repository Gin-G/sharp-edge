{{/* Standard labels applied to every object */}}
{{- define "sharp-edge.labels" -}}
app: {{ .Values.app.name }}
group: {{ .Values.app.group }}
helm.sh/chart: {{ .Chart.Name }}-{{ .Chart.Version }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end -}}

{{/* CNPG cluster name + the secret it produces for the app role */}}
{{- define "sharp-edge.cnpgClusterName" -}}
{{ .Values.app.name }}-cnpg
{{- end -}}

{{- define "sharp-edge.cnpgAppSecret" -}}
{{ include "sharp-edge.cnpgClusterName" . }}-app
{{- end -}}
