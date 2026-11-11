#!/usr/bin/env bash
# ──────────────────────────────────────────────────────────────
# deploy.sh — despliega procesar-receta en Cloud Run (buildpack/Procfile)
# Uso:   ./deploy.sh
# Override opcional: REGION=us-east1 SERVICE=otro ./deploy.sh
# Las variables de entorno ya configuradas en Cloud Run (DB_*, API_KEY,
# DASHBOARD_RECETAS_KEY, DANACONNECT_*, etc.) se conservan tal cual.
# ──────────────────────────────────────────────────────────────
set -euo pipefail

PROJECT_ID="${PROJECT_ID:-sanitas-recetas-prod}"
SERVICE="${SERVICE:-procesar-receta}"
REGION="${REGION:-us-central1}"

cd "$(dirname "$0")"

echo "▶ Verificando sintaxis de main.py..."
python3 -m py_compile main.py

echo "▶ Desplegando '${SERVICE}' en ${REGION} (proyecto ${PROJECT_ID})..."
gcloud run deploy "${SERVICE}" \
  --source . \
  --project "${PROJECT_ID}" \
  --region "${REGION}" \
  --quiet

URL="$(gcloud run services describe "${SERVICE}" \
  --project "${PROJECT_ID}" --region "${REGION}" \
  --format 'value(status.url)')"

echo "✅ Despliegue completado: ${URL}"
echo "   Dashboard: ${URL}/dashboard-recetas?key=<DASHBOARD_RECETAS_KEY>"
